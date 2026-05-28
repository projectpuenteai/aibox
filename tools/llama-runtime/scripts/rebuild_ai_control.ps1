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

# Wait for ai-control to be reachable through Caddy and report portal_ok=true
# so the operator gets a clear ready signal before this script exits. The
# portal loading overlay would also surface this state in the browser, but
# without this CLI confirmation the operator has to refresh the tab to learn
# whether the new container actually came back up. /ai/api/health returns 503
# with the payload during warm-up (readiness_ok=false is the steady state on
# this hardware), so we accept any status code and inspect the body.
function Get-PortalHealthBody {
  param([int]$TimeoutSec = 5)
  $oldPref = $ProgressPreference
  $ProgressPreference = 'SilentlyContinue'
  try {
    try {
      $resp = Invoke-WebRequest -Uri "http://localhost/ai/api/health" -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
      return ($resp.Content | ConvertFrom-Json)
    } catch [System.Net.WebException] {
      if ($_.Exception.Response) {
        try {
          $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
          $bodyText = $reader.ReadToEnd()
          $reader.Close()
          return ($bodyText | ConvertFrom-Json)
        } catch { return $null }
      }
      return $null
    } catch {
      return $null
    }
  } finally {
    $ProgressPreference = $oldPref
  }
}

Write-Info "Waiting for ai-control to report portal_ok (up to 240 s)..."
$waitBudgetSec = 240
$progressEverySec = 30
$startedAt = Get-Date
$readyDeadline = $startedAt.AddSeconds($waitBudgetSec)
$readyConfirmed = $false
$lastProgressAt = $startedAt
while ((Get-Date) -lt $readyDeadline) {
  $body = Get-PortalHealthBody -TimeoutSec 5
  if ($body -and $body.portal_ok -eq $true) {
    $readyConfirmed = $true
    break
  }
  if (((Get-Date) - $lastProgressAt).TotalSeconds -ge $progressEverySec) {
    $elapsed = [int]((Get-Date) - $startedAt).TotalSeconds
    if ($body) {
      Write-Info ("  still warming ({0}s elapsed; status_reason={1})" -f $elapsed, $body.status_reason)
    } else {
      Write-Info ("  still warming ({0}s elapsed; backend not yet responding)" -f $elapsed)
    }
    $lastProgressAt = Get-Date
  }
  Start-Sleep -Seconds 5
}
if ($readyConfirmed) {
  $waited = [int]((Get-Date) - $startedAt).TotalSeconds
  Write-Ok ("ai-control reachable (portal_ok=true after {0}s)" -f $waited)
} else {
  Write-Warn ("ai-control did not report portal_ok within {0} s -- check 'docker logs aibox-ai-control'" -f $waitBudgetSec)
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
