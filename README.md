# 🚀 AI Quota Warmer (5-Hour Limit Starter)

Automatically pings **OpenAI Codex CLI** and **Anthropic Claude Code CLI** with a minimal
prompt (`"hi"`) so your **5-hour rolling rate-limit window starts unattended** — and keeps
watching so the next window opens the moment the current one expires. Works with
**multiple accounts per tool** (e.g. personal + work).

---

## ⚡ Quick Start

```bash
python quota_warmer.py --status
```

| Step | Do this | Or double-click |
| :--- | :--- | :--- |
| 1. Log in to Claude Code (one time) | `claude /login` | `setup_claude_login.bat` |
| 2. See your real 5-hour windows | `python quota_warmer.py --status` | `check_status.bat` |
| 3. Warm every account right now | `python quota_warmer.py --now` | `trigger_now.bat` |
| 4. Keep it warm 24/7 | `python quota_warmer.py --install-startup` | `enable_autorun_on_restart.bat` |
| 5. *(Optional)* Add another account | `python quota_warmer.py --add-account claude work` | `add_account.bat` |

Then optionally open a UI:

```bash
python app_ui.py
```

*(`open_ui.bat` for the web dashboard at http://127.0.0.1:5055, `open_desktop_gui.bat` for the native app.)*

---

## 👥 Multiple Accounts

Both CLIs keep each login in a config folder chosen by an environment variable:
`CLAUDE_CONFIG_DIR` for Claude Code and `CODEX_HOME` for Codex. A second account is just a
second folder, so the warmer runs the official CLI once per account with that variable
set. It never reads or stores your credentials. The CLI's own browser login writes them
into the folder.

```bash
python quota_warmer.py --add-account claude work      # creates ~/.claude-work, opens the browser login
python quota_warmer.py --add-account codex personal   # creates ~/.codex-personal
python quota_warmer.py --list-accounts                # who each account is signed in as (free)
```

- Your existing logins stay as the **default** `claude` and `codex` accounts, so
  single-account setups need no changes.
- Each account has its own window detection, cooldown, backoff and history, and all
  accounts warm in parallel. A running watcher or dashboard picks up a new account within
  about 15 seconds.
- **Sign in with the right account.** If the browser is already signed in to your other
  account, switch accounts first. Otherwise both folders get the same login.
  `--list-accounts` shows each account's email and warns when two look identical.
- For added accounts, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`,
  `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` are removed from the environment. Credentials in
  the environment override the folder's login and would make every account warm the same one.
- To use an added account yourself, point the CLI at its folder:
  `set CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-work` and then run `claude`.
- `--account claude:work` limits any command to specific accounts (repeatable or
  comma-separated). `--target claude` still means all Claude accounts.
- `--login claude:work` redoes the browser login. `--remove-account claude:work` stops
  warming the account but keeps its folder and login.
- `--add-account ... --dir PATH` registers a folder you already use as `CLAUDE_CONFIG_DIR`
  or `CODEX_HOME`.
- An added Codex account starts without your default `config.toml`. Copy the file into its
  folder if you want the same model and settings.

---

## 🔍 How the 5-Hour Windows Are Detected

Everything is read from local files or from the warm-up's own output. **No billed API
calls are made just to check status.**

| Tool | Source | Accuracy |
| :--- | :--- | :--- |
| **OpenAI Codex** | `rate_limits` block inside `<CODEX_HOME>/sessions/**/*.jsonl` | **Exact.** Codex records `window_minutes`, `resets_at` and `used_percent` straight from the API |
| **Claude Code** | The `rate_limit_event` from each warm-up, then activity in `<config dir>/projects/**/*.jsonl` | **Exact after a warm-up.** Warm-ups run `claude -p --output-format stream-json`, which reports the real reset time from the API response headers. Before the first warm-up, or for a window you opened yourself, the window is **inferred** from when your first message landed |

The status line shows which source you're looking at: `rate-limit-event` and
`rate-limit-header` are exact, and `session-logs` is inferred.

> **Optional live Claude numbers (default account only).** Setting `AI_QUOTA_WARMER_LIVE_CLI=1` makes the detector
> also run `claude -p /cost`. This is **off by default on purpose**: that command is a real,
> billed request (~$0.19 a call) and it writes a session file that makes the window look
> permanently active. Even when enabled it is throttled to once every 15 minutes.

---

## 🤔 Why Drive the Official CLIs (Not the API Directly)?

Skipping the CLI and sending "hi" straight to the API, using the token the CLI stored, would
be faster. It's still the wrong approach:

- **Claude:** Anthropic's terms reserve Free/Pro/Max OAuth tokens for Claude Code and
  Anthropic's own apps. Using them from any other tool violates the Consumer Terms, and it
  has been blocked server-side since January 2026. Running the unmodified `claude` binary
  with your own login is the sanctioned path.
- **Codex:** the ChatGPT backend endpoints that the CLI calls are undocumented and change
  without notice. Refresh tokens also rotate, so a second client refreshing yours can log
  the CLI out.

So the warmer uses the CLIs, but through their structured, official interfaces rather
than by scraping text:

| Need | Interface | Cost |
| :--- | :--- | :--- |
| Warm Claude and learn its exact reset | `claude -p hi --output-format stream-json --verbose` → `rate_limit_event` | one tiny prompt |
| Who is Claude signed in as? | `claude auth status --json` | free |
| Warm Codex | `codex exec ... hi` (its session log carries the exact `rate_limits`) | one tiny prompt |
| Who is Codex signed in as? | `codex app-server` → `account/read` (JSON-RPC over stdio) | free |

---

## 🔄 Unattended Operation

Two independent mechanisms — either is enough, and they are safe to combine:

**1. Adaptive watcher (recommended).** `--install-startup` drops a silent `pythonw.exe`
launcher into your Startup folder. On every login it runs `quota_warmer.py --loop`, which
watches every account's window and fires a warm-up *the instant* one resets — no fixed schedule.

**2. Windows Scheduled Task.** `--install-task` registers a plain every-5-hours job.
No admin rights needed.

Safety rails on both:

- **Single instance.** A lock file (`~/.ai_quota_warmer/daemon.lock`) means the Startup
  daemon and an open dashboard never double-ping. Whichever starts first does the warming.
- **Persistent cooldown.** An account is warmed at most once per 5 minutes, and the cooldown
  survives reboots.
- **Exponential backoff.** Consecutive failures back off 5m → 10m → 20m → 40m → 60m, so an
  expired login can never spawn an endless stream of CLI processes.
- **Never warms blind.** If the detector itself errors, no warm-up fires.

---

## 🛠️ Files

| File | Description |
| :--- | :--- |
| `quota_warmer.py` | Main engine: triggers, adaptive watcher, autorun/task install, history |
| `usage_detector.py` | Read-only 5-hour window + token/quota detector, per account folder |
| `app_ui.py` | Local web dashboard (http://127.0.0.1:5055) |
| `gui_app.py` | Native Tkinter desktop app |
| `install_task.ps1` | PowerShell task installer (logon + interval triggers; may need admin) |
| `_env.bat` | Shared helper that locates a working Python for the other `.bat` files |
| `trigger_now.bat` | Warm every account now |
| `check_status.bat` | Show real countdowns, quota %, and history |
| `check_logins.bat` | Show every account and who it is signed in as (free, no prompt sent) |
| `add_account.bat` | Add another Claude Code or Codex account and open its browser login |
| `enable_autorun_on_restart.bat` / `disable_autorun_on_restart.bat` | Toggle the login watcher |
| `install_task_every_5hrs.bat` / `install_task_daily_morning.bat` / `uninstall_task.bat` | Scheduled Task management |
| `start_daemon.bat` | Run the adaptive watcher in a visible window |
| `setup_claude_login.bat` | One-time Claude Code browser login |

---

## 📋 CLI Reference

```
python quota_warmer.py [options]

  --now                  Warm up immediately (default action)
  --target {all,codex,claude}   Limit to one tool's accounts
  --account ACCOUNT      Limit to claude, codex, claude:NAME or codex:NAME (repeatable)
  --prompt TEXT          Prompt to send (default: "hi")
  --force                Ignore the cooldown for --now
  --status               Show real 5-hour windows, quota %, and history
  --list-accounts        Who each account is signed in as, free (alias: --check-login)
  --loop                 Run the adaptive watcher in the foreground

  --add-account TOOL NAME [--dir PATH]         Add an account and open its browser login
  --login ACCOUNT                              Redo the browser login of an account
  --remove-account ACCOUNT                     Stop warming an account (keeps its folder)

  --install-startup / --uninstall-startup      Silent watcher on every login
  --install-task / --uninstall-task            Windows Scheduled Task
  --task-mode {interval,daily,startup}
  --task-time HH:MM      Time for --task-mode daily
  --interval HOURS       Hours between scheduled runs (1-23)
  --no-notify            Suppress desktop notifications

  --codex-path / --claude-path                 Override CLI locations
```

Environment overrides: `CODEX_CLI_PATH`, `CLAUDE_CLI_PATH`, `AI_QUOTA_WARMER_LIVE_CLI`.
`CLAUDE_CONFIG_DIR` and `CODEX_HOME`, if set, pick the folder of the default accounts.

---

## 📂 Data Locations

| Path | Contents |
| :--- | :--- |
| `~/.ai_quota_warmer/accounts.json` | Registered accounts (absent = just the two default accounts) |
| `~/.ai_quota_warmer/history.json` | Last 100 trigger results (atomic writes) |
| `~/.ai_quota_warmer/state.json` | Per-account cooldown, failure counters and the last exact Claude window |
| `~/.ai_quota_warmer/daemon.lock` | Single-instance lock held by the running watcher |
| `~/.claude-NAME`, `~/.codex-NAME` | Config folders (and logins) of added accounts |

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
| Claude warm-ups fail | Run `setup_claude_login.bat` once (added accounts: `python quota_warmer.py --login claude:NAME`) |
| Two accounts show the same email | The browser reused an existing sign-in. Sign out of claude.ai / chatgpt.com (or use a private window), then `--login` the account again |
| `--list-accounts` warns "An API key is configured" | `ANTHROPIC_API_KEY` or an `apiKeyHelper` is set, so warm-ups may be billed to the API instead of starting the subscription window. Unset it |
| Codex account fails with "CODEX_HOME ... does not exist" | Its folder was deleted. Run `--remove-account` and then `--add-account` again |
| `--status` shows `EXPIRED` right after a successful warm-up | Session logs can lag a second or two; re-run `--status` |
| Dashboard says "another daemon is running" | Expected — the Startup watcher already has it covered |
| `install_task.ps1` says "Access is denied" | Use `python quota_warmer.py --install-task` instead (no admin needed) |
| Scheduled task never fires | Confirm it exists: `schtasks /query /tn AIQuotaWarmer_5HourLimit` |
