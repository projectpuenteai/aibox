BeforeAll {
    . (Join-Path $PSScriptRoot '..\lib\lib_io.ps1')
}

Describe 'lib_io' {
    Context 'Write-Utf8NoBom' {
        It 'writes UTF-8 without a BOM' {
            $tmp = New-TemporaryFile
            try {
                Write-Utf8NoBom -Path $tmp.FullName -Lines @('hello')
                $bytes = [System.IO.File]::ReadAllBytes($tmp.FullName)
                # First three bytes must NOT be the UTF-8 BOM (EF BB BF)
                ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) | Should -BeFalse
            } finally {
                Remove-Item -LiteralPath $tmp.FullName -Force
            }
        }

        It 'preserves multi-line content without corruption' {
            $tmp = New-TemporaryFile
            try {
                $lines = @('first line', 'second line', 'third line with unicode: ñ é')
                Write-Utf8NoBom -Path $tmp.FullName -Lines $lines
                $readBack = [System.IO.File]::ReadAllLines($tmp.FullName, [System.Text.Encoding]::UTF8)
                $readBack.Length | Should -Be 3
                $readBack[0] | Should -BeExactly 'first line'
                $readBack[2] | Should -BeExactly 'third line with unicode: ñ é'
            } finally {
                Remove-Item -LiteralPath $tmp.FullName -Force
            }
        }
    }
}
