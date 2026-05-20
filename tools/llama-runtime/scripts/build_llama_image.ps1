# This script builds a local llama.cpp CUDA image by cloning the upstream repo and using its server Dockerfile.
param(
  [string]$ImageTag = "aibox/llama-server:cuda-local",
  [string]$LlamaCppRef = "b8390",
  [string]$LlamaCppRepo = "https://github.com/ggml-org/llama.cpp.git",
  [switch]$NoCache,
  [switch]$KeepSource
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

function Run-Command {
  param([scriptblock]$Cmd, [string]$FailureMessage)
  & $Cmd
  if ($LASTEXITCODE -ne 0) {
    throw "$FailureMessage (exit code $LASTEXITCODE)"
  }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$buildRoot = Join-Path $env:TEMP "aibox-llama-cpp-src"
$srcDir = Join-Path $buildRoot "llama.cpp"
$dockerfilePath = Join-Path $srcDir ".devops\cuda.Dockerfile"

Write-Info "Building llama fallback image via upstream cuda.Dockerfile"
Write-Info "Tag: $ImageTag"
Write-Info "llama.cpp repo: $LlamaCppRepo"
Write-Info "llama.cpp ref: $LlamaCppRef"
Write-Info "build source dir: $srcDir"

if (Test-Path $buildRoot) {
  Remove-Item -Force -Recurse $buildRoot
}
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null

Run-Command -Cmd { git clone --depth 1 $LlamaCppRepo $srcDir } -FailureMessage "git clone failed; check repo or network"

# Support branch, tag, or commit-ish without requiring full history.
Run-Command -Cmd { git -C $srcDir fetch --depth 1 origin $LlamaCppRef } -FailureMessage "git fetch failed; check repo/ref or network"
Run-Command -Cmd { git -C $srcDir checkout --detach FETCH_HEAD } -FailureMessage "git checkout failed; check repo/ref"

if (-not (Test-Path $dockerfilePath)) {
  throw "Expected upstream Dockerfile not found: $dockerfilePath"
}

$buildArgs = @(
  "build",
  "--target", "server",
  "-f", $dockerfilePath,
  "-t", $ImageTag
)

if ($NoCache) {
  $buildArgs += "--no-cache"
}

$buildArgs += $srcDir

& docker @buildArgs
if ($LASTEXITCODE -ne 0) {
  throw "docker build failed (exit code $LASTEXITCODE)"
}

if (-not $KeepSource) {
  Remove-Item -Force -Recurse $buildRoot
}

Write-Ok "Built fallback image: $ImageTag"


