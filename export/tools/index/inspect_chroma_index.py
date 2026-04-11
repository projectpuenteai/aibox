"""Inspect collection size and preview a few stored Chroma rows."""

import argparse
import sys
from pathlib import Path

import chromadb
from chromadb.config import Settings

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import COLLECTION_NAME, PERSIST_DIR


def main(persist_dir: str, collection: str, limit: int):
    """Print basic index stats and a small sample of stored documents."""
    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(collection)

    print("count:", col.count())

    sample = col.get(limit=limit, include=["metadatas", "documents"])
    for i in range(len(sample["ids"])):
        print("=" * 80)
        print("id:", sample["ids"][i])
        print("meta:", sample["metadatas"][i])
        print("doc:", (sample["documents"][i] or "")[:300])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect Chroma index stats and sample rows.")
    parser.add_argument("--persist-dir", default=PERSIST_DIR)
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    main(args.persist_dir, args.collection, args.limit)

