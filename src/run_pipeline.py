"""
End-to-end pipeline from the terminal (same steps as the Streamlit app):

    python -m src.run_pipeline Infosys

company research + profile -> policy retrieval -> pitch -> deck (.pptx)
-> audit -> audit report (.docx) + audit data (.json)
"""

import json
import sys
from pathlib import Path

from src.config import ENV_FILE_HINT, describe_routing  # noqa: F401  (loads .env)

ROOT = Path(__file__).resolve().parent.parent
CHUNKS = ROOT / "data" / "processed" / "chunks" / "all_chunks.json"


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip().replace(" ", "_") or "Company"


def main(company: str) -> int:
    from src.generation.company_profile import generate_company_profile, CompanyProfileError
    from src.generation.pitch_generator import (retrieve_chunks_for_profile, generate_marketing_pitch,
                                                build_pptx, PitchGenerationError)
    from src.retrieval.vector_store import PolicyVectorStore
    from src.audit.verifier import audit_pitch_content
    from src.audit.report_writer import generate_audit_docx

    print("Model routing:\n" + describe_routing())
    policy_docs = json.loads(CHUNKS.read_text(encoding="utf-8"))

    print(f"\n1. Company research + profile: {company}")
    try:
        profile = generate_company_profile(company)
    except CompanyProfileError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"   {profile.data_source} | {len(profile.sources)} web sources | {len(profile.facts)} sourced facts")
    for n in profile.research_notes:
        print(f"   research note: {n}")

    print("\n2. Retrieving policy clauses for each risk...")
    store = PolicyVectorStore()
    store.build(policy_docs)
    matched = retrieve_chunks_for_profile(profile, store)
    print(f"   {len(matched)} relevant policy sections")

    print("\n3. Writing the pitch...")
    try:
        pitch = generate_marketing_pitch(profile, matched, company)
    except PitchGenerationError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"   {len(pitch.benefits)} benefits | recommended: {pitch.recommended_policy.policy_name}")

    name = safe_name(company)
    deck = ROOT / "outputs" / "sample_decks" / f"{name}_Pitch.pptx"
    n = build_pptx(pitch, str(deck), profile=profile)
    print(f"   deck: {deck} ({n} slides)")

    print("\n4. Auditing every claim against the four brochures...")
    audit = audit_pitch_content(pitch, policy_docs, profile=profile)
    print(f"   {audit.overall_status} | confidence {audit.confidence_score}% | "
          f"{audit.verified_count} verified, {audit.conditional_count} with condition, "
          f"{audit.flagged_count} flagged, {audit.rejected_count} rejected, {audit.unchecked_count} unchecked")
    for note in audit.notes:
        print(f"   NOTE: {note}")
    for r in audit.claim_results:
        print(f"   [{r.claim_id}] {r.final_status:<24} {r.quote_status:<22} {r.matched_benefit[:70]}")

    out = ROOT / "outputs" / "audit_reports"
    report = out / f"{name}_Audit_Report.docx"
    generate_audit_docx(company, pitch.model_dump(), audit.model_dump(), str(report), deck_file=deck.name)
    (out / f"{name}_Audit.json").write_text(json.dumps(audit.model_dump(), indent=2), encoding="utf-8")
    print(f"   report: {report}")
    print("\n   NOTE: this is the DRAFT (advisor review pending). Human verification — approve / edit /\n"
          "   remove each flagged item — happens in the app: streamlit run app/streamlit_app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:]) or "Infosys"))
