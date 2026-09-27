"""
Stage 2 — Embeddings + Vector Store
Embeds the chunks produced by chunker.py and stores them in a FAISS
index for semantic search.

Design decisions:
- Embedding model: sentence-transformers/all-MiniLM-L6-v2. Chosen
  specifically because it runs LOCALLY (no API key, no per-call cost)
  and is fast — appropriate for a 4-document project like this. If this
  pipeline had to scale to hundreds of policy documents, swapping in a
  hosted embedding API (e.g. OpenAI's) would be a one-line change here,
  since the rest of the pipeline only depends on the embed()/search()
  interface, not the specific model.
- Vector store: FAISS (local, in-memory, no server to run). Overkill
  for 4 documents in terms of scale, but the exact same code works
  unchanged if Marsh had 400 policies instead of 4 — that scalability
  argument is worth stating explicitly in the write-up.
- We persist BOTH the FAISS index and the original chunk metadata
  (text, source_document, page_number) to disk, keyed by row position,
  so a retrieved vector can always be mapped back to its readable
  source text — this is what the generator and the audit layer both
  depend on.
"""

import json
import pickle
from pathlib import Path

import faiss
from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"


class PolicyVectorStore:
    def __init__(self, model_name: str = EMBEDDING_MODEL_NAME):
        self.model = SentenceTransformer(model_name)
        self.index = None
        self.chunks = []  # parallel list: self.chunks[i] <-> self.index row i

    def build(self, chunks: list[dict]) -> None:
        """
        Embeds all chunks and builds a FAISS index.
        chunks: list of {chunk_id, source_document, page_number, text}
        """
        self.chunks = chunks
        texts = [c["text"] for c in chunks]

        embeddings = self.model.encode(
            texts,
            convert_to_numpy=True,
            show_progress_bar=True,
            normalize_embeddings=True  # so inner product = cosine similarity
        )

        dimension = embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dimension)
        self.index.add(embeddings)

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """
        Returns the top_k most semantically similar chunks to the query,
        each with its similarity score attached.
        """
        query_embedding = self.model.encode(
            [query],
            convert_to_numpy=True,
            normalize_embeddings=True
        )
        scores, indices = self.index.search(query_embedding, top_k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx == -1:
                continue
            chunk = dict(self.chunks[idx])
            chunk["similarity_score"] = float(score)
            results.append(chunk)
        return results

    def save(self, index_path: str, chunks_path: str) -> None:
        faiss.write_index(self.index, index_path)
        with open(chunks_path, "wb") as f:
            pickle.dump(self.chunks, f)

    def load(self, index_path: str, chunks_path: str) -> None:
        self.index = faiss.read_index(index_path)
        with open(chunks_path, "rb") as f:
            self.chunks = pickle.load(f)


def build_and_save_vector_store(
    chunks_json_path: str,
    index_output_path: str,
    chunks_output_path: str
) -> PolicyVectorStore:
    with open(chunks_json_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    store = PolicyVectorStore()
    print(f"Embedding {len(chunks)} chunks using {EMBEDDING_MODEL_NAME}...")
    store.build(chunks)

    Path(index_output_path).parent.mkdir(parents=True, exist_ok=True)
    store.save(index_output_path, chunks_output_path)
    print(f"Vector store saved to {index_output_path} (+ {chunks_output_path})")
    return store


if __name__ == "__main__":
    store = build_and_save_vector_store(
        chunks_json_path="data/processed/chunks/all_chunks.json",
        index_output_path="data/processed/vector_store/policy_index.faiss",
        chunks_output_path="data/processed/vector_store/policy_chunks.pkl"
    )

    print("\n--- Sample retrieval test ---")
    test_query = "coverage for a large IT company with young employees, stress-related health risks"
    results = store.search(test_query, top_k=3)
    for r in results:
        print(f"\n[{r['similarity_score']:.3f}] {r['source_document']} (page {r['page_number']})")
        print(r["text"][:200])
