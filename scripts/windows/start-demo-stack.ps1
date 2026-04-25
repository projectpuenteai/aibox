# AIBox - Start Demo Stack
# -----------------------------------------------------------------------------
# Orchestrator for the Windows demo mode:
#   1. Run check-hotspot-capability.ps1 (abort on FAIL, continue on WARN)
#   2. Self-elevate to Administrator
#   3. Call the existing engine up_stack.ps1 (which handles Docker Compose +
#      offline hotspot + puente.link hosts entry)
#   4. Wait for the portal /ai/api to respond
#   5. Read portal/network-info.json and print a student connection block
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\start-demo-stack.ps1
#   powershell -ExecutionPolicy Bypass -File .\start-demo-stack.ps1 -SkipCapabilityCheck
#   powershell -ExecutionPolicy Bypass -File .\start-demo-stack.ps1 -SkipGpuProbe

param(
  [switch]$SkipCapabilityCheck,
  [switch]$SkipGpuProbe,
  [switch]$SkipCpuSync,
  [int]$HealthTimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"

# ── Path resolution ──────────────────────────────────────────────────────────
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsDir = $scriptDir
$scriptsDir = Split-Path -Parent $windowsDir
$aiboxDir   = Split-Path -Parent $scriptsDir
$stackDir   = Join-Path $aiboxDir "stack"
$stackEnvFile = Join-Path $stackDir ".env"
$portalDir  = Join-Path $stackDir "portal"
$networkInfoFile = Join-Path $portalDir "network-info.json"
$engineDir  = Join-Path $aiboxDir "tools\llama-runtime\scripts"
$upScript   = Join-Path $engineDir "up_stack.ps1"
$netInfoScript = Join-Path $engineDir "get_network_info.ps1"
$capabilityScript = Join-Path $scriptDir "check-hotspot-capability.ps1"
$logsDir    = Join-Path $aiboxDir "logs"

# ── Helpers ──────────────────────────────────────────────────────────────────
function Test-IsAdministrator {
  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

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
    Add-Content -LiteralPath $logFile -Value "$ts [start-demo-stack] [$Level] $Message" -Encoding UTF8
  } catch {}
}

function Wait-ForPortal {
  param([string]$Url, [int]$TimeoutSeconds)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
      if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500) { return $true }
    } catch {}
    Start-Sleep -Seconds 2
  }
  return $false
}

# ── 0. Self-elevate ──────────────────────────────────────────────────────────
if (-not (Test-IsAdministrator)) {
  Write-Host "start-demo-stack requires Administrator. Elevating..." -ForegroundColor Yellow
  $selfArgs = @("-ExecutionPolicy", "Bypass", "-File", $MyInvocation.MyCommand.Path)
  if ($SkipCapabilityCheck) { $selfArgs += "-SkipCapabilityCheck" }
  if ($SkipGpuProbe)        { $selfArgs += "-SkipGpuProbe" }
  if ($SkipCpuSync)         { $selfArgs += "-SkipCpuSync" }
  if ($HealthTimeoutSeconds -ne 90) { $selfArgs += @("-HealthTimeoutSeconds", "$HealthTimeoutSeconds") }
  try {
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $selfArgs -Verb RunAs -Wait -PassThru
    exit $proc.ExitCode
  } catch {
    Write-Host "[FAIL] Elevation cancelled or blocked." -ForegroundColor Red
    exit 1
  }
}

Write-DemoLog "===== start-demo-stack.ps1 starting ====="

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  AIBox Puente - Demo Stack Startup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Capability check ──────────────────────────────────────────────────────
if (-not $SkipCapabilityCheck) {
  if (-not (Test-Path $capabilityScript)) {
    Write-Host "[WARN] check-hotspot-capability.ps1 not found; skipping pre-flight." -ForegroundColor Yellow
    Write-DemoLog "capability script missing" "warn"
  } else {
    Write-Host "[1/4] Running capability check..." -ForegroundColor Cyan
    Write-DemoLog "running capability check"
    & powershell -ExecutionPolicy Bypass -File $capabilityScript
    $capExit = $LASTEXITCODE
    Write-DemoLog "capability check exit=$capExit"
    if ($capExit -ne 0) {
      Write-Host ""
      Write-Host "[FAIL] Capability check reported FAIL status. Aborting startup." -ForegroundColor Red
      Write-Host "Resolve the failures above, or re-run with -SkipCapabilityCheck to force start." -ForegroundColor Red
      exit 1
    }
    Write-Host ""
  }
} else {
  Write-Host "[1/4] Capability check skipped (-SkipCapabilityCheck)." -ForegroundColor DarkGray
  Write-DemoLog "capability check skipped"
}

# ── 2. Call engine up_stack.ps1 (handles compose + hotspot) ─────────────────
Write-Host "[2/4] Starting stack via engine up_stack.ps1..." -ForegroundColor Cyan
if (-not (Test-Path $upScript)) {
  Write-Host "[FAIL] Engine script not found: $upScript" -ForegroundColor Red
  Write-DemoLog "FAIL engine up_stack.ps1 not found" "error"
  exit 1
}
$upArgs = @("-ExecutionPolicy", "Bypass", "-File", $upScript)
if ($SkipGpuProbe) { $upArgs += "-SkipGpuProbe" }
if ($SkipCpuSync)  { $upArgs += "-SkipCpuSync" }
Write-DemoLog "invoking up_stack.ps1"
& powershell @upArgs
$upExit = $LASTEXITCODE
Write-DemoLog "up_stack.ps1 exit=$upExit"
if ($upExit -ne 0) {
  Write-Host "[FAIL] up_stack.ps1 returned exit code $upExit. Stack may be partially up." -ForegroundColor Red
  Write-Host "       Inspect: docker compose -f $(Join-Path $stackDir 'docker-compose.yaml') ps" -ForegroundColor DarkGray
  exit 1
}

# ── 3. Wait for portal health ────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/4] Waiting for portal to respond at http://127.0.0.1/ ..." -ForegroundColor Cyan
$portalUp = Wait-ForPortal -Url "http://127.0.0.1/" -TimeoutSeconds $HealthTimeoutSeconds
if ($portalUp) {
  Write-Host "      + portal responding" -ForegroundColor Green
  Write-DemoLog "portal reachable at http://127.0.0.1/"
} else {
  Write-Host "      ! portal did not respond within $HealthTimeoutSeconds s" -ForegroundColor Yellow
  Write-Host "        (Caddy may still be warming up; try test-demo-network.ps1 in a minute)" -ForegroundColor DarkGray
  Write-DemoLog "portal not reachable within $HealthTimeoutSeconds s" "warn"
}

# ── 4. Refresh network info + print student connection block ────────────────
Write-Host ""
Write-Host "[4/4] Refreshing network info..." -ForegroundColor Cyan
if (Test-Path $netInfoScript) {
  & powershell -ExecutionPolicy Bypass -File $netInfoScript -Quiet | Out-Null
}

$ssid = Read-EnvValue "HOTSPOT_SSID" "AIBox-Puente"
$key  = Read-EnvValue "HOTSPOT_KEY"  "puente1234"
$offlineHost = Read-EnvValue "OFFLINE_HOSTNAME" "puente.link"

$hotspotStatus = "unknown"
$hotspotReadiness = "unknown"
$hotspotIp = $null
$primaryUrl = $null
$fallbackUrl = $null

if (Test-Path $networkInfoFile) {
  try {
    $info = Get-Content $networkInfoFile -Raw | ConvertFrom-Json
    if ($info.hotspot) {
      $hotspotStatus = [string]$info.hotspot.status
      $hotspotReadiness = [string]$info.hotspot.readiness
      $hotspotIp = [string]$info.hotspot.host_ip
      if ($info.hotspot.ssid) { $ssid = [string]$info.hotspot.ssid }
    }
    if ($info.primary_url) { $primaryUrl = [string]$info.primary_url }
    if ($hotspotIp) { $fallbackUrl = "http://$hotspotIp/" }
  } catch {
    Write-DemoLog "could not parse network-info.json: $($_.Exception.Message)" "warn"
  }
}
if (-not $primaryUrl -and $offlineHost) { $primaryUrl = "http://$offlineHost/" }
if (-not $primaryUrl -and $hotspotIp)   { $primaryUrl = "http://$hotspotIp/" }
if (-not $primaryUrl)                   { $primaryUrl = "http://127.0.0.1/" }

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Students connect here" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ("  Wi-Fi network : {0}" -f $ssid)
Write-Host ("  Wi-Fi password: {0}" -f $key)
Write-Host ("  Portal URL    : {0}" -f $primaryUrl)
if ($fallbackUrl -and $fallbackUrl -ne $primaryUrl) {
  Write-Host ("  Fallback URL  : {0}" -f $fallbackUrl)
}
Write-Host ("  Hotspot       : status=$hotspotStatus readiness=$hotspotReadiness")
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Troubleshooting:" -ForegroundColor Cyan
Write-Host "  - Re-run diagnostics : scripts\windows\test-demo-network.ps1"
Write-Host "  - View hotspot status: scripts\windows\check-hotspot-capability.ps1"
Write-Host "  - Stop the demo      : scripts\windows\stop-demo-stack.ps1"
Write-Host ""

Write-DemoLog "===== start-demo-stack.ps1 complete ssid=$ssid primary_url=$primaryUrl ====="

exit 0
