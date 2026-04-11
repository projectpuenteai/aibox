# llama-runtime

Docker runtime tooling for `llama-server` used by the AIBox stack.

## Runtime Modes

- `prebuilt` (default): verifies pullability of `LLAMA_IMAGE` and uses official image at startup.
- `local`: requires local fallback image to exist.
- `auto`: picks `local` for `aibox/*` image names, otherwise `prebuilt`.

Set with `LLAMA_IMAGE_MODE`.

## Fast Path (default)

Default image:

- `ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:5c9266b4f92f1ab0d26dd0f2ede2e65d3853cad99ff86ba219db8fe6d464b995`

Use this for immediate reliable startup.

## Fallback Local Build (upstream recipe)

Build fallback image using llama.cpp's upstream CUDA Dockerfile and `server` target:

```powershell
powershell -ExecutionPolicy Bypass -File C:\AIBox\aibox\tools\llama-runtime\scripts\build_llama_image.ps1
```

Default pinned ref is `b8390` (override with `-LlamaCppRef`).

## Preflight

Checks:

- Docker daemon reachability/permissions
- image readiness by mode (`prebuilt` pullability or `local` image presence)
- NVIDIA runtime visibility in Docker
- model file existence

Run:

```powershell
powershell -ExecutionPolicy Bypass -File C:\AIBox\aibox\tools\llama-runtime\scripts\preflight_llama_runtime.ps1
```

## Guarded Startup

Runs preflight then `docker compose up -d`:

```powershell
powershell -ExecutionPolicy Bypass -File C:\AIBox\aibox\tools\llama-runtime\scripts\up_stack.ps1
```


