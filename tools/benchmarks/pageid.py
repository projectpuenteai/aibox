"""Check how often retrieval finds a chunk from the same source page."""

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
DEVICE = DEFAULT_DEVICE

N = 2000
K = 10
QUERY_CHARS = 220

def main():
    """Sample stored chunks and score same-page recall at the configured top-k."""
    client = chromadb.PersistentClient(path=PERSIST_DIR, settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(COLLECTION)
    model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    test_vec = model.encode(["dim_check"], normalize_embeddings=True)
    if len(test_vec[0]) != EMBED_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch for pageid benchmark model: expected {EMBED_DIM}, got {len(test_vec[0])}"
        )

    data = col.get(limit=N, include=["documents", "metadatas"])
    docs = data["documents"]
    metas = data["metadatas"]

    pairs = []
    for d, m in zip(docs, metas):
        if not d or not m: 
            continue
        pid = m.get("page_id", None)
        if pid is None:
            continue
        q = d.strip().replace("\n", " ")
        if len(q) < 50:
            continue
        pairs.append((pid, q[:QUERY_CHARS]))

    random.shuffle(pairs)
    pairs = pairs[:N]
    if not pairs:
        raise RuntimeError("Not enough docs/metas for benchmark.")

    _ = model.encode(["warmup"], normalize_embeddings=True)

    hits = 0
    t0 = time.time()

    for i, (pid, q) in enumerate(pairs, 1):
        q_emb = model.encode([q], normalize_embeddings=True).tolist()
        res = col.query(query_embeddings=q_emb, n_results=K, include=["metadatas"])
        got_pids = [m.get("page_id") for m in res["metadatas"][0] if m]
        if pid in got_pids:
            hits += 1

        if i % 200 == 0:
            elapsed = time.time() - t0
            print(f"[status] {i}/{len(pairs)} | same-page@{K}={hits/i:.3f} | {i/elapsed:.1f} qps")

    elapsed = time.time() - t0
    print(f"\n[done] N={len(pairs)} K={K}")
    print(f"same-page@{K}: {hits/len(pairs):.4f}")
    print(f"avg qps: {len(pairs)/elapsed:.2f}")

if __name__ == "__main__":
    main()


