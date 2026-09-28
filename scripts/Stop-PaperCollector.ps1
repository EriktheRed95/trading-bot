<#
.SYNOPSIS
Stops exactly one verified local PAPER-ONLY trading server. Never kills python broadly.

.DESCRIPTION
The target must satisfy all of these checks:
- it is the single process listening on 127.0.0.1:<Port>
- that listener answers /api/collector with mode "PAPER ONLY" and reports that same process ID
- the process is python.exe or pythonw.exe, and its command line runs main.py or dashboard.py
If any check fails, nothing is stopped. A server from before the background collector
has no /api/collector. -AllowLegacy accepts one whose /api/status reports PAPER ONLY,
provided the listener-PID and command-line checks also pass.

Stopping is safe for the records. Every paper-book write is one SQLite transaction,
so an interrupted check is rolled back. The next start marks it "interrupted" in
the collector log. Pause state is stored on disk and is unchanged.

If the scheduled task is installed and enabled, it starts the server again within
its repeat interval. Use -DisableTask to stop that too; re-enable it with
Enable-ScheduledTask -TaskName 'TradingBot Paper Collector'.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [int]$Port = 8791,
    [switch]$DisableTask,
    [switch]$AllowLegacy,
    [string]$TaskName = 'TradingBot Paper Collector'
)
$ErrorActionPreference = 'Stop'

$Task = Get-ScheduledTask -TaskPath '\' -TaskName $TaskName -ErrorAction SilentlyContinue
if ($DisableTask -and $Task -and $PSCmdlet.ShouldProcess($TaskName, 'Disable scheduled task')) {
    Disable-ScheduledTask -TaskPath '\' -TaskName $TaskName | Out-Null
    Write-Output "Disabled scheduled task '$TaskName'."
}

$Listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if (-not $Listeners.Count) { Write-Output "No server is listening on 127.0.0.1:$Port. Nothing stopped."; exit 0 }
$Owners = @($Listeners.OwningProcess | Select-Object -Unique)
if ($Owners.Count -ne 1) { throw "More than one process listens on port ${Port} ($($Owners -join ', ')). Refusing to guess." }
$OwnerId = [int]$Owners[0]

try {
    $Status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/collector" -TimeoutSec 5
    $Reported = if ($null -ne $Status.this_pid) { [int]$Status.this_pid } else { [int]$Status.pid }
} catch {
    # Servers from before the background collector have no /api/collector and
    # do not report their PID. Only with -AllowLegacy, identify them by
    # /api/status plus the listener PID and command-line checks below.
    if (-not $AllowLegacy) {
        throw "Port $Port is owned by PID $OwnerId, but /api/collector did not answer. If this is the pre-collector paper server, rerun with -AllowLegacy. Nothing stopped."
    }
    try { $Status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 5 }
    catch { throw "Port $Port is owned by PID $OwnerId, but it did not answer as the paper server. Nothing stopped." }
    $Reported = $OwnerId
}
if ($Status.mode -ne 'PAPER ONLY' -or $Reported -ne $OwnerId) {
    throw "Port owner PID $OwnerId does not match the paper server's reported PID $Reported. Nothing stopped."
}
$Process = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerId"
if (-not $Process -or ($Process.Name -notin @('python.exe', 'pythonw.exe')) -or ($Process.CommandLine -notmatch '(main|dashboard)\.py')) {
    throw "PID $OwnerId ($($Process.Name): $($Process.CommandLine)) is not the paper server command. Nothing stopped."
}

if ($PSCmdlet.ShouldProcess("PID $OwnerId  $($Process.CommandLine)", 'Stop paper server')) {
    Stop-Process -Id $OwnerId
    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        if (-not (Get-Process -Id $OwnerId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    Write-Output "Stopped paper server PID $OwnerId."
    if ($Task -and $Task.State -ne 'Disabled' -and -not $DisableTask) {
        Write-Output "Note: scheduled task '$TaskName' is enabled and will start the server again. Use -DisableTask to prevent that."
    }
}
