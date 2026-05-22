"""Network suite checks (7.x): hotspot stability, DNS, concurrent clients, Caddy logs."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


# 7.1 — Hotspot stability -----------------------------------------------------

@register(
    suite="network", id="7.1", name="hotspot_stability",
    status="real",
    description="One-shot view of `netsh wlan show hostednetwork`. Continuous probing "
                "needs a second device (see spec section 7).",
)
class HotspotStability(Check):
    def run(self, ctx) -> CheckResult:
        netsh = shutil.which("netsh")
        if not netsh:
            return CheckResult(outcome="skipped", summary="netsh not on PATH")
        try:
            out = subprocess.check_output(
                [netsh, "wlan", "show", "hostednetwork"], timeout=10,
            ).decode(errors="ignore")
        except subprocess.CalledProcessError as exc:
            return CheckResult(outcome="fail", summary=f"netsh failed: {exc}")
        status_match = re.search(r"Status\s*:\s*(\w+)", out)
        clients_match = re.search(r"Number of clients\s*:\s*(\d+)", out)
        status = status_match.group(1) if status_match else "unknown"
        clients = int(clients_match.group(1)) if clients_match else None
        ctx.metric("hosted_network_status", status)
        if clients is not None:
            ctx.metric("client_count", clients)
        try:
            ps = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetAdapter -Physical | Where-Object {$_.Name -like '*Hotspot*' -or $_.InterfaceDescription -like '*Hotspot*'} | "
                 "Select-Object Name,Status,LinkSpeed | ConvertTo-Json"],
                stderr=subprocess.DEVNULL, timeout=10,
            ).decode(errors="ignore")
            ctx.metric("hotspot_adapter_info", ps.strip()[:500])
        except Exception:  # noqa: BLE001
            pass
        return CheckResult(
            outcome="ok",
            summary=f"hostednetwork status={status} clients={clients}",
        )


# 7.2 — DNS resolution --------------------------------------------------------

DNS_HOSTNAME = "puente.link"
DNS_EXPECTED_IP = "192.168.137.1"


@register(
    suite="network", id="7.2", name="dns_resolution",
    status="real",
    description="Resolves puente.link from the host. Cross-device probe needs a "
                "second machine; see spec section 7 for the recommended Pi Zero setup.",
)
class DnsResolution(Check):
    def run(self, ctx) -> CheckResult:
        try:
            resolved = socket.gethostbyname(DNS_HOSTNAME)
        except socket.gaierror as exc:
            ctx.metric("resolved", "")
            return CheckResult(
                outcome="fail",
                summary=f"{DNS_HOSTNAME} did not resolve: {exc}",
            )
        ctx.metric("resolved", resolved)
        ctx.metric("matches_expected", resolved == DNS_EXPECTED_IP)
        outcome = "ok" if resolved == DNS_EXPECTED_IP else "fail"
        return CheckResult(
            outcome=outcome,
            summary=f"{DNS_HOSTNAME} -> {resolved} (expected {DNS_EXPECTED_IP})",
        )


# 7.3 — Concurrent clients on hotspot (stub) ----------------------------------

@register(
    suite="network", id="7.3", name="concurrent_clients_hotspot",
    status="stub",
    description="Simulate 30 devices on the hotspot pulling static assets and hitting /chat. "
                "Requires multiple physical clients; the in-process variant (1.1) only "
                "exercises localhost.",
)
class ConcurrentClientsHotspot(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="needs hardware coordination — run 1.1 from a second laptop joined "
                    "to the hotspot for a partial proxy of this measurement",
        )


# 7.4 — Caddy access-log summary ---------------------------------------------

CADDY_CANDIDATE_LOGS = [
    "aibox/logs/caddy/access.log",
    "aibox/stack/logs/caddy/access.log",
    "aibox/backend-data/caddy/access.log",
]


@register(
    suite="network", id="7.4", name="caddy_log_summary",
    status="real",
    description="Parse Caddy JSON access logs (if enabled) and emit per-route status breakdown.",
)
class CaddyLogSummary(Check):
    def run(self, ctx) -> CheckResult:
        candidates = [ctx.repo_root / rel for rel in CADDY_CANDIDATE_LOGS]
        log = next((p for p in candidates if p.exists() and p.is_file()), None)
        if not log:
            return CheckResult(
                outcome="skipped",
                summary="no Caddy access log found at expected locations "
                        "(enable JSON access logs in Caddyfile, or set log path)",
            )
        ctx.metric("source", str(log.relative_to(ctx.repo_root)))
        per_route: dict[str, dict[str, int]] = {}
        total = 0
        bad = 0
        size = log.stat().st_size
        offset = max(0, size - 10 * 1024 * 1024)
        with log.open("rb") as f:
            f.seek(offset)
            if offset > 0:
                f.readline()
            for line in f:
                try:
                    rec = json.loads(line.decode("utf-8", errors="ignore"))
                except json.JSONDecodeError:
                    continue
                status = str(rec.get("status", "?"))
                uri = rec.get("request", {}).get("uri", "/")
                route = self._route_bucket(uri)
                d = per_route.setdefault(route, {})
                d[status] = d.get(status, 0) + 1
                total += 1
                try:
                    if int(status) >= 500:
                        bad += 1
                except ValueError:
                    pass
        ctx.metric("lines_parsed", total)
        ctx.metric("server_errors", bad)
        for route, codes in per_route.items():
            for code, n in codes.items():
                ctx.metric("count", n, route=route, status=code)
        if total == 0:
            return CheckResult(outcome="skipped", summary="log present but no JSON lines parsed")
        outcome = "fail" if bad > total * 0.01 else "ok"
        return CheckResult(
            outcome=outcome,
            summary=f"{total} lines parsed, {bad} 5xx ({bad/total*100:.1f}%)",
        )

    @staticmethod
    def _route_bucket(uri: str) -> str:
        if uri.startswith("/ai/"):
            return "/ai"
        if uri.startswith("/chat"):
            return "/chat"
        if uri.startswith("/wiki"):
            return "/wiki"
        if uri.startswith("/kolibri") or uri.startswith("/learn"):
            return "/kolibri"
        return "/"
