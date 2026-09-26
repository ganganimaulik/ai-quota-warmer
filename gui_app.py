#!/usr/bin/env python3
"""
AI Quota Warmer - Native Desktop GUI (Tkinter) with Actual 5-Hour Usage Detector
---------------------------------------------------------------------------------
A lightweight desktop app displaying real-time actual 5-hour limit usage for every
configured Claude & Codex account.

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
    load_accounts,
    find_account,
    get_all_usage,
)

REFRESH_MS = 5000
SCREEN_MARGIN = 80       # title bar + taskbar allowance when sizing the window


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
        self.cards = {}          # account key -> widgets of its card
        self.card_keys = None    # account keys the cards were built for
        self._scrolling = False  # cards taller than the screen allows

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
            text="Real-time 5-Hour Rate Limit Telemetry for every Claude Code & Codex CLI account",
            font=("Segoe UI", 9),
            fg="#94a3b8",
            bg="#121826",
        ).pack(anchor="w")

        content = tk.Frame(self.root, bg="#0a0e17", padx=16, pady=12)
        content.pack(fill="both", expand=True)

        # 1. One card per account, (re)built by _build_cards once telemetry
        #    arrives. They sit on a canvas so any number of accounts can scroll.
        cards_box = tk.Frame(content, bg="#0a0e17")
        cards_box.pack(fill="x")
        self.cards_scroll = ttk.Scrollbar(cards_box, orient="vertical")
        self.cards_canvas = tk.Canvas(cards_box, bg="#0a0e17", highlightthickness=0, bd=0, height=60,
                                      yscrollcommand=self.cards_scroll.set)
        self.cards_scroll.config(command=self.cards_canvas.yview)
        self.cards_canvas.pack(side="left", fill="x", expand=True)
        self.cards_frame = tk.Frame(self.cards_canvas, bg="#0a0e17")
        self._cards_item = self.cards_canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.cards_frame.bind("<Configure>", lambda _e: self._fit_cards())
        self.cards_canvas.bind("<Configure>",
                               lambda e: self.cards_canvas.itemconfigure(self._cards_item, width=e.width))
        # The wheel scrolls the cards only while the pointer is over them.
        self.cards_canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.cards_canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))
        tk.Label(self.cards_frame, text="Loading accounts...", font=("Segoe UI", 9),
                 fg="#94a3b8", bg="#0a0e17").pack(pady=8)

        # 2. Buttons
        btn_frame = tk.Frame(content, bg="#0a0e17")
        btn_frame.pack(fill="x", pady=(4, 10))

        self.btn_warm_all = tk.Button(
            btn_frame, text="🔥 Warm Up All Now", font=("Segoe UI", 10, "bold"),
            bg="#6366f1", fg="white", relief="flat", pady=8, cursor="hand2",
            command=lambda: self.start_warmup("all"),
        )
        self.btn_warm_all.pack(fill="x", pady=(0, 4))

        # 3. Settings
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

        # 4. Console
        self.txt_log = tk.Text(content, bg="#06090e", fg="#94a3b8", font=("Consolas", 8),
                               height=6, relief="flat", padx=6, pady=6)
        self.txt_log.pack(fill="both", expand=True)
        self.log("Actual 5-Hour Usage Detector Desktop UI ready.")

    def _build_cards(self, accounts):
        """Creates one card per account; called again whenever the list changes."""
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.cards = {}
        self.card_keys = [a["key"] for a in accounts]

        if not accounts:
            tk.Label(
                self.cards_frame,
                text="No accounts configured.\nAdd one with: python quota_warmer.py --add-account claude work",
                font=("Segoe UI", 9), fg="#94a3b8", bg="#0a0e17", justify="center",
            ).pack(pady=8)

        # Two accounts keep the classic large timer; more get a compact one.
        timer_size = 24 if len(accounts) <= 2 else 18
        for acc in accounts:
            is_claude = acc["tool"] == "claude"
            card = tk.Frame(self.cards_frame, bg="#121826", padx=14, pady=10,
                            highlightbackground="#a855f7" if is_claude else "#06b6d4",
                            highlightthickness=1)
            card.pack(fill="x", pady=(0, 10))

            head = tk.Frame(card, bg="#121826")
            head.pack(fill="x")
            tk.Label(head, text=f"{'🟣' if is_claude else '🤖'} {acc['label']}",
                     font=("Segoe UI", 11, "bold"), fg="#c084fc" if is_claude else "#38bdf8",
                     bg="#121826").pack(side="left")
            button = tk.Button(
                head, text="Warm", font=("Segoe UI", 8, "bold"),
                bg="#7c3aed" if is_claude else "#0284c7", fg="white", relief="flat", padx=10,
                state="disabled" if self._busy else "normal",
                command=lambda k=acc["key"]: self.start_warmup(account=k),
            )
            button.pack(side="right", padx=(8, 0))
            badge = tk.Label(head, text="...", font=("Segoe UI", 8, "bold"), fg="#94a3b8", bg="#121826")
            badge.pack(side="right")

            tk.Label(card, text=acc["dir"], font=("Segoe UI", 7), fg="#64748b", bg="#121826").pack(anchor="w")
            timer = tk.Label(card, text="--:--:--", font=("Consolas", timer_size, "bold"),
                             fg="#ffffff", bg="#121826")
            timer.pack(pady=1)
            prog = ttk.Progressbar(card, style=f"{'Claude' if is_claude else 'Codex'}.Horizontal.TProgressbar",
                                   orient="horizontal", mode="determinate")
            prog.pack(fill="x", pady=4)
            details = tk.Label(card, text="Loading telemetry...", font=("Segoe UI", 8), fg="#cbd5e1",
                               bg="#121826", wraplength=560, justify="center")
            details.pack()

            self.cards[acc["key"]] = {"tool": acc["tool"], "badge": badge, "timer": timer,
                                      "prog": prog, "details": details, "button": button}

        self.root.update_idletasks()
        self._fit_cards()
        # Grow the window to fit the cards, never beyond the screen.
        self.root.update_idletasks()
        target = min(self.root.winfo_reqheight(), self.root.winfo_screenheight() - SCREEN_MARGIN)
        if self.root.winfo_height() < target:
            self.root.geometry(f"{max(self.root.winfo_width(), 640)}x{target}")

    def _fit_cards(self):
        """Shows every card while they fit on screen; past that, the cards scroll."""
        needed = self.cards_frame.winfo_reqheight()
        # Everything but the cards: header, buttons, settings and console.
        chrome = self.root.winfo_reqheight() - self.cards_canvas.winfo_reqheight()
        room = max(200, self.root.winfo_screenheight() - SCREEN_MARGIN - chrome)
        self.cards_canvas.configure(height=min(needed, room), scrollregion=(0, 0, 0, needed))
        scrolling = needed > room
        if scrolling and not self._scrolling:
            self.cards_scroll.pack(side="right", fill="y", before=self.cards_canvas)
        elif not scrolling and self._scrolling:
            self.cards_scroll.pack_forget()
            self.cards_canvas.yview_moveto(0)
        self._scrolling = scrolling

    def _bind_wheel(self, on):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            if on:
                self.root.bind_all(seq, self._on_wheel)
            else:
                self.root.unbind_all(seq)

    def _on_wheel(self, event):
        if not self._scrolling:
            return
        # Windows/macOS send <MouseWheel> with a delta; X11 sends buttons 4/5.
        up = getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0
        self.cards_canvas.yview_scroll(-1 if up else 1, "units")

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
                                 text="⏳ Pinging AI Engines..." if busy else "🔥 Warm Up All Now")
        for widgets in self.cards.values():
            widgets["button"].config(state=state)

    # -- status --------------------------------------------------------------

    def request_refresh(self):
        """Reads telemetry on a worker thread; the UI never blocks on disk I/O."""
        if self._refresh_inflight or not self._alive():
            return
        self._refresh_inflight = True

        def _worker():
            try:
                accounts = load_accounts()
                state = load_state()
                usage = get_all_usage(accounts, state)
                snapshot = [
                    dict(acc.describe(), usage=usage[acc.key],
                         cooldown=cooldown_remaining(acc.key, state),
                         failures=int((state.get(acc.key) or {}).get("consecutive_failures", 0)))
                    for acc in accounts
                ]
            except Exception as e:
                # Bind now: `e` is unset once this except block ends.
                self.post(lambda exc=e: self._refresh_failed(exc))
                return
            self.post(lambda: self.apply_status(snapshot))

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_failed(self, exc):
        self._refresh_inflight = False
        self.log(f"Telemetry read failed: {type(exc).__name__}: {exc}")

    def apply_status(self, accounts):
        self._refresh_inflight = False
        if not self._alive():
            return
        try:
            if [a["key"] for a in accounts] != self.card_keys:
                self._build_cards(accounts)
            for acc in accounts:
                self._render(acc)
            self.startup_var.set(is_startup_installed())
        except tk.TclError:
            pass

    def _render(self, acc):
        widgets = self.cards.get(acc["key"])
        if not widgets:
            return
        data, cooldown, failures = acc["usage"], acc["cooldown"], acc["failures"]

        if data.get("status") == "ERROR":
            colour = "#f43f5e"
        elif data.get("is_active"):
            colour = "#10b981"
        elif data.get("has_data"):
            colour = "#f59e0b"
        else:
            colour = "#94a3b8"

        widgets["badge"].config(text=data.get("status", "?"), fg=colour)
        widgets["timer"].config(text=data.get("time_remaining", "--:--:--"))
        widgets["prog"]["value"] = data.get("progress_pct", 0)

        if data.get("status") == "ERROR":
            widgets["details"].config(text=f"Detector error: {data.get('error')}")
            return

        counter = (f"Prompts: {data.get('user_prompts', 0)}" if acc["tool"] == "claude"
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
        widgets["details"].config(text=line)

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

    def start_warmup(self, target="all", account=None):
        """Warms every account matching `target`, or just the account key given."""
        if self._busy:
            return
        self.set_busy(True)
        self.log(f"Starting warmup for {account or target.upper()} (may take up to a minute)...")

        def _worker():
            try:
                accounts = None
                if account:
                    acc = find_account(account)
                    if acc is None:
                        raise ValueError(f"account '{account}' no longer exists")
                    accounts = [acc]
                results = run_trigger_batch(target=account or target, prompt="hi", notify=True,
                                            quiet=True, accounts=accounts)
            except Exception as e:
                msg = f"Warm-up crashed: {type(e).__name__}: {e}"
                self.post(lambda m=msg: self.log(m))
                results = {}
            for key, res in results.items():
                msg = (f"{key}: SUCCESS in {res.get('duration')}s" if res.get("success")
                       else f"{key}: FAILED - {res.get('error')}")
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
