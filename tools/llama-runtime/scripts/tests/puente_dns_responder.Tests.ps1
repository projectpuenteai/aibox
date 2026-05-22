BeforeAll {
    # Dot-source the responder with -SelfTest path bypassed by using a separate
    # dot-source pattern: we want the functions, not the main loop. To do that,
    # we extract the helper functions by sourcing a minimal copy that exports
    # only the parser/builder. Pester runs the responder via a child process.

    $script:Responder = Join-Path $PSScriptRoot '..\puente_dns_responder.ps1'

    # Helper that runs the responder process for `-SelfTest` and returns the exit code.
    function script:Invoke-ResponderSelfTest {
        $p = Start-Process -FilePath powershell.exe `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$script:Responder,'-SelfTest') `
            -NoNewWindow -Wait -PassThru
        return $p.ExitCode
    }

    # For in-process unit testing of the parser/builder, dot-source the script
    # with $SelfTest false but with a guard that prevents the bind. We set
    # $env:AIBOX_DNS_TEST_PARSE_ONLY=1 — but the responder doesn't honor that
    # by design (a script shouldn't have a test backdoor). Instead, copy just
    # the helper functions into the test scope via Invoke-Expression on the
    # script content up to the "Main loop" marker.

    $raw = Get-Content -LiteralPath $script:Responder -Raw
    $startMarker = '# ---- DNS message parsing / building'
    $endMarker   = '# ---- Main loop'
    $startIdx = $raw.IndexOf($startMarker)
    $endIdx   = $raw.IndexOf($endMarker)
    if ($startIdx -lt 0 -or $endIdx -lt 0 -or $endIdx -le $startIdx) {
        throw "Could not locate section markers in puente_dns_responder.ps1 (start=$startIdx end=$endIdx)"
    }
    $helpersOnly = $raw.Substring($startIdx, $endIdx - $startIdx)

    # Predeclare $LogFile so any helper that logs doesn't touch %ProgramData%.
    $bootstrap = @'
$LogFile = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "puente_dns_test.log")
function Write-DnsLog { param([string]$Message) }
'@
    Invoke-Expression ($bootstrap + "`n" + $helpersOnly)
}

Describe 'puente_dns_responder' {
    Context 'Self-test entrypoint' {
        It 'exits 0 with the verification message' {
            $exit = Invoke-ResponderSelfTest
            $exit | Should -Be 0
        }
    }

    Context 'ConvertFrom-DnsQuery' {
        It 'parses puente.link A query' {
            $bytes = [byte[]]@(
                0x12,0x34, 0x01,0x00, 0x00,0x01, 0x00,0x00, 0x00,0x00, 0x00,0x00,
                6,0x70,0x75,0x65,0x6e,0x74,0x65, 4,0x6c,0x69,0x6e,0x6b, 0,
                0x00,0x01, 0x00,0x01
            )
            $q = ConvertFrom-DnsQuery -Bytes $bytes
            $q.Name           | Should -Be "puente.link"
            $q.QType          | Should -Be 1
            $q.QClass         | Should -Be 1
            $q.TransactionId  | Should -Be 0x1234
        }

        It 'lowercases mixed-case labels' {
            $bytes = [byte[]]@(
                0x00,0x01, 0x01,0x00, 0x00,0x01, 0x00,0x00, 0x00,0x00, 0x00,0x00,
                6,0x50,0x55,0x45,0x4e,0x54,0x45, 4,0x4c,0x49,0x4e,0x4b, 0,
                0x00,0x01, 0x00,0x01
            )
            (ConvertFrom-DnsQuery -Bytes $bytes).Name | Should -Be "puente.link"
        }

        It 'returns $null for truncated packets' {
            $bytes = [byte[]]@(0x00,0x01,0x01,0x00,0x00,0x01)
            ConvertFrom-DnsQuery -Bytes $bytes | Should -BeNullOrEmpty
        }

        It 'returns $null when question label runs past buffer' {
            $bytes = [byte[]]@(
                0x00,0x01, 0x01,0x00, 0x00,0x01, 0x00,0x00, 0x00,0x00, 0x00,0x00,
                10, 0x61, 0x62
            )
            ConvertFrom-DnsQuery -Bytes $bytes | Should -BeNullOrEmpty
        }
    }

    Context 'Test-DomainMatch' {
        It 'matches the exact domain' {
            Test-DomainMatch -Name "puente.link" -Patterns @("puente.link") | Should -BeTrue
        }
        It 'matches a subdomain' {
            Test-DomainMatch -Name "www.puente.link" -Patterns @("puente.link") | Should -BeTrue
        }
        It 'matches a multi-level subdomain' {
            Test-DomainMatch -Name "a.b.puente.link" -Patterns @("puente.link") | Should -BeTrue
        }
        It 'does not match unrelated domain' {
            Test-DomainMatch -Name "example.com" -Patterns @("puente.link") | Should -BeFalse
        }
        It 'does not match a domain that merely contains the pattern' {
            Test-DomainMatch -Name "evilpuente.link" -Patterns @("puente.link") | Should -BeFalse
        }
        It 'is case-insensitive' {
            Test-DomainMatch -Name "Puente.LINK" -Patterns @("puente.link") | Should -BeTrue
        }
        It 'handles trailing dots' {
            Test-DomainMatch -Name "puente.link." -Patterns @("puente.link") | Should -BeTrue
        }
    }

    Context 'New-DnsAnswerBytes' {
        BeforeAll {
            $script:queryBytes = [byte[]]@(
                0xAB,0xCD, 0x01,0x00, 0x00,0x01, 0x00,0x00, 0x00,0x00, 0x00,0x00,
                6,0x70,0x75,0x65,0x6e,0x74,0x65, 4,0x6c,0x69,0x6e,0x6b, 0,
                0x00,0x01, 0x00,0x01
            )
            $script:parsed = ConvertFrom-DnsQuery -Bytes $script:queryBytes
        }

        It 'encodes the original transaction id' {
            $resp = New-DnsAnswerBytes -Query $script:parsed -OriginalBytes $script:queryBytes -AnswerIp "192.168.137.1"
            $resp[0] | Should -Be 0xAB
            $resp[1] | Should -Be 0xCD
        }

        It 'sets QR + AA flags and preserves RD' {
            $resp = New-DnsAnswerBytes -Query $script:parsed -OriginalBytes $script:queryBytes -AnswerIp "192.168.137.1"
            ($resp[2] -band 0x84) | Should -Be 0x84
            ($resp[2] -band 0x01) | Should -Be 0x01
        }

        It 'has QDCOUNT = 1 and ANCOUNT = 1' {
            $resp = New-DnsAnswerBytes -Query $script:parsed -OriginalBytes $script:queryBytes -AnswerIp "192.168.137.1"
            (($resp[4] -shl 8) -bor $resp[5]) | Should -Be 1
            (($resp[6] -shl 8) -bor $resp[7]) | Should -Be 1
        }

        It 'ends with the configured answer IP octets' {
            $resp = New-DnsAnswerBytes -Query $script:parsed -OriginalBytes $script:queryBytes -AnswerIp "192.168.137.1"
            $resp[$resp.Length-4] | Should -Be 192
            $resp[$resp.Length-3] | Should -Be 168
            $resp[$resp.Length-2] | Should -Be 137
            $resp[$resp.Length-1] | Should -Be 1
        }

        It 'uses a compression pointer (0xC00C) for the answer name' {
            $resp = New-DnsAnswerBytes -Query $script:parsed -OriginalBytes $script:queryBytes -AnswerIp "192.168.137.1"
            $answerStart = 12 + $script:parsed.QuestionLength
            $resp[$answerStart]     | Should -Be 0xC0
            $resp[$answerStart + 1] | Should -Be 0x0C
        }
    }
}
