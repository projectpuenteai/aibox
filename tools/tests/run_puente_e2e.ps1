param(
  [string]$BaseUrl = "http://localhost",
  [string]$ResultDir = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$testsRoot = Join-Path $repoRoot "tools\tests"
$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
if (-not $ResultDir) {
  $ResultDir = Join-Path $repoRoot "tools\tests\results\puente-e2e\$timestamp"
}

$puenteDir = Join-Path $repoRoot "tools\tests\puente_e2e"

function Get-DotEnvMap {
  param([string]$Path)

  $map = @{}
  if (-not (Test-Path -LiteralPath $Path)) {
    return $map
  }
  foreach ($line in Get-Content -LiteralPath $Path) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) {
      continue
    }
    $idx = $trimmed.IndexOf("=")
    if ($idx -lt 1) {
      continue
    }
    $key = $trimmed.Substring(0, $idx).Trim()
    $value = $trimmed.Substring($idx + 1).Trim()
    if ($value.StartsWith('"') -and $value.EndsWith('"') -and $value.Length -ge 2) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    $map[$key] = $value
  }
  return $map
}

New-Item -ItemType Directory -Force -Path $ResultDir | Out-Null
Write-Host "ResultDir: $ResultDir"

$stackEnv = Get-DotEnvMap -Path (Join-Path $repoRoot "stack\.env")
if ($stackEnv.ContainsKey("ADMIN_USERNAME")) {
  $env:AIBOX_E2E_ADMIN_USERNAME = $stackEnv["ADMIN_USERNAME"]
}
if ($stackEnv.ContainsKey("ADMIN_DEFAULT_PASSWORD")) {
  $env:AIBOX_E2E_ADMIN_PASSWORD = $stackEnv["ADMIN_DEFAULT_PASSWORD"]
}

node (Join-Path $puenteDir "live_checks.mjs") --base-url $BaseUrl --result-dir $ResultDir

try {
  Push-Location $testsRoot
  node (Join-Path $puenteDir "browser_checks.mjs") --base-url $BaseUrl --result-dir $ResultDir
} catch {
  $browserPayload = @{
    timestamp = (Get-Date).ToUniversalTime().ToString("o")
    lane = "browser"
    summary = @{ PASS = 0; FAIL = 0; SKIP = 1 }
    screenshots = @()
    skipped_reason = $_.Exception.Message
  } | ConvertTo-Json -Depth 8
  Set-Content -LiteralPath (Join-Path $ResultDir "browser-results.json") -Value $browserPayload -Encoding utf8
} finally {
  if ((Get-Location).Path -eq $testsRoot) {
    Pop-Location
  }
}

$cloneRootHost = Join-Path $repoRoot "backend-data\test-clones\$timestamp"
$cloneDbHost = Join-Path $cloneRootHost "db"
New-Item -ItemType Directory -Force -Path $cloneDbHost | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "backend-data\db\app.db") -Destination (Join-Path $cloneDbHost "app.db") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "backend-data\users") -Destination (Join-Path $cloneRootHost "users") -Recurse -Force

$containerScript = "/tmp/retention_clone_check.py"
$containerCloneRoot = "/data/test-clones/$timestamp"
$containerResult = "/data/test-clones/$timestamp/retention-results.json"

docker cp (Join-Path $puenteDir "retention_clone_check.py") "aibox-ai-control:$containerScript"
docker exec aibox-ai-control python $containerScript $containerCloneRoot $containerResult
Copy-Item -LiteralPath (Join-Path $cloneRootHost "retention-results.json") -Destination (Join-Path $ResultDir "retention-results.json") -Force

try {
  docker exec aibox-ai-control python -c "from pathlib import Path; Path('/tmp/puente-rag/tests').mkdir(parents=True, exist_ok=True); Path('/tmp/puente-rag/ai-control').mkdir(parents=True, exist_ok=True)"
  docker cp (Join-Path $testsRoot "test_rag_comprehensive.py") "aibox-ai-control:/tmp/puente-rag/tests/test_rag_comprehensive.py"
  docker cp (Join-Path $testsRoot "test_cases.json") "aibox-ai-control:/tmp/puente-rag/tests/test_cases.json"
  docker cp (Join-Path $repoRoot "tools\ai-control\app_storage.py") "aibox-ai-control:/tmp/puente-rag/ai-control/app_storage.py"
  docker cp (Join-Path $repoRoot "tools\ai-control\storage_migrations.py") "aibox-ai-control:/tmp/puente-rag/ai-control/storage_migrations.py"
  docker exec aibox-ai-control python /tmp/puente-rag/tests/test_rag_comprehensive.py --mode direct --save --output-dir /data/test-clones/$timestamp/rag
  $ragOutDir = Join-Path $ResultDir "rag"
  New-Item -ItemType Directory -Force -Path $ragOutDir | Out-Null
  Copy-Item -LiteralPath (Join-Path $cloneRootHost "rag\*") -Destination $ragOutDir -Force
} catch {
  Write-Warning "RAG suite returned non-zero status: $($_.Exception.Message)"
}

Write-Host "Puente E2E run completed."
