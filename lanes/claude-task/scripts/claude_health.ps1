[CmdletBinding()]
param(
    [string]$WorkingDirectory = (Get-Location).Path,

    [switch]$LiveProbe,

    [string]$ProbePrompt = 'Return exactly HEALTH_OK and do not inspect or modify files.',

    [Nullable[int]]$ProbeTimeoutSeconds,

    [switch]$Mcp
)

$ErrorActionPreference = 'Stop'

function Quote-CmdArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Redact-Output {
    param([string]$Value)
    if ($null -eq $Value) {
        return ''
    }
    $redacted = $Value -replace '(?im)(token|api[_-]?key|authorization)\s*[:=]\s*\S+', '$1=<redacted>'
    return ($redacted -replace '(?i)\b(?:sk|gho|github_pat)_[A-Za-z0-9_-]+\b', '<redacted>')
}

function Invoke-ClaudeCommand {
    param(
        [string[]]$Arguments,
        [string]$InputText,
        [Nullable[int]]$TimeoutSeconds
    )

    $psi = [Diagnostics.ProcessStartInfo]::new()
    $directExecutable = $Arguments.Count -gt 0 -and (Test-Path -LiteralPath $Arguments[0] -PathType Leaf) -and ([IO.Path]::GetExtension($Arguments[0]).ToLowerInvariant() -eq '.exe')
    if ($directExecutable) {
        $psi.FileName = $Arguments[0]
        $psi.Arguments = (($Arguments | Select-Object -Skip 1 | ForEach-Object { Quote-CmdArgument $_ }) -join ' ')
    } else {
        $psi.FileName = 'cmd.exe'
        $psi.Arguments = '/d /c ' + (($Arguments | ForEach-Object { Quote-CmdArgument $_ }) -join ' ')
    }
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    if ($psi.PSObject.Properties.Name -contains 'StandardInputEncoding') {
        $psi.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    }
    if ($psi.PSObject.Properties.Name -contains 'StandardOutputEncoding') {
        $psi.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    }
    if ($psi.PSObject.Properties.Name -contains 'StandardErrorEncoding') {
        $psi.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    $timedOut = $false
    try {
        if (-not $process.Start()) {
            throw 'Failed to start claude.cmd.'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($null -ne $InputText) {
            $process.StandardInput.Write($InputText)
        }
        $process.StandardInput.Close()
        if ($null -eq $TimeoutSeconds) {
            $process.WaitForExit()
        } elseif ($TimeoutSeconds -gt 0) {
            if (-not $process.WaitForExit(([int]$TimeoutSeconds) * 1000)) {
                $timedOut = $true
                & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
                $process.WaitForExit(5000) | Out-Null
            }
        } else {
            throw 'ProbeTimeoutSeconds must be greater than zero when supplied.'
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        [ordered]@{
            exit_code = if ($timedOut) { $null } else { $process.ExitCode }
            timed_out = $timedOut
            stdout = Redact-Output $stdout
            stderr = Redact-Output $stderr
        }
    } finally {
        $process.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    throw "Working directory does not exist: $WorkingDirectory"
}
$WorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path

$claudePathOutput = & cmd.exe /d /c where claude.cmd 2>&1
$claudeOnPath = ($LASTEXITCODE -eq 0)
$claudePath = if ($claudeOnPath) { (($claudePathOutput -join "`n").Trim()) } else { $null }
$claudeInvocation = 'claude.cmd'
if ($claudeOnPath) {
    $claudeDirectory = Split-Path -Parent ($claudePath -split "`n")[0]
    $directCandidate = Join-Path $claudeDirectory 'node_modules\@anthropic-ai\claude-code\bin\claude.exe'
    if (Test-Path -LiteralPath $directCandidate -PathType Leaf) {
        $claudeInvocation = $directCandidate
    }
}
$auth = if ($claudeOnPath) { Invoke-ClaudeCommand @($claudeInvocation, 'auth', 'status') $null $null } else { $null }

$live = $null
$livePassed = $true
if ($Mcp -and $claudeOnPath) {
    $pythonPathOutput = & cmd.exe /d /c where py.exe 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonPath = (($pythonPathOutput -join "`n").Split("`n")[0]).Trim()
        $mcpClient = Join-Path $PSScriptRoot 'claude_mcp_delegate.py'
        $mcpArgs = @($pythonPath, '-3', '-X', 'utf8', $mcpClient, '--working-directory', $WorkingDirectory, '--health-only')
        if ($null -ne $ProbeTimeoutSeconds) {
            if ($ProbeTimeoutSeconds -le 0) { throw 'ProbeTimeoutSeconds must be greater than zero when supplied.' }
            $mcpArgs += @('--timeout-seconds', ([int]$ProbeTimeoutSeconds).ToString([Globalization.CultureInfo]::InvariantCulture))
        }
        $mcpRun = Invoke-ClaudeCommand $mcpArgs $null $ProbeTimeoutSeconds
        $mcpParsed = $null
        $mcpParseError = $null
        if (-not [string]::IsNullOrWhiteSpace($mcpRun.stdout)) {
            try { $mcpParsed = $mcpRun.stdout.Trim() | ConvertFrom-Json } catch { $mcpParseError = $_.Exception.Message }
        }
        $mcpPassed = ($null -ne $mcpParsed) -and [bool]$mcpParsed.accepted_by_transport -and [bool]$mcpParsed.agent_tool_available -and (-not $mcpRun.timed_out)
    } else {
        $mcpRun = [ordered]@{ exit_code = $null; timed_out = $false; stdout = ''; stderr = 'py.exe was not found on PATH.' }
        $mcpParsed = $null
        $mcpParseError = 'py.exe was not found on PATH.'
        $mcpPassed = $false
    }
} elseif ($LiveProbe -and $claudeOnPath) {
    # Claude Code 2.1.x reliably accepts the prompt as the --print argument.
    # Sending it on stdin with `-p` can be interpreted as an empty prompt on
    # Windows, which makes this health check report a false timeout/failure.
    $live = Invoke-ClaudeCommand @($claudeInvocation, '--print', $ProbePrompt, '--no-session-persistence', '--output-format', 'json', '--allowed-tools', 'Read') $null $ProbeTimeoutSeconds
    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace($live.stdout)) {
        try { $parsed = $live.stdout.Trim() | ConvertFrom-Json } catch { $parsed = $null }
    }
    $livePassed = ($null -ne $parsed) -and ([string]$parsed.subtype -eq 'success') -and (-not [string]::IsNullOrWhiteSpace($live.stdout)) -and (-not $live.timed_out)
}

$authPassed = $claudeOnPath -and ($null -ne $auth) -and ($auth.exit_code -eq 0)
$routePassed = if ($Mcp) { $mcpPassed } else { $livePassed }
$healthy = $claudeOnPath -and $authPassed -and $routePassed
$result = [ordered]@{
    checked_at = [DateTimeOffset]::UtcNow.ToString('o')
    working_directory = $WorkingDirectory
    claude_on_path = $claudeOnPath
    claude_path = $claudePath
    auth_status_exit_code = if ($null -eq $auth) { $null } else { $auth.exit_code }
    auth_status_timed_out = if ($null -eq $auth) { $null } else { $auth.timed_out }
    auth_status_output = if ($null -eq $auth) { $null } else { $auth.stdout }
    anthropic_base_url_present = -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_BASE_URL)
    anthropic_api_key_present = -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_API_KEY)
    live_probe_requested = [bool]$LiveProbe
    live_probe_timeout_seconds = if ($null -eq $ProbeTimeoutSeconds) { $null } else { [int]$ProbeTimeoutSeconds }
    live_probe_exit_code = if ($null -eq $live) { $null } else { $live.exit_code }
    live_probe_timed_out = if ($null -eq $live) { $null } else { $live.timed_out }
    live_probe_subtype = if ($null -eq $live -or [string]::IsNullOrWhiteSpace($live.stdout)) { $null } else { try { ([string](($live.stdout.Trim() | ConvertFrom-Json).subtype)) } catch { $null } }
    live_probe_response_non_empty = if ($null -eq $live) { $null } else { -not [string]::IsNullOrWhiteSpace($live.stdout) }
    mcp_requested = [bool]$Mcp
    mcp_exit_code = if ($null -eq $mcpRun) { $null } else { $mcpRun.exit_code }
    mcp_timed_out = if ($null -eq $mcpRun) { $null } else { $mcpRun.timed_out }
    mcp_protocol_version = if ($null -eq $mcpParsed) { $null } else { $mcpParsed.protocol_version }
    mcp_server_name = if ($null -eq $mcpParsed) { $null } else { $mcpParsed.server_name }
    mcp_server_version = if ($null -eq $mcpParsed) { $null } else { $mcpParsed.server_version }
    mcp_tool_count = if ($null -eq $mcpParsed) { $null } else { $mcpParsed.tool_count }
    mcp_agent_tool_available = if ($null -eq $mcpParsed) { $null } else { $mcpParsed.agent_tool_available }
    mcp_protocol_error = if ($null -eq $mcpParsed) { $null } else { $mcpParsed.protocol_error }
    mcp_parse_error = $mcpParseError
    mcp_stderr = if ($null -eq $mcpRun) { $null } else { $mcpRun.stderr }
    healthy = $healthy
}
Write-Output ($result | ConvertTo-Json -Depth 8)
if (-not $healthy) { exit 1 }
