"""Measure average retrieval latency for the local embedding model and index."""

import time
import statistics
import sys
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

K = 5
N = 300

QUERIES = [
    "What is April?",
    "Who was Albert Einstein?",
    "How does photosynthesis work?",
    "What is the capital of France?",
    "Explain gravity in simple terms.",
    "What is a volcano?",
    "What is the Internet?",
    "How do airplanes fly?",
]

def main():
    """Run repeated retrieval queries and print simple latency percentiles."""
    client = chromadb.PersistentClient(path=PERSIST_DIR, settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(COLLECTION)
    model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    test_vec = model.encode(["dim_check"], normalize_embeddings=True)
    if len(test_vec[0]) != EMBED_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch for latency benchmark model: expected {EMBED_DIM}, got {len(test_vec[0])}"
        )

    # Warmup
    _ = model.encode(["warmup"], normalize_embeddings=True)
    _ = col.query(query_embeddings=model.encode(["warmup"], normalize_embeddings=True).tolist(),
                  n_results=K, include=["metadatas"])

    times = []
    for i in range(N):
        q = QUERIES[i % len(QUERIES)]
        t0 = time.time()
        q_emb = model.encode([q], normalize_embeddings=True).tolist()
        _ = col.query(query_embeddings=q_emb, n_results=K, include=["metadatas"])
        times.append((time.time() - t0) * 1000.0)

    print(f"N={N} K={K}")
    print(f"mean ms: {statistics.mean(times):.2f}")
    print(f"p50 ms:  {statistics.median(times):.2f}")
    print(f"p95 ms:  {statistics.quantiles(times, n=20)[18]:.2f}")
    print(f"qps:     {1000.0 / statistics.mean(times):.2f}")

if __name__ == "__main__":
    main()


