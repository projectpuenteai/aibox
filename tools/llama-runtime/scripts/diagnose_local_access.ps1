# Runs the local-network diagnostics used for field deployment and prints a
# concise operator summary. Safe to run without elevation.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\diagnose_local_access.ps1

$ErrorActionPreference = "Stop"

function Format-Bool {
  param($Value)
  if ($null -eq $Value -or $Value -eq "") { return "unknown" }
  return [string][bool]$Value
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$netInfoScript = Join-Path $scriptDir "get_network_info.ps1"

if (-not (Test-Path $netInfoScript)) {
  throw "Missing required script: $netInfoScript"
}

$json = & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet
if ($LASTEXITCODE -ne 0) {
  throw "get_network_info.ps1 failed with exit code $LASTEXITCODE"
}

$info = $json | ConvertFrom-Json

Write-Host ""
Write-Host "=== AIBox Local Access Diagnostics ===" -ForegroundColor Cyan
Write-Host ""
Write-Host ("Recommended mode : {0}" -f $info.recommended_method)
Write-Host ("Primary URL      : {0}" -f ($(if ($info.primary_url) { $info.primary_url } else { "(none detected)" })))
Write-Host ("HTTP listening   : {0}" -f (Format-Bool $info.http.listening))
Write-Host ("Firewall port 80 : {0}" -f (Format-Bool $info.http.firewall_allowed))

if ($info.hotspot) {
  Write-Host ("Hotspot status   : {0}" -f $info.hotspot.status)
  if ($info.hotspot.ssid) {
    Write-Host ("Hotspot SSID     : {0}" -f $info.hotspot.ssid)
  }
  if ($info.hotspot.host_ip) {
    Write-Host ("Hotspot host IP  : {0}" -f $info.hotspot.host_ip)
  }
}

if ($info.lan -and $info.lan.ips) {
  $lanIps = @($info.lan.ips)
  if ($lanIps.Count -gt 0) {
    Write-Host ("LAN IPs          : {0}" -f ($lanIps -join ", "))
  }
}

if ($info.preferred) {
  if ($info.preferred.stable_ip) {
    Write-Host ("Preferred IP     : {0}" -f $info.preferred.stable_ip)
  }
  if ($info.preferred.hostname) {
    Write-Host ("Preferred host   : {0}" -f $info.preferred.hostname)
  }
}

if ($info.diagnostics -and $info.diagnostics.steps) {
  Write-Host ""
  Write-Host "Connection steps:" -ForegroundColor Cyan
  $stepNum = 1
  foreach ($step in @($info.diagnostics.steps)) {
    Write-Host ("  {0}. {1}" -f $stepNum, $step)
    $stepNum++
  }
}

if ($info.diagnostics -and $info.diagnostics.warnings) {
  $warnings = @($info.diagnostics.warnings)
  if ($warnings.Count -gt 0) {
    Write-Host ""
    Write-Host "Warnings:" -ForegroundColor Yellow
    foreach ($warning in $warnings) {
      Write-Host ("  - {0}" -f $warning) -ForegroundColor Yellow
    }
  }
}

Write-Host ""
if ($info.primary_url) {
  Write-Host ("Guide page       : {0}/connect.html" -f $info.primary_url) -ForegroundColor Green
}
Write-Host ""
