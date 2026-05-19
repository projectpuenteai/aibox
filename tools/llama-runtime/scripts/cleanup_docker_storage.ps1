# Cleans up Docker storage that is safe to remove in an offline deployment:
#   - Stopped containers
#   - Dangling (untagged) image layers
#   - Unused build cache
#   - Unused networks
#
# NEVER removes named volumes or images that are still tagged/in-use,
# because offline deployment cannot re-pull images.
#
# Usage:
#   cleanup_docker_storage.ps1                   # Dry-run only
#   cleanup_docker_storage.ps1 -Apply            # Always cleans safe items
#   cleanup_docker_storage.ps1 -Apply -ThresholdGB 20
param(
  [double]$ThresholdGB = 0,   # 0 = always clean; >0 = only clean when free space < ThresholdGB
  [switch]$Apply,
  [switch]$Quiet
)

$ErrorActionPreference = "Stop"

function Write-Status {
  param([string]$Message, [string]$Color = "Cyan")
  if (-not $Quiet) {
    Write-Host $Message -ForegroundColor $Color
  }
}

function Get-FreeDiskGB {
  $drive = (Get-Location).Drive.Name + ":"
  try {
    $disk = Get-PSDrive -Name ($drive -replace ":","") -ErrorAction SilentlyContinue
    if ($disk) {
      return [math]::Round($disk.Free / 1GB, 2)
    }
    # Fallback via WMI
    $vol = Get-WmiObject -Query "SELECT FreeSpace FROM Win32_LogicalDisk WHERE DeviceID='$drive'" -ErrorAction SilentlyContinue
    if ($vol) {
      return [math]::Round($vol.FreeSpace / 1GB, 2)
    }
  } catch {}
  return $null
}

function Invoke-DockerPrune {
  param([string[]]$Args, [string]$Label)

  $joined = " $($Args -join ' ') "
  $forbidden = @(" volume ", "--volumes", " image prune -a ", " system prune ")
  foreach ($pattern in $forbidden) {
    if ($joined -like "*$pattern*") {
      throw "Refusing unsafe Docker cleanup command: docker $($Args -join ' ')"
    }
  }

  if (-not $Apply) {
    Write-Status "  [dry-run] would run: docker $($Args -join ' ')" "Yellow"
    return
  }

  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & docker @Args 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $saved
  }

  if ($code -ne 0) {
    Write-Status "  [warn] $Label prune returned exit code $code`: $output" "Yellow"
  } else {
    # Extract the reclaimed space line if present
    $reclaimLine = @($output) | Where-Object { $_ -match "Total reclaimed space" }
    if ($reclaimLine) {
      Write-Status "  $Label`: $reclaimLine" "Green"
    } else {
      Write-Status "  $Label`: done" "Green"
    }
  }
}

# ── Disk space check ──────────────────────────────────────────────────────────

$freeGB = Get-FreeDiskGB

if ($null -ne $freeGB) {
  Write-Status "[info] Free disk space: $freeGB GB"
} else {
  Write-Status "[warn] Could not determine free disk space; proceeding with cleanup." "Yellow"
}

if ($ThresholdGB -gt 0 -and $null -ne $freeGB -and $freeGB -ge $ThresholdGB) {
  Write-Status "[ok] Free disk space ($freeGB GB) is above threshold ($ThresholdGB GB). No cleanup needed." "Green"
  exit 0
}

if ($ThresholdGB -gt 0) {
  if ($Apply) {
    Write-Status "[warn] Free disk space ($freeGB GB) is below threshold ($ThresholdGB GB). Cleaning Docker storage..." "Yellow"
  } else {
    Write-Status "[warn] Free disk space ($freeGB GB) is below threshold ($ThresholdGB GB). Dry-run only; pass -Apply to prune." "Yellow"
  }
} else {
  if ($Apply) {
    Write-Status "[info] Running routine Docker storage cleanup..."
  } else {
    Write-Status "[info] Running routine Docker storage cleanup dry-run. Pass -Apply to prune."
  }
}

# ── Safe pruning operations ───────────────────────────────────────────────────
# Order matters: containers first, then images (so stopped containers don't
# prevent image layer removal), then build cache, then networks.

Write-Status "[step] Removing stopped containers..."
Invoke-DockerPrune -Args @("container", "prune", "--force") -Label "containers"

Write-Status "[step] Removing dangling image layers (untagged, not in use)..."
# NOTE: We intentionally do NOT use 'image prune -a' because that would remove
# all images not currently running — including the ones we need offline.
Invoke-DockerPrune -Args @("image", "prune", "--force") -Label "dangling images"

Write-Status "[step] Removing unused build cache..."
Invoke-DockerPrune -Args @("builder", "prune", "--force") -Label "build cache"

Write-Status "[step] Removing unused networks..."
Invoke-DockerPrune -Args @("network", "prune", "--force") -Label "networks"

# ── Report result ─────────────────────────────────────────────────────────────

if ($Apply) {
  $freeAfter = Get-FreeDiskGB
  if ($null -ne $freeAfter) {
    Write-Status "[ok] Cleanup complete. Free disk space: $freeAfter GB" "Green"
  } else {
    Write-Status "[ok] Cleanup complete." "Green"
  }
} else {
  Write-Status "[ok] Dry-run complete. No changes were made." "Yellow"
}
