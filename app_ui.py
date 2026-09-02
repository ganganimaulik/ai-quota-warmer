#!/usr/bin/env python3
"""
AI Quota Warmer - Web Dashboard UI with Live Actual Usage Detector
-------------------------------------------------------------------
A modern local web dashboard displaying ACTUAL 5-hour limit usage, token counts,
real reset countdowns, and unattended automation controls for Codex and Claude Code.
"""

import json
import secrets
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from quota_warmer import (
    load_history,
    load_state,
    cooldown_remaining,
    trigger_codex,
    trigger_claude,
    run_trigger_batch,
    warm_if_expired,
    is_startup_installed,
    install_startup_autorun,
    uninstall_startup_autorun,
    install_scheduled_task,
    uninstall_scheduled_task,
    SingleInstanceLock,
)
from usage_detector import get_actual_usage_summary

PORT = 5055
HOST = "127.0.0.1"

# Any page the browser loads could POST to a wide-open localhost API, so every
# mutating request must carry this token. It is only ever handed to the page
# this server itself renders.
CSRF_TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", HOST, "localhost"}

_state_lock = threading.Lock()
ADAPTIVE_AUTO_WARM_ENABLED = True
WATCHER_ACTIVE = False           # False when another daemon already holds the lock
EVENT_LOG = deque(maxlen=60)


def push_event(message, level="info"):
    EVENT_LOG.append({
        "time": datetime.now().strftime("%I:%M:%S %p"),
        "message": str(message),
        "level": level,
    })


def background_adaptive_watcher():
    """
    Warms each tool the moment its real 5-hour window resets.

    Shares quota_warmer's persistent cooldown/backoff state and its
    single-instance lock, so running the dashboard alongside the Startup-folder
    daemon can never double-ping or fight over the history file.
    """
    global WATCHER_ACTIVE

    lock = SingleInstanceLock()
    if not lock.acquire():
        push_event("Background daemon already running elsewhere - dashboard watcher stays idle.", "warn")
        return

    WATCHER_ACTIVE = True
    push_event("Adaptive auto-warm watcher started.", "info")
    try:
        while True:
            sleep_for = 60.0
            try:
                if ADAPTIVE_AUTO_WARM_ENABLED:
                    actual = get_actual_usage_summary()
                    waits = []
                    for tool in ("claude", "codex"):
                        usage = actual.get(tool) or {}
                        warm_if_expired(
                            tool, usage, prompt="hi", notify=True,
                            log=lambda m, _t=tool: push_event(m, "info"),
                        )
                        if usage.get("is_active") and usage.get("remaining_seconds"):
                            waits.append(float(usage["remaining_seconds"]) + 5)
                        cd = cooldown_remaining(tool)
                        if cd > 0:
                            waits.append(cd + 5)
                    if waits:
                        sleep_for = min(waits)
            except Exception as e:
                push_event(f"Watcher error: {type(e).__name__}: {e}", "error")

            time.sleep(max(15.0, min(sleep_for, 300.0)))
    finally:
        WATCHER_ACTIVE = False
        lock.release()


def get_dashboard_state():
    triggers = load_history().get("triggers", [])
    state = load_state()

    return {
        "server_time": datetime.now().strftime("%I:%M:%S %p"),
        "actual": get_actual_usage_summary(),
        "startup_autorun": is_startup_installed(),
        "adaptive_auto_warm": ADAPTIVE_AUTO_WARM_ENABLED,
        "watcher_active": WATCHER_ACTIVE,
        "cooldowns": {t: int(cooldown_remaining(t, state)) for t in ("claude", "codex")},
        "failures": {t: int((state.get(t) or {}).get("consecutive_failures", 0)) for t in ("claude", "codex")},
        "triggers_count": len(triggers),
        "recent_triggers": list(reversed(triggers[-8:])),
        "events": list(EVENT_LOG),
    }



MAX_BODY_BYTES = 64 * 1024


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIQuotaWarmer"

    def log_message(self, format, *args):
        pass

    # -- guards ------------------------------------------------------------

    def _host_ok(self) -> bool:
        """Rejects DNS-rebinding: only literal loopback Host headers are served."""
        return (self.headers.get("Host") or "").strip().lower() in ALLOWED_HOSTS

    def _csrf_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-AQW-Token", ""), CSRF_TOKEN)

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")) or {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            self.send_error(403, "Forbidden")
            return

        path = urlparse(self.path).path

        if path == "/api/status":
            self.send_json(get_dashboard_state())
            return

        if path in ("/", "/index.html"):
            page = HTML_TEMPLATE.replace("__CSRF_TOKEN__", CSRF_TOKEN).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(page)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        if not self._host_ok():
            self.send_error(403, "Forbidden")
            return

        payload = self._read_payload()

        if not self._csrf_ok():
            # Without this, any website open in the browser could silently make
            # this endpoint install a scheduled task or burn quota.
            self.send_json({"error": "Invalid or missing CSRF token."}, status=403)
            return

        path = urlparse(self.path).path

        if path == "/api/trigger":
            target = payload.get("target", "all")
            if target not in ("all", "claude", "codex"):
                self.send_json({"error": "Invalid target."}, status=400)
                return
            prompt = str(payload.get("prompt") or "hi")[:2000]
            push_event(f"Manual warm-up requested for {target.upper()}.", "info")
            results = run_trigger_batch(target=target, prompt=prompt, notify=True, quiet=True)
            for name, res in results.items():
                push_event(
                    f"{name.upper()}: {'SUCCESS in ' + str(res.get('duration')) + 's' if res.get('success') else 'FAILED - ' + str(res.get('error'))[:160]}",
                    "success" if res.get("success") else "error",
                )
            self.send_json({
                "success": any(r.get("success") for r in results.values()),
                "results": results,
                "state": get_dashboard_state(),
            })
            return

        if path == "/api/check-login":
            push_event("Login check running (sends one real prompt per tool).", "warn")
            codex_res = trigger_codex("hi", timeout=60)
            claude_res = trigger_claude("hi", timeout=60)
            self.send_json({"codex": codex_res, "claude": claude_res, "state": get_dashboard_state()})
            return

        if path == "/api/toggle-startup":
            if is_startup_installed():
                uninstall_startup_autorun()
                new_state = False
            else:
                install_startup_autorun()
                new_state = True
            push_event(f"Auto-run on PC restart {'ENABLED' if new_state else 'DISABLED'}.", "info")
            self.send_json({"startup_autorun": new_state})
            return

        if path == "/api/toggle-auto-warm":
            global ADAPTIVE_AUTO_WARM_ENABLED
            with _state_lock:
                ADAPTIVE_AUTO_WARM_ENABLED = not ADAPTIVE_AUTO_WARM_ENABLED
                new_state = ADAPTIVE_AUTO_WARM_ENABLED
            push_event(f"Adaptive auto-warm {'ENABLED' if new_state else 'DISABLED'}.", "info")
            self.send_json({"adaptive_auto_warm": new_state})
            return

        if path == "/api/schedule-task":
            mode = payload.get("mode", "interval")
            if mode not in ("interval", "daily", "startup", "uninstall"):
                self.send_json({"error": "Invalid mode."}, status=400)
                return
            if mode == "uninstall":
                ok = uninstall_scheduled_task()
                push_event(f"Scheduled task removal {'succeeded' if ok else 'failed'}.", "info" if ok else "error")
                self.send_json({"status": "uninstalled", "ok": ok})
                return
            try:
                interval = float(payload.get("interval", 5.0))
            except (TypeError, ValueError):
                interval = 5.0
            time_str = str(payload.get("time") or "08:00")
            ok = install_scheduled_task(schedule_mode=mode, interval_hours=interval, time_str=time_str)
            push_event(
                f"Scheduled task install ({mode}) {'succeeded' if ok else 'failed - see console'}.",
                "success" if ok else "error",
            )
            self.send_json({"status": "installed" if ok else "failed", "ok": ok, "mode": mode, "interval": interval})
            return

        self.send_error(404, "Not Found")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Quota Warmer & Actual 5-Hour Usage Detector</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #0a0e17;
      --bg-card: rgba(18, 24, 38, 0.85);
      --bg-card-sub: rgba(10, 14, 23, 0.6);
      --border-color: rgba(255, 255, 255, 0.08);
      --border-highlight: rgba(99, 102, 241, 0.35);
      --accent-claude: #a855f7;
      --accent-claude-glow: rgba(168, 85, 247, 0.35);
      --accent-codex: #06b6d4;
      --accent-codex-glow: rgba(6, 182, 212, 0.35);
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
      --text-dim: #6b7280;
      --radius-lg: 18px;
      --radius-md: 12px;
      --radius-sm: 8px;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: radial-gradient(circle at top center, #151a30 0%, #0a0e17 65%, #05080e 100%);
      color: var(--text-main);
      font-family: 'Outfit', sans-serif;
      min-height: 100vh;
      padding: 24px;
      display: flex;
      justify-content: center;
    }

    .container {
      width: 100%;
      max-width: 1100px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    /* Header */
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 24px;
      background: var(--bg-card);
      backdrop-filter: blur(16px);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .brand-icon {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, #a855f7, #06b6d4);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      box-shadow: 0 4px 20px rgba(168, 85, 247, 0.4);
    }

    .brand-title h1 {
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.5px;
    }

    .brand-title p {
      font-size: 13px;
      color: var(--text-muted);
    }

    .clock-pill {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border-color);
      padding: 8px 14px;
      border-radius: 20px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
      color: #38bdf8;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    /* Dual Hero Cards for Actual Usage */
    .dual-cards {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
    }

    @media (max-width: 860px) {
      .dual-cards { grid-template-columns: 1fr; }
    }

    .card {
      background: var(--bg-card);
      backdrop-filter: blur(16px);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      padding: 22px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      position: relative;
      overflow: hidden;
      transition: all 0.25s ease;
    }

    .card.claude-card { border-top: 3px solid var(--accent-claude); }
    .card.codex-card { border-top: 3px solid var(--accent-codex); }

    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .tool-brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .tool-avatar {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
    }

    .claude-card .tool-avatar { background: rgba(168, 85, 247, 0.2); border: 1px solid rgba(168, 85, 247, 0.4); }
    .codex-card .tool-avatar { background: rgba(6, 182, 212, 0.2); border: 1px solid rgba(6, 182, 212, 0.4); }

    .tool-title h2 { font-size: 16px; font-weight: 700; }
    .tool-title span { font-size: 12px; color: var(--text-muted); }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 20px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .status-badge.active { background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }
    .status-badge.expired,
    .status-badge.reset { background: rgba(245, 158, 11, 0.15); color: var(--accent-amber); border: 1px solid rgba(245, 158, 11, 0.3); }
    .status-badge.idle { background: rgba(156, 163, 175, 0.15); color: var(--text-muted); border: 1px solid var(--border-color); }
    .status-badge.error { background: rgba(244, 63, 94, 0.15); color: var(--accent-rose); border: 1px solid rgba(244, 63, 94, 0.35); }

    .pulse-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.8); }
    }

    /* Countdown Display */
    .timer-center {
      text-align: center;
      padding: 8px 0;
    }

    .timer-center .digits {
      font-family: 'JetBrains Mono', monospace;
      font-size: 38px;
      font-weight: 700;
      color: #fff;
      margin: 4px 0;
    }

    .claude-card .digits { text-shadow: 0 0 25px var(--accent-claude-glow); }
    .codex-card .digits { text-shadow: 0 0 25px var(--accent-codex-glow); }

    .timer-center .sub {
      font-size: 12px;
      color: var(--text-muted);
    }

    /* Progress Bar */
    .progress-container {
      width: 100%;
      height: 8px;
      background: rgba(255, 255, 255, 0.05);
      border-radius: 10px;
      overflow: hidden;
    }

    .progress-fill {
      height: 100%;
      border-radius: 10px;
      transition: width 0.5s ease;
    }

    .claude-card .progress-fill { background: linear-gradient(90deg, #a855f7, #c084fc); }
    .codex-card .progress-fill { background: linear-gradient(90deg, #06b6d4, #38bdf8); }

    /* Key-Value Metrics Grid */
    .metrics-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      background: var(--bg-card-sub);
      padding: 12px 14px;
      border-radius: var(--radius-md);
      font-size: 12px;
      border: 1px solid var(--border-color);
    }

    .metric-cell span {
      display: block;
      color: var(--text-dim);
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 2px;
    }

    .metric-cell strong {
      color: var(--text-main);
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
    }

    /* Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 12px 18px;
      border-radius: var(--radius-md);
      font-family: 'Outfit', sans-serif;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid transparent;
      transition: all 0.2s ease;
      outline: none;
    }

    .btn-claude {
      background: linear-gradient(135deg, #a855f7, #7c3aed);
      color: #fff;
      box-shadow: 0 4px 16px var(--accent-claude-glow);
    }

    .btn-claude:hover { transform: translateY(-1px); box-shadow: 0 6px 20px var(--accent-claude-glow); }

    .btn-codex {
      background: linear-gradient(135deg, #06b6d4, #0284c7);
      color: #fff;
      box-shadow: 0 4px 16px var(--accent-codex-glow);
    }

    .btn-codex:hover { transform: translateY(-1px); box-shadow: 0 6px 20px var(--accent-codex-glow); }

    .btn-primary {
      background: linear-gradient(135deg, #6366f1, #4f46e5);
      color: #fff;
      box-shadow: 0 4px 18px rgba(99, 102, 241, 0.4);
    }

    .btn-primary:hover { transform: translateY(-1px); }

    .btn-secondary {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border-color);
      color: var(--text-main);
    }

    .btn-secondary:hover { background: rgba(255, 255, 255, 0.1); }
    .btn-sm { padding: 6px 12px; font-size: 12px; border-radius: var(--radius-sm); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }

    /* Control Bar */
    .control-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--bg-card);
      padding: 16px 20px;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      gap: 16px;
      flex-wrap: wrap;
    }

    .control-left {
      display: flex;
      align-items: center;
      gap: 16px;
    }

    /* Switch */
    .switch-wrap {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 13px;
      font-weight: 600;
    }

    .switch {
      position: relative;
      display: inline-block;
      width: 44px;
      height: 24px;
    }

    .switch input { opacity: 0; width: 0; height: 0; }
    .slider {
      position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
      background-color: rgba(255, 255, 255, 0.15);
      transition: .3s;
      border-radius: 24px;
    }
    .slider:before {
      position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px;
      background-color: white; transition: .3s; border-radius: 50%;
    }
    input:checked + .slider { background-color: #6366f1; }
    input:checked + .slider:before { transform: translateX(20px); }

    /* Terminal Console */
    .console-card {
      background: #06090e;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      padding: 14px 18px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      color: #94a3b8;
      max-height: 180px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .console-line.success { color: var(--accent-green); }
    .console-line.error { color: var(--accent-rose); }
    .console-line.info { color: #38bdf8; }
    .console-line.warn { color: var(--accent-amber); }

    .spinner {
      border: 2px solid rgba(255, 255, 255, 0.2);
      border-left-color: #fff;
      border-radius: 50%;
      width: 14px;
      height: 14px;
      animation: spin 0.8s linear infinite;
      display: inline-block;
    }

    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <header class="header">
      <div class="brand">
        <div class="brand-icon">⚡</div>
        <div class="brand-title">
          <h1>AI Quota Warmer & Live Usage Tracker</h1>
          <p>Real-Time 5-Hour Rolling Limit Detector for OpenAI Codex & Claude Code</p>
        </div>
      </div>
      <div style="display: flex; gap: 10px; align-items: center;">
        <div class="clock-pill">⏱️ <span id="serverClock">--:--:--</span></div>
        <button class="btn btn-secondary btn-sm" onclick="fetchStatus()">🔄 Refresh Data</button>
      </div>
    </header>

    <!-- Dual Live Actual Usage Cards -->
    <div class="dual-cards">
      <!-- Claude Code Card -->
      <div class="card claude-card">
        <div class="card-header">
          <div class="tool-brand">
            <div class="tool-avatar">🟣</div>
            <div class="tool-title">
              <h2>Claude Code</h2>
              <span id="claudeModels">Models: Checking...</span>
            </div>
          </div>
          <div class="status-badge idle" id="claudeBadge">
            <span class="pulse-dot"></span> <span id="claudeBadgeText">IDLE</span>
          </div>
        </div>

        <div class="timer-center">
          <div class="digits" id="claudeTimer">--:--:--</div>
          <div class="sub" id="claudeSub">Detecting actual 5h window from ~/.claude...</div>
        </div>

        <div class="progress-container">
          <div class="progress-fill" id="claudeProgress" style="width: 0%;"></div>
        </div>

        <div class="metrics-grid">
          <div class="metric-cell">
            <span>5H WINDOW START</span>
            <strong id="claudeStart">N/A</strong>
          </div>
          <div class="metric-cell">
            <span>FULL QUOTA RESETS AT</span>
            <strong id="claudeReset">N/A</strong>
          </div>
          <div class="metric-cell">
            <span>USER PROMPTS IN 5H</span>
            <strong id="claudePrompts">0 prompts</strong>
          </div>
          <div class="metric-cell">
            <span>TOTAL TOKENS USED</span>
            <strong id="claudeTokens">0</strong>
          </div>
        </div>

        <button class="btn btn-claude" id="btnClaudeWarm" onclick="triggerSingle('claude')">
          <span>🟣 Warm Up Claude Limit</span>
        </button>
      </div>

      <!-- OpenAI Codex Card -->
      <div class="card codex-card">
        <div class="card-header">
          <div class="tool-brand">
            <div class="tool-avatar">🤖</div>
            <div class="tool-title">
              <h2>OpenAI Codex CLI</h2>
              <span>gpt-5.4-mini · AppData Executable</span>
            </div>
          </div>
          <div class="status-badge idle" id="codexBadge">
            <span class="pulse-dot"></span> <span id="codexBadgeText">IDLE</span>
          </div>
        </div>

        <div class="timer-center">
          <div class="digits" id="codexTimer">--:--:--</div>
          <div class="sub" id="codexSub">Detecting actual 5h window from ~/.codex...</div>
        </div>

        <div class="progress-container">
          <div class="progress-fill" id="codexProgress" style="width: 0%;"></div>
        </div>

        <div class="metrics-grid">
          <div class="metric-cell">
            <span>5H WINDOW START</span>
            <strong id="codexStart">N/A</strong>
          </div>
          <div class="metric-cell">
            <span>FULL QUOTA RESETS AT</span>
            <strong id="codexReset">N/A</strong>
          </div>
          <div class="metric-cell">
            <span>TURNS IN 5H</span>
            <strong id="codexTurns">0 turns</strong>
          </div>
          <div class="metric-cell">
            <span>TOKENS IN SESSION</span>
            <strong id="codexTokens">0</strong>
          </div>
        </div>

        <button class="btn btn-codex" id="btnCodexWarm" onclick="triggerSingle('codex')">
          <span>🤖 Warm Up Codex Limit</span>
        </button>
      </div>
    </div>

    <!-- Global Action & Unattended Controls -->
    <div class="control-bar">
      <div class="control-left">
        <button class="btn btn-primary" id="btnWarmAll" onclick="triggerSingle('all')">
          <span>🔥 Warm Up Both Now</span>
        </button>

        <div class="switch-wrap">
          <label class="switch">
            <input type="checkbox" id="chkStartup" onchange="toggleStartup()">
            <span class="slider"></span>
          </label>
          <span>Auto-Run on PC Restart</span>
        </div>

        <div class="switch-wrap">
          <label class="switch">
            <input type="checkbox" id="chkAutoWarm" onchange="toggleAutoWarm()">
            <span class="slider"></span>
          </label>
          <span>Adaptive Auto-Warm <span id="watcherState" style="color:var(--text-dim);font-weight:400;"></span></span>
        </div>
      </div>

      <div style="display: flex; gap: 8px;">
        <button class="btn btn-secondary btn-sm" onclick="scheduleTask('interval', 5)">
          ⏱️ Run Every 5h (Task Scheduler)
        </button>
        <button class="btn btn-secondary btn-sm" onclick="checkLogins()">
          🔍 Check Logins
        </button>
      </div>
    </div>

    <!-- Live Console Output -->
    <div class="console-card" id="consoleOutput">
      <div class="console-line info">[System] Actual 5-Hour Limit Detector active. Monitoring ~/.claude and ~/.codex.</div>
    </div>
  </div>

  <script>
    const CSRF_TOKEN = '__CSRF_TOKEN__';
    let claudeRemainingSec = 0;
    let codexRemainingSec = 0;
    let busy = false;
    let lastEventKey = '';

    function log(msg, type = 'info') {
      const con = document.getElementById('consoleOutput');
      const line = document.createElement('div');
      line.className = `console-line ${type}`;
      line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
      con.appendChild(line);
      while (con.childNodes.length > 200) con.removeChild(con.firstChild);
      con.scrollTop = con.scrollHeight;
    }

    async function post(path, body) {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-AQW-Token': CSRF_TOKEN },
        body: JSON.stringify(body || {})
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    }

    function setBusy(state) {
      busy = state;
      ['btnWarmAll', 'btnClaudeWarm', 'btnCodexWarm'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = state;
      });
    }

    function fmt(sec) {
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      return `${String(h).padStart(2,'0')}h ${String(m).padStart(2,'0')}m ${String(s).padStart(2,'0')}s`;
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status', { cache: 'no-store' });
        updateUI(await res.json());
      } catch (err) {
        log('Failed to fetch metrics: ' + err, 'error');
      }
    }

    function renderCard(p, d, cooldown, failures) {
      const badge = document.getElementById(p + 'Badge');
      badge.className = `status-badge ${String(d.status || 'idle').toLowerCase()}`;
      document.getElementById(p + 'BadgeText').textContent = d.status;
      document.getElementById(p + 'Timer').textContent = d.time_remaining;
      document.getElementById(p + 'Progress').style.width = (d.progress_pct || 0) + '%';
      document.getElementById(p + 'Start').textContent = d.window_start;
      document.getElementById(p + 'Reset').textContent = d.window_reset;
      document.getElementById(p + 'Tokens').textContent = (d.tokens?.total || 0).toLocaleString();

      let sub;
      if (d.status === 'ERROR') {
        sub = 'Detector error: ' + (d.error || 'unknown');
      } else if (d.is_active) {
        const quota = (d.session_used_pct !== null && d.session_used_pct !== undefined)
          ? `Quota ${d.session_used_pct}% used · ` : '';
        sub = `${quota}Active 5h window (${d.progress_pct}% elapsed) · via ${d.source}`;
      } else if (failures > 0) {
        sub = `Window reset. Last ${failures} warm-up(s) failed — retry in ${Math.ceil(cooldown / 60)}m.`;
      } else if (cooldown > 0) {
        sub = `Window reset. Cooling down ${Math.ceil(cooldown / 60)}m before next auto-warm.`;
      } else {
        sub = 'Window reset. Ready for next warmup.';
      }
      document.getElementById(p + 'Sub').textContent = sub;
    }

    function updateUI(data) {
      document.getElementById('serverClock').textContent = data.server_time || '--:--:--';
      document.getElementById('chkStartup').checked = !!data.startup_autorun;
      document.getElementById('chkAutoWarm').checked = !!data.adaptive_auto_warm;
      document.getElementById('watcherState').textContent =
        data.watcher_active ? '(watching)' : '(handled by background daemon)';

      const cl = data.actual?.claude, cx = data.actual?.codex;

      if (cl) {
        claudeRemainingSec = cl.remaining_seconds || 0;
        renderCard('claude', cl, data.cooldowns?.claude || 0, data.failures?.claude || 0);
        document.getElementById('claudePrompts').textContent =
          `${cl.user_prompts} prompts (${cl.total_events} events)`;
        if (cl.models_used?.length) {
          document.getElementById('claudeModels').textContent = 'Models: ' + cl.models_used.join(', ');
        }
      }

      if (cx) {
        codexRemainingSec = cx.remaining_seconds || 0;
        renderCard('codex', cx, data.cooldowns?.codex || 0, data.failures?.codex || 0);
        document.getElementById('codexTurns').textContent = `${cx.turns_in_5h} turns`;
      }

      // Mirror events the server-side watcher produced while nobody was looking.
      const events = data.events || [];
      if (events.length) {
        const key = events[events.length - 1].time + events[events.length - 1].message;
        if (key !== lastEventKey) {
          const start = lastEventKey ? events.findIndex(e => (e.time + e.message) === lastEventKey) + 1 : events.length - 1;
          events.slice(Math.max(0, start)).forEach(e => log('[watcher] ' + e.message, e.level));
          lastEventKey = key;
        }
      }
    }

    async function triggerSingle(target) {
      if (busy) return;
      setBusy(true);
      log(`Triggering warmup for [${target.toUpperCase()}]... this can take up to a minute.`, 'info');
      try {
        const data = await post('/api/trigger', { target, prompt: 'hi' });
        for (const [name, r] of Object.entries(data.results || {})) {
          log(`${name.toUpperCase()}: ${r.success ? 'SUCCESS (' + r.duration + 's)' : 'FAILED: ' + r.error}`,
              r.success ? 'success' : 'error');
        }
        if (data.state) updateUI(data.state);
      } catch (err) {
        log('Trigger error: ' + err.message, 'error');
      } finally {
        setBusy(false);
        fetchStatus();
      }
    }

    async function checkLogins() {
      if (busy) return;
      setBusy(true);
      log('Testing logins (sends one real prompt to each tool)...', 'warn');
      try {
        const data = await post('/api/check-login', {});
        log(`Codex: ${data.codex?.success ? 'AUTHENTICATED' : data.codex?.error}`, data.codex?.success ? 'success' : 'error');
        log(`Claude: ${data.claude?.success ? 'AUTHENTICATED' : data.claude?.error}`, data.claude?.success ? 'success' : 'error');
      } catch (err) {
        log('Check login error: ' + err.message, 'error');
      } finally {
        setBusy(false);
        fetchStatus();
      }
    }

    async function toggleStartup() {
      try {
        const data = await post('/api/toggle-startup', {});
        log(`Auto-Run on PC Restart is now: ${data.startup_autorun ? 'ENABLED' : 'DISABLED'}`, 'info');
      } catch (err) {
        log('Failed to toggle startup: ' + err.message, 'error');
      }
      fetchStatus();
    }

    async function toggleAutoWarm() {
      try {
        const data = await post('/api/toggle-auto-warm', {});
        log(`Adaptive auto-warm is now: ${data.adaptive_auto_warm ? 'ENABLED' : 'DISABLED'}`, 'info');
      } catch (err) {
        log('Failed to toggle auto-warm: ' + err.message, 'error');
      }
      fetchStatus();
    }

    async function scheduleTask(mode, interval = 5) {
      log(`Configuring Task Scheduler for ${mode} (${interval}h)...`, 'info');
      try {
        const data = await post('/api/schedule-task', { mode, interval });
        log(`Task Scheduler: ${data.ok ? 'installed (' + mode + ')' : 'FAILED — see the dashboard console window'}`,
            data.ok ? 'success' : 'error');
      } catch (err) {
        log('Schedule error: ' + err.message, 'error');
      }
      fetchStatus();
    }

    // Local 1s countdown between server polls.
    setInterval(() => {
      if (claudeRemainingSec > 0) {
        document.getElementById('claudeTimer').textContent = fmt(--claudeRemainingSec);
      }
      if (codexRemainingSec > 0) {
        document.getElementById('codexTimer').textContent = fmt(--codexRemainingSec);
      }
    }, 1000);

    fetchStatus();
    setInterval(fetchStatus, 8000);
  </script>
</body>
</html>
"""


def run_server():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    url = f"http://{HOST}:{PORT}"

    # ThreadingHTTPServer, not HTTPServer: a warm-up can block for a minute, and
    # a single-threaded server would freeze the whole dashboard until it returned.
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    except OSError as e:
        print(f"[-] Could not bind {url}: {e}")
        print("    Another AI Quota Warmer dashboard is probably already running.")
        print(f"    Open {url} in your browser, or close the other instance first.")
        return
    httpd.daemon_threads = True

    threading.Thread(target=background_adaptive_watcher, daemon=True).start()

    print("=" * 65)
    print("  AI QUOTA WARMER - WEB DASHBOARD RUNNING")
    print("=" * 65)
    print(f"  Dashboard URL : {url}")
    print("  Adaptive auto-warm fires the moment a 5-hour window resets.")
    print("  Press Ctrl+C to stop.")
    print("=" * 65)

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard server stopped.")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    run_server()
