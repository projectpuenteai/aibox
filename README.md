# AIBox

Windows-first Docker stack for local AIBox services, with Git used for code, config, and rollback only.

This repository intentionally does not store local model weights, Chroma databases, downloaded learning content, logs, or other machine-specific runtime state.

## What Is In This Repo

- `stack/`: Docker Compose, Caddy config, `.env.example`, and portal assets
- `tools/ai-control/`: FastAPI control-plane and RAG service (the core custom code)
- `tools/data_prep/`: Wikipedia dump extraction and chunking scripts
- `tools/index/`: ChromaDB index build, inspect, query, and rebuild scripts
- `tools/config/`: shared constants used by all pipeline scripts
- `tools/benchmarks/`: latency and chat runtime profiling
- `tools/tests/`: comprehensive RAG test suite
- `tools/llama-runtime/`: PowerShell preflight and startup scripts

## What Is Not In This Repo

These are expected to exist locally on each machine and are ignored by Git:

- `models/`
- `backend-data/`
- `kolibri-data/`
- `runtime/`
- `kiwix/`
- local `.env` files
- caches, logs, and `node_modules`

## Prerequisites

- Windows with PowerShell
- Git
- Docker Desktop
- NVIDIA GPU + Docker GPU support for the default CUDA llama runtime
- Python 3.12 if you want to run local Python tools outside Docker

## Quick Start

```powershell
git clone https://github.com/projectpuenteai/aibox.git
cd aibox
copy stack\.env.example stack\.env
```

Edit `stack/.env` and fill in the **required** values (see below).

## Required Secrets

The stack requires these values in `stack/.env` before it will start:

| Variable | Purpose | How to generate |
|----------|---------|----------------|
| `APP_ENCRYPTION_MASTER_KEY` | AES-GCM encryption for user data | `python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"` |
| `ADMIN_DEFAULT_PASSWORD` | Initial admin account password | Choose a strong password |
| `SESSION_TOKEN_PEPPER` | Session token hashing pepper | `python -c "import secrets; print(secrets.token_hex(16))"` |

`ADMIN_USERNAME` defaults to `puenteAdmin` but can be changed.

## Required Local Folder Layout

Create these local folders next to the tracked code if they do not already exist:

```text
aibox\
|-- backend-data\
|-- kiwix\
|-- kolibri-data\
`-- models\
    |-- embed-m3\
    |-- llm\
    |   `-- gguf\
    `-- rerank\
```

## Required Local Models

The active Docker stack uses these local model paths:

- `models/llm/gguf/<your-gguf-file>`
- `models/embed-m3/` (BGE-M3 embedding model)
- `models/rerank/` (cross-encoder reranker)

By default, `stack/.env.example` expects:

- `models/llm/gguf/qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf`

The `llama` service reads the GGUF model directly. The `ai-control` service expects local embedding and reranker model directories.

## Kiwix And Kolibri Local Data

- Place your Kiwix ZIM file at `kiwix/Wiki.zim`
- `kolibri-data/` is created and populated locally by the Kolibri container
- `backend-data/` is created and populated locally by the stack, including Chroma and control-plane state

## Startup

### Recommended startup

Run the preflight first. It verifies Docker reachability, GPU runtime visibility, image readiness, and local GGUF model presence.

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\llama-runtime\scripts\preflight_llama_runtime.ps1
```

Then start the stack:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\llama-runtime\scripts\up_stack.ps1
```

### Direct Docker Compose

```powershell
docker compose -f stack/docker-compose.yaml up -d
docker compose -f stack/docker-compose.yaml down
```

If you need a smaller startup surface while debugging, start only the core services first:

```powershell
docker compose -f stack/docker-compose.yaml up -d llama caddy
```

## URLs

- Portal home: `http://localhost/`
- AI chat: `http://localhost/ai/`
- Kiwix: `http://localhost/wiki/`
- Kolibri: `http://localhost/kolibri/`
- Learn alias: `http://localhost/learn/`
- Open WebUI: `http://localhost/chat/`
- DNS Admin: `http://localhost:5380/`

## Troubleshooting

- `[daemon_permission]`: Docker daemon permission denied
- `[daemon_unreachable]`: Docker Desktop or daemon is not running
- `[image_not_found]`: bad image or tag
- `[registry_auth]`: registry authentication denied
- `[nvidia_runtime_missing]`: Docker GPU runtime is not configured
- `[model_missing]`: the GGUF file path does not match `stack/.env`

### Docker Desktop Disk Usage

Docker Desktop stores images, containers, cache, and named volumes in a WSL virtual disk such as:

```text
C:\Users\<you>\AppData\Local\Docker\wsl\disk\docker_data.vhdx
```

That VHDX can stay large after Docker data is deleted because WSL does not always compact the file automatically. To inspect Docker-managed usage:

```powershell
docker system df -v
docker builder du
```

Safe cleanup for unused containers, networks, build cache, and unused images:

```powershell
docker compose -f stack/docker-compose.yaml down
docker system prune -a
wsl --shutdown
```

Do not add `--volumes` unless you intentionally want to delete Docker named volumes. The stack's local model, Chroma, Kiwix, and Kolibri data live in repo-adjacent bind-mounted folders and are not removed by the commands above.

After pruning, compact `docker_data.vhdx` from Docker Desktop's disk cleanup UI or with a Windows VHD compaction tool while WSL is shut down.

## Rollback And Git Usage

This repository is intended for:

- tracking code changes
- syncing the active stack across devices
- rolling back source/config changes if something breaks

It is not intended to version:

- local model downloads
- Chroma indexes or backups
- Kolibri or Kiwix content
- generated logs or caches

## Canonical Paths

- Compose: `stack/docker-compose.yaml`
- Caddy: `stack/Caddyfile`
- Portal: `stack/portal/`
- AI control service: `tools/ai-control/`
- Llama runtime docs/scripts: `tools/llama-runtime/`
