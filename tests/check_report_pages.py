"""
Renders worst-case audit reports and counts REAL pages with LibreOffice.
Skips (with a message) if LibreOffice is not installed.
    python -m tests.check_report_pages
"""
import json
import os
import re
import shutil
import subprocess
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

from src.audit import verifier as V  # noqa: E402
from src.audit.report_writer import generate_audit_docx, MAX_PAGES  # noqa: E402
from src.generation.pitch_generator import BenefitMapping  # noqa: E402
from src.generation.company_profile import SourcedFact  # noqa: E402
from tests.fixtures.sample_pitch import PITCH, PROFILE  # noqa: E402

CHUNKS = json.loads((ROOT / "data/processed/chunks/all_chunks.json").read_text(encoding="utf-8"))
LONG_REASON = ("The source states the benefit only for a specific plan variant and sum insured band, and adds a "
               "sub-limit and an optional add-on premium that the claim leaves out entirely, which could mislead the client.")


def fake(mix):
    def _f(prompt, schema, **kw):
        ids = re.findall(r"^\[(C\d+)\] CLAIM:", prompt, flags=re.M)
        out = []
        for k, i in enumerate(ids):
            v = mix[k % len(mix)]
            out.append(V._Verdict(claim_id=i, verdict=v, missing_condition="only in the VIP+ plan, sub-limit applies",
                                  reason=LONG_REASON))
        return schema(verdicts=out)
    return _f


def soffice_cmd():
    for c in ("soffice", "libreoffice"):
        if shutil.which(c):
            return [c]
    skill = Path("/mnt/skills/public/docx/scripts/office/soffice.py")
    return [sys.executable, str(skill)] if skill.exists() else None


def pages(docx: Path, cmd) -> int:
    subprocess.run(cmd + ["--headless", "--convert-to", "pdf", "--outdir", str(docx.parent), str(docx)],
                   capture_output=True, timeout=180)
    pdf = docx.with_suffix(".pdf")
    data = pdf.read_bytes()
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def scenario(name, pitch, profile, mix):
    V.invoke_structured = fake(mix)
    rep = V.audit_pitch_content(pitch, CHUNKS, profile=profile)
    out = Path(tempfile.mkdtemp()) / f"{name}.docx"
    level = generate_audit_docx("Larsen & Toubro Mindtree" if name != "realistic" else "Infosys",
                                pitch.model_dump(), rep.model_dump(), str(out))
    return out, level, rep


if __name__ == "__main__":
    cmd = soffice_cmd()
    heavy = PITCH.model_copy(deep=True)
    heavy.benefits = (PITCH.benefits + PITCH.benefits[:3])            # 8 benefits
    heavy.recommended_policy.key_talking_points = PITCH.recommended_policy.key_talking_points + [
        "Claim Protect removes most non-medical deductions from hospital bills at discharge."]  # 4 points
    heavy.why_marsh = PITCH.why_marsh + " We benchmark premiums annually against the market for every renewal."
    heavy.assumptions_used = [f"Assumption number {i}: workforce detail not disclosed publicly and inferred from industry norms."
                              for i in range(6)]
    prof = PROFILE.model_copy(deep=True)
    prof.facts = PROFILE.facts + [SourcedFact(label=f"Fact {i}", value=f"Operational figure {i} reported for fiscal 2026",
                                              source_id="S1", source_title="AR", source_url=PROFILE.facts[0].source_url,
                                              verified_in_source=i % 2 == 0) for i in range(3)]
    extreme = heavy.model_copy(deep=True)
    extreme.benefits = [b.model_copy(update={"matched_benefit": b.matched_benefit + " " + b.matched_benefit})
                        for b in (PITCH.benefits * 4)]                  # 20 long benefits

    cases = [
        ("realistic", PITCH, PROFILE, ["SUPPORTED", "SUPPORTED_WITH_CONDITION", "SUPPORTED", "PARTIALLY_SUPPORTED"]),
        ("heavy", heavy, prof, ["PARTIALLY_SUPPORTED", "SUPPORTED_WITH_CONDITION", "NOT_SUPPORTED"]),
        ("extreme", extreme, prof, ["PARTIALLY_SUPPORTED", "SUPPORTED_WITH_CONDITION", "NOT_SUPPORTED"]),
    ]
    ok = True
    for name, p, pr, mix in cases:
        path, level, rep = scenario(name, p, pr, mix)
        n = pages(path, cmd) if cmd else None
        flag = "SKIPPED (no LibreOffice)" if n is None else ("OK" if n <= MAX_PAGES else "TOO LONG")
        ok &= (n is None or n <= MAX_PAGES)
        print(f"  {name:<10} claims={rep.total_claims:<3} review_items={sum(r.final_status != 'VERIFIED' for r in rep.claim_results):<3}"
              f" density_level={level}  pages={n}  -> {flag}   ({path})")
    print("\nAll reports within the page limit." if ok else "\nSOME REPORTS EXCEED THE PAGE LIMIT")
    sys.exit(0 if ok else 1)
