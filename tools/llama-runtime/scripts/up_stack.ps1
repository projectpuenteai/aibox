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


