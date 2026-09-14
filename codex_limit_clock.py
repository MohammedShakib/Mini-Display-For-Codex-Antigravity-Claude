import argparse
import base64
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import request
from urllib.parse import quote, urlparse

from PIL import Image, ImageDraw, ImageFont

from theme_renderer import render_custom_theme


BASE_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = BASE_DIR / "assets"
RUNTIME_DIR = BASE_DIR / "runtime"

DEFAULT_CLOCK_IP = "192.168.0.58"
OUTPUT_NAME = "codex_usage.jpg"
DEFAULT_OUTPUT_PATH = RUNTIME_DIR / OUTPUT_NAME
LOGO_NAME = "codex_logo.png"
ANTIGRAVITY_LOGO_NAME = "antigravity_logo.png"
GITHUB_LOGO_NAME = "github_logo.png"
GIT_BRANCH_ICON_NAME = "git_branch_icon.png"
ANTIGRAVITY_STALE_LIMIT_MINUTES = 30
CODEX_STALE_LIMIT_MINUTES = 30
GITHUB_STALE_LIMIT_MINUTES = 60
ANTIGRAVITY_OFFLINE_SECONDS = 10
CODEX_PING_FAILURE_RETRY_SECONDS = 5 * 60
CODEX_PING_DIR = Path(os.getenv("TEMP", str(RUNTIME_DIR))) / "codex-quota-ping"
CODEX_PING_PROMPT = "Reply exactly: OK"

PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")
ANTIGRAVITY_CSRF_RE = re.compile(r"--csrf_token\s+(\S+)")
ANTIGRAVITY_QUOTA_METHOD = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = RUNTIME_DIR / "runtime_state.json"
LIVE_PREVIEW_PATH = ASSETS_DIR / "live_screen.jpg"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"


def resolve_asset_path(name_or_path):
    path = Path(name_or_path)
    if path.is_absolute() or path.exists():
        return path
    asset_path = ASSETS_DIR / path.name
    if asset_path.exists():
        return asset_path
    return BASE_DIR / path.name


def load_config():
    defaults = {
        "clock_ip": DEFAULT_CLOCK_IP,
        "rotation_interval": 30,
        "ag_model_mode": "auto",
        "alert_threshold": 80,
        "show_splash": True,
        "splash_duration_seconds": 2,
        "codex_display_seconds": 15,
        "antigravity_display_seconds": 15,
        "github_enabled": True,
        "github_display_seconds": 15,
        "github_refresh_interval_minutes": 1,
        "github_repo": "",
        "github_branch": "",
        "github_label": "",
        "github_activity_enabled": True,
        "github_user": "",
        "selected_theme": "default",
        "codex_ping_enabled": True,
        "codex_ping_interval_minutes": 30,
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
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_PATH)
    except Exception:
        pass


def load_runtime_state():
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_cached_antigravity_data(previous_state):
    ag_state = (previous_state or {}).get("ag") or {}
    groups = {}
    for key in ("gemini", "claude_gpt"):
        group = ag_state.get(key) or {}
        if any(group.get(field) is not None for field in ("five_hour_remaining", "weekly_remaining")):
            groups[key] = {
                "five_hour_remaining": group.get("five_hour_remaining"),
                "weekly_remaining": group.get("weekly_remaining"),
                "five_hour_reset": group.get("five_hour_reset"),
                "weekly_reset": group.get("weekly_reset"),
            }
    if not groups:
        return None
    return {
        "source": "cached",
        "timestamp": ag_state.get("last_time"),
        "groups": groups,
    }


def load_cached_codex_data(previous_state):
    codex_state = (previous_state or {}).get("codex") or {}
    if codex_state.get("primary_percent") is None or codex_state.get("weekly_percent") is None:
        return None
    data = {
        "source": "cached",
        "timestamp": codex_state.get("last_event_time") or codex_state.get("last_time"),
        "primary_percent": float(codex_state.get("primary_percent") or 0),
        "weekly_percent": float(codex_state.get("weekly_percent") or 0),
        "primary_reset": codex_state.get("primary_reset"),
        "weekly_reset": codex_state.get("weekly_reset"),
        "total_tokens": 0,
    }
    return apply_codex_reset_correction(data)


def load_cached_github_data(previous_state):
    gh_state = (previous_state or {}).get("github") or {}
    if not gh_state.get("repo"):
        return None
    return {
        "source": "cached",
        "repo": gh_state.get("repo"),
        "display_label": gh_state.get("display_label"),
        "repo_name": gh_state.get("repo_name"),
        "branch": gh_state.get("branch") or "main",
        "today_commits": int(gh_state.get("today_commits") or 0),
        "today_label": gh_state.get("today_label") or "commits",
        "open_prs": int(gh_state.get("open_prs") or 0),
        "last_push": gh_state.get("last_push"),
        "latest_sha": gh_state.get("latest_sha"),
        "latest_message": gh_state.get("latest_message"),
        "latest_author": gh_state.get("latest_author"),
        "latest_date": gh_state.get("latest_date"),
        "local_dirty": bool(gh_state.get("local_dirty")),
        "local_ahead": int(gh_state.get("local_ahead") or 0),
        "local_behind": int(gh_state.get("local_behind") or 0),
    }


def decode_jwt_payload(token):
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = token.split(".", 2)[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_codex_account_email():
    try:
        with open(CODEX_AUTH_PATH, "r", encoding="utf-8") as f:
            auth = json.load(f)
    except Exception:
        return None

    tokens = auth.get("tokens") or {}
    payload = decode_jwt_payload(tokens.get("id_token"))
    for key in ("email", "preferred_username", "login"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def codex_account_display_name(email):
    if not email:
        return None
    name = str(email).split("@", 1)[0].strip()
    return name or None


def codex_account_footer_label(account_name, length=6):
    if not account_name:
        return None
    compact = re.sub(r"\s+", "", str(account_name).strip())
    return compact[:length] or None



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


def reset_time_has_passed(reset_ts):
    if not reset_ts:
        return False
    try:
        return int(reset_ts) <= int(time.time())
    except Exception:
        return False


def apply_codex_reset_correction(data):
    if reset_time_has_passed(data.get("primary_reset")):
        data["primary_percent"] = 0.0
        data["primary_reset_elapsed"] = True
    else:
        data["primary_reset_elapsed"] = False

    if reset_time_has_passed(data.get("weekly_reset")):
        data["weekly_percent"] = 0.0
        data["weekly_reset_elapsed"] = True
    else:
        data["weekly_reset_elapsed"] = False
    return data


def parse_timestamp_epoch(value):
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return float(text)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    return 0.0


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
    return apply_codex_reset_correction(best)


def should_ping_codex(last_codex_data, last_ping_time, interval_minutes):
    interval_seconds = max(60, int(interval_minutes * 60))
    now = time.time()
    if last_ping_time and now - last_ping_time < interval_seconds:
        return False
    event_time = parse_timestamp_epoch((last_codex_data or {}).get("timestamp"))
    if not event_time:
        return True
    return now - event_time >= interval_seconds


def find_codex_executable():
    configured = os.getenv("CODEX_EXE")
    if configured and Path(configured).exists():
        return configured

    found = shutil.which("codex") or shutil.which("codex.exe")
    if found:
        return found

    extension_root = Path.home() / ".vscode" / "extensions"
    candidates = list(extension_root.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"))
    candidates = [path for path in candidates if path.exists()]
    if candidates:
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return str(candidates[0])

    raise FileNotFoundError("codex executable was not found in PATH or the VS Code extension directory")


def ping_codex_quota(timeout=120):
    CODEX_PING_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            find_codex_executable(),
            "exec",
            "-c",
            'web_search="disabled"',
            "-c",
            'model_reasoning_effort="none"',
            "--cd",
            str(CODEX_PING_DIR),
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-",
        ],
        input=CODEX_PING_PROMPT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def run_git(args, timeout=8, cwd=None):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(Path(cwd or BASE_DIR)),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git command failed").strip())
    return proc.stdout.strip()


def parse_github_repo(remote_url):
    if not remote_url:
        return None
    text = remote_url.strip()
    if text.startswith("git@github.com:"):
        repo = text.split(":", 1)[1]
    else:
        parsed = urlparse(text)
        if parsed.netloc.lower() != "github.com":
            return None
        repo = parsed.path.lstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    parts = [part for part in repo.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def git_root_for_path(path):
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def extract_existing_paths_from_command_line(command_line):
    if not command_line:
        return []
    candidates = []
    quoted = re.findall(r'"([^"]+)"', command_line)
    unquoted = re.findall(r"(?<![=\w])([A-Za-z]:\\[^\s\"]+)", command_line)
    for raw in [*quoted[1:], *unquoted]:
        try:
            path = Path(raw)
        except Exception:
            continue
        if path.exists() and path.is_dir():
            candidates.append(path)
    return candidates


def active_editor_git_roots():
    script = (
        "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { ($_.Name -eq 'Code.exe' -or $_.Name -eq 'Cursor.exe') "
        "-and $_.CommandLine -notmatch '--type=' } | "
        "Sort-Object CreationDate -Descending | "
        "Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        command_lines = json.loads(proc.stdout)
    except Exception:
        return []
    if isinstance(command_lines, str):
        command_lines = [command_lines]
    roots = []
    seen = set()
    for command_line in command_lines or []:
        for path in extract_existing_paths_from_command_line(command_line):
            root = git_root_for_path(path)
            if root:
                key = str(root).lower()
                if key not in seen:
                    roots.append(root)
                    seen.add(key)
    return roots


def active_editor_git_root():
    roots = active_editor_git_roots()
    return roots[0] if roots else None


def scan_git_roots(base_path, max_depth=4, max_roots=40):
    base = Path(base_path)
    if not base.exists() or not base.is_dir():
        return []
    roots = []
    base_parts = len(base.resolve().parts)
    skip_names = {
        ".git",
        ".next",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
    }
    for current, dirs, _files in os.walk(base):
        current_path = Path(current)
        depth = len(current_path.resolve().parts) - base_parts
        if ".git" in dirs:
            roots.append(current_path)
            dirs[:] = []
            if len(roots) >= max_roots:
                break
            continue
        dirs[:] = [name for name in dirs if name not in skip_names]
        if depth >= max_depth:
            dirs[:] = []
    return roots


def candidate_git_roots():
    roots = []
    seen = set()

    def add(root):
        if not root:
            return
        try:
            root = Path(root).resolve()
        except Exception:
            return
        key = str(root).lower()
        if key not in seen:
            roots.append(root)
            seen.add(key)

    for root in active_editor_git_roots():
        add(root)
    add(BASE_DIR)

    userprofile = Path(os.getenv("USERPROFILE", ""))
    search_bases = [
        Path("D:/projects"),
        Path("D:/Projects"),
        userprofile / "projects",
        userprofile / "Projects",
    ]
    for base in search_bases:
        for root in scan_git_roots(base):
            add(root)

    return roots


def most_recent_local_github_repo():
    best = None
    for root in candidate_git_roots():
        status = get_local_git_status(root)
        if not status.get("repo"):
            continue
        commit = get_local_git_commit_summary(root)
        ts = parse_timestamp_epoch(commit.get("latest_date")) or 0
        if best is None or ts > best["timestamp"]:
            best = {
                "root": root,
                "status": status,
                "commit": commit,
                "timestamp": ts,
            }
    return best


def get_local_git_status(repo_root=None):
    branch = ""
    cwd = repo_root or BASE_DIR
    try:
        branch = run_git(["branch", "--show-current"], cwd=cwd) or "main"
    except Exception:
        branch = "main"

    remote_repo = None
    try:
        remote_repo = parse_github_repo(run_git(["remote", "get-url", "origin"], cwd=cwd))
    except Exception:
        pass

    dirty = False
    ahead = 0
    behind = 0
    try:
        status = run_git(["status", "--porcelain=v2", "--branch"], cwd=cwd)
        for line in status.splitlines():
            if line.startswith("# branch.ab"):
                match = re.search(r"\+(\d+)\s+-(\d+)", line)
                if match:
                    ahead = int(match.group(1))
                    behind = int(match.group(2))
            elif line and not line.startswith("#"):
                dirty = True
    except Exception:
        pass

    return {
        "root": str(cwd),
        "repo": remote_repo,
        "branch": branch,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
    }


def get_local_git_commit_summary(repo_root=None):
    cwd = repo_root or BASE_DIR
    try:
        latest_sha = run_git(["rev-parse", "--short=7", "HEAD"], cwd=cwd)
        latest_message = run_git(["log", "-1", "--pretty=%s"], cwd=cwd)
        latest_author = run_git(["log", "-1", "--pretty=%an"], cwd=cwd)
        latest_date = run_git(["log", "-1", "--pretty=%cI"], cwd=cwd)
    except Exception:
        latest_sha = ""
        latest_message = "--"
        latest_author = "--"
        latest_date = None

    try:
        today_count = int(run_git(["rev-list", "--count", "--since=midnight", "HEAD"], cwd=cwd) or 0)
    except Exception:
        today_count = 0

    return {
        "today_commits": today_count,
        "latest_sha": latest_sha,
        "latest_message": latest_message or "--",
        "latest_author": latest_author or "--",
        "latest_date": latest_date,
    }


def github_api_json(path_or_url, timeout=10):
    url = path_or_url if path_or_url.startswith("https://") else f"https://api.github.com{path_or_url}"
    req = request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "SyncAI-Quota-Display",
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iso_utc_from_local_midnight():
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def github_contributions_today(user):
    if not user:
        return None
    today = date.today()
    url = (
        f"https://github.com/users/{quote(user)}/contributions"
        f"?from={today.isoformat()}&to={today.isoformat()}"
    )
    try:
        html = request.urlopen(
            request.Request(url, headers={"User-Agent": "SyncAI-Quota-Display"}),
            timeout=10,
        ).read().decode("utf-8", "ignore")
    except Exception:
        return None

    date_pattern = re.escape(today.isoformat())
    tooltip_match = re.search(
        rf"data-date=\"{date_pattern}\".*?<tool-tip[^>]*>(.*?)</tool-tip>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if tooltip_match:
        label = re.sub(r"<[^>]+>", " ", tooltip_match.group(1))
        count_match = re.search(r"(\d+)\s+contributions?", label, flags=re.IGNORECASE)
        if count_match:
            return int(count_match.group(1))
        if re.search(r"no\s+contributions?", label, flags=re.IGNORECASE):
            return 0

    date_index = html.find(f'data-date="{today.isoformat()}"')
    if date_index != -1:
        snippet = html[max(0, date_index - 600):date_index + 1200]
        count_match = re.search(r"(\d+)\s+contributions?", snippet, flags=re.IGNORECASE)
        if count_match:
            return int(count_match.group(1))
        if re.search(r"no\s+contributions?", snippet, flags=re.IGNORECASE):
            return 0
    return None


def github_user_from_repo(repo):
    return repo.split("/", 1)[0] if repo and "/" in repo else None


def humanize_repo_name(repo):
    name = repo.split("/", 1)[-1] if repo else "Repo"
    name = re.sub(r"[-_]+", " ", name)
    name = re.sub(r"\b(for|codex|antigravity|claude)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    words = name.split()
    if len(words) > 2:
        name = " ".join(words[:2])
    if name.islower():
        name = name.title()
    return name or "Repo"


def latest_public_push_activity(user):
    if not user:
        return None
    try:
        events = github_api_json(f"/users/{quote(user)}/events/public?per_page=30")
    except Exception:
        return None
    if not isinstance(events, list):
        return None

    midnight_ts = parse_timestamp_epoch(iso_utc_from_local_midnight())
    today_count = 0
    latest = None
    for event in events:
        if event.get("type") != "PushEvent":
            continue
        payload = event.get("payload") or {}
        commits = payload.get("commits") or []
        head_sha = payload.get("head") or ""
        if not commits and not head_sha:
            continue
        event_ts = parse_timestamp_epoch(event.get("created_at"))
        if commits and event_ts >= midnight_ts:
            today_count += len(commits)
        if latest is None:
            repo_name = (event.get("repo") or {}).get("name")
            ref = payload.get("ref") or ""
            branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref or "main"
            commit = commits[-1] if commits else {}
            message = (commit.get("message") or "").splitlines()
            latest = {
                "repo": repo_name,
                "branch": branch,
                "last_push": event.get("created_at"),
                "latest_sha": (commit.get("sha") or head_sha or "")[:7],
                "latest_message": message[0] if message else "",
                "latest_author": ((commit.get("author") or {}).get("name") or user),
            }

    if latest:
        latest["today_commits"] = today_count
    return latest


def find_github_status(config):
    configured_repo = (config.get("github_repo") or "").strip()
    recent_local = None if configured_repo else most_recent_local_github_repo()
    active_root = recent_local.get("root") if recent_local else None
    local = recent_local.get("status") if recent_local else get_local_git_status(active_root)
    local_commit = recent_local.get("commit") if recent_local else get_local_git_commit_summary(active_root)
    local_repo = local.get("repo") or ""
    configured_user = (config.get("github_user") or "").strip()
    github_user = configured_user or github_user_from_repo(configured_repo or local_repo)
    activity = None
    if config.get("github_activity_enabled", True):
        activity = latest_public_push_activity(github_user)

    if activity and local_repo and activity.get("repo") == local_repo:
        activity_ts = parse_timestamp_epoch(activity.get("last_push")) or 0
        local_ts = parse_timestamp_epoch(local_commit.get("latest_date")) or 0
        if local_ts and local_ts >= activity_ts:
            activity["latest_sha"] = local_commit.get("latest_sha") or activity.get("latest_sha")
            activity["latest_message"] = local_commit.get("latest_message") or activity.get("latest_message")
            activity["latest_author"] = local_commit.get("latest_author") or activity.get("latest_author")

    repo = (configured_repo or (activity or {}).get("repo") or local_repo or "").strip()
    branch = (config.get("github_branch") or (activity or {}).get("branch") or local.get("branch") or "main").strip()
    if not repo:
        raise RuntimeError("GitHub repository could not be detected from git remote origin")

    repo_info = {}
    commits = []
    today_commits = []
    pulls = []
    try:
        repo_info = github_api_json(f"/repos/{repo}")
    except Exception:
        pass
    try:
        commits = github_api_json(f"/repos/{repo}/commits?sha={quote(branch)}&per_page=5")
    except Exception:
        pass
    since = quote(iso_utc_from_local_midnight())
    try:
        today_commits = github_api_json(f"/repos/{repo}/commits?sha={quote(branch)}&since={since}&per_page=100")
    except Exception:
        pass
    try:
        pulls = github_api_json(f"/repos/{repo}/pulls?state=open&per_page=100")
    except Exception:
        pass

    latest = commits[0] if commits else {}
    latest_commit = latest.get("commit") or {}
    latest_author = (latest.get("author") or {}).get("login") or (latest_commit.get("author") or {}).get("name")
    latest_message = (latest_commit.get("message") or "").splitlines()[0] if latest_commit else ""
    display_label = (config.get("github_label") or humanize_repo_name(repo)).strip()
    contributions_today = github_contributions_today(github_user)
    repo_today_count = len(today_commits) if isinstance(today_commits, list) else 0
    activity_today_count = (activity or {}).get("today_commits")
    today_count = contributions_today
    today_label = "commits"
    if today_count is None:
        today_count = int(activity_today_count or repo_today_count or local_commit.get("today_commits") or 0)
        today_label = "commit" if today_count == 1 else "commits"
    latest_date = (latest_commit.get("committer") or latest_commit.get("author") or {}).get("date") if latest_commit else None
    return {
        "source": "github_contributions" if contributions_today is not None else ("github_activity" if activity else "github"),
        "repo": repo,
        "display_label": display_label,
        "repo_name": repo.split("/", 1)[1] if "/" in repo else repo,
        "branch": branch,
        "today_commits": int(today_count or 0),
        "today_label": today_label,
        "open_prs": len(pulls) if isinstance(pulls, list) else 0,
        "last_push": (activity or {}).get("last_push") or repo_info.get("pushed_at") or local_commit.get("latest_date"),
        "latest_sha": (activity or {}).get("latest_sha") or (latest.get("sha") or "")[:7] or local_commit.get("latest_sha"),
        "latest_message": (activity or {}).get("latest_message") or latest_message or local_commit.get("latest_message") or "--",
        "latest_author": (activity or {}).get("latest_author") or latest_author or local_commit.get("latest_author") or "--",
        "latest_date": latest_date or local_commit.get("latest_date"),
        "local_dirty": bool(local.get("dirty")),
        "local_ahead": int(local.get("ahead") or 0),
        "local_behind": int(local.get("behind") or 0),
    }


def should_refresh_github(last_fetch_time, interval_minutes):
    interval_seconds = max(60, int(interval_minutes * 60))
    return not last_fetch_time or (time.time() - last_fetch_time) >= interval_seconds


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


def protobuf_timestamps(fields):
    values = []
    if not fields:
        return values
    for _, wire_type, value, child in fields:
        if wire_type == 0:
            number = int(value)
            if 1_500_000_000 <= number <= 2_200_000_000:
                values.append(number)
            elif 1_500_000_000_000 <= number <= 2_200_000_000_000:
                values.append(number // 1000)
        if child:
            values.extend(protobuf_timestamps(child))
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
            timestamps = protobuf_timestamps(child)
            reset_ts = timestamps[0] if timestamps else None
            if "Five Hour Limit Remaining" in child_strings or "5h" in child_strings:
                group["five_hour_remaining"] = remaining
                if reset_ts:
                    group["five_hour_reset"] = reset_ts
            if "Weekly Limit Remaining" in child_strings or "weekly" in child_strings:
                group["weekly_remaining"] = remaining
                if reset_ts:
                    group["weekly_reset"] = reset_ts

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


def mono_font(size, bold=False):
    names = [
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/lucon.ttf",
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return font(size, bold)


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


def format_update_time(timestamp):
    if not timestamp:
        return "--"
    return datetime.fromtimestamp(timestamp).strftime("%I:%M %p").lstrip("0")


def format_reset_label(reset_ts):
    if not reset_ts:
        return "Reset --"
    try:
        return f"Reset {datetime.fromtimestamp(int(reset_ts)).strftime('%I:%M %p').lstrip('0')}"
    except Exception:
        return "Reset --"


def format_reset_time(reset_ts, include_day=False):
    if not reset_ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(reset_ts))
    except Exception:
        return None
    time_str = dt.strftime("%I:%M %p").lstrip("0")
    if include_day:
        return f"{dt.strftime('%a')} {time_str}"
    return time_str


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
    elif "github" in logo_path.name.lower():
        pixels = logo.load()
        w, h = logo.size
        seen = set()
        stack = []
        for x in range(w):
            stack.append((x, 0))
            stack.append((x, h - 1))
        for y in range(h):
            stack.append((0, y))
            stack.append((w - 1, y))
        while stack:
            x, y = stack.pop()
            if (x, y) in seen or not (0 <= x < w and 0 <= y < h):
                continue
            r, g, b, a = pixels[x, y]
            if a == 0 or not (r > 235 and g > 235 and b > 235):
                continue
            seen.add((x, y))
            pixels[x, y] = (255, 255, 255, 0)
            stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
        converted = []
        for r, g, b, a in logo.getdata():
            if a == 0:
                converted.append((r, g, b, a))
            elif r < 80 and g < 80 and b < 80:
                converted.append((248, 250, 252, a))
            else:
                converted.append((5, 9, 14, a))
        logo.putdata(converted)

    alpha_box = logo.getchannel("A").getbbox()
    if alpha_box:
        logo = logo.crop(alpha_box)
    logo.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return logo


def draw_centered_logo(img, logo_path_str, y, max_size):
    logo_path = resolve_asset_path(logo_path_str)
    logo = load_logo(logo_path, max_size)
    if logo is None:
        return False
    x = int((240 - logo.width) / 2)
    img.alpha_composite(logo, (x, y))
    return True


def fit_text_middle(draw, text, fnt, max_width):
    if not text:
        return ""
    if draw.textbbox((0, 0), text, font=fnt)[2] <= max_width:
        return text
    if "@" in text:
        local, domain = text.split("@", 1)
        for left_count in range(max(3, min(len(local), 14)), 2, -1):
            candidate = f"{local[:left_count]}...@{domain}"
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                return candidate
    for keep in range(max(4, len(text) - 1), 3, -1):
        left = max(2, keep // 2)
        right = max(2, keep - left)
        candidate = f"{text[:left]}...{text[-right:]}"
        if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
            return candidate
    return "..."


def fit_text_end(draw, text, fnt, max_width):
    text = str(text or "")
    if draw.textbbox((0, 0), text, font=fnt)[2] <= max_width:
        return text
    ellipsis = "..."
    for keep in range(len(text) - 1, 1, -1):
        candidate = f"{text[:keep]}{ellipsis}"
        if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
            return candidate
    return ellipsis


def draw_centered_text_fit(draw, text, y, fnt, fill, max_width=208):
    fitted = fit_text_middle(draw, text, fnt, max_width)
    box = draw.textbbox((0, 0), fitted, font=fnt)
    draw.text(((240 - (box[2] - box[0])) // 2, y), fitted, fill=fill, font=fnt)


def wrap_text_lines(draw, text, fnt, max_width, max_lines=2):
    words = str(text or "").split()
    if not words:
        return ["--"]
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(lines) == max_lines and words:
        lines[-1] = fit_text_middle(draw, lines[-1], fnt, max_width)
    return lines


def wrap_commit_message(draw, text, fnt, max_width, max_lines=2):
    words = str(text or "").strip().split()
    if not words:
        return ["--"]

    lines = []
    current = ""
    used_all_words = True
    for index, word in enumerate(words):
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = word
        else:
            lines.append(fit_text_end(draw, word, fnt, max_width))
            current = ""

        if len(lines) >= max_lines:
            used_all_words = index >= len(words)
            break

    if current and len(lines) < max_lines:
        lines.append(current)
    elif current:
        used_all_words = False

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        used_all_words = False

    if not used_all_words and lines:
        base = lines[-1].rstrip(".")
        ellipsis = "..."
        for keep in range(len(base), 0, -1):
            candidate = f"{base[:keep].rstrip()}{ellipsis}"
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                lines[-1] = candidate
                break
        else:
            lines[-1] = fit_text_end(draw, lines[-1], fnt, max_width)

    return [fit_text_end(draw, line, fnt, max_width) for line in lines[:max_lines]]


def draw_progress_bar(draw, x, y, w, h, percent, accent_color, track_color=(25, 30, 40)):
    if percent is None:
        return
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h//2, fill=track_color)
    if percent > 0:
        fill_w = min(w, max(h, int(w * percent / 100)))
        draw.rounded_rectangle((x, y, x + fill_w, y + h), radius=h//2, fill=accent_color)


def draw_segmented_progress_bar(draw, x, y, w, h, percent, fill_color, track_color=(18, 36, 51)):
    if percent is None:
        return
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=track_color)
    if percent <= 0:
        return
    fill_w = min(w, max(h, int(w * percent / 100)))
    step = 8
    seg_w = 5
    for sx in range(x, x + fill_w, step):
        ex = min(sx + seg_w, x + fill_w)
        draw.rounded_rectangle((sx, y, ex, y + h), radius=2, fill=fill_color)


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


def draw_centered_footer(draw, y, text, is_stale, text_color=None):
    muted = text_color if text_color else (150, 160, 180)
    amber = (255, 193, 7)
    fnt = font(16)
    color = amber if is_stale else muted
    footer_text = f"STALE - {text}" if is_stale else text
    box = draw.textbbox((0, 0), footer_text, font=fnt)
    draw.text(((240 - (box[2] - box[0])) // 2, y), footer_text, fill=color, font=fnt)


def format_codex_footer(last_success_ts, account_label=None):
    footer = format_update_time(last_success_ts)
    label = codex_account_footer_label(account_label)
    if label:
        return f"{footer} / {label}"
    return footer


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


def draw_hollow_status_dot(draw, color=(126, 135, 148)):
    draw.ellipse((214, 16, 224, 26), outline=color, width=2)


def draw_row(draw, y, label, percent, accent_color, track_color=(25, 30, 40), label_color=(150, 160, 180), val_color=(240, 240, 240), reset_text=None):
    fnt_label = font(18, True)
    draw.text((16, y), label, fill=label_color, font=fnt_label)
    if reset_text:
        label_box = draw.textbbox((0, 0), label, font=fnt_label)
        draw.text((20 + (label_box[2] - label_box[0]), y), f"({reset_text})", fill=label_color, font=font(18))
    val_str = f"{percent:.0f}%" if percent is not None else "--"
    fnt_val = font(22, True)
    box = draw.textbbox((0, 0), val_str, font=fnt_val)
    draw.text((224 - (box[2] - box[0]), y - 4), val_str, fill=val_color, font=fnt_val)
    draw_progress_bar(draw, 16, y + 26, 208, 12, percent, accent_color, track_color)


def render_codex_cached_screen(data, last_success_ts, output_path, account_label=None):
    used_p = clamp_percent(data.get("primary_percent", 0))
    used_w = clamp_percent(data.get("weekly_percent", 0))

    bg_color = (3, 4, 6, 255)
    border_color = (68, 72, 82)
    track_color = (18, 20, 24)
    accent = (126, 132, 142)
    text_color = (245, 247, 250)
    sub_color = (185, 190, 199)
    footer_color = (132, 138, 148)

    img = Image.new("RGBA", (240, 240), bg_color)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=border_color, width=1)
    draw_hollow_status_dot(d, accent)

    logo = load_logo(resolve_asset_path(LOGO_NAME), 38)
    if logo is not None:
        logo_layer = Image.new("RGBA", logo.size, (255, 255, 255, 0))
        logo_layer.alpha_composite(logo)
        alpha = logo_layer.getchannel("A").point(lambda a: int(a * 0.55))
        logo_layer.putalpha(alpha)
        img.alpha_composite(logo_layer, ((240 - logo.width) // 2, 16))

    draw_centered_text_fit(d, "CODEX", 58, font(14, True), text_color, 208)
    draw_centered_text_fit(d, "LAST KNOWN", 78, font(11, True), (164, 169, 178), 208)

    draw_row(d, 110, "5H", used_p, accent, track_color, sub_color, text_color)
    draw_row(d, 160, "W", used_w, accent, track_color, sub_color, text_color)

    age = format_age(last_success_ts)
    account_short = codex_account_footer_label(account_label)
    footer = f"Updated {age}" if age else "Updated --"
    if account_short:
        footer = f"{footer} / {account_short}"
    draw_centered_text_fit(d, footer, 208, font(13), footer_color, 208)
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_codex_screen(data, freshness_state, last_success_ts, output_path, alert_threshold=80.0, account_label=None):
    if freshness_state == "cached":
        render_codex_cached_screen(data, last_success_ts, output_path, account_label)
        return

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
    
    primary_reset = "done" if data.get("primary_reset_elapsed") else format_reset_time(data.get("primary_reset"))
    weekly_reset = "done" if data.get("weekly_reset_elapsed") else format_reset_time(data.get("weekly_reset"), include_day=True)
    
    draw_row(d, 80, "5H", used_p, accent, track_color, lbl_color, val_p_color, primary_reset)
    draw_row(d, 148, "W", used_w, accent, track_color, lbl_color, val_w_color, weekly_reset)

    draw_centered_footer(d, 211, format_codex_footer(last_success_ts, account_label), is_stale, footer_color)
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_antigravity_usage_screen(
    logo_name,
    model_label,
    five_hour_rem,
    weekly_rem,
    freshness_state,
    last_success_ts,
    output_path,
    alert_threshold=80.0,
    five_hour_reset=None,
    weekly_reset=None,
):
    if freshness_state == "cached":
        render_antigravity_cached_screen(
            logo_name,
            model_label,
            five_hour_rem,
            weekly_rem,
            last_success_ts,
            output_path,
            five_hour_reset,
            weekly_reset,
        )
        return

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
    
    five_reset = format_reset_time(five_hour_reset)
    week_reset = format_reset_time(weekly_reset, include_day=True)

    draw_row(d, 105, "5H", five_used, accent, track_color, lbl_color, val_5h_color, five_reset)
    draw_row(d, 160, "W", w_used, accent, track_color, lbl_color, val_w_color, week_reset)
    draw_centered_footer(d, 208, format_update_time(last_success_ts), is_stale, footer_color)
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_antigravity_cached_screen(
    logo_name,
    model_label,
    five_hour_rem,
    weekly_rem,
    last_success_ts,
    output_path,
    five_hour_reset=None,
    weekly_reset=None,
):
    five_used = clamp_percent(100.0 - five_hour_rem) if five_hour_rem is not None else None
    w_used = clamp_percent(100.0 - weekly_rem) if weekly_rem is not None else None

    bg_color = (3, 4, 6, 255)
    border_color = (68, 72, 82)
    track_color = (18, 20, 24)
    five_color = (176, 181, 190)
    weekly_color = (126, 132, 142)
    text_color = (245, 247, 250)
    sub_color = (185, 190, 199)
    footer_color = (132, 138, 148)

    img = Image.new("RGBA", (240, 240), bg_color)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=border_color, width=1)
    draw_hollow_status_dot(d, (126, 132, 142))

    logo = load_logo(resolve_asset_path(logo_name), 38)
    if logo is not None:
        logo_layer = Image.new("RGBA", logo.size, (255, 255, 255, 0))
        logo_layer.alpha_composite(logo)
        alpha = logo_layer.getchannel("A").point(lambda a: int(a * 0.55))
        logo_layer.putalpha(alpha)
        img.alpha_composite(logo_layer, ((240 - logo.width) // 2, 16))

    model_font = font(14, True)
    tag_font = font(11, True)
    model_box = d.textbbox((0, 0), model_label, font=model_font)
    d.text(((240 - (model_box[2] - model_box[0])) // 2, 58), model_label, fill=text_color, font=model_font)
    tag = "LAST KNOWN"
    tag_box = d.textbbox((0, 0), tag, font=tag_font)
    d.text(((240 - (tag_box[2] - tag_box[0])) // 2, 78), tag, fill=(164, 169, 178), font=tag_font)

    def row(y, label, percent, color, reset_text=None):
        label_font = font(15, True)
        reset_font = font(15)
        value_font = font(18, True)
        d.text((16, y), label, fill=sub_color, font=label_font)
        if reset_text:
            label_box = d.textbbox((0, 0), label, font=label_font)
            d.text((20 + (label_box[2] - label_box[0]), y), f"({reset_text})", fill=sub_color, font=reset_font)
        value = f"{percent:.0f}%" if percent is not None else "--"
        value_box = d.textbbox((0, 0), value, font=value_font)
        d.text((224 - (value_box[2] - value_box[0]), y - 3), value, fill=text_color, font=value_font)
        draw_segmented_progress_bar(d, 16, y + 24, 208, 12, percent, color, track_color)

    row(110, "5H", five_used, five_color, format_reset_time(five_hour_reset))
    row(160, "W", w_used, weekly_color, format_reset_time(weekly_reset, include_day=True))

    age = format_age(last_success_ts)
    footer = f"Updated {age}" if age else "Updated --"
    d.text((16, 208), footer, fill=footer_color, font=font(13))
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_splash_screen(logo_name, output_path=None, subtitle=None):
    bg_color = (10, 12, 18, 255)
    border_color = (30, 40, 60)
    
    img = Image.new("RGBA", (240, 240), bg_color)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=border_color, width=1)
    
    logo = load_logo(resolve_asset_path(logo_name), 96)
    if logo is None:
        logo = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    
    lx = (240 - logo.width) // 2
    ly = (240 - logo.height) // 2
    if subtitle:
        ly = max(54, ly - 18)
    img.alpha_composite(logo, (lx, ly))

    if subtitle:
        draw_centered_text_fit(d, subtitle, ly + logo.height + 13, font(16, True), (230, 235, 245), 214)
    
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_logo_only_screen(logo_name, output_path, max_size=166):
    img = Image.new("RGBA", (240, 240), (2, 6, 16, 255))
    logo = load_logo(resolve_asset_path(logo_name), max_size)
    if logo is not None:
        x = (240 - logo.width) // 2
        y = (240 - logo.height) // 2
        img.alpha_composite(logo, (x, y))
    else:
        d = ImageDraw.Draw(img)
        text = "ANTI GRAVITY"
        fnt = font(20, True)
        box = d.textbbox((0, 0), text, font=fnt)
        d.text(((240 - (box[2] - box[0])) // 2, 106), text, fill=(242, 247, 255), font=fnt)
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_antigravity_offline_screen(logo_name, last_seen_ts, output_path):
    img = Image.new("RGBA", (240, 240), (10, 12, 18, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 236, 236), radius=12, outline=(70, 82, 110), width=1)

    logo = load_logo(resolve_asset_path(logo_name), 58)
    if logo is not None:
        x = (240 - logo.width) // 2
        img.alpha_composite(logo, (x, 46))

    title = "Open Antigravity IDE"
    title_font = font(14, True)

    title_box = d.textbbox((0, 0), title, font=title_font)
    d.text(((240 - (title_box[2] - title_box[0])) // 2, 143), title, fill=(242, 247, 255), font=title_font)

    img.convert("RGB").save(output_path, "JPEG", quality=92)


def format_short_age(timestamp_or_iso):
    ts = parse_timestamp_epoch(timestamp_or_iso)
    if not ts:
        return "--"
    diff = max(0, int(time.time() - ts))
    if diff < 60:
        return "now"
    mins = diff // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    return datetime.fromtimestamp(ts).strftime("%b %-d" if os.name != "nt" else "%b %#d")


def draw_github_mark(draw, x, y, size=28):
    draw.ellipse((x, y, x + size, y + size), fill=(244, 247, 251))
    fnt = font(12, True)
    text = "GH"
    box = draw.textbbox((0, 0), text, font=fnt)
    draw.text(
        (x + (size - (box[2] - box[0])) // 2, y + (size - (box[3] - box[1])) // 2 - 1),
        text,
        fill=(7, 12, 18),
        font=fnt,
    )


def draw_git_branch_icon(draw, x, y, color):
    draw.line((x + 4, y + 3, x + 4, y + 18, x + 18, y + 18), fill=color, width=2)
    draw.ellipse((x, y, x + 8, y + 8), outline=color, width=2)
    draw.ellipse((x, y + 14, x + 8, y + 22), outline=color, width=2)
    draw.ellipse((x + 14, y + 14, x + 22, y + 22), outline=color, width=2)


def draw_github_metric(draw, xy, label, value, accent):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=8, outline=(31, 55, 78), width=1, fill=(7, 13, 20))
    draw.text((x1 + 10, y1 + 8), label, fill=(162, 174, 194), font=font(11, True))
    draw.text((x1 + 10, y1 + 21), value, fill=(244, 247, 251), font=font(21, True))
    draw.rectangle((x1 + 8, y2 - 4, x2 - 8, y2 - 2), fill=accent)


def draw_github_activity_card(draw, xy, kind, label, value, accent, text_color, sub_color):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=8, outline=(17, 73, 128), width=1, fill=(6, 15, 27))
    if kind == "branch":
        draw_git_branch_icon(draw, x1 + 8, y1 + 15, accent)
    else:
        cx, cy = x1 + 18, y1 + 25
        draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline=accent, width=2)
        draw.line((cx, cy - 5, cx, cy + 2, cx + 5, cy + 2), fill=accent, width=2)
    label_font = font(12, True)
    value_font = font(12, True)
    text_x = x1 + 30
    d_value = fit_text_end(draw, value, value_font, x2 - text_x - 7)
    draw.text((text_x, y1 + 10), label, fill=sub_color, font=label_font)
    draw.text((text_x, y1 + 28), d_value, fill=text_color, font=value_font)


def render_github_screen(data, freshness_state, last_success_ts, output_path):
    is_offline = freshness_state == "offline"
    is_cached = freshness_state == "cached"
    is_stale = freshness_state == "stale"
    has_activity = bool(data and (data.get("latest_message") or data.get("latest_sha") or data.get("last_push")))
    bg_color = (7, 17, 31, 255) if not is_offline else (3, 4, 6, 255)
    border_color = (20, 200, 244) if not is_offline else (68, 72, 82)
    text_color = (244, 247, 251)
    sub_color = (145, 160, 184)
    muted = (124, 136, 154)
    cyan = (20, 200, 244)
    divider_color = (39, 65, 94)

    img = Image.new("RGBA", (240, 240), bg_color)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((5, 5, 235, 235), radius=12, outline=border_color, width=1)
    if is_cached:
        draw_hollow_status_dot(d, muted)
    elif is_stale:
        draw_status_dot(d, False, True)
    else:
        draw_status_dot(d, is_offline, False)

    if is_offline and not has_activity:
        logo = load_logo(resolve_asset_path(GITHUB_LOGO_NAME), 54)
        if logo is not None:
            img.alpha_composite(logo, ((240 - logo.width) // 2, 54))
        title = "GitHub unavailable"
        detail = "No recent activity"
        title_font = font(16, True)
        detail_font = font(13)
        title_box = d.textbbox((0, 0), title, font=title_font)
        detail_box = d.textbbox((0, 0), detail, font=detail_font)
        d.text(((240 - (title_box[2] - title_box[0])) // 2, 123), title, fill=text_color, font=title_font)
        d.text(((240 - (detail_box[2] - detail_box[0])) // 2, 149), detail, fill=sub_color, font=detail_font)
        img.convert("RGB").save(output_path, "JPEG", quality=92)
        return

    today = int(data.get("today_commits") or 0)
    if today == 0:
        logo = load_logo(resolve_asset_path(GITHUB_LOGO_NAME), 58)
        if logo is not None:
            img.alpha_composite(logo, ((240 - logo.width) // 2, 44))

        title = "No activity today"
        title_font = font(25, True)
        title_box = d.textbbox((0, 0), title, font=title_font)
        d.text(((240 - (title_box[2] - title_box[0])) // 2, 111), title, fill=text_color, font=title_font)

        detail = "One commit is enough."
        detail_font = font(17)
        detail_box = d.textbbox((0, 0), detail, font=detail_font)
        d.text(((240 - (detail_box[2] - detail_box[0])) // 2, 148), detail, fill=sub_color, font=detail_font)

        updated_font = font(14)
        updated_text = f"Updated {format_age(last_success_ts) if last_success_ts else '--'}"
        updated_box = d.textbbox((0, 0), updated_text, font=updated_font)
        d.text(((240 - (updated_box[2] - updated_box[0])) // 2, 213), updated_text, fill=sub_color, font=updated_font)
        img.convert("RGB").save(output_path, "JPEG", quality=92)
        return

    logo = load_logo(resolve_asset_path(GITHUB_LOGO_NAME), 32)
    if logo is not None:
        img.alpha_composite(logo, (17, 16))

    repo_label = data.get("display_label") or humanize_repo_name(data.get("repo") or data.get("repo_name") or "")
    branch = data.get("branch") or "main"
    repo_font = font(20, True)
    branch_font = font(13)
    d.text((58, 16), fit_text_middle(d, repo_label, repo_font, 144), fill=text_color, font=repo_font)
    branch_icon = load_logo(resolve_asset_path(GIT_BRANCH_ICON_NAME), 20)
    if branch_icon is not None:
        img.alpha_composite(branch_icon, (60, 42))
    d.text((88, 44), fit_text_middle(d, branch, branch_font, 112), fill=sub_color, font=branch_font)

    today_unit = (data.get("today_label") or "").strip()
    if not today_unit:
        today_unit = "commit" if today == 1 else "commits"
    if today_unit == "contributions":
        today_unit = "contrib" if today == 1 else "contribs"
    last_age = format_short_age(data.get("last_push"))
    last_text = "now" if last_age == "now" else f"{last_age} ago"

    stat_label_font = font(14, True)
    stat_value_font = font(17, True)
    stat_y = 76
    left_x = 18
    right_x = 138
    value_y = stat_y + 22
    left_value = fit_text_end(d, f"{today} {today_unit}", stat_value_font, 98)
    right_value = fit_text_end(d, last_text, stat_value_font, 84)
    d.text((left_x, stat_y), "Today:", fill=sub_color, font=stat_label_font)
    d.text((left_x, value_y), left_value, fill=text_color, font=stat_value_font)
    d.line((121, stat_y - 1, 121, 115), fill=divider_color, width=1)
    d.text((right_x, stat_y), "Last:", fill=sub_color, font=stat_label_font)
    d.text((right_x, value_y), right_value, fill=text_color, font=stat_value_font)

    d.line((18, 123, 222, 123), fill=divider_color, width=1)

    message_font = font(19, True)
    lines = wrap_commit_message(d, data.get("latest_message") or "--", message_font, 204, 2)
    y = 138
    for line in lines:
        d.text((18, y), line, fill=text_color, font=message_font)
        y += 23

    sha = str(data.get("latest_sha") or "-------")[:7]
    chip_y = max(200, min(205, y + 12))
    chip_font = mono_font(16, True)
    chip_box = d.textbbox((0, 0), sha, font=chip_font)
    chip_w = min(104, chip_box[2] - chip_box[0] + 28)
    chip_h = 25
    d.rounded_rectangle((18, chip_y, 18 + chip_w, chip_y + chip_h), radius=8, fill=(7, 17, 31), outline=cyan, width=2)
    fitted_sha = fit_text_end(d, sha, chip_font, chip_w - 18)
    fitted_box = d.textbbox((0, 0), fitted_sha, font=chip_font)
    fitted_w = fitted_box[2] - fitted_box[0]
    fitted_h = fitted_box[3] - fitted_box[1]
    fitted_x = 18 + (chip_w - fitted_w) // 2
    fitted_y = chip_y + (chip_h - fitted_h) // 2 - fitted_box[1] - 1
    d.text((fitted_x, fitted_y), fitted_sha, fill=cyan, font=chip_font)

    updated_font = font(12)
    updated_text = f"Updated {format_age(last_success_ts) if last_success_ts else '--'}"
    updated_box = d.textbbox((0, 0), updated_text, font=updated_font)
    d.text((222 - (updated_box[2] - updated_box[0]), 215), updated_text, fill=sub_color, font=updated_font)
    img.convert("RGB").save(output_path, "JPEG", quality=92)


def render_offline_screen(logo_name, provider_name, last_seen_ts, output_path):
    if "Anti" in provider_name:
        render_antigravity_offline_screen(logo_name, last_seen_ts, output_path)
        return

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


def update_live_preview(source_path):
    try:
        import shutil
        shutil.copy2(str(source_path), str(LIVE_PREVIEW_PATH))
    except Exception as exc:
        print(f"warning: live preview update failed: {exc}")


def run_screen(
    clock_ip,
    output_path,
    configure,
    screen_name,
    data_state,
    show_splash=True,
    alert_threshold=80.0,
    theme_id=None,
    splash_duration=1.5,
):
    summary = ""
    dash_temp_path = output_path.with_name("temp_" + output_path.name)
    
    if not theme_id or theme_id == "default":
        theme_id = load_config().get("selected_theme", "default")

    # 1. Pre-render the main dashboard image FIRST so there is zero render delay after splash
    if data_state["ag"]["state"] == "cached" and screen_name in ("ag_gemini", "ag_claude"):
        group_name = "gemini" if screen_name == "ag_gemini" else "claude_gpt"
        model_label = "GEMINI" if screen_name == "ag_gemini" else "CLAUDE/GPT"
        data = (data_state["ag"]["data"] or {}).get("groups", {}).get(group_name, {})
        render_antigravity_usage_screen(
            ANTIGRAVITY_LOGO_NAME,
            model_label,
            data.get("five_hour_remaining"),
            data.get("weekly_remaining"),
            data_state["ag"]["state"],
            data_state["ag"]["time"],
            dash_temp_path,
            alert_threshold,
            data.get("five_hour_reset"),
            data.get("weekly_reset"),
        )
        summary = f"{screen_name} cached"
    elif screen_name == "github":
        render_github_screen(
            data_state["github"]["data"] or {},
            data_state["github"]["state"],
            data_state["github"]["time"],
            dash_temp_path,
        )
        summary = "github cached" if data_state["github"]["state"] == "cached" else "github"
    elif screen_name == "github_offline":
        fallback = data_state["github"]["data"] or {
            "display_label": data_state.get("github_label") or "GitHub",
            "repo_name": "Repo",
            "branch": "main",
            "today_commits": 0,
            "open_prs": 0,
            "latest_message": "GitHub unavailable",
        }
        render_github_screen(fallback, "offline", data_state["github"]["time"], dash_temp_path)
        summary = "github offline"
    elif theme_id and theme_id != "default":
        if screen_name == "codex":
            codex_data = data_state["codex"]["data"] or {}
            used_p = codex_data.get("primary_percent")
            used_w = codex_data.get("weekly_percent")
            reset_ts = codex_data.get("primary_reset")
            reset_str = "Reset --"
            if codex_data.get("primary_reset_elapsed"):
                reset_str = "Reset done"
            elif reset_ts:
                try:
                    reset_str = f"Reset {datetime.fromtimestamp(int(reset_ts)).strftime('%I:%M %p').lstrip('0')}"
                except Exception:
                    pass
            render_custom_theme(
                theme_id=theme_id,
                logo_path=resolve_asset_path(LOGO_NAME),
                model_label="CODEX",
                used_p=used_p,
                used_w=used_w,
                is_offline=(data_state["codex"]["state"] == "offline"),
                is_stale=(data_state["codex"]["state"] in ("stale", "cached")),
                last_success_ts=data_state["codex"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str=reset_str
            )
            summary = f"codex {'cached ' if data_state['codex']['state'] == 'cached' else ''}[{theme_id}]"
        elif screen_name == "codex_offline":
            render_custom_theme(
                theme_id=theme_id,
                logo_path=resolve_asset_path(LOGO_NAME),
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
            reset_str = format_reset_label(data.get("five_hour_reset"))
            render_custom_theme(
                theme_id=theme_id,
                logo_path=resolve_asset_path(ANTIGRAVITY_LOGO_NAME),
                model_label="GEMINI",
                used_p=used_p,
                used_w=used_w,
                is_offline=(data_state["ag"]["state"] == "offline"),
                is_stale=(data_state["ag"]["state"] == "stale"),
                last_success_ts=data_state["ag"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str=reset_str
            )
            summary = f"ag_gemini [{theme_id}]"
        elif screen_name == "ag_claude":
            data = (data_state["ag"]["data"] or {}).get("groups", {}).get("claude_gpt", {})
            rem_p = data.get("five_hour_remaining")
            rem_w = data.get("weekly_remaining")
            used_p = (100.0 - rem_p) if rem_p is not None else None
            used_w = (100.0 - rem_w) if rem_w is not None else None
            reset_str = format_reset_label(data.get("five_hour_reset"))
            render_custom_theme(
                theme_id=theme_id,
                logo_path=resolve_asset_path(ANTIGRAVITY_LOGO_NAME),
                model_label="CLAUDE/GPT",
                used_p=used_p,
                used_w=used_w,
                is_offline=(data_state["ag"]["state"] == "offline"),
                is_stale=(data_state["ag"]["state"] == "stale"),
                last_success_ts=data_state["ag"]["time"],
                output_path=dash_temp_path,
                alert_threshold=alert_threshold,
                reset_str=reset_str
            )
            summary = f"ag_claude [{theme_id}]"
        elif screen_name == "ag_offline":
            render_antigravity_offline_screen(ANTIGRAVITY_LOGO_NAME, data_state["ag"]["time"], dash_temp_path)
            summary = f"ag offline [{theme_id}]"
    else:
        if screen_name == "codex":
            render_codex_screen(
                data_state["codex"]["data"],
                data_state["codex"]["state"],
                data_state["codex"]["time"],
                dash_temp_path,
                alert_threshold,
                data_state.get("codex_account_name"),
            )
            summary = "codex cached" if data_state["codex"]["state"] == "cached" else "codex"
        elif screen_name == "codex_offline":
            render_offline_screen(LOGO_NAME, "Codex", data_state["codex"]["time"], dash_temp_path)
            summary = "codex offline"
        elif screen_name == "ag_gemini":
            data = data_state["ag"]["data"]["groups"]["gemini"]
            render_antigravity_usage_screen(ANTIGRAVITY_LOGO_NAME, "GEMINI", data.get("five_hour_remaining"), data.get("weekly_remaining"), data_state["ag"]["state"], data_state["ag"]["time"], dash_temp_path, alert_threshold, data.get("five_hour_reset"), data.get("weekly_reset"))
            summary = "ag_gemini"
        elif screen_name == "ag_claude":
            data = data_state["ag"]["data"]["groups"]["claude_gpt"]
            render_antigravity_usage_screen(ANTIGRAVITY_LOGO_NAME, "CLAUDE/GPT", data.get("five_hour_remaining"), data.get("weekly_remaining"), data_state["ag"]["state"], data_state["ag"]["time"], dash_temp_path, alert_threshold, data.get("five_hour_reset"), data.get("weekly_reset"))
            summary = "ag_claude"
        elif screen_name == "ag_offline":
            render_offline_screen(ANTIGRAVITY_LOGO_NAME, "Anti Gravity", data_state["ag"]["time"], dash_temp_path)
            summary = "ag offline"

    # 2. Show splash transition if enabled (targeting exact ~0.5s screen visible duration)
    if show_splash:
        splash_shown = False
        if screen_name == "codex" and data_state["codex"]["data"]:
            render_splash_screen(LOGO_NAME, output_path, data_state.get("codex_account_name"))
            splash_shown = True
        elif screen_name in ("ag_gemini", "ag_claude") and data_state["ag"]["data"]:
            render_splash_screen(ANTIGRAVITY_LOGO_NAME, output_path)
            splash_shown = True
        elif screen_name == "github" and data_state["github"]["data"]:
            render_splash_screen(GITHUB_LOGO_NAME, output_path)
            splash_shown = True
            
        if splash_shown:
            update_live_preview(output_path)
            try:
                upload_file(clock_ip, output_path)
                trigger_clock_refresh(clock_ip)
            except Exception as exc:
                print(f"warning: splash upload failed: {exc}")
            time.sleep(max(0.5, float(splash_duration)))

    # 3. Upload the pre-rendered main dashboard image immediately
    status = "preview-only"
    update_live_preview(dash_temp_path)
    try:
        status, _ = upload_file(clock_ip, dash_temp_path, filename=OUTPUT_NAME)
        trigger_clock_refresh(clock_ip)
    except Exception as exc:
        print(f"warning: dashboard upload failed: {exc}")
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


def page_display_seconds(page_name, config, fallback_interval):
    if page_name == "ag_offline":
        return ANTIGRAVITY_OFFLINE_SECONDS
    if page_name.startswith("github"):
        return max(5, int(config.get("github_display_seconds", fallback_interval)))
    if page_name.startswith("ag_"):
        return max(5, int(config.get("antigravity_display_seconds", fallback_interval)))
    return max(5, int(config.get("codex_display_seconds", fallback_interval)))


def main():
    parser = argparse.ArgumentParser(description="Show local limit meter on Smart Weather Clock.")
    parser.add_argument("--clock-ip", default=os.getenv("CODEX_CLOCK_IP", DEFAULT_CLOCK_IP))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--screens", default="codex,antigravity", help="(Ignored) Handled dynamically.")
    parser.add_argument("--ag-model", default="auto", choices=["auto", "gemini", "claude", "both"], help="Anti Gravity model to display: auto (active model), gemini, claude, or both.")
    parser.add_argument("--no-splash", action="store_true", help="Disable the 1-second logo splash transition between screens.")
    parser.add_argument("--loop", type=int, default=30, help="Refresh interval in seconds. 0 means run once.")
    parser.add_argument("--no-configure", action="store_true", help="Upload only; do not switch the clock to Photo mode.")
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    previous_state = load_runtime_state()
    
    last_codex_data = load_cached_codex_data(previous_state)
    last_codex_time = float((previous_state.get("codex") or {}).get("last_time") or 0)
    last_codex_ping_time = float((previous_state.get("codex") or {}).get("last_ping_time") or 0)
    last_codex_ping_status = (previous_state.get("codex") or {}).get("last_ping_status") or "never"
    codex_account_email = (previous_state.get("codex") or {}).get("account_email") or get_codex_account_email()
    last_ag_data = load_cached_antigravity_data(previous_state)
    previous_ag_data = None
    active_ag_model = previous_state.get("active_ag_model") or "gemini"
    last_ag_time = float((previous_state.get("ag") or {}).get("last_time") or 0)
    last_github_data = load_cached_github_data(previous_state)
    last_github_time = float((previous_state.get("github") or {}).get("last_time") or 0)
    last_github_fetch_time = float((previous_state.get("github") or {}).get("last_fetch_time") or 0)
    last_github_status = (previous_state.get("github") or {}).get("last_status") or "never"
    page_index = 0
    is_first_run = True

    while True:
        # Load runtime configuration (hot-reloaded from config.json)
        config = load_config()
        active_clock_ip = config.get("clock_ip", args.clock_ip)
        ag_model_mode = config.get("ag_model_mode", args.ag_model)
        show_splash = config.get("show_splash", not args.no_splash)
        splash_duration = max(0.5, float(config.get("splash_duration_seconds", 1.5)))
        alert_threshold = float(config.get("alert_threshold", 80))
        loop_interval = max(5, int(config.get("rotation_interval", args.loop)))
        codex_ping_enabled = bool(config.get("codex_ping_enabled", True))
        codex_ping_interval = max(5, int(config.get("codex_ping_interval_minutes", 30)))
        github_enabled = bool(config.get("github_enabled", True))
        github_refresh_interval = max(1, int(config.get("github_refresh_interval_minutes", 5)))
        codex_account_email = get_codex_account_email() or codex_account_email

        try:
            codex_data = find_latest_limits()
            if codex_data:
                last_codex_data = codex_data
                last_codex_time = parse_timestamp_epoch(codex_data.get("timestamp")) or time.time()
                if str(last_codex_ping_status).startswith("error") and last_codex_time > last_codex_ping_time:
                    last_codex_ping_time = last_codex_time
                    last_codex_ping_status = "ok"
        except Exception:
            pass

        if args.loop > 0 and codex_ping_enabled and should_ping_codex(last_codex_data, last_codex_ping_time, codex_ping_interval):
            last_codex_ping_time = time.time()
            try:
                ping_codex_quota()
                last_codex_ping_status = "ok"
                codex_data = find_latest_limits()
                if codex_data:
                    last_codex_data = codex_data
                    last_codex_time = parse_timestamp_epoch(codex_data.get("timestamp")) or time.time()
            except subprocess.CalledProcessError as exc:
                last_codex_ping_time = max(0.0, time.time() - (codex_ping_interval * 60) + CODEX_PING_FAILURE_RETRY_SECONDS)
                err = (exc.stderr or exc.stdout or str(exc)).strip()
                last_codex_ping_status = f"error: {err[:240]}"
            except Exception as exc:
                last_codex_ping_time = max(0.0, time.time() - (codex_ping_interval * 60) + CODEX_PING_FAILURE_RETRY_SECONDS)
                last_codex_ping_status = f"error: {exc}"

        ag_current_ok = False
        try:
            ag_data = find_antigravity_limits()
            if ag_data:
                ag_current_ok = True
                active_ag_model = determine_active_ag_model(ag_data, previous_ag_data, active_ag_model)
                previous_ag_data = ag_data
                last_ag_data = ag_data
                last_ag_time = time.time()
        except Exception:
            pass

        if github_enabled and should_refresh_github(last_github_fetch_time, github_refresh_interval):
            last_github_fetch_time = time.time()
            try:
                github_data = find_github_status(config)
                if github_data:
                    last_github_data = github_data
                    last_github_time = time.time()
                    last_github_status = "ok"
            except Exception as exc:
                last_github_status = f"error: {str(exc)[:240]}"
            
        codex_state = get_freshness_state(last_codex_time, CODEX_STALE_LIMIT_MINUTES)
        if codex_state == "offline" and last_codex_data:
            codex_state = "cached"
        ag_state = get_freshness_state(last_ag_time, ANTIGRAVITY_STALE_LIMIT_MINUTES) if ag_current_ok else ("cached" if last_ag_data else "offline")
        github_state = get_freshness_state(last_github_time, GITHUB_STALE_LIMIT_MINUTES) if github_enabled else "offline"
        if github_state == "offline" and last_github_data:
            github_state = "cached"
        
        active_pages = []
        if not last_codex_data:
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

        if github_enabled:
            if last_github_data:
                active_pages.append("github")
            else:
                active_pages.append("github_offline")
            
        if not active_pages:
            active_pages = ["codex_offline"]
            
        current_page = active_pages[page_index % len(active_pages)]
        page_index = (page_index + 1) % len(active_pages)
        
        data_state = {
            "codex": {"data": last_codex_data, "time": last_codex_time, "state": codex_state},
            "ag": {"data": last_ag_data, "time": last_ag_time, "state": ag_state},
            "github": {"data": last_github_data, "time": last_github_time, "state": github_state},
            "github_label": config.get("github_label") or "GitHub",
            "codex_account_email": codex_account_email,
            "codex_account_name": codex_account_display_name(codex_account_email),
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
                "weekly_reset": last_codex_data.get("weekly_reset") if last_codex_data else None,
                "last_time": last_codex_time,
                "last_event_time": parse_timestamp_epoch(last_codex_data.get("timestamp")) if last_codex_data else 0,
                "last_ping_time": last_codex_ping_time,
                "last_ping_status": last_codex_ping_status,
                "account_email": codex_account_email,
            },
            "ag": {
                "state": ag_state,
                "last_time": last_ag_time,
                "gemini": {
                    "five_hour_remaining": ag_gem.get("five_hour_remaining"),
                    "weekly_remaining": ag_gem.get("weekly_remaining"),
                    "five_hour_reset": ag_gem.get("five_hour_reset"),
                    "weekly_reset": ag_gem.get("weekly_reset"),
                },
                "claude_gpt": {
                    "five_hour_remaining": ag_claude.get("five_hour_remaining"),
                    "weekly_remaining": ag_claude.get("weekly_remaining"),
                    "five_hour_reset": ag_claude.get("five_hour_reset"),
                    "weekly_reset": ag_claude.get("weekly_reset"),
                },
            },
            "github": {
                "state": github_state,
                "last_time": last_github_time,
                "last_fetch_time": last_github_fetch_time,
                "last_status": last_github_status,
                "repo": (last_github_data or {}).get("repo"),
                "display_label": (last_github_data or {}).get("display_label"),
                "repo_name": (last_github_data or {}).get("repo_name"),
                "branch": (last_github_data or {}).get("branch"),
                "today_commits": (last_github_data or {}).get("today_commits"),
                "today_label": (last_github_data or {}).get("today_label"),
                "open_prs": (last_github_data or {}).get("open_prs"),
                "last_push": (last_github_data or {}).get("last_push"),
                "latest_sha": (last_github_data or {}).get("latest_sha"),
                "latest_message": (last_github_data or {}).get("latest_message"),
                "latest_author": (last_github_data or {}).get("latest_author"),
                "latest_date": (last_github_data or {}).get("latest_date"),
                "local_dirty": (last_github_data or {}).get("local_dirty"),
                "local_ahead": (last_github_data or {}).get("local_ahead"),
                "local_behind": (last_github_data or {}).get("local_behind"),
            },
            "config": config,
        })
        
        try:
            should_configure = not args.no_configure and is_first_run
            run_screen(
                active_clock_ip,
                output_path,
                should_configure,
                current_page,
                data_state,
                show_splash=show_splash,
                alert_threshold=alert_threshold,
                splash_duration=splash_duration,
            )
            is_first_run = False
        except Exception as exc:
            print(f"error: {exc}")
            
        if args.loop <= 0:
            break
            
        # Sleep in 1-second ticks so changes in config.json or refresh triggers apply promptly
        sleep_interval = page_display_seconds(current_page, config, loop_interval)
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
