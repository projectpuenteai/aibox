# Runtime Control

AIBox runs `ai-control` without the Docker socket by default. This keeps the
portal and storage API from having host-level Docker control if the web service
or a dependency is compromised.

Default startup:

```powershell
docker compose -f aibox\stack\docker-compose.yaml up -d
```

In this mode, admin start/stop/restart controls for the llama container return a
503 response and `/v1/admin/status` reports `runtime_control_enabled=false`.
Health and readiness still use the llama HTTP endpoint.

If an operator-controlled deployment needs those runtime controls, opt in with
the override file:

```powershell
docker compose -f aibox\stack\docker-compose.yaml -f aibox\stack\docker-compose.runtime-control.yaml up -d
```

That override sets `RUNTIME_CONTROL_ENABLED=true` and mounts
`/var/run/docker.sock` into `ai-control`. Use it only on trusted machines.
