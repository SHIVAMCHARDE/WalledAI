"""Tkinter confirmation popups.

Tk is not thread-safe, so the broker's HTTP threads never touch widgets. They call
:meth:`TkPresenter.show` / :meth:`TkPresenter.dismiss`, which only put a message on a
queue; the Tk main loop drains the queue every 50 ms and creates or destroys windows.

Each pending request gets its own always-on-top window, so several simultaneous
requests show as a cascade of popups that can be answered in any order.

Safety details:

* Closing the window or pressing Esc counts as **Block**.
* There is no default button: Enter does nothing, so a popup that appears while the
  user is typing can't be approved by accident. The Allow button is also disabled
  for the first ``ARM_DELAY_MS`` milliseconds.
* Tool names and arguments are shown in read-only widgets with control characters
  stripped, so a request can't style itself to look like something else.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from mcp_guard.broker.core import ApprovalBroker, Pending

log = logging.getLogger(__name__)

ARM_DELAY_MS = 700
POLL_MS = 50
POPUP_W, POPUP_H = 560, 470

COLORS = {
    "bg": "#1f2329",
    "panel": "#2a2f37",
    "fg": "#e8eaed",
    "muted": "#9aa0a6",
    "accent": "#f2b84b",
    "allow": "#2e7d32",
    "allow_hover": "#388e3c",
    "block": "#c62828",
    "block_hover": "#d32f2f",
}


def _clean(text: str, limit: int = 200) -> str:
    """Strip control / bidi-override characters and cap the length of a one-line field."""
    out = "".join(ch for ch in text if ch.isprintable() and ch not in "‪‫‬‭‮⁦⁧⁨⁩")
    return out if len(out) <= limit else out[: limit - 1] + "…"


class TkPresenter:
    def __init__(self, broker_getter: Callable[[], ApprovalBroker], max_display_chars: int = 20000):
        self._broker_getter = broker_getter
        self._max_chars = max_display_chars
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._windows: dict[str, "_Popup"] = {}
        self.root = tk.Tk()
        self.root.withdraw()  # no main window: the broker lives in the background
        self.root.title("MCP Guard")

    # -- thread-safe API used by the broker ------------------------------------------------

    def show(self, pending: Pending) -> None:
        self._queue.put(("show", pending))

    def dismiss(self, request_id: str, reason: str) -> None:
        self._queue.put(("dismiss", request_id, reason))

    def stop(self) -> None:
        self._queue.put(("stop",))

    # -- main loop --------------------------------------------------------------------------

    def run(self) -> None:
        self.root.after(POLL_MS, self._drain)
        self.root.mainloop()

    def _drain(self) -> None:
        # Regular wake-ups also let Python deliver Ctrl+C / SIGTERM while Tk is running.
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg[0] == "show":
                    self._open(msg[1])
                elif msg[0] == "dismiss":
                    win = self._windows.pop(msg[1], None)
                    if win:
                        win.close()
                elif msg[0] == "stop":
                    for win in list(self._windows.values()):
                        win.close()
                    self._windows.clear()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        except Exception:
            log.exception("UI error")
        self.root.after(POLL_MS, self._drain)

    def _open(self, pending: Pending) -> None:
        if pending.event.is_set():  # decided (e.g. cancelled) before we got to it
            return
        slot = len(self._windows)
        win = _Popup(self, pending, slot)
        self._windows[pending.request.id] = win

    def _user_decided(self, request_id: str, allow: bool, remember: bool) -> None:
        win = self._windows.pop(request_id, None)
        if win:
            win.close()
        self._broker_getter().resolve(request_id, allow, remember)


class _Popup:
    def __init__(self, presenter: TkPresenter, pending: Pending, slot: int):
        self.presenter = presenter
        self.pending = pending
        req = pending.request
        self.request_id = req.id
        c = COLORS

        top = self.top = tk.Toplevel(presenter.root)
        top.title(f"MCP Guard - approve {req.method}?")
        top.configure(bg=c["bg"])
        top.resizable(True, True)
        top.minsize(460, 380)
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        x = max(0, (sw - POPUP_W) // 2 + 28 * (slot % 8))
        y = max(0, (sh - POPUP_H) // 3 + 28 * (slot % 8))
        top.geometry(f"{POPUP_W}x{POPUP_H}+{x}+{y}")
        top.protocol("WM_DELETE_WINDOW", lambda: self._decide(False))
        top.bind("<Escape>", lambda _e: self._decide(False))

        # Header
        header = tk.Frame(top, bg=c["bg"])
        header.pack(fill="x", padx=18, pady=(16, 6))
        tk.Label(header, text="⚠  Claude wants to run an MCP tool", bg=c["bg"], fg=c["accent"],
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        self.countdown = tk.Label(header, text="", bg=c["bg"], fg=c["muted"], font=("Segoe UI", 9))
        self.countdown.pack(anchor="w", pady=(2, 0))

        # Facts grid
        grid = tk.Frame(top, bg=c["panel"])
        grid.pack(fill="x", padx=18, pady=6)
        label = "Tool" if req.method == "tools/call" else "Target"
        facts = [
            ("MCP server", req.server),
            (label, req.target or "(none)"),
            ("Method", req.method),
            ("Client", f"{req.client}  via {req.transport}"),
            ("Requested", time.strftime("%H:%M:%S", time.localtime(req.created_at))),
        ]
        for row, (k, v) in enumerate(facts):
            tk.Label(grid, text=k, bg=c["panel"], fg=c["muted"], font=("Segoe UI", 9)).grid(
                row=row, column=0, sticky="w", padx=(12, 10), pady=3)
            bold = k in ("MCP server", label)
            tk.Label(grid, text=_clean(str(v)), bg=c["panel"], fg=c["fg"], anchor="w",
                     font=("Segoe UI", 10, "bold" if bold else "normal")).grid(row=row, column=1, sticky="w", pady=3)
        grid.columnconfigure(1, weight=1)

        # Arguments (read-only, scrollable)
        tk.Label(top, text="Arguments", bg=c["bg"], fg=c["muted"], font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(8, 2))
        body = tk.Frame(top, bg=c["bg"])
        body.pack(fill="both", expand=True, padx=18)
        text = tk.Text(body, height=8, wrap="word", bg="#15181c", fg=c["fg"], insertbackground=c["fg"],
                       relief="flat", font=("Consolas", 10), padx=8, pady=6)
        scroll = ttk.Scrollbar(body, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        rendered = json.dumps(req.arguments, indent=2, ensure_ascii=False) if req.arguments else "{}"
        limit = presenter._max_chars
        if len(rendered) > limit:
            rendered = rendered[:limit] + f"\n… ({len(rendered) - limit} more characters not shown)"
        text.insert("1.0", "".join(ch if ch.isprintable() or ch in "\n\t" else "�" for ch in rendered))
        text.configure(state="disabled")

        # Footer
        footer = tk.Frame(top, bg=c["bg"])
        footer.pack(fill="x", padx=18, pady=(10, 16))
        self.remember = tk.BooleanVar(value=False)
        tk.Checkbutton(footer, text="Remember for this tool until MCP Guard restarts", variable=self.remember,
                       bg=c["bg"], fg=c["muted"], selectcolor=c["panel"], activebackground=c["bg"],
                       activeforeground=c["fg"], font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))
        buttons = tk.Frame(footer, bg=c["bg"])
        buttons.pack(fill="x")
        self.block_btn = self._button(buttons, "Block", c["block"], c["block_hover"], lambda: self._decide(False))
        self.block_btn.pack(side="right")
        self.allow_btn = self._button(buttons, "Allow", c["allow"], c["allow_hover"], lambda: self._decide(True))
        self.allow_btn.pack(side="right", padx=(0, 10))
        self.allow_btn.configure(state="disabled")
        top.after(ARM_DELAY_MS, lambda: self.allow_btn.winfo_exists() and self.allow_btn.configure(state="normal"))

        self._bring_to_front()
        self._tick()

    @staticmethod
    def _button(parent: tk.Widget, text: str, color: str, hover: str, cmd: Callable[[], None]) -> tk.Button:
        btn = tk.Button(parent, text=text, command=cmd, bg=color, fg="white", activebackground=hover,
                        activeforeground="white", disabledforeground="#cfcfcf", relief="flat",
                        font=("Segoe UI", 10, "bold"), width=11, pady=6, cursor="hand2")
        return btn

    def _bring_to_front(self) -> None:
        top = self.top
        top.attributes("-topmost", True)
        top.deiconify()
        top.lift()
        try:
            top.focus_force()
        except tk.TclError:
            pass
        if sys.platform == "win32":
            _flash_taskbar(top)

    def _tick(self) -> None:
        if not self.top.winfo_exists():
            return
        remaining = max(0, int(self.pending.deadline - time.time()))
        self.countdown.configure(text=f"Automatically blocked in {remaining}s if you don't answer")
        # Re-assert topmost: other always-on-top windows may have been raised since.
        self.top.attributes("-topmost", True)
        self.top.after(1000, self._tick)

    def _decide(self, allow: bool) -> None:
        self.presenter._user_decided(self.request_id, allow, self.remember.get())

    def close(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass


def _flash_taskbar(top: tk.Toplevel) -> None:
    """Flash the taskbar button so the request is noticed even if focus can't be stolen."""
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

        top.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        log.debug("taskbar flash failed", exc_info=True)
