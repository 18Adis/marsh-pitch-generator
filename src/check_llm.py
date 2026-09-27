"""
LLM health check for the per-stage routing — run from the project root:

    python -m src.check_llm                          # test every stage's route + Tavily
    python -m src.check_llm openrouter:<model> ...   # test specific model(s)
    python -m src.check_llm --list                   # list free OpenRouter models with structured output
    python -m src.check_llm --tavily Infosys         # test live company research only
    python -m src.check_llm --key                    # check the OpenRouter key format + that OpenRouter accepts it

Times ONE small structured-output call per route through the exact call
path the pipeline uses, and reports which output method worked
(json_schema / function_calling / json_prompt). Use it to pick models,
then set LLM_PROFILE / LLM_PITCH / LLM_AUDIT in .env.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

from src.config import (
    DEFAULT_ROUTES, PROVIDERS, REQUEST_TIMEOUT, STAGE_LABELS, Route,
    describe_routing, get_route, invoke_structured, missing_keys, parse_route,
    LLMCallError, LLMConfigError, _working_method,
)


class _Ping(BaseModel):
    company: str = Field(description="The company name, exactly as given")
    industry: str = Field(description="The company's industry in 2-4 words")


def list_models(provider: str) -> None:
    if provider not in PROVIDERS:
        print(f"Unknown provider '{provider}'. Use: {', '.join(PROVIDERS)}")
        return
    key = os.environ.get(PROVIDERS[provider]["key_env"])
    if not key:
        print(f"ERROR: {PROVIDERS[provider]['key_env']} is not set in .env")
        return
    req = urllib.request.Request(f"{PROVIDERS[provider]['base_url']}/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        models = json.load(r)["data"]
    models = [m for m in models if m["id"].endswith(":free")
              and {"structured_outputs", "tools"} & set(m.get("supported_parameters") or [])]
    print(f"{len(models)} free OpenRouter models support structured output / tool calling:\n")
    models.sort(key=lambda m: m.get("context_length") or m.get("context_window") or 0, reverse=True)
    for m in models:
        print(f"  {provider}:{m['id']:<58} context {m.get('context_length') or m.get('context_window') or '?'}")
    print("\nTest one with:  python -m src.check_llm <provider:model id>")


def check_openrouter_key() -> bool:
    """Diagnoses the key WITHOUT printing it: format checks, then asks OpenRouter."""
    from src.config import ENV_FILE_HINT
    raw = os.environ.get("OPENROUTER_API_KEY")
    print(f".env file: {ENV_FILE_HINT}  (exists: {ENV_FILE_HINT.exists()})")
    if raw is None:
        print("PROBLEM: OPENROUTER_API_KEY is not set in .env")
        return False
    k = raw
    problems = []
    if k != k.strip():
        problems.append("has spaces/newline at the start or end")
    if any(c in k for c in "\"'"):
        problems.append("contains quote characters")
    if " " in k.strip() or "#" in k:
        problems.append("contains a space or '#' (text after the key on the same line?)")
    if not k.strip().startswith("sk-or-v1-"):
        problems.append("does not start with 'sk-or-v1-' (placeholder text or wrong key?)")
    if any(ord(c) > 126 for c in k):
        problems.append("contains hidden non-ASCII characters (retype or re-copy it)")
    print(f"Key length: {len(k)} | starts with sk-or-v1-: {k.strip().startswith('sk-or-v1-')}")
    for p in problems:
        print(f"PROBLEM: key {p}")
    req = urllib.request.Request(f"{PROVIDERS['openrouter']['base_url']}/key",
                                 headers={"Authorization": f"Bearer {k.strip()}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r).get("data", {})
        print(f"RESULT: OK — OpenRouter accepts this key (label: {data.get('label', '?')}, "
              f"free tier: {data.get('is_free_tier', '?')})")
        return True
    except urllib.error.HTTPError as e:
        print(f"RESULT: REJECTED — HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        print("Fix: create a new key at openrouter.ai/keys and paste it into .env as\n"
              "     OPENROUTER_API_KEY=sk-or-v1-....   (one line, no quotes, nothing after it)")
        return False


def check_tavily(company: str = "Infosys") -> bool:
    from src.research.web_search import research_company, get_tavily_key, TAVILY_KEY_ENV
    print(f"\n=== Live company research: Tavily ({company}) ===")
    if not get_tavily_key():
        print(f"RESULT: SKIPPED — {TAVILY_KEY_ENV} not set (profiles will use labelled assumptions)")
        return False
    started = time.time()
    res = research_company(company)
    if not res.ok:
        print(f"RESULT: FAILED after {time.time() - started:.0f}s — {'; '.join(res.errors)}")
        return False
    print(f"RESULT: OK in {time.time() - started:.0f}s — {len(res.sources)} sources")
    for s in res.sources[:4]:
        print(f"  [{s.id}] {s.title[:70]}  {s.url[:80]}")
    if res.errors:
        print(f"  (some queries failed: {'; '.join(res.errors)})")
    return True


def ping(route: Route) -> bool:
    print(f"\n=== {STAGE_LABELS.get(route.stage, 'Test')}: {route} ===")
    started = time.time()
    try:
        out = invoke_structured("Company: Infosys. Return the company name and its industry.",
                                _Ping, stage=route.stage, route=route, temperature=0.0, max_retries=3)
    except (LLMCallError, LLMConfigError) as e:
        print(f"RESULT: FAILED after {time.time() - started:.0f}s\n  {e}")
        return False
    print(f"RESULT: OK in {time.time() - started:.0f}s via {_working_method.get(str(route))} -> {out.model_dump()}")
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--list"]:
        list_models("openrouter")
        sys.exit(0)
    if args[:1] == ["--key"]:
        sys.exit(0 if check_openrouter_key() else 1)
    if args[:1] == ["--tavily"]:
        sys.exit(0 if check_tavily(args[1] if len(args) > 1 else "Infosys") else 1)

    print("Current routing:\n" + describe_routing())
    print(f"Timeout per call: {int(REQUEST_TIMEOUT)}s")
    missing = missing_keys()
    if missing and not args:
        for k, stages in missing.items():
            print(f"ERROR: {k} is missing from .env (needed by: {', '.join(stages)})")
        sys.exit(1)

    if args:
        routes = [parse_route(a, stage="pitch") for a in args]  # stage only sets the log label
    else:
        seen, routes = set(), []
        for stage in DEFAULT_ROUTES:
            r = get_route(stage)
            if str(r) not in seen:
                seen.add(str(r))
                routes.append(r)

    results = {str(r): ping(r) for r in routes}
    if not args:
        results["tavily (company research)"] = check_tavily()
    print("\nSummary:")
    for r, ok in results.items():
        print(f"  {'OK    ' if ok else 'FAILED'}  {r}")
