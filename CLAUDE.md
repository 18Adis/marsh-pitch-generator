# CLAUDE.md

Guidance for Claude Code (or any future session) working in this repository.

## Project

Marsh internship case study (Data Science role) — an AI-powered application
that generates client-specific insurance marketing pitches, grounded in 4
provided policy documents, with an independent audit layer that verifies
every claim before it reaches a human advisor.

Two challenges from the brief:
1. **Pitch Generator** — company name in, grounded 3-5 slide `.pptx` pitch out
2. **Content Auditor** — independently verify every claim traces to a real source clause

## Architecture (7 stages)

```
[4 policy PDFs] -> extract text -> chunk -> embed -> vector store
                                                  |
company_name -> generateCompanyProfile() --------+
                                                  v
                                    retrieve relevant chunks (per-risk queries)
                                                  v
                                generateMarketingPitch() -> .pptx
                                                  v
                           auditPitchContent(pitch, policy_docs)
                                |                        |
                       deterministic quote match  BM25 fallback search
                                |                        |
                                +--- LLM semantic check --+
                                                  v
                                  audit report + confidence score
                                                  v
                                     Streamlit chat UI
```

| Stage | File | Purpose |
|---|---|---|
| 0 | `src/ingestion/pdf_extractor.py` | Per-page PDF text extraction (PyMuPDF) |
| 1 | `src/ingestion/chunker.py` | Splits pages into retrievable chunks; `chunk_pages()` is the reusable core, `chunk_document()` wraps it for file-based use |
| 2 | `src/retrieval/vector_store.py` | Embeddings (sentence-transformers) + FAISS index (`PolicyVectorStore`) |
| 3a | `src/research/web_search.py` | `research_company()` — Tavily web search, numbered sources |
| 3 | `src/generation/company_profile.py` | `generate_company_profile()` — sourced company profile + labelled fallback |
| 4 | `src/generation/pitch_generator.py` (top half) | `retrieve_chunks_for_profile()` — per-risk retrieval + filtering |
| 5 | `src/generation/pitch_generator.py` (bottom half) | `generate_marketing_pitch()` + `build_pptx()` |
| 6 | `src/audit/verifier.py`, `claim_extractor.py`, `text_match.py` | `audit_pitch_content()` — 3-layer verification of benefits + talking points; non-policy statements listed |
| 6c | `src/audit/report_writer.py` | Word audit report: 1 Summary (automated + advisor review), 2 Claim-by-claim (+ advisor decision), 3 Statements outside policy docs; max 3 A4 pages |
| 6d | `src/audit/review.py` | Human verification: `apply_review()` applies approve/edit/remove from the UI, re-checks edits, returns the FINAL pitch the deck + report are built from |
| — | `src/generation/deck_renderer.py` | 5-slide designed deck template (python-pptx) |
| — | `src/run_pipeline.py` | `python -m src.run_pipeline <Company>` — whole pipeline from the terminal |
| 7 | `app/streamlit_app.py` | Chat UI + document management |

## Running things

**Always activate the venv first** (global Python env has unrelated
`rasa`/GPU-torch packages that conflict with this project's dependencies):
```powershell
venv\Scripts\activate
```

Run individual stages:
```powershell
python src/ingestion/pdf_extractor.py
python src/ingestion/chunker.py
python src/retrieval/vector_store.py
```

Every LLM stage imports `src.config`, so they MUST be run as modules
from the project root:
```powershell
python -m src.generation.company_profile TCS
```

**Important Windows/import gotcha:** `company_profile.py`, `pitch_generator.py`
and anything under `src/audit/` use absolute `from src....` imports, so it MUST be run as a
module from the project root, not as a direct script:
```powershell
python -m src.generation.pitch_generator TCS      # correct
python src/generation/pitch_generator.py TCS      # WRONG — ModuleNotFoundError
```

Run the full app:
```powershell
streamlit run app/streamlit_app.py
```
Note: `app/streamlit_app.py` explicitly inserts the project root into
`sys.path` at the top of the file (before any `src.*` imports), because
`streamlit run` always executes the file directly — it doesn't have an
equivalent to `python -m`, so without this line the same `src` import
error would occur here too.

## Environment / secrets

`.env` file in project root (gitignored; template in `.env.example`):
```
OPENROUTER_API_KEY=...   # required — every AI stage
TAVILY_API_KEY=...       # recommended — live company research
```

**LLM: OpenRouter only, routed per stage** (all through `src/config.py`'s
`invoke_structured(prompt, schema, stage=...)`):

| Stage | Route (default) | Override in .env |
|---|---|---|
| 3 Company profile | `openrouter:nvidia/nemotron-3-ultra-550b-a55b:free` | `LLM_PROFILE` |
| 5 Pitch writing | `openrouter:nvidia/nemotron-3-ultra-550b-a55b:free` | `LLM_PITCH` |
| 6 Audit semantic check | `openrouter:nvidia/nemotron-3-ultra-550b-a55b:free` | `LLM_AUDIT` |

Point `LLM_AUDIT` at a different model so the auditor isn't grading its own
writing. `missing_keys()` is checked at app start-up (a missing key stops the
app rather than silently skipping a stage). `python -m src.check_llm` tests
every route plus Tavily; `--list` lists free OpenRouter models; `--tavily X`
tests research only.

**Company research: Tavily** (`src/research/web_search.py`, plain HTTPS, no
extra package). `generate_company_profile()` runs 4 focused searches (basic
depth, 1 credit each), numbers the extracts S1..Sn, and the LLM must cite a
source id for every company fact. Deterministic post-checks: facts citing a
non-existent source are removed; facts whose numbers don't appear in the
cited extract are marked `verified_in_source=False`. No key / no results ->
labelled fallback (`data_source="model_knowledge"`, no facts, sizes prefixed
"Assumption:"). Web data is for COMPANY facts only — never policy claims.

Structured output falls back json_schema -> function_calling -> json_prompt
per model (see config.py docstring for the nemotron empty-reply failure
that caused this). Reasoning models are asked for low reasoning.

## Critical design decisions (and the real bugs that caused them)

These aren't theoretical — each was found by actually running the pipeline
against the real 4 documents, not anticipated in advance.

- **Exclusion-clause filtering (Stage 4).** A risk containing "obesity"
  matched a policy's *Exclusions* clause (which also contains the word
  "obesity") — i.e. the opposite of coverage. Chunks starting with markers
  like "exclusions", "wait period" (note: real docs say "Wait Periods",
  not "waiting period" — both variants are now covered) are filtered out
  before they can reach generation.

- **Minimum similarity threshold (Stage 4).** Forcing `top_k` results per
  risk regardless of score let generic marketing taglines through with
  scores as low as ~0.19. `MIN_SIMILARITY_SCORE = 0.25` fixed this.
  `TOP_K_PER_RISK` was later raised 2→3 after finding some risks got zero
  matches — this widened the candidate pool without lowering the quality
  bar.

- **The HealthReturns/Claim Protect conflation (the big one).** Chunk
  `ABHI_Product_Brochure_p1_c3` contains **four distinct marketing tiles**
  ("Claim Protect", "HealthReturns", and two others) merged into one flat
  block of text, because the source PDF page is a visual grid that
  extracts linearly with no separation. A real generated pitch merged two
  different named features into one false combined claim. This is a
  **Stage 1/2 data-quality issue**, not fully fixable by prompting alone —
  it's the concrete reason the audit layer's independent semantic check
  (Layer 3) exists, not as decoration. Stage 5's prompt now has an explicit
  FEATURE-ISOLATION RULE, but this should still be treated as a residual
  risk area, not a solved problem.

- **Policy name resolution (Stage 6).** The LLM naturally refers to
  policies by product name ("Care Supreme"), but chunk data is keyed by
  PDF filename (`Care_Health_Product_Brochure.pdf`). `POLICY_ALIASES` in
  `verifier.py` bridges this — without it, every claim would fail
  verification on a string mismatch alone.

- **Provider history.** Started on Gemini (`gemini-2.0-flash` → 404
  deprecated → `gemini-3.8-flash` real but frequently 503/overloaded →
  `gemini-flash-latest` alias is explicitly *experimental* with *tighter*
  rate limits per Google's own docs, not a safe "latest stable" choice).
  Switched to Groq entirely. Groq's `llama-3.3-70b-versatile` was then
  found fully decommissioned (Aug 16, 2026). Settled on `qwen/qwen3.8-27b`.
  **Lesson: don't assume a model name will still exist; check for 404s
  and prefer providers' own documented "recommended for new projects"
  guidance over guessing.**
  Later the stages drifted onto three providers at once (Gemini for
  Stage 3, OpenRouter for Stage 5, Groq for Stage 6). The app only
  checked the OpenRouter key, so with no Groq key the audit's semantic
  layer was silently skipped and every claim came out FLAGGED (0%
  confidence in the L&T Mindtree report). Consolidated via `src/config.py`.
  Groq was briefly re-added for profile + audit, but its free tier for
  `qwen/qwen3.8-27b` allowed only 1,000 output tokens/minute and qwen's
  structured output broke (json_schema returned just "{", function calls
  were malformed). Removed; everything now runs on OpenRouter, where
  nemotron returned valid structured output in ~1-2 s.

- **Audit reported 0% confidence (Stage 6).** Four causes, all fixed:
  (1) the semantic prompt included the client risk, so correct benefits
  were judged "adds details not in the source"; the auditor now judges only
  what the claim says the POLICY provides; (2) quote matching was a raw
  substring test that failed on ™/TM, ₹/INR, curly quotes and PDF layout
  reordering — `text_match.py` normalises and accepts layout-reordered
  (STITCHED) quotes; (3) confidence was VERIFIED/total — now a weighted mean
  (1.0 / 0.8 / 0.4 / 0); (4) a failed/rate-limited check was recorded as
  "partially supported" — now UNCHECKED with status AUDIT INCOMPLETE. The
  semantic check is one batched call for all claims (fewer free-tier requests).

- **Audit checked the wrong chunk (Stage 6).** Layer 3 used the FIRST
  chunk on the matched page, not the chunk the quote was actually found
  in, so a true claim whose quote sat in a page's second chunk could be
  judged against unrelated text and rejected. `_locate_quote()` now
  returns `matched_chunk_id` and Layer 3 checks that chunk (plus the
  rest of the same page as context).

## Known limitations (real, not hidden)

- The 4 source documents are **retail/individual** health insurance
  products, not corporate group policies. Some occupational risks (e.g.
  shift-work/circadian disruption) may have no strong supporting clause
  at all — the system correctly declines to fabricate one rather than
  forcing a weak connection.
- Niva Bupa's page 1 is image-only (no extractable text layer) — flagged,
  not silently dropped, but that page's content is unavailable to the
  pipeline.
- Chunk overlap is character-count-based and can occasionally start a
  chunk mid-word (cosmetic only — full context still exists in the
  adjacent chunk).
- The audit scores `pitch.benefits` (quoted) and the recommendation
  `key_talking_points` (evidence retrieved from the recommended policy's
  brochure). `why_marsh`, the comparative justification, company facts and
  assumptions are listed in the report for sign-off but not scored — no
  policy clause can prove them.

## The closed-world constraint (important, don't violate)

The **official, graded submission** must only ever use the original 4
policy documents (`data/policy_docs/`, and the persisted
`data/processed/chunks/all_chunks.json` / FAISS index built from them).
This is what makes every claim traceable and auditable — the entire point
of the case study.

The Streamlit UI's "Add Document" feature is a deliberate exception for
demonstrating extensibility: uploads are processed into a **session-only**
copy of the chunk list and vector store. **Never** make an upload feature
(or any future change) overwrite the files above — that would corrupt the
reproducible baseline the grader expects.

## Testing philosophy

Every stage was tested against the real 4 documents before being handed
off, not just written and assumed correct. Deterministic logic (chunking,
quote-matching, exclusion filtering, BM25 fallback) is fully testable
without any API key. LLM-dependent calls (company profile, pitch
generation, audit semantic check) need a real `OPENROUTER_API_KEY` (and `TAVILY_API_KEY` for research) to verify
end-to-end — flag this distinction explicitly whenever making a change,
rather than assuming a passing syntax check means the feature works.

## Remaining work

- Write-up (Word/PDF): approach, tools used, design decisions, limitations
  — not yet drafted as of this file's creation.
