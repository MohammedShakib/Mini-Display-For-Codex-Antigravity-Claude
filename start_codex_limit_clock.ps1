$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Program Files\Python313\python.exe"
$Script = Join-Path $Root "codex_limit_clock.py"
$Runtime = Join-Path $Root "runtime"
$Log = Join-Path $Runtime "codex_limit_clock.log"
$ClockIp = "192.168.0.58"

Set-Location $Root
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null

$MutexName = "Global\SyncAI_CodexLimitClock"
$CreatedNew = $false
$Mutex = New-Object System.Threading.Mutex($true, $MutexName, [ref]$CreatedNew)
if (-not $CreatedNew) {
    "$(Get-Date -Format s) launcher already running; exiting duplicate instance" | Out-File -FilePath $Log -Append -Encoding utf8
    exit 0
}

while ($true) {
    try {
        & $Python -u $Script --clock-ip $ClockIp --loop 30 *>> $Log
    }
    catch {
        "$(Get-Date -Format s) launcher error: $($_.Exception.Message)" | Out-File -FilePath $Log -Append -Encoding utf8
        Start-Sleep -Seconds 30
    }
}
