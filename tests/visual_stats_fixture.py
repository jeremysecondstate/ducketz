"""Offline Stats layout preview; synthetic snapshots and no runtime services."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ui import stats
from app.ui.scout_stats_data import ScoutStatsSnapshot
from app.ui.theme import BACKGROUND


def fixture():
    forecasts, orders = [], []
    for index, symbol in enumerate(("ALFA", "BRAV", "CHAR", "DELT", "ECHO", "FOXT", "GOLF", "HOTL", "INDI", "JULI", "KILO")):
        for outcome in ("scored", "scored", "missing", "synthetic"):
            forecasts.append(dict(forecast_id=f"f{len(forecasts)}", symbol=symbol, supported_target=True,
                reference_at="2026-10-29T16:00:00Z", issued_at="2026-10-29T16:15:10Z",
                target_open="2026-10-29T16:30:00Z", target_close="2026-10-29T16:45:00Z",
                probability_not_down=.75, outcome_status=outcome, target_not_down=index % 3 != 0))
        orders.append(dict(request_id=f"r{index}", symbol=symbol, side="BUY" if index % 2 else "SELL",
            status="FILLED" if index < 9 else "UNKNOWN", submission_state="ACCEPTED" if index < 9 else "UNKNOWN",
            quantity=25, filled_quantity=25 if index < 9 else None, issued_at="2026-10-29T16:16:00Z",
            reference_at="2026-10-29T16:00:00Z", observed_at="2026-10-29T16:17:00Z",
            fills=({"fill_id":f"fill{index}", "quantity":25, "price":100.25, "executed_at":"2026-10-29T16:16:01Z"},) if index < 9 else ()))
    return ScoutStatsSnapshot(session="2026-10-29", status="available", message="Offline visual fixture · Synthetic evidence only",
        forecasts=tuple(forecasts), orders=tuple(orders), prediction_status="read", execution_status="read")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1240)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    saved = fixture()
    stats.scout_stats_sessions = lambda *_: (saved.session,)
    stats.load_scout_stats = lambda *_: saved
    root = tk.Tk()
    root.title("Duckets · Stats · Offline layout fixture")
    root.geometry(f"{args.width}x{args.height}")
    root.configure(background=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use("clam")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    frame = ttk.Frame(notebook)
    notebook.add(frame, text="Stats")
    stats.StatsTab(root, frame, auto_load=True)
    root.mainloop()
