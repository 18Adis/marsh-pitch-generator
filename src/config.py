"""
Central LLM configuration — ONE provider (OpenRouter), routed PER STAGE,
one shared call path. (Company research uses Tavily — see
src/research/web_search.py; that is web search, not an LLM.)

Stage routing (defaults; override each with one line in .env):
    LLM_PROFILE = openrouter:nvidia/nemotron-3-ultra-550b-a55b:free   Stage 3 company profile
    LLM_PITCH   = openrouter:nvidia/nemotron-3-ultra-550b-a55b:free   Stage 5 pitch writing
    LLM_AUDIT   = openrouter:nvidia/nemotron-3-ultra-550b-a55b:free   Stage 6 audit semantic check
Format is "<provider>:<model id>" (split on the FIRST colon only, so
OpenRouter ids like "...:free" work). Routing per stage is kept so the
audit can be pointed at a DIFFERENT model from the pitch writer (so the
auditor is not grading its own output) with one line: LLM_AUDIT=...

Provider history (why OpenRouter only): stages previously ran on three
providers at once (Gemini, OpenRouter, Groq). The app only checked the
OpenRouter key, so with no Groq key the audit's semantic layer was
silently skipped and every claim came out FLAGGED (0% confidence in the
L&T Mindtree report). Groq was then tried for profile + audit, but its
free tier for qwen/qwen3.8-27b allowed only 1,000 output tokens per
minute and qwen's structured output broke (json_schema -> "{", malformed
tool calls). Groq was removed; everything runs on OpenRouter, where
nemotron returned valid structured output in ~1-2 s.

.env (project root, gitignored):
    OPENROUTER_API_KEY=...
    TAVILY_API_KEY=...          # live company research (optional but recommended)
    # optional: LLM_PROFILE / LLM_PITCH / LLM_AUDIT, LLM_TIMEOUT=180,
    # LLM_MAX_TOKENS_PROFILE / _PITCH / _AUDIT, OPENROUTER_REASONING=low

STRUCTURED OUTPUT — why there are three methods (found via a real failure):
nemotron-3-ultra (a reasoning model) spent its entire 3,849-token output
budget on hidden reasoning and returned content='' with finish_reason
"error"; LangChain then raised "Structured Output response does not have
a 'parsed' field". Models also differ in which structured-output mechanism
they support. So invoke_structured() tries, per model:
  1. json_schema       — provider-enforced schema (best when supported)
  2. function_calling  — schema passed as a tool (widely supported)
  3. json_prompt       — plain completion; schema in the prompt; we extract
                         and validate the JSON ourselves with Pydantic
and remembers which one worked for that model for the rest of the run.
Reasoning models are asked to keep reasoning low/hidden so they leave
budget for the actual answer; if a provider rejects those reasoning
parameters for a model, the call is retried once without them.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Type, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE_HINT = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE_HINT, override=True)  # .env wins over stale shell vars

# ------------------------------------------------------------
# Providers and per-stage routing
# ------------------------------------------------------------

PROVIDERS = {
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
}

DEFAULT_ROUTES = {
    "profile": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
    "pitch": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
    "audit": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
}
STAGE_LABELS = {"profile": "Company profile", "pitch": "Pitch generation", "audit": "Audit check"}

REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", os.environ.get("OPENROUTER_TIMEOUT", "180")))
# Max output tokens requested PER STAGE (override with LLM_MAX_TOKENS_PROFILE
# / _PITCH / _AUDIT). Some providers count the *requested* max_tokens against
# a per-minute output limit (found via a real "429 Request too large" failure),
# so short-reply stages ask for less. If a provider reports a lower limit,
# the cap is learned at runtime (see _learned_cap).
DEFAULT_STAGE_MAX_TOKENS = {"profile": 3000, "pitch": 8000, "audit": 4000}
MIN_OUTPUT_TOKENS = 512


def stage_max_tokens(stage: str) -> int:
    return int(os.environ.get(f"LLM_MAX_TOKENS_{stage.upper()}", DEFAULT_STAGE_MAX_TOKENS.get(stage, 4096)))
OPENROUTER_REASONING = os.environ.get("OPENROUTER_REASONING", "low").strip().lower()
STRUCTURED_METHODS = ("json_schema", "function_calling", "json_prompt")
_working_method: dict[str, str] = {}     # "provider:model" -> method that last worked
_reasoning_level: dict[str, int] = {}     # route -> index into its reasoning-param ladder
_learned_cap: dict[str, int] = {}         # route -> max output tokens the provider actually allows

RETRYABLE_MARKERS = ("429", "502", "503", "504", "rate limit", "rate_limit", "overloaded",
                     "timeout", "timed out", "temporarily", "connection")

T = TypeVar("T", bound=BaseModel)


class LLMConfigError(Exception):
    """Raised when routing is invalid or a required API key is missing."""


class LLMCallError(Exception):
    """Raised when an LLM call fails after all retries / fallbacks."""


@dataclass(frozen=True)
class Route:
    stage: str
    provider: str
    model: str

    @property
    def key_env(self) -> str:
        return PROVIDERS[self.provider]["key_env"]

    @property
    def base_url(self) -> str:
        return PROVIDERS[self.provider]["base_url"]

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_route(spec: str, stage: str = "?") -> Route:
    provider, sep, model = spec.strip().partition(":")
    provider = provider.strip().lower()
    if not sep or not model.strip():
        raise LLMConfigError(f"Invalid LLM route '{spec}' for stage '{stage}'. Use '<provider>:<model id>'.")
    if provider not in PROVIDERS:
        raise LLMConfigError(f"Unknown provider '{provider}' in '{spec}'. Use one of: {', '.join(PROVIDERS)}.")
    return Route(stage, provider, model.strip())


def get_route(stage: str) -> Route:
    if stage not in DEFAULT_ROUTES:
        raise LLMConfigError(f"Unknown stage '{stage}'. Use one of: {', '.join(DEFAULT_ROUTES)}.")
    return parse_route(os.environ.get(f"LLM_{stage.upper()}", DEFAULT_ROUTES[stage]), stage)


def required_keys() -> dict[str, list[str]]:
    """{env var name: [stages that need it]} for the CURRENT routing."""
    need: dict[str, list[str]] = {}
    for stage in DEFAULT_ROUTES:
        need.setdefault(get_route(stage).key_env, []).append(stage)
    return need


def missing_keys() -> dict[str, list[str]]:
    return {k: v for k, v in required_keys().items() if not os.environ.get(k)}


def get_api_key(stage: str, api_key: str | None = None) -> str:
    route = get_route(stage)
    key = api_key or os.environ.get(route.key_env)
    if not key:
        raise LLMConfigError(
            f"No {route.provider} API key found for the {STAGE_LABELS[stage].lower()} stage "
            f"({route}). Set {route.key_env} in the .env file at {ENV_FILE_HINT}."
        )
    return key


def key_for_route(route: Route, api_key: str | None = None) -> str:
    """Key for THIS route's provider (not the stage's default provider)."""
    key = api_key or os.environ.get(route.key_env)
    if not key:
        raise LLMConfigError(f"No {route.provider} API key found for {route}. "
                             f"Set {route.key_env} in the .env file at {ENV_FILE_HINT}.")
    return key


def describe_routing() -> str:
    return "\n".join(f"  {STAGE_LABELS[s]:<17} -> {get_route(s)}" for s in DEFAULT_ROUTES)


# ------------------------------------------------------------
# Client
# ------------------------------------------------------------

def _reasoning_ladder(route: Route) -> list[dict]:
    """
    Reasoning settings to try for this route, most preferred first. If the
    provider rejects one (e.g. "reasoning_effort is not supported with this
    model"), the next is used for the rest of the run. The last rung is
    always {} (send nothing).
    """
    if route.provider == "openrouter":
        if OPENROUTER_REASONING in ("low", "medium", "high"):
            return [{"reasoning": {"effort": OPENROUTER_REASONING, "exclude": True}}, {}]
        if OPENROUTER_REASONING == "off":
            return [{"reasoning": {"enabled": False}}, {}]
    return [{}]


def _reasoning_params(route: Route) -> dict:
    ladder = _reasoning_ladder(route)
    return ladder[min(_reasoning_level.get(str(route), 0), len(ladder) - 1)]


def _next_reasoning_level(route: Route) -> bool:
    """Moves to the next rung; False if already sending nothing."""
    ladder = _reasoning_ladder(route)
    level = _reasoning_level.get(str(route), 0)
    if level >= len(ladder) - 1:
        return False
    _reasoning_level[str(route)] = level + 1
    return True


def get_llm(route: Route, temperature: float = 0.3, api_key: str | None = None,
            max_tokens: int | None = None):
    """Returns a LangChain chat model for this route (OpenAI-compatible API)."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=route.model,
        temperature=temperature,
        api_key=key_for_route(route, api_key),
        base_url=route.base_url,
        timeout=REQUEST_TIMEOUT,
        max_tokens=max_tokens or stage_max_tokens(route.stage),
        extra_body=_reasoning_params(route) or None,
        # IMPORTANT: 0 here. Retries are handled ONLY by invoke_structured().
        # The OpenAI SDK otherwise retries silently (2x by default), which
        # stacked with our own retries meant a slow model could hang for
        # 3 x 3 x timeout (~18 min at 120 s) with no output.
        max_retries=0,
    )


# ------------------------------------------------------------
# JSON-prompt fallback helpers
# ------------------------------------------------------------

def _schema_instruction(schema: Type[BaseModel]) -> str:
    return (
        "\n\nOUTPUT FORMAT: respond with ONLY one JSON object (no prose, no "
        "markdown fences) that validates against this JSON Schema:\n"
        + json.dumps(schema.model_json_schema(), indent=1)
    )


def extract_json_object(text: str) -> str:
    """Pulls the JSON object out of a model reply (handles ```json fences,
    leading prose and <think> blocks)."""
    if not text:
        raise ValueError("model returned empty content")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unterminated JSON object in model reply (output likely truncated)")


def _call_once(llm, prompt: str, schema: Type[T], method: str) -> T:
    if method == "json_prompt":
        reply = llm.invoke(prompt + _schema_instruction(schema))
        content = reply.content if isinstance(reply.content, str) else str(reply.content)
        return schema.model_validate_json(extract_json_object(content))
    result = llm.with_structured_output(schema, method=method).invoke(prompt)
    if result is None:
        raise ValueError("model returned no structured output")
    return result


def _token_numbers(msg: str) -> dict:
    out = {}
    for name in ("limit", "used", "requested"):
        m = re.search(name + r"\W{0,3}(\d+)", msg, flags=re.I)
        if m:
            out[name] = int(m.group(1))
    return out


def _too_large_limit(err: Exception) -> int | None:
    """
    Distinguishes two different 429s (found via a real run on a
    per-minute-token-limited provider):
      "Request too large ... Limit 1000, Requested 2048"   -> can NEVER fit:
            returns the limit so the output budget is lowered
      "Rate limit reached ... Limit 1000, Used 508, Requested 800.
       Please try again in 18.48s"                         -> fits, just busy:
            returns None so the caller WAITS and retries
    """
    msg = str(err)
    low = msg.lower()
    if "token" not in low:
        return None
    nums = _token_numbers(msg)
    if "too large" in low:
        return nums.get("limit", 0)
    if "limit" in nums and "requested" in nums and nums["requested"] > nums["limit"]:
        return nums["limit"]
    return None


def _retry_after_seconds(err: Exception) -> float | None:
    """Parses 'Please try again in 7.5s' / '850ms' / '1m2.5s' from a rate-limit error."""
    m = re.search(r"try again in\s*(?:(\d+)m)?\s*([\d.]+)\s*(ms|s)", str(err), flags=re.I)
    if not m:
        return None
    minutes = int(m.group(1) or 0)
    value = float(m.group(2))
    return minutes * 60 + (value / 1000 if m.group(3).lower() == "ms" else value)


def _is_transient(err: Exception) -> bool:
    if _too_large_limit(err) is not None:
        return False  # a size problem, not a busy provider
    msg = f"{type(err).__name__} {err}".lower()
    return any(m in msg for m in RETRYABLE_MARKERS)


def _is_auth_error(err: Exception) -> bool:
    """401/403 — the key itself was rejected. No retry or format change can help."""
    msg = f"{type(err).__name__} {err}".lower()
    return ("401" in msg or "403" in msg or "authentication" in msg or "unauthorized" in msg
            or "no auth credentials" in msg or "user not found" in msg)


def _rejects_reasoning_params(err: Exception) -> bool:
    msg = str(err).lower()
    return ("reasoning" in msg) and any(w in msg for w in ("not supported", "unsupported", "invalid", "unknown", "400"))


# ------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------

def invoke_structured(
    prompt: str,
    schema: Type[T],
    *,
    stage: str,
    temperature: float = 0.3,
    api_key: str | None = None,
    max_retries: int = 3,
    route: Route | None = None,
) -> T:
    """
    One structured-output call for a pipeline stage ("profile" | "pitch" | "audit").
      - transient errors (429 / 5xx / timeout) -> wait and retry the SAME method
      - provider rejects reasoning params      -> retry once without them
      - format errors (empty reply, no parsed field, invalid JSON)
                                               -> try the NEXT method
    Always returns a validated `schema` instance or raises LLMCallError.
    """
    route = route or get_route(stage)
    label = STAGE_LABELS.get(stage, stage)
    key = key_for_route(route, api_key)
    max_tokens = min(stage_max_tokens(route.stage), _learned_cap.get(str(route), 10**9))
    llm = get_llm(route, temperature, key, max_tokens)

    known = _working_method.get(str(route))
    methods = [known] + [m for m in STRUCTURED_METHODS if m != known] if known else list(STRUCTURED_METHODS)

    errors: list[str] = []
    for method in methods:
        attempt = 0
        while attempt < max_retries:
            started = time.time()
            print(f"  {label}: {route} [{method}] "
                  f"(attempt {attempt + 1}/{max_retries}, timeout {int(REQUEST_TIMEOUT)}s)...", flush=True)
            try:
                result = _call_once(llm, prompt, schema, method)
                _working_method[str(route)] = method
                print(f"  {label}: done in {time.time() - started:.0f}s via {method}", flush=True)
                return result
            except Exception as e:  # noqa: BLE001 — SDK/parsers raise many types
                short = f"{type(e).__name__}: {str(e)[:400]}"
                print(f"  {label}: failed after {time.time() - started:.0f}s — {short}", flush=True)

                if _is_auth_error(e):
                    # Found via a real run: a rejected key ("401 Missing Authentication
                    # header") was retried across all 3 output formats. Fail fast instead.
                    raise LLMCallError(
                        f"{label}: {route.provider} rejected the API key ({short}). "
                        f"Check {route.key_env} in {ENV_FILE_HINT}: it must be your current, "
                        f"active key on one line — no quotes, spaces or text after it. "
                        f"Test it with: python -m src.check_llm --key"
                    ) from e

                if _rejects_reasoning_params(e) and _next_reasoning_level(route):
                    print(f"  {label}: provider rejected reasoning settings; retrying with "
                          f"{_reasoning_params(route) or 'none'}", flush=True)
                    llm = get_llm(route, temperature, key, max_tokens)
                    continue  # same attempt number, same method

                limit = _too_large_limit(e)
                if limit is not None and max_tokens > MIN_OUTPUT_TOKENS:
                    new_budget = int(limit * 0.8) if limit else max_tokens // 2
                    if new_budget >= max_tokens:          # stated limit isn't the binding one
                        new_budget = max_tokens // 2
                    new_budget = max(MIN_OUTPUT_TOKENS, new_budget)
                    if new_budget < max_tokens:
                        print(f"  {label}: request too large for the provider's token limit; "
                              f"lowering max output tokens {max_tokens} -> {new_budget}", flush=True)
                        max_tokens = new_budget
                        _learned_cap[str(route)] = new_budget  # later calls (e.g. each audit claim) start here
                        llm = get_llm(route, temperature, key, max_tokens)
                        continue  # same attempt number, same method

                attempt += 1
                if _is_transient(e) and attempt < max_retries:
                    wait = _retry_after_seconds(e)
                    wait = (wait + 1) if wait is not None else 8 * attempt
                    print(f"  {label}: provider busy/rate-limited, retrying in {wait:.0f}s", flush=True)
                    time.sleep(wait)
                    continue
                errors.append(f"[{method}] {short}")
                if _is_transient(e):
                    # Provider down/overloaded — switching output format won't help.
                    raise LLMCallError(f"{label} failed: {route.provider} unavailable. {short}") from e
                break  # format problem -> next method

    raise LLMCallError(
        f"{label} failed with every output method for {route}: " + " | ".join(errors)
        + ". Try another model: python -m src.check_llm --list " + route.provider
    )
