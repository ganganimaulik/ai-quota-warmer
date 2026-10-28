#!/usr/bin/env python3
"""
AI Quota Warmer & 5-Hour Limit Starter
---------------------------------------
Triggers a minimal 'hi' prompt to OpenAI Codex CLI, Anthropic Claude Code CLI
and Google's Antigravity CLI (Gemini) so that your 5-hour rolling rate limit
window starts unattended.

Features:
  - Automatic binary discovery for Codex CLI, Claude Code CLI and Antigravity CLI.
  - Multiple accounts per tool: each login lives in its own config directory
    (CLAUDE_CONFIG_DIR / CODEX_HOME), so the official CLIs keep them apart.
    Extra Gemini accounts each get a folder the Antigravity CLI uses as its
    user profile, with its sign-in kept in a file there.
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
DEFAULT_CLAUDE_MODEL = os.getenv("AQW_CLAUDE_MODEL", "haiku")
DEFAULT_CODEX_MODEL = os.getenv("AQW_CODEX_MODEL", "luna")
# An alias resolved against `agy models` (see resolve_gemini_model): Gemini
# model ids change every few months, and Flash at low effort costs the least.
DEFAULT_GEMINI_MODEL = os.getenv("AQW_GEMINI_MODEL", "flash-low")
QUOTA_WINDOW_HOURS = 5.0
TASK_NAME = "AIQuotaWarmer_5HourLimit"


def _normalize_codex_model(model: str) -> str:
    """Normalizes 'luna' to its canonical Codex CLI slug 'gpt-5.6-luna'."""
    if not model:
        return model
    m = str(model).strip()
    if m.lower() in ("luna", "gpt-luna"):
        return "gpt-5.6-luna"
    return m

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


_SIGNIN_ERROR_RE = re.compile(
    r"not logged in|/login\b|codex login|failed to authenticate|authentication|unauthori[sz]ed|\b401\b"
    r"|invalid api key|(?:oauth|token)\b[^\n]{0,40}\b(?:expired|revoked|invalid)|refresh[ _]token"
    r"|please sign in",
    re.I,
)
# Signing in can fail for want of a network (agy refreshes its token on the
# way); that is not a sign-out, and must not raise a "signed out" alert.
_NETWORK_ERROR_RE = re.compile(
    r"dial tcp|no such host|i/o timeout|connection (?:refused|reset)|wsarecv|wsasend|network is unreachable"
    r"|getaddrinfo|name resolution",
    re.I,
)


def signin_problem(error):
    """When a failed request says the account has to sign in again, a short reason; else None."""
    text = str(error or "")
    if "deactivated_workspace" in text:
        return "Its ChatGPT workspace was deactivated - sign in with another account."
    match = _SIGNIN_ERROR_RE.search(text)
    if not match:
        return None
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    line = text[start:end if end >= 0 else len(text)]
    if _NETWORK_ERROR_RE.search(line):
        return None
    return line.split(" (Run '")[0].strip()[:200]


def _apply_login(entry, logged_in, detail, via, now_t):
    """
    Updates an account's login state. `via` is "check" for the CLI's local
    who-am-I, or "usage" / "warmup" / "signin" for a real request.

    A check saying "signed in" cannot clear a sign-out a real request
    reported: the credentials can still be on disk after the server stopped
    accepting them. Returns the reason when the account was just found signed out.
    """
    current = entry.get("login") or {}
    if via == "check" and logged_in and current.get("logged_in") is False and current.get("via") != "check":
        return None
    login = {"logged_in": bool(logged_in), "via": via, "checked_at": now_t}
    if not logged_in:
        login["detail"] = detail or "Not signed in."
    entry["login"] = login
    if not logged_in and current.get("logged_in") is not False:
        return login["detail"]
    return None


def record_login(key: str, logged_in: bool, detail="", via="check"):
    """Persists whether an account is signed in; returns the reason when it was just found signed out."""
    with _FILE_LOCK:
        state = load_state()
        entry = state.setdefault(key, {"last_attempt": 0.0, "last_success": 0.0, "consecutive_failures": 0})
        before = dict(entry.get("login") or {})
        newly_out = _apply_login(entry, logged_in, detail, via, time.time())
        after = entry.get("login") or {}
        # Skip the write when only the timestamp moved: a read succeeds every minute.
        if {k: v for k, v in before.items() if k != "checked_at"} != {k: v for k, v in after.items() if k != "checked_at"}:
            save_state(state)
        return newly_out


def record_attempt(key: str, success: bool, window=None, error=None):
    """
    Persists the outcome of a warm attempt so cooldowns survive restarts.

    `window` is the exact 5-hour window the CLI reported for the request
    ({"resets_at": epoch, ...}). It is kept even when the attempt failed: a
    rate-limited warm-up still says exactly when the limit lifts. A failure
    whose `error` says the account must sign in again marks it signed out.
    """
    with _FILE_LOCK:
        state = load_state()
        entry = state.setdefault(key, {"last_attempt": 0.0, "last_success": 0.0, "consecutive_failures": 0})
        now_t = time.time()
        entry["last_attempt"] = now_t
        if success:
            entry["last_success"] = now_t
            entry["consecutive_failures"] = 0
            _apply_login(entry, True, "", "warmup", now_t)
        else:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
            problem = signin_problem(error)
            if problem:
                _apply_login(entry, False, problem, "warmup", now_t)
        if window:
            entry["window"] = dict(window, as_of=now_t)
        save_state(state)
        try:
            from usage_detector import invalidate_usage_cache
            invalidate_usage_cache()
        except ImportError:
            pass
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
        try:
            from usage_detector import invalidate_usage_cache
            invalidate_usage_cache()
        except ImportError:
            pass


def record_window(key: str, window):
    """
    Stores a freshly read 5-hour window ({"resets_at": epoch or None, "as_of": epoch, ...})
    as the account's exact window, unless a warm-up recorded a newer one while
    the read was in flight.
    """
    with _FILE_LOCK:
        state = load_state()
        entry = state.setdefault(key, {"last_attempt": 0.0, "last_success": 0.0, "consecutive_failures": 0})
        current = entry.get("window") or {}
        if float(current.get("as_of") or 0) > float(window["as_of"]):
            return
        entry["window"] = dict(window)
        save_state(state)
        try:
            from usage_detector import invalidate_usage_cache
            invalidate_usage_cache()
        except ImportError:
            pass


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
#
# The Antigravity CLI (agy), which runs the Gemini models, has no such
# variable. Its own login - the default Gemini account - is one Google sign-in
# per Windows user, in the Credential Manager ("gemini:antigravity"), whatever
# directory agy runs with. An added Gemini account gets a folder that agy is
# run with as its user profile (USERPROFILE, with %APPDATA% and %LOCALAPPDATA%
# inside it), so its settings and conversations live there, plus
# SSH_CONNECTION: agy treats that like a
# headless SSH host and keeps the sign-in in a file under the profile instead
# of the Credential Manager. A profile alone is not enough - agy would still
# find the default account's sign-in in the Credential Manager - so every agy
# run of an added account is checked to have used that file
# (_agy_run_problem), and is never trusted otherwise.

TOOLS = ("claude", "codex", "gemini")
TOOL_TITLES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}
DEFAULT_ACCOUNT = "default"
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_HOME_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
_DEFAULT_HOME = {"claude": Path(".claude"), "codex": Path(".codex"), "gemini": Path(".gemini", "antigravity-cli")}
# Any value works: agy only checks that the variable is set (verified with agy 1.3).
_AGY_SSH_SESSION = "127.0.0.1 50000 127.0.0.1 22"

# Credentials in the environment beat the per-directory login, which would make
# every extra account silently warm the same one. They are stripped for
# accounts that have their own directory.
_OVERRIDING_AUTH_ENV = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
               "CLAUDE_SECURESTORAGE_CONFIG_DIR"),
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS",
               "JETSKI_OAUTH_TOKEN"),
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
        env_dir = os.getenv(_HOME_ENV[self.tool]) if self.tool in _HOME_ENV else None
        return Path(env_dir).expanduser() if env_dir else Path.home() / _DEFAULT_HOME[self.tool]

    def env(self):
        """Subprocess environment selecting this login, or None to inherit ours."""
        if self.is_default:
            return None
        env = os.environ.copy()
        for var in _OVERRIDING_AUTH_ENV[self.tool]:
            env.pop(var, None)
        if self.tool == "gemini":
            # Wherever agy keeps a file - under the profile, %APPDATA% or
            # %LOCALAPPDATA% - it lands in the account's folder.
            env["USERPROFILE"] = env["HOME"] = str(self.config_dir)
            env["APPDATA"], env["LOCALAPPDATA"] = (str(p) for p in _gemini_appdata(self.config_dir))
            env["SSH_CONNECTION"] = _AGY_SSH_SESSION
        else:
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


def _gemini_appdata(profile):
    """An added Gemini account's stand-ins for %APPDATA% and %LOCALAPPDATA%."""
    return Path(profile, "AppData", "Roaming"), Path(profile, "AppData", "Local")


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
    """
    Configured accounts in file order. Without accounts.json: one default per
    tool, Gemini only when the Antigravity CLI is installed - unlike Claude
    and Codex it is new here, and a setup without it would gain an account
    that can only fail.
    """
    raw = _read_json(ACCOUNTS_FILE, None)
    if raw is None or not isinstance(raw.get("accounts"), list):
        return [Account(tool) for tool in TOOLS if tool != "gemini" or find_gemini_binary()]

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
    Accounts matching a tool filter ('all', 'claude', 'codex', 'gemini') and, optionally,
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
        if acc.tool == "gemini" and acc.is_default:
            raise ValueError("Gemini's default account (agy's own sign-in) is already added. "
                             "Give the new account a name, e.g. 'work'.")
        raise ValueError(f"Account '{acc.tool}:{acc.name}' already exists.")
    for other in accounts:
        if other.tool == acc.tool and _norm_dir(other.home) == _norm_dir(acc.home):
            raise ValueError(f"{other.label} already uses {other.home}.")


def prepare_account(tool, name, config_dir=None):
    """
    Validates a new account and creates its directory, without registering it.

    Without `config_dir` the account lives in ~/.claude-NAME, ~/.codex-NAME or
    ~/.gemini-NAME. The first two are where you would point CLAUDE_CONFIG_DIR /
    CODEX_HOME to use the same login interactively; a Gemini folder is the
    user profile agy runs with (`--login gemini:NAME` opens agy that way). The
    name "default" restores the CLI's standard location. Register the account
    with register_account() once it is signed in: a registered account that is
    not logged in yet would be picked up by the watcher, fail to warm and land
    in backoff mid-login.
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
        target_dir = os.path.abspath(os.path.expanduser(str(config_dir or (Path.home() / f".{tool}-{name}"))))
        if tool == "gemini" and _norm_dir(target_dir) == _norm_dir(Path.home()):
            raise ValueError("That folder is your own user profile, where agy keeps the default "
                             "Gemini account. Choose another folder.")
        acc = Account(tool, name, target_dir)

    _check_new_account(acc, load_accounts())
    _ensure_home(acc)
    return acc


def _ensure_home(acc):
    # Codex refuses to start at all when CODEX_HOME does not exist yet, and an
    # added Gemini account's folder is the profile agy runs in. agy creates
    # its own folder for the default account.
    if acc.tool != "gemini" or not acc.is_default:
        acc.home.mkdir(parents=True, exist_ok=True)
    if acc.tool == "gemini" and not acc.is_default:
        for folder in _gemini_appdata(acc.home):
            folder.mkdir(parents=True, exist_ok=True)


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


def find_gemini_binary(custom_path=None):
    """Locates the Antigravity CLI (agy), which the Gemini accounts run through."""
    if custom_path and os.path.exists(custom_path):
        return custom_path

    env_path = os.getenv("AGY_CLI_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found

    local_appdata = os.getenv("LOCALAPPDATA", "")
    if local_appdata:
        candidate = os.path.join(local_appdata, "agy", "bin", "agy.exe")
        if os.path.exists(candidate):
            return candidate

    candidate = Path.home() / ".local" / "bin" / "agy"
    if candidate.exists():
        return str(candidate)

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

def _run_cli(target, cmd, binary, timeout, env=None, parse=None, cwd=None):
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
            cwd=cwd,
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
    if account.tool == "gemini":
        return "Run 'setup_gemini_login.bat' (or 'agy') once and sign in"
    return "Run 'codex login' once to authenticate"


def _add_login_hint(res, account):
    low = (res.get("error") or "").lower()
    if not res["success"] and ("not logged in" in low or "login" in low or "authenticat" in low):
        res["error"] += f" ({_login_hint(account)})"
    return res


def trigger_codex(prompt=DEFAULT_PROMPT, custom_path=None, timeout=90, account=None, model=DEFAULT_CODEX_MODEL):
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
    ]
    resolved_model = _normalize_codex_model(model)
    if resolved_model:
        cmd.extend(["-m", resolved_model])
    cmd.extend(["--", prompt])
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


def trigger_claude(prompt=DEFAULT_PROMPT, custom_path=None, timeout=90, account=None, model=DEFAULT_CLAUDE_MODEL):
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
    args = []
    if model:
        args.extend(["--model", str(model)])
    args.extend(["-p", prompt, "--output-format", "stream-json", "--verbose"])
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


_GEMINI_ALIAS_RE = re.compile(r"^(flash|pro)(?:-(low|medium|high))?$", re.I)
_GEMINI_MODEL_RE = re.compile(r"^gemini-(\d+(?:\.\d+)*)-(flash|pro)-(low|medium|high)$")
GEMINI_MODELS_TTL_SEC = 6 * 3600
_GEMINI_MODELS = {}              # (agy path, Account.key) -> (time listed, [model ids])
_GEMINI_MODELS_LOCK = threading.Lock()
# What agy logs when it keeps the sign-in in a file rather than the Credential Manager.
_AGY_FILE_SIGNIN_LOG = "Using file-based token storage"
_AGY_EMAIL_RE = re.compile(r"applyAuthResult: email=([^\s,]+)")


def _gemini_workdir():
    """
    An empty folder for the warmer's agy runs. agy makes its working directory
    the session's workspace, and a watcher started from the Startup folder
    could be sitting anywhere (System32, a project), so every run gets this
    one, with nothing in it for the agent to read.
    """
    path = DATA_DIR / "gemini-warmup"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return str(path)


def _agy_env(account=None):
    """agy's environment for the warmer's own runs of `account` (default: the default account)."""
    env = (account.env() if account is not None else None) or os.environ.copy()
    # A background run every few minutes must not download and swap the user's
    # CLI; agy still updates itself whenever it is used directly. Only the
    # exact value "true" is honoured.
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return env


def _agy_log_name(kind, account=None):
    # One log per account and kind of run ("usage", "warmup", "models"), so
    # runs in parallel never write, or get checked against, the same file.
    if account is None or account.is_default:
        return f"agy-{kind}.log"
    return f"agy-{kind}-{account.name}.log"


def _agy_cmd(agy_bin, args, log_name):
    # Every agy run rewrites ~/.gemini/antigravity-cli/cli.log, so the warmer's
    # runs log into its own folder instead of replacing a log the user may be
    # reading. --log-file is a global flag: it goes before any subcommand.
    ensure_data_dir()
    return [agy_bin, "--log-file", str(DATA_DIR / log_name)] + list(args)


def _agy_log_text(log_name):
    try:
        with open(DATA_DIR / log_name, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _forget_agy_log(account, log_name):
    # An added account's run is judged by its log, so a log left by an earlier
    # run must not stand in for one this run failed to write.
    if not account.is_default:
        try:
            (DATA_DIR / log_name).unlink()
        except OSError:
            pass


def _agy_log_email(log_name):
    """The Google account agy said it signed in as in that run's log, or None."""
    found = _AGY_EMAIL_RE.findall(_agy_log_text(log_name))
    return found[-1] if found else None


def _agy_run_problem(account, log_name):
    """
    Why the agy run that wrote `log_name` must not be trusted for `account`,
    or None. An added account is only kept apart while agy keeps its sign-in
    in the file under the account's folder: an agy that stopped honouring the
    switch would have answered with the default account's sign-in instead.
    """
    if account.is_default or _AGY_FILE_SIGNIN_LOG in _agy_log_text(log_name):
        return None
    return (f"agy did not use {account.label}'s own sign-in (its log shows no file-based sign-in), so it "
            "would have run as the default Gemini account. Nothing was trusted or warmed for it. This agy "
            "version may store sign-ins differently: update AI Quota Warmer.")


def _gemini_signin_files(account):
    """
    The sign-in agy saved in an added account's folder (only looked for, never
    read). With file-based sign-ins agy 1.3 keeps it in
    .gemini/antigravity-cli/antigravity-oauth-token under the profile; the
    search allows for it moving within .gemini. Only a shortcut: finding none,
    callers still ask agy itself.
    """
    root = Path(account.home) / ".gemini"
    found = []
    for pattern in ("*-oauth-token", "*/*-oauth-token", "*/*/*-oauth-token"):
        found.extend(p for p in root.glob(pattern) if p.is_file())
    return found


def _gemini_not_signed_in(account):
    return f"You are not logged into Antigravity in {_display_path(account.home)}."


def _run_agy(agy_bin, account, args, kind, timeout=90):
    """
    Runs one quiet agy command as `account`. Returns (CompletedProcess or
    None, problem), where problem says why the run must not be trusted.
    """
    log_name = _agy_log_name(kind, account)
    _forget_agy_log(account, log_name)
    proc = _run_quiet(_agy_cmd(agy_bin, args, log_name), env=_agy_env(account), timeout=timeout,
                      cwd=_gemini_workdir())
    return proc, (_agy_run_problem(account, log_name) if proc is not None else None)


def _last_line(proc):
    lines = [l.strip() for l in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if l.strip()]
    return lines[-1] if lines else f"agy exited with code {proc.returncode}."


def _list_gemini_models(agy_bin, account=None, force=False):
    """
    The model ids `agy models` lists for `account` (free, no prompt, a few
    seconds), cached for hours: ([ids], None), or ([], why it could not).
    Signed out, it says so in about a second.
    """
    account = account or Account("gemini")
    key = (agy_bin, account.key)
    now_t = time.time()
    if not force:
        with _GEMINI_MODELS_LOCK:
            cached = _GEMINI_MODELS.get(key)
            if cached and now_t - cached[0] < GEMINI_MODELS_TTL_SEC:
                return cached[1], None
    proc, problem = _run_agy(agy_bin, account, ["models"], "models")
    if proc is None:
        return [], "Could not run 'agy models'."
    if problem:
        return [], problem
    ids = []
    if proc.returncode == 0:
        # One "id<TAB>label" line per model; progress text goes to stderr.
        for line in (proc.stdout or "").splitlines():
            model_id = line.split("\t", 1)[0].strip()
            if model_id and " " not in model_id:
                ids.append(model_id)
    if not ids:
        error = _last_line(proc)
        if not account.is_default and signin_problem(error):
            # agy's own advice, to run agy, would sign in the default account.
            error = _gemini_not_signed_in(account)
        return [], error
    with _GEMINI_MODELS_LOCK:
        _GEMINI_MODELS[key] = (now_t, ids)
    return ids, None


def resolve_gemini_model(model, agy_bin, account=None):
    """
    The model id to warm Gemini with. "flash-low" (likewise "flash-medium",
    "pro-high", ..., with plain "flash" / "pro" meaning low effort) picks the
    newest matching Gemini model agy offers; any other value is used as
    given, and "" or "default" leaves agy's own default.

    Returns None when an alias cannot be resolved (agy signed out or
    offline), so the warm-up runs on agy's default model.
    """
    model = str(model or "").strip()
    if not model or model.lower() == "default":
        return None
    alias = _GEMINI_ALIAS_RE.match(model)
    if not alias:
        return model
    family, effort = alias.group(1).lower(), (alias.group(2) or "low").lower()
    best = None
    for model_id in _list_gemini_models(agy_bin, account)[0]:
        match = _GEMINI_MODEL_RE.match(model_id)
        if match and match.group(2) == family and match.group(3) == effort:
            version = tuple(int(part) for part in match.group(1).split("."))
            if best is None or version > best[0]:
                best = (version, model_id)
    return best[1] if best else None


def _agy_result(stdout):
    """The JSON object `agy -p ... --output-format json` printed, or None."""
    data = None
    for line in (stdout or "").splitlines():
        msg = _parse_json_object(line)
        if msg is not None and "status" in msg:
            data = msg
    return data


def _parse_agy_json(stdout):
    """
    Reads `agy -p ... --output-format json`: one JSON object whose "status"
    is "SUCCESS" (with the "response") or "ERROR" (with the "error").
    """
    data = _agy_result(stdout)
    if data is None:
        # No JSON at all: an older CLI, or a failure before the run started.
        return stdout, None, {}
    if str(data.get("status") or "").upper() != "SUCCESS":
        return "", str(data.get("error") or f"Antigravity reported {data.get('status') or 'an error'}"), {}
    return str(data.get("response") or "").strip(), None, {}


def trigger_gemini(prompt=DEFAULT_PROMPT, custom_path=None, timeout=90, account=None, model=DEFAULT_GEMINI_MODEL):
    """
    Runs the Antigravity CLI non-interactively to open the Gemini 5-hour window.

    The warm-up only counts once the free /usage read shows the window open,
    and that read gives its exact reset, the way Claude's rate_limit_event
    does. A run that answered but left the window closed (sent to a
    non-Gemini model, or billed to AI credits) counts as failed, so it backs
    off instead of repeating every cooldown.

    An added account is first probed with the free `agy models`: no prompt
    goes out unless agy just proved it signs in as that account.
    """
    account = account or Account("gemini")
    agy_bin = find_gemini_binary(custom_path)

    def failed(error):
        return {"target": account.key, "success": False, "response": "", "error": error,
                "duration": 0.0, "path": agy_bin}

    if not agy_bin:
        return failed("Antigravity CLI (agy) not found in PATH or %LOCALAPPDATA%\\agy.")
    if not account.is_default:
        _, problem = _list_gemini_models(agy_bin, account, force=True)
        if problem:
            return _add_login_hint(failed(problem), account)

    resolved = resolve_gemini_model(model, agy_bin, account)
    # The prompt goes last, and slash commands are off, so a custom prompt such
    # as "/usage" is sent to the model instead of being run as a command.
    args = ["--output-format", "json", "--disable-slash-commands",
            "--print-timeout", f"{max(30, int(timeout) - 15)}s"]
    if resolved:
        args.extend(["--model", resolved])
    args.extend(["-p", prompt])
    log_name = _agy_log_name("warmup", account)
    _forget_agy_log(account, log_name)
    started = time.time()
    res = _run_cli(account.key, _agy_cmd(agy_bin, args, log_name), agy_bin, timeout,
                   env=_agy_env(account), parse=_parse_agy_json, cwd=_gemini_workdir())
    res["model"] = resolved or "(agy default)"
    problem = _agy_run_problem(account, log_name)
    if problem:
        res.update(success=False, error=problem)
    elif res["success"]:
        res.update(_confirm_gemini_window(account, custom_path, started, res["model"]))
    return _add_login_hint(res, account)


def _confirm_gemini_window(account, custom_path, started, model):
    """Result fields for a warm-up agy answered: the window it opened, or why it opened none."""
    reading = None
    for attempt in range(2):
        if attempt:
            time.sleep(5)  # the quota service can trail the request by a moment
        reading, _ = fetch_gemini_usage(account, custom_path)
        if reading is None or reading.get("resets_at"):
            break
    if reading is None:
        # The read itself failed (a network blip): the window opened with the request.
        return {"window": {"resets_at": started + QUOTA_WINDOW_HOURS * 3600, "source": "warm-up"}}
    if reading.get("resets_at"):
        return {"window": reading}
    return {
        "success": False, "window": reading,
        "error": (f"agy answered, but the Gemini 5-hour window did not open (model: {model}). "
                  "The request went to a non-Gemini model or was billed to AI credits."),
    }


# ---------------------------------------------------------------------------
# Login status & login (free - no prompt is ever sent)
# ---------------------------------------------------------------------------

def _run_quiet(cmd, env=None, timeout=60, input=None, cwd=None):
    """Runs a short informational command; returns the CompletedProcess or None."""
    try:
        return subprocess.run(
            cmd, input=input, stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env, cwd=cwd, creationflags=_NO_WINDOW,
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


def _gemini_login_status(account, custom_path=None):
    agy_bin = find_gemini_binary(custom_path)
    if not agy_bin:
        return {"logged_in": None, "detail": "Antigravity CLI (agy) not found."}
    # agy has no who-am-I command, but the free `agy models` only answers
    # signed in (signed out it says so in a second, where the quota read
    # waits a minute for a sign-in), and its log names the Google account.
    ids, error = _list_gemini_models(agy_bin, account, force=True)
    if ids:
        info = {"logged_in": True, "method": "Antigravity sign-in"}
        email = _agy_log_email(_agy_log_name("models", account))
        if email:
            info["email"] = email
        return info
    problem = signin_problem(error)
    if problem:
        return {"logged_in": False, "detail": problem}
    return {"logged_in": None, "detail": error or "Could not ask agy who it is signed in as."}


def account_login_status(account, claude_path=None, codex_path=None, gemini_path=None):
    """
    Asks the account's CLI who it is signed in as. Free: no prompt is sent.

    Returns {"logged_in": True/False/None (unknown), "email", "org", "plan",
    "method", "detail", "warning"} with only the fields that are known.
    """
    try:
        if account.tool == "claude":
            return _claude_login_status(account, claude_path)
        if account.tool == "gemini":
            return _gemini_login_status(account, gemini_path)
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


def _login_command(account, claude_path=None, codex_path=None, gemini_path=None):
    """The CLI's own login command for this account, as (cmd, None) or (None, reason)."""
    if account.tool == "claude":
        binary = find_claude_binary(claude_path)
        if not binary:
            return None, "Claude Code CLI not found - install it first."
        return _claude_login_cmd(binary, account.env()), None
    if account.tool == "gemini":
        binary = find_gemini_binary(gemini_path)
        if not binary:
            return None, "Antigravity CLI (agy) not found - install it first."
        # agy has no login subcommand: its interactive screen signs in.
        return [binary], None
    binary = find_codex_binary(codex_path)
    if not binary:
        return None, "Codex CLI not found - install it first."
    return [binary, "login"], None


_GEMINI_SIGNIN_STEPS = ("It asks you to sign in when it is signed out; to switch to another Google "
                        "account, type /logout and then /login. Type /exit once you are signed in.")


def _gemini_signin_steps(account):
    if account.is_default:
        return _GEMINI_SIGNIN_STEPS
    return ("Pick Google OAuth. Open the link it shows in a private/incognito window and sign in there "
            f"with the Google account you want as {account.label}, then paste the code that page shows "
            "into the Antigravity window and press Enter. If it opens signed in to the wrong account, "
            "type /logout and then /login. Type /exit once you are signed in.")


def _gemini_signin_env(account, agy_bin):
    """
    (env, problem) for agy's interactive sign-in of `account`. For an added
    account a free probe runs first: were agy to ignore the account's folder,
    that window would show - and could sign out - the default account.
    """
    if account.is_default:
        return None, None
    proc, problem = _run_agy(agy_bin, account, ["models"], "models")
    if proc is None:
        return None, "Could not run the Antigravity CLI."
    return (None, problem) if problem else (_agy_env(account), None)


def login_account(account, claude_path=None, codex_path=None, gemini_path=None):
    """
    Runs the CLI's own interactive login with this account's directory selected.

    The CLI opens the browser and stores the result in the account directory
    (Antigravity's default account: in the system keyring); this tool never
    sees the credentials.
    """
    _ensure_home(account)
    cmd, problem = _login_command(account, claude_path, codex_path, gemini_path)
    if not cmd:
        print(f"[-] {problem}")
        return False

    if account.tool == "gemini":
        env, problem = _gemini_signin_env(account, cmd[0])
        if problem:
            print(f"[-] {problem}")
            return False
        print(f"[*] Opening the Antigravity CLI to sign in {account.label}"
              + ("." if account.is_default else f" (folder {_display_path(account.home)})."))
        print("    " + _gemini_signin_steps(account))
        try:
            # Inherits this console: agy's sign-in screen needs a real terminal.
            rc = subprocess.call(cmd, cwd=_gemini_workdir(), env=env)
        except OSError as e:
            print(f"[-] Could not start the Antigravity CLI: {e}")
            return False
        return rc == 0

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
# Background sign-in (driven by the web dashboard)
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
    passes to the waiting CLI. Antigravity signs in from its interactive
    screen instead, in a console window of its own. With `register`, the
    account is added to accounts.json only once the sign-in succeeded.
    """

    def __init__(self, account, register=False, claude_path=None, codex_path=None, gemini_path=None):
        self.account = account
        self.register = register
        self._paths = (claude_path, codex_path, gemini_path)
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
                "hint": ("A window with the Antigravity CLI opened. " + _gemini_signin_steps(self.account)
                         if self.account.tool == "gemini" else None),
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

    def _succeed(self, status, note=""):
        if self.register:
            try:
                register_account(self.account)
            except ValueError as e:
                return self._end("failed", str(e), status)
        clear_backoff(self.account.key)
        record_login(self.account.key, True, via="signin")
        self._end("succeeded", describe_login(status) + note, status)

    def _run(self):
        acc = self.account
        try:
            _ensure_home(acc)
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
        with self._lock:
            if self.state == "cancelled":
                return  # replaced by a newer sign-in: don't open a second browser tab
        if acc.tool == "gemini":
            return self._run_in_console(cmd)
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

    def _run_in_console(self, cmd):
        """
        agy signs in from its interactive screen, which needs a real terminal,
        so it runs in a console window of its own rather than behind pipes.
        The sign-in is checked once that window closes - or, for a new added
        account, as soon as agy saved a sign-in in its folder.
        """
        acc = self.account
        if not _IS_WINDOWS:
            return self._end("failed", "Run 'agy' in a terminal and sign in there, then use Check Logins.")
        env, problem = _gemini_signin_env(acc, cmd[0])
        if problem:
            return self._end("failed", problem)
        with self._lock:
            if self.state == "cancelled":
                return
        opened = time.time()
        try:
            proc = subprocess.Popen(cmd, cwd=_gemini_workdir(), env=env,
                                    creationflags=subprocess.CREATE_NEW_CONSOLE)
        except (OSError, ValueError) as e:
            return self._end("failed", f"Could not start the Antigravity CLI: {e}")
        with self._lock:
            cancelled = self.state == "cancelled"
            if not cancelled:
                self._proc, self.state = proc, "running"
        if cancelled:
            return _kill_tree(proc)
        # A new account's folder had no sign-in, so one appearing is the user
        # finishing it. (Signing in again may only refresh the old one.)
        watch = self.register and not acc.is_default
        checked = set()
        rc = None
        while time.time() - opened < LOGIN_TIMEOUT_SEC:
            try:
                rc = proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                pass
            with self._lock:
                if self.state == "cancelled":
                    return
            saved = set()
            for path in _gemini_signin_files(acc) if watch else ():
                try:
                    saved.add((str(path), path.stat().st_mtime))
                except OSError:
                    pass  # replaced while agy saved it: the next pass sees the new one
            if saved - checked:
                checked |= saved
                status = account_login_status(acc, *self._paths)
                if status.get("logged_in") is True:
                    # Leave agy running: the window is the user's now.
                    return self._succeed(status, " You can close the Antigravity window.")
        # On a timeout, by now it is a working agy session: leave the window open.
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
            return self._succeed(status)
        if rc is None:
            reason = "Timed out waiting for the sign-in to finish."
        else:
            with self._lock:
                last = next((l for l in reversed(self.output) if not _SIGNIN_URL_RE.search(l)), "")
            reason = last or "The sign-in did not complete."
        self._end("failed", reason, status)


_LOGIN_JOB = None
_LOGIN_JOB_LOCK = threading.Lock()


def start_login_job(account, register=False, claude_path=None, codex_path=None, gemini_path=None):
    """
    Starts a background sign-in. Only one runs at a time, since Codex's
    callback port is fixed, so asking again cancels the one in progress and
    starts over: the earlier browser tab was closed, or went to the wrong account.
    """
    global _LOGIN_JOB
    with _LOGIN_JOB_LOCK:
        if _LOGIN_JOB is not None:
            _LOGIN_JOB.cancel()  # waits for its CLI to exit, which frees the callback port
        _LOGIN_JOB = LoginJob(account, register, claude_path, codex_path, gemini_path).start()
        return _LOGIN_JOB


def current_login_job():
    return _LOGIN_JOB


def dismiss_login_job():
    """Forgets a finished sign-in so the UIs stop showing it."""
    global _LOGIN_JOB
    with _LOGIN_JOB_LOCK:
        if _LOGIN_JOB is not None and not _LOGIN_JOB.active:
            _LOGIN_JOB = None


def check_logins(codex_path=None, claude_path=None, accounts=None, gemini_path=None):
    """Checks and reports who every account is logged in as (no prompt is sent)."""
    accounts = load_accounts() if accounts is None else accounts
    print("=" * 70)
    print("  AI QUOTA WARMER - ACCOUNTS & LOGIN CHECK")
    print("=" * 70)
    print("  Free check: each CLI is asked who it is signed in as. No prompt is sent.")

    results, identities = {}, {}
    for acc in accounts:
        status = account_login_status(acc, claude_path, codex_path, gemini_path)
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
    print("            python quota_warmer.py --add-account gemini work   (its own Google sign-in)")
    print("  Re-login: python quota_warmer.py --login claude:work")
    print("  Remove  : python quota_warmer.py --remove-account claude:work")
    print("=" * 70)
    return results


def trigger_account(account, prompt=DEFAULT_PROMPT, codex_path=None, claude_path=None, timeout=90,
                    claude_model=None, codex_model=None, gemini_path=None, gemini_model=None):
    """Sends one warm-up prompt through the CLI of `account`."""
    if account.tool == "claude":
        m = DEFAULT_CLAUDE_MODEL if claude_model is None else claude_model
        return trigger_claude(prompt, claude_path, timeout=timeout, account=account, model=m)
    if account.tool == "gemini":
        m = DEFAULT_GEMINI_MODEL if gemini_model is None else gemini_model
        return trigger_gemini(prompt, gemini_path, timeout=timeout, account=account, model=m)
    m = DEFAULT_CODEX_MODEL if codex_model is None else codex_model
    return trigger_codex(prompt, codex_path, timeout=timeout, account=account, model=m)


def _fmt_epoch(epoch):
    return datetime.datetime.fromtimestamp(float(epoch)).strftime("%I:%M:%S %p")


def run_trigger_batch(target="all", prompt=DEFAULT_PROMPT, codex_path=None, claude_path=None,
                      notify=True, quiet=False, accounts=None, claude_model=None, codex_model=None,
                      gemini_path=None, gemini_model=None):
    """Executes triggers concurrently for `accounts` (default: every account matching `target`)."""
    accounts = select_accounts(target) if accounts is None else accounts
    now = datetime.datetime.now()
    reset_at = now + datetime.timedelta(hours=QUOTA_WINDOW_HOURS)
    lines = []

    c_model = DEFAULT_CLAUDE_MODEL if claude_model is None else claude_model
    x_model = _normalize_codex_model(DEFAULT_CODEX_MODEL if codex_model is None else codex_model)
    g_model = DEFAULT_GEMINI_MODEL if gemini_model is None else gemini_model

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
    out(f"  Claude Model   : '{c_model or '(CLI default)'}'")
    out(f"  Codex Model    : '{x_model or '(CLI default)'}'")
    if any(a.tool == "gemini" for a in accounts):
        out(f"  Gemini Model   : '{g_model or '(CLI default)'}'")
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
                target=lambda a=acc: results.__setitem__(
                    a.key, trigger_account(a, prompt, codex_path, claude_path,
                                           claude_model=c_model, codex_model=x_model,
                                           gemini_path=gemini_path, gemini_model=g_model)
                ),
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
        record_attempt(acc.key, bool(res.get("success")), window=res.get("window"), error=res.get("error"))
        resets_at = (res.get("window") or {}).get("resets_at")
        reset_note = f" | resets {_fmt_epoch(resets_at)}" if resets_at else ""
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
        "models": {"claude": c_model, "codex": x_model, "gemini": g_model},
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
# Live Claude & Gemini windows and sign-in state (free - no prompt is ever sent)
# ---------------------------------------------------------------------------

USAGE_REFRESH_SEC = 60           # how often each Claude account's real window is re-read
USAGE_RETRY_SEC = 300            # after a failed read (signed out, offline, older CLI)
# Gemini's read takes ~10s of mostly network wait. Every five minutes is plenty:
# while the watcher runs a closed window is warmed at once anyway, and an open
# one's reset is already known to the second.
GEMINI_REFRESH_SEC = 300
GEMINI_RETRY_SEC = 600
LOGIN_CHECK_SEC = 600            # how often a Codex account's sign-in is checked
_USAGE_READS = {}                # Account.key -> (time of last read or check, succeeded)
_USAGE_READS_LOCK = threading.Lock()


def _iso_to_epoch(value):
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def fetch_claude_usage(account=None, custom_path=None, timeout=60):
    """
    Reads an account's real 5-hour window from Claude Code itself.

    Claude Code answers a `get_usage` control request on its stream-json
    interface with the same plan usage `/usage` shows. No prompt is sent, so
    it is free, uses no quota, and (with --no-session-persistence) writes no
    transcript. The CLI marks the request experimental, so any unexpected
    answer returns None and the caller keeps its previous reading.

    Returns {"resets_at": epoch, "used_pct": ..., "week_used_pct": ...,
    "source": "usage-api"}, where resets_at is None when no window is open.
    """
    account = account or Account("claude")
    claude_bin = find_claude_binary(custom_path)
    if not claude_bin:
        return None

    args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--no-session-persistence"]
    if _is_npx(claude_bin):
        cmd = [claude_bin, "-y", "@anthropic-ai/claude-code"] + args
        timeout = max(timeout, 300)
    else:
        cmd = [claude_bin] + args
    request = {"type": "control_request", "request_id": "usage", "request": {"subtype": "get_usage"}}
    proc = _run_quiet(cmd, env=account.env(), timeout=timeout, input=json.dumps(request) + "\n")
    if not proc:
        return None

    for line in (proc.stdout or "").splitlines():
        msg = _parse_json_object(line)
        if not msg or msg.get("type") != "control_response":
            continue
        resp = msg.get("response") or {}
        if resp.get("request_id") != "usage":
            continue
        data = resp.get("response") if resp.get("subtype") == "success" else None
        if not isinstance(data, dict) or not data.get("rate_limits_available"):
            return None  # signed out, or an error
        limits = data.get("rate_limits")
        five = limits.get("five_hour") if isinstance(limits, dict) else None
        if not isinstance(five, dict):
            return None  # a plan without a 5-hour limit
        resets_at = None
        if five.get("resets_at") is not None:
            resets_at = _iso_to_epoch(five["resets_at"])
            if resets_at is None:
                return None
        week = limits.get("seven_day") if isinstance(limits.get("seven_day"), dict) else {}
        return {
            "resets_at": resets_at,
            "used_pct": five.get("utilization"),
            "week_used_pct": week.get("utilization"),
            "source": "usage-api",
        }
    return None


def _fraction(value):
    """A 0..1 quota fraction, or None when `value` is not a number."""
    try:
        frac = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, frac)) if frac == frac else None


def fetch_gemini_usage(account=None, custom_path=None, timeout=90):
    """
    Reads the Gemini 5-hour and weekly windows from the Antigravity CLI.

    `agy -p /usage` is answered from the quota service without starting an
    agent turn, so it is free, uses no quota and leaves no conversation
    behind (about half a second of CPU; the rest is network). Gemini Flash
    and Pro share one 5-hour bucket; Claude and GPT models run through agy
    have their own, which this leaves alone. While a bucket is unused its
    reset just tracks now + 5 hours, so a full bucket means no window is open.

    Returns (reading, error). reading is {"resets_at": epoch or None,
    "used_pct", "week_used_pct", "week_remaining", "week_resets_at",
    "source": "usage-api"}, or None when the read failed and error says why.
    """
    account = account or Account("gemini")
    agy_bin = find_gemini_binary(custom_path)
    if not agy_bin:
        return None, "Antigravity CLI (agy) not found."
    if not account.is_default and not _gemini_signin_files(account):
        # No sign-in where agy keeps one, and signed out the quota read waits a
        # full minute for a sign-in: the quick `agy models` asks first.
        ids, error = _list_gemini_models(agy_bin, account, force=True)
        if not ids:
            return None, error
    proc, problem = _run_agy(agy_bin, account, ["--output-format", "json", "-p", "/usage"], "usage",
                             timeout=timeout)
    if proc is None:
        return None, "Could not run 'agy -p /usage'."
    if problem:
        return None, problem
    data = _agy_result(proc.stdout)
    if data is None:
        return None, _last_line(proc)
    if str(data.get("status") or "").upper() != "SUCCESS":
        return None, str(data.get("error") or "Antigravity could not read the quota.")

    groups = ((data.get("command") or {}).get("data") or {}).get("groups")
    five = week = None
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        buckets = [b for b in group.get("buckets") or [] if isinstance(b, dict)]
        if not (any(b.get("id") == "gemini-5h" for b in buckets) or "gemini" in str(group.get("name") or "").lower()):
            continue
        for bucket in buckets:
            if bucket.get("window") == "5h":
                five = bucket
            elif bucket.get("window") == "weekly":
                week = bucket
        break
    if five is None:
        return None, "Antigravity reported no Gemini 5-hour limit (signed in with a Gemini API key?)."

    left = _fraction(five.get("remaining_fraction"))
    resets_at = _iso_to_epoch(five["reset_time"]) if five.get("reset_time") else None
    if left is None or resets_at is None:
        return None, "Unexpected answer from 'agy -p /usage'."
    reading = {"resets_at": resets_at if left < 1 else None,
               "used_pct": round((1 - left) * 100, 2), "source": "usage-api"}
    week_left = _fraction((week or {}).get("remaining_fraction"))
    if week_left is not None:
        reading["week_used_pct"] = round((1 - week_left) * 100, 2)
        reading["week_remaining"] = week_left
        week_reset = _iso_to_epoch(week["reset_time"]) if week.get("reset_time") else None
        if week_reset is not None and week_left < 1:
            reading["week_resets_at"] = week_reset
    return reading, None


def refresh_live_status(accounts, force=False, claude_path=None, codex_path=None, gemini_path=None):
    """
    Keeps every account's live readings current, all accounts in parallel:

    - Claude: the real 5-hour window, re-read every minute (every five while
      that fails) and stored as the account's exact window. Without it a
      window opened elsewhere (claude.ai, the desktop app, another PC) was a
      guess from local transcripts until the next warm-up. When the read
      fails, the CLI is asked whether the account is still signed in.
    - Gemini: the 5-hour and weekly windows, re-read every five minutes.
      agy only answers the read signed in, so it is also the sign-in check.
    - Codex: whether the account is still signed in, every ten minutes.

    Returns [(account, reason)] for the accounts just found signed out.
    """
    now_t = time.time()
    due = []
    state = load_state()
    with _USAGE_READS_LOCK:
        for acc in accounts:
            last, ok = _USAGE_READS.get(acc.key, (0.0, True))
            if acc.tool == "claude":
                wait = USAGE_REFRESH_SEC if ok else USAGE_RETRY_SEC
            elif acc.tool == "gemini":
                wait = GEMINI_REFRESH_SEC if ok else GEMINI_RETRY_SEC
            else:
                login = (state.get(acc.key) or {}).get("login") or {}
                if login.get("logged_in") is False and login.get("via") != "check" and not force:
                    continue  # only a working request can clear this; a check would not change it
                wait = LOGIN_CHECK_SEC
            # A couple of seconds of slack so a 60s watcher pass is never just short.
            if force or now_t - last >= wait - 2:
                _USAGE_READS[acc.key] = (now_t, ok)
                due.append(acc)

    signed_out = []

    def check_login(acc):
        status = account_login_status(acc, claude_path, codex_path, gemini_path)
        if status.get("logged_in") is None:
            return False
        reason = record_login(acc.key, status["logged_in"], status.get("detail"), via="check")
        if reason:
            signed_out.append((acc, reason))
        return True

    def refresh(acc):
        asked = time.time()
        if acc.tool == "claude":
            reading = fetch_claude_usage(acc, custom_path=claude_path)
            ok = reading is not None
            if ok:
                record_window(acc.key, dict(reading, as_of=asked))
                record_login(acc.key, True, via="usage")
            else:
                check_login(acc)
        elif acc.tool == "gemini":
            reading, error = fetch_gemini_usage(acc, custom_path=gemini_path)
            ok = reading is not None
            if ok:
                record_window(acc.key, dict(reading, as_of=asked))
                record_login(acc.key, True, via="usage")
            else:
                problem = signin_problem(error)
                reason = record_login(acc.key, False, problem, via="usage") if problem else None
                if reason:
                    signed_out.append((acc, reason))
        else:
            ok = check_login(acc)
        with _USAGE_READS_LOCK:
            _USAGE_READS[acc.key] = (asked, ok)

    threads = [threading.Thread(target=refresh, args=(a,), daemon=True) for a in due]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return signed_out


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------

def get_all_usage(accounts=None, state=None, force=False):
    """
    Local 5-hour window readings for every account, keyed by Account.key.

    Claude readings are anchored on the exact reset the last warm-up reported
    (kept in state.json), so they stop being guesses once a warm-up has run.
    Gemini readings are that stored window alone: agy keeps no local record
    of its quota.
    """
    from usage_detector import get_account_usage, invalidate_usage_cache

    if force:
        invalidate_usage_cache()

    accounts = load_accounts() if accounts is None else accounts
    state = load_state() if state is None else state
    return {
        acc.key: get_account_usage(
            acc.tool, acc.home,
            exact=(state.get(acc.key) or {}).get("window"),
            # The opt-in `claude -p /cost` probe is billed; never multiply it per account.
            allow_live_cli=acc.is_default,
            force=force,
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
    refresh_live_status(accounts, force=True)
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


_STATUS_TITLES = {"claude": "CLAUDE CODE", "codex": "CODEX CLI", "gemini": "GEMINI - ANTIGRAVITY CLI"}


def _print_account_usage(acc, data, state):
    title = _STATUS_TITLES[acc.tool]
    print(f"\n  [{title}] {acc.label} - {_display_path(acc.home)}")
    print("  " + "-" * 66)
    login = (state.get(acc.key) or {}).get("login") or {}
    if login.get("logged_in") is False:
        print(f"    Login             : [SIGNED OUT] {login.get('detail') or ''}")
        print(f"    Fix               : {_login_hint(acc)}")
    if data.get("status") == "ERROR":
        print(f"    Status            : [ERROR] {data.get('error')}")
    elif not data.get("has_data"):
        if acc.tool == "gemini":
            print("    No quota reading yet: 'agy -p /usage' did not answer. Check it with")
            print(f"    python quota_warmer.py --list-accounts --account {acc.key}")
        else:
            print(f"    No {acc.label} sessions found in the lookback window.")
    elif acc.tool == "gemini":
        print(f"    Status            : [{data['status']}]  (source: {data.get('source')})")
        if data["status"] == "LIMITED":
            print(f"    Weekly Limit      : used up - resets {data.get('week_reset')}. No warm-ups until")
            print("                        then: they could only fail, or be billed to AI credits.")
        else:
            print(f"    Active Window     : {data['window_start']} -> Resets at {data['window_reset']}")
            print(f"    Countdown         : {data['time_remaining']} remaining ({data['progress_pct']}% elapsed)")
        if data.get("session_used_pct") is not None:
            print(f"    5h Quota Used     : {data['session_used_pct']}%   as of {data.get('limits_as_of')}")
        if data.get("week_used_pct") is not None:
            print(f"    Weekly Quota Used : {data['week_used_pct']}%   (resets {data.get('week_reset')})")
    elif acc.tool == "claude":
        print(f"    Status            : [{data['status']}]  (source: {data.get('source')})")
        print(f"    Active Window     : {data['window_start']} -> Resets at {data['window_reset']}")
        print(f"    Countdown         : {data['time_remaining']} remaining ({data['progress_pct']}% elapsed)")
        if data.get("session_used_pct") is not None:
            print(f"    5h Quota Used     : {data['session_used_pct']}%   (weekly: {data.get('week_used_pct')}%)")
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

def warm_if_expired(account, usage, prompt=DEFAULT_PROMPT, notify=True, log=print,
                    claude_model=None, codex_model=None, gemini_model=None):
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
    res = trigger_account(account, prompt, claude_model=claude_model, codex_model=codex_model,
                          gemini_model=gemini_model)
    entry = record_attempt(account.key, bool(res.get("success")), window=res.get("window"), error=res.get("error"))

    if res.get("success"):
        resets_at = (res.get("window") or {}).get("resets_at")
        reset_note = f", resets {_fmt_epoch(resets_at)}" if resets_at else ""
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


def report_signed_out(signed_out, notify=True, log=print):
    """Tells the user about accounts refresh_live_status() just found signed out."""
    for acc, reason in signed_out:
        log(f"  [-] {acc.label} is signed out: {reason}")
        if notify:
            send_windows_notification(f"{acc.label} Signed Out",
                                      f"{reason} Sign in again from the dashboard to keep it warm.")


def warm_cycle(accounts, prompt=DEFAULT_PROMPT, notify=True, log=print, idle_sleep=60.0,
               claude_model=None, codex_model=None, gemini_model=None):
    """
    One pass of the adaptive watcher: warms every account whose window has
    lapsed (in parallel, so the last of several accounts is not left waiting)
    and returns how long to sleep before the next pass.

    Claude windows are re-read from Claude Code every minute first (Gemini
    windows from agy every five), so a window opened or reset elsewhere is
    acted on promptly, and accounts that got signed out are reported.
    Otherwise the sleep ends shortly after the nearest reset or cooldown
    expiry, so an idle machine polls rarely but still fires within seconds of
    a window opening.
    """
    cycle_start = time.time()
    report_signed_out(refresh_live_status(accounts), notify=notify, log=log)
    usage = get_all_usage(accounts)
    due = [a for a in accounts
           if not usage[a.key].get("is_active") and usage[a.key].get("status") != "ERROR"]
    threads = [
        threading.Thread(target=warm_if_expired, args=(a, usage[a.key]),
                         kwargs={"prompt": prompt, "notify": notify, "log": log,
                                 "claude_model": claude_model, "codex_model": codex_model,
                                 "gemini_model": gemini_model}, daemon=True)
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
    if any(acc.tool == "claude" for acc in accounts):
        waits.append(USAGE_REFRESH_SEC - (time.time() - cycle_start))

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
                               notify=True, verbose=True, account_specs=None,
                               claude_model=None, codex_model=None, gemini_model=None):
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
    c_model = DEFAULT_CLAUDE_MODEL if claude_model is None else claude_model
    x_model = _normalize_codex_model(DEFAULT_CODEX_MODEL if codex_model is None else codex_model)
    g_model = DEFAULT_GEMINI_MODEL if gemini_model is None else gemini_model

    log("=" * 70)
    log("  AI QUOTA WARMER - SMART ADAPTIVE AUTO-WARM DAEMON")
    log("=" * 70)
    log(f"  Watching: {', '.join(a.label for a in accounts) or 'no accounts yet'}")
    log(f"  Prompt  : '{prompt}'")
    log(f"  Models  : Claude='{c_model}', Codex='{x_model}', Gemini='{g_model}'")
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
                                       idle_sleep=check_interval_sec,
                                       claude_model=c_model, codex_model=x_model, gemini_model=g_model)
            except Exception as e:
                log(f"  [!] Watcher error: {type(e).__name__}: {e}")
                sleep_for = float(check_interval_sec)

            sleep_until_next_cycle(sleep_for)
    except KeyboardInterrupt:
        log("\nSmart Adaptive Watcher stopped.")
    finally:
        lock.release()


def run_daemon_loop(interval_hours=5.0, target="all", prompt=DEFAULT_PROMPT, notify=True,
                    claude_model=None, codex_model=None, gemini_model=None):
    """Runs the continuous smart adaptive auto-warmup monitor."""
    run_smart_adaptive_monitor(target=target, prompt=prompt, notify=notify, verbose=True,
                               claude_model=claude_model, codex_model=codex_model,
                               gemini_model=gemini_model)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="AI Quota Warmer - Start the 5-Hour Limit Unattended for Codex, Claude Code & Gemini",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--now", action="store_true", help="Trigger warmup immediately (default action)")
    parser.add_argument("--target", choices=["all", "codex", "claude", "gemini"], default="all",
                        help="Limit to one tool's accounts (default: all)")
    parser.add_argument("--account", action="append", metavar="ACCOUNT",
                        help="Limit to specific account(s): claude, codex, gemini, or TOOL:NAME such as "
                             "claude:work or gemini:work (repeatable or comma-separated)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help=f"Prompt to send (default: '{DEFAULT_PROMPT}')")
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL,
                        help=f"Model for Claude Code warmup (default: '{DEFAULT_CLAUDE_MODEL}')")
    parser.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL,
                        help=f"Model for Codex warmup (default: '{DEFAULT_CODEX_MODEL}')")
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL,
                        help=f"Model for Gemini warmup (default: '{DEFAULT_GEMINI_MODEL}', the newest "
                             "Gemini Flash at low effort; also flash-medium, pro-low, ... or a model id)")
    parser.add_argument("--status", action="store_true", help="Show the real 5-hour window countdown and history")
    parser.add_argument("--check-login", "--list-accounts", dest="check_login", action="store_true",
                        help="List accounts and who each is logged in as (free - no prompt is sent)")
    parser.add_argument("--loop", action="store_true", help="Run the adaptive background watcher")
    parser.add_argument("--interval", type=float, default=5.0, help="Interval in hours for the scheduled task (default: 5.0)")
    parser.add_argument("--no-notify", action="store_true", help="Disable Windows desktop notifications")
    parser.add_argument("--force", action="store_true", help="Ignore the per-account cooldown for --now")
    parser.add_argument("--codex-path", default=None, help="Custom path to the codex executable")
    parser.add_argument("--claude-path", default=None, help="Custom path to the claude CLI")
    parser.add_argument("--agy-path", dest="gemini_path", default=None, metavar="PATH",
                        help="Custom path to the Antigravity CLI (agy) the Gemini account runs through")

    parser.add_argument("--add-account", nargs="+", metavar=("TOOL", "NAME"),
                        help="Add an account (TOOL: claude, codex or gemini; NAME defaults to 'default', "
                             "the CLI's own login) and open its sign-in")
    parser.add_argument("--dir", default=None,
                        help="Config directory for --add-account "
                             "(default: ~/.claude-NAME, ~/.codex-NAME or ~/.gemini-NAME)")
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
        if len(args.add_account) > 2:
            parser.error("--add-account takes a TOOL and an optional NAME")
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
        login_account(acc, args.claude_path, args.codex_path, args.gemini_path)
        status = account_login_status(acc, args.claude_path, args.codex_path, args.gemini_path)
        if status.get("logged_in"):
            clear_backoff(acc.key)
            record_login(acc.key, True, via="signin")
        print(f"[{'+' if status.get('logged_in') else '-'}] {acc.label}: {describe_login(status)}")
        return

    try:
        selected = select_accounts(args.target, args.account)
    except ValueError as e:
        parser.error(str(e))

    if args.check_login:
        check_logins(codex_path=args.codex_path, claude_path=args.claude_path, accounts=selected,
                     gemini_path=args.gemini_path)
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
                                   account_specs=args.account,
                                   claude_model=args.claude_model, codex_model=args.codex_model,
                                   gemini_model=args.gemini_model)
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
        claude_model=args.claude_model,
        codex_model=args.codex_model,
        gemini_path=args.gemini_path,
        gemini_model=args.gemini_model,
    )


def _cli_add_account(args):
    tool, name = (list(args.add_account) + [DEFAULT_ACCOUNT])[:2]
    try:
        acc = prepare_account(tool, name, args.dir)
    except (ValueError, OSError) as e:
        print(f"[-] {e}")
        sys.exit(1)
    print(f"[*] Setting up {acc.label} as '{acc.key}' in {acc.home}")

    paths = (args.claude_path, args.codex_path, args.gemini_path)
    status = account_login_status(acc, *paths)
    if not status.get("logged_in"):
        login_account(acc, *paths)
        status = account_login_status(acc, *paths)
    print(f"    Login  : {describe_login(status)}")
    if status.get("warning"):
        print(f"    Warning: {status['warning']}")
    # Registering a signed-out account would only make the watcher fail on it.
    if status.get("logged_in") is False:
        retry = f"python quota_warmer.py --add-account {acc.tool}" + ("" if acc.is_default else f" {acc.name}")
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
    if acc.tool == "gemini" and not acc.is_default:
        print(f"    To use this login yourself: python quota_warmer.py --login {acc.key}  (opens agy as it)")
    elif not acc.is_default:
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
