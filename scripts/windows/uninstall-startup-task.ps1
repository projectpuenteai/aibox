# AIBox - Uninstall Startup Task
# -----------------------------------------------------------------------------
# Thin wrapper that delegates to
# aibox/tools/llama-runtime/scripts/uninstall_autostart.ps1 - removes the
# AIBox-Puente-Startup scheduled task plus the Desktop / Start Menu shortcuts.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\uninstall-startup-task.ps1

param(
  [string]$TaskName = "AIBox-Puente-Startup"
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir = $scriptDir
$scriptsDir = Split-Path -Parent $windowsDir
$aiboxDir   = Split-Path -Parent $scriptsDir
$engineDir  = Join-Path $aiboxDir "tools\llama-runtime\scripts"
$target     = Join-Path $engineDir "uninstall_autostart.ps1"
$logsDir    = Join-Path $aiboxDir "logs"

function Write-DemoLog {
  param([string]$Message, [string]$Level = "info")
  try {
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    $logFile = Join-Path $logsDir "windows-demo-startup.log"
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $logFile -Value "$ts [uninstall-startup-task] [$Level] $Message" -Encoding UTF8
  } catch {}
}

if (-not (Test-Path $target)) {
  Write-Host "[FAIL] Engine script not found: $target" -ForegroundColor Red
  Write-DemoLog "FAIL uninstall_autostart.ps1 not found" "error"
  exit 1
}

$engineArgs = @("-ExecutionPolicy", "Bypass", "-File", $target, "-TaskName", $TaskName)
Write-DemoLog "invoking uninstall_autostart.ps1 TaskName=$TaskName"
& powershell @engineArgs
$code = $LASTEXITCODE
Write-DemoLog "uninstall_autostart.ps1 exit=$code"
exit $code
