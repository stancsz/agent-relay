[CmdletBinding()]
param(
    [string]$ListenHost = '127.0.0.1',

    [int]$Port = 8787,

    [string]$WorkspaceRoot = (Get-Location).Path,

    [string]$AuthToken = $env:CLAUDE_A2A_AUTH_TOKEN,

    [string]$TlsCert,

    [string]$TlsKey,

    [string]$WorkerAgentType,

    [string]$VerifierAgentType,

    [string]$AgentsJson,

    [switch]$CliFallback,

    [switch]$NoCliFallback,

    [string]$StateDir = $(if ($env:CLAUDE_TEAM_BRIDGE_STATE_DIR) { $env:CLAUDE_TEAM_BRIDGE_STATE_DIR } else { Join-Path $env:USERPROFILE '.claude-team-bridge' }),

    [Nullable[int]]$TimeoutSeconds
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
$resolvedStateDir = (New-Item -ItemType Directory -Path $StateDir -Force).FullName
if ($ListenHost -notin @('127.0.0.1', 'localhost', '::1') -and [string]::IsNullOrWhiteSpace($AuthToken)) {
    throw 'LAN binding requires -AuthToken or CLAUDE_A2A_AUTH_TOKEN.'
}
if (($null -eq $TlsCert) -xor ($null -eq $TlsKey)) {
    throw 'LAN TLS requires both -TlsCert and -TlsKey.'
}
if ($ListenHost -notin @('127.0.0.1', 'localhost', '::1') -and ([string]::IsNullOrWhiteSpace($TlsCert) -or [string]::IsNullOrWhiteSpace($TlsKey))) {
    throw 'LAN binding requires -TlsCert and -TlsKey; use a secure tunnel for plain HTTP.'
}
$pythonOutput = & cmd.exe /d /c where py.exe 2>$null
$pythonExitCode = $LASTEXITCODE
$python = $pythonOutput | Select-Object -First 1
if ($pythonExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($python)) {
    throw 'py.exe was not found on PATH.'
}
$server = Join-Path $PSScriptRoot 'claude_a2a_server.py'
$args = @('-3', '-B', $server, '--host', $ListenHost, '--port', $Port.ToString([Globalization.CultureInfo]::InvariantCulture), '--workspace-root', $resolvedRoot)
if (-not [string]::IsNullOrWhiteSpace($AuthToken)) { $args += @('--auth-token', $AuthToken) }
if (-not [string]::IsNullOrWhiteSpace($TlsCert)) { $args += @('--tls-cert', $TlsCert) }
if (-not [string]::IsNullOrWhiteSpace($TlsKey)) { $args += @('--tls-key', $TlsKey) }
if (-not [string]::IsNullOrWhiteSpace($WorkerAgentType)) { $args += @('--worker-agent-type', $WorkerAgentType) }
if (-not [string]::IsNullOrWhiteSpace($VerifierAgentType)) { $args += @('--verifier-agent-type', $VerifierAgentType) }
if (-not [string]::IsNullOrWhiteSpace($AgentsJson)) { $args += @('--agents-json', $AgentsJson) }
if ($CliFallback) { $args += '--cli-fallback' }
if ($NoCliFallback) { $args += '--no-cli-fallback' }
if (-not [string]::IsNullOrWhiteSpace($resolvedStateDir)) { $args += @('--state-dir', $resolvedStateDir) }
if ($null -ne $TimeoutSeconds) {
    if ($TimeoutSeconds -le 0) { throw 'TimeoutSeconds must be greater than zero when supplied.' }
    $args += @('--timeout-seconds', $TimeoutSeconds.Value.ToString([Globalization.CultureInfo]::InvariantCulture))
}
& $python @args
exit $LASTEXITCODE
