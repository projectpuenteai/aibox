# Pure (no-side-effect) helpers for model-directory and GGUF shard integrity.
# These helpers do NOT call exit/throw — they return a result object so the
# caller (e.g. preflight_llama_runtime.ps1) can map them onto its own Fail.
#
# Each helper returns a PSCustomObject with:
#   Ok      : [bool]  true when the check passed
#   Reason  : [string] machine-readable reason code on failure (empty on ok)
#   Message : [string] human-readable detail (empty on ok)

function Test-ModelDirectoryIntegrity {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [switch]$RequireContent,
        [string[]]$RequiredFiles = @()
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return [pscustomobject]@{
            Ok      = $false
            Reason  = 'directory_missing'
            Message = "Directory not found: $Path"
        }
    }

    if ($RequireContent) {
        $first = Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $first) {
            return [pscustomobject]@{
                Ok      = $false
                Reason  = 'directory_empty'
                Message = "Directory is empty: $Path"
            }
        }
    }

    if ($RequiredFiles) {
        foreach ($name in $RequiredFiles) {
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            $candidate = Join-Path $Path $name
            if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                return [pscustomobject]@{
                    Ok      = $false
                    Reason  = 'required_file_missing'
                    Message = "Directory $Path is missing required file $name"
                }
            }
        }
    }

    return [pscustomobject]@{
        Ok      = $true
        Reason  = ''
        Message = ''
    }
}

function Get-GgufShardPlan {
    # Parse a configured GGUF model filename. If it matches the multi-shard
    # naming pattern '<prefix>-<index>-of-<total>.gguf' (case-insensitive),
    # return @{ IsSharded = $true; Prefix = '...'; Total = N }. Otherwise
    # return @{ IsSharded = $false }.
    param([Parameter(Mandatory=$true)][string]$FileName)

    if ($FileName -match '^(?<prefix>.+)-(?<idx>\d{5})-of-(?<total>\d{5})\.gguf$') {
        $total = [int]$Matches['total']
        return [pscustomobject]@{
            IsSharded = $true
            Prefix    = $Matches['prefix']
            Total     = $total
        }
    }
    return [pscustomobject]@{
        IsSharded = $false
        Prefix    = ''
        Total     = 0
    }
}

function Test-GgufShardSet {
    param(
        [Parameter(Mandatory=$true)][string]$Directory,
        [Parameter(Mandatory=$true)][string]$FileName
    )

    $plan = Get-GgufShardPlan -FileName $FileName

    if (-not $plan.IsSharded) {
        $path = Join-Path $Directory $FileName
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            return [pscustomobject]@{ Ok = $true; Reason = ''; Message = '' }
        }
        return [pscustomobject]@{
            Ok      = $false
            Reason  = 'model_missing'
            Message = "Model file not found: $path"
        }
    }

    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return [pscustomobject]@{
            Ok      = $false
            Reason  = 'model_dir_missing'
            Message = "Model directory not found: $Directory"
        }
    }

    $globPattern = "{0}-*-of-*.gguf" -f $plan.Prefix
    $found = @(Get-ChildItem -LiteralPath $Directory -Filter $globPattern -File -ErrorAction SilentlyContinue)
    $actual = $found.Count
    if ($actual -ne $plan.Total) {
        return [pscustomobject]@{
            Ok      = $false
            Reason  = 'model_shard_mismatch'
            Message = "GGUF shard count mismatch in $Directory for prefix '$($plan.Prefix)': expected $($plan.Total), found $actual"
        }
    }

    return [pscustomobject]@{ Ok = $true; Reason = ''; Message = '' }
}
