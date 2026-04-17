param(
    [string]$BaseOrigin = "http://localhost",
    [string]$BaseUrl = "http://localhost/ai/api",
    [string]$ComposeFile = $(Join-Path $PSScriptRoot "..\stack\docker-compose.yaml"),
    [string]$AdminUsername = $(if ($env:AIBOX_ADMIN_USERNAME) { $env:AIBOX_ADMIN_USERNAME } elseif ($env:ADMIN_USERNAME) { $env:ADMIN_USERNAME } else { "puenteAdmin" }),
    [string]$AdminPassword = $(if ($env:AIBOX_ADMIN_PASSWORD) { $env:AIBOX_ADMIN_PASSWORD } elseif ($env:ADMIN_DEFAULT_PASSWORD) { $env:ADMIN_DEFAULT_PASSWORD } else { "puente123rocks" }),
    [string]$ChromePath = "",
    [int]$TimeoutSec = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[analytics-proof] $Message"
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
        return $Text | ConvertFrom-Json -Depth 100
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
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session = $null,
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
    }
    if ($null -ne $Session) {
        $params.WebSession = $Session
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

function New-Session {
    return New-Object Microsoft.PowerShell.Commands.WebRequestSession
}

function Find-Chrome {
    if ($ChromePath -and (Test-Path $ChromePath)) {
        return $ChromePath
    }
    $candidates = @(
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    throw "Could not find Chrome or Edge. Pass -ChromePath explicitly."
}

function Get-TodayRange {
    $today = (Get-Date).ToString('yyyy-MM-dd')
    return [pscustomobject]@{
        DateFrom = $today
        DateTo = $today
    }
}

function Get-AdminAnalyticsSummary {
    param(
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [string]$DateFrom,
        [string]$DateTo
    )
    $query = "?date_from=$DateFrom&date_to=$DateTo"
    return (Invoke-Api -Method GET -Path "/v1/app/admin/analytics/summary$query" -ExpectedStatus @(200) -Session $Session).Json
}

function Get-DbAnalyticsSnapshot {
    param(
        [string]$DateFrom,
        [string]$DateTo
    )

    $python = "import json,sqlite3,sys; start,end=sys.argv[1],sys.argv[2]; c=sqlite3.connect('/data/db/app.db'); c.row_factory=sqlite3.Row; raw={r['event_type']:int(r['total']) for r in c.execute(""SELECT event_type,SUM(value) AS total FROM usage_events WHERE day_bucket>=? AND day_bucket<=? GROUP BY event_type"",(start,end))}; roll={r['metric_key']:int(r['total']) for r in c.execute(""SELECT metric_key,SUM(value) AS total FROM analytics_daily_rollups WHERE day_bucket>=? AND day_bucket<=? GROUP BY metric_key"",(start,end))}; active=int(c.execute(""SELECT COUNT(*) AS c FROM analytics_daily_active_users WHERE day_bucket>=? AND day_bucket<=?"",(start,end)).fetchone()['c']); print(json.dumps({'raw':raw,'rollups':roll,'active_users':active}))"
    $output = & docker compose -f $ComposeFile exec -T ai-control python -c $python -- $DateFrom $DateTo
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query analytics DB snapshot from ai-control"
    }
    $joined = ($output | Out-String).Trim()
    $json = Parse-Json -Text $joined
    if ($null -eq $json) {
        throw "Failed to parse analytics DB snapshot JSON: $joined"
    }
    return $json
}

function Get-TotalValue {
    param(
        [object]$Totals,
        [string]$Key
    )
    if ($null -eq $Totals) { return 0 }
    $value = $Totals.PSObject.Properties[$Key]
    if ($null -eq $value) { return 0 }
    return [int]$value.Value
}

function Get-ProofValue {
    param(
        [object]$Proof,
        [string]$Key
    )
    if ($null -eq $Proof) { return 0 }
    $visible = $Proof.metrics.PSObject.Properties[$Key]
    if ($null -ne $visible) {
        return [int]$visible.Value
    }
    $daily = $Proof.daily_metrics.PSObject.Properties[$Key]
    if ($null -ne $daily) {
        return [int]$daily.Value
    }
    return 0
}

function Invoke-ChromeDumpDom {
    param(
        [string]$BrowserPath,
        [string]$Url
    )
    $args = @(
        '--headless=new',
        '--disable-gpu',
        '--hide-scrollbars',
        '--window-size=1900,2200',
        '--virtual-time-budget=20000',
        '--dump-dom',
        $Url
    )
    $output = & $BrowserPath @args
    if ($LASTEXITCODE -ne 0) {
        throw "Browser dump-dom failed for $Url"
    }
    return ($output | Out-String)
}

function Capture-AdminProof {
    param(
        [string]$BrowserPath,
        [string]$Label,
        [string]$DateFrom,
        [string]$DateTo,
        [string]$ScreenshotPath
    )

    $encodedLabel = [uri]::EscapeDataString($Label)
    $encodedUser = [uri]::EscapeDataString($AdminUsername)
    $encodedPass = [uri]::EscapeDataString($AdminPassword)
    $url = "{0}/analytics-proof.html?label={1}&admin_username={2}&admin_password={3}&date_from={4}&date_to={5}" -f $BaseOrigin.TrimEnd('/'), $encodedLabel, $encodedUser, $encodedPass, $DateFrom, $DateTo

    $dom = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $dom = Invoke-ChromeDumpDom -BrowserPath $BrowserPath -Url $url
        if ($dom -match 'data-proof-ready="true"') {
            break
        }
        Start-Sleep -Seconds 2
    }
    Assert-True ($dom -match 'data-proof-ready="true"') "Analytics proof page did not become ready for step '$Label'"

    $jsonMatch = [regex]::Match($dom, '<script id="proofJson" type="application/json">([\s\S]*?)</script>')
    Assert-True ($jsonMatch.Success) "Could not extract proof JSON from analytics proof page for step '$Label'"
    $proofJson = [System.Net.WebUtility]::HtmlDecode($jsonMatch.Groups[1].Value)
    $proof = Parse-Json -Text $proofJson
    Assert-True ($null -ne $proof) "Proof JSON was invalid for step '$Label'"

    $args = @(
        '--headless=new',
        '--disable-gpu',
        '--hide-scrollbars',
        '--window-size=1900,2200',
        '--virtual-time-budget=20000',
        "--screenshot=$ScreenshotPath",
        $url
    )
    & $BrowserPath @args | Out-Null
    Assert-True ($LASTEXITCODE -eq 0) "Browser screenshot failed for step '$Label'"
    Assert-True (Test-Path $ScreenshotPath) "Screenshot was not created for step '$Label'"

    return $proof
}

function Assert-AnalyticsConsistency {
    param(
        [object]$ApiSummary,
        [object]$DbSnapshot,
        [string[]]$Keys
    )

    $totals = $ApiSummary.summary.totals
    foreach ($key in $Keys) {
        $apiValue = if ($key -eq 'active_users') { Get-TotalValue -Totals $totals -Key $key } else { Get-TotalValue -Totals $totals -Key $key }
        $dbRollup = if ($key -eq 'active_users') { [int]$DbSnapshot.active_users } else {
            $prop = $DbSnapshot.rollups.PSObject.Properties[$key]
            if ($null -eq $prop) { 0 } else { [int]$prop.Value }
        }
        Assert-True ($apiValue -eq $dbRollup) "Admin API total for '$key' ($apiValue) did not match DB rollup ($dbRollup)"
        if ($key -ne 'active_users') {
            $rawProp = $DbSnapshot.raw.PSObject.Properties[$key]
            $rawValue = if ($null -eq $rawProp) { 0 } else { [int]$rawProp.Value }
            Assert-True ($rawValue -eq $dbRollup) "Raw usage total for '$key' ($rawValue) did not match DB rollup ($dbRollup)"
        }
    }
}

function Assert-MetricsIncreased {
    param(
        [object]$BeforeApi,
        [object]$AfterApi,
        [object]$BeforeProof,
        [object]$AfterProof,
        [string[]]$Keys,
        [string[]]$VisibleKeys = @()
    )

    foreach ($key in $Keys) {
        $beforeValue = Get-TotalValue -Totals $BeforeApi.summary.totals -Key $key
        $afterValue = Get-TotalValue -Totals $AfterApi.summary.totals -Key $key
        Assert-True ($afterValue -gt $beforeValue) "Expected API metric '$key' to increase, but it went from $beforeValue to $afterValue"
    }
    foreach ($key in $VisibleKeys) {
        $beforeVisible = Get-ProofValue -Proof $BeforeProof -Key $key
        $afterVisible = Get-ProofValue -Proof $AfterProof -Key $key
        Assert-True ($afterVisible -gt $beforeVisible) "Expected visible admin metric '$key' to increase, but it went from $beforeVisible to $afterVisible"
    }
}

function New-CheckpointRecord {
    param(
        [string]$Step,
        [string]$Screenshot,
        [object]$ApiSummary,
        [object]$DbSnapshot,
        [object]$ProofSnapshot
    )
    return [pscustomobject]@{
        step = $Step
        screenshot = $Screenshot
        api_summary = $ApiSummary
        db_snapshot = $DbSnapshot
        proof_snapshot = $ProofSnapshot
    }
}

$browserPath = Find-Chrome
$range = Get-TodayRange
$timestamp = Get-Date -Format 'yyyyMMddHHmmss'
$userName = "analytics_user_$timestamp"
$userPassword = "analytics-pass"
$outputRoot = Join-Path $PSScriptRoot ("artifacts\analytics-proof\" + $timestamp)
$null = New-Item -ItemType Directory -Force -Path $outputRoot

$admin = New-Session
$user = New-Session
$checkpoints = New-Object System.Collections.Generic.List[object]

Write-Step "Using browser: $browserPath"
Write-Step "Output directory: $outputRoot"

Write-Step "Logging in as admin"
[void](Invoke-Api -Method POST -Path '/v1/app/auth/login' -Body @{ username = $AdminUsername; password = $AdminPassword } -ExpectedStatus @(200) -Session $admin)

Write-Step "Capturing baseline"
$baselineApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$baselineDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$baselineShot = Join-Path $outputRoot '01-baseline.png'
$baselineProof = Capture-AdminProof -BrowserPath $browserPath -Label '01 Baseline' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $baselineShot
Assert-AnalyticsConsistency -ApiSummary $baselineApi -DbSnapshot $baselineDb -Keys @('active_users','accounts_created','logins_succeeded','chat_sessions_created','chat_completion_requested','chat_completion_succeeded','chat_completion_stopped','chat_messages_sent','documents_created','documents_updated','documents_deleted','documents_restored','wiki_shell_open','learn_shell_open')
$checkpoints.Add((New-CheckpointRecord -Step 'baseline' -Screenshot $baselineShot -ApiSummary $baselineApi -DbSnapshot $baselineDb -ProofSnapshot $baselineProof)) | Out-Null

Write-Step "Creating account"
[void](Invoke-Api -Method POST -Path '/v1/app/auth/signup' -Body @{ username = $userName; password = $userPassword; role = 'user' } -ExpectedStatus @(200) -Session $user)
Start-Sleep -Milliseconds 600
$afterSignupApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterSignupDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterSignupShot = Join-Path $outputRoot '02-after-signup.png'
$afterSignupProof = Capture-AdminProof -BrowserPath $browserPath -Label '02 After Signup' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $afterSignupShot
Assert-AnalyticsConsistency -ApiSummary $afterSignupApi -DbSnapshot $afterSignupDb -Keys @('active_users','accounts_created')
Assert-MetricsIncreased -BeforeApi $baselineApi -AfterApi $afterSignupApi -BeforeProof $baselineProof -AfterProof $afterSignupProof -Keys @('accounts_created','active_users') -VisibleKeys @('accounts_created','active_users')
$checkpoints.Add((New-CheckpointRecord -Step 'after_signup' -Screenshot $afterSignupShot -ApiSummary $afterSignupApi -DbSnapshot $afterSignupDb -ProofSnapshot $afterSignupProof)) | Out-Null

Write-Step "Logging in as new user"
[void](Invoke-Api -Method POST -Path '/v1/app/auth/login' -Body @{ username = $userName; password = $userPassword } -ExpectedStatus @(200) -Session $user)
Start-Sleep -Milliseconds 600
$afterLoginApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterLoginDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterLoginShot = Join-Path $outputRoot '03-after-login.png'
$afterLoginProof = Capture-AdminProof -BrowserPath $browserPath -Label '03 After Login' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $afterLoginShot
Assert-AnalyticsConsistency -ApiSummary $afterLoginApi -DbSnapshot $afterLoginDb -Keys @('active_users','accounts_created','logins_succeeded')
Assert-MetricsIncreased -BeforeApi $afterSignupApi -AfterApi $afterLoginApi -BeforeProof $afterSignupProof -AfterProof $afterLoginProof -Keys @('logins_succeeded') -VisibleKeys @('logins')
$checkpoints.Add((New-CheckpointRecord -Step 'after_login' -Screenshot $afterLoginShot -ApiSummary $afterLoginApi -DbSnapshot $afterLoginDb -ProofSnapshot $afterLoginProof)) | Out-Null

Write-Step "Sending successful chat"
$completion = Invoke-Api -Method POST -Path '/v1/app/chat/completions' -Body @{
    model = 'qwen2.5-7b-instruct-q4_0'
    messages = @(@{ role = 'user'; content = 'Reply with the single word TEST.' })
    stream = $false
    retrieval_enabled = $false
} -ExpectedStatus @(200) -Session $user -RequestTimeoutSec $TimeoutSec
$chatId = [string]$completion.Json.chat_id
Assert-True (-not [string]::IsNullOrWhiteSpace($chatId)) 'Successful chat did not return a chat_id'
Start-Sleep -Milliseconds 800
$afterChatApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterChatDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterChatShot = Join-Path $outputRoot '04-after-chat-success.png'
$afterChatProof = Capture-AdminProof -BrowserPath $browserPath -Label '04 After Chat Success' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $afterChatShot
Assert-AnalyticsConsistency -ApiSummary $afterChatApi -DbSnapshot $afterChatDb -Keys @('chat_sessions_created','chat_completion_requested','chat_completion_succeeded','chat_messages_sent')
Assert-MetricsIncreased -BeforeApi $afterLoginApi -AfterApi $afterChatApi -BeforeProof $afterLoginProof -AfterProof $afterChatProof -Keys @('chat_sessions_created','chat_completion_requested','chat_completion_succeeded','chat_messages_sent') -VisibleKeys @('chat_sessions_created','chat_completion_succeeded')
$checkpoints.Add((New-CheckpointRecord -Step 'after_chat_success' -Screenshot $afterChatShot -ApiSummary $afterChatApi -DbSnapshot $afterChatDb -ProofSnapshot $afterChatProof)) | Out-Null

Write-Step "Recording stopped chat"
[void](Invoke-Api -Method POST -Path '/v1/app/analytics/events' -Body @{ event_name = 'chat_completion_stopped'; surface = 'chat'; metadata = @{ chat_id = $chatId; source = 'proof-script' } } -ExpectedStatus @(200) -Session $user)
Start-Sleep -Milliseconds 600
$afterStopApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterStopDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterStopShot = Join-Path $outputRoot '05-after-chat-stop.png'
$afterStopProof = Capture-AdminProof -BrowserPath $browserPath -Label '05 After Chat Stop' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $afterStopShot
Assert-AnalyticsConsistency -ApiSummary $afterStopApi -DbSnapshot $afterStopDb -Keys @('chat_completion_stopped')
Assert-MetricsIncreased -BeforeApi $afterChatApi -AfterApi $afterStopApi -BeforeProof $afterChatProof -AfterProof $afterStopProof -Keys @('chat_completion_stopped') -VisibleKeys @('chat_completion_stopped')
$checkpoints.Add((New-CheckpointRecord -Step 'after_chat_stop' -Screenshot $afterStopShot -ApiSummary $afterStopApi -DbSnapshot $afterStopDb -ProofSnapshot $afterStopProof)) | Out-Null

Write-Step "Running document workflow"
$docCreate = Invoke-Api -Method POST -Path '/v1/app/docs' -Body @{ title = "Analytics Doc $timestamp"; type = 'markdown'; content_markdown = '# Analytics proof' } -ExpectedStatus @(200) -Session $user
$docId = [string]$docCreate.Json.document.id
Assert-True (-not [string]::IsNullOrWhiteSpace($docId)) 'Document creation did not return an id'
[void](Invoke-Api -Method GET -Path "/v1/app/docs/$docId" -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method PATCH -Path "/v1/app/docs/$docId" -Body @{ title = "Analytics Doc $timestamp Updated"; content_markdown = 'Updated proof body' } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method DELETE -Path "/v1/app/docs/$docId" -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path "/v1/app/docs/$docId/restore" -ExpectedStatus @(200) -Session $user)
Start-Sleep -Milliseconds 800
$afterDocsApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterDocsDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterDocsShot = Join-Path $outputRoot '06-after-docs.png'
$afterDocsProof = Capture-AdminProof -BrowserPath $browserPath -Label '06 After Docs' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $afterDocsShot
Assert-AnalyticsConsistency -ApiSummary $afterDocsApi -DbSnapshot $afterDocsDb -Keys @('documents_created','documents_opened','documents_updated','documents_deleted','documents_restored')
Assert-MetricsIncreased -BeforeApi $afterStopApi -AfterApi $afterDocsApi -BeforeProof $afterStopProof -AfterProof $afterDocsProof -Keys @('documents_created','documents_updated','documents_deleted','documents_restored') -VisibleKeys @('documents_created','documents_updated','documents_deleted','documents_restored')
$checkpoints.Add((New-CheckpointRecord -Step 'after_docs' -Screenshot $afterDocsShot -ApiSummary $afterDocsApi -DbSnapshot $afterDocsDb -ProofSnapshot $afterDocsProof)) | Out-Null

Write-Step "Recording wiki activity"
[void](Invoke-Api -Method POST -Path '/v1/app/analytics/events' -Body @{ event_name = 'wiki_shell_open'; surface = 'wiki'; metadata = @{ href = '/wiki-app/' } } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path '/v1/app/analytics/events' -Body @{ event_name = 'wiki_open_full_page'; surface = 'wiki'; metadata = @{ href = '/wiki/' } } -ExpectedStatus @(200) -Session $user)
Start-Sleep -Milliseconds 600
$afterWikiApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterWikiDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterWikiShot = Join-Path $outputRoot '07-after-wiki.png'
$afterWikiProof = Capture-AdminProof -BrowserPath $browserPath -Label '07 After Wiki' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $afterWikiShot
Assert-AnalyticsConsistency -ApiSummary $afterWikiApi -DbSnapshot $afterWikiDb -Keys @('wiki_shell_open','wiki_open_full_page')
Assert-MetricsIncreased -BeforeApi $afterDocsApi -AfterApi $afterWikiApi -BeforeProof $afterDocsProof -AfterProof $afterWikiProof -Keys @('wiki_shell_open') -VisibleKeys @('wiki_shell_open')
$checkpoints.Add((New-CheckpointRecord -Step 'after_wiki' -Screenshot $afterWikiShot -ApiSummary $afterWikiApi -DbSnapshot $afterWikiDb -ProofSnapshot $afterWikiProof)) | Out-Null

Write-Step "Recording learn activity"
[void](Invoke-Api -Method POST -Path '/v1/app/analytics/events' -Body @{ event_name = 'learn_shell_open'; surface = 'learn'; metadata = @{ href = '/learn-app/' } } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path '/v1/app/analytics/events' -Body @{ event_name = 'learn_open_full_page'; surface = 'learn'; metadata = @{ href = '/kolibri/' } } -ExpectedStatus @(200) -Session $user)
Start-Sleep -Milliseconds 600
$afterLearnApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterLearnDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterLearnShot = Join-Path $outputRoot '08-after-learn.png'
$afterLearnProof = Capture-AdminProof -BrowserPath $browserPath -Label '08 After Learn' -DateFrom $range.DateFrom -DateTo $range.DateTo -ScreenshotPath $afterLearnShot
Assert-AnalyticsConsistency -ApiSummary $afterLearnApi -DbSnapshot $afterLearnDb -Keys @('learn_shell_open','learn_open_full_page')
Assert-MetricsIncreased -BeforeApi $afterWikiApi -AfterApi $afterLearnApi -BeforeProof $afterWikiProof -AfterProof $afterLearnProof -Keys @('learn_shell_open') -VisibleKeys @('learn_shell_open')
$checkpoints.Add((New-CheckpointRecord -Step 'after_learn' -Screenshot $afterLearnShot -ApiSummary $afterLearnApi -DbSnapshot $afterLearnDb -ProofSnapshot $afterLearnProof)) | Out-Null

Write-Step "Recording portal tool click"
[void](Invoke-Api -Method POST -Path '/v1/app/analytics/events' -Body @{ event_name = 'portal_tool_open'; surface = 'portal'; metadata = @{ tool_id = 'chat'; href = '/ai/' } } -ExpectedStatus @(200) -Session $user)
Start-Sleep -Milliseconds 400
$afterPortalApi = Get-AdminAnalyticsSummary -Session $admin -DateFrom $range.DateFrom -DateTo $range.DateTo
$afterPortalDb = Get-DbAnalyticsSnapshot -DateFrom $range.DateFrom -DateTo $range.DateTo
Assert-AnalyticsConsistency -ApiSummary $afterPortalApi -DbSnapshot $afterPortalDb -Keys @('portal_tool_open')

Write-Step "Verifying export"
$jsonExport = (Invoke-Api -Method GET -Path "/v1/app/admin/analytics/export?date_from=$($range.DateFrom)&date_to=$($range.DateTo)&export_format=json" -ExpectedStatus @(200) -Session $admin).Json
Assert-True ($null -ne $jsonExport.summary) 'JSON analytics export did not return summary data'
Assert-True ((Get-TotalValue -Totals $jsonExport.summary.totals -Key 'learn_shell_open') -eq (Get-TotalValue -Totals $afterLearnApi.summary.totals -Key 'learn_shell_open')) 'JSON export totals did not match admin summary'
$csvExport = Invoke-Api -Method GET -Path "/v1/app/admin/analytics/export?date_from=$($range.DateFrom)&date_to=$($range.DateTo)&export_format=csv" -ExpectedStatus @(200) -Session $admin
Assert-True ($csvExport.Text -match '^day,') 'CSV analytics export did not include a header row'

$manifestPath = Join-Path $outputRoot 'proof-manifest.json'
$manifest = [pscustomobject]@{
    generated_at = (Get-Date).ToString('o')
    base_origin = $BaseOrigin
    base_url = $BaseUrl
    date_from = $range.DateFrom
    date_to = $range.DateTo
    screenshots = $checkpoints
    final_api_summary = $afterLearnApi
    final_db_snapshot = $afterLearnDb
    final_export_summary = $jsonExport.summary
}
$manifest | ConvertTo-Json -Depth 100 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Step "Analytics proof complete"
Write-Step "Screenshots and manifest saved to $outputRoot"
