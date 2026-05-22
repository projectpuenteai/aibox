# Shuts down the AIBox stack: stops the Windows Mobile Hotspot (and removes the
# puente.link hosts entry), then runs `docker compose stop`. Self-elevates to
# Administrator so the hotspot teardown can touch WinRT tethering + the hosts
# file.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\down_stack.ps1
#   powershell -ExecutionPolicy Bypass -File .\down_stack.ps1 -EmitJson
#   powershell -ExecutionPolicy Bypass -File .\down_stack.ps1 -EmitJson -JsonOutFile <path>
#   powershell -ExecutionPolicy Bypass -File .\down_stack.ps1 -SkipHotspot
#   powershell -ExecutionPolicy Bypass -File .\down_stack.ps1 -SkipDocker

param(
  [string]$ComposeFile = "",
  [switch]$SkipHotspot,
  [switch]$SkipDocker,
  [switch]$EmitJson,
  [string]$JsonOutFile = ""
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$toolsDir   = Split-Path -Parent $runtimeDir
$aiboxDir   = Split-Path -Parent $toolsDir

if ([string]::IsNullOrWhiteSpace($ComposeFile)) {
  $ComposeFile = Join-Path $aiboxDir "stack\docker-compose.yaml"
}

$result = [ordered]@{
  ok           = $false
  hotspot      = $null
  docker       = $null
  errors       = New-Object System.Collections.Generic.List[string]
  generated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
}

function Emit-Result {
  param([int]$ExitCode = 0)
  $result.ok = ($result.errors.Count -eq 0)
  $json = $result | ConvertTo-Json -Depth 8
  if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) {
    $json | Set-Content -Path $JsonOutFile -Encoding UTF8
  }
  if ($EmitJson) { Write-Output $json }
  exit $ExitCode
}

# Self-elevate unless already admin. Re-invokes this script with the same args.
if (-not (Test-IsAdministrator)) {
  $selfArgs = @("-ExecutionPolicy", "Bypass", "-File", $MyInvocation.MyCommand.Path)
  if ($ComposeFile) { $selfArgs += @("-ComposeFile", $ComposeFile) }
  if ($SkipHotspot) { $selfArgs += "-SkipHotspot" }
  if ($SkipDocker)  { $selfArgs += "-SkipDocker" }
  if ($EmitJson)    { $selfArgs += "-EmitJson" }
  if ($JsonOutFile) { $selfArgs += @("-JsonOutFile", $JsonOutFile) }
  try {
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $selfArgs -Verb RunAs -Wait -PassThru
    exit $proc.ExitCode
  } catch {
    $result.errors.Add("Elevation cancelled or blocked; cannot shut down hotspot/stack.")
    Emit-Result -ExitCode 1
  }
}

Write-Host ""
Write-Host "=== AIBox Shutdown ===" -ForegroundColor Cyan
Write-Host ""

# 1) Stop the Puente DNS responder + restore ICS DNS proxy (if we toggled it).
. (Join-Path $scriptDir 'lib\lib_ics_dns.ps1')
. (Join-Path $scriptDir 'lib\lib_puente_dns.ps1')

$dnsPidFile   = Join-Path $aiboxDir "backend-data\appdata\host-admin\puente_dns.pid"
$icsStateFile = Join-Path $aiboxDir "backend-data\appdata\host-admin\ics_dns_prev.json"

if (Test-Path -LiteralPath $dnsPidFile) {
  Write-Host "[1/3] Stopping Puente DNS responder..."
  try {
    $dnsPid = [int](Get-Content -LiteralPath $dnsPidFile -Raw).Trim()
    if (-not (Stop-PuenteResponderByPid -ProcessId $dnsPid)) {
      Write-Host "       (PID $dnsPid was not our responder or already gone - skipping kill.)"
    }
  } catch {
    $result.errors.Add("Could not stop Puente DNS responder: $($_.Exception.Message)")
  } finally {
    Remove-Item -LiteralPath $dnsPidFile -Force -ErrorAction SilentlyContinue
  }
} else {
  Write-Host "[1/3] No Puente DNS responder PID file; skipping responder stop."
}

# Only re-enable ICS DNS if up_stack actually toggled it. Reading the sidecar
# tells us the exact prior state to restore. Without the sidecar we leave the
# registry alone - the user may have configured EnableProxy themselves and
# down_stack should not silently revert it.
$icsPrior = Read-IcsDnsPriorState -Path $icsStateFile
if ($icsPrior -and $icsPrior.we_toggled) {
  if ($icsPrior.prev_enabled) {
    try {
      Enable-IcsDnsProxy
    } catch {
      $result.errors.Add("Could not restore ICS DNS proxy: $($_.Exception.Message)")
    }
  } else {
    # Prior state was disabled; nothing to restore. Leave EnableProxy at 0 to
    # match what the user had before up_stack ran.
    Write-Host "       (ICS DNS proxy was disabled before up_stack; leaving it disabled.)"
  }
} else {
  Write-Host "       (No ICS DNS sidecar found; up_stack did not toggle the proxy.)"
}
Remove-Item -LiteralPath $icsStateFile -Force -ErrorAction SilentlyContinue

# 2) Hotspot teardown
if (-not $SkipHotspot) {
  $hotspotScript = Join-Path $scriptDir "setup_hotspot.ps1"
  if (Test-Path $hotspotScript) {
    $jsonFile = Join-Path ([System.IO.Path]::GetTempPath()) ("aibox-hotspot-stop-" + [guid]::NewGuid().ToString() + ".json")
    try {
      Write-Host "[2/3] Stopping Windows Mobile Hotspot..."
      & powershell -ExecutionPolicy Bypass -File $hotspotScript -Stop -EmitJson -JsonOutFile $jsonFile | Out-Null
      if (Test-Path $jsonFile) {
        try { $result.hotspot = Get-Content $jsonFile -Raw | ConvertFrom-Json } catch {}
      }
      if ($LASTEXITCODE -ne 0) {
        $result.errors.Add("Hotspot stop exited with code $LASTEXITCODE")
      }
      if ($result.hotspot -and $result.hotspot.errors) {
        foreach ($hotspotError in @($result.hotspot.errors)) {
          if (-not [string]::IsNullOrWhiteSpace([string]$hotspotError)) {
            $result.errors.Add([string]$hotspotError)
          }
        }
      }
    } catch {
      $result.errors.Add("Hotspot stop threw: $($_.Exception.Message)")
    } finally {
      if (Test-Path $jsonFile) { Remove-Item -LiteralPath $jsonFile -Force -ErrorAction SilentlyContinue }
    }
  } else {
    $result.errors.Add("setup_hotspot.ps1 not found at $hotspotScript")
  }
} else {
  Write-Host "[2/3] Skipping hotspot teardown (-SkipHotspot)."
}

# 3) docker compose stop
if (-not $SkipDocker) {
  Write-Host "[3/3] docker compose stop..."
  if (-not (Test-Path $ComposeFile)) {
    $result.errors.Add("Compose file not found: $ComposeFile")
  } else {
    try {
      & docker compose -f $ComposeFile stop
      $result.docker = [ordered]@{ exit_code = $LASTEXITCODE }
      if ($LASTEXITCODE -ne 0) {
        $result.errors.Add("docker compose stop exited with code $LASTEXITCODE")
      }
    } catch {
      $result.errors.Add("docker compose stop threw: $($_.Exception.Message)")
    }
  }
} else {
  Write-Host "[3/3] Skipping docker compose stop (-SkipDocker)."
}

if ($result.errors.Count -eq 0) {
  Write-Host ""
  Write-Ok "AIBox stack shut down."
  Emit-Result -ExitCode 0
} else {
  Write-Host ""
  Write-Warn "Shutdown completed with errors:"
  foreach ($e in $result.errors) { Write-Host "  - $e" -ForegroundColor Yellow }
  Emit-Result -ExitCode 1
}
