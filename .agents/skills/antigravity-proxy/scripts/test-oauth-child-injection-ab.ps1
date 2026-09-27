[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Tool = Join-Path $PSScriptRoot 'set-oauth-child-injection-ab.ps1'
$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$FixtureRoot = Join-Path $TempRoot ('antigravity-oauth-ab-' + [guid]::NewGuid().ToString('N'))
$ConfigPath = Join-Path $FixtureRoot 'config.json'
$BackupPath = "$ConfigPath.oauth-ab.bak"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$OriginalJson = @'
{
  "child_injection": true,
  "proxy": {
    "host": "127.0.0.1",
    "port": 7890
  },
  "备注": "OAuth A/B"
}
'@

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) { throw $Message }
}

New-Item -ItemType Directory -Path $FixtureRoot | Out-Null
[IO.File]::WriteAllText($ConfigPath, $OriginalJson, $Utf8NoBom)

try {
    $disableOutput = & $Tool -Action Disable -ConfigPath $ConfigPath -SkipProcessCheck | Out-String
    $disabled = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json

    Assert-True ($disabled.child_injection -eq $false) 'Disable 没有把 child_injection 设为 false。'
    Assert-True (Test-Path -LiteralPath $BackupPath -PathType Leaf) 'Disable 没有创建原始备份。'
    Assert-True ([IO.File]::ReadAllText($BackupPath) -eq $OriginalJson) '原始备份内容发生变化。'
    Assert-True ($disableOutput.Contains('OAuth A/B 已禁用 child_injection')) 'Disable 缺少完成提示。'

    $restoreOutput = & $Tool -Action Restore -ConfigPath $ConfigPath -SkipProcessCheck | Out-String
    Assert-True ([IO.File]::ReadAllText($ConfigPath) -eq $OriginalJson) 'Restore 没有原样恢复配置。'
    Assert-True (Test-Path -LiteralPath $BackupPath -PathType Leaf) 'Restore 不应删除原始备份。'
    Assert-True ($restoreOutput.Contains('已从 A/B 备份原样恢复')) 'Restore 缺少完成提示。'

    Write-Output '[通过] OAuth child_injection A/B 脚本回归测试通过。'
} finally {
    $resolvedFixtureRoot = [IO.Path]::GetFullPath($FixtureRoot)
    if ($resolvedFixtureRoot.StartsWith($TempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedFixtureRoot)) {
        Remove-Item -LiteralPath $resolvedFixtureRoot -Recurse -Force
    }
}
