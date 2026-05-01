# Removes the AIBox autostart scheduled task and Desktop / Start Menu
# shortcuts created by install_autostart.ps1.

param(
  [string]$TaskName = "AIBox-Puente-Startup"
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
  $selfArgs = @("-ExecutionPolicy", "Bypass", "-File", $MyInvocation.MyCommand.Path, "-TaskName", $TaskName)
  Start-Process -FilePath "powershell.exe" -ArgumentList $selfArgs -Verb RunAs -Wait
  exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== AIBox Autostart Uninstall ===" -ForegroundColor Cyan
Write-Host ""

# 1) Scheduled Task
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
  Write-Host "[1/3] - Removed scheduled task '$TaskName'." -ForegroundColor Green
} else {
  Write-Host "[1/3] = No scheduled task named '$TaskName'."
}

# 2) Desktop shortcut
$desktopRemoved = $false
foreach ($shortcutName in @("Consola Puente Admin.lnk", "AIBox Control.lnk")) {
  $desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) $shortcutName
  if (Test-Path $desktopLink) {
    Remove-Item -LiteralPath $desktopLink -Force
    $desktopRemoved = $true
  }
}
if ($desktopRemoved) {
  Write-Host "[2/3] - Removed Desktop shortcut." -ForegroundColor Green
} else {
  Write-Host "[2/3] = No Desktop shortcut to remove."
}

# 3) Start Menu shortcut + folder (if empty)
$startMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\AIBox"
$startRemoved = $false
foreach ($shortcutName in @("Consola Puente Admin.lnk", "AIBox Control.lnk")) {
  $startLink = Join-Path $startMenuDir $shortcutName
  if (Test-Path $startLink) {
    Remove-Item -LiteralPath $startLink -Force
    $startRemoved = $true
  }
}
if ($startRemoved) {
  Write-Host "[3/3] - Removed Start Menu shortcut." -ForegroundColor Green
} else {
  Write-Host "[3/3] = No Start Menu shortcut to remove."
}
if ((Test-Path $startMenuDir) -and -not (Get-ChildItem -LiteralPath $startMenuDir -Force | Select-Object -First 1)) {
  Remove-Item -LiteralPath $startMenuDir -Force
  Write-Host "      - Removed empty Start Menu folder." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
