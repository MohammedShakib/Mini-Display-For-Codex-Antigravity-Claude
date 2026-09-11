# Agent Notes

This repository turns a 240x240 Wi-Fi display clock into a local AI quota dashboard from a Windows PC. The main goal is to display local Codex and Antigravity IDE quota usage on the physical display without using external API keys.

## Important Runtime Facts

- Working directory: repository root.
- Main script: `codex_limit_clock.py`.
- Local dashboard: `web_server.py` serving `web/index.html` on port `5050`.
- Clock IP is configured in `config.json`; current default is `192.168.0.58`.
- The Windows scheduled task name is `CodexLimitClock`.
- `start_codex_limit_clock.ps1` is the intended auto-restart launcher.
- `start_codex_limit_clock_hidden.vbs` starts the PowerShell launcher with no visible window and is the preferred scheduled task action.
- The PowerShell launcher uses a named mutex, `Global\SyncAI_CodexLimitClock`, to prevent duplicate upload loops.
- On this PC, the old `CodexLimitClock` Scheduled Task is intentionally disabled because it previously surfaced a visible terminal. Prefer the per-user Startup shortcut `SyncAI Quota Display.lnk`.

## Data Sources

Codex usage:

- Source: local JSONL files under `~/.codex/sessions`.
- The parser searches recent files for `token_count` events with `rate_limits`.
- `primary.used_percent` is treated as the 5-hour session usage.
- `secondary.used_percent` is treated as weekly usage.
- If `primary_reset` or `weekly_reset` is already in the past, stale percentages are corrected to `0.0` so old full-limit screens are not kept after reset.

Antigravity usage:

- Primary source: local Antigravity IDE language server gRPC quota endpoint.
- The script discovers `language_server_windows_x64.exe`, local listening ports, and the process CSRF token.
- Never print, log, or commit CSRF tokens from command lines.
- Fallback source: Windows UI Automation reading the visible Settings > Models page.
- If Antigravity is not currently readable, treat it as offline unless previously captured quota data is available for a clearly marked cached screen.
- Direct gRPC parsing exposes `five_hour_reset` and `weekly_reset` epoch seconds per model group when available.
- If previously captured Antigravity quota data exists, an unreadable/offline IDE may render a clearly marked `LAST KNOWN` / `cached` screen using muted frozen styling. Never present cached Antigravity data as live.

## Screen Rotation Rules

- Normal rotation interval comes from `config.json` key `rotation_interval`, currently `30`.
- Antigravity offline placeholder is fixed at `10` seconds via `ANTIGRAVITY_OFFLINE_SECONDS`.
- Antigravity offline screen must say `Open Antigravity IDE`.
- Antigravity offline screen must not show old quota data.
- If cached Antigravity quota data exists, show it as a muted `LAST KNOWN` frozen screen instead of the offline placeholder.
- Antigravity online screens should show the active model's 5-hour reset time in the footer when available.
- Codex can continue showing latest local data, with reset-time correction.

## Rendering

- Output image uploaded to the clock: `runtime/codex_usage.jpg`.
- Temporary render file: `runtime/temp_codex_usage.jpg`.
- Live dashboard preview: `assets/live_screen.jpg`.
- Codex logo: `assets/codex_logo.png`.
- Antigravity logo: `assets/antigravity_logo.png`.
- README architecture diagram: `assets/architecture_syncai.png`.
- README branding/product mockup: `assets/syncai_clock_mockup.png`.
- `theme_renderer.py` contains alternate themes used when `selected_theme` is not `default`.

Keep 240x240 readability in mind. Text must fit at this physical size.

## Verification Commands

Compile:

```powershell
python -m py_compile .\codex_limit_clock.py .\theme_renderer.py .\web_server.py
```

Read current Codex data:

```powershell
python -c "import json, codex_limit_clock as c; print(json.dumps(c.find_latest_limits(), indent=2))"
```

Read current Antigravity data:

```powershell
python -c "import json, codex_limit_clock as c; print(json.dumps(c.find_antigravity_limits(), indent=2))"
```

Check scheduled task and loop:

```powershell
Get-ScheduledTask -TaskName "CodexLimitClock"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'codex_limit_clock.py' }
Get-Content .\runtime\codex_limit_clock.log -Tail 20
```

Restart the scheduled loop cleanly:

```powershell
Stop-ScheduledTask -TaskName "CodexLimitClock" -ErrorAction SilentlyContinue
$self = $PID
Get-CimInstance Win32_Process |
  Where-Object { $_.ProcessId -ne $self -and ($_.CommandLine -match 'codex_limit_clock.py' -or $_.CommandLine -match 'start_codex_limit_clock.ps1') } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
wscript.exe ".\start_codex_limit_clock_hidden.vbs"
```

Preferred scheduled task action:

```powershell
wscript.exe ".\start_codex_limit_clock_hidden.vbs"
```

If task registration is blocked, use a per-user Startup shortcut to the same VBS launcher.

## Git Hygiene

Do not commit generated runtime files:

- `runtime/`
- `runtime/codex_limit_clock.log`
- `runtime/codex_usage.jpg`
- `temp_*.jpg`
- `runtime/runtime_state.json`
- `assets/live_screen.jpg`
- root-level preview JPGs

Do commit source, docs, config defaults, and curated assets/previews under `assets/`.
