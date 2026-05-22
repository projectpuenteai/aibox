"""Perf suite checks (4.x): latency baseline, throughput, cold/warm, embedding throughput."""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import requests

from aibox.checks.harness.base import Check, CheckResult, register


BASE = os.environ.get("AIBOX_CHECK_BASE", "http://localhost")
HEALTH_PATH = "/ai/api/health"
CHAT_PATH = "/ai/api/v1/app/chat/completions"
TOKEN = os.environ.get("AIBOX_CHECK_TOKEN")


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# 4.1 — Latency baseline ------------------------------------------------------

LAT_N_REQUESTS = int(os.environ.get("AIBOX_CHECK_PERF_N", "20"))


@register(
    suite="perf", id="4.1", name="latency_baseline",
    status="real",
    description="N sequential probes to /health (always) and /chat (if AIBOX_CHECK_TOKEN set). "
                "Records p50/p95/p99 + error rate.",
)
class LatencyBaseline(Check):
    def run(self, ctx) -> CheckResult:
        url_health = f"{BASE.rstrip('/')}{HEALTH_PATH}"
        url_chat = f"{BASE.rstrip('/')}{CHAT_PATH}"
        h_times, h_errors = self._probe_get(url_health, LAT_N_REQUESTS)
        self._emit(ctx, "health", h_times, h_errors)
        if h_errors == LAT_N_REQUESTS:
            return CheckResult(outcome="fail",
                               summary=f"all {LAT_N_REQUESTS} health probes failed (stack not running?)")
        if not TOKEN:
            return CheckResult(
                outcome="ok",
                summary=f"health p95={_percentile(h_times, 0.95)*1000:.0f}ms "
                        f"(set AIBOX_CHECK_TOKEN to also probe chat)",
            )
        queries = self._load_queries()
        c_times, c_errors = self._probe_chat(url_chat, queries[:LAT_N_REQUESTS])
        self._emit(ctx, "chat", c_times, c_errors)
        summary = (
            f"health p95={_percentile(h_times, 0.95)*1000:.0f}ms; "
            f"chat p95={_percentile(c_times, 0.95)*1000:.0f}ms "
            f"(errors {c_errors}/{LAT_N_REQUESTS})"
        )
        outcome = "fail" if c_errors > LAT_N_REQUESTS * 0.1 else "ok"
        return CheckResult(outcome=outcome, summary=summary)

    @staticmethod
    def _probe_get(url: str, n: int) -> tuple[list[float], int]:
        times = []
        errors = 0
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                resp = requests.get(url, timeout=10)
                if resp.status_code >= 400:
                    errors += 1
                else:
                    times.append(time.perf_counter() - t0)
            except Exception:  # noqa: BLE001
                errors += 1
        return times, errors

    @staticmethod
    def _probe_chat(url: str, queries: list[dict]) -> tuple[list[float], int]:
        times = []
        errors = 0
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        for q in queries:
            payload = {"messages": [{"role": "user", "content": q["query"]}], "stream": False}
            t0 = time.perf_counter()
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=120)
                if resp.status_code >= 400:
                    errors += 1
                else:
                    times.append(time.perf_counter() - t0)
            except Exception:  # noqa: BLE001
                errors += 1
        return times, errors

    @staticmethod
    def _load_queries() -> list[dict]:
        fp = Path(__file__).resolve().parents[1] / "fixtures" / "queries_en.jsonl"
        if not fp.exists():
            return []
        return [json.loads(line) for line in fp.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _emit(ctx, stage: str, times: list[float], errors: int) -> None:
        ctx.metric(f"{stage}_n", len(times) + errors, stage=stage)
        ctx.metric(f"{stage}_errors", errors, stage=stage)
        if not times:
            return
        ctx.metric(f"{stage}_p50_ms", _percentile(times, 0.50) * 1000, unit="ms", stage=stage)
        ctx.metric(f"{stage}_p95_ms", _percentile(times, 0.95) * 1000, unit="ms", stage=stage)
        ctx.metric(f"{stage}_p99_ms", _percentile(times, 0.99) * 1000, unit="ms", stage=stage)
        ctx.metric(f"{stage}_mean_ms", statistics.mean(times) * 1000, unit="ms", stage=stage)


# 4.2 — Throughput baseline ---------------------------------------------------

THROUGHPUT_CONCURRENCY = int(os.environ.get("AIBOX_CHECK_THROUGHPUT_C", "8"))
THROUGHPUT_DURATION_S = int(os.environ.get("AIBOX_CHECK_THROUGHPUT_DUR", "10"))


@register(
    suite="perf", id="4.2", name="throughput_baseline",
    status="real",
    description=f"Concurrency={THROUGHPUT_CONCURRENCY} on /health for "
                f"{THROUGHPUT_DURATION_S}s. Reports QPS and error rate.",
)
class ThroughputBaseline(Check):
    def run(self, ctx) -> CheckResult:
        url = f"{BASE.rstrip('/')}/ai/api/health"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code >= 400:
                return CheckResult(outcome="skipped", summary=f"/health returned {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(outcome="skipped", summary=f"/health unreachable: {exc}")
        ok, err, lat = asyncio.run(self._run_load(url))
        ctx.metric("ok_count", ok)
        ctx.metric("error_count", err)
        ctx.metric("duration_s", THROUGHPUT_DURATION_S)
        ctx.metric("concurrency", THROUGHPUT_CONCURRENCY)
        qps = ok / THROUGHPUT_DURATION_S
        ctx.metric("qps", qps, unit="req/s")
        if lat:
            ctx.metric("p95_ms", sorted(lat)[int(len(lat) * 0.95)] * 1000, unit="ms")
        return CheckResult(
            outcome="ok" if err < ok * 0.05 else "fail",
            summary=f"qps={qps:.1f} ok={ok} err={err}",
        )

    @staticmethod
    async def _run_load(url: str):
        try:
            import aiohttp
        except ImportError:
            return _stdlib_load(url, THROUGHPUT_CONCURRENCY, THROUGHPUT_DURATION_S)
        deadline = time.time() + THROUGHPUT_DURATION_S
        ok = err = 0
        lat: list[float] = []
        async with aiohttp.ClientSession() as session:
            async def worker():
                nonlocal ok, err
                while time.time() < deadline:
                    t0 = time.perf_counter()
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                            if r.status >= 400:
                                err += 1
                            else:
                                ok += 1
                                lat.append(time.perf_counter() - t0)
                    except Exception:  # noqa: BLE001
                        err += 1
            await asyncio.gather(*[worker() for _ in range(THROUGHPUT_CONCURRENCY)])
        return ok, err, lat


def _stdlib_load(url: str, concurrency: int, duration_s: int):
    """Threaded fallback if aiohttp isn't installed."""
    import threading
    deadline = time.time() + duration_s
    ok = err = 0
    lat: list[float] = []
    lock = threading.Lock()

    def worker():
        nonlocal ok, err
        while time.time() < deadline:
            t0 = time.perf_counter()
            try:
                r = requests.get(url, timeout=10)
                with lock:
                    if r.status_code >= 400:
                        err += 1
                    else:
                        ok += 1
                        lat.append(time.perf_counter() - t0)
            except Exception:  # noqa: BLE001
                with lock:
                    err += 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ok, err, lat


# 4.3 — Cold vs warm cache delta (stub) ---------------------------------------

@register(
    suite="perf", id="4.3", name="cold_warm_delta",
    status="stub",
    description="Run latency baseline twice — once with caches cold (forced restart), "
                "once warm immediately after. Stub: requires destructive restart.",
    destructive=True,
)
class ColdWarmDelta(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="needs orchestrated docker compose restart between probe rounds — "
                    "see 1.5 for the cold-start cost measurement",
        )


# 4.4 — Embedding throughput --------------------------------------------------

@register(
    suite="perf", id="4.4", name="embedding_throughput",
    status="real",
    description="bge-m3 throughput at batch sizes 1/8/32/128. Loads the model into "
                "GPU; can take ~30s on first run.",
    requires=("module:sentence_transformers",),
)
class EmbeddingThroughput(Check):
    BATCHES = (1, 8, 32, 128)
    MODEL = os.environ.get("AIBOX_CHECK_EMBED_MODEL", "BAAI/bge-m3")
    N_TEXTS = 256

    def run(self, ctx) -> CheckResult:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            return CheckResult(outcome="skipped", summary=f"sentence_transformers missing: {exc}")
        device = "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
        except ImportError:
            pass
        ctx.metric("device", device)
        local_path = ctx.repo_root / "aibox" / "models" / "bge-m3"
        model_path = str(local_path) if local_path.exists() else self.MODEL
        try:
            model = SentenceTransformer(model_path, device=device)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(outcome="skipped",
                               summary=f"model load failed (offline + no local cache?): {exc}")
        sample = ["This is a test sentence for embedding throughput."] * self.N_TEXTS
        results = []
        for bs in self.BATCHES:
            t0 = time.perf_counter()
            model.encode(sample, batch_size=bs, show_progress_bar=False)
            dt = time.perf_counter() - t0
            tps = self.N_TEXTS / dt
            ctx.metric("texts_per_sec", tps, unit="texts/s", batch_size=bs)
            ctx.metric("seconds_total", dt, unit="s", batch_size=bs)
            results.append((bs, tps))
        best_bs, best_tps = max(results, key=lambda r: r[1])
        return CheckResult(
            outcome="ok",
            summary=f"best batch={best_bs} @ {best_tps:.0f} texts/s on {device}",
        )
