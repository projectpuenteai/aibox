# AIBox - Start Hotspot
# -----------------------------------------------------------------------------
# Thin wrapper that self-elevates and delegates to the engine script
# aibox/tools/llama-runtime/scripts/setup_hotspot.ps1 (the 36 KB WinRT-based
# hotspot engine that also manages firewall rules and the puente.link hosts
# entry).
#
# This wrapper exists so operators can discover the demo networking layer under
# aibox/scripts/windows/ without needing to know the engine path.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\start-hotspot.ps1
#   powershell -ExecutionPolicy Bypass -File .\start-hotspot.ps1 -EmitJson

param(
  [switch]$SkipFirewall,
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
    Add-Content -LiteralPath $logFile -Value "$ts [start-hotspot] [$Level] $Message" -Encoding UTF8
  } catch {}
}

if (-not (Test-Path $target)) {
  Write-Host "[FAIL] Engine script not found: $target" -ForegroundColor Red
  Write-DemoLog "FAIL engine script not found: $target" "error"
  exit 1
}

$engineArgs = @("-ExecutionPolicy", "Bypass", "-File", $target)
if ($SkipFirewall) { $engineArgs += "-SkipFirewall" }
if ($EmitJson)     { $engineArgs += "-EmitJson" }
if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) { $engineArgs += @("-JsonOutFile", $JsonOutFile) }

Write-DemoLog "invoking setup_hotspot.ps1 (admin=$([bool](Test-IsAdministrator)))"

if (Test-IsAdministrator) {
  & powershell @engineArgs
  exit $LASTEXITCODE
}

Write-Host "Elevating for Mobile Hotspot setup..." -ForegroundColor Yellow
try {
  $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $engineArgs -Verb RunAs -Wait -PassThru
  Write-DemoLog "setup_hotspot.ps1 elevated run finished with exit $($proc.ExitCode)"
  exit $proc.ExitCode
} catch {
  Write-Host "[FAIL] Elevation cancelled or blocked." -ForegroundColor Red
  Write-DemoLog "elevation refused: $($_.Exception.Message)" "error"
  exit 1
}
