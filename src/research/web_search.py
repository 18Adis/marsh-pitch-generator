"""
Stage 3a — Live company research via Tavily (web search API for AI apps).

Why this exists: generateCompanyProfile() previously relied only on the
LLM's own memory, so company facts were guesses presented with an
"Assumption:" label (e.g. "300k-350k employees, based on FY2024"). The
brief asks the profile step to GATHER company info and fall back to
labelled assumptions only when data is unavailable. Tavily returns short,
relevant page extracts with their URLs, so every company fact in the
pitch can carry a source the advisor (and the audit report) can check.

Scope boundary (important — see CLAUDE.md "closed-world constraint"):
web research is used ONLY for facts about the CLIENT COMPANY. Insurance
policy benefits are still grounded exclusively in the 4 policy PDFs;
nothing from the web ever reaches a policy claim.

Implementation notes:
- Plain HTTPS POST with urllib — no extra package to install.
- A handful of focused queries (overview, size, workforce/locations,
  employee health) rather than one vague query; results are de-duplicated
  by URL and trimmed so the profile prompt stays small.
- Failure is never fatal: a missing key, network error or empty result
  returns a ResearchResult with `sources=[]` and the reason in `errors`,
  and the profile step falls back to clearly-labelled model knowledge.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_KEY_ENV = "TAVILY_API_KEY"

MAX_RESULTS_PER_QUERY = 4
MAX_SOURCES = 8            # total sources passed to the profile prompt
MAX_CHARS_PER_SOURCE = 900 # trims each extract so the prompt stays small
REQUEST_TIMEOUT = 30

QUERY_TEMPLATES = [
    "{company} company overview industry headquarters",
    "{company} total number of employees annual report revenue",
    "{company} workforce offices locations countries employees",
    "{company} employee health wellbeing insurance benefits",
]


class TavilyError(Exception):
    """Raised for a failed Tavily request (caught inside research_company)."""


@dataclass
class Source:
    id: str            # "S1", "S2", ... (what the LLM cites)
    title: str
    url: str
    content: str
    query: str
    score: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "url": self.url,
                "content": self.content, "query": self.query, "score": self.score}


@dataclass
class ResearchResult:
    company: str
    sources: list[Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.sources)

    def by_id(self) -> dict[str, Source]:
        return {s.id: s for s in self.sources}


def get_tavily_key() -> str | None:
    key = os.environ.get(TAVILY_KEY_ENV, "").strip()
    return key or None


def tavily_search(query: str, api_key: str, max_results: int = MAX_RESULTS_PER_QUERY) -> list[dict]:
    """One Tavily search. Returns the raw `results` list."""
    body = json.dumps({
        "query": query,
        "search_depth": "basic",
        "topic": "general",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        hint = {401: "invalid TAVILY_API_KEY", 429: "rate limited", 432: "plan limit reached", 433: "pay-as-you-go limit reached"}.get(e.code, "")
        raise TavilyError(f"HTTP {e.code} {hint} {detail}".strip()) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TavilyError(f"network error: {e}") from e
    return payload.get("results") or []


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def research_company(company: str, api_key: str | None = None,
                     templates: list[str] | None = None) -> ResearchResult:
    """
    Runs the focused queries, merges and de-duplicates results by URL
    (keeping the highest-scoring hit), and returns the top MAX_SOURCES as
    numbered sources S1..Sn. Never raises — problems go into `errors`.
    """
    result = ResearchResult(company=company)
    key = api_key or get_tavily_key()
    if not key:
        result.errors.append(f"{TAVILY_KEY_ENV} not set — live company research skipped")
        return result

    best: dict[str, dict] = {}
    for template in (templates or QUERY_TEMPLATES):
        query = template.format(company=company)
        result.queries.append(query)
        try:
            hits = tavily_search(query, key)
        except TavilyError as e:
            result.errors.append(f"'{query}': {e}")
            if "invalid" in str(e) or "limit" in str(e):
                break  # key/plan problem — further queries will fail the same way
            continue
        for h in hits:
            url, content = h.get("url"), _clean(h.get("content", ""))
            if not url or len(content) < 40:
                continue
            score = float(h.get("score") or 0.0)
            if url not in best or score > best[url]["score"]:
                best[url] = {"title": _clean(h.get("title", "")) or url, "url": url,
                             "content": content[:MAX_CHARS_PER_SOURCE], "query": query, "score": score}

    ranked = sorted(best.values(), key=lambda h: h["score"], reverse=True)[:MAX_SOURCES]
    result.sources = [Source(id=f"S{i}", **h) for i, h in enumerate(ranked, 1)]
    if not result.sources and not result.errors:
        result.errors.append("Tavily returned no usable results")
    return result


if __name__ == "__main__":
    # python -m src.research.web_search Infosys
    import sys
    from src.config import ENV_FILE_HINT  # noqa: F401  (loads .env)

    name = sys.argv[1] if len(sys.argv) > 1 else "Infosys"
    res = research_company(name)
    print(f"Queries: {len(res.queries)} | sources: {len(res.sources)} | errors: {res.errors or 'none'}")
    for s in res.sources:
        print(f"\n[{s.id}] {s.title}\n     {s.url}\n     {s.content[:220]}...")
