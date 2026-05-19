param(
  [string]$ComposeFile = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
if ([string]::IsNullOrWhiteSpace($ComposeFile)) {
  $ComposeFile = Join-Path $repoRoot "stack\docker-compose.yaml"
}

$saved = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $raw = & docker compose -f $ComposeFile config 2>&1
  $code = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $saved
}
if ($code -ne 0) {
  $raw | ForEach-Object { Write-Output $_ }
  exit $code
}

$secretKeys = @(
  "APP_ENCRYPTION_MASTER_KEY",
  "ADMIN_DEFAULT_PASSWORD",
  "SESSION_TOKEN_PEPPER",
  "DNS_SERVER_ADMIN_PASSWORD",
  "DNS_ADMIN_PASSWORD",
  "HOTSPOT_KEY"
)

foreach ($line in $raw) {
  $text = [string]$line
  foreach ($key in $secretKeys) {
    $escaped = [regex]::Escape($key)
    $text = $text -replace "(${escaped}:\s*).+$", "`$1[REDACTED]"
    $text = $text -replace "(-\s*${escaped}=).+$", "`$1[REDACTED]"
  }
  Write-Output $text
}
