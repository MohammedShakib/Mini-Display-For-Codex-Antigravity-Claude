import json
import os
import time
from pathlib import Path
from flask import Flask, jsonify, request, send_file, send_from_directory

app = Flask(__name__, static_folder="web", static_url_path="")

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "runtime_state.json"
ASSETS_DIR = BASE_DIR / "assets"
LIVE_PREVIEW_PATH = ASSETS_DIR / "live_screen.jpg"


def read_config():
    defaults = {
        "clock_ip": "192.168.0.58",
        "rotation_interval": 30,
        "ag_model_mode": "auto",
        "alert_threshold": 80,
        "show_splash": True,
        "selected_theme": "default",
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults


def write_config(data):
    current = read_config()
    current.update(data)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    return current


@app.route("/")
def index():
    index_file = BASE_DIR / "web" / "index.html"
    if index_file.exists():
        return send_file(str(index_file))
    return "<h1>SyncAI Control Dashboard is loading...</h1>"


@app.route("/assets/<path:filename>")
def serve_asset(filename):
    return send_from_directory(str(ASSETS_DIR), filename)


@app.route("/api/preview")
def preview():
    if LIVE_PREVIEW_PATH.exists():
        resp = send_file(str(LIVE_PREVIEW_PATH), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    # Fallback to codex_usage.jpg in root
    root_img = BASE_DIR / "codex_usage.jpg"
    if root_img.exists():
        resp = send_file(str(root_img), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    return "No preview available", 404


@app.route("/api/status")
def status():
    state = {}
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
    state["config"] = read_config()
    state["server_time"] = time.time()
    return jsonify(state)


@app.route("/api/config", methods=["GET", "POST"])
def config_endpoint():
    if request.method == "POST":
        payload = request.get_json(force=True, silent=True) or {}
        # Sanitize values
        updated = {}
        if "clock_ip" in payload:
            updated["clock_ip"] = str(payload["clock_ip"]).strip()
        if "rotation_interval" in payload:
            updated["rotation_interval"] = max(5, int(payload["rotation_interval"]))
        if "ag_model_mode" in payload:
            updated["ag_model_mode"] = str(payload["ag_model_mode"]).strip()
        if "alert_threshold" in payload:
            updated["alert_threshold"] = max(10, min(100, int(payload["alert_threshold"])))
        if "show_splash" in payload:
            updated["show_splash"] = bool(payload["show_splash"])
        if "selected_theme" in payload:
            updated["selected_theme"] = str(payload["selected_theme"]).strip()

        saved = write_config(updated)
        return jsonify({"status": "ok", "config": saved})
    return jsonify(read_config())


@app.route("/api/refresh", methods=["POST"])
def refresh():
    write_config({"force_refresh": True})
    return jsonify({"status": "ok", "message": "Clock refresh triggered"})


def run_server(host="0.0.0.0", port=5050):
    print(f"Starting SyncAI Control Dashboard on http://localhost:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    run_server()
