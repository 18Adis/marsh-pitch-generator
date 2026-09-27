"""
Stage 0 — Document Ingestion
Extracts raw text from the 4 policy PDFs, page by page, using PyMuPDF.

Design decisions:
- Extraction is done PER PAGE (not one big blob) so we can later trace
  a retrieved chunk back to its source page number — this feeds directly
  into the audit layer's "exact_quote_from_source" requirement.
- Light cleanup only (collapsing excessive blank lines). We deliberately
  do NOT aggressively reformat text, because policy brochures contain
  tables (e.g. HDFC ERGO's benefit table, Care Supreme's eligibility
  table) and over-cleaning can silently scramble numeric values —
  which would be dangerous in an insurance-grounding pipeline.
- Output: one JSON file per policy document, structured as a list of
  {page_number, text} objects. This becomes the input to Stage 1 (chunker.py).
"""

import json
import re
from pathlib import Path

import pymupdf  # PyMuPDF


def clean_text(raw_text: str) -> str:
    """
    Light, safe cleanup:
    - collapse 3+ blank lines into 1
    - strip trailing whitespace per line
    Does NOT touch numbers, currency symbols, or table-like spacing,
    since those carry meaning in these policy documents.
    """
    lines = [line.rstrip() for line in raw_text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf(pdf_path: str) -> list[dict]:
    """
    Extracts text from a single PDF, page by page.

    Returns:
        list of dicts: [{"page_number": 1, "text": "..."}, ...]
    """
    doc = pymupdf.open(pdf_path)
    pages = []

    for page_index in range(len(doc)):
        page = doc[page_index]
        raw_text = page.get_text()
        cleaned = clean_text(raw_text)

        # Skip pages that extracted to (near) nothing — e.g. a pure
        # image/banner page with no real text (common in these brochures).
        # We log it instead of silently dropping it, since a reviewer
        # (or you, debugging later) should know a page was image-only.
        if len(cleaned) < 20:
            pages.append({
                "page_number": page_index + 1,
                "text": "",
                "note": "low/no extractable text (likely an image-heavy page)"
            })
            continue

        pages.append({
            "page_number": page_index + 1,
            "text": cleaned
        })

    doc.close()
    return pages


def extract_all_policy_docs(input_dir: str, output_dir: str) -> None:
    """
    Runs extract_pdf() on every PDF in input_dir and writes one
    JSON file per document into output_dir.
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(input_path.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in {input_dir}")
        return

    for pdf_file in pdf_files:
        print(f"Extracting: {pdf_file.name}")
        pages = extract_pdf(str(pdf_file))

        out_file = output_path / f"{pdf_file.stem}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "source_document": pdf_file.name,
                    "total_pages": len(pages),
                    "pages": pages
                },
                f,
                indent=2,
                ensure_ascii=False
            )

        non_empty = sum(1 for p in pages if p["text"])
        print(f"  -> {non_empty}/{len(pages)} pages had extractable text. Saved to {out_file}")


if __name__ == "__main__":
    # Assumes this script is run from the project root, or adjust paths.
    extract_all_policy_docs(
        input_dir="data/policy_docs",
        output_dir="data/processed/extracted_text"
    )
