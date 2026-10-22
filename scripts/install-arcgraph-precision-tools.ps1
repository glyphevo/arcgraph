param(
    [switch]$CheckOnly,
    [switch]$PersistUserPath,
    [switch]$InstallGoWithWinget,
    [string]$InstallDir = "$env:LOCALAPPDATA\ArcGraph\bin"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScipVersion = "v0.7.1"
$ScipPythonVersion = "0.6.6"
# scip-python 0.6.6 fails while loading on native Windows (it builds a regular expression
# from the "\" path separator), so it is not installed here; it runs in WSL.
$ScipPythonNote = "scip-python $ScipPythonVersion does not run on native Windows; run 'arcgraph precision scip-python' in WSL after 'npm install -g @sourcegraph/scip-python@$ScipPythonVersion'."
$PyrightVersion = "1.1.409"
# scip v0.7.1 publishes no Windows binary, and its go.mod has a replace directive, which
# "go install package@version" refuses; build it from a clone of the tag instead.
$ScipRepository = "https://github.com/scip-code/scip.git"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Get-ToolCommand {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }
    return $null
}

function Add-CurrentProcessPath {
    param([string]$PathToAdd)
    $expanded = [Environment]::ExpandEnvironmentVariables($PathToAdd)
    $parts = @($env:PATH -split ";" | Where-Object { $_ })
    if ($parts -notcontains $expanded) {
        $env:PATH = "$expanded;$env:PATH"
    }
}

function Add-UserPath {
    param([string]$PathToAdd)
    $expanded = [Environment]::ExpandEnvironmentVariables($PathToAdd)
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($current) {
        $parts = @($current -split ";" | Where-Object { $_ })
    }
    if ($parts -notcontains $expanded) {
        $next = (@($expanded) + $parts) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $next, "User")
        Write-Step "Persisted user PATH entry: $expanded"
    }
}

function Invoke-Checked {
    param(
        [string]$Command,
        [string[]]$Arguments
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

function Test-VersionCommand {
    param(
        [string]$Name,
        [string[]]$Commands
    )
    $command = Get-ToolCommand -Names $Commands
    if (-not $command) {
        Write-Host "MISSING $Name"
        return $false
    }
    $version = & $command --version 2>&1 | Select-Object -First 1
    Write-Host "OK $Name -> $command $version"
    return $true
}

function Test-CommandExists {
    param(
        [string]$Name,
        [string[]]$Commands
    )
    $command = Get-ToolCommand -Names $Commands
    if (-not $command) {
        Write-Host "MISSING $Name"
        return $false
    }
    Write-Host "OK $Name -> $command"
    return $true
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This helper is intended for Windows. Use CI/Linux package installation on non-Windows hosts."
}

Add-CurrentProcessPath -PathToAdd $InstallDir

if ($CheckOnly) {
    Write-Step "Checking ArcGraph precision tools"
    $ok = $true
    $ok = (Test-VersionCommand -Name "scip" -Commands @("scip.exe", "scip")) -and $ok
    $ok = (Test-VersionCommand -Name "pyright" -Commands @("pyright.cmd", "pyright")) -and $ok
    $ok = (Test-CommandExists -Name "pyright-langserver" -Commands @("pyright-langserver.cmd", "pyright-langserver")) -and $ok
    Write-Host $ScipPythonNote
    if (-not $ok) {
        Write-Host ""
        Write-Host "Install with:"
        Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\install-arcgraph-precision-tools.ps1"
        Write-Host ""
        Write-Host "If Go or Git is missing, first run:"
        Write-Host "  winget install --id GoLang.Go -e"
        Write-Host "  winget install --id Git.Git -e"
        exit 1
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Add-CurrentProcessPath -PathToAdd $InstallDir

$npm = Get-ToolCommand -Names @("npm.cmd", "npm")
if (-not $npm) {
    throw "npm is required to install pyright. Install Node.js first."
}

Write-Step "Installing pyright"
Invoke-Checked -Command $npm -Arguments @("install", "-g", "--prefix", $InstallDir, "pyright@$PyrightVersion")

$go = Get-ToolCommand -Names @("go.exe", "go")
if (-not $go -and $InstallGoWithWinget) {
    $winget = Get-ToolCommand -Names @("winget.exe", "winget")
    if (-not $winget) {
        throw "winget is not available; install Go manually from https://go.dev/dl/."
    }
    Write-Step "Installing Go with winget"
    Invoke-Checked -Command $winget -Arguments @("install", "--id", "GoLang.Go", "-e")
    $go = Get-ToolCommand -Names @("go.exe", "go")
}

if (-not $go) {
    throw "Go is required to build scip.exe on Windows. Install it with: winget install --id GoLang.Go -e"
}

$git = Get-ToolCommand -Names @("git.exe", "git")
if (-not $git) {
    throw "Git is required to fetch the scip $ScipVersion source on Windows. Install it with: winget install --id Git.Git -e"
}

Write-Step "Building scip.exe $ScipVersion into $InstallDir"
$scipSource = Join-Path ([System.IO.Path]::GetTempPath()) ("arcgraph-scip-" + [guid]::NewGuid().ToString("N"))
$scipOutput = Join-Path (Resolve-Path $InstallDir).Path "scip.exe"
try {
    Invoke-Checked -Command $git -Arguments @("-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", $ScipVersion, $ScipRepository, $scipSource)
    Push-Location $scipSource
    try {
        Invoke-Checked -Command $go -Arguments @("build", "-o", $scipOutput, "./cmd/scip")
    }
    finally {
        Pop-Location
    }
}
finally {
    if (Test-Path $scipSource) {
        Remove-Item -Recurse -Force $scipSource
    }
}

if ($PersistUserPath) {
    Add-UserPath -PathToAdd $InstallDir
}

Write-Step "Verifying installed tools"
$ok = $true
$ok = (Test-VersionCommand -Name "scip" -Commands @("scip.exe", "scip")) -and $ok
$ok = (Test-VersionCommand -Name "pyright" -Commands @("pyright.cmd", "pyright")) -and $ok
$ok = (Test-CommandExists -Name "pyright-langserver" -Commands @("pyright-langserver.cmd", "pyright-langserver")) -and $ok
if (-not $ok) {
    throw "One or more precision tools could not be verified."
}

Write-Host ""
Write-Host "Current PowerShell session can now run:"
Write-Host "  scip --version"
Write-Host "  pyright-langserver --version"
Write-Host ""
Write-Host $ScipPythonNote
if (-not $PersistUserPath) {
    Write-Host ""
    Write-Host "To persist PATH for future shells, rerun with -PersistUserPath."
}
