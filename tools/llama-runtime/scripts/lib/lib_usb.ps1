# Helpers for building and consuming the AIBox offline USB payload.
#
# Pure helpers return values/objects and avoid exit/throw so the caller
# (build_usb.ps1 / install_from_usb.ps1) can map results onto its own error
# handling - same convention as lib_model.ps1.
#
# USB layout (see CLAUDE.md "USB install"):
#   <drive>\AIBox-Payload\
#     manifest.json                 SHA256 catalog
#     content\<repo-relative>\...   files copied into <repo>\aibox\<repo-relative>
#
# A manifest "file" entry has:
#   path       USB-relative source path     (e.g. content/models/llm/...)
#   dest       aibox-relative install path  (e.g. models/llm/...)
#   size_bytes [long]
#   sha256     lowercase hex

$script:UsbManifestSchema = 'aibox-usb/1'
$script:UsbPayloadDirName = 'AIBox-Payload'
$script:UsbContentDirName = 'content'

function Get-UsbContentSpec {
    # Static, side-effect-free description of what ships on the USB. RepoRel is
    # forward-slash and relative to the repo's aibox/ directory. 'file' entries
    # are copied verbatim; 'tree' entries are expanded recursively at build time
    # (used for the Chroma index, whose HNSW segment subdir has a UUID name).
    #
    # The model file lists are the curated runtime subset (onnx/imgs/duplicate
    # weights and VCS junk pruned). Docker images and Kolibri content are
    # intentionally absent - under the content-only scope they come from the
    # internet on the target machine (docker compose pull + kolibri
    # importchannel network). This is the sole source of truth for what ships;
    # the retired download installer's release-config.yaml is archived under
    # legacy/installer-download-path/.
    [CmdletBinding()]
    param()

    $spec = New-Object System.Collections.Generic.List[object]

    # LLM - Qwen2.5-7B-Instruct Q4_0 (2 GGUF shards)
    $spec.Add([pscustomobject]@{ Kind = 'file'; RepoRel = 'models/llm/gguf/qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf' })
    $spec.Add([pscustomobject]@{ Kind = 'file'; RepoRel = 'models/llm/gguf/qwen2.5-7b-instruct-q4_0-00002-of-00002.gguf' })

    # LLM sidecars (tokenizer/config)
    foreach ($f in @('config.json', 'generation_config.json', 'tokenizer.json', 'tokenizer_config.json', 'vocab.json', 'merges.txt', 'LICENSE', 'README.md')) {
        $spec.Add([pscustomobject]@{ Kind = 'file'; RepoRel = "models/llm/$f" })
    }

    # Embedding - bge-m3 (PyTorch path; ONNX excluded)
    foreach ($f in @('config.json', 'config_sentence_transformers.json', 'modules.json', 'sentence_bert_config.json', 'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json', 'sentencepiece.bpe.model', 'colbert_linear.pt', 'sparse_linear.pt', 'pytorch_model.bin', '1_Pooling/config.json', 'README.md')) {
        $spec.Add([pscustomobject]@{ Kind = 'file'; RepoRel = "models/embed-m3/$f" })
    }

    # Reranker - bge-reranker-base (safetensors only)
    foreach ($f in @('config.json', 'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json', 'sentencepiece.bpe.model', 'model.safetensors', 'README.md')) {
        $spec.Add([pscustomobject]@{ Kind = 'file'; RepoRel = "models/rerank/$f" })
    }

    # Wikipedia ZIMs (Kiwix). Filenames must match the docker-compose commands
    # and KIWIX_BOOK_* env values.
    $spec.Add([pscustomobject]@{ Kind = 'file'; RepoRel = 'kiwix/wikipedia_en_all_mini_2026-03.zim' })
    $spec.Add([pscustomobject]@{ Kind = 'file'; RepoRel = 'kiwix/wikipedia_es_all_maxi_2026-02.zim' })

    # Spanish Chroma index - the bind-mount source up_stack.ps1 populates the
    # chroma_db_es_native volume from. Recursive because the segment subdir name
    # is a UUID that varies per build.
    $spec.Add([pscustomobject]@{ Kind = 'tree'; RepoRel = 'backend-data/chroma_db_es' })

    return $spec.ToArray()
}

function Resolve-UsbSourceFiles {
    # Expand Get-UsbContentSpec against a real aibox/ directory into a flat list
    # of concrete files. Returns objects with:
    #   RepoRel   forward-slash path relative to aibox/
    #   FullPath  absolute source path
    #   SizeBytes [long]
    #   Exists    [bool]
    # Missing 'file' entries are returned with Exists=$false so the caller can
    # report them; 'tree' entries enumerate only what is present on disk.
    param(
        [Parameter(Mandatory = $true)][string]$AiboxDir,
        [object[]]$Spec
    )

    if (-not $Spec) { $Spec = Get-UsbContentSpec }
    $out = New-Object System.Collections.Generic.List[object]

    foreach ($entry in $Spec) {
        $repoRel = [string]$entry.RepoRel
        $native = $repoRel -replace '/', '\'
        $full = Join-Path $AiboxDir $native

        if ($entry.Kind -eq 'tree') {
            if (Test-Path -LiteralPath $full -PathType Container) {
                $base = (Resolve-Path -LiteralPath $full).Path.TrimEnd('\')
                foreach ($file in Get-ChildItem -LiteralPath $full -Recurse -File -Force) {
                    $rel = $file.FullName.Substring($base.Length).TrimStart('\', '/')
                    $relFwd = $repoRel.TrimEnd('/') + '/' + ($rel -replace '\\', '/')
                    $out.Add([pscustomobject]@{
                            RepoRel   = $relFwd
                            FullPath  = $file.FullName
                            SizeBytes = [long]$file.Length
                            Exists    = $true
                        })
                }
            }
            # A missing tree contributes nothing; the caller validates presence.
        }
        else {
            if (Test-Path -LiteralPath $full -PathType Leaf) {
                $out.Add([pscustomobject]@{
                        RepoRel   = $repoRel
                        FullPath  = $full
                        SizeBytes = [long](Get-Item -LiteralPath $full).Length
                        Exists    = $true
                    })
            }
            else {
                $out.Add([pscustomobject]@{
                        RepoRel   = $repoRel
                        FullPath  = $full
                        SizeBytes = [long]0
                        Exists    = $false
                    })
            }
        }
    }

    return $out.ToArray()
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-FileSha256 {
    # Returns @{ Ok; Expected; Actual; Reason }. Reason is 'file_missing' or
    # 'sha256_mismatch' on failure, '' on success.
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected
    )

    $exp = ([string]$Expected).ToLowerInvariant()

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{ Ok = $false; Expected = $exp; Actual = $null; Reason = 'file_missing' }
    }

    $actual = Get-FileSha256 -Path $Path
    $ok = ($actual -eq $exp)
    $reason = ''
    if (-not $ok) { $reason = 'sha256_mismatch' }
    return [pscustomobject]@{ Ok = $ok; Expected = $exp; Actual = $actual; Reason = $reason }
}

function ConvertTo-UsbManifestJson {
    # Pure: build the manifest JSON string from a list of file entries. Each
    # entry must expose path / dest / size_bytes / sha256.
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Files,
        [string]$BuiltAt = '',
        [string]$SourceHost = ''
    )

    if ([string]::IsNullOrEmpty($BuiltAt)) {
        $BuiltAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }

    $items = New-Object System.Collections.Generic.List[object]
    foreach ($f in $Files) {
        $items.Add([pscustomobject]@{
                path       = [string]$f.path
                dest       = [string]$f.dest
                size_bytes = [long]$f.size_bytes
                sha256     = ([string]$f.sha256).ToLowerInvariant()
            })
    }

    $doc = [pscustomobject]@{
        schema      = $script:UsbManifestSchema
        built_at    = $BuiltAt
        source_host = $SourceHost
        file_count  = $items.Count
        files       = $items.ToArray()
    }

    return ($doc | ConvertTo-Json -Depth 6)
}

function ConvertFrom-UsbManifestJson {
    # Pure: parse a manifest JSON string into a normalized object. Files is
    # always coerced to an array (PS 5.1 collapses single-element arrays).
    param([Parameter(Mandatory = $true)][string]$Json)

    $doc = $Json | ConvertFrom-Json
    $files = @()
    if ($null -ne $doc.files) { $files = @($doc.files) }

    return [pscustomobject]@{
        Schema     = [string]$doc.schema
        BuiltAt    = [string]$doc.built_at
        SourceHost = [string]$doc.source_host
        FileCount  = $(if ($null -ne $doc.file_count) { [long]$doc.file_count } else { [long]0 })
        Files      = $files
    }
}

function New-UsbManifest {
    # Write manifest.json as UTF-8 without a BOM.
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Files,
        [Parameter(Mandatory = $true)][string]$OutFile,
        [string]$SourceHost = ''
    )

    $json = ConvertTo-UsbManifestJson -Files $Files -SourceHost $SourceHost
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($OutFile, $json, $enc)
}

function Read-UsbManifest {
    # Read + parse manifest.json. Returns $null when the file is absent.
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $json = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    return ConvertFrom-UsbManifestJson -Json $json
}

function Resolve-UsbPayloadRoot {
    # Locate the AIBox-Payload directory (the one holding manifest.json).
    #
    # With -UsbPath the path may point at the drive root, the payload dir
    # itself, or a parent; we normalize to the dir that actually holds
    # manifest.json (or $null). With no -UsbPath we scan every ready drive for
    # <root>\AIBox-Payload\manifest.json, preferring removable drives.
    param([string]$UsbPath = '')

    $manifestName = 'manifest.json'
    $payloadDir = $script:UsbPayloadDirName

    function Test-PayloadCandidate {
        param([string]$Dir)
        if ([string]::IsNullOrWhiteSpace($Dir)) { return $null }
        if (Test-Path -LiteralPath (Join-Path $Dir $manifestName) -PathType Leaf) { return $Dir }
        $nested = Join-Path $Dir $payloadDir
        if (Test-Path -LiteralPath (Join-Path $nested $manifestName) -PathType Leaf) { return $nested }
        return $null
    }

    if (-not [string]::IsNullOrWhiteSpace($UsbPath)) {
        return (Test-PayloadCandidate -Dir $UsbPath)
    }

    $drives = @()
    try {
        $drives = [System.IO.DriveInfo]::GetDrives() | Where-Object { $_.IsReady }
    }
    catch { $drives = @() }

    $ordered = @()
    $ordered += @($drives | Where-Object { "$($_.DriveType)" -eq 'Removable' })
    $ordered += @($drives | Where-Object { "$($_.DriveType)" -ne 'Removable' })

    foreach ($d in $ordered) {
        $hit = Test-PayloadCandidate -Dir $d.RootDirectory.FullName
        if ($hit) { return $hit }
    }
    return $null
}

function Test-UsbManifestEntry {
    # Validate one manifest entry's path/dest BEFORE any copy. The USB manifest
    # is untrusted input (no signature under the content-only scope), so a
    # tampered payload must not be able to read outside <PayloadRoot>\content or
    # write outside the allowlisted content roots under AiboxDir -- otherwise a
    # crafted 'dest' (e.g. tools/llama-runtime/scripts/up_stack.ps1) could
    # overwrite a script the installer then executes at handoff.
    #
    # Returns @{ Ok; Reason; SourcePath; DestPath }. On success SourcePath and
    # DestPath are the canonical absolute paths the caller MUST use -- never
    # re-join the raw manifest strings. Reason codes on failure: empty_path,
    # empty_dest, path_dotdot, dest_dotdot, path_rooted, dest_rooted,
    # path_not_under_content, dest_not_allowlisted, invalid_path, path_escapes,
    # dest_escapes.
    param(
        [Parameter(Mandatory = $true)]$Entry,
        [Parameter(Mandatory = $true)][string]$PayloadRoot,
        [Parameter(Mandatory = $true)][string]$AiboxDir,
        # Destination roots a payload is allowed to write to (mirrors the roots
        # produced by Get-UsbContentSpec).
        [string[]]$DestAllowlist = @('models/', 'kiwix/', 'backend-data/chroma_db_es/')
    )

    $rawPath = [string]$Entry.path
    $rawDest = [string]$Entry.dest
    $fail = { param($r) [pscustomobject]@{ Ok = $false; Reason = $r; SourcePath = $null; DestPath = $null } }

    if ([string]::IsNullOrWhiteSpace($rawPath)) { return (& $fail 'empty_path') }
    if ([string]::IsNullOrWhiteSpace($rawDest)) { return (& $fail 'empty_dest') }

    # Normalize to forward-slash for logical checks; both separators are treated
    # as segment boundaries so a backslash-encoded '..' cannot slip through.
    $pathFwd = $rawPath -replace '\\', '/'
    $destFwd = $rawDest -replace '\\', '/'

    foreach ($seg in ($pathFwd -split '/')) { if ($seg -eq '..') { return (& $fail 'path_dotdot') } }
    foreach ($seg in ($destFwd -split '/')) { if ($seg -eq '..') { return (& $fail 'dest_dotdot') } }

    if ($pathFwd.StartsWith('/') -or $rawPath.StartsWith('\\') -or [System.IO.Path]::IsPathRooted($rawPath)) { return (& $fail 'path_rooted') }
    if ($destFwd.StartsWith('/') -or $rawDest.StartsWith('\\') -or [System.IO.Path]::IsPathRooted($rawDest)) { return (& $fail 'dest_rooted') }

    if (-not $pathFwd.StartsWith('content/', [System.StringComparison]::OrdinalIgnoreCase)) { return (& $fail 'path_not_under_content') }

    $destOk = $false
    foreach ($p in $DestAllowlist) {
        if ($destFwd.StartsWith($p, [System.StringComparison]::OrdinalIgnoreCase)) { $destOk = $true; break }
    }
    if (-not $destOk) { return (& $fail 'dest_not_allowlisted') }

    # Canonical containment (defense in depth: GetFullPath collapses any residual
    # tricks, and we require the result to stay under the expected base).
    try {
        $contentRoot = [System.IO.Path]::GetFullPath((Join-Path $PayloadRoot 'content'))
        $aiboxFull = [System.IO.Path]::GetFullPath($AiboxDir)
        $srcFull = [System.IO.Path]::GetFullPath((Join-Path $PayloadRoot ($pathFwd -replace '/', '\')))
        $dstFull = [System.IO.Path]::GetFullPath((Join-Path $AiboxDir ($destFwd -replace '/', '\')))
    }
    catch { return (& $fail 'invalid_path') }

    $sep = [System.IO.Path]::DirectorySeparatorChar
    if (-not $srcFull.StartsWith($contentRoot.TrimEnd($sep) + $sep, [System.StringComparison]::OrdinalIgnoreCase)) { return (& $fail 'path_escapes') }
    if (-not $dstFull.StartsWith($aiboxFull.TrimEnd($sep) + $sep, [System.StringComparison]::OrdinalIgnoreCase)) { return (& $fail 'dest_escapes') }

    return [pscustomobject]@{ Ok = $true; Reason = ''; SourcePath = $srcFull; DestPath = $dstFull }
}
