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
- `docs/operator-safe-commands.md`: safe, risky, and prohibited field commands
- `docs/maintenance.md`: dependency updates, image policy, and PowerShell validation

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

- Place the English Kiwix ZIM at `kiwix/wikipedia_en_all_mini_2026-03.zim`
- Place the Spanish Kiwix ZIM at `kiwix/wikipedia_es_all_maxi_2026-02.zim`
- `kolibri-data/` is created and populated locally by the Kolibri container
- `backend-data/` is created and populated locally by the stack, including Chroma and control-plane state

## Startup

### Recommended startup

Run the preflight first. It verifies Docker reachability, GPU runtime visibility, image readiness, and local GGUF model presence.

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\llama-runtime\scripts\preflight_llama_runtime.ps1
```

Preflight is offline-first: it accepts required images already present in the
local Docker cache. On an online maintenance machine, add `-OnlineImageCheck` to
verify registry availability too.

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

On the host machine you can use `http://localhost/`. Nearby devices must use the
host's LAN or hotspot address instead, for example `http://192.168.1.50/` or
`http://192.168.137.1/`.

- Portal home: `http://<host-ip>/`
- AI chat: `http://<host-ip>/ai/`
- Kiwix: `http://<host-ip>/wiki/`
- Kolibri: `http://<host-ip>/kolibri/`
- Learn alias: `http://<host-ip>/learn/`
- DNS Admin: `http://<host-ip>:5380/`

Open WebUI is disabled in the default field stack because it has a separate
authentication and audit surface. For trusted debugging only, start with
`--profile debug-openwebui` and use `http://<host-ip>/chat/`.

## Offline Local Access

The stack already binds on the host network edge for client traffic:

- Caddy publishes `80:80` and listens on all host interfaces
- `ai-control` listens on `0.0.0.0:8081` inside the container
- `llama` listens on `0.0.0.0:2020` inside the container

That means nearby devices should connect to the Windows host's private IPv4
address, not `localhost`.

### Recommended field modes

1. Hotspot mode for fully offline use.
   Double-click `AIBox Control` for the one-click operator flow, or run
   `tools\llama-runtime\scripts\up_stack.ps1` directly.
   Startup brings up the Docker stack, starts Windows Mobile Hotspot, writes a
   `puente.link` entry into the Windows `hosts` file for the hotspot DNS proxy,
   and validates that hotspot clients can resolve it.
   Clients join the hotspot SSID and browse to `http://puente.link/`.
   If diagnostics show `ip_only`, use `http://192.168.137.1/` temporarily.
   This is the most stable no-router option on Windows.
2. Fixed LAN IP mode when a router or switch exists.
   Reserve the Windows host IP in the router DHCP server or assign a static IPv4
   address on the active NIC.
   Set `OFFLINE_ACCESS_IP` in `stack/.env` to that same address so the operator
   diagnostics and portal show the intended stable URL.

### Operator diagnostics

Use these scripts from the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\llama-runtime\scripts\diagnose_local_access.ps1
powershell -ExecutionPolicy Bypass -File .\tools\llama-runtime\scripts\get_network_info.ps1
```

They report:

- current LAN and hotspot addresses
- the recommended client URL
- whether Windows is listening on port 80
- whether a Windows Firewall allow rule for port 80 was detected
- warnings when `OFFLINE_ACCESS_IP` does not match the active NIC

The portal also exposes a field-facing guide at `/connect.html`.

### Simple hostname

You can set `OFFLINE_HOSTNAME` in `stack/.env` so the diagnostics, startup
checks, and connection page target a friendly name such as `puente.link`.

During hotspot startup AIBox now validates whether nearby clients should
actually be able to resolve that name through the laptop DNS service. If that
check fails, diagnostics will fall back to the hotspot IP instead of claiming
that the hostname is ready.

For hotspot mode specifically, AIBox relies on Windows ICS reading the host
`hosts` file. Startup adds `192.168.137.1 puente.link # AIBox-Puente` and stop
removes that tagged line again.

### Local DNS Name Like `puente.link`

For nearby clients to open `http://puente.link/` on the local network, you need:

- a stable host IP
- clients using the AIBox DNS server
- a local DNS record that maps `puente.link` to that host IP

The repo includes a helper script for Technitium DNS when you are using LAN mode
and want clients on the same router or switch to resolve `puente.link` through
the AIBox DNS service:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\llama-runtime\scripts\configure_local_dns_name.ps1 -Domain puente.link
```

By default it uses:

- `OFFLINE_HOSTNAME` for the DNS name
- `OFFLINE_ACCESS_IP` for the target IP when set
- otherwise the current primary LAN IP from `get_network_info.ps1`

Important:

- this creates a local DNS answer only; it does not change public internet DNS
- hotspot mode does not depend on this helper; hotspot DNS is driven by the
  Windows `hosts` file mapping described above
- clients must use the AIBox DNS server on port 53 for `puente.link` to resolve locally
- `DNS_SERVER_DOMAIN` and `DNS_ADMIN_PASSWORD` only initialize Technitium on first start when its config volume is still empty

### Important LAN note

For HTTP-only local deployments, set:

```text
SESSION_COOKIE_SECURE=false
```

Otherwise modern browsers will silently drop the session cookie over plain HTTP.

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
