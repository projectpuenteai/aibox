"""Validate an AIBox appdata backup without modifying it."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from crypto_blobs import decrypt_blob, load_key, looks_like_encrypted_blob


def verify(args: argparse.Namespace) -> int:
    appdata = Path(args.appdata_root).resolve()
    db_path = Path(args.db_path).resolve() if args.db_path else appdata / "db" / "app.db"
    users_root = Path(args.users_root).resolve() if args.users_root else appdata / "users"
    key_raw = os.getenv("APP_ENCRYPTION_MASTER_KEY", "")
    key = load_key(key_raw) if key_raw else None

    result: Dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "appdata_root": str(appdata),
        "db_path": str(db_path),
        "users_root": str(users_root),
        "db_integrity_ok": False,
        "encrypted_samples_checked": 0,
        "encrypted_samples_failed": 0,
        "errors": [],
    }
    errors: List[str] = []

    if not db_path.exists():
        errors.append(f"database not found: {db_path}")
    else:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                result["db_integrity_ok"] = bool(row and str(row[0]).lower() == "ok")
                if not result["db_integrity_ok"]:
                    errors.append(f"sqlite integrity_check failed: {row[0] if row else 'no result'}")
            finally:
                conn.close()
        except Exception as exc:
            errors.append(f"sqlite open/integrity failed: {type(exc).__name__}: {exc}")

    if users_root.exists() and key is not None:
        checked = 0
        failed = 0
        for path in sorted(users_root.rglob("*.json")):
            if checked >= args.max_samples:
                break
            if not looks_like_encrypted_blob(path):
                continue
            checked += 1
            try:
                plain = decrypt_blob(path.read_bytes(), key)
                json.loads(plain.decode("utf-8"))
            except Exception as exc:
                failed += 1
                errors.append(f"encrypted sample failed {path}: {type(exc).__name__}: {exc}")
        result["encrypted_samples_checked"] = checked
        result["encrypted_samples_failed"] = failed
    elif users_root.exists():
        errors.append("APP_ENCRYPTION_MASTER_KEY not set; skipped encrypted blob samples")
    else:
        errors.append(f"users root not found: {users_root}")

    result["errors"] = errors
    print(json.dumps(result, indent=2))
    return 0 if result["db_integrity_ok"] and not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an AIBox appdata backup.")
    parser.add_argument("--appdata-root", default="aibox/backend-data/appdata")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--users-root", default="")
    parser.add_argument("--max-samples", type=int, default=25)
    return verify(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
