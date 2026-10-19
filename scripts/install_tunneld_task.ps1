# Registers the "iPad tunneld" scheduled task: pymobiledevice3's tunneld (scripts\tunneld.pyw),
# with no window and admin rights (it creates TUN interfaces), started at logon and restarted
# every minute if it stops. tunneld keeps the tunnel to the phone over USB or Wi-Fi.
# Run from an admin PowerShell; re-running replaces the task.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_tunneld_task.ps1 -Python <python.exe>
# -Python is the interpreter SideTap's packages are installed in (pythonw.exe next to it runs the task).
# -TaskName matches TUNNELD_TASK in .env (default "iPad tunneld").

param(
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$TaskName = 'iPad tunneld',
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an admin PowerShell: tunneld needs admin rights to create its network interfaces.'
}
$Repo = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
$Script = Join-Path $PSScriptRoot 'tunneld.pyw'
foreach ($f in $Pythonw, $Script) { if (-not (Test-Path $f)) { throw "missing $f" } }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Script`"" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "pymobiledevice3 tunneld for SideTap ($Repo)" -Force | Out-Null
Write-Host "Registered '$TaskName' -> $Pythonw $Script"
if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started '$TaskName'"
}
