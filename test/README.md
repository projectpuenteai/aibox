# Deprecated Compatibility Path

This folder is a compatibility shim.

Use `../stack/` as the canonical location for stack runtime files:

- `../stack/docker-compose.yaml`
- `../stack/Caddyfile`
- `../stack/portal/`

`test/docker-compose.yaml` remains runnable for backward compatibility, and points Caddy/portal mounts to `../stack/*`.

For guarded startup with image/GPU/model preflight checks, use:

```powershell
powershell -ExecutionPolicy Bypass -File C:\AIBox\aibox\tools\llama-runtime\scripts\up_stack.ps1
```
