"""Shared constants for the offline wiki extraction and indexing pipeline.

Keeping these paths in one file ensures the extraction, chunking, index build, and
inspection scripts all point to the same local models and backend-data layout.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = Path(os.getenv("AIBOX_MODELS_DIR", str(REPO_ROOT / "models")))
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL", str(MODELS_DIR / "embed-m3"))
EMBED_DIM = 1024
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "simplewiki_chunks")


def _default_index_device() -> str:
    """Prefer CUDA automatically when available, unless the caller overrides it."""
    override = os.getenv("INDEX_DEVICE")
    if override:
        return override

    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass

    return "cpu"


DEFAULT_DEVICE = _default_index_device()
BACKEND_DATA_DIR = REPO_ROOT / "backend-data"
PERSIST_DIR = str(BACKEND_DATA_DIR / "chroma_db")
PAGES_FILE = str(BACKEND_DATA_DIR / "pages.jsonl")
CHUNKS_FILE = str(BACKEND_DATA_DIR / "chunks.jsonl")
