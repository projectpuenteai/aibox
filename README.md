# AIBox

Windows-first Docker stack for local AIBox services, with Git used for code, config, and rollback only.

This repository intentionally does not store local model weights, Chroma databases, downloaded learning content, logs, or other machine-specific runtime state.

## What Is In This Repo

- `stack/`: Docker Compose, Caddy config, and portal assets
- `backend/`: legacy Python AI service used by the optional `legacy-ai` profile
- `tools/`: runtime scripts, indexing tools, and support utilities
- `test/`: compatibility compose shim and test docs

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

## Clone And Prepare

```powershell
git clone https://github.com/projectpuenteai/aibox.git
cd aibox
copy stack\.env.example stack\.env
```

Edit `stack/.env` only if you need to override the default llama image or model file name.

## Required Local Folder Layout

Create these local folders next to the tracked code if they do not already exist:

```text
aibox\
|-- backend-data\
|-- kiwix\
|-- kolibri-data\
`-- models\
    |-- embed\
    |-- llm\
    |   `-- gguf\
    `-- rerank\
```

## Required Local Models

### Current default stack

The active Docker stack uses these local model paths:

- `models/llm/gguf/<your-gguf-file>`
- `models/embed/`
- `models/rerank/`

By default, `stack/.env.example` expects:

- `models/llm/gguf/qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf`

The `llama` service reads the GGUF model directly. The `ai-control` service expects local embedding and reranker model directories at `models/embed` and `models/rerank`.

### Optional legacy profile

If you run the optional legacy backend profile, it also expects Hugging Face-style model files under:

- `models/llm/`
- optional fallback path `models/llm-small/`

Start the legacy profile only if you actually need the Python backend:

```powershell
docker compose -f stack/docker-compose.yaml --profile legacy-ai up -d ai
```

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
- Compatibility compose shim: `test/docker-compose.yaml`


