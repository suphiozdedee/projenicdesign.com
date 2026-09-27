[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Collector = Join-Path $PSScriptRoot 'collect-oauth-diagnostics.ps1'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$TempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
$FixtureRoot = Join-Path $TempBase ('antigravity-oauth-test-' + [guid]::NewGuid().ToString('N'))

function Write-Utf8Fixture {
    param([string]$Path, [string[]]$Lines)
    [IO.File]::WriteAllLines($Path, $Lines, $Utf8NoBom)
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-Contains {
    param([string]$Text, [string]$Expected, [string]$Message)
    if (-not $Text.Contains($Expected)) { throw $Message }
}

function Assert-NotContains {
    param([string]$Text, [string]$Unexpected, [string]$Message)
    if ($Text.Contains($Unexpected)) { throw $Message }
}

$oldEnvironment = @{
    TEMP = $env:TEMP
    TMP = $env:TMP
    APPDATA = $env:APPDATA
    LOCALAPPDATA = $env:LOCALAPPDATA
}

try {
    $InstallDir = Join-Path $FixtureRoot 'install'
    $ProxyLogDir = Join-Path $InstallDir 'logs'
    $IdeLogRoot = Join-Path $FixtureRoot 'appdata\Antigravity IDE\logs'
    $IdeSessionDir = Join-Path $IdeLogRoot 'session-1'
    $IsolatedTemp = Join-Path $FixtureRoot 'temp'
    $OutputDir = Join-Path $FixtureRoot 'evidence'
    foreach ($path in @($ProxyLogDir, $IdeSessionDir, $IsolatedTemp)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }

    $env:TEMP = $IsolatedTemp
    $env:TMP = $IsolatedTemp
    $env:APPDATA = Join-Path $FixtureRoot 'appdata'
    $env:LOCALAPPDATA = Join-Path $FixtureRoot 'localappdata'

    $VersionDll = Join-Path $InstallDir 'version.dll'
    [IO.File]::WriteAllBytes($VersionDll, [byte[]](0x41, 0x55, 0x52, 0x41, 0x2D, 0x58))

    $PowerShellExe = (Get-Process -Id $PID).Path
    $IdeExe = Join-Path $InstallDir 'Antigravity IDE.exe'
    Copy-Item -LiteralPath $PowerShellExe -Destination $IdeExe

    $ProxyLog = Join-Path $ProxyLogDir 'proxy-20260901.log'
    Write-Utf8Fixture -Path $ProxyLog -Lines @(
        '2026-09-01 17:03:00 [信息] BEFORE_WINDOW_MARKER token=BEFORE_SECRET',
        '2026-09-01 17:05:00 [信息] INSIDE_WINDOW_MARKER antigravity-ide://oauth-success?code=CODE_SECRET_123&state=STATE_SECRET_456#FRAGMENT_SECRET',
        'Authorization: Bearer BEARER_SECRET_789',
        'jwt HEADERHEADERHEADER.PAYLOADPAYLOADPAYLOAD.SIGNATURESIGNATURE',
        'nonce=short7',
        '--csrf_token CSRF_SECRET_123 --extension_server_csrf_token EXTENSION_CSRF_SECRET_456',
        '2026-09-01 17:07:00 [信息] AFTER_WINDOW_MARKER code=AFTER_SECRET'
    )

    $AuthLog = Join-Path $IdeSessionDir 'auth.log'
    Write-Utf8Fixture -Path $AuthLog -Lines @(
        '无时间戳回退行 access_token=FALLBACK_SECRET_123',
        'callback=https://USERINFO_SECRET:PASSWORD_SECRET@accounts.example.test/callback?code=HTTPS_SECRET#HTTPS_FRAGMENT'
    )
    (Get-Item -LiteralPath $AuthLog).LastWriteTime = [datetime]'2026-09-01 17:05:10'

    $OldAuthLog = Join-Path $IdeSessionDir 'auth-old.log'
    Write-Utf8Fixture -Path $OldAuthLog -Lines @('OLD_FALLBACK_MARKER token=OLD_SECRET')
    (Get-Item -LiteralPath $OldAuthLog).LastWriteTime = [datetime]'2026-09-01 16:00:00'

    $sourceHashes = @{}
    foreach ($source in @($ProxyLog, $AuthLog, $OldAuthLog)) {
        $sourceHashes[$source] = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    }

    $WindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $WindowsPowerShell -PathType Leaf)) {
        $WindowsPowerShell = $PowerShellExe
    }
    $collectorOutput = & $WindowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $Collector `
        -InstallDir $InstallDir `
        -OutputDir $OutputDir `
        -WindowStart '2026-09-01 17:04:30' `
        -WindowEnd '2026-09-01 17:05:30' `
        -SkipLiveSystemState 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "采集脚本执行失败：$($collectorOutput -join [Environment]::NewLine)"
    }

    $SummaryJson = Join-Path $OutputDir 'summary.json'
    $SummaryMarkdown = Join-Path $OutputDir 'summary.md'
    Assert-True (Test-Path -LiteralPath $SummaryJson -PathType Leaf) '缺少 summary.json。'
    Assert-True (Test-Path -LiteralPath $SummaryMarkdown -PathType Leaf) '缺少 summary.md。'

    $Evidence = Get-Content -LiteralPath $SummaryJson -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-True ($Evidence.Context.LiveSystemStateSkipped -eq $true) '合成测试没有进入隔离系统状态模式。'
    Assert-True ($Evidence.Context.LogRoot -eq $IdeLogRoot) '默认日志根没有优先选择 Antigravity IDE\logs。'
    Assert-True (@($Evidence.Processes).Count -eq 0) '隔离模式不应采集当前进程。'
    Assert-True (@($Evidence.ProtocolRegistrations).Count -eq 0) '隔离模式不应读取协议注册表。'

    $ExpectedDllHash = (Get-FileHash -LiteralPath $VersionDll -Algorithm SHA256).Hash
    $DllRow = @($Evidence.InstalledDlls | Where-Object { $_.Path -eq $VersionDll }) | Select-Object -First 1
    Assert-True ($null -ne $DllRow) 'summary.json 缺少 version.dll 记录。'
    Assert-True ($DllRow.Exists -eq $true) 'version.dll 应标记为存在。'
    Assert-True ($DllRow.SHA256 -eq $ExpectedDllHash) 'version.dll SHA-256 不匹配。'

    $IdeRow = @($Evidence.IdeVersions | Where-Object { $_.Path -eq $IdeExe }) | Select-Object -First 1
    Assert-True ($null -ne $IdeRow -and $IdeRow.Exists -eq $true) 'summary.json 缺少 IDE 版本记录。'
    Assert-True (-not [string]::IsNullOrWhiteSpace([string]$IdeRow.FileVersion)) 'IDE FileVersion 为空。'

    $LogFiles = @(Get-ChildItem -LiteralPath (Join-Path $OutputDir 'logs') -File -Recurse)
    Assert-True ($LogFiles.Count -eq 2) '应仅生成时间窗内的 proxy 与 auth 两份日志。'
    $AllOutput = @(
        Get-Content -LiteralPath $SummaryJson -Raw -Encoding UTF8
        Get-Content -LiteralPath $SummaryMarkdown -Raw -Encoding UTF8
        $LogFiles | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 }
    ) -join [Environment]::NewLine

    Assert-Contains $AllOutput 'INSIDE_WINDOW_MARKER' '时间窗内日志行未被保留。'
    Assert-Contains $AllOutput 'antigravity-ide://oauth-success?[REDACTED]#[REDACTED]' '协议 URL 没有按预期保留路径并遮蔽参数。'
    Assert-Contains $AllOutput '[REDACTED_JWT]' 'JWT 没有被遮蔽。'
    Assert-Contains $AllOutput 'nonce=[REDACTED]' '短 nonce 没有被遮蔽。'
    Assert-Contains $AllOutput '--csrf_token [REDACTED]' '下划线 CSRF 参数没有被遮蔽。'
    Assert-Contains $AllOutput '--extension_server_csrf_token [REDACTED]' '扩展服务 CSRF 参数没有被遮蔽。'
    Assert-Contains $AllOutput 'https://accounts.example.test/callback?[REDACTED]#[REDACTED]' 'URL userinfo 没有被移除。'
    foreach ($unexpected in @(
        'BEFORE_WINDOW_MARKER', 'AFTER_WINDOW_MARKER', 'OLD_FALLBACK_MARKER',
        'CODE_SECRET_123', 'STATE_SECRET_456', 'FRAGMENT_SECRET', 'BEARER_SECRET_789',
        'HEADERHEADERHEADER.PAYLOADPAYLOADPAYLOAD.SIGNATURESIGNATURE', 'short7',
        'FALLBACK_SECRET_123', 'USERINFO_SECRET', 'PASSWORD_SECRET',
        'HTTPS_SECRET', 'HTTPS_FRAGMENT', 'CSRF_SECRET_123',
        'EXTENSION_CSRF_SECRET_456', 'OLD_SECRET'
    )) {
        Assert-NotContains $AllOutput $unexpected "输出仍包含不应出现的内容：$unexpected"
    }

    $FallbackRow = @($Evidence.Logs | Where-Object { $_.SourcePath -eq $AuthLog }) | Select-Object -First 1
    Assert-True ($FallbackRow.FilterMode -like '文件修改时间回退*') '无时间戳日志没有使用文件修改时间回退。'
    $OldFallbackRow = @($Evidence.Logs | Where-Object { $_.SourcePath -eq $OldAuthLog }) | Select-Object -First 1
    Assert-True ($OldFallbackRow.LinesWritten -eq 0 -and [string]::IsNullOrEmpty($OldFallbackRow.OutputFile)) '窗口外的无时间戳日志不应导出。'

    foreach ($source in $sourceHashes.Keys) {
        Assert-True ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -eq $sourceHashes[$source]) "源日志被修改：$source"
    }

    $ErrorActionPreference = 'Continue'
    try {
        $secondOutput = & $WindowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $Collector `
            -InstallDir $InstallDir -OutputDir $OutputDir `
            -WindowStart '2026-09-01 17:04:30' -WindowEnd '2026-09-01 17:05:30' `
            -SkipLiveSystemState 2>&1
        $secondExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = 'Stop'
    }
    Assert-True ($secondExitCode -ne 0) '复用已存在的 OutputDir 应失败。'
    Assert-Contains ($secondOutput -join [Environment]::NewLine) 'OutputDir 已存在' 'OutputDir 防覆盖错误信息不明确。'

    Write-Host '[通过] OAuth 诊断采集脚本合成回归测试通过。'
} finally {
    $env:TEMP = $oldEnvironment.TEMP
    $env:TMP = $oldEnvironment.TMP
    $env:APPDATA = $oldEnvironment.APPDATA
    $env:LOCALAPPDATA = $oldEnvironment.LOCALAPPDATA

    $resolvedFixture = [IO.Path]::GetFullPath($FixtureRoot)
    if ($resolvedFixture.StartsWith($TempBase, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedFixture) -like 'antigravity-oauth-test-*' -and
        (Test-Path -LiteralPath $resolvedFixture)) {
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    }
}
