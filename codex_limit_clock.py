import argparse
import json
import os
import re
import ssl
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib import request
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont

from theme_renderer import render_custom_theme


DEFAULT_CLOCK_IP = "192.168.0.58"
OUTPUT_NAME = "codex_usage.jpg"
LOGO_NAME = "codex_logo.png"
ANTIGRAVITY_LOGO_NAME = "antigravity_logo.png"
ANTIGRAVITY_STALE_LIMIT_MINUTES = 30
CODEX_STALE_LIMIT_MINUTES = 30

PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")
ANTIGRAVITY_CSRF_RE = re.compile(r"--csrf_token\s+(\S+)")
ANTIGRAVITY_QUOTA_METHOD = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
CONFIG_PATH = Path(__file__).with_name("config.json")
STATE_PATH = Path(__file__).with_name("runtime_state.json")
LIVE_PREVIEW_PATH = Path(__file__).parent / "assets" / "live_screen.jpg"


def load_config():
    defaults = {
        "clock_ip": DEFAULT_CLOCK_IP,
        "rotation_interval": 30,
        "ag_model_mode": "auto",
        "alert_threshold": 80,
        "show_splash": True,
        "selected_theme": "default",
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception:
            pass
    return defaults


def save_runtime_state(state):
    try:
        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_PATH)
    except Exception:
        pass



def iter_recent_session_files(limit=40):
    root = Path.home() / ".codex" / "sessions"
    files = list(root.rglob("*.jsonl")) if root.exists() else []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def tail_lines(path, max_bytes=12 * 1024 * 1024):
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        data = f.read()
    return data.decode("utf-8", errors="ignore").splitlines()


def find_latest_limits():
    best = None
    for path in iter_recent_session_files():
        for line in reversed(tail_lines(path)):
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") or {}
            limits = payload.get("rate_limits") or {}
            primary = limits.get("primary") or {}
            secondary = limits.get("secondary") or {}
            usage = (payload.get("info") or {}).get("total_token_usage") or {}
            if "used_percent" not in primary or "used_percent" not in secondary:
                continue
            item = {
                "timestamp": event.get("timestamp"),
                "source": str(path),
                "primary_percent": float(primary.get("used_percent", 0)),
                "weekly_percent": float(secondary.get("used_percent", 0)),
                "primary_reset": primary.get("resets_at"),
                "weekly_reset": secondary.get("resets_at"),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
            if best is None or (item["timestamp"] or "") > (best["timestamp"] or ""):
                best = item
            break
    if not best:
        raise RuntimeError("No Codex token_count event found in ~/.codex/sessions")
    return best


def powershell_json(script, timeout=10):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "PowerShell command failed")
    output = proc.stdout.strip()
    if not output:
        return []
    data = json.loads(output)
    return data if isinstance(data, list) else [data]


def list_antigravity_servers():
    script = r"""
$result = @()
$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'language_server_windows_x64.exe' }
foreach ($p in $procs) {
    $ports = @(Get-NetTCPConnection -State Listen -OwningProcess $p.ProcessId -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort)
    $result += [pscustomobject]@{
        ProcessId = $p.ProcessId
        CommandLine = $p.CommandLine
        Ports = $ports
    }
}
$result | ConvertTo-Json -Compress -Depth 4
"""
    servers = []
    for row in powershell_json(script):
        command_line = row.get("CommandLine") or ""
        token_match = ANTIGRAVITY_CSRF_RE.search(command_line)
        ports = row.get("Ports") or []
        if not isinstance(ports, list):
            ports = [ports]
        if token_match and ports:
            servers.append(
                {
                    "pid": row.get("ProcessId"),
                    "csrf_token": token_match.group(1),
                    "ports": [int(p) for p in ports if p],
                }
            )
    return servers


def fetch_antigravity_quota_response():
    try:
        import grpc
    except ImportError as exc:
        raise RuntimeError("Python grpc package is not available") from exc

    servers = list_antigravity_servers()
    if not servers:
        raise RuntimeError("Antigravity IDE language server is not running")

    last_error = None
    for server in servers:
        for port in server["ports"]:
            channel = None
            try:
                certificate = ssl.get_server_certificate(("127.0.0.1", port), timeout=1).encode("ascii")
                credentials = grpc.ssl_channel_credentials(root_certificates=certificate)
                channel = grpc.secure_channel(
                    f"127.0.0.1:{port}",
                    credentials,
                    options=(("grpc.ssl_target_name_override", "localhost"),),
                )
                stub = channel.unary_unary(ANTIGRAVITY_QUOTA_METHOD)
                return stub(
                    b"",
                    timeout=3,
                    metadata=(("x-codeium-csrf-token", server["csrf_token"]),),
                )
            except Exception as exc:
                last_error = exc
            finally:
                if channel is not None:
                    channel.close()

    message = str(last_error) if last_error else "quota service did not respond"
    raise RuntimeError(f"Antigravity quota service unavailable: {message}")


def read_protobuf_varint(data, pos):
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated protobuf varint")


def parse_protobuf_fields(data, depth=0, max_depth=8):
    pos = 0
    fields = []
    try:
        while pos < len(data):
            key, pos = read_protobuf_varint(data, pos)
            field_no = key >> 3
            wire_type = key & 7
            if field_no == 0:
                return None
            if wire_type == 0:
                value, pos = read_protobuf_varint(data, pos)
                fields.append((field_no, wire_type, value, None))
            elif wire_type == 1:
                value = data[pos : pos + 8]
                pos += 8
                if len(value) != 8:
                    return None
                fields.append((field_no, wire_type, value, None))
            elif wire_type == 2:
                length, pos = read_protobuf_varint(data, pos)
                value = data[pos : pos + length]
                pos += length
                if len(value) != length:
                    return None
                child = parse_protobuf_fields(value, depth + 1, max_depth) if depth < max_depth and value else None
                fields.append((field_no, wire_type, value, child))
            elif wire_type == 5:
                value = data[pos : pos + 4]
                pos += 4
                if len(value) != 4:
                    return None
                fields.append((field_no, wire_type, value, None))
            else:
                return None
        return fields
    except Exception:
        return None


def protobuf_string(value):
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text and all((31 < ord(ch) < 127) or ch in "\r\n\t" for ch in text):
        return text
    return None


def protobuf_immediate_strings(fields):
    strings = []
    if not fields:
        return strings
    for _, wire_type, value, _ in fields:
        if wire_type == 2:
            text = protobuf_string(value)
            if text:
                strings.append(text)
    return strings


def protobuf_floats(fields):
    values = []
    if not fields:
        return values
    for _, wire_type, value, child in fields:
        if wire_type == 5:
            number = struct.unpack("<f", value)[0]
            if -0.01 <= number <= 1.01:
                values.append(number)
        if child:
            values.extend(protobuf_floats(child))
    return values


def iter_protobuf_nodes(fields):
    if not fields:
        return
    yield fields
    for _, _, _, child in fields:
        if child:
            yield from iter_protobuf_nodes(child)


def parse_antigravity_quota_response(payload):
    fields = parse_protobuf_fields(payload)
    if not fields:
        raise RuntimeError("Antigravity quota response could not be parsed")

    groups = {}
    for node in iter_protobuf_nodes(fields):
        strings = protobuf_immediate_strings(node)
        if "Gemini Models" in strings:
            key = "gemini"
            label = "GEMINI"
        elif "Claude and GPT models" in strings:
            key = "claude_gpt"
            label = "CLAUDE/GPT"
        else:
            continue

        group = {"label": label}
        for _, _, _, child in node:
            child_strings = protobuf_immediate_strings(child)
            if not child_strings:
                continue
            floats = protobuf_floats(child)
            if not floats:
                continue
            remaining = max(0.0, min(100.0, floats[0] * 100.0))
            if "Five Hour Limit Remaining" in child_strings or "5h" in child_strings:
                group["five_hour_remaining"] = remaining
            if "Weekly Limit Remaining" in child_strings or "weekly" in child_strings:
                group["weekly_remaining"] = remaining

        if "five_hour_remaining" in group and "weekly_remaining" in group:
            groups[key] = group

    if "gemini" not in groups or "claude_gpt" not in groups:
        raise RuntimeError("Antigravity quota values were not found")

    return {
        "source": "grpc",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "groups": groups,
    }


def find_antigravity_limits_direct():
    return parse_antigravity_quota_response(fetch_antigravity_quota_response())


def read_antigravity_accessibility_rows():
    script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$rows = @()
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
foreach ($w in $wins) {
    if ($w.Current.Name -notlike '*Settings*') {
        continue
    }
    $els = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($e in $els) {
        $name = $e.Current.Name
        if ($name -and $name -match 'Models|Usage|Quota|Limit|Remaining|Gemini|Claude|GPT|Credit|Plan|^[0-9]+%$') {
            $rows += [pscustomobject]@{
                Window = $w.Current.Name
                Name = $name
            }
        }
    }
}
$rows | ConvertTo-Json -Compress -Depth 3
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=12,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Antigravity accessibility read failed")
    output = proc.stdout.strip()
    if not output:
        return []
    data = json.loads(output)
    return data if isinstance(data, list) else [data]


def parse_antigravity_rows(rows):
    by_window = {}
    for row in rows:
        by_window.setdefault(row.get("Window") or "", []).append(row.get("Name") or "")

    for window, names in by_window.items():
        groups = {}
        current = None
        pending = None
        for name in names:
            if name == "Gemini Models":
                current = "gemini"
                groups[current] = {"label": "GEMINI"}
                pending = None
                continue
            if name == "Claude and GPT models":
                current = "claude_gpt"
                groups[current] = {"label": "CLAUDE/GPT"}
                pending = None
                continue
            if not current:
                continue
            if name == "Weekly Limit Remaining":
                pending = "weekly_remaining"
                continue
            if name == "Five Hour Limit Remaining":
                pending = "five_hour_remaining"
                continue
            match = PERCENT_RE.match(name)
            if pending and match:
                groups[current][pending] = float(match.group(1))
                pending = None

        if (
            "gemini" in groups
            and "claude_gpt" in groups
            and "five_hour_remaining" in groups["gemini"]
            and "weekly_remaining" in groups["gemini"]
            and "five_hour_remaining" in groups["claude_gpt"]
            and "weekly_remaining" in groups["claude_gpt"]
        ):
            return {
                "source": "accessibility",
                "source_window": window,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "groups": groups,
            }

    raise RuntimeError("Open Antigravity Settings > Models so the quota values are visible")


def find_antigravity_limits():
    direct_error = None
    try:
        return find_antigravity_limits_direct()
    except Exception as exc:
        direct_error = exc

    try:
        return parse_antigravity_rows(read_antigravity_accessibility_rows())
    except Exception as fallback_error:
        raise RuntimeError(f"Antigravity quota unavailable: {direct_error}; fallback: {fallback_error}") from fallback_error


def font(size, bold=False):
    names = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def format_age(timestamp):
    if not timestamp:
        return ""
    diff = int(time.time() - timestamp)
    if diff < 60:
        return "now"
    mins = diff // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    return f"{hours}h ago"


def clamp_percent(p):
    if p is None:
        return None
    return max(0.0, min(100.0, float(p)))


def load_logo(logo_path, max_size):
    if not logo_path.exists():
        return None
    logo = Image.open(logo_path).convert("RGBA")
    
    if "antigravity" in logo_path.name.lower():
        cleaned = []
        for r, g, b, a in logo.getdata():
            cleaned.append((r, g, b, 0 if r < 8 and g < 8 and b < 8 else a))
        logo.putdata(cleaned)

    alpha_box = logo.getchannel("A").getbbox()
    if alpha_box:
        logo = logo.crop(alpha_box)
    logo.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return logo


def draw_centered_logo(img, logo_path_str, y, max_size):
    logo_path = Path(__file__).with_name(logo_path_str)
    logo = load_logo(logo_path, max_size)
    if logo is None:
        return False
    x = int((240 - logo.width) / 2)
    img.alpha_composite(logo, (x, y))
    return True


def draw_progress_bar(draw, x, y, w, h, percent, accent_color, track_color=(25, 30, 40)):
    if percent is None:
        return
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h//2, fill=track_color)
    if percent > 0:
        fill_w = min(w, max(h, int(w * percent / 100)))
        draw.rounded_rectangle((x, y, x + fill_w, y + h), radius=h//2, fill=accent_color)


def draw_footer(draw, y, right_text_str, is_stale, left_text_str=None, text_color=None):
    muted = text_color if text_color else (150, 160, 180)
    amber = (255, 193, 7)
    fnt = font(14)
    color = amber if is_stale else muted
    
    right_str = f"⚠ STALE · {right_text_str}" if is_stale else right_text_str
    
    box = draw.textbbox((0, 0), right_str, font=fnt)
    rx = 240 - (box[2] - box[0]) - 16
    draw.text((rx, y), right_str, fill=color, font=fnt)
    
    if left_text_str:
        draw.text((16, y), left_text_str, fill=muted, font=fnt)


def get_status_color(is_offline, is_stale):
    if is_offline:
        return (100, 100, 100)
    if is_stale:
        return (255, 193, 7)
    return (76, 175, 80)


def draw_status_dot(draw, is_offline, is_stale, override_color=None):
    if override_color and not is_offline and not is_stale:
        color = override_color
    else:
        color = get_status_color(is_offline, is_stale)
    draw.ellipse((214, 16, 224, 26), fill=color)


def draw_row(draw, y, label, percent, accent_color, track_color=(25, 30, 40), label_color=(150, 160, 180), val_color=(240, 240, 240)):
    draw.text((16, y), label, fill=label_color, font=font(18, True))
    val_str = f"{percent:.0f}%" if percent is not None else "--"
    fnt_val = font(22, True)
    box = draw.textbbox((0, 0), val_str, font=fnt_val)
    draw.text((224 - (box[2] - box[0]), y - 4), val_str, fill=val_color, font=fnt_val)
    draw_progress_bar(draw, 16, y + 26, 208, 12, percent, accent_color, track_color)


def render_codex_screen(data, freshness_state, last_success_ts, output_path, alert_threshold=80.0):
    is_offline = freshness_state == "offline"
    is_stale = freshness_state == "stale"
    
    # Usage percentage: 0% to 100% (progress towards quota limit)
    used_p = clamp_percent(data.get("primary_percent", 0))
    used_w = clamp_percent(data.get("weekly_percent", 0))
    
    is_warning = (used_p is not None and used_p >= alert_threshold) or (used_w is not None and used_w >= alert_threshold)
    
    bg_color = (22, 7, 10, 255) if is_warning else (10, 12, 18, 255)
    border_color = (140, 35, 45) if is_warning else (30, 40, 60)
    track_color = (45, 18, 24) if is_warning else (25, 30, 40)
    accent = (255, 65, 85) if is_warning else (0, 188, 212)
    lbl_color = (230, 130, 140) if is_warning else (150, 160, 180)
    footer_color = (180, 120, 130) if is_warning else (150, 160, 180)
    dot_override = (255, 65, 85) if is_warning else None
    
    img = Image.new("RGBA", (240, 240), bg_color)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=border_color, width=1)
    draw_status_dot(d, is_offline, is_stale, dot_override)
    
    draw_centered_logo(img, LOGO_NAME, 20, 44)
    
    val_p_color = (255, 80, 95) if (is_warning and used_p is not None and used_p >= 80.0) else ((245, 210, 215) if is_warning else (240, 240, 240))
    val_w_color = (255, 80, 95) if (is_warning and used_w is not None and used_w >= 80.0) else ((245, 210, 215) if is_warning else (240, 240, 240))
    
    draw_row(d, 80, "5H", used_p, accent, track_color, lbl_color, val_p_color)
    draw_row(d, 140, "W", used_w, accent, track_color, lbl_color, val_w_color)
    
    reset_ts = data.get("primary_reset")
    reset_str = None
    if reset_ts:
        try:
            if isinstance(reset_ts, (int, float)) or (isinstance(reset_ts, str) and reset_ts.isdigit()):
                reset_str = f"Reset {datetime.fromtimestamp(int(reset_ts)).strftime('%I:%M %p').lstrip('0')}"
        except Exception:
            pass

    draw_footer(d, 208, format_age(last_success_ts), is_stale, reset_str, footer_color)
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_antigravity_usage_screen(logo_name, model_label, five_hour_rem, weekly_rem, freshness_state, last_success_ts, output_path, alert_threshold=80.0):
    is_offline = freshness_state == "offline"
    is_stale = freshness_state == "stale"
    
    # Usage percentage: 0% to 100% (progress towards quota limit)
    # Anti Gravity provides remaining percentage (100% -> 0%), so used = 100 - remaining
    five_used = clamp_percent(100.0 - five_hour_rem) if five_hour_rem is not None else None
    w_used = clamp_percent(100.0 - weekly_rem) if weekly_rem is not None else None
    
    is_warning = (five_used is not None and five_used >= alert_threshold) or (w_used is not None and w_used >= alert_threshold)
    
    bg_color = (22, 7, 10, 255) if is_warning else (10, 12, 18, 255)
    border_color = (140, 35, 45) if is_warning else (30, 40, 60)
    track_color = (45, 18, 24) if is_warning else (25, 30, 40)
    accent = (255, 65, 85) if is_warning else ((41, 182, 246) if "GEMINI" in model_label else (149, 117, 205))
    lbl_color = (230, 130, 140) if is_warning else (150, 160, 180)
    footer_color = (180, 120, 130) if is_warning else (150, 160, 180)
    model_color = (255, 140, 150) if is_warning else (200, 210, 230)
    dot_override = (255, 65, 85) if is_warning else None
    
    img = Image.new("RGBA", (240, 240), bg_color)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=border_color, width=1)
    draw_status_dot(d, is_offline, is_stale, dot_override)
    draw_centered_logo(img, logo_name, 20, 44)
    
    fnt_label = font(16, True)
    w = d.textbbox((0, 0), model_label, font=fnt_label)[2]
    d.text(((240 - w)//2, 70), model_label, fill=model_color, font=fnt_label)
    
    val_5h_color = (255, 80, 95) if (is_warning and five_used is not None and five_used >= 80.0) else ((245, 210, 215) if is_warning else (240, 240, 240))
    val_w_color = (255, 80, 95) if (is_warning and w_used is not None and w_used >= 80.0) else ((245, 210, 215) if is_warning else (240, 240, 240))
    
    draw_row(d, 105, "5H", five_used, accent, track_color, lbl_color, val_5h_color)
    draw_row(d, 160, "W", w_used, accent, track_color, lbl_color, val_w_color)
    draw_footer(d, 208, format_age(last_success_ts), is_stale, text_color=footer_color)
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_splash_screen(logo_name, output_path=None):
    bg_color = (10, 12, 18, 255)
    border_color = (30, 40, 60)
    
    img = Image.new("RGBA", (240, 240), bg_color)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=border_color, width=1)
    
    logo = Image.open(logo_name).convert("RGBA")
    logo.thumbnail((96, 96), Image.Resampling.LANCZOS)
    
    lx = (240 - logo.width) // 2
    ly = (240 - logo.height) // 2
    img.alpha_composite(logo, (lx, ly))
    
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_offline_screen(logo_name, provider_name, last_seen_ts, output_path):
    img = Image.new("RGBA", (240, 240), (10, 12, 18, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=(30, 40, 60), width=1)
    draw_status_dot(d, True, False)
    draw_centered_logo(img, logo_name, 70, 60)
    
    msg = f"Open {provider_name}" if "Anti" in provider_name else f"{provider_name} unavailable"
    fnt = font(16)
    w = d.textbbox((0, 0), msg, font=fnt)[2]
    d.text(((240 - w)//2, 140), msg, fill=(200, 200, 200), font=fnt)
    
    if last_seen_ts:
        age = format_age(last_seen_ts)
        msg2 = f"Last seen {age}"
        w2 = d.textbbox((0, 0), msg2, font=fnt)[2]
        d.text(((240 - w2)//2, 170), msg2, fill=(150, 160, 180), font=fnt)

    img.convert("RGB").save(output_path, "JPEG", quality=92)


def upload_file(clock_ip, file_path, filename=OUTPUT_NAME):
    boundary = "----codex-clock-boundary"
    content = Path(file_path).read_bytes()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = head + content + tail
    req = request.Request(
        f"http://{clock_ip}/photo/upload",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


def get_json(clock_ip, path):
    with request.urlopen(f"http://{clock_ip}{path}", timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def hit(clock_ip, path):
    with request.urlopen(f"http://{clock_ip}{path}", timeout=8) as resp:
        return resp.status


def configure_clock(clock_ip):
    try:
        hit(clock_ip, "/theme/toggle?id=2&state=1")
        hit(clock_ip, "/api/set?key=theme&value=2")
        photos = get_json(clock_ip, "/photo/list").get("files", [])
        for photo in photos:
            name = photo.get("name")
            state = 1 if name == OUTPUT_NAME else 0
            if name:
                hit(clock_ip, f"/photo/toggle?name={quote(name)}&state={state}")
    except Exception as exc:
        print(f"configure_clock error: {exc}")


def get_freshness_state(last_time, limit_minutes):
    if not last_time:
        return "offline"
    mins = (time.time() - last_time) / 60.0
    if mins > limit_minutes:
        return "offline"
    if mins > 5:
        return "stale"
    return "fresh"


def trigger_clock_refresh(clock_ip):
    try:
        hit(clock_ip, "/theme/toggle?id=2&state=1")
    except Exception:
        pass


def run_screen(clock_ip, output_path, configure, screen_name, data_state, show_splash=True, alert_threshold=80.0, theme_id=None):
    summary = ""
    dash_temp_path = output_path.with_name("temp_" + output_path.name)
    
    if not theme_id or theme_id == "default":
        theme_id = load_config().get("selected_theme", "default")

    # 1. Pre-render the main dashboard image FIRST so there is zero render delay after splash
    if theme_id and theme_id != "default":
        if screen_name == "codex":
            codex_data = data_state["codex"]["data"] or {}
            used_p = codex_data.get("primary_percent")
            used_w = codex_data.get("weekly_percent")
            reset_ts = codex_data.get("primary_reset")
            reset_str = "Reset --"
            if reset_ts:
                try:
                    reset_str = f"Reset {datetime.fromtimestamp(int(reset_ts)).strftime('%I:%M %p').lstrip('0')}"
                except Exception:
                    pass
            render_custom_theme(
                theme_id=theme_id,
                logo_path=Path(__file__).with_name(LOGO_NAME),
                model_label="CODEX",
                used_p=used_p,
                used_w=used_w,
                is_offline=(data_state["codex"]["state"] == "offline"),
                is_stale=(data_state["codex"]["state"] == "stale"),
                last_success_ts=data_state["codex"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str=reset_str
            )
            summary = f"codex [{theme_id}]"
        elif screen_name == "codex_offline":
            render_custom_theme(
                theme_id=theme_id,
                logo_path=Path(__file__).with_name(LOGO_NAME),
                model_label="CODEX",
                used_p=None,
                used_w=None,
                is_offline=True,
                is_stale=False,
                last_success_ts=data_state["codex"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str="Offline"
            )
            summary = f"codex offline [{theme_id}]"
        elif screen_name == "ag_gemini":
            data = (data_state["ag"]["data"] or {}).get("groups", {}).get("gemini", {})
            rem_p = data.get("five_hour_remaining")
            rem_w = data.get("weekly_remaining")
            used_p = (100.0 - rem_p) if rem_p is not None else None
            used_w = (100.0 - rem_w) if rem_w is not None else None
            render_custom_theme(
                theme_id=theme_id,
                logo_path=Path(__file__).with_name(ANTIGRAVITY_LOGO_NAME),
                model_label="GEMINI",
                used_p=used_p,
                used_w=used_w,
                is_offline=(data_state["ag"]["state"] == "offline"),
                is_stale=(data_state["ag"]["state"] == "stale"),
                last_success_ts=data_state["ag"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str="5H Limit"
            )
            summary = f"ag_gemini [{theme_id}]"
        elif screen_name == "ag_claude":
            data = (data_state["ag"]["data"] or {}).get("groups", {}).get("claude_gpt", {})
            rem_p = data.get("five_hour_remaining")
            rem_w = data.get("weekly_remaining")
            used_p = (100.0 - rem_p) if rem_p is not None else None
            used_w = (100.0 - rem_w) if rem_w is not None else None
            render_custom_theme(
                theme_id=theme_id,
                logo_path=Path(__file__).with_name(ANTIGRAVITY_LOGO_NAME),
                model_label="CLAUDE/GPT",
                used_p=used_p,
                used_w=used_w,
                is_offline=(data_state["ag"]["state"] == "offline"),
                is_stale=(data_state["ag"]["state"] == "stale"),
                last_success_ts=data_state["ag"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str="5H Limit"
            )
            summary = f"ag_claude [{theme_id}]"
        elif screen_name == "ag_offline":
            render_custom_theme(
                theme_id=theme_id,
                logo_path=Path(__file__).with_name(ANTIGRAVITY_LOGO_NAME),
                model_label="ANTI GRAVITY",
                used_p=None,
                used_w=None,
                is_offline=True,
                is_stale=False,
                last_success_ts=data_state["ag"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str="Offline"
            )
            summary = f"ag offline [{theme_id}]"
    else:
        if screen_name == "codex":
            render_codex_screen(data_state["codex"]["data"], data_state["codex"]["state"], data_state["codex"]["time"], dash_temp_path, alert_threshold)
            summary = "codex"
        elif screen_name == "codex_offline":
            render_offline_screen(LOGO_NAME, "Codex", data_state["codex"]["time"], dash_temp_path)
            summary = "codex offline"
        elif screen_name == "ag_gemini":
            data = data_state["ag"]["data"]["groups"]["gemini"]
            render_antigravity_usage_screen(ANTIGRAVITY_LOGO_NAME, "GEMINI", data.get("five_hour_remaining"), data.get("weekly_remaining"), data_state["ag"]["state"], data_state["ag"]["time"], dash_temp_path, alert_threshold)
            summary = "ag_gemini"
        elif screen_name == "ag_claude":
            data = data_state["ag"]["data"]["groups"]["claude_gpt"]
            render_antigravity_usage_screen(ANTIGRAVITY_LOGO_NAME, "CLAUDE/GPT", data.get("five_hour_remaining"), data.get("weekly_remaining"), data_state["ag"]["state"], data_state["ag"]["time"], dash_temp_path, alert_threshold)
            summary = "ag_claude"
        elif screen_name == "ag_offline":
            render_offline_screen(ANTIGRAVITY_LOGO_NAME, "Anti Gravity", data_state["ag"]["time"], dash_temp_path)
            summary = "ag offline"

    # 2. Show splash transition if enabled (targeting exact ~0.5s screen visible duration)
    if show_splash:
        splash_shown = False
        if screen_name == "codex" and data_state["codex"]["data"]:
            render_splash_screen(LOGO_NAME, output_path)
            splash_shown = True
        elif screen_name in ("ag_gemini", "ag_claude") and data_state["ag"]["data"]:
            render_splash_screen(ANTIGRAVITY_LOGO_NAME, output_path)
            splash_shown = True
            
        if splash_shown:
            upload_file(clock_ip, output_path)
            trigger_clock_refresh(clock_ip)
            try:
                import shutil
                shutil.copy2(str(output_path), str(LIVE_PREVIEW_PATH))
            except Exception:
                pass
            time.sleep(0.5)

    # 3. Upload the pre-rendered main dashboard image immediately
    status, _ = upload_file(clock_ip, dash_temp_path, filename=OUTPUT_NAME)
    trigger_clock_refresh(clock_ip)
    try:
        import shutil
        shutil.copy2(str(dash_temp_path), str(LIVE_PREVIEW_PATH))
    except Exception:
        pass
    if configure:
        configure_clock(clock_ip)
    print(f"uploaded {output_path.name} to {clock_ip} | {summary} status={status}")


def get_antigravity_selected_model():
    """
    Detects the real-time active Antigravity model directly from IDE session logs.
    Returns: 'gemini' or 'claude_gpt' if detected, else None.
    """
    brain_root = Path.home() / ".gemini" / "antigravity-ide" / "brain"
    if not brain_root.exists():
        return None

    transcripts = list(brain_root.glob("*/.system_generated/logs/transcript_full.jsonl"))
    if not transcripts:
        transcripts = list(brain_root.glob("*/.system_generated/logs/transcript.jsonl"))
    if not transcripts:
        return None

    transcripts.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    change_regex = re.compile(
        r"The user changed setting `Model Selection` from .*? to (.*?)(?:\.\s*No need|\.\s*</USER_SETTINGS_CHANGE>|\.$)",
        re.DOTALL,
    )

    for transcript_path in transcripts[:10]:
        try:
            lines = transcript_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in reversed(lines):
                if '"USER_INPUT"' not in line or "Model Selection" not in line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                if data.get("type") != "USER_INPUT":
                    continue

                content = data.get("content") or ""
                match = change_regex.search(content)
                if match:
                    model_target = match.group(1).strip()
                    lower = model_target.lower()
                    if any(x in lower for x in ["claude", "sonnet", "opus", "gpt"]):
                        return "claude_gpt"
                    elif any(x in lower for x in ["gemini", "flash", "pro"]):
                        return "gemini"
        except Exception:
            continue

    return None


def determine_active_ag_model(ag_data, previous_ag_data, current_active_model="gemini"):
    # 1. Primary: Direct real-time detection from IDE conversation logs
    detected = get_antigravity_selected_model()
    if detected:
        return detected

    if not ag_data or "groups" not in ag_data:
        return current_active_model

    # 2. Fallback: Usage delta between refresh cycles
    groups = ag_data.get("groups", {})
    gem_curr = groups.get("gemini", {})
    claude_curr = groups.get("claude_gpt", {})

    if previous_ag_data and "groups" in previous_ag_data:
        prev_groups = previous_ag_data.get("groups", {})
        gem_prev = prev_groups.get("gemini", {})
        claude_prev = prev_groups.get("claude_gpt", {})

        gem_delta = 0.0
        if gem_prev and gem_curr:
            gem_5h_drop = (gem_prev.get("five_hour_remaining") or 0.0) - (gem_curr.get("five_hour_remaining") or 0.0)
            gem_week_drop = (gem_prev.get("weekly_remaining") or 0.0) - (gem_curr.get("weekly_remaining") or 0.0)
            gem_delta = max(gem_5h_drop, gem_week_drop)

        claude_delta = 0.0
        if claude_prev and claude_curr:
            claude_5h_drop = (claude_prev.get("five_hour_remaining") or 0.0) - (claude_curr.get("five_hour_remaining") or 0.0)
            claude_week_drop = (claude_prev.get("weekly_remaining") or 0.0) - (claude_curr.get("weekly_remaining") or 0.0)
            claude_delta = max(claude_5h_drop, claude_week_drop)

        if gem_delta > 0.01 and gem_delta >= claude_delta:
            return "gemini"
        if claude_delta > 0.01 and claude_delta > gem_delta:
            return "claude_gpt"

    return current_active_model


def main():
    parser = argparse.ArgumentParser(description="Show local limit meter on Smart Weather Clock.")
    parser.add_argument("--clock-ip", default=os.getenv("CODEX_CLOCK_IP", DEFAULT_CLOCK_IP))
    parser.add_argument("--output", default=OUTPUT_NAME)
    parser.add_argument("--screens", default="codex,antigravity", help="(Ignored) Handled dynamically.")
    parser.add_argument("--ag-model", default="auto", choices=["auto", "gemini", "claude", "both"], help="Anti Gravity model to display: auto (active model), gemini, claude, or both.")
    parser.add_argument("--no-splash", action="store_true", help="Disable the 1-second logo splash transition between screens.")
    parser.add_argument("--loop", type=int, default=30, help="Refresh interval in seconds. 0 means run once.")
    parser.add_argument("--no-configure", action="store_true", help="Upload only; do not switch the clock to Photo mode.")
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    
    last_codex_data = None
    last_codex_time = 0
    last_ag_data = None
    previous_ag_data = None
    active_ag_model = "gemini"
    last_ag_time = 0
    page_index = 0
    is_first_run = True

    while True:
        # Load runtime configuration (hot-reloaded from config.json)
        config = load_config()
        active_clock_ip = config.get("clock_ip", args.clock_ip)
        ag_model_mode = config.get("ag_model_mode", args.ag_model)
        show_splash = config.get("show_splash", not args.no_splash)
        alert_threshold = float(config.get("alert_threshold", 80))
        loop_interval = max(5, int(config.get("rotation_interval", args.loop)))

        try:
            codex_data = find_latest_limits()
            if codex_data:
                last_codex_data = codex_data
                last_codex_time = time.time()
        except Exception:
            pass

        try:
            ag_data = find_antigravity_limits()
            if ag_data:
                active_ag_model = determine_active_ag_model(ag_data, previous_ag_data, active_ag_model)
                previous_ag_data = ag_data
                last_ag_data = ag_data
                last_ag_time = time.time()
        except Exception:
            pass
            
        codex_state = get_freshness_state(last_codex_time, CODEX_STALE_LIMIT_MINUTES)
        ag_state = get_freshness_state(last_ag_time, ANTIGRAVITY_STALE_LIMIT_MINUTES)
        
        active_pages = []
        if codex_state == "offline" or not last_codex_data:
            active_pages.append("codex_offline")
        else:
            active_pages.append("codex")
            
        if ag_state == "offline" or not last_ag_data:
            active_pages.append("ag_offline")
        else:
            if ag_model_mode == "gemini":
                active_pages.append("ag_gemini")
            elif ag_model_mode == "claude":
                active_pages.append("ag_claude")
            elif ag_model_mode == "both":
                active_pages.append("ag_gemini")
                active_pages.append("ag_claude")
            else:  # auto
                if active_ag_model == "claude_gpt":
                    active_pages.append("ag_claude")
                else:
                    active_pages.append("ag_gemini")
            
        if not active_pages:
            active_pages = ["codex_offline"]
            
        current_page = active_pages[page_index % len(active_pages)]
        page_index = (page_index + 1) % len(active_pages)
        
        data_state = {
            "codex": {"data": last_codex_data, "time": last_codex_time, "state": codex_state},
            "ag": {"data": last_ag_data, "time": last_ag_time, "state": ag_state}
        }

        # Save runtime state for the local web dashboard
        ag_gem = (last_ag_data or {}).get("groups", {}).get("gemini", {})
        ag_claude = (last_ag_data or {}).get("groups", {}).get("claude_gpt", {})
        save_runtime_state({
            "timestamp": time.time(),
            "active_page": current_page,
            "active_ag_model": active_ag_model,
            "clock_ip": active_clock_ip,
            "codex": {
                "state": codex_state,
                "primary_percent": last_codex_data.get("primary_percent") if last_codex_data else None,
                "weekly_percent": last_codex_data.get("weekly_percent") if last_codex_data else None,
                "primary_reset": last_codex_data.get("primary_reset") if last_codex_data else None,
                "last_time": last_codex_time,
            },
            "ag": {
                "state": ag_state,
                "last_time": last_ag_time,
                "gemini": {
                    "five_hour_remaining": ag_gem.get("five_hour_remaining"),
                    "weekly_remaining": ag_gem.get("weekly_remaining"),
                },
                "claude_gpt": {
                    "five_hour_remaining": ag_claude.get("five_hour_remaining"),
                    "weekly_remaining": ag_claude.get("weekly_remaining"),
                },
            },
            "config": config,
        })
        
        try:
            should_configure = not args.no_configure and is_first_run
            run_screen(active_clock_ip, output_path, should_configure, current_page, data_state, show_splash=show_splash, alert_threshold=alert_threshold)
            is_first_run = False
        except Exception as exc:
            print(f"error: {exc}")
            
        if args.loop <= 0:
            break
            
        # Sleep in 1-second ticks so changes in config.json or refresh triggers apply promptly
        sleep_interval = max(1.0, loop_interval - 0.8) if show_splash else loop_interval
        start_sleep = time.time()
        initial_theme = config.get("selected_theme", "default")
        while time.time() - start_sleep < sleep_interval:
            time.sleep(0.5)
            # Check if a refresh was requested via config or if theme changed
            new_conf = load_config()
            if new_conf.get("force_refresh") or new_conf.get("selected_theme", "default") != initial_theme:
                # Clear flag and break out to refresh immediately
                new_conf.pop("force_refresh", None)
                try:
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(new_conf, f, indent=2)
                except Exception:
                    pass
                break


if __name__ == "__main__":
    main()
