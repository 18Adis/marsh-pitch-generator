"""
Step 6 checks — human verification in the UI (approve / edit / remove),
applied to the pitch, the deck and the 3-section audit report. No API calls.

Run from project root:  python -m tests.test_step6_human_review
"""
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for mod in ("faiss", "sentence_transformers"):
    try:
        __import__(mod)
    except ImportError:
        m = types.ModuleType(mod)
        m.SentenceTransformer = object
        sys.modules[mod] = m
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test")

from docx import Document  # noqa: E402
from pptx import Presentation  # noqa: E402

from src.audit import verifier as V  # noqa: E402
from src.audit.review import apply_review, items_for_review, suggested_edit  # noqa: E402
from src.audit.report_writer import generate_audit_docx  # noqa: E402
from src.generation.pitch_generator import build_pptx  # noqa: E402
from tests.fixtures.sample_pitch import PITCH, PROFILE  # noqa: E402

CHUNKS = json.loads((ROOT / "data/processed/chunks/all_chunks.json").read_text(encoding="utf-8"))
passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  PASS  {name}")


def fake(verdicts):
    def _f(prompt, schema, **kw):
        ids = re.findall(r"^\[(C\d+)\] CLAIM:", prompt, flags=re.M)
        return schema(verdicts=[V._Verdict(claim_id=i, verdict=verdicts.get(i, ("SUPPORTED", ""))[0],
                                           missing_condition=verdicts.get(i, ("", ""))[1], reason=f"reason {i}")
                                for i in ids])
    return _f


# Automated audit: C2 condition, C4 flagged, C7 rejected
V.invoke_structured = fake({"C2": ("SUPPORTED_WITH_CONDITION", "only in the VIP+ plan"),
                            "C4": ("PARTIALLY_SUPPORTED", ""), "C7": ("NOT_SUPPORTED", "")})
audit = V.audit_pitch_content(PITCH, CHUNKS, profile=PROFILE)
items = items_for_review(audit.model_dump())
check("only non-verified claims go to human review", [r["claim_id"] for r in items] == ["C2", "C4", "C7"])
check("edit box pre-filled with the missing condition", "(only in the VIP+ plan)" in suggested_edit(items[0]))
check("new report/audit starts as review PENDING", audit.review_status == "PENDING")

# Advisor decisions: C2 edit (add condition), C4 edit, C7 remove; drop one Marsh sentence + one assumption
marsh_idx = next(i for i, o in enumerate(audit.other_statements) if o.category == "Marsh service")
assump_idx = next(i for i, o in enumerate(audit.other_statements) if o.category == "Assumption")
dropped_marsh = audit.other_statements[marsh_idx].statement
decisions = {"C2": {"action": "edit", "text": suggested_edit(items[0])},
             "C4": {"action": "edit", "text": "Activ One Claim Protect covers listed non-medical expenses such as gloves and face masks."},
             "C7": {"action": "remove"}}
V.invoke_structured = fake({"C2": ("SUPPORTED", ""), "C4": ("PARTIALLY_SUPPORTED", "")})   # re-check of the edits
final, fprof, rev = apply_review(PITCH, audit, decisions, {marsh_idx: False, assump_idx: False}, "A. Shaju",
                                 CHUNKS, profile=PROFILE)
d = {r.claim_id: r for r in rev.claim_results}
check("verified claims marked auto-verified", d["C1"].advisor_decision == "auto-verified")
check("C2 edited + re-check passed", d["C2"].advisor_decision == "edited" and d["C2"].recheck_status == "VERIFIED")
check("C4 edited but re-check still flagged -> recorded", d["C4"].recheck_status == "FLAGGED")
check("C7 removed", d["C7"].advisor_decision == "removed")
check("final pitch: edited wording applied to the benefit", "(only in the VIP+ plan)" in final.benefits[1].matched_benefit)
check("final pitch: removed talking point is gone", len(final.recommended_policy.key_talking_points) == 2
      and PITCH.recommended_policy.key_talking_points[1] not in final.recommended_policy.key_talking_points)
check("final pitch: removed Marsh sentence dropped from why_marsh", dropped_marsh not in final.why_marsh)
check("review recorded: reviewer, date, final status with override",
      rev.review_status == "COMPLETED" and rev.reviewed_by == "A. Shaju" and rev.reviewed_at
      and rev.final_status == "APPROVED WITH ADVISOR OVERRIDES")
check("original pitch is untouched", PITCH.recommended_policy.key_talking_points[1] == "Day 1 cover for diabetes, blood pressure and 5 other chronic conditions — no waiting period.")

# Clean review -> APPROVED FOR CLIENT
V.invoke_structured = fake({"C2": ("SUPPORTED", ""), "C4": ("SUPPORTED", "")})
_, _, rev2 = apply_review(PITCH, audit, decisions, {}, "A. Shaju", CHUNKS, profile=PROFILE)
check("all edits re-verified -> APPROVED FOR CLIENT", rev2.final_status == "APPROVED FOR CLIENT")

# Re-check unavailable -> accepted on advisor's word, flagged as 'not run'
def boom(*a, **k):
    raise V.LLMCallError("429 rate limit")
V.invoke_structured = boom
_, _, rev3 = apply_review(PITCH, audit, decisions, {}, "A. Shaju", CHUNKS, profile=PROFILE)
check("re-check failure -> UNCHECKED with reason (not silently passed)",
      {r.claim_id: r.recheck_status for r in rev3.claim_results}["C2"] == "UNCHECKED")

# Deck from the FINAL pitch
tmp = Path(tempfile.mkdtemp())
build_pptx(final, str(tmp / "d.pptx"), profile=fprof)
deck_text = "\n".join(sh.text_frame.text for s in Presentation(str(tmp / "d.pptx")).slides for sh in s.shapes if sh.has_text_frame)
check("deck shows the advisor's edited wording, not the removed talking point",
      "(only in the VIP+ plan)" in "\n".join(c.text for s in Presentation(str(tmp / "d.pptx")).slides
                                            for sh in s.shapes if sh.has_table for r in sh.table.rows for c in r.cells)
      and "Day 1 cover for diabetes, blood" not in deck_text)

# Report: exactly 3 numbered sections, decisions shown
generate_audit_docx("Infosys", final.model_dump(), rev.model_dump(), str(tmp / "r.docx"))
doc = Document(str(tmp / "r.docx"))
body = "\n".join(p.text for p in doc.paragraphs)
cells = "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
check("report sections: 1 Summary, 2 Claim-by-Claim, 3 Statements — nothing else",
      "1. Summary" in body and "2. Claim-by-Claim Results" in body and "3. Statements Outside the Policy Documents" in body
      and "Items Requiring Review" not in body and "How Each Claim Was Checked" not in body and "Sign-off" not in body)
check("summary shows the advisor review (reviewer + final status)", "A. Shaju" in cells and "APPROVED WITH ADVISOR OVERRIDES" in cells)
check("claim table shows each advisor decision",
      all(t in cells for t in ("Auto-verified", "Edited — re-check passed", "Removed from pitch")))
check("statements table shows confirmed/removed counts", "confirmed" in cells and "removed" in cells)

# Report before review -> 'Pending'
generate_audit_docx("Infosys", PITCH.model_dump(), audit.model_dump(), str(tmp / "p.docx"))
pd_ = Document(str(tmp / "p.docx"))
check("un-reviewed report says review is pending", "Pending" in "\n".join(p.text for p in pd_.paragraphs)
      and "Pending review" in "\n".join(c.text for t in pd_.tables for row in t.rows for c in row.cells))

print(f"\nAll {passed} checks passed.")
