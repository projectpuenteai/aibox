"""Stress suite checks (1.x): concurrent chat, soak, spike, RAG-only, cold start."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


BASE = os.environ.get("AIBOX_CHECK_BASE", "http://localhost")
TOKEN = os.environ.get("AIBOX_CHECK_TOKEN")
CONCURRENCY = int(os.environ.get("AIBOX_CHECK_STRESS_C", "4"))
DURATION_S = int(os.environ.get("AIBOX_CHECK_STRESS_DUR", "30"))
THINK_TIME_MEAN = float(os.environ.get("AIBOX_CHECK_STRESS_THINK", "3"))


def _p(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = int(k); c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _threaded_driver(url, queries):
    """Minimal fallback — single-thread, ignores concurrency knob."""
    import requests
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    deadline = time.time() + DURATION_S
    ok = err = 0
    first_token_times = []
    full_times = []
    while time.time() < deadline:
        q = random.choice(queries)
        payload = {"messages": [{"role": "user", "content": q["query"]}], "stream": False}
        t0 = time.perf_counter()
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=120)
            if r.status_code >= 400:
                err += 1
                continue
            full = time.perf_counter() - t0
            ok += 1
            full_times.append(full)
            first_token_times.append(full)
        except Exception:  # noqa: BLE001
            err += 1
        time.sleep(THINK_TIME_MEAN)
    return {"ok": ok, "err": err, "first_token_times": first_token_times, "full_times": full_times}


# 1.1 — Concurrent chat -------------------------------------------------------

@register(
    suite="stress", id="1.1", name="concurrent_chat",
    status="real",
    description=f"Concurrent chat load: default C={CONCURRENCY} for {DURATION_S}s. "
                "Token-authenticated; without AIBOX_CHECK_TOKEN this skips. "
                "Override C / duration / think-time via AIBOX_CHECK_STRESS_*.",
)
class ConcurrentChat(Check):
    def run(self, ctx) -> CheckResult:
        if not TOKEN:
            return CheckResult(
                outcome="skipped",
                summary="set AIBOX_CHECK_TOKEN to a valid bearer token to run this check",
            )
        fp = Path(__file__).resolve().parents[1] / "fixtures" / "queries_en.jsonl"
        queries = [json.loads(l) for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not queries:
            return CheckResult(outcome="skipped", summary="no fixture queries available")
        url = f"{BASE.rstrip('/')}/ai/api/v1/app/chat/completions"
        stats = asyncio.run(self._drive(url, queries))
        ok = stats["ok"]
        err = stats["err"]
        ftt = stats["first_token_times"]
        full = stats["full_times"]
        ctx.metric("concurrency", CONCURRENCY)
        ctx.metric("duration_s", DURATION_S)
        ctx.metric("ok", ok)
        ctx.metric("err", err)
        if ftt:
            ctx.metric("first_token_p50_ms", _p(ftt, 0.5) * 1000, unit="ms")
            ctx.metric("first_token_p95_ms", _p(ftt, 0.95) * 1000, unit="ms")
            ctx.metric("first_token_p99_ms", _p(ftt, 0.99) * 1000, unit="ms")
        if full:
            ctx.metric("full_p50_ms", _p(full, 0.5) * 1000, unit="ms")
            ctx.metric("full_p95_ms", _p(full, 0.95) * 1000, unit="ms")
        if ok == 0:
            return CheckResult(outcome="fail", summary=f"all {err} requests failed")
        outcome = "fail" if err > ok * 0.05 else "ok"
        return CheckResult(
            outcome=outcome,
            summary=f"C={CONCURRENCY} ok={ok} err={err} "
                    f"first-token p95={_p(ftt, 0.95)*1000:.0f}ms",
        )

    async def _drive(self, url: str, queries: list[dict]) -> dict:
        try:
            import aiohttp
        except ImportError:
            return await asyncio.to_thread(_threaded_driver, url, queries)
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        deadline = time.time() + DURATION_S
        ok = err = 0
        first_token_times: list[float] = []
        full_times: list[float] = []

        async def session_user(idx: int):
            nonlocal ok, err
            async with aiohttp.ClientSession() as session:
                while time.time() < deadline:
                    q = random.choice(queries)
                    payload = {
                        "messages": [{"role": "user", "content": q["query"]}],
                        "stream": True,
                    }
                    t0 = time.perf_counter()
                    first_seen = None
                    try:
                        async with session.post(
                            url, headers=headers, json=payload,
                            timeout=aiohttp.ClientTimeout(total=120),
                        ) as resp:
                            if resp.status >= 400:
                                err += 1
                                continue
                            async for chunk in resp.content.iter_any():
                                if first_seen is None and chunk:
                                    first_seen = time.perf_counter() - t0
                            full = time.perf_counter() - t0
                            ok += 1
                            if first_seen is not None:
                                first_token_times.append(first_seen)
                            full_times.append(full)
                    except Exception:  # noqa: BLE001
                        err += 1
                    await asyncio.sleep(max(0.0, random.expovariate(1 / THINK_TIME_MEAN)))
        await asyncio.gather(*[session_user(i) for i in range(CONCURRENCY)])
        return {
            "ok": ok, "err": err,
            "first_token_times": first_token_times,
            "full_times": full_times,
        }


# 1.2 — Soak test (stub) ------------------------------------------------------

@register(
    suite="stress", id="1.2", name="soak_test",
    status="stub",
    description="24h / 72h / 7-day soak. Sample RSS + FD count + VRAM every 60s; "
                "look for slow leaks. Stubbed because it needs orchestration outside "
                "a single harness invocation.",
)
class SoakTest(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("recommended_runner", "scheduled task that invokes 1.1 + telemetry sampler hourly")
        return CheckResult(
            outcome="stub",
            summary="schedule 1.1 + 6.1/6.2 every hour for the soak window, then "
                    "report the slope of RSS / VRAM over the window",
        )


# 1.3 — Spike test (stub) -----------------------------------------------------

@register(
    suite="stress", id="1.3", name="spike_test",
    status="stub",
    description="Ramp 0→30 concurrent users in 5s, hold for 60s, measure time-to-recovery.",
)
class SpikeTest(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="would use 1.1's driver with a step-up ramp profile",
        )


# 1.4 — RAG-only stress (stub) ------------------------------------------------

@register(
    suite="stress", id="1.4", name="rag_only_stress",
    status="stub",
    description="Hit embedding + ChromaDB + reranker without the LLM. Isolates which "
                "stage bottlenecks first under load.",
)
class RagOnlyStress(Check):
    def run(self, ctx) -> CheckResult:
        ctx.metric("required_endpoint", "/ai/api/v1/admin/retrieval/dry-run (does not exist yet)")
        return CheckResult(
            outcome="stub",
            summary="needs an internal retrieval-only endpoint or a Python harness that "
                    "reuses ai-control's retrieval module",
        )


# 1.5 — Cold-start cost (stub) ------------------------------------------------

@register(
    suite="stress", id="1.5", name="cold_start_cost",
    status="stub",
    description="Time from `docker compose up` to first successful /ai/api/chat response. "
                "Destructive: restarts the stack. Requires --i-mean-it.",
    destructive=True,
)
class ColdStartCost(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="would `docker compose down`, then `up`, poll /health until 200, "
                    "then run a single chat probe. Restarts disturb live users — gate "
                    "behind --i-mean-it and a maintenance-window flag.",
        )
