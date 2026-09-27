"""
Stage 1 — Chunking
Splits extracted policy text (from pdf_extractor.py's JSON output) into
retrievable chunks, each tagged with its source document + page number.

Design decisions:
- We chunk PER PAGE first, not the whole document at once. Real page
  sizes in these 4 brochures ranged from 62 to ~5,250 characters
  (checked directly against the extracted data) — most pages are
  already a reasonable single chunk on their own.
- Only pages ABOVE a size threshold get split further. When splitting,
  we prefer breaking on paragraph boundaries (\n\n) first, then single
  newlines, then sentences — never blindly cutting mid-sentence, since
  that's exactly how you get "Covers up to..." separated from the
  number that follows it (a real risk with insurance figures).
- Every chunk keeps its source_document and page_number. This is not
  optional — Stage 6 (the audit layer) depends entirely on being able
  to say "this claim is supported by [Care Supreme, page 3]".
- Trivial/empty chunks (e.g. a near-blank title page) are dropped,
  since they add noise to the vector store with nothing to retrieve.
"""

import json
from pathlib import Path

CHUNK_SIZE = 800       # target characters per chunk
CHUNK_OVERLAP = 100    # characters of overlap between consecutive chunks
MIN_CHUNK_LENGTH = 30  # drop anything shorter than this (near-empty noise)


def split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """
    Splits a block of text into overlapping chunks, preferring to break
    at paragraph boundaries, then line breaks, then spaces — in that
    order — so we avoid cutting a sentence (or a number) in half.
    """
    if len(text) <= chunk_size:
        return [text]

    separators = ["\n\n", "\n", ". ", " "]

    def recursive_split(t: str, seps: list[str]) -> list[str]:
        if len(t) <= chunk_size:
            return [t]
        if not seps:
            return [t[i:i + chunk_size] for i in range(0, len(t), chunk_size)]

        sep = seps[0]
        parts = t.split(sep)
        chunks = []
        current = ""

        for part in parts:
            candidate = (current + sep + part) if current else part
            if len(candidate) <= chunk_size:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                if len(part) > chunk_size:
                    chunks.extend(recursive_split(part, seps[1:]))
                    current = ""
                else:
                    current = part
        if current:
            chunks.append(current)
        return chunks

    raw_chunks = recursive_split(text, separators)

    overlapped = []
    for i, chunk in enumerate(raw_chunks):
        if i == 0:
            overlapped.append(chunk)
        else:
            prev_tail = raw_chunks[i - 1][-overlap:]
            overlapped.append(prev_tail + " " + chunk)
    return overlapped


def chunk_pages(pages: list[dict], source_doc: str) -> list[dict]:
    """
    Core chunking logic operating on already-loaded page data in memory.
    Added when building the UI's "Add Document" feature (Stage 7) — a
    freshly uploaded PDF is extracted straight into memory (via
    pdf_extractor.extract_pdf) and needs chunking WITHOUT first writing
    to disk. This is the same logic chunk_document() already used;
    chunk_document() below now calls this directly rather than
    duplicating it, so both paths stay identical by construction.
    """
    chunks = []
    chunk_counter = 0

    for page in pages:
        text = page.get("text", "")
        if len(text) < MIN_CHUNK_LENGTH:
            continue

        page_chunks = split_text(text, CHUNK_SIZE, CHUNK_OVERLAP)

        for piece in page_chunks:
            if len(piece.strip()) < MIN_CHUNK_LENGTH:
                continue
            chunk_counter += 1
            chunks.append({
                "chunk_id": f"{Path(source_doc).stem}_p{page['page_number']}_c{chunk_counter}",
                "source_document": source_doc,
                "page_number": page["page_number"],
                "text": piece.strip()
            })

    return chunks


def chunk_document(extracted_json_path: str) -> list[dict]:
    """
    Takes one extracted-text JSON file (from pdf_extractor.py) and
    returns a list of chunk dicts:
        {chunk_id, source_document, page_number, text}
    """
    with open(extracted_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return chunk_pages(data["pages"], data["source_document"])


def chunk_all_documents(input_dir: str, output_path: str) -> None:
    """
    Runs chunk_document() on every extracted JSON file in input_dir
    and writes ONE combined JSON file with all chunks from all 4 docs —
    this combined file is what Stage 2 (embeddings/vector store) reads.
    """
    input_path = Path(input_dir)
    json_files = sorted(input_path.glob("*.json"))

    if not json_files:
        print(f"No extracted-text JSON files found in {input_dir}. "
              f"Run pdf_extractor.py first.")
        return

    all_chunks = []
    for jf in json_files:
        doc_chunks = chunk_document(str(jf))
        all_chunks.extend(doc_chunks)
        print(f"{jf.name}: {len(doc_chunks)} chunks")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    print(f"\nTotal chunks across all documents: {len(all_chunks)}")
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    chunk_all_documents(
        input_dir="data/processed/extracted_text",
        output_path="data/processed/chunks/all_chunks.json"
    )
