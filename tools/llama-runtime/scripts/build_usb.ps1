# Assemble the offline AIBox USB payload from THIS dev machine.
#
# Copies the curated model files, both Wikipedia ZIMs, and the Spanish Chroma
# index (the backend-data/chroma_db_es bind-mount source) into
#   <UsbRoot>\AIBox-Payload\content\...
# and emits a SHA256 catalog at <UsbRoot>\AIBox-Payload\manifest.json.
#
# Docker images and Kolibri content are deliberately NOT packaged - under the
# content-only scope they come from the internet on the target machine
# (docker compose pull + kolibri importchannel network), driven by up_stack.ps1.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_usb.ps1 -UsbRoot E:\
#   powershell -ExecutionPolicy Bypass -File .\build_usb.ps1 -UsbRoot D:\staging -VerifyCopy
#
# Resumable: a file already present on the USB with a matching size and SHA256
# is not re-copied unless -Force is given.
param(
    [Parameter(Mandatory = $true)][string]$UsbRoot,
    # Re-copy files even when a same-size copy already exists on the USB.
    [switch]$Force,
    # After copying each file, re-hash the USB copy and compare to the source
    # (catches a bad write at packaging time instead of at install time).
    [switch]$VerifyCopy,
    # Skip the ~32 GB Spanish Chroma index (models + ZIMs only).
    [switch]$NoChromaIndex
)

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_usb.ps1')

$ErrorActionPreference = 'Stop'

# -- Resolve repo paths from $PSScriptRoot -------------------------------------
$runtimeDir = Split-Path -Parent $PSScriptRoot   # llama-runtime
$toolsDir = Split-Path -Parent $runtimeDir       # tools
$aiboxDir = Split-Path -Parent $toolsDir         # aibox

Write-Info "Repo aibox dir: $aiboxDir"

# -- Prepare the USB payload directory -----------------------------------------
if (-not (Test-Path -LiteralPath $UsbRoot -PathType Container)) {
    throw "UsbRoot not found or not a directory: $UsbRoot"
}
$UsbRootFull = (Resolve-Path -LiteralPath $UsbRoot).Path
$payloadRoot = Join-Path $UsbRootFull 'AIBox-Payload'
$contentRoot = Join-Path $payloadRoot 'content'
New-Item -ItemType Directory -Path $contentRoot -Force | Out-Null
Write-Info "Payload root: $payloadRoot"

# -- Resolve the source file set -----------------------------------------------
$spec = Get-UsbContentSpec
if ($NoChromaIndex) {
    $spec = @($spec | Where-Object { $_.RepoRel -ne 'backend-data/chroma_db_es' })
    Write-Warn "Skipping the Spanish Chroma index (-NoChromaIndex). The target stack will have no RAG index until you provision chroma_db_es separately."
}

$sourceFiles = Resolve-UsbSourceFiles -AiboxDir $aiboxDir -Spec $spec

$missing = @($sourceFiles | Where-Object { -not $_.Exists })
if ($missing.Count -gt 0) {
    Write-Err "These required source files are missing under $aiboxDir :"
    foreach ($m in $missing) { Write-Err "  - $($m.RepoRel)" }
    throw "Cannot build USB payload: $($missing.Count) source file(s) missing. Resolve the gaps (or re-run on a fully provisioned machine) and try again."
}

# Guard: the Chroma tree must have actually expanded to files when requested.
if (-not $NoChromaIndex) {
    $chromaFiles = @($sourceFiles | Where-Object { $_.RepoRel -like 'backend-data/chroma_db_es/*' })
    if ($chromaFiles.Count -eq 0) {
        throw "backend-data/chroma_db_es is empty or missing under $aiboxDir - the Spanish Chroma index cannot be packaged. Use -NoChromaIndex to build without it, or restore the index first."
    }
}

$present = @($sourceFiles | Where-Object { $_.Exists })
$totalBytes = ($present | Measure-Object -Property SizeBytes -Sum).Sum
if ($null -eq $totalBytes) { $totalBytes = 0 }
$totalGiB = [math]::Round($totalBytes / 1GB, 2)
Write-Info "Source set: $($present.Count) files, $totalGiB GiB."

# -- Free-space check ----------------------------------------------------------
try {
    $qualifier = (Split-Path -Qualifier $UsbRootFull)            # e.g. "E:"
    $driveInfo = New-Object System.IO.DriveInfo ($qualifier + '\')
    $free = $driveInfo.AvailableFreeSpace
    $freeGiB = [math]::Round($free / 1GB, 2)
    Write-Info "Target drive free space: $freeGiB GiB."
    if ($free -lt $totalBytes) {
        throw "Not enough free space on $qualifier ($freeGiB GiB free, need ~$totalGiB GiB). Use a larger drive."
    }
}
catch [System.IO.IOException] { throw }
catch {
    Write-Warn "Could not determine free space for $UsbRootFull ($($_.Exception.Message)); continuing without the check."
}

# -- Copy + hash ---------------------------------------------------------------
$manifestFiles = New-Object System.Collections.Generic.List[object]
$copied = 0
$skipped = 0
$index = 0

foreach ($src in $present) {
    $index++
    $native = $src.RepoRel -replace '/', '\'
    $destPath = Join-Path $contentRoot $native
    $usbRel = 'content/' + $src.RepoRel
    $sizeGiB = [math]::Round($src.SizeBytes / 1GB, 2)

    $destParent = Split-Path -Parent $destPath
    if (-not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }

    $sha = Get-FileSha256 -Path $src.FullPath
    $alreadyGood = $false
    if (-not $Force -and (Test-Path -LiteralPath $destPath -PathType Leaf)) {
        if ((Get-Item -LiteralPath $destPath).Length -eq $src.SizeBytes) {
            $destCheck = Test-FileSha256 -Path $destPath -Expected $sha
            if ($destCheck.Ok) {
                $alreadyGood = $true
            }
            else {
                Write-Warn "[$index/$($present.Count)] present but hash mismatch; re-copying $($src.RepoRel)"
            }
        }
    }

    if ($alreadyGood) {
        Write-Info "[$index/$($present.Count)] skip (verified present)  $($src.RepoRel)  ($sizeGiB GiB)"
        $skipped++
    }
    else {
        Write-Run "[$index/$($present.Count)] copy  $($src.RepoRel)  ($sizeGiB GiB)"
        Copy-Item -LiteralPath $src.FullPath -Destination $destPath -Force
        $copied++
    }

    if ($VerifyCopy) {
        $check = Test-FileSha256 -Path $destPath -Expected $sha
        if (-not $check.Ok) {
            throw "Copy verification failed for $($src.RepoRel) (reason=$($check.Reason)). Re-run with -Force to recopy."
        }
    }

    $manifestFiles.Add([pscustomobject]@{
            path       = $usbRel
            dest       = $src.RepoRel
            size_bytes = $src.SizeBytes
            sha256     = $sha
        })
}

# -- Write the manifest --------------------------------------------------------
$manifestPath = Join-Path $payloadRoot 'manifest.json'
New-UsbManifest -Files $manifestFiles.ToArray() -OutFile $manifestPath -SourceHost $env:COMPUTERNAME

Write-Ok "USB payload built at $payloadRoot"
Write-Info "  files: $($manifestFiles.Count)  (copied $copied, skipped $skipped)"
Write-Info "  total: $totalGiB GiB"
Write-Info "  manifest: $manifestPath"
Write-Host ""
Write-Info "Next: carry the drive to the target machine (with the AIBox code tree"
Write-Info "already present) and run install_from_usb.ps1 there."
