# Helper invoked by release.yml to code-sign artifacts via Azure Trusted
# Signing. Lives next to the workflows so the YAML stays readable.
#
# Usage:
#   sign-files.ps1 -Paths @("dist/foo.exe", "dist/bar.exe")
#
# Required env vars (set as repo secrets):
#   AZURE_SIGNING_TENANT
#   AZURE_SIGNING_CLIENT_ID
#   AZURE_SIGNING_CLIENT_SECRET
#   AZURE_SIGNING_ACCOUNT       — e.g. "puente-trusted-signing"
#   AZURE_SIGNING_PROFILE       — e.g. "puente-ev-codesigning"
#   AZURE_SIGNING_ENDPOINT      — e.g. "https://eus.codesigning.azure.net/"
#
# If any required env var is missing this script is a no-op so the
# release pipeline can still produce unsigned artifacts for dry runs.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Paths
)

$required = @(
    "AZURE_SIGNING_TENANT",
    "AZURE_SIGNING_CLIENT_ID",
    "AZURE_SIGNING_CLIENT_SECRET",
    "AZURE_SIGNING_ACCOUNT",
    "AZURE_SIGNING_PROFILE",
    "AZURE_SIGNING_ENDPOINT"
)
foreach ($r in $required) {
    if (-not (Get-Item env:$r -ErrorAction SilentlyContinue)) {
        Write-Warning "$r not set; skipping code signing."
        return
    }
}

# Install Microsoft.Trusted.Signing.Client if not already present
$tool = "Microsoft.Trusted.Signing.Client"
$toolPath = "$env:USERPROFILE\.dotnet\tools\$tool"
if (-not (Test-Path "$toolPath")) {
    Write-Host "Installing $tool..."
    dotnet tool install --global $tool
    if ($LASTEXITCODE -ne 0) { throw "Failed to install $tool" }
}
$dlibPath = Get-ChildItem "$env:USERPROFILE\.dotnet\tools" -Recurse -Filter "Azure.CodeSigning.Dlib.dll" | Select-Object -First 1 -ExpandProperty FullName
if (-not $dlibPath) { throw "Azure.CodeSigning.Dlib.dll not found after $tool installation" }

$metadata = @{
    "Endpoint"             = $env:AZURE_SIGNING_ENDPOINT
    "CodeSigningAccountName" = $env:AZURE_SIGNING_ACCOUNT
    "CertificateProfileName" = $env:AZURE_SIGNING_PROFILE
    "CorrelationId"        = (New-Guid).ToString()
} | ConvertTo-Json -Compress

$tmpMeta = Join-Path $env:RUNNER_TEMP "trusted-signing.json"
$metadata | Set-Content -Path $tmpMeta -Encoding utf8

$signtool = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe' -ErrorAction SilentlyContinue |
    Sort-Object { [version]($_.Directory.Parent.Name) } -Descending |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $signtool) { throw "signtool.exe not found in any Windows Kits 10 SDK" }

foreach ($p in $Paths) {
    Write-Host "Signing $p"
    & $signtool sign `
        /v `
        /debug `
        /fd SHA256 `
        /tr "http://timestamp.acs.microsoft.com" `
        /td SHA256 `
        /dlib $dlibPath `
        /dmdf $tmpMeta `
        $p

    if ($LASTEXITCODE -ne 0) {
        Write-Error "signtool failed on $p (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
}
