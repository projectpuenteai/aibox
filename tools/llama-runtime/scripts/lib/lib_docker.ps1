function Test-LocalDockerImage {
    param([Parameter(Mandatory=$true)][string]$ImageRef)

    & docker image inspect $ImageRef 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return $true
    }

    if ($ImageRef -match "@sha256:([a-fA-F0-9]{64})$") {
        $digest = $Matches[1].ToLowerInvariant()
        $output = & docker image ls --digests --no-trunc --format "{{.Repository}}:{{.Tag}} {{.Digest}}" 2>$null
        if ($LASTEXITCODE -eq 0 -and $output) {
            $joined = ($output -join "`n").ToLowerInvariant()
            if ($joined.Contains("sha256:$digest")) {
                return $true
            }
        }
    }

    return $false
}

function Test-DockerPruneArgs {
    param(
        [Parameter(Mandatory=$true)]
        [AllowEmptyCollection()]
        [string[]]$ArgList
    )
    if ($null -eq $ArgList -or $ArgList.Count -lt 2) { return $false }
    $allowed = @(
        @('container','prune'),
        @('image','prune'),
        @('builder','prune'),
        @('network','prune')
    )
    $firstTwo = @($ArgList[0], $ArgList[1])
    $allowedMatch = $false
    foreach ($combo in $allowed) {
        if ($firstTwo[0] -eq $combo[0] -and $firstTwo[1] -eq $combo[1]) {
            $allowedMatch = $true
            break
        }
    }
    if (-not $allowedMatch) { return $false }
    foreach ($arg in $ArgList) {
        if ($arg -in @('-a','--all','--volumes')) { return $false }
    }
    return $true
}
