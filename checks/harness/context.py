"""Per-run context. Carries metadata, the result store, and helpers."""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import ResultStore


def _git_sha(repo: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        return out.decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _env_hash(env_path: Path) -> str:
    if not env_path.exists():
        return "no-env"
    h = hashlib.sha256(env_path.read_bytes()).hexdigest()
    return h[:16]


def _gpu_info() -> str:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return "no-nvidia-smi"
    try:
        out = subprocess.check_output(
            [nvidia_smi, "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            timeout=10,
        )
        return out.decode().strip().splitlines()[0]
    except Exception as exc:  # noqa: BLE001
        return f"nvidia-smi-error: {exc}"


@dataclass
class RunContext:
    run_id: int
    store: ResultStore
    repo_root: Path
    invocation: str
    dry_run: bool = False
    quick: bool = False
    full: bool = False
    soak: bool = False
    i_mean_it: bool = False
    unattended: bool = False
    # current check context (filled in by runner)
    current_check_db_id: int | None = None
    current_suite: str = ""
    current_check_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def begin(cls, *, repo_root: Path, store: ResultStore, invocation: str,
              dry_run: bool = False, quick: bool = False, full: bool = False,
              soak: bool = False, i_mean_it: bool = False,
              unattended: bool = False, notes: str = "") -> "RunContext":
        env_path = repo_root / "aibox" / "stack" / ".env"
        run_id = store.start_run(
            git_sha=_git_sha(repo_root),
            host=socket.gethostname(),
            env_hash=_env_hash(env_path),
            gpu_info=_gpu_info(),
            invocation=invocation,
            notes=notes,
        )
        return cls(run_id=run_id, store=store, repo_root=repo_root,
                   invocation=invocation, dry_run=dry_run, quick=quick,
                   full=full, soak=soak, i_mean_it=i_mean_it,
                   unattended=unattended)

    def metric(self, name: str, value: Any, unit: str = "", **tags) -> None:
        self.store.record_metric(
            run_id=self.run_id, check_db_id=self.current_check_db_id,
            suite=self.current_suite, check_id=self.current_check_id,
            name=name, value=value, unit=unit, tags=tags,
        )

    def artifact(self, path: str | Path, kind: str = "file") -> None:
        path = Path(path)
        sha = ""
        if path.exists() and path.is_file():
            try:
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:  # noqa: BLE001
                sha = ""
        self.store.record_artifact(
            run_id=self.run_id, check_db_id=self.current_check_db_id,
            path=str(path), kind=kind, sha256=sha,
        )

    def scratch_dir(self) -> Path:
        d = self.repo_root / "aibox" / "checks" / "scratch" / f"run_{self.run_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def snapshot(self, src: str | Path) -> Path:
        """Hardlink-copy a directory or file into scratch. Returns the copy path."""
        src = Path(src)
        if not src.exists():
            raise FileNotFoundError(src)
        dst = self.scratch_dir() / src.name
        if src.is_file():
            try:
                os.link(src, dst)
            except (OSError, NotImplementedError):
                shutil.copy2(src, dst)
        else:
            shutil.copytree(src, dst, copy_function=_link_or_copy, dirs_exist_ok=True)
        return dst

    def host_summary(self) -> dict:
        return {
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        }

    def end(self) -> None:
        self.store.end_run(self.run_id)


def _link_or_copy(src, dst, *, follow_symlinks=True):  # signature matches shutil.copytree contract
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError):
        shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
