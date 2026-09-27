[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$LogRoot = "",
    [string]$OutputDir = "",
    [datetime]$WindowStart = (Get-Date).AddMinutes(-15),
    [datetime]$WindowEnd = (Get-Date),
    [switch]$SkipLiveSystemState
)

$ErrorActionPreference = "Stop"
$CollectedAt = Get-Date
$Warnings = @()
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Protect-SensitiveText {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) { return "" }
    $result = $Text

    # URL 只保留 scheme、主机与路径，查询参数和 fragment 不进入证据包。
    $urlPattern = '(?i)\b(?<base>[a-z][a-z0-9+.-]*://[^\s?#<>"'']+)(?<query>\?[^\s#<>"'']*)?(?<fragment>#[^\s<>"'']*)?'
    $result = [regex]::Replace($result, $urlPattern, {
        param($match)
        $safe = $match.Groups['base'].Value
        $safe = [regex]::Replace(
            $safe,
            '^(?<scheme>[a-z][a-z0-9+.-]*://)[^/?#@]*@',
            '${scheme}',
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if ($match.Groups['query'].Success) { $safe += '?[REDACTED]' }
        if ($match.Groups['fragment'].Success) { $safe += '#[REDACTED]' }
        $safe
    })

    $patterns = @(
        @{ Pattern = '(?i)([?&#](?:code|state|nonce|token|access_token|refresh_token|id_token|code_verifier|code_challenge|csrf_token|extension_server_csrf_token)=)[^&\s<>"'']+'; Replacement = '$1[REDACTED]' },
        @{ Pattern = '(?i)(\b(?:access_token|refresh_token|id_token|authorization_code|code_verifier|code_challenge|client_secret|csrf_token|extension_server_csrf_token)\b["'']?\s*[:=]\s*["'']?)[^\s,;"''}\]]+'; Replacement = '$1[REDACTED]' },
        @{ Pattern = '(?i)(\b(?:code|state|nonce|token)\b["'']?\s*[:=]\s*["'']?)[^\s,;"''}\]]+'; Replacement = '$1[REDACTED]' },
        @{ Pattern = '(?i)(--(?:access[-_]token|refresh[-_]token|id[-_]token|csrf[-_]token|extension[-_]server[-_]csrf[-_]token|token|code|state|nonce)\s+)(?:"[^"]+"|''[^'']+''|\S+)'; Replacement = '$1[REDACTED]' },
        @{ Pattern = '(?i)((?:%3F|%26)(?:code|state|nonce|token|access_token|refresh_token|id_token|code_verifier)%3D)[A-Za-z0-9._~%+/-]+'; Replacement = '$1[REDACTED]' },
        @{ Pattern = '(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+'; Replacement = '$1[REDACTED]' },
        @{ Pattern = '\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'; Replacement = '[REDACTED_JWT]' }
    )
    foreach ($item in $patterns) { $result = [regex]::Replace($result, $item.Pattern, $item.Replacement) }
    return $result
}

function Add-CollectionWarning {
    param([string]$Message)
    $safe = Protect-SensitiveText $Message
    $script:Warnings += $safe
    Write-Warning $safe
}

function Write-Utf8Text {
    param([string]$Path, [AllowEmptyString()][string]$Content)
    [IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}

function Test-LogTimestamp {
    param([string]$Line, [ref]$Timestamp)
    $Timestamp.Value = [datetime]::MinValue
    if ($Line -notmatch '(?<time>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,7})?(?:Z|[+-]\d{2}:?\d{2})?)') {
        return $false
    }
    $parsed = [datetimeoffset]::MinValue
    $styles = [Globalization.DateTimeStyles]::AllowWhiteSpaces -bor [Globalization.DateTimeStyles]::AssumeLocal
    if (-not [datetimeoffset]::TryParse(
        $Matches['time'].Replace(',', '.'),
        [Globalization.CultureInfo]::InvariantCulture,
        $styles,
        [ref]$parsed
    )) { return $false }
    $Timestamp.Value = $parsed.LocalDateTime
    return $true
}

function Export-WindowedLog {
    param([IO.FileInfo]$File, [string]$Category, [string]$RelativeOutput)
    try {
        $lines = @(Get-Content -LiteralPath $File.FullName -Encoding UTF8 -ErrorAction Stop)
        $selected = New-Object 'System.Collections.Generic.List[string]'
        $hasTimestamp = $false
        $includeContinuation = $false
        foreach ($line in $lines) {
            $lineTime = [datetime]::MinValue
            if (Test-LogTimestamp -Line $line -Timestamp ([ref]$lineTime)) {
                $hasTimestamp = $true
                $includeContinuation = $lineTime -ge $WindowStart -and $lineTime -le $WindowEnd
            }
            if ($includeContinuation) { $selected.Add((Protect-SensitiveText $line)) }
        }

        $mode = "行时间戳"
        if (-not $hasTimestamp) {
            $selected.Clear()
            $mode = "文件修改时间回退（文件内无可解析时间戳）"
            if ($File.LastWriteTime -ge $WindowStart -and $File.LastWriteTime -le $WindowEnd) {
                foreach ($line in $lines) { $selected.Add((Protect-SensitiveText $line)) }
            }
        }

        if ($selected.Count -gt 0) {
            $target = Join-Path $OutputDir $RelativeOutput
            $header = @(
                "# 类别: $Category", "# 来源: $(Protect-SensitiveText $File.FullName)",
                "# 时间窗: $($WindowStart.ToString('yyyy-MM-dd HH:mm:ss zzz')) -> $($WindowEnd.ToString('yyyy-MM-dd HH:mm:ss zzz'))",
                "# 过滤: $mode；敏感字段已做尽力遮蔽", ""
            )
            [IO.File]::WriteAllLines($target, [string[]]($header + $selected.ToArray()), $Utf8NoBom)
        }

        return [PSCustomObject]@{
            Category = $Category; SourcePath = (Protect-SensitiveText $File.FullName)
            LastWriteTime = $File.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss zzz')
            FilterMode = $mode; LinesWritten = $selected.Count
            OutputFile = $(if ($selected.Count -gt 0) { $RelativeOutput.Replace('\', '/') } else { "" })
            Error = ""
        }
    } catch {
        return [PSCustomObject]@{
            Category = $Category; SourcePath = (Protect-SensitiveText $File.FullName)
            LastWriteTime = $File.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss zzz')
            FilterMode = "读取失败"; LinesWritten = 0; OutputFile = ""
            Error = (Protect-SensitiveText $_.Exception.Message)
        }
    }
}

function Get-ProcessClassification {
    param([string]$Name, [string]$CommandLine)
    $text = "$Name $CommandLine"
    if ($text -match '(?i)--open-url|antigravity-ide://') { return "协议回调启动" }
    if ($text -match '(?i)--uninstall|--squirrel-uninstall|update\.exe|setup\.exe') {
        return "命令行呈现安装/更新/卸载辅助特征"
    }
    if ($Name -match '(?i)^language_server') { return "语言服务器" }
    if ($CommandLine -match '(?i)--type=([^\s"'']+)') { return "IDE 子进程 ($($Matches[1]))" }
    if ($Name -match '(?i)^Antigravity(?: IDE)?\.exe$') { return "IDE 主进程或第二实例" }
    return "Antigravity 派生进程"
}

if ($WindowEnd -lt $WindowStart) { throw "WindowEnd 不能早于 WindowStart。" }

if ($SkipLiveSystemState) {
    $ProcessSnapshot = @()
} else {
    try { $ProcessSnapshot = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop) }
    catch {
        $ProcessSnapshot = @()
        Add-CollectionWarning "读取 Win32_Process 失败: $($_.Exception.Message)"
    }
}

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $candidates = @($ProcessSnapshot | Where-Object {
        $_.Name -match '(?i)^Antigravity(?: IDE)?\.exe$' -and $_.ExecutablePath
    } | ForEach-Object { Split-Path -Parent $_.ExecutablePath })
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Antigravity IDE'
        $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Antigravity'
    }
    $InstallDir = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -First 1
    if (-not $InstallDir -and $candidates.Count -gt 0) { $InstallDir = $candidates[0] }
}
if ([string]::IsNullOrWhiteSpace($LogRoot) -and $env:APPDATA) {
    $logRootCandidates = @(
        (Join-Path $env:APPDATA 'Antigravity IDE\logs'),
        (Join-Path $env:APPDATA 'Antigravity\logs')
    )
    $LogRoot = $logRootCandidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Container
    } | Select-Object -First 1
    if (-not $LogRoot) { $LogRoot = $logRootCandidates[0] }
}
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path ([IO.Path]::GetTempPath()) ('antigravity-oauth-diagnostics-' + $CollectedAt.ToString('yyyyMMdd-HHmmss'))
}
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
if (Test-Path -LiteralPath $OutputDir) { throw "OutputDir 已存在，请指定新目录: $OutputDir" }
New-Item -ItemType Directory -Path (Join-Path $OutputDir 'logs\proxy') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutputDir 'logs\ide') -Force | Out-Null

Write-Host "[采集] 输出目录: $OutputDir"
Write-Host "[采集] 时间窗: $($WindowStart.ToString('yyyy-MM-dd HH:mm:ss zzz')) -> $($WindowEnd.ToString('yyyy-MM-dd HH:mm:ss zzz'))"
Write-Host "[采集] 安装文件、版本与协议注册..."

$dllPaths = @{}
if ($InstallDir) {
    $expected = Join-Path $InstallDir 'version.dll'
    $dllPaths[$expected.ToLowerInvariant()] = $expected
    if (Test-Path -LiteralPath $InstallDir -PathType Container) {
        try {
            Get-ChildItem -LiteralPath $InstallDir -File -Recurse -ErrorAction Stop | Where-Object {
                $_.Name -in @('version.dll', 'dbghelp.dll', 'antigravity_proxy.dll')
            } | ForEach-Object { $dllPaths[$_.FullName.ToLowerInvariant()] = $_.FullName }
        } catch { Add-CollectionWarning "枚举安装目录 DLL 失败: $($_.Exception.Message)" }
    }
} else { Add-CollectionWarning "没有定位到安装目录；请显式传入 -InstallDir。" }

$Dlls = @($dllPaths.Values | Sort-Object | ForEach-Object {
    $path = $_; $exists = Test-Path -LiteralPath $path -PathType Leaf
    $file = $null; $hash = ""; $errorText = ""
    if ($exists) {
        try {
            $file = Get-Item -LiteralPath $path -ErrorAction Stop
            $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
        } catch { $errorText = Protect-SensitiveText $_.Exception.Message }
    }
    [PSCustomObject]@{
        Path = (Protect-SensitiveText $path); Exists = $exists
        SizeBytes = $(if ($file) { $file.Length } else { $null })
        LastWriteTime = $(if ($file) { $file.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss zzz') } else { "" })
        SHA256 = $hash; Error = $errorText
    }
})

$exePaths = @{}
if ($InstallDir) {
    foreach ($name in @('Antigravity IDE.exe', 'Antigravity.exe')) {
        $path = Join-Path $InstallDir $name; $exePaths[$path.ToLowerInvariant()] = $path
    }
    if (Test-Path -LiteralPath $InstallDir -PathType Container) {
        try {
            Get-ChildItem -LiteralPath $InstallDir -File -Filter '*.exe' -Recurse -ErrorAction Stop | Where-Object {
                $_.Name -match '(?i)^Antigravity(?: IDE)?\.exe$'
            } | ForEach-Object { $exePaths[$_.FullName.ToLowerInvariant()] = $_.FullName }
        } catch { Add-CollectionWarning "枚举 IDE 可执行文件失败: $($_.Exception.Message)" }
    }
}
$IdeVersions = @($exePaths.Values | Sort-Object | ForEach-Object {
    $path = $_; $exists = Test-Path -LiteralPath $path -PathType Leaf
    $file = $null; $info = $null; $hash = ""; $errorText = ""
    if ($exists) {
        try {
            $file = Get-Item -LiteralPath $path -ErrorAction Stop
            $info = [Diagnostics.FileVersionInfo]::GetVersionInfo($path)
            $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
        }
        catch { $errorText = Protect-SensitiveText $_.Exception.Message }
    }
    [PSCustomObject]@{
        Path = (Protect-SensitiveText $path); Exists = $exists
        SizeBytes = $(if ($file) { $file.Length } else { $null })
        LastWriteTime = $(if ($file) { $file.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss zzz') } else { "" })
        SHA256 = $hash; FileVersion = $(if ($info) { $info.FileVersion } else { "" })
        ProductVersion = $(if ($info) { $info.ProductVersion } else { "" }); Error = $errorText
    }
})

$registryLocations = @(
    @{ Scope = 'HKCU'; Hive = [Microsoft.Win32.RegistryHive]::CurrentUser; View = [Microsoft.Win32.RegistryView]::Default },
    @{ Scope = 'HKLM-64'; Hive = [Microsoft.Win32.RegistryHive]::LocalMachine; View = [Microsoft.Win32.RegistryView]::Registry64 },
    @{ Scope = 'HKLM-32'; Hive = [Microsoft.Win32.RegistryHive]::LocalMachine; View = [Microsoft.Win32.RegistryView]::Registry32 }
)
$ProtocolRegistrations = @()
if (-not $SkipLiveSystemState) {
    $ProtocolRegistrations = @($registryLocations | ForEach-Object {
        $subKeyPath = 'Software\Classes\antigravity-ide\shell\open\command'
        $baseKey = $null; $key = $null; $exists = $false; $command = ""; $errorText = ""
        try {
            $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey($_.Hive, $_.View)
            $key = $baseKey.OpenSubKey($subKeyPath, $false)
            $exists = $null -ne $key
            if ($exists) { $command = Protect-SensitiveText ([string]$key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)) }
        } catch { $errorText = Protect-SensitiveText $_.Exception.Message }
        finally {
            if ($key) { $key.Dispose() }
            if ($baseKey) { $baseKey.Dispose() }
        }
        [PSCustomObject]@{ Scope = $_.Scope; RegistryPath = "$($_.Scope)\$subKeyPath"; Exists = $exists; Command = $command; Error = $errorText }
    })
}

Write-Host "[采集] 当前 Antigravity 进程树（不会停止进程）..."
$selectedPids = New-Object 'System.Collections.Generic.HashSet[uint32]'
$prefix = if ($InstallDir) { $InstallDir.TrimEnd('\') + '\' } else { "" }
foreach ($process in $ProcessSnapshot) {
    $matchesName = $process.Name -match '(?i)^Antigravity(?: IDE)?\.exe$'
    $matchesPath = $prefix -and $process.ExecutablePath -and $process.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    if ($matchesName -or $matchesPath) { [void]$selectedPids.Add([uint32]$process.ProcessId) }
}
do {
    $added = $false
    foreach ($process in $ProcessSnapshot) {
        if ($selectedPids.Contains([uint32]$process.ParentProcessId) -and $selectedPids.Add([uint32]$process.ProcessId)) { $added = $true }
    }
} while ($added)
$Processes = @($ProcessSnapshot | Where-Object { $selectedPids.Contains([uint32]$_.ProcessId) } | Sort-Object ProcessId | ForEach-Object {
    $commandLine = if ($null -eq $_.CommandLine) { "[不可读取]" } else { [string]$_.CommandLine }
    [PSCustomObject]@{
        PID = $_.ProcessId; PPID = $_.ParentProcessId; Name = $_.Name
        Classification = (Get-ProcessClassification -Name $_.Name -CommandLine $commandLine)
        CreationTime = $(if ($_.CreationDate) { ([datetime]$_.CreationDate).ToString('yyyy-MM-dd HH:mm:ss zzz') } else { "" })
        ExecutablePath = (Protect-SensitiveText ([string]$_.ExecutablePath))
        CommandLine = (Protect-SensitiveText $commandLine)
    }
})

Write-Host "[采集] 指定时间窗内的 proxy 与 IDE main/auth 日志..."
$logCandidates = @{}; $proxyRoots = @()
if ($InstallDir) { $proxyRoots += Join-Path $InstallDir 'logs' }
if ($env:TEMP) { $proxyRoots += Join-Path $env:TEMP 'antigravity-proxy-logs' }
if ($LogRoot) { $proxyRoots += $LogRoot }
foreach ($root in $proxyRoots) {
    if (Test-Path -LiteralPath $root -PathType Container) {
        try {
            Get-ChildItem -LiteralPath $root -File -Filter 'proxy-*.log' -ErrorAction Stop | ForEach-Object {
                $logCandidates[$_.FullName.ToLowerInvariant()] = @{ File = $_; Category = 'proxy' }
            }
        } catch { Add-CollectionWarning "枚举 proxy 日志失败 ($root): $($_.Exception.Message)" }
    }
}
if ($LogRoot -and (Test-Path -LiteralPath $LogRoot -PathType Container)) {
    try {
        Get-ChildItem -LiteralPath $LogRoot -File -Filter '*.log' -Recurse -ErrorAction Stop | Where-Object {
            $_.BaseName -match '(?i)(^|[-_.])(main|auth)([-_.]|$)' -or $_.BaseName -match '(?i)auth'
        } | ForEach-Object {
            $key = $_.FullName.ToLowerInvariant()
            if (-not $logCandidates.ContainsKey($key)) { $logCandidates[$key] = @{ File = $_; Category = 'ide' } }
        }
    } catch { Add-CollectionWarning "枚举 IDE main/auth 日志失败 ($LogRoot): $($_.Exception.Message)" }
} elseif ($LogRoot) { Add-CollectionWarning "IDE 日志根目录不存在: $LogRoot" }

$Logs = @(); $proxyIndex = 0; $ideIndex = 0
foreach ($candidate in ($logCandidates.Values | Sort-Object Category, @{ Expression = { $_.File.FullName } })) {
    if ($candidate.Category -eq 'proxy') {
        $proxyIndex++; $relative = 'logs\proxy\proxy-{0:D3}-{1}' -f $proxyIndex, $candidate.File.Name
    } else {
        $ideIndex++; $safeName = $candidate.File.Name -replace '[^A-Za-z0-9._-]', '_'
        $relative = 'logs\ide\ide-{0:D3}-{1}' -f $ideIndex, $safeName
    }
    $row = Export-WindowedLog -File $candidate.File -Category $candidate.Category -RelativeOutput $relative
    $Logs += $row
    if ($row.Error) { Add-CollectionWarning "读取日志失败 ($($row.SourcePath)): $($row.Error)" }
}

$Evidence = [PSCustomObject]@{
    Context = [PSCustomObject]@{
        CollectedAt = $CollectedAt.ToString('yyyy-MM-dd HH:mm:ss zzz')
        PowerShell = $PSVersionTable.PSVersion.ToString()
        LiveSystemStateSkipped = [bool]$SkipLiveSystemState
        InstallDir = (Protect-SensitiveText $InstallDir); LogRoot = (Protect-SensitiveText $LogRoot)
        WindowStart = $WindowStart.ToString('yyyy-MM-dd HH:mm:ss zzz'); WindowEnd = $WindowEnd.ToString('yyyy-MM-dd HH:mm:ss zzz')
    }
    InstalledDlls = $Dlls; IdeVersions = $IdeVersions
    ProtocolRegistrations = $ProtocolRegistrations; Processes = $Processes
    Logs = $Logs; Warnings = $Warnings
}
Write-Utf8Text -Path (Join-Path $OutputDir 'summary.json') -Content ($Evidence | ConvertTo-Json -Depth 6)

$lineTotal = ($Logs | Measure-Object -Property LinesWritten -Sum).Sum
if ($null -eq $lineTotal) { $lineTotal = 0 }
$summary = @(
    '# Antigravity OAuth 跳转诊断证据包', '',
    "- 采集时间：$($CollectedAt.ToString('yyyy-MM-dd HH:mm:ss zzz'))",
    "- 安装目录：$(Protect-SensitiveText $InstallDir)", "- IDE 日志根目录：$(Protect-SensitiveText $LogRoot)",
    "- 时间窗：$($WindowStart.ToString('yyyy-MM-dd HH:mm:ss zzz')) 至 $($WindowEnd.ToString('yyyy-MM-dd HH:mm:ss zzz'))", '',
    '## 边界', '',
    '- 只读取安装文件、注册表、进程信息和源日志；不停止进程、不写注册表、不修改安装目录或源日志。',
    '- 仅在本输出目录创建 `summary.md`、`summary.json` 和已过滤的 `logs/` 副本。',
    '- URL query/fragment、OAuth 参数、Bearer/JWT 已做尽力遮蔽；分享前仍需人工复核。', '',
    '## 结果', '',
    "- DLL 记录：$($Dlls.Count)", "- IDE 版本记录：$($IdeVersions.Count)",
    "- 协议注册位置：$($ProtocolRegistrations.Count)", "- 当前相关进程：$($Processes.Count)",
    "- 候选日志：$($Logs.Count)", "- 写入日志行：$lineTotal", "- 采集告警：$($Warnings.Count)", '',
    '结构化证据见 `summary.json`；按时间窗提取且已遮蔽的日志见 `logs/`。'
) -join [Environment]::NewLine
Write-Utf8Text -Path (Join-Path $OutputDir 'summary.md') -Content ($summary + [Environment]::NewLine)
Write-Host "[完成] 证据包已生成: $OutputDir"
Write-Host "[提示] 分享前请人工复核 summary.md、summary.json 与日志副本。"
