# AIBox - Install Startup Task
# -----------------------------------------------------------------------------
# Thin wrapper that delegates to the existing engine:
# aibox/tools/llama-runtime/scripts/install_autostart.ps1 - registers the
# scheduled task `AIBox-Puente-Startup` (logon trigger, 45 s delay, highest
# privileges, no UAC prompt) plus Desktop + Start Menu shortcuts that launch
# the WPF control panel (aibox_control_ui.ps1).
#
# Output from the scheduled task (when it fires on logon) does not appear on
# any terminal; to monitor it, review:
#   aibox/logs/windows-demo-startup.log             (this wrapper layer)
#   Task Scheduler > AIBox-Puente-Startup > History (Windows native)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\install-startup-task.ps1

param(
  [string]$TaskName = "AIBox-Puente-Startup",
  [switch]$SkipDesktopShortcut,
  [switch]$SkipStartMenuShortcut,
  [switch]$SkipTask
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir = $scriptDir
$scriptsDir = Split-Path -Parent $windowsDir
$aiboxDir   = Split-Path -Parent $scriptsDir
$engineDir  = Join-Path $aiboxDir "tools\llama-runtime\scripts"
$target     = Join-Path $engineDir "install_autostart.ps1"
$logsDir    = Join-Path $aiboxDir "logs"

function Write-DemoLog {
  param([string]$Message, [string]$Level = "info")
  try {
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    $logFile = Join-Path $logsDir "windows-demo-startup.log"
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $logFile -Value "$ts [install-startup-task] [$Level] $Message" -Encoding UTF8
  } catch {}
}

if (-not (Test-Path $target)) {
  Write-Host "[FAIL] Engine script not found: $target" -ForegroundColor Red
  Write-DemoLog "FAIL install_autostart.ps1 not found" "error"
  exit 1
}

$engineArgs = @("-ExecutionPolicy", "Bypass", "-File", $target, "-TaskName", $TaskName)
if ($SkipDesktopShortcut)   { $engineArgs += "-SkipDesktopShortcut" }
if ($SkipStartMenuShortcut) { $engineArgs += "-SkipStartMenuShortcut" }
if ($SkipTask)              { $engineArgs += "-SkipTask" }

Write-DemoLog "invoking install_autostart.ps1 TaskName=$TaskName"

# install_autostart.ps1 self-elevates internally; just forward and let the
# engine script handle the UAC prompt.
& powershell @engineArgs
$code = $LASTEXITCODE
Write-DemoLog "install_autostart.ps1 exit=$code"

if ($code -eq 0) {
  Write-Host ""
  Write-Host "Autostart installed. The stack will launch at the next user logon." -ForegroundColor Green
  Write-Host "Startup log tail: Get-Content $logsDir\windows-demo-startup.log -Tail 30" -ForegroundColor DarkGray
}

exit $code
