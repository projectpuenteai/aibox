"""Rotate AIBox encrypted document/chat blobs from one master key to another.

This tool never reads keys from command-line arguments. Provide keys through
environment variables or prompt input so they do not land in shell history.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from crypto_blobs import decrypt_blob, encrypt_blob, load_key, looks_like_encrypted_blob, write_bytes_atomic


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_key(env_name: str, prompt: str) -> bytes:
    raw = os.getenv(env_name)
    if raw is None:
        raw = getpass.getpass(prompt)
    return load_key(raw)


def _find_blob_files(root: Path) -> List[Path]:
    return sorted(path for path in root.rglob("*.json") if looks_like_encrypted_blob(path))


def _copy_backup(files: List[Path], source_root: Path, backup_root: Path) -> None:
    backup_root.mkdir(parents=True, exist_ok=False)
    for path in files:
        rel = path.relative_to(source_root)
        dst = backup_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)


def rotate(args: argparse.Namespace) -> int:
    source_root = Path(args.users_root).resolve()
    if not source_root.exists() or not source_root.is_dir():
        raise SystemExit(f"users root not found: {source_root}")

    old_key = _read_key("AIBOX_OLD_ENCRYPTION_MASTER_KEY", "Old APP_ENCRYPTION_MASTER_KEY: ")
    new_key = _read_key("AIBOX_NEW_ENCRYPTION_MASTER_KEY", "New APP_ENCRYPTION_MASTER_KEY: ")
    if old_key == new_key:
        raise SystemExit("old and new keys are identical; refusing to rotate")

    files = _find_blob_files(source_root)
    manifest: Dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "users_root": str(source_root),
        "dry_run": bool(args.dry_run),
        "files_total": len(files),
        "files_rotated": 0,
        "files_failed": 0,
        "failures": [],
    }
    if not files:
        print(json.dumps(manifest, indent=2))
        return 0

    backup_root = Path(args.backup_root).resolve() / f"encrypted-blobs-before-key-rotation-{_now_stamp()}"
    if not args.dry_run:
        _copy_backup(files, source_root, backup_root)
        manifest["backup_root"] = str(backup_root)

    failures: List[Dict[str, str]] = []
    rotated = 0
    for path in files:
        rel = str(path.relative_to(source_root))
        try:
            plain = decrypt_blob(path.read_bytes(), old_key)
            rotated_blob = encrypt_blob(plain, new_key)
            # Verify before replacing the source file.
            if decrypt_blob(rotated_blob, new_key) != plain:
                raise RuntimeError("post-encryption verification mismatch")
            if not args.dry_run:
                write_bytes_atomic(path, rotated_blob)
            rotated += 1
        except Exception as exc:
            failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            if args.stop_on_error:
                break

    manifest["files_rotated"] = rotated
    manifest["files_failed"] = len(failures)
    manifest["failures"] = failures

    manifest_path = Path(args.manifest).resolve() if args.manifest else None
    output = json.dumps(manifest, indent=2)
    if manifest_path:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate AIBox encrypted user blob files between master keys.")
    parser.add_argument("--users-root", default="aibox/backend-data/appdata/users", help="Path to appdata/users.")
    parser.add_argument("--backup-root", default="aibox/backend-data/appdata/backups/key-rotation", help="Where source blob backups are copied before writes.")
    parser.add_argument("--manifest", default="", help="Optional path to write a JSON rotation manifest.")
    parser.add_argument("--dry-run", action="store_true", help="Decrypt and verify candidates without writing files or backups.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop at the first failed file.")
    return rotate(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
