# 🚀 AI Quota Warmer (5-Hour Limit Starter)

Automatically pings **OpenAI Codex CLI** and **Anthropic Claude Code CLI** with a minimal
prompt (`"hi"`) so your **5-hour rolling rate-limit window starts unattended** — and keeps
watching so the next window opens the moment the current one expires.

---

## ⚡ Quick Start

```bash
python quota_warmer.py --status
```

| Step | Do this | Or double-click |
| :--- | :--- | :--- |
| 1. Log in to Claude Code (one time) | `claude /login` | `setup_claude_login.bat` |
| 2. See your real 5-hour windows | `python quota_warmer.py --status` | `check_status.bat` |
| 3. Warm both tools right now | `python quota_warmer.py --now` | `trigger_now.bat` |
| 4. Keep it warm 24/7 | `python quota_warmer.py --install-startup` | `enable_autorun_on_restart.bat` |

Then optionally open a UI:

```bash
python app_ui.py
```

*(`open_ui.bat` for the web dashboard at http://127.0.0.1:5055, `open_desktop_gui.bat` for the native app.)*

---

## 🔍 How the 5-Hour Windows Are Detected

Everything is read from local session logs. **No billed API calls are made just to check status.**

| Tool | Source | Accuracy |
| :--- | :--- | :--- |
| **OpenAI Codex** | `rate_limits` block inside `~/.codex/sessions/**/*.jsonl` | **Exact** — Codex records `window_minutes`, `resets_at` and `used_percent` straight from the API |
| **Claude Code** | Activity clustering over `~/.claude/projects/**/*.jsonl` | **Inferred** — Claude does not persist its limit headers locally, so the window is derived from when your first message landed |

The status line tells you which one you are looking at (`source: rate-limit-header` vs `source: session-logs`).

> **Optional exact Claude numbers.** Setting `AI_QUOTA_WARMER_LIVE_CLI=1` makes the detector
> also run `claude -p /cost`. This is **off by default on purpose**: that command is a real,
> billed request (~$0.19 a call) and it writes a session file that makes the window look
> permanently active. Even when enabled it is throttled to once every 15 minutes.

---

## 🔄 Unattended Operation

Two independent mechanisms — either is enough, and they are safe to combine:

**1. Adaptive watcher (recommended).** `--install-startup` drops a silent `pythonw.exe`
launcher into your Startup folder. On every login it runs `quota_warmer.py --loop`, which
watches both windows and fires a warm-up *the instant* one resets — no fixed schedule.

**2. Windows Scheduled Task.** `--install-task` registers a plain every-5-hours job.
No admin rights needed.

Safety rails on both:

- **Single instance.** A lock file (`~/.ai_quota_warmer/daemon.lock`) means the Startup
  daemon and an open dashboard never double-ping. Whichever starts first does the warming.
- **Persistent cooldown.** A tool is warmed at most once per 5 minutes, and the cooldown
  survives reboots.
- **Exponential backoff.** Consecutive failures back off 5m → 10m → 20m → 40m → 60m, so an
  expired login can never spawn an endless stream of CLI processes.
- **Never warms blind.** If the detector itself errors, no warm-up fires.

---

## 🛠️ Files

| File | Description |
| :--- | :--- |
| `quota_warmer.py` | Main engine: triggers, adaptive watcher, autorun/task install, history |
| `usage_detector.py` | Read-only 5-hour window + token/quota detector for both tools |
| `app_ui.py` | Local web dashboard (http://127.0.0.1:5055) |
| `gui_app.py` | Native Tkinter desktop app |
| `install_task.ps1` | PowerShell task installer (logon + interval triggers; may need admin) |
| `_env.bat` | Shared helper that locates a working Python for the other `.bat` files |
| `trigger_now.bat` | Warm both tools now |
| `check_status.bat` | Show real countdowns, quota %, and history |
| `check_logins.bat` | Verify authentication (⚠️ sends one real prompt per tool) |
| `enable_autorun_on_restart.bat` / `disable_autorun_on_restart.bat` | Toggle the login watcher |
| `install_task_every_5hrs.bat` / `install_task_daily_morning.bat` / `uninstall_task.bat` | Scheduled Task management |
| `start_daemon.bat` | Run the adaptive watcher in a visible window |
| `setup_claude_login.bat` | One-time Claude Code browser login |

---

## 📋 CLI Reference

```
python quota_warmer.py [options]

  --now                  Warm up immediately (default action)
  --target {all,codex,claude}
  --prompt TEXT          Prompt to send (default: "hi")
  --force                Ignore the cooldown for --now
  --status               Show real 5-hour windows, quota %, and history
  --check-login          Verify auth (sends one real prompt per tool)
  --loop                 Run the adaptive watcher in the foreground

  --install-startup / --uninstall-startup      Silent watcher on every login
  --install-task / --uninstall-task            Windows Scheduled Task
  --task-mode {interval,daily,startup}
  --task-time HH:MM      Time for --task-mode daily
  --interval HOURS       Hours between scheduled runs (1-23)
  --no-notify            Suppress desktop notifications

  --codex-path / --claude-path                 Override CLI locations
```

Environment overrides: `CODEX_CLI_PATH`, `CLAUDE_CLI_PATH`, `AI_QUOTA_WARMER_LIVE_CLI`.

---

## 📂 Data Locations

| Path | Contents |
| :--- | :--- |
| `~/.ai_quota_warmer/history.json` | Last 100 trigger results (atomic writes) |
| `~/.ai_quota_warmer/state.json` | Per-tool cooldown and failure counters |
| `~/.ai_quota_warmer/daemon.lock` | Single-instance lock held by the running watcher |

---

## 🔒 Dashboard Security

`app_ui.py` binds to `127.0.0.1` only, and every state-changing endpoint requires a
per-run CSRF token plus a loopback `Host` header. Without those guards any website open
in your browser could POST to the local API and install scheduled tasks or burn quota.

---

## 🩺 Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| `'python' is not recognized` | Install Python 3.9+ with "Add python.exe to PATH" ticked |
| Claude warm-ups fail | Run `setup_claude_login.bat` once |
| `--status` shows `EXPIRED` right after a successful warm-up | Session logs can lag a second or two; re-run `--status` |
| Dashboard says "another daemon is running" | Expected — the Startup watcher already has it covered |
| `install_task.ps1` says "Access is denied" | Use `python quota_warmer.py --install-task` instead (no admin needed) |
| Scheduled task never fires | Confirm it exists: `schtasks /query /tn AIQuotaWarmer_5HourLimit` |
