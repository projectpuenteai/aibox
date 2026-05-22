# Reload the Caddy reverse proxy after editing stack/Caddyfile.
#
# Caddy's Caddyfile is mounted into aibox-caddy read-only, so edits on the
# host are visible to the container immediately — but Caddy itself doesn't
# auto-reload, so `caddy reload` must be triggered manually. This script
# performs a zero-downtime reload (config is re-parsed and swapped in place;
# in-flight requests are not dropped) and falls back to `docker compose
# restart caddy` if the reload signal fails for any reason.
#
# Usage:
#   reload_caddy.ps1            # zero-downtime reload, fall back to restart
#   reload_caddy.ps1 -Restart   # skip reload, do a full container restart

param(
  [switch]$Restart
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir              # tools/llama-runtime
$toolsDir = Split-Path -Parent $runtimeDir              # tools
$aiboxDir = Split-Path -Parent $toolsDir                # aibox
$composeFile = Join-Path $aiboxDir "stack\docker-compose.yaml"
$caddyfile = Join-Path $aiboxDir "stack\Caddyfile"

if (-not (Test-Path $composeFile)) {
  throw "docker-compose.yaml not found at $composeFile"
}
if (-not (Test-Path $caddyfile)) {
  throw "Caddyfile not found at $caddyfile"
}

Write-Info "Validating Caddyfile..."
& docker exec aibox-caddy caddy validate --config /etc/caddy/Caddyfile
if ($LASTEXITCODE -ne 0) {
  throw "Caddyfile validation failed (exit code $LASTEXITCODE). Aborting reload."
}

if ($Restart) {
  Write-Info "Restarting aibox-caddy container..."
  & docker compose -f $composeFile restart caddy
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose restart failed (exit code $LASTEXITCODE)"
  }
  Write-Ok "Caddy restarted."
  exit 0
}

Write-Info "Zero-downtime reload of Caddy config..."
& docker exec aibox-caddy caddy reload --config /etc/caddy/Caddyfile
if ($LASTEXITCODE -ne 0) {
  Write-Warn "caddy reload failed (exit code $LASTEXITCODE); falling back to container restart."
  & docker compose -f $composeFile restart caddy
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose restart failed (exit code $LASTEXITCODE)"
  }
  Write-Ok "Caddy restarted."
  exit 0
}

Write-Ok "Caddy reloaded."
