# Tiny UDP DNS responder for the offline Puente hotspot.
#
# Answers A queries for puente.link (and any subdomain) with the configured
# hotspot host IP, and forwards everything else to an upstream resolver. Runs
# in the foreground as a long-lived process; up_stack.ps1 launches it after
# disabling ICS's built-in DNS proxy so port 53 on 192.168.137.1 is free.

param(
    [string]   $ListenIp    = "192.168.137.1",
    [int]      $ListenPort  = 53,
    [string[]] $Domain      = @("puente.link"),
    [string]   $AnswerIp    = "192.168.137.1",
    [string]   $UpstreamDns = "1.1.1.1",
    [int]      $UpstreamTimeoutMs = 800,
    [string]   $LogFile     = "",
    [switch]   $SelfTest
)

$ErrorActionPreference = "Stop"

# Normalize and validate inputs early so a typo blows up before binding port 53.
$Domain = @($Domain | ForEach-Object { $_.Trim().TrimEnd('.').ToLowerInvariant() } | Where-Object { $_ })
if ($Domain.Count -eq 0) { throw "At least one -Domain must be provided." }

try { $null = [System.Net.IPAddress]::Parse($AnswerIp) } catch {
    throw "Invalid -AnswerIp '$AnswerIp': $($_.Exception.Message)"
}
try { $null = [System.Net.IPAddress]::Parse($ListenIp) } catch {
    throw "Invalid -ListenIp '$ListenIp': $($_.Exception.Message)"
}

if (-not $LogFile) {
    $defaultLogDir = Join-Path $env:ProgramData "AIBox\logs"
    if (-not (Test-Path $defaultLogDir)) {
        try { New-Item -ItemType Directory -Path $defaultLogDir -Force | Out-Null } catch {}
    }
    $LogFile = Join-Path $defaultLogDir "puente_dns.log"
}

function Write-DnsLog {
    param([string]$Message)
    try {
        $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffK"
        Add-Content -LiteralPath $LogFile -Value "$ts $Message" -Encoding UTF8
    } catch {}
}

# ---- DNS message parsing / building ----------------------------------------

function ConvertFrom-DnsQuery {
    # Parses a DNS query packet just far enough to extract the transaction id,
    # the (lowercased) question name, qtype and qclass. Returns $null for
    # malformed packets.
    param(
        [Parameter(Mandatory=$true)][byte[]]$Bytes
    )
    if ($null -eq $Bytes -or $Bytes.Length -lt 12) { return $null }

    # PowerShell's -shl on a [byte] truncates to 8 bits, so cast to int first.
    $tid     = [uint16]((([int]$Bytes[0]) -shl 8) -bor [int]$Bytes[1])
    $flags   = [uint16](([int]$Bytes[2] -shl 8) -bor [int]$Bytes[3])
    $qdcount = [uint16](([int]$Bytes[4] -shl 8) -bor [int]$Bytes[5])
    if ($qdcount -lt 1) { return $null }

    # Read labels at offset 12. We don't follow compression pointers in the
    # question section (the client should not send any).
    $pos = 12
    $labels = New-Object System.Collections.Generic.List[string]
    while ($true) {
        if ($pos -ge $Bytes.Length) { return $null }
        $len = $Bytes[$pos]
        if ($len -eq 0) { $pos++; break }
        if (($len -band 0xC0) -ne 0) {
            # Pointer in question name -> unsupported.
            return $null
        }
        $pos++
        if ($pos + $len -gt $Bytes.Length) { return $null }
        $label = [System.Text.Encoding]::ASCII.GetString($Bytes, $pos, $len)
        $labels.Add($label.ToLowerInvariant()) | Out-Null
        $pos += $len
    }
    if ($pos + 4 -gt $Bytes.Length) { return $null }
    $qtype  = [uint16](([int]$Bytes[$pos]   -shl 8) -bor [int]$Bytes[$pos + 1])
    $qclass = [uint16](([int]$Bytes[$pos+2] -shl 8) -bor [int]$Bytes[$pos + 3])
    $questionLength = ($pos + 4) - 12

    return [pscustomobject]@{
        TransactionId  = $tid
        Flags          = $flags
        Name           = ($labels -join '.')
        QType          = $qtype
        QClass         = $qclass
        QuestionStart  = 12
        QuestionLength = $questionLength
    }
}

function Test-DomainMatch {
    # Returns $true if $Name == one of $Patterns, or $Name ends with ".<pattern>".
    param(
        [string]  $Name,
        [string[]]$Patterns
    )
    if ([string]::IsNullOrWhiteSpace($Name)) { return $false }
    $n = $Name.Trim().TrimEnd('.').ToLowerInvariant()
    foreach ($p in $Patterns) {
        if ($n -eq $p) { return $true }
        if ($n.EndsWith(".$p")) { return $true }
    }
    return $false
}

function New-DnsAnswerBytes {
    # Builds a DNS response that points $Query at $AnswerIp via an A record.
    # Uses a compression pointer (0xC00C) for the answer name, so it reuses
    # the question name bytes verbatim.
    param(
        [Parameter(Mandatory=$true)] $Query,
        [Parameter(Mandatory=$true)][byte[]]$OriginalBytes,
        [Parameter(Mandatory=$true)][string]$AnswerIp,
        [int]$Ttl = 60
    )

    $resp = New-Object System.Collections.Generic.List[byte]

    # Header: copy tid, flags = QR=1 AA=1 RD=copy RA=1, qdcount=1, ancount=1.
    $rd = ($Query.Flags -band 0x0100)   # preserve client's RD bit
    $flagsOut = [uint16](0x8480 -bor $rd)
    $resp.Add([byte](($Query.TransactionId -shr 8) -band 0xFF)) | Out-Null
    $resp.Add([byte]($Query.TransactionId -band 0xFF))          | Out-Null
    $resp.Add([byte](($flagsOut -shr 8) -band 0xFF))            | Out-Null
    $resp.Add([byte]($flagsOut -band 0xFF))                     | Out-Null
    # QDCOUNT=1
    $resp.Add([byte]0) | Out-Null; $resp.Add([byte]1) | Out-Null
    # ANCOUNT=1
    $resp.Add([byte]0) | Out-Null; $resp.Add([byte]1) | Out-Null
    # NSCOUNT=0
    $resp.Add([byte]0) | Out-Null; $resp.Add([byte]0) | Out-Null
    # ARCOUNT=0
    $resp.Add([byte]0) | Out-Null; $resp.Add([byte]0) | Out-Null

    # Question: copy the original bytes from offset 12 for QuestionLength bytes.
    $questionBytes = New-Object 'byte[]' $Query.QuestionLength
    [System.Array]::Copy($OriginalBytes, 12, $questionBytes, 0, $Query.QuestionLength)
    foreach ($b in $questionBytes) { $resp.Add($b) | Out-Null }

    # Answer record:
    # NAME = pointer to offset 12 -> 0xC00C
    $resp.Add([byte]0xC0) | Out-Null
    $resp.Add([byte]0x0C) | Out-Null
    # TYPE = A (1)
    $resp.Add([byte]0) | Out-Null; $resp.Add([byte]1) | Out-Null
    # CLASS = IN (1)
    $resp.Add([byte]0) | Out-Null; $resp.Add([byte]1) | Out-Null
    # TTL (4 bytes, big-endian)
    $resp.Add([byte](($Ttl -shr 24) -band 0xFF)) | Out-Null
    $resp.Add([byte](($Ttl -shr 16) -band 0xFF)) | Out-Null
    $resp.Add([byte](($Ttl -shr  8) -band 0xFF)) | Out-Null
    $resp.Add([byte]( $Ttl         -band 0xFF)) | Out-Null
    # RDLENGTH = 4
    $resp.Add([byte]0) | Out-Null; $resp.Add([byte]4) | Out-Null
    # RDATA = IPv4 octets
    $ipBytes = [System.Net.IPAddress]::Parse($AnswerIp).GetAddressBytes()
    foreach ($b in $ipBytes) { $resp.Add($b) | Out-Null }

    return $resp.ToArray()
}

function New-DnsServfailBytes {
    # Minimal SERVFAIL response: copy header, set QR=1, RCODE=2, keep question.
    param(
        [Parameter(Mandatory=$true)] $Query,
        [Parameter(Mandatory=$true)][byte[]]$OriginalBytes
    )
    $headerAndQuestion = 12 + $Query.QuestionLength
    $resp = New-Object 'byte[]' $headerAndQuestion
    [System.Array]::Copy($OriginalBytes, 0, $resp, 0, $headerAndQuestion)
    # Set flags: QR=1, OPCODE=copy, AA=0, TC=0, RD=copy, RA=1, RCODE=2.
    $rd = ($Query.Flags -band 0x0100)
    $flagsOut = [uint16](0x8082 -bor $rd)
    $resp[2] = [byte](($flagsOut -shr 8) -band 0xFF)
    $resp[3] = [byte]($flagsOut -band 0xFF)
    # ANCOUNT = 0
    $resp[6] = 0; $resp[7] = 0
    return $resp
}

# ---- Self-test (no socket binding) -----------------------------------------

if ($SelfTest) {
    # Round-trip a hand-built query to validate parser + builder.
    # Query for "puente.link" A:
    #   tid=0x1234 flags=0x0100 qdcount=1 ancount=0 nscount=0 arcount=0
    #   QNAME = 6 puente 4 link 0
    #   QTYPE=1 QCLASS=1
    $bytes = [byte[]]@(
        0x12,0x34, 0x01,0x00, 0x00,0x01, 0x00,0x00, 0x00,0x00, 0x00,0x00,
        6,0x70,0x75,0x65,0x6e,0x74,0x65, 4,0x6c,0x69,0x6e,0x6b, 0,
        0x00,0x01, 0x00,0x01
    )
    $q = ConvertFrom-DnsQuery -Bytes $bytes
    if (-not $q -or $q.Name -ne "puente.link" -or $q.QType -ne 1) {
        Write-Error "SelfTest parser FAILED: parsed=$($q | ConvertTo-Json -Compress)"
        exit 1
    }
    $matches = Test-DomainMatch -Name $q.Name -Patterns @("puente.link")
    if (-not $matches) {
        Write-Error "SelfTest match FAILED for puente.link"
        exit 1
    }
    $ans = New-DnsAnswerBytes -Query $q -OriginalBytes $bytes -AnswerIp "192.168.137.1"
    # Last 4 bytes should be the IP octets.
    if ($ans[$ans.Length-4] -ne 192 -or $ans[$ans.Length-3] -ne 168 -or $ans[$ans.Length-2] -ne 137 -or $ans[$ans.Length-1] -ne 1) {
        Write-Error "SelfTest answer FAILED: last 4 bytes = $($ans[-4..-1] -join ',')"
        exit 1
    }
    # Subdomain match
    if (-not (Test-DomainMatch -Name "www.puente.link" -Patterns @("puente.link"))) {
        Write-Error "SelfTest subdomain match FAILED"
        exit 1
    }
    # Non-match
    if (Test-DomainMatch -Name "example.com" -Patterns @("puente.link")) {
        Write-Error "SelfTest non-match FAILED"
        exit 1
    }
    Write-Output "OK: DNS query parser, domain matcher, and answer builder verified."
    exit 0
}

# ---- Main loop --------------------------------------------------------------

Write-DnsLog "Starting puente_dns_responder. listen=${ListenIp}:${ListenPort} domains=$($Domain -join ',') answer=$AnswerIp upstream=$UpstreamDns pid=$PID"

$listenEndpoint = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($ListenIp), $ListenPort)
$any            = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)

try {
    $udp = New-Object System.Net.Sockets.UdpClient $listenEndpoint
} catch {
    Write-DnsLog "FATAL bind to ${ListenIp}:${ListenPort} failed: $($_.Exception.Message)"
    throw
}
Write-DnsLog "Listening on ${ListenIp}:${ListenPort}."

# Graceful shutdown when the process is terminated.
$shutdown = [scriptblock]{
    Write-DnsLog "Shutting down (signal)."
    try { $udp.Close() } catch {}
}
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action $shutdown | Out-Null

try {
    while ($true) {
        $remote = $any
        try {
            $bytes = $udp.Receive([ref]$remote)
        } catch [System.ObjectDisposedException] {
            break
        } catch {
            Write-DnsLog "Receive error: $($_.Exception.Message)"
            continue
        }

        $query = $null
        try { $query = ConvertFrom-DnsQuery -Bytes $bytes } catch { $query = $null }

        if ($null -eq $query) {
            Write-DnsLog "Malformed query from $($remote.Address):$($remote.Port) ($($bytes.Length) bytes); ignoring."
            continue
        }

        $isAQuery = ($query.QType -eq 1 -and $query.QClass -eq 1)
        $matches  = $isAQuery -and (Test-DomainMatch -Name $query.Name -Patterns $Domain)

        if ($matches) {
            try {
                $response = New-DnsAnswerBytes -Query $query -OriginalBytes $bytes -AnswerIp $AnswerIp
                [void]$udp.Send($response, $response.Length, $remote)
                Write-DnsLog "ANSWER $($query.Name) -> $AnswerIp [client=$($remote.Address):$($remote.Port) tid=$('0x{0:x4}' -f [int]$query.TransactionId)]"
            } catch {
                Write-DnsLog "Answer send failed for $($query.Name): $($_.Exception.Message)"
            }
        } else {
            # Forward to upstream and relay the response back.
            $forwarder = $null
            try {
                $forwarder = New-Object System.Net.Sockets.UdpClient
                $forwarder.Client.ReceiveTimeout = $UpstreamTimeoutMs
                $upstreamEp = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($UpstreamDns), 53)
                [void]$forwarder.Send($bytes, $bytes.Length, $upstreamEp)
                $upstreamReply = $forwarder.Receive([ref]$upstreamEp)
                [void]$udp.Send($upstreamReply, $upstreamReply.Length, $remote)
                Write-DnsLog "FORWARD $($query.Name) qtype=$($query.QType) -> $UpstreamDns ($($upstreamReply.Length) bytes back)"
            } catch [System.Net.Sockets.SocketException] {
                $servfail = New-DnsServfailBytes -Query $query -OriginalBytes $bytes
                try { [void]$udp.Send($servfail, $servfail.Length, $remote) } catch {}
                Write-DnsLog "FORWARD $($query.Name) timeout; sent SERVFAIL"
            } catch {
                Write-DnsLog "Forward error for $($query.Name): $($_.Exception.Message)"
            } finally {
                if ($forwarder) { try { $forwarder.Close() } catch {} }
            }
        }
    }
} finally {
    try { $udp.Close() } catch {}
    Write-DnsLog "Exited main loop."
}
