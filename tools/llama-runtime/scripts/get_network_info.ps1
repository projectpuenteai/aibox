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

. (Join-Path $PSScriptRoot 'lib\lib_io.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_env.ps1')

if ($PSVersionTable.PSEdition -ne 'Desktop') {
    Write-Error "This script requires Windows PowerShell 5.1 (powershell.exe). PowerShell 7 cannot use WinRT directly here."
    exit 64
}

$ErrorActionPreference = "SilentlyContinue"

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

function Add-WinRtHelpers {
  if ("AIBox.WinRtAwait" -as [type]) { return }
  $code = @'
using System;
using System.Threading.Tasks;
using Windows.Foundation;
using System.Runtime.InteropServices.WindowsRuntime;

namespace AIBox {
  public static class WinRtAwait {
    public static T WaitFor<T>(IAsyncOperation<T> op) {
      return op.AsTask().GetAwaiter().GetResult();
    }
  }
}
'@
  Add-Type -TypeDefinition $code -ReferencedAssemblies System.Runtime.WindowsRuntime | Out-Null
}

function Get-InterfaceTypeLabel {
  param($InterfaceType)

  if ($null -eq $InterfaceType) { return "unknown" }

  switch ([int]$InterfaceType) {
    6 { return "ethernet" }
    23 { return "ppp" }
    24 { return "loopback" }
    71 { return "wifi" }
    131 { return "tunnel" }
    default { return "type_$InterfaceType" }
  }
}

function Get-ConnectionProfileMetadata {
  param($Profile)

  $adapterId = $null
  $interfaceType = $null
  try {
    $networkAdapter = $Profile.NetworkAdapter
    if ($networkAdapter) {
      try { $adapterId = [string]$networkAdapter.NetworkAdapterId } catch {}
      try { $interfaceType = [int]$networkAdapter.IanaInterfaceType } catch {}
    }
  } catch {}

  $label = Get-InterfaceTypeLabel $interfaceType
  return [pscustomobject]@{
    profile_name         = [string]$Profile.ProfileName
    adapter_id           = $adapterId
    interface_type       = $interfaceType
    interface_type_label = $label
    is_ethernet          = ($interfaceType -eq 6)
    is_wifi              = ($interfaceType -eq 71)
  }
}

function Load-ManagedEthernetState {
  if (-not (Test-Path $ethernetRestoreStateFile)) { return $null }
  try {
    return (Get-Content $ethernetRestoreStateFile -Raw -ErrorAction Stop | ConvertFrom-Json)
  } catch {
    return $null
  }
}

function Get-MobileHotspotContext {
  Add-WinRtHelpers
  [void][Windows.Networking.Connectivity.NetworkInformation,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows,ContentType=WindowsRuntime]

  $profiles = New-Object System.Collections.Generic.List[object]
  $seenKeys = New-Object System.Collections.Generic.HashSet[string]
  $internet = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if ($internet) {
    $internetMeta = Get-ConnectionProfileMetadata -Profile $internet
    $internetKey = "{0}|{1}" -f $internetMeta.profile_name, $internetMeta.adapter_id
    if ($seenKeys.Add($internetKey)) { [void]$profiles.Add($internet) }
  }
  foreach ($profile in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) {
    if (-not $profile) { continue }
    $meta = Get-ConnectionProfileMetadata -Profile $profile
    $key = "{0}|{1}" -f $meta.profile_name, $meta.adapter_id
    if ($seenKeys.Add($key)) { [void]$profiles.Add($profile) }
  }

  $internetProfileName = if ($internet) { [string]$internet.ProfileName } else { $null }
  $candidates = New-Object System.Collections.Generic.List[object]
  foreach ($profile in $profiles) {
    try {
      $capability = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($profile)
      if ($capability -eq [Windows.Networking.NetworkOperators.TetheringCapability]::Enabled) {
        $meta = Get-ConnectionProfileMetadata -Profile $profile
        [void]$candidates.Add([pscustomobject]@{
          profile_name         = $meta.profile_name
          capability           = [string]$capability
          manager              = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
          interface_type       = $meta.interface_type
          interface_type_label = $meta.interface_type_label
          is_ethernet          = $meta.is_ethernet
          is_wifi              = $meta.is_wifi
          adapter_id           = $meta.adapter_id
          is_internet_preferred = ($internetProfileName -and $internetProfileName -eq $meta.profile_name)
        })
      }
    } catch {}
  }

  $activeCandidate = $null
  foreach ($candidate in $candidates) {
    try {
      if ($candidate.manager.TetheringOperationalState -eq [Windows.Networking.NetworkOperators.TetheringOperationalState]::On) {
        $activeCandidate = $candidate
        break
      }
    } catch {}
  }

  $preferredCandidate = if ($activeCandidate) {
    $activeCandidate
  } else {
    @($candidates | Where-Object { -not $_.is_ethernet } | Select-Object -First 1)[0]
  }
  if (-not $preferredCandidate -and $candidates.Count -gt 0) {
    $preferredCandidate = $candidates[0]
  }

  if (-not $preferredCandidate) { return $null }

  return [pscustomobject]@{
    profile_name = $preferredCandidate.profile_name
    capability = $preferredCandidate.capability
    manager = $preferredCandidate.manager
    interface_type = $preferredCandidate.interface_type
    interface_type_label = $preferredCandidate.interface_type_label
    is_ethernet = $preferredCandidate.is_ethernet
    is_wifi = $preferredCandidate.is_wifi
    adapter_id = $preferredCandidate.adapter_id
    active_profile_name = if ($activeCandidate) { $activeCandidate.profile_name } else { $null }
    active_interface_type = if ($activeCandidate) { $activeCandidate.interface_type } else { $null }
    active_interface_type_label = if ($activeCandidate) { $activeCandidate.interface_type_label } else { $null }
    active_is_ethernet = if ($activeCandidate) { $activeCandidate.is_ethernet } else { $false }
    candidates = @($candidates)
  }
}

function Resolve-HostnameUsingServer {
  param(
    [string]$Domain,
    [string]$Server
  )

  if ([string]::IsNullOrWhiteSpace($Domain) -or [string]::IsNullOrWhiteSpace($Server)) {
    return @()
  }

  try {
    $records = Resolve-DnsName -Name $Domain -Server $Server -Type A -DnsOnly -NoHostsFile -ErrorAction Stop
    return @(
      $records |
        Where-Object { $_.Type -eq "A" -and -not [string]::IsNullOrWhiteSpace($_.IPAddress) } |
        Select-Object -ExpandProperty IPAddress -Unique
    )
  } catch {
    return @()
  }
}

# Resolve paths
$scriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir   = Split-Path -Parent $scriptDir
$toolsDir     = Split-Path -Parent $runtimeDir
$aiboxDir     = Split-Path -Parent $toolsDir
$stackEnvFile = Join-Path $aiboxDir "stack\.env"
$backendDataDir = Join-Path $aiboxDir "backend-data"
$appDataDir = Join-Path $backendDataDir "appdata"
$hotspotStateDir = Join-Path $appDataDir "hotspot"
$ethernetRestoreStateFile = Join-Path $hotspotStateDir "ethernet-restore-state.json"
$hotspotLastResultFile = Join-Path $hotspotStateDir "hotspot-last-result.json"
$portalDir    = Join-Path $aiboxDir "stack\portal"
$outFile      = Join-Path $portalDir "network-info.json"

function Read-EnvValue {
  # Local wrapper preserving the (Key, Default) signature used throughout this
  # script. Routes parsing through lib_env.ps1::Get-DotEnvMap (shared parser)
  # while keeping the historical semantics: process env var wins, otherwise
  # falls back to the stack .env, otherwise $Default.
  param(
    [string]$Key,
    [string]$Default = ""
  )
  $val = [System.Environment]::GetEnvironmentVariable($Key)
  if (-not [string]::IsNullOrWhiteSpace($val)) { return $val }
  if (Test-Path -LiteralPath $stackEnvFile) {
    $map = Get-DotEnvMap -Path $stackEnvFile
    if ($map.ContainsKey($Key)) {
      $val = $map[$Key]
      if (-not [string]::IsNullOrWhiteSpace($val)) { return $val }
    }
  }
  return $Default
}

function Load-LastHotspotResult {
  if (-not (Test-Path $hotspotLastResultFile)) { return $null }
  try {
    return (Get-Content $hotspotLastResultFile -Raw -ErrorAction Stop | ConvertFrom-Json)
  } catch {
    return $null
  }
}

# Read config
$configuredSsid       = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$configuredKey        = Read-EnvValue "HOTSPOT_KEY" "puente1234"
$hotspotEthernetPolicy = ([string](Read-EnvValue "HOTSPOT_ETHERNET_POLICY" "warn")).Trim().ToLowerInvariant()
$hotspotWifiBand = ([string](Read-EnvValue "HOTSPOT_WIFI_BAND" "2.4ghz")).Trim().ToLowerInvariant()
if ($hotspotEthernetPolicy -notin @("disable", "warn", "allow")) {
  $hotspotEthernetPolicy = "warn"
}
$preferredStableIp    = Read-EnvValue "OFFLINE_ACCESS_IP" ""
$preferredHostnameRaw = Read-EnvValue "OFFLINE_HOSTNAME" ""
$preferredHostname    = Normalize-PreferredHostname $preferredHostnameRaw
$hostName             = $env:COMPUTERNAME
$httpPort             = 80

# Probe hotspot status
$hotspotActive   = $false
$hotspotSsid     = $configuredSsid
$hotspotSupport  = "unknown"
$hotspotBackend  = "unknown"
$hotspotProfile  = $null
$hotspotSourceType = $null
$hotspotSourceTypeLabel = "unknown"
$hotspotSourceReady = $false
$hotspotCandidateProfiles = @()
$managedEthernetState = Load-ManagedEthernetState
$lastHotspotResult = Load-LastHotspotResult
$hotspotContext  = Get-MobileHotspotContext

if ($hotspotContext) {
  $hotspotBackend = "mobile_hotspot"
  $hotspotSupport = "available"
  $hotspotProfile = $hotspotContext.profile_name
  $hotspotSourceType = $hotspotContext.interface_type
  $hotspotSourceTypeLabel = $hotspotContext.interface_type_label
  $hotspotSourceReady = (-not $hotspotContext.is_ethernet)
  $hotspotCandidateProfiles = @(
    [ordered]@{
      profile_name = $hotspotContext.profile_name
      interface_type = $hotspotContext.interface_type
      interface_type_label = $hotspotContext.interface_type_label
      is_ethernet = $hotspotContext.is_ethernet
      is_wifi = $hotspotContext.is_wifi
      is_internet_preferred = $true
    }
  )
  try {
    $hotspotActive = ($hotspotContext.manager.TetheringOperationalState -eq [Windows.Networking.NetworkOperators.TetheringOperationalState]::On)
    $currentConfig = $hotspotContext.manager.GetCurrentAccessPointConfiguration()
    if ($hotspotActive -and $currentConfig -and -not [string]::IsNullOrWhiteSpace($currentConfig.Ssid)) {
      $hotspotSsid = $currentConfig.Ssid
    }
  } catch {
    $hotspotSupport = "available_or_unknown"
  }
} else {
  $hostedNetOutput = & netsh wlan show hostednetwork 2>&1
  $hotspotBackend = "hostednetwork_legacy"
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
}

if (-not $hotspotActive -and $lastHotspotResult -and $lastHotspotResult.status -in @("ready", "ip_only")) {
  $hotspotActive = $true
  $hotspotBackend = if ($lastHotspotResult.backend) { [string]$lastHotspotResult.backend } else { "mobile_hotspot" }
  $hotspotSupport = "available"
  if ($lastHotspotResult.ssid) { $hotspotSsid = [string]$lastHotspotResult.ssid }
  if ($lastHotspotResult.details -and $lastHotspotResult.details.source_profile) {
    $hotspotProfile = [string]$lastHotspotResult.details.source_profile.profile_name
    $hotspotSourceType = $lastHotspotResult.details.source_profile.interface_type
    $hotspotSourceTypeLabel = [string]$lastHotspotResult.details.source_profile.interface_type_label
    $hotspotSourceReady = $true
  }
  $hotspotCandidateProfiles = @(
    [ordered]@{
      profile_name = $hotspotProfile
      interface_type = $hotspotSourceType
      interface_type_label = $hotspotSourceTypeLabel
      is_ethernet = $false
      is_wifi = ($hotspotSourceTypeLabel -eq "wifi")
      is_internet_preferred = $true
    }
  )
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

$hotspotHttpReachable = $false
$hotspotDnsReady = $false
$hotspotHostnameReady = $false
$hotspotHostnameTargetIp = $null
$hotspotReadiness = if ($hotspotActive -and $hotspotIp) { "ip_only" } else { "unavailable" }

if ($hotspotIp) {
  $hotspotHttpReachable = Test-TcpReachable -HostName $hotspotIp -Port $httpPort
  $hotspotDnsReady = Test-TcpReachable -HostName $hotspotIp -Port 53
  if ($preferredHostname) {
    $resolvedIps = @(Resolve-HostnameUsingServer -Domain $preferredHostname -Server $hotspotIp)
    if ($resolvedIps.Count -gt 0) {
      $hotspotHostnameTargetIp = [string]$resolvedIps[0]
    }
    $hotspotHostnameReady = ($resolvedIps -contains $hotspotIp)
  }
}

if ($hotspotActive -and $hotspotIp -and $hotspotHttpReachable -and $hotspotDnsReady -and $hotspotHostnameReady) {
  $hotspotReadiness = "ready"
} elseif ($hotspotActive -and $hotspotIp -and $hotspotHttpReachable) {
  $hotspotReadiness = "ip_only"
}

$recommendedMethod = "unknown"
$primaryCandidate = $null

if ($hotspotActive -and $hotspotIp) {
  $recommendedMethod = "hotspot"
  if ($hotspotReadiness -eq "ready" -and $preferredHostname) {
    $primaryCandidate = $preferredHostname
    $notes.Add("Offline hotspot mode is active and nearby clients should be able to use puente.link directly.")
  } else {
    $primaryCandidate = $hotspotIp
    $notes.Add("Offline hotspot mode is the most stable field option on Windows because the hotspot host address normally remains 192.168.137.1.")
  }
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
if ($recommendedMethod -eq "hotspot" -and $hotspotIp) {
  $primaryReachable = Test-TcpReachable -HostName $hotspotIp -Port $httpPort
} elseif ($primaryCandidate) {
  $primaryReachable = Test-TcpReachable -HostName $primaryCandidate -Port $httpPort
} else {
  $primaryReachable = $false
}

if ($primaryCandidate -and -not $primaryReachable -and $httpListening) {
  $warnings.Add("The host selected $primaryCandidate as the client address, but a local TCP check to port 80 failed. Verify Docker/Caddy is up and Windows is bound on the LAN interface.")
}

if ($hotspotSupport -eq "unsupported_or_disabled") {
  $warnings.Add("Windows hotspot mode is not available on this adapter or is disabled.")
}

if ($hotspotActive -and -not $hotspotHttpReachable) {
  $warnings.Add("Hotspot Wi-Fi is active but TCP port 80 is not reachable on $hotspotIp.")
}
if ($hotspotActive -and -not $hotspotDnsReady) {
  $warnings.Add("Hotspot Wi-Fi is active but the laptop is not answering DNS on $hotspotIp:53.")
}
if ($hotspotActive -and -not $hotspotSourceReady) {
  $warnings.Add("Hotspot Wi-Fi is active but Windows selected a wired source profile ($hotspotSourceTypeLabel). AIBox is allowing this so the SSID remains visible and joinable.")
}
if ($hotspotActive -and $preferredHostname -and -not $hotspotHostnameReady) {
  $warnings.Add("$preferredHostname is not resolving to $hotspotIp through the laptop's DNS service yet. Clients may need to use the hotspot IP.")
}

$hostnameCandidates = @()
if ($preferredHostname) {
  if ($hotspotReadiness -eq "ready" -or $recommendedMethod -eq "lan") {
    $hostnameCandidates += "http://$preferredHostname"
  }
  if ($preferredHostname -notmatch "\.") {
    $hostnameCandidates += "http://$preferredHostname.local"
  }
  if ($hotspotReadiness -eq "ready") {
    $notes.Add("OFFLINE_HOSTNAME is validated for hotspot clients and currently resolves through the AIBox DNS service.")
  } else {
    $notes.Add("OFFLINE_HOSTNAME is only advertised when client DNS, mDNS, or hosts entries resolve it to the host IP.")
  }
}
$hostnameCandidates += "http://$($hostName.ToLowerInvariant())"
$hostnameCandidates += "http://$($hostName.ToLowerInvariant()).local"
$hostnameCandidates = @($hostnameCandidates | Select-Object -Unique)

$steps = @()
switch ($recommendedMethod) {
  "hotspot" {
    $steps += "On the client device, join SSID '$hotspotSsid'."
    if ($hotspotReadiness -eq "ready" -and $preferredHostname) {
      $steps += "Open http://$preferredHostname/ in a browser."
      $steps += "If the page does not load, use http://$hotspotIp/ as a temporary fallback and rerun diagnose_local_access.ps1 on the host."
    } else {
      $steps += "Open http://$hotspotIp/ in a browser."
      $steps += "The hotspot is up, but puente.link is not fully ready yet."
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
    status      = if ($hotspotActive) { "active" } else { "inactive" }
    backend     = $hotspotBackend
    support     = $hotspotSupport
    profile     = $hotspotProfile
    ethernet_policy = $hotspotEthernetPolicy
    wifi_band  = $hotspotWifiBand
    readiness   = $hotspotReadiness
    ssid        = $hotspotSsid
    password    = $configuredKey
    host_ip     = $hotspotIp
    dns_server  = $hotspotIp
    source      = [ordered]@{
      interface_type = $hotspotSourceType
      interface_type_label = $hotspotSourceTypeLabel
      source_ready = $hotspotSourceReady
    }
    candidate_profiles = $hotspotCandidateProfiles
    ethernet_restore = if ($managedEthernetState) { [ordered]@{ state_found = $true; adapters = @($managedEthernetState.adapters) } } else { [ordered]@{ state_found = $false; adapters = @() } }
    validation  = [ordered]@{
      http_ready         = $hotspotHttpReachable
      dns_ready          = $hotspotDnsReady
      source_ready       = $hotspotSourceReady
      hostname_ready     = $hotspotHostnameReady
      hostname_target_ip = $hotspotHostnameTargetIp
    }
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

$publicInfo = [ordered]@{
  recommended_method = $info.recommended_method
  primary_url = $info.primary_url
  preferred = $info.preferred
  hotspot = [ordered]@{
    status = $info.hotspot.status
    backend = $info.hotspot.backend
    support = $info.hotspot.support
    ethernet_policy = $info.hotspot.ethernet_policy
    wifi_band = $info.hotspot.wifi_band
    readiness = $info.hotspot.readiness
    ssid = $info.hotspot.ssid
    password = $info.hotspot.password
    host_ip = $info.hotspot.host_ip
    dns_server = $info.hotspot.dns_server
    validation = [ordered]@{
      http_ready = $info.hotspot.validation.http_ready
      dns_ready = $info.hotspot.validation.dns_ready
      hostname_ready = $info.hotspot.validation.hostname_ready
      hostname_target_ip = $info.hotspot.validation.hostname_target_ip
    }
  }
  lan = [ordered]@{
    ips = @($info.lan.ips)
    primary_ip = $info.lan.primary_ip
  }
  hostnames = [ordered]@{
    candidates = @($info.hostnames.candidates)
  }
  http = [ordered]@{
    port = $info.http.port
    listening = $info.http.listening
    firewall_allowed = $info.http.firewall_allowed
    loopback_reachable = $info.http.loopback_reachable
    primary_reachable = $info.http.primary_reachable
  }
  diagnostics = [ordered]@{
    warnings = @($info.diagnostics.warnings)
    notes = @($info.diagnostics.notes)
    steps = @($info.diagnostics.steps)
  }
  generated_at = $info.generated_at
}

$json = $publicInfo | ConvertTo-Json -Depth 6 -Compress:$false

if (-not (Test-Path $portalDir)) {
  if (-not $Quiet) {
    Write-Host "WARNING: Portal directory not found: $portalDir" -ForegroundColor Yellow
  }
} else {
  Write-Utf8NoBom -Path $outFile -Lines @($json -split "`r?`n")
  if (-not $Quiet) {
    Write-Host "[ok] Wrote $outFile" -ForegroundColor Green
  }
}

if (-not $Quiet) {
  Write-Host ""
  Write-Host "Recommended mode : $recommendedMethod"
  if ($hotspotActive) {
    Write-Host "Hotspot          : ACTIVE  SSID: $hotspotSsid  IP: $hotspotIp  Backend: $hotspotBackend" -ForegroundColor Green
    Write-Host "Hotspot readiness: $hotspotReadiness"
    Write-Host "Hotspot source   : $hotspotSourceTypeLabel"
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
    Write-Host "Hostname ready   : $(if ($hotspotHostnameReady) { 'yes' } else { 'no' })"
  }
  foreach ($warning in $warnings) {
    Write-Host "WARN             : $warning" -ForegroundColor Yellow
  }
  Write-Host ""
}

Write-Output $json
