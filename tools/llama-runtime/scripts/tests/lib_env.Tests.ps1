BeforeAll {
    . (Join-Path $PSScriptRoot '..\lib\lib_env.ps1')
}

Describe 'lib_env' {
    Context 'Get-DotEnvMap' {
        It 'parses an unquoted value' {
            $tmp = New-TemporaryFile
            try {
                Set-Content -LiteralPath $tmp.FullName -Value "KEY=hello"
                $map = Get-DotEnvMap -Path $tmp.FullName
                $map['KEY'] | Should -BeExactly 'hello'
            } finally {
                Remove-Item -LiteralPath $tmp.FullName -Force
            }
        }
    }
}
