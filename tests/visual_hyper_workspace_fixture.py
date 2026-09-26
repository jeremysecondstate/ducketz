"""Deterministic offline H.Y.P.E.R. fixture; default numbers are illustrative.

    .venv/Scripts/python.exe tests/visual_hyper_workspace_fixture.py --mode Paper
    .venv/Scripts/python.exe tests/visual_hyper_workspace_fixture.py --mode Powder --size 1180x760

The default fixture reads no runtime artifacts and cannot submit orders or start
workers. --local-data-root optionally takes one read-only snapshot before Tk starts.
"""
from __future__ import annotations

import argparse
import math
import sys
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.hyperliquid_paper_view import HyperliquidPaperViewService, PaperViewSnapshot, SourceState
from app.ui.ducket_bucket import DucketBucketApp
from app.ui.hyper_workspace import HyperWorkspace
from visual_option_management_fixture import _write_window_png


OBSERVED = datetime(2026, 9, 26, 3, 30, 12, tzinfo=timezone.utc)


def _snapshot() -> PaperViewSnapshot:
    """A coherent sample portfolio with qualified and research observations."""
    stamp = OBSERVED.isoformat()
    forecast_stamp = (OBSERVED - timedelta(seconds=12)).isoformat()
    accounts = {
        "alex": {"role": "short_perp", "equity": 5825.55, "initial_equity": 6325.00,
                 "total_pnl": .55, "net_transfers": -500, "free_cash": 4838.52,
                 "gross_exposure": 987.03, "fees": 12.18, "funding": -.21},
        "jeremy": {"role": "long_perp", "equity": 6220.80, "initial_equity": 6010.80,
                   "total_pnl": 10.00, "net_transfers": 200, "free_cash": 1736.80,
                   "gross_exposure": 4484.00, "fees": 7.46, "funding": -.09},
        "clearpond": {"role": "spot", "equity": 30066.51, "initial_equity": 29742.48,
                      "total_pnl": 24.03, "net_transfers": 300, "free_cash": 28102.45,
                      "gross_exposure": 1964.06, "fees": 10.10, "funding": 0},
    }
    pooled = {key: round(sum(account[key] for account in accounts.values()), 2)
              for key in ("equity", "initial_equity", "total_pnl", "net_transfers", "free_cash",
                          "gross_exposure", "fees", "funding")}
    # Deliberately unlike Paper P/L: realizing inherited positions is not new performance.
    pooled["net_realized_pnl"] = -795.93
    positions = [
        {"account": "alex", "coin": "HYPE", "kind": "perp", "quantity": -18.0,
         "avg_entry": 55.01, "mark_price": 54.82, "notional": 986.76, "unrealized_pnl": 3.42,
         "side": "Short", "passive": False},
        {"account": "alex", "coin": "HYPE", "kind": "spot", "quantity": .005,
         "avg_entry": 52.00, "mark_price": 54.81, "notional": .27, "unrealized_pnl": .014,
         "side": "Long", "passive": True},
        {"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": .04,
         "avg_entry": 111700, "mark_price": 112100, "notional": 4484, "unrealized_pnl": 16,
         "side": "Long", "passive": False},
        {"account": "clearpond", "coin": "ETH", "kind": "spot", "quantity": .14,
         "avg_entry": 3831, "mark_price": 3850, "notional": 539, "unrealized_pnl": 2.66,
         "side": "Long", "passive": False},
        {"account": "clearpond", "coin": "HYPE", "kind": "spot", "quantity": 26,
         "avg_entry": 54.20, "mark_price": 54.81, "notional": 1425.06, "unrealized_pnl": 15.86,
         "side": "Long", "passive": False},
    ]
    forecasts = []
    for coin, probability, qualified in (("BTC", .643, True), ("ETH", .527, False),
                                          ("HYPE", .382, True), ("ZEC", .491, False)):
        forecasts.append({
            "coin": coin, "p_not_down": probability, "p_down": 1 - probability,
            "qualified": qualified, "qualification": "qualified" if qualified else "research",
            "created_at_utc": forecast_stamp, "decision_close_utc": forecast_stamp,
            "target_close_utc": (OBSERVED + timedelta(minutes=60, seconds=-12)).isoformat(),
            "horizon_bars": 4, "horizon_minutes": 60,
            "per_model": {"logistic": probability - .015, "random_forest": probability + .021,
                          "lightgbm": probability + .003, "xgboost": probability - .009},
            "forecast_id": f"fixture-forecast-{coin}", "model_id": f"fixture-model-{coin}",
            "data_run_id": f"fixture-data-{coin}",
            "model_published_at_utc": (OBSERVED - timedelta(minutes=31)).isoformat(),
            "training_cutoff_utc": "2026-08-31T00:00:00+00:00",
            "calibration_cutoff_utc": "2026-09-15T00:00:00+00:00",
        })
    decisions = []
    for index, (account, coin, action, current, target, reason) in enumerate((
        ("jeremy", "BTC", "fill", 2242, 4484, "signal_rebalance"),
        ("clearpond", "ETH", "hold", 539, 539, "entry_deadband"),
        ("alex", "HYPE", "fill", -800, -986.76, "signal_rebalance"),
        ("clearpond", "ZEC", "skip", 0, 0, "entry_deadband"),
    )):
        forecast = next(row for row in forecasts if row["coin"] == coin)
        details = {**forecast, "kind": "spot" if account == "clearpond" else "perp",
                   "current_notional": current, "target_notional": target,
                   "policy": {"reason": reason, "confidence": .42, "effective_horizon_sigma": .012,
                              "entry_threshold": .55, "account_utilization": .80}}
        if action == "fill":
            requested, quantity, price = ((.02, .02, 112100) if coin == "BTC"
                                          else (-4.0, -3.407, 54.82))
            details["execution"] = {"requested_quantity": requested, "quantity": quantity,
                                    "price": price, "unfilled_quantity": requested - quantity,
                                    "status": "filled" if requested == quantity else "partial",
                                    "raw_book_vwap": price / (1 + (1 if quantity > 0 else -1) * .0001),
                                    "extra_slippage_bps": 1.0, "book_time_utc": forecast_stamp}
        decisions.append({**details, "details": details, "decision_id": f"fixture-decision-{index}",
                          "timestamp_utc": forecast_stamp, "account": account, "coin": coin,
                          "action": action, "reason": reason, "source": "paper"})
    fills = []
    for index, (account, coin, quantity, price, requested) in enumerate((
        ("jeremy", "BTC", .02, 112100, .02),
        ("alex", "HYPE", -3.407, 54.82, -4.0),
        ("clearpond", "ETH", .14, 3831, .14),
    )):
        forecast = next(row for row in forecasts if row["coin"] == coin)
        notional = abs(quantity) * price
        details = {**forecast, "requested_quantity": requested, "quantity": quantity,
                   "price": price, "unfilled_quantity": requested - quantity,
                   "book_time_utc": forecast_stamp, "extra_slippage_bps": 1.0,
                   "raw_book_vwap": price / (1 + (1 if quantity > 0 else -1) * .0001),
                   "status": "filled" if requested == quantity else "partial",
                   "reason": "signal_rebalance"}
        fills.append({**details, "details": details, "fill_id": f"fixture-fill-{index}",
                      "timestamp_utc": forecast_stamp, "account": account, "coin": coin,
                      "kind": "spot" if account == "clearpond" else "perp", "notional": notional,
                      "fee": round(notional * .00035, 4), "realized_pnl": 0, "source": "paper"})
    transfers = [{"transfer_id": "fixture-transfer-1", "timestamp_utc": forecast_stamp,
                  "from_account": "alex", "to_account": "jeremy", "amount": 200,
                  "reason": "receiver_funding", "status": "committed", "source": "paper",
                  "details": {"donor_cash": 6500, "donor_reserve": 400,
                              "receiver_requirement": 200}},
                 {"transfer_id": "fixture-transfer-2", "timestamp_utc": forecast_stamp,
                  "from_account": "alex", "to_account": "clearpond", "amount": 300,
                  "reason": "receiver_funding", "status": "committed", "source": "paper",
                  "details": {"donor_cash": 6300, "donor_reserve": 400,
                              "receiver_requirement": 300}}]
    equity_history = []
    peaks = {account: values["initial_equity"] for account, values in accounts.items()}
    peaks["pooled"] = pooled["initial_equity"]
    for index in range(73):
        progress = index / 72
        at = (OBSERVED - timedelta(minutes=360 - index * 5)).isoformat()
        total_at = 0
        for account, values in accounts.items():
            pnl = values["total_pnl"] * progress + math.sin(index / 6) * (12 if account == "clearpond" else 6) * (1 - progress)
            equity = values["initial_equity"] + values["net_transfers"] * progress + pnl
            total_at += pnl
            transfer_adjusted_equity = values["initial_equity"] + pnl
            peaks[account] = max(peaks[account], transfer_adjusted_equity)
            equity_history.append({"timestamp_utc": at, "account": account, "equity": equity,
                                   "total_pnl": pnl, "drawdown_fraction": transfer_adjusted_equity / peaks[account] - 1,
                                   "fees": values["fees"] * progress, "funding": values["funding"] * progress})
        equity = pooled["initial_equity"] + total_at
        peaks["pooled"] = max(peaks["pooled"], equity)
        equity_history.append({"timestamp_utc": at, "account": "pooled", "equity": equity,
                               "total_pnl": total_at, "drawdown_fraction": equity / peaks["pooled"] - 1,
                               "fees": pooled["fees"] * progress, "funding": pooled["funding"] * progress})
    sources = {
        "ledger": SourceState("Ledger", "fresh", stamp, 0, 30, "Read-only consistent transaction"),
        "paper": SourceState("Paper", "fresh", stamp, 0, 30, "Paper loop running · sample"),
        "models": SourceState("Models", "fresh", stamp, 0, 5, "Model polling · sample"),
        "performance": SourceState("Performance export", "fresh", forecast_stamp, 12, 300),
        "coordinator": SourceState("Coordinator", "fresh", stamp, 0, 5),
    }
    for coin in ("BTC", "ETH", "HYPE", "ZEC"):
        sources[f"data:{coin}"] = SourceState(f"{coin} data", "fresh", forecast_stamp, 12, 900)
        sources[f"forecast:{coin}"] = SourceState(f"{coin} forecast", "fresh", forecast_stamp, 12, 900)
        sources[f"training:{coin}"] = SourceState(f"{coin} training", "fresh",
                                                  (OBSERVED - timedelta(seconds=1860)).isoformat(), 1860, 3600)
    timings = [{"kind": kind, "coin": coin, "at_utc": stamp,
                "work_seconds": 1.82 if kind == "data" else 28.6,
                "queue_wait_seconds": .21, "poll_wait_seconds": 4.1,
                "publication_seconds": .08, "details": {}}
               for coin in ("BTC", "ETH", "HYPE", "ZEC") for kind in ("data", "model")]
    return PaperViewSnapshot(
        observed_at_utc=stamp, portfolio_observed_at_utc=stamp,
        pooled=pooled, accounts=accounts, positions=positions, decisions=decisions,
        fills=fills, transfers=transfers, equity_history=equity_history, forecasts=forecasts,
        performance={"as_of_utc": forecast_stamp, "max_drawdown_fraction": min(
            row["drawdown_fraction"] for row in equity_history if row["account"] == "pooled")},
        runtime={"status": "running", "updated_at_utc": stamp, "simulated": True, "poll_seconds": 30},
        policy={"version": "sample-paper-policy", "require_qualified_forecasts": False,
                "account_utilization": .80, "per_symbol_gross_fraction": .15,
                "pool_gross_fraction": .60, "poll_seconds": 30},
        seed={"timestamp_utc": equity_history[0]["timestamp_utc"]},
        sources=sources, timings=timings, warnings=(),
    )


class FixtureService:
    """Only an in-memory snapshot is reachable from the view."""

    def __init__(self, snapshot: PaperViewSnapshot | None = None):
        self.snapshot = snapshot

    def load_snapshot(self) -> PaperViewSnapshot:
        return self.snapshot if self.snapshot is not None else _snapshot()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("Paper", "Powder"), default="Paper")
    parser.add_argument("--size", default="1706x1000", help="Root dimensions, for example 1180x760.")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--scroll-bottom", action="store_true")
    parser.add_argument("--local-data-root", type=Path,
                        help="Read a local artifact tree once before starting Tk, using the read-only adapter.")
    parser.add_argument("--view", choices=("Positions", "Decisions", "Fills", "Transfers"), default="Positions")
    args = parser.parse_args()
    try:
        width, height = (int(value) for value in args.size.lower().split("x", maxsplit=1))
        if width < 800 or height < 600:
            raise ValueError
    except (TypeError, ValueError):
        parser.error("--size must be WIDTHxHEIGHT, at least 800x600.")
    snapshot = (HyperliquidPaperViewService(args.local_data_root).load_snapshot()
                if args.local_data_root else _snapshot())
    evidence_label = ("CURRENT LOCAL RECORDS · read-only" if args.local_data_root else
                      "OFFLINE VISUAL FIXTURE · Illustrative sample data · 2026-09-25 20:30 PT")
    root = tk.Tk()
    root.title(f"H.Y.P.E.R. {args.mode} — {evidence_label}")
    root.geometry(f"{width}x{height}+0+0")
    root.configure(background="#08111f")
    DucketBucketApp._apply_theme(SimpleNamespace(root=root))
    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True)
    frames = {}
    for title in ("Rolling Forecasts", "Options Strategies", "Schwab Duckets",
                  "Hyperliquid Duckets", "H.Y.P.E.R.", "Gameplan Stats", "Gameplan"):
        frame = ttk.Frame(notebook)
        frames[title] = frame
        notebook.add(frame, text=title)
    workspace = HyperWorkspace(root, frames["H.Y.P.E.R."], service=FixtureService(snapshot), auto_load=False)
    root.fixture_hyper_workspace = workspace  # type: ignore[attr-defined]
    notebook.select(frames["H.Y.P.E.R."])
    workspace.show_snapshot(snapshot)
    workspace.mode.set(args.mode)
    workspace.view.set(args.view)
    workspace._choose_mode()
    workspace._render()
    tk.Label(root, text=evidence_label,
             background="#08111f", foreground="#a7b6c6", font=("Segoe UI", 9)).pack(fill=tk.X)
    if args.capture is not None:
        root.after(1000, lambda: _capture_and_exit(root, args.capture, scroll_bottom=args.scroll_bottom))
    elif args.scroll_bottom:
        root.after(400, lambda: workspace.canvas.yview_moveto(1))
    root.mainloop()


def _capture_and_exit(root: tk.Tk, path: Path, *, scroll_bottom: bool = False) -> None:
    try:
        root.update_idletasks()
        if scroll_bottom:
            root.fixture_hyper_workspace.canvas.yview_moveto(1)  # type: ignore[attr-defined]
        root.lift()
        root.attributes("-topmost", True)
        root.update()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_window_png(root.winfo_id(), path)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
