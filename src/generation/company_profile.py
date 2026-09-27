"""
Stage 3 — generateCompanyProfile(company_name)

Researches a client company and returns a structured profile: industry,
size, sourced company facts, and key workforce health risks to match
against the policy documents.

Design decisions:
- LIVE RESEARCH FIRST (Tavily, src/research/web_search.py). A few focused
  web searches return short page extracts with URLs, numbered S1..Sn. The
  LLM builds the profile ONLY from those extracts, and every company fact
  must cite the source id it came from. Previously the profile came from
  the model's memory alone, so figures like employee counts were guesses.
- DETERMINISTIC CHECKS AFTER THE LLM (no AI involved):
    * a fact citing a source id that doesn't exist is removed and listed
      as an unsourced assumption (the model invented the citation);
    * every number in a fact must appear in its cited extract (commas and
      spaces ignored, so "3,28,594" matches "328594"); if not, the fact
      is kept but marked verified_in_source=False for the advisor/audit.
- CLEARLY-LABELLED FALLBACK (brief, Objective 1.2): if Tavily is not
  configured, fails, or finds nothing, the profile is built from model
  knowledge with data_source="model_knowledge", NO facts, and every
  uncertain value prefixed "Assumption:" — the deck and audit show this.
- Web research is for the CLIENT COMPANY only. Policy benefits remain
  grounded exclusively in the 4 policy PDFs (closed-world constraint).
- Provider/model: the "profile" route in src/config.py (OpenRouter).
  Structured output via the shared invoke_structured() (retries and
  json_schema -> function_calling -> json_prompt fallback).
- Objective 1.4 (input validation): missing/empty company_name raises a
  clear, catchable CompanyProfileError; the UI turns it into a message.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from src.config import invoke_structured, get_api_key, LLMConfigError, LLMCallError
from src.research.web_search import ResearchResult, research_company


# ------------------------------------------------------------
# Schemas
# ------------------------------------------------------------

class _DraftFact(BaseModel):
    label: str = Field(description="Short label, e.g. 'Employees', 'Revenue', 'Headquarters', 'Global presence'")
    value: str = Field(description="The fact, copied as precisely as possible from the cited source, including its date/period if stated")
    source_id: str = Field(description="The id of the ONE source this fact comes from, e.g. 'S2'")


class _ProfileDraft(BaseModel):
    """What the LLM fills in."""
    industry: str = Field(description="The company's primary industry/sector")
    size_estimate: str = Field(
        description="Company size in one short line. If taken from a source, state it plainly "
                    "(e.g. '3,28,594 employees (Mar 2026)'); otherwise prefix with 'Assumption: '"
    )
    facts: list[_DraftFact] = Field(
        default_factory=list,
        description="3-8 key company facts, each copied from exactly one cited source. Empty if no sources."
    )
    key_risks: list[str] = Field(
        description="3-5 occupational/health-related risk factors for this company's workforce, "
                    "for matching against insurance policy benefits"
    )
    assumptions_used: list[str] = Field(
        default_factory=list,
        description="Plain-language list of everything inferred rather than taken from a source "
                    "(including the key risks, which are inferences about the workforce)"
    )


class SourcedFact(BaseModel):
    label: str
    value: str
    source_id: str
    source_title: str
    source_url: str
    verified_in_source: bool = Field(
        description="True if every number in `value` appears in the cited extract (deterministic check)"
    )


class CompanyProfile(BaseModel):
    industry: str = Field(description="The company's primary industry/sector")
    size_estimate: str = Field(description="Company size; prefixed 'Assumption:' when not from a source")
    key_risks: list[str] = Field(description="3-5 workforce health risk factors")
    assumptions_used: list[str] = Field(default_factory=list)
    facts: list[SourcedFact] = Field(default_factory=list, description="Company facts with their web sources")
    data_source: str = Field(default="model_knowledge", description="'live_web' or 'model_knowledge'")
    sources: list[dict] = Field(default_factory=list, description="[{id, title, url}] of sources used in the prompt")
    research_notes: list[str] = Field(default_factory=list, description="Research problems (e.g. key missing)")


class CompanyProfileError(Exception):
    """Raised when company profile generation fails or input is invalid."""
    pass


# ------------------------------------------------------------
# Deterministic post-checks
# ------------------------------------------------------------

_NUM = re.compile(r"\d[\d,\s.]*\d|\d")


def _numbers(text: str) -> list[str]:
    """Digit groups in text with separators removed ('3,28,594' -> '328594'; '1.8' -> '18')."""
    out = []
    for m in _NUM.findall(text or ""):
        digits = re.sub(r"[,\s]", "", m).replace(".", "")
        if digits:
            out.append(digits)
    return out


def number_supported(value: str, source_text: str) -> bool:
    """Every number in `value` must appear (separators ignored) in `source_text`."""
    haystack = re.sub(r"[,\s.]", "", source_text or "")
    return all(n in haystack for n in _numbers(value))


def _finalise(draft: _ProfileDraft, research: ResearchResult) -> CompanyProfile:
    by_id = research.by_id()
    facts: list[SourcedFact] = []
    assumptions = list(draft.assumptions_used)

    for f in draft.facts:
        sid = f.source_id.strip().upper().strip("[]")
        src = by_id.get(sid)
        if src is None:
            assumptions.append(f"Unsourced (removed from facts — cited '{f.source_id}' which does not exist): "
                               f"{f.label}: {f.value}")
            continue
        facts.append(SourcedFact(
            label=f.label.strip(), value=f.value.strip(), source_id=sid,
            source_title=src.title, source_url=src.url,
            verified_in_source=number_supported(f.value, src.content),
        ))

    unverified = [f for f in facts if not f.verified_in_source]
    if unverified:
        assumptions.append(
            "Figures not found verbatim in their cited source (check before use): "
            + "; ".join(f"{f.label}: {f.value} [{f.source_id}]" for f in unverified)
        )

    live = bool(research.sources)
    if not live:
        assumptions.insert(0, "No live web data was available for this company; the profile is based on "
                              "general model knowledge and every company figure is an assumption.")
        facts = []  # nothing to cite -> no facts, by design

    return CompanyProfile(
        industry=draft.industry.strip(),
        size_estimate=draft.size_estimate.strip(),
        key_risks=[r.strip() for r in draft.key_risks if r.strip()],
        assumptions_used=assumptions,
        facts=facts,
        data_source="live_web" if live else "model_knowledge",
        sources=[{"id": s.id, "title": s.title, "url": s.url} for s in research.sources],
        research_notes=list(research.errors),
    )


# ------------------------------------------------------------
# Prompts
# ------------------------------------------------------------

_RISK_GUIDE = """3-5 key occupational/health-related risk factors for the typical workforce
   (e.g. sedentary desk work, high-stress roles, shift work, physical labour,
   travel-heavy or overseas roles, large young workforce, dependants' care) —
   these will be matched against health insurance policy benefits."""


def _prompt_with_sources(company: str, research: ResearchResult) -> str:
    blocks = "\n\n".join(f"[{s.id}] {s.title}\nURL: {s.url}\n{s.content}" for s in research.sources)
    return f"""You are researching a company for an insurance advisor who will pitch
health insurance to it. Use ONLY the web sources below for company facts.

Company: {company}

WEB SOURCES:
{blocks}

Provide:
1. industry — from the sources.
2. size_estimate — employee count from the sources, with its date. If the
   sources do not state it, prefix with "Assumption: ".
3. facts — 3-8 key facts (e.g. employees, revenue, headquarters, global
   presence/offices, workforce mix). RULES:
   - each fact comes from exactly ONE source and cites its id (e.g. "S2");
   - copy numbers exactly as the source writes them, with the period/date;
   - never combine numbers from different sources into one fact;
   - if sources disagree, use the most recent and official one.
4. key_risks — {_RISK_GUIDE}
   Tie them to facts where possible (e.g. large global workforce -> overseas roles).
5. assumptions_used — everything you inferred rather than read in a source,
   including that the key risks are inferred.

Do NOT use outside knowledge for any company figure. Do not state uncertain
information as confirmed fact.
"""


def _prompt_without_sources(company: str) -> str:
    return f"""You are researching a company for an insurance advisor who will pitch
health insurance to it. No live web data is available, so rely on general
knowledge and be explicit about uncertainty.

Company: {company}

Provide:
1. industry
2. size_estimate — ALWAYS prefixed with "Assumption: " (no live data).
3. facts — leave EMPTY (there are no sources to cite).
4. key_risks — {_RISK_GUIDE}
5. assumptions_used — every inferred item, including the size and the risks.
"""


# ------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------

def generate_company_profile(company_name: str, api_key: str = None,
                             research: ResearchResult | None = None) -> CompanyProfile:
    """
    Objective 1.2 implementation.

    Args:
        company_name: name of the client company, e.g. "Infosys"
        api_key: optional OpenRouter key override (default: .env)
        research: optional pre-computed ResearchResult (tests / UI reuse);
                  by default research_company() is called here.

    Raises:
        CompanyProfileError: on missing input or LLM failure (Objective 1.4).
        Research failures are NOT errors — they trigger the labelled fallback.
    """
    if not company_name or not company_name.strip():
        raise CompanyProfileError("Company name is required and cannot be empty.")

    try:
        resolved_key = get_api_key("profile", api_key)
    except LLMConfigError as e:
        raise CompanyProfileError(str(e)) from e

    company_name = company_name.strip()
    if research is None:
        research = research_company(company_name)

    prompt = _prompt_with_sources(company_name, research) if research.ok else _prompt_without_sources(company_name)

    try:
        draft = invoke_structured(prompt, _ProfileDraft, stage="profile", temperature=0.1, api_key=resolved_key)
    except LLMCallError as e:
        raise CompanyProfileError(f"Company profile generation failed: {e}") from e

    return _finalise(draft, research)


if __name__ == "__main__":
    # python -m src.generation.company_profile Infosys
    import sys

    test_company = sys.argv[1] if len(sys.argv) > 1 else "Infosys"
    print(f"Generating profile for: {test_company}\n")
    try:
        profile = generate_company_profile(test_company)
    except CompanyProfileError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print(f"Data source: {profile.data_source} ({len(profile.sources)} web sources)")
    for n in profile.research_notes:
        print(f"  research note: {n}")
    print(profile.model_dump_json(indent=2))
