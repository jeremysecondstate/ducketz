"""Manual fake-only visual fixture for the universal option-order review dialog.

Usage: pythonw tests/visual_option_order_review_fixture.py [close|roll|exit|exit_single|stale]
       [--ack] [--bottom] [--compact] [--probe]
The fixture never supplies a working broker submitter.
"""

from __future__ import annotations

import runpy
import sys
import tkinter as tk
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk

from app.services.option_exit_plans import SINGLE_TARGET, TARGET_STOP, build_exit_plan_draft
from app.services.option_order_review import (
    OptionOrderReviewController,
    closing_order_review,
    exit_plan_review,
    roll_order_review,
)
from app.services.schwab_option_management import build_closing_order_draft, option_position_book
from app.ui.option_order_review import OptionOrderReviewDialog
from app.ui.theme import BACKGROUND, BORDER, MUTED_TEXT, SURFACE, TABLE_FIELD, TEXT


def main() -> None:
    fixture_mode = (sys.argv[1] if len(sys.argv) > 1 else "close").lower()
    geometry_probe = "--probe" in sys.argv
    compact = "--compact" in sys.argv
    confirmed = "--ack" in sys.argv
    show_bottom = "--bottom" in sys.argv
    helpers = runpy.run_path(str(Path(__file__).with_name("test_option_order_review.py")))
    now = datetime.now(timezone.utc)
    snapshot = helpers["_snapshot"](
        quote_at=now - timedelta(minutes=5) if fixture_mode == "stale" else now
    )
    book = option_position_book(snapshot)

    if fixture_mode == "roll":
        draft = helpers["_roll_draft"]()
        draft = replace(
            draft,
            oldest_quote_at=now,
            close_legs=tuple(replace(leg, quote_observed_at=now) for leg in draft.close_legs),
            replacement_legs=tuple(
                replace(leg, quote_observed_at=now) for leg in draft.replacement_legs
            ),
        )
        review = roll_order_review(draft, now=now)
        controller = OptionOrderReviewController(
            review=review,
            draft=draft,
            now_provider=lambda: datetime.now(timezone.utc),
        )
    elif fixture_mode in {"exit", "exit_single"}:
        draft = build_exit_plan_draft(
            book,
            tuple(leg.symbol for leg in book.legs),
            template_id=SINGLE_TARGET if fixture_mode == "exit_single" else TARGET_STOP,
        )
        review = exit_plan_review(draft, now=now)
        controller = OptionOrderReviewController(review=review, draft=draft)
    else:
        draft = build_closing_order_draft(book, tuple(leg.symbol for leg in book.legs))
        review = closing_order_review(draft, now=now)

        class NeverSubmit:
            def submit_order(self, _payload: dict[str, object]) -> None:
                raise AssertionError("The visual fixture must never submit an order.")

        controller = OptionOrderReviewController(
            review=review,
            draft=draft,
            snapshot_loader=lambda: snapshot,
            session_factory=NeverSubmit,
            now_provider=lambda: datetime.now(timezone.utc),
        )

    root = tk.Tk()
    root.title("Duckets option review visual fixture")
    root.geometry("980x680+0+0" if compact else "1680x943+0+0")
    root.minsize(980, 680)
    root.configure(background=BACKGROUND)
    ttk.Style(root).theme_use("clam")
    _fake_workspace(root)
    def open_review() -> None:
        dialog = OptionOrderReviewDialog(root=root, controller=controller)
        if confirmed:
            def confirm_review() -> None:
                dialog.acknowledged_var.set(True)
                dialog._acknowledgment_changed()

            root.after(700, confirm_review)
        if show_bottom:
            root.after(800, lambda: dialog.body.canvas.yview_moveto(1.0))
        if geometry_probe:
            dialog.update_idletasks()
            root.title(
                "probe "
                f"root={root.winfo_geometry()} "
                f"dialog={dialog.winfo_geometry()} "
                f"requested={dialog.winfo_reqwidth()}x{dialog.winfo_reqheight()}"
            )
            def report_geometry() -> None:
                print(
                    "root=",
                    root.winfo_geometry(),
                    "dialog=",
                    dialog.winfo_geometry(),
                    "requested=",
                    f"{dialog.winfo_reqwidth()}x{dialog.winfo_reqheight()}",
                    "screen=",
                    f"{dialog.winfo_screenwidth()}x{dialog.winfo_screenheight()}",
                    flush=True,
                )
                dialog.destroy()
                root.destroy()

            root.after(500, report_geometry)

    root.after(100, open_review)
    root.mainloop()


def _fake_workspace(root: tk.Tk) -> None:
    header = tk.Frame(root, background="#050c16", height=62)
    header.pack(fill=tk.X)
    tk.Label(
        header,
        text="◈  Duckets",
        background="#050c16",
        foreground=TEXT,
        font=("Segoe UI", 12, "bold"),
        padx=18,
        pady=14,
    ).pack(side=tk.LEFT)
    body = tk.Frame(root, background=BACKGROUND, padx=22, pady=18)
    body.pack(fill=tk.BOTH, expand=True)
    tk.Label(
        body,
        text="Options Command Center",
        background=BACKGROUND,
        foreground=TEXT,
        font=("Segoe UI", 20, "bold"),
    ).pack(anchor=tk.W)
    tk.Label(
        body,
        text="Fake positions workspace for visual review only",
        background=BACKGROUND,
        foreground=MUTED_TEXT,
        font=("Segoe UI", 10),
    ).pack(anchor=tk.W, pady=(2, 14))
    surface = tk.Frame(
        body,
        background=SURFACE,
        highlightbackground=BORDER,
        highlightthickness=1,
        padx=14,
        pady=12,
    )
    surface.pack(fill=tk.BOTH, expand=True)
    for row in range(9):
        background = TABLE_FIELD if row % 2 == 0 else SURFACE
        tk.Label(
            surface,
            text="Exact option position" if row < 2 else "",
            background=background,
            foreground=MUTED_TEXT,
            anchor=tk.W,
            padx=8,
            pady=12,
        ).pack(fill=tk.X)


if __name__ == "__main__":
    main()
