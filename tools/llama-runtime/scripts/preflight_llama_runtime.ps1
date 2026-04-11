# This script validates Docker access, image availability, GPU support, and model-file presence before the stack starts.
param(
  [string]$ComposeFile = "",
  [switch]$SkipGpuProbe
)

$ErrorActionPreference = "Stop"

function Get-DotEnvMap {
  param([string]$Path)

  $map = @{}
  if (-not (Test-Path $Path)) {
    return $map
  }

  foreach ($line in Get-Content $Path) {
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

function Resolve-Setting {
  param(
    [string]$Name,
    [hashtable]$PrimaryMap,
    [hashtable]$SecondaryMap,
    [string]$DefaultValue
  )

  $envValue = [Environment]::GetEnvironmentVariable($Name)
  if (-not [string]::IsNullOrWhiteSpace($envValue)) {
    return @{ Value = $envValue; Source = "env" }
  }

  if ($PrimaryMap.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace($PrimaryMap[$Name])) {
    return @{ Value = $PrimaryMap[$Name]; Source = "stack/.env" }
  }

  if ($SecondaryMap.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace($SecondaryMap[$Name])) {
    return @{ Value = $SecondaryMap[$Name]; Source = "aibox/.env" }
  }

  return @{ Value = $DefaultValue; Source = "default" }
}

function Invoke-Docker {
  param([string[]]$Args)

  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & docker @Args 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $saved
  }

  return @{
    ExitCode = $code
    Output = [string]::Join("`n", @($output))
  }
}

function Fail {
  param(
    [string]$Code,
    [string]$Message
  )

  Write-Host "[error][$Code] $Message" -ForegroundColor Red
  exit 1
}

function Classify-DockerAccessError {
  param([string]$Output)

  if ($Output -match "Access is denied|elevated privileges|open //./pipe/docker_engine|permission denied while trying to connect to the docker API") {
    return "daemon_permission"
  }

  if ($Output -match "Cannot connect to the Docker daemon|daemon is not running|error during connect") {
    return "daemon_unreachable"
  }

  return "daemon_error"
}

function Classify-RegistryError {
  param([string]$Output)

  if ($Output -match "pull access denied|requested access to the resource is denied|unauthorized|authentication required|may require 'docker login'") {
    return "registry_auth"
  }

  if ($Output -match "not found|manifest unknown|repository does not exist") {
    return "image_not_found"
  }

  if ($Output -match "TLS|timeout|i/o timeout|temporary failure|no such host") {
    return "registry_network"
  }

  return "registry_error"
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Split-Path -Parent $scriptDir
$toolsDir = Split-Path -Parent $runtimeDir
$aiboxDir = Split-Path -Parent $toolsDir
$stackDir = Join-Path $aiboxDir "stack"

if ([string]::IsNullOrWhiteSpace($ComposeFile)) {
  $ComposeFile = Join-Path $stackDir "docker-compose.yaml"
}

if (-not (Test-Path $ComposeFile)) {
  Fail "config_missing" "Compose file not found: $ComposeFile"
}

$stackEnvPath = Join-Path (Split-Path -Parent $ComposeFile) ".env"
$repoEnvPath = Join-Path $aiboxDir ".env"
$stackEnv = Get-DotEnvMap -Path $stackEnvPath
$repoEnv = Get-DotEnvMap -Path $repoEnvPath

$llamaImage = Resolve-Setting -Name "LLAMA_IMAGE" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:5c9266b4f92f1ab0d26dd0f2ede2e65d3853cad99ff86ba219db8fe6d464b995"
$modelFile = Resolve-Setting -Name "LLAMA_MODEL_FILE" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf"
$imageMode = Resolve-Setting -Name "LLAMA_IMAGE_MODE" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "prebuilt"

$mode = $imageMode.Value.ToLowerInvariant()
if ($mode -notin @("prebuilt", "local", "auto")) {
  Fail "mode_invalid" "Unsupported LLAMA_IMAGE_MODE '$($imageMode.Value)'. Use prebuilt|local|auto."
}

if ($mode -eq "auto") {
  if ($llamaImage.Value -like "aibox/*") {
    $mode = "local"
  } else {
    $mode = "prebuilt"
  }
}

Write-Host "[info] Compose file: $ComposeFile"
Write-Host "[info] LLAMA_IMAGE = $($llamaImage.Value) [$($llamaImage.Source)]"
Write-Host "[info] LLAMA_IMAGE_MODE = $mode [$($imageMode.Source)]"
Write-Host "[info] LLAMA_MODEL_FILE = $($modelFile.Value) [$($modelFile.Source)]"

$dockerInfo = Invoke-Docker -Args @("info")
if ($dockerInfo.Output -match "Error loading config file: .*Access is denied") {
  Fail "docker_config_permission" "Docker config file is not readable. Fix access to %USERPROFILE%\\.docker\\config.json or run under the correct user profile."
}

if ($dockerInfo.ExitCode -ne 0) {
  $code = Classify-DockerAccessError -Output $dockerInfo.Output
  if ($code -eq "daemon_permission") {
    Fail $code "Docker daemon permission denied. Run PowerShell as Administrator or fix Docker Desktop user permissions."
  }
  if ($code -eq "daemon_unreachable") {
    Fail $code "Docker daemon not reachable. Start Docker Desktop and retry."
  }
  Fail $code "Docker info failed: $($dockerInfo.Output)"
}

if ($mode -eq "local") {
  $inspect = Invoke-Docker -Args @("image", "inspect", $llamaImage.Value)
  if ($inspect.ExitCode -ne 0) {
    $buildScript = Join-Path $scriptDir "build_llama_image.ps1"
    Fail "local_image_missing" "Local image '$($llamaImage.Value)' not found. Build it first: powershell -ExecutionPolicy Bypass -File $buildScript"
  }
  Write-Host "[info] Local image present: $($llamaImage.Value)"
} else {
  $manifest = Invoke-Docker -Args @("manifest", "inspect", $llamaImage.Value)
  if ($manifest.ExitCode -ne 0) {
    $code = Classify-RegistryError -Output $manifest.Output
    if ($code -eq "registry_auth") {
      Fail $code "Registry auth denied for '$($llamaImage.Value)'. Run docker login or use a public valid image."
    }
    if ($code -eq "image_not_found") {
      Fail $code "Image/tag not found: '$($llamaImage.Value)'."
    }
    if ($code -eq "registry_network") {
      Fail $code "Registry network failure while checking '$($llamaImage.Value)'."
    }
    Fail $code "Unable to verify pullability for '$($llamaImage.Value)': $($manifest.Output)"
  }
  Write-Host "[info] Prebuilt image pullability verified: $($llamaImage.Value)"
}

if (-not $SkipGpuProbe) {
  $runtimeResult = Invoke-Docker -Args @("info", "--format", "{{json .Runtimes}}")
  $runtimesJson = $runtimeResult.Output
  if ($runtimeResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($runtimesJson)) {
    Fail "gpu_probe_failed" "Unable to query Docker runtimes."
  }

  if ($runtimesJson -notmatch 'nvidia') {
    # Docker Desktop (WSL2) commonly supports --gpus without listing an explicit "nvidia" runtime in .Runtimes.
    # Fall back to an actual GPU probe container to avoid false negatives.
    $probe = Invoke-Docker -Args @(
      "run", "--rm",
      "--gpus", "all",
      "nvidia/cuda:12.4.1-runtime-ubuntu22.04",
      "nvidia-smi"
    )
    if ($probe.ExitCode -ne 0) {
      Fail "nvidia_runtime_missing" "NVIDIA GPU not usable from containers. Ensure Docker Desktop GPU support and WSL2 NVIDIA CUDA are installed. Probe error: $($probe.Output)"
    }
    Write-Host "[info] NVIDIA GPU probe succeeded (nvidia-smi)."
  } else {
    Write-Host "[info] Docker runtimes: $runtimesJson"
  }
}

$modelRoot = Join-Path $aiboxDir "models\llm\gguf"
$modelPath = Join-Path $modelRoot $modelFile.Value
if (-not (Test-Path $modelPath)) {
  $available = ""
  if (Test-Path $modelRoot) {
    $available = (Get-ChildItem -Path $modelRoot -Filter *.gguf -File | Select-Object -ExpandProperty Name) -join ", "
  }
  Fail "model_missing" "Model file not found: $modelPath. Available: $available"
}

Write-Host "[ok] Preflight passed." -ForegroundColor Green
exit 0



