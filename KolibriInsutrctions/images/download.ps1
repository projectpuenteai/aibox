# Download Kolibri documentation screenshots for the teacher guide.
# Run from the images/ directory:
#   pwsh ./download.ps1      (PowerShell 7+)
#   powershell ./download.ps1 (Windows PowerShell 5+)
#
# Pass -Force to re-download even if local files exist.

param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$base = "https://kolibri.readthedocs.io/en/latest/_images"
$images = @(
    "create-account.png",
    "manage-users.png",
    "coach-type.png",
    "groups-home.png",
    "learner-groups.png",
    "lessons-home.png",
    "lesson-visible.png",
    "quizzes-home.png",
    "coach-home.png",
    "learners-home.png"
)

foreach ($name in $images) {
    if ((Test-Path $name) -and -not $Force) {
        Write-Host "  ok    $name (already present)"
        continue
    }
    Write-Host "  fetch $name"
    try {
        Invoke-WebRequest -Uri "$base/$name" -OutFile $name -UseBasicParsing
    } catch {
        Write-Warning "  FAIL  $name : $($_.Exception.Message)"
    }
}

Write-Host "All done."
