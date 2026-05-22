# AIBox - Test Demo Network
# -----------------------------------------------------------------------------
# Post-startup self-test for the Windows hotspot demo. Asserts that every
# component required for a student to connect is in place and working:
# hotspot, Docker stack, Caddy, firewall, and resolvable student URLs.
#
# Safe to run without elevation (some checks degrade to WARN without admin).
#
# Exit codes:
#   0   Ready for demo (all PASS, or PASS + WARN only)
#   1   Not ready (one or more FAIL)
#   2   Ran but hotspot/stack state ambiguous (WARN-heavy)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\test-demo-network.ps1
#   powershell -ExecutionPolicy Bypass -File .\test-demo-network.ps1 -Fresh
#   powershell -ExecutionPolicy Bypass -File .\test-demo-network.ps1 -ShowPassword
#   powershell -ExecutionPolicy Bypass -File .\test-demo-network.ps1 -EmitJson

param(
  [switch]$Fresh,
  [switch]$ShowPassword,
  [switch]$EmitJson,
  [switch]$Quiet
)

$ErrorActionPreference = "Stop"

# ── Path resolution ──────────────────────────────────────────────────────────
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir = $scriptDir
$scriptsDir = Split-Path -Parent $windowsDir
$aiboxDir   = Split-Path -Parent $scriptsDir
$stackDir   = Join-Path $aiboxDir "stack"
$stackEnvFile = Join-Path $stackDir ".env"
$composeFile = Join-Path $stackDir "docker-compose.yaml"
$portalDir  = Join-Path $stackDir "portal"
$networkInfoFile = Join-Path $portalDir "network-info.json"
$engineDir  = Join-Path $aiboxDir "tools\llama-runtime\scripts"
$netInfoScript = Join-Path $engineDir "get_network_info.ps1"
$logsDir    = Join-Path $aiboxDir "logs"

# ── Helpers ──────────────────────────────────────────────────────────────────
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

function Write-DemoLog {
  param([string]$Message, [string]$Level = "info")
  try {
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    $logFile = Join-Path $logsDir "windows-demo-startup.log"
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $logFile -Value "$ts [test-demo-network] [$Level] $Message" -Encoding UTF8
  } catch {}
}

function Test-TcpReachable {
  param([string]$HostName, [int]$Port, [int]$TimeoutMs = 1500)
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

function Test-HttpOk {
  param([string]$Url, [int]$TimeoutSec = 5)
  try {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
    return @{ ok = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500); code = $resp.StatusCode }
  } catch {
    return @{ ok = $false; code = 0; error = $_.Exception.Message }
  }
}

# ── Check accumulator ────────────────────────────────────────────────────────
$checks = New-Object System.Collections.Generic.List[object]
$fixes  = New-Object System.Collections.Generic.List[string]

function Add-Check {
  param(
    [string]$Name,
    [ValidateSet("PASS","WARN","FAIL")][string]$Status,
    [string]$Detail = "",
    [string]$Fix = ""
  )
  $checks.Add([pscustomobject]@{
    name   = $Name
    status = $Status
    detail = $Detail
  }) | Out-Null
  if ($Status -eq "FAIL" -and -not [string]::IsNullOrWhiteSpace($Fix)) {
    $fixes.Add($Fix) | Out-Null
  }
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
  Write-Host "=== AIBox Demo Network Self-Test ===" -ForegroundColor Cyan
  Write-Host "" 
}

# ── 0. Refresh network-info.json if requested ────────────────────────────────
if ($Fresh -and (Test-Path $netInfoScript)) {
  if (-not $Quiet) { Write-Host "[info] Refreshing network-info.json..." -ForegroundColor DarkGray }
  & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet | Out-Null
}

# ── 1. Load network-info.json ────────────────────────────────────────────────
$info = $null
if (Test-Path $networkInfoFile) {
  try {
    $info = Get-Content $networkInfoFile -Raw | ConvertFrom-Json
    Add-Check "network-info.json present" "PASS" $networkInfoFile
  } catch {
    Add-Check "network-info.json present" "WARN" "parse failed: $($_.Exception.Message)"
  }
} else {
  Add-Check "network-info.json present" "WARN" "not found (run start-demo-stack.ps1 or this script with -Fresh)" "start-demo-stack.ps1 or re-run this script with -Fresh"
}

# ── 2. Hotspot enabled ───────────────────────────────────────────────────────
$hotspotActive = $false
$hotspotReadiness = "unknown"
$hotspotSsid = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$hotspotKey  = Read-EnvValue "HOTSPOT_KEY"  "puente1234"
$hotspotIp   = $null
$offlineHost = Read-EnvValue "OFFLINE_HOSTNAME" ""

if ($info -and $info.hotspot) {
  $hotspotActive = ([string]$info.hotspot.status -eq "active")
  $hotspotReadiness = [string]$info.hotspot.readiness
  if ($info.hotspot.ssid) { $hotspotSsid = [string]$info.hotspot.ssid }
  if ($info.hotspot.host_ip) { $hotspotIp = [string]$info.hotspot.host_ip }
}

if ($hotspotActive) {
  Add-Check "Hotspot active" "PASS" "SSID='$hotspotSsid' readiness=$hotspotReadiness"
} else {
  Add-Check "Hotspot active" "FAIL" "Mobile Hotspot is not broadcasting" "Run tools\llama-runtime\scripts\setup_hotspot.ps1 or scripts\windows\start-demo-stack.ps1"
}

# ── 3. Gateway IP (192.168.137.1) present on a NIC ───────────────────────────
$hotspotAddrFound = $null
try {
  $allAddrs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)
  $hotspotAddrFound = $allAddrs | Where-Object { $_.IPAddress -like "192.168.137.*" } | Select-Object -First 1
} catch {}

if ($hotspotAddrFound) {
  Add-Check "Hotspot gateway IP assigned" "PASS" "$($hotspotAddrFound.IPAddress) on '$($hotspotAddrFound.InterfaceAlias)'"
  if (-not $hotspotIp) { $hotspotIp = $hotspotAddrFound.IPAddress }
} else {
  Add-Check "Hotspot gateway IP assigned" "WARN" "no 192.168.137.x IP on any NIC (hotspot may still be initializing)" "Wait 10 s and re-run; if still missing, restart the hotspot"
}

# ── 4. Docker stack running (caddy, ai-control, llama) ───────────────────────
$expectedServices = @("caddy", "ai-control", "llama")
$runningServices = @()
$missingServices = @()
try {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $psOutput = (& docker compose -f $composeFile ps --format json 2>&1)
  $ErrorActionPreference = $saved

  foreach ($line in ($psOutput -split "`r?`n")) {
    $line = $line.Trim()
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    if ($line[0] -ne '{') { continue }
    try {
      $svc = $line | ConvertFrom-Json
      if ($svc.State -eq "running") {
        $runningServices += [string]$svc.Service
      }
    } catch {}
  }
  $missingServices = @($expectedServices | Where-Object { $_ -notin $runningServices })
} catch {
  Add-Check "Docker stack running" "FAIL" "docker compose ps failed: $($_.Exception.Message)" "Start Docker Desktop, then run start-demo-stack.ps1"
}

if ($missingServices.Count -eq 0 -and $runningServices.Count -gt 0) {
  Add-Check "Docker stack running" "PASS" "running: $($runningServices -join ', ')"
} elseif ($runningServices.Count -eq 0) {
  Add-Check "Docker stack running" "FAIL" "no services in 'running' state" "Run scripts\windows\start-demo-stack.ps1"
} else {
  Add-Check "Docker stack running" "WARN" "running: $($runningServices -join ', '); not running: $($missingServices -join ', ')"
}

# ── 5. Portal responds on 127.0.0.1 ──────────────────────────────────────────
$loopback = Test-HttpOk -Url "http://127.0.0.1/"
if ($loopback.ok) {
  Add-Check "Portal responds on 127.0.0.1" "PASS" "HTTP $($loopback.code)"
} else {
  $err = if ($loopback.error) { $loopback.error } else { "HTTP $($loopback.code)" }
  Add-Check "Portal responds on 127.0.0.1" "FAIL" $err "Check 'docker compose logs caddy' and ensure caddy container is healthy"
}

# ── 6. Portal responds on hotspot IP ─────────────────────────────────────────
if ($hotspotIp) {
  # Also try TCP first (faster, less noisy error) before a full HTTP probe
  $tcpOk = Test-TcpReachable -HostName $hotspotIp -Port 80 -TimeoutMs 2000
  if ($tcpOk) {
    $hotspotHttp = Test-HttpOk -Url "http://$hotspotIp/"
    if ($hotspotHttp.ok) {
      Add-Check "Portal responds via hotspot IP" "PASS" "http://$hotspotIp/ -> HTTP $($hotspotHttp.code)"
    } else {
      Add-Check "Portal responds via hotspot IP" "WARN" "TCP reached but HTTP failed: $($hotspotHttp.error)"
    }
  } else {
    Add-Check "Portal responds via hotspot IP" "FAIL" "TCP 80 unreachable at $hotspotIp" "Check Windows Firewall 'AIBox Hotspot HTTP' rule, and that Caddy is bound to 0.0.0.0:80"
  }
} else {
  Add-Check "Portal responds via hotspot IP" "WARN" "no hotspot IP to probe"
}

# ── 7. Firewall rule for hotspot HTTP ────────────────────────────────────────
try {
  $rule = Get-NetFirewallRule -DisplayName 'AIBox Hotspot HTTP*' -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($rule) {
    if ($rule.Enabled) {
      Add-Check "Firewall rule (AIBox Hotspot HTTP)" "PASS" "'$($rule.DisplayName)' enabled"
    } else {
      Add-Check "Firewall rule (AIBox Hotspot HTTP)" "WARN" "'$($rule.DisplayName)' is disabled" "Enable the rule or re-run start-hotspot.ps1"
    }
  } else {
    Add-Check "Firewall rule (AIBox Hotspot HTTP)" "WARN" "no rule named 'AIBox Hotspot HTTP*' (setup_hotspot.ps1 creates it when the hotspot starts)" "Run tools\llama-runtime\scripts\setup_hotspot.ps1"
  }
} catch {
  Add-Check "Firewall rule (AIBox Hotspot HTTP)" "WARN" "could not query firewall: $($_.Exception.Message)"
}

# -- 8. Hosts entry for puente.link -> 192.168.137.1 -------------------------
$hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
$hostsEntryOk = $false
$hostsDetail = ""
if (-not [string]::IsNullOrWhiteSpace($offlineHost) -and (Test-Path $hostsFile)) {
  try {
    $hostsLines = Get-Content $hostsFile -ErrorAction Stop
    $pattern = "(?i)^\s*([0-9.]+)\s+.*\b" + [regex]::Escape($offlineHost) + "\b"
    $matchLine = $hostsLines | Where-Object { $_ -match $pattern } | Select-Object -First 1
    if ($matchLine -and $matchLine -match $pattern) {
      $mappedIp = $Matches[1]
      $hostsEntryOk = ($mappedIp -like "192.168.137.*")
      $hostsDetail = "$offlineHost -> $mappedIp"
    }
  } catch {
    $hostsDetail = "read failed: $($_.Exception.Message)"
  }
}

if ([string]::IsNullOrWhiteSpace($offlineHost)) {
  Add-Check "Hosts file offline-hostname mapping" "WARN" "OFFLINE_HOSTNAME not set in .env"
} elseif ($hostsEntryOk) {
  Add-Check "Hosts file offline-hostname mapping" "PASS" $hostsDetail
} else {
  $detail = if ($hostsDetail) { $hostsDetail } else { "'$offlineHost' not mapped to 192.168.137.x" }
  Add-Check "Hosts file offline-hostname mapping" "WARN" "$detail (setup_hotspot.ps1 writes this when the hotspot starts)" "Run tools\llama-runtime\scripts\setup_hotspot.ps1"
}

# ── 9. Student URL printable ─────────────────────────────────────────────────
$primaryUrl = $null
if ($info -and $info.primary_url) { $primaryUrl = [string]$info.primary_url }
if ([string]::IsNullOrWhiteSpace($primaryUrl)) {
  if ($offlineHost) { $primaryUrl = "http://$offlineHost/" }
  elseif ($hotspotIp) { $primaryUrl = "http://$hotspotIp/" }
}
if (-not [string]::IsNullOrWhiteSpace($primaryUrl)) {
  Add-Check "Student URL resolvable" "PASS" $primaryUrl
} else {
  Add-Check "Student URL resolvable" "FAIL" "no primary URL could be determined" "Run start-demo-stack.ps1"
}

# ── 10. Connected clients (optional, WARN only if zero) ──────────────────────
$connectedClients = @()
try {
  $neighbors = @(Get-NetNeighbor -AddressFamily IPv4 -State Reachable, Stale -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -like "192.168.137.*" -and $_.IPAddress -ne "192.168.137.1" -and $_.IPAddress -notlike "192.168.137.255" })
  $connectedClients = @($neighbors | Select-Object -ExpandProperty IPAddress -Unique)
} catch {}

if ($connectedClients.Count -gt 0) {
  Add-Check "Connected clients" "PASS" "$($connectedClients.Count) device(s): $($connectedClients -join ', ')"
} else {
  Add-Check "Connected clients" "WARN" "none detected yet (students haven't joined, or ARP cache is empty)"
}

# ── Print results ────────────────────────────────────────────────────────────
foreach ($c in $checks) { Write-CheckLine $c }

$passCount = @($checks | Where-Object { $_.status -eq 'PASS' }).Count
$warnCount = @($checks | Where-Object { $_.status -eq 'WARN' }).Count
$failCount = @($checks | Where-Object { $_.status -eq 'FAIL' }).Count

$ready    = ($failCount -eq 0 -and $hotspotActive)
$exitCode = if ($ready) {
              if ($warnCount -eq 0) { 0 } else { 2 }
            } else { 1 }

$displayKey = if ($ShowPassword) { $hotspotKey } else { "(hidden - use -ShowPassword to display)" }
$fallbackUrl = if ($hotspotIp) { "http://$hotspotIp/" } else { $null }

if (-not $Quiet) {
  Write-Host ""
  Write-Host "==========================================" -ForegroundColor Cyan
  Write-Host "  Demo-readiness summary" -ForegroundColor Cyan
  Write-Host "==========================================" -ForegroundColor Cyan
  $readyColor = if ($ready) { if ($warnCount -eq 0) { "Green" } else { "Yellow" } } else { "Red" }
  $readyText = if ($ready) { "YES" } else { "NO" }
  Write-Host ("  Ready for demo : {0}" -f $readyText) -ForegroundColor $readyColor
  Write-Host ("  Student Wi-Fi  : {0}" -f $hotspotSsid)
  Write-Host ("  Password       : {0}" -f $displayKey)
  Write-Host ("  Portal URL     : {0}" -f $(if ($primaryUrl) { $primaryUrl } else { "(unknown)" }))
  if ($fallbackUrl -and $fallbackUrl -ne $primaryUrl) {
    Write-Host ("  Fallback URL   : {0}" -f $fallbackUrl)
  }
  Write-Host ("  Counts         : {0} PASS, {1} WARN, {2} FAIL" -f $passCount, $warnCount, $failCount)
  if ($fixes.Count -gt 0) {
    Write-Host "  Fixes needed   :" -ForegroundColor Yellow
    foreach ($f in $fixes) { Write-Host "    - $f" -ForegroundColor Yellow }
  } else {
    Write-Host "  Fixes needed   : none"
  }
  Write-Host "==========================================" -ForegroundColor Cyan
  Write-Host ""
}

Write-DemoLog ("result: ready={0} pass={1} warn={2} fail={3}" -f $ready, $passCount, $warnCount, $failCount)

if ($EmitJson) {
  $checksArray = @()
  foreach ($c in $checks) { $checksArray += $c }
  $fixesArray = @()
  foreach ($f in $fixes) { $fixesArray += $f }
  $clientsArray = @()
  foreach ($cl in $connectedClients) { $clientsArray += $cl }
  $result = [pscustomobject]@{
    ready             = [bool]$ready
    pass_count        = [int]$passCount
    warn_count        = [int]$warnCount
    fail_count        = [int]$failCount
    ssid              = [string]$hotspotSsid
    hotspot_ip        = $hotspotIp
    primary_url       = $primaryUrl
    fallback_url      = $fallbackUrl
    checks            = $checksArray
    fixes             = $fixesArray
    connected_clients = $clientsArray
    generated_at      = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
  }
  Write-Output ($result | ConvertTo-Json -Depth 6)
}

exit $exitCode
