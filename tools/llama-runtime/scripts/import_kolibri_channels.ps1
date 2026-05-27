# Import the curated Kolibri channels over the network on THIS machine.
#
# The USB payload is content-only and does NOT carry Kolibri content (it would
# overflow the drive), so Kolibri channels are reproduced by importing them from
# Kolibri Studio at install time. This script is the single source of truth for
# WHICH channels a deployment should have. It runs, per channel:
#
#   /kolibri manage importchannel network <id>   # channel DB / metadata
#   /kolibri manage importcontent network <id>   # the actual resource files (large)
#
# (treehouses/kolibri ships the kolibri binary at /kolibri, not on $PATH.)
#
# Both are needed to reproduce the lessons: importchannel alone only fetches the
# channel structure, not the videos/exercises/documents. importcontent is
# resumable and skips files already present, so this script is safe to re-run.
#
# Requires: the stack already up (the kolibri container must be running) and
# internet access on this machine. Each channel must still be published on
# Kolibri Studio - import fails for any channel that has been unpublished.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\import_kolibri_channels.ps1
#   powershell -ExecutionPolicy Bypass -File .\import_kolibri_channels.ps1 -MetadataOnly
#   powershell -ExecutionPolicy Bypass -File .\import_kolibri_channels.ps1 -Channels c1f2b7e6ac9f56a2bb44fa7a48b66dce
param(
    # Path to docker-compose.yaml. Auto-resolved from $PSScriptRoot when omitted.
    [string]$ComposeFile = '',
    # Import only these channel IDs (subset of the manifest below). Default: all.
    [string[]]$Channels = @(),
    # Fetch channel metadata only (importchannel); skip the large importcontent.
    # Useful to verify Studio reachability without downloading gigabytes.
    [switch]$MetadataOnly
)

. (Join-Path $PSScriptRoot 'lib\lib_log.ps1')

$ErrorActionPreference = 'Stop'

# -- Channel manifest: the curated set this deployment ships -------------------
# Captured 2026-05-26 from the live kolibri_data_native volume (7 channels, all
# content available on disk). Keep this list in sync with the content actually
# imported on the reference machine. Sizes are approximate resource counts.
$ChannelManifest = @(
    [pscustomobject]@{ Id = 'da53f90b1be25752a04682bbc353659f'; Name = 'Ciencia NASA';                   Resources = 1404 }
    [pscustomobject]@{ Id = '07cd1633691b4473b6fda08caf826253'; Name = 'Ciensacion';                      Resources = 101  }
    [pscustomobject]@{ Id = 'c1f2b7e6ac9f56a2bb44fa7a48b66dce'; Name = 'Khan Academy (Espanol)';          Resources = 8421 }
    [pscustomobject]@{ Id = 'f446655247a95c0aa94ca9fa4d66783b'; Name = 'Proyecto Biosfera';               Resources = 645  }
    [pscustomobject]@{ Id = 'c4ad70f67dff57738591086e466f9afc'; Name = 'Proyecto Descartes';              Resources = 1065 }
    [pscustomobject]@{ Id = '8fa678af1dd05329bf3218c549b84996'; Name = 'Simulaciones interactivas PhET';  Resources = 203  }
    [pscustomobject]@{ Id = 'b06dd546e8ba4b44bf921862c9948ffe'; Name = 'WiiXii';                           Resources = 191  }
)

# -- Resolve the compose file from $PSScriptRoot -------------------------------
if (-not $ComposeFile) {
    $runtimeDir = Split-Path -Parent $PSScriptRoot   # llama-runtime
    $toolsDir = Split-Path -Parent $runtimeDir       # tools
    $aiboxDir = Split-Path -Parent $toolsDir         # aibox
    $ComposeFile = Join-Path $aiboxDir 'stack\docker-compose.yaml'
}
if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) {
    throw "docker-compose.yaml not found at '$ComposeFile'. Pass -ComposeFile explicitly."
}
Write-Info "Compose file: $ComposeFile"

# -- Confirm the kolibri container is running ----------------------------------
$kolibriState = (& docker compose -f $ComposeFile ps --format '{{.Service}} {{.State}}' 2>$null | Select-String -SimpleMatch 'kolibri running')
if (-not $kolibriState) {
    throw "The 'kolibri' service is not running. Start the stack first (up_stack.ps1), then re-run this script."
}

# -- Select the channels to import ---------------------------------------------
$targets = if ($Channels.Count -gt 0) {
    $ChannelManifest | Where-Object { $Channels -contains $_.Id }
} else {
    $ChannelManifest
}
if (-not $targets) {
    throw "No matching channels. -Channels must be IDs from the manifest: $($ChannelManifest.Id -join ', ')"
}

Write-Info "Importing $(@($targets).Count) Kolibri channel(s) over the network$(if ($MetadataOnly) { ' (metadata only)' })."
if (-not $MetadataOnly) {
    Write-Warn "importcontent downloads the full resource files (tens of GB total) and may take a long time. It is resumable - re-run if interrupted."
}

# -- Import loop ---------------------------------------------------------------
$results = New-Object System.Collections.Generic.List[object]
foreach ($ch in $targets) {
    Write-Info "-- $($ch.Name)  [$($ch.Id)] --"
    $metaOk = $false
    $contentOk = $null

    Write-Run "docker compose exec -T kolibri /kolibri manage importchannel network $($ch.Id)"
    & docker compose -f $ComposeFile exec -T kolibri /kolibri manage importchannel network $ch.Id
    $metaOk = ($LASTEXITCODE -eq 0)
    if (-not $metaOk) {
        Write-Err "importchannel failed for $($ch.Name) (exit $LASTEXITCODE) - channel may be unpublished on Studio."
    } elseif (-not $MetadataOnly) {
        Write-Run "docker compose exec -T kolibri /kolibri manage importcontent network $($ch.Id)"
        & docker compose -f $ComposeFile exec -T kolibri /kolibri manage importcontent network $ch.Id
        $contentOk = ($LASTEXITCODE -eq 0)
        if (-not $contentOk) {
            Write-Err "importcontent failed for $($ch.Name) (exit $LASTEXITCODE) - re-run to resume."
        } else {
            Write-Ok "Imported content for $($ch.Name)."
        }
    } else {
        Write-Ok "Fetched metadata for $($ch.Name)."
    }

    $results.Add([pscustomobject]@{ Name = $ch.Name; Id = $ch.Id; Metadata = $metaOk; Content = $contentOk })
}

# -- Summary -------------------------------------------------------------------
Write-Info "-- Import summary --"
$failed = 0
foreach ($r in $results) {
    $contentTag = if ($MetadataOnly) { 'skipped' } elseif ($r.Content) { 'ok' } elseif ($null -eq $r.Content) { 'n/a' } else { 'FAIL' }
    $metaTag = if ($r.Metadata) { 'ok' } else { 'FAIL' }
    if ((-not $r.Metadata) -or ((-not $MetadataOnly) -and ($r.Content -eq $false))) { $failed++ }
    Write-Info ("  {0,-34} metadata={1,-4} content={2}" -f $r.Name, $metaTag, $contentTag)
}
if ($failed -gt 0) {
    Write-Warn "$failed channel(s) had failures. Fix Studio availability / network and re-run (imports resume)."
    exit 1
}
Write-Ok "All $(@($targets).Count) channel(s) imported successfully."
