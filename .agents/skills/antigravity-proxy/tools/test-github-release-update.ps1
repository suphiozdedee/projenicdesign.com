# ============================================================
#  Antigravity-Proxy GitHub Release 更新检查验证脚本
#  只验证 latest release API 和代理站可达性，不下载 Release 资产。
# ============================================================

[CmdletBinding()]
param(
    [string]$Owner = "yuaotian",
    [string]$Repo = "antigravity-proxy",
    [int]$TimeoutSec = 5,
    [switch]$SkipCertificateCheck
)

$ErrorActionPreference = "Stop"

$latestApi = "https://api.github.com/repos/$Owner/$Repo/releases/latest"
$mirrors = @(
    "https://wget.la/",
    "https://rapidgit.jjda.de5.net/",
    "https://fastgit.cc/",
    "https://gitproxy.mrhjx.cn/",
    "https://github.boki.moe/",
    "https://github.ednovas.xyz/"
)

function Join-MirrorUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Mirror,
        [Parameter(Mandatory = $true)][string]$Target
    )
    $m = $Mirror.Trim()
    if (-not $m.EndsWith("/")) { $m += "/" }
    return "$m$Target"
}

function Test-ReleaseEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url
    )

    $result = [ordered]@{
        name = $Name
        url = $Url
        ok = $false
        elapsed_ms = $null
        tag_name = $null
        html_url = $null
        error = $null
    }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $params = @{
            Uri = $Url
            Method = "GET"
            TimeoutSec = $TimeoutSec
            Headers = @{
                "Accept" = "application/vnd.github+json"
                "X-GitHub-Api-Version" = "2022-11-28"
                "User-Agent" = "antigravity-proxy-update-test"
            }
        }
        if ($SkipCertificateCheck -and (Get-Command Invoke-RestMethod).Parameters.ContainsKey("SkipCertificateCheck")) {
            $params.SkipCertificateCheck = $true
        }
        $json = Invoke-RestMethod @params
        $result.ok = [bool]$json.tag_name
        $result.tag_name = [string]$json.tag_name
        $result.html_url = [string]$json.html_url
    } catch {
        $result.error = $_.Exception.Message
    } finally {
        $sw.Stop()
        $result.elapsed_ms = [int]$sw.ElapsedMilliseconds
    }

    [pscustomobject]$result
}

$targets = @(
    [pscustomobject]@{ Name = "github"; Url = $latestApi }
)
foreach ($mirror in $mirrors) {
    $targets += [pscustomobject]@{
        Name = "mirror:$mirror"
        Url = Join-MirrorUrl -Mirror $mirror -Target $latestApi
    }
}

$results = foreach ($target in $targets) {
    Test-ReleaseEndpoint -Name $target.Name -Url $target.Url
}

$results | Sort-Object @{ Expression = "ok"; Descending = $true }, elapsed_ms | Format-Table -AutoSize

$best = $results | Where-Object { $_.ok } | Sort-Object elapsed_ms | Select-Object -First 1
if ($best) {
    Write-Host ""
    Write-Host "最佳可用源: $($best.name)  $($best.elapsed_ms)ms  tag=$($best.tag_name)" -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "未找到可用源，请检查网络、代理站或本地代理设置。" -ForegroundColor Yellow
exit 1
