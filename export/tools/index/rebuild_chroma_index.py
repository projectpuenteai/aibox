"""Back up the current Chroma directory and rebuild it from chunk JSONL.

This wraps tools/index/build_chroma_index.py so operators have a safer one-command
rebuild path. The backup step protects the previous vector store if the new build
fails or produces worse retrieval results.
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import (
    BACKEND_DATA_DIR,
    CHUNKS_FILE,
    COLLECTION_NAME,
    DEFAULT_DEVICE,
    PERSIST_DIR,
)
from tools.index.build_chroma_index import main as build_index


def backup_persist_dir(persist_dir: str, backup_root: Path) -> Path | None:
    """Move the current Chroma directory to a timestamped backup location."""
    src = Path(persist_dir)
    if not src.exists() or not any(src.iterdir()):
        src.mkdir(parents=True, exist_ok=True)
        return None

    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    dest = backup_root / f"chroma_db-{stamp}"
    shutil.move(str(src), str(dest))
    src.mkdir(parents=True, exist_ok=True)
    return dest


def run_rebuild(
    chunks_file: str,
    persist_dir: str,
    collection_name: str,
    device: str,
    backup_root: str,
    workers: int,
    cpu_threads: int,
    chroma_batch: int,
    embed_batch: int,
    max_cpu_percent: int = 90,
) -> None:
    backup_path = backup_persist_dir(persist_dir, Path(backup_root))
    if backup_path is None:
        print(f"[info] no existing index to back up in {persist_dir}")
    else:
        print(f"[info] backup created: {backup_path}")

    print("[info] rebuilding index...")
    build_index(
        chunks_file,
        persist_dir,
        collection_name,
        device,
        workers,
        cpu_threads,
        chroma_batch,
        embed_batch,
        max_cpu_percent,
    )
    print("[done] rebuild complete")


def main() -> None:
    """Parse CLI args for a rebuild and run the full backup-plus-build flow."""
    parser = argparse.ArgumentParser(
        description="Back up current Chroma DB and rebuild from chunks.jsonl deterministically."
    )
    parser.add_argument("--chunks-file", default=CHUNKS_FILE)
    parser.add_argument("--persist-dir", default=PERSIST_DIR)
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--cpu-threads", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--chroma-batch", type=int, default=1024)
    parser.add_argument("--embed-batch", type=int, default=0,
                        help="Sequences per forward pass. 0 = auto (256 for CUDA, 64 for CPU).")
    parser.add_argument("--max-cpu-percent", type=int, default=90,
                        help="Cap CPU thread usage to this percent of available cores (default 90).")
    parser.add_argument(
        "--backup-root",
        default=str(BACKEND_DATA_DIR / "chroma_backups"),
        help="Directory where timestamped backups are stored.",
    )
    args = parser.parse_args()

    embed_batch = args.embed_batch
    if embed_batch <= 0:
        if args.device.lower() == "cuda":
            try:
                import torch
                if torch.cuda.is_available():
                    vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
                    embed_batch = 16 if vram_gb < 10 else 64 if vram_gb < 16 else 128
                else:
                    embed_batch = 64
            except Exception:
                embed_batch = 64
        else:
            embed_batch = 64
    chroma_batch = args.chroma_batch

    run_rebuild(
        chunks_file=args.chunks_file,
        persist_dir=args.persist_dir,
        collection_name=args.collection,
        device=args.device,
        backup_root=args.backup_root,
        workers=args.workers,
        cpu_threads=args.cpu_threads,
        chroma_batch=chroma_batch,
        embed_batch=embed_batch,
        max_cpu_percent=args.max_cpu_percent,
    )


if __name__ == "__main__":
    main()

