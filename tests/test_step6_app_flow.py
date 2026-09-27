"""
End-to-end UI check of the human-verification flow in the real Streamlit app
(streamlit.testing.AppTest). All AI / web calls are faked; no keys needed.

Run from project root:  python -m tests.test_step6_app_flow
"""
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for mod in ("faiss", "sentence_transformers"):
    try:
        __import__(mod)
    except ImportError:
        m = types.ModuleType(mod)
        m.SentenceTransformer = object
        sys.modules[mod] = m
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test")

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    print("SKIPPED — streamlit not installed")
    sys.exit(0)

import src.retrieval.vector_store as vs  # noqa: E402
vs.PolicyVectorStore.__init__ = lambda self, *a, **k: None
vs.PolicyVectorStore.build = lambda self, chunks: None
from tests.fixtures.sample_pitch import PITCH, PROFILE  # noqa: E402
import src.generation.company_profile as cp  # noqa: E402
cp.generate_company_profile = lambda name, *a, **k: PROFILE
import src.generation.pitch_generator as pg  # noqa: E402
pg.retrieve_chunks_for_profile = lambda profile, store, *a, **k: [{"x": 1}] * 5
pg.generate_marketing_pitch = lambda profile, chunks, name, *a, **k: PITCH
import src.audit.verifier as V  # noqa: E402


def fake(prompt, schema, **kw):
    ids = re.findall(r"^\[(C\d+)\] CLAIM:", prompt, flags=re.M)
    vv = {"C2": ("SUPPORTED_WITH_CONDITION", "only in the VIP+ plan"), "C4": ("PARTIALLY_SUPPORTED", ""),
          "C7": ("NOT_SUPPORTED", "")}
    return schema(verdicts=[V._Verdict(claim_id=i, verdict=vv.get(i, ("SUPPORTED", ""))[0],
                                       missing_condition=vv.get(i, ("", ""))[1], reason=f"reason {i}") for i in ids])


V.invoke_structured = fake
passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  PASS  {name}")


deck = ROOT / "outputs/sample_decks/Infosys_Pitch.pptx"
report = ROOT / "outputs/audit_reports/Infosys_Audit_Report.docx"
for f in (deck, report):
    if f.exists():
        f.unlink()

at = AppTest.from_file(str(ROOT / "app/streamlit_app.py"), default_timeout=180)
at.run()
check("app starts without errors", not at.exception)
at.chat_input[0].set_value("Infosys").run()
md = "\n".join(m.value for m in at.markdown)
check("automated audit shown", "Automated audit: NEEDS HUMAN REVIEW" in md)
check("human verification panel shown", "Human verification" in md)
check("no files offered before the advisor finalises", len(at.get("download_button")) == 0 and not deck.exists())
check("one decision per flagged claim (3) + sidebar menu", len(at.radio) == 4)

at.text_input[0].set_value("A. Shaju")
at.radio[2].set_value("Remove from pitch")      # C7
at.checkbox[0].set_value(False)                 # drop one Marsh-service sentence
at.button[0].click().run()
md = "\n".join(m.value for m in at.markdown)
check("finalise succeeds", not at.exception)
check("final status after advisor review shown", "After advisor review: APPROVED" in md and "A. Shaju" in md)
check("deck, report and data downloads appear", len(at.get("download_button")) == 3)
check("final deck and report written to outputs/", deck.exists() and report.exists())
check("review panel replaced by the result", "Human verification" not in md)
print(f"\nAll {passed} checks passed.")
