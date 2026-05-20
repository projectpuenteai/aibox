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
    }
}
