# Emits host-only diagnostics for the elevated AIBox admin console.
# Safe to run repeatedly. It stores a tiny network counter snapshot so the next
# invocation can report approximate bandwidth.
#
# -IncludePerCore opts into the more expensive per-logical-core CPU breakdown
# used by the admin console's expand-on-click view. Default emit stays cheap.

param(
  [switch]$Quiet,
  [switch]$IncludePerCore
)

$ErrorActionPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$toolsDir = Split-Path -Parent $runtimeDir
$aiboxDir = Split-Path -Parent $toolsDir
$stateDir = Join-Path $aiboxDir "backend-data\appdata\host-admin"
$stateFile = Join-Path $stateDir "network-metrics-state.json"

function Get-CpuLoadPercent {
  try {
    $samples = @(Get-CimInstance Win32_Processor | Where-Object { $null -ne $_.LoadPercentage })
    if ($samples.Count -eq 0) { return $null }
    return [int](($samples | Measure-Object -Property LoadPercentage -Average).Average)
  } catch {
    return $null
  }
}

function Get-GpuSnapshot {
  $result = [ordered]@{
    available = $false
    load_percent = $null
    memory_used_mb = $null
    memory_total_mb = $null
    temperature_c = $null
    graphics_clock_mhz = $null
  }

  try {
    $line = (& nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,clocks.current.graphics --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($line)) { return $result }
    $parts = @($line -split "," | ForEach-Object { $_.Trim() })
    if ($parts.Count -ge 3) {
      $result.available = $true
      $result.load_percent = [int]$parts[0]
      $result.memory_used_mb = [int]$parts[1]
      $result.memory_total_mb = [int]$parts[2]
    }
    if ($parts.Count -ge 4 -and $parts[3] -match '^\d+$') {
      $result.temperature_c = [int]$parts[3]
    }
    if ($parts.Count -ge 5 -and $parts[4] -match '^\d+$') {
      $result.graphics_clock_mhz = [int]$parts[4]
    }
  } catch {}

  return $result
}

function Get-PerCoreCpu {
  try {
    $cores = @(Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -ErrorAction Stop |
      Where-Object { $_.Name -ne "_Total" })
    return @(
      $cores | Sort-Object {
        try { [int]$_.Name } catch { 999 }
      } | ForEach-Object {
        [ordered]@{
          name = [string]$_.Name
          load_percent = [int]$_.PercentProcessorTime
        }
      }
    )
  } catch {
    return @()
  }
}

function Get-NetworkTotals {
  $rx = [int64]0
  $tx = [int64]0
  $adapters = @()

  try {
    $netAdapters = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
      $_.Status -eq "Up" -and
      $_.Name -notmatch "(?i)loopback|vEthernet|WSL|Docker|Hyper-V" -and
      $_.InterfaceDescription -notmatch "(?i)loopback|virtual|hyper-v"
    })
    foreach ($adapter in $netAdapters) {
      $stats = Get-NetAdapterStatistics -Name $adapter.Name -ErrorAction SilentlyContinue
      if (-not $stats) { continue }
      $rx += [int64]$stats.ReceivedBytes
      $tx += [int64]$stats.SentBytes
      $adapters += [ordered]@{
        name = $adapter.Name
        received_bytes = [int64]$stats.ReceivedBytes
        sent_bytes = [int64]$stats.SentBytes
      }
    }
  } catch {}

  return [ordered]@{
    received_bytes = $rx
    sent_bytes = $tx
    adapters = $adapters
  }
}

function Get-ConnectedHotspotDevices {
  $devices = @()
  try {
    $neighbors = @(Get-NetNeighbor -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
      $_.IPAddress -like "192.168.137.*" -and
      $_.IPAddress -ne "192.168.137.1" -and
      $_.IPAddress -notlike "192.168.137.255" -and
      $_.LinkLayerAddress -and
      $_.State -in @("Reachable", "Stale", "Delay", "Probe", "Permanent")
    })
    foreach ($neighbor in $neighbors) {
      $devices += [ordered]@{
        ip = [string]$neighbor.IPAddress
        mac = [string]$neighbor.LinkLayerAddress
        state = [string]$neighbor.State
      }
    }
  } catch {}

  if ($devices.Count -eq 0) {
    try {
      foreach ($line in (& arp -a 2>$null)) {
        if ($line -match "^\s*(192\.168\.137\.(\d+))\s+([0-9a-fA-F-]{17})\s+\w+") {
          $lastOctet = [int]$Matches[2]
          if ($lastOctet -eq 1 -or $lastOctet -eq 255) { continue }
          $devices += [ordered]@{
            ip = $Matches[1]
            mac = $Matches[3]
            state = "arp"
          }
        }
      }
    } catch {}
  }

  return @($devices | Sort-Object ip -Unique)
}

function Get-SystemMetricsData {
  param([switch]$IncludePerCore)

  $now = Get-Date
  $totals = Get-NetworkTotals
  $rxBps = $null
  $txBps = $null

  try {
    if (Test-Path $stateFile) {
      $previous = Get-Content $stateFile -Raw | ConvertFrom-Json
      $previousTime = [DateTime]::Parse([string]$previous.sampled_at)
      $elapsed = [Math]::Max(0.001, ($now - $previousTime).TotalSeconds)
      if ($previous.received_bytes -le $totals.received_bytes) {
        $rxBps = [int64](($totals.received_bytes - [int64]$previous.received_bytes) / $elapsed)
      }
      if ($previous.sent_bytes -le $totals.sent_bytes) {
        $txBps = [int64](($totals.sent_bytes - [int64]$previous.sent_bytes) / $elapsed)
      }
    }
  } catch {}

  try {
    if (-not (Test-Path $stateDir)) {
      New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    ([ordered]@{
      sampled_at = $now.ToString("o")
      received_bytes = $totals.received_bytes
      sent_bytes = $totals.sent_bytes
    } | ConvertTo-Json -Depth 4) | Set-Content -Path $stateFile -Encoding UTF8
  } catch {}

  $connectedDevices = @(Get-ConnectedHotspotDevices)

  $cpuBlock = [ordered]@{
    load_percent = Get-CpuLoadPercent
  }
  if ($IncludePerCore) {
    $cpuBlock.per_core = Get-PerCoreCpu
  }

  return [ordered]@{
    generated_at = $now.ToString("yyyy-MM-ddTHH:mm:ssK")
    cpu = $cpuBlock
    gpu = Get-GpuSnapshot
    network = [ordered]@{
      received_bytes = $totals.received_bytes
      sent_bytes = $totals.sent_bytes
      receive_bps = $rxBps
      send_bps = $txBps
      adapters = $totals.adapters
    }
    hotspot = [ordered]@{
      connected_device_count = $connectedDevices.Count
      connected_devices = $connectedDevices
    }
  }
}

# Standalone-script behavior: only emit JSON when run as a script (not dot-sourced).
# When dot-sourced (as the admin UI now does), only the function definitions above
# are exposed; the caller invokes Get-SystemMetricsData directly.
if ($MyInvocation.InvocationName -ne '.') {
  $payload = Get-SystemMetricsData -IncludePerCore:$IncludePerCore
  $json = $payload | ConvertTo-Json -Depth 8
  if (-not $Quiet) {
    Write-Host $json
  } else {
    Write-Output $json
  }
}
