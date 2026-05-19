import base64
import sqlite3
import sys
from pathlib import Path


AI_CONTROL_DIR = Path(__file__).resolve().parents[1] / "ai-control"
if str(AI_CONTROL_DIR) not in sys.path:
    sys.path.insert(0, str(AI_CONTROL_DIR))

import app_storage  # type: ignore
import storage_migrations  # type: ignore


def _runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "db" / "app.db"))
    monkeypatch.setenv("SESSION_TOKEN_PEPPER", "test-pepper")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "changeme")
    monkeypatch.setenv("APP_ENCRYPTION_MASTER_KEY", base64.b64encode(b"3" * 32).decode("ascii"))
    return app_storage.StorageRuntime("http://localhost:2020")


def test_init_db_records_numbered_migrations(monkeypatch, tmp_path):
    runtime = _runtime(monkeypatch, tmp_path)

    runtime.init_db()
    runtime.init_db()

    with runtime.db() as c:
        rows = c.execute("SELECT version,name FROM schema_migrations ORDER BY version").fetchall()
        assert [(int(row["version"]), row["name"]) for row in rows] == [
            (migration.version, migration.name) for migration in storage_migrations.MIGRATIONS
        ]
        assert "active_generations" in {
            row["name"] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }


def test_init_db_upgrades_old_ad_hoc_schema(monkeypatch, tmp_path):
    runtime = _runtime(monkeypatch, tmp_path)
    runtime.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(runtime.db_path)) as c:
        c.executescript(
            """
            CREATE TABLE users(
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                username_norm TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE chats(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                file_path TEXT NOT NULL
            );
            """
        )

    runtime.init_db()

    with runtime.db() as c:
        user_columns = set(runtime.table_columns(c, "users"))
        chat_columns = set(runtime.table_columns(c, "chats"))
        assert {"preferred_language", "preferred_theme", "guest_logout_at"}.issubset(user_columns)
        assert {"is_saved", "folder_id", "deleted_by_user", "is_guest_owned"}.issubset(chat_columns)
        assert c.execute("SELECT COUNT(*) c FROM schema_migrations").fetchone()["c"] == len(storage_migrations.MIGRATIONS)
