# Starts or stops the Windows Mobile Hotspot so nearby devices can connect to
# AIBox without any outside network. Must be run as Administrator when mutating
# hotspot or firewall state.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\setup_hotspot.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup_hotspot.ps1 -Stop
#   powershell -ExecutionPolicy Bypass -File .\setup_hotspot.ps1 -EmitJson

param(
  [switch]$Stop,
  [switch]$SkipFirewall,
  [switch]$EmitJson,
  [string]$JsonOutFile = ""
)

. (Join-Path $PSScriptRoot 'lib\lib_io.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_env.ps1')

if ($PSVersionTable.PSEdition -ne 'Desktop') {
    Write-Error "This script requires Windows PowerShell 5.1 (powershell.exe). PowerShell 7 cannot use WinRT directly here."
    exit 64
}

$ErrorActionPreference = "Stop"

$scriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir   = Split-Path -Parent $scriptDir
$toolsDir     = Split-Path -Parent $runtimeDir
$aiboxDir     = Split-Path -Parent $toolsDir
$stackEnvFile = Join-Path $aiboxDir "stack\.env"
$backendDataDir = Join-Path $aiboxDir "backend-data"
$appDataDir = Join-Path $backendDataDir "appdata"
$hotspotStateDir = Join-Path $appDataDir "hotspot"
$ethernetRestoreStateFile = Join-Path $hotspotStateDir "ethernet-restore-state.json"
$wifiRestoreStateFile = Join-Path $hotspotStateDir "wifi-restore-state.json"
$hotspotLastResultFile = Join-Path $hotspotStateDir "hotspot-last-result.json"
$script:restoreOnFailure = $false
$script:restoreWifiOnFailure = $false

function Read-EnvValue {
  # Local wrapper preserving the (Key, Default) signature used throughout this
  # script. Routes parsing through lib_env.ps1::Get-DotEnvMap (shared parser)
  # while keeping the historical semantics: process env var wins, otherwise
  # falls back to the stack .env, otherwise $Default.
  param([string]$Key, [string]$Default = "")
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

function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Wait-TetheringState {
  # Polls $Manager.TetheringOperationalState until it reaches $TargetState or
  # the deadline expires. Returns $true if the target was reached.
  #
  # PowerShell 5.1 cannot project WinRT IAsyncOperation<T> / IAsyncAction
  # results returned by .StartTetheringAsync() / .StopTetheringAsync() /
  # .ConfigureAccessPointAsync() - they come back as bare System.__ComObject
  # and .Status returns empty. Polling the manager's operational state side-
  # steps that projection gap.
  param(
    $Manager,
    $TargetState,
    [int]$TimeoutMs = 20000
  )
  $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
  while ([DateTime]::UtcNow -lt $deadline) {
    if ($Manager.TetheringOperationalState -eq $TargetState) { return $true }
    Start-Sleep -Milliseconds 250
  }
  return $Manager.TetheringOperationalState -eq $TargetState
}

function New-Result {
  return [ordered]@{
    ok         = $false
    status     = "unavailable"
    backend    = "unknown"
    ssid       = $null
    password   = $null
    domain     = $null
    host_ip    = $null
    dns_server = $null
    validation = [ordered]@{
      hotspot_active      = $false
      http_ready          = $false
      dns_ready           = $false
      source_ready        = $false
      hostname_ready      = $false
      hostname_target_ip  = $null
    }
    warnings   = New-Object System.Collections.Generic.List[string]
    errors     = New-Object System.Collections.Generic.List[string]
    details    = [ordered]@{}
    generated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
  }
}

function Finalize-Result {
  param(
    [hashtable]$Result,
    [int]$ExitCode = 0
  )

  if ($ExitCode -ne 0 -and $script:restoreOnFailure) {
    try {
      $restoreResult = Restore-ManagedEthernetAdapters -ClearState
      $Result.details.failed_start_restore = $restoreResult
      foreach ($restoreFailure in @($restoreResult.failures)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$restoreFailure)) {
          $Result.errors.Add("Ethernet restore failed after hotspot startup error: $restoreFailure")
        }
      }
    } catch {
      $Result.errors.Add("Ethernet restore failed after hotspot startup error: $($_.Exception.Message)")
    } finally {
      $script:restoreOnFailure = $false
    }
  }

  if ($ExitCode -ne 0 -and $script:restoreWifiOnFailure) {
    try {
      $wifiRestoreResult = Restore-ManagedWifiConnection -ClearState
      $Result.details.failed_start_wifi_restore = $wifiRestoreResult
      foreach ($wifiRestoreFailure in @($wifiRestoreResult.failures)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$wifiRestoreFailure)) {
          $Result.errors.Add("Wi-Fi reconnect failed after hotspot startup error: $wifiRestoreFailure")
        }
      }
    } catch {
      $Result.errors.Add("Wi-Fi reconnect failed after hotspot startup error: $($_.Exception.Message)")
    } finally {
      $script:restoreWifiOnFailure = $false
    }
  }

  $json = $Result | ConvertTo-Json -Depth 8
  try {
    Ensure-HotspotStateDirectory
    Write-Utf8NoBom -Path $hotspotLastResultFile -Lines @($json -split "`r?`n")
  } catch {}
  if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) {
    Write-Utf8NoBom -Path $JsonOutFile -Lines @($json -split "`r?`n")
  }
  if ($EmitJson) {
    Write-Output $json
  }
  exit $ExitCode
}

function Test-TcpReachable {
  param(
    [string]$HostName,
    [int]$Port,
    [int]$TimeoutMs = 1500
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

function Wait-ForHostnameResolutionUsingServer {
  param(
    [string]$Domain,
    [string]$Server,
    [string]$ExpectedIp,
    [int]$TimeoutSeconds = 6
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $resolvedIps = @(Resolve-HostnameUsingServer -Domain $Domain -Server $Server)
    if ($resolvedIps -contains $ExpectedIp) {
      return $resolvedIps
    }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)

  return @(Resolve-HostnameUsingServer -Domain $Domain -Server $Server)
}

function Get-HotspotHostIp {
  try {
    $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
      Where-Object { $_.IPAddress -like "192.168.137.*" -and $_.IPAddress -notlike "169.254.*" } |
      Select-Object -ExpandProperty IPAddress -First 1
    if ($ip) { return $ip }
  } catch {}

  foreach ($line in (& ipconfig)) {
    if ($line -match 'IPv4 Address[^\:]*:\s*([0-9.]+)') {
      if ($Matches[1] -like "192.168.137.*") {
        return $Matches[1]
      }
    }
  }

  return $null
}

function Wait-ForHotspotIp {
  param([int]$TimeoutSeconds = 20)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $ip = Get-HotspotHostIp
    if ($ip) { return $ip }
    Start-Sleep -Seconds 1
  } while ((Get-Date) -lt $deadline)
  return $null
}

$hostsFilePath = "$env:SystemRoot\System32\drivers\etc\hosts"
$hostsTagPrefix = "# AIBox-Puente"

function Set-HostsEntry {
  # ICS's DNS proxy on 192.168.137.1:53 consults the host's hosts file before
  # forwarding upstream, so we map $Domain -> $IpAddress here. This is the
  # only mechanism we found that makes ICS answer for an offline-only domain.
  param([string]$Domain, [string]$IpAddress)
  if ([string]::IsNullOrWhiteSpace($Domain) -or [string]::IsNullOrWhiteSpace($IpAddress)) { return $false }
  try {
    $lines = @()
    if (Test-Path $hostsFilePath) {
      $raw = Get-Content -LiteralPath $hostsFilePath -ErrorAction SilentlyContinue
      if ($raw) { $lines = @($raw) }
    }
    $domainPattern = "\s$([regex]::Escape($Domain))(\s|$)"
    $tagPattern    = [regex]::Escape($hostsTagPrefix)
    $filtered = @($lines | Where-Object { $_ -and ($_ -notmatch $domainPattern) -and ($_ -notmatch $tagPattern) })
    $newLine  = "$IpAddress $Domain $hostsTagPrefix"
    $newContent = $filtered + @($newLine)
    $tmpHosts = "$hostsFilePath.aibox-tmp"
    [System.IO.File]::WriteAllLines($tmpHosts, [string[]]$newContent, [System.Text.Encoding]::ASCII)
    Move-FileAtomic -Source $tmpHosts -Destination $hostsFilePath
    return $true
  } catch {
    Write-Host "      ! Set-HostsEntry failed: $($_.Exception.Message)" -ForegroundColor Yellow
    return $false
  }
}

function Remove-HostsEntry {
  param([string]$Domain)
  try {
    if (-not (Test-Path $hostsFilePath)) { return $true }
    $raw = Get-Content -LiteralPath $hostsFilePath -ErrorAction SilentlyContinue
    if (-not $raw) { return $true }
    $lines = @($raw)
    $filtered = @($lines | Where-Object { $_ -notmatch [regex]::Escape($hostsTagPrefix) })
    if ($filtered.Count -ne $lines.Count) {
      $tmpHosts = "$hostsFilePath.aibox-tmp"
      [System.IO.File]::WriteAllLines($tmpHosts, [string[]]$filtered, [System.Text.Encoding]::ASCII)
      Move-FileAtomic -Source $tmpHosts -Destination $hostsFilePath
    }
    return $true
  } catch {
    return $false
  }
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

function Ensure-HotspotStateDirectory {
  if (-not (Test-Path $hotspotStateDir)) {
    New-Item -ItemType Directory -Path $hotspotStateDir -Force | Out-Null
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

function Get-NetAdapterFromAdapterId {
  param([string]$AdapterId)

  if ([string]::IsNullOrWhiteSpace($AdapterId)) { return $null }
  try {
    $guid = [guid]$AdapterId
  } catch {
    return $null
  }

  try {
    return Get-NetAdapter -IncludeHidden -ErrorAction Stop |
      Where-Object { $_.InterfaceGuid -eq $guid } |
      Select-Object -First 1
  } catch {
    return $null
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

function Save-ManagedEthernetState {
  param([array]$Adapters)

  Ensure-HotspotStateDirectory
  $state = [ordered]@{
    disabled_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
    adapters = @(
      $Adapters | ForEach-Object {
        [ordered]@{
          name = $_.Name
          interface_guid = [string]$_.InterfaceGuid
          interface_description = $_.InterfaceDescription
        }
      }
    )
  }
  $json = $state | ConvertTo-Json -Depth 6
  Write-Utf8NoBom -Path $ethernetRestoreStateFile -Lines @($json -split "`r?`n")
  return $state
}

function Clear-ManagedEthernetState {
  if (Test-Path $ethernetRestoreStateFile) {
    Remove-Item -LiteralPath $ethernetRestoreStateFile -Force -ErrorAction SilentlyContinue
  }
}

function Get-ConnectedWifiProfile {
  $raw = ""
  try {
    $raw = (& netsh wlan show interfaces 2>&1 | Out-String)
  } catch {
    return $null
  }

  if ($raw -notmatch "(?im)^\s*State\s*:\s*connected\s*$") { return $null }

  $interfaceName = $null
  $ssid = $null
  $profileName = $null
  $channel = $null
  $radioType = $null

  if ($raw -match "(?im)^\s*Name\s*:\s*(.+?)\s*$") { $interfaceName = $Matches[1].Trim() }
  if ($raw -match "(?im)^\s*SSID\s*:\s*(.+?)\s*$") { $ssid = $Matches[1].Trim() }
  if ($raw -match "(?im)^\s*Profile\s*:\s*(.+?)\s*$") { $profileName = $Matches[1].Trim() }
  if ($raw -match "(?im)^\s*Channel\s*:\s*(\d+)\s*$") { $channel = [int]$Matches[1] }
  if ($raw -match "(?im)^\s*Radio type\s*:\s*(.+?)\s*$") { $radioType = $Matches[1].Trim() }
  if ([string]::IsNullOrWhiteSpace($profileName)) { $profileName = $ssid }

  if ([string]::IsNullOrWhiteSpace($interfaceName) -or [string]::IsNullOrWhiteSpace($profileName)) {
    return $null
  }

  return [pscustomobject]@{
    interface_name = $interfaceName
    profile_name = $profileName
    ssid = $ssid
    channel = $channel
    radio_type = $radioType
  }
}

function Save-ManagedWifiState {
  param($Profile)
  if (-not $Profile) { return $null }
  Ensure-HotspotStateDirectory
  $state = [ordered]@{
    disconnected_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
    interface_name = [string]$Profile.interface_name
    profile_name = [string]$Profile.profile_name
    ssid = [string]$Profile.ssid
    channel = $Profile.channel
    radio_type = [string]$Profile.radio_type
  }
  $json = $state | ConvertTo-Json -Depth 5
  Write-Utf8NoBom -Path $wifiRestoreStateFile -Lines @($json -split "`r?`n")
  return $state
}

function Load-ManagedWifiState {
  if (-not (Test-Path $wifiRestoreStateFile)) { return $null }
  try {
    return (Get-Content $wifiRestoreStateFile -Raw -ErrorAction Stop | ConvertFrom-Json)
  } catch {
    return $null
  }
}

function Clear-ManagedWifiState {
  if (Test-Path $wifiRestoreStateFile) {
    Remove-Item -LiteralPath $wifiRestoreStateFile -Force -ErrorAction SilentlyContinue
  }
}

function Disconnect-UpstreamWifiForHotspot {
  $disconnectResult = [ordered]@{
    attempted = $false
    disconnected = $false
    skipped_reason = $null
    profile = $null
    failures = @()
  }

  $profile = Get-ConnectedWifiProfile
  if (-not $profile) {
    $disconnectResult.skipped_reason = "no_connected_wifi_profile"
    return $disconnectResult
  }

  $disconnectResult.attempted = $true
  $disconnectResult.profile = [ordered]@{
    interface_name = $profile.interface_name
    profile_name = $profile.profile_name
    ssid = $profile.ssid
    channel = $profile.channel
    radio_type = $profile.radio_type
  }

  try {
    [void](Save-ManagedWifiState -Profile $profile)
    Write-Host "      + Saved upstream Wi-Fi profile '$($profile.profile_name)' for reconnect on stop."
    $netshArgs = @('wlan', 'disconnect', "interface=$($profile.interface_name)")
    $output = & netsh @netshArgs 2>&1
    $disconnectResult.output = @($output | ForEach-Object { [string]$_ })
    Start-Sleep -Seconds 3
    $after = Get-ConnectedWifiProfile
    if ($after -and $after.interface_name -eq $profile.interface_name) {
      $disconnectResult.failures += "Wi-Fi interface '$($profile.interface_name)' is still connected to '$($after.profile_name)' after disconnect."
    } else {
      $disconnectResult.disconnected = $true
      $script:restoreWifiOnFailure = $true
    }
  } catch {
    $disconnectResult.failures += "Failed to disconnect upstream Wi-Fi: $($_.Exception.Message)"
  }

  return $disconnectResult
}

function Restore-ManagedWifiConnection {
  param([switch]$ClearState)

  $restore = [ordered]@{
    state_file = $wifiRestoreStateFile
    state_found = $false
    attempted = $false
    reconnected = $false
    profile_name = $null
    interface_name = $null
    failures = @()
  }

  $state = Load-ManagedWifiState
  if (-not $state) {
    if ($ClearState) { Clear-ManagedWifiState }
    return $restore
  }

  $restore.state_found = $true
  $restore.profile_name = [string]$state.profile_name
  $restore.interface_name = [string]$state.interface_name
  if ([string]::IsNullOrWhiteSpace($restore.profile_name)) {
    $restore.failures += "Saved Wi-Fi reconnect state did not include a profile name."
    return $restore
  }

  try {
    $restore.attempted = $true
    $args = @("wlan", "connect", "name=$($restore.profile_name)")
    if (-not [string]::IsNullOrWhiteSpace($restore.interface_name)) {
      $args += "interface=$($restore.interface_name)"
    }
    $output = & netsh @args 2>&1
    $restore.output = @($output | ForEach-Object { [string]$_ })
    Start-Sleep -Seconds 4
    $connected = Get-ConnectedWifiProfile
    $restore.reconnected = ($connected -and $connected.profile_name -eq $restore.profile_name)
    if (-not $restore.reconnected) {
      $restore.failures += "Windows did not reconnect to Wi-Fi profile '$($restore.profile_name)' within the validation window."
    }
  } catch {
    $restore.failures += "Failed to reconnect Wi-Fi profile '$($restore.profile_name)': $($_.Exception.Message)"
  }

  if ($ClearState -and $restore.failures.Count -eq 0) {
    Clear-ManagedWifiState
  }

  return $restore
}

function Restore-ManagedEthernetAdapters {
  param([switch]$ClearState)

  $restore = [ordered]@{
    state_file = $ethernetRestoreStateFile
    state_found = $false
    restored = @()
    failures = @()
  }

  $state = Load-ManagedEthernetState
  if (-not $state) {
    if ($ClearState) { Clear-ManagedEthernetState }
    return $restore
  }

  $restore.state_found = $true
  foreach ($entry in @($state.adapters)) {
    if (-not $entry) { continue }
    $adapter = $null
    if ($entry.interface_guid) {
      $adapter = Get-NetAdapterFromAdapterId -AdapterId ([string]$entry.interface_guid)
    }
    if (-not $adapter -and $entry.name) {
      try {
        $adapter = Get-NetAdapter -Name ([string]$entry.name) -IncludeHidden -ErrorAction SilentlyContinue
      } catch {}
    }
    if (-not $adapter) {
      $restore.failures += "Managed Ethernet adapter '$($entry.name)' could not be found for restore."
      continue
    }
    try {
      Enable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction Stop | Out-Null
      $restore.restored += [ordered]@{
        name = $adapter.Name
        interface_guid = [string]$adapter.InterfaceGuid
      }
    } catch {
      $restore.failures += "Failed to re-enable Ethernet adapter '$($adapter.Name)': $($_.Exception.Message)"
    }
  }

  if ($ClearState -and $restore.failures.Count -eq 0) {
    Clear-ManagedEthernetState
  }

  return $restore
}

function Configure-FirewallRules {
  if ($SkipFirewall) {
    Write-Host "[3/5] Skipping firewall (--SkipFirewall)."
    return
  }

  Write-Host "[3/5] Verifying Windows Firewall inbound rules..."
  $httpPort = [int](Read-EnvValue "HTTP_PORT" "80")
  $dnsPort  = [int](Read-EnvValue "DNS_PORT"  "53")
  $rules = @(
    @{ Name = "AIBox-HTTP-Inbound-$httpPort";    Proto = "TCP"; Port = $httpPort; Desc = "AIBox portal (HTTP $httpPort)" },
    @{ Name = "AIBox-DNS-TCP-Inbound-$dnsPort";  Proto = "TCP"; Port = $dnsPort;  Desc = "AIBox DNS (TCP $dnsPort)" },
    @{ Name = "AIBox-DNS-UDP-Inbound-$dnsPort";  Proto = "UDP"; Port = $dnsPort;  Desc = "AIBox DNS (UDP $dnsPort)" }
  )

  foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if (-not $existing) {
      New-NetFirewallRule `
        -DisplayName $rule.Name `
        -Direction Inbound `
        -Protocol $rule.Proto `
        -LocalPort $rule.Port `
        -Action Allow `
        -Profile Any `
        -Description $rule.Desc | Out-Null
      Write-Host "      + Created rule: $($rule.Name)"
    } else {
      Write-Host "      = Rule already exists: $($rule.Name)"
    }
  }
}

function Get-TetheringCandidates {
  [void][Windows.Networking.Connectivity.NetworkInformation,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.TetheringWiFiBand,Windows,ContentType=WindowsRuntime]

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
      if ($capability -ne [Windows.Networking.NetworkOperators.TetheringCapability]::Enabled) { continue }
      $meta = Get-ConnectionProfileMetadata -Profile $profile
      $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
      [void]$candidates.Add([pscustomobject]@{
        Profile             = $profile
        Capability          = [string]$capability
        Manager             = $manager
        ProfileName         = $meta.profile_name
        AdapterId           = $meta.adapter_id
        InterfaceType       = $meta.interface_type
        InterfaceTypeLabel  = $meta.interface_type_label
        IsEthernet          = $meta.is_ethernet
        IsWiFi              = $meta.is_wifi
        IsInternetPreferred = ($internetProfileName -and $internetProfileName -eq $meta.profile_name)
      })
    } catch {}
  }

  return $candidates.ToArray()
}

function Get-TetheringContext {
  $candidates = @(Get-TetheringCandidates)
  $activeCandidate = $null

  foreach ($candidate in $candidates) {
    try {
      if ($candidate.Manager.TetheringOperationalState -eq [Windows.Networking.NetworkOperators.TetheringOperationalState]::On) {
        $activeCandidate = $candidate
        break
      }
    } catch {}
  }

  $preferredNonEthernet = @($candidates | Where-Object { -not $_.IsEthernet })
  $ethernetCandidates = @($candidates | Where-Object { $_.IsEthernet })
  $selected = if ($activeCandidate) {
    $activeCandidate
  } elseif ($preferredNonEthernet.Count -gt 0) {
    $preferredNonEthernet[0]
  } elseif ($candidates.Count -gt 0) {
    $candidates[0]
  } else {
    $null
  }

  return [pscustomobject]@{
    Selected = $selected
    ActiveCandidate = $activeCandidate
    Candidates = $candidates
    NonEthernetCandidates = $preferredNonEthernet
    EthernetCandidates = $ethernetCandidates
  }
}

function Stop-AnyActiveHotspot {
  param($Context)

  $stopResult = [ordered]@{
    attempted = @()
    stopped = @()
    failures = @()
    active_found = $false
  }

  foreach ($candidate in @($Context.Candidates)) {
    if (-not $candidate -or -not $candidate.Manager) { continue }
    $currentState = $null
    try {
      $currentState = [string]$candidate.Manager.TetheringOperationalState
    } catch {
      $stopResult.failures += "Could not read hotspot state for '$($candidate.ProfileName)': $($_.Exception.Message)"
      continue
    }
    if ($currentState -ne "On") { continue }

    $stopResult.active_found = $true
    $stopResult.attempted += [ordered]@{
      profile_name = $candidate.ProfileName
      interface_type_label = $candidate.InterfaceTypeLabel
    }

    try {
      [void]$candidate.Manager.StopTetheringAsync()
      $reached = Wait-TetheringState -Manager $candidate.Manager -TargetState ([Windows.Networking.NetworkOperators.TetheringOperationalState]::Off) -TimeoutMs 20000
      if (-not $reached) {
        $stopResult.failures += "Hotspot profile '$($candidate.ProfileName)' did not return to Off state within timeout."
      } else {
        $stopResult.stopped += [ordered]@{
          profile_name = $candidate.ProfileName
          interface_type_label = $candidate.InterfaceTypeLabel
        }
      }
    } catch {
      $stopResult.failures += "Failed to stop hotspot profile '$($candidate.ProfileName)': $($_.Exception.Message)"
    }
  }

  return $stopResult
}

function Get-DisableEligibleEthernetAdapters {
  param($Context)

  $adapters = New-Object System.Collections.Generic.List[object]
  $seen = New-Object System.Collections.Generic.HashSet[string]
  foreach ($candidate in @($Context.EthernetCandidates)) {
    $adapter = Get-NetAdapterFromAdapterId -AdapterId $candidate.AdapterId
    if (-not $adapter) { continue }
    $guidText = [string]$adapter.InterfaceGuid
    if (-not $seen.Add($guidText)) { continue }
    if ($adapter.Status -eq "Disabled" -or $adapter.Status -eq "Not Present") { continue }
    if ($adapter.HardwareInterface -eq $false) { continue }
    [void]$adapters.Add($adapter)
  }
  return $adapters.ToArray()
}

function Disable-ManagedEthernetAdapters {
  param([array]$Adapters)

  $disableResult = [ordered]@{
    disabled = @()
    failures = @()
    state_file = $ethernetRestoreStateFile
  }

  $disabledAdapters = New-Object System.Collections.Generic.List[object]
  foreach ($adapter in @($Adapters)) {
    if (-not $adapter) { continue }
    try {
      Disable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction Stop | Out-Null
      [void]$disabledAdapters.Add($adapter)
      $disableResult.disabled += [ordered]@{
        name = $adapter.Name
        interface_guid = [string]$adapter.InterfaceGuid
        interface_description = $adapter.InterfaceDescription
      }
    } catch {
      $disableResult.failures += "Failed to disable Ethernet adapter '$($adapter.Name)': $($_.Exception.Message)"
    }
  }

  if ($disabledAdapters.Count -gt 0) {
    [void](Save-ManagedEthernetState -Adapters @($disabledAdapters))
  }

  return $disableResult
}

function Get-RequestedWifiBand {
  param([string]$RawValue)

  $value = ([string]$RawValue).Trim().ToLowerInvariant()
  switch ($value) {
    "2.4" { return [pscustomobject]@{ Label = "2.4ghz"; Enum = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::TwoPointFourGigahertz } }
    "2.4ghz" { return [pscustomobject]@{ Label = "2.4ghz"; Enum = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::TwoPointFourGigahertz } }
    "5" { return [pscustomobject]@{ Label = "5ghz"; Enum = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::FiveGigahertz } }
    "5ghz" { return [pscustomobject]@{ Label = "5ghz"; Enum = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::FiveGigahertz } }
    "6" { return [pscustomobject]@{ Label = "6ghz"; Enum = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::SixGigahertz } }
    "6ghz" { return [pscustomobject]@{ Label = "6ghz"; Enum = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::SixGigahertz } }
    default { return [pscustomobject]@{ Label = "auto"; Enum = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::Auto } }
  }
}

$ssid = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$key  = Read-EnvValue "HOTSPOT_KEY"  "puente1234"
$preferredHostname = Read-EnvValue "OFFLINE_HOSTNAME" "puente.link"
$ethernetPolicy = ([string](Read-EnvValue "HOTSPOT_ETHERNET_POLICY" "warn")).Trim().ToLowerInvariant()
$wifiClientPolicy = ([string](Read-EnvValue "HOTSPOT_WIFI_CLIENT_POLICY" "disconnect")).Trim().ToLowerInvariant()
$wifiBand = Get-RequestedWifiBand -RawValue (Read-EnvValue "HOTSPOT_WIFI_BAND" "2.4ghz")
if ($ethernetPolicy -notin @("disable", "warn", "allow")) {
  $ethernetPolicy = "warn"
}
if ($wifiClientPolicy -notin @("disconnect", "warn", "allow")) {
  $wifiClientPolicy = "disconnect"
}

$result = New-Result
$result.ssid = $ssid
$result.password = $key
$result.domain = $preferredHostname
$result.details.ethernet_policy = $ethernetPolicy
$result.details.wifi_client_policy = $wifiClientPolicy
$result.details.wifi_band = $wifiBand.Label

if (-not (Test-IsAdministrator)) {
  $result.errors.Add("Hotspot setup requires an Administrator PowerShell session.")
  Finalize-Result -Result $result -ExitCode 1
}

Write-Host ""
Write-Host "=== AIBox Hotspot Setup ===" -ForegroundColor Cyan
Write-Host ""

$wlanSvc = Get-Service -Name "WlanSvc" -ErrorAction SilentlyContinue
if (-not $wlanSvc) {
  $result.errors.Add("WLAN service (WlanSvc) not found. A Wi-Fi adapter is required.")
  Write-Host "ERROR: WLAN service (WlanSvc) not found. Wi-Fi adapter required." -ForegroundColor Red
  Finalize-Result -Result $result -ExitCode 1
}
if ($wlanSvc.Status -ne "Running") {
  $result.errors.Add("WLAN service is not running.")
  Write-Host "ERROR: WLAN service is not running (status: $($wlanSvc.Status))." -ForegroundColor Red
  Write-Host "Enable Wi-Fi in Windows Settings and try again." -ForegroundColor Yellow
  Finalize-Result -Result $result -ExitCode 1
}

$context = $null
try {
  $context = Get-TetheringContext
} catch {
  $result.errors.Add("Failed to enumerate tethering profiles: $($_.Exception.Message)")
  Write-Host "ERROR: Could not query Windows Mobile Hotspot capabilities." -ForegroundColor Red
  Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
  Finalize-Result -Result $result -ExitCode 1
}
$candidateDiagnostics = @(
  @($(if ($context) { $context.Candidates } else { @() })) | ForEach-Object {
    [ordered]@{
      profile_name = $_.ProfileName
      capability = $_.Capability
      interface_type = $_.InterfaceType
      interface_type_label = $_.InterfaceTypeLabel
      is_ethernet = $_.IsEthernet
      is_wifi = $_.IsWiFi
      is_internet_preferred = $_.IsInternetPreferred
    }
  }
)
$result.details.candidate_profiles = $candidateDiagnostics

if ($Stop) {
  Write-Host "Stopping Windows Mobile Hotspot..."
  $stopResult = Stop-AnyActiveHotspot -Context $context
  $result.details.hotspot_stop = $stopResult
  foreach ($stopFailure in @($stopResult.failures)) {
    $result.errors.Add([string]$stopFailure)
  }
  if (-not $stopResult.active_found) {
    $result.details.stop_reached_off = $true
    Write-Host "      = Hotspot already off."
  }
  $hostsRemoved = Remove-HostsEntry -Domain $preferredHostname
  $result.details.hosts_entry_removed = $hostsRemoved
  $restoreResult = Restore-ManagedEthernetAdapters -ClearState
  $result.details.ethernet_restore = $restoreResult
  foreach ($restoreFailure in @($restoreResult.failures)) {
    $result.errors.Add($restoreFailure)
  }
  if ($restoreResult.state_found -and @($restoreResult.restored).Count -gt 0) {
    Write-Host "      + Restored AIBox-managed Ethernet adapters."
  }
  $wifiRestoreResult = Restore-ManagedWifiConnection -ClearState
  $result.details.wifi_restore = $wifiRestoreResult
  foreach ($wifiRestoreFailure in @($wifiRestoreResult.failures)) {
    $result.errors.Add($wifiRestoreFailure)
  }
  if ($wifiRestoreResult.state_found -and $wifiRestoreResult.attempted) {
    if ($wifiRestoreResult.reconnected) {
      Write-Host "      + Reconnected upstream Wi-Fi profile '$($wifiRestoreResult.profile_name)'."
    } else {
      Write-Host "      ! Upstream Wi-Fi reconnect attempted but not confirmed." -ForegroundColor Yellow
    }
  }
  $result.ok = ($result.errors.Count -eq 0)
  $result.status = "stopped"
  $exitCode = if ($result.errors.Count -gt 0) { 1 } else { 0 }
  Finalize-Result -Result $result -ExitCode $exitCode
}

if (-not $context.Selected) {
  $result.errors.Add("Windows Mobile Hotspot is not available on the current adapter/profile.")
  $result.warnings.Add("This laptop cannot expose a Mobile Hotspot through the Windows tethering APIs right now.")
  $result.warnings.Add("Check that Wi-Fi is enabled and the adapter supports Mobile Hotspot on this Windows build.")
  Write-Host "ERROR: Windows Mobile Hotspot is not available on this machine right now." -ForegroundColor Red
  Write-Host "Enable Wi-Fi and verify the adapter supports Mobile Hotspot in Windows Settings." -ForegroundColor Yellow
  Finalize-Result -Result $result -ExitCode 1
}

$selectedCandidate = $context.Selected
if ($selectedCandidate.IsEthernet -and $ethernetPolicy -eq "disable") {
  Write-Host "[0/5] Ethernet-backed hotspot source detected. Disabling Ethernet adapters before hotspot start..."
  $adaptersToDisable = @(Get-DisableEligibleEthernetAdapters -Context $context)
  if ($adaptersToDisable.Count -eq 0) {
    $result.errors.Add("Windows selected Ethernet as the hotspot source, but no eligible Ethernet adapter could be disabled automatically.")
    $result.details.source_profile = [ordered]@{
      profile_name = $selectedCandidate.ProfileName
      interface_type = $selectedCandidate.InterfaceType
      interface_type_label = $selectedCandidate.InterfaceTypeLabel
      is_ethernet = $selectedCandidate.IsEthernet
    }
    Finalize-Result -Result $result -ExitCode 1
  }

  $disableResult = Disable-ManagedEthernetAdapters -Adapters $adaptersToDisable
  $result.details.ethernet_disable = $disableResult
  foreach ($disableFailure in @($disableResult.failures)) {
    $result.errors.Add($disableFailure)
  }
  if (@($disableResult.disabled).Count -gt 0) {
    $script:restoreOnFailure = $true
    Start-Sleep -Seconds 3
  }

  $context = Get-TetheringContext
  $result.details.candidate_profiles_after_isolation = @(
    $context.Candidates | ForEach-Object {
      [ordered]@{
        profile_name = $_.ProfileName
        capability = $_.Capability
        interface_type = $_.InterfaceType
        interface_type_label = $_.InterfaceTypeLabel
        is_ethernet = $_.IsEthernet
        is_wifi = $_.IsWiFi
        is_internet_preferred = $_.IsInternetPreferred
      }
    }
  )
  if (-not $context.Selected -or $context.Selected.IsEthernet) {
    $result.errors.Add("Windows still exposes only an Ethernet-backed tethering profile after Ethernet isolation. Offline Wi-Fi-only hotspot mode is unavailable.")
    Finalize-Result -Result $result -ExitCode 1
  }
  $selectedCandidate = $context.Selected
} elseif ($selectedCandidate.IsEthernet) {
  $result.warnings.Add("Windows selected an Ethernet-backed hotspot source. AIBox is allowing it so the hotspot remains visible and joinable on this machine.")
}

$manager = $selectedCandidate.Manager
$result.backend = "mobile_hotspot"
$result.details.source_profile = [ordered]@{
  profile_name = $selectedCandidate.ProfileName
  interface_type = $selectedCandidate.InterfaceType
  interface_type_label = $selectedCandidate.InterfaceTypeLabel
  is_ethernet = $selectedCandidate.IsEthernet
  is_wifi = $selectedCandidate.IsWiFi
}
$result.details.tethering_capability = [string]$selectedCandidate.Capability
$result.details.rejected_profiles = @(
  @($context.Candidates) |
    Where-Object { $_.ProfileName -ne $selectedCandidate.ProfileName -or $_.AdapterId -ne $selectedCandidate.AdapterId } |
    ForEach-Object {
      [ordered]@{
        profile_name = $_.ProfileName
        interface_type = $_.InterfaceType
        interface_type_label = $_.InterfaceTypeLabel
        rejection_reason = if ($_.IsEthernet -and -not $selectedCandidate.IsEthernet) { "wired_source_rejected" } else { "lower_priority_candidate" }
      }
    }
)

Write-Host "SSID     : $ssid"
Write-Host "Password : $key"
Write-Host "Band     : $($wifiBand.Label)"
Write-Host "Policy   : Ethernet source = $ethernetPolicy"
Write-Host "Policy   : Upstream Wi-Fi = $wifiClientPolicy"
Write-Host ""

Write-Host "[1/5] Configuring Windows Mobile Hotspot..."
try {
  $config = $manager.GetCurrentAccessPointConfiguration()
  $config.Ssid = $ssid
  $config.Passphrase = $key
  try {
    $config.Band = $wifiBand.Enum
  } catch {}
  [void]$manager.ConfigureAccessPointAsync($config)
  # Configuration is applied quickly; the WinRT async result isn't inspectable
  # from PS 5.1, so we pause briefly and re-read the applied SSID to confirm.
  Start-Sleep -Milliseconds 1500
  $applied = $manager.GetCurrentAccessPointConfiguration()
  $result.details.applied_ssid = $applied.Ssid
  try {
    $result.details.applied_band = [string]$applied.Band
    $result.details.band_ignored = ([string]$applied.Band -ne [string]$wifiBand.Enum)
  } catch {
    $result.details.applied_band = $null
    $result.details.band_ignored = $false
  }
  if ($applied.Ssid -ne $ssid) {
    $result.warnings.Add("Hotspot SSID did not persist as expected (wanted '$ssid', got '$($applied.Ssid)').")
  }
  Write-Host "      + Access point configuration updated."
} catch {
  $result.errors.Add("Failed to configure Mobile Hotspot: $($_.Exception.Message)")
  Write-Host "ERROR: Failed to configure Windows Mobile Hotspot." -ForegroundColor Red
  Finalize-Result -Result $result -ExitCode 1
}

Write-Host "[2/5] Starting Windows Mobile Hotspot..."
try {
  if ($manager.TetheringOperationalState -ne [Windows.Networking.NetworkOperators.TetheringOperationalState]::On) {
    if ($wifiClientPolicy -eq "disconnect") {
      Write-Host "      + Checking for upstream Wi-Fi client connection before hotspot start..."
      $wifiDisconnectResult = Disconnect-UpstreamWifiForHotspot
      $result.details.wifi_client_disconnect = $wifiDisconnectResult
      foreach ($wifiDisconnectFailure in @($wifiDisconnectResult.failures)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$wifiDisconnectFailure)) {
          $result.errors.Add([string]$wifiDisconnectFailure)
        }
      }
      if (@($wifiDisconnectResult.failures).Count -gt 0) {
        Write-Host "ERROR: Could not isolate the Wi-Fi radio for Mobile Hotspot." -ForegroundColor Red
        Finalize-Result -Result $result -ExitCode 1
      }
      if ($wifiDisconnectResult.disconnected) {
        $result.warnings.Add("Disconnected upstream Wi-Fi profile '$($wifiDisconnectResult.profile.profile_name)' so the Mobile Hotspot SSID can broadcast reliably on this single-radio adapter.")
      }
    } elseif ($wifiClientPolicy -eq "warn") {
      $connectedWifi = Get-ConnectedWifiProfile
      if ($connectedWifi) {
        $result.warnings.Add("The laptop is still connected to upstream Wi-Fi profile '$($connectedWifi.profile_name)'. On single-radio adapters this can prevent the hotspot SSID from appearing.")
        $result.details.connected_wifi_warning = $connectedWifi
      }
    }
    [void]$manager.StartTetheringAsync()
    $reached = Wait-TetheringState -Manager $manager -TargetState ([Windows.Networking.NetworkOperators.TetheringOperationalState]::On) -TimeoutMs 25000
    $result.details.start_reached_on = $reached
    $result.details.start_final_state = [string]$manager.TetheringOperationalState
  if (-not $reached) {
      $message = "Windows Mobile Hotspot did not reach the On state (current: $($manager.TetheringOperationalState))."
      $result.errors.Add($message)
      Write-Host "ERROR: $message" -ForegroundColor Red
      Write-Host "Check Windows Settings > Network & Internet > Mobile hotspot and ensure Wi-Fi can be shared." -ForegroundColor Yellow
      Finalize-Result -Result $result -ExitCode 1
    }
  } else {
    Write-Host "      = Hotspot already running."
  }
} catch {
  $result.errors.Add("Windows Mobile Hotspot threw an exception while starting: $($_.Exception.Message)")
  Write-Host "ERROR: Windows Mobile Hotspot could not be started." -ForegroundColor Red
  Write-Host $_.Exception.Message -ForegroundColor Yellow
  Finalize-Result -Result $result -ExitCode 1
}

Start-Sleep -Seconds 4

Configure-FirewallRules

Write-Host "[4/5] Validating hotspot and offline DNS..."
$hostIp = Wait-ForHotspotIp -TimeoutSeconds 20
$result.host_ip = $hostIp
$result.dns_server = $hostIp
$result.validation.hotspot_active = ($manager.TetheringOperationalState -eq [Windows.Networking.NetworkOperators.TetheringOperationalState]::On)
$result.validation.source_ready = (-not $selectedCandidate.IsEthernet)

if (-not $hostIp) {
  $result.errors.Add("The hotspot started but no 192.168.137.x host IP appeared on the laptop.")
  Write-Host "ERROR: Hotspot IP was not assigned on the host." -ForegroundColor Red
  Finalize-Result -Result $result -ExitCode 1
}

$result.validation.http_ready = Test-TcpReachable -HostName $hostIp -Port 80
$result.validation.dns_ready = Test-TcpReachable -HostName $hostIp -Port 53

if (-not $result.validation.http_ready) {
  $result.errors.Add("Port 80 is not reachable on $hostIp. Clients will not be able to open the portal yet.")
}

# ICS DNS proxy on $hostIp:53 consults the host hosts file before forwarding
# upstream. Mapping $preferredHostname -> $hostIp here is what makes clients
# resolve the offline domain via their default gateway.
$hostsAdded = Set-HostsEntry -Domain $preferredHostname -IpAddress $hostIp
$result.details.hosts_entry_added = $hostsAdded
if (-not $hostsAdded) {
  $result.warnings.Add("Could not write hosts entry for $preferredHostname - clients may have to use http://$hostIp/.")
} else {
  Start-Sleep -Milliseconds 500
}

$result.validation.dns_ready = Test-TcpReachable -HostName $hostIp -Port 53
$resolvedIps = @(Wait-ForHostnameResolutionUsingServer -Domain $preferredHostname -Server $hostIp -ExpectedIp $hostIp -TimeoutSeconds 6)
if ($resolvedIps.Count -gt 0) {
  $result.validation.hostname_target_ip = [string]$resolvedIps[0]
}
$result.validation.hostname_ready = ($resolvedIps -contains $hostIp)

Write-Host "[5/5] Refreshing portal connection info..."
$netInfoScript = Join-Path $scriptDir "get_network_info.ps1"
if (Test-Path $netInfoScript) {
  try {
    & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet | Out-Null
  } catch {
    $result.warnings.Add("Could not refresh network-info.json after hotspot setup.")
  }
}

if (-not $result.validation.dns_ready) {
  $result.warnings.Add("The laptop is not answering TCP DNS requests on port 53 at $hostIp.")
}
if (-not $result.validation.hostname_ready) {
  if ($hostsAdded) {
    $result.warnings.Add("$preferredHostname does not currently resolve to $hostIp through the hotspot DNS proxy even though the hosts entry was written.")
  } else {
    $result.warnings.Add("$preferredHostname does not currently resolve to $hostIp through the laptop's local DNS service.")
  }
}

if ($result.validation.hotspot_active -and $result.validation.http_ready -and $result.validation.dns_ready -and $result.validation.hostname_ready) {
  $result.ok = $true
  $result.status = "ready"
} elseif ($result.validation.hotspot_active -and $result.validation.http_ready) {
  $result.ok = $true
  $result.status = "ip_only"
} else {
  $result.ok = $false
  $result.status = "unavailable"
}

Write-Host ""
if ($result.status -eq "ready") {
  Write-Host "==========================================" -ForegroundColor Green
  Write-Host "  Hotspot Ready" -ForegroundColor Green
  Write-Host "==========================================" -ForegroundColor Green
  Write-Host "  Join Wi-Fi : $ssid" -ForegroundColor Cyan
  Write-Host "  Password   : $key" -ForegroundColor Cyan
  Write-Host "  Source     : $($selectedCandidate.InterfaceTypeLabel)" -ForegroundColor Cyan
  Write-Host "  Open       : http://$preferredHostname/" -ForegroundColor Cyan
  Write-Host "  Fallback   : http://$hostIp/" -ForegroundColor Cyan
  Finalize-Result -Result $result -ExitCode 0
}

if ($result.status -eq "ip_only") {
  Write-Host "==========================================" -ForegroundColor Yellow
  Write-Host "  Hotspot Active (IP fallback only)" -ForegroundColor Yellow
  Write-Host "==========================================" -ForegroundColor Yellow
  Write-Host "  Join Wi-Fi : $ssid" -ForegroundColor Cyan
  Write-Host "  Password   : $key" -ForegroundColor Cyan
  Write-Host "  Source     : $($selectedCandidate.InterfaceTypeLabel)" -ForegroundColor Cyan
  Write-Host "  Use        : http://$hostIp/" -ForegroundColor Cyan
  Write-Host "  Hostname   : $preferredHostname is not ready yet" -ForegroundColor Yellow
  Finalize-Result -Result $result -ExitCode 2
}

Write-Host "==========================================" -ForegroundColor Red
Write-Host "  Hotspot Unavailable" -ForegroundColor Red
Write-Host "==========================================" -ForegroundColor Red
Finalize-Result -Result $result -ExitCode 1
