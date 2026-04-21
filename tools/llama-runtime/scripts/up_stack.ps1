# This script optionally syncs WSL CPU settings, runs the llama runtime preflight, and then starts the selected Docker Compose services.
param(
  [string]$ComposeFile = "",
  [string[]]$Services = @(),
  [switch]$SkipGpuProbe,
  [switch]$SkipCpuSync,
  [switch]$SkipWslRestart
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-HotspotStartup {
  param([string]$ScriptPath)

  if (-not (Test-Path $ScriptPath)) {
    return [pscustomobject]@{
      ok = $false
      status = "unavailable"
      warnings = @("setup_hotspot.ps1 not found; skipping hotspot startup.")
      errors = @()
    }
  }

  $jsonFile = Join-Path ([System.IO.Path]::GetTempPath()) ("aibox-hotspot-" + [guid]::NewGuid().ToString() + ".json")
  try {
    if (Test-IsAdministrator) {
      & powershell -ExecutionPolicy Bypass -File $ScriptPath -EmitJson -JsonOutFile $jsonFile | Out-Null
    } else {
      try {
        $proc = Start-Process `
          -FilePath "powershell.exe" `
          -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", $ScriptPath, "-EmitJson", "-JsonOutFile", $jsonFile) `
          -Verb RunAs `
          -Wait `
          -PassThru
        $null = $proc
      } catch {
        return [pscustomobject]@{
          ok = $false
          status = "unavailable"
          warnings = @("Hotspot startup requires Administrator approval. Startup continued without offline Wi-Fi access.")
          errors = @("Elevation was cancelled or blocked before hotspot setup could run.")
        }
      }
    }

    if (Test-Path $jsonFile) {
      try {
        return (Get-Content $jsonFile -Raw | ConvertFrom-Json)
      } catch {
        return [pscustomobject]@{
          ok = $false
          status = "unavailable"
          warnings = @("Hotspot setup ran, but its status output could not be parsed.")
          errors = @($_.Exception.Message)
        }
      }
    }

    return [pscustomobject]@{
      ok = $false
      status = "unavailable"
      warnings = @("Hotspot setup did not produce status output. Startup continued without confirmed offline Wi-Fi access.")
      errors = @()
    }
  } finally {
    if (Test-Path $jsonFile) {
      Remove-Item -LiteralPath $jsonFile -Force -ErrorAction SilentlyContinue
    }
  }
}

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

function Get-ComposeExistingServices {
  param([string]$ComposeFilePath)

  $existing = @()
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & docker compose -f $ComposeFilePath ps --services --all 2>&1
    if ($LASTEXITCODE -eq 0) {
      $existing = @(
        $output |
          Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
          ForEach-Object { ([string]$_).Trim() }
      )
    }
  } finally {
    $ErrorActionPreference = $saved
  }

  return @($existing | Select-Object -Unique)
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

$desiredServices = @($Services)
$existingServices = @(Get-ComposeExistingServices -ComposeFilePath $ComposeFile)
$canUseStart = ($desiredServices.Count -eq 0 -and $existingServices.Count -gt 0)
if ($desiredServices.Count -gt 0) {
  $missingServices = @($desiredServices | Where-Object { $_ -notin $existingServices })
  $canUseStart = ($missingServices.Count -eq 0)
}

if ($canUseStart) {
  $cmd = @("compose", "-f", $ComposeFile, "start")
  if ($desiredServices.Count -gt 0) {
    $cmd += $desiredServices
  }
  Write-Host "[run] docker $($cmd -join ' ')"
  & docker @cmd
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose start failed (exit code $LASTEXITCODE)"
  }
} else {
  $cmd = @("compose", "-f", $ComposeFile, "up", "-d", "--no-recreate")
  if ($desiredServices.Count -gt 0) {
    $cmd += $desiredServices
  }
  Write-Host "[run] docker $($cmd -join ' ')"
  & docker @cmd
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose up --no-recreate failed (exit code $LASTEXITCODE)"
  }
}

Write-Host "[ok] stack started" -ForegroundColor Green

# Try to bring up the offline hotspot after the stack is live so the local DNS
# service is already available when the hotspot validation runs.
$hotspotScript = Join-Path $scriptDir "setup_hotspot.ps1"
Write-Host "[info] Starting offline hotspot..."
$hotspotResult = Invoke-HotspotStartup -ScriptPath $hotspotScript
if ($hotspotResult) {
  foreach ($hotspotWarning in @($hotspotResult.warnings)) {
    if (-not [string]::IsNullOrWhiteSpace([string]$hotspotWarning)) {
      Write-Host "[warn] $hotspotWarning" -ForegroundColor Yellow
    }
  }
  foreach ($hotspotError in @($hotspotResult.errors)) {
    if (-not [string]::IsNullOrWhiteSpace([string]$hotspotError)) {
      Write-Host "[warn] $hotspotError" -ForegroundColor Yellow
    }
  }

  switch ([string]$hotspotResult.status) {
    "ready" {
      Write-Host "[ok] Hotspot ready for offline clients at http://$($hotspotResult.domain)/" -ForegroundColor Green
    }
    "ip_only" {
      Write-Host "[warn] Hotspot is active, but offline DNS is not ready. Clients should use http://$($hotspotResult.host_ip)/ until puente.link validates." -ForegroundColor Yellow
    }
    default {
      Write-Host "[warn] Hotspot is not ready. Stack startup completed, but offline student access is unavailable." -ForegroundColor Yellow
    }
  }
}

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
