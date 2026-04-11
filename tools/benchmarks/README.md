# Chat Runtime Benchmarking (Docker llama.cpp Stream)

This folder contains reproducible scripts to profile chat latency, throughput, and host/container resource usage.

## Controlled Profile (Docker-native llama + Caddy)

1. Start required Docker services:

```powershell
docker compose -f stack/docker-compose.yaml up -d llama ai-control caddy dns chat kiwix kolibri
```

2. Run profile:

```powershell
python tools/benchmarks/chat_runtime_profile.py --requests 10 --max-tokens 256
```

The script posts to `http://localhost/ai/api/v1/chat/completions`, parses OpenAI-style stream chunks, and captures:

- median TTFT
- median tokens/sec
- median total response time
- per-run `finish_reason`
- active-generation GPU utilization (median/peak via periodic `nvidia-smi` sampling)
- resource snapshots before/after (`docker stats`, `nvidia-smi`, and `vmmem` process memory)

Token counts are computed via llama tokenization endpoints (`/tokenize`, `/v1/tokenize`) with char/4 fallback when unavailable.

## Native vs Docker Comparison

For a direct comparison with previous native-host runs:

1. Run this benchmark with Docker-native llama.
2. Compare median TPS/TTFT against archived native baseline runs.
3. Check GPU attachment and utilization during runs (`nvidia-smi` + control API status).


