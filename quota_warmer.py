#!/usr/bin/env python3
"""
AI Quota Warmer & 5-Hour Limit Starter
---------------------------------------
Triggers a minimal 'hi' prompt to OpenAI Codex CLI and Anthropic Claude Code CLI
so that your 5-hour rolling rate limit window starts unattended.

Features:
  - Automatic binary discovery for Codex CLI and Claude Code CLI.
  - Concurrently sends a minimal ping prompt ('hi') to both tools.
  - Watches the real 5-hour windows and warms each tool the moment it resets.
  - Persistent cooldown + exponential backoff so a broken login cannot cause a
    retry storm.
  - Single-instance locking so the startup daemon and the dashboard never
    double-fire.
  - Auto-runs on Windows restarts/logins (via the Startup folder) and/or the
    Windows Task Scheduler.
  - Native Windows desktop notifications and persistent JSON history.
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Paths & Defaults
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".ai_quota_warmer"
HISTORY_FILE = DATA_DIR / "history.json"
STATE_FILE = DATA_DIR / "state.json"
LOCK_FILE = DATA_DIR / "daemon.lock"
DEFAULT_PROMPT = "hi"
QUOTA_WINDOW_HOURS = 5.0
TASK_NAME = "AIQuotaWarmer_5HourLimit"

# Retry policy for the adaptive watcher.
BASE_COOLDOWN_SEC = 300          # minimum gap between warm attempts per tool
MAX_BACKOFF_SEC = 3600           # cap after repeated failures
MAX_HISTORY_ENTRIES = 100

# Windows Startup Directory
STARTUP_DIR = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
STARTUP_VBS_FILE = STARTUP_DIR / "AI_Quota_Warmer_Startup.vbs"

_IS_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0

# Guards concurrent writers inside a single process. Cross-process safety comes
# from the atomic os.replace() in _write_json_atomic.
_FILE_LOCK = threading.Lock()
_TRIGGER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: Path, data):
    """Writes JSON via a temp file + atomic rename so a crash cannot truncate it."""
    ensure_data_dir()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def load_history():
    ensure_data_dir()
    history = _read_json(HISTORY_FILE, {"triggers": []})
    if not isinstance(history.get("triggers"), list):
        history["triggers"] = []
    return history


def save_history_entry(entry):
    with _FILE_LOCK:
        history = load_history()
        history["triggers"].append(entry)
        history["triggers"] = history["triggers"][-MAX_HISTORY_ENTRIES:]
        try:
            _write_json_atomic(HISTORY_FILE, history)
        except OSError:
            pass


def load_state():
    state = _read_json(STATE_FILE, {})
    for tool in ("claude", "codex"):
        entry = state.get(tool)
        if not isinstance(entry, dict):
            state[tool] = {"last_attempt": 0.0, "last_success": 0.0, "consecutive_failures": 0}
    return state


def save_state(state):
    with _FILE_LOCK:
        try:
            _write_json_atomic(STATE_FILE, state)
        except OSError:
            pass


def record_attempt(tool: str, success: bool):
    """Persists the outcome of a warm attempt so cooldowns survive restarts."""
    state = load_state()
    entry = state.setdefault(tool, {"last_attempt": 0.0, "last_success": 0.0, "consecutive_failures": 0})
    now_t = time.time()
    entry["last_attempt"] = now_t
    if success:
        entry["last_success"] = now_t
        entry["consecutive_failures"] = 0
    else:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
    save_state(state)
    return entry


def cooldown_remaining(tool: str, state=None) -> float:
    """
    Seconds left before `tool` may be warmed again.

    Failures back off exponentially (5m, 10m, 20m, ... capped at 1h) so an
    expired login cannot produce an endless stream of subprocesses.
    """
    state = state if state is not None else load_state()
    entry = state.get(tool) or {}
    failures = int(entry.get("consecutive_failures", 0))
    cooldown = min(BASE_COOLDOWN_SEC * (2 ** max(0, failures)), MAX_BACKOFF_SEC)
    elapsed = time.time() - float(entry.get("last_attempt", 0) or 0)
    return max(0.0, cooldown - elapsed)


class SingleInstanceLock:
    """
    Best-effort cross-process lock so only one adaptive watcher runs at a time.

    Without this, the Startup-folder daemon and an open dashboard would each
    warm the same tool, doubling every ping.
    """

    def __init__(self, path: Path = LOCK_FILE):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        ensure_data_dir()
        try:
            self._fh = open(self.path, "a+")
        except OSError:
            return False
        try:
            if _IS_WINDOWS:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False

        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"{os.getpid()} {datetime.datetime.now().isoformat()}\n")
            self._fh.flush()
        except OSError:
            pass
        return True

    def release(self):
        if self._fh is None:
            return
        try:
            if _IS_WINDOWS:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def __enter__(self):
        self.acquired = self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

def find_codex_binary(custom_path=None):
    if custom_path and os.path.exists(custom_path):
        return custom_path

    env_path = os.getenv("CODEX_CLI_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    found = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.exe")
    if found:
        return found

    appdata = os.getenv("LOCALAPPDATA", "")
    if appdata:
        candidates = glob.glob(os.path.join(appdata, "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            return candidates[0]

    return None


def find_claude_binary(custom_path=None):
    if custom_path and os.path.exists(custom_path):
        return custom_path

    env_path = os.getenv("CLAUDE_CLI_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    found = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    if found:
        return found

    appdata_roaming = os.getenv("APPDATA", "")
    if appdata_roaming:
        npm_claude = os.path.join(appdata_roaming, "npm", "claude.cmd")
        if os.path.exists(npm_claude):
            return npm_claude

    local_bin = Path.home() / ".local" / "bin"
    for name in ("claude.exe", "claude.cmd", "claude"):
        candidate = local_bin / name
        if candidate.exists():
            return str(candidate)

    # Fall back to a one-shot npx invocation. Resolve to a real path so the
    # command can run without a shell.
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if npx:
        return npx

    return None


def _is_npx(path: str) -> bool:
    return bool(path) and Path(path).stem.lower() == "npx"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

_NOTIFY_PS = r"""
try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $b = New-Object System.Windows.Forms.NotifyIcon
  $b.Icon = [System.Drawing.SystemIcons]::Information
  $b.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
  $b.BalloonTipTitle = $env:AQW_NOTIFY_TITLE
  $b.BalloonTipText  = $env:AQW_NOTIFY_TEXT
  $b.Visible = $true
  $b.ShowBalloonTip(8000)
  Start-Sleep -Seconds 9
  $b.Visible = $false
  $b.Dispose()
} catch { }
"""


def send_windows_notification(title: str, message: str):
    """
    Shows a native Windows balloon notification.

    Title/message travel through environment variables rather than being
    interpolated into the script text, so quotes and here-string terminators in
    CLI error output cannot corrupt (or inject into) the PowerShell source. The
    script also outlives ShowBalloonTip - a NotifyIcon disappears the instant its
    process exits, which is why the previous fire-and-exit version never showed
    anything.
    """
    if not _IS_WINDOWS:
        return
    try:
        env = os.environ.copy()
        env["AQW_NOTIFY_TITLE"] = str(title)[:120]
        env["AQW_NOTIFY_TEXT"] = str(message)[:400]
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _NOTIFY_PS],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=_NO_WINDOW,
        )
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _run_cli(target, cmd, binary, timeout):
    """Runs a warm-up command and normalises the result dict."""
    start_t = time.time()
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {
            "target": target, "success": False, "response": "",
            "error": f"Timed out after {timeout}s", "duration": float(timeout), "path": binary,
        }
    except (OSError, ValueError) as e:
        return {
            "target": target, "success": False, "response": "",
            "error": f"{type(e).__name__}: {e}", "duration": round(time.time() - start_t, 2), "path": binary,
        }

    duration = round(time.time() - start_t, 2)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 0:
        return {
            "target": target, "success": True, "response": stdout or "OK",
            "error": "", "duration": duration, "path": binary,
        }

    return {
        "target": target, "success": False, "response": stdout,
        "error": stderr or stdout or f"Exited with code {proc.returncode}",
        "duration": duration, "path": binary,
    }


def trigger_codex(prompt=DEFAULT_PROMPT, custom_path=None, timeout=90):
    """Runs codex non-interactively to trigger the quota window."""
    codex_bin = find_codex_binary(custom_path)
    if not codex_bin:
        return {
            "target": "codex", "success": False, "response": "",
            "error": "Codex executable not found in PATH or AppData.", "duration": 0.0, "path": None,
        }

    # NOTE: --ephemeral is deliberately NOT used. It suppresses the session file,
    # which means the warm-up leaves no trace for the detector to see - the tool
    # would look permanently expired and re-warm every cooldown, forever. The
    # persisted session also carries the authoritative `rate_limits` block.
    cmd = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--",
        prompt,
    ]
    return _run_cli("codex", cmd, codex_bin, timeout)


def trigger_claude(prompt=DEFAULT_PROMPT, custom_path=None, timeout=90):
    """Runs claude code non-interactively to trigger the quota window."""
    claude_bin = find_claude_binary(custom_path)
    if not claude_bin:
        return {
            "target": "claude", "success": False, "response": "",
            "error": "Claude Code CLI not found (neither 'claude' nor 'npx' available).",
            "duration": 0.0, "path": None,
        }

    if _is_npx(claude_bin):
        cmd = [claude_bin, "-y", "@anthropic-ai/claude-code", "-p", prompt]
        # The first npx run downloads the package; give it room.
        timeout = max(timeout, 300)
    else:
        cmd = [claude_bin, "-p", prompt]

    # No shell=True here. Routing through cmd.exe made any prompt containing
    # |, >, < or ^ break (or execute) instead of being passed through verbatim.
    res = _run_cli("claude", cmd, claude_bin, timeout)
    if not res["success"] and res.get("error"):
        low = res["error"].lower()
        if "not logged in" in low or "login" in low or "authenticat" in low:
            res["error"] += " (Run 'setup_claude_login.bat' once to authenticate)"
    return res


def check_logins(codex_path=None, claude_path=None):
    """Checks and reports authentication status for both tools."""
    print("=" * 65)
    print("  AI QUOTA WARMER - AUTHENTICATION & LOGIN CHECK")
    print("=" * 65)
    print("  Note: each check sends one real 'hi' prompt and consumes quota.")
    print("-" * 65)

    print("  [1] Checking OpenAI Codex CLI Login...")
    codex_res = trigger_codex("hi", codex_path, timeout=60)
    if codex_res["success"]:
        print(f"      Status   : [LOGGED IN & READY] (Response in {codex_res['duration']}s)")
        print(f"      Path     : {codex_res['path']}")
    else:
        print("      Status   : [NOT READY / ERROR]")
        print(f"      Detail   : {codex_res['error']}")

    print("-" * 65)

    print("  [2] Checking Anthropic Claude Code CLI Login...")
    claude_res = trigger_claude("hi", claude_path, timeout=60)
    if claude_res["success"]:
        print(f"      Status   : [LOGGED IN & READY] (Response in {claude_res['duration']}s)")
        print(f"      Path     : {claude_res['path']}")
    else:
        print("      Status   : [NOT LOGGED IN / AUTH REQUIRED]")
        print(f"      Detail   : {claude_res['error']}")
        print("      Fix      : Run 'setup_claude_login.bat' or 'npx @anthropic-ai/claude-code /login'")

    print("=" * 65)
    return {"codex": codex_res, "claude": claude_res}


def run_trigger_batch(target="all", prompt=DEFAULT_PROMPT, codex_path=None, claude_path=None,
                      notify=True, quiet=False):
    """Executes triggers concurrently for the selected targets."""
    now = datetime.datetime.now()
    reset_at = now + datetime.timedelta(hours=QUOTA_WINDOW_HOURS)
    lines = []

    def out(msg):
        lines.append(msg)
        if not quiet:
            print(msg)

    out("=" * 60)
    out("  AI QUOTA WARMER - 5-HOUR LIMIT TRIGGER")
    out("=" * 60)
    out(f"  Trigger Time   : {now.strftime('%Y-%m-%d %I:%M:%S %p')}")
    out(f"  Est. Reset At  : {reset_at.strftime('%Y-%m-%d %I:%M:%S %p')} (+{int(QUOTA_WINDOW_HOURS)}h 00m)")
    out(f"  Prompt Message : '{prompt}'")
    out(f"  Target(s)      : {target.upper()}")
    out("-" * 60)

    results = {}

    # Only one batch at a time: two concurrent batches would double-ping and
    # interleave their history writes.
    with _TRIGGER_LOCK:
        threads = []
        if target in ("all", "codex"):
            threads.append(threading.Thread(
                target=lambda: results.__setitem__("codex", trigger_codex(prompt, codex_path)),
                daemon=True,
            ))
        if target in ("all", "claude"):
            threads.append(threading.Thread(
                target=lambda: results.__setitem__("claude", trigger_claude(prompt, claude_path)),
                daemon=True,
            ))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

    out("  RESULTS:")
    any_success = False
    status_summary = []

    for name in sorted(results):
        res = results[name]
        record_attempt(name, bool(res.get("success")))
        if res.get("success"):
            any_success = True
            sample = (res.get("response") or "").replace("\n", " ")[:60]
            out(f"  [+] {name.upper()}: SUCCESS in {res['duration']}s -> \"{sample}\"")
            status_summary.append(f"{name.upper()}: OK ({res['duration']}s)")
        else:
            out(f"  [-] {name.upper()}: FAILED ({res['duration']}s) -> {res.get('error')}")
            status_summary.append(f"{name.upper()}: FAILED")

    out("=" * 60)
    if any_success:
        out("  SUCCESS! Your 5-hour limit window has started.")
        out(f"  Check the real reset time with: python quota_warmer.py --status")
    else:
        out("  WARNING: No targets succeeded. Check the errors above.")
    out("=" * 60 + "\n")

    save_history_entry({
        "timestamp": now.isoformat(),
        "reset_at": reset_at.isoformat(),
        "prompt": prompt,
        "target": target,
        "results": results,
        "any_success": any_success,
    })

    if notify and status_summary:
        if any_success:
            body = f"5-hour limit started! ({', '.join(status_summary)})"
        else:
            body = f"Quota trigger failed! {', '.join(status_summary)}"
        send_windows_notification("AI Quota Warmer", body)

    return results


# ---------------------------------------------------------------------------
# Autorun installation
# ---------------------------------------------------------------------------

def _pythonw_exe() -> str:
    """Prefers pythonw.exe so background runs never flash a console window."""
    exe = Path(sys.executable)
    if _IS_WINDOWS and exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(exe)


def is_startup_installed():
    return STARTUP_VBS_FILE.exists()


def install_startup_autorun():
    """Installs a silent VBS launcher into the Windows Startup folder."""
    try:
        STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        python_exe = _pythonw_exe()
        script_path = str(Path(__file__).resolve())

        # VBScript string literals escape a quote by doubling it.
        def vbs_quote(s):
            return s.replace('"', '""')

        vbs_content = (
            'Set WshShell = CreateObject("WScript.Shell")\r\n'
            f'WshShell.Run """{vbs_quote(python_exe)}"" ""{vbs_quote(script_path)}"" --loop", 0, False\r\n'
            'Set WshShell = Nothing\r\n'
        )
        # WScript reads .vbs as ANSI unless it is UTF-16LE with a BOM; non-ASCII
        # user paths would otherwise be mangled.
        STARTUP_VBS_FILE.write_text(vbs_content, encoding="utf-16")

        print("[+] Successfully enabled Silent Auto-Run on Restart / Boot!")
        print(f"    Startup Launcher: {STARTUP_VBS_FILE}")
        print(f"    Interpreter     : {python_exe}")
        print("    -> The adaptive watcher runs silently on every login and warms each")
        print("       tool the moment its 5-hour window resets.")
        return True
    except OSError as e:
        print(f"[-] Failed to install startup auto-run: {e}")
        return False


def uninstall_startup_autorun():
    """Removes the silent launcher from the Windows Startup folder."""
    try:
        if STARTUP_VBS_FILE.exists():
            STARTUP_VBS_FILE.unlink()
            print("[+] Successfully removed Auto-Run from the Windows Startup folder.")
        else:
            print("[*] Startup auto-run was not installed.")
        return True
    except OSError as e:
        print(f"[-] Failed to remove startup launcher: {e}")
        return False


def install_scheduled_task(schedule_mode="interval", interval_hours=5, time_str="08:00"):
    """Installs a persistent background Windows Scheduled Task."""
    if not _IS_WINDOWS:
        print("[-] Scheduled tasks are a Windows-only feature.")
        return False

    python_exe = _pythonw_exe()
    script_path = str(Path(__file__).resolve())
    action_cmd = f'"{python_exe}" "{script_path}" --now'

    if schedule_mode == "startup":
        sch_arg = ["/sc", "onstart"]
    elif schedule_mode == "daily":
        try:
            hh, mm = time_str.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            print(f"[-] Invalid --task-time '{time_str}'. Expected HH:MM (e.g. 08:00).")
            return False
        sch_arg = ["/sc", "daily", "/st", time_str]
    else:
        hours = int(round(float(interval_hours)))
        if not (1 <= hours <= 23):
            print(f"[-] Invalid interval {interval_hours}h. schtasks /sc hourly accepts 1-23.")
            return False
        sch_arg = ["/sc", "hourly", "/mo", str(hours)]

    print(f"Installing Windows Scheduled Task '{TASK_NAME}'...")
    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", action_cmd] + sch_arg + ["/f"]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[-] Could not run schtasks: {e}")
        return False

    if res.returncode == 0:
        print(f"[+] Successfully installed Scheduled Task '{TASK_NAME}'!")
        print(f"    Mode  : {schedule_mode} (interval {interval_hours}h / time {time_str})")
        print(f"    Action: {action_cmd}")
        if _apply_task_catchup_settings():
            print("    Catch-up: ON (a run missed while the PC was off fires at next boot)")
        else:
            print("    Catch-up: could not be enabled; the task still runs on schedule.")
        return True

    print(f"[-] Failed to install Scheduled Task: {(res.stderr or res.stdout or '').strip()}")
    print("    Tip: '--install-startup' gives you unattended warming with zero admin rights.")
    return False


def _apply_task_catchup_settings():
    """
    Turns on StartWhenAvailable for the task schtasks just created.

    `schtasks /create` has no flag for it, and the default is OFF - meaning a
    5-hour run that came due while the PC was asleep is silently skipped rather
    than fired at the next boot. Set-ScheduledTask can flip it without admin.
    """
    ps = (
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10); "
        f"Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $s -ErrorAction Stop | Out-Null"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=_NO_WINDOW,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wait_for_network(timeout_sec=90.0, host="api.anthropic.com"):
    """
    Blocks until DNS resolves or the timeout lapses. Returns True if reachable.

    At login the watcher usually starts before networking is ready; without this
    the first warm-up fails on a DNS error and the backoff pushes the real
    attempt out by ten minutes.
    """
    import socket

    deadline = time.time() + timeout_sec
    delay = 2.0
    while time.time() < deadline:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            time.sleep(min(delay, max(0.0, deadline - time.time())))
            delay = min(delay * 1.5, 10.0)
    return False


def uninstall_scheduled_task():
    """Removes the Windows Scheduled Task."""
    if not _IS_WINDOWS:
        print("[-] Scheduled tasks are a Windows-only feature.")
        return False
    try:
        res = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[-] Could not run schtasks: {e}")
        return False

    if res.returncode == 0:
        print(f"[+] Successfully uninstalled Scheduled Task '{TASK_NAME}'.")
        return True
    print(f"[-] Task '{TASK_NAME}' not found or removal failed.")
    return False


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------

def print_status():
    """Displays the real 5-hour quota usage for Claude & Codex plus trigger history."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        from usage_detector import get_actual_usage_summary
    except ImportError as e:
        print(f"[-] Could not load usage_detector: {e}")
        return

    actual = get_actual_usage_summary()
    claude = actual["claude"]
    codex = actual["codex"]
    triggers = load_history().get("triggers", [])
    state = load_state()
    now = datetime.datetime.now()

    print("=" * 70)
    print("  AI QUOTA WARMER - ACTUAL 5-HOUR LIMIT & RATE USAGE DETECTOR")
    print("=" * 70)
    print(f"  Current Time              : {now.strftime('%Y-%m-%d %I:%M:%S %p')}")
    print(f"  Auto-Run on PC Restart    : {'[ENABLED - Runs on Boot]' if is_startup_installed() else '[DISABLED]'}")
    print("=" * 70)

    print("\n  [CLAUDE CODE] Anthropic Claude Code (5-Hour Rolling Limit)")
    print("  " + "-" * 66)
    if claude.get("has_data"):
        print(f"    Status            : [{claude['status']}]  (source: {claude.get('source')})")
        print(f"    Active Window     : {claude['window_start']} -> Resets at {claude['window_reset']}")
        print(f"    Countdown         : {claude['time_remaining']} remaining ({claude['progress_pct']}% elapsed)")
        print(f"    Prompts in 5h     : {claude['user_prompts']} user prompts ({claude['total_events']} total events)")
        tok = claude["tokens"]
        print(f"    Tokens Consumed   : {tok['total']:,} total "
              f"(In: {tok['input']:,} | Out: {tok['output']:,} | Cache: {tok['cache_read']:,})")
        if claude.get("models_used"):
            print(f"    Models Active     : {', '.join(claude['models_used'])}")
    else:
        print("    No Claude Code sessions found in the lookback window.")
    _print_backoff("claude", state)

    print("\n  [CODEX CLI] OpenAI Codex CLI (5-Hour Rolling Limit)")
    print("  " + "-" * 66)
    if codex.get("has_data"):
        print(f"    Status            : [{codex['status']}]  (source: {codex.get('source')})")
        print(f"    Active Window     : {codex['window_start']} -> Resets at {codex['window_reset']}")
        print(f"    Countdown         : {codex['time_remaining']} remaining ({codex['progress_pct']}% elapsed)")
        if codex.get("session_used_pct") is not None:
            print(f"    5h Quota Used     : {codex['session_used_pct']}%   "
                  f"(weekly: {codex.get('week_used_pct')}%)  as of {codex.get('limits_as_of')}")
        print(f"    Turns in 5h       : {codex['turns_in_5h']} turns")
        print(f"    Tokens Used       : {codex['tokens']['total']:,} tokens")
    else:
        print("    No Codex sessions found in the lookback window.")
    _print_backoff("codex", state)

    print("\n" + "=" * 70)
    print("  RECENT AUTOMATION TRIGGER HISTORY")
    print("  " + "-" * 66)
    if triggers:
        for t in reversed(triggers[-5:]):
            try:
                ts = datetime.datetime.fromisoformat(t["timestamp"]).strftime("%m/%d %I:%M %p")
            except (ValueError, KeyError, TypeError):
                ts = "??"
            res_list = [f"{k.upper()}:{'OK' if v.get('success') else 'ERR'}"
                        for k, v in (t.get("results") or {}).items()]
            print(f"    [{ts}] {', '.join(res_list) or 'no targets'}")
    else:
        print("    No trigger history recorded yet.")
    print("=" * 70 + "\n")


def _print_backoff(tool, state):
    entry = state.get(tool) or {}
    failures = int(entry.get("consecutive_failures", 0))
    remaining = cooldown_remaining(tool, state)
    if failures:
        print(f"    Warm Backoff      : {failures} consecutive failure(s); "
              f"next attempt allowed in {int(remaining // 60)}m {int(remaining % 60)}s")
    elif remaining > 0:
        print(f"    Warm Cooldown     : {int(remaining // 60)}m {int(remaining % 60)}s")


# ---------------------------------------------------------------------------
# Adaptive watcher
# ---------------------------------------------------------------------------

def warm_if_expired(tool, usage, prompt=DEFAULT_PROMPT, notify=True, log=print):
    """
    Warms one tool if its window has lapsed and its cooldown has elapsed.

    Returns True when a warm attempt was actually made.
    """
    if usage.get("is_active"):
        return False
    if usage.get("status") == "ERROR":
        # Detector failure - never warm blind.
        return False

    remaining = cooldown_remaining(tool)
    if remaining > 0:
        return False

    log(f"[{datetime.datetime.now():%I:%M:%S %p}] {tool.upper()} 5h window expired -> warming...")
    trigger = trigger_claude if tool == "claude" else trigger_codex
    res = trigger(prompt)
    entry = record_attempt(tool, bool(res.get("success")))

    if res.get("success"):
        log(f"  [+] {tool.upper()} warm-up SUCCESS ({res['duration']}s)")
        if notify:
            send_windows_notification(
                f"{tool.title()} Auto-Warmed",
                f"{tool.title()} 5-hour limit restarted (took {res['duration']}s).",
            )
    else:
        failures = entry.get("consecutive_failures", 1)
        next_try = min(BASE_COOLDOWN_SEC * (2 ** failures), MAX_BACKOFF_SEC)
        log(f"  [-] {tool.upper()} warm-up FAILED: {res.get('error')}")
        log(f"      Backing off {int(next_try // 60)}m before retry (failure #{failures}).")
        if notify and failures == 1:
            send_windows_notification(
                f"{tool.title()} Warm-Up Failed",
                str(res.get("error"))[:200],
            )
    return True


def run_smart_adaptive_monitor(check_interval_sec=60, target="all", prompt=DEFAULT_PROMPT,
                               notify=True, verbose=True):
    """
    Watches the real 5-hour windows for Claude Code and Codex and warms each one
    the moment it resets.

    Sleep length adapts to the nearest reset/cooldown so an idle machine polls
    rarely but still fires within seconds of a window opening.
    """
    from usage_detector import get_actual_usage_summary

    def log(msg):
        if verbose:
            print(msg, flush=True)

    tools = [t for t in ("claude", "codex") if target in ("all", t)]

    log("=" * 70)
    log("  AI QUOTA WARMER - SMART ADAPTIVE AUTO-WARM DAEMON")
    log("=" * 70)
    log(f"  Watching: {', '.join(t.upper() for t in tools)}")
    log(f"  Prompt  : '{prompt}'")
    log("  Warm-up fires as soon as a tool's 5-hour window resets.")
    log("  Press Ctrl+C to stop.")
    log("=" * 70 + "\n")

    lock = SingleInstanceLock()
    if not lock.acquire():
        log("[-] Another AI Quota Warmer daemon is already running. Exiting.")
        log(f"    (lock file: {LOCK_FILE})")
        return

    # Launched from the Startup folder this runs before networking settles.
    if not wait_for_network():
        log("[!] No network after 90s - continuing anyway; failures will back off.")

    try:
        while True:
            try:
                actual = get_actual_usage_summary()
                waits = []
                for tool in tools:
                    usage = actual.get(tool) or {}
                    warm_if_expired(tool, usage, prompt=prompt, notify=notify, log=log)

                    # Wake shortly after this tool's window resets, or when its
                    # cooldown lapses - whichever comes first.
                    if usage.get("is_active") and usage.get("remaining_seconds"):
                        waits.append(float(usage["remaining_seconds"]) + 5)
                    cd = cooldown_remaining(tool)
                    if cd > 0:
                        waits.append(cd + 5)

                sleep_for = min(waits) if waits else check_interval_sec
                sleep_for = max(15.0, min(sleep_for, 300.0))
            except Exception as e:
                log(f"  [!] Watcher error: {type(e).__name__}: {e}")
                sleep_for = float(check_interval_sec)

            time.sleep(sleep_for)
    except KeyboardInterrupt:
        log("\nSmart Adaptive Watcher stopped.")
    finally:
        lock.release()


def run_daemon_loop(interval_hours=5.0, target="all", prompt=DEFAULT_PROMPT, notify=True):
    """Runs the continuous smart adaptive auto-warmup monitor."""
    run_smart_adaptive_monitor(target=target, prompt=prompt, notify=notify, verbose=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="AI Quota Warmer - Start the 5-Hour Limit Unattended for Codex & Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--now", action="store_true", help="Trigger warmup immediately (default action)")
    parser.add_argument("--target", choices=["all", "codex", "claude"], default="all", help="Target tool (default: all)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help=f"Prompt to send (default: '{DEFAULT_PROMPT}')")
    parser.add_argument("--status", action="store_true", help="Show the real 5-hour window countdown and history")
    parser.add_argument("--check-login", action="store_true", help="Check login status (sends one real prompt per tool)")
    parser.add_argument("--loop", action="store_true", help="Run the adaptive background watcher")
    parser.add_argument("--interval", type=float, default=5.0, help="Interval in hours for the scheduled task (default: 5.0)")
    parser.add_argument("--no-notify", action="store_true", help="Disable Windows desktop notifications")
    parser.add_argument("--force", action="store_true", help="Ignore the per-tool cooldown for --now")
    parser.add_argument("--codex-path", default=None, help="Custom path to the codex executable")
    parser.add_argument("--claude-path", default=None, help="Custom path to the claude CLI")

    parser.add_argument("--install-startup", action="store_true", help="Enable auto-run on Windows restart/boot")
    parser.add_argument("--uninstall-startup", action="store_true", help="Disable auto-run on Windows restart/boot")
    parser.add_argument("--install-all-autorun", action="store_true", help="Enable BOTH restart auto-run AND the recurring task")

    parser.add_argument("--install-task", action="store_true", help="Register a Windows Scheduled Task")
    parser.add_argument("--task-mode", choices=["interval", "daily", "startup"], default="interval", help="Scheduled task trigger mode")
    parser.add_argument("--task-time", default="08:00", help="Daily trigger time HH:MM (with --task-mode daily)")
    parser.add_argument("--uninstall-task", action="store_true", help="Remove the Windows Scheduled Task")

    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    if args.check_login:
        check_logins(codex_path=args.codex_path, claude_path=args.claude_path)
        return
    if args.status:
        print_status()
        return
    if args.install_startup:
        install_startup_autorun()
        return
    if args.uninstall_startup:
        uninstall_startup_autorun()
        return
    if args.install_all_autorun:
        print("=== Setting up Complete Unattended Automation ===")
        install_startup_autorun()
        print()
        install_scheduled_task(schedule_mode="interval", interval_hours=5)
        return
    if args.install_task:
        install_scheduled_task(schedule_mode=args.task_mode, interval_hours=args.interval, time_str=args.task_time)
        return
    if args.uninstall_task:
        uninstall_scheduled_task()
        return
    if args.loop:
        run_smart_adaptive_monitor(target=args.target, prompt=args.prompt, notify=not args.no_notify)
        return

    # Default action: warm now. Respects the backoff unless --force is given, so
    # a Scheduled Task firing into a broken login cannot hammer the CLI.
    if not args.force:
        blocked = [t for t in ("claude", "codex")
                   if args.target in ("all", t) and cooldown_remaining(t) > 0]
        if blocked and len(blocked) == len([t for t in ("claude", "codex") if args.target in ("all", t)]):
            for t in blocked:
                remaining = cooldown_remaining(t)
                print(f"[*] {t.upper()} is in cooldown for another "
                      f"{int(remaining // 60)}m {int(remaining % 60)}s. Use --force to override.")
            return

    run_trigger_batch(
        target=args.target,
        prompt=args.prompt,
        codex_path=args.codex_path,
        claude_path=args.claude_path,
        notify=not args.no_notify,
    )


if __name__ == "__main__":
    main()
