[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$delegate = Join-Path $PSScriptRoot 'claude_delegate.ps1'
$successBin = Join-Path $PSScriptRoot 'test-fixtures\fake-claude-success'
$unrelatedBin = Join-Path $PSScriptRoot 'test-fixtures\fake-claude-unrelated'
$mcpBin = Join-Path $PSScriptRoot 'test-fixtures\fake-mcp-success'
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('claude-delegator-smoke-' + [Guid]::NewGuid().ToString('N'))

function Invoke-Smoke {
    param(
        [string]$BinPath,
        [string]$ResultPath,
        [switch]$ExpectFailure
    )
    $oldPath = $env:Path
    try {
        $env:Path = "$BinPath;$oldPath"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $delegate `
            -WorkingDirectory $tempRoot `
            -Prompt 'Return the smoke result.' `
            -TargetPath 'target.txt' `
            -ExpectNoChange `
            -Transport Cli `
            -AllowedTools 'Read,Bash' `
            -ResultPath $ResultPath | Out-Null
        $exitCode = $LASTEXITCODE
    } finally {
        $env:Path = $oldPath
    }
    $receipt = Get-Content -Raw -LiteralPath $ResultPath | ConvertFrom-Json
    if ($ExpectFailure) {
        if ($exitCode -eq 0 -or $receipt.accepted -or -not $receipt.unexpected_worktree_change) {
            throw 'Unexpected-worktree smoke test did not reject the unrelated change.'
        }
    } else {
        if ($exitCode -ne 0 -or -not $receipt.accepted -or $receipt.timeout_seconds -ne $null -or $receipt.model_override -ne $null -or $receipt.budget_override_usd -ne $null) {
            throw 'Success smoke test did not accept the unbounded default handoff.'
        }
    }
    return $receipt
}

function Invoke-McpSmoke {
    $oldPath = $env:Path
    $resultPath = Join-Path $tempRoot 'mcp.json'
    try {
        $env:Path = "$mcpBin;$oldPath"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $delegate `
            -WorkingDirectory $tempRoot `
            -Prompt 'Return the MCP smoke result.' `
            -TargetPath 'target.txt' `
            -ExpectNoChange `
            -Transport Mcp `
            -ResultPath $resultPath | Out-Null
        $exitCode = $LASTEXITCODE
    } finally { $env:Path = $oldPath }
    $receipt = Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json
    if ($exitCode -ne 0 -or -not $receipt.accepted -or $receipt.transport -ne 'mcp' -or $receipt.mcp_protocol_version -ne '2025-06-18' -or $receipt.mcp_agent_tool_available -ne $true) {
        throw 'MCP smoke test did not accept the fake MCP Agent handoff.'
    }
    return $receipt
}

try {
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    [IO.File]::WriteAllText((Join-Path $tempRoot 'target.txt'), 'stable`n', [Text.UTF8Encoding]::new($false))
    & git init --quiet $tempRoot
    & git -C $tempRoot config user.email smoke@example.invalid
    & git -C $tempRoot config user.name smoke
    & git -C $tempRoot add target.txt
    & git -C $tempRoot commit --quiet -m init

    $successResult = Join-Path $tempRoot 'success.json'
    $success = Invoke-Smoke -BinPath $successBin -ResultPath $successResult
    $successArgs = Get-Content -Raw -LiteralPath (Join-Path $successBin 'last-args.txt')
    if ($successArgs -match '--model|--max-budget-usd') {
        throw "Default smoke handoff unexpectedly passed model/budget flags: $successArgs"
    }

    $mcp = Invoke-McpSmoke

    $unrelatedResult = Join-Path $tempRoot 'unrelated.json'
    $unrelated = Invoke-Smoke -BinPath $unrelatedBin -ResultPath $unrelatedResult -ExpectFailure
    [ordered]@{
        success_accepted = $success.accepted
        success_timeout_seconds = $success.timeout_seconds
        success_model_override = $success.model_override
        success_budget_override_usd = $success.budget_override_usd
        mcp_accepted = $mcp.accepted
        mcp_server = $mcp.mcp_server_name
        unrelated_accepted = $unrelated.accepted
        unrelated_worktree_rejected = $unrelated.unexpected_worktree_change
    } | ConvertTo-Json
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
