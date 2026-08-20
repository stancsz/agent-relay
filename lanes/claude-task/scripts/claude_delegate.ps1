[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$WorkingDirectory,

    [Parameter(Mandatory = $true, ParameterSetName = 'Prompt')]
    [string]$Prompt,

    [Parameter(Mandatory = $true, ParameterSetName = 'PromptFile')]
    [string]$PromptFile,

    [string[]]$TargetPath = @(),

    [switch]$ExpectChange,

    [switch]$ExpectNoChange,

    [string]$Model,

    [Nullable[decimal]]$BudgetUsd,

    [Nullable[int]]$TimeoutSeconds,

    [string]$AllowedTools = 'Read,Bash',

    [string]$RequiredResponseText,

    [string]$ResultPath,

    [ValidateSet('Mcp', 'Cli')]
    [string]$Transport = 'Cli',

    [string]$McpAgentType
)

$ErrorActionPreference = 'Stop'

function Invoke-GitText {
    param([string[]]$Arguments)
    # Git may emit a harmless line-ending warning on stderr for this Windows
    # checkout. Capture stdout only and use the exit code for failure; mixing
    # stderr into the value corrupts branch/status snapshots and PowerShell's
    # Stop preference can turn a zero-exit warning into an exception.
    $previousErrorAction = $ErrorActionPreference
    try {
        # Native stderr is surfaced as an ErrorRecord by Windows PowerShell
        # even when redirected; do not let a zero-exit warning abort the gate.
        $ErrorActionPreference = 'Continue'
        $value = & git -C $WorkingDirectory @Arguments 2>$null
        $gitExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($gitExitCode -ne 0) {
        throw "git command failed ($gitExitCode): git -C `"$WorkingDirectory`" $($Arguments -join ' ')`n$($value -join "`n")"
    }
    return (($value -join "`n").TrimEnd())
}

function Get-FileFingerprint {
    param([string]$RelativePath)
    $path = Join-Path $WorkingDirectory $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return 'missing'
    }
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
}

function Get-RepoSnapshot {
    param([string[]]$Paths)
    $fingerprints = [ordered]@{}
    foreach ($path in $Paths) {
        $fingerprints[$path] = Get-FileFingerprint -RelativePath $path
    }
    [ordered]@{
        branch = Invoke-GitText @('branch', '--show-current')
        head = Invoke-GitText @('rev-parse', 'HEAD')
        status = Invoke-GitText @('status', '--porcelain=v1')
        changed_paths = Invoke-GitText @('diff', '--name-only')
        staged_paths = Invoke-GitText @('diff', '--cached', '--name-only')
        target_fingerprints = $fingerprints
    }
}

function Get-StatusPath {
    param([string]$StatusLine)
    if ([string]::IsNullOrWhiteSpace($StatusLine) -or $StatusLine.Length -lt 4) {
        return ''
    }
    $path = $StatusLine.Substring(3).Trim()
    if ($path.Contains(' -> ')) {
        $path = $path.Substring($path.LastIndexOf(' -> ', [StringComparison]::Ordinal) + 4)
    }
    return $path.Trim('"').Replace('\', '/')
}

function Quote-CmdArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    throw "Working directory does not exist: $WorkingDirectory"
}

$WorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
$null = & cmd.exe /d /c where claude.cmd 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'claude.cmd was not found on PATH. Fix the Claude CLI installation or PATH before delegating.'
}

if ($PSCmdlet.ParameterSetName -eq 'PromptFile') {
    if (-not (Test-Path -LiteralPath $PromptFile -PathType Leaf)) {
        throw "Prompt file does not exist: $PromptFile"
    }
    $Prompt = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $PromptFile).Path, [Text.UTF8Encoding]::new($false))
}

if ([string]::IsNullOrWhiteSpace($Prompt)) {
    throw 'Prompt must not be empty.'
}

if ($ExpectChange -and $ExpectNoChange) {
    throw 'Use only one of -ExpectChange or -ExpectNoChange.'
}

$before = Get-RepoSnapshot -Paths $TargetPath
$started = [DateTimeOffset]::UtcNow
$stopwatch = [Diagnostics.Stopwatch]::StartNew()

$stdout = ''
$stderr = ''
$timedOut = $false
$exitCode = $null
$mcpReceipt = $null
$mcpTransportError = $null

if ($Transport -eq 'Mcp') {
    if (-not [string]::IsNullOrWhiteSpace($Model) -or $null -ne $BudgetUsd) {
        throw 'MCP transport does not accept model or budget overrides. Omit -Model and -BudgetUsd.'
    }
    $pythonPathOutput = & cmd.exe /d /c where py.exe 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'py.exe was not found on PATH. MCP transport uses the stdlib-only Python client.'
    }
    $pythonPath = (($pythonPathOutput -join "`n").Split("`n")[0]).Trim()
    $mcpClient = Join-Path $PSScriptRoot 'claude_mcp_delegate.py'
    $mcpArgs = @('-3', '-X', 'utf8', $mcpClient, '--working-directory', $WorkingDirectory)
    if (-not [string]::IsNullOrWhiteSpace($McpAgentType)) { $mcpArgs += @('--agent-type', $McpAgentType) }
    if ($null -ne $TimeoutSeconds) {
        if ($TimeoutSeconds -le 0) { throw 'TimeoutSeconds must be greater than zero when supplied.' }
        $mcpArgs += @('--timeout-seconds', ([int]$TimeoutSeconds).ToString([Globalization.CultureInfo]::InvariantCulture))
    }
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $pythonPath
    $psi.Arguments = ($mcpArgs | ForEach-Object { Quote-CmdArgument $_ }) -join ' '
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    if ($psi.PSObject.Properties.Name -contains 'StandardInputEncoding') { $psi.StandardInputEncoding = [Text.UTF8Encoding]::new($false) }
    if ($psi.PSObject.Properties.Name -contains 'StandardOutputEncoding') { $psi.StandardOutputEncoding = [Text.UTF8Encoding]::new($false) }
    if ($psi.PSObject.Properties.Name -contains 'StandardErrorEncoding') { $psi.StandardErrorEncoding = [Text.UTF8Encoding]::new($false) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    try {
        if (-not $process.Start()) { throw 'Failed to start the MCP client.' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Write($Prompt)
        $process.StandardInput.Close()
        if ($null -eq $TimeoutSeconds) { $process.WaitForExit() }
        elseif (-not $process.WaitForExit(([int]$TimeoutSeconds) * 1000)) {
            $timedOut = $true
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
            $process.WaitForExit(5000) | Out-Null
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if (-not $timedOut) { $exitCode = $process.ExitCode }
    } finally { $process.Dispose() }
    if (-not [string]::IsNullOrWhiteSpace($stdout)) {
        try { $mcpReceipt = $stdout.Trim() | ConvertFrom-Json } catch { $mcpTransportError = $_.Exception.Message }
    }
} else {
    $claudeArgs = @('claude.cmd', '-p', '--no-session-persistence', '--output-format', 'json', '--allowed-tools', $AllowedTools)
    if (-not [string]::IsNullOrWhiteSpace($Model)) { $claudeArgs += @('--model', $Model) }
    if ($null -ne $BudgetUsd) { $claudeArgs += @('--max-budget-usd', ([decimal]$BudgetUsd).ToString([Globalization.CultureInfo]::InvariantCulture)) }
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'cmd.exe'
    $psi.Arguments = '/d /c ' + (($claudeArgs | ForEach-Object { Quote-CmdArgument $_ }) -join ' ')
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    if ($psi.PSObject.Properties.Name -contains 'StandardInputEncoding') { $psi.StandardInputEncoding = [Text.UTF8Encoding]::new($false) }
    if ($psi.PSObject.Properties.Name -contains 'StandardOutputEncoding') { $psi.StandardOutputEncoding = [Text.UTF8Encoding]::new($false) }
    if ($psi.PSObject.Properties.Name -contains 'StandardErrorEncoding') { $psi.StandardErrorEncoding = [Text.UTF8Encoding]::new($false) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    try {
        if (-not $process.Start()) { throw 'Failed to start claude.cmd.' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Write($Prompt)
        $process.StandardInput.Close()
        if ($null -eq $TimeoutSeconds) { $process.WaitForExit() }
        elseif ($TimeoutSeconds -gt 0) {
            if (-not $process.WaitForExit(([int]$TimeoutSeconds) * 1000)) {
                $timedOut = $true
                & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
                $process.WaitForExit(5000) | Out-Null
            }
        } else { throw 'TimeoutSeconds must be greater than zero when supplied.' }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if (-not $timedOut) { $exitCode = $process.ExitCode }
    } finally { $process.Dispose() }
}

$stopwatch.Stop()
Start-Sleep -Milliseconds 500
$after = Get-RepoSnapshot -Paths $TargetPath

$targetChanged = $false
foreach ($path in $TargetPath) {
    if ($before.target_fingerprints[$path] -ne $after.target_fingerprints[$path]) {
        $targetChanged = $true
        break
    }
}

$parsed = $null
$parseError = $null
if ($Transport -eq 'Mcp') {
    $parsed = $mcpReceipt
    if ($null -eq $parsed -and -not [string]::IsNullOrWhiteSpace($stdout)) { $parseError = $mcpTransportError }
} elseif (-not [string]::IsNullOrWhiteSpace($stdout)) {
    try {
        $parsed = $stdout.Trim() | ConvertFrom-Json
    } catch {
        $parseError = $_.Exception.Message
    }
}

$subtype = if ($Transport -eq 'Mcp') { if ($null -ne $parsed -and [bool]$parsed.accepted_by_transport) { 'success' } else { 'error' } } elseif ($null -ne $parsed) { [string]$parsed.subtype } else { '' }
$isError = if ($Transport -eq 'Mcp') { ($null -eq $parsed) -or (-not [bool]$parsed.accepted_by_transport) } elseif ($null -ne $parsed -and $null -ne $parsed.is_error) { [bool]$parsed.is_error } else { $false }
$permissionDenials = @()
if ($null -ne $parsed -and $null -ne $parsed.permission_denials) {
    $permissionDenials = @($parsed.permission_denials)
}
$branchOrHeadChanged = ($before.branch -ne $after.branch) -or ($before.head -ne $after.head)
$changeExpectationSatisfied = if ($ExpectChange) { $targetChanged } elseif ($ExpectNoChange) { -not $targetChanged } else { $true }
$requiredResponseSatisfied = if ([string]::IsNullOrWhiteSpace($RequiredResponseText)) { $true } else { $stdout.IndexOf($RequiredResponseText, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
$repoStatusUnchanged = $before.status -eq $after.status
$allowedTargetPaths = @($TargetPath | ForEach-Object { $_.Replace('\', '/') })
$unexpectedWorktreeChange = $false
if ($ExpectNoChange) {
    $unexpectedWorktreeChange = -not $repoStatusUnchanged
} else {
    $beforeStatusLines = @($before.status -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $beforeStatusSet = @{}
    foreach ($line in $beforeStatusLines) {
        $beforeStatusSet[$line] = $true
    }
    $afterStatusLines = @($after.status -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    foreach ($line in $afterStatusLines) {
        if ($beforeStatusSet.ContainsKey($line)) {
            continue
        }
        $statusPath = Get-StatusPath -StatusLine $line
        if ($allowedTargetPaths -notcontains $statusPath) {
            $unexpectedWorktreeChange = $true
            break
        }
    }
}
$claudeResultText = if ($Transport -eq 'Mcp') { if ($null -ne $parsed) { [string]$parsed.result_text } else { '' } } elseif ($null -ne $parsed) { [string]$parsed.result } else { '' }
$claudeUsage = if ($null -ne $parsed) { $parsed.usage } else { $null }
$claudeModelUsage = if ($null -ne $parsed) { $parsed.modelUsage } else { $null }
$accepted = (-not $timedOut) -and ($exitCode -eq 0) -and ($subtype -eq 'success') -and
    (-not [string]::IsNullOrWhiteSpace($stdout)) -and (-not $isError) -and
    ($permissionDenials.Count -eq 0) -and (-not $branchOrHeadChanged) -and
    (-not $unexpectedWorktreeChange) -and $changeExpectationSatisfied -and $requiredResponseSatisfied

$result = [ordered]@{
    started_at = $started.ToString('o')
    duration_ms = $stopwatch.ElapsedMilliseconds
    claude_duration_ms = if ($null -ne $parsed) { $parsed.duration_ms } else { $null }
    claude_duration_api_ms = if ($null -ne $parsed) { $parsed.duration_api_ms } else { $null }
    claude_total_cost_usd = if ($null -ne $parsed) { $parsed.total_cost_usd } else { $null }
    claude_input_tokens = if ($null -ne $claudeUsage) { $claudeUsage.input_tokens } else { $null }
    claude_output_tokens = if ($null -ne $claudeUsage) { $claudeUsage.output_tokens } else { $null }
    claude_session_id = if ($null -ne $parsed) { $parsed.session_id } else { $null }
    claude_result_chars = $claudeResultText.Length
    claude_model_usage = $claudeModelUsage
    working_directory = $WorkingDirectory
    transport = $Transport.ToLowerInvariant()
    mcp_protocol_version = if ($null -ne $mcpReceipt) { $mcpReceipt.protocol_version } else { $null }
    mcp_server_name = if ($null -ne $mcpReceipt) { $mcpReceipt.server_name } else { $null }
    mcp_server_version = if ($null -ne $mcpReceipt) { $mcpReceipt.server_version } else { $null }
    mcp_tool_count = if ($null -ne $mcpReceipt) { $mcpReceipt.tool_count } else { $null }
    mcp_agent_tool_available = if ($null -ne $mcpReceipt) { $mcpReceipt.agent_tool_available } else { $null }
    mcp_agent_type = if ($null -ne $mcpReceipt) { $mcpReceipt.agent_type } else { $null }
    mcp_protocol_error = if ($null -ne $mcpReceipt) { $mcpReceipt.protocol_error } else { $mcpTransportError }
    mcp_receipt = $mcpReceipt
    model_override = if ([string]::IsNullOrWhiteSpace($Model)) { $null } else { $Model }
    budget_override_usd = if ($null -eq $BudgetUsd) { $null } else { [decimal]$BudgetUsd }
    timeout_seconds = if ($null -eq $TimeoutSeconds) { $null } else { [int]$TimeoutSeconds }
    allowed_tools = $AllowedTools
    required_response_text = if ([string]::IsNullOrWhiteSpace($RequiredResponseText)) { $null } else { $RequiredResponseText }
    required_response_satisfied = $requiredResponseSatisfied
    process_exit_code = $exitCode
    timed_out = $timedOut
    claude_subtype = $subtype
    claude_is_error = $isError
    response_non_empty = -not [string]::IsNullOrWhiteSpace($stdout)
    json_parse_error = $parseError
    permission_denials = $permissionDenials
    branch_before = $before.branch
    branch_after = $after.branch
    head_before = $before.head
    head_after = $after.head
    branch_or_head_changed = $branchOrHeadChanged
    target_paths = $TargetPath
    target_changed = $targetChanged
    expect_change = [bool]$ExpectChange
    expect_no_change = [bool]$ExpectNoChange
    change_expectation_satisfied = $changeExpectationSatisfied
    repo_status_unchanged = $repoStatusUnchanged
    unexpected_worktree_change = $unexpectedWorktreeChange
    accepted = $accepted
    status_before = $before.status
    status_after = $after.status
    changed_paths_after = $after.changed_paths
    staged_paths_after = $after.staged_paths
    stdout = $stdout
    stderr = $stderr
}

$json = $result | ConvertTo-Json -Depth 12
if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
    $parent = Split-Path -Parent $ResultPath
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $resolvedResultPath = $ResultPath
    if (Test-Path -LiteralPath $ResultPath) {
        $resolvedResultPath = (Resolve-Path -LiteralPath $ResultPath).Path
    }
    [IO.File]::WriteAllText($resolvedResultPath, $json, [Text.UTF8Encoding]::new($false))
}
Write-Output $json

if (-not $accepted) {
    exit 1
}
