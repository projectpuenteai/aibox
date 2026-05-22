"""Hardware suite checks (6.x): GPU, CPU/RAM, disk, battery, power events."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


# 6.1 — GPU telemetry ----------------------------------------------------------

@register(
    suite="hardware", id="6.1", name="gpu_telemetry",
    status="real",
    description="One-shot nvidia-smi snapshot. Sustained sampling lives in the stress suite.",
    requires=("cmd:nvidia-smi",),
)
class GpuTelemetry(Check):
    QUERY = (
        "name,driver_version,temperature.gpu,utilization.gpu,utilization.memory,"
        "memory.total,memory.used,memory.free,power.draw,power.limit,clocks.current.graphics,"
        "clocks.current.memory,clocks_throttle_reasons.active"
    )

    def run(self, ctx) -> CheckResult:
        smi = shutil.which("nvidia-smi")
        out = subprocess.check_output(
            [smi, f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
            timeout=10,
        ).decode().strip()
        if not out:
            return CheckResult(outcome="fail", summary="nvidia-smi returned no rows")
        first = out.splitlines()[0]
        cols = [c.strip() for c in first.split(",")]
        keys = [k.strip() for k in self.QUERY.split(",")]
        data = dict(zip(keys, cols))

        def num(k):
            try:
                return float(data[k])
            except (KeyError, ValueError):
                return None

        ctx.metric("name", data.get("name", "unknown"))
        ctx.metric("driver_version", data.get("driver_version", "unknown"))
        ctx.metric("temp_c", num("temperature.gpu"), unit="C")
        ctx.metric("util_gpu_pct", num("utilization.gpu"), unit="%")
        ctx.metric("util_mem_pct", num("utilization.memory"), unit="%")
        ctx.metric("vram_total_mb", num("memory.total"), unit="MB")
        ctx.metric("vram_used_mb", num("memory.used"), unit="MB")
        ctx.metric("vram_free_mb", num("memory.free"), unit="MB")
        ctx.metric("power_draw_w", num("power.draw"), unit="W")
        ctx.metric("power_limit_w", num("power.limit"), unit="W")
        ctx.metric("clock_graphics_mhz", num("clocks.current.graphics"), unit="MHz")
        ctx.metric("clock_memory_mhz", num("clocks.current.memory"), unit="MHz")
        ctx.metric("throttle_reasons", data.get("clocks_throttle_reasons.active", ""))
        temp = num("temperature.gpu") or 0
        outcome = "ok"
        notes = []
        if temp >= 87:
            outcome = "fail"
            notes.append(f"GPU temp {temp}C ≥ 87C threshold")
        vram_free = num("memory.free") or 0
        vram_total = num("memory.total") or 1
        if vram_free / vram_total < 0.05:
            outcome = "fail"
            notes.append(f"VRAM nearly exhausted ({vram_free}MB free of {vram_total}MB)")
        summary = f"{data.get('name','?')} temp={temp}C used={num('memory.used')}/{num('memory.total')}MB"
        if notes:
            summary += "  WARN: " + "; ".join(notes)
        return CheckResult(outcome=outcome, summary=summary)


# 6.2 — CPU + RAM telemetry ----------------------------------------------------

@register(
    suite="hardware", id="6.2", name="cpu_ram_telemetry",
    status="real",
    description="psutil snapshot of CPU/RAM. WSL2 VM ceiling reported when detectable.",
    requires=("module:psutil",),
)
class CpuRamTelemetry(Check):
    def run(self, ctx) -> CheckResult:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        ctx.metric("cpu_pct", cpu, unit="%")
        ctx.metric("ram_total_mb", mem.total / 1e6, unit="MB")
        ctx.metric("ram_available_mb", mem.available / 1e6, unit="MB")
        ctx.metric("ram_used_pct", mem.percent, unit="%")
        ctx.metric("swap_total_mb", swap.total / 1e6, unit="MB")
        ctx.metric("swap_used_pct", swap.percent, unit="%")
        ctx.metric("cpu_cores", psutil.cpu_count(logical=False) or 0)
        ctx.metric("cpu_threads", psutil.cpu_count(logical=True) or 0)
        wsl_ceiling_mb = self._wsl_ceiling()
        if wsl_ceiling_mb:
            ctx.metric("wsl_vm_ceiling_mb", wsl_ceiling_mb, unit="MB")
        outcome = "ok"
        warn = []
        if mem.percent >= 92:
            outcome = "fail"
            warn.append(f"RAM at {mem.percent}%")
        if swap.percent >= 60:
            warn.append(f"swap at {swap.percent}%")
        summary = f"cpu={cpu}% ram_used={mem.percent}% ({mem.total/1e9:.1f}GB total)"
        if warn:
            summary += "  WARN: " + "; ".join(warn)
        return CheckResult(outcome=outcome, summary=summary)

    @staticmethod
    def _wsl_ceiling() -> float | None:
        try:
            out = subprocess.check_output(
                ["wsl", "--", "cat", "/proc/meminfo"],
                timeout=5, stderr=subprocess.DEVNULL,
            ).decode()
            for line in out.splitlines():
                if line.startswith("MemTotal:"):
                    return float(line.split()[1]) / 1024.0  # kB -> MB
        except Exception:  # noqa: BLE001
            return None
        return None


# 6.3 — Disk health ------------------------------------------------------------

@register(
    suite="hardware", id="6.3", name="disk_health",
    status="real",
    description="Free space per volume always; SMART data when smartctl is on PATH.",
    requires=("module:psutil",),
)
class DiskHealth(Check):
    def run(self, ctx) -> CheckResult:
        import psutil
        worst = "ok"
        warn = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:  # noqa: BLE001
                continue
            tag = {"mount": part.mountpoint, "fstype": part.fstype}
            ctx.metric("disk_total_gb", usage.total / 1e9, unit="GB", **tag)
            ctx.metric("disk_used_gb", usage.used / 1e9, unit="GB", **tag)
            ctx.metric("disk_free_gb", usage.free / 1e9, unit="GB", **tag)
            ctx.metric("disk_used_pct", usage.percent, unit="%", **tag)
            if usage.percent >= 95:
                worst = "fail"
                warn.append(f"{part.mountpoint} at {usage.percent}%")
            elif usage.percent >= 85 and worst == "ok":
                warn.append(f"{part.mountpoint} at {usage.percent}%")

        smart_ok = self._smart_collect(ctx, warn)
        summary_bits = [f"{len(psutil.disk_partitions())} volumes scanned"]
        if smart_ok is False:
            summary_bits.append("smartctl present but reported issues")
            # SMART trouble is the whole reason this check exists — never
            # allow it to remain "ok" just because disk usage looks fine.
            worst = "fail"
        elif smart_ok is None:
            summary_bits.append("smartctl not installed (install smartmontools for SMART data)")
        if warn:
            summary_bits.append("WARN: " + "; ".join(warn))
        return CheckResult(outcome=worst, summary=" · ".join(summary_bits))

    @staticmethod
    def _smart_collect(ctx, warn) -> bool | None:
        smartctl = shutil.which("smartctl")
        if not smartctl:
            return None
        try:
            scan = subprocess.check_output([smartctl, "--scan", "-j"], timeout=10).decode()
            data = json.loads(scan)
        except Exception:  # noqa: BLE001
            return False
        any_bad = False
        for dev in data.get("devices", []):
            name = dev.get("name", "?")
            try:
                info = json.loads(subprocess.check_output(
                    [smartctl, "-a", "-j", name], timeout=15,
                ).decode())
            except Exception:  # noqa: BLE001
                continue
            health = info.get("smart_status", {}).get("passed")
            ctx.metric("smart_passed", bool(health), device=name)
            ctx.metric("smart_model", info.get("model_name", ""), device=name)
            for k in ("temperature", "power_on_time", "power_cycle_count"):
                v = info.get(k)
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if isinstance(vv, (int, float)):
                            ctx.metric(f"smart_{k}_{kk}", vv, device=name)
                elif isinstance(v, (int, float)):
                    ctx.metric(f"smart_{k}", v, device=name)
            if health is False:
                any_bad = True
                warn.append(f"{name} SMART status FAILED")
        return not any_bad


# 6.4 — Battery + thermal envelope --------------------------------------------

@register(
    suite="hardware", id="6.4", name="battery_thermal",
    status="real",
    description="powercfg /batteryreport parsing. Returns capacity ratio + cycle count.",
    safe=True,
)
class BatteryThermal(Check):
    def run(self, ctx) -> CheckResult:
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "battery.html"
            try:
                subprocess.check_call(
                    ["powercfg", "/batteryreport", "/output", str(report_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                )
            except FileNotFoundError:
                return CheckResult(outcome="skipped", summary="powercfg not on PATH")
            except subprocess.CalledProcessError as exc:
                return CheckResult(outcome="skipped",
                                   summary=f"powercfg failed (likely no battery): {exc}")
            if not report_path.exists():
                return CheckResult(outcome="skipped", summary="no battery report generated")
            html = report_path.read_text(encoding="utf-8", errors="ignore")

        design = self._first_int(html, r"DESIGN CAPACITY[^0-9]*([0-9,]+)")
        full = self._first_int(html, r"FULL CHARGE CAPACITY[^0-9]*([0-9,]+)")
        cycles = self._first_int(html, r"CYCLE COUNT[^0-9]*([0-9,]+)")
        ratio = (full / design) if (design and full) else None
        ctx.metric("design_capacity_mwh", design)
        ctx.metric("full_charge_capacity_mwh", full)
        ctx.metric("cycle_count", cycles)
        if ratio is not None:
            ctx.metric("capacity_ratio", ratio, unit="ratio")
        outcome = "ok"
        warn = []
        if ratio is not None and ratio < 0.6:
            outcome = "fail"
            warn.append(f"battery health {ratio*100:.0f}%")
        summary = f"cycles={cycles} capacity={full}/{design} mWh"
        if warn:
            summary += "  WARN: " + "; ".join(warn)
        return CheckResult(outcome=outcome, summary=summary)

    @staticmethod
    def _first_int(text: str, pattern: str) -> int | None:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None


# 6.5 — Power-event resilience (stub) -----------------------------------------

@register(
    suite="hardware", id="6.5", name="power_event_resilience",
    status="stub",
    description="Confirm clean shutdown on AC loss + battery drain. Coordinates "
                "a hardware-level test that this harness can only verify after the fact.",
    destructive=False,
)
class PowerEventResilience(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("manual_step", "pull AC and drain to 5%, then re-run integrity suite")
        return CheckResult(
            outcome="stub",
            summary="manual test — verify section 3 integrity checks pass after a power-loss drill",
        )
