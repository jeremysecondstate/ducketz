from __future__ import annotations

import argparse
import subprocess
import tkinter as tk
import time
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk

from app.ui.options_strategies import OptionsStrategiesTab
from app.ui.theme import BACKGROUND


def render(output: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    root = tk.Tk()
    root.title("Duckets - Options Strategies fixture")
    root.geometry("1500x840+40+40")
    root.configure(background=BACKGROUND)

    # Build the production widget without starting its asynchronous Schwab refresh.
    real_after_idle = root.after_idle
    root.after_idle = lambda *_args, **_kwargs: "fixture-refresh-disabled"  # type: ignore[method-assign]
    host = ttk.Frame(root)
    host.pack(fill=tk.BOTH, expand=True)
    tab = OptionsStrategiesTab(
        root=root,
        parent=host,
        snapshot_loader=lambda: None,  # type: ignore[arg-type]
        session_factory=lambda: None,  # type: ignore[arg-type]
        past_positions_loader=lambda: None,  # type: ignore[arg-type]
    )
    root.after_idle = real_after_idle  # type: ignore[method-assign]

    position = SimpleNamespace(
        symbol="GOOG",
        shares=100.0,
        option_contracts=0.0,
        working_option_orders=0,
    )
    fit = SimpleNamespace(label="Fits", detail="Defined-risk position fits current limits.")
    candidates = (
        SimpleNamespace(
            symbol="GOOG",
            horizon="1d",
            rank=1,
            strategy_display_name="Bull Call Spread",
            exact_legs="Buy GOOG 195C @ $4.20 / Sell GOOG 205C @ $1.65",
            predictive_score=82.40,
            expected_return=0.183,
            portfolio_fit=fit,
            score_basis="BSGP + Strategy ML",
            position=position,
        ),
        SimpleNamespace(
            symbol="GOOG",
            horizon="1d",
            rank=2,
            strategy_display_name="Cash-Secured Put",
            exact_legs="Sell GOOG 190P @ $2.35",
            predictive_score=74.10,
            expected_return=0.121,
            portfolio_fit=fit,
            score_basis="Black-Scholes + ML",
            position=position,
        ),
        SimpleNamespace(
            symbol="GOOG",
            horizon="1d",
            rank=3,
            strategy_display_name="Long Call",
            exact_legs="Buy GOOG 200C @ $3.10",
            predictive_score=61.80,
            expected_return=0.076,
            portfolio_fit=fit,
            score_basis="Pricing Scenario",
            position=position,
        ),
    )
    tab.view = SimpleNamespace(
        symbols=("GOOG",),
        candidates=candidates,
        horizons_by_symbol={"GOOG": ("1d",)},
        route_diagnoses={},
        empty_diagnosis=None,
    )
    tab._symbol_box.configure(values=("GOOG",))
    tab.symbol.set("GOOG")
    tab._set_horizon_choices()
    tab._render_candidates()
    tab._secondary_notebook.select(0)

    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.update_idletasks()
    root.update()
    time.sleep(0.25)
    root.update()
    left = root.winfo_rootx()
    top = root.winfo_rooty()
    right = left + root.winfo_width()
    bottom = top + root.winfo_height()
    escaped_output = str(output).replace("'", "''")
    capture = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$bitmap = New-Object System.Drawing.Bitmap({right - left}, {bottom - top}); "
        "$graphics = [System.Drawing.Graphics]::FromImage($bitmap); "
        f"$graphics.CopyFromScreen({left}, {top}, 0, 0, $bitmap.Size); "
        f"$bitmap.Save('{escaped_output}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$graphics.Dispose(); $bitmap.Dispose()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", capture],
        check=True,
    )
    root.destroy()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render the production Options Strategies widget with pricing-score fixtures."
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(render(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
