"""
Actual 5-Hour Usage & Rate Limit Detector for Claude Code and OpenAI Codex
-------------------------------------------------------------------------
Reads real usage telemetry directly from local session logs:

- OpenAI Codex CLI : ~/.codex/sessions/**/*.jsonl  (authoritative `rate_limits`
                     payload with window_minutes / resets_at / used_percent)
                     plus ~/.codex/thread_history_1.sqlite for turn counts.
- Claude Code      : ~/.claude/projects/**/*.jsonl (session transcripts; Claude
                     does not persist its rate-limit headers locally, so the
                     5-hour window is inferred by clustering activity).

Design notes
------------
* Everything here is READ-ONLY and free. No CLI subprocess is spawned during
  normal operation - an earlier version shelled out to `claude -p /cost` on
  every poll, which cost real money, burned the very quota this tool exists to
  protect, and wrote a new session file that corrupted the window detection.
  That probe is now opt-in (AI_QUOTA_WARMER_LIVE_CLI=1) and heavily throttled.
* Parsed files are cached by (mtime, size) so polling every few seconds is cheap.
* All datetimes are timezone-aware. Naive timestamps are coerced to UTC rather
  than silently dropped.
"""

import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

CLAUDE_WINDOW_HOURS = 5.0
LOOKBACK_HOURS = 48

# Opt-in live CLI probe. Off by default: `claude -p /cost` performs a real,
# billed API round-trip and writes a session file that skews detection.
LIVE_CLI_ENABLED = os.getenv("AI_QUOTA_WARMER_LIVE_CLI", "").strip().lower() in ("1", "true", "yes")
LIVE_CLI_MIN_INTERVAL_SEC = 900.0  # 15 min, even when explicitly enabled

_CLAUDE_CLI_CACHE = {"timestamp": 0.0, "data": None}
_CLI_LOCK = threading.Lock()

# path -> (mtime, size, parsed_events)
_FILE_CACHE = {}
_FILE_CACHE_LOCK = threading.Lock()
_FILE_CACHE_MAX = 4000


def _as_aware(dt):
    """Coerces a datetime to timezone-aware (assumes UTC when naive)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_ts(ts_str):
    """Parses an ISO-8601 timestamp into an aware datetime, or None."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        return _as_aware(datetime.fromisoformat(ts_str.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def _iter_recent_files(pattern, cutoff_ts):
    """Yields files matching a glob whose mtime is at or after the cutoff."""
    for path in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) >= cutoff_ts:
                yield path
        except OSError:
            continue


def _read_jsonl_cached(path, extract):
    """
    Parses a JSONL file through an (mtime, size) keyed cache.

    `extract(obj)` returns the compact record to keep, or None to skip. Session
    logs are append-only, so any change to mtime/size invalidates the entry.
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime, st.st_size)
    except OSError:
        return []

    with _FILE_CACHE_LOCK:
        cached = _FILE_CACHE.get(path)
        if cached and cached[0] == key[0] and cached[1] == key[1]:
            return cached[2]

    records = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    rec = extract(obj)
                except Exception:
                    rec = None
                if rec is not None:
                    records.append(rec)
    except OSError:
        return []

    with _FILE_CACHE_LOCK:
        if len(_FILE_CACHE) > _FILE_CACHE_MAX:
            _FILE_CACHE.clear()
        _FILE_CACHE[path] = (key[0], key[1], records)
    return records


def _fmt_remaining(remaining_sec, is_active):
    if not is_active:
        return "Window Reset (Ready)"
    hrs, rem = divmod(max(0, int(remaining_sec)), 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs:02d}h {mins:02d}m {secs:02d}s"


def _fmt_local(dt):
    return dt.astimezone().strftime("%I:%M:%S %p") if dt else "N/A"


def _cluster_windows(events, window_hours):
    """
    Groups chronologically sorted (dt, obj) events into rolling windows.

    A window opens at the first event seen and closes `window_hours` later; the
    next event after that boundary opens a fresh window.
    """
    windows = []
    start = end = None
    bucket = []

    for dt, obj in events:
        if end is None or dt > end:
            if start is not None:
                windows.append((start, end, bucket))
            start = dt
            end = dt + timedelta(hours=window_hours)
            bucket = [(dt, obj)]
        else:
            bucket.append((dt, obj))

    if start is not None:
        windows.append((start, end, bucket))
    return windows


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

def find_claude_binary():
    """Locates the Claude CLI binary on the host system."""
    for candidate in [
        os.getenv("CLAUDE_CLI_PATH"),
        shutil.which("claude"),
        shutil.which("claude.cmd"),
        shutil.which("claude.exe"),
        str(Path.home() / ".local" / "bin" / "claude.exe"),
        str(Path.home() / ".local" / "bin" / "claude"),
    ]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def parse_claude_reset_str(reset_str: str):
    """
    Parses human-readable reset timestamps from Claude into an aware datetime.
    Supports formats like:
      - 'Sep 1, 9pm', 'Sep 1, 8:59pm', 'Sep 1, 9:30am'
      - '8:59pm', '9pm', '09:00 PM'
      - 'in 2 hours 15 minutes', 'in 45m', 'in 3h'
    """
    if not reset_str:
        return None

    reset_clean = reset_str.strip()
    now = datetime.now().astimezone()

    # Pattern 0: Relative e.g. 'in 2 hours 15 minutes', 'in 45m', 'in 3h'
    m_rel = re.search(r'in\s+(?:(\d+)\s*(?:hours?|hrs?|h))?\s*(?:(\d+)\s*(?:mins?|minutes?|m))?', reset_clean, re.I)
    if m_rel and (m_rel.group(1) or m_rel.group(2)):
        h = int(m_rel.group(1)) if m_rel.group(1) else 0
        m = int(m_rel.group(2)) if m_rel.group(2) else 0
        return now + timedelta(hours=h, minutes=m)

    # Pattern 1: 'Sep 1, 8:59pm' or 'September 1, 9pm'
    m1 = re.search(r'([A-Za-z]+)\s+(\d+),\s*(\d+)(?::(\d+))?\s*(am|pm)', reset_clean, re.I)
    if m1:
        month_str, day_str, hour_str, min_str, ampm = m1.groups()
        hour = int(hour_str)
        minute = int(min_str) if min_str else 0
        if ampm.lower() == "pm" and hour != 12:
            hour += 12
        elif ampm.lower() == "am" and hour == 12:
            hour = 0
        try:
            dt_month = datetime.strptime(month_str[:3], "%b").month
            dt = now.replace(
                year=now.year, month=dt_month, day=int(day_str),
                hour=hour, minute=minute, second=0, microsecond=0,
            )
            if dt < (now - timedelta(hours=12)):
                dt = dt.replace(year=now.year + 1)
            return dt
        except ValueError:
            pass

    # Pattern 2: Time only, e.g. '8:59pm', '9pm', '9:30 AM'
    m2 = re.search(r'\b(\d+)(?::(\d+))?\s*(am|pm)\b', reset_clean, re.I)
    if m2:
        hour_str, min_str, ampm = m2.groups()
        hour = int(hour_str)
        minute = int(min_str) if min_str else 0
        if hour > 12:
            return None
        if ampm.lower() == "pm" and hour != 12:
            hour += 12
        elif ampm.lower() == "am" and hour == 12:
            hour = 0
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt < (now - timedelta(minutes=15)):
            dt += timedelta(days=1)
        return dt

    return None


def fetch_live_claude_cli():
    """
    Optionally queries the Claude CLI for exact subscription usage.

    Disabled unless AI_QUOTA_WARMER_LIVE_CLI=1: this spawns a *billed* Claude
    request and writes a session transcript, so calling it on a poll loop both
    costs money and makes the 5-hour window look permanently active.
    """
    if not LIVE_CLI_ENABLED:
        return None

    with _CLI_LOCK:
        now_t = time.time()
        if _CLAUDE_CLI_CACHE["data"] is not None and (now_t - _CLAUDE_CLI_CACHE["timestamp"] < LIVE_CLI_MIN_INTERVAL_SEC):
            return _CLAUDE_CLI_CACHE["data"]
        # Reserve the slot up front so a failure does not retry on every poll.
        _CLAUDE_CLI_CACHE["timestamp"] = now_t

    claude_bin = find_claude_binary()
    if not claude_bin:
        return None

    try:
        proc = subprocess.run(
            [claude_bin, "-p", "/cost", "--output-format", "json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None

        data = json.loads(proc.stdout)
        result_text = data.get("result", "") or ""
        if not result_text:
            return None

        session_pct = week_pct = 0
        session_reset_str = session_tz = ""
        week_reset_str = week_tz = ""

        # Parse line-by-line so the weekly reset never bleeds into the session reset.
        for line in result_text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue

            if re.match(r'^Current session:', line_str, re.I):
                m_pct = re.search(r'Current session:\s*(\d+)%', line_str, re.I)
                if m_pct:
                    session_pct = int(m_pct.group(1))
                m_res = re.search(r'resets\s+([^(]+?)(?:\s*\(([^)]+)\))?$', line_str, re.I)
                if m_res:
                    session_reset_str = m_res.group(1).strip()
                    session_tz = (m_res.group(2) or "").strip()

            elif re.match(r'^Current week', line_str, re.I):
                m_pct = re.search(r'Current week[^\n:]*:\s*(\d+)%', line_str, re.I)
                if m_pct:
                    week_pct = int(m_pct.group(1))
                m_res = re.search(r'resets\s+([^(]+?)(?:\s*\(([^)]+)\))?$', line_str, re.I)
                if m_res:
                    week_reset_str = m_res.group(1).strip()
                    week_tz = (m_res.group(2) or "").strip()

        now_aware = datetime.now().astimezone()
        session_reset_dt = parse_claude_reset_str(session_reset_str)
        # A 5-hour rolling reset can never be more than ~5h away, nor in the past.
        if session_reset_dt and not (
            (now_aware - timedelta(minutes=10)) <= session_reset_dt <= (now_aware + timedelta(hours=5, minutes=15))
        ):
            session_reset_dt = None

        parsed_info = {
            "has_cli_data": True,
            "session_used_pct": session_pct,
            "session_reset_raw": session_reset_str,
            "session_reset_dt": session_reset_dt,
            "session_tz": session_tz,
            "week_used_pct": week_pct,
            "week_reset_raw": week_reset_str,
            "week_reset_dt": parse_claude_reset_str(week_reset_str),
            "week_tz": week_tz,
            "raw_text": result_text,
        }

        with _CLI_LOCK:
            _CLAUDE_CLI_CACHE["timestamp"] = time.time()
            _CLAUDE_CLI_CACHE["data"] = parsed_info
        return parsed_info
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _extract_claude_event(obj):
    ts = _parse_ts(obj.get("timestamp"))
    if ts is None:
        return None
    ev_type = obj.get("type")
    model = None
    usage = None
    if ev_type == "assistant":
        msg = obj.get("message") or {}
        if isinstance(msg, dict):
            model = msg.get("model")
            u = msg.get("usage")
            if isinstance(u, dict):
                usage = (
                    u.get("input_tokens", 0) or 0,
                    u.get("output_tokens", 0) or 0,
                    u.get("cache_read_input_tokens", 0) or 0,
                    u.get("cache_creation_input_tokens", 0) or 0,
                )
    return (ts, ev_type, model, usage)


def get_claude_actual_usage():
    """
    Detects the Claude Code 5-hour rolling window by clustering session activity.

    Claude does not persist rate-limit headers locally, so the window is inferred:
    it opens at the first message after the previous window lapsed and runs for
    five hours.
    """
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    cutoff = now_utc - timedelta(hours=LOOKBACK_HOURS)
    cutoff_ts = cutoff.timestamp() - 3600

    pattern = str(Path.home() / ".claude" / "projects" / "**" / "*.jsonl")

    all_events = []
    for path in _iter_recent_files(pattern, cutoff_ts):
        for rec in _read_jsonl_cached(path, _extract_claude_event):
            if rec[0] >= cutoff:
                all_events.append(rec)

    all_events.sort(key=lambda r: r[0])
    windows = _cluster_windows([(r[0], r) for r in all_events], CLAUDE_WINDOW_HOURS)

    live_cli = fetch_live_claude_cli()

    last_window = windows[-1] if windows else None
    active_window = last_window if (last_window and now_utc < last_window[1]) else None

    # Prefer an explicit reset time from the CLI when one is available and sane.
    if live_cli and live_cli.get("session_reset_dt"):
        reset_dt_local = live_cli["session_reset_dt"]
        start_dt_local = reset_dt_local - timedelta(hours=CLAUDE_WINDOW_HOURS)
        remaining_sec = max(0, int((reset_dt_local - now_local).total_seconds()))
        is_active = remaining_sec > 0
    elif active_window:
        start_dt_local = active_window[0].astimezone()
        reset_dt_local = active_window[1].astimezone()
        remaining_sec = max(0, int((active_window[1] - now_utc).total_seconds()))
        is_active = remaining_sec > 0
    elif last_window:
        start_dt_local = last_window[0].astimezone()
        reset_dt_local = last_window[1].astimezone()
        remaining_sec = 0
        is_active = False
    else:
        start_dt_local = reset_dt_local = None
        remaining_sec = 0
        is_active = False

    window_events = active_window[2] if active_window else (last_window[2] if last_window else [])

    user_prompts = 0
    total_input = total_output = total_cache_read = total_cache_write = 0
    models_used = set()
    latest_dt = None

    for dt, rec in window_events:
        latest_dt = dt
        _, ev_type, model, usage = rec
        if ev_type == "user":
            user_prompts += 1
        if model:
            models_used.add(model)
        if usage:
            total_input += usage[0]
            total_output += usage[1]
            total_cache_read += usage[2]
            total_cache_write += usage[3]

    if is_active and start_dt_local:
        elapsed = (now_local - start_dt_local).total_seconds()
        progress_pct = min(100, max(0, int((elapsed / (CLAUDE_WINDOW_HOURS * 3600)) * 100)))
    else:
        progress_pct = 0 if is_active else 100

    return {
        "tool": "claude",
        "has_data": bool(all_events) or live_cli is not None,
        "is_active": is_active,
        "status": "ACTIVE" if is_active else ("EXPIRED" if last_window else "IDLE"),
        "source": "cli" if (live_cli and live_cli.get("session_reset_dt")) else "session-logs",
        "window_start": _fmt_local(start_dt_local),
        "window_reset": _fmt_local(reset_dt_local),
        "reset_epoch": reset_dt_local.timestamp() if reset_dt_local else None,
        "latest_interaction": _fmt_local(latest_dt),
        "time_remaining": _fmt_remaining(remaining_sec, is_active),
        "remaining_seconds": remaining_sec,
        "progress_pct": progress_pct,
        "session_used_pct": live_cli.get("session_used_pct") if live_cli else None,
        "week_used_pct": live_cli.get("week_used_pct") if live_cli else None,
        "week_reset_raw": live_cli.get("week_reset_raw", "") if live_cli else "",
        "user_prompts": user_prompts,
        "total_events": len(window_events),
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_read": total_cache_read,
            "cache_write": total_cache_write,
            "total": total_input + total_output + total_cache_read + total_cache_write,
        },
        "models_used": sorted(models_used),
    }


# --------------------------------------------------------------------------
# OpenAI Codex
# --------------------------------------------------------------------------

def _extract_codex_event(obj):
    ts = _parse_ts(obj.get("timestamp"))
    if ts is None:
        return None
    payload = obj.get("payload")
    payload_type = None
    rate_limits = None
    total_tokens = 0
    is_turn = False

    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "token_count":
            info = payload.get("info") or {}
            usage = info.get("total_token_usage") or {}
            total_tokens = usage.get("total_tokens", 0) or 0
            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                rate_limits = rl
        elif payload_type == "task_started":
            is_turn = True

    return (ts, obj.get("type"), payload_type, rate_limits, total_tokens, is_turn)


def _read_codex_sqlite_turns(cutoff_ts):
    """Reads turn start times from Codex's thread history DB (read-only)."""
    db_path = Path.home() / ".codex" / "thread_history_1.sqlite"
    if not db_path.exists():
        return []

    turns = []
    con = None
    try:
        # Read-only URI so we never block or mutate a DB that Codex may be using.
        uri = f"file:{db_path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        rows = con.execute(
            "SELECT started_at FROM thread_turns WHERE started_at >= ?",
            (int(cutoff_ts),),
        ).fetchall()
        for (started_at,) in rows:
            if not started_at:
                continue
            # Guard against a future schema switching to milliseconds.
            value = started_at / 1000.0 if started_at > 3e10 else started_at
            try:
                turns.append(datetime.fromtimestamp(value, tz=timezone.utc))
            except (ValueError, OverflowError, OSError):
                continue
    except sqlite3.Error:
        return turns
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
    return turns


def _latest_codex_rate_limits(events):
    """Returns the most recent `rate_limits` block found in the session logs."""
    for dt, _type, _ptype, rate_limits, _tok, _turn in reversed(events):
        if rate_limits:
            return dt, rate_limits
    return None, None


def get_codex_actual_usage():
    """
    Reports the Codex 5-hour rolling window.

    Codex writes authoritative limit data into its session logs
    (`rate_limits.primary` = {used_percent, window_minutes, resets_at}), so that
    is used whenever present. Activity clustering is only a fallback.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=LOOKBACK_HOURS)
    cutoff_ts = cutoff.timestamp() - 3600

    pattern = str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl")

    all_events = []
    for path in _iter_recent_files(pattern, cutoff_ts):
        for rec in _read_jsonl_cached(path, _extract_codex_event):
            if rec[0] >= cutoff:
                all_events.append(rec)

    all_events.sort(key=lambda r: r[0])

    sqlite_turns = _read_codex_sqlite_turns(cutoff_ts)
    combined = [(r[0], r) for r in all_events] + [
        (dt, (dt, "thread_turn", None, None, 0, True)) for dt in sqlite_turns if dt >= cutoff
    ]
    combined.sort(key=lambda x: x[0])

    rl_dt, rate_limits = _latest_codex_rate_limits(all_events)
    primary = (rate_limits or {}).get("primary") or {}
    secondary = (rate_limits or {}).get("secondary") or {}

    window_hours = CLAUDE_WINDOW_HOURS
    source = "session-logs"
    start_dt_local = reset_dt_local = None
    remaining_sec = 0
    is_active = False

    resets_at = primary.get("resets_at")
    if isinstance(resets_at, (int, float)) and resets_at > 0:
        window_minutes = primary.get("window_minutes") or int(CLAUDE_WINDOW_HOURS * 60)
        try:
            window_hours = float(window_minutes) / 60.0
            reset_dt = datetime.fromtimestamp(float(resets_at), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            reset_dt = None
        if reset_dt:
            source = "rate-limit-header"
            reset_dt_local = reset_dt.astimezone()
            start_dt_local = reset_dt_local - timedelta(hours=window_hours)
            remaining_sec = max(0, int((reset_dt - now_utc).total_seconds()))
            is_active = remaining_sec > 0

    windows = _cluster_windows(combined, window_hours)
    last_window = windows[-1] if windows else None
    active_window = last_window if (last_window and now_utc < last_window[1]) else None

    if source != "rate-limit-header":
        chosen = active_window or last_window
        if chosen:
            start_dt_local = chosen[0].astimezone()
            reset_dt_local = chosen[1].astimezone()
            remaining_sec = max(0, int((chosen[1] - now_utc).total_seconds())) if active_window else 0
            is_active = remaining_sec > 0

    window_events = active_window[2] if active_window else (last_window[2] if last_window else [])

    turns = 0
    total_tokens = 0
    latest_dt = None
    for dt, rec in window_events:
        latest_dt = dt
        if rec[5]:
            turns += 1
        total_tokens = max(total_tokens, rec[4])

    if is_active and start_dt_local:
        now_local = datetime.now().astimezone()
        elapsed = (now_local - start_dt_local).total_seconds()
        progress_pct = min(100, max(0, int((elapsed / (window_hours * 3600)) * 100)))
    else:
        progress_pct = 0 if is_active else 100

    return {
        "tool": "codex",
        "has_data": bool(combined) or bool(rate_limits),
        "is_active": is_active,
        "status": "ACTIVE" if is_active else ("EXPIRED" if (last_window or rate_limits) else "IDLE"),
        "source": source,
        "window_start": _fmt_local(start_dt_local),
        "window_reset": _fmt_local(reset_dt_local),
        "reset_epoch": reset_dt_local.timestamp() if reset_dt_local else None,
        "latest_interaction": _fmt_local(latest_dt),
        "time_remaining": _fmt_remaining(remaining_sec, is_active),
        "remaining_seconds": remaining_sec,
        "progress_pct": progress_pct,
        "session_used_pct": primary.get("used_percent"),
        "week_used_pct": secondary.get("used_percent"),
        "limits_as_of": _fmt_local(rl_dt),
        "plan_type": (rate_limits or {}).get("plan_type"),
        "turns_in_5h": turns,
        "total_events": len(window_events),
        "tokens": {"total": total_tokens},
    }


def get_actual_usage_summary():
    """Returns the combined actual 5-hour limit usage for both Codex and Claude Code."""
    try:
        claude = get_claude_actual_usage()
    except Exception as e:  # never let a telemetry read crash a caller's UI loop
        claude = _error_state("claude", e)
    try:
        codex = get_codex_actual_usage()
    except Exception as e:
        codex = _error_state("codex", e)

    return {"timestamp": datetime.now().isoformat(), "claude": claude, "codex": codex}


def _error_state(tool, exc):
    """A safe, fully-populated result so UIs never KeyError on a failed read."""
    return {
        "tool": tool,
        "has_data": False,
        # Treated as active so a detector failure can never trigger a warm-up storm.
        "is_active": True,
        "status": "ERROR",
        "source": "error",
        "error": f"{type(exc).__name__}: {exc}",
        "window_start": "N/A",
        "window_reset": "N/A",
        "reset_epoch": None,
        "latest_interaction": "N/A",
        "time_remaining": "Unavailable",
        "remaining_seconds": 0,
        "progress_pct": 0,
        "session_used_pct": None,
        "week_used_pct": None,
        "user_prompts": 0,
        "turns_in_5h": 0,
        "total_events": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
        "models_used": [],
    }


if __name__ == "__main__":
    print(json.dumps(get_actual_usage_summary(), indent=2))
