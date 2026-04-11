"""Measure title-only recall for the local embedding model and Chroma index."""

import random
import sys
import time
from pathlib import Path
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import (
    COLLECTION_NAME as COLLECTION,
    DEFAULT_DEVICE,
    EMBED_DIM,
    EMBED_MODEL_NAME,
    PERSIST_DIR,
)

MODEL_NAME = EMBED_MODEL_NAME
DEVICE = DEFAULT_DEVICE  # set to "cpu" if needed

N = 2000       # number of titles to test
K = 10         # top-k
GET_LIMIT = 50000  # how many items to pull for sampling (increase if you want)
FUSION_CANDIDATES = 10  # candidates per template query before fusion

# Query expansion improves title-only benchmark recall for this collection/model.
QUERY_TEMPLATES = (
    "{title}",
    "Represent this sentence for searching relevant passages: {title}",
    "What is {title}?",
)

def main():
    """Sample titles from the index and test whether retrieval finds the same title."""
    client = chromadb.PersistentClient(path=PERSIST_DIR, settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(COLLECTION)

    model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    test_vec = model.encode(["dim_check"], normalize_embeddings=True)
    if len(test_vec[0]) != EMBED_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch for benchmark model: expected {EMBED_DIM}, got {len(test_vec[0])}"
        )

    # Pull a bunch of items and sample unique titles
    data = col.get(limit=GET_LIMIT, include=["metadatas"])
    metas = data["metadatas"]

    titles = [m.get("title","").strip() for m in metas if m and m.get("title")]
    titles = list({t for t in titles if len(t) >= 3})
    random.shuffle(titles)
    titles = titles[:N]

    if not titles:
        raise RuntimeError("No titles found in metadatas.")

    # Warmup
    _ = model.encode(["warmup"], normalize_embeddings=True)

    hits = 0
    t0 = time.time()

    for i, title in enumerate(titles, 1):
        query_texts = [tmpl.format(title=title) for tmpl in QUERY_TEMPLATES]
        query_embs = model.encode(query_texts, normalize_embeddings=True)

        # Merge candidates from multiple query variants using best (lowest) distance.
        best_distance_by_title = {}
        for emb in query_embs:
            emb_list = emb.tolist() if hasattr(emb, "tolist") else emb
            res = col.query(
                query_embeddings=[emb_list],
                n_results=max(K, FUSION_CANDIDATES),
                include=["metadatas", "distances"],
            )
            metas = res["metadatas"][0]
            dists = res["distances"][0]
            for meta, dist in zip(metas, dists):
                if not meta:
                    continue
                candidate_title = meta.get("title", "")
                if not candidate_title:
                    continue
                prev = best_distance_by_title.get(candidate_title)
                if prev is None or dist < prev:
                    best_distance_by_title[candidate_title] = dist

        ranked_titles = sorted(best_distance_by_title.items(), key=lambda x: x[1])[:K]
        got_titles = [candidate_title for candidate_title, _ in ranked_titles]
        if title in got_titles:
            hits += 1

        if i % 200 == 0:
            elapsed = time.time() - t0
            print(f"[status] {i}/{len(titles)} | recall@{K}={hits/i:.3f} | {i/elapsed:.1f} qps")

    elapsed = time.time() - t0
    print(f"\n[done] N={len(titles)} K={K}")
    print(f"recall@{K}: {hits/len(titles):.4f}")
    print(f"avg qps: {len(titles)/elapsed:.2f}")

if __name__ == "__main__":
    main()


