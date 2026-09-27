"""
Stage 6a — Claim extraction

Splits everything the pitch says into two groups:

1. POLICY CLAIMS (audited and scored):
   - every benefit on the "benefits mapped" slide — each already carries
     source_policy, page_number and an exact quote (Stage 5 schema);
   - every recommendation talking point — shown on the recommendation
     slide, so a client reads it as a policy fact. It has no quote, so the
     verifier retrieves evidence from the recommended policy's brochure.
   (Previously only benefits were audited, so talking points such as
   "unlimited virtual doctor visits, zero copay" reached the client
   slide unchecked.)

2. OTHER STATEMENTS (listed for advisor sign-off, NOT scored — there is no
   policy clause that could prove or disprove them):
   - "Why Marsh" sentences (Marsh service claims);
   - the comparative justification ("only plan of the four that ...");
   - company facts from live web research (with their verification state);
   - assumptions.
"""

from __future__ import annotations

import re

from src.generation.pitch_generator import PitchContent, BenefitMapping


def _sentences(text: str) -> list[str]:
    t = " ".join((text or "").split())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", t) if s.strip()]


def extract_claims_from_pitch(pitch: PitchContent) -> list[dict]:
    """[{claim_id, claim_type, section, claim: BenefitMapping}] in deck order."""
    out: list[dict] = []
    n = 0
    for b in pitch.benefits or []:
        n += 1
        out.append({"claim_id": f"C{n}", "claim_type": "benefit",
                    "section": "Benefits mapped to your risks", "claim": b})
    rec = pitch.recommended_policy
    for p in (rec.key_talking_points or []) if rec else []:
        if not p.strip():
            continue
        n += 1
        out.append({"claim_id": f"C{n}", "claim_type": "talking_point", "section": "Recommendation",
                    "claim": BenefitMapping(risk="Recommendation talking point", matched_benefit=p.strip(),
                                            source_policy=rec.policy_name, page_number=0,
                                            exact_quote_from_source="")})
    return out


def extract_other_statements(pitch: PitchContent, profile=None) -> list[dict]:
    out: list[dict] = []
    for s in _sentences(pitch.why_marsh):
        out.append({"category": "Marsh service", "statement": s, "source": "—",
                    "status": "Advisor sign-off — not a policy term"})
    rec = pitch.recommended_policy
    if rec and rec.justification:
        for s in _sentences(rec.justification):
            out.append({"category": "Comparative reasoning", "statement": s,
                        "source": "Derived from the audited benefits",
                        "status": "Advisor review — cross-policy comparison"})
    for f in (getattr(profile, "facts", None) or []):
        out.append({"category": "Company fact", "statement": f"{f.label}: {f.value}",
                    "source": f"[{f.source_id}] {f.source_url}",
                    "status": "Verified in web source" if f.verified_in_source else "NOT verified in source — do not quote"})
    seen = set()
    for a in list(getattr(profile, "assumptions_used", None) or []) + list(pitch.assumptions_used or []):
        k = a.strip().lower()[:80]
        if k and k not in seen:
            seen.add(k)
            out.append({"category": "Assumption", "statement": a.strip(), "source": "—",
                        "status": "Advisor to confirm with client"})
    return out
