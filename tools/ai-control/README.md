# ai-control

Lightweight Docker-native control API for llama lifecycle and override state.

## Endpoints (behind Caddy `/ai/api/v1/admin/*`)

- `GET /status`
- `GET /health`
- `GET /ai-enabled`
- `POST /ai-enabled`
- `POST /runtime/start`
- `POST /runtime/stop`
- `POST /runtime/restart`
- `POST /runtime/clear-override`

Override modes:

- `auto`
- `forced_on`
- `forced_off`

Persistent state file:

- `/state/control_state.json`
