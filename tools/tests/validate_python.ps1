param(
  [string]$Python = "py -3"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolsDir = Split-Path -Parent $scriptDir
$pythonParts = $Python.Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)

function Invoke-Python {
  param([string[]]$Arguments)

  if ($pythonParts.Count -gt 1) {
    & $pythonParts[0] @($pythonParts[1..($pythonParts.Count - 1)]) @Arguments
  } else {
    & $pythonParts[0] @Arguments
  }
}

$sourceDirs = @(
  "ai-control",
  "benchmarks",
  "config",
  "data_prep",
  "index",
  "storage"
)

foreach ($name in $sourceDirs) {
  $path = Join-Path $toolsDir $name
  if (-not (Test-Path -LiteralPath $path -PathType Container)) {
    continue
  }
  Write-Host "[compile] $path"
  Invoke-Python -Arguments @("-m", "compileall", "-q", $path)
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

$testFiles = Get-ChildItem -LiteralPath $scriptDir -Recurse -Filter "*.py" -File |
  Where-Object {
    $_.FullName -notmatch "\\node_modules\\" -and
    $_.FullName -notmatch "\\results\\" -and
    $_.FullName -notmatch "\\__pycache__\\"
  }
foreach ($file in $testFiles) {
  Write-Host "[compile] $($file.FullName)"
  Invoke-Python -Arguments @("-m", "py_compile", $file.FullName)
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

Write-Host "[ok] Python compile validation passed."
