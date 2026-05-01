"""Compare English vs Spanish ChromaDB recall@10. Pulls titles in batches to avoid SQL var limit."""

import os
import random
import sys
import time
from pathlib import Path

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parents[2]
EMBED_MODEL = os.getenv("EMBED_MODEL", str(ROOT / "models" / "embed-m3"))
DEVICE = os.getenv("INDEX_DEVICE", "cpu")
COLLECTION = os.getenv("CHROMA_COLLECTION", "simplewiki_chunks")
PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", str(ROOT / "backend-data" / "chroma_db"))
LANG = os.getenv("BENCH_LANG", "en")

N = 500
K = 10
SAMPLE_BATCH = 800
SAMPLE_TOTAL = 8000

QUERY_TEMPLATES_EN = (
    "{title}",
    "Represent this sentence for searching relevant passages: {title}",
    "What is {title}?",
)
QUERY_TEMPLATES_ES = (
    "{title}",
    "Representa esta oracion para buscar pasajes relevantes: {title}",
    "Que es {title}?",
)
templates = QUERY_TEMPLATES_ES if LANG == "es" else QUERY_TEMPLATES_EN


def main():
    print(f"[bench] lang={LANG} persist={PERSIST_DIR} collection={COLLECTION}")
    client = chromadb.PersistentClient(path=PERSIST_DIR, settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(COLLECTION)
    total = col.count()
    print(f"[bench] collection count: {total}")

    titles_seen = set()
    pulled = 0
    while len(titles_seen) < SAMPLE_TOTAL // 2 and pulled < SAMPLE_TOTAL:
        offset = random.randrange(max(1, total - SAMPLE_BATCH))
        try:
            data = col.get(limit=SAMPLE_BATCH, offset=offset, include=["metadatas"])
        except Exception as e:
            print(f"[warn] sample batch failed offset={offset}: {e}")
            pulled += SAMPLE_BATCH
            continue
        for m in data.get("metadatas") or []:
            if m and m.get("title"):
                t = str(m["title"]).strip()
                if len(t) >= 3:
                    titles_seen.add(t)
        pulled += SAMPLE_BATCH
    titles = list(titles_seen)
    random.shuffle(titles)
    titles = titles[:N]
    print(f"[bench] sampled {len(titles)} unique titles")
    if not titles:
        raise SystemExit("no titles sampled")

    print(f"[bench] loading embedder: {EMBED_MODEL} device={DEVICE}")
    model = SentenceTransformer(EMBED_MODEL, device=DEVICE)
    _ = model.encode(["warmup"], normalize_embeddings=True)

    hits = 0
    t0 = time.time()
    sample_results = []
    for i, title in enumerate(titles, 1):
        qs = [tmpl.format(title=title) for tmpl in templates]
        embs = model.encode(qs, normalize_embeddings=True).tolist()
        best = {}
        for emb in embs:
            res = col.query(query_embeddings=[emb], n_results=max(K, 10), include=["metadatas", "distances"])
            for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
                if not meta:
                    continue
                t = meta.get("title", "")
                if not t:
                    continue
                prev = best.get(t)
                if prev is None or dist < prev:
                    best[t] = dist
        ranked = sorted(best.items(), key=lambda x: x[1])[:K]
        got = [t for t, _ in ranked]
        if title in got:
            hits += 1
        if i <= 3:
            sample_results.append((title, got[:5]))
        if i % 100 == 0:
            print(f"[status] {i}/{len(titles)} recall@{K}={hits/i:.3f} qps={i/(time.time()-t0):.1f}")

    elapsed = time.time() - t0
    print(f"\n[done] lang={LANG} N={len(titles)} K={K}")
    print(f"recall@{K}: {hits/len(titles):.4f}")
    print(f"qps: {len(titles)/elapsed:.2f}")
    print(f"sample queries:")
    for t, got in sample_results:
        print(f"  query={t!r}")
        for g in got:
            print(f"    -> {g!r}")


if __name__ == "__main__":
    main()
