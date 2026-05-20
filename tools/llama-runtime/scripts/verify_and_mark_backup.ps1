# Runs the AIBox appdata integrity verification (sqlite integrity_check +
# encrypted-blob decrypt sample) and, on success, writes the
# `latest_verified_backup.json` marker that `run_cleanup` requires when
# CLEANUP_REQUIRE_BACKUP_MARKER=1 (the production default).
#
# Without this marker, the background and on-demand cleanup paths refuse to
# delete anything once the host disk crosses 85% usage, which in turn blocks
# every write path (new chat, document upload, message append). This script is
# the supported way to keep that marker fresh.
#
# Modes:
#   (default)         Verify the LIVE appdata and write the marker. No backup
#                     copy is made. Use when an external backup process runs
#                     separately and you only need to re-arm the marker.
#   -BackupRoot PATH  Robocopy the appdata to <PATH>\appdata first, verify the
#                     COPY, then write the marker on the live path. Use when
#                     this script IS your backup process.
#   -VerifyOnly       With -BackupRoot, skip the copy and just verify the
#                     existing destination.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\verify_and_mark_backup.ps1
#   powershell -ExecutionPolicy Bypass -File .\verify_and_mark_backup.ps1 -BackupRoot D:\AIBox-backup
#   powershell -ExecutionPolicy Bypass -File .\verify_and_mark_backup.ps1 -BackupRoot D:\AIBox-backup -VerifyOnly
#   powershell -ExecutionPolicy Bypass -File .\verify_and_mark_backup.ps1 -EmitJson -JsonOutFile out.json
#
# Schedule daily (run as a user that can read the .env and write the marker):
#   schtasks /Create /SC DAILY /ST 03:00 /TN "AIBox-VerifyBackup" `
#     /TR "powershell -ExecutionPolicy Bypass -File C:\AIBox\aibox\tools\llama-runtime\scripts\verify_and_mark_backup.ps1" /F
#
# Exit codes:
#   0  marker written
#   1  verification failed; marker NOT written

param(
  [string]$BackupRoot = "",
  [switch]$VerifyOnly,
  [string]$EnvFile = "",
  [int]$MaxSamples = 25,
  [switch]$EmitJson,
  [string]$JsonOutFile = ""
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$toolsDir   = Split-Path -Parent $runtimeDir
$aiboxDir   = Split-Path -Parent $toolsDir
$repoRoot   = Split-Path -Parent $aiboxDir

$appDataRoot  = Join-Path $aiboxDir "backend-data\appdata"
$verifyScript = Join-Path $aiboxDir "tools\storage\verify_storage_backup.py"
$defaultEnv   = Join-Path $aiboxDir "stack\.env"
$markerDir    = Join-Path $appDataRoot "backups"
$markerPath   = Join-Path $markerDir "latest_verified_backup.json"

$result = [ordered]@{
  ok                          = $false
  marker_path                 = $markerPath
  appdata_root                = $appDataRoot
  verify_target               = $null
  backup_copy_performed       = $false
  db_integrity_ok             = $false
  encrypted_samples_checked   = 0
  encrypted_samples_failed    = 0
  marker_written              = $false
  errors                      = New-Object System.Collections.Generic.List[string]
  generated_at                = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
}

function Emit-Result {
  param([int]$ExitCode = 0)
  $result.ok = ($result.errors.Count -eq 0 -and $result.marker_written)
  $json = $result | ConvertTo-Json -Depth 8
  if (-not [string]::IsNullOrWhiteSpace($JsonOutFile)) {
    $json | Set-Content -Path $JsonOutFile -Encoding UTF8
  }
  if ($EmitJson) { Write-Output $json }
  exit $ExitCode
}

Write-Host ""
Write-Host "=== AIBox: verify + mark backup ===" -ForegroundColor Cyan
Write-Host ""

# Resolve the verify script and appdata root early so we fail fast.
if (-not (Test-Path $verifyScript)) {
  $result.errors.Add("verify_storage_backup.py not found at $verifyScript")
  Emit-Result -ExitCode 1
}
if (-not (Test-Path $appDataRoot)) {
  $result.errors.Add("appdata root not found at $appDataRoot")
  Emit-Result -ExitCode 1
}

# Load APP_ENCRYPTION_MASTER_KEY from stack/.env if not already exported.
if ([string]::IsNullOrWhiteSpace($env:APP_ENCRYPTION_MASTER_KEY)) {
  $envToUse = if ([string]::IsNullOrWhiteSpace($EnvFile)) { $defaultEnv } else { $EnvFile }
  if (-not (Test-Path $envToUse)) {
    $result.errors.Add("APP_ENCRYPTION_MASTER_KEY not set and .env not found at $envToUse")
    Emit-Result -ExitCode 1
  }
  foreach ($line in Get-Content $envToUse) {
    if ($line -match '^APP_ENCRYPTION_MASTER_KEY\s*=\s*(.+?)\s*$') {
      $env:APP_ENCRYPTION_MASTER_KEY = $matches[1].Trim('"').Trim("'")
      break
    }
  }
  if ([string]::IsNullOrWhiteSpace($env:APP_ENCRYPTION_MASTER_KEY)) {
    $result.errors.Add("APP_ENCRYPTION_MASTER_KEY not found in $envToUse")
    Emit-Result -ExitCode 1
  }
}

# Pick a Python interpreter. Prefer the project venv so the cryptography deps
# match the runtime; fall back to py / python on PATH.
$venvPython = Join-Path $repoRoot ".venv-rag\Scripts\python.exe"
$python = $null
if (Test-Path $venvPython) {
  $python = $venvPython
} else {
  foreach ($cand in @("py", "python")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
  }
}
if (-not $python) {
  $result.errors.Add("No Python interpreter found (looked for .venv-rag, py, python)")
  Emit-Result -ExitCode 1
}

# Decide what we are verifying.
$verifyTarget = $appDataRoot
if (-not [string]::IsNullOrWhiteSpace($BackupRoot)) {
  $verifyTarget = Join-Path $BackupRoot "appdata"
  if (-not $VerifyOnly) {
    Write-Host "[1/3] robocopy $appDataRoot -> $verifyTarget"
    if (-not (Test-Path $verifyTarget)) {
      New-Item -ItemType Directory -Force -Path $verifyTarget | Out-Null
    }
    & robocopy $appDataRoot $verifyTarget /MIR /R:2 /W:5 /NFL /NDL /NJH /NJS | Out-Null
    # robocopy returns 0-3 for success; >=8 is an error.
    if ($LASTEXITCODE -ge 8) {
      $result.errors.Add("robocopy failed with exit code $LASTEXITCODE")
      Emit-Result -ExitCode 1
    }
    $result.backup_copy_performed = $true
  } else {
    Write-Host "[1/3] -VerifyOnly: skipping copy, verifying existing $verifyTarget"
  }
} else {
  Write-Host "[1/3] No -BackupRoot specified; verifying live appdata at $appDataRoot"
}
$result.verify_target = $verifyTarget

# Run verify_storage_backup.py. It prints JSON; we capture and parse.
Write-Host "[2/3] Running verify_storage_backup.py against $verifyTarget"
$verifyArgs = @($verifyScript, "--appdata-root", $verifyTarget, "--max-samples", $MaxSamples.ToString())
$verifyOutput = & $python @verifyArgs 2>&1
$verifyExit = $LASTEXITCODE
$verifyText = ($verifyOutput | Out-String).Trim()
Write-Host $verifyText
$verifyObj = $null
try {
  $verifyObj = $verifyText | ConvertFrom-Json
} catch {
  $result.errors.Add("Could not parse verify_storage_backup.py JSON output: $($_.Exception.Message)")
  Emit-Result -ExitCode 1
}
$result.db_integrity_ok           = [bool]$verifyObj.db_integrity_ok
$result.encrypted_samples_checked = [int]$verifyObj.encrypted_samples_checked
$result.encrypted_samples_failed  = [int]$verifyObj.encrypted_samples_failed
if ($verifyExit -ne 0 -or -not $result.db_integrity_ok -or $result.encrypted_samples_failed -gt 0) {
  $result.errors.Add("verify_storage_backup.py reported failure (exit=$verifyExit, integrity_ok=$($result.db_integrity_ok), encrypted_failed=$($result.encrypted_samples_failed))")
  if ($verifyObj.errors) {
    foreach ($e in @($verifyObj.errors)) { $result.errors.Add("verify: $e") }
  }
  Emit-Result -ExitCode 1
}

# Write the marker. cleanup_backup_marker_is_fresh() requires top-level
# `verified: true` plus a parseable `verified_at` (or `created_at`).
Write-Host "[3/3] Writing marker to $markerPath"
New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
$marker = [ordered]@{
  verified                  = $true
  verified_at               = (Get-Date).ToUniversalTime().ToString("o")
  source                    = "verify_and_mark_backup.ps1"
  db_integrity_ok           = $true
  encrypted_samples_checked = $result.encrypted_samples_checked
  encrypted_samples_failed  = 0
  backup_destination        = $(if ($result.backup_copy_performed) { (Resolve-Path $verifyTarget).Path } else { $null })
  verified_target           = $verifyTarget
  appdata_source            = $appDataRoot
}
# Write UTF-8 WITHOUT a BOM. PS 5.1's `Set-Content -Encoding UTF8` emits one,
# and Python's read_text(encoding="utf-8") in app_storage.cleanup_backup_marker_is_fresh
# treats a leading BOM as an unexpected non-JSON char and rejects the marker.
$markerJson = $marker | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($markerPath, $markerJson, (New-Object System.Text.UTF8Encoding $false))
$result.marker_written = $true

Write-Host ""
Write-Ok "Verified appdata integrity and refreshed cleanup marker."
Emit-Result -ExitCode 0
