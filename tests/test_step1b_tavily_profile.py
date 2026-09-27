"""
Step 1b checks — Tavily company research + sourced company profile.
No network or API keys needed: Tavily HTTP and the LLM call are faked.

Run from project root:  python -m tests.test_step1b_tavily_profile
"""
import io
import json
import os
import sys
import types
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for mod in ("faiss", "sentence_transformers"):
    try:
        __import__(mod)
    except ImportError:
        m = types.ModuleType(mod)
        m.SentenceTransformer = object
        sys.modules[mod] = m

import src.research.web_search as ws  # noqa: E402
import src.generation.company_profile as cp  # noqa: E402
from src.generation.pitch_generator import _build_company_facts_block  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  PASS  {name}")


# ---------- fake Tavily HTTP ----------
PAGES = {
    "overview": [
        {"title": "Infosys - Wikipedia", "url": "https://en.wikipedia.org/wiki/Infosys", "score": 0.91,
         "content": "Infosys Limited is an Indian multinational information technology company headquartered in Bengaluru. It provides business consulting and IT services."},
    ],
    "employees": [
        {"title": "Infosys Integrated Annual Report 2025-26", "url": "https://www.infosys.com/investors/ar-26.pdf", "score": 0.95,
         "content": "Corporate overview: 3,28,594 employees. 1,965 active clients. Total revenues in fiscal 2026: 1,78,650 crore."},
        {"title": "Infosys - Wikipedia", "url": "https://en.wikipedia.org/wiki/Infosys", "score": 0.5,
         "content": "duplicate lower-score hit for the same URL that should be dropped in favour of the better one"},
    ],
    "workforce": [
        {"title": "Infosys global presence", "url": "https://www.infosys.com/about/global-presence.html", "score": 0.8,
         "content": "Infosys has offices and development centers in 59 countries across the Americas, Europe and Asia Pacific."},
        {"title": "too short", "url": "https://example.com/short", "score": 0.99, "content": "tiny"},
    ],
    "health": [],
}
sent = []


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_urlopen(req, timeout=None):
    body = json.loads(req.data)
    sent.append({"auth": req.headers.get("Authorization"), "body": body})
    q = body["query"]
    key = ("overview" if "overview" in q else "workforce" if "workforce" in q
           else "health" if "health" in q else "employees")
    return _Resp(json.dumps({"results": PAGES[key]}).encode())


ws.urllib.request.urlopen = fake_urlopen

# 1. Research
res = ws.research_company("Infosys", api_key="tvly-test")
check("4 focused queries sent, with Bearer auth", len(sent) == 4 and sent[0]["auth"] == "Bearer tvly-test")
check("results de-duplicated by URL, short extracts dropped, ranked by score",
      [s.url for s in res.sources] == ["https://www.infosys.com/investors/ar-26.pdf",
                                       "https://en.wikipedia.org/wiki/Infosys",
                                       "https://www.infosys.com/about/global-presence.html"])
check("sources numbered S1..Sn", [s.id for s in res.sources] == ["S1", "S2", "S3"])
check("duplicate URL keeps the higher-scoring extract", "headquartered in Bengaluru" in res.by_id()["S2"].content)

no_key = ws.research_company("Infosys", api_key=None) if not ws.get_tavily_key() else None
if no_key is not None:
    check("no TAVILY_API_KEY -> no sources, clear note, no exception",
          not no_key.ok and "TAVILY_API_KEY not set" in no_key.errors[0])


def failing_urlopen(req, timeout=None):
    raise urllib.error.HTTPError(ws.TAVILY_URL, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"bad key"}'))


ws.urllib.request.urlopen = failing_urlopen
bad = ws.research_company("Infosys", api_key="tvly-bad")
check("invalid key -> stops after first query, error recorded, no exception",
      not bad.ok and len(bad.errors) == 1 and "401" in bad.errors[0] and "invalid" in bad.errors[0])
ws.urllib.request.urlopen = fake_urlopen

# 2. Deterministic number check
check("'3,28,594 employees' matches source text '3,28,594'", cp.number_supported("3,28,594 employees", "…3,28,594 employees…"))
check("'328,594' (different grouping) still matches '3,28,594'", cp.number_supported("328,594", "3,28,594 employees"))
check("'3,50,000 employees' does NOT match", not cp.number_supported("3,50,000 employees", "3,28,594 employees"))
check("text with no numbers is trivially supported", cp.number_supported("Bengaluru, India", "anything"))

# 3. Profile built from sources, with post-checks (LLM faked)
draft = cp._ProfileDraft(
    industry="IT services and consulting",
    size_estimate="3,28,594 employees (FY2026)",
    facts=[
        cp._DraftFact(label="Employees", value="3,28,594 employees (fiscal 2026)", source_id="S1"),
        cp._DraftFact(label="Headquarters", value="Bengaluru, India", source_id="[s2]"),
        cp._DraftFact(label="Global presence", value="Offices in 65 countries", source_id="S3"),   # wrong number
        cp._DraftFact(label="Median age", value="28 years", source_id="S9"),                       # invented source
    ],
    key_risks=["Sedentary desk-based work", "Overseas client assignments", "Large young workforce"],
    assumptions_used=["Key risks are inferred from the nature of IT services work."],
)
captured = {}


def fake_invoke(prompt, schema, **kw):
    captured["prompt"], captured["stage"] = prompt, kw.get("stage")
    return draft


cp.invoke_structured = fake_invoke
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test")
prof = cp.generate_company_profile("Infosys", research=res)
check("profile stage routed as 'profile', prompt contains numbered sources",
      captured["stage"] == "profile" and "[S1] Infosys Integrated Annual Report" in captured["prompt"])
check("data_source = live_web with 3 sources listed", prof.data_source == "live_web" and len(prof.sources) == 3)
labels = {f.label: f for f in prof.facts}
check("fact citing a non-existent source (S9) removed and listed as unsourced",
      "Median age" not in labels and any("S9" in a and "Unsourced" in a for a in prof.assumptions_used))
check("'[s2]' citation normalised to S2 and linked to its URL",
      labels["Headquarters"].source_id == "S2" and labels["Headquarters"].source_url.endswith("/wiki/Infosys"))
check("correct figure verified against its source", labels["Employees"].verified_in_source)
check("wrong figure ('65 countries' vs source '59') kept but marked unverified + noted",
      not labels["Global presence"].verified_in_source
      and any("not found verbatim" in a and "65" in a for a in prof.assumptions_used))

# 4. Fallback without research
empty = ws.ResearchResult(company="Infosys", errors=["TAVILY_API_KEY not set — live company research skipped"])
draft_fb = cp._ProfileDraft(industry="IT services", size_estimate="Assumption: 300,000+ employees",
                            facts=[cp._DraftFact(label="Employees", value="300,000", source_id="S1")],
                            key_risks=["Sedentary work"], assumptions_used=[])
cp.invoke_structured = lambda prompt, schema, **kw: (captured.__setitem__("prompt", prompt), draft_fb)[1]
fb = cp.generate_company_profile("Infosys", research=empty)
check("fallback prompt tells the model there is no live data",
      "No live web data is available" in captured["prompt"] and "facts — leave EMPTY" in captured["prompt"])
check("fallback: data_source=model_knowledge, NO facts even if the model invented one, clear first assumption",
      fb.data_source == "model_knowledge" and fb.facts == [] and fb.assumptions_used[0].startswith("No live web data"))
check("fallback keeps the research note for the UI", "TAVILY_API_KEY not set" in fb.research_notes[0])

# 5. Facts reach the pitch prompt correctly
block = _build_company_facts_block(prof)
check("pitch prompt lists verified facts with sources and flags unverified ones",
      "3,28,594 employees" in block and "infosys.com/investors" in block
      and "[UNVERIFIED — do not quote this number]" in block)
check("pitch prompt says 'none verified' when there are no facts",
      "none verified" in _build_company_facts_block(fb))

# 6. Empty company name still rejected before any research
try:
    cp.generate_company_profile("   ")
    check("empty name rejected", False)
except cp.CompanyProfileError:
    check("empty company name rejected (Objective 1.4)", True)

print(f"\nAll {passed} checks passed.")
