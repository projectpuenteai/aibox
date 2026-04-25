# AIBox - Stop Hotspot
# -----------------------------------------------------------------------------
# Thin wrapper that self-elevates and delegates to
# aibox/tools/llama-runtime/scripts/setup_hotspot.ps1 -Stop. Also removes the
# puente.link hosts entry if present (the engine handles this).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\stop-hotspot.ps1
#   powershell -ExecutionPolicy Bypass -File .\stop-hotspot.ps1 -EmitJson

param(
  [switch]$EmitJson,
  [string]$JsonOutFile = ""
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir = $scriptDir
$scriptsDir = Split-Path -Parent $windowsDir
$aiboxDir   = Split-Path -Parent $scriptsDir
$engineDir  = Join-Path $aiboxDir "tools\llama-runtime\scripts"
$target     = Join-Path $engineDir "setup_hotspot.ps1"
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
    Add-Content -LiteralPath $logFile -Value "$ts [stop-hotspot] [$Level] $Message" -Encoding UTF8
  } catch {}
}

if (-not (Test-Path $target)) {
  Write-Host "[FAIL] Engine script not found: $target" -ForegroundColor Red
  Write-DemoLog "FAIL engine script not found: $target" "error"
  exit 1
}

$engineArgs = @("-ExecutionPolicy", "Bypass", "-File", $target, "-Stop")
if ($EmitJson) { $engineArgs += "-EmitJson" }
if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) { $engineArgs += @("-JsonOutFile", $JsonOutFile) }

Write-DemoLog "invoking setup_hotspot.ps1 -Stop (admin=$([bool](Test-IsAdministrator)))"

if (Test-IsAdministrator) {
  & powershell @engineArgs
  exit $LASTEXITCODE
}

Write-Host "Elevating for Mobile Hotspot teardown..." -ForegroundColor Yellow
try {
  $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $engineArgs -Verb RunAs -Wait -PassThru
  Write-DemoLog "setup_hotspot.ps1 -Stop elevated run finished with exit $($proc.ExitCode)"
  exit $proc.ExitCode
} catch {
  Write-Host "[FAIL] Elevation cancelled or blocked." -ForegroundColor Red
  Write-DemoLog "elevation refused: $($_.Exception.Message)" "error"
  exit 1
}
