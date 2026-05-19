# Operator Safe Commands

Commands assume PowerShell from `C:\AIBox`.

## Daily Safe Commands

Use these for normal startup, shutdown, and status checks:

```powershell
powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\preflight_llama_runtime.ps1
powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\up_stack.ps1
docker compose -f .\aibox\stack\docker-compose.yaml ps
docker compose -f .\aibox\stack\docker-compose.yaml logs --tail=120 ai-control
powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\down_stack.ps1
```

For support handoffs, prefer redacted validation output:

```powershell
powershell -ExecutionPolicy Bypass -File .\aibox\tools\tests\compose_config_redacted.ps1
```

Do not paste raw `docker compose config` output. It expands secrets from
`stack/.env`, including admin and encryption values.

## Backup And Restore Checks

Run before risky maintenance or cleanup:

```powershell
py -3 .\aibox\tools\storage\verify_storage_backup.py --appdata-root .\aibox\backend-data\appdata
```

For encryption-key rotation, follow `SECURITY_RUNBOOK.md` exactly. Never rotate
the key without a snapshot and a successful dry-run.

## Cleanup Commands

Dry-run is safe and should be the default:

```powershell
powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\cleanup_docker_storage.ps1
```

Apply mode is risky and should only be run after the dry-run output is reviewed:

```powershell
powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\cleanup_docker_storage.ps1 -Apply
```

## Risky Commands

Use only when an operator has a current backup and understands the impact:

```powershell
docker compose -f .\aibox\stack\docker-compose.yaml down
docker compose -f .\aibox\stack\docker-compose.yaml up -d --build
docker compose -f .\aibox\stack\docker-compose.yaml pull
py -3 .\aibox\tools\storage\rotate_encryption_key.py --users-root .\aibox\backend-data\appdata\users --backup-root .\aibox\backend-data\appdata\backups\key-rotation --manifest .\aibox\backend-data\appdata\backups\key-rotation\last-rotation.json --stop-on-error
```

## Never Run On Field Devices

These commands can delete offline data, models, databases, or image caches:

```powershell
docker compose -f .\aibox\stack\docker-compose.yaml down --volumes
docker system prune --all --volumes
docker volume prune
Remove-Item -Recurse -Force .\aibox\backend-data
Remove-Item -Recurse -Force .\aibox\models
Remove-Item -Recurse -Force .\aibox\kiwix
git reset --hard
```
