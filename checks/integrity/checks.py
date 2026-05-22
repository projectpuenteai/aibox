"""Integrity suite checks (3.x): SQLite integrity, encrypted roundtrip, ChromaDB, models, volumes."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


# 3.1 — SQLite integrity_check ------------------------------------------------

SQLITE_CANDIDATES = [
    "aibox/backend-data/storage.db",
    "aibox/backend-data/auth.db",
    "aibox/backend-data/chat.db",
]


@register(
    suite="integrity", id="3.1", name="sqlite_integrity",
    status="real",
    description="PRAGMA integrity_check + foreign_key_check against each known SQLite DB.",
)
class SqliteIntegrity(Check):
    def run(self, ctx) -> CheckResult:
        scanned = 0
        problems = []
        for rel in SQLITE_CANDIDATES:
            p = ctx.repo_root / rel
            if not p.exists():
                continue
            scanned += 1
            try:
                conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
                ic = conn.execute("PRAGMA integrity_check").fetchall()
                fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                conn.close()
            except sqlite3.DatabaseError as exc:
                ctx.metric("open_error", str(exc), db=rel)
                problems.append(f"{rel}: open failed: {exc}")
                continue
            ok = ic == [("ok",)] and not fk
            ctx.metric("integrity_ok", ok, db=rel)
            ctx.metric("foreign_key_violations", len(fk), db=rel)
            if not ok:
                problems.append(f"{rel}: integrity_check={ic}, fk={fk}")
        if scanned == 0:
            return CheckResult(outcome="skipped", summary="no SQLite DBs found")
        return CheckResult(
            outcome="fail" if problems else "ok",
            summary=f"{scanned} DBs checked" + (
                ("; problems: " + "; ".join(problems)) if problems else ""
            ),
        )


# 3.2 — Encrypted roundtrip (stub) -------------------------------------------

@register(
    suite="integrity", id="3.2", name="encrypted_roundtrip",
    status="stub",
    description="Decrypt N random encrypted blobs and validate plaintext shape. "
                "Needs the storage encryption key from .env to be loaded into the check.",
)
class EncryptedRoundtrip(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("required_inputs", "AIBOX_STORAGE_KEY (from .env)")
        return CheckResult(
            outcome="stub",
            summary="needs ai-control crypto helpers wired in — see app_storage.py",
        )


# 3.3 — ChromaDB consistency --------------------------------------------------

CHROMA_CANDIDATE_DIRS = ("chroma_db", "chroma_db_es", "chroma_db_en")
EXPECTED_EMBEDDING_DIM = 1024  # bge-m3 default


@register(
    suite="integrity", id="3.3", name="chromadb_consistency",
    status="real",
    description="Per chroma_db_*: collection exists, count > 0, embedding dim matches expectation, "
                "and a small sample of vectors has populated metadata.",
    requires=("module:chromadb",),
)
class ChromaDbConsistency(Check):
    def run(self, ctx) -> CheckResult:
        import chromadb
        problems = []
        found = 0
        for name in CHROMA_CANDIDATE_DIRS:
            candidates = [ctx.repo_root / "aibox" / name, ctx.repo_root / name]
            p = next((c for c in candidates if c.exists()), None)
            if not p:
                continue
            found += 1
            try:
                client = chromadb.PersistentClient(path=str(p))
                cols = client.list_collections()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{name}: open failed: {exc}")
                continue
            for c in cols:
                try:
                    cnt = c.count()
                    sample = c.peek(limit=3) if cnt else {"ids": [], "embeddings": [], "metadatas": []}
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{name}/{c.name}: peek failed: {exc}")
                    continue
                ctx.metric("vector_count", cnt, path=name, collection=c.name)
                embeddings = sample.get("embeddings") or []
                if embeddings is not None and len(embeddings) > 0 and embeddings[0] is not None:
                    dim = len(embeddings[0])
                    ctx.metric("embedding_dim", dim, path=name, collection=c.name)
                    if dim != EXPECTED_EMBEDDING_DIM:
                        problems.append(f"{name}/{c.name}: dim={dim} != expected {EXPECTED_EMBEDDING_DIM}")
                metas = sample.get("metadatas") or []
                metas_filled = sum(1 for m in metas if m)
                ctx.metric("sample_metas_filled", metas_filled, path=name, collection=c.name)
                if metas and metas_filled == 0:
                    problems.append(f"{name}/{c.name}: sampled vectors have empty metadata")
                if cnt == 0:
                    problems.append(f"{name}/{c.name}: empty collection")
        if found == 0:
            return CheckResult(outcome="skipped", summary="no chroma_db_* directories found")
        return CheckResult(
            outcome="fail" if problems else "ok",
            summary=f"{found} dirs scanned" + (
                ("; problems: " + "; ".join(problems[:3])) if problems else ""
            ),
        )


# 3.4 — Model file hashes -----------------------------------------------------

MANIFEST_PATH_REL = "aibox/checks/baselines/model_manifest.json"
MODEL_DIR_REL = "aibox/models"
LARGE_FILE_THRESHOLD = 2 * 1024**3
HASH_CHUNK = 1024 * 1024


def _full_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            block = f.read(HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _sampled_hash(p: Path, samples: int = 5) -> str:
    """Sampled hash: read 1 MB from N evenly-spaced offsets. Good enough as a canary."""
    h = hashlib.sha256()
    size = p.stat().st_size
    h.update(size.to_bytes(8, "little"))
    if samples < 2 or size < HASH_CHUNK * samples:
        return _full_hash(p)
    step = (size - HASH_CHUNK) // (samples - 1)
    with p.open("rb") as f:
        for i in range(samples):
            f.seek(i * step)
            h.update(f.read(HASH_CHUNK))
    return h.hexdigest()


@register(
    suite="integrity", id="3.4", name="model_hashes",
    status="real",
    description="Sampled SHA256 of every file in aibox/models/. On first run writes "
                "a manifest; on later runs diffs against the manifest.",
)
class ModelHashes(Check):
    def run(self, ctx) -> CheckResult:
        models_dir = ctx.repo_root / MODEL_DIR_REL
        manifest_path = ctx.repo_root / MANIFEST_PATH_REL
        if not models_dir.exists():
            return CheckResult(outcome="skipped",
                               summary=f"no models dir at {MODEL_DIR_REL}")
        observed: dict[str, dict] = {}
        for f in models_dir.rglob("*"):
            if not f.is_file():
                continue
            size = f.stat().st_size
            mode = "sampled" if size > LARGE_FILE_THRESHOLD else "full"
            digest = _sampled_hash(f) if mode == "sampled" else _full_hash(f)
            rel = f.relative_to(models_dir).as_posix()
            observed[rel] = {"size": size, "sha256_mode": mode, "sha256": digest}
        if not manifest_path.exists():
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(observed, indent=2), encoding="utf-8")
            ctx.metric("manifest_created", True)
            return CheckResult(
                outcome="ok",
                summary=f"baseline manifest written ({len(observed)} files)",
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        added, removed, changed = [], [], []
        for rel, info in observed.items():
            if rel not in manifest:
                added.append(rel)
            elif manifest[rel].get("sha256") != info["sha256"]:
                changed.append(rel)
        for rel in manifest:
            if rel not in observed:
                removed.append(rel)
        ctx.metric("added", len(added))
        ctx.metric("removed", len(removed))
        ctx.metric("changed", len(changed))
        outcome = "ok" if not changed and not removed else "fail"
        summary = f"observed {len(observed)} files; added={len(added)} removed={len(removed)} changed={len(changed)}"
        if changed:
            summary += "  CHANGED: " + ", ".join(changed[:3])
        return CheckResult(outcome=outcome, summary=summary)


# 3.5 — Docker volumes --------------------------------------------------------

@register(
    suite="integrity", id="3.5", name="docker_volumes",
    status="real",
    description="Inventory `docker volume ls` output and report against expected list.",
    requires=("cmd:docker",),
)
class DockerVolumes(Check):
    EXPECTED_PREFIXES = ("aibox", "puente", "open-webui", "kolibri", "kiwix")

    def run(self, ctx) -> CheckResult:
        docker = shutil.which("docker")
        try:
            out = subprocess.check_output(
                [docker, "volume", "ls", "--format", "{{json .}}"],
                stderr=subprocess.STDOUT, timeout=15,
            ).decode()
        except subprocess.CalledProcessError as exc:
            return CheckResult(outcome="skipped",
                               summary=f"docker not running or unreachable: {exc.output.decode()[:120]}")
        except subprocess.TimeoutExpired:
            return CheckResult(outcome="fail", summary="docker volume ls timed out")
        volumes = [json.loads(line) for line in out.splitlines() if line.strip()]
        ctx.metric("volume_count", len(volumes))
        relevant = [v for v in volumes if any(v["Name"].startswith(p) for p in self.EXPECTED_PREFIXES)]
        ctx.metric("relevant_volume_count", len(relevant))
        for v in relevant:
            ctx.metric("volume", v["Name"], driver=v.get("Driver", ""))
        return CheckResult(
            outcome="ok",
            summary=f"{len(volumes)} volumes total, {len(relevant)} project-related",
        )
