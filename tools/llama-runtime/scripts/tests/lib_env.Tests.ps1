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

        It 'strips a matched pair of double quotes' {
            $tmp = New-TemporaryFile
            try {
                Set-Content -LiteralPath $tmp.FullName -Value 'KEY="hello world"'
                (Get-DotEnvMap -Path $tmp.FullName)['KEY'] | Should -BeExactly 'hello world'
            } finally { Remove-Item -LiteralPath $tmp.FullName -Force }
        }

        It 'preserves literal single quotes (does NOT strip)' {
            $tmp = New-TemporaryFile
            try {
                Set-Content -LiteralPath $tmp.FullName -Value "KEY='hello'"
                (Get-DotEnvMap -Path $tmp.FullName)['KEY'] | Should -BeExactly "'hello'"
            } finally { Remove-Item -LiteralPath $tmp.FullName -Force }
        }

        It 'preserves embedded equals sign in value' {
            $tmp = New-TemporaryFile
            try {
                Set-Content -LiteralPath $tmp.FullName -Value 'TOKEN=abc=def=ghi'
                (Get-DotEnvMap -Path $tmp.FullName)['TOKEN'] | Should -BeExactly 'abc=def=ghi'
            } finally { Remove-Item -LiteralPath $tmp.FullName -Force }
        }

        It 'ignores comment-only and blank lines' {
            $tmp = New-TemporaryFile
            try {
                Set-Content -LiteralPath $tmp.FullName -Value @('# a comment','','KEY=value','   ','# another')
                $map = Get-DotEnvMap -Path $tmp.FullName
                $map['KEY'] | Should -BeExactly 'value'
                $map.Keys.Count | Should -Be 1
            } finally { Remove-Item -LiteralPath $tmp.FullName -Force }
        }
    }
}
