"""
Renders sample decks WITHOUT any API call, for visual QA of the template.
    python -m tests.render_samples            -> outputs/sample_decks/_template_check_*.pptx
"""
import sys
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

from tests.fixtures.sample_pitch import PITCH, PROFILE  # noqa: E402
from src.generation.pitch_generator import build_pptx, BenefitMapping  # noqa: E402

out = ROOT / "outputs" / "sample_decks"

# 1. realistic
n1 = build_pptx(PITCH, str(out / "_template_check_realistic.pptx"), profile=PROFILE)

# 2. stress: no live data, long name, many/long benefits, short why-marsh, 4 talking points
stress = PITCH.model_copy(deep=True)
stress.company_name = "Larsen & Toubro Technology Services and Mindtree Consolidated Holdings"
stress.size_estimate = "Assumption: large enterprise, estimated 80,000-90,000 employees across India and overseas delivery centres"
stress.why_marsh = "Marsh combines global placement capacity with deep analytics on workforce health trends. We structure programs that align local compliance, claims servicing and wellness integration."
stress.benefits = PITCH.benefits + [
    BenefitMapping(risk="Mental and emotional stress from long working hours and tight delivery deadlines in client-facing roles",
                   matched_benefit="Care Supreme: unlimited e-consultations with general physicians within the empanelled network, plus Care OPD add-on covering up to 4 physical consultations with 14 specified specialists per insured per policy year.",
                   source_policy="Care Supreme", page_number=3, exact_quote_from_source="Unlimited E-Consultations"),
    BenefitMapping(risk="Young workforce planning families", matched_benefit="HDFC ERGO Parenthood add-on covers maternity expenses, embryo storage costs and IVF treatments.",
                   source_policy="HDFC ERGO Optima Secure+", page_number=8, exact_quote_from_source="Covers maternity expenses, embryo storage costs and IVF treatments"),
    BenefitMapping(risk="Wellness engagement", matched_benefit="Niva Bupa ReAssure 2.0 Live Healthy: up to 30% discount on renewal premium.",
                   source_policy="Niva Bupa ReAssure 2.0", page_number=2, exact_quote_from_source="Up to 30% discount on premium at the time of Renewal"),
]
stress.recommended_policy.key_talking_points = PITCH.recommended_policy.key_talking_points + [
    "Claim Protect removes most non-medical deductions from hospital bills, so employees pay less out of pocket at discharge."]
n2 = build_pptx(stress, str(out / "_template_check_stress.pptx"), profile=None)
print(f"realistic: {n1} slides | stress (no web data, 8 benefits): {n2} slides -> {out}")
