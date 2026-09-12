"""Shared presentation helpers for the two saved Gameplan views."""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import BORDER, SURFACE, SURFACE_ALT, TEXT


def label(parent, text="", *, size=10, bold=False, color=TEXT, background=SURFACE, **kwargs):
    return tk.Label(parent, text=text, font=("Segoe UI", size, "bold" if bold else "normal"),
                    foreground=color, background=background, anchor="w", **kwargs)


def panel(parent):
    return tk.Frame(parent, background=SURFACE, highlightbackground=BORDER, highlightthickness=1)


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.window = None
        self.job = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def _enter(self, _event=None):
        self.hide()
        self.job = self.widget.after(450, self._show)

    def _show(self):
        self.job = None
        if not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.geometry(f"+{self.widget.winfo_rootx()+12}+{self.widget.winfo_rooty()+self.widget.winfo_height()+5}")
        label(self.window, self.text() if callable(self.text) else self.text,
               size=9, background=SURFACE_ALT, wraplength=400, justify="left", padx=12, pady=9).pack()

    def hide(self, _event=None):
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None


