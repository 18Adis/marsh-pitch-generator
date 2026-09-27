"""
Stage 6b — auditPitchContent(pitch_slides, policy_docs)

Objective 2.1 / 2.2. Independently re-verifies every POLICY claim in a
generated pitch against the four source brochures, and lists every
non-policy statement (Marsh services, company facts, comparative
reasoning) separately for advisor sign-off.

WHAT IS AUDITED (see claim_extractor.py):
  - every benefit on the "benefits mapped" slide (carries a quote + page), and
  - every recommendation talking point (no quote — evidence is retrieved
    from the recommended policy's brochure with BM25).

LAYER 1 — deterministic quote location (text_match.py, no AI).
  Normalises symbols/currency/ligatures and tolerates PDF layout
  reordering, then locates the quote on the cited page, elsewhere in the
  cited brochure, or (BM25) in another brochure.
LAYER 2 — BM25 search across all four brochures (wrong-document citations,
  and evidence retrieval for talking points).
LAYER 3 — independent LLM semantic check, ONE batched call for all claims.
  It judges only whether the source text supports what the claim says the
  POLICY does — not whether the benefit suits the client's risk (a
  brochure never mentions "sedentary desk work"; judging that was the
  main reason every claim used to come out "partially supported").

WHY THE 0% REPORTS HAPPENED (fixed here, each found in real runs):
  1. The semantic prompt included the client risk, so correct benefits were
     judged "adds details not in the source" -> FLAGGED.
  2. Quote matching was a raw substring test, so ™/TM, ₹/INR, curly quotes
     and layout-reordered PDF text failed a genuine quote.
  3. confidence = VERIFIED / total, so any flag counted as a total failure.
  4. A failed/rate-limited semantic call was recorded as "partially
     supported" -> FLAGGED, i.e. an infrastructure error looked like a bad
     pitch. Now such claims are UNCHECKED and the report says the audit is
     incomplete instead of reporting a misleading score.

VERDICT -> STATUS -> CONFIDENCE WEIGHT
  quote located (EXACT/STITCHED) + SUPPORTED              -> VERIFIED                 1.00
  located + SUPPORTED_WITH_CONDITION (claim omits a       -> VERIFIED_WITH_CONDITION  0.80
    condition the source attaches: add-on, plan, limit)
  cited page/brochure wrong but quote real + SUPPORTED    -> VERIFIED_WITH_CONDITION  0.80
  quote only FUZZY (wording differs) + SUPPORTED          -> VERIFIED_WITH_CONDITION  0.80
  PARTIALLY_SUPPORTED                                     -> FLAGGED                  0.40
  NOT_SUPPORTED, or quote not found anywhere              -> REJECTED                 0.00
  semantic check could not run                            -> UNCHECKED (excluded from the score)
confidence_score = weighted mean over CHECKED claims (0-100).
fully_verified_pct = share of all claims that are VERIFIED.
overall_status: PASS | PASS WITH CONDITIONS | NEEDS HUMAN REVIEW |
                REJECT — REGENERATE | AUDIT INCOMPLETE | NO CLAIMS TO AUDIT
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from src.config import invoke_structured, get_api_key, get_route, LLMCallError, LLMConfigError
from src.audit.text_match import locate_in_text, normalize, tokens
from src.generation.pitch_generator import PitchContent

# ============================================================
# Policy name resolution
# ============================================================
# The LLM refers to policies by product name ("Care Supreme"); chunks are
# keyed by PDF filename. Without this map every claim would fail on a
# string mismatch alone.
POLICY_ALIASES: dict[str, list[str]] = {
    "ABHI_Product_Brochure.pdf": ["abhi", "activ one", "activone", "aditya birla"],
    "Care_Health_Product_Brochure.pdf": ["care supreme", "care health", "care advanced", "care opd", "care "],
    "HDFC_Product_Brochure.pdf": ["hdfc", "optima secure", "hdfc ergo"],
    "Niva_Bupa_Product_Brochure.pdf": ["niva bupa", "reassure", "niva"],
}
POLICY_DISPLAY = {
    "ABHI_Product_Brochure.pdf": "Aditya Birla Activ One",
    "Care_Health_Product_Brochure.pdf": "Care Supreme",
    "HDFC_Product_Brochure.pdf": "HDFC ERGO Optima Secure+",
    "Niva_Bupa_Product_Brochure.pdf": "Niva Bupa ReAssure 2.0",
}


def resolve_source_document(claimed_policy_name: str) -> str | None:
    """Maps a claim's human-readable policy name to the real source filename."""
    normalized = f" {(claimed_policy_name or '').lower().strip()} "
    for filename, aliases in POLICY_ALIASES.items():
        if any(alias.strip() and alias in normalized for alias in aliases):
            return filename
    stripped = normalized.strip()
    for filename in POLICY_ALIASES:
        if stripped and stripped in filename.lower():
            return filename
    return None


# Backwards-compatible helpers (used by earlier tests)
def normalize_text(text: str) -> str:
    return normalize(text)


def exact_substring_match(quote: str, chunk_text: str) -> bool:
    return locate_in_text(quote, chunk_text)[0] == "EXACT"


# ============================================================
# Schemas
# ============================================================

STATUS_WEIGHT = {"VERIFIED": 1.0, "VERIFIED_WITH_CONDITION": 0.8, "FLAGGED": 0.4, "REJECTED": 0.0}


class ClaimAuditResult(BaseModel):
    claim_id: str = ""
    claim_type: str = "benefit"          # benefit | talking_point
    section: str = ""                    # where it appears in the deck
    risk: str
    matched_benefit: str                 # the claim text as it appears in the pitch
    source_policy: str
    page_number: int
    exact_quote_from_source: str
    quote_status: str                    # EXACT_CLAIMED_PAGE / STITCHED_CLAIMED_PAGE / EXACT_OTHER_PAGE /
                                         # FUZZY_... / FOUND_IN_OTHER_DOC / RETRIEVED_EVIDENCE /
                                         # NOT_FOUND_ANYWHERE / UNKNOWN_SOURCE / NO_EVIDENCE
    matched_document: str | None
    matched_page: int | None
    evidence_excerpt: str = ""
    semantic_verdict: str                # SUPPORTED / SUPPORTED_WITH_CONDITION / PARTIALLY_SUPPORTED /
                                         # NOT_SUPPORTED / UNCHECKED / SKIPPED
    semantic_reason: str
    missing_condition: str = ""
    final_status: str                    # VERIFIED / VERIFIED_WITH_CONDITION / FLAGGED / REJECTED / UNCHECKED
    confidence_weight: float | None = None
    recommended_action: str
    # --- filled in by the advisor's human verification (src/audit/review.py) ---
    advisor_decision: str = ""           # "" (pending) | auto-verified | approved | edited | removed
    advisor_text: str = ""               # the advisor's replacement wording (if edited)
    recheck_status: str = ""             # automated re-check of the edited wording
    recheck_reason: str = ""


class OtherStatement(BaseModel):
    category: str                        # Marsh service | Company fact | Comparative reasoning | Assumption
    statement: str
    source: str = ""
    status: str                          # e.g. "Advisor sign-off", "Verified in web source", "Unverified"
    advisor_decision: str = ""           # "" (pending) | confirmed | removed


class AuditReport(BaseModel):
    total_claims: int
    verified_count: int
    conditional_count: int = 0
    flagged_count: int
    rejected_count: int
    unchecked_count: int = 0
    confidence_score: float              # weighted, over checked claims (0-100)
    fully_verified_pct: float = 0.0
    overall_status: str
    claim_results: list[ClaimAuditResult]
    other_statements: list[OtherStatement] = Field(default_factory=list)
    audit_model: str = ""
    audited_at: str = ""
    notes: list[str] = Field(default_factory=list)
    # --- human verification ---
    review_status: str = "PENDING"       # PENDING | COMPLETED
    reviewed_by: str = ""
    reviewed_at: str = ""
    final_status: str = ""               # outcome after advisor review


class _Verdict(BaseModel):
    claim_id: str = Field(description="The claim id exactly as given, e.g. 'C3'")
    verdict: str = Field(description="Exactly one of: SUPPORTED, SUPPORTED_WITH_CONDITION, PARTIALLY_SUPPORTED, NOT_SUPPORTED")
    missing_condition: str = Field(default="", description="If SUPPORTED_WITH_CONDITION: the condition the claim omits, in a few words")
    reason: str = Field(description="One sentence explaining the verdict, citing the source wording")


class _BatchVerdicts(BaseModel):
    verdicts: list[_Verdict]


# ============================================================
# Layer 1 + 2 — locating quotes / retrieving evidence
# ============================================================

def _page_text(docs: list[dict], doc: str, page: int) -> str:
    return "\n".join(c["text"] for c in docs if c["source_document"] == doc and c["page_number"] == page)


def _locate_quote(claim, policy_docs: list[dict], bm25: BM25Okapi) -> dict:
    """Where (if anywhere) the claim's quote exists. Checks whole pages so a
    quote spanning a chunk boundary is still found."""
    out = {"quote_status": "UNKNOWN_SOURCE", "matched_document": None, "matched_page": None, "evidence": ""}
    doc = resolve_source_document(claim.source_policy)
    quote = claim.exact_quote_from_source or ""

    if doc is not None:
        pages = sorted({c["page_number"] for c in policy_docs if c["source_document"] == doc})
        order = [claim.page_number] + [p for p in pages if p != claim.page_number]
        best_fuzzy = None
        for p in order:
            text = _page_text(policy_docs, doc, p)
            if not text:
                continue
            level, _ = locate_in_text(quote, text)
            where = "CLAIMED_PAGE" if p == claim.page_number else "OTHER_PAGE"
            if level in ("EXACT", "STITCHED"):
                return {"quote_status": f"{level}_{where}", "matched_document": doc, "matched_page": p, "evidence": text}
            if level == "FUZZY" and best_fuzzy is None:
                best_fuzzy = {"quote_status": f"FUZZY_{where}", "matched_document": doc, "matched_page": p, "evidence": text}
        if best_fuzzy:
            return best_fuzzy

    # Layer 2: BM25 across all four brochures (quote cited under the wrong policy?)
    q = tokens(quote)
    if q:
        scores = bm25.get_scores(q)
        for idx in sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:5]:
            c = policy_docs[idx]
            text = _page_text(policy_docs, c["source_document"], c["page_number"])
            if locate_in_text(quote, text)[0] in ("EXACT", "STITCHED"):
                return {"quote_status": "FOUND_IN_OTHER_DOC", "matched_document": c["source_document"],
                        "matched_page": c["page_number"], "evidence": text}

    out["quote_status"] = "NOT_FOUND_ANYWHERE" if doc is not None else "UNKNOWN_SOURCE"
    return out


def _retrieve_evidence(text: str, policy_name: str, policy_docs: list[dict], top_n: int = 3) -> dict:
    """Evidence for an unquoted claim (talking point): best-matching pages of the named policy."""
    doc = resolve_source_document(policy_name)
    pool = [c for c in policy_docs if doc is None or c["source_document"] == doc]
    if not pool:
        return {"quote_status": "NO_EVIDENCE", "matched_document": doc, "matched_page": None, "evidence": ""}
    bm = BM25Okapi([tokens(c["text"]) or ["_"] for c in pool])
    scores = bm.get_scores(tokens(text) or ["_"])
    ranked = sorted(range(len(pool)), key=lambda i: scores[i], reverse=True)[:top_n]
    ranked = [i for i in ranked if scores[i] > 0]
    if not ranked:
        return {"quote_status": "NO_EVIDENCE", "matched_document": doc, "matched_page": None, "evidence": ""}
    parts = [f"[{pool[i]['source_document']} p.{pool[i]['page_number']}]\n{pool[i]['text']}" for i in ranked]
    return {"quote_status": "RETRIEVED_EVIDENCE", "matched_document": pool[ranked[0]]["source_document"],
            "matched_page": pool[ranked[0]]["page_number"], "evidence": "\n\n".join(parts)}


# ============================================================
# Layer 3 — batched, independent semantic check
# ============================================================

SEMANTIC_PROMPT = """You are a strict insurance compliance auditor. You did NOT write these
claims. For EACH claim, decide whether its SOURCE TEXT supports what the claim
says the insurance POLICY provides.

Judge ONLY the policy facts in the claim (features, amounts, limits, time
periods, conditions, which product/feature it is). Do NOT judge whether the
benefit suits the client or their risks — the brochure will never mention the
client, and that is fine. Paraphrasing is fine. The source text is extracted
from a designed brochure, so its lines may be out of order; treat headings and
the sentences near them as belonging together.

Verdicts:
- SUPPORTED: every policy fact in the claim is stated in the source.
- SUPPORTED_WITH_CONDITION: the facts are in the source, but the claim leaves
  out a condition the source attaches to them (e.g. optional add-on / extra
  premium, only in a specific plan or sum insured, a sub-limit, "up to").
  Put that condition in missing_condition.
- PARTIALLY_SUPPORTED: some facts are supported, but at least one number,
  limit or feature detail is overstated, altered or not in the source.
- NOT_SUPPORTED: the source does not state it, contradicts it, or the claim
  attributes a feature to the wrong named benefit (e.g. describing what
  "Claim Protect" does under the name "HealthReturns").

Return one verdict per claim id.

CLAIMS:
{claims}
"""


def _semantic_batch(items: list[dict], api_key: str) -> tuple[dict[str, _Verdict], str]:
    """items: [{id, claim, evidence}] -> ({id: verdict}, error_message)."""
    blocks = []
    for it in items:
        ev = it["evidence"]
        if len(ev) > 3500:
            ev = ev[:3500] + " …"
        blocks.append(f"[{it['id']}] CLAIM: {it['claim']}\nSOURCE TEXT:\n\"\"\"{ev}\"\"\"")
    prompt = SEMANTIC_PROMPT.format(claims="\n\n".join(blocks))
    try:
        result = invoke_structured(prompt, _BatchVerdicts, stage="audit", temperature=0.0, api_key=api_key)
    except LLMCallError as e:
        return {}, str(e)
    out = {}
    for v in result.verdicts:
        cid = (v.claim_id or "").strip().upper().strip("[]")
        v.verdict = (v.verdict or "").strip().upper().replace(" ", "_")
        out[cid] = v
    return out, ""


def _semantic_check(claim, source_text: str, api_key: str, max_retries: int = 2) -> tuple[str, str]:
    """Single-claim check (kept for callers/tests that use it directly)."""
    verdicts, err = _semantic_batch([{"id": "C1", "claim": claim.matched_benefit, "evidence": source_text}], api_key)
    v = verdicts.get("C1")
    if v is None:
        return "UNCHECKED", f"Semantic check could not run: {err or 'no verdict returned'}"
    return v.verdict, v.reason


# ============================================================
# Combining layers
# ============================================================

def _decide(loc_status: str, verdict: str, reason: str, missing: str, claim_type: str) -> tuple[str, str, str]:
    """-> (final_status, recommended_action, reason_suffix)"""
    located_exact = loc_status.startswith(("EXACT_CLAIMED", "STITCHED_CLAIMED"))
    located_moved = loc_status.startswith(("EXACT_OTHER", "STITCHED_OTHER")) or loc_status == "FOUND_IN_OTHER_DOC"
    fuzzy = loc_status.startswith("FUZZY")
    retrieved = loc_status == "RETRIEVED_EVIDENCE"

    if verdict == "UNCHECKED":
        return "UNCHECKED", "Re-run the audit — the independent check could not run for this claim.", ""
    if verdict == "NOT_SUPPORTED":
        return "REJECTED", "Reject — remove or rewrite this claim; the source does not support it.", ""
    if verdict == "PARTIALLY_SUPPORTED":
        return "FLAGGED", "Edit — correct the overstated or altered detail to match the source wording.", ""

    if verdict in ("SUPPORTED", "SUPPORTED_WITH_CONDITION"):
        notes = []
        if verdict == "SUPPORTED_WITH_CONDITION":
            notes.append(f"add the condition: {missing}" if missing else "add the condition the source attaches")
        if located_moved:
            notes.append("correct the citation to the page/brochure where the text actually appears")
        if fuzzy:
            notes.append("quote wording differs from the source; use the exact source wording")
        if notes:
            return ("VERIFIED_WITH_CONDITION",
                    "Approve after a minor edit — " + "; ".join(notes) + ".", "")
        if located_exact or retrieved:
            return "VERIFIED", "Approve — claim is accurately grounded in the source.", ""
    return "FLAGGED", "Edit — review manually; the checker returned an unrecognised verdict.", ""


# ============================================================
# Main entry point
# ============================================================

def audit_pitch_content(
    pitch: PitchContent,
    policy_docs: list[dict],
    api_key: str = None,
    profile=None,
) -> AuditReport:
    """
    Objective 2.1 / 2.2.

    Args:
        pitch: generated PitchContent (Stage 5)
        policy_docs: full chunk list of the four brochures (the ground truth)
        api_key: optional key override for the audit route (default: .env)
        profile: optional CompanyProfile — its web-sourced facts are listed in
                 the report as external statements (not scored)
    """
    from src.audit.claim_extractor import extract_claims_from_pitch, extract_other_statements

    claims = extract_claims_from_pitch(pitch)
    others = [OtherStatement(**o) for o in extract_other_statements(pitch, profile)]
    route = str(get_route("audit"))
    stamp = datetime.now().strftime("%d %b %Y, %H:%M")

    if not claims:
        return AuditReport(total_claims=0, verified_count=0, flagged_count=0, rejected_count=0,
                           confidence_score=0.0, overall_status="NO CLAIMS TO AUDIT", claim_results=[],
                           other_statements=others, audit_model=route, audited_at=stamp,
                           notes=["The pitch contained no policy claims to audit."])

    try:
        resolved_key = get_api_key("audit", api_key)
        key_problem = ""
    except LLMConfigError as e:
        resolved_key, key_problem = None, str(e)

    bm25 = BM25Okapi([tokens(c["text"]) or ["_"] for c in policy_docs])

    # Layers 1-2
    located = []
    for c in claims:
        if c["claim_type"] == "benefit":
            loc = _locate_quote(c["claim"], policy_docs, bm25)
        else:
            loc = _retrieve_evidence(c["claim"].matched_benefit, c["claim"].source_policy, policy_docs)
        located.append(loc)

    # Layer 3 (one batched call) for everything that has evidence
    to_check = [{"id": c["claim_id"], "claim": c["claim"].matched_benefit, "evidence": loc["evidence"]}
                for c, loc in zip(claims, located)
                if loc["evidence"] and loc["quote_status"] not in ("NOT_FOUND_ANYWHERE", "UNKNOWN_SOURCE", "NO_EVIDENCE")]
    verdicts, sem_error = ({}, key_problem) if not resolved_key else (_semantic_batch(to_check, resolved_key) if to_check else ({}, ""))

    results: list[ClaimAuditResult] = []
    for c, loc in zip(claims, located):
        cl = c["claim"]
        base = dict(claim_id=c["claim_id"], claim_type=c["claim_type"], section=c["section"], risk=cl.risk,
                    matched_benefit=cl.matched_benefit, source_policy=cl.source_policy, page_number=cl.page_number,
                    exact_quote_from_source=cl.exact_quote_from_source, quote_status=loc["quote_status"],
                    matched_document=loc["matched_document"], matched_page=loc["matched_page"],
                    evidence_excerpt=_excerpt(loc["evidence"], cl.exact_quote_from_source or cl.matched_benefit))

        if loc["quote_status"] in ("NOT_FOUND_ANYWHERE", "UNKNOWN_SOURCE"):
            why = ("The quoted text does not appear in any of the four brochures."
                   if loc["quote_status"] == "NOT_FOUND_ANYWHERE"
                   else f"'{cl.source_policy}' is not one of the four source policies.")
            results.append(ClaimAuditResult(**base, semantic_verdict="SKIPPED", semantic_reason=why,
                                            final_status="REJECTED", confidence_weight=0.0,
                                            recommended_action="Reject — remove or rewrite this claim; it cannot be traced to a source."))
            continue
        if loc["quote_status"] == "NO_EVIDENCE":
            results.append(ClaimAuditResult(**base, semantic_verdict="SKIPPED",
                                            semantic_reason="No supporting text found in the recommended policy's brochure.",
                                            final_status="REJECTED", confidence_weight=0.0,
                                            recommended_action="Reject — remove this talking point or rewrite it from a brochure benefit."))
            continue

        v = verdicts.get(c["claim_id"])
        if v is None or v.verdict not in ("SUPPORTED", "SUPPORTED_WITH_CONDITION", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED"):
            verdict = "UNCHECKED"
            reason = (f"Independent check could not run: {sem_error}" if sem_error
                      else "The checker returned no valid verdict for this claim.")
            missing = ""
        else:
            verdict, reason, missing = v.verdict, v.reason, v.missing_condition
        status, action, _ = _decide(loc["quote_status"], verdict, reason, missing, c["claim_type"])
        results.append(ClaimAuditResult(**base, semantic_verdict=verdict, semantic_reason=reason,
                                        missing_condition=missing or "", final_status=status,
                                        confidence_weight=STATUS_WEIGHT.get(status), recommended_action=action))

    return _summarise(results, others, route, stamp, sem_error)


def _excerpt(evidence: str, anchor: str, width: int = 320) -> str:
    """Short source excerpt around the best-matching words, for the report."""
    ev = " ".join((evidence or "").split())
    if not ev:
        return ""
    words = [w for w in tokens(anchor) if len(w) > 3][:6]
    low = normalize(ev)
    pos = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
    start = max(0, pos - 60)
    snippet = ev[start:start + width]
    return ("… " if start else "") + snippet + (" …" if start + width < len(ev) else "")


def _summarise(results, others, route, stamp, sem_error) -> AuditReport:
    count = lambda s: sum(1 for r in results if r.final_status == s)  # noqa: E731
    total = len(results)
    checked = [r for r in results if r.final_status != "UNCHECKED"]
    weighted = (100 * sum(r.confidence_weight or 0 for r in checked) / len(checked)) if checked else 0.0
    v, vc, f, rj, u = (count(s) for s in ("VERIFIED", "VERIFIED_WITH_CONDITION", "FLAGGED", "REJECTED", "UNCHECKED"))

    notes = []
    if u:
        notes.append(f"{u} of {total} claims could not be independently checked"
                     + (f" ({sem_error})" if sem_error else "") + ". Re-run the audit before relying on this report.")
    if rj:
        status = "REJECT — REGENERATE" if rj / total >= 0.5 else "NEEDS HUMAN REVIEW"
    elif f:
        status = "NEEDS HUMAN REVIEW"
    elif u == total:
        status = "AUDIT INCOMPLETE"
    elif u:
        status = "AUDIT INCOMPLETE"
    elif vc:
        status = "PASS WITH CONDITIONS"
    else:
        status = "PASS"

    return AuditReport(
        total_claims=total, verified_count=v, conditional_count=vc, flagged_count=f, rejected_count=rj,
        unchecked_count=u, confidence_score=round(weighted, 1),
        fully_verified_pct=round(100 * v / total, 1) if total else 0.0,
        overall_status=status, claim_results=results, other_statements=others,
        audit_model=route, audited_at=stamp, notes=notes,
    )
