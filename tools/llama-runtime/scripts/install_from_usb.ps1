# Install the offline AIBox content from a USB payload onto THIS (target) machine.
#
# Expects the AIBox code tree to already be present here (this script lives
# inside it). It copies the large binaries the repo does not track - model
# weights, both Wikipedia ZIMs, and the Spanish Chroma index - from the USB
# into the repo's aibox/ tree, verifying every file against the SHA256 manifest,
# then hands off to up_stack.ps1 to finish the install.
#
# up_stack.ps1 (reused, not duplicated here) generates stack/.env, pre-creates the
# external named volumes, populates chroma_db_es_native from the
# backend-data/chroma_db_es we just placed, performs `docker compose pull` (needs
# internet), runs preflight, and starts the stack. Kolibri channels are then
# imported over the network by import_kolibri_channels.ps1 (the USB carries no
# Kolibri content).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\install_from_usb.ps1
#   powershell -ExecutionPolicy Bypass -File .\install_from_usb.ps1 -UsbPath E:\
#   powershell -ExecutionPolicy Bypass -File .\install_from_usb.ps1 -SkipStart
param(
    # USB drive root or AIBox-Payload path. Auto-detected from removable drives
    # when omitted.
    [string]$UsbPath = '',
    # Copy + verify only; do not run up_stack.ps1 (no .env, no volume populate,
    # no stack start). Finish later with up_stack.ps1.
    [switch]$SkipStart,
    # Skip SHA256 verification of copied files (faster, less safe).
    [switch]$SkipVerify,
    # Forwarded to up_stack.ps1 - skip the Mobile Hotspot bring-up (avoids UAC).
    [switch]$SkipHotspot,
    # Re-copy files even when a same-size copy already exists at the destination.
    [switch]$Force,
    # Skip the post-install Kolibri channel import. Use on offline targets, or to
    # defer the large content download and run import_kolibri_channels.ps1 later.
    [switch]$SkipKolibriImport
)

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')
. (Join-Path $PSScriptRoot 'lib\lib_usb.ps1')

$ErrorActionPreference = 'Stop'

# -- Resolve repo paths from $PSScriptRoot -------------------------------------
$runtimeDir = Split-Path -Parent $PSScriptRoot   # llama-runtime
$toolsDir = Split-Path -Parent $runtimeDir       # tools
$aiboxDir = Split-Path -Parent $toolsDir         # aibox
Write-Info "Repo aibox dir: $aiboxDir"

# -- Locate the USB payload + manifest -----------------------------------------
$payloadRoot = Resolve-UsbPayloadRoot -UsbPath $UsbPath
if (-not $payloadRoot) {
    if ($UsbPath) {
        throw "No AIBox-Payload\manifest.json found at '$UsbPath'. Point -UsbPath at the USB drive root or the AIBox-Payload folder."
    }
    throw "Could not auto-detect an AIBox USB payload on any drive. Insert the drive built by build_usb.ps1, or pass -UsbPath <drive-or-folder>."
}
Write-Info "USB payload: $payloadRoot"

$manifest = Read-UsbManifest -Path (Join-Path $payloadRoot 'manifest.json')
if (-not $manifest) {
    throw "manifest.json could not be read under $payloadRoot."
}
$files = @($manifest.Files)
if ($files.Count -eq 0) {
    throw "manifest.json lists no files - the payload looks empty or corrupt."
}
Write-Info "Manifest: schema=$($manifest.Schema) built_at=$($manifest.BuiltAt) source=$($manifest.SourceHost) files=$($files.Count)"

if ($manifest.Schema -ne 'aibox-usb/1') {
    throw "Unsupported manifest schema '$($manifest.Schema)' - refusing. Expected 'aibox-usb/1'. Rebuild the USB with build_usb.ps1."
}
if ($manifest.FileCount -gt 0 -and $manifest.FileCount -ne $files.Count) {
    throw "Manifest file_count ($($manifest.FileCount)) does not match the $($files.Count) entries present - the manifest looks tampered or truncated."
}

# -- Validate every entry, then confirm presence, BEFORE any copy --------------
# The manifest is untrusted input. A tampered payload must not be able to
# redirect a copy outside the content tree (e.g. overwrite up_stack.ps1, which
# the installer executes at handoff). Test-UsbManifestEntry enforces an
# allowlist + canonical containment and returns the safe absolute paths we use;
# any bad entry fails the whole install closed before a single byte is copied.
$plan = New-Object System.Collections.Generic.List[object]
$totalBytes = 0
foreach ($f in $files) {
    $v = Test-UsbManifestEntry -Entry $f -PayloadRoot $payloadRoot -AiboxDir $aiboxDir
    if (-not $v.Ok) {
        throw "Refusing to install: manifest entry (path='$($f.path)' dest='$($f.dest)') failed validation (reason=$($v.Reason)). The payload may be tampered or corrupt."
    }
    if (-not (Test-Path -LiteralPath $v.SourcePath -PathType Leaf)) {
        throw "USB payload is incomplete - missing file: $($f.path). Re-run build_usb.ps1 on the source machine."
    }
    $sourceSize = [long](Get-Item -LiteralPath $v.SourcePath).Length
    if ($sourceSize -ne [long]$f.size_bytes) {
        throw "USB payload is corrupt - size mismatch for $($f.path) (manifest=$($f.size_bytes), actual=$sourceSize). Re-run build_usb.ps1 on the source machine."
    }
    $plan.Add([pscustomobject]@{
            Dest      = [string]$f.dest
            Sha256    = [string]$f.sha256
            SizeBytes = [long]$f.size_bytes
            Source    = $v.SourcePath
            Target    = $v.DestPath
        })
    $totalBytes += [long]$f.size_bytes
}
$totalGiB = [math]::Round($totalBytes / 1GB, 2)

try {
    $qualifier = (Split-Path -Qualifier $aiboxDir)
    $free = (New-Object System.IO.DriveInfo ($qualifier + '\')).AvailableFreeSpace
    $freeGiB = [math]::Round($free / 1GB, 2)
    Write-Info "Target drive $qualifier free: $freeGiB GiB; payload: $totalGiB GiB."
    if ($free -lt $totalBytes) {
        throw "Not enough free space on $qualifier ($freeGiB GiB free, need ~$totalGiB GiB for the copied files; the Chroma volume needs roughly that much again)."
    }
}
catch [System.IO.IOException] { throw }
catch { Write-Warn "Could not determine free space ($($_.Exception.Message)); continuing." }

# -- Copy + verify (using only validated paths) --------------------------------
$index = 0
$copied = 0
$skipped = 0
$verified = 0
$total = $plan.Count

foreach ($p in $plan) {
    $index++
    $sizeGiB = [math]::Round($p.SizeBytes / 1GB, 2)

    $destParent = Split-Path -Parent $p.Target
    if (-not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }

    $alreadyGood = $false
    if (-not $Force -and (Test-Path -LiteralPath $p.Target -PathType Leaf)) {
        if ((Get-Item -LiteralPath $p.Target).Length -eq $p.SizeBytes) {
            $alreadyGood = $true
        }
    }

    if ($alreadyGood) {
        Write-Info "[$index/$total] present  $($p.Dest)  ($sizeGiB GiB)"
        $skipped++
    }
    else {
        Write-Run "[$index/$total] copy  $($p.Dest)  ($sizeGiB GiB)"
        Copy-Item -LiteralPath $p.Source -Destination $p.Target -Force
        $copied++
    }

    if (-not $SkipVerify) {
        $check = Test-FileSha256 -Path $p.Target -Expected $p.Sha256
        if (-not $check.Ok) {
            throw "Integrity check FAILED for $($p.Dest) (reason=$($check.Reason); expected $($check.Expected), got $($check.Actual)). The copy is corrupt - re-run, or re-build the USB."
        }
        $verified++
    }
}

Write-Ok "Content placed into $aiboxDir (copied $copied, present $skipped, verified $verified of $total)."

# -- Hand off ------------------------------------------------------------------
if ($SkipStart) {
    Write-Host ""
    Write-Info "-SkipStart set: files are in place and verified, but the stack was not started."
    Write-Info "Finish the install with:"
    Write-Info "  powershell -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'up_stack.ps1')`""
    Write-Info "That step generates stack/.env, populates the chroma_db_es_native volume"
    Write-Info "from backend-data/chroma_db_es, pulls the container images, and starts the stack."
    return
}

$upStack = Join-Path $PSScriptRoot 'up_stack.ps1'
if (-not (Test-Path -LiteralPath $upStack -PathType Leaf)) {
    throw "up_stack.ps1 not found next to this script ($upStack)."
}

Write-Host ""
Write-Info "Handing off to up_stack.ps1 (generates .env, populates the Chroma volume,"
Write-Info "pulls images over the internet, and starts the stack)..."
$upArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $upStack)
if ($SkipHotspot) { $upArgs += '-SkipHotspot' }
Write-Run "powershell $($upArgs -join ' ')"
& powershell @upArgs
if ($LASTEXITCODE -ne 0) {
    throw "up_stack.ps1 failed (exit $LASTEXITCODE). The content is installed; re-run up_stack.ps1 after resolving the error."
}

Write-Ok "Stack is up."

# -- Kolibri channels (content-only scope: not on the USB) ---------------------
$importScript = Join-Path $PSScriptRoot 'import_kolibri_channels.ps1'
if ($SkipKolibriImport) {
    Write-Info "Skipping Kolibri channel import (-SkipKolibriImport). Run it later with:"
    Write-Info "  powershell -ExecutionPolicy Bypass -File `"$importScript`""
} elseif (-not (Test-Path -LiteralPath $importScript -PathType Leaf)) {
    Write-Warn "import_kolibri_channels.ps1 not found next to this script; skipping Kolibri import."
} else {
    Write-Info "Importing the curated Kolibri channels over the network (large; resumable)..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $importScript
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "One or more Kolibri channels did not import. Re-run after resolving:"
        Write-Warn "  powershell -ExecutionPolicy Bypass -File `"$importScript`""
    }
}

Write-Ok "USB install complete."
