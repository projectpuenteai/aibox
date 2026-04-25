# AIBox - Diagnose Hotspot Failure
# -----------------------------------------------------------------------------
# Captures every diagnostic surface relevant to a Mobile Hotspot startup
# failure. Read-only (does NOT try to start/stop the hotspot). Safe to run
# without elevation; warns if not admin since some queries are admin-only.
#
# Output is structured: each section starts with === HEADER === so the
# operator can scroll/copy specific sections.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\diagnose-hotspot-failure.ps1
#   powershell -ExecutionPolicy Bypass -File .\diagnose-hotspot-failure.ps1 -OutFile C:\temp\hotspot-diag.txt

param(
  [string]$OutFile = ""
)

$ErrorActionPreference = "Continue"
$startTime = Get-Date

$buffer = New-Object System.Collections.Generic.List[string]
function Out-Both {
  param([string]$Line, [string]$Color = "Gray")
  $buffer.Add($Line) | Out-Null
  Write-Host $Line -ForegroundColor $Color
}

function Section {
  param([string]$Title)
  Out-Both ""
  Out-Both ("=== {0} ===" -f $Title) "Cyan"
}

function Format-AsLines {
  param($Object)
  if ($null -eq $Object) { return @("(null)") }
  if ($Object -is [string]) { return @($Object) }
  return @($Object | Out-String -Stream)
}

# ── 0. Header ────────────────────────────────────────────────────────────────
Out-Both ("AIBox Hotspot Failure Diagnostic - {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:sszzz")) "Cyan"
Out-Both ("Computer: {0}" -f $env:COMPUTERNAME)
Out-Both ("User: {0}" -f $env:USERNAME)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Out-Both ("Elevated: {0}" -f $isAdmin) $(if ($isAdmin) { "Green" } else { "Yellow" })

# ── 1. Wi-Fi adapter detail ──────────────────────────────────────────────────
Section "1. Wi-Fi adapter detail"
try {
  $adapters = Get-NetAdapter -Physical | Where-Object {
    $_.PhysicalMediaType -eq 'Native 802.11' -or $_.InterfaceDescription -match 'Wireless|Wi-Fi|WLAN|802\.11'
  }
  foreach ($a in $adapters) {
    Out-Both ("Name              : {0}" -f $a.Name)
    Out-Both ("Description       : {0}" -f $a.InterfaceDescription)
    Out-Both ("Status            : {0}" -f $a.Status)
    Out-Both ("MAC               : {0}" -f $a.MacAddress)
    Out-Both ("LinkSpeed         : {0}" -f $a.LinkSpeed)
    Out-Both ("DriverVersion     : {0}" -f $a.DriverVersion)
    Out-Both ("DriverDate        : {0}" -f $a.DriverDate)
    Out-Both ("DriverProvider    : {0}" -f $a.DriverProvider)
    Out-Both ("InterfaceGuid     : {0}" -f $a.InterfaceGuid)
    Out-Both ""
  }
} catch {
  Out-Both ("ERROR: {0}" -f $_.Exception.Message) "Red"
}

# ── 2. netsh wlan show drivers (hosted-network capability) ──────────────────
Section "2. netsh wlan show drivers (hosted-network capability)"
try {
  $drivers = & netsh wlan show drivers 2>&1 | Out-String
  Out-Both $drivers
} catch {
  Out-Both ("ERROR: {0}" -f $_.Exception.Message) "Red"
}

# ── 3. netsh wlan show interfaces (current connection state) ─────────────────
Section "3. netsh wlan show interfaces"
try {
  $ifaces = & netsh wlan show interfaces 2>&1 | Out-String
  Out-Both $ifaces
} catch {
  Out-Both ("ERROR: {0}" -f $_.Exception.Message) "Red"
}

# ── 4. netsh wlan show hostednetwork (legacy API state) ─────────────────────
Section "4. netsh wlan show hostednetwork (legacy hosted-network API)"
try {
  $hostednet = & netsh wlan show hostednetwork 2>&1 | Out-String
  Out-Both $hostednet
} catch {
  Out-Both ("ERROR: {0}" -f $_.Exception.Message) "Red"
}

# ── 5. Critical services ─────────────────────────────────────────────────────
Section "5. Critical services"
foreach ($svcName in @('WlanSvc','SharedAccess','icssvc','dhcp','Dnscache','mpssvc')) {
  try {
    $s = Get-Service -Name $svcName -ErrorAction Stop
    $cfg = Get-CimInstance -ClassName Win32_Service -Filter "Name='$svcName'" -ErrorAction SilentlyContinue
    Out-Both ("{0,-15} status={1,-10} starttype={2}" -f $svcName, $s.Status, $(if ($cfg) { $cfg.StartMode } else { '?' })) $(if ($s.Status -eq 'Running') { 'Green' } else { 'Yellow' })
  } catch {
    Out-Both ("{0,-15} not found ({1})" -f $svcName, $_.Exception.Message) "Yellow"
  }
}

# ── 6. NetConnectionProfile (what Windows considers the active networks) ────
Section "6. Get-NetConnectionProfile (active networks)"
try {
  $profiles = Get-NetConnectionProfile
  foreach ($p in $profiles) {
    Out-Both ("Name='{0}' Category={1} IPv4Conn={2} IPv6Conn={3} InterfaceAlias='{4}' InterfaceIndex={5}" -f `
      $p.Name, $p.NetworkCategory, $p.IPv4Connectivity, $p.IPv6Connectivity, $p.InterfaceAlias, $p.InterfaceIndex)
  }
} catch {
  Out-Both ("ERROR: {0}" -f $_.Exception.Message) "Red"
}

# ── 7. WinRT NetworkOperatorTetheringManager state ──────────────────────────
Section "7. WinRT TetheringManager state per connection profile"
try {
  [void][Windows.Networking.Connectivity.NetworkInformation,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows,ContentType=WindowsRuntime]

  $internet = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  $allProfiles = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()
  $list = @()
  if ($internet) { $list += $internet }
  foreach ($p in $allProfiles) {
    if ($p -and ($list -notcontains $p)) { $list += $p }
  }

  foreach ($p in $list) {
    $name = [string]$p.ProfileName
    $iana = $null
    try { $iana = [int]$p.NetworkAdapter.IanaInterfaceType } catch {}
    $ifaceLabel = switch ($iana) {
      6 { "ethernet" }
      71 { "wifi" }
      default { "type=$iana" }
    }
    $cap = $null
    try { $cap = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($p) } catch { $capErr = $_.Exception.Message }
    $opState = "?"
    $clientCount = "?"
    $ssid = "?"
    if ($cap -eq [Windows.Networking.NetworkOperators.TetheringCapability]::Enabled) {
      try {
        $mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p)
        $opState = [string]$mgr.TetheringOperationalState
        $clientCount = [string]$mgr.ClientCount
        try {
          $cfg = $mgr.GetCurrentAccessPointConfiguration()
          if ($cfg) {
            $ssid = [string]$cfg.Ssid
          }
        } catch {}
      } catch { $opState = "create-failed: $($_.Exception.Message)" }
    }
    Out-Both ("Profile='{0}' iface={1} capability={2} opState={3} clients={4} ssid='{5}'" -f `
      $name, $ifaceLabel, $cap, $opState, $clientCount, $ssid)
  }
} catch {
  Out-Both ("ERROR: WinRT API failed: {0}" -f $_.Exception.Message) "Red"
}

# ── 8. Re-run setup_hotspot.ps1 standalone with full JSON capture ────────────
Section "8. Standalone setup_hotspot.ps1 run (FULL JSON output)"
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir = $scriptDir
$scriptsDir = Split-Path -Parent $windowsDir
$aiboxDir   = Split-Path -Parent $scriptsDir
$engineSetup = Join-Path $aiboxDir "tools\llama-runtime\scripts\setup_hotspot.ps1"
$jsonOut = Join-Path ([System.IO.Path]::GetTempPath()) ("aibox-hotspot-diag-" + [guid]::NewGuid().ToString() + ".json")

if (-not $isAdmin) {
  Out-Both "SKIP: not running as Administrator. Re-run this script in an Admin PowerShell to invoke setup_hotspot.ps1." "Yellow"
} elseif (-not (Test-Path $engineSetup)) {
  Out-Both ("SKIP: setup_hotspot.ps1 not found at {0}" -f $engineSetup) "Red"
} else {
  Out-Both ("Invoking: {0} -EmitJson -JsonOutFile {1}" -f $engineSetup, $jsonOut) "DarkGray"
  Out-Both "(this WILL attempt to start the hotspot - it is the diagnostic point)" "Yellow"
  try {
    & powershell -ExecutionPolicy Bypass -File $engineSetup -EmitJson -JsonOutFile $jsonOut 2>&1 | ForEach-Object { Out-Both ([string]$_) }
    Out-Both ""
    Out-Both ("Engine exit code: {0}" -f $LASTEXITCODE) $(if ($LASTEXITCODE -eq 0) { "Green" } else { "Yellow" })
    if (Test-Path $jsonOut) {
      Out-Both ""
      Out-Both "--- JSON output ---"
      $rawJson = Get-Content $jsonOut -Raw
      Out-Both $rawJson
    } else {
      Out-Both "No JSON output file produced" "Yellow"
    }
  } catch {
    Out-Both ("Engine threw: {0}" -f $_.Exception.Message) "Red"
  } finally {
    if (Test-Path $jsonOut) { Remove-Item -LiteralPath $jsonOut -Force -ErrorAction SilentlyContinue }
  }
}

# ── 9. Hotspot state file from previous run (if any) ─────────────────────────
Section "9. Stored hotspot state (post-mortem of previous run)"
$hotspotStateDir = Join-Path $aiboxDir "backend-data\appdata\hotspot"
if (Test-Path $hotspotStateDir) {
  Get-ChildItem $hotspotStateDir -File -ErrorAction SilentlyContinue | ForEach-Object {
    Out-Both ("--- {0} ({1} bytes) ---" -f $_.Name, $_.Length)
    try {
      Out-Both (Get-Content $_.FullName -Raw -ErrorAction Stop)
    } catch {
      Out-Both ("(could not read: {0})" -f $_.Exception.Message) "Yellow"
    }
  }
} else {
  Out-Both ("(no state directory at {0})" -f $hotspotStateDir) "DarkGray"
}

# ── 10. Recent Mobile Hotspot / WLAN event log entries ──────────────────────
Section "10. Last 30 minutes of WLAN-related event log entries"
if (-not $isAdmin) {
  Out-Both "SKIP: event log queries need Administrator." "Yellow"
} else {
  $cutoff = (Get-Date).AddMinutes(-30)
  $logs = @(
    'Microsoft-Windows-WLAN-AutoConfig/Operational',
    'Microsoft-Windows-NetworkProfile/Operational',
    'Microsoft-Windows-WlanConn/Operational'
  )
  foreach ($logName in $logs) {
    try {
      $events = Get-WinEvent -FilterHashtable @{LogName=$logName; StartTime=$cutoff} -MaxEvents 50 -ErrorAction SilentlyContinue
      Out-Both ("--- {0} ({1} events) ---" -f $logName, ($events | Measure-Object).Count)
      foreach ($e in $events) {
        Out-Both ("[{0}] [{1}] {2}: {3}" -f $e.TimeCreated, $e.LevelDisplayName, $e.Id, ($e.Message -replace "`r?`n", " | ").Substring(0, [Math]::Min(200, $e.Message.Length)))
      }
    } catch {
      Out-Both ("(could not read {0})" -f $logName) "DarkGray"
    }
  }
}

# ── 11. Mobile Hotspot policy registry keys ──────────────────────────────────
Section "11. Mobile Hotspot / Wi-Fi sense policy registry keys"
$policyKeys = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Microsoft\Wcm',
  'HKLM:\SOFTWARE\Microsoft\WcmSvc\wifinetworkmanager\config',
  'HKLM:\SYSTEM\CurrentControlSet\Services\WlanSvc\Parameters',
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\NetworkList\Profiles'
)
foreach ($k in $policyKeys) {
  try {
    if (Test-Path $k) {
      Out-Both ("--- {0} ---" -f $k)
      Get-ItemProperty -Path $k -ErrorAction SilentlyContinue | Select-Object -Property * -ExcludeProperty PS* | Format-List | Out-String -Stream | ForEach-Object { Out-Both $_ }
    } else {
      Out-Both ("{0} : not present" -f $k) "DarkGray"
    }
  } catch {
    Out-Both ("{0} : {1}" -f $k, $_.Exception.Message) "Yellow"
  }
}

# ── 12. Summary ──────────────────────────────────────────────────────────────
Section "12. Summary"
$elapsed = (Get-Date) - $startTime
Out-Both ("Diagnostic complete in {0:N1}s" -f $elapsed.TotalSeconds) "Green"
Out-Both ""
Out-Both "Next steps:" "Cyan"
Out-Both "  - Review Section 2 'Hosted network supported' line - if it says 'No', the driver is the cause"
Out-Both "  - Review Section 5 SharedAccess service - if not Running, that explains 'unavailable'"
Out-Both "  - Review Section 7 - find the profile with capability=Enabled and check its opState"
Out-Both "  - Review Section 8 JSON 'errors' and 'warnings' arrays for the actual failure reason"
Out-Both "  - Review Section 10 events for adapter-level failure messages around the failed start time"

if (-not [string]::IsNullOrWhiteSpace($OutFile)) {
  $buffer -join "`r`n" | Set-Content -Path $OutFile -Encoding UTF8
  Write-Host ""
  Write-Host ("Saved diagnostic to: {0}" -f $OutFile) -ForegroundColor Green
}
