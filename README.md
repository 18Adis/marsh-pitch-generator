AI-Powered Insurance Pitch Generator & Auditor

Internship case study submission for Marsh (India Knowledge Services, Data Science Team).

## Problem
A Client Advisor needs to quickly generate a client-specific marketing pitch for
Marsh's insurance policies — grounded in real policy documents, not AI guesswork —
and needs confidence that every claim in the pitch is traceable back to a source
clause before it reaches a client.

## Solution Overview
Two connected systems:

1. **Pitch Generator (Challenge 1)** — a RAG (Retrieval-Augmented Generation)
   pipeline that researches a client company, retrieves relevant clauses from the
   4 policy documents, and generates a grounded 3–5 slide pitch deck.
2. **Content Auditor (Challenge 2)** — an independent verification pass that
   extracts every factual claim from the generated pitch and re-checks it against
   the source documents, flagging anything unsupported.

## Pipeline

```
[4 policy PDFs] -> extract text -> chunk -> embed -> vector store
                                                  |
company_name -> generateCompanyProfile()  -------+
                                                  v
                                    retrieve relevant chunks
                                                  v
                                generateMarketingPitch() -> .pptx
                                                  v
                           auditPitchContent(pitch, policy_docs)
                                |                        |
                       re-retrieve per claim     verify claim vs source
                                                  v
                                  audit report + confidence score
                                                  v
                            Advisor: approve / edit / reject
```

## Repo Structure

```
marsh-pitch-generator/
├── app/                      # Streamlit UI entry point
├── data/
│   ├── policy_docs/          # the 4 source policy PDFs
│   └── processed/            # extracted/chunked text, vector store files
├── src/
│   ├── ingestion/            # PDF extraction + chunking
│   ├── retrieval/            # embeddings + vector search
│   ├── generation/           # generateCompanyProfile, generateMarketingPitch
│   └── audit/                # auditPitchContent, claim extraction, verification
├── outputs/
│   ├── sample_decks/         # generated sample .pptx pitch(es)
│   └── audit_reports/        # generated audit report(s)
├── docs/                     # write-up (Word/PDF) for submission
├── requirements.txt
└── README.md
```

## Deliverables Checklist (per case study brief)
- [ ] Working application (UI + backend)
- [ ] Sample pitch deck (.pptx, 3–5 slides, for one chosen company)
- [ ] Audit results (claim-level traceability report)
- [ ] Write-up (approach, tools/libraries, design decisions)

## Status
🚧 Architecture planned, implementation not yet started.
