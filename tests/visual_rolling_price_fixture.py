"""Render the native rolling-price component using static offline evidence only.

This fixture makes no market requests, starts no Ducketz services, and closes
after capturing the requested PNG. Use --missing to inspect stale coverage.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tkinter as tk

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.ui.gameplan_widgets import label, panel
from app.ui.rolling_price_widgets import RollingPricePanel
from app.ui.theme import BACKGROUND, MUTED_TEXT
from test_rolling_price_widgets import fixture_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=860)
    parser.add_argument("--missing", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = tk.Tk()
    root.title("Ducketz · Offline rolling-price visual fixture")
    root.configure(background=BACKGROUND)
    root.geometry(f"{args.width}x700+30+30")
    details = panel(root)
    details.pack(fill="x", padx=8, pady=(8, 6))
    label(details, "Order details", size=12, bold=True).pack(anchor="w", padx=12, pady=(9, 4))
    label(details, "Select a saved order to inspect fills, recorded times, retries and reasons.",
          size=9, color=MUTED_TEXT, wraplength=args.width - 50).pack(anchor="w", padx=12, pady=(0, 18))
    view = RollingPricePanel(root)
    view.frame.pack(fill="x", padx=8, pady=(0, 8))
    view.set_session_context("2026-09-11")
    view.set_snapshot(fixture_snapshot(missing=args.missing))
    root.update()
    root.geometry(f"{args.width}x{view.frame.winfo_y() + view.frame.winfo_height() + 10}")
    root.attributes("-topmost", True)
    root.update()

    def capture():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(args.output)
        root.destroy()

    root.after(350, capture)
    root.mainloop()


if __name__ == "__main__":
    main()
