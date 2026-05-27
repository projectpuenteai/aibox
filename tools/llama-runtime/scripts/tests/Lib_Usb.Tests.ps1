BeforeAll {
    . (Join-Path $PSScriptRoot '..\lib\lib_usb.ps1')
}

Describe 'lib_usb' {

    Context 'Get-UsbContentSpec' {
        It 'lists both GGUF shards as file entries' {
            $spec = Get-UsbContentSpec
            $gguf = @($spec | Where-Object { $_.RepoRel -like 'models/llm/gguf/*.gguf' })
            $gguf.Count | Should -Be 2
            ($gguf | ForEach-Object { $_.Kind } | Select-Object -Unique) | Should -Be 'file'
        }

        It 'includes the Chroma index as a recursive tree entry' {
            $spec = Get-UsbContentSpec
            $chroma = @($spec | Where-Object { $_.RepoRel -eq 'backend-data/chroma_db_es' })
            $chroma.Count | Should -Be 1
            $chroma[0].Kind | Should -Be 'tree'
        }

        It 'uses only the file and tree kinds' {
            $kinds = @(Get-UsbContentSpec | ForEach-Object { $_.Kind } | Select-Object -Unique)
            foreach ($k in $kinds) { $k | Should -BeIn @('file', 'tree') }
        }

        It 'ships both Wikipedia ZIMs' {
            $zims = @(Get-UsbContentSpec | Where-Object { $_.RepoRel -like 'kiwix/*.zim' })
            $zims.Count | Should -Be 2
        }

        It 'does not ship pruned junk (onnx, imgs, .gitattributes)' {
            $spec = Get-UsbContentSpec
            @($spec | Where-Object { $_.RepoRel -like '*onnx*' }).Count | Should -Be 0
            @($spec | Where-Object { $_.RepoRel -like '*imgs*' }).Count | Should -Be 0
            @($spec | Where-Object { $_.RepoRel -like '*.gitattributes' }).Count | Should -Be 0
        }
    }

    Context 'ConvertTo/From-UsbManifestJson' {
        It 'round-trips a multi-file manifest' {
            $files = @(
                [pscustomobject]@{ path = 'content/models/a.bin'; dest = 'models/a.bin'; size_bytes = 10; sha256 = 'aa' },
                [pscustomobject]@{ path = 'content/kiwix/b.zim'; dest = 'kiwix/b.zim'; size_bytes = 20; sha256 = 'bb' }
            )
            $json = ConvertTo-UsbManifestJson -Files $files -BuiltAt '2026-05-26T00:00:00Z' -SourceHost 'TESTHOST'
            $parsed = ConvertFrom-UsbManifestJson -Json $json

            $parsed.Schema | Should -BeExactly 'aibox-usb/1'
            $parsed.BuiltAt | Should -BeExactly '2026-05-26T00:00:00Z'
            $parsed.SourceHost | Should -BeExactly 'TESTHOST'
            @($parsed.Files).Count | Should -Be 2
            $parsed.Files[0].path | Should -BeExactly 'content/models/a.bin'
            $parsed.Files[0].dest | Should -BeExactly 'models/a.bin'
            $parsed.Files[1].size_bytes | Should -Be 20
        }

        It 'lowercases sha256 values' {
            $files = @([pscustomobject]@{ path = 'content/x'; dest = 'x'; size_bytes = 1; sha256 = 'ABCDEF' })
            $parsed = ConvertFrom-UsbManifestJson -Json (ConvertTo-UsbManifestJson -Files $files -BuiltAt 'z')
            @($parsed.Files)[0].sha256 | Should -BeExactly 'abcdef'
        }
    }

    Context 'New-UsbManifest / Read-UsbManifest' {
        It 'writes UTF-8 without a BOM and reads back' {
            $tmp = New-TemporaryFile
            try {
                $files = @([pscustomobject]@{ path = 'content/m'; dest = 'm'; size_bytes = 3; sha256 = 'cc' })
                New-UsbManifest -Files $files -OutFile $tmp.FullName -SourceHost 'HOST'

                $bytes = [System.IO.File]::ReadAllBytes($tmp.FullName)
                ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) | Should -BeFalse

                $parsed = Read-UsbManifest -Path $tmp.FullName
                $parsed.Schema | Should -BeExactly 'aibox-usb/1'
                @($parsed.Files).Count | Should -Be 1
                $parsed.Files[0].dest | Should -BeExactly 'm'
            }
            finally {
                Remove-Item -LiteralPath $tmp.FullName -Force
            }
        }

        It 'returns $null for a missing manifest' {
            Read-UsbManifest -Path (Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString() + '.json')) | Should -BeNullOrEmpty
        }
    }

    Context 'Test-FileSha256' {
        It 'reports Ok for a matching hash' {
            $tmp = New-TemporaryFile
            try {
                Set-Content -LiteralPath $tmp.FullName -Value 'payload' -NoNewline
                $sha = Get-FileSha256 -Path $tmp.FullName
                $r = Test-FileSha256 -Path $tmp.FullName -Expected $sha.ToUpperInvariant()
                $r.Ok | Should -BeTrue
                $r.Reason | Should -BeExactly ''
            }
            finally { Remove-Item -LiteralPath $tmp.FullName -Force }
        }

        It 'reports a mismatch' {
            $tmp = New-TemporaryFile
            try {
                Set-Content -LiteralPath $tmp.FullName -Value 'payload' -NoNewline
                $r = Test-FileSha256 -Path $tmp.FullName -Expected ('0' * 64)
                $r.Ok | Should -BeFalse
                $r.Reason | Should -BeExactly 'sha256_mismatch'
            }
            finally { Remove-Item -LiteralPath $tmp.FullName -Force }
        }

        It 'reports a missing file' {
            $r = Test-FileSha256 -Path (Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())) -Expected 'aa'
            $r.Ok | Should -BeFalse
            $r.Reason | Should -BeExactly 'file_missing'
        }
    }

    Context 'Resolve-UsbSourceFiles' {
        It 'expands a tree entry and flags missing files' {
            $root = Join-Path ([IO.Path]::GetTempPath()) ('aibox-usb-' + [guid]::NewGuid().ToString())
            try {
                # Present file
                New-Item -ItemType Directory -Path (Join-Path $root 'models\llm\gguf') -Force | Out-Null
                Set-Content -LiteralPath (Join-Path $root 'models\llm\gguf\m.gguf') -Value 'x' -NoNewline
                # Tree with two files in a UUID-named subdir
                $seg = Join-Path $root 'backend-data\chroma_db_es\seg-uuid'
                New-Item -ItemType Directory -Path $seg -Force | Out-Null
                Set-Content -LiteralPath (Join-Path $root 'backend-data\chroma_db_es\chroma.sqlite3') -Value 'db' -NoNewline
                Set-Content -LiteralPath (Join-Path $seg 'data_level0.bin') -Value 'bin' -NoNewline

                $spec = @(
                    [pscustomobject]@{ Kind = 'file'; RepoRel = 'models/llm/gguf/m.gguf' },
                    [pscustomobject]@{ Kind = 'file'; RepoRel = 'models/llm/missing.json' },
                    [pscustomobject]@{ Kind = 'tree'; RepoRel = 'backend-data/chroma_db_es' }
                )
                $resolved = Resolve-UsbSourceFiles -AiboxDir $root -Spec $spec

                @($resolved | Where-Object { $_.RepoRel -eq 'models/llm/gguf/m.gguf' -and $_.Exists }).Count | Should -Be 1
                @($resolved | Where-Object { $_.RepoRel -eq 'models/llm/missing.json' -and -not $_.Exists }).Count | Should -Be 1

                $chroma = @($resolved | Where-Object { $_.RepoRel -like 'backend-data/chroma_db_es/*' })
                $chroma.Count | Should -Be 2
                # forward-slash, relative to aibox/
                @($chroma | Where-Object { $_.RepoRel -eq 'backend-data/chroma_db_es/seg-uuid/data_level0.bin' }).Count | Should -Be 1
                @($chroma | Where-Object { $_.RepoRel -eq 'backend-data/chroma_db_es/chroma.sqlite3' }).Count | Should -Be 1
            }
            finally {
                Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Context 'Resolve-UsbPayloadRoot' {
        It 'accepts a path that is itself the payload dir' {
            $dir = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            try {
                Set-Content -LiteralPath (Join-Path $dir 'manifest.json') -Value '{}' -NoNewline
                (Resolve-UsbPayloadRoot -UsbPath $dir) | Should -BeExactly $dir
            }
            finally { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue }
        }

        It 'accepts a parent that contains AIBox-Payload' {
            $parent = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
            $payload = Join-Path $parent 'AIBox-Payload'
            New-Item -ItemType Directory -Path $payload -Force | Out-Null
            try {
                Set-Content -LiteralPath (Join-Path $payload 'manifest.json') -Value '{}' -NoNewline
                (Resolve-UsbPayloadRoot -UsbPath $parent) | Should -BeExactly $payload
            }
            finally { Remove-Item -LiteralPath $parent -Recurse -Force -ErrorAction SilentlyContinue }
        }

        It 'returns $null when no manifest is found at the given path' {
            $dir = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            try {
                (Resolve-UsbPayloadRoot -UsbPath $dir) | Should -BeNullOrEmpty
            }
            finally { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }

    Context 'Test-UsbManifestEntry (untrusted manifest hardening)' {
        BeforeAll {
            $payload = 'C:\fake\AIBox-Payload'
            $aibox = 'C:\fake\repo\aibox'
        }

        It 'accepts a legitimate model entry and returns safe absolute paths' {
            $e = [pscustomobject]@{ path = 'content/models/llm/gguf/m.gguf'; dest = 'models/llm/gguf/m.gguf' }
            $r = Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox
            $r.Ok | Should -BeTrue
            $r.SourcePath | Should -BeExactly 'C:\fake\AIBox-Payload\content\models\llm\gguf\m.gguf'
            $r.DestPath | Should -BeExactly 'C:\fake\repo\aibox\models\llm\gguf\m.gguf'
        }

        It 'accepts a legitimate chroma index entry' {
            $e = [pscustomobject]@{ path = 'content/backend-data/chroma_db_es/chroma.sqlite3'; dest = 'backend-data/chroma_db_es/chroma.sqlite3' }
            (Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox).Ok | Should -BeTrue
        }

        It 'REJECTS overwriting up_stack.ps1 (the Codex attack)' {
            $e = [pscustomobject]@{ path = 'content/models/llm/gguf/m.gguf'; dest = 'tools/llama-runtime/scripts/up_stack.ps1' }
            $r = Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox
            $r.Ok | Should -BeFalse
            $r.Reason | Should -BeExactly 'dest_not_allowlisted'
        }

        It 'REJECTS a dest with .. traversal' {
            $e = [pscustomobject]@{ path = 'content/models/x'; dest = 'models/../../../Windows/System32/evil.dll' }
            (Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox).Reason | Should -BeExactly 'dest_dotdot'
        }

        It 'REJECTS a backslash-encoded dest traversal' {
            $e = [pscustomobject]@{ path = 'content/models/x'; dest = 'models\..\..\up_stack.ps1' }
            (Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox).Reason | Should -BeExactly 'dest_dotdot'
        }

        It 'REJECTS a rooted/absolute dest' {
            $e = [pscustomobject]@{ path = 'content/models/x'; dest = 'C:\Windows\System32\evil.dll' }
            (Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox).Reason | Should -BeExactly 'dest_rooted'
        }

        It 'REJECTS a source path outside content/' {
            $e = [pscustomobject]@{ path = 'secrets/key.txt'; dest = 'models/x' }
            (Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox).Reason | Should -BeExactly 'path_not_under_content'
        }

        It 'REJECTS .. traversal in the source path' {
            $e = [pscustomobject]@{ path = 'content/../../../etc/passwd'; dest = 'models/x' }
            (Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox).Reason | Should -BeExactly 'path_dotdot'
        }

        It 'REJECTS an empty dest' {
            $e = [pscustomobject]@{ path = 'content/models/x'; dest = '' }
            (Test-UsbManifestEntry -Entry $e -PayloadRoot $payload -AiboxDir $aibox).Reason | Should -BeExactly 'empty_dest'
        }
    }
}
