"""Fake-only visual fixture for Concepts A-D.

Usage:
    pythonw tests/visual_option_management_fixture.py [command|roll|exit|review]

All account, position, chain, order, and submission behavior is local fixture data.
The fake session rejects submission and never performs network I/O.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import tkinter as tk
import zlib
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk

from app.models.portfolio import PortfolioSnapshot
from app.services.option_exit_plans import SINGLE_TARGET
from app.services.option_order_review import BrokerOrderRejected
from app.ui.option_templates import OptionsTemplatesView
from app.ui.options_management import OptionsManagementView
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    FIELD_BACKGROUND,
    FIELD_TEXT,
    MUTED_TEXT,
    SURFACE,
    SURFACE_ALT,
    TABLE_FIELD,
    TEXT,
)


POSITION_SYMBOL = "WULF  260918C00024000"


class FakeSchwabSession:
    def get_option_chain(self, symbol: str, _strike_count: int) -> dict[str, object]:
        if symbol != "WULF":
            raise ValueError(f"Fixture has no chain for {symbol}.")
        return _chain_payload()

    def get_recent_orders(self) -> list[dict[str, object]]:
        return []

    def cancel_order(self, _order_id: str) -> dict[str, str]:
        return {"status": "fixture-only"}

    def submit_order(self, _payload: dict[str, object]) -> None:
        raise BrokerOrderRejected("Visual fixture: submission is intentionally disabled.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("command", "roll", "exit", "review", "analyze", "exercise", "templates"),
        default="command",
    )
    parser.add_argument("--size", default="1680x944", help="Root size as WIDTHxHEIGHT.")
    parser.add_argument("--capture", type=Path, help="Capture the active fixture window to a PNG and exit.")
    parser.add_argument("--single-target", action="store_true", help="Select the executable single-target exit.")
    parser.add_argument("--time-exit", action="store_true", help="Expand and configure the relative timed exit.")
    parser.add_argument(
        "--specific-time-exit",
        action="store_true",
        help="Expand and configure the explicitly zoned date-and-time exit.",
    )
    parser.add_argument("--scroll-bottom", action="store_true", help="Scroll a review body to its final controls.")
    parser.add_argument("--acknowledge", action="store_true", help="Acknowledge a universal review before capture.")
    parser.add_argument(
        "--refresh-fails",
        action="store_true",
        help="Make the convenience review refresh fail while retaining the fresh reviewed snapshot.",
    )
    args = parser.parse_args()
    mode = args.mode
    try:
        width, height = (int(value) for value in args.size.casefold().split("x", maxsplit=1))
    except (TypeError, ValueError) as exc:
        raise SystemExit("--size must be WIDTHxHEIGHT.") from exc
    if width < 1080 or height < 720:
        raise SystemExit("--size must be at least 1080x720.")
    if args.single_target and mode != "exit":
        raise SystemExit("--single-target requires exit mode.")
    if args.time_exit and mode != "exit":
        raise SystemExit("--time-exit requires exit mode.")
    if args.specific_time_exit and mode != "exit":
        raise SystemExit("--specific-time-exit requires exit mode.")
    if args.acknowledge and mode != "review":
        raise SystemExit("--acknowledge requires review mode.")
    if args.refresh_fails and mode != "review":
        raise SystemExit("--refresh-fails requires review mode.")
    root = tk.Tk()
    root.title(f"Duckets fake option management — {mode}")
    root.geometry(f"{width}x{height}+0+0")
    root.minsize(1080, 720)
    root.configure(background=BACKGROUND)
    ttk.Style(root).theme_use("clam")
    _fixture_styles(root)
    _global_header(root)

    content = ttk.Frame(root, padding=(12, 9, 12, 10), style="StrategyPage.TFrame")
    content.pack(fill=tk.BOTH, expand=True)
    title = ttk.Frame(content, style="StrategyPage.TFrame")
    title.pack(fill=tk.X, pady=(0, 8))
    tk.Label(
        title,
        text="Options Command Center",
        background=BACKGROUND,
        foreground=TEXT,
        font=("Segoe UI", 18, "bold"),
    ).pack(anchor=tk.W)
    tk.Label(
        title,
        text="Fake-only release-readiness fixture • no credentials • no network • no live orders",
        background=BACKGROUND,
        foreground=MUTED_TEXT,
        font=("Segoe UI", 9),
    ).pack(anchor=tk.W, pady=(2, 0))

    notebook = ttk.Notebook(content, style="StrategySecondary.TNotebook")
    notebook.pack(fill=tk.BOTH, expand=True)
    discover = ttk.Frame(notebook, style="StrategyPage.TFrame")
    positions = ttk.Frame(notebook, style="StrategyPage.TFrame")
    orders = ttk.Frame(notebook, style="StrategyPage.TFrame")
    templates = ttk.Frame(notebook, style="StrategyPage.TFrame")
    notebook.add(discover, text="Discover")
    notebook.add(positions, text="Positions")
    notebook.add(orders, text="Orders")
    notebook.add(templates, text="Templates")
    _discover_placeholder(discover)

    snapshot = _snapshot()
    session = FakeSchwabSession()

    def snapshot_loader() -> PortfolioSnapshot:
        if args.refresh_fails:
            raise ConnectionError("fixture refresh offline")
        return snapshot

    view = OptionsManagementView(
        root=root,
        positions_parent=positions,
        orders_parent=orders,
        snapshot_loader=snapshot_loader,
        session_factory=lambda: session,
        on_refresh=lambda: view.show_snapshot(snapshot),
        on_show_orders=lambda: notebook.select(orders),
    )
    root.fixture_management_view = view  # type: ignore[attr-defined]
    OptionsTemplatesView(root=root, parent=templates, roll_loader=lambda: (), exit_loader=lambda: ())
    view.show_snapshot(snapshot)
    notebook.select(positions)

    if mode == "roll":
        root.after(350, view._open_roll)
    elif mode == "exit":
        root.after(350, view._open_exit_plan)
    elif mode == "review":
        root.after(350, view._review_closing_order)
    elif mode == "analyze":
        root.after(350, view._analyze_closing_order)
    elif mode == "exercise":
        root.after(350, view._open_exercise_analysis)
    elif mode == "templates":
        notebook.select(templates)
    if args.capture is not None:
        root.after(
            1800,
            lambda: _stage_capture(
                root,
                args.capture,
                single_target=args.single_target,
                time_exit=args.time_exit,
                specific_time_exit=args.specific_time_exit,
                scroll_bottom=args.scroll_bottom,
                acknowledge=args.acknowledge,
            ),
        )
    root.mainloop()


def _stage_capture(
    root: tk.Tk,
    path: Path,
    *,
    single_target: bool,
    time_exit: bool,
    specific_time_exit: bool,
    scroll_bottom: bool,
    acknowledge: bool,
) -> None:
    candidates = [
        child
        for child in root.winfo_children()
        if isinstance(child, tk.Toplevel) and child.winfo_viewable()
    ]
    target: tk.Tk | tk.Toplevel = candidates[-1] if candidates else root
    if single_target:
        select_template = getattr(target, "_template_clicked", None)
        if not callable(select_template):
            raise RuntimeError("The exit-plan dialog was not available for single-target capture.")
        select_template(SINGLE_TARGET)
    if time_exit or specific_time_exit:
        toggle_time_exit = getattr(target, "_toggle_time_exit", None)
        if not callable(toggle_time_exit):
            raise RuntimeError("The timed-exit accordion was not available for capture.")
        toggle_time_exit()
        if specific_time_exit:
            target.time_exit_type.set("Specific Date and Time")
            target._time_exit_type_changed()
        left_scroll = getattr(target, "left_scroll", None)
        canvas = getattr(left_scroll, "canvas", None)
        if canvas is not None:
            target.update_idletasks()
            canvas.yview_moveto(1.0)
    if acknowledge:
        controller = getattr(target, "controller", None)
        acknowledge_review = getattr(controller, "acknowledge", None)
        if not callable(acknowledge_review):
            raise RuntimeError("The universal review was not available for acknowledgment capture.")
        acknowledge_review(True)
    if scroll_bottom:
        body = getattr(target, "body", None)
        canvas = getattr(body, "canvas", None)
        if canvas is None:
            management_view = getattr(root, "fixture_management_view", None)
            manage_scroll = getattr(management_view, "manage_scroll", None)
            canvas = getattr(manage_scroll, "canvas", None)
        if canvas is None:
            raise RuntimeError("The active fixture window does not have a scrollable body.")
        canvas.yview_moveto(1.0)
    target.lift()
    target.focus_force()
    target.attributes("-topmost", True)
    target.update_idletasks()
    target.after(250, lambda: _capture_and_exit(root, target, path))


def _capture_and_exit(root: tk.Tk, target: tk.Tk | tk.Toplevel, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_window_png(target.winfo_id(), path)
    finally:
        root.destroy()


def _write_window_png(hwnd: int, path: Path) -> None:
    """Capture one native window through GDI without third-party packages."""

    class Rect(ctypes.Structure):
        _fields_ = (
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        )

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = (
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        )

    class BitmapInfo(ctypes.Structure):
        _fields_ = (("bmiHeader", BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 3))

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = Rect()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("Could not read fixture window bounds.")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, 2):
            raise OSError("Windows could not render the fixture window.")
        info = BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        pixels = (ctypes.c_ubyte * (width * height * 4))()
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0):
            raise OSError("Windows could not read the rendered fixture pixels.")
        source = memoryview(pixels).cast("B")
        scanlines = bytearray()
        row_bytes = width * 4
        for y in range(height):
            row = source[y * row_bytes : (y + 1) * row_bytes]
            scanlines.append(0)
            for x in range(0, row_bytes, 4):
                scanlines.extend((row[x + 2], row[x + 1], row[x]))
        path.write_bytes(_png_bytes(width, height, bytes(scanlines)))
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def _png_bytes(width: int, height: int, scanlines: bytes) -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(name + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def _fixture_styles(root: tk.Tk) -> None:
    style = ttk.Style(root)
    root.option_add("*TCombobox*Listbox.background", FIELD_BACKGROUND)
    root.option_add("*TCombobox*Listbox.foreground", FIELD_TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    style.configure(".", background=BACKGROUND, foreground=TEXT, fieldbackground=TABLE_FIELD)
    style.configure(
        "TButton",
        background=SURFACE_ALT,
        foreground=TEXT,
        bordercolor=BORDER,
        darkcolor=BORDER,
        lightcolor=BORDER,
        focusthickness=1,
        padding=(10, 6),
    )
    style.map(
        "TButton",
        background=[("active", ACCENT), ("disabled", SURFACE)],
        foreground=[("disabled", MUTED_TEXT)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=FIELD_BACKGROUND,
        background=FIELD_BACKGROUND,
        foreground=FIELD_TEXT,
        arrowcolor=FIELD_TEXT,
        selectbackground=FIELD_BACKGROUND,
        selectforeground=FIELD_TEXT,
        bordercolor=BORDER,
        darkcolor=BORDER,
        lightcolor=BORDER,
        padding=5,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", FIELD_BACKGROUND), ("active", FIELD_BACKGROUND)],
        foreground=[("readonly", FIELD_TEXT), ("active", FIELD_TEXT)],
        selectbackground=[("readonly", FIELD_BACKGROUND), ("active", FIELD_BACKGROUND)],
        selectforeground=[("readonly", FIELD_TEXT), ("active", FIELD_TEXT)],
        bordercolor=[("focus", ACCENT), ("active", ACCENT)],
        arrowcolor=[("disabled", MUTED_TEXT)],
    )
    style.configure(
        "TScrollbar",
        background=SURFACE_ALT,
        troughcolor=TABLE_FIELD,
        bordercolor=BACKGROUND,
        darkcolor=SURFACE_ALT,
        lightcolor=SURFACE_ALT,
        arrowcolor=MUTED_TEXT,
    )
    style.configure(
        "Treeview",
        background=TABLE_FIELD,
        foreground=TEXT,
        fieldbackground=TABLE_FIELD,
        bordercolor=BORDER,
        font=("Segoe UI", 10),
        rowheight=26,
    )
    style.configure(
        "Treeview.Heading",
        background=SURFACE_ALT,
        foreground=TEXT,
        bordercolor=BORDER,
        font=("Segoe UI", 9),
    )
    style.map(
        "Treeview",
        background=[("selected", ACCENT)],
        foreground=[("selected", "#020617")],
    )
    style.configure("StrategyPage.TFrame", background=BACKGROUND)
    style.configure(
        "StrategySecondary.TNotebook",
        background=BACKGROUND,
        bordercolor=BORDER,
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "StrategySecondary.TNotebook.Tab",
        background=BACKGROUND,
        foreground=MUTED_TEXT,
        font=("Segoe UI", 10),
        borderwidth=0,
        padding=(15, 7),
    )
    style.map(
        "StrategySecondary.TNotebook.Tab",
        background=[("selected", BACKGROUND), ("active", "#17253a")],
        foreground=[("selected", "#1687e8"), ("active", TEXT)],
    )


def _global_header(root: tk.Tk) -> None:
    header = tk.Frame(root, background="#050c16", highlightbackground=BORDER, highlightthickness=1)
    header.pack(fill=tk.X)
    tk.Label(
        header,
        text="◈  Duckets",
        background="#050c16",
        foreground=TEXT,
        font=("Segoe UI", 11, "bold"),
        padx=12,
        pady=9,
    ).pack(side=tk.LEFT)
    for label in ("Rolling Forecasts", "Options Strategies", "Ducket Bucket", "Schwab Duckets"):
        tk.Label(
            header,
            text=label,
            background=SURFACE if label == "Options Strategies" else "#050c16",
            foreground=TEXT if label == "Options Strategies" else MUTED_TEXT,
            font=("Segoe UI", 9),
            padx=16,
            pady=10,
        ).pack(side=tk.LEFT)


def _discover_placeholder(parent: ttk.Frame) -> None:
    surface = tk.Frame(
        parent,
        background=SURFACE,
        highlightbackground=BORDER,
        highlightthickness=1,
        padx=18,
        pady=16,
    )
    surface.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    tk.Label(
        surface,
        text="Discover remains available in the production tab.",
        background=SURFACE,
        foreground=TEXT,
        font=("Segoe UI", 12, "bold"),
    ).pack(anchor=tk.W)


def _snapshot() -> PortfolioSnapshot:
    now = datetime.now(timezone.utc)
    position = {
        "status": "CURRENT",
        "symbol": POSITION_SYMBOL,
        "asset_type": "OPTION",
        "contract_multiplier": 100.0,
        "underlying_symbol": "WULF",
        "option_type": "CALL",
        "strike": 24.0,
        "expiration": "2026-09-18",
        "delta": 0.42,
        "theta": -0.035,
        "net_quantity": 1.0,
        "settled_quantity": 1.0,
        "price": 1.03,
        "bid": 0.99,
        "ask": 1.07,
        "market_value": 103.0,
        "unrealized_pnl": 18.0,
        "day_pnl": 4.0,
        "quote_observed_at": now.isoformat(),
        "source_ref": "fixture:WULF-call",
        "option_fields_complete": True,
        "option_unavailable_reasons": [],
        "unavailable_reasons": [],
    }
    return PortfolioSnapshot(
        source="fixture",
        account_label="Schwab ••••0907",
        synced_at=now,
        status="Fake fixture current",
        account_facts={
            "observed_at": now.isoformat(),
            "account_values": {
                "status": "PARTIAL",
                "available_funds": 43_959.84,
                "buying_power": 51_234.56,
            },
            "positions": {
                # Deliberately reproduces the formerly broken case: a generic
                # account row is incomplete while the option row-set is exact.
                "status": "INCOMPLETE",
                "option_row_set_complete": True,
                "items": [position],
                "option_quote_status": "CURRENT",
                "option_unavailable_reasons": [],
                "unavailable_reasons": ["Fixture-only unrelated equity field is incomplete."],
            },
            "working_orders": {
                "status": "CURRENT",
                "items": [],
                "active_option_orders": [],
            },
        },
    )


def _chain_payload() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    quote_time = int(now.timestamp() * 1000)

    def rows(expiration: str, code: str) -> dict[str, list[dict[str, object]]]:
        result: dict[str, list[dict[str, object]]] = {}
        for strike, bid, ask in ((22.0, 2.25, 2.35), (24.0, 1.34, 1.44), (26.0, 0.72, 0.82)):
            result[f"{strike:g}"] = [
                {
                    "symbol": f"WULF  {code}C{int(round(strike * 1000)):08d}",
                    "expirationDate": expiration,
                    "strikePrice": strike,
                    "bid": bid,
                    "ask": ask,
                    "mark": (bid + ask) / 2,
                    "delta": 0.52 if strike <= 24 else 0.31,
                    "theta": -0.04,
                    "multiplier": 100,
                    "quoteTimeInLong": quote_time,
                }
            ]
        return result

    return {
        "symbol": "WULF",
        "underlyingPrice": 25.10,
        "underlying": {"symbol": "WULF", "mark": 25.10, "quoteTime": quote_time},
        "callExpDateMap": {
            "2026-10-16:72": rows("2026-10-16", "261016"),
            "2026-11-20:107": rows("2026-11-20", "261120"),
        },
        "putExpDateMap": {},
    }


if __name__ == "__main__":
    main()
