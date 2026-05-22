"""Recovery suite checks (9.x): container crash, disk full, OOM, power loss, rollback. All stubs."""
from __future__ import annotations

from aibox.checks.harness.base import Check, CheckResult, register


# 9.1 — Container crash recovery ---------------------------------------------

@register(
    suite="recovery", id="9.1", name="container_crash_recovery",
    status="stub",
    description="`docker kill --signal=KILL` each container in turn; measure time-to-healthy "
                "and verify zero data loss. Destructive — needs maintenance window.",
    destructive=True,
)
class ContainerCrashRecovery(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="iterate compose services; for each: kill, poll health, time it, check integrity",
        )


# 9.2 — Disk-full simulation -------------------------------------------------

@register(
    suite="recovery", id="9.2", name="disk_full_simulation",
    status="stub",
    description="Allocate a sparse-but-fully-written file until <100MB free in a snapshot "
                "of backend-data, then run smoke. Stub: needs ironclad snapshot teardown.",
    destructive=True,
)
class DiskFullSimulation(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="needs guaranteed cleanup of the filler file even on Ctrl-C",
        )


# 9.3 — OOM recovery ---------------------------------------------------------

@register(
    suite="recovery", id="9.3", name="oom_recovery",
    status="stub",
    description="Memory-pressure ai-control until OOMKilled (esp. Spanish HNSW path), "
                "confirm restart + chat-history integrity. Stub: requires cgroup overrides.",
    destructive=True,
)
class OomRecovery(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="needs a temporary docker compose override with low mem_limit",
        )


# 9.4 — Power-loss simulation ------------------------------------------------

@register(
    suite="recovery", id="9.4", name="power_loss_simulation",
    status="stub",
    description="Stop the WSL2 VM with TurnOff while writes in flight; on boot run integrity. "
                "Stub: requires Hyper-V control and a manual confirmation gate.",
    destructive=True,
)
class PowerLossSimulation(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="manual procedure: kick off a chat, then `wsl --shutdown` mid-stream, "
                    "boot, then run 3.1 + 3.3",
        )


# 9.5 — Rollback drill -------------------------------------------------------

@register(
    suite="recovery", id="9.5", name="rollback_drill",
    status="stub",
    description="Roll back compose to previous pinned digests + previous schema; "
                "confirm storage.db reads cleanly. Stub: needs release manifest history.",
)
class RollbackDrill(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="needs an aibox/checks/baselines/releases/<sha>.yaml history "
                    "to roll back against",
        )
