# Sets up a Windows Wi-Fi hosted network (software access point) so nearby
# devices can connect to AIBox without a pre-existing Wi-Fi network or internet
# connection.  Must be run as Administrator.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\setup_hotspot.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup_hotspot.ps1 -SkipFirewall
#   powershell -ExecutionPolicy Bypass -File .\setup_hotspot.ps1 -Stop
#
# After setup clients connect to the Wi-Fi SSID shown and browse to
# http://192.168.137.1  (or whatever IP is reported by get_network_info.ps1).

param(
  # Stop the hosted network and exit (does not remove firewall rules)
  [switch]$Stop,
  # Skip adding/verifying Windows Firewall inbound rules
  [switch]$SkipFirewall
)

$ErrorActionPreference = "Stop"

# ── Require elevation ─────────────────────────────────────────────────────────
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host ""
  Write-Host "ERROR: This script must be run as Administrator." -ForegroundColor Red
  Write-Host "Right-click PowerShell and choose 'Run as administrator', then re-run." -ForegroundColor Yellow
  exit 1
}

# ── Resolve paths ─────────────────────────────────────────────────────────────
$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir  = Split-Path -Parent $scriptDir
$toolsDir    = Split-Path -Parent $runtimeDir
$aiboxDir    = Split-Path -Parent $toolsDir
$stackEnvFile = Join-Path $aiboxDir "stack\.env"

# ── Read config from environment or stack/.env ────────────────────────────────
function Read-EnvValue {
  param([string]$Key, [string]$Default = "")
  $val = [System.Environment]::GetEnvironmentVariable($Key)
  if (-not [string]::IsNullOrWhiteSpace($val)) { return $val }
  if (Test-Path $stackEnvFile) {
    $line = Get-Content $stackEnvFile -ErrorAction SilentlyContinue |
            Where-Object { $_ -match "^\s*$Key\s*=" } |
            Select-Object -First 1
    if ($line) {
      $val = ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
      if (-not [string]::IsNullOrWhiteSpace($val)) { return $val }
    }
  }
  return $Default
}

$ssid = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$key  = Read-EnvValue "HOTSPOT_KEY"  "puente1234"

# ── Stop mode ────────────────────────────────────────────────────────────────
if ($Stop) {
  Write-Host ""
  Write-Host "=== Stopping AIBox Hosted Network ===" -ForegroundColor Yellow
  $out = & netsh wlan stop hostednetwork 2>&1
  Write-Host $out
  Write-Host "Done." -ForegroundColor Green
  exit 0
}

# ── Check WLAN service ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== AIBox Hotspot Setup ===" -ForegroundColor Cyan
Write-Host ""

$wlanSvc = Get-Service -Name "WlanSvc" -ErrorAction SilentlyContinue
if (-not $wlanSvc) {
  Write-Host "ERROR: WLAN service (WlanSvc) not found. Wi-Fi adapter required." -ForegroundColor Red
  exit 1
}
if ($wlanSvc.Status -ne "Running") {
  Write-Host "ERROR: WLAN service is not running (status: $($wlanSvc.Status))." -ForegroundColor Red
  Write-Host "Enable Wi-Fi in Windows Settings and try again." -ForegroundColor Yellow
  exit 1
}

Write-Host "SSID     : $ssid"
Write-Host "Password : $key"
Write-Host ""

# ── Configure hosted network ─────────────────────────────────────────────────
Write-Host "[1/4] Configuring hosted network..."
$out = & netsh wlan set hostednetwork mode=allow ssid="$ssid" key="$key" 2>&1
Write-Host "      $out"
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERROR: netsh failed (exit $LASTEXITCODE)." -ForegroundColor Red
  Write-Host "       Your adapter may not support hosted network mode." -ForegroundColor Yellow
  Write-Host "       Check: netsh wlan show drivers | findstr Hosted" -ForegroundColor Yellow
  exit 1
}

# ── Start hosted network ──────────────────────────────────────────────────────
Write-Host "[2/4] Starting hosted network..."
$out = & netsh wlan start hostednetwork 2>&1
Write-Host "      $out"
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERROR: Could not start hosted network (exit $LASTEXITCODE)." -ForegroundColor Red
  Write-Host "       Ensure no other AP software is running." -ForegroundColor Yellow
  exit 1
}

# ── Firewall rules ────────────────────────────────────────────────────────────
if ($SkipFirewall) {
  Write-Host "[3/4] Skipping firewall (--SkipFirewall)."
} else {
  Write-Host "[3/4] Verifying Windows Firewall inbound rules..."

  $rules = @(
    @{ Name = "AIBox-HTTP-Inbound";    Proto = "TCP"; Port = 80;   Desc = "AIBox portal (HTTP)" },
    @{ Name = "AIBox-DNS-TCP-Inbound"; Proto = "TCP"; Port = 53;   Desc = "AIBox DNS (TCP)" },
    @{ Name = "AIBox-DNS-UDP-Inbound"; Proto = "UDP"; Port = 53;   Desc = "AIBox DNS (UDP)" }
  )

  foreach ($r in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $r.Name -ErrorAction SilentlyContinue
    if (-not $existing) {
      New-NetFirewallRule `
        -DisplayName $r.Name `
        -Direction   Inbound `
        -Protocol    $r.Proto `
        -LocalPort   $r.Port `
        -Action      Allow `
        -Profile     Any `
        -Description $r.Desc | Out-Null
      Write-Host "      + Created rule: $($r.Name)"
    } else {
      Write-Host "      = Rule already exists: $($r.Name)"
    }
  }
}

# ── Refresh network-info.json for portal + local DNS name ─────────────────────
Write-Host "[4/4] Writing connection info and hotspot DNS guidance..."
$netInfoScript = Join-Path $scriptDir "get_network_info.ps1"
if (Test-Path $netInfoScript) {
  $netInfoJson = & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet
} else {
  Write-Host "      WARNING: get_network_info.ps1 not found; portal JSON not updated." -ForegroundColor Yellow
}

$preferredHostname = Read-EnvValue "OFFLINE_HOSTNAME" "puente.link"
$dnsNameScript = Join-Path $scriptDir "configure_local_dns_name.ps1"
if ((Test-Path $dnsNameScript) -and $netInfoJson) {
  try {
    $netInfo = $netInfoJson | ConvertFrom-Json
    $hotspotHostIp = $netInfo.hotspot.host_ip
    if ($netInfo.hotspot.status -eq "active" -and -not [string]::IsNullOrWhiteSpace($hotspotHostIp)) {
      & powershell -ExecutionPolicy Bypass -File $dnsNameScript -Domain $preferredHostname -IpAddress $hotspotHostIp | Out-Null
      Write-Host "      + Local DNS name refreshed: $preferredHostname -> $hotspotHostIp"
    } else {
      Write-Host "      = Hotspot IP not detected yet; skipped local DNS update." -ForegroundColor DarkYellow
    }
  } catch {
    Write-Host "      WARNING: Could not refresh local DNS name for hotspot clients." -ForegroundColor Yellow
  }
}

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Hotspot Active" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Connect to Wi-Fi:"
Write-Host "    SSID     : $ssid" -ForegroundColor Cyan
Write-Host "    Password : $key"  -ForegroundColor Cyan
Write-Host ""
Write-Host "  Then open in a browser:"
Write-Host "    http://$preferredHostname/   (preferred)" -ForegroundColor Cyan
Write-Host "    http://192.168.137.1/        (fallback)" -ForegroundColor Cyan
Write-Host "    http://192.168.137.1/connect.html  (connection guide)" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Hotspot clients should automatically receive:"
Write-Host "    Gateway : 192.168.137.1"
Write-Host "    DNS     : 192.168.137.1"
Write-Host ""
Write-Host "  To check status at any time:"
Write-Host "    netsh wlan show hostednetwork"
Write-Host "    powershell -ExecutionPolicy Bypass -File diagnose_local_access.ps1"
Write-Host ""
Write-Host "  To stop the hotspot:"
Write-Host "    powershell -ExecutionPolicy Bypass -File setup_hotspot.ps1 -Stop"
Write-Host ""

# ── Troubleshooting tips ──────────────────────────────────────────────────────
Write-Host "--- Troubleshooting -----------------------------------------" -ForegroundColor DarkGray
Write-Host " Adapter check : netsh wlan show drivers | findstr /i hosted" -ForegroundColor DarkGray
Write-Host " ICS required  : Control Panel > Network Connections > right-" -ForegroundColor DarkGray
Write-Host "                 click Wi-Fi > Properties > Sharing tab" -ForegroundColor DarkGray
Write-Host " Session cookies: set SESSION_COOKIE_SECURE=false in stack/.env" -ForegroundColor DarkGray
Write-Host "                  (required for HTTP-only LAN deployments)" -ForegroundColor DarkGray
Write-Host " Stable LAN mode : set OFFLINE_ACCESS_IP in stack/.env after reserving" -ForegroundColor DarkGray
Write-Host "                  the host IP in your router or on the NIC" -ForegroundColor DarkGray
Write-Host " Hostname note   : OFFLINE_HOSTNAME only works on clients if DNS/mDNS" -ForegroundColor DarkGray
Write-Host "                  actually resolves that name to this host" -ForegroundColor DarkGray
Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
