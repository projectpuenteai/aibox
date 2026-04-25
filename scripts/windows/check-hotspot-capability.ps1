# AIBox Hotspot Capability Check
# -----------------------------------------------------------------------------
# Pre-flight scanner that verifies a Windows host is ready to broadcast the
# Project Puente AI demo hotspot. Does NOT mutate state. Safe to run without
# administrator privileges (missing admin becomes a WARN, not a FAIL).
#
# Exit codes:
#   0   all checks PASS, or PASS + WARN only
#   1   one or more FAIL
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\check-hotspot-capability.ps1
#   powershell -ExecutionPolicy Bypass -File .\check-hotspot-capability.ps1 -EmitJson
#   powershell -ExecutionPolicy Bypass -File .\check-hotspot-capability.ps1 -Quiet

param(
  [switch]$EmitJson,
  [switch]$Quiet
)

$ErrorActionPreference = "Stop"

# -- Path resolution ----------------------------------------------------------
$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir  = $scriptDir
$scriptsDir  = Split-Path -Parent $windowsDir
$aiboxDir    = Split-Path -Parent $scriptsDir
$stackDir    = Join-Path $aiboxDir "stack"
$stackEnvFile = Join-Path $stackDir ".env"
$composeFile = Join-Path $stackDir "docker-compose.yaml"
$engineDir   = Join-Path $aiboxDir "tools\llama-runtime\scripts"
$logsDir     = Join-Path $aiboxDir "logs"

# ── Inline helpers (follow existing codebase convention) ─────────────────────
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

function Write-DemoLog {
  param([string]$Message, [string]$Level = "info")
  try {
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    $logFile = Join-Path $logsDir "windows-demo-startup.log"
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $logFile -Value "$ts [check-hotspot-capability] [$Level] $Message" -Encoding UTF8
  } catch {}
}

# ── Check results accumulator ────────────────────────────────────────────────
$checks = New-Object System.Collections.Generic.List[object]

function Add-Check {
  param(
    [string]$Name,
    [ValidateSet("PASS","WARN","FAIL")][string]$Status,
    [string]$Detail = ""
  )
  $checks.Add([pscustomobject]@{
    name   = $Name
    status = $Status
    detail = $Detail
  }) | Out-Null
}

function Write-CheckLine {
  param([pscustomobject]$Check)
  if ($Quiet) { return }
  $color = switch ($Check.status) {
    "PASS" { "Green" }
    "WARN" { "Yellow" }
    "FAIL" { "Red" }
  }
  $line = "[{0}] {1}" -f $Check.status, $Check.name
  if (-not [string]::IsNullOrWhiteSpace($Check.detail)) {
    $line += ": $($Check.detail)"
  }
  Write-Host $line -ForegroundColor $color
}

if (-not $Quiet) {
  Write-Host ""
  Write-Host "=== AIBox Hotspot Capability Check ===" -ForegroundColor Cyan
  Write-Host ""
}

# ── 1. Windows version ───────────────────────────────────────────────────────
try {
  $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
  $verStr = $os.Version
  $caption = $os.Caption
  $verParts = $verStr.Split('.')
  $major = [int]$verParts[0]
  if ($major -ge 10) {
    Add-Check "Windows version" "PASS" "$caption ($verStr)"
  } else {
    Add-Check "Windows version" "FAIL" "Requires Windows 10 or newer; found $caption ($verStr)"
  }
} catch {
  Add-Check "Windows version" "WARN" "Could not query Win32_OperatingSystem: $($_.Exception.Message)"
}

# ── 2. PowerShell version ────────────────────────────────────────────────────
$psVer = $PSVersionTable.PSVersion
if ($psVer.Major -gt 5 -or ($psVer.Major -eq 5 -and $psVer.Minor -ge 1)) {
  Add-Check "PowerShell version" "PASS" "$psVer"
} else {
  Add-Check "PowerShell version" "FAIL" "Requires PowerShell 5.1 or newer; found $psVer"
}

# ── 3. Administrator privileges ──────────────────────────────────────────────
if (Test-IsAdministrator) {
  Add-Check "Administrator privileges" "PASS" "running elevated"
} else {
  Add-Check "Administrator privileges" "WARN" "not running as admin (start scripts will self-elevate)"
}

# ── 4. Wi-Fi physical adapter ────────────────────────────────────────────────
$wifiAdapters = @()
try {
  $wifiAdapters = @(Get-NetAdapter -Physical -ErrorAction Stop | Where-Object {
    $_.PhysicalMediaType -eq 'Native 802.11' -or
    $_.InterfaceDescription -match 'Wireless|Wi-Fi|WLAN|802\.11'
  })
} catch {}

if ($wifiAdapters.Count -eq 0) {
  Add-Check "Wi-Fi adapter present" "FAIL" "no physical 802.11 adapter detected"
} else {
  $primary = $wifiAdapters[0]
  $statusText = "$($primary.InterfaceDescription) (Status: $($primary.Status))"
  if ($primary.Status -eq 'Up' -or $primary.Status -eq 'Disabled') {
    $lvl = if ($primary.Status -eq 'Up') { "PASS" } else { "WARN" }
    Add-Check "Wi-Fi adapter present" $lvl $statusText
  } else {
    Add-Check "Wi-Fi adapter present" "WARN" $statusText
  }
}

# ── 5. WlanSvc service ───────────────────────────────────────────────────────
try {
  $wlanSvc = Get-Service -Name WlanSvc -ErrorAction Stop
  if ($wlanSvc.Status -eq 'Running') {
    Add-Check "WlanSvc (WLAN AutoConfig)" "PASS" "Running"
  } else {
    Add-Check "WlanSvc (WLAN AutoConfig)" "WARN" "Status: $($wlanSvc.Status); start-demo-stack.ps1 may fail"
  }
} catch {
  Add-Check "WlanSvc (WLAN AutoConfig)" "FAIL" "service not installed"
}

# ── 6a. Legacy hosted-network support (informational only) ──────────────────
# `netsh wlan show drivers` reports whether the driver implements the LEGACY
# Native Wi-Fi Hosted Network OIDs (deprecated by Microsoft circa Win10 1607).
# Modern Mobile Hotspot does NOT use this API - it uses Wi-Fi Direct under
# the hood via Windows.Networking.NetworkOperators (checked in 6b below).
# Almost every modern Wi-Fi 6/6E driver (Intel AX2xx, MediaTek MT79xx,
# Qualcomm) reports "No" here even when Mobile Hotspot works correctly.
# This check is left in for diagnostic context; it is NOT decisive.
$hostedNetSupported = $null
$hostedNetDetail = ""
try {
  $drvOut = (& netsh wlan show drivers 2>&1 | Out-String)
  if ($drvOut -match '(?im)^\s*Hosted network supported\s*:\s*(\w+)') {
    $val = $Matches[1].Trim()
    $hostedNetSupported = ($val -eq 'Yes')
    $hostedNetDetail = "legacy API reports '$val' (informational; Mobile Hotspot uses Wi-Fi Direct, not this API)"
  } else {
    $hostedNetDetail = "could not parse netsh output"
  }
} catch {
  $hostedNetDetail = "netsh wlan show drivers threw: $($_.Exception.Message)"
}

# Always emit as informational - never as PASS/FAIL since it doesn't gate Mobile Hotspot.
Add-Check "Legacy hosted-network API" "WARN" $hostedNetDetail

# ── 6b. Mobile Hotspot infrastructure check (Wi-Fi Direct virtual adapters) ─
# Mobile Hotspot needs the "Microsoft Wi-Fi Direct Virtual Adapter" device(s)
# present and AdminStatus=Up. If they're missing, the AP infrastructure is
# not installed; if present, the adapter at least supports it.
$wfdAdapters = @()
try {
  $wfdAdapters = @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Where-Object {
    $_.InterfaceDescription -match 'Wi-Fi Direct Virtual Adapter'
  })
} catch {}
if ($wfdAdapters.Count -gt 0) {
  $upCount = @($wfdAdapters | Where-Object { $_.AdminStatus -eq 'Up' }).Count
  Add-Check "Wi-Fi Direct virtual adapters" "PASS" "$($wfdAdapters.Count) present, $upCount admin-up (Mobile Hotspot infra OK)"
} else {
  Add-Check "Wi-Fi Direct virtual adapters" "FAIL" "no Microsoft Wi-Fi Direct Virtual Adapter found - Mobile Hotspot infrastructure missing. Try: pnputil /scan-devices, or reinstall the Wi-Fi driver."
}

# ── 6c. Tethering profile available (WinRT, authoritative for Mobile Hotspot) ─
# This is the AUTHORITATIVE check for whether Windows can tether through some
# connection profile right now. It correctly returns Enabled on modern adapters
# (including MT7921) where the legacy API in 6a returns No.
$tetheringCapable = $false
$tetheringDetail = ""
try {
  [void][Windows.Networking.Connectivity.NetworkInformation,Windows,ContentType=WindowsRuntime]
  [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows,ContentType=WindowsRuntime]

  $profiles = @([Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles())
  $internet = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if ($internet) { $profiles = @($internet) + $profiles }

  $seen = New-Object System.Collections.Generic.HashSet[string]
  foreach ($p in $profiles) {
    if (-not $p) { continue }
    $key = [string]$p.ProfileName
    if (-not $seen.Add($key)) { continue }
    try {
      $cap = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($p)
      if ($cap -eq [Windows.Networking.NetworkOperators.TetheringCapability]::Enabled) {
        $tetheringCapable = $true
        $tetheringDetail = "via profile '$($p.ProfileName)'"
        break
      } else {
        $tetheringDetail = "last capability: $cap on '$($p.ProfileName)'"
      }
    } catch {}
  }
} catch {
  $tetheringDetail = "WinRT load failed: $($_.Exception.Message)"
}

if ($tetheringCapable) {
  Add-Check "Tethering profile available (WinRT)" "PASS" $tetheringDetail
} else {
  $msg = "no enabled tethering profile"
  if ($tetheringDetail) { $msg = $tetheringDetail }
  Add-Check "Tethering profile available (WinRT)" "WARN" "$msg (may still work once an upstream profile is selected)"
}

# ── 7. Active wireless interface ─────────────────────────────────────────────
$wlanIfacesRaw = ""
try {
  $wlanIfacesRaw = (& netsh wlan show interfaces 2>&1 | Out-String)
} catch {}
if ($wlanIfacesRaw -match 'There is no wireless interface') {
  Add-Check "Active wireless interface" "WARN" "netsh reports no wireless interface"
} elseif ($wlanIfacesRaw -match 'Name\s*:') {
  Add-Check "Active wireless interface" "PASS" "netsh lists at least one wireless interface"
} else {
  Add-Check "Active wireless interface" "WARN" "could not parse netsh wlan output"
}

# ── 8. Docker Desktop running ────────────────────────────────────────────────
$dockerOk = $false
$dockerDetail = ""
try {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $dockerVer = (& docker version --format '{{.Server.Version}}' 2>&1 | Out-String).Trim()
  $dockerExit = $LASTEXITCODE
  $ErrorActionPreference = $saved
  if ($dockerExit -eq 0 -and $dockerVer -match '^\d') {
    $dockerOk = $true
    $dockerDetail = "daemon version $dockerVer"
  } else {
    $dockerDetail = "docker version exit=$dockerExit; is Docker Desktop running?"
  }
} catch {
  $dockerDetail = "docker CLI not found or threw: $($_.Exception.Message)"
}
if ($dockerOk) {
  Add-Check "Docker Desktop" "PASS" $dockerDetail
} else {
  Add-Check "Docker Desktop" "FAIL" $dockerDetail
}

# ── 9. Compose file present ──────────────────────────────────────────────────
if (Test-Path $composeFile) {
  Add-Check "Compose file" "PASS" $composeFile
} else {
  Add-Check "Compose file" "FAIL" "not found at $composeFile"
}

# ── 10. Port 80 availability ─────────────────────────────────────────────────
function Get-PortListenerSummary {
  param([int]$Port)
  try {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    if ($listeners.Count -eq 0) { return $null }
    $pids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    $names = @()
    foreach ($procId in $pids) {
      try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        $names += "$($p.ProcessName) (PID $procId)"
      } catch {
        $names += "PID $procId"
      }
    }
    return ($names -join ", ")
  } catch {
    return $null
  }
}

$port80Owner = Get-PortListenerSummary -Port 80
if (-not $port80Owner) {
  Add-Check "Port 80 (HTTP / Caddy)" "PASS" "free (stack can claim it)"
} elseif ($port80Owner -match 'com\.docker|vpnkit|backend|wslrelay|dockerd') {
  Add-Check "Port 80 (HTTP / Caddy)" "PASS" "held by Docker Desktop: $port80Owner"
} else {
  Add-Check "Port 80 (HTTP / Caddy)" "FAIL" "occupied by $port80Owner - stop that service before starting the demo stack"
}

# ── 11. Port 5380 availability (Technitium DNS optional) ─────────────────────
$port5380Owner = Get-PortListenerSummary -Port 5380
if (-not $port5380Owner) {
  Add-Check "Port 5380 (Technitium DNS UI)" "PASS" "free (stack can claim it)"
} elseif ($port5380Owner -match 'com\.docker|vpnkit|backend|wslrelay|dockerd') {
  Add-Check "Port 5380 (Technitium DNS UI)" "PASS" "held by Docker Desktop: $port5380Owner"
} else {
  Add-Check "Port 5380 (Technitium DNS UI)" "WARN" "occupied by $port5380Owner (non-fatal; DNS UI only)"
}

# ── 12. Windows Firewall profiles ────────────────────────────────────────────
try {
  $profiles = @(Get-NetFirewallProfile -ErrorAction Stop)
  $disabled = @($profiles | Where-Object { -not $_.Enabled } | Select-Object -ExpandProperty Name)
  if ($disabled.Count -eq 0) {
    Add-Check "Windows Firewall profiles" "PASS" "all profiles enabled"
  } else {
    Add-Check "Windows Firewall profiles" "WARN" "disabled on: $($disabled -join ', ')"
  }
} catch {
  Add-Check "Windows Firewall profiles" "WARN" "could not query: $($_.Exception.Message)"
}

# ── 13. Existing hotspot firewall rule ───────────────────────────────────────
try {
  $rule = Get-NetFirewallRule -DisplayName 'AIBox Hotspot*' -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($rule) {
    $state = if ($rule.Enabled) { "enabled" } else { "disabled" }
    Add-Check "AIBox Hotspot firewall rule" "PASS" "$($rule.DisplayName) ($state)"
  } else {
    Add-Check "AIBox Hotspot firewall rule" "WARN" "not yet created (setup_hotspot.ps1 creates it on first run)"
  }
} catch {
  Add-Check "AIBox Hotspot firewall rule" "WARN" "could not query firewall: $($_.Exception.Message)"
}

# ── 14. Hotspot env vars set ─────────────────────────────────────────────────
$ssid = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$key  = Read-EnvValue "HOTSPOT_KEY"  "puente1234"
$offlineHost = Read-EnvValue "OFFLINE_HOSTNAME" ""

if (-not [string]::IsNullOrWhiteSpace($ssid)) {
  Add-Check "HOTSPOT_SSID" "PASS" "'$ssid'"
} else {
  Add-Check "HOTSPOT_SSID" "FAIL" "not resolvable from env or .env"
}

# Don't print the password; just confirm it's set + flag defaults
if ([string]::IsNullOrWhiteSpace($key)) {
  Add-Check "HOTSPOT_KEY" "FAIL" "not resolvable from env or .env"
} elseif ($key -eq "puente1234") {
  Add-Check "HOTSPOT_KEY" "WARN" "using default password 'puente1234' (set a unique HOTSPOT_KEY in stack/.env before a real demo)"
} else {
  Add-Check "HOTSPOT_KEY" "PASS" "custom password set (length $($key.Length))"
}

if ([string]::IsNullOrWhiteSpace($offlineHost)) {
  Add-Check "OFFLINE_HOSTNAME" "WARN" "not set; clients will use raw 192.168.137.1"
} else {
  Add-Check "OFFLINE_HOSTNAME" "PASS" "'$offlineHost'"
}

# ── 15. Hosts file writable (required for puente.link mapping) ───────────────
$hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
if (Test-Path $hostsFile) {
  Add-Check "Hosts file present" "PASS" $hostsFile
} else {
  Add-Check "Hosts file present" "FAIL" "not found at $hostsFile"
}

# ── 16. Engine scripts present ───────────────────────────────────────────────
$requiredEngine = @(
  "setup_hotspot.ps1",
  "up_stack.ps1",
  "down_stack.ps1",
  "get_network_info.ps1"
)
$missingEngine = @()
foreach ($n in $requiredEngine) {
  if (-not (Test-Path (Join-Path $engineDir $n))) { $missingEngine += $n }
}
if ($missingEngine.Count -eq 0) {
  Add-Check "Existing engine scripts" "PASS" "all required scripts present under tools/llama-runtime/scripts/"
} else {
  Add-Check "Existing engine scripts" "FAIL" "missing: $($missingEngine -join ', ')"
}

# ── Print results ────────────────────────────────────────────────────────────
foreach ($c in $checks) { Write-CheckLine $c }

$passCount = @($checks | Where-Object { $_.status -eq 'PASS' }).Count
$warnCount = @($checks | Where-Object { $_.status -eq 'WARN' }).Count
$failCount = @($checks | Where-Object { $_.status -eq 'FAIL' }).Count

$readyStatus = if ($failCount -eq 0) { "READY" } else { "NOT READY" }
$exitCode    = if ($failCount -eq 0) { 0 } else { 1 }

if (-not $Quiet) {
  Write-Host ""
  Write-Host ("Summary: {0} PASS, {1} WARN, {2} FAIL" -f $passCount, $warnCount, $failCount)
  $statusColor = if ($failCount -eq 0) { if ($warnCount -eq 0) { "Green" } else { "Yellow" } } else { "Red" }
  Write-Host ("Status: {0}" -f $readyStatus) -ForegroundColor $statusColor
  Write-Host ""
}

Write-DemoLog ("result: {0} (pass={1} warn={2} fail={3})" -f $readyStatus, $passCount, $warnCount, $failCount)

if ($EmitJson) {
  $checksArray = @()
  foreach ($c in $checks) { $checksArray += $c }
  $result = [pscustomobject]@{
    ready        = [bool]($failCount -eq 0)
    status       = [string]$readyStatus
    pass_count   = [int]$passCount
    warn_count   = [int]$warnCount
    fail_count   = [int]$failCount
    checks       = $checksArray
    generated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
  }
  Write-Output ($result | ConvertTo-Json -Depth 6)
}

exit $exitCode
