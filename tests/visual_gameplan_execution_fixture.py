"""Open only the native Stats tab for offline visual QA; no application services.

Use --datastore with saved prediction/execution reports. F6/F7 select the two
review sizes; Escape closes this preview. This is never a trader launcher.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.gameplan_stats import GameplanStatsTab
from app.ui.theme import (
    ACCENT, BACKGROUND, BORDER, FIELD_BACKGROUND, MUTED_TEXT, SURFACE,
    SURFACE_ALT, TABLE_FIELD, TEXT,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--size", default="1180x760")
    args = parser.parse_args()
    root = tk.Tk()
    root.title("Ducketz Stats · offline visual review")
    root.geometry(args.size + "+30+30")
    root.configure(background=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use("clam")
    root.option_add("*Font", "{Segoe UI} 10")
    root.option_add("*TCombobox*Listbox.background", FIELD_BACKGROUND)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    style.configure(".", background=BACKGROUND, foreground=TEXT,
                    fieldbackground=TABLE_FIELD, font=("Segoe UI", 10))
    style.configure("TFrame", background=BACKGROUND)
    style.configure("TButton", background=SURFACE_ALT, foreground=TEXT,
                    bordercolor=BORDER, padding=(10, 6))
    style.map("TButton", background=[("active", ACCENT), ("disabled", SURFACE)],
              foreground=[("disabled", MUTED_TEXT)])
    style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
    style.configure("TNotebook.Tab", background=SURFACE, foreground=TEXT,
                    font=("Segoe UI", 9), padding=(12, 7))
    style.map("TNotebook.Tab", background=[("selected", SURFACE_ALT)])
    style.configure("TCombobox", background=FIELD_BACKGROUND,
                    fieldbackground=FIELD_BACKGROUND, foreground=TEXT,
                    arrowcolor=TEXT, padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD_BACKGROUND)],
              foreground=[("readonly", TEXT)], selectbackground=[("readonly", FIELD_BACKGROUND)],
              selectforeground=[("readonly", TEXT)])
    style.configure("TScrollbar", background=SURFACE_ALT, troughcolor=TABLE_FIELD,
                    bordercolor=BACKGROUND, arrowcolor=MUTED_TEXT)
    style.configure("Treeview", background=TABLE_FIELD, foreground=TEXT,
                    fieldbackground=TABLE_FIELD, rowheight=26)
    style.configure("Treeview.Heading", background=SURFACE_ALT, foreground=TEXT)
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    for title in ("Rolling Forecasts", "Options Strategies", "Schwab Duckets",
                  "Hyperliquid Duckets", "H.Y.P.E.R.", "Gameplan", "Gameplan Stats",
                  "15-Minute Duckets"):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
        if title == "Gameplan Stats":
            stats_frame = frame
    notebook.select(stats_frame)
    tab = GameplanStatsTab(root, stats_frame, datastore_root=args.datastore, auto_load=False)
    tab.session.set(args.session)
    tab._follow_latest = False
    tab.refresh()
    root.bind("<F6>", lambda _e: root.geometry("1180x760"))
    root.bind("<F7>", lambda _e: root.geometry("1586x990"))
    root.bind("<Escape>", lambda _e: root.destroy())
    root.mainloop()


if __name__ == "__main__":
    main()
