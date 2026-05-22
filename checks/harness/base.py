"""Check base class and registry."""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

REGISTRY: dict[str, type["Check"]] = {}


@dataclass
class CheckResult:
    outcome: str = "ok"
    summary: str = ""
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class Check:
    SUITE: str = ""
    CHECK_ID: str = ""
    NAME: str = ""
    STATUS: str = "real"  # "real" or "stub"
    DESCRIPTION: str = ""
    SAFE_IN_LIVE_STACK: bool = True
    DESTRUCTIVE: bool = False
    REQUIRES: tuple[str, ...] = ()  # external CLIs / files that must exist

    def run(self, ctx) -> CheckResult:
        raise NotImplementedError

    def execute(self, ctx) -> CheckResult:
        if self.DESTRUCTIVE and not ctx.i_mean_it:
            return CheckResult(
                outcome="skipped",
                summary=f"destructive check '{self.CHECK_ID}' requires --i-mean-it",
            )
        if ctx.dry_run:
            return CheckResult(
                outcome="dry-run",
                summary=f"[{self.STATUS}] would run {self.CHECK_ID} {self.NAME}",
            )
        missing = self._missing_requirements()
        if missing:
            return CheckResult(
                outcome="skipped",
                summary=f"missing requirements: {', '.join(missing)}",
            )
        if self.STATUS == "stub":
            res = self.run(ctx)
            if not res.summary:
                res.summary = f"STUB: {self.CHECK_ID} {self.NAME} not yet implemented"
            if res.outcome == "ok":
                res.outcome = "stub"
            return res
        try:
            return self.run(ctx)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                outcome="error",
                summary=f"unhandled exception in {self.CHECK_ID}",
                error=f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc()}",
            )

    def _missing_requirements(self) -> list[str]:
        import shutil
        missing: list[str] = []
        for req in self.REQUIRES:
            if req.startswith("cmd:"):
                if shutil.which(req[4:]) is None:
                    missing.append(req)
            elif req.startswith("module:"):
                import importlib
                try:
                    importlib.import_module(req[7:])
                except Exception:  # noqa: BLE001
                    missing.append(req)
        return missing


def register(*, suite: str, id: str, name: str, status: str = "real",
             description: str = "", safe: bool = True, destructive: bool = False,
             requires: tuple[str, ...] = ()) -> Callable[[type[Check]], type[Check]]:
    def deco(cls: type[Check]) -> type[Check]:
        cls.SUITE = suite
        cls.CHECK_ID = id
        cls.NAME = name
        cls.STATUS = status
        cls.DESCRIPTION = description
        cls.SAFE_IN_LIVE_STACK = safe
        cls.DESTRUCTIVE = destructive
        cls.REQUIRES = requires
        if id in REGISTRY:
            raise ValueError(f"duplicate check id: {id}")
        REGISTRY[id] = cls
        return cls
    return deco
