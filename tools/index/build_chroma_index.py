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
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
from tools.pipeline_utils import count_lines, format_eta, set_low_priority

# CHROMA_BATCH controls how many vectors are flushed to Chroma per `col.add()`
# call during the bulk-load phase. Larger batches mean fewer commits and lower
# total overhead but raise peak RAM during a single write — each batch holds
# `chroma_batch * embed_dim * 4` bytes of float32 vectors in Python lists plus
# their metadata dicts. 256 is a safe default on a 16 GB RAM laptop; bump it
# to 1024+ on a workstation with plenty of headroom for faster loads. Exposed
# via --chroma-batch on the CLI.
CHROMA_BATCH = 256
# EMBED_BATCH is the sequences-per-forward-pass for the embedding model.
# bge-m3 typically sustains 128–256 on an RTX 3070 (8 GB VRAM). Drop this if
# you see CUDA OOM during phase 1. Exposed via --embed-batch on the CLI.
EMBED_BATCH = 256
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


INDEX_TOOL_VERSION = "2026-05-15.1"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(
    chunks_file: str,
    persist_dir: str,
    collection_name: str,
    device: str,
    workers: int,
    cpu_threads: int,
    chroma_batch: int,
    embed_batch: int,
    max_cpu_percent: int,
    total_lines: int,
    embedded: int,
    skipped: int,
    skip_reasons: Dict[str, int],
    embed_dimension: int,
    phase1_seconds: float,
    phase2_seconds: float,
    total_seconds: float,
) -> Dict[str, Any]:
    chunks_path = Path(chunks_file)
    model_path = Path(EMBED_MODEL_NAME)
    return {
        "schema_version": 1,
        "tool": "tools/index/build_chroma_index.py",
        "tool_version": INDEX_TOOL_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "chunks_file": str(chunks_path),
            "chunks_file_name": chunks_path.name,
            "chunks_file_size_bytes": chunks_path.stat().st_size if chunks_path.exists() else None,
            "chunks_file_sha256": sha256_file(str(chunks_path)) if chunks_path.exists() else None,
            "total_lines": total_lines,
        },
        "embedding_model": {
            "path": str(model_path),
            "name": str(EMBED_MODEL_NAME),
            "exists": model_path.exists(),
            "dimension": int(embed_dimension),
            "device": device,
        },
        "chroma": {
            "persist_dir": str(Path(persist_dir)),
            "collection_name": collection_name,
            "hnsw_space": "cosine",
            "chunk_count": int(embedded),
        },
        "chunker": {
            "max_embed_text_chars": MAX_EMBED_TEXT_CHARS,
            "min_index_words": MIN_INDEX_WORDS,
            "min_index_chars": MIN_INDEX_CHARS,
            "small_section_max_words": SMALL_SECTION_MAX_WORDS,
            "small_section_max_chars": SMALL_SECTION_MAX_CHARS,
            "skip_section_titles": sorted(SKIP_SECTION_TITLES),
        },
        "build": {
            "workers": int(workers),
            "cpu_threads": int(cpu_threads),
            "max_cpu_percent": int(max_cpu_percent),
            "embed_batch": int(embed_batch),
            "chroma_batch": int(chroma_batch),
            "embedded_chunks": int(embedded),
            "skipped_chunks": int(skipped),
            "skip_reasons": dict(skip_reasons),
            "phase1_seconds": round(float(phase1_seconds), 3),
            "phase2_seconds": round(float(phase2_seconds), 3),
            "total_seconds": round(float(total_seconds), 3),
        },
    }


def collection_metadata_from_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "hnsw:space": "cosine",
        "dimension": int((manifest.get("embedding_model") or {}).get("dimension") or 0),
        "index_manifest_schema": int(manifest.get("schema_version") or 1),
        "index_tool_version": str(manifest.get("tool_version") or ""),
        "index_built_at": str(manifest.get("built_at") or ""),
        "source_sha256": str(((manifest.get("source") or {}).get("chunks_file_sha256")) or ""),
        "source_file": str(((manifest.get("source") or {}).get("chunks_file_name")) or ""),
        "chunk_count": int(((manifest.get("chroma") or {}).get("chunk_count")) or 0),
    }


def write_manifest(persist_dir: str, manifest: Dict[str, Any]) -> Path:
    path = Path(persist_dir) / "index_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def get_or_create_collection_compatible(client, name: str, metadata: Dict[str, Any] | None = None):
    """Create the collection while tolerating older and newer Chroma APIs."""
    metadata = metadata or {"hnsw:space": "cosine"}
    try:
        return client.get_or_create_collection(
            name=name,
            configuration={"hnsw": {"space": "cosine"}},
            metadata=metadata,
        )
    except TypeError:
        pass

    try:
        return client.get_or_create_collection(
            name=name,
            metadata=metadata,
        )
    except TypeError:
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

    # Fail fast when the local embedding model is absent. The previous fallback
    # silently caught the TypeError from sentence-transformers' older signature
    # AND from missing files, which let us download the wrong checkpoint and
    # build a corrupted index. We now retry without local_files_only ONLY when
    # the path exists — a missing path is a real error and should stop the run.
    embed_model_path = Path(EMBED_MODEL_NAME)
    if not embed_model_path.exists():
        raise FileNotFoundError(
            f"Embedding model not found at {embed_model_path}. "
            f"Set EMBED_MODEL to a real local path, or download the model "
            f"into {embed_model_path} before running this script. We do not "
            f"silently fall back to network download to avoid building the "
            f"index with the wrong checkpoint."
        )
    try:
        model = SentenceTransformer(str(embed_model_path), device=device, local_files_only=True)
    except TypeError:
        # Older sentence-transformers versions don't accept local_files_only.
        model = SentenceTransformer(str(embed_model_path), device=device)

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
    embed_dimension = len(test_vec[0])
    if embed_dimension != EMBED_DIM:
        raise RuntimeError(f"Embedding dim mismatch: expected {EMBED_DIM}, got {embed_dimension}")

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

    # Free GPU memory before Chroma write phase
    del model
    if device.lower() == "cuda":
        torch.cuda.empty_cache()

    phase1_dt = time.time() - t0
    print(f"[phase1] done: {embedded:,} embedded, {skipped:,} skipped in {phase1_dt:,.1f}s "
          f"({embedded / max(1e-9, phase1_dt):,.0f} embeds/sec)")
    print(f"[phase1] skip reasons: {skip_reasons}")

    if not all_embeddings:
        print("[phase1] no chunks passed filters — nothing to index")
        return

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
    manifest = build_manifest(
        chunks_file=chunks_file,
        persist_dir=persist_dir,
        collection_name=collection_name,
        device=device,
        workers=workers,
        cpu_threads=configured_threads,
        chroma_batch=chroma_batch,
        embed_batch=embed_batch,
        max_cpu_percent=max_cpu_percent,
        total_lines=total_lines,
        embedded=embedded,
        skipped=skipped,
        skip_reasons=skip_reasons,
        embed_dimension=embed_dimension,
        phase1_seconds=phase1_dt,
        phase2_seconds=0.0,
        total_seconds=0.0,
    )
    col = get_or_create_collection_compatible(client, collection_name, collection_metadata_from_manifest(manifest))

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
    manifest["build"]["phase2_seconds"] = round(float(phase2_dt), 3)
    manifest["build"]["total_seconds"] = round(float(total_dt), 3)
    manifest_path = write_manifest(persist_dir, manifest)
    print(f"[phase2] done: {loaded:,} vectors loaded in {phase2_dt:,.1f}s "
          f"({loaded / max(1e-9, phase2_dt):,.0f} writes/sec)")
    print(f"[done] total: {embedded:,} inserted, {skipped:,} skipped")
    print(f"[done] time: {total_dt:,.1f}s (embed: {phase1_dt:,.1f}s, load: {phase2_dt:,.1f}s)")
    print(f"[done] persist dir: {persist_dir} | collection: {collection_name}")
    print(f"[done] manifest: {manifest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Chroma index from chunk JSONL.")
    parser.add_argument("--chunks-file", default=CHUNKS_FILE)
    parser.add_argument("--persist-dir", default=PERSIST_DIR)
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 1)))
    parser.add_argument("--cpu-threads", type=int, default=max(1, (os.cpu_count() or 1)))
    parser.add_argument("--chroma-batch", type=int, default=CHROMA_BATCH,
                        help=(f"Vectors per Chroma write batch (default {CHROMA_BATCH}). "
                              "Larger = fewer commits but more peak RAM during a single write."))
    parser.add_argument("--embed-batch", type=int, default=EMBED_BATCH,
                        help=(f"Sequences per embedding forward pass (default {EMBED_BATCH}). "
                              "bge-m3 typically sustains 128–256 on an RTX 3070; "
                              "drop this if you hit CUDA OOM. Pass 0 to auto-pick based on VRAM."))
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
