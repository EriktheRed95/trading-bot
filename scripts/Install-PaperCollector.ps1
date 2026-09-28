<#
.SYNOPSIS
Registers, or removes, the per-user scheduled task that keeps the local PAPER-ONLY
trading server and its background collector running while you are signed in.

.DESCRIPTION
One task, current user only, standard (non-administrator) rights. It starts
pythonw.exe (no window) with this repository's main.py at logon and re-launches it
every -RepeatMinutes if it is not running. MultipleInstances=IgnoreNew stops the
task from starting a second copy of itself. The server also holds an exclusive
runtime lock and an exclusive port, so a copy started by Start-Trading.ps1 or by
hand makes any later launch exit without doing anything. Nothing is ever killed.

The task never wakes the computer, and it does not run while you are signed out.
It is not a cloud service. Pause state lives in the paper databases and survives
restarts. No brokerage connection or credentials are involved.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Install-PaperCollector.ps1 -DryRun
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Install-PaperCollector.ps1
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Install-PaperCollector.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$DryRun,
    [switch]$Force,
    [string]$TaskName = 'TradingBot Paper Collector',
    [ValidateRange(5, 60)][int]$RepeatMinutes = 10,
    [ValidateRange(0, 1440)][int]$RestartHungAfterMinutes = 60
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Main = Join-Path $Root 'main.py'
foreach ($Required in 'main.py', 'trading_app.py', 'collector.py') {
    if (-not (Test-Path (Join-Path $Root $Required))) { throw "Not a trading-bot checkout: $Root is missing $Required." }
}
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Existing = Get-ScheduledTask -TaskPath '\' -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Uninstall) {
    if (-not $Existing) { Write-Output "No scheduled task named '$TaskName'. Nothing to remove."; exit 0 }
    if ($DryRun) { Write-Output "Dry run: would unregister '$TaskName'."; exit 0 }
    Unregister-ScheduledTask -TaskPath '\' -TaskName $TaskName -Confirm:$false
    Write-Output "Removed '$TaskName'. A server that is already running keeps running; stop it with scripts\Stop-PaperCollector.ps1."
    exit 0
}

# pythonw.exe beside the python on PATH: same interpreter and packages, no console window.
$Python = (Get-Command python -ErrorAction Stop).Source
if ($Python -like '*\WindowsApps\*') { throw "python resolves to the Microsoft Store alias ($Python). Put the real interpreter first on PATH." }
$PythonW = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path $PythonW)) { throw "pythonw.exe was not found beside $Python." }
Push-Location $Root
# Windows PowerShell turns native stderr (e.g. library warnings) into errors under 'Stop'.
$ErrorActionPreference = 'Continue'
try {
    & $Python -B -c 'import collector, trading_app, market_lab, active_experiment, stock_experiments' *> $null
    $ImportExit = $LASTEXITCODE
} finally { Pop-Location; $ErrorActionPreference = 'Stop' }
if ($ImportExit -ne 0) { throw "The interpreter at $Python cannot import the paper modules (exit $ImportExit)." }

# Refuse to add a second launcher for this repository under another name.
$Others = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $Task = $_
    -not ($Task.TaskPath -eq '\' -and $Task.TaskName -eq $TaskName) -and
    @($Task.Actions | Where-Object { "$($_.Execute) $($_.Arguments) $($_.WorkingDirectory)" -like "*$Root*" }).Count -gt 0
})
if ($Others.Count -and -not $Force) {
    $Names = ($Others | ForEach-Object { "$($_.TaskPath)$($_.TaskName)" }) -join ', '
    throw "Other scheduled tasks already reference ${Root}: $Names. Remove them first, or pass -Force to add this one anyway."
}

$Arguments = "-B `"$Main`" --no-browser --restart-hung-after $RestartHungAfterMinutes"
$Action = New-ScheduledTaskAction -Execute $PythonW -Argument $Arguments -WorkingDirectory $Root
$AtLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
$Repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
# ExecutionTimeLimit 0: the server runs until stopped. No WakeToRun: never wakes a sleeping PC.
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Description = 'PAPER ONLY. Keeps the local trading-bot server and background collector running while this user is signed in. ' +
    'No broker connection. Does not wake the computer. Remove with scripts\Install-PaperCollector.ps1 -Uninstall.'

Write-Output "Task:        \$TaskName ($(if ($Existing) { 'replace existing' } else { 'new' }))"
Write-Output "Runs as:     $User, logged-on only, standard rights (RunLevel Limited)"
Write-Output "Program:     $PythonW"
Write-Output "Arguments:   $Arguments"
Write-Output "Start in:    $Root"
Write-Output "Triggers:    at logon of $User; every $RepeatMinutes minutes (ignored while already running)"
Write-Output "Server URL:  http://127.0.0.1:8791 (loopback only)"
if ($DryRun) { Write-Output 'Dry run: nothing registered.'; exit 0 }

$Task = New-ScheduledTask -Action $Action -Trigger @($AtLogon, $Repeat) -Principal $Principal -Settings $Settings -Description $Description
try {
    Register-ScheduledTask -TaskPath '\' -TaskName $TaskName -InputObject $Task -Force | Out-Null
} catch [Microsoft.Management.Infrastructure.CimException] {
    # Some Windows editions reserve logon triggers for administrators. The
    # repeating trigger alone still starts the server within RepeatMinutes of logon.
    Write-Warning "Logon trigger refused ($($_.Exception.Message.Trim())). Registering the repeating trigger only."
    $Task = New-ScheduledTask -Action $Action -Trigger $Repeat -Principal $Principal -Settings $Settings -Description $Description
    Register-ScheduledTask -TaskPath '\' -TaskName $TaskName -InputObject $Task -Force | Out-Null
}
Write-Output "Registered '$TaskName'. It starts within $RepeatMinutes minutes (or run: Start-ScheduledTask -TaskName '$TaskName')."
