# Rebuilds the ai-control image, restarts the container, and prunes the
# orphaned dangling image and build cache layers left behind by the build.
#
# Use this instead of running `docker compose build ai-control` directly so
# that storage stays bounded: each unpruned rebuild leaves a ~2 GB dangling
# image plus build-cache layers behind, which add up fast across iterations.
#
# Usage:
#   rebuild_ai_control.ps1            # build + up + safe prune
#   rebuild_ai_control.ps1 -NoCache   # force a full rebuild (ignores cache)
#   rebuild_ai_control.ps1 -SkipPrune # build + up only (skip cleanup)
param(
  [switch]$NoCache,
  [switch]$SkipPrune
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir              # tools/llama-runtime
$toolsDir = Split-Path -Parent $runtimeDir                # tools
$aiboxDir = Split-Path -Parent $toolsDir                  # aibox
$composeFile = Join-Path $aiboxDir "stack\docker-compose.yaml"

if (-not (Test-Path $composeFile)) {
  throw "docker-compose.yaml not found at $composeFile"
}

$buildArgs = @("compose", "-f", $composeFile, "build")
if ($NoCache) { $buildArgs += "--no-cache" }
$buildArgs += "ai-control"

Write-Info "Building ai-control image..."
& docker @buildArgs
if ($LASTEXITCODE -ne 0) {
  throw "docker compose build failed (exit code $LASTEXITCODE)"
}

Write-Info "Restarting ai-control container..."
& docker compose -f $composeFile up -d ai-control
if ($LASTEXITCODE -ne 0) {
  throw "docker compose up failed (exit code $LASTEXITCODE)"
}

if ($SkipPrune) {
  Write-Ok "Rebuild complete (prune skipped)."
  exit 0
}

# Reap the dangling parent image and unused build cache that this rebuild
# just orphaned. cleanup_docker_storage.ps1 is explicitly safe — it refuses
# to touch named volumes or tagged images, so it cannot remove anything the
# stack needs to keep running offline.
$cleanupScript = Join-Path $scriptDir "cleanup_docker_storage.ps1"
if (Test-Path $cleanupScript) {
  Write-Info "Pruning dangling images and build cache..."
  & powershell -ExecutionPolicy Bypass -File $cleanupScript -Apply -Quiet
  if ($LASTEXITCODE -ne 0) {
    Write-Warn "Cleanup returned exit code $LASTEXITCODE; continuing."
  }
} else {
  Write-Warn "cleanup_docker_storage.ps1 not found; skipping prune."
}

Write-Ok "Rebuild complete."
