# This script optionally syncs WSL CPU settings, runs the llama runtime preflight, and then starts the selected Docker Compose services.
param(
  [string]$ComposeFile = "",
  [string[]]$Services = @(),
  [switch]$SkipGpuProbe,
  [switch]$SkipCpuSync,
  [switch]$SkipWslRestart,
  [switch]$FailOnHotspotCancel,
  # Bypass Mobile Hotspot bring-up (developer iteration).
  [switch]$SkipHotspot,
  # Force recreate containers (apply .env changes that would otherwise be ignored).
  [switch]$Recreate
)

. (Join-Path $PSScriptRoot 'lib\lib_io.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_env.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

$script:EnvDefaultsPath = Join-Path $PSScriptRoot '..\..\..\stack\.env.defaults'
$script:EnvDefaults = if (Test-Path -LiteralPath $script:EnvDefaultsPath) { Get-DotEnvMap -Path $script:EnvDefaultsPath } else { @{} }

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
  $emittedError = $false
  while ((Get-Date) -lt $deadline) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $errorOutput = & docker info 2>&1 | Out-String
      $code = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $saved
    }

    if ($code -eq 0) {
      return $true
    }

    if (-not $emittedError -and $errorOutput) {
      $firstLine = ($errorOutput -split "`r?`n") | Where-Object { $_ -match '\S' } | Select-Object -First 1
      if ($firstLine) {
        Write-Warn "docker info: $firstLine"
        $emittedError = $true
      }
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
    Write-Info "Docker daemon is already reachable."
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

  Write-Info "Docker daemon is not reachable. Launching Docker Desktop..."
  Start-Process -FilePath $dockerDesktop -WindowStyle Minimized | Out-Null
  Write-Info "Waiting for Docker daemon to become reachable..."
  if (-not (Wait-DockerDaemon -TimeoutSeconds $TimeoutSeconds -IntervalSeconds 3)) {
    throw "Docker daemon did not become reachable within $TimeoutSeconds seconds after launching Docker Desktop."
  }
  Write-Ok "Docker daemon is reachable."
}

function Get-ComposeExistingServices {
  param([string]$ComposeFilePath)

  # §3.4: separate stdout/stderr via Start-Process so warning lines from docker
  # (e.g. credential-helper noise on stderr) cannot pollute the service-name
  # list parsed downstream.
  $stdoutFile = [IO.Path]::GetTempFileName()
  $stderrFile = [IO.Path]::GetTempFileName()
  try {
    $proc = Start-Process -FilePath "docker" `
      -ArgumentList @("compose","-f",$ComposeFilePath,"ps","--services","--all") `
      -NoNewWindow -Wait -PassThru `
      -RedirectStandardOutput $stdoutFile `
      -RedirectStandardError $stderrFile
    if ($proc.ExitCode -ne 0) {
      return @()
    }
    $lines = @(
      Get-Content -LiteralPath $stdoutFile |
        Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
        ForEach-Object { ([string]$_).Trim() }
    )
    return @($lines | Select-Object -Unique)
  } finally {
    Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
  }
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
    Write-Info "DNS_ADMIN_PASSWORD supplied by process environment."
    return
  }

  if ($stackEnv.ContainsKey("DNS_ADMIN_PASSWORD") -and -not [string]::IsNullOrWhiteSpace($stackEnv["DNS_ADMIN_PASSWORD"])) {
    Write-Info "DNS_ADMIN_PASSWORD already configured in stack/.env."
    return
  }

  $generated = New-HexSecret -Bytes 32
  Set-DotEnvValue -Path $stackEnvPath -Name "DNS_ADMIN_PASSWORD" -Value $generated
  Write-Info "Generated DNS_ADMIN_PASSWORD and saved it to stack/.env."
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

  Write-Run "powershell -ExecutionPolicy Bypass -File $syncScript -EmitJson"
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
      Write-Warn "sync_wsl_cpu.ps1 wrote to stderr:"
      foreach ($line in ($syncStderr -split "`r?`n")) {
        if (-not [string]::IsNullOrWhiteSpace($line)) {
          Write-Warn "  $line"
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
    Write-Info ".wslconfig updated ($($syncResult.configured_processors_before) -> $($syncResult.configured_processors_after)); restarting WSL..."
    try {
      $runningDistros = & wsl --list --running --quiet 2>$null
      if ($LASTEXITCODE -eq 0 -and $runningDistros) {
        $otherDistros = $runningDistros | Where-Object { $_ -and ($_ -notmatch '^docker-desktop') } | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        if ($otherDistros.Count -gt 0) {
          Write-Warn "WSL --shutdown is about to terminate these running distros (unsaved work will be lost):"
          foreach ($d in $otherDistros) { Write-Warn "  - $d" }
          Write-Warn "Sleeping 5 seconds; press Ctrl+C to abort."
          Start-Sleep -Seconds 5
        }
      }
    } catch {
      # Best-effort warning; don't fail the WSL sync over a query glitch.
    }
    & wsl --shutdown
    if ($LASTEXITCODE -ne 0) {
      throw "wsl --shutdown failed (exit code $LASTEXITCODE)"
    }

    Write-Info "Waiting for Docker daemon to recover..."
    if (-not (Wait-DockerDaemon -TimeoutSeconds 300 -IntervalSeconds 3)) {
      throw "Docker daemon did not recover after WSL restart within timeout."
    }
    Write-Info "Docker daemon is back online."
  } elseif ($syncResult.changed) {
    Write-Warn ".wslconfig updated but restart was skipped; CPU allocation change will apply after next WSL restart."
  } else {
    Write-Info "WSL CPU allocation already aligned with host logical CPUs ($($syncResult.host_logical_cpus))."
  }
}

# ── One-time backend-data layout migration ────────────────────────────────────
# docker-compose now mounts backend-data/appdata → /data (instead of the old
# backend-data → /data).  This block migrates existing deployments by moving
# user-data subdirectories into the new appdata/ subdirectory.  It is
# idempotent: if appdata/ already contains a given directory it is skipped.
#
# §3.5: gated by a sentinel file so we stop printing "[migrate] Skipping ..."
# lines on every boot once the migration has been completed.
#
# Directories that stay at backend-data/ root (NOT moved):
#   ai-control/   → /state
#   chroma_db/    → /chroma_db
#   chroma_db_es/ → /chroma_db_es
#   llama/        → /tmp/llama
$backendDataDir = Join-Path $aiboxDir "backend-data"
$appdataDir     = Join-Path $backendDataDir "appdata"
$migrationSentinel = Join-Path $backendDataDir ".migrations\appdata-layout.done"

if (-not (Test-Path -LiteralPath $migrationSentinel)) {
  # Subdirectories that belong under appdata/
  $migrateNames = @("db", "users", "tmp", "security")

  foreach ($name in $migrateNames) {
    $oldPath = Join-Path $backendDataDir $name
    $newPath = Join-Path $appdataDir     $name

    if ((Test-Path $oldPath) -and -not (Test-Path $newPath)) {
      Write-Info "backend-data/$name → backend-data/appdata/$name"
      # Ensure parent exists before moving
      if (-not (Test-Path $appdataDir)) {
        New-Item -ItemType Directory -Path $appdataDir -Force | Out-Null
      }
      Move-Item -Path $oldPath -Destination $newPath -ErrorAction Stop
      Write-Info "Done: $name"
    } elseif ((Test-Path $oldPath) -and (Test-Path $newPath)) {
      Write-Info "Skipping $name — already at appdata/$name"
    }
  }

  # Ensure appdata/ directory exists for a fresh install (app creates its own
  # subdirs on first startup, but Docker needs the mountpoint to exist).
  if (-not (Test-Path $appdataDir)) {
    New-Item -ItemType Directory -Path $appdataDir -Force | Out-Null
    Write-Info "Created backend-data/appdata/ for fresh install."
  }

  # Drop the sentinel so this block stays silent on subsequent boots.
  $sentinelDir = Split-Path -Parent $migrationSentinel
  if (-not (Test-Path -LiteralPath $sentinelDir)) {
    New-Item -ItemType Directory -Path $sentinelDir -Force | Out-Null
  }
  New-Item -ItemType File -Path $migrationSentinel -Force | Out-Null
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
  Write-Info "Creating Docker volume $spanishChromaVolume"
  & docker volume create $spanishChromaVolume | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Could not create Docker volume $spanishChromaVolume"
  }
}

$volumeProbeImage = if ($env:VOLUME_PROBE_IMAGE) {
  $env:VOLUME_PROBE_IMAGE
} elseif ($script:EnvDefaults.ContainsKey('VOLUME_PROBE_IMAGE')) {
  $script:EnvDefaults['VOLUME_PROBE_IMAGE']
} else {
  "caddy:2@sha256:ec18ee54aab3315c22e25f3b2babda73ff8007d39b13b3bd1bfffa2f0444c7d9"
}
# Pre-pull the volume probe image when missing so preflight and bootstrap blocks
# can use it. Soft-fail when offline or pull is rate-limited.
& docker image inspect $volumeProbeImage *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Info "Pulling volume probe image $volumeProbeImage"
  & docker pull $volumeProbeImage 2>&1 | ForEach-Object { Write-Host "  $_" }
  if ($LASTEXITCODE -ne 0) {
    Write-Warn "Volume probe image not pullable (offline or rate-limited); bootstrap will skip volume-content probes."
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
  # NTFS reparse points. Returns $true only if the path ultimately resolves
  # to a real file on disk.
  param([string]$Path)
  try {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  } catch {
    return $false
  }
  if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    try {
      $resolved = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path -ErrorAction Stop))
      return (Test-Path -LiteralPath $resolved -PathType Leaf)
    } catch {
      return $false
    }
  }
  return (-not $item.PSIsContainer)
}

$spanishChromaCatalogPath = Join-Path $spanishChromaSource "chroma.sqlite3"
if (-not $volumeProbeImagePresent) {
  Write-Warn "Volume probe image $volumeProbeImage is not local; skipping Spanish Chroma volume copy."
} elseif (-not $volumeHasCatalog -and (Test-RealFile -Path $spanishChromaCatalogPath)) {
  Write-Info "Populating $spanishChromaVolume from backend-data/chroma_db_es"
  & docker run --rm -v "${spanishChromaSource}:/src:ro" -v "${spanishChromaVolume}:/dst" $volumeProbeImage sh -c "cd /src && tar cf - . | tar xf - -C /dst"
  if ($LASTEXITCODE -ne 0) {
    throw "Could not populate $spanishChromaVolume from $spanishChromaSource"
  }
} elseif (-not $volumeHasCatalog) {
  Write-Warn "$spanishChromaVolume is not populated and backend-data/chroma_db_es/chroma.sqlite3 is missing."
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
  Write-Info "Creating Docker volume $kolibriDataVolume"
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
  Write-Warn "Volume probe image $volumeProbeImage is not local; skipping Kolibri volume copy."
} elseif (-not $kolibriHasCatalog -and (Test-Path -LiteralPath $kolibriDataSource -PathType Container) -and (@(Get-ChildItem -LiteralPath $kolibriDataSource -Force -ErrorAction SilentlyContinue).Count -gt 0)) {
  Write-Info "Populating $kolibriDataVolume from kolibri-data/"
  & docker run --rm -v "${kolibriDataSource}:/src:ro" -v "${kolibriDataVolume}:/dst" $volumeProbeImage sh -c "cd /src && tar cf - . | tar xf - -C /dst"
  if ($LASTEXITCODE -ne 0) {
    throw "Could not populate $kolibriDataVolume from $kolibriDataSource"
  }
} elseif (-not $kolibriHasCatalog) {
  Write-Warn "$kolibriDataVolume is not populated and bind-mount source ($kolibriDataSource) is empty/missing."
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
  Write-Run "docker $($cmd -join ' ')"
  & docker @cmd
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose start failed (exit code $LASTEXITCODE)"
  }
} else {
  # §3.10: allow operator to override the default --no-recreate (which preserves
  # existing containers and silently ignores .env changes) with --force-recreate
  # when they actually want changes applied.
  $cmd = @("compose", "-f", $ComposeFile, "up", "-d")
  if ($Recreate) {
    $cmd += "--force-recreate"
  } else {
    $cmd += "--no-recreate"
  }
  if ($desiredServices.Count -gt 0) {
    $cmd += $desiredServices
  }
  Write-Run "docker $($cmd -join ' ')"
  & docker @cmd
  if ($LASTEXITCODE -ne 0) {
    $recreateMode = if ($Recreate) { "--force-recreate" } else { "--no-recreate" }
    throw "docker compose up $recreateMode failed (exit code $LASTEXITCODE)"
  }
}

Write-Ok "stack started"

Write-Info "Current compose service status:"
# §3.6: emit one colored line per service via JSON parse instead of the default
# table formatter so health states are obvious at a glance.
try {
  $psJson = & docker compose -f $ComposeFile ps --format json 2>$null
  if ($LASTEXITCODE -eq 0 -and $psJson) {
    # docker compose ps --format json emits one JSON object per line (ndjson).
    $services = @(
      $psJson |
        ForEach-Object { try { $_ | ConvertFrom-Json } catch { $null } } |
        Where-Object { $_ }
    )
    foreach ($svc in $services) {
      $state = $svc.State
      $health = $svc.Health
      $statusLine = "$($svc.Name) $state"
      if ($health) { $statusLine += " ($health)" }
      if ($state -eq 'running' -and ($health -eq 'healthy' -or -not $health)) {
        Write-Ok   $statusLine
      } elseif ($state -eq 'running') {
        Write-Warn $statusLine
      } else {
        Write-Err  $statusLine
      }
    }
  } else {
    & docker compose -f $ComposeFile ps   # fallback to default format
    if ($LASTEXITCODE -ne 0) {
      Write-Warn "docker compose ps returned non-zero; service health summary unavailable."
    }
  }
} catch {
  & docker compose -f $ComposeFile ps       # fallback on parse errors
  if ($LASTEXITCODE -ne 0) {
    Write-Warn "docker compose ps returned non-zero; service health summary unavailable."
  }
}

# Prune dangling layers from any rebuild — runs after compose start so live containers are reused.
$cleanupScript = Join-Path $scriptDir "cleanup_docker_storage.ps1"
if (Test-Path $cleanupScript) {
  Write-Info "Running routine Docker storage cleanup..."
  & powershell -ExecutionPolicy Bypass -File $cleanupScript -Apply -Quiet
  if ($LASTEXITCODE -ne 0) {
    Write-Warn "Docker storage cleanup returned a non-zero exit code; continuing anyway."
  }
} else {
  Write-Warn "cleanup_docker_storage.ps1 not found; skipping storage check."
}

if (-not $SkipHotspot) {
  # Try to bring up the offline hotspot after the stack is live so the local DNS
  # service is already available when the hotspot validation runs.
  $hotspotScript = Join-Path $scriptDir "setup_hotspot.ps1"
  if ($FailOnHotspotCancel) {
    Write-Info "Hotspot-cancel policy: STRICT (-FailOnHotspotCancel set; script will exit 1 if elevation is declined or hotspot does not come up)."
  } else {
    Write-Info "Hotspot-cancel policy: LENIENT (default; script will continue without offline networking if elevation is declined)."
  }
  Write-Info "Starting offline hotspot..."
  $hotspotResult = Invoke-HotspotStartup -ScriptPath $hotspotScript
  if ($hotspotResult) {
    foreach ($hotspotWarning in @($hotspotResult.warnings)) {
      if (-not [string]::IsNullOrWhiteSpace([string]$hotspotWarning)) {
        Write-Warn "$hotspotWarning"
      }
    }
    foreach ($hotspotError in @($hotspotResult.errors)) {
      if (-not [string]::IsNullOrWhiteSpace([string]$hotspotError)) {
        Write-Warn "$hotspotError"
      }
    }

    switch ([string]$hotspotResult.status) {
      "ready" {
        Write-Ok "Hotspot ready for offline clients at http://$($hotspotResult.domain)/"
      }
      "ip_only" {
        Write-Warn "Hotspot is active, but offline DNS is not ready. Clients should use http://$($hotspotResult.host_ip)/ until puente.link validates."
      }
      default {
        Write-Warn "Hotspot is not ready. Stack startup completed, but offline student access is unavailable."
      }
    }

    if ($FailOnHotspotCancel -and [string]$hotspotResult.status -ne "ready" -and [string]$hotspotResult.status -ne "ip_only") {
      Write-Err "-FailOnHotspotCancel was set and hotspot did not reach a usable state (status='$([string]$hotspotResult.status)'). Exiting with code 1."
      exit 1
    }
  }
} else {
  Write-Info "Hotspot skipped via -SkipHotspot."
}

# ── Update portal connection info (best-effort, non-fatal) ────────────────────
# Writes portal/network-info.json so connect.html shows current IPs / hotspot
# status without needing a live API call.  Does not require elevation.
$netInfoScript = Join-Path $scriptDir "get_network_info.ps1"
if (Test-Path $netInfoScript) {
  Write-Info "Refreshing network info for portal..."
  & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet
  if ($LASTEXITCODE -ne 0) {
    Write-Warn "get_network_info.ps1 returned non-zero; portal connection info may be stale."
  }
} else {
  Write-Warn "get_network_info.ps1 not found; portal connection info not updated."
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
