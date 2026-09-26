#!/usr/bin/env python3
"""
AI Quota Warmer - Web Dashboard UI with Live Actual Usage Detector
-------------------------------------------------------------------
A modern local web dashboard displaying ACTUAL 5-hour limit usage, token counts,
real reset countdowns, and unattended automation controls for every configured
Codex and Claude Code account.
"""

import json
import secrets
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from quota_warmer import (
    load_history,
    load_state,
    load_accounts,
    find_account,
    select_accounts,
    prepare_account,
    remove_account,
    start_login_job,
    current_login_job,
    dismiss_login_job,
    cooldown_remaining,
    get_all_usage,
    run_trigger_batch,
    warm_cycle,
    sleep_until_next_cycle,
    account_login_status,
    describe_login,
    is_startup_installed,
    install_startup_autorun,
    uninstall_startup_autorun,
    install_scheduled_task,
    uninstall_scheduled_task,
    SingleInstanceLock,
)
from usage_detector import invalidate_usage_cache

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


def _watcher_log(msg):
    text = str(msg).strip()
    level = "success" if text.startswith("[+]") else "error" if text.startswith("[-]") else "info"
    push_event(text, level)


def background_adaptive_watcher():
    """
    Warms each account the moment its real 5-hour window resets.

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
                    # Re-read every pass so accounts added from the CLI appear without a restart.
                    sleep_for = warm_cycle(load_accounts(), prompt="hi", notify=True, log=_watcher_log)
            except Exception as e:
                push_event(f"Watcher error: {type(e).__name__}: {e}", "error")

            sleep_until_next_cycle(max(15.0, min(sleep_for, 300.0)))
    finally:
        WATCHER_ACTIVE = False
        lock.release()


def get_dashboard_state(force=False):
    triggers = load_history().get("triggers", [])
    state = load_state()
    accounts = load_accounts()
    usage = get_all_usage(accounts, state, force=force)

    return {
        "server_time": datetime.now().strftime("%I:%M:%S %p"),
        "accounts": [
            dict(
                acc.describe(),
                usage=usage[acc.key],
                cooldown=int(cooldown_remaining(acc.key, state)),
                failures=int((state.get(acc.key) or {}).get("consecutive_failures", 0)),
            )
            for acc in accounts
        ],
        "startup_autorun": is_startup_installed(),
        "adaptive_auto_warm": ADAPTIVE_AUTO_WARM_ENABLED,
        "watcher_active": WATCHER_ACTIVE,
        "triggers_count": len(triggers),
        "recent_triggers": list(reversed(triggers[-8:])),
        "events": list(EVENT_LOG),
        "login_job": current_login_job().snapshot() if current_login_job() else None,
    }


def check_all_logins():
    """Free login status for every account, probed in parallel."""
    accounts = load_accounts()
    statuses = {}
    threads = [
        threading.Thread(target=lambda a=acc: statuses.__setitem__(a.key, account_login_status(a)), daemon=True)
        for acc in accounts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [
        dict(statuses.get(acc.key) or {}, key=acc.key, label=acc.label,
             summary=describe_login(statuses.get(acc.key) or {}))
        for acc in accounts
    ]



MAX_BODY_BYTES = 64 * 1024


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIQuotaWarmer"

    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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

        if path == "/api/login-job":
            job = current_login_job()
            self.send_json({"job": job.snapshot() if job else None})
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
            prompt = str(payload.get("prompt") or "hi")[:2000]
            if payload.get("account"):
                acc = find_account(str(payload.get("account")))
                if acc is None:
                    self.send_json({"error": "Unknown account."}, status=400)
                    return
                target, accounts, what = acc.key, [acc], acc.label
            else:
                target = payload.get("target", "all")
                if target not in ("all", "claude", "codex"):
                    self.send_json({"error": "Invalid target."}, status=400)
                    return
                accounts, what = select_accounts(target), target.upper()
            push_event(f"Manual warm-up requested for {what}.", "info")
            results = run_trigger_batch(
                target=target,
                prompt=prompt,
                notify=True,
                quiet=True,
                accounts=accounts,
                claude_model=payload.get("claude_model"),
                codex_model=payload.get("codex_model"),
            )
            labels = {a.key: a.label for a in accounts}
            for key, res in results.items():
                push_event(
                    f"{labels.get(key, key)}: {'SUCCESS in ' + str(res.get('duration')) + 's' if res.get('success') else 'FAILED - ' + str(res.get('error'))[:160]}",
                    "success" if res.get("success") else "error",
                )
            invalidate_usage_cache()
            self.send_json({
                "success": any(r.get("success") for r in results.values()),
                "results": [dict(res, key=key, label=labels.get(key, key)) for key, res in results.items()],
                "state": get_dashboard_state(force=True),
            })
            return

        if path == "/api/check-login":
            push_event("Login check running (free - asks each CLI who it is signed in as).", "info")
            self.send_json({"logins": check_all_logins(), "state": get_dashboard_state()})
            return

        # -- account management: the CLI's own sign-in, run in the background --

        if path == "/api/accounts/add":
            try:
                # The folder is always the default ~/.claude-NAME / ~/.codex-NAME;
                # custom folders stay a CLI option (--dir).
                acc = prepare_account(str(payload.get("tool") or ""), str(payload.get("name") or "").strip())
                job = start_login_job(acc, register=True)
            except (ValueError, OSError) as e:
                self.send_json({"error": str(e)}, status=400)
                return
            self.send_json({"job": job.snapshot()})
            return

        if path == "/api/accounts/login":
            acc = find_account(str(payload.get("account") or ""))
            if acc is None:
                self.send_json({"error": "Unknown account."}, status=400)
                return
            try:
                job = start_login_job(acc, register=False)
            except ValueError as e:
                self.send_json({"error": str(e)}, status=400)
                return
            self.send_json({"job": job.snapshot()})
            return

        if path == "/api/accounts/login/code":
            job = current_login_job()
            try:
                if job is None:
                    raise ValueError("No sign-in is in progress.")
                job.submit_code(payload.get("code"))
            except ValueError as e:
                self.send_json({"error": str(e)}, status=400)
                return
            self.send_json({"job": job.snapshot()})
            return

        if path == "/api/accounts/login/cancel":
            job = current_login_job()
            if job is not None:
                job.cancel()
            self.send_json({"job": job.snapshot() if job else None})
            return

        if path == "/api/accounts/login/dismiss":
            dismiss_login_job()
            self.send_json({"job": None})
            return

        if path == "/api/accounts/remove":
            try:
                acc = remove_account(str(payload.get("account") or ""))
            except ValueError as e:
                self.send_json({"error": str(e)}, status=400)
                return
            invalidate_usage_cache()
            push_event(f"Removed {acc.label}. Its folder and login stay in {acc.home}.", "info")
            self.send_json({"removed": acc.key, "state": get_dashboard_state(force=True)})
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
      --bg-card: #111726;
      --bg-card-sub: #0a0e17;
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

    @media (prefers-reduced-motion: reduce) {
      *, ::before, ::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
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

    /* One Hero Card per Account */
    .account-cards {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
    }

    @media (max-width: 860px) {
      .account-cards { grid-template-columns: 1fr; }
    }

    .empty-state {
      display: none;
      background: var(--bg-card);
      border: 1px dashed var(--border-color);
      border-radius: var(--radius-lg);
      padding: 22px;
      color: var(--text-muted);
      font-size: 14px;
    }

    .empty-state code {
      font-family: 'JetBrains Mono', monospace;
      color: var(--text-main);
    }

    .card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      padding: 22px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      position: relative;
      overflow: hidden;
      transition: border-color 0.2s ease;
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
      display: inline-block;
    }

    .status-badge.active .pulse-dot {
      box-shadow: 0 0 6px currentColor;
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.35; }
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

    .claude-card .digits { text-shadow: 0 0 12px var(--accent-claude-glow); }
    .codex-card .digits { text-shadow: 0 0 12px var(--accent-codex-glow); }

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

    /* `hidden` must win over the display rules below. */
    [hidden] { display: none !important; }

    /* Account panel: add form + live sign-in */
    .panel {
      background: var(--bg-card);
      border: 1px solid var(--border-highlight);
      border-radius: var(--radius-lg);
      padding: 18px 22px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    #addForm, #loginView, #loginRunning, #linkBox, #codeBox { display: flex; flex-direction: column; gap: 8px; }
    #loginView { gap: 12px; }
    #loginResult:empty, #loginError:empty, #addError:empty { display: none; }
    .panel h3 { font-size: 16px; font-weight: 700; }
    .muted { color: var(--text-muted); font-size: 13px; }
    .form-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

    .field {
      background: var(--bg-card-sub);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-sm);
      color: var(--text-main);
      font-family: 'Outfit', sans-serif;
      font-size: 13px;
      padding: 7px 10px;
      outline: none;
    }

    .field:focus { border-color: var(--border-highlight); }
    .field.mono { font-family: 'JetBrains Mono', monospace; font-size: 12px; flex: 1; min-width: 240px; }
    a.btn { text-decoration: none; }
    .form-error { color: var(--accent-rose); font-size: 13px; }
    .result-ok { color: var(--accent-green); font-size: 14px; }
    .result-bad { color: var(--accent-rose); font-size: 14px; }

    .card-actions { display: flex; justify-content: flex-end; gap: 14px; margin-top: -6px; }

    .link-btn {
      background: none;
      border: none;
      padding: 0;
      cursor: pointer;
      color: var(--text-dim);
      font-family: 'Outfit', sans-serif;
      font-size: 12px;
    }

    .link-btn:hover { color: var(--text-main); text-decoration: underline; }
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

    <!-- One Live Usage Card per Account (built by buildCards) -->
    <div class="account-cards" id="accountCards"></div>
    <div class="empty-state" id="noAccounts">
      No accounts configured yet. Use <strong>➕ Add Account</strong> below, or run
      <code>python quota_warmer.py --add-account claude work</code> in a terminal.
    </div>

    <!-- Account panel: the add form, then the live sign-in (hidden until used) -->
    <div class="panel" id="accountPanel" hidden>
      <div id="addForm">
        <h3>Add an account</h3>
        <p class="muted">Signs in through the tool's own browser login and keeps it in a folder of
          its own (~/.claude-NAME or ~/.codex-NAME). The account shows up here once the sign-in finishes.</p>
        <div class="form-row">
          <select class="field" id="addTool">
            <option value="claude">Claude Code</option>
            <option value="codex">Codex</option>
          </select>
          <input class="field" id="addName" maxlength="32" placeholder="Name, e.g. personal2"
                 autocomplete="off" onkeydown="if (event.key === 'Enter') submitAdd()">
          <button class="btn btn-primary btn-sm" id="btnAddSubmit" onclick="submitAdd()">Add &amp; Sign In</button>
          <button class="btn btn-secondary btn-sm" onclick="closePanel()">Cancel</button>
        </div>
        <div class="form-error" id="addError"></div>
      </div>

      <div id="loginView" hidden>
        <h3 id="loginTitle">Signing in…</h3>
        <div id="loginRunning">
          <p class="muted">A browser tab should have opened. Finish the sign-in there.</p>
          <div id="linkBox" hidden>
            <p class="muted">Is that browser signed in to a different account? Copy this link into a
              private/incognito window and sign in there with the account you want:</p>
            <div class="form-row">
              <input class="field mono" id="loginUrl" readonly>
              <button class="btn btn-secondary btn-sm" onclick="copyLoginUrl()">Copy</button>
              <a class="btn btn-secondary btn-sm" id="loginOpen" target="_blank" rel="noopener noreferrer">Open</a>
            </div>
          </div>
          <div id="codeBox" hidden>
            <p class="muted">Signed in through the link? Claude then shows a code. Paste it here:</p>
            <div class="form-row">
              <input class="field mono" id="loginCode" placeholder="Paste the code" autocomplete="off"
                     onkeydown="if (event.key === 'Enter') submitCode()">
              <button class="btn btn-primary btn-sm" onclick="submitCode()">Submit code</button>
            </div>
          </div>
        </div>
        <p id="loginResult"></p>
        <div class="form-error" id="loginError"></div>
        <div class="form-row">
          <button class="btn btn-secondary btn-sm" id="btnLoginCancel" onclick="cancelLogin()">Cancel sign-in</button>
          <button class="btn btn-secondary btn-sm" id="btnLoginClose" onclick="closePanel()" hidden>Close</button>
        </div>
      </div>
    </div>

    <!-- Global Action & Unattended Controls -->
    <div class="control-bar">
      <div class="control-left">
        <button class="btn btn-primary" id="btnWarmAll" onclick="trigger({ target: 'all' }, 'ALL ACCOUNTS')">
          <span>🔥 Warm Up All Now</span>
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
        <button class="btn btn-secondary btn-sm" onclick="openAddForm()">
          ➕ Add Account
        </button>
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
      <div class="console-line info">[System] Actual 5-Hour Limit Detector active. Monitoring every configured account.</div>
    </div>
  </div>

  <script>
    const CSRF_TOKEN = '__CSRF_TOKEN__';
    const cards = new Map();      // account key -> card element
    const remaining = {};         // account key -> seconds left, ticked locally between polls
    let cardKeys = null;
    let busy = false;
    let lastEventKey = '';

    // Static skeleton only - account names, paths and readings go in through
    // textContent so nothing from disk is ever parsed as HTML.
    const CARD_HTML = `
      <div class="card-header">
        <div class="tool-brand">
          <div class="tool-avatar"></div>
          <div class="tool-title"><h2 class="acc-label"></h2><span class="acc-sub"></span></div>
        </div>
        <div class="status-badge idle"><span class="pulse-dot"></span> <span class="badge-text">IDLE</span></div>
      </div>
      <div class="timer-center">
        <div class="digits">--:--:--</div>
        <div class="sub">Detecting actual 5h window...</div>
      </div>
      <div class="progress-container"><div class="progress-fill" style="width: 0%;"></div></div>
      <div class="metrics-grid">
        <div class="metric-cell"><span>5H WINDOW START</span><strong class="m-start">N/A</strong></div>
        <div class="metric-cell"><span>FULL QUOTA RESETS AT</span><strong class="m-reset">N/A</strong></div>
        <div class="metric-cell"><span class="m-count-label"></span><strong class="m-count">0</strong></div>
        <div class="metric-cell"><span class="m-tokens-label"></span><strong class="m-tokens">0</strong></div>
      </div>
      <button class="btn warm-btn"></button>
      <div class="card-actions">
        <button class="link-btn relogin-btn">Sign in again</button>
        <button class="link-btn remove-btn">Remove</button>
      </div>`;

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
      document.getElementById('btnWarmAll').disabled = state;
      for (const node of cards.values()) {
        if (node.warmBtn) node.warmBtn.disabled = state;
      }
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

    function buildCards(accounts) {
      const wrap = document.getElementById('accountCards');
      wrap.textContent = '';
      cards.clear();
      for (const acc of accounts) {
        const isClaude = acc.tool === 'claude';
        const card = document.createElement('div');
        card.className = `card ${isClaude ? 'claude-card' : 'codex-card'}`;
        card.innerHTML = CARD_HTML;
        card.querySelector('.tool-avatar').textContent = isClaude ? '🟣' : '🤖';
        card.querySelector('.acc-label').textContent = acc.label;
        card.querySelector('.m-count-label').textContent = isClaude ? 'USER PROMPTS IN 5H' : 'TURNS IN 5H';
        card.querySelector('.m-tokens-label').textContent = isClaude ? 'TOTAL TOKENS USED' : 'TOKENS IN SESSION';
        const btn = card.querySelector('.warm-btn');
        btn.classList.add(isClaude ? 'btn-claude' : 'btn-codex');
        btn.textContent = `${isClaude ? '🟣' : '🤖'} Warm Up ${acc.label}`;
        btn.disabled = busy;
        btn.addEventListener('click', () => trigger({ account: acc.key }, acc.label));
        card.querySelector('.relogin-btn').addEventListener('click', () => relogin(acc.key, acc.label));
        card.querySelector('.remove-btn').addEventListener('click', () => removeAccount(acc.key, acc.label, acc.dir));
        wrap.appendChild(card);
        cards.set(acc.key, {
          card,
          badge: card.querySelector('.status-badge'),
          badgeText: card.querySelector('.badge-text'),
          digits: card.querySelector('.digits'),
          progressFill: card.querySelector('.progress-fill'),
          mStart: card.querySelector('.m-start'),
          mReset: card.querySelector('.m-reset'),
          mTokens: card.querySelector('.m-tokens'),
          mCount: card.querySelector('.m-count'),
          accSub: card.querySelector('.acc-sub'),
          sub: card.querySelector('.sub'),
          warmBtn: btn,
        });
      }
      for (const key of Object.keys(remaining)) {
        if (!cards.has(key)) delete remaining[key];
      }
      document.getElementById('noAccounts').style.display = accounts.length ? 'none' : 'block';
    }

    function renderCard(acc) {
      const node = cards.get(acc.key);
      if (!node) return;
      const d = acc.usage || {};
      const statusClass = String(d.status || 'idle').toLowerCase();
      node.badge.className = `status-badge ${statusClass}`;
      node.badgeText.textContent = d.status || 'IDLE';
      node.digits.textContent = d.time_remaining || '--:--:--';
      node.progressFill.style.width = (d.progress_pct || 0) + '%';
      node.mStart.textContent = d.window_start || 'N/A';
      node.mReset.textContent = d.window_reset || 'N/A';
      node.mTokens.textContent = (d.tokens?.total || 0).toLocaleString();
      node.mCount.textContent = acc.tool === 'claude'
        ? `${d.user_prompts || 0} prompts (${d.total_events || 0} events)`
        : `${d.turns_in_5h || 0} turns`;
      const extra = acc.tool === 'claude'
        ? (d.models_used?.length ? ' · ' + d.models_used.join(', ') : '')
        : (d.plan_type ? ' · ' + d.plan_type : '');
      node.accSub.textContent = acc.dir + extra;
      node.sub.textContent = subText(d, acc.cooldown || 0, acc.failures || 0);

      if (d.is_active && (d.reset_epoch || d.remaining_seconds)) {
        const resetEpoch = d.reset_epoch ? Number(d.reset_epoch) : (Date.now() / 1000 + Number(d.remaining_seconds || 0));
        remaining[acc.key] = { resetEpoch, is_active: true };
      } else {
        remaining[acc.key] = null;
      }
    }

    function subText(d, cooldown, failures) {
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
      return sub;
    }

    function updateUI(data) {
      document.getElementById('serverClock').textContent = data.server_time || '--:--:--';
      document.getElementById('chkStartup').checked = !!data.startup_autorun;
      document.getElementById('chkAutoWarm').checked = !!data.adaptive_auto_warm;
      document.getElementById('watcherState').textContent =
        data.watcher_active ? '(watching)' : '(handled by background daemon)';

      // Rebuild only when the account list changes (e.g. one was added from the CLI).
      const accounts = data.accounts || [];
      const keys = accounts.map(a => a.key).join('|');
      if (keys !== cardKeys) {
        buildCards(accounts);
        cardKeys = keys;
      }
      accounts.forEach(renderCard);

      // A sign-in in progress (e.g. started in another tab, or before a reload).
      const job = data.login_job;
      if (job && (job.active || (currentJob && currentJob.started_at === job.started_at))) {
        showLoginJob(job);
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

    async function trigger(body, label) {
      if (busy) return;
      setBusy(true);
      log(`Triggering warmup for [${label}]... this can take up to a minute.`, 'info');
      try {
        const data = await post('/api/trigger', { ...body, prompt: 'hi' });
        for (const r of data.results || []) {
          log(`${r.label}: ${r.success ? 'SUCCESS (' + r.duration + 's)' : 'FAILED: ' + r.error}`,
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
      log('Checking logins (free - asks each CLI who it is signed in as, no prompt is sent)...', 'info');
      try {
        const data = await post('/api/check-login', {});
        for (const l of data.logins || []) {
          log(`${l.label}: ${l.summary}`, l.logged_in ? 'success' : (l.logged_in === false ? 'error' : 'warn'));
          if (l.warning) log(`${l.label}: ${l.warning}`, 'warn');
        }
        if (!(data.logins || []).length) log('No accounts configured.', 'warn');
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

    // -- Accounts: add, sign in again, remove ------------------------------

    let currentJob = null;    // the sign-in shown in the panel
    let loginPoll = null;
    let loggedOutcome = '';   // job whose result is already in the console

    function openAddForm() {
      if (currentJob && currentJob.active) { showLoginJob(currentJob); return; }
      document.getElementById('accountPanel').hidden = false;
      document.getElementById('addForm').hidden = false;
      document.getElementById('loginView').hidden = true;
      document.getElementById('addError').textContent = '';
      document.getElementById('addName').focus();
    }

    function closePanel() {
      if (currentJob && !currentJob.active) post('/api/accounts/login/dismiss', {}).catch(() => {});
      currentJob = null;
      stopLoginPoll();
      document.getElementById('accountPanel').hidden = true;
    }

    async function submitAdd() {
      const tool = document.getElementById('addTool').value;
      const name = document.getElementById('addName').value.trim();
      const err = document.getElementById('addError');
      err.textContent = '';
      if (!name) { err.textContent = 'Give the account a name, e.g. personal2.'; return; }
      try {
        const data = await post('/api/accounts/add', { tool, name });
        document.getElementById('addName').value = '';
        log(`Signing in new account ${data.job.label}...`, 'info');
        showLoginJob(data.job);
      } catch (e) {
        err.textContent = e.message;
      }
    }

    async function relogin(key, label) {
      try {
        const data = await post('/api/accounts/login', { account: key });
        log(`Signing in ${label} again...`, 'info');
        showLoginJob(data.job);
      } catch (e) {
        log('Sign-in error: ' + e.message, 'error');
      }
    }

    async function removeAccount(key, label, dir) {
      if (!confirm(`Stop warming ${label}?\n\nIts folder and login stay in ${dir}, so you can add it back later.`)) return;
      try {
        const data = await post('/api/accounts/remove', { account: key });
        log(`Removed ${label}.`, 'info');
        if (data.state) updateUI(data.state);
      } catch (e) {
        log('Remove error: ' + e.message, 'error');
      }
    }

    function showLoginJob(job) {
      if (!currentJob || currentJob.started_at !== job.started_at) {
        document.getElementById('loginError').textContent = '';
        document.getElementById('loginCode').value = '';
      }
      currentJob = job;
      renderLoginJob(job);
      if (job.active) startLoginPoll();
    }

    function startLoginPoll() {
      if (loginPoll) return;
      loginPoll = setInterval(async () => {
        try {
          const res = await fetch('/api/login-job', { cache: 'no-store' });
          const data = await res.json();
          if (!data.job) { stopLoginPoll(); return; }
          currentJob = data.job;
          renderLoginJob(data.job);
          if (!data.job.active) { stopLoginPoll(); fetchStatus(); }
        } catch (err) { /* transient - keep polling */ }
      }, 1200);
    }

    function stopLoginPoll() {
      if (loginPoll) { clearInterval(loginPoll); loginPoll = null; }
    }

    function renderLoginJob(job) {
      document.getElementById('accountPanel').hidden = false;
      document.getElementById('addForm').hidden = true;
      document.getElementById('loginView').hidden = false;
      const verb = { starting: 'Starting sign-in for', running: 'Signing in', succeeded: 'Signed in',
                     failed: 'Sign-in failed for', cancelled: 'Sign-in cancelled for' }[job.state] || 'Signing in';
      document.getElementById('loginTitle').textContent = `${verb} ${job.label}`;
      // While "starting" the CLI has not opened the browser yet.
      document.getElementById('loginRunning').hidden = job.state !== 'running';

      // Only ever link to https - the URL is text a CLI printed.
      const url = (job.url && /^https:\/\//.test(job.url)) ? job.url : '';
      document.getElementById('linkBox').hidden = !url;
      document.getElementById('codeBox').hidden = !(url && job.accepts_code);
      if (url) {
        document.getElementById('loginUrl').value = url;
        document.getElementById('loginOpen').href = url;
      }

      const result = document.getElementById('loginResult');
      result.className = job.state === 'succeeded' ? 'result-ok' : 'result-bad';
      result.textContent = job.active ? '' : job.state === 'succeeded'
        ? `✅ ${job.detail}${job.register ? ' — added, and it will be warmed within seconds.' : ''}`
        : `❌ ${job.detail || 'The sign-in did not complete.'}`;
      document.getElementById('btnLoginCancel').hidden = !job.active;
      document.getElementById('btnLoginClose').hidden = job.active;

      const id = job.key + '@' + job.started_at;
      if (!job.active && loggedOutcome !== id) {
        loggedOutcome = id;
        log(`${job.label}: ${job.detail}`, job.state === 'succeeded' ? 'success' : 'error');
      }
    }

    async function submitCode() {
      const input = document.getElementById('loginCode');
      const err = document.getElementById('loginError');
      err.textContent = '';
      if (!input.value.trim()) { err.textContent = 'Paste the code from the Claude page first.'; return; }
      try {
        const data = await post('/api/accounts/login/code', { code: input.value.trim() });
        input.value = '';
        showLoginJob(data.job);
      } catch (e) {
        err.textContent = e.message;
      }
    }

    async function cancelLogin() {
      try {
        const data = await post('/api/accounts/login/cancel', {});
        if (data.job) showLoginJob(data.job);
      } catch (e) {
        log('Cancel error: ' + e.message, 'error');
      }
    }

    async function copyLoginUrl() {
      const input = document.getElementById('loginUrl');
      try {
        await navigator.clipboard.writeText(input.value);
      } catch (err) {
        input.select();
        document.execCommand('copy');
      }
      log('Sign-in link copied - paste it into a private/incognito window.', 'info');
    }

    // Local 1s countdown between server polls (idle-aware & drift-free).
    function tickCountdown() {
      if (document.hidden) return;
      const now = Date.now() / 1000;
      for (const [key, item] of Object.entries(remaining)) {
        if (!item || !item.is_active) continue;
        const left = Math.max(0, Math.round(item.resetEpoch - now));
        const node = cards.get(key);
        if (node && node.digits) {
          node.digits.textContent = left > 0 ? fmt(left) : '00h 00m 00s';
        }
      }
    }

    let pollInterval = null;
    let countdownInterval = null;

    function startLoops() {
      if (!countdownInterval) countdownInterval = setInterval(tickCountdown, 1000);
      if (!pollInterval) pollInterval = setInterval(fetchStatus, 15000);
    }

    function stopLoops() {
      if (countdownInterval) { clearInterval(countdownInterval); countdownInterval = null; }
      if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    }

    // Stop CPU/GPU work when tab is minimized or hidden in background.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        stopLoops();
      } else {
        fetchStatus();
        startLoops();
      }
    });

    fetchStatus();
    startLoops();
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
    except Exception as e:
        print(f"\nDashboard server error: {type(e).__name__}: {e}")
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    run_server()
