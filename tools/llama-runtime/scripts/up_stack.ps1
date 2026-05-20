# This script optionally syncs WSL CPU settings, runs the llama runtime preflight, and then starts the selected Docker Compose services.
param(
  [string]$ComposeFile = "",
  [string[]]$Services = @(),
  [switch]$SkipGpuProbe,
  [switch]$SkipCpuSync,
  [switch]$SkipWslRestart,
  [switch]$FailOnHotspotCancel
)

. (Join-Path $PSScriptRoot 'lib\lib_io.ps1')

# ── Boot-log transcript (§3.12) ───────────────────────────────────────────────
# Capture stdout/stderr to %ProgramData%\AIBox\logs so the scheduled task at
# logon (which runs hidden) leaves a trail for post-mortem debugging.  Some
# hosts (e.g. ISE) already have a transcript running and Start-Transcript
# errors out — the try/catch + SilentlyContinue handles that gracefully.
$bootLogDir = Join-Path $env:ProgramData "AIBox\logs"
if (-not (Test-Path -LiteralPath $bootLogDir)) {
  New-Item -ItemType Directory -Path $bootLogDir -Force | Out-Null
}
$bootLogFile = Join-Path $bootLogDir ("up_stack_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
try { Start-Transcript -Path $bootLogFile -Append -ErrorAction SilentlyContinue | Out-Null } catch {}

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

function Test-DockerDaemon {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $null = & docker info 2>&1
    return ($LASTEXITCODE -eq 0)
  } finally {
    $ErrorActionPreference = $saved
  }
}

function Start-DockerDesktopIfNeeded {
  param(
    [int]$TimeoutSeconds = 300
  )

  if (Test-DockerDaemon) {
    Write-Host "[info] Docker daemon is already reachable."
    return
  }

  $dockerDesktopCandidates = @(
    (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe")
  ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

  $dockerDesktop = $dockerDesktopCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $dockerDesktop) {
    throw "Docker daemon is not reachable and Docker Desktop.exe was not found under Program Files."
  }

  Write-Host "[info] Docker daemon is not reachable. Launching Docker Desktop..."
  Start-Process -FilePath $dockerDesktop -WindowStyle Minimized | Out-Null
  Write-Host "[info] Waiting for Docker daemon to become reachable..."
  if (-not (Wait-DockerDaemon -TimeoutSeconds $TimeoutSeconds -IntervalSeconds 3)) {
    throw "Docker daemon did not become reachable within $TimeoutSeconds seconds after launching Docker Desktop."
  }
  Write-Host "[ok] Docker daemon is reachable."
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

function Get-DotEnvMap {
  param([string]$Path)

  $map = @{}
  if (-not (Test-Path $Path)) {
    return $map
  }

  foreach ($line in Get-Content $Path) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) {
      continue
    }

    $idx = $trimmed.IndexOf("=")
    if ($idx -lt 1) {
      continue
    }

    $key = $trimmed.Substring(0, $idx).Trim()
    $value = $trimmed.Substring($idx + 1).Trim()
    if ($value.StartsWith('"') -and $value.EndsWith('"') -and $value.Length -ge 2) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    $map[$key] = $value
  }

  return $map
}

function New-HexSecret {
  param([int]$Bytes = 32)

  $buffer = New-Object byte[] $Bytes
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($buffer)
  } finally {
    $rng.Dispose()
  }
  return ([BitConverter]::ToString($buffer) -replace "-", "").ToLowerInvariant()
}

function Set-DotEnvValue {
  param(
    [string]$Path,
    [string]$Name,
    [string]$Value
  )

  $line = "$Name=$Value"
  if (-not (Test-Path $Path)) {
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path $parent)) {
      New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Write-Utf8NoBom -Path $Path -Lines @("# AIBox local stack configuration", $line)
    return
  }

  $lines = @(Get-Content $Path)
  $updated = $false
  $pattern = "^\s*$([regex]::Escape($Name))\s*="
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ([string]$lines[$i] -match $pattern) {
      $lines[$i] = $line
      $updated = $true
      break
    }
  }

  if (-not $updated) {
    if ($lines.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace([string]$lines[-1])) {
      $lines += ""
    }
    $lines += "# Auto-generated by up_stack.ps1 for the local DNS admin UI."
    $lines += $line
  }

  Write-Utf8NoBom -Path $Path -Lines @($lines)
}

function Ensure-StartupEnv {
  param([string]$ComposeFilePath)

  $stackDir = Split-Path -Parent $ComposeFilePath
  $stackEnvPath = Join-Path $stackDir ".env"
  $stackEnv = Get-DotEnvMap -Path $stackEnvPath
  $dnsEnv = [Environment]::GetEnvironmentVariable("DNS_ADMIN_PASSWORD")

  if (-not [string]::IsNullOrWhiteSpace($dnsEnv)) {
    Write-Host "[info] DNS_ADMIN_PASSWORD supplied by process environment."
    return
  }

  if ($stackEnv.ContainsKey("DNS_ADMIN_PASSWORD") -and -not [string]::IsNullOrWhiteSpace($stackEnv["DNS_ADMIN_PASSWORD"])) {
    Write-Host "[info] DNS_ADMIN_PASSWORD already configured in stack/.env."
    return
  }

  $generated = New-HexSecret -Bytes 32
  Set-DotEnvValue -Path $stackEnvPath -Name "DNS_ADMIN_PASSWORD" -Value $generated
  Write-Host "[info] Generated DNS_ADMIN_PASSWORD and saved it to stack/.env."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$toolsDir = Split-Path -Parent $runtimeDir
$aiboxDir = Split-Path -Parent $toolsDir

if ([string]::IsNullOrWhiteSpace($ComposeFile)) {
  $ComposeFile = Join-Path $aiboxDir "stack\docker-compose.yaml"
}

Ensure-StartupEnv -ComposeFilePath $ComposeFile

Start-DockerDesktopIfNeeded -TimeoutSeconds 300

if (-not $SkipCpuSync) {
  $syncScript = Join-Path $scriptDir "sync_wsl_cpu.ps1"
  if (-not (Test-Path $syncScript)) {
    throw "CPU sync script not found: $syncScript"
  }

  Write-Host "[run] powershell -ExecutionPolicy Bypass -File $syncScript -EmitJson"
  # Capture both stdout and stderr via temp files. In Windows PowerShell 5.1
  # using `2>&1` on a native exe wraps stderr lines in ErrorRecord objects and
  # corrupts $?; Start-Process with -RedirectStandardError gives us the raw
  # bytes the child wrote to FD 2, which is what we want to surface in logs.
  $syncStdoutFile = Join-Path ([System.IO.Path]::GetTempPath()) ("aibox-syncwsl-out-" + [guid]::NewGuid().ToString() + ".log")
  $syncStderrFile = Join-Path ([System.IO.Path]::GetTempPath()) ("aibox-syncwsl-err-" + [guid]::NewGuid().ToString() + ".log")
  $syncOutput = ""
  $syncStderr = ""
  $syncExit = 1
  try {
    $syncProc = Start-Process `
      -FilePath "powershell.exe" `
      -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $syncScript, "-EmitJson") `
      -NoNewWindow `
      -Wait `
      -PassThru `
      -RedirectStandardOutput $syncStdoutFile `
      -RedirectStandardError $syncStderrFile
    $syncExit = $syncProc.ExitCode
    if (Test-Path -LiteralPath $syncStdoutFile) {
      $syncOutput = Get-Content -LiteralPath $syncStdoutFile -Raw
    }
    if (Test-Path -LiteralPath $syncStderrFile) {
      $syncStderr = Get-Content -LiteralPath $syncStderrFile -Raw
    }
    if (-not [string]::IsNullOrWhiteSpace($syncStderr)) {
      Write-Host "[stderr] sync_wsl_cpu.ps1 wrote to stderr:" -ForegroundColor Yellow
      foreach ($line in ($syncStderr -split "`r?`n")) {
        if (-not [string]::IsNullOrWhiteSpace($line)) {
          Write-Host "[stderr]   $line" -ForegroundColor Yellow
        }
      }
    }
    if ($syncExit -ne 0) {
      throw "CPU sync script failed (exit code $syncExit). stderr: $syncStderr"
    }
  } finally {
    if (Test-Path -LiteralPath $syncStdoutFile) {
      Remove-Item -LiteralPath $syncStdoutFile -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $syncStderrFile) {
      Remove-Item -LiteralPath $syncStderrFile -Force -ErrorAction SilentlyContinue
    }
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
# or named volumes), so it is safe to run unconditionally — this stops build
# cache and dangling layers from accumulating between stack restarts even
# when free disk is plentiful.
$cleanupScript = Join-Path $scriptDir "cleanup_docker_storage.ps1"
if (Test-Path $cleanupScript) {
  Write-Host "[info] Running routine Docker storage cleanup..."
  & powershell -ExecutionPolicy Bypass -File $cleanupScript -Apply -Quiet
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

# ── Spanish Chroma native-volume bootstrap ───────────────────────────────────
# Runtime mounts chroma_db_es_native at /chroma_db_es for faster HNSW loads on
# WSL2. Keep startup self-healing by creating/populating the volume from the
# repo-adjacent bind-mount source when needed.
$spanishChromaSource = Join-Path $backendDataDir "chroma_db_es"
$spanishChromaVolume = "chroma_db_es_native"
$volumeInspect = & docker volume inspect $spanishChromaVolume 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "[info] Creating Docker volume $spanishChromaVolume"
  & docker volume create $spanishChromaVolume | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Could not create Docker volume $spanishChromaVolume"
  }
}

$volumeProbeImage = if ($env:VOLUME_PROBE_IMAGE) { $env:VOLUME_PROBE_IMAGE } else { "caddy:2@sha256:ec18ee54aab3315c22e25f3b2babda73ff8007d39b13b3bd1bfffa2f0444c7d9" }
# Pre-pull the volume probe image when missing so preflight and bootstrap blocks
# can use it. Soft-fail when offline or pull is rate-limited.
& docker image inspect $volumeProbeImage *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "[info] Pulling volume probe image $volumeProbeImage"
  & docker pull $volumeProbeImage 2>&1 | ForEach-Object { Write-Host "  $_" }
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[warn] Volume probe image not pullable (offline or rate-limited); bootstrap will skip volume-content probes." -ForegroundColor Yellow
  }
}
$volumeHasCatalog = $false
$volumeProbeImagePresent = $false
& docker image inspect $volumeProbeImage *> $null
if ($LASTEXITCODE -eq 0) {
  $volumeProbeImagePresent = $true
  & docker run --rm -v "${spanishChromaVolume}:/dst:ro" $volumeProbeImage sh -c "test -f /dst/chroma.sqlite3" *> $null
  $volumeHasCatalog = ($LASTEXITCODE -eq 0)
}

function Test-RealFile {
  # Robust replacement for `Test-Path -PathType Leaf` that correctly handles
  # NTFS reparse points (junctions, symlinks). Returns $true only if the path
  # ultimately resolves to a real file on disk.
  param([string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    return $false
  }

  try {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      $target = $null
      try {
        $target = Get-Item -LiteralPath $Path -Force -ErrorAction Stop | Select-Object -ExpandProperty Target
      } catch {
        $target = $null
      }
      # .Target may be $null, a string, or a collection depending on PS edition.
      if ($target) {
        $targetPath = if ($target -is [System.Array]) { [string]$target[0] } else { [string]$target }
        if (-not [System.IO.Path]::IsPathRooted($targetPath)) {
          $parent = Split-Path -Parent $Path
          if ($parent) {
            $targetPath = Join-Path $parent $targetPath
          }
        }
        Write-Host "[info] Spanish Chroma catalog path '$Path' is a reparse point; following to '$targetPath'."
        try {
          $resolved = Get-Item -LiteralPath $targetPath -Force -ErrorAction Stop
          return (-not ($resolved.PSIsContainer))
        } catch {
          Write-Host "[warn] Could not resolve reparse target '$targetPath'; falling back to Test-Path -PathType Leaf." -ForegroundColor Yellow
          return (Test-Path -LiteralPath $Path -PathType Leaf)
        }
      }
      # Reparse point with no readable target — fall back.
      Write-Host "[warn] Reparse point '$Path' has no readable .Target; falling back to Test-Path -PathType Leaf." -ForegroundColor Yellow
      return (Test-Path -LiteralPath $Path -PathType Leaf)
    }
    return (-not ($item.PSIsContainer))
  } catch {
    return (Test-Path -LiteralPath $Path -PathType Leaf)
  }
}

$spanishChromaCatalogPath = Join-Path $spanishChromaSource "chroma.sqlite3"
if (-not $volumeProbeImagePresent) {
  Write-Host "[warn] Volume probe image $volumeProbeImage is not local; skipping Spanish Chroma volume copy." -ForegroundColor Yellow
} elseif (-not $volumeHasCatalog -and (Test-RealFile -Path $spanishChromaCatalogPath)) {
  Write-Host "[info] Populating $spanishChromaVolume from backend-data/chroma_db_es"
  & docker run --rm -v "${spanishChromaSource}:/src:ro" -v "${spanishChromaVolume}:/dst" $volumeProbeImage sh -c "cp -a /src/. /dst/"
  if ($LASTEXITCODE -ne 0) {
    throw "Could not populate $spanishChromaVolume from $spanishChromaSource"
  }
} elseif (-not $volumeHasCatalog) {
  Write-Host "[warn] $spanishChromaVolume is not populated and backend-data/chroma_db_es/chroma.sqlite3 is missing." -ForegroundColor Yellow
}
# ─────────────────────────────────────────────────────────────────────────────

# ── Kolibri native-volume bootstrap ──────────────────────────────────────────
# Kolibri was migrated from a bind-mount to a named volume on 2026-05-19 to
# avoid SQLite corruption on NTFS. Mirror the Spanish-Chroma self-healing
# pattern: ensure the volume exists, and seed it from the repo-adjacent
# bind-mount source when the volume is empty.
$kolibriDataSource = Join-Path $aiboxDir "kolibri-data"
$kolibriDataVolume = "kolibri_data_native"
$kolibriVolumeInspect = & docker volume inspect $kolibriDataVolume 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "[info] Creating Docker volume $kolibriDataVolume"
  & docker volume create $kolibriDataVolume | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Could not create Docker volume $kolibriDataVolume"
  }
}

$kolibriHasCatalog = $false
if ($volumeProbeImagePresent) {
  & docker run --rm -v "${kolibriDataVolume}:/dst:ro" $volumeProbeImage sh -c "test -f /dst/db.sqlite3" *> $null
  $kolibriHasCatalog = ($LASTEXITCODE -eq 0)
}

if (-not $volumeProbeImagePresent) {
  Write-Host "[warn] Volume probe image $volumeProbeImage is not local; skipping Kolibri volume copy." -ForegroundColor Yellow
} elseif (-not $kolibriHasCatalog -and (Test-Path -LiteralPath $kolibriDataSource -PathType Container) -and (@(Get-ChildItem -LiteralPath $kolibriDataSource -Force -ErrorAction SilentlyContinue).Count -gt 0)) {
  Write-Host "[info] Populating $kolibriDataVolume from kolibri-data/"
  & docker run --rm -v "${kolibriDataSource}:/src:ro" -v "${kolibriDataVolume}:/dst" $volumeProbeImage sh -c "cp -a /src/. /dst/"
  if ($LASTEXITCODE -ne 0) {
    throw "Could not populate $kolibriDataVolume from $kolibriDataSource"
  }
} elseif (-not $kolibriHasCatalog) {
  Write-Host "[warn] $kolibriDataVolume is not populated and bind-mount source ($kolibriDataSource) is empty/missing." -ForegroundColor Yellow
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

Write-Host "[info] Current compose service status:"
& docker compose -f $ComposeFile ps
if ($LASTEXITCODE -ne 0) {
  Write-Host "[warn] docker compose ps returned non-zero; service health summary unavailable." -ForegroundColor Yellow
}

# Try to bring up the offline hotspot after the stack is live so the local DNS
# service is already available when the hotspot validation runs.
$hotspotScript = Join-Path $scriptDir "setup_hotspot.ps1"
if ($FailOnHotspotCancel) {
  Write-Host "[info] Hotspot-cancel policy: STRICT (-FailOnHotspotCancel set; script will exit 1 if elevation is declined or hotspot does not come up)."
} else {
  Write-Host "[info] Hotspot-cancel policy: LENIENT (default; script will continue without offline networking if elevation is declined)."
}
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

  if ($FailOnHotspotCancel -and [string]$hotspotResult.status -ne "ready" -and [string]$hotspotResult.status -ne "ip_only") {
    Write-Host "[fatal] -FailOnHotspotCancel was set and hotspot did not reach a usable state (status='$([string]$hotspotResult.status)'). Exiting with code 1." -ForegroundColor Red
    exit 1
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

# ── Stop transcript + rotate boot logs (§3.12) ────────────────────────────────
# Best-effort: if Start-Transcript above failed (no-op in unsupported host),
# Stop-Transcript will also fail silently.  Windows flushes the transcript on
# process exit, so even if the script throws before reaching here the partial
# log is recoverable.
try { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null } catch {}
# Rotate: keep the 10 most recent boot logs.
try {
  Get-ChildItem -LiteralPath $bootLogDir -Filter "up_stack_*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 10 |
    Remove-Item -Force -ErrorAction SilentlyContinue
} catch {}
