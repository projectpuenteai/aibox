# ICS DNS proxy registry helpers. Windows Internet Connection Sharing runs a
# built-in DNS proxy on its hotspot interface (typically 192.168.137.1:53).
# That proxy only consults the local hosts file for queries from the host
# itself, so connected clients can't resolve puente.link through it. To run
# our own DNS responder on port 53, we have to release ICS's DNS proxy first
# by setting EnableProxy=0 in the SharedAccess service parameters and
# restarting the service. DHCP and NAT continue to work; only the DNS proxy
# stops.

$script:IcsDnsRegPath  = 'HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters'
$script:IcsDnsRegValue = 'EnableProxy'
$script:IcsServiceName = 'SharedAccess'

function Test-IcsDnsProxyEnabled {
    # Returns $true if ICS's DNS proxy is enabled (or the value is absent, which
    # is ICS's default). Returns $false only if EnableProxy is explicitly 0.
    param(
        [string]$RegPath = $script:IcsDnsRegPath
    )
    $prop = Get-ItemProperty -Path $RegPath -Name $script:IcsDnsRegValue -ErrorAction SilentlyContinue
    if ($null -eq $prop) { return $true }
    return ([int]$prop.($script:IcsDnsRegValue) -ne 0)
}

function Set-IcsDnsProxyValue {
    # Writes EnableProxy=$Value (DWORD) and restarts SharedAccess so the change
    # takes effect (the listener is rebound on service start).
    param(
        [Parameter(Mandatory=$true)][int]$Value,
        [string]$RegPath     = $script:IcsDnsRegPath,
        [string]$ServiceName = $script:IcsServiceName,
        [switch]$NoServiceRestart
    )
    if (-not (Test-Path $RegPath)) {
        throw "ICS registry path not found: $RegPath. Is Internet Connection Sharing installed?"
    }
    New-ItemProperty -Path $RegPath -Name $script:IcsDnsRegValue -Value $Value -PropertyType DWord -Force | Out-Null
    if (-not $NoServiceRestart) {
        Restart-Service -Name $ServiceName -Force -ErrorAction Stop
    }
}

function Disable-IcsDnsProxy {
    # Disables ICS's built-in DNS proxy and returns the previous state ($true
    # if it was enabled, $false if it was already disabled). Callers should
    # save the return value so Enable-IcsDnsProxy can be called on the way
    # down only if we were the ones that disabled it.
    param(
        [string]$RegPath     = $script:IcsDnsRegPath,
        [string]$ServiceName = $script:IcsServiceName,
        [switch]$NoServiceRestart
    )
    $previous = Test-IcsDnsProxyEnabled -RegPath $RegPath
    Set-IcsDnsProxyValue -Value 0 -RegPath $RegPath -ServiceName $ServiceName -NoServiceRestart:$NoServiceRestart
    return $previous
}

function Enable-IcsDnsProxy {
    # Re-enables ICS's built-in DNS proxy (the default behavior).
    param(
        [string]$RegPath     = $script:IcsDnsRegPath,
        [string]$ServiceName = $script:IcsServiceName,
        [switch]$NoServiceRestart
    )
    Set-IcsDnsProxyValue -Value 1 -RegPath $RegPath -ServiceName $ServiceName -NoServiceRestart:$NoServiceRestart
}
