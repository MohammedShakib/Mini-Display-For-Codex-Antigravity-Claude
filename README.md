# SyncAI Smart Clock Dashboard

SyncAI turns a 240x240 Smart Weather Clock display into a local quota meter for:

- OpenAI Codex
- Google Antigravity IDE

The project runs on the PC. It renders a JPG screen locally and uploads that image to the clock over the local Wi-Fi network.

## Current Behavior

- Codex usage is read from local Codex session logs in `~/.codex/sessions`.
- Codex shows 5-hour usage, weekly usage, reset time, and total usage status.
- If a Codex reset time has passed but Codex is closed and no new log event exists, the script corrects the stale session value so the old 100% screen is not kept.
- Antigravity quota is read from the local Antigravity IDE language server over its local gRPC quota endpoint.
- No separate OpenAI or Google API key is used.
- Antigravity shows either Gemini or Claude/GPT limits depending on the selected/detected active model.
- Antigravity offline state shows `Open Antigravity IDE` for 10 seconds, then returns to Codex.
- Usage at or above the configured alert threshold, default 80%, switches the matching UI into a red warning theme.
- A local web dashboard is available at `http://localhost:5050`.

## Hardware Assumptions

- Smart Weather Clock is already connected to the same LAN as the PC.
- Current clock IP is `192.168.0.58`.
- Clock accepts uploads at `http://<clock-ip>/photo/upload`.
- Clock can be switched to the uploaded photo display through its local web API.

If the clock IP changes, update `config.json` or start the script with `--clock-ip`.

## Requirements

- Windows
- Python 3.10+
- Codex installed and used locally
- Antigravity IDE installed if Antigravity quota display is wanted
- Python packages from `requirements.txt`

Install packages:

```powershell
python -m pip install -r requirements.txt
```

## Files

```text
codex_limit_clock.py          Main loop, quota readers, image renderer, clock uploader
theme_renderer.py             Extra 240x240 visual themes
web_server.py                 Local Flask control dashboard
start_codex_limit_clock.ps1   Background runner used by Windows Scheduled Task
config.json                   Live configuration, hot-reloaded by the loop
web/index.html                Browser dashboard UI
assets/                       Logos, previews, live screen asset
docs/                         Original device manuals
firmware/                     Device firmware binary
AGENTS.md                     Notes for future coding agents
```

Runtime/generated files are ignored by Git:

```text
codex_limit_clock.log
codex_usage.jpg
temp_*.jpg
runtime_state.json
assets/live_screen.jpg
```

## Run Manually

Run the display loop:

```powershell
python .\codex_limit_clock.py --clock-ip 192.168.0.58 --loop 30
```

Run once:

```powershell
python .\codex_limit_clock.py --clock-ip 192.168.0.58 --loop 0
```

Run the local web dashboard:

```powershell
python .\web_server.py
```

Then open:

```text
http://localhost:5050
```

## Auto Start On Windows

The background runner is:

```powershell
.\start_codex_limit_clock.ps1
```

To create or replace the scheduled task for login auto-start:

```powershell
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\start_codex_limit_clock.ps1`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "CodexLimitClock" -Action $Action -Trigger $Trigger -Settings $Settings -Force
Start-ScheduledTask -TaskName "CodexLimitClock"
```

Useful checks:

```powershell
Get-ScheduledTask -TaskName "CodexLimitClock"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'codex_limit_clock.py' }
Get-Content .\codex_limit_clock.log -Tail 20
```

## Configuration

`config.json` is hot-reloaded by the running loop.

```json
{
  "clock_ip": "192.168.0.58",
  "rotation_interval": 30,
  "ag_model_mode": "auto",
  "alert_threshold": 80,
  "show_splash": true,
  "selected_theme": "default"
}
```

Fields:

- `clock_ip`: Smart clock IP address.
- `rotation_interval`: Normal page duration in seconds.
- `ag_model_mode`: `auto`, `gemini`, `claude`, or `both`.
- `alert_threshold`: Usage percentage that triggers red warning UI.
- `show_splash`: Shows a brief logo transition before live data screens.
- `selected_theme`: Theme ID used by `theme_renderer.py`; `default` uses the built-in compact theme.

Antigravity offline placeholder uses a fixed 10-second duration, independent of `rotation_interval`.

## Data Sources

Codex:

- Reads local JSONL session files in `~/.codex/sessions`.
- Looks for `token_count` events containing `rate_limits`.
- Uses primary rate limit as 5-hour usage and secondary rate limit as weekly usage.

Antigravity IDE:

- Finds local `language_server_windows_x64.exe` processes.
- Reads their local listening ports.
- Calls the local quota gRPC method with the IDE process CSRF metadata.
- Parses quota summary values for Gemini and Claude/GPT.
- Falls back to Windows UI Automation only if direct gRPC reading fails and the Models settings screen is visible.

Do not log or commit CSRF tokens from process command lines.

## Clock Setup

Initial hardware setup usually needs a phone or PC connected to the clock's temporary Wi-Fi AP:

```text
http://192.168.4.1/
```

After Wi-Fi is configured, use the LAN IP shown by the router/app. This project currently targets:

```text
192.168.0.58
```

The script configures the clock to use the photo theme and enables the uploaded `codex_usage.jpg` image.

## Troubleshooting

Clock does not update:

```powershell
ping 192.168.0.58
Get-Content .\codex_limit_clock.log -Tail 20
```

Codex value looks old:

- Open/use Codex once so it writes a fresh session event.
- Reset-passed stale 5-hour values are corrected by the script, but total token count only changes when Codex writes a new event.

Antigravity says `Open Antigravity IDE`:

- Start Antigravity IDE.
- The display should update on the next rotation.
- The offline placeholder only stays for 10 seconds before returning to Codex.

Duplicate Python loops:

```powershell
Stop-ScheduledTask -TaskName "CodexLimitClock"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'codex_limit_clock.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName "CodexLimitClock"
```
