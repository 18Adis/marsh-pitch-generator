"""
Step 0 checks — no API key or network needed.

Covers: single-provider config, missing-key errors, retry behaviour,
and the Layer 3 "wrong chunk" fix, against the REAL 4-document chunks.
Run from project root:  python -m tests.test_step0_config_and_audit
"""
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Heavy embedding deps aren't needed for these checks — stub them only if absent.
for mod in ("faiss", "sentence_transformers"):
    try:
        __import__(mod)
    except ImportError:
        m = types.ModuleType(mod)
        m.SentenceTransformer = object
        sys.modules[mod] = m

import src.config as config  # noqa: E402
from src.audit import verifier  # noqa: E402
from src.generation.company_profile import generate_company_profile, CompanyProfileError  # noqa: E402
from src.generation.pitch_generator import (  # noqa: E402
    generate_marketing_pitch, PitchGenerationError, PitchContent, BenefitMapping, RecommendedPolicy,
)

CHUNKS = json.loads((ROOT / "data/processed/chunks/all_chunks.json").read_text(encoding="utf-8"))
passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  PASS  {name}")


# 1. Missing key -> clear, catchable errors naming OPENROUTER_API_KEY
saved = {k: os.environ.pop(k, None) for k in ("OPENROUTER_API_KEY",)}
try:
    try:
        generate_company_profile("Infosys")
        check("profile raises without key", False)
    except CompanyProfileError as e:
        check("profile error names OPENROUTER_API_KEY", "OPENROUTER_API_KEY" in str(e))
    try:
        generate_marketing_pitch(None, [{"x": 1}], "Infosys")
        check("pitch raises without key", False)
    except PitchGenerationError as e:
        check("pitch error names OPENROUTER_API_KEY", "OPENROUTER_API_KEY" in str(e))
    check("missing_keys() -> only OPENROUTER_API_KEY, needed by all three stages",
          config.missing_keys() == {"OPENROUTER_API_KEY": ["profile", "pitch", "audit"]})
finally:
    for k, v in saved.items():
        if v:
            os.environ[k] = v

# 2. Routing
check("Groq provider removed", list(config.PROVIDERS) == ["openrouter"])
r = {st: config.get_route(st) for st in ("profile", "pitch", "audit")}
check("all stages default to OpenRouter nemotron",
      all(str(x) == "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free" for x in r.values()))
check("':free' model ids survive parsing (split on first colon only)", r["pitch"].model.endswith(":free"))
os.environ["LLM_AUDIT"] = "openrouter:some/other-model:free"
check("LLM_AUDIT in .env points the audit at a different model", str(config.get_route("audit")) == "openrouter:some/other-model:free")
del os.environ["LLM_AUDIT"]
for bad in ("groq:qwen/qwen3.8-27b", "gemini:x"):
    try:
        config.parse_route(bad, "pitch"); check(f"{bad} rejected", False)
    except config.LLMConfigError:
        check(f"'{bad.split(':')[0]}' provider rejected with a clear error", True)
o = config.get_llm(r["pitch"], api_key="k")
check("OpenRouter client: base URL, reasoning effort low, SDK retries off, pitch budget 8000",
      "openrouter.ai" in str(o.openai_api_base) and o.extra_body == {"reasoning": {"effort": "low", "exclude": True}}
      and o.max_retries == 0 and o.max_tokens == 8000)

# 3. Retry + structured-output fallback (no network: fake LLM)
class _Dummy(config.BaseModel):
    company: str
    industry: str

class FakeLLM:
    """script: dict method -> list of outcomes ('ok' | exception message | raw text for json_prompt)."""
    def __init__(self, script):
        self.script, self.calls = script, []
    def _next(self, method):
        self.calls.append(method)
        return self.script[method].pop(0)
    def with_structured_output(self, schema, method=None):
        outer = self
        class S:
            def invoke(self, prompt):
                out = outer._next(method)
                if out == "ok":
                    return _Dummy(company="Infosys", industry="IT services")
                raise RuntimeError(out)
        return S()
    def invoke(self, prompt):
        out = self._next("json_prompt")
        class R: content = out
        return R()

EMPTY_PARSED = ("Structured Output response does not have a 'parsed' field nor a "
                "'refusal' field. content='' finish_reason='error'")
orig_get_llm, orig_sleep = config.get_llm, config.time.sleep
config.time.sleep = lambda s: None

def run(script, model):
    fake = FakeLLM(script)
    config.get_llm = lambda *a, **kw: fake
    try:
        route = config.Route("pitch", "openrouter", model)
        return config.invoke_structured("p", _Dummy, stage="pitch", route=route, api_key="k"), fake.calls
    except config.LLMCallError as e:
        return e, fake.calls

out, calls = run({"json_schema": ["Error code: 429 rate limit", "ok"]}, "m-retry")
check("429 is retried on the same method, then succeeds", isinstance(out, _Dummy) and calls == ["json_schema", "json_schema"])

out, calls = run({"json_schema": [EMPTY_PARSED], "function_calling": ["ok"]}, "m-nemotron")
check("the real nemotron 'no parsed field' error falls back to function_calling", isinstance(out, _Dummy) and calls == ["json_schema", "function_calling"])

out, calls = run({"function_calling": ["ok"]}, "m-nemotron")
check("working method is remembered per model (no wasted json_schema call)", calls == ["function_calling"])

out, calls = run({"json_schema": [EMPTY_PARSED], "function_calling": ["tool_calls missing"],
                  "json_prompt": ['Sure!\n```json\n{"company": "Infosys", "industry": "IT {services}"}\n```']}, "m-plain")
check("falls back to json_prompt and parses fenced JSON", isinstance(out, _Dummy) and out.industry == "IT {services}")

out, calls = run({"json_schema": ["no parsed field"], "function_calling": ["tool_calls missing"], "json_prompt": [""]}, "m-bad")
check("all methods failing -> one clear LLMCallError", isinstance(out, config.LLMCallError) and "every output method" in str(out))

REAL_401 = "Error code: 401 - {'error': {'message': 'Missing Authentication header', 'code': 401}}"
out, calls = run({"json_schema": [REAL_401], "function_calling": ["ok"]}, "m-401")
check("real OpenRouter 401 -> fails fast after ONE call, message says the key was rejected",
      isinstance(out, config.LLMCallError) and calls == ["json_schema"]
      and "rejected the API key" in str(out) and "OPENROUTER_API_KEY" in str(out))

out, calls = run({"json_schema": ["Error code: 503"] * 3}, "m-down")
check("provider down (503 x3) -> stops, does not cycle formats", isinstance(out, config.LLMCallError) and calls == ["json_schema"] * 3)

check("extract_json_object strips <think> blocks",
      config.extract_json_object('<think>{"x":1}</think> {"company":"A","industry":"B"}') == '{"company":"A","industry":"B"}')

out, calls = run({"json_schema": ["Error code: 400 - reasoning_format is not supported with this model", "ok"]}, "m-groq-nonreasoning")
check("provider rejecting reasoning params -> next rung of the ladder", isinstance(out, _Dummy)
      and config._reasoning_level.get("openrouter:m-groq-nonreasoning") == 1)

config.get_llm, config.time.sleep = orig_get_llm, orig_sleep
config.time.sleep = lambda s: None

# Groq's real OTPM rejection (message shape from the actual run) -> shrink budget, don't retry blindly
GROQ_TOO_LARGE = ("Error code: 429 - {'error': {'message': \"Request too large for model `qwen/qwen3.8-27b` "
                  "in organization `org_x` service tier `on_demand` on output tokens per minute (OTPM): "
                  "Limit 6000, Requested 8000, please reduce your message size and try again.\", "
                  "'type': 'tokens', 'code': 'rate_limit_exceeded'}}")
budgets = []
def run_budget(script, model, stage="profile"):
    fake = FakeLLM(script)
    def fake_get_llm(route, temperature=0.3, api_key=None, max_tokens=None):
        budgets.append(max_tokens); return fake
    config.get_llm = fake_get_llm
    route = config.Route(stage, "openrouter", model)
    try:
        return config.invoke_structured("p", _Dummy, stage=stage, route=route, api_key="k"), fake.calls
    except config.LLMCallError as e:
        return e, fake.calls

check("'Request too large (OTPM)' is NOT treated as transient", not config._is_transient(RuntimeError(GROQ_TOO_LARGE)))
check("limit parsed from the provider message", config._too_large_limit(RuntimeError(GROQ_TOO_LARGE)) == 6000)
os.environ["LLM_MAX_TOKENS_PROFILE"] = "8000"   # reproduce the original failing budget
budgets.clear()
out, calls = run_budget({"json_schema": [GROQ_TOO_LARGE, "ok"]}, "m-otpm")
check("too-large -> budget lowered below the limit and retried once", isinstance(out, _Dummy) and budgets == [8000, 4800] and calls == ["json_schema"] * 2)
del os.environ["LLM_MAX_TOKENS_PROFILE"]
check("default stage budgets: profile 3000 / pitch 8000 / audit 4000 (all claims checked in one call)",
      [config.stage_max_tokens(x) for x in ("profile", "pitch", "audit")] == [3000, 8000, 4000])
# Real messages from the Groq run
REAL_TOO_LARGE = ("Error code: 429 - Request too large for model `qwen/qwen3.8-27b` ... output tokens per minute "
                  "(OTPM): Limit 1000, Requested 2048. The request's expected output tokens exceed the enforced limit")
REAL_RATE_LIMIT = ("Error code: 429 - Rate limit reached for model `qwen/qwen3.8-27b` ... output tokens per minute "
                   "(OTPM): Limit 1000, Used 508, Requested 800. Please try again in 18.48s. Need more tokens?")
check("real 'Request too large (Limit 1000, Requested 2048)' -> shrink, not wait",
      config._too_large_limit(RuntimeError(REAL_TOO_LARGE)) == 1000 and not config._is_transient(RuntimeError(REAL_TOO_LARGE)))
check("real 'Rate limit reached (Used 508, Requested 800)' -> WAIT, not shrink (the bug in the last run)",
      config._too_large_limit(RuntimeError(REAL_RATE_LIMIT)) is None and config._is_transient(RuntimeError(REAL_RATE_LIMIT))
      and config._retry_after_seconds(RuntimeError(REAL_RATE_LIMIT)) == 18.48)
budgets.clear()
out, calls = run_budget({"json_schema": [REAL_TOO_LARGE, "ok"]}, "m-learn")
out2, calls2 = run_budget({"json_schema": ["ok"]}, "m-learn")
check("learned cap (800) is reused by the next call on that route (no repeat rejection)",
      budgets == [3000, 800, 800] and calls2 == ["json_schema"])
check("retry-after parsed ('try again in 7.5s' / '850ms' / '1m2s')",
      config._retry_after_seconds(RuntimeError("Please try again in 7.5s")) == 7.5
      and config._retry_after_seconds(RuntimeError("try again in 850ms")) == 0.85
      and config._retry_after_seconds(RuntimeError("try again in 1m2s")) == 62)
waits = []
config.time.sleep = lambda s: waits.append(s)
out, calls = run_budget({"json_schema": ["Error code: 429 rate_limit_exceeded. Please try again in 7.5s.", "ok"]}, "m-rpm")
check("rate limit waits the time the provider asked for (+1s)", isinstance(out, _Dummy) and waits == [8.5])

config.get_llm, config.time.sleep = orig_get_llm, orig_sleep

# 4. Layer 3 wrong-chunk fix, on real data:
#    find a page with 2+ chunks and take a quote that only exists in a LATER chunk.
by_page = {}
for c in CHUNKS:
    by_page.setdefault((c["source_document"], c["page_number"]), []).append(c)
target = None
for (doc, page), cs in by_page.items():
    if len(cs) < 2:
        continue
    for later in cs[1:]:
        words = later["text"].split()
        for i in range(0, max(1, len(words) - 12)):
            q = " ".join(words[i:i + 10])
            if len(q) > 40 and not verifier.exact_substring_match(q, cs[0]["text"]):
                target = (doc, page, later, cs[0], q)
                break
        if target:
            break
    if target:
        break
doc, page, later, first, quote = target
policy_name = {"ABHI_Product_Brochure.pdf": "Activ One", "Care_Health_Product_Brochure.pdf": "Care Supreme",
               "HDFC_Product_Brochure.pdf": "HDFC ERGO Optima Secure+", "Niva_Bupa_Product_Brochure.pdf": "Niva Bupa ReAssure 2.0"}[doc]
print(f"  (test quote from {doc} p{page}, chunk {later['chunk_id']}, not in {first['chunk_id']})")

seen = {}
def fake_batch(items, api_key):
    seen["text"] = items[0]["evidence"]
    return {it["id"]: verifier._Verdict(claim_id=it["id"], verdict="SUPPORTED", reason="stub") for it in items}, ""
verifier._semantic_batch = fake_batch

claim = BenefitMapping(risk="r", matched_benefit="b", source_policy=policy_name,
                       page_number=page, exact_quote_from_source=quote)
pitch = PitchContent(company_name="X", industry="i", size_estimate="s", key_risks_summary=[],
                     why_marsh="w", benefits=[claim],
                     recommended_policy=RecommendedPolicy(policy_name=policy_name, justification="j", key_talking_points=[]))
report = verifier.audit_pitch_content(pitch, CHUNKS, api_key="sk-test")
r = report.claim_results[0]
check("quote located on the claimed page", r.quote_status == "EXACT_CLAIMED_PAGE")
check("Layer 3 received text that CONTAINS the quote (whole cited page)", verifier.exact_substring_match(quote, seen["text"]))
check("claim VERIFIED -> 100% confidence, PASS", r.final_status == "VERIFIED" and report.confidence_score == 100.0 and report.overall_status == "PASS")

# 5. A fabricated quote is still REJECTED (regression guard)
bad = claim.model_copy(update={"exact_quote_from_source": "unlimited zero copay mental health visits for all employees forever"})
pitch.benefits = [bad]
report = verifier.audit_pitch_content(pitch, CHUNKS, api_key="sk-test")
check("fabricated quote -> REJECTED", report.claim_results[0].final_status == "REJECTED")

# 6. Audit with the key missing -> UNCHECKED + AUDIT INCOMPLETE, reason names the key (never silent,
#    never disguised as a bad pitch)
saved_g = os.environ.pop("OPENROUTER_API_KEY", None)
pitch.benefits = [claim]
report = verifier.audit_pitch_content(pitch, CHUNKS)
r0 = report.claim_results[0]
check("no audit key -> UNCHECKED, AUDIT INCOMPLETE, reason names OPENROUTER_API_KEY",
      r0.final_status == "UNCHECKED" and report.overall_status == "AUDIT INCOMPLETE"
      and "OPENROUTER_API_KEY" in r0.semantic_reason)
if saved_g: os.environ["OPENROUTER_API_KEY"] = saved_g

print(f"\nAll {passed} checks passed.")
