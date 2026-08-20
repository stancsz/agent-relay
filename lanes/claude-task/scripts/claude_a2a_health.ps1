[CmdletBinding()]
param(
    [string]$ServerUrl = $(if ($env:CLAUDE_A2A_SERVER_URL) { $env:CLAUDE_A2A_SERVER_URL } else { 'http://127.0.0.1:8787' }),

    [string]$AuthToken = $env:CLAUDE_A2A_AUTH_TOKEN,

    [switch]$ProbeCapabilities,

    [Nullable[double]]$TimeoutSeconds
)

$ErrorActionPreference = 'Stop'
$pythonOutput = & cmd.exe /d /c where py.exe 2>$null
$pythonExitCode = $LASTEXITCODE
$python = $pythonOutput | Select-Object -First 1
if ($pythonExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($python)) {
    throw 'py.exe was not found on PATH.'
}
$client = Join-Path $PSScriptRoot 'claude_a2a_client.py'
$args = @('-3', '-B', $client, '--server-url', $ServerUrl, '--health')
if (-not [string]::IsNullOrWhiteSpace($AuthToken)) { $args += @('--auth-token', $AuthToken) }
if ($ProbeCapabilities) { $args += '--probe-capabilities' }
if ($null -ne $TimeoutSeconds) {
    if ($TimeoutSeconds -le 0) { throw 'TimeoutSeconds must be greater than zero when supplied.' }
    $args += @('--timeout-seconds', ([double]$TimeoutSeconds).ToString([Globalization.CultureInfo]::InvariantCulture))
}
& $python @args
exit $LASTEXITCODE
