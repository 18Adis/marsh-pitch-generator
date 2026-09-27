"""
Step 3 checks — the audit engine and the Word report, against the REAL
brochure chunks. The LLM verdicts are faked (no API key needed).

Run from project root:  python -m tests.test_step3_audit
"""
import json
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

import os  # noqa: E402
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test")

from src.audit import verifier as V  # noqa: E402
from src.audit.text_match import locate_in_text  # noqa: E402
from src.audit.report_writer import generate_audit_docx  # noqa: E402
from src.generation.pitch_generator import BenefitMapping  # noqa: E402
from tests.fixtures.sample_pitch import PITCH, PROFILE  # noqa: E402

CHUNKS = json.loads((ROOT / "data/processed/chunks/all_chunks.json").read_text(encoding="utf-8"))
passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  PASS  {name}")


sent = {}


def fake_batch(verdict_for):
    """verdict_for: dict claim_id -> (verdict, missing) ; default SUPPORTED."""
    def _fake(prompt, schema, **kw):
        sent["prompt"], sent["stage"] = prompt, kw.get("stage")
        import re
        ids = re.findall(r"^\[(C\d+)\] CLAIM:", prompt, flags=re.M)
        return schema(verdicts=[V._Verdict(claim_id=i, verdict=verdict_for.get(i, ("SUPPORTED", ""))[0],
                                           missing_condition=verdict_for.get(i, ("", ""))[1],
                                           reason=f"stub reason for {i}") for i in ids])
    return _fake


# 1. Matching robustness on real text
abhi_p1 = V._page_text(CHUNKS, "ABHI_Product_Brochure.pdf", 1)
check("PDF 'HealthReturnsTM' matches '100% HealthReturns™'", locate_in_text("100% HealthReturns™", abhi_p1)[0] == "EXACT")
check("layout-reordered 'Claim Protect' quote is located (STITCHED)",
      locate_in_text("CLAIM PROTECT feature comes to your rescue and takes care of 100% out-of-pocket expenses", abhi_p1)[0]
      in ("EXACT", "STITCHED"))
check("fabricated text is NOT located", locate_in_text("unlimited zero copay mental health counselling", abhi_p1)[0] == "NONE")

# 2. Realistic pitch, all claims supported -> PASS, 100%
V.invoke_structured = fake_batch({})
rep = V.audit_pitch_content(PITCH, CHUNKS, profile=PROFILE)
ben = [r for r in rep.claim_results if r.claim_type == "benefit"]
tps = [r for r in rep.claim_results if r.claim_type == "talking_point"]
check("5 benefits + 3 talking points audited (talking points are no longer skipped)",
      len(ben) == 5 and len(tps) == 3 and rep.total_claims == 8)
check("every realistic benefit quote is located in the real brochures",
      all(r.quote_status.startswith(("EXACT", "STITCHED")) for r in ben))
check("talking points get evidence from the recommended policy's brochure",
      all(r.quote_status == "RETRIEVED_EVIDENCE" and r.matched_document == "ABHI_Product_Brochure.pdf" for r in tps))
check("ONE batched semantic call on the audit route", sent["stage"] == "audit")
check("the auditor is NOT asked to judge the client risk (cause of the old 0%)",
      "Sedentary desk work" not in sent["prompt"]
      and "Do NOT judge whether the benefit suits the client" in " ".join(sent["prompt"].split()))
check("all supported -> PASS, confidence 100%", rep.overall_status == "PASS" and rep.confidence_score == 100.0)

# 3. Mixed verdicts -> weighted score, correct statuses
V.invoke_structured = fake_batch({"C2": ("SUPPORTED_WITH_CONDITION", "only in the VIP+ plan"),
                                  "C4": ("PARTIALLY_SUPPORTED", ""),
                                  "C7": ("NOT_SUPPORTED", "")})
rep = V.audit_pitch_content(PITCH, CHUNKS, profile=PROFILE)
st = {r.claim_id: r.final_status for r in rep.claim_results}
check("statuses: condition / flagged / rejected mapped correctly",
      st["C2"] == "VERIFIED_WITH_CONDITION" and st["C4"] == "FLAGGED" and st["C7"] == "REJECTED")
expected = round(100 * (5 * 1.0 + 0.8 + 0.4 + 0.0) / 8, 1)
check(f"weighted confidence = {expected}% (not 0% because one claim was flagged)", rep.confidence_score == expected)
check("condition text carried into the result", rep.claim_results[1].missing_condition == "only in the VIP+ plan")
check("any rejected claim -> NEEDS HUMAN REVIEW", rep.overall_status == "NEEDS HUMAN REVIEW")

# 4. Checker failure -> UNCHECKED + AUDIT INCOMPLETE (never disguised as a bad pitch)
def boom(*a, **k):
    raise V.LLMCallError("Audit check failed: openrouter unavailable. Error code: 429 rate limit")
V.invoke_structured = boom
rep = V.audit_pitch_content(PITCH, CHUNKS, profile=PROFILE)
check("rate-limited checker -> every claim UNCHECKED, status AUDIT INCOMPLETE",
      rep.unchecked_count == 8 and rep.overall_status == "AUDIT INCOMPLETE")
check("the report says why (429) and asks for a re-run", rep.notes and "429" in rep.notes[0] and "Re-run" in rep.notes[0])

# 5. Citation problems
V.invoke_structured = fake_batch({})
bad = PITCH.model_copy(deep=True)
bad.benefits = [
    BenefitMapping(risk="r", matched_benefit="Day 1 chronic cover", source_policy="Activ One", page_number=1,  # wrong page
                   exact_quote_from_source="Now get Day 1 cover for listed 7 chronic conditions with zero waiting period"),
    BenefitMapping(risk="r", matched_benefit="Chronic cover", source_policy="HDFC ERGO Optima Secure+", page_number=2,  # wrong brochure
                   exact_quote_from_source="Now get Day 1 cover for listed 7 chronic conditions with zero waiting period"),
    BenefitMapping(risk="Eye strain", matched_benefit="HDFC covers eye strain and vision problems", source_policy="HDFC ERGO Optima Secure+",
                   page_number=2, exact_quote_from_source="enhanced coverage for eye strain and vision problems"),  # invented
]
bad.recommended_policy.key_talking_points = []
rep = V.audit_pitch_content(bad, CHUNKS)
r = rep.claim_results
check("real quote on the wrong page -> VERIFIED_WITH_CONDITION with 'correct the citation'",
      r[0].final_status == "VERIFIED_WITH_CONDITION" and r[0].matched_page == 2 and "citation" in r[0].recommended_action)
check("real quote under the wrong brochure -> found in the right one", r[1].quote_status == "FOUND_IN_OTHER_DOC"
      and r[1].matched_document == "ABHI_Product_Brochure.pdf")
check("invented quote (the old L&T 'eye strain' claim) -> REJECTED", r[2].final_status == "REJECTED")

# 6. Non-policy statements listed, not scored
rep = V.audit_pitch_content(PITCH, CHUNKS, profile=PROFILE)
cats = {o.category for o in rep.other_statements}
check("Marsh service, comparative reasoning, company facts and assumptions listed separately",
      {"Marsh service", "Comparative reasoning", "Company fact", "Assumption"} <= cats)

# 7. Word report renders with all sections
V.invoke_structured = fake_batch({"C2": ("SUPPORTED_WITH_CONDITION", "only in the VIP+ plan"), "C4": ("PARTIALLY_SUPPORTED", "")})
rep = V.audit_pitch_content(PITCH, CHUNKS, profile=PROFILE)
out = Path(tempfile.mkdtemp()) / "r.docx"
generate_audit_docx("Infosys", PITCH.model_dump(), rep.model_dump(), str(out), deck_file="Infosys_Pitch.pptx")
from docx import Document  # noqa: E402
d = Document(str(out))
body = "\n".join(p.text for p in d.paragraphs) + "\n".join(c.text for t in d.tables for row in t.rows for c in row.cells)
check("report has title + the 3 sections (Summary, Claim-by-Claim, Statements)", all(x in body for x in (
    "Insurance Pitch Audit Report — Infosys", "1. Summary", "2. Claim-by-Claim Results",
    "3. Statements Outside the Policy Documents")) and "Items Requiring Review" not in body)
check("page border present on the section", d.sections[0]._sectPr.find(
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pgBorders") is not None)
check("footer has Page X of Y fields", "NUMPAGES" in d.sections[0].footer._element.xml)
check("summary shows the weighted confidence", f"{rep.confidence_score}%" in body)

# 8. Page limit: the builder's own height budget keeps even a huge audit within 3 pages
from src.audit import report_writer as RW  # noqa: E402
big = PITCH.model_copy(deep=True)
big.benefits = PITCH.benefits * 4
V.invoke_structured = fake_batch({f"C{i}": ("PARTIALLY_SUPPORTED", "") for i in range(1, 30)})
rep_big = V.audit_pitch_content(big, CHUNKS, profile=PROFILE)
lvl = RW.generate_audit_docx("Infosys", big.model_dump(), rep_big.model_dump(), str(out))
_, budget = RW._build("Infosys", big.model_dump(), rep_big.model_dump(), RW.LEVELS[lvl], None, None)
check(f"23 claims all needing review -> compact level {lvl}, estimated {budget.pages:.2f} pages (<= 3 incl. safety margin)",
      lvl > 0 and budget.pages <= RW.MAX_PAGES - RW.SAFETY_PAGES)
check("normal audit keeps full detail (level 0)",
      RW.generate_audit_docx("Infosys", PITCH.model_dump(), rep.model_dump(), str(out)) == 0)

print(f"\nAll {passed} checks passed.")
