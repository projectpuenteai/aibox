BeforeAll {
    . (Join-Path $PSScriptRoot '..\lib\lib_docker.ps1')
}

Describe 'lib_docker' {
    Context 'Test-DockerPruneArgs' {
        It 'accepts container prune --force' {
            Test-DockerPruneArgs -ArgList @('container','prune','--force') | Should -BeTrue
        }
        It 'rejects system prune' {
            Test-DockerPruneArgs -ArgList @('system','prune') | Should -BeFalse
        }
        It 'rejects container prune --volumes' {
            Test-DockerPruneArgs -ArgList @('container','prune','--volumes') | Should -BeFalse
        }
        It 'rejects empty arg list' {
            Test-DockerPruneArgs -ArgList @() | Should -BeFalse
        }
        It 'rejects image prune --all' {
            Test-DockerPruneArgs -ArgList @('image','prune','--all') | Should -BeFalse
        }
    }

    Context 'Test-LocalDockerImage' {
        It 'returns true when docker image inspect succeeds' -Skip:(-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            # If docker is reachable, picking an image known to exist locally tests the primary path.
            # caddy:2 was pulled for volume probing; if it's local, this should return $true.
            $caddyDigest = "caddy:2@sha256:ec18ee54aab3315c22e25f3b2babda73ff8007d39b13b3bd1bfffa2f0444c7d9"
            $localCaddyExists = $false
            & docker image inspect $caddyDigest 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $localCaddyExists = $true }
            if (-not $localCaddyExists) {
                Set-ItResult -Skipped -Because "caddy:2 reference not local on this machine"
                return
            }
            Test-LocalDockerImage -ImageRef $caddyDigest | Should -BeTrue
        }

        It 'returns false for an obviously fake digest' -Skip:(-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            $fake = "aibox/does-not-exist@sha256:0000000000000000000000000000000000000000000000000000000000000000"
            Test-LocalDockerImage -ImageRef $fake | Should -BeFalse
        }
    }
}
