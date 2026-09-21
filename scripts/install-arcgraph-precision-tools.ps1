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
$PyrightVersion = "1.1.409"
# SCIP v0.7.1's canonical Go module is scip-code/scip; sourcegraph/scip redirects to it.
$ScipGoPackage = "github.com/scip-code/scip/cmd/scip@$ScipVersion"

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
    $ok = (Test-VersionCommand -Name "scip-python" -Commands @("scip-python.cmd", "scip-python")) -and $ok
    $ok = (Test-VersionCommand -Name "pyright" -Commands @("pyright.cmd", "pyright")) -and $ok
    $ok = (Test-CommandExists -Name "pyright-langserver" -Commands @("pyright-langserver.cmd", "pyright-langserver")) -and $ok
    if (-not $ok) {
        Write-Host ""
        Write-Host "Install with:"
        Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\install-arcgraph-precision-tools.ps1"
        Write-Host ""
        Write-Host "If Go is missing, first run:"
        Write-Host "  winget install --id GoLang.Go -e"
        exit 1
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Add-CurrentProcessPath -PathToAdd $InstallDir

$npm = Get-ToolCommand -Names @("npm.cmd", "npm")
if (-not $npm) {
    throw "npm is required to install scip-python and pyright. Install Node.js first."
}

Write-Step "Installing npm precision tools"
Invoke-Checked -Command $npm -Arguments @("install", "-g", "--prefix", $InstallDir, "@sourcegraph/scip-python@$ScipPythonVersion", "pyright@$PyrightVersion")

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

Write-Step "Building scip.exe $ScipVersion into $InstallDir"
$previousGoBin = $env:GOBIN
try {
    $env:GOBIN = (Resolve-Path $InstallDir).Path
    Invoke-Checked -Command $go -Arguments @("install", $ScipGoPackage)
}
finally {
    $env:GOBIN = $previousGoBin
}

if ($PersistUserPath) {
    Add-UserPath -PathToAdd $InstallDir
}

Write-Step "Verifying installed tools"
$ok = $true
$ok = (Test-VersionCommand -Name "scip" -Commands @("scip.exe", "scip")) -and $ok
$ok = (Test-VersionCommand -Name "scip-python" -Commands @("scip-python.cmd", "scip-python")) -and $ok
$ok = (Test-VersionCommand -Name "pyright" -Commands @("pyright.cmd", "pyright")) -and $ok
$ok = (Test-CommandExists -Name "pyright-langserver" -Commands @("pyright-langserver.cmd", "pyright-langserver")) -and $ok
if (-not $ok) {
    throw "One or more precision tools could not be verified."
}

Write-Host ""
Write-Host "Current PowerShell session can now run:"
Write-Host "  scip --version"
Write-Host "  scip-python --version"
Write-Host "  pyright-langserver --version"
if (-not $PersistUserPath) {
    Write-Host ""
    Write-Host "To persist PATH for future shells, rerun with -PersistUserPath."
}
