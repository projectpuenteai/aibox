# Runs all Pester tests in this directory and exits with the Pester exit code.
$result = Invoke-Pester -Path $PSScriptRoot -Output Detailed -PassThru
exit $result.FailedCount
