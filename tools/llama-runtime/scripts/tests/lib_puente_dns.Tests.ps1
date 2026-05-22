BeforeAll {
    . (Join-Path $PSScriptRoot '..\lib\lib_puente_dns.ps1')

    $script:TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("aibox-puente-dns-tests-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:TempDir -Force | Out-Null
}

AfterAll {
    if (Test-Path -LiteralPath $script:TempDir) {
        Remove-Item -Path $script:TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Describe 'lib_puente_dns' {
    Context 'Test-PuenteResponderProcess' {
        It 'returns $false for a non-existent PID' {
            Test-PuenteResponderProcess -ProcessId 99999999 | Should -BeFalse
        }

        It 'returns $false for an unrelated process (this Pester process itself)' {
            # Our own PID is the Pester host, which is a powershell process but its
            # command line does NOT mention puente_dns_responder.ps1.
            Test-PuenteResponderProcess -ProcessId $PID | Should -BeFalse
        }
    }

    Context 'Save-IcsDnsPriorState / Read-IcsDnsPriorState round-trip' {
        It 'returns $null when the sidecar does not exist' {
            $path = Join-Path $script:TempDir "missing.json"
            Read-IcsDnsPriorState -Path $path | Should -BeNullOrEmpty
        }

        It 'round-trips prev_enabled=true / we_toggled=true' {
            $path = Join-Path $script:TempDir "case1.json"
            Save-IcsDnsPriorState -Path $path -PrevEnabled $true -WeToggled $true
            $state = Read-IcsDnsPriorState -Path $path
            $state.prev_enabled | Should -BeTrue
            $state.we_toggled   | Should -BeTrue
        }

        It 'round-trips prev_enabled=false / we_toggled=true' {
            $path = Join-Path $script:TempDir "case2.json"
            Save-IcsDnsPriorState -Path $path -PrevEnabled $false -WeToggled $true
            $state = Read-IcsDnsPriorState -Path $path
            $state.prev_enabled | Should -BeFalse
            $state.we_toggled   | Should -BeTrue
        }

        It 'round-trips we_toggled=false' {
            $path = Join-Path $script:TempDir "case3.json"
            Save-IcsDnsPriorState -Path $path -PrevEnabled $true -WeToggled $false
            $state = Read-IcsDnsPriorState -Path $path
            $state.we_toggled | Should -BeFalse
        }

        It 'creates the parent directory if missing' {
            $path = Join-Path $script:TempDir "nested\dir\state.json"
            Save-IcsDnsPriorState -Path $path -PrevEnabled $true
            Test-Path -LiteralPath $path | Should -BeTrue
        }

        It 'returns $null for malformed JSON' {
            $path = Join-Path $script:TempDir "broken.json"
            Set-Content -LiteralPath $path -Value "{this is not json" -Encoding UTF8
            Read-IcsDnsPriorState -Path $path | Should -BeNullOrEmpty
        }
    }

    Context 'Stop-PuenteResponderByPid' {
        It 'returns $false (no-op) when PID does not exist' {
            Stop-PuenteResponderByPid -ProcessId 99999999 | Should -BeFalse
        }

        It 'returns $false (no-op) when PID is an unrelated process' {
            # Pester host PID is powershell but not the responder.
            Stop-PuenteResponderByPid -ProcessId $PID | Should -BeFalse
            # Confirm we didn't suicide.
            (Get-Process -Id $PID -ErrorAction SilentlyContinue) | Should -Not -BeNullOrEmpty
        }
    }
}
