# Cleans up Docker storage that is safe to remove in an offline deployment:
#   - Stopped containers
#   - Dangling (untagged) image layers
#   - Unused build cache
#   - Unused networks
#
# NEVER removes named volumes or images that are still tagged/in-use,
# because offline deployment cannot re-pull images.
#
# Used by: up_stack.ps1 (post-compose), rebuild_ai_control.ps1 — both invoke with -Apply -Quiet.
#
# Usage:
#   cleanup_docker_storage.ps1                   # Dry-run (no -Apply)
#   cleanup_docker_storage.ps1 -Apply            # Always cleans safe items
#   cleanup_docker_storage.ps1 -Apply -ThresholdGB 20
param(
  [double]$ThresholdGB = 0,   # 0 = always clean; >0 = only clean when free space < ThresholdGB
  [switch]$Apply,
  [switch]$Quiet
)

. (Join-Path $PSScriptRoot 'lib\lib_docker.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

$ErrorActionPreference = "Stop"

function Write-Status {
  # Wraps the standard log helpers in a -Quiet gate so cleanup_docker_storage.ps1
  # stays silent when invoked from rebuild/up scripts with -Quiet. The $Level
  # param replaces the older $Color param; the legacy color names are mapped to
  # the closest log level.
  param(
    [string]$Message,
    [ValidateSet("Info","Ok","Warn","Err","Run")]
    [string]$Level = "Info"
  )
  if ($Quiet) { return }
  switch ($Level) {
    "Info" { Write-Info $Message }
    "Ok"   { Write-Ok   $Message }
    "Warn" { Write-Warn $Message }
    "Err"  { Write-Err  $Message }
    "Run"  { Write-Run  $Message }
  }
}

function Get-FreeDiskGB {
  $dockerRoot = $null
  try {
    $dockerRoot = (& docker info --format '{{ .DockerRootDir }}' 2>$null)
    if ($LASTEXITCODE -ne 0) { $dockerRoot = $null }
  } catch { $dockerRoot = $null }

  if ($dockerRoot -and ($dockerRoot -match '^([A-Z]):')) {
    $drive = $Matches[1] + ':'
  } else {
    $drive = "C:"
  }
  try {
    $disk = Get-PSDrive -Name ($drive -replace ":","") -ErrorAction SilentlyContinue
    if ($disk) {
      return [math]::Round($disk.Free / 1GB, 2)
    }
    # Fallback via CIM
    $vol = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$drive'" -ErrorAction SilentlyContinue
    if ($vol) {
      return [math]::Round($vol.FreeSpace / 1GB, 2)
    }
  } catch {}
  return $null
}

function Invoke-DockerPrune {
  param([string[]]$ArgList, [string]$Label)

  if (-not (Test-DockerPruneArgs -ArgList $ArgList)) {
      throw "Refusing to run forbidden docker command: docker $($ArgList -join ' ')"
  }

  if (-not $Apply) {
    Write-Status "  [dry-run] would run: docker $($ArgList -join ' ')" "Warn"
    return
  }

  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & docker @ArgList 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $saved
  }

  if ($code -ne 0) {
    Write-Status "$Label prune returned exit code $code`: $output" "Warn"
  } else {
    # Extract the reclaimed space line if present
    $reclaimLine = @($output) | Where-Object { $_ -match "Total reclaimed space" }
    if ($reclaimLine) {
      Write-Status "$Label`: $reclaimLine" "Ok"
    } else {
      Write-Status "$Label`: done" "Ok"
    }
  }
}

# ── Disk space check ──────────────────────────────────────────────────────────

$freeGB = Get-FreeDiskGB

if ($null -ne $freeGB) {
  Write-Status "Free disk space: $freeGB GB" "Info"
} else {
  Write-Status "Could not determine free disk space; proceeding with cleanup." "Warn"
}

if ($ThresholdGB -gt 0 -and $null -ne $freeGB -and $freeGB -ge $ThresholdGB) {
  Write-Status "Free disk space ($freeGB GB) is above threshold ($ThresholdGB GB). No cleanup needed." "Ok"
  exit 0
}

if ($ThresholdGB -gt 0) {
  if ($Apply) {
    Write-Status "Free disk space ($freeGB GB) is below threshold ($ThresholdGB GB). Cleaning Docker storage..." "Warn"
  } else {
    Write-Status "Free disk space ($freeGB GB) is below threshold ($ThresholdGB GB). Dry-run only; pass -Apply to prune." "Warn"
  }
} else {
  if ($Apply) {
    Write-Status "Running routine Docker storage cleanup..." "Info"
  } else {
    Write-Status "Running routine Docker storage cleanup dry-run. Pass -Apply to prune." "Info"
  }
}

# ── Safe pruning operations ───────────────────────────────────────────────────
# Order matters: containers first, then images (so stopped containers don't
# prevent image layer removal), then build cache, then networks.

Write-Status "Removing stopped containers..." "Info"
Invoke-DockerPrune -ArgList @("container", "prune", "--force") -Label "containers"

Write-Status "Removing dangling image layers (untagged, not in use)..." "Info"
# NOTE: We intentionally do NOT use 'image prune -a' because that would remove
# all images not currently running — including the ones we need offline.
Invoke-DockerPrune -ArgList @("image", "prune", "--force") -Label "dangling images"

Write-Status "Removing unused build cache..." "Info"
Invoke-DockerPrune -ArgList @("builder", "prune", "--force") -Label "build cache"

Write-Status "Removing unused networks..." "Info"
Invoke-DockerPrune -ArgList @("network", "prune", "--force") -Label "networks"

# ── Report result ─────────────────────────────────────────────────────────────

if ($Apply) {
  $freeAfter = Get-FreeDiskGB
  if ($null -ne $freeAfter) {
    Write-Status "Cleanup complete. Free disk space: $freeAfter GB" "Ok"
  } else {
    Write-Status "Cleanup complete." "Ok"
  }
} else {
  Write-Status "Dry-run complete. No changes were made." "Warn"
}
