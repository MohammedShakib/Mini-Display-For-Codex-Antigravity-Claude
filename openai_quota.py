"""Local OpenAI complimentary-pool usage client, cache, and 240px renderer."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).parent.resolve()
MODEL_LIST_PATH = BASE_DIR / "openai_quota_models.json"
EXPORT_PATH = BASE_DIR / "output" / "openai_quota_status.json"
CACHE_PATH = BASE_DIR / "runtime" / "openai_quota_cache.json"
ENDPOINT = "https://api.openai.com/v1/organization/usage/completions"
DEFAULT_QUOTA = 2_500_000


def load_eligible_models():
    try:
        with MODEL_LIST_PATH.open("r", encoding="utf-8") as handle:
            values = json.load(handle)
        return {str(value).strip() for value in values if str(value).strip()}
    except Exception:
        return set()


def _font(size, bold=False):
    candidates = ["C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf", "arial.ttf"]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _tokens(record):
    def number(*names):
        for name in names:
            value = record.get(name)
            if isinstance(value, (int, float)):
                return max(0, int(value))
        return 0
    # input_tokens is already inclusive of cached input on the Usage API.
    return number("input_tokens", "prompt_tokens") + number("output_tokens", "completion_tokens")


def _records(payload):
    records = []
    for bucket in payload.get("data", []) if isinstance(payload, dict) else []:
        if isinstance(bucket, dict) and isinstance(bucket.get("results"), list):
            records.extend(item for item in bucket["results"] if isinstance(item, dict))
        elif isinstance(bucket, dict):
            records.append(bucket)
    return records


def calculate_usage(payload, tracked_api_key_id, daily_quota=DEFAULT_QUOTA):
    eligible = load_eligible_models()
    org_used = 0
    key_used = 0
    included_records = 0
    for record in _records(payload):
        if record.get("model") not in eligible:
            continue
        tokens = _tokens(record)
        org_used += tokens
        if tracked_api_key_id and str(record.get("api_key_id") or "") == str(tracked_api_key_id):
            key_used += tokens
        included_records += 1
    org_used = max(0, org_used)
    quota = max(1, int(daily_quota or DEFAULT_QUOTA))
    remaining = max(0, quota - org_used)
    percent_used = max(0.0, min(100.0, (org_used / quota) * 100.0))
    return {
        "organization_used": org_used,
        "selected_key_used": max(0, key_used),
        "estimated_remaining": remaining,
        "percent_used": percent_used,
        "percent_remaining": max(0.0, min(100.0, 100.0 - percent_used)),
        "included_records": included_records,
    }


def _read_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _write_json(path, value):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
        tmp.replace(path)
    except Exception:
        pass


def _day_start_utc():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _reset_label(timezone_name):
    try:
        zone = ZoneInfo(timezone_name or "Asia/Dhaka")
    except Exception:
        zone = timezone.utc
    reset = datetime.fromtimestamp(_day_start_utc().timestamp() + 86400, zone)
    return reset.timestamp(), reset.strftime("%I:%M %p").lstrip("0")


def _fetch(config, timeout=15):
    key = os.getenv("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_ADMIN_KEY is not set")
    start = int(_day_start_utc().timestamp())
    params = f"?start_time={start}&end_time={int(time.time())}&bucket_width=1d&group_by=model&group_by=api_key_id&limit=31"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    org_id = str(config.get("organization_id") or "").strip()
    if org_id:
        headers["OpenAI-Organization"] = org_id
    req = request.Request(ENDPOINT + params, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = str((detail.get("error") or {}).get("message") or "").strip()
        except Exception:
            message = ""
        suffix = f": {message[:160]}" if message else ""
        if exc.code in (401, 403, 429):
            raise RuntimeError(f"OpenAI usage request returned HTTP {exc.code}{suffix}") from exc
        raise RuntimeError(f"OpenAI usage request returned HTTP {exc.code}{suffix}") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenAI usage request failed") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("OpenAI usage response was malformed")
    return payload


def _export(config, data):
    result = {
        "organization_id": config.get("organization_id", ""),
        "tracked_api_key_id": config.get("tracked_api_key_id", ""),
        "last_sync": data.get("last_sync"),
        "daily_quota": data.get("daily_quota", DEFAULT_QUOTA),
        "organization_used": data.get("organization_used"),
        "selected_key_used": data.get("selected_key_used"),
        "estimated_remaining": data.get("estimated_remaining"),
        "percent_used": data.get("percent_used"),
        "percent_remaining": data.get("percent_remaining"),
        "status": data.get("status"),
        "stale": bool(data.get("stale")),
        "display_reset_time": data.get("display_reset_time"),
        "note": "Estimated remaining quota is based on organization eligible usage",
    }
    _write_json(EXPORT_PATH, result)


def get_status(config, force=False):
    """Fetch at most once per refresh interval, retaining the last good result."""
    quota_config = config.get("openai_quota") or {}
    base = {
        "daily_quota": int(quota_config.get("daily_quota", DEFAULT_QUOTA) or DEFAULT_QUOTA),
        "organization_used": None, "selected_key_used": None, "estimated_remaining": None,
        "percent_used": None, "percent_remaining": None, "last_sync": None,
        "status": "not_configured", "stale": False, "display_reset_time": None,
    }
    cached = _read_json(CACHE_PATH) or {}
    base.update(cached)
    base["daily_quota"] = int(quota_config.get("daily_quota", base.get("daily_quota", DEFAULT_QUOTA)) or DEFAULT_QUOTA)
    refresh = max(60, int(quota_config.get("refresh_interval_seconds", 300) or 300))
    configured = bool(quota_config.get("enabled", False) and quota_config.get("organization_id") and quota_config.get("tracked_api_key_id"))
    if not configured:
        base.update(status="not_configured", stale=False)
        _export(quota_config, base)
        return base
    last_sync = float(base.get("last_sync") or 0)
    if not force and last_sync and time.time() - last_sync < refresh:
        _, base["display_reset_time"] = _reset_label(quota_config.get("timezone", "Asia/Dhaka"))
        base["status"] = "stale" if base.get("stale") else "ok"
        _export(quota_config, base)
        return base
    try:
        values = calculate_usage(_fetch(quota_config), quota_config.get("tracked_api_key_id"), base["daily_quota"])
        base.update(values, last_sync=time.time(), status="ok", stale=False)
        _write_json(CACHE_PATH, base)
    except Exception as exc:
        base["status"] = "cached" if base.get("last_sync") else "error"
        base["stale"] = bool(base.get("last_sync"))
        base["error"] = str(exc)[:120]
    _, base["display_reset_time"] = _reset_label(quota_config.get("timezone", "Asia/Dhaka"))
    _export(quota_config, base)
    return base


def compact_tokens(value):
    if value is None:
        return "--"
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(int(value))


def render_screen(data, output_path):
    img = Image.new("RGBA", (240, 240), (7, 12, 21, 255))
    draw = ImageDraw.Draw(img)
    white, muted, cyan, green, red = (240, 246, 252), (150, 165, 184), (48, 190, 255), (52, 224, 146), (255, 92, 105)
    draw.rounded_rectangle((10, 10, 230, 230), radius=16, outline=(27, 55, 78), width=2)
    logo_path = BASE_DIR / "assets" / "openai_logo.png"
    try:
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((46, 46), Image.Resampling.LANCZOS)
        img.alpha_composite(logo, ((240 - logo.width) // 2, 20))
    except Exception:
        pass
    if data.get("status") == "not_configured":
        draw.text((22, 92), "Not configured", fill=white, font=_font(18, True))
    elif data.get("estimated_remaining") is None:
        draw.text((22, 92), "No data yet", fill=white, font=_font(18, True))
    else:
        remaining = data.get("estimated_remaining", 0)
        color = red if float(data.get("percent_remaining", 0)) < 20 else green
        draw.text((22, 76), compact_tokens(remaining), fill=color, font=_font(37, True))
        draw.text((23, 121), f"{float(data.get('percent_remaining', 0)):.0f}% left", fill=white, font=_font(16, True))
        draw.rounded_rectangle((22, 153, 218, 166), radius=6, fill=(25, 39, 54))
        draw.rounded_rectangle((22, 153, 22 + int(196 * float(data.get("percent_remaining", 0)) / 100), 166), radius=6, fill=color)
    reset = data.get("display_reset_time") or "6:00 AM"
    draw.text((22, 204), f"Reset {reset}", fill=muted, font=_font(11, True))
    img.convert("RGB").save(output_path, "JPEG", quality=92)
