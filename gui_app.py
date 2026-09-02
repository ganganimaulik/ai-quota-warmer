#!/usr/bin/env python3
"""
AI Quota Warmer - Native Desktop GUI (Tkinter) with Actual 5-Hour Usage Detector
---------------------------------------------------------------------------------
A lightweight desktop app displaying real-time actual 5-hour limit usage for Claude & Codex.

Threading model: Tk is not thread-safe, so every widget update happens on the
main thread. Telemetry reads (which walk hundreds of session logs) and warm-up
subprocesses run on worker threads and hand results back via `root.after`.
"""

import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk

from quota_warmer import (
    run_trigger_batch,
    is_startup_installed,
    install_startup_autorun,
    uninstall_startup_autorun,
    cooldown_remaining,
    load_state,
)
from usage_detector import get_actual_usage_summary

REFRESH_MS = 5000


class QuotaWarmerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AI Quota Warmer - Actual 5-Hour Limit Detector")
        self.root.geometry("640x720")
        self.root.minsize(580, 640)
        self.root.configure(bg="#0a0e17")

        self._ui_queue = queue.Queue()
        self._refresh_inflight = False
        self._busy = False
        self._closing = False
        self._pump_id = None
        self._tick_id = None

        self.setup_styles()
        self.create_widgets()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pump_queue()
        self.tick_timer()

    def setup_styles(self):
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure("Claude.Horizontal.TProgressbar", thickness=8, background="#a855f7")
        self.style.configure("Codex.Horizontal.TProgressbar", thickness=8, background="#06b6d4")

    def create_widgets(self):
        # Header
        hdr = tk.Frame(self.root, bg="#121826", padx=20, pady=14)
        hdr.pack(fill="x")

        tk.Label(
            hdr,
            text="⚡ AI Quota Warmer & Actual 5-Hour Usage",
            font=("Segoe UI", 15, "bold"),
            fg="#f8fafc",
            bg="#121826",
        ).pack(anchor="w")

        tk.Label(
            hdr,
            text="Real-time 5-Hour Rate Limit Telemetry for Claude Code & Codex CLI",
            font=("Segoe UI", 9),
            fg="#94a3b8",
            bg="#121826",
        ).pack(anchor="w")

        content = tk.Frame(self.root, bg="#0a0e17", padx=16, pady=12)
        content.pack(fill="both", expand=True)

        # 1. Claude Card
        claude_card = tk.Frame(content, bg="#121826", padx=14, pady=12,
                               highlightbackground="#a855f7", highlightthickness=1)
        claude_card.pack(fill="x", pady=(0, 10))

        cl_hdr = tk.Frame(claude_card, bg="#121826")
        cl_hdr.pack(fill="x")
        tk.Label(cl_hdr, text="🟣 Anthropic Claude Code", font=("Segoe UI", 11, "bold"),
                 fg="#c084fc", bg="#121826").pack(side="left")
        self.lbl_claude_badge = tk.Label(cl_hdr, text="...", font=("Segoe UI", 8, "bold"),
                                         fg="#94a3b8", bg="#121826")
        self.lbl_claude_badge.pack(side="right")

        self.lbl_claude_timer = tk.Label(claude_card, text="--:--:--", font=("Consolas", 24, "bold"),
                                         fg="#ffffff", bg="#121826")
        self.lbl_claude_timer.pack(pady=2)

        self.claude_prog = ttk.Progressbar(claude_card, style="Claude.Horizontal.TProgressbar",
                                           orient="horizontal", mode="determinate")
        self.claude_prog.pack(fill="x", pady=4)

        self.lbl_claude_details = tk.Label(claude_card, text="Loading telemetry...",
                                           font=("Segoe UI", 8), fg="#cbd5e1", bg="#121826",
                                           wraplength=560, justify="center")
        self.lbl_claude_details.pack()

        # 2. Codex Card
        codex_card = tk.Frame(content, bg="#121826", padx=14, pady=12,
                              highlightbackground="#06b6d4", highlightthickness=1)
        codex_card.pack(fill="x", pady=(0, 10))

        cx_hdr = tk.Frame(codex_card, bg="#121826")
        cx_hdr.pack(fill="x")
        tk.Label(cx_hdr, text="🤖 OpenAI Codex CLI", font=("Segoe UI", 11, "bold"),
                 fg="#38bdf8", bg="#121826").pack(side="left")
        self.lbl_codex_badge = tk.Label(cx_hdr, text="...", font=("Segoe UI", 8, "bold"),
                                        fg="#94a3b8", bg="#121826")
        self.lbl_codex_badge.pack(side="right")

        self.lbl_codex_timer = tk.Label(codex_card, text="--:--:--", font=("Consolas", 24, "bold"),
                                        fg="#ffffff", bg="#121826")
        self.lbl_codex_timer.pack(pady=2)

        self.codex_prog = ttk.Progressbar(codex_card, style="Codex.Horizontal.TProgressbar",
                                          orient="horizontal", mode="determinate")
        self.codex_prog.pack(fill="x", pady=4)

        self.lbl_codex_details = tk.Label(codex_card, text="Loading telemetry...",
                                          font=("Segoe UI", 8), fg="#cbd5e1", bg="#121826",
                                          wraplength=560, justify="center")
        self.lbl_codex_details.pack()

        # Buttons
        btn_frame = tk.Frame(content, bg="#0a0e17")
        btn_frame.pack(fill="x", pady=(0, 10))

        self.btn_warm_all = tk.Button(
            btn_frame, text="🔥 Warm Up Both Now", font=("Segoe UI", 10, "bold"),
            bg="#6366f1", fg="white", relief="flat", pady=8, cursor="hand2",
            command=lambda: self.start_warmup("all"),
        )
        self.btn_warm_all.pack(fill="x", pady=(0, 4))

        sub_btns = tk.Frame(btn_frame, bg="#0a0e17")
        sub_btns.pack(fill="x")

        self.btn_warm_claude = tk.Button(
            sub_btns, text="🟣 Warm Claude", font=("Segoe UI", 8, "bold"),
            bg="#7c3aed", fg="white", relief="flat", pady=5,
            command=lambda: self.start_warmup("claude"),
        )
        self.btn_warm_claude.pack(side="left", fill="x", expand=True, padx=(0, 2))

        self.btn_warm_codex = tk.Button(
            sub_btns, text="🤖 Warm Codex", font=("Segoe UI", 8, "bold"),
            bg="#0284c7", fg="white", relief="flat", pady=5,
            command=lambda: self.start_warmup("codex"),
        )
        self.btn_warm_codex.pack(side="right", fill="x", expand=True, padx=(2, 0))

        # Settings
        settings_frame = tk.Frame(content, bg="#121826", padx=12, pady=8)
        settings_frame.pack(fill="x", pady=(0, 10))

        self.startup_var = tk.BooleanVar(value=is_startup_installed())
        tk.Checkbutton(
            settings_frame, text="Auto-Run on Windows Restart / Boot",
            variable=self.startup_var, command=self.toggle_startup,
            font=("Segoe UI", 8, "bold"), fg="#f8fafc", bg="#121826",
            selectcolor="#0a0e17", activebackground="#121826", activeforeground="#f8fafc",
        ).pack(side="left")

        tk.Button(
            settings_frame, text="🔄 Refresh", font=("Segoe UI", 8),
            bg="#334155", fg="white", relief="flat", command=self.request_refresh,
        ).pack(side="right")

        # Console
        self.txt_log = tk.Text(content, bg="#06090e", fg="#94a3b8", font=("Consolas", 8),
                               height=6, relief="flat", padx=6, pady=6)
        self.txt_log.pack(fill="both", expand=True)
        self.log("Actual 5-Hour Usage Detector Desktop UI ready.")

    # -- thread-safe plumbing ------------------------------------------------

    def _alive(self):
        if self._closing:
            return False
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def post(self, fn):
        """Queues a callable to run on the Tk main thread."""
        self._ui_queue.put(fn)

    def pump_queue(self):
        self._pump_id = None
        if not self._alive():
            return
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except tk.TclError:
                return
        if self._alive():
            self._pump_id = self.root.after(100, self.pump_queue)

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{ts}] {msg}\n")
        # Keep the widget bounded; an all-day session would grow forever.
        if int(self.txt_log.index("end-1c").split(".")[0]) > 500:
            self.txt_log.delete("1.0", "200.0")
        self.txt_log.see("end")

    def set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_warm_all.config(state=state,
                                 text="⏳ Pinging AI Engines..." if busy else "🔥 Warm Up Both Now")
        self.btn_warm_claude.config(state=state)
        self.btn_warm_codex.config(state=state)

    # -- status --------------------------------------------------------------

    def request_refresh(self):
        """Reads telemetry on a worker thread; the UI never blocks on disk I/O."""
        if self._refresh_inflight or not self._alive():
            return
        self._refresh_inflight = True

        def _worker():
            try:
                data = get_actual_usage_summary()
                state = load_state()
                cooldowns = {t: cooldown_remaining(t, state) for t in ("claude", "codex")}
                failures = {t: int((state.get(t) or {}).get("consecutive_failures", 0))
                            for t in ("claude", "codex")}
            except Exception as e:
                self.post(lambda: self._refresh_failed(e))
                return
            self.post(lambda: self.apply_status(data, cooldowns, failures))

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_failed(self, exc):
        self._refresh_inflight = False
        self.log(f"Telemetry read failed: {type(exc).__name__}: {exc}")

    def apply_status(self, actual, cooldowns, failures):
        self._refresh_inflight = False
        if not self._alive():
            return
        try:
            self._render("claude", actual["claude"], cooldowns["claude"], failures["claude"])
            self._render("codex", actual["codex"], cooldowns["codex"], failures["codex"])
            self.startup_var.set(is_startup_installed())
        except tk.TclError:
            pass

    def _render(self, tool, data, cooldown, failures):
        badge = self.lbl_claude_badge if tool == "claude" else self.lbl_codex_badge
        timer = self.lbl_claude_timer if tool == "claude" else self.lbl_codex_timer
        prog = self.claude_prog if tool == "claude" else self.codex_prog
        details = self.lbl_claude_details if tool == "claude" else self.lbl_codex_details

        if data.get("status") == "ERROR":
            colour = "#f43f5e"
        elif data.get("is_active"):
            colour = "#10b981"
        elif data.get("has_data"):
            colour = "#f59e0b"
        else:
            colour = "#94a3b8"

        badge.config(text=data.get("status", "?"), fg=colour)
        timer.config(text=data.get("time_remaining", "--:--:--"))
        prog["value"] = data.get("progress_pct", 0)

        if data.get("status") == "ERROR":
            details.config(text=f"Detector error: {data.get('error')}")
            return

        counter = (f"Prompts: {data.get('user_prompts', 0)}" if tool == "claude"
                   else f"Turns: {data.get('turns_in_5h', 0)}")
        line = (f"Window: {data.get('window_start')} → Resets: {data.get('window_reset')}  |  "
                f"{counter}  |  Tokens: {data.get('tokens', {}).get('total', 0):,}")
        if data.get("session_used_pct") is not None:
            line += f"  |  Quota: {data['session_used_pct']}%"
        if not data.get("is_active"):
            if failures:
                line += f"\n{failures} failed warm-up(s) — next retry in {int(cooldown // 60)}m {int(cooldown % 60)}s"
            elif cooldown > 0:
                line += f"\nCooling down {int(cooldown // 60)}m {int(cooldown % 60)}s before the next auto-warm"
        details.config(text=line)

    def tick_timer(self):
        self._tick_id = None
        if not self._alive():
            return
        self.request_refresh()
        self._tick_id = self.root.after(REFRESH_MS, self.tick_timer)

    # -- actions -------------------------------------------------------------

    def toggle_startup(self):
        if self.startup_var.get():
            ok = install_startup_autorun()
            self.log("Auto-run on PC Restart: ENABLED" if ok else "Failed to enable auto-run")
        else:
            ok = uninstall_startup_autorun()
            self.log("Auto-run on PC Restart: DISABLED" if ok else "Failed to disable auto-run")
        self.startup_var.set(is_startup_installed())

    def start_warmup(self, target="all"):
        if self._busy:
            return
        self.set_busy(True)
        self.log(f"Starting warmup for {target.upper()} (may take up to a minute)...")

        def _worker():
            try:
                results = run_trigger_batch(target=target, prompt="hi", notify=True, quiet=True)
            except Exception as e:
                self.post(lambda: self.log(f"Warm-up crashed: {type(e).__name__}: {e}"))
                results = {}
            for name, res in results.items():
                msg = (f"{name.upper()}: SUCCESS in {res.get('duration')}s" if res.get("success")
                       else f"{name.upper()}: FAILED - {res.get('error')}")
                self.post(lambda m=msg: self.log(m))
            self.post(self._warmup_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _warmup_done(self):
        self.set_busy(False)
        self.request_refresh()

    def on_close(self):
        # Cancel pending `after` callbacks first; otherwise they fire against a
        # half-torn-down interpreter and Tk prints "invalid command name".
        self._closing = True
        for attr in ("_pump_id", "_tick_id"):
            token = getattr(self, attr, None)
            if token is not None:
                try:
                    self.root.after_cancel(token)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main():
    # A cp1252 console would otherwise raise UnicodeEncodeError on any non-ASCII
    # text that reaches stdout from a worker thread.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    root = tk.Tk()
    QuotaWarmerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
