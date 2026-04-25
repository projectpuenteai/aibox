# AIBox - Stop Demo Stack
# -----------------------------------------------------------------------------
# Stops the Docker Compose stack. By default the Windows Mobile Hotspot is
# LEFT RUNNING so students can still see the SSID (and so you don't
# accidentally kick yourself off your own Wi-Fi during a demo). Pass
# -StopHotspot to also tear down the hotspot and remove the puente.link hosts
# entry.
#
# Delegates to aibox/tools/llama-runtime/scripts/down_stack.ps1 which already
# supports -SkipHotspot / -SkipDocker. This wrapper flips the mental model:
# the DEFAULT here is keep-hotspot (safer for demos); -StopHotspot opts into
# full teardown.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\stop-demo-stack.ps1
#   powershell -ExecutionPolicy Bypass -File .\stop-demo-stack.ps1 -StopHotspot
#   powershell -ExecutionPolicy Bypass -File .\stop-demo-stack.ps1 -EmitJson

param(
  [switch]$StopHotspot,
  [switch]$SkipDocker,
  [switch]$EmitJson,
  [string]$JsonOutFile = ""
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir = $scriptDir
$scriptsDir = Split-Path -Parent $windowsDir
$aiboxDir   = Split-Path -Parent $scriptsDir
$stackDir   = Join-Path $aiboxDir "stack"
$composeFile = Join-Path $stackDir "docker-compose.yaml"
$engineDir  = Join-Path $aiboxDir "tools\llama-runtime\scripts"
$target     = Join-Path $engineDir "down_stack.ps1"
$logsDir    = Join-Path $aiboxDir "logs"

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-DemoLog {
  param([string]$Message, [string]$Level = "info")
  try {
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    $logFile = Join-Path $logsDir "windows-demo-startup.log"
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $logFile -Value "$ts [stop-demo-stack] [$Level] $Message" -Encoding UTF8
  } catch {}
}

if (-not (Test-Path $target)) {
  Write-Host "[FAIL] Engine script not found: $target" -ForegroundColor Red
  Write-DemoLog "FAIL engine down_stack.ps1 not found" "error"
  exit 1
}

Write-Host ""
if ($StopHotspot) {
  Write-Host "=== AIBox Stop Demo (full teardown: stack + hotspot) ===" -ForegroundColor Cyan
  Write-DemoLog "requesting full teardown (stack + hotspot)"
} else {
  Write-Host "=== AIBox Stop Demo (stack only, hotspot kept up) ===" -ForegroundColor Cyan
  Write-DemoLog "requesting stack-only teardown (-SkipHotspot)"
}
Write-Host ""

# Keep-hotspot path: avoid the engine's unconditional self-elevation by just
# running `docker compose stop` directly. Docker CLI works for any user in the
# docker-users group, so no elevation is needed when hotspot teardown is not
# requested. This is the fast, UAC-free default path.
if (-not $StopHotspot) {
  if ($SkipDocker) {
    Write-Host "-SkipDocker and hotspot kept up: nothing to do." -ForegroundColor DarkGray
    Write-DemoLog "no-op (-SkipDocker with hotspot kept up)"
    exit 0
  }
  if (-not (Test-Path $composeFile)) {
    Write-Host "[FAIL] Compose file not found: $composeFile" -ForegroundColor Red
    Write-DemoLog "FAIL compose file missing: $composeFile" "error"
    exit 1
  }
  Write-Host "[1/1] docker compose stop..." -ForegroundColor Cyan
  & docker compose -f $composeFile stop
  $code = $LASTEXITCODE
  Write-DemoLog "docker compose stop finished exit=$code"
  if ($code -eq 0) {
    Write-Host ""
    Write-Host "[ok] Stack stopped. Hotspot left running." -ForegroundColor Green
    Write-Host "     Restart with: scripts\windows\start-demo-stack.ps1" -ForegroundColor DarkGray
  }
  if ($EmitJson) {
    $result = [pscustomobject]@{
      ok           = [bool]($code -eq 0)
      mode         = "keep_hotspot"
      docker       = [pscustomobject]@{ exit_code = [int]$code }
      generated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
    }
    $json = $result | ConvertTo-Json -Depth 6
    if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) {
      $json | Set-Content -Path $JsonOutFile -Encoding UTF8
    }
    Write-Output $json
  }
  exit $code
}

# Full teardown path: delegate to down_stack.ps1 (which manages hotspot +
# compose + hosts file cleanup together). down_stack.ps1 self-elevates if
# needed, so we just invoke it and forward args.
$engineArgs = @("-ExecutionPolicy", "Bypass", "-File", $target)
if ($SkipDocker) { $engineArgs += "-SkipDocker" }
if ($EmitJson)   { $engineArgs += "-EmitJson" }
if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) { $engineArgs += @("-JsonOutFile", $JsonOutFile) }

Write-DemoLog "delegating full teardown to down_stack.ps1"
& powershell @engineArgs
$code = $LASTEXITCODE
Write-DemoLog "down_stack.ps1 finished exit=$code"
exit $code
