# Helpers shared by up_stack.ps1 and down_stack.ps1 for managing the Puente
# DNS responder process and persisting the prior ICS DNS proxy state across
# the up→down lifecycle.

function Test-PuenteResponderProcess {
    # Returns $true only if the given PID belongs to a live powershell.exe
    # whose command line references puente_dns_responder.ps1. Guards against
    # killing an unrelated process whose PID happens to match a stale entry.
    param(
        [Parameter(Mandatory=$true)][int]$ProcessId
    )
    try {
        $p = Get-Process -Id $ProcessId -ErrorAction Stop
    } catch {
        return $false
    }
    if ($p.ProcessName -notmatch '^(powershell|pwsh)$') { return $false }
    try {
        $cim = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    } catch {
        return $false
    }
    if (-not $cim) { return $false }
    return ([string]$cim.CommandLine -match 'puente_dns_responder\.ps1')
}

function Stop-PuenteResponderByPid {
    # Safely stops a process only after confirming it is the Puente responder.
    # Returns $true if the PID was killed, $false if it was skipped (either
    # because the PID doesn't exist or doesn't belong to us).
    param(
        [Parameter(Mandatory=$true)][int]$ProcessId
    )
    if (-not (Test-PuenteResponderProcess -ProcessId $ProcessId)) { return $false }
    try { Stop-Process -Id $ProcessId -Force -ErrorAction Stop; return $true }
    catch { return $false }
}

function Save-IcsDnsPriorState {
    # Persists the prior ICS DNS proxy state to a small JSON sidecar so
    # down_stack.ps1 can decide whether (and how) to restore it.
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][bool]$PrevEnabled,
        [bool]$WeToggled = $true
    )
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $payload = [ordered]@{
        prev_enabled = $PrevEnabled
        we_toggled   = $WeToggled
        timestamp    = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
    } | ConvertTo-Json -Compress
    Set-Content -LiteralPath $Path -Value $payload -Encoding UTF8
}

function Read-IcsDnsPriorState {
    # Returns @{ prev_enabled = <bool>; we_toggled = <bool> } or $null if no
    # sidecar exists / it can't be parsed.
    param(
        [Parameter(Mandatory=$true)][string]$Path
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        return @{
            prev_enabled = [bool]$obj.prev_enabled
            we_toggled   = [bool]$obj.we_toggled
        }
    } catch { return $null }
}

function Test-PuenteResponderAnswering {
    # Sends a fresh A query for $Domain to ${HostIp}:53 and verifies that the
    # answer points back at $ExpectedIp. Skips the Windows DNS Client cache by
    # clearing it first. Returns $true on a clean match, $false otherwise.
    param(
        [Parameter(Mandatory=$true)][string]$HostIp,
        [Parameter(Mandatory=$true)][string]$Domain,
        [Parameter(Mandatory=$true)][string]$ExpectedIp,
        [int]$TimeoutSec = 2
    )
    try { Clear-DnsClientCache -ErrorAction SilentlyContinue } catch {}
    try {
        $r = Resolve-DnsName -Server $HostIp -Name $Domain -Type A `
                             -NoHostsFile -DnsOnly `
                             -ErrorAction Stop
        if (-not $r) { return $false }
        return [bool](@($r) | Where-Object { $_.IPAddress -eq $ExpectedIp })
    } catch {
        return $false
    }
}

function Wait-PuenteResponderAnswering {
    # Polls Test-PuenteResponderAnswering up to $TimeoutSec, returning $true
    # on the first success. The fixed 1-second sleep on first boot can be too
    # short for a hidden powershell.exe to load; polling is robust to that.
    param(
        [Parameter(Mandatory=$true)][string]$HostIp,
        [Parameter(Mandatory=$true)][string]$Domain,
        [Parameter(Mandatory=$true)][string]$ExpectedIp,
        [int]$TimeoutSec     = 6,
        [int]$IntervalMs     = 250
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-PuenteResponderAnswering -HostIp $HostIp -Domain $Domain -ExpectedIp $ExpectedIp) {
            return $true
        }
        Start-Sleep -Milliseconds $IntervalMs
    }
    return $false
}
