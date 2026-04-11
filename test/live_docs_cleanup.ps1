param(
    [string]$BaseUrl = "http://localhost/ai/api",
    [int]$TimeoutSec = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[cleanup] $Message"
}

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Parse-Json {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return $null
    }
    try {
        return $Text | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [object]$Body = $null,
        [int[]]$ExpectedStatus = @(200),
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [int]$RequestTimeoutSec = $TimeoutSec
    )

    $uri = $BaseUrl.TrimEnd('/') + $Path
    $params = @{
        Method = $Method
        Uri = $uri
        TimeoutSec = $RequestTimeoutSec
        Headers = @{ Accept = 'application/json' }
        ErrorAction = 'Stop'
        UseBasicParsing = $true
        WebSession = $Session
    }
    if ($null -ne $Body) {
        $params.ContentType = 'application/json'
        $params.Body = ($Body | ConvertTo-Json -Depth 100 -Compress)
    }

    $status = $null
    $text = ''
    try {
        $response = Invoke-WebRequest @params
        $status = [int]$response.StatusCode
        $text = [string]$response.Content
    } catch {
        if (-not $_.Exception.Response) {
            throw
        }
        $status = [int]$_.Exception.Response.StatusCode
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $text = [string]$_.ErrorDetails.Message
        } else {
            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $text = $reader.ReadToEnd()
                $reader.Dispose()
            } catch {
                $text = ''
            }
        }
    }

    $json = Parse-Json -Text $text
    if ($ExpectedStatus -notcontains $status) {
        $detail = if ($json -and $json.detail) { [string]$json.detail } elseif ($text) { $text } else { 'no response body' }
        throw "${Method} ${Path} returned ${status}: ${detail}"
    }

    return [pscustomobject]@{
        StatusCode = $status
        Text = $text
        Json = $json
    }
}

function Find-ById {
    param(
        [object[]]$Items,
        [string]$Id
    )
    foreach ($item in $Items) {
        if ($item.id -eq $Id) {
            return $item
        }
    }
    return $null
}

function Age-Docs {
    param(
        [string]$DbPath,
        [string]$Timestamp,
        [string[]]$DocIds,
        [string]$ComposeMain,
        [string]$ComposeOverride
    )

    $python = @'
import sqlite3, sys; db_path = sys.argv[1]; stamp = sys.argv[2]; doc_ids = sys.argv[3:]; conn = sqlite3.connect(db_path); cur = conn.cursor(); [cur.execute('UPDATE documents SET created_at=?, updated_at=?, last_accessed_at=? WHERE id=?', (stamp, stamp, stamp, doc_id)) for doc_id in doc_ids]; conn.commit(); conn.close()
'@

    $launchers = @(
        @('py', '-3', '-c', $python, $DbPath, $Timestamp),
        @('python', '-c', $python, $DbPath, $Timestamp)
    )

    foreach ($launcher in $launchers) {
        $command = $launcher[0]
        $args = $launcher[1..($launcher.Length - 1)] + $DocIds
        try {
            & $command @args
            if ($LASTEXITCODE -eq 0) {
                return
            }
        } catch {
            continue
        }
    }

    try {
        $dockerArgs = @('compose', '-f', $ComposeMain, '-f', $ComposeOverride, 'exec', '-T', 'ai-control', 'python', '-c', $python, '/data/db/app.db', $Timestamp) + $DocIds
        & docker @dockerArgs
        if ($LASTEXITCODE -eq 0) {
            return
        }
    } catch {
        # Fall through to the final error.
    }

    throw "Failed to age documents in SQLite. Ensure Python is available as py or python, or run the cleanup stack with Docker access."
}

$timestamp = Get-Date -Format 'yyyyMMddHHmmss'
$userName = "cleanup_user_$timestamp"
$user = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$dbPath = Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\backend-data\db')).Path 'app.db'
$composeMain = Join-Path $PSScriptRoot 'docker-compose.yaml'
$composeOverride = Join-Path $PSScriptRoot 'docker-compose.docs-cleanup.yaml'
$agedStamp = '2000-01-01T00:00:00+00:00'

Write-Step 'Signing up cleanup test user'
[void](Invoke-Api -Method POST -Path '/v1/app/auth/signup' -Body @{ username = $userName; password = 'cleanup-pass'; role = 'user' } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path '/v1/app/auth/login' -Body @{ username = $userName; password = 'cleanup-pass' } -ExpectedStatus @(200) -Session $user)

Write-Step 'Creating one unstarred and one starred document'
$autoDeleteDoc = Invoke-Api -Method POST -Path '/v1/app/docs' -Body @{ title = "Cleanup Delete $timestamp"; type = 'markdown'; content_markdown = 'old body delete' } -ExpectedStatus @(200) -Session $user
$protectedDoc = Invoke-Api -Method POST -Path '/v1/app/docs' -Body @{ title = "Cleanup Starred $timestamp"; type = 'markdown'; content_markdown = 'old body keep' } -ExpectedStatus @(200) -Session $user
$autoDeleteDocId = [string]$autoDeleteDoc.Json.document.id
$protectedDocId = [string]$protectedDoc.Json.document.id
Assert-True (-not [string]::IsNullOrWhiteSpace($autoDeleteDocId)) 'Unstarred cleanup doc did not return an id'
Assert-True (-not [string]::IsNullOrWhiteSpace($protectedDocId)) 'Starred cleanup doc did not return an id'
[void](Invoke-Api -Method POST -Path "/v1/app/docs/$protectedDocId/star" -Body @{ starred = $true } -ExpectedStatus @(200) -Session $user)

Write-Step 'Aging both documents so cleanup considers them expired'
Age-Docs -DbPath $dbPath -Timestamp $agedStamp -DocIds @($autoDeleteDocId, $protectedDocId) -ComposeMain $composeMain -ComposeOverride $composeOverride

Write-Step 'Triggering cleanup via a new document write under forced cleanup settings'
$triggerDoc = Invoke-Api -Method POST -Path '/v1/app/docs' -Body @{ title = "Cleanup Trigger $timestamp"; type = 'markdown'; content_markdown = 'trigger write' } -ExpectedStatus @(200) -Session $user
$triggerDocId = [string]$triggerDoc.Json.document.id
Assert-True (-not [string]::IsNullOrWhiteSpace($triggerDocId)) 'Cleanup trigger doc did not return an id'

Write-Step 'Verifying only the unstarred aged doc was auto deleted'
$activeDocs = Invoke-Api -Method GET -Path '/v1/app/docs' -ExpectedStatus @(200) -Session $user
$allDocs = Invoke-Api -Method GET -Path '/v1/app/docs?include_deleted=true' -ExpectedStatus @(200) -Session $user
Assert-True ($null -eq (Find-ById -Items $activeDocs.Json.documents -Id $autoDeleteDocId)) 'Unstarred expired doc still exists after cleanup'
Assert-True ($null -eq (Find-ById -Items $allDocs.Json.documents -Id $autoDeleteDocId)) 'Unstarred expired doc was trashed instead of auto deleted'
Assert-True ($null -ne (Find-ById -Items $activeDocs.Json.documents -Id $protectedDocId)) 'Starred expired doc should have survived cleanup'
Assert-True ($null -ne (Find-ById -Items $activeDocs.Json.documents -Id $triggerDocId)) 'Trigger document should remain active after cleanup'

Write-Step 'Live docs cleanup test passed'
