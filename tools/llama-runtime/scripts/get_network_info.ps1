# Detects the host's current network addresses and hotspot status, then writes
# portal/network-info.json so the connect.html page can display connection
# instructions without needing a live API call.
#
# Safe to run without elevation. Can be called standalone or from up_stack.ps1
# and setup_hotspot.ps1.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\get_network_info.ps1
#   powershell -ExecutionPolicy Bypass -File .\get_network_info.ps1 -Quiet

param(
  # Suppress progress output; still writes the JSON and returns it on stdout
  [switch]$Quiet
)

$ErrorActionPreference = "SilentlyContinue"

function Read-EnvValue {
  param(
    [string]$Key,
    [string]$Default = ""
  )
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

function Test-TcpReachable {
  param(
    [string]$HostName,
    [int]$Port = 80,
    [int]$TimeoutMs = 1200
  )
  try {
    $client = New-Object System.Net.Sockets.TcpClient
    $async = $client.BeginConnect($HostName, $Port, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
      $client.Close()
      return $false
    }
    $client.EndConnect($async) | Out-Null
    $client.Close()
    return $true
  } catch {
    return $false
  }
}

function Get-BestLanAddress {
  param([array]$Candidates)
  if (-not $Candidates -or $Candidates.Count -eq 0) { return $null }

  $bestRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric, InterfaceMetric |
    Select-Object -First 1

  if ($bestRoute) {
    $preferred = $Candidates |
      Where-Object { $_.InterfaceIndex -eq $bestRoute.InterfaceIndex } |
      Select-Object -First 1
    if ($preferred) { return $preferred }
  }

  return $Candidates | Select-Object -First 1
}

function Get-IpconfigAdapters {
  $blocks = @()
  $current = $null
  foreach ($line in (& ipconfig)) {
    if ($line -match '^[A-Za-z].*adapter\s+(.+):$') {
      if ($current) { $blocks += [pscustomobject]$current }
      $current = @{
        name = $Matches[1].Trim()
        ipv4 = @()
        gateway = @()
      }
      continue
    }
    if (-not $current) { continue }
    if ($line -match 'IPv4 Address[^\:]*:\s*([0-9.]+)') {
      $current.ipv4 += $Matches[1]
      continue
    }
    if ($line -match 'Default Gateway[^\:]*:\s*([0-9.]+)') {
      if (-not [string]::IsNullOrWhiteSpace($Matches[1])) {
        $current.gateway += $Matches[1]
      }
      continue
    }
  }
  if ($current) { $blocks += [pscustomobject]$current }
  return $blocks
}

function Test-FirewallPortAllowed {
  param(
    [int]$Port,
    [string]$Protocol = "TCP"
  )
  try {
    $rules = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction Stop
    foreach ($rule in $rules) {
      $filters = $rule | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
      foreach ($filter in $filters) {
        if ($filter.Protocol -eq $Protocol -and ($filter.LocalPort -eq "$Port" -or $filter.LocalPort -eq "Any")) {
          return $true
        }
      }
    }
  } catch {
    return $null
  }
  return $false
}

function Normalize-PreferredHostname {
  param([string]$Value)
  $text = [string]$Value
  $text = $text.Trim().ToLowerInvariant()
  if ([string]::IsNullOrWhiteSpace($text)) { return $null }
  $text = ($text -replace "[^a-z0-9.-]", "-").Trim(".-")
  if ([string]::IsNullOrWhiteSpace($text)) { return $null }
  return $text
}

# Resolve paths
$scriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir   = Split-Path -Parent $scriptDir
$toolsDir     = Split-Path -Parent $runtimeDir
$aiboxDir     = Split-Path -Parent $toolsDir
$stackEnvFile = Join-Path $aiboxDir "stack\.env"
$portalDir    = Join-Path $aiboxDir "stack\portal"
$outFile      = Join-Path $portalDir "network-info.json"

# Read config
$configuredSsid       = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$configuredKey        = Read-EnvValue "HOTSPOT_KEY" "puente1234"
$preferredStableIp    = Read-EnvValue "OFFLINE_ACCESS_IP" ""
$preferredHostnameRaw = Read-EnvValue "OFFLINE_HOSTNAME" ""
$preferredHostname    = Normalize-PreferredHostname $preferredHostnameRaw
$hostName             = $env:COMPUTERNAME
$httpPort             = 80

# Probe hotspot status
$hostedNetOutput = & netsh wlan show hostednetwork 2>&1
$hotspotActive   = $false
$hotspotSsid     = $configuredSsid
$hotspotSupport  = "unknown"

if ($hostedNetOutput -match "(?i)Status\s*:\s*Started") {
  $hotspotActive = $true
}
if ($hostedNetOutput -match '(?i)The hosted network couldn''t be started|not supported') {
  $hotspotSupport = "unsupported_or_disabled"
} elseif ($hostedNetOutput) {
  $hotspotSupport = "available_or_unknown"
}
if ($hostedNetOutput -match '(?i)SSID name\s*:\s*"([^"]+)"') {
  $hotspotSsid = $Matches[1].Trim()
} elseif ($hostedNetOutput -match '(?i)SSID name\s*:\s*(\S+)') {
  $hotspotSsid = $Matches[1].Trim()
}

$lanCandidates = @()
$hotspotAddr = $null
$usedNetAdapterApi = $false
try {
  $allAddrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
    Where-Object {
      $_.IPAddress -notlike "127.*" -and
      $_.IPAddress -notlike "169.254.*" -and
      $_.PrefixOrigin -ne "WellKnown"
    }

  foreach ($addr in $allAddrs) {
    $adapter = Get-NetAdapter -InterfaceIndex $addr.InterfaceIndex -ErrorAction SilentlyContinue
    $alias = if ($adapter) { $adapter.Name } else { $addr.InterfaceAlias }
    $desc = if ($adapter) { $adapter.InterfaceDescription } else { "" }
    $isVirtual = ($alias -match "(?i)wsl|docker|hyper-v|virtual|vethernet|loopback") -or
                 ($desc -match "(?i)wsl|docker|hyper-v|virtual|loopback")

    $entry = [pscustomobject]@{
      ip              = $addr.IPAddress
      prefix_length   = $addr.PrefixLength
      interface_alias = $alias
      interface_index = $addr.InterfaceIndex
      prefix_origin   = $addr.PrefixOrigin
      has_gateway     = $false
    }

    if ($addr.IPAddress -like "192.168.137.*") {
      $hotspotAddr = $entry
    } elseif (-not $isVirtual) {
      $lanCandidates += $entry
    }
  }
  $usedNetAdapterApi = $lanCandidates.Count -gt 0 -or $hotspotAddr
} catch {}

if (-not $usedNetAdapterApi) {
  foreach ($adapter in (Get-IpconfigAdapters)) {
    $alias = $adapter.name
    $isVirtual = $alias -match "(?i)wsl|docker|hyper-v|virtual|vethernet|loopback|bluetooth"
    foreach ($ip in @($adapter.ipv4)) {
      if ($ip -like "127.*" -or $ip -like "169.254.*") { continue }
      $entry = [pscustomobject]@{
        ip              = $ip
        prefix_length   = $null
        interface_alias = $alias
        interface_index = $null
        prefix_origin   = "ipconfig"
        has_gateway     = @($adapter.gateway).Count -gt 0
      }
      if ($ip -like "192.168.137.*") {
        $hotspotAddr = $entry
      } elseif (-not $isVirtual) {
        $lanCandidates += $entry
      }
    }
  }
}

$bestLan = if ($lanCandidates | Where-Object { $_.has_gateway }) {
  ($lanCandidates | Where-Object { $_.has_gateway } | Select-Object -First 1)
} else {
  Get-BestLanAddress $lanCandidates
}
$lanIps = @($lanCandidates | ForEach-Object { $_.ip })
$hotspotIp = if ($hotspotAddr) { $hotspotAddr.ip } else { $null }

$listeningAddresses = @()
$httpListening = $null
try {
  $listeningAddresses = @(
    Get-NetTCPConnection -State Listen -LocalPort $httpPort -ErrorAction Stop |
      Select-Object -ExpandProperty LocalAddress -Unique
  )
  $httpListening = $listeningAddresses.Count -gt 0
} catch {}
$firewallHttpAllowed = Test-FirewallPortAllowed -Port $httpPort -Protocol "TCP"

$warnings = New-Object System.Collections.Generic.List[string]
$notes = New-Object System.Collections.Generic.List[string]

if ($httpListening -eq $false) {
  $warnings.Add("No process is listening on TCP port 80 on the host. Nearby clients will not be able to open the portal.")
} elseif ($null -eq $httpListening) {
  $notes.Add("Windows did not allow inspection of listening sockets from this shell. Port 80 reachability was checked separately.")
}
if ($null -eq $firewallHttpAllowed) {
  $notes.Add("Windows Firewall rules could not be inspected from this shell. Verify TCP port 80 is allowed if clients cannot connect.")
} elseif (-not $firewallHttpAllowed) {
  $warnings.Add("No enabled inbound Windows Firewall allow rule for TCP port 80 was detected. Windows may block nearby clients.")
}

$recommendedMethod = "unknown"
$primaryCandidate = $null

if ($hotspotActive -and $hotspotIp) {
  $recommendedMethod = "hotspot"
  $primaryCandidate = $hotspotIp
  $notes.Add("Offline hotspot mode is the most stable field option on Windows because the hotspot host address normally remains 192.168.137.1.")
} elseif ($bestLan) {
  $recommendedMethod = "lan"
  if (-not [string]::IsNullOrWhiteSpace($preferredStableIp)) {
    $preferredMatch = $lanCandidates | Where-Object { $_.ip -eq $preferredStableIp } | Select-Object -First 1
    if ($preferredMatch) {
      $primaryCandidate = $preferredStableIp
      $notes.Add("Using OFFLINE_ACCESS_IP as the preferred client address.")
    } else {
      $primaryCandidate = $bestLan.ip
      $warnings.Add("OFFLINE_ACCESS_IP is set to $preferredStableIp but that address is not assigned on the current host. Configure a DHCP reservation or static NIC IP to make it reliable.")
    }
  } else {
    $primaryCandidate = $bestLan.ip
    $notes.Add("For a stable LAN address across restarts, reserve this host's IP in the router or configure a static IPv4 address on the active NIC, then set OFFLINE_ACCESS_IP in stack/.env.")
  }
} else {
  $warnings.Add("No non-virtual LAN IPv4 address was detected. The host is not currently reachable from nearby devices on a local network.")
}

$primaryUrl = if ($primaryCandidate) { "http://$primaryCandidate" } else { $null }
$loopbackReachable = Test-TcpReachable -HostName "127.0.0.1" -Port $httpPort
if ($primaryCandidate) {
  $primaryReachable = Test-TcpReachable -HostName $primaryCandidate -Port $httpPort
} else {
  $primaryReachable = $false
}

if ($primaryCandidate -and -not $primaryReachable -and $httpListening) {
  $warnings.Add("The host selected $primaryCandidate as the client address, but a local TCP check to port 80 failed. Verify Docker/Caddy is up and Windows is bound on the LAN interface.")
}

if ($hotspotSupport -eq "unsupported_or_disabled") {
  $warnings.Add("Windows hosted-network mode is not available on this adapter or is disabled. Offline Wi-Fi hotspot mode may require Windows Mobile Hotspot support instead.")
}

$hostnameCandidates = @()
if ($preferredHostname) {
  $hostnameCandidates += "http://$preferredHostname"
  if ($preferredHostname -notmatch "\.") {
    $hostnameCandidates += "http://$preferredHostname.local"
  }
  $notes.Add("OFFLINE_HOSTNAME is advisory only unless client DNS, mDNS, or hosts entries resolve it to the host IP.")
}
$hostnameCandidates += "http://$($hostName.ToLowerInvariant())"
$hostnameCandidates += "http://$($hostName.ToLowerInvariant()).local"
$hostnameCandidates = @($hostnameCandidates | Select-Object -Unique)

$steps = @()
switch ($recommendedMethod) {
  "hotspot" {
    $steps += "On the host, start the offline hotspot with setup_hotspot.ps1 as Administrator."
    $steps += "On the client device, join SSID '$hotspotSsid'."
    if ($preferredHostname) {
      $steps += "Open http://$preferredHostname/ in a browser. Hotspot clients should receive the host as DNS automatically."
      $steps += "If the hostname does not open, use $primaryUrl instead."
    } else {
      $steps += "Open $primaryUrl in a browser."
    }
  }
  "lan" {
    $steps += "Connect the client device to the same local network as the AIBox host."
    if ($preferredHostname) {
      $steps += "If the client is using the AIBox DNS server, open http://$preferredHostname/."
      $steps += "If the hostname does not resolve, use $primaryUrl."
    } elseif (-not [string]::IsNullOrWhiteSpace($preferredStableIp)) {
      $steps += "Use the reserved/static host address $primaryUrl."
    } else {
      $steps += "Open $primaryUrl in a browser."
    }
    $steps += "If you want that address to survive router reboots and host restarts, reserve it in the router or assign it statically on the active NIC."
  }
  default {
    $steps += "Connect the host to a local network or start hotspot mode, then rerun get_network_info.ps1."
  }
}

$info = [ordered]@{
  recommended_method = $recommendedMethod
  primary_url = $primaryUrl
  preferred = [ordered]@{
    stable_ip = if ([string]::IsNullOrWhiteSpace($preferredStableIp)) { $null } else { $preferredStableIp }
    hostname  = $preferredHostname
  }
  hotspot = [ordered]@{
    status    = if ($hotspotActive) { "active" } else { "inactive" }
    support   = $hotspotSupport
    ssid      = $hotspotSsid
    password  = $configuredKey
    host_ip   = $hotspotIp
    dns_server = $hotspotIp
  }
  lan = [ordered]@{
    ips        = $lanIps
    interfaces = $lanCandidates
    primary_ip = if ($bestLan) { $bestLan.ip } else { $null }
    hostname   = $hostName
  }
  hostnames = [ordered]@{
    computer_name = $hostName
    candidates    = $hostnameCandidates
  }
  http = [ordered]@{
    port              = $httpPort
    listening         = $httpListening
    listen_addresses  = $listeningAddresses
    firewall_allowed  = $firewallHttpAllowed
    loopback_reachable = $loopbackReachable
    primary_reachable = $primaryReachable
  }
  diagnostics = [ordered]@{
    warnings = @($warnings)
    notes    = @($notes)
    steps    = $steps
  }
  generated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
}

$json = $info | ConvertTo-Json -Depth 6 -Compress:$false

if (-not (Test-Path $portalDir)) {
  if (-not $Quiet) {
    Write-Host "WARNING: Portal directory not found: $portalDir" -ForegroundColor Yellow
  }
} else {
  $json | Set-Content -Path $outFile -Encoding UTF8
  if (-not $Quiet) {
    Write-Host "[ok] Wrote $outFile" -ForegroundColor Green
  }
}

if (-not $Quiet) {
  Write-Host ""
  Write-Host "Recommended mode : $recommendedMethod"
  if ($hotspotActive) {
    Write-Host "Hotspot          : ACTIVE  SSID: $hotspotSsid  IP: $hotspotIp" -ForegroundColor Green
  } else {
    Write-Host "Hotspot          : inactive" -ForegroundColor DarkYellow
  }
  if ($lanIps.Count -gt 0) {
    Write-Host "LAN IPs          : $($lanIps -join ', ')"
  }
  if ($primaryUrl) {
    Write-Host "Primary URL      : $primaryUrl" -ForegroundColor Cyan
    Write-Host "Guide URL        : $primaryUrl/connect.html" -ForegroundColor Cyan
  }
  Write-Host "HTTP listener    : $(if ($httpListening) { 'yes' } else { 'no' }) on $($listeningAddresses -join ', ')"
  Write-Host "Firewall port 80 : $(if ($firewallHttpAllowed) { 'allow rule detected' } else { 'allow rule not detected' })"
  if ($preferredStableIp) {
    Write-Host "Preferred IP     : $preferredStableIp"
  }
  if ($preferredHostname) {
    Write-Host "Preferred host   : $preferredHostname"
  }
  foreach ($warning in $warnings) {
    Write-Host "WARN             : $warning" -ForegroundColor Yellow
  }
  Write-Host ""
}

Write-Output $json
