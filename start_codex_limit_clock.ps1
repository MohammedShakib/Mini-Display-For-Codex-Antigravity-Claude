$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Program Files\Python313\python.exe"
$Script = Join-Path $Root "codex_limit_clock.py"
$Log = Join-Path $Root "codex_limit_clock.log"
$ClockIp = "192.168.0.58"

Set-Location $Root

while ($true) {
    try {
        & $Python -u $Script --clock-ip $ClockIp --loop 30 *>> $Log
    }
    catch {
        "$(Get-Date -Format s) launcher error: $($_.Exception.Message)" | Out-File -FilePath $Log -Append -Encoding utf8
        Start-Sleep -Seconds 30
    }
}
