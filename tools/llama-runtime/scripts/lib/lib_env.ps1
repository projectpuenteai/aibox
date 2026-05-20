function Get-DotEnvMap {
    param([Parameter(Mandatory=$true)][string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $eq = $trimmed.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $trimmed.Substring(0, $eq).Trim()
        $value = $trimmed.Substring($eq + 1).Trim()
        if ($value.StartsWith('"') -and $value.EndsWith('"') -and $value.Length -ge 2) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $map[$key] = $value
    }
    return $map
}

function Read-EnvValue {
    param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)][string]$Key)
    $map = Get-DotEnvMap -Path $Path
    if ($map.ContainsKey($Key)) { return $map[$Key] }
    return $null
}
