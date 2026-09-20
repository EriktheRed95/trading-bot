$ErrorActionPreference = 'Stop'
$TradingRoot = $PSScriptRoot
$TradingUrl = 'http://127.0.0.1:8791'
try {
    $TradingStatus = Invoke-RestMethod -Uri "$TradingUrl/api/status" -TimeoutSec 2
    if ($TradingStatus.mode -eq 'PAPER ONLY') {
        Start-Process $TradingUrl
        exit 0
    }
} catch { }
$TradingPython = (Get-Command python -ErrorAction Stop).Source
$TradingRuntime = Join-Path $TradingRoot 'runtime'
New-Item -ItemType Directory -Force $TradingRuntime | Out-Null
Start-Process -FilePath $TradingPython -ArgumentList @('-B','main.py','--no-browser') -WorkingDirectory $TradingRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $TradingRuntime 'server.log') -RedirectStandardError (Join-Path $TradingRuntime 'server-errors.log')
for ($TradingAttempt = 0; $TradingAttempt -lt 20; $TradingAttempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $TradingStatus = Invoke-RestMethod -Uri "$TradingUrl/api/status" -TimeoutSec 1
        if ($TradingStatus.mode -eq 'PAPER ONLY') { Start-Process $TradingUrl; exit 0 }
    } catch { }
}
throw 'Trading dashboard did not start. See runtime/server-errors.log.'
