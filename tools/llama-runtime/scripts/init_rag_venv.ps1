# Creates the .venv-rag virtual environment used by the offline-wiki
# indexing pipeline (extract_pages_from_dump, chunk_pages_for_rag,
# build_chroma_index) and installs the requirements from aibox/requirements.txt.
#
# Idempotent. Does not touch an existing venv; pass -Recreate to wipe it first.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\init_rag_venv.ps1
#   powershell -ExecutionPolicy Bypass -File .\init_rag_venv.ps1 -Recreate
#   powershell -ExecutionPolicy Bypass -File .\init_rag_venv.ps1 -Python "C:\Python311\python.exe"

param(
  [switch]$Recreate,
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir  = Split-Path -Parent $scriptDir
$toolsDir    = Split-Path -Parent $runtimeDir
$aiboxDir    = Split-Path -Parent $toolsDir
$venvDir     = Join-Path $aiboxDir ".venv-rag"
$reqsFile    = Join-Path $aiboxDir "requirements.txt"

if (-not (Test-Path $reqsFile)) {
  throw "requirements.txt not found at $reqsFile"
}

if ([string]::IsNullOrWhiteSpace($Python)) {
  # Prefer the `py` launcher (Windows). Fall back to `python` on PATH.
  $pyCmd = Get-Command "py" -ErrorAction SilentlyContinue
  if ($pyCmd) {
    $Python = $pyCmd.Source
    $pyArgs = @("-3")
  } else {
    $pythonCmd = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
      throw "Could not find a Python interpreter. Install Python 3.11+ or pass -Python <path>."
    }
    $Python = $pythonCmd.Source
    $pyArgs = @()
  }
} else {
  if (-not (Test-Path $Python)) {
    throw "Specified Python not found: $Python"
  }
  $pyArgs = @()
}

if ($Recreate -and (Test-Path $venvDir)) {
  Write-Host "Removing existing venv at $venvDir ..." -ForegroundColor Yellow
  Remove-Item -Recurse -Force -LiteralPath $venvDir
}

if (-not (Test-Path $venvDir)) {
  Write-Host "Creating venv at $venvDir ..." -ForegroundColor Cyan
  & $Python @pyArgs -m venv $venvDir
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
} else {
  Write-Host "Reusing existing venv at $venvDir" -ForegroundColor Green
}

$venvPy = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  throw "Expected venv Python not found at $venvPy"
}

Write-Host "Upgrading pip ..." -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit $LASTEXITCODE)" }

Write-Host "Installing requirements from $reqsFile ..." -ForegroundColor Cyan
& $venvPy -m pip install -r $reqsFile
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "Done. Activate with:" -ForegroundColor Green
Write-Host "  & '$venvDir\Scripts\Activate.ps1'" -ForegroundColor DarkGray
Write-Host "Or run scripts directly with:" -ForegroundColor Green
Write-Host "  & '$venvPy' -m tools.index.build_chroma_index --help" -ForegroundColor DarkGray
