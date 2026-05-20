# Installs AIBox autostart + user-facing shortcuts.
#
# 1. Registers Scheduled Task `AIBox-Puente-Startup` with "Run with highest
#    privileges" and logon trigger, so the stack + hotspot come up on every
#    boot without a UAC prompt.
# 2. Creates a Desktop shortcut "Consola Puente Admin.lnk" pointing at the WPF UI.
#    The script itself requests elevation so it can show a friendly page when
#    UAC is denied.
# 3. Creates the same shortcut under Start Menu \ Programs \ AIBox \.
#
# All paths are derived from this script's location — nothing is hard-coded.

param(
  [string]$TaskName = "AIBox-Puente-Startup",
  [switch]$SkipDesktopShortcut,
  [switch]$SkipStartMenuShortcut,
  [switch]$SkipTask
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
  Write-Host "Elevating..." -ForegroundColor Yellow
  $selfArgs = @("-ExecutionPolicy", "Bypass", "-File", $MyInvocation.MyCommand.Path)
  if ($TaskName)               { $selfArgs += @("-TaskName", $TaskName) }
  if ($SkipDesktopShortcut)    { $selfArgs += "-SkipDesktopShortcut" }
  if ($SkipStartMenuShortcut)  { $selfArgs += "-SkipStartMenuShortcut" }
  if ($SkipTask)               { $selfArgs += "-SkipTask" }
  Start-Process -FilePath "powershell.exe" -ArgumentList $selfArgs -Verb RunAs -Wait
  exit $LASTEXITCODE
}

$scriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$upScript     = Join-Path $scriptDir "up_stack.ps1"
$uiScript     = Join-Path $scriptDir "aibox_control_ui.ps1"
$cliLauncher  = Join-Path $scriptDir "launch_admin_console.cmd"
$runtimeDir   = Split-Path -Parent $scriptDir
$toolsDir     = Split-Path -Parent $runtimeDir
$aiboxDir     = Split-Path -Parent $toolsDir
$iconCandidate = Join-Path $aiboxDir "stack\portal\assets\circlelogo.png"  # .ico preferred; .png accepted by some shells

foreach ($p in @($upScript, $uiScript)) {
  if (-not (Test-Path $p)) { throw "Required script missing: $p" }
}

Write-Host ""
Write-Host "=== AIBox Autostart Install ===" -ForegroundColor Cyan
Write-Host ""

# 1) Scheduled Task at logon
if (-not $SkipTask) {
  Write-Host "[1/3] Registering scheduled task '$TaskName' (logon trigger, highest privileges)..."
  $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($existing) {
    Write-Host "      = Removing existing task with same name." -ForegroundColor DarkGray
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
  }

  $taskAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ('-ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $upScript + '"')

  $taskTrigger = New-ScheduledTaskTrigger -AtLogOn
  # Small delay so network stack + Docker Desktop are up before we attempt
  # `docker compose up` and hotspot configuration.
  $taskTrigger.Delay = "PT90S"

  $taskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

  $taskPrincipal = New-ScheduledTaskPrincipal `
    -GroupId "BUILTIN\Administrators" `
    -RunLevel Highest

  Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $taskAction `
    -Trigger    $taskTrigger `
    -Settings   $taskSettings `
    -Principal  $taskPrincipal `
    -Description "Starts the AIBox Docker stack + offline Wi-Fi hotspot at user logon." | Out-Null

  # ExecutionTimeLimit = PT0S (ISO 8601 "no limit"). Setting it via
  # New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 0)
  # is interpreted inconsistently across Windows builds, so we patch the
  # registered task's XML directly.
  $task = Get-ScheduledTask -TaskName $TaskName
  $task.Settings.ExecutionTimeLimit = "PT0S"
  Set-ScheduledTask -InputObject $task | Out-Null

  Write-Host "      + Task registered." -ForegroundColor Green
} else {
  Write-Host "[1/3] Skipping scheduled task (-SkipTask)."
}

# Helper: create shortcut. The .lnk owns UAC: the RunAsAdministrator flag is
# SET so Windows prompts up-front before PowerShell launches. The script gets
# `-NoElevate` so it does not try to re-elevate itself once Windows has already
# given it admin rights.
function New-AdminShortcut {
  param(
    [string]$LinkPath,
    [string]$TargetPath,
    [string]$Arguments,
    [string]$WorkingDirectory,
    [string]$IconPath = ""
  )

  $dir = Split-Path -Parent $LinkPath
  if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
  }

  $ws = New-Object -ComObject WScript.Shell
  $sc = $ws.CreateShortcut($LinkPath)
  $sc.TargetPath       = $TargetPath
  $sc.Arguments        = $Arguments
  $sc.WorkingDirectory = $WorkingDirectory
  $sc.WindowStyle      = 7  # 7 = minimized, hides the transient PS console
  if ($IconPath -and (Test-Path $IconPath)) {
    $sc.IconLocation = $IconPath + ",0"
  }
  $sc.Description = "AIBox - Puente control panel"
  $sc.Save()

  # Set "Run as Administrator" bit (byte 21, bit 0x20) so Windows shows the
  # UAC prompt before powershell.exe starts.
  $bytes = [System.IO.File]::ReadAllBytes($LinkPath)
  if ($bytes.Length -gt 21) {
    $bytes[21] = $bytes[21] -bor 0x20
    [System.IO.File]::WriteAllBytes($LinkPath, $bytes)
  }
}

$uiTargetArgs = '-NoLogo -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $uiScript + '" -NoElevate'

if (-not $SkipDesktopShortcut) {
  Write-Host "[2/3] Creating Desktop shortcut..."
  $desktop = [Environment]::GetFolderPath("Desktop")
  $link = Join-Path $desktop "Consola Puente Admin.lnk"
  New-AdminShortcut `
    -LinkPath $link `
    -TargetPath "powershell.exe" `
    -Arguments $uiTargetArgs `
    -WorkingDirectory $scriptDir `
    -IconPath $iconCandidate
  Write-Host "      + $link" -ForegroundColor Green
} else {
  Write-Host "[2/3] Skipping Desktop shortcut (-SkipDesktopShortcut)."
}

if (-not $SkipStartMenuShortcut) {
  Write-Host "[3/3] Creating Start Menu shortcuts..."
  $startMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\AIBox"

  $link = Join-Path $startMenu "Consola Puente Admin.lnk"
  New-AdminShortcut `
    -LinkPath $link `
    -TargetPath "powershell.exe" `
    -Arguments $uiTargetArgs `
    -WorkingDirectory $scriptDir `
    -IconPath $iconCandidate
  Write-Host "      + $link" -ForegroundColor Green

  # CLI alias: a Start Menu entry that points at launch_admin_console.cmd, so
  # users can type "Puente" in Start and find a backup launcher. The .cmd
  # self-elevates via the script's own UAC path, so no admin bit is needed.
  if (Test-Path $cliLauncher) {
    $cliLink = Join-Path $startMenu "Puente Admin (CLI).lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($cliLink)
    $sc.TargetPath = $cliLauncher
    $sc.WorkingDirectory = $scriptDir
    $sc.WindowStyle = 7
    if (Test-Path $iconCandidate) { $sc.IconLocation = $iconCandidate + ",0" }
    $sc.Description = "AIBox - Puente control panel (CLI fallback)"
    $sc.Save()
    Write-Host "      + $cliLink" -ForegroundColor Green
  }
} else {
  Write-Host "[3/3] Skipping Start Menu shortcuts (-SkipStartMenuShortcut)."
}

Write-Host ""
Write-Host "Install complete. Reboot to verify autostart, or run the task manually:" -ForegroundColor Green
Write-Host "  Start-ScheduledTask -TaskName $TaskName" -ForegroundColor DarkGray
