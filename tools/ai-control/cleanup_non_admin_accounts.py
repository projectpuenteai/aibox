"""Delete all non-admin accounts and their persisted app data.

This is a one-time maintenance utility for clearing test clutter from the
mounted backend-data volume without changing normal backend behavior.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable, List


def repo_root() -> Path:
    path = Path(__file__).resolve()
    return path.parents[2] if len(path.parents) >= 3 else path.parent


def default_data_root() -> Path:
    return repo_root() / "backend-data"


def safe_user_dir(users_root: Path, user_id: str) -> Path:
    root = users_root.resolve()
    target = (root / user_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Refusing to access path outside users root: {target}") from exc
    return target


def remove_user_dir(users_root: Path, user_id: str) -> bool:
    target = safe_user_dir(users_root, user_id)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def cleanup_non_admin_accounts(db_path: Path, data_root: Path) -> dict:
    users_root = (data_root / "users").resolve()
    db_path = db_path.resolve()
    data_root = data_root.resolve()
    summary = {
        "deleted_users": 0,
        "deleted_dirs": 0,
        "deleted_documents": 0,
        "deleted_chats": 0,
        "deleted_folders": 0,
        "deleted_sessions": 0,
        "deleted_login_attempts": 0,
        "deleted_restrictions": 0,
        "deleted_rate_events": 0,
        "deleted_security_events": 0,
    }

    user_rows: List[sqlite3.Row]
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        user_rows = conn.execute(
            "SELECT id, username, username_norm FROM users WHERE role <> 'admin' ORDER BY username_norm"
        ).fetchall()

        user_ids = [str(row["id"]) for row in user_rows]
        username_norms = [str(row["username_norm"]) for row in user_rows]
        if not user_ids:
            conn.commit()
            return summary

        placeholders = ",".join("?" for _ in user_ids)
        user_args: Iterable[str] = tuple(user_ids)
        summary["deleted_documents"] = int(
            conn.execute(f"SELECT COUNT(*) FROM documents WHERE user_id IN ({placeholders})", user_args).fetchone()[0]
        )
        summary["deleted_chats"] = int(
            conn.execute(f"SELECT COUNT(*) FROM chats WHERE user_id IN ({placeholders})", user_args).fetchone()[0]
        )
        summary["deleted_folders"] = int(
            conn.execute(f"SELECT COUNT(*) FROM chat_folders WHERE user_id IN ({placeholders})", user_args).fetchone()[0]
        )
        summary["deleted_sessions"] = int(
            conn.execute(f"SELECT COUNT(*) FROM sessions WHERE user_id IN ({placeholders})", user_args).fetchone()[0]
        )
        summary["deleted_restrictions"] = int(
            conn.execute(f"SELECT COUNT(*) FROM user_restrictions WHERE user_id IN ({placeholders})", user_args).fetchone()[0]
        )
        summary["deleted_rate_events"] = int(
            conn.execute(f"SELECT COUNT(*) FROM rate_events WHERE user_id IN ({placeholders})", user_args).fetchone()[0]
        )
        summary["deleted_security_events"] = int(
            conn.execute(f"SELECT COUNT(*) FROM security_events WHERE user_id IN ({placeholders})", user_args).fetchone()[0]
        )

        if username_norms:
            login_placeholders = ",".join("?" for _ in username_norms)
            login_args: Iterable[str] = tuple(username_norms)
            summary["deleted_login_attempts"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM login_attempts WHERE username_norm IN ({login_placeholders})",
                    login_args,
                ).fetchone()[0]
            )
            conn.execute(
                f"DELETE FROM login_attempts WHERE username_norm IN ({login_placeholders})",
                login_args,
            )

        conn.execute(f"DELETE FROM security_events WHERE user_id IN ({placeholders})", user_args)
        conn.execute(f"DELETE FROM rate_events WHERE user_id IN ({placeholders})", user_args)
        conn.execute(f"DELETE FROM users WHERE id IN ({placeholders})", user_args)
        conn.commit()
        summary["deleted_users"] = len(user_ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for user_id in user_ids:
        if remove_user_dir(users_root, user_id):
            summary["deleted_dirs"] += 1
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete all non-admin accounts and their persisted app data.")
    parser.add_argument("--data-root", default=None, help="Path to backend data root.")
    parser.add_argument("--db-path", default=None, help="Path to app.db. Defaults to <data-root>/db/app.db.")
    args = parser.parse_args()

    data_root = Path(args.data_root) if args.data_root else default_data_root()
    db_path = Path(args.db_path) if args.db_path else data_root / "db" / "app.db"
    summary = cleanup_non_admin_accounts(db_path=db_path, data_root=data_root)

    print("Non-admin cleanup complete.")
    for key, value in summary.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
