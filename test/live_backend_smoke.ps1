# This smoke test exercises the live API end-to-end: auth, admin actions, docs, folders, chats, and one completion request.
param(
    [string]$BaseUrl = "http://localhost/ai/api",
    [string]$AdminUsername = $(if ($env:AIBOX_ADMIN_USERNAME) { $env:AIBOX_ADMIN_USERNAME } elseif ($env:ADMIN_USERNAME) { $env:ADMIN_USERNAME } else { "puenteAdmin" }),
    [string]$AdminPassword = $(if ($env:AIBOX_ADMIN_PASSWORD) { $env:AIBOX_ADMIN_PASSWORD } elseif ($env:ADMIN_DEFAULT_PASSWORD) { $env:ADMIN_DEFAULT_PASSWORD } else { "puente123rocks" }),
    [int]$ProbeCount = 100,
    [int]$TimeoutSec = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[smoke] $Message"
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
    $headers = $null
    try {
        $response = Invoke-WebRequest @params
        $status = [int]$response.StatusCode
        $text = [string]$response.Content
        $headers = $response.Headers
    } catch {
        if (-not $_.Exception.Response) {
            throw
        }
        $status = [int]$_.Exception.Response.StatusCode
        $headers = $_.Exception.Response.Headers
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
        Headers = $headers
    }
}

function Probe-Path {
    param(
        [string]$Name,
        [string]$Method,
        [string]$Path,
        [int]$ExpectedStatus,
        [int]$Count,
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session = $null
    )

    Write-Step "Probing $Name $Count times"
    $failures = New-Object System.Collections.Generic.List[string]
    for ($i = 1; $i -le $Count; $i++) {
        try {
            [void](Invoke-Api -Method $Method -Path $Path -ExpectedStatus @($ExpectedStatus) -Session $Session -RequestTimeoutSec 20)
        } catch {
            $failures.Add("#$i $($_.Exception.Message)")
        }
    }
    Assert-True ($failures.Count -eq 0) ("Probe failed for {0}: {1}" -f $Name, ($failures -join '; '))
}

function New-Session {
    return New-Object Microsoft.PowerShell.Commands.WebRequestSession
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

$timestamp = Get-Date -Format 'yyyyMMddHHmmss'
$userName = "smoke_user_$timestamp"
$folderBase = "Smoke Folder $timestamp"
$docTitle = "Smoke Doc $timestamp"
$chatTitle = "Smoke Chat $timestamp"
$modelName = 'qwen2.5-7b-instruct-q4_0'

$anon = New-Session
$user = New-Session
$admin = New-Session

Write-Step 'Running unauthenticated checks'
[void](Invoke-Api -Method GET -Path '/health' -ExpectedStatus @(200) -Session $anon)
[void](Invoke-Api -Method GET -Path '/ready' -ExpectedStatus @(200) -Session $anon)
[void](Invoke-Api -Method GET -Path '/status' -ExpectedStatus @(200) -Session $anon)
[void](Invoke-Api -Method GET -Path '/v1/app/auth/me' -ExpectedStatus @(401) -Session $anon)
[void](Invoke-Api -Method GET -Path '/v1/admin/health' -ExpectedStatus @(200) -Session $anon)
[void](Invoke-Api -Method GET -Path '/v1/admin/status' -ExpectedStatus @(200) -Session $anon)

Probe-Path -Name 'health' -Method GET -Path '/health' -ExpectedStatus 200 -Count $ProbeCount -Session $anon
Probe-Path -Name 'auth/me unauthenticated' -Method GET -Path '/v1/app/auth/me' -ExpectedStatus 401 -Count $ProbeCount -Session $anon
Probe-Path -Name 'admin health' -Method GET -Path '/v1/admin/health' -ExpectedStatus 200 -Count $ProbeCount -Session $anon
Probe-Path -Name 'admin status' -Method GET -Path '/v1/admin/status' -ExpectedStatus 200 -Count $ProbeCount -Session $anon

Write-Step 'Running admin flow'
[void](Invoke-Api -Method POST -Path '/v1/app/auth/login' -Body @{ username = $AdminUsername; password = $AdminPassword } -ExpectedStatus @(200) -Session $admin)
$adminMe = Invoke-Api -Method GET -Path '/v1/app/auth/me' -ExpectedStatus @(200) -Session $admin
Assert-True ($adminMe.Json.user.role -eq 'admin') 'Admin session did not authenticate as admin'
$adminUsers = Invoke-Api -Method GET -Path '/v1/app/admin/users' -ExpectedStatus @(200) -Session $admin
Assert-True (($adminUsers.Json.users | Measure-Object).Count -ge 1) 'Admin users endpoint returned no users'
[void](Invoke-Api -Method GET -Path '/v1/app/admin/storage-insights' -ExpectedStatus @(200) -Session $admin)
[void](Invoke-Api -Method GET -Path '/v1/app/admin/security-events?limit=20' -ExpectedStatus @(200) -Session $admin)
[void](Invoke-Api -Method GET -Path '/v1/admin/ai-enabled' -ExpectedStatus @(200) -Session $admin)

Write-Step 'Running user auth flow'
[void](Invoke-Api -Method POST -Path '/v1/app/auth/signup' -Body @{ username = $userName; password = 'smoke-pass'; role = 'user' } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path '/v1/app/auth/login' -Body @{ username = $userName; password = 'smoke-pass' } -ExpectedStatus @(200) -Session $user)
$userMe = Invoke-Api -Method GET -Path '/v1/app/auth/me' -ExpectedStatus @(200) -Session $user
Assert-True ($userMe.Json.user.username -eq $userName) 'User auth/me returned the wrong username'
[void](Invoke-Api -Method POST -Path '/v1/app/auth/logout' -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method GET -Path '/v1/app/auth/me' -ExpectedStatus @(401) -Session $user)
[void](Invoke-Api -Method POST -Path '/v1/app/auth/login' -Body @{ username = $userName; password = 'smoke-pass' } -ExpectedStatus @(200) -Session $user)

Write-Step 'Running docs flow'
$docCreate = Invoke-Api -Method POST -Path '/v1/app/docs' -Body @{ title = $docTitle; type = 'markdown'; content_markdown = '# Smoke`nhello' } -ExpectedStatus @(200) -Session $user
$docId = [string]$docCreate.Json.document.id
Assert-True (-not [string]::IsNullOrWhiteSpace($docId)) 'Document creation did not return an id'
$docsList = Invoke-Api -Method GET -Path '/v1/app/docs' -ExpectedStatus @(200) -Session $user
Assert-True ($null -ne (Find-ById -Items $docsList.Json.documents -Id $docId)) 'Created document not found in active list'
$docGet = Invoke-Api -Method GET -Path "/v1/app/docs/$docId" -ExpectedStatus @(200) -Session $user
Assert-True ($docGet.Json.document.title -eq $docTitle) 'Document get returned the wrong title'
[void](Invoke-Api -Method PATCH -Path "/v1/app/docs/$docId" -Body @{ title = "$docTitle Updated"; content_markdown = 'updated body' } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path "/v1/app/docs/$docId/star" -Body @{ starred = $true } -ExpectedStatus @(200) -Session $user)
$starredDelete = Invoke-Api -Method DELETE -Path "/v1/app/docs/$docId" -ExpectedStatus @(409) -Session $user
Assert-True ($starredDelete.Json.detail -eq 'Starred documents must be unstarred before deletion.') 'Starred document delete was not blocked with the expected message'
[void](Invoke-Api -Method POST -Path "/v1/app/docs/$docId/star" -Body @{ starred = $false } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method DELETE -Path "/v1/app/docs/$docId" -ExpectedStatus @(200) -Session $user)
$docsDeleted = Invoke-Api -Method GET -Path '/v1/app/docs?include_deleted=true' -ExpectedStatus @(200) -Session $user
$deletedDoc = Find-ById -Items $docsDeleted.Json.documents -Id $docId
Assert-True ($null -ne $deletedDoc -and [bool]$deletedDoc.is_deleted) 'Deleted document did not appear in include_deleted list'
$deletedDocGet = Invoke-Api -Method GET -Path "/v1/app/docs/${docId}?include_deleted=true" -ExpectedStatus @(200) -Session $user
Assert-True ([bool]$deletedDocGet.Json.document.is_deleted) 'Deleted document could not be loaded read-only'
[void](Invoke-Api -Method POST -Path "/v1/app/docs/$docId/restore" -ExpectedStatus @(200) -Session $user)
$docRestored = Invoke-Api -Method GET -Path "/v1/app/docs/$docId" -ExpectedStatus @(200) -Session $user
Assert-True ($docRestored.Json.document.title -eq "$docTitle Updated") 'Restored document did not keep updated title'

Write-Step 'Running folders and chat flow'
$folderCreate = Invoke-Api -Method POST -Path '/v1/app/chat-folders' -Body @{ name = $folderBase } -ExpectedStatus @(200) -Session $user
$folderId = [string]$folderCreate.Json.folder.id
Assert-True (-not [string]::IsNullOrWhiteSpace($folderId)) 'Folder creation did not return an id'
[void](Invoke-Api -Method PATCH -Path "/v1/app/chat-folders/$folderId" -Body @{ name = "$folderBase Renamed" } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path '/v1/app/chat-folders' -Body @{ name = "$folderBase Duplicate" } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method POST -Path '/v1/app/chat-folders' -Body @{ name = "$folderBase Duplicate" } -ExpectedStatus @(409) -Session $user)

$chatCreate = Invoke-Api -Method POST -Path '/v1/app/chats' -Body @{ title = $chatTitle } -ExpectedStatus @(200) -Session $user
$chatId = [string]$chatCreate.Json.chat.id
Assert-True (-not [string]::IsNullOrWhiteSpace($chatId)) 'Chat creation did not return an id'
[void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$chatId" -Body @{ title = "$chatTitle Updated" } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$chatId" -Body @{ folder_id = $folderId } -ExpectedStatus @(200) -Session $user)
$chatsList = Invoke-Api -Method GET -Path '/v1/app/chats' -ExpectedStatus @(200) -Session $user
$chatRow = Find-ById -Items $chatsList.Json.chats -Id $chatId
Assert-True ($null -ne $chatRow -and $chatRow.folder_id -eq $folderId) 'Chat was not moved into the folder'
[void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$chatId" -Body @{ is_saved = $true } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$chatId" -Body @{ is_saved = $false } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$chatId" -Body @{ folder_id = $null } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$chatId" -Body @{ folder_id = $folderId } -ExpectedStatus @(200) -Session $user)
[void](Invoke-Api -Method DELETE -Path "/v1/app/chat-folders/$folderId" -ExpectedStatus @(200) -Session $user)
$chatsAfterFolderDelete = Invoke-Api -Method GET -Path '/v1/app/chats' -ExpectedStatus @(200) -Session $user
$chatAfterFolderDelete = Find-ById -Items $chatsAfterFolderDelete.Json.chats -Id $chatId
Assert-True ($null -ne $chatAfterFolderDelete -and $null -eq $chatAfterFolderDelete.folder_id) 'Deleting a folder did not uncategorize its chats'
[void](Invoke-Api -Method DELETE -Path "/v1/app/chats/$chatId" -ExpectedStatus @(200) -Session $user)
$deletedChats = Invoke-Api -Method GET -Path '/v1/app/chats?include_deleted=true' -ExpectedStatus @(200) -Session $user
$deletedChat = Find-ById -Items $deletedChats.Json.chats -Id $chatId
Assert-True ($null -ne $deletedChat -and [bool]$deletedChat.is_deleted) 'Deleted chat did not appear in include_deleted list'
$deletedChatGet = Invoke-Api -Method GET -Path "/v1/app/chats/${chatId}?include_deleted=true" -ExpectedStatus @(200) -Session $user
Assert-True ([bool]$deletedChatGet.Json.chat.is_deleted) 'Deleted chat could not be loaded read-only'
[void](Invoke-Api -Method POST -Path "/v1/app/chats/$chatId/restore" -ExpectedStatus @(200) -Session $user)
$restoredChatGet = Invoke-Api -Method GET -Path "/v1/app/chats/$chatId" -ExpectedStatus @(200) -Session $user
Assert-True ($restoredChatGet.Json.chat.title -eq "$chatTitle Updated") 'Restored chat title mismatch'

Write-Step 'Running saved-chat limit checks'
$savedChatIds = New-Object System.Collections.Generic.List[string]
for ($i = 1; $i -le 10; $i++) {
    $created = Invoke-Api -Method POST -Path '/v1/app/chats' -Body @{ title = "Saved Limit $timestamp $i" } -ExpectedStatus @(200) -Session $user
    $savedId = [string]$created.Json.chat.id
    $savedChatIds.Add($savedId)
    [void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$savedId" -Body @{ is_saved = $true } -ExpectedStatus @(200) -Session $user)
}
$overflowChat = Invoke-Api -Method POST -Path '/v1/app/chats' -Body @{ title = "Saved Limit Overflow $timestamp" } -ExpectedStatus @(200) -Session $user
[void](Invoke-Api -Method PATCH -Path "/v1/app/chats/$($overflowChat.Json.chat.id)" -Body @{ is_saved = $true } -ExpectedStatus @(409) -Session $user)

Write-Step 'Running completion flow'
$completion = Invoke-Api -Method POST -Path '/v1/app/chat/completions' -Body @{
    model = $modelName
    chat_id = $chatId
    messages = @(@{ role = 'user'; content = 'Reply with the single word TEST.' })
    stream = $false
    retrieval_enabled = $false
} -ExpectedStatus @(200) -Session $user -RequestTimeoutSec $TimeoutSec
Assert-True ($null -ne $completion.Json.choices -and ($completion.Json.choices | Measure-Object).Count -ge 1) 'Completion response did not return any choices'
Assert-True (-not [string]::IsNullOrWhiteSpace([string]$completion.Json.choices[0].message.content)) 'Completion response was empty'
Assert-True ($null -ne $completion.Json.choices[0].message.citations) 'Completion response did not include additive citations field'
Assert-True (($completion.Json.choices[0].message.citations | Measure-Object).Count -eq 0) 'Completion response returned citations even though retrieval was disabled'
$completedChat = Invoke-Api -Method GET -Path "/v1/app/chats/$chatId" -ExpectedStatus @(200) -Session $user
Assert-True (($completedChat.Json.chat.messages | Measure-Object).Count -ge 2) 'Completed chat did not persist messages'

Write-Step 'Live backend smoke test passed'
