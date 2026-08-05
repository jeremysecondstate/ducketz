"""Offline Rolling Forecast visual fixture with optional section collapsing.

Usage:
    pythonw tests/visual_rolling_forecast_fixture.py --size 1900x1000
    python tests/visual_rolling_forecast_fixture.py --collapse-all --capture forecast-collapsed.png
"""

from __future__ import annotations

import argparse
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from app.ui.rolling_forecasts import RollingForecastTab
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    MUTED_TEXT,
    SURFACE_ALT,
    TABLE_FIELD,
    TEXT,
)
from visual_option_management_fixture import _write_window_png


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", default="1900x1000", help="Window size as WIDTHxHEIGHT.")
    parser.add_argument("--collapse-all", action="store_true")
    parser.add_argument("--collapse", action="append", default=[], metavar="SYMBOL")
    parser.add_argument("--refresh-after-collapse", action="store_true")
    parser.add_argument("--resize-after-collapse", metavar="WIDTHxHEIGHT")
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args()
    try:
        width, height = (int(value) for value in args.size.casefold().split("x", maxsplit=1))
    except (TypeError, ValueError) as exc:
        raise SystemExit("--size must be WIDTHxHEIGHT.") from exc
    if width < 560 or height < 600:
        raise SystemExit("--size must be at least 560x600.")

    root = tk.Tk()
    root.title("Duckets rolling forecast visual fixture")
    root.geometry(f"{width}x{height}+0+0")
    root.minsize(560, 600)
    root.configure(background=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        "TButton",
        background=SURFACE_ALT,
        foreground=TEXT,
        bordercolor=BORDER,
        focuscolor=ACCENT,
        padding=(12, 7),
    )
    style.map("TButton", background=[("active", TABLE_FIELD)])
    style.configure(
        "TScrollbar",
        background=SURFACE_ALT,
        troughcolor=TABLE_FIELD,
        bordercolor=BACKGROUND,
        arrowcolor=MUTED_TEXT,
    )
    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    tab = RollingForecastTab(root=root, parent=parent)

    if args.capture is not None:
        root.after(
            2200,
            lambda: _stage_capture(
                root,
                tab,
                args.capture,
                collapse_all=args.collapse_all,
                collapsed_symbols=tuple(args.collapse),
                refresh_after_collapse=args.refresh_after_collapse,
                resize_after_collapse=args.resize_after_collapse,
            ),
        )
    root.mainloop()


def _stage_capture(
    root: tk.Tk,
    tab: RollingForecastTab,
    path: Path,
    *,
    collapse_all: bool,
    collapsed_symbols: tuple[str, ...],
    refresh_after_collapse: bool,
    resize_after_collapse: str | None,
) -> None:
    if collapse_all:
        tab._set_all_symbols_expanded(False)
    for symbol in collapsed_symbols:
        tab._set_symbol_expanded(symbol, False)
    if refresh_after_collapse:
        tab.refresh()
    if resize_after_collapse:
        try:
            width, height = (
                int(value)
                for value in resize_after_collapse.casefold().split("x", maxsplit=1)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("--resize-after-collapse must be WIDTHxHEIGHT.") from exc
        root.geometry(f"{width}x{height}+0+0")
    root.update_idletasks()
    root.after(
        900 if refresh_after_collapse else 250,
        lambda: _capture_and_exit(root, path),
    )


def _capture_and_exit(root: tk.Tk, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_window_png(root.winfo_id(), path)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
