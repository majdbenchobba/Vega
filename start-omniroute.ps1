$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $nodeCommand -or -not $npmCommand) { throw 'Node.js and npm are required for the OmniRoute gateway.' }
$node = $nodeCommand.Source
$globalRoot = (& $npmCommand.Source root -g).Trim()
if ($LASTEXITCODE -ne 0 -or -not $globalRoot) { throw 'The global npm package directory is unavailable.' }
$serverDirectory = Join-Path (Join-Path $globalRoot 'omniroute') 'dist'
$server = Join-Path $serverDirectory 'server.js'
if (-not (Test-Path -LiteralPath $server)) { throw 'The OmniRoute server is missing.' }

$env:PORT = '20128'
$env:HOSTNAME = '127.0.0.1'
$env:DATA_DIR = Join-Path $env:USERPROFILE '.omniroute'

# If another instance already owns the loopback port, leave it alone.
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect('127.0.0.1', 20128)
    $client.Close()
    exit 0
} catch {
    # Port is free; continue.
}

Set-Location -LiteralPath $serverDirectory
& $node $server
