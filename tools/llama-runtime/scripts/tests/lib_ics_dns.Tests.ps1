BeforeAll {
    . (Join-Path $PSScriptRoot '..\lib\lib_ics_dns.ps1')

    # Use a scratch registry path so the real ICS service is never touched by
    # tests. HKCU is writable without elevation.
    $script:TestRegRoot = 'HKCU:\Software\AIBoxPuenteTests'
    $script:TestRegPath = Join-Path $script:TestRegRoot 'Parameters'
}

AfterAll {
    if (Test-Path $script:TestRegRoot) {
        Remove-Item -Path $script:TestRegRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Describe 'lib_ics_dns' {
    BeforeEach {
        if (Test-Path $script:TestRegPath) {
            Remove-Item -Path $script:TestRegPath -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -Path $script:TestRegPath -Force | Out-Null
    }

    Context 'Test-IcsDnsProxyEnabled' {
        It 'returns $true when EnableProxy is absent (ICS default)' {
            Test-IcsDnsProxyEnabled -RegPath $script:TestRegPath | Should -BeTrue
        }
        It 'returns $false when EnableProxy = 0' {
            New-ItemProperty -Path $script:TestRegPath -Name EnableProxy -Value 0 -PropertyType DWord -Force | Out-Null
            Test-IcsDnsProxyEnabled -RegPath $script:TestRegPath | Should -BeFalse
        }
        It 'returns $true when EnableProxy = 1' {
            New-ItemProperty -Path $script:TestRegPath -Name EnableProxy -Value 1 -PropertyType DWord -Force | Out-Null
            Test-IcsDnsProxyEnabled -RegPath $script:TestRegPath | Should -BeTrue
        }
    }

    Context 'Set-IcsDnsProxyValue (no service restart)' {
        It 'writes EnableProxy = 0' {
            Set-IcsDnsProxyValue -Value 0 -RegPath $script:TestRegPath -NoServiceRestart
            $prop = Get-ItemProperty -Path $script:TestRegPath -Name EnableProxy
            $prop.EnableProxy | Should -Be 0
        }
        It 'overwrites an existing value' {
            New-ItemProperty -Path $script:TestRegPath -Name EnableProxy -Value 1 -PropertyType DWord -Force | Out-Null
            Set-IcsDnsProxyValue -Value 0 -RegPath $script:TestRegPath -NoServiceRestart
            $prop = Get-ItemProperty -Path $script:TestRegPath -Name EnableProxy
            $prop.EnableProxy | Should -Be 0
        }
        It 'throws if registry path does not exist' {
            $missing = 'HKCU:\Software\AIBoxPuenteTests\DoesNotExist'
            { Set-IcsDnsProxyValue -Value 0 -RegPath $missing -NoServiceRestart } | Should -Throw
        }
    }

    Context 'Disable-IcsDnsProxy / Enable-IcsDnsProxy round-trip' {
        It 'returns previous state ($true) and sets EnableProxy = 0' {
            New-ItemProperty -Path $script:TestRegPath -Name EnableProxy -Value 1 -PropertyType DWord -Force | Out-Null
            $prev = Disable-IcsDnsProxy -RegPath $script:TestRegPath -NoServiceRestart
            $prev | Should -BeTrue
            (Get-ItemProperty -Path $script:TestRegPath -Name EnableProxy).EnableProxy | Should -Be 0
        }
        It 'returns previous state ($false) when EnableProxy was already 0' {
            New-ItemProperty -Path $script:TestRegPath -Name EnableProxy -Value 0 -PropertyType DWord -Force | Out-Null
            $prev = Disable-IcsDnsProxy -RegPath $script:TestRegPath -NoServiceRestart
            $prev | Should -BeFalse
        }
        It 'Enable-IcsDnsProxy sets EnableProxy = 1' {
            New-ItemProperty -Path $script:TestRegPath -Name EnableProxy -Value 0 -PropertyType DWord -Force | Out-Null
            Enable-IcsDnsProxy -RegPath $script:TestRegPath -NoServiceRestart
            (Get-ItemProperty -Path $script:TestRegPath -Name EnableProxy).EnableProxy | Should -Be 1
        }
    }
}
