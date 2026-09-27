"""
Step 4 checks — deck template renderer. No API calls.
Run from project root:  python -m tests.test_step4_deck_template
"""
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

from pptx import Presentation  # noqa: E402
from pptx.util import Emu  # noqa: E402

from tests.fixtures.sample_pitch import PITCH, PROFILE  # noqa: E402
from src.generation.pitch_generator import build_pptx  # noqa: E402
from src.generation import deck_renderer as dr  # noqa: E402

passed = 0
tmp = Path(tempfile.mkdtemp())


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  PASS  {name}")


def all_text(prs):
    out = []
    for sl in prs.slides:
        for sh in sl.shapes:
            if sh.has_text_frame:
                out.append(sh.text_frame.text)
            if sh.has_table:
                out += [c.text for r in sh.table.rows for c in r.cells]
    return "\n".join(out)


def within_bounds(prs):
    W, H = prs.slide_width, prs.slide_height
    for sl in prs.slides:
        for sh in sl.shapes:
            if sh.left < 0 or sh.top < 0 or sh.left + sh.width > W + Emu(10) or sh.top + sh.height > H + Emu(10):
                return False
    return True


# 1. realistic deck
p1 = tmp / "a.pptx"
n = build_pptx(PITCH, str(p1), profile=PROFILE)
prs = Presentation(str(p1))
txt = all_text(prs)
check("realistic pitch -> 5 slides (within the brief's 3-5)", n == 5 and len(prs.slides) == 5)
check("16:9 canvas", (prs.slide_width, prs.slide_height) == (Emu(9144000), Emu(5143500)))
check("every shape inside the slide", within_bounds(prs))
check("sourced facts become stat cards", "3,28,594" in txt and "₹1,78,650 crore" in txt and "59 countries" in txt)
check("risks carry the ASSUMPTIONS label", "ASSUMPTIONS — advisor to confirm" in txt)
check("every benefit appears with its source page",
      all(b.source_policy.replace("'s", "") .split()[0] in txt for b in PITCH.benefits) and "p.2" in txt)
check("recommended plan is the dark slide title", "Aditya Birla Health Insurance — Activ One" in txt)
check("sources slide lists web sources", "[S1] Infosys Integrated Annual Report" in txt)
notes3 = prs.slides[2].notes_slide.notes_text_frame.text
check("benefit slide notes carry the exact source quotes", all(b.exact_quote_from_source in notes3 for b in PITCH.benefits))
check("slide 1 notes keep full risk text + fact URLs", PITCH.key_risks_summary[0] in prs.slides[0].notes_slide.notes_text_frame.text
      and "infosys-ar-26.pdf" in prs.slides[0].notes_slide.notes_text_frame.text)
check("fonts are Office-safe (Cambria / Calibri only)",
      {r.font.name for sl in prs.slides for sh in sl.shapes if sh.has_text_frame
       for p in sh.text_frame.paragraphs for r in p.runs} <= {"Cambria", "Calibri"})

# 2. no web research -> no stat cards, clear message
p2 = tmp / "b.pptx"
build_pptx(PITCH, str(p2), profile=None)
t2 = all_text(Presentation(str(p2)))
check("no profile -> 'No verified company figures' notice, no invented stat cards",
      "No verified company figures" in t2 and "₹1,78,650 crore" not in t2 and "Model knowledge only" in t2)

# 3. edge cases keep 3-5 slides
empty = PITCH.model_copy(deep=True); empty.benefits = []
n3 = build_pptx(empty, str(tmp / "c.pptx"), profile=PROFILE)
check("zero grounded benefits -> honest message slide, still 5 slides",
      n3 == 5 and "No policy benefits could be confidently grounded" in all_text(Presentation(str(tmp / "c.pptx"))))
many = PITCH.model_copy(deep=True); many.benefits = PITCH.benefits * 3   # 15 rows
n4 = build_pptx(many, str(tmp / "d.pptx"), profile=PROFILE)
check("15 benefits -> 2 benefit slides, sources slide dropped, total stays 5", n4 == 5
      and "(continued)" in all_text(Presentation(str(tmp / "d.pptx"))))
check("every shape inside the slide (stress)", within_bounds(Presentation(str(tmp / "d.pptx"))))

# 4. helpers
check("stat parser: '₹1,78,650 crore total revenues in fiscal 2026'",
      dr.stat_from_fact(PROFILE.facts[1]) == ("₹1,78,650 crore", "total revenues in fiscal 2026"))
check("stat parser: no dangling 'Offices in'", dr.stat_from_fact(PROFILE.facts[3]) == ("59 countries", "Global presence"))
check("facts without a figure are skipped", dr.stat_from_fact(PROFILE.facts[4]) is None)
check("shorten() never exceeds the box", dr.text_height(dr.shorten("word " * 400, 3, 1, 10), 3, 10) <= 1)

print(f"\nAll {passed} checks passed.")
