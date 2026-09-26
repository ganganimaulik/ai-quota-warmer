#!/usr/bin/env python3
"""
AI Quota Warmer & 5-Hour Limit Starter
---------------------------------------
Triggers a minimal 'hi' prompt to OpenAI Codex CLI and Anthropic Claude Code CLI
so that your 5-hour rolling rate limit window starts unattended.

Features:
  - Automatic binary discovery for Codex CLI and Claude Code CLI.
  - Multiple accounts per tool: each login lives in its own config directory
    (CLAUDE_CONFIG_DIR / CODEX_HOME), so the official CLIs keep them apart.
  - Concurrently sends a minimal ping prompt ('hi') to every account.
  - Watches the real 5-hour windows and warms each account the moment it resets.
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
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

# Paths & Defaults
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".ai_quota_warmer"
HISTORY_FILE = DATA_DIR / "history.json"
STATE_FILE = DATA_DIR / "state.json"
LOCK_FILE = DATA_DIR / "daemon.lock"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
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
# from the atomic os.replace() in _write_json_atomic. Re-entrant because
# record_attempt holds it across its whole read-modify-write: accounts warm in
# parallel, and two threads interleaving load/save would drop one's cooldown.
_FILE_LOCK = threading.RLock()
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
    """Per-account warm state, keyed by Account.key ("claude", "codex:work", ...)."""
    state = _read_json(STATE_FILE, {})
    # Drop malformed entries so callers can always treat an entry as a dict.
    return {k: v for k, v in state.items() if isinstance(v, dict)}


def save_state(state):
    with _FILE_LOCK:
        try:
            _write_json_atomic(STATE_FILE, state)
        except OSError:
            pass


def record_attempt(key: str, success: bool, window=None):
    """
    Persists the outcome of a warm attempt so cooldowns survive restarts.

    `window` is the exact 5-hour window the CLI reported for the request
    ({"resets_at": epoch, ...}). It is kept even when the attempt failed: a
    rate-limited warm-up still says exactly when the limit lifts.
    """
    with _FILE_LOCK:
        state = load_state()
        entry = state.setdefault(key, {"last_attempt": 0.0, "last_success": 0.0, "consecutive_failures": 0})
        now_t = time.time()
        entry["last_attempt"] = now_t
        if success:
            entry["last_success"] = now_t
            entry["consecutive_failures"] = 0
        else:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        if window:
            entry["window"] = dict(window, as_of=now_t)
        save_state(state)
        return entry


def clear_backoff(key: str):
    """
    Forgets failed warm attempts, e.g. after the account was signed in again,
    so the watcher retries on its next pass instead of waiting out a backoff
    earned while the login was broken.
    """
    with _FILE_LOCK:
        state = load_state()
        entry = state.get(key)
        if not entry or not int(entry.get("consecutive_failures", 0) or 0):
            return
        entry["consecutive_failures"] = 0
        entry["last_attempt"] = 0.0
        save_state(state)


def cooldown_remaining(key: str, state=None) -> float:
    """
    Seconds left before account `key` may be warmed again.

    Failures back off exponentially (5m, 10m, 20m, ... capped at 1h) so an
    expired login cannot produce an endless stream of subprocesses.
    """
    state = state if state is not None else load_state()
    entry = state.get(key) or {}
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
# Accounts
# ---------------------------------------------------------------------------
#
# Each CLI keeps one login per config directory, chosen by an env var:
#   Claude Code : CLAUDE_CONFIG_DIR - credentials, settings and projects/
#                 transcripts (on macOS the Keychain entry is keyed by it too)
#   Codex CLI   : CODEX_HOME        - auth.json, config.toml and sessions/
# A second account is therefore just a second directory. This tool never reads
# or stores credentials: it points the official CLI at the directory and lets
# the CLI's own login flow fill it.

TOOLS = ("claude", "codex")
TOOL_TITLES = {"claude": "Claude", "codex": "Codex"}
DEFAULT_ACCOUNT = "default"
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_HOME_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}

# Credentials in the environment beat the per-directory login, which would make
# every extra account silently warm the same one. They are stripped for
# accounts that have their own directory.
_OVERRIDING_AUTH_ENV = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
               "CLAUDE_SECURESTORAGE_CONFIG_DIR"),
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"),
}


class Account:
    """One login of one tool. `config_dir` is None for the CLI's standard location."""

    def __init__(self, tool, name=DEFAULT_ACCOUNT, config_dir=None):
        self.tool = tool
        self.name = name
        self.config_dir = config_dir

    @property
    def is_default(self):
        return self.config_dir is None

    @property
    def key(self):
        # Default accounts keep the bare tool name, so state and history written
        # by single-account versions still apply to them.
        return self.tool if self.is_default else f"{self.tool}:{self.name}"

    @property
    def label(self):
        title = TOOL_TITLES[self.tool]
        return title if self.is_default else f"{title} ({self.name})"

    @property
    def home(self) -> Path:
        """The directory the CLI actually uses for this account."""
        if self.config_dir:
            return Path(self.config_dir)
        env_dir = os.getenv(_HOME_ENV[self.tool])
        return Path(env_dir).expanduser() if env_dir else Path.home() / f".{self.tool}"

    def env(self):
        """Subprocess environment selecting this login, or None to inherit ours."""
        if self.is_default:
            return None
        env = os.environ.copy()
        for var in _OVERRIDING_AUTH_ENV[self.tool]:
            env.pop(var, None)
        env[_HOME_ENV[self.tool]] = str(self.config_dir)
        return env

    def to_dict(self):
        data = {"tool": self.tool, "name": self.name}
        if self.config_dir:
            data["dir"] = self.config_dir
        return data

    def describe(self):
        """JSON-friendly view for the dashboards."""
        return {"key": self.key, "tool": self.tool, "name": self.name, "label": self.label,
                "dir": _display_path(self.home), "is_default": self.is_default}


def _display_path(path) -> str:
    """Shortens paths under the home directory to ~ for display."""
    try:
        return "~" + os.sep + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def _norm_dir(path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _account_from_dict(item):
    if not isinstance(item, dict):
        return None
    tool = str(item.get("tool") or "").lower()
    name = str(item.get("name") or DEFAULT_ACCOUNT)
    config_dir = item.get("dir") or None
    if tool not in TOOLS or not _ACCOUNT_NAME_RE.match(name):
        return None
    if config_dir is not None and not isinstance(config_dir, str):
        return None
    if name.lower() == DEFAULT_ACCOUNT:
        # "default" always means the CLI's own location.
        return Account(tool)
    if not config_dir:
        return None
    return Account(tool, name, config_dir)


def load_accounts():
    """Configured accounts in file order. Without accounts.json: one default per tool."""
    raw = _read_json(ACCOUNTS_FILE, None)
    if raw is None or not isinstance(raw.get("accounts"), list):
        return [Account(tool) for tool in TOOLS]

    accounts, seen = [], set()
    for item in raw["accounts"]:
        acc = _account_from_dict(item)
        if acc is None:
            continue
        # Two entries pointing at one directory would double-warm one login.
        ids = {("key", acc.key.lower()), ("dir", acc.tool, _norm_dir(acc.home))}
        if ids & seen:
            continue
        seen |= ids
        accounts.append(acc)
    return accounts


def save_accounts(accounts):
    with _FILE_LOCK:
        _write_json_atomic(ACCOUNTS_FILE, {"accounts": [a.to_dict() for a in accounts]})


def find_account(spec, accounts=None):
    """Resolves 'claude', 'claude:default' or 'claude:work' (case-insensitive)."""
    accounts = load_accounts() if accounts is None else accounts
    tool, _, name = str(spec or "").strip().partition(":")
    tool, name = tool.lower(), (name or DEFAULT_ACCOUNT).lower()
    for acc in accounts:
        if acc.tool == tool and acc.name.lower() == name:
            return acc
    return None


def select_accounts(target="all", specs=None, accounts=None):
    """
    Accounts matching a tool filter ('all', 'claude', 'codex') and, optionally,
    explicit account specs (repeatable and/or comma-separated).

    Raises ValueError naming the configured accounts when a spec matches none.
    """
    accounts = load_accounts() if accounts is None else accounts
    if specs:
        chosen = []
        for spec in specs:
            for part in str(spec).split(","):
                if not part.strip():
                    continue
                acc = find_account(part, accounts)
                if acc is None:
                    known = ", ".join(a.key for a in accounts) or "none"
                    raise ValueError(f"Unknown account '{part.strip()}'. Configured accounts: {known}")
                if acc not in chosen:
                    chosen.append(acc)
        accounts = chosen
    return [a for a in accounts if target in ("all", a.tool)]


def _check_new_account(acc, accounts):
    if find_account(acc.key, accounts):
        raise ValueError(f"Account '{acc.tool}:{acc.name}' already exists.")
    for other in accounts:
        if other.tool == acc.tool and _norm_dir(other.home) == _norm_dir(acc.home):
            raise ValueError(f"{other.label} already uses {other.home}.")


def prepare_account(tool, name, config_dir=None):
    """
    Validates a new account and creates its directory, without registering it.

    Without `config_dir` the account lives in ~/.claude-NAME or ~/.codex-NAME,
    which is also where you would point CLAUDE_CONFIG_DIR / CODEX_HOME to use
    the same login interactively. The name "default" restores the CLI's
    standard location. Register it with register_account() once it is signed
    in: a registered account that is not logged in yet would be picked up by
    the watcher, fail to warm and land in backoff mid-login.
    """
    tool = str(tool).lower()
    if tool not in TOOLS:
        raise ValueError(f"Unknown tool '{tool}'. Use one of: {', '.join(TOOLS)}")
    if not _ACCOUNT_NAME_RE.match(name or ""):
        raise ValueError("Account names are 1-32 characters: letters, digits, '.', '_' or '-'.")

    if name.lower() == DEFAULT_ACCOUNT:
        if config_dir:
            raise ValueError("'default' is reserved for the CLI's standard location; "
                             "choose another name to use a custom --dir.")
        acc = Account(tool)
    else:
        target_dir = config_dir or (Path.home() / f".{tool}-{name}")
        acc = Account(tool, name, os.path.abspath(os.path.expanduser(str(target_dir))))

    _check_new_account(acc, load_accounts())
    # Codex refuses to start at all when CODEX_HOME does not exist yet.
    acc.home.mkdir(parents=True, exist_ok=True)
    return acc


def register_account(acc):
    """Adds a prepared account to accounts.json, re-checking for clashes first."""
    with _FILE_LOCK:
        accounts = load_accounts()
        _check_new_account(acc, accounts)
        accounts.append(acc)
        save_accounts(accounts)
    return acc


def add_account(tool, name, config_dir=None):
    """Prepares and registers an account in one step (no sign-in)."""
    return register_account(prepare_account(tool, name, config_dir))


def remove_account(spec):
    """Unregisters an account. Its directory, and the login inside it, are kept."""
    accounts = load_accounts()
    acc = find_account(spec, accounts)
    if acc is None:
        known = ", ".join(a.key for a in accounts) or "none"
        raise ValueError(f"Unknown account '{spec}'. Configured accounts: {known}")
    save_accounts([a for a in accounts if a is not acc])
    return acc


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

def _run_cli(target, cmd, binary, timeout, env=None, parse=None):
    """
    Runs a warm-up command and normalises the result dict.

    `parse(stdout)` may turn machine-readable output into
    (response_text, error_text, extra_fields); an error_text marks the attempt
    failed even when the process exited 0.
    """
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
            env=env,
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
    response, parse_error, extra = parse(stdout) if parse else (stdout, None, {})

    if proc.returncode == 0 and not parse_error:
        res = {
            "target": target, "success": True, "response": response or "OK",
            "error": "", "duration": duration, "path": binary,
        }
    else:
        res = {
            "target": target, "success": False, "response": response,
            "error": parse_error or stderr or response or f"Exited with code {proc.returncode}",
            "duration": duration, "path": binary,
        }
    res.update(extra)
    return res


def _login_hint(account):
    if not account.is_default:
        return f"Run 'python quota_warmer.py --login {account.key}' to authenticate"
    if account.tool == "claude":
        return "Run 'setup_claude_login.bat' once to authenticate"
    return "Run 'codex login' once to authenticate"


def _add_login_hint(res, account):
    low = (res.get("error") or "").lower()
    if not res["success"] and ("not logged in" in low or "login" in low or "authenticat" in low):
        res["error"] += f" ({_login_hint(account)})"
    return res


def trigger_codex(prompt=DEFAULT_PROMPT, custom_path=None, timeout=90, account=None):
    """Runs codex non-interactively to trigger the quota window of one account."""
    account = account or Account("codex")
    codex_bin = find_codex_binary(custom_path)
    if not codex_bin:
        return {
            "target": account.key, "success": False, "response": "",
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
    res = _run_cli(account.key, cmd, codex_bin, timeout, env=account.env())
    return _add_login_hint(res, account)


def _five_hour_window(info, now=None):
    """
    Pulls the 5-hour window out of a Claude `rate_limit_info` dict, or None.

    The top-level resetsAt belongs to whichever limit is currently binding -
    possibly the weekly one - so it is only trusted when labelled five_hour
    and when it lands inside the next five hours.
    """
    now = time.time() if now is None else now
    windows = info.get("unifiedWindows")
    five = windows.get("five_hour") if isinstance(windows, dict) else None
    if isinstance(five, dict) and five.get("resetsAt"):
        resets_at, utilization = five.get("resetsAt"), five.get("utilization")
    elif info.get("rateLimitType") == "five_hour" and info.get("resetsAt"):
        resets_at, utilization = info.get("resetsAt"), info.get("utilization")
    else:
        return None
    try:
        resets_at = float(resets_at)
    except (TypeError, ValueError):
        return None
    if not (now < resets_at <= now + QUOTA_WINDOW_HOURS * 3600 + 300):
        return None
    window = {"resets_at": resets_at, "status": info.get("status")}
    if isinstance(utilization, (int, float)):
        window["utilization"] = utilization
    return window


def _parse_claude_stream(stdout):
    """
    Reads `claude -p --output-format stream-json` output.

    Besides the final `result` message, Claude Code emits `rate_limit_event`
    messages carrying the account's limit state straight from the API
    response headers - the exact 5-hour reset, instead of one inferred from
    transcripts.
    """
    result, window, plain = None, None, []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line) if line.startswith("{") else None
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            plain.append(line)
        elif msg.get("type") == "result":
            result = msg
        elif msg.get("type") == "rate_limit_event" and isinstance(msg.get("rate_limit_info"), dict):
            window = _five_hour_window(msg["rate_limit_info"]) or window

    extra = {"window": window} if window else {}
    if result is None:
        # Not stream-json (an error printed before streaming began, or an old
        # CLI): keep whatever plain text it wrote.
        return "\n".join(plain), None, extra
    text = str(result.get("result") or "").strip()
    if result.get("is_error"):
        return "", text or f"Claude reported an error ({result.get('subtype') or 'unknown'})", extra
    return text, None, extra


def trigger_claude(prompt=DEFAULT_PROMPT, custom_path=None, timeout=90, account=None):
    """Runs claude code non-interactively to trigger the quota window of one account."""
    account = account or Account("claude")
    claude_bin = find_claude_binary(custom_path)
    if not claude_bin:
        return {
            "target": account.key, "success": False, "response": "",
            "error": "Claude Code CLI not found (neither 'claude' nor 'npx' available).",
            "duration": 0.0, "path": None,
        }

    # stream-json (which needs --verbose alongside -p) is what exposes the
    # rate_limit_event messages; the request sent is the same as plain -p.
    args = ["-p", prompt, "--output-format", "stream-json", "--verbose"]
    if _is_npx(claude_bin):
        cmd = [claude_bin, "-y", "@anthropic-ai/claude-code"] + args
        # The first npx run downloads the package; give it room.
        timeout = max(timeout, 300)
    else:
        cmd = [claude_bin] + args

    # No shell=True here. Routing through cmd.exe made any prompt containing
    # |, >, < or ^ break (or execute) instead of being passed through verbatim.
    res = _run_cli(account.key, cmd, claude_bin, timeout, env=account.env(), parse=_parse_claude_stream)
    return _add_login_hint(res, account)


# ---------------------------------------------------------------------------
# Login status & login (free - no prompt is ever sent)
# ---------------------------------------------------------------------------

def _run_quiet(cmd, env=None, timeout=60):
    """Runs a short informational command; returns the CompletedProcess or None."""
    try:
        return subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env, creationflags=_NO_WINDOW,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _parse_json_object(text):
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def codex_app_server_request(binary, env=None, calls=(), timeout=30.0):
    """
    Calls read-only methods on `codex app-server` (JSON-RPC over stdio) and
    returns {method: reply}, where reply holds "result" or "error".

    This is the interface Codex's own IDE extension uses; methods such as
    `account/read` and `account/rateLimits/read` send no prompt and use no
    quota. Returns what it got so far if the server is missing or stalls.
    """
    try:
        proc = subprocess.Popen(
            [binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            env=env, creationflags=_NO_WINDOW,
        )
    except (OSError, ValueError):
        return {}

    lines = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + timeout

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def call(msg_id, method, params):
        send({"id": msg_id, "method": method, "params": params})
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                return None
            if line is None:  # server exited
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            # Skip notifications (configWarning, ...) the server emits unprompted.
            if isinstance(msg, dict) and msg.get("id") == msg_id and ("result" in msg or "error" in msg):
                return msg

    replies = {}
    try:
        init = call(1, "initialize", {
            "clientInfo": {"name": "ai_quota_warmer", "title": "AI Quota Warmer", "version": "1.0"},
            "capabilities": None,
        })
        if init and "result" in init:
            send({"method": "initialized"})
            for msg_id, (method, params) in enumerate(calls, start=2):
                reply = call(msg_id, method, params)
                if reply is None:
                    break
                replies[method] = reply
    except (OSError, ValueError):
        pass
    finally:
        # Closing stdin is the app-server's shutdown signal; on Windows that also
        # reaches the real codex.exe behind an npm codex.cmd wrapper.
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return replies


def _claude_login_status(account, custom_path=None):
    claude_bin = find_claude_binary(custom_path)
    if not claude_bin:
        return {"logged_in": None, "detail": "Claude Code CLI not found."}
    prefix = [claude_bin, "-y", "@anthropic-ai/claude-code"] if _is_npx(claude_bin) else [claude_bin]
    proc = _run_quiet(prefix + ["auth", "status", "--json"], env=account.env(),
                      timeout=300 if _is_npx(claude_bin) else 60)
    data = _parse_json_object(proc.stdout) if proc else None
    if data is None or "loggedIn" not in data:
        return {"logged_in": None,
                "detail": "Could not read 'claude auth status' - update Claude Code for a free login check."}

    info = {
        "logged_in": bool(data.get("loggedIn")),
        "email": data.get("email"),
        "org": data.get("orgName") or data.get("orgId"),
        "plan": data.get("subscriptionType"),
        "method": data.get("authMethod"),
    }
    if data.get("apiKeySource"):
        info["warning"] = (f"An API key is configured ({data['apiKeySource']}): warm-ups may be billed "
                           "to the API instead of starting the subscription's 5-hour window.")
    return info


def _codex_login_status(account, custom_path=None):
    codex_bin = find_codex_binary(custom_path)
    if not codex_bin:
        return {"logged_in": None, "detail": "Codex executable not found."}
    if not account.home.is_dir():
        return {"logged_in": False, "detail": f"{_display_path(account.home)} does not exist yet."}

    reply = codex_app_server_request(codex_bin, account.env(), [("account/read", {})]).get("account/read")
    if reply and isinstance(reply.get("result"), dict):
        acct = reply["result"].get("account")
        if not isinstance(acct, dict):
            return {"logged_in": False, "detail": "Not logged in"}
        info = {"logged_in": True, "email": acct.get("email"), "plan": acct.get("planType"),
                "method": acct.get("type")}
        if acct.get("type") == "apiKey":
            info["warning"] = ("Logged in with an API key: usage is billed per token and there is "
                               "no 5-hour window to start.")
        return info

    # Codex builds without the app-server only say how, not who.
    proc = _run_quiet([codex_bin, "login", "status"], env=account.env())
    if proc is None:
        return {"logged_in": None, "detail": "Could not run 'codex login status'."}
    lines = [l.strip() for l in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()
             if l.strip() and not l.startswith("WARNING")]
    return {"logged_in": proc.returncode == 0, "detail": lines[-1] if lines else ""}


def account_login_status(account, claude_path=None, codex_path=None):
    """
    Asks the account's CLI who it is signed in as. Free: no prompt is sent.

    Returns {"logged_in": True/False/None (unknown), "email", "org", "plan",
    "method", "detail", "warning"} with only the fields that are known.
    """
    try:
        if account.tool == "claude":
            return _claude_login_status(account, claude_path)
        return _codex_login_status(account, codex_path)
    except Exception as e:  # a status probe must never take down a UI or the watcher
        return {"logged_in": None, "detail": f"{type(e).__name__}: {e}"}


def describe_login(status):
    """One-line summary of an account_login_status() result."""
    if status.get("logged_in") is None:
        return f"UNKNOWN - {status.get('detail') or 'no answer'}"
    if not status.get("logged_in"):
        return f"NOT LOGGED IN{' - ' + status['detail'] if status.get('detail') else ''}"
    who = " ".join(str(v) for v in (status.get("email"), status.get("org")) if v)
    extras = ", ".join(str(v) for v in (status.get("plan"), status.get("method")) if v)
    text = "LOGGED IN" + (f" as {who}" if who else "") + (f" ({extras})" if extras else "")
    if not who and not extras and status.get("detail"):
        text += f" - {status['detail']}"
    return text


def _claude_login_cmd(claude_bin, env):
    if _is_npx(claude_bin):
        return [claude_bin, "-y", "@anthropic-ai/claude-code", "auth", "login"]
    probe = _run_quiet([claude_bin, "auth", "--help"], env=env)
    if probe and "claude auth" in (probe.stdout or ""):
        return [claude_bin, "auth", "login"]
    # Releases without `claude auth` log in through the interactive /login.
    return [claude_bin, "/login"]


def _login_command(account, claude_path=None, codex_path=None):
    """The CLI's own login command for this account, as (cmd, None) or (None, reason)."""
    if account.tool == "claude":
        binary = find_claude_binary(claude_path)
        if not binary:
            return None, "Claude Code CLI not found - install it first."
        return _claude_login_cmd(binary, account.env()), None
    binary = find_codex_binary(codex_path)
    if not binary:
        return None, "Codex CLI not found - install it first."
    return [binary, "login"], None


def login_account(account, claude_path=None, codex_path=None):
    """
    Runs the CLI's own interactive login with this account's directory selected.

    The CLI opens the browser and stores the result in the account directory;
    this tool never sees the credentials.
    """
    account.home.mkdir(parents=True, exist_ok=True)
    cmd, problem = _login_command(account, claude_path, codex_path)
    if not cmd:
        print(f"[-] {problem}")
        return False

    print(f"[*] Opening the {account.label} sign-in for {account.home}")
    print("    Your browser opens on its own. To sign in with a different account than")
    print("    the one it is already signed in to, copy the link printed below into a")
    if account.tool == "claude":
        print("    private/incognito window instead, then paste the code it shows here.")
    else:
        print("    private/incognito window instead.")
    if cmd[-1] == "/login":
        print("    Close the Claude session (/exit) once it says you are logged in.")
    try:
        # Inherits this console so the CLI can show its prompts and URL.
        rc = subprocess.call(cmd, env=account.env())
    except OSError as e:
        print(f"[-] Could not start the login: {e}")
        return False
    return rc == 0


# ---------------------------------------------------------------------------
# Background sign-in (driven by the dashboard and the desktop app)
# ---------------------------------------------------------------------------

LOGIN_TIMEOUT_SEC = 900
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SIGNIN_URL_RE = re.compile(r"https://\S*authorize\S*")
_PASTE_PROMPT = "Paste code here if prompted >"


def _kill_tree(proc):
    """Stops a CLI and whatever it spawned: npm wrappers run the real binary as a child."""
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           timeout=15, creationflags=_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class LoginJob:
    """
    One account's CLI sign-in, run in the background so a UI can drive it.

    The CLI still opens the default browser itself. The job also captures the
    sign-in link the CLI prints: opening that link in a private window is the
    easy way to sign in with a different account than the one the browser is
    already signed in to. Codex's link completes on its own through its local
    callback; Claude's ends on a page showing a code, which submit_code()
    passes to the waiting CLI. With `register`, the account is added to
    accounts.json only once the sign-in succeeded.
    """

    def __init__(self, account, register=False, claude_path=None, codex_path=None):
        self.account = account
        self.register = register
        self._paths = (claude_path, codex_path)
        self._lock = threading.Lock()
        self._proc = None
        self.state = "starting"          # -> running -> succeeded | failed | cancelled
        self.url = None
        self.detail = ""
        self.login = None
        self.output = deque(maxlen=30)
        self.started_at = time.time()
        self.finished_at = None

    @property
    def active(self):
        return self.state in ("starting", "running")

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def snapshot(self):
        """Thread-safe, JSON-friendly view for the UIs."""
        with self._lock:
            return {
                "key": self.account.key, "label": self.account.label, "tool": self.account.tool,
                "dir": _display_path(self.account.home), "register": self.register,
                "state": self.state, "active": self.active, "url": self.url,
                "accepts_code": self.account.tool == "claude", "detail": self.detail,
                "login": self.login, "output": list(self.output),
                "started_at": self.started_at, "finished_at": self.finished_at,
            }

    def submit_code(self, code):
        """Hands the code from Claude's sign-in page to the waiting CLI."""
        code = str(code or "").strip()
        if not code or len(code) > 4000 or any(c in code for c in "\r\n\x00"):
            raise ValueError("That doesn't look like a sign-in code.")
        with self._lock:
            proc = self._proc if self.state == "running" else None
        if proc is None or self.account.tool != "claude":
            raise ValueError("No sign-in is waiting for a code.")
        try:
            proc.stdin.write(code + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            raise ValueError("The sign-in is no longer accepting a code.")

    def cancel(self):
        with self._lock:
            if not self.active:
                return False
            self.state, self.detail, self.finished_at = "cancelled", "Sign-in cancelled.", time.time()
            proc = self._proc
        if proc is not None:
            _kill_tree(proc)
        return True

    def _end(self, state, detail, login=None):
        with self._lock:
            if self.state == "cancelled":
                return
            self.state, self.detail, self.login, self.finished_at = state, detail, login, time.time()

    def _run(self):
        acc = self.account
        try:
            acc.home.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return self._end("failed", f"Could not create {acc.home}: {e}")
        if self.register:
            # Re-adding a removed account: its folder still holds a valid login.
            status = account_login_status(acc, *self._paths)
            if status.get("logged_in") is True:
                with self._lock:
                    if self.state == "cancelled":
                        return
                try:
                    register_account(acc)
                except ValueError as e:
                    return self._end("failed", str(e), status)
                return self._end("succeeded", f"Already {describe_login(status)}", status)
        cmd, problem = _login_command(acc, *self._paths)
        if not cmd:
            return self._end("failed", problem)
        if cmd[-1] == "/login":
            return self._end("failed", "This Claude Code has no 'claude auth login' - update it "
                                       "('claude update') or use add_account.bat.")
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=acc.env(),
                creationflags=_NO_WINDOW, start_new_session=not _IS_WINDOWS,
            )
        except (OSError, ValueError) as e:
            return self._end("failed", f"Could not start the sign-in: {e}")
        with self._lock:
            cancelled = self.state == "cancelled"
            if not cancelled:
                self._proc, self.state = proc, "running"
        if cancelled:
            return _kill_tree(proc)

        reader = threading.Thread(target=self._read, args=(proc,), daemon=True)
        reader.start()
        try:
            rc = proc.wait(timeout=LOGIN_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            rc = None
        # The CLI's last words (often the error) may still be in the pipe.
        reader.join(timeout=5)
        self._finish(rc)

    def _read(self, proc):
        try:
            for raw in proc.stdout:
                line = _ANSI_RE.sub("", raw).replace(_PASTE_PROMPT, "").strip()
                if not line:
                    continue
                with self._lock:
                    self.output.append(line)
                    match = _SIGNIN_URL_RE.search(line)
                    if match and self.url is None:
                        self.url = match.group(0)
        except (OSError, ValueError):
            pass

    def _finish(self, rc):
        with self._lock:
            if self.state == "cancelled":
                return
        status = account_login_status(self.account, *self._paths)
        # An unknown status (old CLI) falls back to trusting the exit code.
        if status.get("logged_in") is True or (status.get("logged_in") is None and rc == 0):
            if self.register:
                try:
                    register_account(self.account)
                except ValueError as e:
                    return self._end("failed", str(e), status)
            clear_backoff(self.account.key)
            return self._end("succeeded", describe_login(status), status)
        if rc is None:
            reason = "Timed out waiting for the sign-in to finish."
        else:
            with self._lock:
                last = next((l for l in reversed(self.output) if not _SIGNIN_URL_RE.search(l)), "")
            reason = last or "The sign-in did not complete."
        self._end("failed", reason, status)


_LOGIN_JOB = None
_LOGIN_JOB_LOCK = threading.Lock()


def start_login_job(account, register=False, claude_path=None, codex_path=None):
    """Starts a background sign-in; one at a time, since Codex's callback port is fixed."""
    global _LOGIN_JOB
    with _LOGIN_JOB_LOCK:
        if _LOGIN_JOB is not None and _LOGIN_JOB.active:
            raise ValueError(f"A sign-in for {_LOGIN_JOB.account.label} is already in progress.")
        _LOGIN_JOB = LoginJob(account, register, claude_path, codex_path).start()
        return _LOGIN_JOB


def current_login_job():
    return _LOGIN_JOB


def dismiss_login_job():
    """Forgets a finished sign-in so the UIs stop showing it."""
    global _LOGIN_JOB
    with _LOGIN_JOB_LOCK:
        if _LOGIN_JOB is not None and not _LOGIN_JOB.active:
            _LOGIN_JOB = None


def check_logins(codex_path=None, claude_path=None, accounts=None):
    """Checks and reports who every account is logged in as (no prompt is sent)."""
    accounts = load_accounts() if accounts is None else accounts
    print("=" * 70)
    print("  AI QUOTA WARMER - ACCOUNTS & LOGIN CHECK")
    print("=" * 70)
    print("  Free check: each CLI is asked who it is signed in as. No prompt is sent.")

    results, identities = {}, {}
    for acc in accounts:
        status = account_login_status(acc, claude_path, codex_path)
        results[acc.key] = status
        print("-" * 70)
        print(f"  [{acc.key}] {acc.label}")
        print(f"      Directory : {_display_path(acc.home)}")
        print(f"      Login     : {describe_login(status)}")
        if status.get("warning"):
            print(f"      Warning   : {status['warning']}")
        if status.get("logged_in") is False:
            print(f"      Fix       : {_login_hint(acc)}")
        if status.get("logged_in") and status.get("email"):
            ident = (acc.tool, str(status["email"]).lower(), str(status.get("org") or status.get("plan") or ""))
            identities.setdefault(ident, []).append(acc.key)

    for keys in identities.values():
        if len(keys) > 1:
            print("-" * 70)
            print(f"  [!] {', '.join(keys)} look like the same login (same email and org/plan).")
            print("      If so they share one 5-hour window and warming both is redundant -")
            print("      re-login one of them with the other account, or remove it.")

    print("=" * 70)
    print("  Add     : python quota_warmer.py --add-account claude work")
    print("  Re-login: python quota_warmer.py --login claude:work")
    print("  Remove  : python quota_warmer.py --remove-account claude:work")
    print("=" * 70)
    return results


def trigger_account(account, prompt=DEFAULT_PROMPT, codex_path=None, claude_path=None, timeout=90):
    """Sends one warm-up prompt through the CLI of `account`."""
    if account.tool == "claude":
        return trigger_claude(prompt, claude_path, timeout=timeout, account=account)
    return trigger_codex(prompt, codex_path, timeout=timeout, account=account)


def _fmt_epoch(epoch):
    return datetime.datetime.fromtimestamp(float(epoch)).strftime("%I:%M:%S %p")


def run_trigger_batch(target="all", prompt=DEFAULT_PROMPT, codex_path=None, claude_path=None,
                      notify=True, quiet=False, accounts=None):
    """Executes triggers concurrently for `accounts` (default: every account matching `target`)."""
    accounts = select_accounts(target) if accounts is None else accounts
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
    out(f"  Account(s)     : {', '.join(a.key for a in accounts) or 'none'}")
    out("-" * 60)

    if not accounts:
        out("  No accounts match. See: python quota_warmer.py --list-accounts")
        out("=" * 60 + "\n")
        return {}

    results = {}

    # Only one batch at a time: two concurrent batches would double-ping and
    # interleave their history writes.
    with _TRIGGER_LOCK:
        threads = [
            threading.Thread(
                target=lambda a=acc: results.__setitem__(a.key, trigger_account(a, prompt, codex_path, claude_path)),
                daemon=True,
            )
            for acc in accounts
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # Threads finish in any order; report and store in account order.
    results = {a.key: results[a.key] for a in accounts if a.key in results}

    out("  RESULTS:")
    any_success = False
    status_summary = []

    for acc in accounts:
        res = results.get(acc.key)
        if res is None:
            continue
        name = acc.label.upper()
        record_attempt(acc.key, bool(res.get("success")), window=res.get("window"))
        reset_note = f" | resets {_fmt_epoch(res['window']['resets_at'])}" if res.get("window") else ""
        if res.get("success"):
            any_success = True
            sample = (res.get("response") or "").replace("\n", " ")[:60]
            out(f"  [+] {name}: SUCCESS in {res['duration']}s -> \"{sample}\"{reset_note}")
            status_summary.append(f"{acc.label}: OK ({res['duration']}s)")
        else:
            out(f"  [-] {name}: FAILED ({res['duration']}s) -> {res.get('error')}{reset_note}")
            status_summary.append(f"{acc.label}: FAILED")

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

def get_all_usage(accounts=None, state=None):
    """
    Local 5-hour window readings for every account, keyed by Account.key.

    Claude readings are anchored on the exact reset the last warm-up reported
    (kept in state.json), so they stop being guesses once a warm-up has run.
    """
    from usage_detector import get_account_usage

    accounts = load_accounts() if accounts is None else accounts
    state = load_state() if state is None else state
    return {
        acc.key: get_account_usage(
            acc.tool, acc.home,
            exact=(state.get(acc.key) or {}).get("window"),
            # The opt-in `claude -p /cost` probe is billed; never multiply it per account.
            allow_live_cli=acc.is_default,
        )
        for acc in accounts
    }


def print_status(accounts=None):
    """Displays the real 5-hour quota usage for every account plus trigger history."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    accounts = load_accounts() if accounts is None else accounts
    state = load_state()
    try:
        usage = get_all_usage(accounts, state)
    except ImportError as e:
        print(f"[-] Could not load usage_detector: {e}")
        return
    triggers = load_history().get("triggers", [])
    now = datetime.datetime.now()

    print("=" * 70)
    print("  AI QUOTA WARMER - ACTUAL 5-HOUR LIMIT & RATE USAGE DETECTOR")
    print("=" * 70)
    print(f"  Current Time              : {now.strftime('%Y-%m-%d %I:%M:%S %p')}")
    print(f"  Auto-Run on PC Restart    : {'[ENABLED - Runs on Boot]' if is_startup_installed() else '[DISABLED]'}")
    print(f"  Accounts                  : {', '.join(a.key for a in accounts) or 'none'}")
    print("=" * 70)

    for acc in accounts:
        _print_account_usage(acc, usage[acc.key], state)
    if not accounts:
        print("\n  No accounts configured. Add one with: python quota_warmer.py --add-account claude work")

    print("\n" + "=" * 70)
    print("  RECENT AUTOMATION TRIGGER HISTORY")
    print("  " + "-" * 66)
    if triggers:
        for t in reversed(triggers[-5:]):
            try:
                ts = datetime.datetime.fromisoformat(t["timestamp"]).strftime("%m/%d %I:%M %p")
            except (ValueError, KeyError, TypeError):
                ts = "??"
            res_list = [f"{k.upper()} {'OK' if v.get('success') else 'ERR'}"
                        for k, v in (t.get("results") or {}).items()]
            print(f"    [{ts}] {', '.join(res_list) or 'no targets'}")
    else:
        print("    No trigger history recorded yet.")
    print("=" * 70 + "\n")


def _print_account_usage(acc, data, state):
    title = "CLAUDE CODE" if acc.tool == "claude" else "CODEX CLI"
    print(f"\n  [{title}] {acc.label} - {_display_path(acc.home)}")
    print("  " + "-" * 66)
    if data.get("status") == "ERROR":
        print(f"    Status            : [ERROR] {data.get('error')}")
    elif not data.get("has_data"):
        print(f"    No {acc.label} sessions found in the lookback window.")
    elif acc.tool == "claude":
        print(f"    Status            : [{data['status']}]  (source: {data.get('source')})")
        print(f"    Active Window     : {data['window_start']} -> Resets at {data['window_reset']}")
        print(f"    Countdown         : {data['time_remaining']} remaining ({data['progress_pct']}% elapsed)")
        print(f"    Prompts in 5h     : {data['user_prompts']} user prompts ({data['total_events']} total events)")
        tok = data["tokens"]
        print(f"    Tokens Consumed   : {tok['total']:,} total "
              f"(In: {tok['input']:,} | Out: {tok['output']:,} | Cache: {tok['cache_read']:,})")
        if data.get("models_used"):
            print(f"    Models Active     : {', '.join(data['models_used'])}")
    else:
        print(f"    Status            : [{data['status']}]  (source: {data.get('source')})")
        print(f"    Active Window     : {data['window_start']} -> Resets at {data['window_reset']}")
        print(f"    Countdown         : {data['time_remaining']} remaining ({data['progress_pct']}% elapsed)")
        if data.get("session_used_pct") is not None:
            print(f"    5h Quota Used     : {data['session_used_pct']}%   "
                  f"(weekly: {data.get('week_used_pct')}%)  as of {data.get('limits_as_of')}")
        print(f"    Turns in 5h       : {data['turns_in_5h']} turns")
        print(f"    Tokens Used       : {data['tokens']['total']:,} tokens")
    _print_backoff(acc.key, state)


def _print_backoff(key, state):
    entry = state.get(key) or {}
    failures = int(entry.get("consecutive_failures", 0))
    remaining = cooldown_remaining(key, state)
    if failures:
        print(f"    Warm Backoff      : {failures} consecutive failure(s); "
              f"next attempt allowed in {int(remaining // 60)}m {int(remaining % 60)}s")
    elif remaining > 0:
        print(f"    Warm Cooldown     : {int(remaining // 60)}m {int(remaining % 60)}s")


# ---------------------------------------------------------------------------
# Adaptive watcher
# ---------------------------------------------------------------------------

def warm_if_expired(account, usage, prompt=DEFAULT_PROMPT, notify=True, log=print):
    """
    Warms one account if its window has lapsed and its cooldown has elapsed.

    Returns True when a warm attempt was actually made.
    """
    if isinstance(account, str):
        # Older callers passed a bare tool name ("claude" / "codex").
        account = find_account(account) or Account(account)
    if usage.get("is_active"):
        return False
    if usage.get("status") == "ERROR":
        # Detector failure - never warm blind.
        return False

    remaining = cooldown_remaining(account.key)
    if remaining > 0:
        return False

    log(f"[{datetime.datetime.now():%I:%M:%S %p}] {account.label} 5h window expired -> warming...")
    res = trigger_account(account, prompt)
    entry = record_attempt(account.key, bool(res.get("success")), window=res.get("window"))

    if res.get("success"):
        reset_note = f", resets {_fmt_epoch(res['window']['resets_at'])}" if res.get("window") else ""
        log(f"  [+] {account.label} warm-up SUCCESS ({res['duration']}s{reset_note})")
        if notify:
            send_windows_notification(
                f"{account.label} Auto-Warmed",
                f"{account.label} 5-hour limit restarted (took {res['duration']}s).",
            )
    else:
        failures = entry.get("consecutive_failures", 1)
        next_try = min(BASE_COOLDOWN_SEC * (2 ** failures), MAX_BACKOFF_SEC)
        log(f"  [-] {account.label} warm-up FAILED: {res.get('error')}")
        log(f"      Backing off {int(next_try // 60)}m before retry (failure #{failures}).")
        if notify and failures == 1:
            send_windows_notification(
                f"{account.label} Warm-Up Failed",
                str(res.get("error"))[:200],
            )
    return True


def warm_cycle(accounts, prompt=DEFAULT_PROMPT, notify=True, log=print, idle_sleep=60.0):
    """
    One pass of the adaptive watcher: warms every account whose window has
    lapsed (in parallel, so the last of several accounts is not left waiting)
    and returns how long to sleep before the next pass.

    The sleep ends shortly after the nearest reset or cooldown expiry, so an
    idle machine polls rarely but still fires within seconds of a window opening.
    """
    usage = get_all_usage(accounts)
    due = [a for a in accounts
           if not usage[a.key].get("is_active") and usage[a.key].get("status") != "ERROR"]
    threads = [
        threading.Thread(target=warm_if_expired, args=(a, usage[a.key]),
                         kwargs={"prompt": prompt, "notify": notify, "log": log}, daemon=True)
        for a in due
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = load_state()
    waits = []
    for acc in accounts:
        u = usage[acc.key]
        # Wake shortly after this account's window resets, or when its cooldown
        # lapses - whichever comes first.
        if u.get("is_active") and u.get("remaining_seconds"):
            waits.append(float(u["remaining_seconds"]) + 5)
        cd = cooldown_remaining(acc.key, state)
        if cd > 0:
            waits.append(cd + 5)

    sleep_for = min(waits) if waits else float(idle_sleep)
    return max(15.0, min(sleep_for, 300.0))


def _watch_stamp():
    stamps = []
    for path in (ACCOUNTS_FILE, STATE_FILE):
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            stamps.append(None)
    return tuple(stamps)


def sleep_until_next_cycle(seconds, step=15.0):
    """
    Sleeps between watcher passes, but wakes early when accounts.json or
    state.json changes: an account added, removed or signed in again from
    another window is acted on within seconds rather than after the current
    (up to 5 minute) sleep.
    """
    stamp = _watch_stamp()
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))
        if _watch_stamp() != stamp:
            return


def run_smart_adaptive_monitor(check_interval_sec=60, target="all", prompt=DEFAULT_PROMPT,
                               notify=True, verbose=True, account_specs=None):
    """
    Watches the real 5-hour window of every account and warms each one the
    moment it resets.

    The account list is re-read on every pass, so accounts added or removed
    while the watcher runs take effect without a restart.
    """
    def log(msg):
        if verbose:
            print(msg, flush=True)

    accounts = select_accounts(target, account_specs)

    log("=" * 70)
    log("  AI QUOTA WARMER - SMART ADAPTIVE AUTO-WARM DAEMON")
    log("=" * 70)
    log(f"  Watching: {', '.join(a.label for a in accounts) or 'no accounts yet'}")
    log(f"  Prompt  : '{prompt}'")
    log("  Warm-up fires as soon as an account's 5-hour window resets.")
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

    watching = [a.key for a in accounts]
    try:
        while True:
            try:
                accounts = select_accounts(target, account_specs)
                if [a.key for a in accounts] != watching:
                    watching = [a.key for a in accounts]
                    log(f"[*] Now watching: {', '.join(a.label for a in accounts) or 'no accounts'}")
                sleep_for = warm_cycle(accounts, prompt=prompt, notify=notify, log=log,
                                       idle_sleep=check_interval_sec)
            except Exception as e:
                log(f"  [!] Watcher error: {type(e).__name__}: {e}")
                sleep_for = float(check_interval_sec)

            sleep_until_next_cycle(sleep_for)
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
    parser.add_argument("--target", choices=["all", "codex", "claude"], default="all",
                        help="Limit to one tool's accounts (default: all)")
    parser.add_argument("--account", action="append", metavar="ACCOUNT",
                        help="Limit to specific account(s): claude, codex, claude:NAME, codex:NAME "
                             "(repeatable or comma-separated)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help=f"Prompt to send (default: '{DEFAULT_PROMPT}')")
    parser.add_argument("--status", action="store_true", help="Show the real 5-hour window countdown and history")
    parser.add_argument("--check-login", "--list-accounts", dest="check_login", action="store_true",
                        help="List accounts and who each is logged in as (free - no prompt is sent)")
    parser.add_argument("--loop", action="store_true", help="Run the adaptive background watcher")
    parser.add_argument("--interval", type=float, default=5.0, help="Interval in hours for the scheduled task (default: 5.0)")
    parser.add_argument("--no-notify", action="store_true", help="Disable Windows desktop notifications")
    parser.add_argument("--force", action="store_true", help="Ignore the per-account cooldown for --now")
    parser.add_argument("--codex-path", default=None, help="Custom path to the codex executable")
    parser.add_argument("--claude-path", default=None, help="Custom path to the claude CLI")

    parser.add_argument("--add-account", nargs=2, metavar=("TOOL", "NAME"),
                        help="Add an account (TOOL: claude or codex) and open its browser login")
    parser.add_argument("--dir", default=None,
                        help="Config directory for --add-account (default: ~/.claude-NAME or ~/.codex-NAME)")
    parser.add_argument("--login", metavar="ACCOUNT", help="Re-run the browser login for an account")
    parser.add_argument("--remove-account", metavar="ACCOUNT",
                        help="Stop warming an account (its directory and login are kept)")

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

    if args.add_account:
        _cli_add_account(args)
        return
    if args.remove_account:
        _cli_remove_account(args.remove_account)
        return
    if args.login:
        acc = find_account(args.login)
        if acc is None:
            print(f"[-] Unknown account '{args.login}'. See: python quota_warmer.py --list-accounts")
            sys.exit(1)
        login_account(acc, args.claude_path, args.codex_path)
        status = account_login_status(acc, args.claude_path, args.codex_path)
        if status.get("logged_in"):
            clear_backoff(acc.key)
        print(f"[{'+' if status.get('logged_in') else '-'}] {acc.label}: {describe_login(status)}")
        return

    try:
        selected = select_accounts(args.target, args.account)
    except ValueError as e:
        parser.error(str(e))

    if args.check_login:
        check_logins(codex_path=args.codex_path, claude_path=args.claude_path, accounts=selected)
        return
    if args.status:
        print_status(selected)
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
        run_smart_adaptive_monitor(target=args.target, prompt=args.prompt, notify=not args.no_notify,
                                   account_specs=args.account)
        return

    # Default action: warm now. Respects the backoff unless --force is given, so
    # a Scheduled Task firing into a broken login cannot hammer the CLI.
    if not args.force:
        state = load_state()
        blocked = [a for a in selected if cooldown_remaining(a.key, state) > 0]
        for a in blocked:
            remaining = cooldown_remaining(a.key, state)
            print(f"[*] {a.label} is in cooldown for another "
                  f"{int(remaining // 60)}m {int(remaining % 60)}s. Use --force to override.")
        selected = [a for a in selected if a not in blocked]
        if blocked and not selected:
            return

    run_trigger_batch(
        target=args.target,
        prompt=args.prompt,
        codex_path=args.codex_path,
        claude_path=args.claude_path,
        notify=not args.no_notify,
        accounts=selected,
    )


def _cli_add_account(args):
    tool, name = args.add_account
    try:
        acc = prepare_account(tool, name, args.dir)
    except (ValueError, OSError) as e:
        print(f"[-] {e}")
        sys.exit(1)
    print(f"[*] Setting up {acc.label} as '{acc.key}' in {acc.home}")

    status = account_login_status(acc, args.claude_path, args.codex_path)
    if not status.get("logged_in"):
        login_account(acc, args.claude_path, args.codex_path)
        status = account_login_status(acc, args.claude_path, args.codex_path)
    print(f"    Login  : {describe_login(status)}")
    if status.get("warning"):
        print(f"    Warning: {status['warning']}")
    # Registering a signed-out account would only make the watcher fail on it.
    if status.get("logged_in") is False:
        retry = f"python quota_warmer.py --add-account {acc.tool} {acc.name}"
        print("[-] The sign-in did not complete, so the account was not added.")
        print(f"    Try again with: {retry}" + (f' --dir "{acc.home}"' if args.dir else ""))
        sys.exit(1)
    try:
        register_account(acc)
    except ValueError as e:
        print(f"[-] {e}")
        sys.exit(1)
    print(f"[+] Added {acc.label} as '{acc.key}'.")
    print("    A running watcher starts warming it within ~15 seconds - no restart needed.")
    if not acc.is_default:
        var = _HOME_ENV[acc.tool]
        print(f"    To use this login yourself, set {var}={acc.home} before starting {acc.tool}.")


def _cli_remove_account(spec):
    try:
        acc = remove_account(spec)
    except ValueError as e:
        print(f"[-] {e}")
        sys.exit(1)
    print(f"[+] Removed {acc.label} ('{acc.key}'). It will no longer be warmed.")
    if not acc.is_default:
        print(f"    Its login is still stored in {acc.home} - delete that folder to discard it,")
        print(f"    or re-add it: python quota_warmer.py --add-account {acc.tool} {acc.name} --dir \"{acc.home}\"")


if __name__ == "__main__":
    main()
