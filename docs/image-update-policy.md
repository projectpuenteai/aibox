# AIBox Container Image Update Policy

AIBox is designed for offline field deployments, so image updates must be deliberate and reversible. Do not replace digest-pinned images during field work unless a fix depends on it.

## Current Image Pins

| Service | Image reference | Policy |
|---|---|---|
| `ai-control` base | `python:3.11-slim@sha256:9a7765b36773a37061455b332f18e265e7f58f6fea9c419a550d2a8b0e9db834` | Multi-arch digest pin for Python 3.11.15-slim-trixie. Rebuild and run Python validation before changing. |
| `dns` | `technitium/dns-server:latest@sha256:85c2...` | Digest pinned. Review upstream release notes before changing the digest. |
| `caddy` | `caddy:2@sha256:ec18ee54aab3315c22e25f3b2babda73ff8007d39b13b3bd1bfffa2f0444c7d9` | Multi-arch digest pin for Caddy 2.11.3-alpine. Refresh during monthly image review. |
| `llama` | `ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:5c92...` | Digest pinned. Update only after GPU smoke test and chat/RAG validation. |
| `chat` | `ghcr.io/open-webui/open-webui:main@sha256:fd69...` | Digest pinned but branch label is moving. Treat digest as authoritative. |
| `kiwix-en` / `kiwix-es` | `ghcr.io/kiwix/kiwix-serve:latest@sha256:e21b...` | Digest pinned. Update only after both ZIM routes are tested. |
| `kolibri` | `treehouses/kolibri:latest@sha256:a68b...` | Digest pinned. Update only after content library and login checks. |

## Monthly Review

1. On an online development machine, check upstream release notes for security fixes.
2. Pull candidate images by explicit digest.
3. Update `aibox/stack/docker-compose.yaml` in a branch.
4. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File aibox\tools\tests\compose_config_redacted.ps1
   docker compose -f aibox\stack\docker-compose.yaml up -d
   docker compose -f aibox\stack\docker-compose.yaml ps
   ```
5. Validate portal, AI chat, RAG, Kiwix English/Spanish, Kolibri, DNS, and hotspot access.
6. Export or cache the tested images before shipping to offline devices.

## Rollback

Keep the previous compose file and image cache until the new build has passed a full storage disaster drill and field-style startup. Roll back by restoring the previous digest references and running:

```powershell
docker compose -f aibox\stack\docker-compose.yaml up -d --no-recreate
```
