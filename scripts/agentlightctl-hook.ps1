param(
    [ValidateSet('codex')]
    [string]$Source = 'codex'
)

$ErrorActionPreference = 'Stop'

# 与 src/agentlight/paths.py 的 default_data_root() 保持一致。
function Resolve-DataRoot {
    if ($env:AGENTLIGHT_DATA_DIR) { return $env:AGENTLIGHT_DATA_DIR }
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:APPDATA }
    if ($base) { return (Join-Path $base 'AgentLight') }
    return (Join-Path $HOME '.agentlight')
}

$DataRoot = Resolve-DataRoot
$ConfigPath = Join-Path $DataRoot 'config.json'
$ErrorLog = Join-Path $DataRoot 'logs\hook-errors.log'
$AppPath = Join-Path $PSScriptRoot 'AgentLight.exe'

function Write-HookError([string]$Message) {
    try {
        $logDir = Split-Path -Parent $ErrorLog
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        Add-Content -LiteralPath $ErrorLog -Encoding UTF8 -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    } catch {
        # Hook diagnostics must never block Codex.
    }
}

function Clean-Identifier($Value, [string]$Fallback) {
    $cleaned = ([string]$Value -replace '[^\p{L}\p{N}_.:-]', '')
    if ($cleaned.Length -gt 64) { $cleaned = $cleaned.Substring(0, 64) }
    if ([string]::IsNullOrWhiteSpace($cleaned)) { return $Fallback }
    return $cleaned
}

function Read-JsonStringField([string]$Json, [string]$Name) {
    $pattern = '"' + [regex]::Escape($Name) + '"\s*:\s*"((?:\\.|[^"\\])*)"'
    $match = [regex]::Match($Json, $pattern)
    if (-not $match.Success) { return '' }
    try {
        return ConvertFrom-Json -InputObject ('"' + $match.Groups[1].Value + '"')
    } catch {
        return $match.Groups[1].Value
    }
}

function Send-Event([string]$Uri, [hashtable]$Headers, [string]$Body) {
    # Codex only gives command hooks three seconds. PowerShell startup already uses
    # part of that budget, so a dead/stale endpoint must fail quickly.
    Invoke-RestMethod -Uri $Uri -Method Post -Headers $Headers -ContentType 'application/json; charset=utf-8' -Body $Body -TimeoutSec 1 | Out-Null
}

try {
    $raw = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw)) { exit 0 }
    try {
        $payload = $raw | ConvertFrom-Json
    } catch {
        # Recover the routing fields needed when a large JSON payload is truncated.
        $payload = [pscustomobject]@{
            hook_event_name = Read-JsonStringField $raw 'hook_event_name'
            session_id = Read-JsonStringField $raw 'session_id'
            thread_id = Read-JsonStringField $raw 'thread_id'
            tool_name = Read-JsonStringField $raw 'tool_name'
        }
        if ([string]::IsNullOrWhiteSpace($payload.hook_event_name)) { throw }
    }
    $eventName = Clean-Identifier $payload.hook_event_name ''
    $sessionId = Clean-Identifier $(if ($payload.session_id) { $payload.session_id } else { $payload.thread_id }) 'default'
    $toolName = ([string]$payload.tool_name).ToLowerInvariant().Replace('_', '')

    $state = switch ($eventName) {
        'SessionStart' { 'ready' }
        'UserPromptSubmit' { 'thinking' }
        'PermissionRequest' { 'permission' }
        'PreToolUse' {
            if ($toolName.EndsWith('requestuserinput') -or $toolName.EndsWith('askuserquestion')) { 'attention' } else { 'busy' }
        }
        'PostToolUse' { 'busy' }
        'SubagentStart' { 'subagent' }
        'SubagentStop' { 'subagent' }
        'Stop' { 'done' }
        'SessionEnd' { 'off' }
        default { $null }
    }
    if (-not $state) { exit 0 }

    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $uri = 'http://{0}:{1}/v1/events' -f $config.api.host, $config.api.port
    $headers = @{ Authorization = 'Bearer {0}' -f $config.api.token }
    $body = @{
        source = Clean-Identifier $Source 'codex'
        sessionId = $sessionId
        state = $state
        eventName = $eventName
    } | ConvertTo-Json -Compress

    try {
        Send-Event $uri $headers $body
    } catch {
        # Starting the app is best-effort and asynchronous. Do not wait and retry
        # here: a stale port can consume the entire Codex hook timeout per request.
        # A later hook will deliver the next state after AgentLight is ready.
        if (Test-Path -LiteralPath $AppPath) {
            Start-Process -FilePath $AppPath -ArgumentList '--background' -WindowStyle Hidden
        }
        throw
    }
} catch {
    Write-HookError ('Codex PowerShell hook: {0}' -f $_.Exception.Message)
}

exit 0
