"""
Stage 4 — Retrieval: connecting a CompanyProfile's key_risks to the
policy vector store.

Stage 5 — generateMarketingPitch(): turns the company profile + matched
policy chunks into a structured, grounded pitch, then renders it as a
.pptx file.

=== STAGE 4 DESIGN DECISIONS ===
- ONE QUERY PER RISK, not one combined query. Tested directly: a single
  vague combined query returned weak/irrelevant matches, because it
  doesn't resemble how the brochures are actually phrased. Querying per
  individual risk factor gives each risk a fair, focused shot at
  finding its best-matching benefit.
- DEDUPE BY CHUNK, KEEP HIGHEST SCORE. The same policy chunk can
  legitimately answer multiple risks. We keep one entry per chunk,
  tagged with whichever risk it matched best.
- MINIMUM SIMILARITY THRESHOLD (found via testing): forcing top_k
  results per risk regardless of quality let generic marketing taglines
  and unrelated disclaimers through with scores as low as ~0.19-0.24.
  Chunks below MIN_SIMILARITY_SCORE are dropped entirely — fewer,
  trustworthy matches beat more, noisy ones.
- TOP_K_PER_RISK raised from 2 to 3 (revision note): a real test run
  against TCS/Infosys only surfaced grounded benefits for 2 of 5
  identified risks, making the pitch feel thin. Retrieving one extra
  candidate per risk (while keeping MIN_SIMILARITY_SCORE unchanged —
  we did NOT loosen the quality bar) gives weaker-but-real risks one
  more chance to find a genuine match, without reintroducing the noise
  problem the threshold was added to prevent.
- EXCLUSION FILTERING (found via real testing): a risk mentioning
  "obesity" matched a policy's EXCLUSIONS clause containing the same
  word — even though the exclusion means the OPPOSITE of coverage.
  We filter chunks that look like exclusion/limitation clauses before
  they can reach Stage 5, rather than relying solely on the audit
  layer (Stage 6) to catch this after the fact.

=== STAGE 5 DESIGN DECISIONS ===
- MODEL CHOICE (revision): the "pitch" route in src/config.py (default
  OpenRouter nvidia/nemotron-3-ultra-550b-a55b:free — the largest model
  does the long client-facing writing). Change it with LLM_PITCH in .env.
  Same call path, retries and structured-output fallback as Stage 3/6.
- KNOWN ROOT-CAUSE LIMITATION (found via manual audit of real output,
  not anticipated in advance): chunk ABHI_Product_Brochure_p1_c3
  contains FOUR distinct marketing tiles ("Claim Protect", "HealthReturns",
  and two others) merged into one continuous text block with no
  separation, because the source PDF page is a visual grid that
  extracts as flat linear text. A real generated pitch conflated
  "HealthReturns" (a cashback/reward feature) with "Claim Protect"
  (a non-medical-expense coverage feature) into one FALSE combined
  claim, because the model had no way to see these as separate
  features within one blended chunk. This is a Stage 1/2 data-quality
  issue, not purely a prompting weakness — the prompt-level rules
  below (see FEATURE-ISOLATION RULE) substantially reduce this risk
  for cases where features are named, but cannot GUARANTEE elimination
  for a chunk this ambiguous. This is precisely why Stage 6 (the audit
  layer) exists as a final independent safety net, not as decoration.
- RECURRENCE NOTE: this exact conflation recurred after switching to
  the OpenRouter free-tier model, even with the FEATURE-ISOLATION RULE
  already in place — the rule alone wasn't enough for this weaker
  model. A concrete WORKED EXAMPLE using the exact real failure case
  was added directly into the rule (see below) since abstract
  instructions were evidently insufficient for this model's
  instruction-following ability. The audit layer correctly caught the
  recurrence both times, which is the intended defense-in-depth design
  — but reducing how often generation fails in the first place is
  still worth doing.
- FEATURE-ISOLATION RULE (added after finding the above): the model is
  explicitly instructed to treat each NAMED feature (e.g. "HealthReturns",
  "Claim Protect", "Super Reload") as its own distinct unit, and never
  combine details from two differently-named features into one claim.
- RELEVANCE RULE (added after finding a weak risk-benefit link): the
  model must only map a risk to a benefit if there's a direct, logical
  functional connection — not just because both chunks were retrieved
  for that risk.
- NO INTERNAL JARGON RULE (added after finding "retrieved context"
  leak into client-facing text): the model is explicitly told never to
  use RAG-pipeline/engineering language in any client-facing field.
- COMPARATIVE RECOMMENDATION LOGIC (added after finding an unexplained
  recommendation): the model must justify the recommended policy by
  explicitly comparing it against the other candidates using the
  actual grounded benefits found, not a vague "strongest evidence"
  claim.
- SEPARATE KEY_TALKING_POINTS FIELD (added after finding Slide 4 and
  Slide 5 showed identical, copy-pasted text): recommended_policy now
  has its own key_talking_points field, generated with different
  wording than the benefits list, specifically so the summary slide
  doesn't just repeat the benefits slide verbatim.
"""

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from src.config import invoke_structured, get_api_key, LLMConfigError, LLMCallError
from src.generation.company_profile import CompanyProfile
from src.retrieval.vector_store import PolicyVectorStore

# ============================================================
# STAGE 4 — RETRIEVAL
# ============================================================

TOP_K_PER_RISK = 3  # raised from 2 — see design note above
MIN_SIMILARITY_SCORE = 0.25  # unchanged — quality bar was not loosened

EXCLUSION_MARKERS = [
    "standard exclusions",
    "exclusions",
    "not covered",
    "non-payable",
    "wait period",
    "waiting period",
]


def is_exclusion_chunk(text: str) -> bool:
    """Heuristic: does this chunk look like an exclusions/limitations clause?"""
    head = text[:100].lower()
    return any(marker in head for marker in EXCLUSION_MARKERS)


def retrieve_chunks_for_profile(
    profile: CompanyProfile,
    vector_store: PolicyVectorStore,
    top_k_per_risk: int = TOP_K_PER_RISK
) -> list[dict]:
    """Runs one retrieval query per risk factor, merges into a ranked, deduped list."""
    best_per_chunk: dict[str, dict] = {}

    for risk in profile.key_risks:
        results = vector_store.search(risk, top_k=top_k_per_risk)

        for chunk in results:
            chunk_id = chunk["chunk_id"]
            score = chunk["similarity_score"]

            if score < MIN_SIMILARITY_SCORE:
                continue
            if is_exclusion_chunk(chunk["text"]):
                continue

            if chunk_id not in best_per_chunk or score > best_per_chunk[chunk_id]["similarity_score"]:
                enriched = dict(chunk)
                enriched["matched_risk"] = risk
                best_per_chunk[chunk_id] = enriched

    return sorted(best_per_chunk.values(), key=lambda c: c["similarity_score"], reverse=True)


# ============================================================
# STAGE 5 — GENERATION
# ============================================================

class BenefitMapping(BaseModel):
    risk: str = Field(description="The company risk factor being addressed")
    matched_benefit: str = Field(description="The specific policy benefit that addresses this risk")
    source_policy: str = Field(description="Which of the 4 policy documents this benefit is from")
    page_number: int = Field(description="Page number in the source document")
    exact_quote_from_source: str = Field(
        description="A short exact phrase copied from the source text supporting this claim"
    )


class RecommendedPolicy(BaseModel):
    policy_name: str = Field(description="Name of the recommended policy, e.g. 'Care Supreme'")
    justification: str = Field(
        description="Comparative reasoning for why this policy beats the other 3 "
                    "candidates, referencing specific risks it addresses that others "
                    "don't. Written in plain client-facing business language — never "
                    "reference 'context', 'retrieved', 'provided documents', 'chunks', "
                    "or any other internal/technical process terms."
    )
    key_talking_points: list[str] = Field(
        description="2-4 SHORT, FRESHLY-WORDED talking points summarizing why to "
                    "choose this policy. Must be reworded/condensed — NOT copied "
                    "verbatim from the benefits list, since this appears on a "
                    "separate summary slide."
    )


class PitchContent(BaseModel):
    company_name: str
    industry: str
    size_estimate: str
    key_risks_summary: list[str]
    why_marsh: str = Field(description="2-3 sentences on why the client should work with Marsh")
    benefits: list[BenefitMapping] = Field(
        description="One entry per risk that has a genuine supporting chunk AND a "
                     "direct logical connection to that risk. Do NOT include a risk "
                     "here if no chunk supports it, or if the connection is forced."
    )
    recommended_policy: RecommendedPolicy
    assumptions_used: list[str] = Field(default_factory=list)


class PitchGenerationError(Exception):
    """Raised when pitch generation or slide rendering fails."""
    pass


def _build_context_block(matched_chunks: list[dict]) -> str:
    """Formats retrieved chunks into a labeled block for the prompt."""
    lines = []
    for c in matched_chunks:
        lines.append(
            f"[Source: {c['source_document']}, Page {c['page_number']}]\n"
            f"Addresses risk: \"{c['matched_risk']}\"\n"
            f"Text: {c['text']}\n"
        )
    return "\n---\n".join(lines)


def _build_company_facts_block(profile: CompanyProfile) -> str:
    """
    Company facts from live web research (Stage 3). These are the ONLY
    company figures the pitch may state; policy claims still come only
    from the policy context. If research was unavailable, say so plainly
    so the model labels company figures as assumptions.
    """
    facts = getattr(profile, "facts", None) or []
    if not facts:
        return ("COMPANY FACTS: none verified (no live web data). Any company figure you "
                "mention (e.g. headcount, revenue) must be written as an assumption.")
    lines = [
        f"- {f.label}: {f.value} (source: {f.source_title}; {f.source_url})"
        + ("" if f.verified_in_source else "  [UNVERIFIED — do not quote this number]")
        for f in facts
    ]
    return ("VERIFIED COMPANY FACTS (from live web research — the ONLY company figures you "
            "may state; never use them as evidence for policy benefits):\n" + "\n".join(lines))


def generate_marketing_pitch(
    profile: CompanyProfile,
    matched_chunks: list[dict],
    company_name: str,
    api_key: str = None,
    max_retries: int = 3
) -> PitchContent:
    """
    Objective 1.3 implementation. See module docstring for the full list
    of rules added after auditing a real generated pitch and finding
    specific, concrete errors (feature conflation, jargon leak, weak
    recommendation logic, duplicate slide content).
    """
    if not matched_chunks:
        raise PitchGenerationError(
            "No matched policy chunks available — cannot generate a "
            "grounded pitch with zero supporting evidence."
        )

    try:
        resolved_key = get_api_key("pitch", api_key)
    except LLMConfigError as e:
        raise PitchGenerationError(str(e)) from e

    context_block = _build_context_block(matched_chunks)
    company_facts_block = _build_company_facts_block(profile)

    prompt = f"""You are drafting a client-specific insurance marketing pitch for a Marsh
Client Advisor.

COMPANY: {company_name}
Industry: {profile.industry}
Size: {profile.size_estimate}
Key risks identified: {profile.key_risks}

{company_facts_block}

RETRIEVED POLICY CONTEXT (this is the ONLY source of truth for benefit claims —
do not use any outside knowledge about these insurance products):
{context_block}

CRITICAL RULES — read carefully, each was added after a real, confirmed error:

1. FEATURE-ISOLATION RULE: Some source text blocks contain MULTIPLE distinctly
   NAMED features/benefits sitting close together (e.g. a chunk might mention
   both "HealthReturns" and "Claim Protect" as two separate named things).
   These are DIFFERENT features with DIFFERENT mechanics even if they appear
   in the same block of text. NEVER combine details from two differently-named
   features into a single benefit claim. If a chunk contains multiple named
   features, either (a) pick the ONE most relevant to the risk and describe
   ONLY that one accurately, or (b) if you cannot cleanly separate which
   detail belongs to which named feature, do not use that chunk for a
   benefit claim at all.

   WORKED EXAMPLE OF THIS EXACT MISTAKE (do not repeat it):
   Source text: "...Tadaa! You just earned HealthReturns for yourself!...
   CLAIM PROTECT# feature comes to your rescue and takes care of 100%
   out-of-pocket expenses like gloves, cotton, face masks..."
   WRONG: "HealthReturns covers non-medical out-of-pocket expenses like
   gloves and face masks" <- FALSE. This merges two different features.
   HealthReturns is a cashback/reward feature paid on renewal premiums,
   diagnostics, and medical bills. Claim Protect is the SEPARATE feature
   that actually covers gloves/cotton/face masks. Do not merge them.
   CORRECT: either describe Claim Protect's actual non-medical-expense
   coverage on its own, or describe HealthReturns' actual cashback
   mechanic on its own — never both blended into one sentence.

2. RELEVANCE RULE: Only create a benefit entry for a risk if there is a
   direct, logical, functional connection between the benefit and that
   specific risk. Do not force a connection just because a chunk was
   retrieved alongside that risk — if the actual content doesn't clearly
   address the risk, omit that risk from "benefits" rather than stretch
   the connection.

3. NO-INVENTION RULE: For "benefits", create ONE entry per risk that has a
   genuinely matching, clearly relevant chunk above. If a risk has no such
   match, DO NOT invent one — omit that risk entirely. Fewer, fully-grounded
   claims beat more, forced ones.

4. EXACT QUOTE RULE: Every "exact_quote_from_source" must be verbatim text
   that actually appears in the provided context above, and must relate to
   only ONE named feature — never a blended quote spanning two features.

5. NO INTERNAL JARGON RULE: This document will be read by a real client.
   NEVER use engineering/pipeline language anywhere in your output — no
   phrases like "retrieved context", "provided documents", "source chunks",
   "the context above", "based on the data provided", etc. Write exactly as
   a professional insurance advisor would speak to a client directly.

6. COMPARATIVE RECOMMENDATION RULE: For "recommended_policy.justification",
   explicitly compare the recommended policy against the other 3 candidates
   using the ACTUAL grounded benefits found above (e.g. name specifically
   which risks it addresses that the others don't, or how many of the
   identified risks it covers vs. others). Do not just assert it has "the
   strongest evidence" without explaining why in concrete terms.

7. DISTINCT SUMMARY RULE: "recommended_policy.key_talking_points" must be
   freshly worded, condensed points — NOT copied or lightly reworded from
   the "benefits" list, since both appear on different slides and must not
   read as duplicates.

8. COMPANY FIGURES RULE: company numbers (headcount, revenue, locations)
   may only come from the COMPANY FACTS block above, copied exactly. For
   "size_estimate", use the verified headcount fact if one exists. If you
   are uncertain about any non-factual framing, note it in "assumptions_used".
"""

    try:
        return invoke_structured(
            prompt, PitchContent, stage="pitch", temperature=0.3,
            api_key=resolved_key, max_retries=max_retries,
        )
    except LLMCallError as e:
        raise PitchGenerationError(str(e)) from e


# ============================================================
# .pptx RENDERING — UNCHANGED FORMATTING/LAYOUT
# ============================================================

def build_pptx(pitch: PitchContent, output_path: str, profile: CompanyProfile | None = None) -> int:
    """
    Renders the pitch with the designed deck template (src/generation/deck_renderer.py):
    company overview, why Marsh, benefits mapped to risks, recommendation, and
    sources & assumptions — 3 to 5 slides. `profile` (optional) supplies the
    sourced company facts and web sources. Returns the number of slides.
    """
    from src.generation.deck_renderer import render_deck
    try:
        return render_deck(pitch, output_path, profile=profile)
    except Exception as e:  # noqa: BLE001
        raise PitchGenerationError(f"Slide rendering failed: {e}") from e


if __name__ == "__main__":
    import sys

    from src.generation.company_profile import generate_company_profile, CompanyProfileError

    company_name = sys.argv[1] if len(sys.argv) > 1 else "TCS"

    print(f"1. Generating company profile for: {company_name}")
    try:
        profile = generate_company_profile(company_name)
    except CompanyProfileError as e:
        print(f"ERROR generating profile: {e}")
        sys.exit(1)
    print(f"   Key risks identified: {len(profile.key_risks)}")

    print("\n2. Retrieving matching policy chunks...")
    store = PolicyVectorStore()
    store.load(
        index_path="data/processed/vector_store/policy_index.faiss",
        chunks_path="data/processed/vector_store/policy_chunks.pkl"
    )
    matched_chunks = retrieve_chunks_for_profile(profile, store)
    print(f"   Retrieved {len(matched_chunks)} usable chunks")

    print("\n3. Generating grounded pitch content...")
    try:
        pitch = generate_marketing_pitch(profile, matched_chunks, company_name)
    except PitchGenerationError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(json.dumps(pitch.model_dump(), indent=2))

    print("\n4. Rendering .pptx...")
    output_path = f"outputs/sample_decks/{company_name}_Pitch.pptx"
    n_slides = build_pptx(pitch, output_path, profile=profile)
    print(f"   Saved {n_slides} slides to {output_path}")
