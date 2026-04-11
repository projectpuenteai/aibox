"""Small interactive CLI for manually testing the Chroma retrieval index."""

import argparse
import sys
from pathlib import Path

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import (
    COLLECTION_NAME,
    DEFAULT_DEVICE,
    EMBED_DIM,
    EMBED_MODEL_NAME,
    PERSIST_DIR,
)


def main(persist_dir: str, collection: str, top_k: int, device: str):
    """Embed typed queries and print the top matching stored chunks."""
    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(collection)

    model = SentenceTransformer(EMBED_MODEL_NAME, device=device)
    test_vec = model.encode(["dim_check"], normalize_embeddings=True)
    if len(test_vec[0]) != EMBED_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch for retrieval model: expected {EMBED_DIM}, got {len(test_vec[0])}"
        )

    while True:
        q = input("\nQuery> ").strip()
        if not q:
            continue
        if q.lower() in ("exit", "quit"):
            break

        q_emb = model.encode([q], normalize_embeddings=True).tolist()

        res = col.query(
            query_embeddings=q_emb,
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        for i in range(len(res["ids"][0])):
            meta = res["metadatas"][0][i]
            dist = res["distances"][0][i]
            doc = res["documents"][0][i] or ""
            print("\n" + "=" * 90)
            print(
                f"{i + 1}. dist={dist:.4f} | title={meta.get('title')} | "
                f"page_id={meta.get('page_id')} | chunk={meta.get('chunk_index')}"
            )
            print(doc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive Chroma query tool.")
    parser.add_argument("--persist-dir", default=PERSIST_DIR)
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    args = parser.parse_args()
    main(args.persist_dir, args.collection, args.top_k, args.device)

