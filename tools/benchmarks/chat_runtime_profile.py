"""Profile streamed chat requests against the llama.cpp-backed chat endpoint.

This script collects latency numbers together with lightweight Docker, GPU, and WSL
resource snapshots so operators can compare runtime settings or hardware changes.
"""

import argparse
import json
import math
import random
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PROMPTS = [
    "How can a coffee farmer in Guatemala reduce losses from rust disease? Give practical steps.",
    "Explain crop rotation for small farms in Peru with an example plan.",
    "What are 5 low-cost irrigation improvements for rural farms in Honduras?",
    "Create a simple weekly study plan for a 12-year-old learning math and science.",
    "Explain photosynthesis in Spanish for children with one hands-on activity.",
]


def utc_now() -> str:
    """Return a UTC ISO timestamp for benchmark logs and JSON output."""
    return datetime.now(timezone.utc).isoformat()


def run_cmd(command: str) -> Dict[str, Any]:
    """Run a shell command and capture stdout/stderr without raising on failure."""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "command": command,
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }


def parse_gpu_query_output(raw: str) -> List[Dict[str, int]]:
    entries: List[Dict[str, int]] = []
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text:
            continue
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 3:
            continue
        try:
            util = int(parts[0])
            mem_used = int(parts[1])
            mem_total = int(parts[2])
        except ValueError:
            continue
        entries.append({
            "utilization_gpu_pct": util,
            "memory_used_mib": mem_used,
            "memory_total_mib": mem_total,
        })
    return entries


def query_gpu_snapshot() -> Optional[List[Dict[str, int]]]:
    result = run_cmd("nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits")
    if not result.get("ok"):
        return None
    parsed = parse_gpu_query_output(result.get("stdout", ""))
    return parsed or None


class ActiveGpuSampler:
    def __init__(self, interval_seconds: float = 0.2) -> None:
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.samples: List[int] = []

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            snapshot = query_gpu_snapshot()
            if snapshot:
                util_values = [int(item.get("utilization_gpu_pct", 0)) for item in snapshot]
                if util_values:
                    self.samples.append(max(util_values))
            self._stop_event.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> Dict[str, Any]:
        if self._thread is None:
            return {
                "sample_count": 0,
                "median_util_pct": None,
                "peak_util_pct": None,
            }
        self._stop_event.set()
        self._thread.join(timeout=2.5)
        self._thread = None

        if not self.samples:
            return {
                "sample_count": 0,
                "median_util_pct": None,
                "peak_util_pct": None,
            }

        ordered = sorted(self.samples)
        median_val = int(round(statistics.median(ordered)))
        peak_val = int(max(ordered))
        return {
            "sample_count": len(self.samples),
            "median_util_pct": median_val,
            "peak_util_pct": peak_val,
        }


def capture_resource_snapshot(label: str) -> Dict[str, Any]:
    return {
        "label": label,
        "ts": utc_now(),
        "docker_stats": run_cmd('docker stats --no-stream --format "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"'),
        "gpu": run_cmd("nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits"),
        "vmmem": run_cmd("powershell -Command \"Get-Process -Name vmmem,vmmemWSL -ErrorAction SilentlyContinue | Select-Object ProcessName,WorkingSet64 | ConvertTo-Json -Compress\""),
    }


def post_json(url: str, payload: Dict[str, Any], timeout_seconds: int) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def extract_token_count(obj: Dict[str, Any]) -> Optional[int]:
    tokens = obj.get("tokens")
    if isinstance(tokens, list):
        return len(tokens)
    if isinstance(tokens, int):
        return tokens
    token_count = obj.get("token_count")
    if isinstance(token_count, int):
        return token_count
    count = obj.get("count")
    if isinstance(count, int):
        return count
    return None


def count_tokens_via_llama(text: str, chat_url: str, timeout_seconds: int) -> int:
    parsed = urllib.parse.urlparse(chat_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    path = parsed.path or ""
    prefix = ""
    marker = "/v1/chat/completions"
    if marker in path:
        prefix = path.split(marker, 1)[0]

    def with_prefix(p: str) -> str:
        if prefix:
            return f"{base}{prefix}{p}"
        return f"{base}{p}"

    candidates = [
        (with_prefix("/tokenize"), {"content": text}),
        (with_prefix("/tokenize"), {"text": text}),
        (with_prefix("/v1/tokenize"), {"input": text}),
        (f"{base}/tokenize", {"content": text}),
        (f"{base}/tokenize", {"text": text}),
        (f"{base}/v1/tokenize", {"input": text}),
    ]

    for url, payload in candidates:
        obj = post_json(url, payload, timeout_seconds)
        if not isinstance(obj, dict):
            continue
        count = extract_token_count(obj)
        if isinstance(count, int) and count > 0:
            return count

    return max(1, int(math.ceil(len(text) / 4.0)))


def request_chat(url: str, model: str, prompt: str, max_tokens: int, timeout_seconds: int) -> Dict[str, Any]:
    """Send one streamed chat request and summarize its timing characteristics."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": True,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    events_count = 0
    first_token_ms: Optional[int] = None
    finish_reason = "unknown"
    answer_parts: List[str] = []

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            while True:
                raw_line = resp.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue

                payload = line[5:].strip()
                if not payload:
                    continue

                if payload == "[DONE]":
                    break

                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                events_count += 1

                if isinstance(evt, dict) and evt.get("error"):
                    raise RuntimeError(f"llama error: {evt['error']}")

                choices = evt.get("choices") if isinstance(evt, dict) else None
                if not isinstance(choices, list) or not choices:
                    continue

                choice = choices[0] if isinstance(choices[0], dict) else {}
                maybe_finish = choice.get("finish_reason")
                if isinstance(maybe_finish, str) and maybe_finish:
                    finish_reason = maybe_finish

                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                    answer_parts.append(content)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"request failed: {type(exc).__name__}: {exc}") from exc

    total_ms = int((time.perf_counter() - started) * 1000)
    answer = "".join(answer_parts).strip()

    if not answer:
        raise RuntimeError("No assistant text received from stream")

    ttft_ms = first_token_ms if isinstance(first_token_ms, int) else total_ms
    generation_ms = max(1, total_ms - ttft_ms)
    output_tokens = count_tokens_via_llama(answer, url, timeout_seconds)
    tps = round((output_tokens * 1000.0) / max(generation_ms, 1), 2)

    return {
        "ttft_ms": ttft_ms,
        "generation_ms": generation_ms,
        "total_ms": total_ms,
        "tokens_per_sec": tps,
        "output_tokens": output_tokens,
        "stop_reason": finish_reason,
        "events_count": events_count,
    }


def median(values: List[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def run_profile(args: argparse.Namespace) -> Dict[str, Any]:
    """Run the requested number of chat calls and aggregate benchmark metrics."""
    prompts = DEFAULT_PROMPTS[:]
    random.seed(args.seed)
    random.shuffle(prompts)

    results: List[Dict[str, Any]] = []
    snapshots = [capture_resource_snapshot("before")]

    for i in range(args.requests):
        prompt = prompts[i % len(prompts)]
        sampler = ActiveGpuSampler(interval_seconds=args.gpu_sample_interval)
        sampler.start()
        try:
            result = request_chat(
                url=args.url,
                model=args.model,
                prompt=prompt,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
            )
        finally:
            gpu_active = sampler.stop()

        result["index"] = i + 1
        result["prompt"] = prompt
        result["active_gpu"] = gpu_active
        results.append(result)

    snapshots.append(capture_resource_snapshot("after"))

    ttft = [float(r["ttft_ms"]) for r in results if isinstance(r.get("ttft_ms"), (int, float))]
    tps = [float(r["tokens_per_sec"]) for r in results if isinstance(r.get("tokens_per_sec"), (int, float))]
    total = [float(r["total_ms"]) for r in results if isinstance(r.get("total_ms"), (int, float))]
    active_gpu_medians = [
        float(r["active_gpu"]["median_util_pct"])
        for r in results
        if isinstance(r.get("active_gpu", {}).get("median_util_pct"), (int, float))
    ]
    active_gpu_peaks = [
        float(r["active_gpu"]["peak_util_pct"])
        for r in results
        if isinstance(r.get("active_gpu", {}).get("peak_util_pct"), (int, float))
    ]

    return {
        "ts": utc_now(),
        "url": args.url,
        "model": args.model,
        "requests": args.requests,
        "max_tokens": args.max_tokens,
        "summary": {
            "median_ttft_ms": median(ttft),
            "median_tps": median(tps),
            "median_total_ms": median(total),
            "median_active_gpu_util_pct": median(active_gpu_medians),
            "peak_active_gpu_util_pct": max(active_gpu_peaks) if active_gpu_peaks else 0.0,
            "stop_reason_counts": {
                key: sum(1 for r in results if r.get("stop_reason") == key)
                for key in sorted({str(r.get("stop_reason")) for r in results})
            },
        },
        "results": results,
        "resource_snapshots": snapshots,
    }


def main() -> None:
    """Parse CLI args, run the profile, and write the JSON report to disk."""
    parser = argparse.ArgumentParser(description="Profile llama.cpp chat runtime and collect resource telemetry.")
    parser.add_argument("--url", default="http://localhost/ai/api/v1/chat/completions")
    parser.add_argument("--model", default="qwen2.5-7b-instruct-q4_0")
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--gpu-sample-interval", type=float, default=0.2)
    parser.add_argument(
        "--output",
        default=str(
            Path(__file__).resolve().parent / "results" / "chat_runtime_profile.json"
        ),
    )
    args = parser.parse_args()

    profile = run_profile(args)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")

    summary = profile["summary"]
    print(f"[done] requests={args.requests}")
    print(f"[done] median TTFT ms: {summary['median_ttft_ms']:.1f}")
    print(f"[done] median TPS: {summary['median_tps']:.2f}")
    print(f"[done] median total ms: {summary['median_total_ms']:.1f}")
    print(f"[done] median active GPU util %: {summary['median_active_gpu_util_pct']:.1f}")
    print(f"[done] peak active GPU util %: {summary['peak_active_gpu_util_pct']:.1f}")
    print(f"[done] output: {output_path}")


if __name__ == "__main__":
    main()

