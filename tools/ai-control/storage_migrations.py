"""SQLite schema migrations for the AIBox storage runtime.

Migrations are intentionally idempotent because field devices may have databases
created by older ad hoc schema guards. Each migration can be run repeatedly and
is recorded after it succeeds.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, List


MigrationFn = Callable[[sqlite3.Connection], None]

# SQLite identifier whitelist: PRAGMA does not accept parameter binding, so the
# only way to use it safely with a dynamic table name is to reject anything
# that isn't a plain identifier. Used by table_columns / ensure_column below.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str, kind: str = "identifier") -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe {kind}: {name!r}")
    return name


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationFn


def table_columns(c: sqlite3.Connection, table: str) -> List[str]:
    safe = _validate_ident(table, "table name")
    return [str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in c.execute(f"PRAGMA table_info({safe})").fetchall()]


def ensure_column(c: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    safe_table = _validate_ident(table, "table name")
    safe_column = _validate_ident(column, "column name")
    if safe_column in table_columns(c, safe_table):
        return
    c.execute(f"ALTER TABLE {safe_table} ADD COLUMN {ddl}")
    # SQLite ALTER TABLE ADD COLUMN with NOT NULL + DEFAULT will populate
    # existing rows with the default automatically, but if the migration is
    # ever rerun on a partially-upgraded DB the column may already exist with
    # NULLs in it. Backfill from the DEFAULT clause when one is present and
    # NULLs are forbidden.
    ddl_lower = ddl.lower()
    if "not null" in ddl_lower and "default" in ddl_lower:
        m = re.search(r"default\s+([^\s,]+(?:\s+[^\s,]+)?)", ddl, flags=re.IGNORECASE)
        if m:
            default_expr = m.group(1).strip()
            c.execute(f"UPDATE {safe_table} SET {safe_column} = {default_expr} WHERE {safe_column} IS NULL")


def _migration_001_core_schema(c: sqlite3.Connection) -> None:
    c.executescript(
        """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,username_norm TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL,created_at TEXT NOT NULL,last_login_at TEXT,last_active_at TEXT,storage_bytes_used INTEGER NOT NULL DEFAULT 0,is_deleted INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,locked_until TEXT,lock_reason TEXT,preferred_language TEXT NOT NULL DEFAULT 'es',preferred_theme TEXT NOT NULL DEFAULT 'light');
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,last_accessed_at TEXT,ip TEXT,user_agent TEXT,revoked_at TEXT,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS ix_sessions_token ON sessions(token_hash);
CREATE TABLE IF NOT EXISTS login_attempts(username_norm TEXT NOT NULL,ip TEXT NOT NULL,fail_count INTEGER NOT NULL DEFAULT 0,first_attempt_ts INTEGER NOT NULL,last_attempt_ts INTEGER NOT NULL,lockout_until_ts INTEGER,PRIMARY KEY(username_norm,ip));
CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_accessed_at TEXT,token_count_estimate INTEGER NOT NULL DEFAULT 0,file_path TEXT NOT NULL,size_bytes INTEGER NOT NULL DEFAULT 0,is_deleted INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,deleted_by_user INTEGER NOT NULL DEFAULT 0,is_guest_owned INTEGER NOT NULL DEFAULT 0,is_saved INTEGER NOT NULL DEFAULT 0,folder_id TEXT,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,title TEXT NOT NULL,type TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_accessed_at TEXT,size_bytes INTEGER NOT NULL DEFAULT 0,file_path TEXT NOT NULL,is_starred INTEGER NOT NULL DEFAULT 0,is_deleted INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,deleted_by_user INTEGER NOT NULL DEFAULT 0,is_guest_owned INTEGER NOT NULL DEFAULT 0,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS rate_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,event_type TEXT NOT NULL,bytes INTEGER NOT NULL DEFAULT 0,created_ts INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS ix_rate_events_user_type_ts ON rate_events(user_id,event_type,created_ts);
CREATE TABLE IF NOT EXISTS ip_rate_events(id INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT NOT NULL,event_type TEXT NOT NULL,created_ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ip_rate_events_key_type_ts ON ip_rate_events(key,event_type,created_ts);
CREATE TABLE IF NOT EXISTS security_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,username TEXT,ip TEXT,endpoint TEXT,event_type TEXT NOT NULL,severity TEXT NOT NULL,detail TEXT,observed REAL,threshold REAL,action TEXT,created_at TEXT NOT NULL,created_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cleanup_events(id INTEGER PRIMARY KEY AUTOINCREMENT,reason TEXT NOT NULL,level TEXT NOT NULL,bytes_reclaimed INTEGER NOT NULL,items_deleted INTEGER NOT NULL,used_percent REAL NOT NULL,free_bytes INTEGER NOT NULL,details TEXT,created_at TEXT NOT NULL);
        """
    )
    ensure_column(c, "users", "last_login_at", "last_login_at TEXT")
    ensure_column(c, "users", "last_active_at", "last_active_at TEXT")
    ensure_column(c, "users", "storage_bytes_used", "storage_bytes_used INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "users", "is_deleted", "is_deleted INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "users", "deleted_at", "deleted_at TEXT")
    ensure_column(c, "users", "locked_until", "locked_until TEXT")
    ensure_column(c, "users", "lock_reason", "lock_reason TEXT")
    ensure_column(c, "users", "preferred_language", "preferred_language TEXT NOT NULL DEFAULT 'es'")
    ensure_column(c, "users", "preferred_theme", "preferred_theme TEXT NOT NULL DEFAULT 'light'")

    ensure_column(c, "chats", "last_accessed_at", "last_accessed_at TEXT")
    ensure_column(c, "chats", "token_count_estimate", "token_count_estimate INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "size_bytes", "size_bytes INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "is_deleted", "is_deleted INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "deleted_at", "deleted_at TEXT")
    ensure_column(c, "chats", "deleted_by_user", "deleted_by_user INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "is_guest_owned", "is_guest_owned INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "is_saved", "is_saved INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "folder_id", "folder_id TEXT")

    ensure_column(c, "documents", "last_accessed_at", "last_accessed_at TEXT")
    ensure_column(c, "documents", "size_bytes", "size_bytes INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "documents", "is_starred", "is_starred INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "documents", "is_deleted", "is_deleted INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "documents", "deleted_at", "deleted_at TEXT")
    ensure_column(c, "documents", "deleted_by_user", "deleted_by_user INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "documents", "is_guest_owned", "is_guest_owned INTEGER NOT NULL DEFAULT 0")


def _migration_002_user_and_chat_lifecycle(c: sqlite3.Connection) -> None:
    ensure_column(c, "users", "preferred_language", "preferred_language TEXT NOT NULL DEFAULT 'es'")
    ensure_column(c, "users", "preferred_theme", "preferred_theme TEXT NOT NULL DEFAULT 'light'")
    ensure_column(c, "users", "guest_logout_at", "guest_logout_at TEXT")
    c.execute("UPDATE users SET preferred_language='es' WHERE preferred_language IS NULL OR TRIM(preferred_language)='' OR LOWER(TRIM(preferred_language)) NOT IN ('en','es')")
    c.execute("UPDATE users SET preferred_theme='light' WHERE preferred_theme IS NULL OR TRIM(preferred_theme)='' OR LOWER(TRIM(preferred_theme)) NOT IN ('light','dark')")

    c.execute(
        "CREATE TABLE IF NOT EXISTS chat_folders(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,name_norm TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"
    )
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_folders_user_name_norm ON chat_folders(user_id,name_norm)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_chat_folders_user_updated ON chat_folders(user_id,updated_at)")

    ensure_column(c, "chats", "is_saved", "is_saved INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "folder_id", "folder_id TEXT")
    ensure_column(c, "chats", "deleted_by_user", "deleted_by_user INTEGER NOT NULL DEFAULT 0")
    ensure_column(c, "chats", "is_guest_owned", "is_guest_owned INTEGER NOT NULL DEFAULT 0")
    c.execute("UPDATE chats SET is_saved=0 WHERE is_saved IS NULL")
    c.execute("UPDATE chats SET folder_id=NULL WHERE folder_id IS NOT NULL AND TRIM(folder_id)='' ")
    c.execute("UPDATE chats SET folder_id=NULL WHERE folder_id IS NOT NULL AND folder_id NOT IN (SELECT id FROM chat_folders)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_chats_user_deleted_updated ON chats(user_id,is_deleted,updated_at)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_chats_user_saved_deleted ON chats(user_id,is_saved,is_deleted)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_chats_user_folder_deleted ON chats(user_id,folder_id,is_deleted)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_chats_deleted_deleted_at ON chats(is_deleted,deleted_at)")

    c.execute("CREATE TABLE IF NOT EXISTS user_restrictions(user_id TEXT PRIMARY KEY,docs_write_blocked_until TEXT,docs_block_reason TEXT,ai_prompt_cooldown_until TEXT,ai_send_blocked_until TEXT,manual_locked_until TEXT,manual_lock_reason TEXT,manual_locked_by TEXT,manual_lock_permanent INTEGER NOT NULL DEFAULT 0,updated_at TEXT,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)")


def _migration_003_analytics(c: sqlite3.Connection) -> None:
    c.executescript(
        """
CREATE TABLE IF NOT EXISTS usage_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL,surface TEXT NOT NULL,day_bucket TEXT NOT NULL,created_at TEXT NOT NULL,created_ts INTEGER NOT NULL,user_id TEXT,username TEXT,user_role TEXT,preferred_language TEXT,session_id TEXT,value INTEGER NOT NULL DEFAULT 1,metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_usage_events_day_event_surface ON usage_events(day_bucket,event_type,surface);
CREATE INDEX IF NOT EXISTS ix_usage_events_user_day ON usage_events(user_id,day_bucket);
CREATE TABLE IF NOT EXISTS analytics_daily_rollups(day_bucket TEXT NOT NULL,metric_key TEXT NOT NULL,surface TEXT NOT NULL DEFAULT '',user_role TEXT NOT NULL DEFAULT '',preferred_language TEXT NOT NULL DEFAULT '',value INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(day_bucket,metric_key,surface,user_role,preferred_language));
CREATE TABLE IF NOT EXISTS analytics_daily_active_users(day_bucket TEXT NOT NULL,user_id TEXT NOT NULL,user_role TEXT NOT NULL DEFAULT '',preferred_language TEXT NOT NULL DEFAULT '',PRIMARY KEY(day_bucket,user_id));
CREATE INDEX IF NOT EXISTS ix_analytics_active_users_day ON analytics_daily_active_users(day_bucket,user_role,preferred_language);
CREATE TABLE IF NOT EXISTS analytics_exports(id TEXT PRIMARY KEY,format TEXT NOT NULL,date_from TEXT NOT NULL,date_to TEXT NOT NULL,created_at TEXT NOT NULL,created_by_user_id TEXT,status TEXT NOT NULL,file_path TEXT,metadata_json TEXT NOT NULL DEFAULT '{}');
        """
    )


def _migration_004_active_generations(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS active_generations(
            request_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            started_at TEXT NOT NULL
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS ix_active_gen_user ON active_generations(user_id)")


def _migration_005_time_based_indexes(c: sqlite3.Connection) -> None:
    """Speed up the cleanup pass and session lookups.

    These indexes turn full-table scans on `created_ts` / `created_at` /
    `token_hash` into bounded range/point lookups. The cleanup loop runs
    inside the same DB, so adding these prevents it from holding write
    locks for seconds at a time on long-lived installs.
    """
    c.execute("CREATE INDEX IF NOT EXISTS ix_rate_events_created_ts ON rate_events(created_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ip_rate_events_created_ts ON ip_rate_events(created_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_security_events_created_at ON security_events(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_security_events_created_ts ON security_events(created_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_usage_events_created_ts ON usage_events(created_ts)")
    # sessions.token_hash is already declared UNIQUE in migration 1 (an implicit
    # index), and a dedicated covering index exists, but installs that ran older
    # ad-hoc DDL may have missed it — guard with IF NOT EXISTS.
    c.execute("CREATE INDEX IF NOT EXISTS ix_sessions_token_hash ON sessions(token_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sessions_expires_revoked ON sessions(expires_at, revoked_at)")


MIGRATIONS = (
    Migration(1, "core_schema", _migration_001_core_schema),
    Migration(2, "user_and_chat_lifecycle", _migration_002_user_and_chat_lifecycle),
    Migration(3, "analytics", _migration_003_analytics),
    Migration(4, "active_generations", _migration_004_active_generations),
    Migration(5, "time_based_indexes", _migration_005_time_based_indexes),
)


def run_migrations(c: sqlite3.Connection, now_iso: str) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations(
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {int(row["version"] if isinstance(row, sqlite3.Row) else row[0]) for row in c.execute("SELECT version FROM schema_migrations").fetchall()}
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        migration.apply(c)
        c.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (migration.version, migration.name, now_iso),
        )
