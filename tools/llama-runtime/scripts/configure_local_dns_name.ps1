# Creates or updates a local Technitium DNS zone so nearby clients can resolve a
# friendly name like puente.link to the AIBox host's private IP.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\configure_local_dns_name.ps1
#   powershell -ExecutionPolicy Bypass -File .\configure_local_dns_name.ps1 -Domain puente.link
#   powershell -ExecutionPolicy Bypass -File .\configure_local_dns_name.ps1 -Domain puente.link -IpAddress 192.168.1.50

param(
  [string]$Domain = "",
  [string]$IpAddress = "",
  [string]$DnsApiBaseUrl = "http://127.0.0.1:5380",
  [string]$AdminUser = "admin",
  [string]$AdminPassword = "",
  [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

function Read-EnvValue {
  param(
    [string]$Key,
    [string]$Default = ""
  )
  $envValue = [System.Environment]::GetEnvironmentVariable($Key)
  if (-not [string]::IsNullOrWhiteSpace($envValue)) {
    return $envValue
  }
  if (Test-Path $stackEnvFile) {
    $line = Get-Content $stackEnvFile -ErrorAction SilentlyContinue |
      Where-Object { $_ -match "^\s*$Key\s*=" } |
      Select-Object -First 1
    if ($line) {
      $value = ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
      if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value
      }
    }
  }
  return $Default
}

function Invoke-DnsApi {
  param(
    [string]$Path,
    [hashtable]$Query
  )
  $pairs = @()
  foreach ($key in $Query.Keys) {
    if ($null -eq $Query[$key] -or $Query[$key] -eq "") { continue }
    $pairs += ("{0}={1}" -f [uri]::EscapeDataString([string]$key), [uri]::EscapeDataString([string]$Query[$key]))
  }
  $url = "$DnsApiBaseUrl$Path"
  if ($pairs.Count -gt 0) {
    $url += "?" + ($pairs -join "&")
  }
  $response = Invoke-RestMethod -Method Get -Uri $url -TimeoutSec 15
  if ($response.status -ne "ok") {
    if ($response.errorMessage) {
      throw $response.errorMessage
    }
    throw "DNS API call failed: $Path"
  }
  return $response
}

function Get-PrimaryUrlFromNetworkInfo {
  $scriptPath = Join-Path $scriptDir "get_network_info.ps1"
  if (-not (Test-Path $scriptPath)) { return $null }
  try {
    $json = & powershell -ExecutionPolicy Bypass -File $scriptPath -Quiet
    if ($LASTEXITCODE -ne 0) { return $null }
    $info = $json | ConvertFrom-Json
    return $info.primary_url
  } catch {
    return $null
  }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$toolsDir = Split-Path -Parent $runtimeDir
$aiboxDir = Split-Path -Parent $toolsDir
$stackEnvFile = Join-Path $aiboxDir "stack\.env"

if ([string]::IsNullOrWhiteSpace($Domain)) {
  $Domain = Read-EnvValue "OFFLINE_HOSTNAME" "puente.link"
}
$Domain = ([string]$Domain).Trim().Trim(".").ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($Domain)) {
  throw "Domain is required. Set OFFLINE_HOSTNAME in stack/.env or pass -Domain."
}

if ([string]::IsNullOrWhiteSpace($IpAddress)) {
  $IpAddress = Read-EnvValue "OFFLINE_ACCESS_IP" ""
}
if ([string]::IsNullOrWhiteSpace($IpAddress)) {
  $primaryUrl = Get-PrimaryUrlFromNetworkInfo
  if ($primaryUrl) {
    try {
      $IpAddress = ([uri]$primaryUrl).Host
    } catch {}
  }
}
if ([string]::IsNullOrWhiteSpace($IpAddress)) {
  throw "IP address is required. Set OFFLINE_ACCESS_IP in stack/.env or pass -IpAddress."
}

if ([string]::IsNullOrWhiteSpace($AdminPassword)) {
  $AdminPassword = Read-EnvValue "DNS_ADMIN_PASSWORD" ""
}
if ([string]::IsNullOrWhiteSpace($AdminPassword)) {
  $AdminPassword = "admin"
}

Write-Host ""
Write-Host "=== Configure Local DNS Name ===" -ForegroundColor Cyan
Write-Host ""
Write-Host ("Domain   : {0}" -f $Domain)
Write-Host ("IP       : {0}" -f $IpAddress)
Write-Host ("DNS API  : {0}" -f $DnsApiBaseUrl)
Write-Host ""

if ($ValidateOnly) {
  Write-Host "Validation only. No DNS changes were made." -ForegroundColor Yellow
  Write-Host ""
  exit 0
}

$login = Invoke-DnsApi -Path "/api/user/login" -Query @{
  user = $AdminUser
  pass = $AdminPassword
}
$token = $login.token
if (-not $token) {
  throw "DNS login succeeded but no API token was returned."
}

$zoneList = Invoke-DnsApi -Path "/api/zones/list" -Query @{ token = $token }
$zoneExists = $false
if ($zoneList.response -and $zoneList.response.zones) {
  foreach ($zone in @($zoneList.response.zones)) {
    if ($zone.name -eq $Domain) {
      $zoneExists = $true
      break
    }
  }
}

if (-not $zoneExists) {
  $zoneCreated = $false
  try {
    Invoke-DnsApi -Path "/api/zones/create" -Query @{
      token = $token
      zone = $Domain
      type = "Primary"
    } | Out-Null
    $zoneCreated = $true
  } catch {
    Invoke-DnsApi -Path "/api/zones/create" -Query @{
      token = $token
      zoneName = $Domain
      zoneType = "Primary"
    } | Out-Null
    $zoneCreated = $true
  }
  if (-not $zoneCreated) {
    throw "Failed to create DNS zone $Domain."
  }
  Write-Host ("Created zone : {0}" -f $Domain) -ForegroundColor Green
} else {
  Write-Host ("Zone exists  : {0}" -f $Domain) -ForegroundColor DarkYellow
}

Invoke-DnsApi -Path "/api/zones/records/add" -Query @{
  token = $token
  domain = $Domain
  zone = $Domain
  type = "A"
  ipAddress = $IpAddress
  overwrite = "true"
} | Out-Null

try {
  Invoke-DnsApi -Path "/api/user/logout" -Query @{ token = $token } | Out-Null
} catch {}

Write-Host ("A record set : {0} -> {1}" -f $Domain, $IpAddress) -ForegroundColor Green
Write-Host ""
Write-Host "Next step for clients:" -ForegroundColor Cyan
Write-Host ("  Use this DNS server on the client device: {0}" -f $IpAddress)
Write-Host ("  Then open: http://{0}/" -f $Domain)
Write-Host ""
Write-Host "Notes:" -ForegroundColor Cyan
Write-Host "  - This creates a local DNS answer only. It does not update public internet DNS."
Write-Host "  - Clients must use the AIBox DNS server or another local DNS server that forwards to it."
Write-Host "  - If the DNS container was already initialized, DNS_SERVER_DOMAIN and DNS_ADMIN_PASSWORD in stack/.env will not retroactively change it."
Write-Host ""
