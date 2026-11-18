$ErrorActionPreference = 'Stop'
$taskName = 'Vega OmniRoute Gateway'
$script = Join-Path $PSScriptRoot 'start-omniroute.ps1'
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $nodeCommand -or -not $npmCommand) { throw 'Node.js and npm are required for the OmniRoute gateway.' }
$globalRoot = (& $npmCommand.Source root -g).Trim()
if ($LASTEXITCODE -ne 0 -or -not $globalRoot) { throw 'The global npm package directory is unavailable.' }
$server = Join-Path (Join-Path $globalRoot 'omniroute') 'dist\server.js'
if (-not (Test-Path -LiteralPath $server)) { throw 'Install and configure OmniRoute before adding its startup task.' }
if (-not (Test-Path -LiteralPath $script)) { throw 'The Vega OmniRoute launcher is missing.' }

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    $matching = @($existing.Actions | Where-Object { $_.Arguments -and $_.Arguments.Contains($script) })
    if ($matching.Count -ne 1) { throw 'An unrelated task already uses the OmniRoute name. It was preserved.' }
}
$powershell = Join-Path $PSHOME 'powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershell `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + $script + '"') `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentIdentity
$principal = New-ScheduledTaskPrincipal -UserId $currentIdentity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Local OmniRoute gateway for Vega; listens only on loopback.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
