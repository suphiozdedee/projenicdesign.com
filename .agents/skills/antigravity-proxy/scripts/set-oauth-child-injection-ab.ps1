[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Disable', 'Restore')]
    [string]$Action,

    [string]$ConfigPath,

    [switch]$SkipProcessCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    if (-not $env:LOCALAPPDATA) {
        throw '无法定位 LOCALAPPDATA，请通过 -ConfigPath 指定 config.json。'
    }
    $ConfigPath = Join-Path $env:LOCALAPPDATA 'Programs\Antigravity IDE\config.json'
}

$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$BackupPath = "$ConfigPath.oauth-ab.bak"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Assert-IdeStopped {
    if ($SkipProcessCheck) { return }

    $running = @(Get-Process -Name 'Antigravity IDE', 'Antigravity' -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        $pids = ($running | Select-Object -ExpandProperty Id | Sort-Object -Unique) -join ', '
        throw "检测到 Antigravity IDE 仍在运行（PID: $pids）。请完全退出 IDE 后重试。"
    }
}

function Read-ConfigObject {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "配置文件不存在：$Path"
    }

    $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not ($value.PSObject.Properties.Name -contains 'child_injection')) {
        throw "配置缺少 child_injection：$Path"
    }
    return $value
}

function Write-ConfigObject {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $temporaryPath = "$Path.oauth-ab.tmp-$PID"
    try {
        $json = $Value | ConvertTo-Json -Depth 100
        [IO.File]::WriteAllText($temporaryPath, $json + [Environment]::NewLine, $Utf8NoBom)
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

Assert-IdeStopped

switch ($Action) {
    'Disable' {
        if (Test-Path -LiteralPath $BackupPath) {
            throw "备份已存在，为避免覆盖原始配置已停止：$BackupPath"
        }

        $config = Read-ConfigObject -Path $ConfigPath
        Copy-Item -LiteralPath $ConfigPath -Destination $BackupPath
        $config.child_injection = $false

        try {
            Write-ConfigObject -Value $config -Path $ConfigPath
            $verified = Read-ConfigObject -Path $ConfigPath
            if ($verified.child_injection -ne $false) {
                throw '写入后校验失败：child_injection 不是 false。'
            }
        } catch {
            Copy-Item -LiteralPath $BackupPath -Destination $ConfigPath -Force
            throw
        }

        Write-Output '[完成] OAuth A/B 已禁用 child_injection。'
        Write-Output "配置：$ConfigPath"
        Write-Output "原始备份：$BackupPath"
    }

    'Restore' {
        if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
            throw "未找到 A/B 原始备份：$BackupPath"
        }

        Copy-Item -LiteralPath $BackupPath -Destination $ConfigPath -Force
        $restored = Read-ConfigObject -Path $ConfigPath

        Write-Output '[完成] 已从 A/B 备份原样恢复 config.json。'
        Write-Output "配置：$ConfigPath"
        Write-Output "恢复后的 child_injection=$($restored.child_injection)"
        Write-Output "保留备份：$BackupPath"
    }
}
