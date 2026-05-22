"""Growth suite checks (2.x): disk growth, SQLite bloat, ChromaDB size, logs, backups."""
from __future__ import annotations

import json as _json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


# Docker named volumes whose host path lives in WSL2 and is not walkable from
# the Windows host. Measured via `docker exec` against the already-running
# container that mounts them — no extra image pull required.
DOCKER_VOLUMES = [
    {
        "volume": "chroma_db_es_native",
        "container": "aibox-ai-control",
        "container_path": "/chroma_db_es",
        "label": "docker:chroma_db_es_native",
    },
]


def _measure_docker_volume(spec: dict) -> tuple[int | None, int | None, str]:
    """Return (bytes, files, note). bytes/files None when measurement failed."""
    docker = shutil.which("docker")
    if not docker:
        return None, None, "docker CLI not on PATH"
    exec_cmd = [
        docker, "exec", spec["container"], "sh", "-c",
        f"du -sb {spec['container_path']} && "
        f"find {spec['container_path']} -type f | wc -l",
    ]
    try:
        out = subprocess.check_output(
            exec_cmd, stderr=subprocess.STDOUT, timeout=20,
        ).decode().strip().splitlines()
        if len(out) >= 2:
            size_b = int(out[0].split()[0])
            files = int(out[1].strip())
            return size_b, files, f"docker exec {spec['container']}"
        note_fallback = "exec returned unexpected output; trying volume mount"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        note_fallback = f"exec failed ({type(exc).__name__}); trying volume mount"
    except (ValueError, IndexError) as exc:
        note_fallback = f"exec parse failed ({type(exc).__name__}); trying volume mount"
    except OSError as exc:
        return None, None, f"docker exec OSError: {exc}"

    run_cmd = [
        docker, "run", "--rm",
        "-v", f"{spec['volume']}:/v:ro",
        "alpine:latest",
        "sh", "-c", "du -sb /v && find /v -type f | wc -l",
    ]
    try:
        out = subprocess.check_output(
            run_cmd, stderr=subprocess.STDOUT, timeout=60,
        ).decode().strip().splitlines()
        if len(out) >= 2:
            size_b = int(out[0].split()[0])
            files = int(out[1].strip())
            return size_b, files, f"docker run alpine (fallback after: {note_fallback})"
        return None, None, f"fallback produced unexpected output ({note_fallback})"
    except subprocess.TimeoutExpired:
        return None, None, f"fallback timed out ({note_fallback})"
    except subprocess.CalledProcessError as exc:
        return None, None, (
            f"fallback failed ({note_fallback}); "
            f"docker run rc={exc.returncode}"
        )
    except OSError as exc:
        return None, None, f"fallback OSError: {exc} ({note_fallback})"


# 2.1 — Disk growth sampler + projection --------------------------------------

WATCHED_RELATIVE = [
    "aibox/backend-data",
    "aibox/kolibri-data",
    "aibox/kiwix",
    "aibox/models",
    "aibox/logs",
    "aibox/runtime",
]


def _dir_size(path: Path) -> tuple[int, int]:
    total = 0
    files = 0
    for root, _dirs, fnames in os.walk(path, followlinks=False):
        for f in fnames:
            try:
                total += os.path.getsize(os.path.join(root, f))
                files += 1
            except OSError:
                continue
    return total, files


@register(
    suite="growth", id="2.1", name="disk_growth_sampler",
    status="real",
    description="Walks each watched path, records total bytes + file count. "
                "Projection lives in 2.1.b (runs across history).",
)
class DiskGrowthSampler(Check):
    def run(self, ctx) -> CheckResult:
        totals = []
        for rel in WATCHED_RELATIVE:
            p = ctx.repo_root / rel
            if not p.exists():
                ctx.metric("missing_path", rel)
                continue
            size, files = _dir_size(p)
            ctx.metric("bytes", size, unit="B", path=rel)
            ctx.metric("files", files, path=rel)
            totals.append((rel, size, files))
        for chroma in (ctx.repo_root / "aibox").glob("chroma_db*"):
            if chroma.is_dir():
                size, files = _dir_size(chroma)
                rel = chroma.relative_to(ctx.repo_root).as_posix()
                ctx.metric("bytes", size, unit="B", path=rel)
                ctx.metric("files", files, path=rel)
                totals.append((rel, size, files))
        # Docker named volumes (host path lives inside WSL2, not on the
        # Windows host filesystem). Measured via `docker exec` against the
        # running container that already mounts them.
        for spec in DOCKER_VOLUMES:
            size_b, files_n, note = _measure_docker_volume(spec)
            ctx.metric("docker_volume_status", note, path=spec["label"])
            if size_b is None:
                continue
            ctx.metric("bytes", size_b, unit="B", path=spec["label"])
            ctx.metric("files", files_n, path=spec["label"])
            totals.append((spec["label"], size_b, files_n))
        if not totals:
            return CheckResult(outcome="fail", summary="no watched paths found")
        biggest = max(totals, key=lambda t: t[1])
        return CheckResult(
            outcome="ok",
            summary=f"{len(totals)} paths sampled; largest={biggest[0]} "
                    f"({biggest[1] / 1e9:.2f}GB)",
        )


@register(
    suite="growth", id="2.1.b", name="disk_growth_projection",
    status="real",
    description="Fits a linear trend across historical samples in results.db and "
                "projects days-until-90%-full per watched path.",
)
class DiskGrowthProjection(Check):
    def run(self, ctx) -> CheckResult:
        store = ctx.store
        with store.transaction() as conn:
            rows = conn.execute(
                "SELECT tags, recorded_at, value FROM metrics"
                " WHERE check_id='2.1' AND name='bytes' AND value IS NOT NULL"
                " ORDER BY recorded_at ASC"
            ).fetchall()
        series: dict[str, list[tuple[float, float]]] = {}
        for tags, ts, val in rows:
            try:
                path = _json.loads(tags).get("path", "?")
            except Exception:  # noqa: BLE001
                path = "?"
            series.setdefault(path, []).append((ts, val))
        import psutil
        root_free = psutil.disk_usage(str(ctx.repo_root)).free
        worst_days = None
        worst_path = None
        for path, samples in series.items():
            if len(samples) < 2:
                ctx.metric("insufficient_samples", True, path=path)
                continue
            (t0, v0), (t1, v1) = samples[0], samples[-1]
            dt_days = max((t1 - t0) / 86400.0, 1 / 1440)
            slope = (v1 - v0) / dt_days
            ctx.metric("growth_bytes_per_day", slope, unit="B/day", path=path)
            if slope <= 0:
                continue
            budget = root_free * 0.9
            days = budget / slope
            ctx.metric("days_until_90pct_full", days, unit="days", path=path)
            if worst_days is None or days < worst_days:
                worst_days = days
                worst_path = path
        if worst_days is None:
            return CheckResult(
                outcome="ok",
                summary="not enough historical samples yet to project (need ≥2 runs of 2.1)",
            )
        outcome = "ok"
        if worst_days < 90:
            outcome = "fail"
        elif worst_days < 180:
            outcome = "fail"
        return CheckResult(
            outcome=outcome,
            summary=f"worst projection: {worst_path} fills 90% in {worst_days:.0f} days",
        )


# 2.2 — SQLite bloat ----------------------------------------------------------

# Runtime path contract: docker-compose mounts ../backend-data/appdata -> /data
# and ai-control defaults to APP_DB_PATH=/data/db/app.db (see
# tools/ai-control/app_storage.py). The check runs on the host, so we look at
# the host equivalent. Override with AIBOX_HOST_APP_DB_PATH for non-default
# deployments.
DEFAULT_APP_DB_RELPATH = "aibox/backend-data/appdata/db/app.db"
BLOAT_FAIL_PCT = 50.0
BLOAT_WARN_PCT = 25.0
FULL_COUNTS_ENV = "AIBOX_CHECK_FULL_COUNTS"


def _resolve_app_db_path(repo_root: Path) -> Path:
    override = os.getenv("AIBOX_HOST_APP_DB_PATH")
    if override:
        return Path(override)
    return repo_root / DEFAULT_APP_DB_RELPATH


@register(
    suite="growth", id="2.2", name="sqlite_bloat",
    status="real",
    description="Reports total size, WAL size, page count, freelist, per-table row estimates. "
                "Set AIBOX_CHECK_FULL_COUNTS=1 for exact COUNT(*) (slow on large tables).",
)
class SqliteBloat(Check):
    def run(self, ctx) -> CheckResult:
        db_path = _resolve_app_db_path(ctx.repo_root)
        warn: list[str] = []
        fail: list[str] = []
        deep = os.getenv(FULL_COUNTS_ENV) == "1"

        if not db_path.exists():
            ctx.metric("db_present", False, db=str(db_path))
            return CheckResult(
                outcome="fail",
                summary=f"canonical app DB missing: {db_path} "
                        "(stack not initialized, or APP_DB_PATH drift)",
            )
        ctx.metric("db_present", True, db=str(db_path))
        size = db_path.stat().st_size
        ctx.metric("db_bytes", size, unit="B", db=str(db_path))
        wal = db_path.with_suffix(db_path.suffix + "-wal")
        wal_size = wal.stat().st_size if wal.exists() else 0
        ctx.metric("wal_bytes", wal_size, unit="B", db=str(db_path))

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.DatabaseError as exc:
            ctx.metric("open_error", str(exc), db=str(db_path))
            return CheckResult(outcome="fail", summary=f"could not open {db_path}: {exc}")

        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # Belt-and-braces: even with mode=ro, force the connection to
            # refuse any write so a stray PRAGMA/temp-store side-effect can't
            # touch a live deployment.
            cur.execute("PRAGMA query_only = 1")
            page_count = cur.execute("PRAGMA page_count").fetchone()[0]
            page_size = cur.execute("PRAGMA page_size").fetchone()[0]
            freelist = cur.execute("PRAGMA freelist_count").fetchone()[0]
            ctx.metric("page_count", page_count, db=str(db_path))
            ctx.metric("page_size", page_size, db=str(db_path), unit="B")
            ctx.metric("freelist_count", freelist, db=str(db_path))
            bloat_pct = (freelist / page_count) * 100 if page_count else 0.0
            ctx.metric("bloat_pct", bloat_pct, unit="%", db=str(db_path))
            if bloat_pct >= BLOAT_FAIL_PCT:
                fail.append(f"bloat={bloat_pct:.0f}% — VACUUM required")
            elif bloat_pct >= BLOAT_WARN_PCT:
                warn.append(f"bloat={bloat_pct:.0f}% — consider VACUUM")

            tables = cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            ctx.metric("table_count", len(tables), db=str(db_path))
            deadline = time.monotonic() + 10.0
            for (name,) in tables:
                if time.monotonic() > deadline:
                    ctx.metric("row_scan_deadline_hit", True, db=str(db_path))
                    warn.append("per-table scan budget exhausted; some tables not counted")
                    break
                try:
                    if deep:
                        rows = cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                        ctx.metric("rows_exact", rows, db=str(db_path), table=name)
                    else:
                        # MAX(rowid) is O(1) on rowid tables and bounds the
                        # row count from above. Good enough for growth
                        # trending without touching every page.
                        est = cur.execute(
                            f'SELECT COALESCE(MAX(rowid), 0) FROM "{name}"'
                        ).fetchone()[0]
                        ctx.metric("rows_estimate", est, db=str(db_path), table=name)
                except sqlite3.DatabaseError as exc:
                    ctx.metric("row_scan_error", str(exc),
                               db=str(db_path), table=name)
                    continue
        except sqlite3.DatabaseError as exc:
            ctx.metric("read_error", str(exc), db=str(db_path))
            fail.append(f"read error: {exc}")
        finally:
            conn.close()

        bits = [f"db={db_path.name} {size / 1e6:.1f}MB"]
        if not deep:
            bits.append(
                "row estimates via MAX(rowid); "
                f"set {FULL_COUNTS_ENV}=1 for exact counts"
            )
        if warn:
            bits.append("WARN: " + "; ".join(warn))
        if fail:
            bits.append("FAIL: " + "; ".join(fail))
        outcome = "fail" if fail else "ok"
        return CheckResult(outcome=outcome, summary=" · ".join(bits))


# 2.3 — ChromaDB growth -------------------------------------------------------

CHROMA_CANDIDATE_DIRS = ("chroma_db", "chroma_db_es", "chroma_db_en")


@register(
    suite="growth", id="2.3", name="chromadb_growth",
    status="real",
    description="On-disk size + collection counts for each chroma_db_* directory.",
    requires=("module:chromadb",),
)
class ChromaDbGrowth(Check):
    def run(self, ctx) -> CheckResult:
        import chromadb
        found = 0
        for name in CHROMA_CANDIDATE_DIRS:
            p = ctx.repo_root / "aibox" / name
            if not p.exists():
                p2 = ctx.repo_root / name
                p = p2 if p2.exists() else p
            if not p.exists():
                continue
            found += 1
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            ctx.metric("on_disk_bytes", size, unit="B", path=name)
            try:
                client = chromadb.PersistentClient(path=str(p))
                cols = client.list_collections()
                ctx.metric("collection_count", len(cols), path=name)
                for c in cols:
                    try:
                        cnt = c.count()
                    except Exception:  # noqa: BLE001
                        cnt = -1
                    ctx.metric("vector_count", cnt, path=name, collection=c.name)
            except Exception as exc:  # noqa: BLE001
                ctx.metric("chroma_open_error", str(exc), path=name)
        if found == 0:
            return CheckResult(outcome="skipped", summary="no chroma_db_* directories found")
        return CheckResult(outcome="ok", summary=f"{found} chroma_db directories scanned")


# 2.4 — Log rotation ----------------------------------------------------------

LOG_HOTSPOTS = [
    "aibox/logs",
    "aibox/backend-data/logs",
    "aibox/stack/logs",
]


@register(
    suite="growth", id="2.4", name="log_rotation",
    status="real",
    description="Walks known log locations; reports total bytes and biggest files. "
                "Caller should inspect the Docker compose logging driver too.",
)
class LogRotation(Check):
    LIMIT_BYTES = 2 * 1024**3

    def run(self, ctx) -> CheckResult:
        biggest = []
        total = 0
        for rel in LOG_HOTSPOTS:
            p = ctx.repo_root / rel
            if not p.exists():
                continue
            for root, _dirs, files in os.walk(p):
                for f in files:
                    fp = Path(root) / f
                    try:
                        sz = fp.stat().st_size
                    except OSError:
                        continue
                    total += sz
                    biggest.append((sz, fp.relative_to(ctx.repo_root).as_posix()))
        biggest.sort(reverse=True)
        ctx.metric("total_bytes", total, unit="B")
        ctx.metric("file_count", len(biggest))
        for sz, name in biggest[:10]:
            ctx.metric("top_file_bytes", sz, unit="B", path=name)
        outcome = "ok"
        warn = []
        if total > self.LIMIT_BYTES:
            outcome = "fail"
            warn.append(f"log dirs total {total/1e9:.1f}GB > 2GB cap")
        for sz, name in biggest[:3]:
            if sz > 500 * 1024**2:
                warn.append(f"{name} = {sz/1e6:.0f}MB (rotation suspect)")
        summary = f"{len(biggest)} files / {total/1e6:.1f} MB total"
        if warn:
            summary += "  WARN: " + "; ".join(warn)
        return CheckResult(outcome=outcome, summary=summary)


# 2.5 — Backup roundtrip (stub) ----------------------------------------------

@register(
    suite="growth", id="2.5", name="backup_roundtrip",
    status="stub",
    description="Snapshot backend-data, write a backup archive, restore into a "
                "scratch dir, diff. Stub until a documented backup tool exists.",
)
class BackupRoundtrip(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("required_inputs", "backup_script_path, restore_script_path")
        return CheckResult(
            outcome="stub",
            summary="needs documented backup+restore scripts before it can run",
        )
