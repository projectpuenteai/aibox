"""SQLite-backed result store. One DB for all check runs and metrics."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    git_sha TEXT,
    host TEXT,
    env_hash TEXT,
    gpu_info TEXT,
    notes TEXT,
    invocation TEXT
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    suite TEXT NOT NULL,
    check_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    outcome TEXT,
    error TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_checks_run ON checks(run_id);
CREATE INDEX IF NOT EXISTS idx_checks_id ON checks(check_id);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    check_db_id INTEGER,
    suite TEXT NOT NULL,
    check_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    unit TEXT,
    tags TEXT,
    recorded_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (check_db_id) REFERENCES checks(id)
);

CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(check_id, name);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    check_db_id INTEGER,
    path TEXT NOT NULL,
    kind TEXT,
    sha256 TEXT,
    recorded_at REAL NOT NULL
);
"""


class ResultStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_run(self, *, git_sha: str, host: str, env_hash: str,
                  gpu_info: str, invocation: str, notes: str = "") -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO runs(started_at, git_sha, host, env_hash, gpu_info, notes, invocation)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), git_sha, host, env_hash, gpu_info, notes, invocation),
            )
            return cur.lastrowid

    def end_run(self, run_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE runs SET ended_at=? WHERE id=?", (time.time(), run_id))

    def start_check(self, run_id: int, suite: str, check_id: str, name: str, status: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO checks(run_id, suite, check_id, name, status, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, suite, check_id, name, status, time.time()),
            )
            return cur.lastrowid

    def end_check(self, check_db_id: int, outcome: str, error: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE checks SET ended_at=?, outcome=?, error=? WHERE id=?",
                (time.time(), outcome, error, check_db_id),
            )

    def record_metric(self, run_id: int, check_db_id: int | None, suite: str,
                      check_id: str, name: str, value: Any, unit: str = "",
                      tags: dict | None = None) -> None:
        value_num: float | None = None
        value_text: str | None = None
        if isinstance(value, (int, float, bool)):
            value_num = float(value)
        else:
            value_text = str(value)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO metrics(run_id, check_db_id, suite, check_id, name,"
                " value, value_text, unit, tags, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, check_db_id, suite, check_id, name, value_num, value_text,
                 unit, json.dumps(tags or {}), time.time()),
            )

    def record_artifact(self, run_id: int, check_db_id: int | None,
                        path: str, kind: str, sha256: str = "") -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts(run_id, check_db_id, path, kind, sha256, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, check_db_id, path, kind, sha256, time.time()),
            )

    def latest_run_id(self) -> int | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return row[0] if row else None

    def run_summary(self, run_id: int) -> dict:
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            cols = [c[0] for c in conn.execute("SELECT * FROM runs LIMIT 0").description]
            run_dict = dict(zip(cols, run)) if run else {}
            checks = conn.execute(
                "SELECT suite, check_id, name, status, outcome, error,"
                " (ended_at - started_at) AS duration"
                " FROM checks WHERE run_id=? ORDER BY suite, check_id", (run_id,)
            ).fetchall()
            check_cols = ["suite", "check_id", "name", "status", "outcome", "error", "duration"]
            metrics = conn.execute(
                "SELECT suite, check_id, name, value, value_text, unit, tags"
                " FROM metrics WHERE run_id=? ORDER BY suite, check_id, name", (run_id,)
            ).fetchall()
            metric_cols = ["suite", "check_id", "name", "value", "value_text", "unit", "tags"]
        return {
            "run": run_dict,
            "checks": [dict(zip(check_cols, c)) for c in checks],
            "metrics": [dict(zip(metric_cols, m)) for m in metrics],
        }

    def metric_history(self, check_id: str, name: str, limit: int = 50) -> list[tuple[float, float]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT recorded_at, value FROM metrics"
                " WHERE check_id=? AND name=? AND value IS NOT NULL"
                " ORDER BY recorded_at DESC LIMIT ?",
                (check_id, name, limit),
            ).fetchall()
        return list(reversed(rows))
