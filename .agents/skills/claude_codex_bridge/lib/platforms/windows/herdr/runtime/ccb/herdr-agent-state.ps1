# Herdr agent-state hook for CCB (Claude Code Bridge)
# Place this file at: C:\Users\Administrator\.ccb\hooks\herdr-agent-state.ps1
#
# Herdr calls this hook to discover CCB-managed agents in the current
# project.  The hook queries ccbd via its control-plane socket and emits
# JSON with one agent entry per CCB-managed pane.
#
# Expected output (JSON to stdout):
# {
#   "agents": [
#     {"name": "agent1", "provider": "codex", "pane_id": "wH:p3", "state": "idle"},
#     {"name": "agent2", "provider": "claude", "pane_id": "wH:p4", "state": "idle"}
#   ]
# }

param(
    [string] $ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

function ConvertTo-DisplayName {
    param([string] $Name)
    $text = ([string] $Name).Trim()
    if (-not $text) {
        return ""
    }
    $words = $text -replace '[_-]+', ' '
    return (Get-Culture).TextInfo.ToTitleCase($words.ToLowerInvariant())
}

function Get-JsonValue {
    param(
        [object] $Json,
        [string[]] $Names
    )
    if ($null -eq $Json) {
        return $null
    }
    foreach ($name in @($Names)) {
        $property = $Json.PSObject.Properties |
            Where-Object { $_.Name -ieq $name } |
            Select-Object -First 1
        if ($null -ne $property) {
            return $property.Value
        }
    }
    return $null
}

function Resolve-TokenPath {
    param(
        [string] $Reference,
        [string] $BaseDirectory
    )
    $text = ([string] $Reference).Trim()
    if (-not $text) {
        return ""
    }
    if ([System.IO.Path]::IsPathRooted($text)) {
        return $text
    }
    return (Join-Path $BaseDirectory $text)
}

function ConvertTo-AgentRecord {
    param([object] $Agent)
    $name = [string] (Get-JsonValue -Json $Agent -Names @('name', 'agent', 'agent_name'))
    $name = $name.Trim()
    if (-not $name) {
        return $null
    }
    $normalizedName = $name.ToLowerInvariant()
    $displayName = [string] (Get-JsonValue -Json $Agent -Names @('display_name', 'label', 'title'))
    if (-not $displayName.Trim()) {
        $displayName = ConvertTo-DisplayName $normalizedName
    }
    return @{
        name = $normalizedName
        display_name = $displayName.Trim()
        provider = [string] (Get-JsonValue -Json $Agent -Names @('provider', 'provider_name'))
        pane_id = [string] (Get-JsonValue -Json $Agent -Names @('pane_id', 'pane', 'pane_ref'))
        state = [string] (Get-JsonValue -Json $Agent -Names @('runtime_state', 'state', 'status'))
        window = [string] (Get-JsonValue -Json $Agent -Names @('window', 'window_name'))
    }
}

function Add-AgentRecord {
    param(
        [object[]] $Records,
        [hashtable] $Seen,
        [hashtable] $Record
    )
    if ($null -eq $Record) {
        return @($Records)
    }
    $name = [string] $Record.name
    if (-not $name.Trim()) {
        return @($Records)
    }
    if ($Seen.ContainsKey($name)) {
        $Records[$Seen[$name]] = $Record
        return @($Records)
    }
    $Seen[$name] = $Records.Count
    return @($Records + $Record)
}

# Locate the ccbd control-plane token and socket.
# The state root follows CCB_RUNTIME_STATE_HOME / project_id / ccbd.
$ccbDir = Join-Path $ProjectRoot '.ccb'
$refPath = Join-Path $ccbDir 'runtime-root-ref.json'
if (-not (Test-Path $refPath)) {
    Write-Output '{"agents":[]}'
    exit 0
}

try {
    $ref = Get-Content -Raw $refPath | ConvertFrom-Json
    $projectId = $ref.project_id
    $stateRoot = if ($ref.PSObject.Properties['runtime_state_root']) { $ref.runtime_state_root } else { "D:\.c8\rs\$projectId" }
} catch {
    Write-Output '{"agents":[]}'
    exit 0
}

$ccbdDir = Join-Path $stateRoot 'ccbd'
$endpointPath = Join-Path $ccbdDir 'control-plane-endpoint.json'
if (-not (Test-Path $endpointPath)) {
    Write-Output '{"agents":[]}'
    exit 0
}

# Read the current endpoint. Tokens no longer carry address/port.
try {
    $endpoint = Get-Content -Raw $endpointPath | ConvertFrom-Json
    $address = [string] (Get-JsonValue -Json $endpoint -Names @('host', 'address'))
    if (-not $address.Trim()) {
        $address = "127.0.0.1"
    }
    $port = [int] (Get-JsonValue -Json $endpoint -Names @('port'))
    $tokenReference = [string] (Get-JsonValue -Json $endpoint -Names @('token_ref', 'auth_ref', 'token_path'))
    $inlineToken = [string] (Get-JsonValue -Json $endpoint -Names @('token', 'auth_token'))
    if ($inlineToken.Trim()) {
        $authToken = $inlineToken
    } else {
        $tokenPath = Resolve-TokenPath -Reference $tokenReference -BaseDirectory (Split-Path -Parent $endpointPath)
        $tokenPayload = Get-Content -Raw $tokenPath | ConvertFrom-Json
        $authToken = [string] (Get-JsonValue -Json $tokenPayload -Names @('token', 'auth_token'))
    }
} catch {
    Write-Output '{"agents":[]}'
    exit 0
}

if ((-not $port) -or (-not $authToken.Trim())) {
    Write-Output '{"agents":[]}'
    exit 0
}

# Query ccbd for agent state via TCP control plane.
$agents = @()
$agentIndexByName = @{}
try {
    $tcp = New-Object System.Net.Sockets.TcpClient($address, $port)
    $stream = $tcp.GetStream()
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $writer = New-Object System.IO.StreamWriter($stream, $utf8NoBom)
    $reader = New-Object System.IO.StreamReader($stream)
    $writer.NewLine = "`n"

    # Prefer project_view: it is the stable UI projection and includes every
    # configured agent row plus pane ids. Older ping fallback remains below for
    # compatibility with pre-project_view daemons.
    $auth = (@{ schema = "ccbd-control-plane-token-v1"; token = $authToken } | ConvertTo-Json -Compress) + "`n"
    $writer.Write($auth)
    $writer.Flush()
    $ack = $reader.ReadLine()
    if (-not $ack) {
        throw "ccbd auth ack missing"
    }
    $request = (@{ api_version = 2; op = "project_view"; request = @{ schema_version = 1 } } | ConvertTo-Json -Compress -Depth 4) + "`n"
    $writer.Write($request)
    $writer.Flush()

    # Read response with a short timeout.
    $tcp.ReceiveTimeout = 3000
    $response = $reader.ReadLine()
    if ($response) {
        $payload = $response | ConvertFrom-Json
        $view = Get-JsonValue -Json $payload -Names @('view')
        if ($null -eq $view) {
            $view = $payload
        }
        $rawAgents = Get-JsonValue -Json $view -Names @('agents')
        if ($null -ne $rawAgents) {
            foreach ($agent in @($rawAgents)) {
                $record = ConvertTo-AgentRecord $agent
                if ($null -ne $record) {
                    $agents = @(Add-AgentRecord -Records $agents -Seen $agentIndexByName -Record $record)
                }
            }
        }
    }
    $reader.Close()
    $writer.Close()
    $stream.Close()
    $tcp.Close()
} catch {
    # ccbd not reachable; return empty.
}

if ($agents.Count -eq 0) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient($address, $port)
        $stream = $tcp.GetStream()
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $writer = New-Object System.IO.StreamWriter($stream, $utf8NoBom)
        $reader = New-Object System.IO.StreamReader($stream)
        $writer.NewLine = "`n"

        $auth = (@{ schema = "ccbd-control-plane-token-v1"; token = $authToken } | ConvertTo-Json -Compress) + "`n"
        $writer.Write($auth)
        $writer.Flush()
        $ack = $reader.ReadLine()
        if (-not $ack) {
            throw "ccbd auth ack missing"
        }
        $request = (@{ api_version = 2; op = "ping"; request = @{ target = "ccbd" } } | ConvertTo-Json -Compress -Depth 4) + "`n"
        $writer.Write($request)
        $writer.Flush()

        $tcp.ReceiveTimeout = 3000
        $response = $reader.ReadLine()
        if ($response) {
            $payload = $response | ConvertFrom-Json
            $rawAgents = Get-JsonValue -Json $payload -Names @('agents')
            if ($null -ne $rawAgents) {
                foreach ($agent in @($rawAgents)) {
                    $record = ConvertTo-AgentRecord $agent
                    if ($null -ne $record) {
                        $agents = @(Add-AgentRecord -Records $agents -Seen $agentIndexByName -Record $record)
                    }
                }
            }
        }
        $reader.Close()
        $writer.Close()
        $stream.Close()
        $tcp.Close()
    } catch {
        # Older or unreachable ccbd; return any project_view results or empty.
    }
}

$output = @{ agents = $agents } | ConvertTo-Json -Compress
Write-Output $output
