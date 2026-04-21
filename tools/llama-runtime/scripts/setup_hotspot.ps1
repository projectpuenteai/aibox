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

$ErrorActionPreference = "Stop"

$scriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir   = Split-Path -Parent $scriptDir
$toolsDir     = Split-Path -Parent $runtimeDir
$aiboxDir     = Split-Path -Parent $toolsDir
$stackEnvFile = Join-Path $aiboxDir "stack\.env"

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

  $json = $Result | ConvertTo-Json -Depth 8
  if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) {
    $json | Set-Content -Path $JsonOutFile -Encoding UTF8
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
    [System.IO.File]::WriteAllLines($hostsFilePath, [string[]]$newContent, [System.Text.Encoding]::ASCII)
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
      [System.IO.File]::WriteAllLines($hostsFilePath, [string[]]$filtered, [System.Text.Encoding]::ASCII)
    }
    return $true
  } catch {
    return $false
  }
}

function Configure-FirewallRules {
  if ($SkipFirewall) {
    Write-Host "[3/5] Skipping firewall (--SkipFirewall)."
    return
  }

  Write-Host "[3/5] Verifying Windows Firewall inbound rules..."
  $rules = @(
    @{ Name = "AIBox-HTTP-Inbound";    Proto = "TCP"; Port = 80; Desc = "AIBox portal (HTTP)" },
    @{ Name = "AIBox-DNS-TCP-Inbound"; Proto = "TCP"; Port = 53; Desc = "AIBox DNS (TCP)" },
    @{ Name = "AIBox-DNS-UDP-Inbound"; Proto = "UDP"; Port = 53; Desc = "AIBox DNS (UDP)" }
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

function Get-TetheringContext {
  [void][Windows.Networking.Connectivity.NetworkInformation,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.TetheringWiFiBand,Windows,ContentType=WindowsRuntime]

  $profiles = New-Object System.Collections.Generic.List[object]
  $internet = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if ($internet) { [void]$profiles.Add($internet) }

  foreach ($profile in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) {
    if (-not $profile) { continue }
    $alreadySeen = $false
    foreach ($existing in $profiles) {
      if ($existing.ProfileName -eq $profile.ProfileName) {
        $alreadySeen = $true
        break
      }
    }
    if (-not $alreadySeen) { [void]$profiles.Add($profile) }
  }

  foreach ($profile in $profiles) {
    try {
      $capability = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($profile)
      if ($capability -eq [Windows.Networking.NetworkOperators.TetheringCapability]::Enabled) {
        return [pscustomobject]@{
          Profile    = $profile
          Capability = $capability
          Manager    = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
        }
      }
    } catch {}
  }

  return $null
}

$ssid = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$key  = Read-EnvValue "HOTSPOT_KEY"  "puente1234"
$preferredHostname = Read-EnvValue "OFFLINE_HOSTNAME" "puente.link"

$result = New-Result
$result.ssid = $ssid
$result.password = $key
$result.domain = $preferredHostname

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

$context = Get-TetheringContext
if (-not $context) {
  $result.errors.Add("Windows Mobile Hotspot is not available on the current adapter/profile.")
  $result.warnings.Add("This laptop cannot expose a Mobile Hotspot through the Windows tethering APIs right now.")
  $result.warnings.Add("Check that Wi-Fi is enabled and the adapter supports Mobile Hotspot on this Windows build.")
  Write-Host "ERROR: Windows Mobile Hotspot is not available on this machine right now." -ForegroundColor Red
  Write-Host "Enable Wi-Fi and verify the adapter supports Mobile Hotspot in Windows Settings." -ForegroundColor Yellow
  Finalize-Result -Result $result -ExitCode 1
}

$manager = $context.Manager
$result.backend = "mobile_hotspot"
$result.details.profile_name = $context.Profile.ProfileName
$result.details.tethering_capability = [string]$context.Capability

if ($Stop) {
  Write-Host "Stopping Windows Mobile Hotspot..."
  try {
    if ($manager.TetheringOperationalState -ne [Windows.Networking.NetworkOperators.TetheringOperationalState]::Off) {
      [void]$manager.StopTetheringAsync()
      $reached = Wait-TetheringState -Manager $manager -TargetState ([Windows.Networking.NetworkOperators.TetheringOperationalState]::Off) -TimeoutMs 20000
      $result.details.stop_reached_off = $reached
      if (-not $reached) {
        $result.errors.Add("Hotspot did not return to Off state within timeout (current: $($manager.TetheringOperationalState)).")
        Finalize-Result -Result $result -ExitCode 1
      }
    } else {
      $result.details.stop_reached_off = $true
      Write-Host "      = Hotspot already off."
    }
  } catch {
    $result.errors.Add("Failed to stop the Windows Mobile Hotspot: $($_.Exception.Message)")
    Finalize-Result -Result $result -ExitCode 1
  }
  $hostsRemoved = Remove-HostsEntry -Domain $preferredHostname
  $result.details.hosts_entry_removed = $hostsRemoved
  $result.ok = $true
  $result.status = "stopped"
  Finalize-Result -Result $result -ExitCode 0
}

Write-Host "SSID     : $ssid"
Write-Host "Password : $key"
Write-Host ""

Write-Host "[1/5] Configuring Windows Mobile Hotspot..."
try {
  $config = $manager.GetCurrentAccessPointConfiguration()
  $config.Ssid = $ssid
  $config.Passphrase = $key
  try {
    $config.Band = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::Auto
  } catch {}
  [void]$manager.ConfigureAccessPointAsync($config)
  # Configuration is applied quickly; the WinRT async result isn't inspectable
  # from PS 5.1, so we pause briefly and re-read the applied SSID to confirm.
  Start-Sleep -Milliseconds 1500
  $applied = $manager.GetCurrentAccessPointConfiguration()
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

Configure-FirewallRules

Write-Host "[4/5] Validating hotspot and offline DNS..."
$hostIp = Wait-ForHotspotIp -TimeoutSeconds 20
$result.host_ip = $hostIp
$result.dns_server = $hostIp
$result.validation.hotspot_active = ($manager.TetheringOperationalState -eq [Windows.Networking.NetworkOperators.TetheringOperationalState]::On)

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
  Write-Host "  Use        : http://$hostIp/" -ForegroundColor Cyan
  Write-Host "  Hostname   : $preferredHostname is not ready yet" -ForegroundColor Yellow
  Finalize-Result -Result $result -ExitCode 2
}

Write-Host "==========================================" -ForegroundColor Red
Write-Host "  Hotspot Unavailable" -ForegroundColor Red
Write-Host "==========================================" -ForegroundColor Red
Finalize-Result -Result $result -ExitCode 1
