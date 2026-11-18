$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$vegaHost = Join-Path $root 'vega_host.py'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'The Vega Python environment is missing. Run Initialize-Vega.ps1 first.' }
if (-not (Test-Path -LiteralPath $vegaHost)) { throw 'The Vega host source is missing.' }

function Test-OmniRouteGateway {
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', 20128)
        return $true
    } catch {
        return $false
    } finally {
        if ($client) { $client.Dispose() }
    }
}

# The gateway has its own sign-in task. Give that task time to start before
# requesting it directly, then keep the dashboard available if it stays down.
$gatewayReady = $false
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    if (Test-OmniRouteGateway) {
        $gatewayReady = $true
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $gatewayReady) {
    $gatewayTaskName = 'Vega OmniRoute Gateway'
    $gatewayScript = Join-Path $root 'start-omniroute.ps1'
    $gatewayTask = Get-ScheduledTask -TaskName $gatewayTaskName -ErrorAction SilentlyContinue
    $gatewayActions = if ($gatewayTask) { @($gatewayTask.Actions) } else { @() }
    $matchingActions = @()
    if ($gatewayActions.Count -eq 1) {
        $matchingActions = @($gatewayActions | Where-Object {
            $_.Arguments -and $_.Arguments.IndexOf($gatewayScript, [StringComparison]::OrdinalIgnoreCase) -ge 0
        })
    }

    if ($matchingActions.Count -eq 1) {
        try {
            Start-ScheduledTask -TaskName $gatewayTaskName -ErrorAction Stop
        } catch {
            Write-Warning 'The existing OmniRoute task could not be started. Vega will still start.'
        }
    } else {
        Write-Warning 'The expected OmniRoute task was not found. Vega will still start.'
    }

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-OmniRouteGateway) {
            $gatewayReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }

    if (-not $gatewayReady) {
        Write-Warning 'The local OmniRoute gateway is still unavailable. Vega will still start.'
    }
}

Set-Location -LiteralPath $root
& $pythonw $vegaHost
