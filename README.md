# 🚀 AI Quota Warmer (5-Hour Limit Starter)

Automatically pings **OpenAI Codex CLI**, **Anthropic Claude Code CLI** and **Google Gemini**
(through the Antigravity CLI, `agy`) with a minimal prompt (`"hi"`) so your **5-hour rolling
rate-limit window starts unattended** — and keeps watching so the next window opens the moment
the current one expires. Works with **multiple accounts per tool** (e.g. personal + work), Gemini
included.

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
| 6. *(Optional)* Warm Gemini too | Sign in to `agy` once, then `python quota_warmer.py --add-account gemini` | `setup_gemini_login.bat`, then `add_account.bat` |
| 7. *(Optional)* Another Google account | `python quota_warmer.py --add-account gemini work` | `add_account.bat` |

Without an `accounts.json` (a fresh setup), Gemini is warmed automatically as soon as the
Antigravity CLI is installed. Setups that already list their accounts add it once with step 6.

Then optionally open the web dashboard:

```bash
python app_ui.py
```

*(Or double-click `open_ui.bat` for the web dashboard at http://127.0.0.1:5055.)*

---

## 👥 Multiple Accounts

Both CLIs keep each login in a config folder chosen by an environment variable:
`CLAUDE_CONFIG_DIR` for Claude Code and `CODEX_HOME` for Codex. A second account is just a
second folder, so the warmer runs the official CLI once per account with that variable
set. It never reads or stores your credentials. The CLI's own browser login writes them
into the folder.

**Gemini works differently.** The Antigravity CLI has no config-folder variable: its own login (the
**default** Gemini account) is one Google sign-in per Windows user, kept in the Windows Credential
Manager (`gemini:antigravity`) whatever folder it runs in. So each added Gemini account gets a
folder (`~/.gemini-NAME`) that the warmer runs `agy` in as its user profile (`USERPROFILE`), with
`SSH_CONNECTION` set. `agy` treats that like a headless SSH host: it skips the Credential Manager
and keeps the sign-in in a file inside that profile, next to the account's own settings and
conversations. Your main `agy` login is never read or changed.

- A profile folder alone would not be enough: `agy` would still find the main login in the
  Credential Manager. So every `agy` run of an added account is checked in its log to have used the
  account's own sign-in file, and a warm-up first runs the free `agy models` to prove it. Should a
  future `agy` stop honouring the switch, the account fails with a clear error instead of quietly
  warming your main login.
- An added account signs in through `agy`'s own screen, in a console window: pick **Google OAuth**,
  open the link it shows in a private/incognito window, sign in with the Google account you want,
  and paste the code that page shows back into the window. The dashboard adds the account as soon
  as the sign-in is saved. `--list-accounts` (or **🔍 Check Logins**) shows which Google account
  each Gemini account is signed in as.
- To use an added account yourself, `python quota_warmer.py --login gemini:NAME` opens `agy` as it.
- Checked with agy 1.3.

**From the web dashboard:** click **➕ Add Account**, pick the tool, name the
account and sign in. Each card also has **Sign in again** and **Remove**.

**From a terminal** (or double-click `add_account.bat`):

```bash
python quota_warmer.py --add-account claude work      # creates ~/.claude-work, opens the browser login
python quota_warmer.py --add-account codex personal   # creates ~/.codex-personal
python quota_warmer.py --add-account gemini work      # creates ~/.gemini-work, opens agy's sign-in
python quota_warmer.py --list-accounts                # who each account is signed in as (free)
```

**Signing in with a second account.** The sign-in opens your default browser, which is
probably still signed in to your first account. Close that tab and copy the sign-in link
(shown in the UI, or printed in the terminal) into a private/incognito window. Then sign
in there with the account you want. Codex finishes on its own. Claude shows a code at the
end: paste it into the UI's code field, or into the terminal's
`Paste code here if prompted >` prompt.

- An account is only added once its sign-in succeeds, so the watcher never tries to warm
  an account that is still signing in.
- Your existing logins stay as the **default** `claude` and `codex` accounts, so
  single-account setups need no changes.
- Each account has its own window detection, cooldown, backoff and history, and all
  accounts warm in parallel. A running watcher or dashboard picks up a new account within
  about 15 seconds.
- `--list-accounts` (or **🔍 Check Logins**) shows each account's email and warns when two
  look identical.
- A signed-out account is caught on its own, for free: Claude accounts within about five
  minutes (when the per-minute usage read fails), Gemini accounts within five (their quota
  read only answers signed in), Codex accounts within ten, and any account whose warm-up
  fails with a sign-in error. Its card turns **SIGNED OUT** with the
  reason and a **Sign in again** button, the watcher shows a desktop notification, and
  `--status` prints the fix. The badge clears once the account works again. A sign-in that
  failed only because the network was down does not count.
- Gemini signs in from `agy`'s own full-screen interface, so **Sign in again** on a Gemini card
  opens `agy` in a console window, as that account. Sign in there, then type `/exit`.
- An added Gemini account with no sign-in saved is reported signed out at once, without running
  `agy` (which would wait a minute for a sign-in).
- For added accounts, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`,
  `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` and `AGY_ADC_AUTH` are
  removed from the environment. Credentials in the environment override the folder's login and
  would make every account warm the same one.
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
| **Claude Code** | Claude Code's own usage report, re-read **every minute**; the `rate_limit_event` from each warm-up; activity in `<config dir>/projects/**/*.jsonl` as a fallback | **Exact.** Each minute the watcher asks the CLI for the account's plan usage (the numbers `/usage` shows), so a window opened anywhere (claude.ai, the desktop app, another PC) is picked up within a minute, and a window that has closed is warmed straight away. Only when that read fails (account signed out, offline, older CLI) is the window **inferred** from when your first message landed |
| **Gemini** | `agy -p /usage --output-format json`, re-read **every five minutes** and right after each warm-up | **Exact.** Antigravity reports the Gemini 5-hour and weekly buckets with their reset times. A full 5-hour bucket means no window is open. `agy` keeps no local record of its quota, so without a reading (signed out, offline) nothing is shown |

The status line shows which source you're looking at: `usage-api`, `rate-limit-event` and
`rate-limit-header` are exact, and `session-logs` is inferred. A Gemini window shown as
`warm-up` is the one a warm-up opened when the read right after it failed (it assumes the
window opened then). The next read replaces it.

**Gemini details.** In Antigravity, Gemini Flash and Pro share one 5-hour and one weekly limit.
Claude and GPT models run through `agy` have their own pair, which the warmer leaves alone.
Warm-ups use the newest Gemini Flash at low effort (`--gemini-model flash-low`), about 0.05% of
the 5-hour quota each. While the **weekly** limit is used up the card shows **LIMITED** and the
watcher does not warm until the weekly reset: a warm-up could only fail then, or be billed to
your AI credits when **Use AI Credits** is on. A warm-up only counts once the read shows the
5-hour window open, so a request that answered without opening it backs off instead of repeating.

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
- **Gemini:** the Antigravity CLI keeps its sign-in in the system keyring (added accounts: in a
  file `agy` writes into the account's folder), and its print mode (`-p` with
  `--output-format json`) is the interface it offers scripts.

So the warmer uses the CLIs, but through their structured, official interfaces rather
than by scraping text:

| Need | Interface | Cost |
| :--- | :--- | :--- |
| Warm Claude and learn its exact reset | `claude -p hi --output-format stream-json --verbose` → `rate_limit_event` | one tiny prompt |
| Claude's current 5-hour window, every minute | `claude -p --input-format stream-json --no-session-persistence` → `get_usage` control request | free, no prompt sent, ~1.5s of CPU per account per minute |
| Who is Claude signed in as? | `claude auth status --json` | free |
| Warm Codex | `codex exec ... hi` (its session log carries the exact `rate_limits`) | one tiny prompt |
| Who is Codex signed in as? | `codex app-server` → `account/read` (JSON-RPC over stdio) | free |
| Warm Gemini | `agy -p hi --output-format json --model <newest gemini-*-flash-low>` | one tiny prompt |
| Gemini's 5-hour and weekly windows, and whether it is signed in | `agy -p /usage --output-format json` | free: no agent turn, no quota, no conversation left behind |
| Who is Gemini signed in as? | `agy models`, then the account `agy` logs that it signed in as | free |

The warmer's own `agy` runs start in an empty folder (`~/.ai_quota_warmer/gemini-warmup`), log
to `~/.ai_quota_warmer/agy-*.log` (one per account) instead of overwriting `agy`'s own `cli.log`, and set
`AGY_CLI_DISABLE_AUTO_UPDATE=true`, so a background read never downloads and swaps the CLI.
`agy` still updates itself whenever you use it. Each warm-up shows up as a short "hi"
conversation in `agy`'s `/resume` list.

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
| `install_task.ps1` | PowerShell task installer (logon + interval triggers; may need admin) |
| `_env.bat` | Shared helper that locates a working Python for the other `.bat` files |
| `trigger_now.bat` | Warm every account now |
| `check_status.bat` | Show real countdowns, quota %, and history |
| `check_logins.bat` | Show every account and who it is signed in as (free, no prompt sent) |
| `add_account.bat` | Add another Claude Code, Codex or Gemini account and open its sign-in |
| `enable_autorun_on_restart.bat` / `disable_autorun_on_restart.bat` | Toggle the login watcher |
| `install_task_every_5hrs.bat` / `install_task_daily_morning.bat` / `uninstall_task.bat` | Scheduled Task management |
| `start_daemon.bat` | Run the adaptive watcher in a visible window |
| `setup_claude_login.bat` | One-time Claude Code browser login |
| `setup_gemini_login.bat` | One-time Antigravity CLI (`agy`) sign-in for Gemini |

---

## 📋 CLI Reference

```
python quota_warmer.py [options]

  --now                  Warm up immediately (default action)
  --target {all,codex,claude,gemini}   Limit to one tool's accounts
  --account ACCOUNT      Limit to claude, codex, gemini or TOOL:NAME, e.g. gemini:work (repeatable)
  --prompt TEXT          Prompt to send (default: "hi")
  --claude-model / --codex-model / --gemini-model MODEL
                         Warm-up models (default: haiku / luna / flash-low, the newest
                         Gemini Flash at low effort; also flash-medium, pro-low, ... or an id)
  --force                Ignore the cooldown for --now
  --status               Show real 5-hour windows, quota %, and history
  --list-accounts        Who each account is signed in as, free (alias: --check-login)
  --loop                 Run the adaptive watcher in the foreground

  --add-account TOOL [NAME] [--dir PATH]       Add an account and open its sign-in
                                               (NAME defaults to "default", the CLI's own login)
  --login ACCOUNT                              Redo the browser login of an account
  --remove-account ACCOUNT                     Stop warming an account (keeps its folder)

  --install-startup / --uninstall-startup      Silent watcher on every login
  --install-task / --uninstall-task            Windows Scheduled Task
  --task-mode {interval,daily,startup}
  --task-time HH:MM      Time for --task-mode daily
  --interval HOURS       Hours between scheduled runs (1-23)
  --no-notify            Suppress desktop notifications

  --codex-path / --claude-path / --agy-path    Override CLI locations
```

Environment overrides: `CODEX_CLI_PATH`, `CLAUDE_CLI_PATH`, `AGY_CLI_PATH`, `AI_QUOTA_WARMER_LIVE_CLI`,
and `AQW_CLAUDE_MODEL` / `AQW_CODEX_MODEL` / `AQW_GEMINI_MODEL` for the model defaults.
`CLAUDE_CONFIG_DIR` and `CODEX_HOME`, if set, pick the folder of the default accounts.

---

## 📂 Data Locations

| Path | Contents |
| :--- | :--- |
| `~/.ai_quota_warmer/accounts.json` | Registered accounts (absent = just the two default accounts) |
| `~/.ai_quota_warmer/history.json` | Last 100 trigger results (atomic writes) |
| `~/.ai_quota_warmer/state.json` | Per-account cooldown, failure counters and the last exact Claude and Gemini windows |
| `~/.ai_quota_warmer/daemon.lock` | Single-instance lock held by the running watcher |
| `~/.ai_quota_warmer/gemini-warmup/` | Empty working folder for the warmer's `agy` runs |
| `~/.ai_quota_warmer/agy-warmup.log`, `agy-usage.log`, `agy-models.log` | Logs of the warmer's last `agy` warm-up, quota read and sign-in check (`agy-*-NAME.log` for added Gemini accounts) |
| `~/.claude-NAME`, `~/.codex-NAME` | Config folders (and logins) of added accounts |
| `~/.gemini-NAME` | Profile folder of an added Gemini account: `agy`'s data and its sign-in file, under `.gemini\` |

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
| Two accounts show the same email | The browser reused an existing sign-in. Use **Sign in again** on that account (or `--login`) and open the sign-in link in a private/incognito window |
| `--list-accounts` warns "An API key is configured" | `ANTHROPIC_API_KEY` or an `apiKeyHelper` is set, so warm-ups may be billed to the API instead of starting the subscription window. Unset it |
| Codex account fails with "CODEX_HOME ... does not exist" | Its folder was deleted. Run `--remove-account` and then `--add-account` again |
| Gemini shows "Antigravity CLI (agy) not found" | Install the Antigravity CLI, or point `--agy-path` / `AGY_CLI_PATH` at `agy.exe` |
| Gemini is **SIGNED OUT** ("You are not logged into Antigravity") | Run `setup_gemini_login.bat` (or `agy`) and sign in, or use **Sign in again** on its card |
| Gemini warm-up: "the Gemini 5-hour window did not open" | The request went to a non-Gemini model or was billed to AI credits. Check `--gemini-model`, and the weekly limit in `--status` |
| Gemini is **LIMITED** | Its weekly limit is used up. The watcher resumes at the weekly reset shown on the card |
| Added Gemini account: "agy did not use ... own sign-in" | The installed `agy` no longer keeps sign-ins apart the way this version expects, so nothing is warmed for that account. Your main login is untouched; update AI Quota Warmer |
| Two Gemini accounts show the same email | The browser picked the already signed-in Google account. Use **Sign in again** on that card, type `/logout` and then `/login`, and open the link in a private/incognito window |
| `--status` shows `EXPIRED` right after a successful warm-up | Session logs can lag a second or two; re-run `--status` |
| Dashboard says "another daemon is running" | Expected — the Startup watcher already has it covered |
| `install_task.ps1` says "Access is denied" | Use `python quota_warmer.py --install-task` instead (no admin needed) |
| Scheduled task never fires | Confirm it exists: `schtasks /query /tn AIQuotaWarmer_5HourLimit` |
