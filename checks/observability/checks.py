"""Observability suite checks (11.x): log inventory, local dashboard, alerting."""
from __future__ import annotations

import json
import os
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


# 11.1 — Structured log inventory --------------------------------------------

LOG_ROOTS = [
    "aibox/logs",
    "aibox/backend-data/logs",
    "aibox/stack/logs",
]


@register(
    suite="observability", id="11.1", name="structured_log_inventory",
    status="real",
    description="Walks log dirs, reports counts + sizes, and samples a few lines from each "
                "to verify they're JSON (structured) where expected.",
)
class StructuredLogInventory(Check):
    def run(self, ctx) -> CheckResult:
        seen = 0
        json_seen = 0
        per_file = []
        for rel in LOG_ROOTS:
            base = ctx.repo_root / rel
            if not base.exists():
                continue
            for root, _dirs, files in os.walk(base):
                for f in files:
                    fp = Path(root) / f
                    try:
                        size = fp.stat().st_size
                    except OSError:
                        continue
                    seen += 1
                    sample = self._tail(fp, 5)
                    is_json = any(self._looks_json(line) for line in sample)
                    if is_json:
                        json_seen += 1
                    per_file.append((str(fp.relative_to(ctx.repo_root)), size, is_json))
        ctx.metric("log_files_total", seen)
        ctx.metric("log_files_json", json_seen)
        for path, size, is_json in per_file[:25]:
            ctx.metric("log_size_b", size, unit="B", path=path, json=is_json)
        if seen == 0:
            return CheckResult(outcome="skipped", summary="no log files found")
        return CheckResult(
            outcome="ok",
            summary=f"{seen} log files; {json_seen} look JSON-structured",
        )

    @staticmethod
    def _tail(p: Path, n: int) -> list[str]:
        try:
            with p.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                data = f.read()
            lines = data.decode("utf-8", errors="ignore").splitlines()
            return lines[-n:]
        except OSError:
            return []

    @staticmethod
    def _looks_json(line: str) -> bool:
        s = line.strip()
        if not s.startswith("{") or not s.endswith("}"):
            return False
        try:
            json.loads(s)
            return True
        except json.JSONDecodeError:
            return False


# 11.2 — Local dashboard (stub) ----------------------------------------------

@register(
    suite="observability", id="11.2", name="local_dashboard",
    status="stub",
    description="One localhost page showing stack health, telemetry, recent error rate, "
                "latest check-suite results. Reuse Caddy + a small read-only endpoint.",
)
class LocalDashboard(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="build a /checks/dashboard route in ai-control OR a separate static "
                    "HTML page that fetches results.db via a small JSON endpoint",
        )


# 11.3 — Alerting (stub) -----------------------------------------------------

@register(
    suite="observability", id="11.3", name="alerting",
    status="stub",
    description="On failure: write a clear message to dashboard + append to daily summary. "
                "Optional email when internet is available.",
)
class Alerting(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="define severity levels + dedup window + daily roll-up file in logs/checks/",
        )
