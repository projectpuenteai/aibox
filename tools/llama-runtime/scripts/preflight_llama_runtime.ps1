# This script validates Docker access, image availability, GPU support, and model-file presence before the stack starts.
param(
  [string]$ComposeFile = "",
  [switch]$SkipGpuProbe,
  [switch]$OnlineImageCheck
)

$ErrorActionPreference = "Stop"

# Pure model-integrity helpers (Test-ModelDirectoryIntegrity, Get-GgufShardPlan,
# Test-GgufShardSet). Side-effect-free so the Pester suite can exercise them
# without dot-sourcing this script.
. (Join-Path $PSScriptRoot 'lib\lib_model.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_env.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')
$script:EnvDefaultsPath = Join-Path $PSScriptRoot '..\..\..\stack\.env.defaults'
$script:EnvDefaults = if (Test-Path -LiteralPath $script:EnvDefaultsPath) { Get-DotEnvMap -Path $script:EnvDefaultsPath } else { @{} }

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

  Write-Err "[$Code] $Message"
  exit 1
}

function Test-RequiredSetting {
  param(
    [string]$Name,
    [hashtable]$PrimaryMap,
    [hashtable]$SecondaryMap
  )

  $setting = Resolve-Setting -Name $Name -PrimaryMap $PrimaryMap -SecondaryMap $SecondaryMap -DefaultValue ""
  if ([string]::IsNullOrWhiteSpace([string]$setting.Value)) {
    Fail "missing_required_env" "$Name is required. Set it in stack/.env before startup."
  }
  return $setting
}

function Test-RequiredFile {
  param(
    [string]$Code,
    [string]$Path,
    [string]$Description
  )

  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Fail $Code "$Description not found: $Path"
  }
  Write-Info "Found $Description`: $Path"
}

function Test-RequiredDirectory {
  param(
    [string]$Code,
    [string]$Path,
    [string]$Description,
    [switch]$RequireContent,
    [string[]]$RequiredFiles
  )

  $result = Test-ModelDirectoryIntegrity -Path $Path -RequireContent:$RequireContent -RequiredFiles $RequiredFiles
  if (-not $result.Ok) {
    switch ($result.Reason) {
      'directory_missing'     { Fail $Code "$Description directory not found: $Path" }
      'directory_empty'       { Fail $Code "$Description directory is empty: $Path" }
      'required_file_missing' { Fail $Code "$Description $($result.Message)" }
      default                 { Fail $Code "$Description integrity check failed: $($result.Message)" }
    }
  }

  Write-Info "Found $Description`: $Path"
}

function Test-DockerVolumeDirectory {
  param(
    [string]$VolumeName,
    [string]$Description,
    [string]$RequiredFile
  )

  $inspect = Invoke-Docker -Args @("volume", "inspect", $VolumeName)
  if ($inspect.ExitCode -ne 0) {
    Fail "volume_missing" "$Description Docker volume '$VolumeName' was not found. Populate it before startup or switch compose back to a bind mount."
  }

  if (-not (Test-LocalDockerImage -ImageRef $script:VolumeProbeImage)) {
    # Probe image is absent (e.g. first boot, fully offline). Try a host-side
    # mountpoint check against the Docker volume; if the WSL-visible _data path
    # is populated we treat the volume as good and continue with a warning.
    $mountpoint = $null
    $inspectMount = Invoke-Docker -Args @("volume", "inspect", "--format", "{{.Mountpoint}}", $VolumeName)
    if ($inspectMount.ExitCode -eq 0) {
      $mountpoint = ($inspectMount.Output | Out-String).Trim()
    }

    $hostCheckPassed = $false
    if (-not [string]::IsNullOrWhiteSpace($mountpoint)) {
      # Docker Desktop on Windows stores volumes inside the docker-desktop-data
      # WSL distro. Probe the volume's _data dir via wsl.exe — non-blocking,
      # ignores errors, and we only treat a non-empty listing as success.
      $wslPath = "/mnt/wsl/docker-desktop-data/data/docker/volumes/$VolumeName/_data"
      $listing = & wsl.exe -e sh -c "ls -A '$wslPath' 2>/dev/null | head -1" 2>$null
      if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($listing | Out-String))) {
        $hostCheckPassed = $true
      }
    }

    if ($hostCheckPassed) {
      Write-Warn "Volume probe image '$script:VolumeProbeImage' absent - falling back to host-side mountpoint check, $Description volume '$VolumeName' looks populated."
      return
    }

    Fail "volume_probe_image_missing" "Volume probe image '$script:VolumeProbeImage' is not present locally. Pull/start the stack once while online, then retry preflight."
  }
  $probeScript = "test -d /data && test -n `"$(find /data -mindepth 1 -maxdepth 1 -print -quit)`""
  if (-not [string]::IsNullOrWhiteSpace($RequiredFile)) {
    $probeScript = "$probeScript && test -f /data/$RequiredFile"
  }
  $probe = Invoke-Docker -Args @("run", "--rm", "-v", "${VolumeName}:/data:ro", $script:VolumeProbeImage, "sh", "-c", $probeScript)
  if ($probe.ExitCode -ne 0) {
    $requiredText = if ($RequiredFile) { " containing $RequiredFile" } else { "" }
    Fail "volume_empty" "$Description Docker volume '$VolumeName' is missing expected content$requiredText."
  }
  Write-Info "Found $Description Docker volume: $VolumeName"
}

function Test-LocalDockerImage {
  param([string]$ImageRef)

  $inspect = Invoke-Docker -Args @("image", "inspect", $ImageRef)
  if ($inspect.ExitCode -eq 0) {
    return $true
  }

  if ($ImageRef -match "@sha256:([a-fA-F0-9]{64})$") {
    $digest = $Matches[1].ToLowerInvariant()
    $all = Invoke-Docker -Args @("image", "ls", "--digests", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.Digest}}")
    if ($all.ExitCode -eq 0 -and $all.Output.ToLowerInvariant().Contains("sha256:$digest")) {
      return $true
    }
  }

  return $false
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

$requiredSettings = @(
  "APP_ENCRYPTION_MASTER_KEY",
  "ADMIN_USERNAME",
  "ADMIN_DEFAULT_PASSWORD",
  "SESSION_TOKEN_PEPPER",
  "DNS_ADMIN_PASSWORD"
)

foreach ($requiredName in $requiredSettings) {
  $null = Test-RequiredSetting -Name $requiredName -PrimaryMap $stackEnv -SecondaryMap $repoEnv
}
Write-Info "Required stack secrets are configured."

$llamaImageDefault = if ($script:EnvDefaults.ContainsKey('LLAMA_IMAGE')) { $script:EnvDefaults['LLAMA_IMAGE'] } else { "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:5c9266b4f92f1ab0d26dd0f2ede2e65d3853cad99ff86ba219db8fe6d464b995" }
$llamaImage = Resolve-Setting -Name "LLAMA_IMAGE" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue $llamaImageDefault
$modelFile = Resolve-Setting -Name "LLAMA_MODEL_FILE" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf"
$imageMode = Resolve-Setting -Name "LLAMA_IMAGE_MODE" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "prebuilt"
$volumeProbeImageDefault = if ($script:EnvDefaults.ContainsKey('VOLUME_PROBE_IMAGE')) { $script:EnvDefaults['VOLUME_PROBE_IMAGE'] } else { "caddy:2@sha256:ec18ee54aab3315c22e25f3b2babda73ff8007d39b13b3bd1bfffa2f0444c7d9" }
$volumeProbeImage = Resolve-Setting -Name "VOLUME_PROBE_IMAGE" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue $volumeProbeImageDefault
$script:VolumeProbeImage = $volumeProbeImage.Value
$embedModel = Resolve-Setting -Name "EMBED_MODEL" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "/models/embed-m3"
$rerankModel = Resolve-Setting -Name "RERANK_MODEL" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "/models/rerank"
$retrievalEnabled = Resolve-Setting -Name "RETRIEVAL_ENABLED_DEFAULT" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "1"
$chromaCollection = Resolve-Setting -Name "CHROMA_COLLECTION" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "simplewiki_chunks"
$chromaCollectionEs = Resolve-Setting -Name "CHROMA_COLLECTION_ES" -PrimaryMap $stackEnv -SecondaryMap $repoEnv -DefaultValue "simplewiki_chunks"

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

Write-Info "Compose file: $ComposeFile"
Write-Info "LLAMA_IMAGE = $($llamaImage.Value) [$($llamaImage.Source)]"
Write-Info "LLAMA_IMAGE_MODE = $mode [$($imageMode.Source)]"
Write-Info "VOLUME_PROBE_IMAGE = $script:VolumeProbeImage [$($volumeProbeImage.Source)]"
Write-Info "LLAMA_MODEL_FILE = $($modelFile.Value) [$($modelFile.Source)]"
Write-Info "CHROMA_COLLECTION = $($chromaCollection.Value) [$($chromaCollection.Source)]"
Write-Info "CHROMA_COLLECTION_ES = $($chromaCollectionEs.Value) [$($chromaCollectionEs.Source)]"

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
  if (-not (Test-LocalDockerImage -ImageRef $llamaImage.Value)) {
    $buildScript = Join-Path $scriptDir "build_llama_image.ps1"
    Fail "local_image_missing" "Local image '$($llamaImage.Value)' not found. Build it first: powershell -ExecutionPolicy Bypass -File $buildScript"
  }
  Write-Info "Local image present: $($llamaImage.Value)"
} else {
  if (Test-LocalDockerImage -ImageRef $llamaImage.Value) {
    Write-Info "Prebuilt image present locally: $($llamaImage.Value)"
  } elseif ($OnlineImageCheck) {
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
    Write-Info "Prebuilt image pullability verified online: $($llamaImage.Value)"
  } else {
    Fail "image_missing" "Image '$($llamaImage.Value)' is not present locally. Pull it while online, or rerun with -OnlineImageCheck to verify registry availability."
  }
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
    Write-Info "NVIDIA GPU probe succeeded (nvidia-smi)."
  } else {
    Write-Info "Docker runtimes: $runtimesJson"
  }
}

$modelRoot = Join-Path $aiboxDir "models\llm\gguf"
$modelPath = Join-Path $modelRoot $modelFile.Value
$shardResult = Test-GgufShardSet -Directory $modelRoot -FileName $modelFile.Value
if (-not $shardResult.Ok) {
  $available = ""
  if (Test-Path $modelRoot) {
    $available = (Get-ChildItem -Path $modelRoot -Filter *.gguf -File | Select-Object -ExpandProperty Name) -join ", "
  }
  Fail $shardResult.Reason "$($shardResult.Message). Available: $available"
}
$shardPlan = Get-GgufShardPlan -FileName $modelFile.Value
if ($shardPlan.IsSharded) {
  Write-Info "Found GGUF model shards: $($shardPlan.Total) shards under $modelRoot (prefix '$($shardPlan.Prefix)')"
} else {
  Write-Info "Found GGUF model file: $modelPath"
}

$embedRel = ([string]$embedModel.Value).TrimStart("/").Replace("/", "\")
$rerankRel = ([string]$rerankModel.Value).TrimStart("/").Replace("/", "\")
Test-RequiredDirectory -Code "embed_model_missing" -Path (Join-Path $aiboxDir $embedRel) -Description "embedding model" -RequireContent -RequiredFiles @('config.json','tokenizer.json')
Test-RequiredDirectory -Code "rerank_model_missing" -Path (Join-Path $aiboxDir $rerankRel) -Description "rerank model" -RequireContent -RequiredFiles @('config.json')

$kiwixDir = Join-Path $aiboxDir "kiwix"
Test-RequiredFile -Code "kiwix_en_missing" -Path (Join-Path $kiwixDir "wikipedia_en_all_mini_2026-03.zim") -Description "English Kiwix ZIM"
Test-RequiredFile -Code "kiwix_es_missing" -Path (Join-Path $kiwixDir "wikipedia_es_all_maxi_2026-02.zim") -Description "Spanish Kiwix ZIM"

Test-DockerVolumeDirectory -VolumeName "kolibri_data_native" -Description "Kolibri data"
$kolibriDir = Join-Path $aiboxDir "kolibri-data"
if (Test-Path -LiteralPath $kolibriDir -PathType Container) {
  # DarkGray soft-info hint; intentionally left as raw Write-Host so the visual
  # weight stays low (lib_log Write-Info uses default foreground).
  Write-Host "[INFO] Bind-mount source still present at $kolibriDir; safe to delete after verifying named-volume contents." -ForegroundColor DarkGray
}

$backendDataDir = Join-Path $aiboxDir "backend-data"
$appdataDir = Join-Path $backendDataDir "appdata"
Test-RequiredDirectory -Code "appdata_missing" -Path $appdataDir -Description "appdata mount"

$retrievalOn = ([string]$retrievalEnabled.Value).Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
if ($retrievalOn) {
  $chromaDir = Join-Path $backendDataDir "chroma_db"
  $chromaEsDir = Join-Path $backendDataDir "chroma_db_es"
  Test-RequiredDirectory -Code "chroma_en_missing" -Path $chromaDir -Description "English Chroma index" -RequireContent
  $chromaDb = Join-Path $chromaDir "chroma.sqlite3"
  Test-RequiredFile -Code "chroma_en_sqlite_missing" -Path $chromaDb -Description "English Chroma SQLite catalog"
  Test-DockerVolumeDirectory -VolumeName "chroma_db_es_native" -Description "Spanish Chroma index" -RequiredFile "chroma.sqlite3"
  if (Test-Path -LiteralPath $chromaEsDir -PathType Container) {
    Write-Info "Found Spanish Chroma bind-mount source for repopulation: $chromaEsDir"
  } else {
    Write-Warn "Spanish Chroma bind-mount source is absent: $chromaEsDir. Runtime uses chroma_db_es_native."
  }
} else {
  Write-Warn "Retrieval is disabled by RETRIEVAL_ENABLED_DEFAULT; Chroma index validation skipped."
}

Write-Ok "Preflight passed."
exit 0



