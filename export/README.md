# AIBox Export

This folder is a portable RAG snapshot for moving the embedding pipeline to another machine.

Included here:

- `tools/config/`, `tools/data_prep/`, and `tools/index/`
- `models/embed-m3/` and `models/rerank/`
- `backend-data/pages.jsonl`, `backend-data/chunks.jsonl`, and the current `backend-data/chroma_db/`
- `requirements.txt`

What this export is for:

- rebuilding the index from `backend-data/chunks.jsonl`
- reusing the current `backend-data/chroma_db/` without rebuilding
- keeping the local model and data paths relative so the folder works from any location

What it does not include:

- the raw Wikipedia dump
- the legacy app stack
- unrelated runtime data, backups, or caches

Usage:

1. Put `export/` anywhere on the machine you want to use.
2. Install the Python dependencies from `requirements.txt`.
3. If you want to rebuild the index, run the scripts under `tools/data_prep/` and `tools/index/`.
4. If the machine already has a CUDA-capable GPU, the export copy will prefer it automatically.

The export copy is intentionally separate from the main repo code so the original branch stays untouched.
