$ErrorActionPreference = 'Stop'
$taskName = 'Vega Independent Companion'
$vegaScript = Join-Path $PSScriptRoot 'vega_host.py'
$pythonWindowless = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $vegaScript) -or -not (Test-Path -LiteralPath $pythonWindowless)) {
    throw 'The Vega host or private Python environment is missing. Run Initialize-Vega.ps1 first.'
}
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    $matching = @($existing.Actions | Where-Object { $_.Arguments -and $_.Arguments.Contains($vegaScript) })
    if ($matching.Count -ne 1) { throw 'An unrelated task already uses the Vega name. It was preserved.' }
}
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('"' + $vegaScript + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentIdentity
$principal = New-ScheduledTaskPrincipal -UserId $currentIdentity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
# Vega serves the interactive dashboard. Scheduler default 7 lowers both CPU
# and I/O priority; 6 keeps normal scheduling when other apps are busy.
$settings.Priority = 6
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Independent owner-paired Vega host. Starts at Windows sign-in and remains separate from the Codex process lifecycle.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
