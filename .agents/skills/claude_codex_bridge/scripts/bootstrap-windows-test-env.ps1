param(
  [string]$InstallPrefix = "$env:LOCALAPPDATA\codex-dual",
  [string]$SmokeProjectPath = "$env:USERPROFILE\Desktop\ccb-smoke",
  [string]$LogPath = "",
  [switch]$SkipGit,
  [switch]$SkipPython,
  [switch]$SkipNode,
  [switch]$SkipProviderInstall,
  [switch]$SkipCodex,
  [switch]$SkipClaude,
  [switch]$SkipGemini,
  [switch]$SkipOpenCode,
  [switch]$SkipCCSwitch,
  [switch]$SkipSmokeProject,
  [switch]$LaunchSmoke,
  [switch]$Force
)

try {
  $script:utf8NoBom = [System.Text.UTF8Encoding]::new($false)
} catch {
  $script:utf8NoBom = [System.Text.Encoding]::UTF8
}
try { $OutputEncoding = $script:utf8NoBom } catch {}
try { [Console]::OutputEncoding = $script:utf8NoBom } catch {}
try { [Console]::InputEncoding = $script:utf8NoBom } catch {}
try { chcp 65001 | Out-Null } catch {}

$ErrorActionPreference = "Stop"
$script:BootstrapScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$installScript = Join-Path $repoRoot "install.ps1"
$script:TranscriptStarted = $false
$script:LogPath = $null

function Resolve-LogPath {
  param([string]$RequestedPath)
  if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
    return $RequestedPath
  }
  $logsDir = Join-Path $script:BootstrapScriptDir "bootstrap-logs"
  New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
  return (Join-Path $logsDir "bootstrap.log")
}

function Start-BootstrapTranscript {
  param([string]$RequestedPath)
  $resolved = Resolve-LogPath -RequestedPath $RequestedPath
  $parent = Split-Path -Parent $resolved
  if (-not [string]::IsNullOrWhiteSpace($parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
  try {
    Start-Transcript -Path $resolved -Force | Out-Null
    $script:TranscriptStarted = $true
    $script:LogPath = $resolved
  } catch {
    $script:TranscriptStarted = $false
    $script:LogPath = $resolved
    Write-Warning "Failed to start transcript at $resolved : $_"
  }
}

function Stop-BootstrapTranscript {
  if ($script:TranscriptStarted) {
    try {
      Stop-Transcript | Out-Null
    } catch {}
    $script:TranscriptStarted = $false
  }
}

function Write-LogLocation {
  if (-not [string]::IsNullOrWhiteSpace([string]$script:LogPath)) {
    Write-Host ""
    Write-Host "[INFO] Bootstrap log: $script:LogPath"
  }
}

function Write-Step {
  param([string]$Message)
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info {
  param([string]$Message)
  Write-Host "[INFO] $Message"
}

function Write-Ok {
  param([string]$Message)
  Write-Host "[OK] $Message" -ForegroundColor Green
}

function Fail-Step {
  param([string]$Message)
  throw $Message
}

function Test-IsWindowsStoreAliasPath {
  param([string]$PathText)
  if ([string]::IsNullOrWhiteSpace($PathText)) {
    return $false
  }
  $normalized = $PathText.Trim().Trim('"').ToLowerInvariant()
  return (
    $normalized -like "*\microsoft\windowsapps\python.exe" -or
    $normalized -like "*\microsoft\windowsapps\python3.exe"
  )
}

function Refresh-SessionPath {
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $parts = @()
  foreach ($segment in @($machinePath, $userPath, $env:Path) -join ";" -split ";") {
    $item = [string]$segment
    if ([string]::IsNullOrWhiteSpace($item)) {
      continue
    }
    $trimmed = $item.Trim()
    if ($parts -notcontains $trimmed) {
      $parts += $trimmed
    }
  }
  $env:Path = $parts -join ";"
}

function Ensure-UserPathContains {
  param([string]$PathEntry)
  if ([string]::IsNullOrWhiteSpace($PathEntry)) {
    return
  }
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $entries = if ($userPath) { $userPath -split ";" | Where-Object { $_ } } else { @() }
  if ($entries -contains $PathEntry) {
    return
  }
  $newPath = if ($entries.Count -gt 0) { ($entries + $PathEntry) -join ";" } else { $PathEntry }
  [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
  Refresh-SessionPath
}

function Test-CommandExists {
  param([string]$CommandName)
  return [bool](Get-Command $CommandName -ErrorAction SilentlyContinue)
}

function Get-ProcessVersionInfo {
  param([string]$CommandText)

  if ([string]::IsNullOrWhiteSpace($CommandText)) {
    return $null
  }
  $cmdParts = $CommandText -split ' ', 2
  $fileName = $cmdParts[0]
  $baseArgs = if ($cmdParts.Length -gt 1) { $cmdParts[1] } else { "" }

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $fileName
  if ($baseArgs) {
    $psi.Arguments = "$baseArgs -c `"import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}|{v.major}|{v.minor}|{sys.executable}')`""
  } else {
    $psi.Arguments = "-c `"import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}|{v.major}|{v.minor}|{sys.executable}')`""
  }
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true

  try {
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $process.Start() | Out-Null
    $process.WaitForExit()
    $stdout = $process.StandardOutput.ReadToEnd().Trim()
    $stderr = $process.StandardError.ReadToEnd().Trim()
    if ($process.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($stdout)) {
      return $null
    }
    $parts = $stdout -split "\|", 4
    if ($parts.Length -lt 4) {
      return $null
    }
    return @{
      Command = $CommandText
      Version = $parts[0]
      Major = [int]$parts[1]
      Minor = [int]$parts[2]
      Executable = $parts[3]
      Error = $stderr
    }
  } catch {
    return $null
  }
}

function Get-WorkingPythonCommand {
  $candidates = New-Object System.Collections.Generic.List[string]

  function Add-PythonCandidate {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return }
    $trimmed = $Value.Trim()
    if (Test-IsWindowsStoreAliasPath $trimmed) { return }
    if ($candidates -notcontains $trimmed) {
      $candidates.Add($trimmed)
    }
  }

  Add-PythonCandidate $env:CCB_PYTHON_CMD
  Add-PythonCandidate "py -3"
  Add-PythonCandidate "python"
  Add-PythonCandidate "python3"

  try {
    $wherePython = & where.exe python 2>$null
    foreach ($item in @($wherePython)) {
      Add-PythonCandidate ([string]$item)
    }
  } catch {}

  $globPatterns = @(
    "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
    "$env:ProgramFiles\Python*\python.exe",
    "$env:ProgramFiles\Python\Python*\python.exe",
    "$env:ProgramFiles(x86)\Python*\python.exe",
    "$env:ProgramFiles(x86)\Python\Python*\python.exe"
  )
  foreach ($pattern in $globPatterns) {
    try {
      Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | ForEach-Object {
        Add-PythonCandidate $_.FullName
      }
    } catch {}
  }

  foreach ($candidate in $candidates) {
    $info = Get-ProcessVersionInfo -CommandText $candidate
    if (
      $null -ne $info -and
      $info.Major -eq 3 -and
      $info.Minor -ge 10 -and
      -not (Test-IsWindowsStoreAliasPath $info.Executable)
    ) {
      return $candidate
    }
  }
  return $null
}

function Get-CommandLocation {
  param([string]$CommandName)
  $command = Get-Command $CommandName -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -eq $command) {
    return $null
  }
  return $command.Source
}

function Test-Python310 {
  return ($null -ne (Get-WorkingPythonCommand))
}

function Show-PythonDiagnostics {
  $working = Get-WorkingPythonCommand
  if (-not [string]::IsNullOrWhiteSpace($working)) {
    Write-Info "Working Python command: $working"
    $info = Get-ProcessVersionInfo -CommandText $working
    if ($null -ne $info) {
      Write-Info "Resolved Python version: $($info.Version)"
      Write-Info "Resolved Python executable: $($info.Executable)"
    }
  } else {
    Write-Info "Working Python command: not found"
  }

  $checks = @("py -3", "python", "python3")
  foreach ($check in $checks) {
    $info = Get-ProcessVersionInfo -CommandText $check
    if ($null -ne $info) {
      Write-Info ("{0} resolves as: {1} ({2})" -f $check, $info.Version, $info.Executable)
    } else {
      Write-Info ("{0} resolves as: unavailable" -f $check)
    }
  }

  try {
    $wherePython = & where.exe python 2>$null
    if ($LASTEXITCODE -eq 0) {
      foreach ($item in @($wherePython)) {
        $pathText = [string]$item
        if ([string]::IsNullOrWhiteSpace($pathText)) {
          continue
        }
        if (Test-IsWindowsStoreAliasPath $pathText) {
          Write-Info "where python => $pathText (Windows Store alias ignored)"
        } else {
          Write-Info "where python => $pathText"
        }
      }
    } else {
      Write-Info "where python => not found"
    }
  } catch {
    Write-Info "where python => unavailable"
  }
}

function Require-Winget {
  if (-not (Test-CommandExists "winget")) {
    Fail-Step "winget is required for this bootstrap script. Install App Installer from Microsoft Store and retry."
  }
}

function Install-WingetPackage {
  param(
    [string]$DisplayName,
    [string]$PackageId,
    [string[]]$Commands
  )

  $commandList = @($Commands | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
  if (-not $Force) {
    foreach ($commandName in $commandList) {
      if (Test-CommandExists $commandName) {
        Write-Ok "$DisplayName already available at $(Get-CommandLocation $commandName)"
        return
      }
    }
  }

  Require-Winget
  Write-Step "Installing $DisplayName with winget ($PackageId)"
  $wingetArgs = @(
    "install",
    "--id", $PackageId,
    "--exact",
    "--accept-package-agreements",
    "--accept-source-agreements",
    "--disable-interactivity"
  )
  if ($Force) {
    $wingetArgs += "--force"
  }
  & winget @wingetArgs
  Refresh-SessionPath

  foreach ($commandName in $commandList) {
    if (Test-CommandExists $commandName) {
      Write-Ok "$DisplayName installed at $(Get-CommandLocation $commandName)"
      return
    }
  }

  Fail-Step "$DisplayName install completed but the expected command was not found on PATH."
}

function Install-Python312 {
  if (-not $Force) {
    $existing = Get-WorkingPythonCommand
    if (-not [string]::IsNullOrWhiteSpace($existing)) {
      $info = Get-ProcessVersionInfo -CommandText $existing
      if ($null -ne $info) {
        Write-Ok "Python 3.10+ already available at $($info.Executable)"
      } else {
        Write-Ok "Python 3.10+ already available"
      }
      return $existing
    }
  }

  Require-Winget
  Write-Step "Installing Python 3.12 with winget (Python.Python.3.12)"
  $wingetArgs = @(
    "install",
    "--id", "Python.Python.3.12",
    "--exact",
    "--accept-package-agreements",
    "--accept-source-agreements",
    "--disable-interactivity",
    "--scope", "user",
    "--override", "InstallAllUsers=0 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=0 Include_test=0"
  )
  if ($Force) {
    $wingetArgs += "--force"
  }
  & winget @wingetArgs
  Refresh-SessionPath
  return (Get-WorkingPythonCommand)
}

function Ensure-NpmGlobalBinOnPath {
  if (-not (Test-CommandExists "npm")) {
    return
  }
  $prefix = ""
  try {
    $prefix = (& npm config get prefix 2>$null | Select-Object -First 1).Trim()
  } catch {}
  if ([string]::IsNullOrWhiteSpace($prefix)) {
    return
  }
  Write-Info "npm global bin prefix: $prefix"
  Ensure-UserPathContains $prefix
}

function Install-NpmGlobalPackage {
  param(
    [string]$DisplayName,
    [string]$PackageName,
    [string]$CommandName
  )

  if ((-not $Force) -and (Test-CommandExists $CommandName)) {
    Write-Ok "$DisplayName already available at $(Get-CommandLocation $CommandName)"
    return
  }
  if (-not (Test-CommandExists "npm")) {
    Fail-Step "npm is required to install $DisplayName."
  }

  Write-Step "Installing $DisplayName via npm ($PackageName)"
  & npm install -g $PackageName
  Ensure-NpmGlobalBinOnPath
  Refresh-SessionPath

  if (-not (Test-CommandExists $CommandName)) {
    Fail-Step "$DisplayName install completed but command '$CommandName' is still missing."
  }
  Write-Ok "$DisplayName installed at $(Get-CommandLocation $CommandName)"
}

function Get-CCSwitchInstallInfo {
  $roots = @(
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
  )
  foreach ($root in $roots) {
    try {
      $entry = Get-ItemProperty -Path $root -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like "CC Switch*" } |
        Select-Object -First 1
      if ($null -ne $entry) {
        return $entry
      }
    } catch {}
  }
  return $null
}

function Get-CCSwitchExecutablePath {
  $candidates = New-Object System.Collections.Generic.List[string]

  function Add-CCSwitchCandidate {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
      return
    }
    $trimmed = $Value.Trim().Trim('"')
    if ($candidates -notcontains $trimmed) {
      $candidates.Add($trimmed)
    }
  }

  $installInfo = Get-CCSwitchInstallInfo
  if ($null -ne $installInfo) {
    foreach ($field in @("InstallLocation", "DisplayIcon")) {
      $raw = [string]$installInfo.$field
      if ([string]::IsNullOrWhiteSpace($raw)) {
        continue
      }
      $trimmed = $raw.Trim().Trim('"')
      if ($trimmed.ToLowerInvariant().EndsWith(".exe")) {
        Add-CCSwitchCandidate $trimmed
      } else {
        Add-CCSwitchCandidate (Join-Path $trimmed "CC Switch.exe")
        Add-CCSwitchCandidate (Join-Path $trimmed "cc-switch.exe")
      }
    }
  }

  Add-CCSwitchCandidate (Join-Path $env:LOCALAPPDATA "Programs\CC Switch\CC Switch.exe")
  Add-CCSwitchCandidate (Join-Path $env:LOCALAPPDATA "Programs\CC Switch\cc-switch.exe")
  Add-CCSwitchCandidate (Join-Path $env:LOCALAPPDATA "Programs\cc-switch\CC Switch.exe")
  Add-CCSwitchCandidate (Join-Path $env:LOCALAPPDATA "Programs\cc-switch\cc-switch.exe")

  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }
  return $null
}

function Install-CCSwitch {
  $existing = Get-CCSwitchInstallInfo
  $existingExe = Get-CCSwitchExecutablePath
  if ((-not $Force) -and (($null -ne $existing) -or (-not [string]::IsNullOrWhiteSpace($existingExe)))) {
    $version = if ($null -ne $existing) { [string]$existing.DisplayVersion } else { "" }
    if ([string]::IsNullOrWhiteSpace($version)) {
      if ([string]::IsNullOrWhiteSpace($existingExe)) {
        Write-Ok "CC Switch already installed"
      } else {
        Write-Ok "CC Switch already installed at $existingExe"
      }
    } else {
      if ([string]::IsNullOrWhiteSpace($existingExe)) {
        Write-Ok "CC Switch already installed (version $version)"
      } else {
        Write-Ok "CC Switch already installed (version $version) at $existingExe"
      }
    }
    return
  }

  Write-Step "Installing CC Switch from latest GitHub release"
  $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ccb-ccswitch-" + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
  try {
    $headers = @{ "User-Agent" = "ccb-bootstrap" }
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/farion1231/cc-switch/releases/latest" -Headers $headers
    $asset = $release.assets | Where-Object { $_.name -like "CC-Switch-v*-Windows.msi" } | Select-Object -First 1
    if ($null -eq $asset) {
      Fail-Step "Unable to find a Windows MSI asset in the latest CC Switch release."
    }

    $msiPath = Join-Path $tempDir $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -Headers $headers -OutFile $msiPath

    $process = Start-Process msiexec.exe -ArgumentList @("/i", $msiPath, "/qn", "/norestart") -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010)) {
      Fail-Step "CC Switch installer failed with exit code $($process.ExitCode)."
    }

    $installed = Get-CCSwitchInstallInfo
    $installedExe = Get-CCSwitchExecutablePath
    if (($null -eq $installed) -and [string]::IsNullOrWhiteSpace($installedExe)) {
      Write-Info "CC Switch MSI install finished, but install detection did not confirm it immediately."
    } else {
      $version = if ($null -ne $installed) { [string]$installed.DisplayVersion } else { "" }
      if ([string]::IsNullOrWhiteSpace($version)) {
        Write-Ok "CC Switch installed"
      } else {
        Write-Ok "CC Switch installed (version $version)"
      }
      if (-not [string]::IsNullOrWhiteSpace($installedExe)) {
        Write-Info "CC Switch executable: $installedExe"
      }
    }
  } finally {
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
  }
}

function Test-CCBInstalled {
  $candidate = Join-Path $InstallPrefix "bin\ccb.cmd"
  return (Test-Path $candidate)
}

function Invoke-CCBInstall {
  if (-not (Test-Path $installScript)) {
    Fail-Step "install.ps1 not found at $installScript"
  }
  if ((-not $Force) -and (Test-CCBInstalled)) {
    Write-Ok "CCB already installed at $InstallPrefix"
    if (-not (Test-CommandExists "ccb")) {
      Ensure-UserPathContains (Join-Path $InstallPrefix "bin")
      Refresh-SessionPath
    }
    return
  }
  Write-Step "Installing CCB into $InstallPrefix"
  & $installScript install -InstallPrefix $InstallPrefix -Yes
  Refresh-SessionPath
  if (-not (Test-CommandExists "ccb")) {
    $candidate = Join-Path $InstallPrefix "bin\ccb.cmd"
    if (Test-Path $candidate) {
      Ensure-UserPathContains (Join-Path $InstallPrefix "bin")
      Refresh-SessionPath
    }
  }
  if (-not (Test-CommandExists "ccb")) {
    Fail-Step "ccb install completed but command 'ccb' is still missing from PATH."
  }
  Write-Ok "ccb available at $(Get-CommandLocation 'ccb')"
}

Start-BootstrapTranscript -RequestedPath $LogPath
try {
function Write-SmokeProject {
  param([string]$ProjectPath)
  $configDir = Join-Path $ProjectPath ".ccb"
  $configPath = Join-Path $configDir "ccb.config"
  New-Item -ItemType Directory -Path $configDir -Force | Out-Null
  $config = "cmd,writer:codex;reviewer:claude,qa:gemini,ops:opencode"
  [System.IO.File]::WriteAllText($configPath, $config.Trim() + "`r`n", $script:utf8NoBom)

  $readmePath = Join-Path $ProjectPath "README.md"
  if (-not (Test-Path $readmePath) -or $Force) {
    $readmeLines = @(
      "# CCB Windows smoke project",
      "",
      "This directory was generated by scripts/bootstrap-windows-test-env.ps1.",
      "",
      "Run:",
      "",
      '```powershell',
      "cd $ProjectPath",
      "ccb",
      '```'
    )
    $readme = $readmeLines -join "`r`n"
    [System.IO.File]::WriteAllText($readmePath, $readme.Trim() + "`r`n", $script:utf8NoBom)
  }
  Write-Ok "Smoke project ready at $ProjectPath"
}

function Show-ProviderSummary {
  $providers = @("codex", "claude", "gemini", "opencode", "ccb")
  Write-Step "Installed command summary"
  foreach ($name in $providers) {
    $location = Get-CommandLocation $name
    if ($location) {
      Write-Host ("  {0,-8} {1}" -f $name, $location)
    } else {
      Write-Host ("  {0,-8} missing" -f $name)
    }
  }
  $ccswitch = Get-CCSwitchInstallInfo
  $ccswitchExe = Get-CCSwitchExecutablePath
  if (($null -eq $ccswitch) -and [string]::IsNullOrWhiteSpace($ccswitchExe)) {
    Write-Host ("  {0,-8} missing" -f "ccswitch")
  } else {
    $label = if ($null -ne $ccswitch -and -not [string]::IsNullOrWhiteSpace([string]$ccswitch.DisplayVersion)) {
      "installed (version $([string]$ccswitch.DisplayVersion))"
    } elseif (-not [string]::IsNullOrWhiteSpace($ccswitchExe)) {
      "installed at $ccswitchExe"
    } else {
      "installed"
    }
    Write-Host ("  {0,-8} {1}" -f "ccswitch", $label)
  }
}

function Show-PathDiagnostics {
  Write-Step "PATH diagnostics"
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  Write-Info "User PATH: $userPath"
  Write-Info "Machine PATH: $machinePath"
  try {
    $whereCodex = & where.exe codex 2>$null
    if ($LASTEXITCODE -eq 0) {
      foreach ($item in @($whereCodex)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$item)) {
          Write-Info "where codex => $([string]$item)"
        }
      }
    } else {
      Write-Info "where codex => not found"
    }
  } catch {
    Write-Info "where codex => unavailable"
  }
}

Refresh-SessionPath

Write-Step "Preparing Windows test environment"
Write-Info "Repo root: $repoRoot"
Write-Info "Install prefix: $InstallPrefix"

if (-not $SkipGit) {
  Install-WingetPackage -DisplayName "Git" -PackageId "Git.Git" -Commands @("git")
}

if (-not $SkipPython) {
  $workingPython = Get-WorkingPythonCommand
  if (-not [string]::IsNullOrWhiteSpace($workingPython)) {
    $env:CCB_PYTHON_CMD = $workingPython
    Write-Ok "Python 3.10+ already available"
    Show-PythonDiagnostics
  } else {
    $workingPython = Install-Python312
    $workingPython = Get-WorkingPythonCommand
    if ([string]::IsNullOrWhiteSpace($workingPython)) {
      Write-Info "Python install completed, but bootstrap could not validate 3.10+ immediately in this shell."
      Write-Info "Continuing and deferring strict Python validation to install.ps1."
      Show-PythonDiagnostics
    } else {
      $env:CCB_PYTHON_CMD = $workingPython
      Write-Ok "Python 3.10+ is available after installation"
      Show-PythonDiagnostics
    }
  }
}

if (-not $SkipNode) {
  Install-WingetPackage -DisplayName "Node.js LTS" -PackageId "OpenJS.NodeJS.LTS" -Commands @("npm")
  Ensure-NpmGlobalBinOnPath
}

if (-not $SkipProviderInstall) {
  if (-not $SkipCodex) {
    Install-NpmGlobalPackage -DisplayName "Codex CLI" -PackageName "@openai/codex" -CommandName "codex"
  }
  if (-not $SkipClaude) {
    Install-NpmGlobalPackage -DisplayName "Claude Code" -PackageName "@anthropic-ai/claude-code" -CommandName "claude"
  }
  if (-not $SkipGemini) {
    Install-NpmGlobalPackage -DisplayName "Gemini CLI" -PackageName "@google/gemini-cli" -CommandName "gemini"
  }
  if (-not $SkipOpenCode) {
    Install-NpmGlobalPackage -DisplayName "OpenCode" -PackageName "opencode-ai" -CommandName "opencode"
  }
}

if (-not $SkipCCSwitch) {
  Install-CCSwitch
}

Invoke-CCBInstall

if (-not $SkipSmokeProject) {
  Write-Step "Creating Windows smoke project"
  Write-SmokeProject -ProjectPath $SmokeProjectPath
}

Show-ProviderSummary
Show-PathDiagnostics

Write-Step "Next steps"
Write-Host "1. Log in to the providers you plan to use: codex / claude / gemini / opencode"
if (-not $SkipSmokeProject) {
  Write-Host "2. Open the smoke project: $SmokeProjectPath"
  Write-Host "3. Run: ccb"
} else {
  Write-Host "2. Run CCB inside your target project: ccb"
}
Write-Host ""
Write-Host "If you launched this bootstrap with 'powershell -File ...', open a new terminal before testing codex / claude / gemini / opencode / ccb."

if ($LaunchSmoke -and -not $SkipSmokeProject) {
  Write-Step "Launching CCB smoke project"
  Push-Location $SmokeProjectPath
  try {
    & ccb
  } finally {
    Pop-Location
  }
}
  Write-LogLocation
} catch {
  Write-Host ""
  try { Show-PythonDiagnostics } catch {}
  try { Show-ProviderSummary } catch {}
  try { Show-PathDiagnostics } catch {}
  Write-Host "[ERROR] Bootstrap failed: $($_.Exception.Message)" -ForegroundColor Red
  Write-LogLocation
  throw
} finally {
  Stop-BootstrapTranscript
}
