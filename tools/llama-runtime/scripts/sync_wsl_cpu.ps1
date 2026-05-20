# This script keeps .wslconfig CPU settings aligned with the host so Docker and WSL use the expected processor count.
param(
  [string]$ConfigPath = "",
  [switch]$EmitJson
)

. (Join-Path $PSScriptRoot 'lib\lib_io.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

$ErrorActionPreference = "Stop"

function Get-HostLogicalCpuCount {
  $fromEnv = [Environment]::GetEnvironmentVariable("NUMBER_OF_PROCESSORS")
  $parsed = 0
  if ([int]::TryParse($fromEnv, [ref]$parsed) -and $parsed -gt 0) {
    return $parsed
  }

  $fallback = [Environment]::ProcessorCount
  if ($fallback -gt 0) {
    return $fallback
  }

  throw "Unable to determine host logical CPU count."
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
  $ConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
}

$targetProcessors = Get-HostLogicalCpuCount

$existingLines = @()
if (Test-Path $ConfigPath) {
  $existingLines = Get-Content $ConfigPath
}

$lines = [System.Collections.Generic.List[string]]::new()
foreach ($line in $existingLines) {
  $lines.Add([string]$line)
}

$wslStart = -1
$wslEnd = $lines.Count
for ($i = 0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -match '^\s*\[(.+?)\]\s*$') {
    $section = $Matches[1].Trim().ToLowerInvariant()
    if ($wslStart -ge 0 -and $wslEnd -eq $lines.Count) {
      $wslEnd = $i
    }
    if ($section -eq "wsl2") {
      $wslStart = $i
      $wslEnd = $lines.Count
    }
  }
}

$configuredBefore = $null
$changed = $false

if ($wslStart -lt 0) {
  if ($lines.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($lines[$lines.Count - 1])) {
    $lines.Add("")
  }
  $lines.Add("[wsl2]")
  $lines.Add("processors=$targetProcessors")
  $changed = $true
} else {
  $procIndex = -1
  for ($i = $wslStart + 1; $i -lt $wslEnd; $i++) {
    if ($lines[$i] -imatch '^\s*processors\s*=\s*(.+?)\s*$') {
      if ($procIndex -lt 0) {
        $procIndex = $i
        $configuredBefore = $Matches[1].Trim()
      }
    }
  }

  if ($procIndex -ge 0) {
    if ($configuredBefore -ne [string]$targetProcessors) {
      $lines[$procIndex] = "processors=$targetProcessors"
      $changed = $true
    }
  } else {
    $lines.Insert($wslEnd, "processors=$targetProcessors")
    $changed = $true
  }
}

$configuredAfter = [string]$targetProcessors

if ($changed) {
  $parent = Split-Path -Parent $ConfigPath
  if (-not [string]::IsNullOrWhiteSpace($parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }

  Write-Utf8NoBom -Path $ConfigPath -Lines @($lines)
}

$result = [ordered]@{
  path = $ConfigPath
  host_logical_cpus = $targetProcessors
  configured_processors_before = $configuredBefore
  configured_processors_after = $configuredAfter
  changed = $changed
}

if ($EmitJson) {
  $result | ConvertTo-Json -Compress
  exit 0
}

if ($changed) {
  Write-Info "Updated $ConfigPath to processors=$targetProcessors"
} else {
  Write-Info "No change needed in $ConfigPath (processors=$targetProcessors)."
}

exit 0

