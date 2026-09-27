"""
Stage 6d — Human verification (Objective 2.2: approve / edit / reject).

After the automated audit, the advisor reviews every item that was not
fully verified, directly in the UI, BEFORE the final deck and audit report
are produced:

  Policy claims not fully verified (VERIFIED_WITH_CONDITION / FLAGGED /
  REJECTED / UNCHECKED):
      approve  — keep the wording; the advisor has checked it manually
      edit     — replace the wording; the new wording is automatically
                 re-checked against the same brochure evidence
      remove   — drop the claim from the pitch
  Statements outside the policy documents (Marsh services, comparative
  reasoning, company facts, assumptions):
      confirm  — keep
      remove   — drop from the pitch (and the deck)

apply_review() returns the FINAL pitch (+ profile) that the deck is
rendered from, and the audit annotated with every decision, so the report
shows both what the AI produced and what the advisor signed off.
"""

from __future__ import annotations

import re
from datetime import datetime

from src.audit import verifier as V
from src.generation.pitch_generator import PitchContent

NEEDS_REVIEW = ("VERIFIED_WITH_CONDITION", "FLAGGED", "REJECTED", "UNCHECKED")
EDIT_OK = ("VERIFIED", "VERIFIED_WITH_CONDITION")


def items_for_review(audit: dict) -> list[dict]:
    """Claims the advisor must decide on (everything not fully verified)."""
    return [r for r in audit.get("claim_results", []) if r.get("final_status") in NEEDS_REVIEW]


def suggested_edit(r: dict) -> str:
    """Pre-filled text for the edit box: original wording + the missing condition, if any."""
    text = r.get("matched_benefit", "").strip()
    cond = (r.get("missing_condition") or "").strip()
    if cond and cond.lower() not in text.lower():
        text = text.rstrip(". ") + f" ({cond})."
    return text


def _sentences(text: str) -> list[str]:
    t = " ".join((text or "").split())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", t) if s.strip()]


def apply_review(pitch: PitchContent, audit: V.AuditReport, claim_decisions: dict, statement_decisions: dict,
                 reviewer: str, policy_docs: list[dict], profile=None, recheck: bool = True):
    """
    claim_decisions:     {claim_id: {"action": "approve"|"edit"|"remove", "text": str}}
    statement_decisions: {index_in_other_statements: True (keep) | False (remove)}
    Returns (final_pitch, final_profile, reviewed_audit).
    """
    from src.audit.claim_extractor import extract_claims_from_pitch

    final = pitch.model_copy(deep=True)
    prof = profile.model_copy(deep=True) if profile is not None else None
    rep = audit.model_copy(deep=True)
    claims = {c["claim_id"]: c for c in extract_claims_from_pitch(pitch)}   # same ids as the audit

    remove_benefits, remove_points = set(), set()
    benefit_idx = {c["claim_id"]: i for i, c in enumerate(v for v in claims.values() if v["claim_type"] == "benefit")}
    point_idx = {c["claim_id"]: i for i, c in enumerate(v for v in claims.values() if v["claim_type"] == "talking_point")}
    to_recheck = []

    for r in rep.claim_results:
        if r.final_status not in NEEDS_REVIEW:
            r.advisor_decision = "auto-verified"
            continue
        d = claim_decisions.get(r.claim_id) or {"action": "approve"}
        action = d.get("action", "approve")
        if action == "remove":
            r.advisor_decision = "removed"
            (remove_benefits if r.claim_type == "benefit" else remove_points).add(r.claim_id)
        elif action == "edit" and (d.get("text") or "").strip() and d["text"].strip() != r.matched_benefit.strip():
            new = " ".join(d["text"].split())
            r.advisor_decision, r.advisor_text = "edited", new
            if r.claim_type == "benefit":
                final.benefits[benefit_idx[r.claim_id]].matched_benefit = new
            else:
                final.recommended_policy.key_talking_points[point_idx[r.claim_id]] = new
            to_recheck.append(r)
        else:
            r.advisor_decision = "approved"

    # Automated re-check of edited wording against the same brochure evidence
    if to_recheck:
        if recheck:
            items = []
            for r in to_recheck:
                c = claims[r.claim_id]["claim"].model_copy(update={"matched_benefit": r.advisor_text})
                loc = (V._locate_quote(c, policy_docs, V.BM25Okapi([V.tokens(x["text"]) or ["_"] for x in policy_docs]))
                       if r.claim_type == "benefit" else V._retrieve_evidence(r.advisor_text, c.source_policy, policy_docs))
                if loc.get("evidence"):
                    items.append({"id": r.claim_id, "claim": r.advisor_text, "evidence": loc["evidence"], "loc": loc})
            try:
                key = V.get_api_key("audit")
                verdicts, err = V._semantic_batch([{k: i[k] for k in ("id", "claim", "evidence")} for i in items], key) \
                    if items else ({}, "")
            except V.LLMConfigError as e:
                verdicts, err = {}, str(e)
            by_id = {i["id"]: i for i in items}
            for r in to_recheck:
                v = verdicts.get(r.claim_id)
                if v is None or r.claim_id not in by_id:
                    r.recheck_status = "UNCHECKED"
                    r.recheck_reason = f"Re-check could not run{': ' + err if err else ''}; accepted on the advisor's verification."
                    continue
                status, _, _ = V._decide(by_id[r.claim_id]["loc"]["quote_status"], v.verdict, v.reason,
                                         v.missing_condition, r.claim_type)
                r.recheck_status, r.recheck_reason = status, v.reason
        else:
            for r in to_recheck:
                r.recheck_status, r.recheck_reason = "UNCHECKED", "Re-check skipped; accepted on the advisor's verification."

    # Remove claims from the final pitch (reverse order keeps indices valid)
    for cid in sorted(remove_benefits, key=lambda c: benefit_idx[c], reverse=True):
        del final.benefits[benefit_idx[cid]]
    for cid in sorted(remove_points, key=lambda c: point_idx[c], reverse=True):
        del final.recommended_policy.key_talking_points[point_idx[cid]]

    # Statements outside the policy documents
    drop_marsh, drop_just, drop_assump, drop_facts = set(), set(), set(), set()
    for i, o in enumerate(rep.other_statements):
        keep = statement_decisions.get(i, statement_decisions.get(str(i), True))
        o.advisor_decision = "confirmed" if keep else "removed"
        if keep:
            continue
        if o.category == "Marsh service":
            drop_marsh.add(o.statement)
        elif o.category == "Comparative reasoning":
            drop_just.add(o.statement)
        elif o.category == "Assumption":
            drop_assump.add(o.statement.strip().lower())
        elif o.category == "Company fact":
            drop_facts.add(o.statement)
    if drop_marsh:
        final.why_marsh = " ".join(s for s in _sentences(final.why_marsh) if s not in drop_marsh)
    if drop_just:
        final.recommended_policy.justification = " ".join(
            s for s in _sentences(final.recommended_policy.justification) if s not in drop_just)
    if drop_assump:
        final.assumptions_used = [a for a in final.assumptions_used if a.strip().lower() not in drop_assump]
        if prof is not None:
            prof.assumptions_used = [a for a in prof.assumptions_used if a.strip().lower() not in drop_assump]
    if drop_facts and prof is not None:
        prof.facts = [f for f in prof.facts if f"{f.label}: {f.value}" not in drop_facts]

    # Outcome
    kept = [r for r in rep.claim_results if r.advisor_decision != "removed"]
    problems = [r for r in kept if r.advisor_decision == "edited" and r.recheck_status in ("FLAGGED", "REJECTED")]
    rep.review_status = "COMPLETED"
    rep.reviewed_by = (reviewer or "").strip() or "Advisor (name not given)"
    rep.reviewed_at = datetime.now().strftime("%d %b %Y, %H:%M")
    if not kept:
        rep.final_status = "NO CLAIMS LEFT — REGENERATE"
    elif problems:
        rep.final_status = "APPROVED WITH ADVISOR OVERRIDES"
    else:
        rep.final_status = "APPROVED FOR CLIENT"
    return final, prof, rep
