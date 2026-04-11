"""Build the local Chroma vector index from precomputed wiki chunks.

The earlier data-prep scripts produce JSONL files in backend-data/. This script reads
those chunk records, embeds them with the configured sentence-transformer model, and
stores both vectors and source text in the Chroma collection used at runtime.

Two-phase approach for speed:
  Phase 1 - Read chunks + embed on GPU/CPU -> accumulate in memory
  Phase 2 - Bulk-load all embeddings into Chroma at once
This avoids the progressive HNSW slowdown that makes incremental writes very slow.
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import List, Tuple

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
import numpy as np
import torch

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import (
    CHUNKS_FILE,
    COLLECTION_NAME,
    DEFAULT_DEVICE,
    EMBED_DIM,
    EMBED_MODEL_NAME,
    PERSIST_DIR,
)

CHROMA_BATCH = 1024
EMBED_BATCH = 64
DEFAULT_MAX_CPU_PERCENT = 90
MAX_EMBED_TEXT_CHARS = int(os.getenv("INDEX_MAX_EMBED_TEXT_CHARS", "6000"))
MIN_INDEX_WORDS = int(os.getenv("INDEX_MIN_WORDS", "50"))
MIN_INDEX_CHARS = int(os.getenv("INDEX_MIN_CHARS", "500"))
SMALL_SECTION_MAX_WORDS = int(os.getenv("INDEX_SMALL_SECTION_MAX_WORDS", "120"))
SMALL_SECTION_MAX_CHARS = int(os.getenv("INDEX_SMALL_SECTION_MAX_CHARS", "1200"))
SKIP_SECTION_TITLES = {
    # English
    "related pages",
    "other websites",
    "see also",
    "external links",
    "references",
    "further reading",
    "bibliography",
    "notes",
    # Spanish
    "paginas relacionadas",
    "otros sitios web",
    "vease tambien",
    "enlaces externos",
    "referencias",
    "lectura adicional",
    "bibliografia",
    "notas",
}


def count_lines(filepath: str) -> int:
    """Fast line count without loading entire file into memory."""
    count = 0
    with open(filepath, "rb") as f:
        while True:
            buf = f.raw.read(1024 * 1024)
            if not buf:
                break
            count += buf.count(b"\n")
    return count


def format_eta(seconds: float) -> str:
    """Format seconds into a human-readable ETA string."""
    if seconds <= 0 or not math.isfinite(seconds):
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def set_low_priority():
    """Set the current process to below-normal priority (Windows-aware)."""
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                BELOW_NORMAL_PRIORITY_CLASS,
            )
        else:
            os.nice(10)
    except Exception:
        pass


def get_or_create_collection_compatible(client, name: str):
    """Create the collection while tolerating older and newer Chroma APIs."""
    try:
        return client.get_or_create_collection(
            name=name,
            configuration={"hnsw": {"space": "cosine"}},
        )
    except TypeError:
        pass
    except Exception:
        pass

    try:
        return client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception:
        return client.get_or_create_collection(name=name)


def configure_cpu_threads(cpu_threads: int, workers: int, max_cpu_percent: int = 100) -> Tuple[int, int]:
    """Tune CPU thread counts for embedding-heavy indexing runs."""
    raw_threads = max(1, cpu_threads if cpu_threads > 0 else (os.cpu_count() or 1))
    threads = max(1, int(raw_threads * min(100, max(1, max_cpu_percent)) / 100))
    worker_threads = max(1, threads // max(1, workers))
    os.environ["OMP_NUM_THREADS"] = str(worker_threads if workers > 1 else threads)
    os.environ["MKL_NUM_THREADS"] = str(worker_threads if workers > 1 else threads)
    try:
        torch.set_num_threads(worker_threads if workers > 1 else threads)
    except Exception:
        pass
    try:
        torch.set_num_interop_threads(max(1, min(8, worker_threads if workers > 1 else threads)))
    except Exception:
        pass
    return threads, worker_threads


def build_embedding_text(title: str, section_path: str, section_title: str, text: str) -> str:
    """Assemble the text that gets embedded for each stored chunk."""
    header = []
    if title:
        header.append(f"Title: {title}")
    if section_path:
        header.append(f"Section: {section_path}")
    elif section_title:
        header.append(f"Section: {section_title}")

    body = (text or "").strip()
    if len(body) > MAX_EMBED_TEXT_CHARS:
        body = body[:MAX_EMBED_TEXT_CHARS].rsplit(" ", 1)[0].strip() or body[:MAX_EMBED_TEXT_CHARS]

    if not header:
        return body
    return "\n".join(header) + f"\n\n{body}"


def should_skip_chunk(title: str, section_title: str, word_count: int, char_count: int) -> Tuple[bool, str | None]:
    normalized_title = (title or "").strip().lower()
    normalized_section = (section_title or "").strip().lower()

    if word_count < MIN_INDEX_WORDS and char_count < MIN_INDEX_CHARS:
        return True, "tiny"
    if (
        normalized_section in SKIP_SECTION_TITLES
        and word_count < SMALL_SECTION_MAX_WORDS
        and char_count < SMALL_SECTION_MAX_CHARS
    ):
        return True, "support_section"
    if (
        normalized_title.startswith("list of ")
        and word_count < SMALL_SECTION_MAX_WORDS
        and char_count < SMALL_SECTION_MAX_CHARS
    ):
        return True, "small_list"
    return False, None


def main(
    chunks_file: str,
    persist_dir: str,
    collection_name: str,
    device: str,
    workers: int,
    cpu_threads: int,
    chroma_batch: int,
    embed_batch: int,
    max_cpu_percent: int = DEFAULT_MAX_CPU_PERCENT,
):
    set_low_priority()

    print(f"[init] counting chunks in {chunks_file} ...")
    total_lines = count_lines(chunks_file)
    print(f"[init] {total_lines:,} lines to process")

    if device.lower() == "cpu":
        configured_threads, worker_threads = configure_cpu_threads(cpu_threads, workers, max_cpu_percent)
    else:
        raw = max(1, cpu_threads if cpu_threads > 0 else (os.cpu_count() or 1))
        configured_threads = max(1, int(raw * min(100, max(1, max_cpu_percent)) / 100))
        worker_threads = configured_threads

    try:
        model = SentenceTransformer(EMBED_MODEL_NAME, device=device, local_files_only=True)
    except TypeError:
        model = SentenceTransformer(EMBED_MODEL_NAME, device=device)

    # Buffer many texts before calling encode() — encode() handles internal
    # batching via batch_size, so a large buffer amortizes Python overhead
    # and lets the GPU stay saturated.
    encode_buffer_size = max(embed_batch, 4096)

    print(
        f"[config] device={device} workers={workers} cpu_threads={configured_threads} "
        f"worker_threads={worker_threads} embed_batch={embed_batch} chroma_batch={chroma_batch} "
        f"encode_buffer={encode_buffer_size} max_cpu_percent={max_cpu_percent} "
        f"max_embed_text_chars={MAX_EMBED_TEXT_CHARS} "
        f"min_index_words={MIN_INDEX_WORDS} min_index_chars={MIN_INDEX_CHARS}"
    )

    test_vec = model.encode(["dim_check"], normalize_embeddings=True)
    dim = len(test_vec[0])
    if dim != EMBED_DIM:
        raise RuntimeError(f"Embedding dim mismatch: expected {EMBED_DIM}, got {dim}")

    # ── Phase 1: Read chunks, filter, embed in batches ──────────────────
    print("[phase1] reading and embedding chunks ...")

    all_ids: List[str] = []
    all_store_texts: List[str] = []
    all_metas: List[dict] = []
    all_embeddings: List[np.ndarray] = []

    batch_embed_texts: List[str] = []
    seen = 0
    skipped = 0
    embedded = 0
    skip_reasons = {"tiny": 0, "support_section": 0, "small_list": 0}
    t0 = time.time()
    last_report = t0

    try:
        with open(chunks_file, "r", encoding="utf-8") as f:
            for line in f:
                seen += 1
                obj = json.loads(line)

                cid = str(obj.get("chunk_id", "")).strip()
                text = (obj.get("text") or "").strip()
                title = (obj.get("title") or "").strip()
                page_id = obj.get("page_id", -1)
                chunk_index = obj.get("chunk_index", 0)
                section_title = (obj.get("section_title") or "").strip()
                section_path = (obj.get("section_path") or "").strip()
                word_count = int(obj.get("word_count") or 0)
                char_count = int(obj.get("char_count") or 0)

                if not cid or not text:
                    continue

                skip, reason = should_skip_chunk(title, section_title, word_count, char_count)
                if skip:
                    skipped += 1
                    if reason:
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue

                language = (obj.get("language") or "en").strip()

                meta = {
                    "page_id": int(page_id),
                    "title": title,
                    "chunk_index": int(chunk_index),
                    "section_title": section_title,
                    "section_path": section_path,
                    "word_count": int(word_count or len(text.split())),
                    "char_count": int(char_count or len(text)),
                    "language": language,
                }

                embed_text = build_embedding_text(title, section_path, section_title, text)

                all_ids.append(cid)
                all_store_texts.append(text)
                all_metas.append(meta)
                batch_embed_texts.append(embed_text)

                # Accumulate a large buffer, then encode with internal batching
                if len(batch_embed_texts) >= encode_buffer_size:
                    vecs = model.encode(
                        batch_embed_texts,
                        batch_size=max(1, embed_batch),
                        show_progress_bar=False,
                        normalize_embeddings=True,
                    )
                    all_embeddings.append(np.array(vecs, dtype=np.float32))
                    embedded += len(batch_embed_texts)
                    batch_embed_texts = []

                    now = time.time()
                    if now - last_report >= 2:
                        elapsed = now - t0
                        rate = embedded / max(1e-9, elapsed)
                        remaining = max(0, total_lines - seen)
                        eta_sec = remaining / rate if rate > 0 else 0
                        pct = (seen / total_lines * 100) if total_lines > 0 else 0
                        print(
                            f"[phase1] {seen:,} / {total_lines:,} ({pct:.1f}%) | "
                            f"embedded: {embedded:,} | skipped: {skipped:,} | "
                            f"{rate:,.0f} embeds/sec | ETA: {format_eta(eta_sec)}"
                        )
                        last_report = now

        # Flush remaining
        if batch_embed_texts:
            vecs = model.encode(
                batch_embed_texts,
                batch_size=max(1, embed_batch),
                show_progress_bar=False,
                normalize_embeddings=True,
            )
            all_embeddings.append(np.array(vecs, dtype=np.float32))
            embedded += len(batch_embed_texts)
    finally:
        pass

    # Free GPU memory before Chroma write phase
    del model
    if device.lower() == "cuda":
        torch.cuda.empty_cache()

    phase1_dt = time.time() - t0
    print(f"[phase1] done: {embedded:,} embedded, {skipped:,} skipped in {phase1_dt:,.1f}s "
          f"({embedded / max(1e-9, phase1_dt):,.0f} embeds/sec)")
    print(f"[phase1] skip reasons: {skip_reasons}")

    # Concatenate all embedding batches
    embeddings_array = np.concatenate(all_embeddings, axis=0)
    del all_embeddings
    print(f"[phase1] embeddings shape: {embeddings_array.shape} "
          f"({embeddings_array.nbytes / 1024 / 1024:,.0f} MB)")

    # ── Phase 2: Bulk-load into Chroma ──────────────────────────────────
    print(f"[phase2] loading {embedded:,} vectors into Chroma ...")

    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(anonymized_telemetry=False))
    try:
        client.delete_collection(name=collection_name)
    except Exception:
        pass
    col = get_or_create_collection_compatible(client, collection_name)

    t2 = time.time()
    last_report = t2
    loaded = 0

    for start in range(0, embedded, chroma_batch):
        end = min(start + chroma_batch, embedded)
        batch_embeds = embeddings_array[start:end].tolist()
        col.add(
            ids=all_ids[start:end],
            documents=all_store_texts[start:end],
            metadatas=all_metas[start:end],
            embeddings=batch_embeds,
        )
        loaded += (end - start)

        now = time.time()
        if now - last_report >= 2:
            elapsed = now - t2
            rate = loaded / max(1e-9, elapsed)
            remaining = max(0, embedded - loaded)
            eta_sec = remaining / rate if rate > 0 else 0
            pct = (loaded / embedded * 100) if embedded > 0 else 0
            print(
                f"[phase2] {loaded:,} / {embedded:,} ({pct:.1f}%) | "
                f"{rate:,.0f} writes/sec | ETA: {format_eta(eta_sec)}"
            )
            last_report = now

    phase2_dt = time.time() - t2
    total_dt = time.time() - t0
    print(f"[phase2] done: {loaded:,} vectors loaded in {phase2_dt:,.1f}s "
          f"({loaded / max(1e-9, phase2_dt):,.0f} writes/sec)")
    print(f"[done] total: {embedded:,} inserted, {skipped:,} skipped")
    print(f"[done] time: {total_dt:,.1f}s (embed: {phase1_dt:,.1f}s, load: {phase2_dt:,.1f}s)")
    print(f"[done] persist dir: {persist_dir} | collection: {collection_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Chroma index from chunk JSONL.")
    parser.add_argument("--chunks-file", default=CHUNKS_FILE)
    parser.add_argument("--persist-dir", default=PERSIST_DIR)
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 1)))
    parser.add_argument("--cpu-threads", type=int, default=max(1, (os.cpu_count() or 1)))
    parser.add_argument("--chroma-batch", type=int, default=CHROMA_BATCH)
    parser.add_argument("--embed-batch", type=int, default=0,
                        help="Sequences per forward pass. 0 = auto (based on device/VRAM).")
    parser.add_argument("--max-cpu-percent", type=int, default=DEFAULT_MAX_CPU_PERCENT,
                        help="Cap CPU thread usage to this percent of available cores (default 90).")
    args = parser.parse_args()

    embed_batch = args.embed_batch
    if embed_batch <= 0:
        if args.device.lower() == "cuda" and torch.cuda.is_available():
            vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            embed_batch = 16 if vram_gb < 10 else 64 if vram_gb < 16 else 128
            print(f"[init] GPU VRAM: {vram_gb:.1f} GB -> auto embed_batch={embed_batch}")
        else:
            embed_batch = EMBED_BATCH
    chroma_batch = args.chroma_batch

    main(
        args.chunks_file,
        args.persist_dir,
        args.collection,
        args.device,
        args.workers,
        args.cpu_threads,
        chroma_batch,
        embed_batch,
        args.max_cpu_percent,
    )
