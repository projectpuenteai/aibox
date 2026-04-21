# This script optionally syncs WSL CPU settings, runs the llama runtime preflight, and then starts the selected Docker Compose services.
param(
  [string]$ComposeFile = "",
  [string[]]$Services = @(),
  [switch]$SkipGpuProbe,
  [switch]$SkipCpuSync,
  [switch]$SkipWslRestart
)

$ErrorActionPreference = "Stop"

function Wait-DockerDaemon {
  param(
    [int]$TimeoutSeconds = 240,
    [int]$IntervalSeconds = 3
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $null = & docker info 2>&1
      $code = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $saved
    }

    if ($code -eq 0) {
      return $true
    }

    Start-Sleep -Seconds $IntervalSeconds
  }

  return $false
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$toolsDir = Split-Path -Parent $runtimeDir
$aiboxDir = Split-Path -Parent $toolsDir

if ([string]::IsNullOrWhiteSpace($ComposeFile)) {
  $ComposeFile = Join-Path $aiboxDir "stack\docker-compose.yaml"
}

if (-not $SkipCpuSync) {
  $syncScript = Join-Path $scriptDir "sync_wsl_cpu.ps1"
  if (-not (Test-Path $syncScript)) {
    throw "CPU sync script not found: $syncScript"
  }

  Write-Host "[run] powershell -ExecutionPolicy Bypass -File $syncScript -EmitJson"
  $syncOutput = & powershell -ExecutionPolicy Bypass -File $syncScript -EmitJson
  if ($LASTEXITCODE -ne 0) {
    throw "CPU sync script failed (exit code $LASTEXITCODE)"
  }

  try {
    $syncResult = $syncOutput | ConvertFrom-Json
  } catch {
    throw "CPU sync script produced invalid JSON output: $syncOutput"
  }

  if ($syncResult.changed -and -not $SkipWslRestart) {
    Write-Host "[info] .wslconfig updated ($($syncResult.configured_processors_before) -> $($syncResult.configured_processors_after)); restarting WSL..."
    & wsl --shutdown
    if ($LASTEXITCODE -ne 0) {
      throw "wsl --shutdown failed (exit code $LASTEXITCODE)"
    }

    Write-Host "[info] Waiting for Docker daemon to recover..."
    if (-not (Wait-DockerDaemon -TimeoutSeconds 300 -IntervalSeconds 3)) {
      throw "Docker daemon did not recover after WSL restart within timeout."
    }
    Write-Host "[info] Docker daemon is back online."
  } elseif ($syncResult.changed) {
    Write-Host "[warn] .wslconfig updated but restart was skipped; CPU allocation change will apply after next WSL restart."
  } else {
    Write-Host "[info] WSL CPU allocation already aligned with host logical CPUs ($($syncResult.host_logical_cpus))."
  }
}

# ── Docker storage cleanup (safe offline prune) ───────────────────────────────
# Cleans stopped containers, dangling image layers, and build cache.
# Only removes items that cannot be pulled back (never removes tagged images
# or named volumes). Triggers when free disk space falls below 15 GB.
$cleanupScript = Join-Path $scriptDir "cleanup_docker_storage.ps1"
if (Test-Path $cleanupScript) {
  Write-Host "[info] Checking Docker storage (threshold: 15 GB free)..."
  & powershell -ExecutionPolicy Bypass -File $cleanupScript -ThresholdGB 15
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[warn] Docker storage cleanup returned a non-zero exit code; continuing anyway." -ForegroundColor Yellow
  }
} else {
  Write-Host "[warn] cleanup_docker_storage.ps1 not found; skipping storage check." -ForegroundColor Yellow
}

# ── One-time backend-data layout migration ────────────────────────────────────
# docker-compose now mounts backend-data/appdata → /data (instead of the old
# backend-data → /data).  This block migrates existing deployments by moving
# user-data subdirectories into the new appdata/ subdirectory.  It is
# idempotent: if appdata/ already contains a given directory it is skipped.
#
# Directories that stay at backend-data/ root (NOT moved):
#   ai-control/   → /state
#   chroma_db/    → /chroma_db
#   chroma_db_es/ → /chroma_db_es
#   llama/        → /tmp/llama
$backendDataDir = Join-Path $aiboxDir "backend-data"
$appdataDir     = Join-Path $backendDataDir "appdata"

# Subdirectories that belong under appdata/
$migrateNames = @("db", "users", "tmp", "security")

foreach ($name in $migrateNames) {
  $oldPath = Join-Path $backendDataDir $name
  $newPath = Join-Path $appdataDir     $name

  if ((Test-Path $oldPath) -and -not (Test-Path $newPath)) {
    Write-Host "[migrate] backend-data/$name → backend-data/appdata/$name"
    # Ensure parent exists before moving
    if (-not (Test-Path $appdataDir)) {
      New-Item -ItemType Directory -Path $appdataDir -Force | Out-Null
    }
    Move-Item -Path $oldPath -Destination $newPath -ErrorAction Stop
    Write-Host "[migrate] Done: $name"
  } elseif ((Test-Path $oldPath) -and (Test-Path $newPath)) {
    Write-Host "[migrate] Skipping $name — already at appdata/$name"
  }
}

# Ensure appdata/ directory exists for a fresh install (app creates its own
# subdirs on first startup, but Docker needs the mountpoint to exist).
if (-not (Test-Path $appdataDir)) {
  New-Item -ItemType Directory -Path $appdataDir -Force | Out-Null
  Write-Host "[info] Created backend-data/appdata/ for fresh install."
}
# ─────────────────────────────────────────────────────────────────────────────

$preflight = Join-Path $scriptDir "preflight_llama_runtime.ps1"
$preflightArgs = @("-ExecutionPolicy", "Bypass", "-File", $preflight, "-ComposeFile", $ComposeFile)
if ($SkipGpuProbe) {
  $preflightArgs += "-SkipGpuProbe"
}

& powershell @preflightArgs
if ($LASTEXITCODE -ne 0) {
  throw "Preflight failed. Aborting docker compose up."
}

$cmd = @("compose", "-f", $ComposeFile, "up", "-d")
if ($Services.Count -gt 0) {
  $cmd += $Services
}

Write-Host "[run] docker $($cmd -join ' ')"
& docker @cmd
if ($LASTEXITCODE -ne 0) {
  throw "docker compose up failed (exit code $LASTEXITCODE)"
}

Write-Host "[ok] stack started" -ForegroundColor Green

# ── Update portal connection info (best-effort, non-fatal) ────────────────────
# Writes portal/network-info.json so connect.html shows current IPs / hotspot
# status without needing a live API call.  Does not require elevation.
$netInfoScript = Join-Path $scriptDir "get_network_info.ps1"
if (Test-Path $netInfoScript) {
  Write-Host "[info] Refreshing network info for portal..."
  & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[warn] get_network_info.ps1 returned non-zero; portal connection info may be stale." -ForegroundColor Yellow
  }
} else {
  Write-Host "[warn] get_network_info.ps1 not found; portal connection info not updated." -ForegroundColor Yellow
}

