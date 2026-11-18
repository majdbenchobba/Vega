param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) { throw 'Python 3.13 or newer is required.' }
$pythonVersion = & $pythonCommand.Source -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
if ($LASTEXITCODE -ne 0 -or "$pythonVersion" -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Python 3.13 or newer is required.'
}
if ([version]$pythonVersion -lt [version]'3.13.0') {
    throw 'Python 3.13 or newer is required.'
}

$settingsPath = Join-Path $root 'settings.json'
$tasksPath = Join-Path $root 'tasks.json'
$settingsExample = Join-Path $root 'settings.example.json'
$tasksExample = Join-Path $root 'tasks.example.json'
if (-not (Test-Path -LiteralPath $settingsExample) -or -not (Test-Path -LiteralPath $tasksExample)) {
    throw 'The example configuration files are missing.'
}
if (-not (Test-Path -LiteralPath $settingsPath)) {
    Copy-Item -LiteralPath $settingsExample -Destination $settingsPath
}
if (-not (Test-Path -LiteralPath $tasksPath)) {
    Copy-Item -LiteralPath $tasksExample -Destination $tasksPath
}

$localAppData = $env:LOCALAPPDATA
if (-not $localAppData) { $localAppData = [Environment]::GetFolderPath('LocalApplicationData') }
if (-not $localAppData) { throw 'The Windows local application-data folder is unavailable.' }
$workspaceRoot = Join-Path $localAppData 'Vega\workspace'
New-Item -ItemType Directory -Path $workspaceRoot -Force | Out-Null

if (-not $SkipDependencyInstall) {
    $venv = Join-Path $root '.venv'
    $pythonw = Join-Path $venv 'Scripts\pythonw.exe'
    $python = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        & $pythonCommand.Source -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "Creating Vega's private Python environment failed." }
    }
    if (-not (Test-Path -LiteralPath $pythonw)) { throw "Vega's Python environment is incomplete." }
    & $python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "Installing Vega's pinned Python dependencies failed." }

    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { throw 'Node.js 24.12 or newer is required.' }
    $nodeVersion = (& $nodeCommand.Source --version).Trim().TrimStart('v')
    if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw 'Node.js 24.12 or newer is required.'
    }
    if ([version]$nodeVersion -lt [version]'24.12.0') {
        throw 'Node.js 24.12 or newer is required.'
    }
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) { throw 'npm is missing from the Node.js installation.' }
    Push-Location $root
    try {
        & $npmCommand.Source ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Installing Vega dependencies failed.' }
    } finally {
        Pop-Location
    }
}

Write-Host 'Vega first-run files are ready.'
Write-Host "Private workspace: $workspaceRoot"
Write-Host 'Next: configure your own OmniRoute providers, then run Install-Startup.ps1.'
