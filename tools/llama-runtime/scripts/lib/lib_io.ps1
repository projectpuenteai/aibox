function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [AllowNull()][AllowEmptyCollection()][AllowEmptyString()][string[]]$Lines = @()
    )
    if ($null -eq $Lines) {
        $Lines = @()
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, [string[]]$Lines, $utf8NoBom)
}

function Move-FileAtomic {
    param(
        [Parameter(Mandatory=$true)][string]$Source,
        [Parameter(Mandatory=$true)][string]$Destination
    )
    Move-Item -LiteralPath $Source -Destination $Destination -Force
}
