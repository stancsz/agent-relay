[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResultPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ResultPath -PathType Leaf)) {
    throw "Result file does not exist: $ResultPath"
}

$outer = Get-Content -LiteralPath $ResultPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($outer.stdout)) {
    throw 'The delegation receipt contains no Claude stdout.'
}
$claude = $outer.stdout | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($claude.result)) {
    throw 'The Claude receipt contains no result text.'
}

$parent = Split-Path -Parent $OutputPath
if (-not [string]::IsNullOrWhiteSpace($parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}
[IO.File]::WriteAllText($OutputPath, [string]$claude.result, [Text.UTF8Encoding]::new($false))
[ordered]@{
    source_result = (Resolve-Path -LiteralPath $ResultPath).Path
    output_path = $OutputPath
    claude_subtype = $claude.subtype
    response_chars = ([string]$claude.result).Length
} | ConvertTo-Json
