"""Synthetic, provider-free Stats preview; no operating files are read."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ui.stats import StatsTab
from app.ui.atlas_stats_data import AtlasStatsSnapshot
from app.ui.ducket_bucket import DucketBucketApp
from test_scout_stats_report_ui import saved_report


def build(root, datastore):
    theme = DucketBucketApp.__new__(DucketBucketApp)
    theme.root = root
    theme._apply_theme()
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    tab = StatsTab(root, frame, datastore_root=datastore, auto_load=False)
    tab.session.set("2026-09-11")
    tab.date_box.configure(values=("2026-09-11",))
    tab.status.set("Synthetic offline preview · September 11 session")
    forecasts = []
    for index, symbol in enumerate(("TEST", "DEMO", "SAMPLE")):
        for number, status in enumerate(("scored", "scored", "missing", "synthetic", "pending")):
            forecasts.append(dict(forecast_id=f"fixture-{index}-{number}", symbol=symbol,
                probability_not_down=.8 if number % 2 == 0 else .3, supported_target=True,
                issued_at="2026-09-11T16:15:00Z", reference_at="2026-09-11T16:00:00Z",
                target_open="2026-09-11T16:30:00Z", target_close="2026-09-11T16:45:00Z",
                outcome_status=status, target_not_down=True))
    orders = []
    for index, status in enumerate(("FILLED", "PARTIAL", "WORKING", "CANCELLED", "REJECTED", "NOT_SENT", "UNKNOWN")):
        filled = 3 if status == "FILLED" else 1 if status == "PARTIAL" else None if status == "UNKNOWN" else 0
        orders.append(dict(request_id=f"fixture-order-{index}", symbol="TEST", side="BUY", quantity=3,
            status=status, submission_state="NOT_SENT" if status == "NOT_SENT" else "UNKNOWN" if status == "UNKNOWN" else "ACCEPTED",
            filled_quantity=filled, reference_at="2026-09-11T16:00:00Z",
            submitted_at=None if status in {"NOT_SENT", "UNKNOWN"} else "2026-09-11T16:16:00Z",
            observed_at="2026-09-11T16:17:00Z", fills=() if not filled else
            (dict(fill_id=f"fixture-fill-{index}", quantity=filled, price=100.25, executed_at="2026-09-11T16:16:01Z"),)))
    tab.atlas.set_snapshot(AtlasStatsSnapshot(session="2026-09-11", status="partial",
        message="Synthetic offline preview · Local saved-evidence reporting", forecasts=tuple(forecasts), orders=tuple(orders),
        prediction_status="read", execution_status="partial", warnings=("Incomplete order evidence · One request has no saved submission or fill result.",)))
    tab.scout_report.set_snapshot(saved_report("2026-09-11"))
    tab._show_scout()
    return tab


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="1280x1000")
    parser.add_argument("--atlas", action="store_true")
    parser.add_argument("--scout", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="atlas-stats-preview-") as directory:
        root = tk.Tk()
        root.title(f"Ducketz Stats · Synthetic offline preview · {args.size}")
        root.geometry(args.size)
        tab = build(root, Path(directory))
        def focus():
            root.update_idletasks()
            if args.atlas or args.scout:
                bounds = tab.canvas.bbox("all")
                if bounds and bounds[3]:
                    panel = tab.scout_report if args.scout else tab.atlas
                    tab.canvas.yview_moveto(panel.frame.winfo_y() / bounds[3])
        root.after(150, focus)
        root.after(180000, root.destroy)
        root.mainloop()


if __name__ == "__main__":
    main()
