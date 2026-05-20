# Tests for the model-integrity helpers extracted into lib\lib_model.ps1.
# (Option B: the side-effect-free helpers in lib_model.ps1 are dot-sourced and
# called directly here. preflight_llama_runtime.ps1 itself is NOT dot-sourced
# because it has top-level execution that would touch Docker, .env files, etc.)

BeforeAll {
    . (Join-Path $PSScriptRoot '..\lib\lib_model.ps1')
}

Describe 'lib_model: Test-ModelDirectoryIntegrity' {
    Context '-RequiredFiles' {
        It 'fails when the directory does not exist' {
            $missing = Join-Path $env:TEMP "preflight-test-missing-$(New-Guid)"
            $result = Test-ModelDirectoryIntegrity -Path $missing -RequireContent -RequiredFiles @('config.json')
            $result.Ok | Should -BeFalse
            $result.Reason | Should -BeExactly 'directory_missing'
        }

        It 'fails when the directory is empty and -RequireContent is set' {
            $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
            try {
                $result = Test-ModelDirectoryIntegrity -Path $tmp.FullName -RequireContent
                $result.Ok | Should -BeFalse
                $result.Reason | Should -BeExactly 'directory_empty'
            } finally {
                Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
            }
        }

        It 'fails when a required file is missing (stub-file scenario)' {
            $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
            try {
                # Only an unrelated file is present — directory is "non-empty"
                # but the required tokenizer.json is missing. This is exactly
                # the half-finished-pull case §2.7 fixes.
                New-Item -ItemType File -Path (Join-Path $tmp.FullName 'unrelated.txt') | Out-Null
                $result = Test-ModelDirectoryIntegrity -Path $tmp.FullName -RequireContent -RequiredFiles @('config.json','tokenizer.json')
                $result.Ok | Should -BeFalse
                $result.Reason | Should -BeExactly 'required_file_missing'
                $result.Message | Should -Match 'config\.json'
            } finally {
                Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
            }
        }

        It 'passes when all required files are present' {
            $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
            try {
                New-Item -ItemType File -Path (Join-Path $tmp.FullName 'config.json') | Out-Null
                New-Item -ItemType File -Path (Join-Path $tmp.FullName 'tokenizer.json') | Out-Null
                $result = Test-ModelDirectoryIntegrity -Path $tmp.FullName -RequireContent -RequiredFiles @('config.json','tokenizer.json')
                $result.Ok | Should -BeTrue
                $result.Reason | Should -BeExactly ''
            } finally {
                Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
            }
        }

        It 'passes with no RequiredFiles when directory just exists' {
            $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
            try {
                $result = Test-ModelDirectoryIntegrity -Path $tmp.FullName
                $result.Ok | Should -BeTrue
            } finally {
                Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
            }
        }
    }
}

Describe 'lib_model: Get-GgufShardPlan' {
    It 'parses a multi-shard filename' {
        $plan = Get-GgufShardPlan -FileName 'qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf'
        $plan.IsSharded | Should -BeTrue
        $plan.Prefix    | Should -BeExactly 'qwen2.5-7b-instruct-q4_0'
        $plan.Total     | Should -Be 2
    }

    It 'parses a different shard total' {
        $plan = Get-GgufShardPlan -FileName 'model-name-00001-of-00005.gguf'
        $plan.IsSharded | Should -BeTrue
        $plan.Total     | Should -Be 5
    }

    It 'reports single-file models as not sharded' {
        $plan = Get-GgufShardPlan -FileName 'tinyllama-q4.gguf'
        $plan.IsSharded | Should -BeFalse
    }

    It 'does not match malformed shard suffixes' {
        $plan = Get-GgufShardPlan -FileName 'foo-1-of-2.gguf'  # only 1-digit indices
        $plan.IsSharded | Should -BeFalse
    }
}

Describe 'lib_model: Test-GgufShardSet' {
    It 'passes for a single-file model when the file exists' {
        $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
        try {
            New-Item -ItemType File -Path (Join-Path $tmp.FullName 'tinyllama-q4.gguf') | Out-Null
            $result = Test-GgufShardSet -Directory $tmp.FullName -FileName 'tinyllama-q4.gguf'
            $result.Ok | Should -BeTrue
        } finally {
            Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
        }
    }

    It 'fails for a single-file model when the file is missing' {
        $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
        try {
            $result = Test-GgufShardSet -Directory $tmp.FullName -FileName 'tinyllama-q4.gguf'
            $result.Ok | Should -BeFalse
            $result.Reason | Should -BeExactly 'model_missing'
        } finally {
            Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
        }
    }

    It 'passes for a multi-shard model when all shards are present' {
        $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
        try {
            New-Item -ItemType File -Path (Join-Path $tmp.FullName 'model-x-00001-of-00002.gguf') | Out-Null
            New-Item -ItemType File -Path (Join-Path $tmp.FullName 'model-x-00002-of-00002.gguf') | Out-Null
            $result = Test-GgufShardSet -Directory $tmp.FullName -FileName 'model-x-00001-of-00002.gguf'
            $result.Ok | Should -BeTrue
        } finally {
            Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
        }
    }

    It 'fails for a multi-shard model when one shard is missing' {
        $tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "preflight-test-$(New-Guid)")
        try {
            # Only shard 1 of 2 — exactly the half-finished-pull case.
            New-Item -ItemType File -Path (Join-Path $tmp.FullName 'model-x-00001-of-00002.gguf') | Out-Null
            $result = Test-GgufShardSet -Directory $tmp.FullName -FileName 'model-x-00001-of-00002.gguf'
            $result.Ok | Should -BeFalse
            $result.Reason | Should -BeExactly 'model_shard_mismatch'
            $result.Message | Should -Match 'expected 2, found 1'
        } finally {
            Remove-Item -LiteralPath $tmp.FullName -Recurse -Force
        }
    }

    It 'fails for a multi-shard model when the directory is missing' {
        $missing = Join-Path $env:TEMP "preflight-test-missing-$(New-Guid)"
        $result = Test-GgufShardSet -Directory $missing -FileName 'model-x-00001-of-00002.gguf'
        $result.Ok | Should -BeFalse
        $result.Reason | Should -BeExactly 'model_dir_missing'
    }
}
