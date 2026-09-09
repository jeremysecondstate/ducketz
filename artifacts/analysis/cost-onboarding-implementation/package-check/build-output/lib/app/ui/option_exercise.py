from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from app.models.option_management import OptionExerciseAnalysis
from app.ui.theme import ACCENT, BACKGROUND, BORDER, MUTED_TEXT, SURFACE, SURFACE_ALT, TEXT, WARNING


class OptionExerciseAnalysisDialog(tk.Toplevel):
    """Read-only exact-leg exercise analysis; no broker submission is exposed."""

    def __init__(self, *, root: tk.Misc, analysis: OptionExerciseAnalysis) -> None:
        super().__init__(root)
        self.analysis = analysis
        self.title("Analyze Option Exercise")
        self.configure(background=BACKGROUND)
        self.minsize(720, 560)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._fit_to_root(root)
        self._build()
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()
        self.focus_set()

    def _fit_to_root(self, root: tk.Misc) -> None:
        root.update_idletasks()
        width, height = 780, 620
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(width, screen_width - 32)
        height = min(height, screen_height - 48)
        x = max(0, root.winfo_rootx() + (max(root.winfo_width(), width) - width) // 2)
        y = max(0, root.winfo_rooty() + (max(root.winfo_height(), height) - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        outer = tk.Frame(self, background=BACKGROUND, padx=18, pady=15)
        outer.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            outer,
            text="Analyze Option Exercise",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 19, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            outer,
            text=f"Exact long leg • {self.analysis.symbol}",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(2, 12))

        facts = tk.Frame(
            outer,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        facts.pack(fill=tk.X)
        rows = (
            ("Contract", self.analysis.contract_label),
            ("Exercise Quantity", str(self.analysis.quantity)),
            ("Resulting Stock Quantity", f"{self.analysis.resulting_stock_quantity:+g} shares"),
            ("Strike Cash Effect", _signed_money(self.analysis.strike_cash_effect)),
            ("Underlying Price", _money(self.analysis.underlying_price)),
            ("Intrinsic Value / Share", _money(self.analysis.intrinsic_value_per_share)),
            ("Extrinsic Value / Share", _money(self.analysis.extrinsic_value_per_share)),
            ("Settlement", self.analysis.settlement or "Unavailable until broker confirmation"),
        )
        facts.grid_columnconfigure(1, weight=1)
        for row, (label, value) in enumerate(rows):
            background = SURFACE_ALT if row % 2 == 0 else SURFACE
            tk.Label(
                facts,
                text=label,
                background=background,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 9),
                anchor=tk.W,
                padx=8,
                pady=6,
            ).grid(row=row, column=0, sticky=tk.NSEW)
            tk.Label(
                facts,
                text=value,
                background=background,
                foreground=TEXT,
                font=("Segoe UI", 9, "bold"),
                anchor=tk.E,
                padx=8,
                pady=6,
            ).grid(row=row, column=1, sticky=tk.NSEW)

        notes = tk.Frame(
            outer,
            background="#382a10",
            highlightbackground=WARNING,
            highlightthickness=1,
            padx=10,
            pady=9,
        )
        notes.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        tk.Label(
            notes,
            text="Analysis Only",
            background="#382a10",
            foreground=WARNING,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            notes,
            text=self.analysis.capability_reason,
            background="#382a10",
            foreground=TEXT,
            font=("Segoe UI", 9),
            wraplength=720,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, fill=tk.X, pady=(3, 8))
        for warning in self.analysis.warnings:
            tk.Label(
                notes,
                text=f"• {warning}",
                background="#382a10",
                foreground=TEXT,
                font=("Segoe UI", 8),
                wraplength=710,
                justify=tk.LEFT,
                anchor=tk.W,
            ).pack(anchor=tk.W, fill=tk.X, pady=2)

        footer = tk.Frame(outer, background=BACKGROUND)
        footer.pack(fill=tk.X, pady=(12, 0))
        tk.Label(
            footer,
            text="No exercise request can be transmitted from this application.",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)
        ttk.Button(footer, text="Close Analysis", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(footer, text="Submission Unavailable", state=tk.DISABLED).pack(side=tk.RIGHT, padx=(0, 8))


def _money(value: float | None) -> str:
    return "Unavailable" if value is None else f"${value:,.2f}"


def _signed_money(value: float) -> str:
    return f"+${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"


__all__ = ["OptionExerciseAnalysisDialog"]
