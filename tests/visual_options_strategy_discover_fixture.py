"""Fake-only visual fixture for Options Command Center > Discover.

Usage: pythonw tests/visual_options_strategy_discover_fixture.py [--details] [--chat] [--probe]
No network-capable Schwab session is supplied.
"""

from __future__ import annotations

import runpy
import sys
import tempfile
import tkinter as tk
from datetime import date, datetime, timezone
from pathlib import Path
from tkinter import ttk

import pandas as pd

from app.models.past_positions import HistoryCoverage, PastPositionsSnapshot
from app.models.portfolio import PortfolioSnapshot
from app.ui.options_strategies import OptionsStrategiesTab
from app.ui.theme import BACKGROUND
from ml.parquet_contracts import STRATEGY_CANDIDATE_SCHEMA, write_parquet_with_schema
from ml.strategy_selection.contracts import (
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
)


def main() -> None:
    probe = "--probe" in sys.argv
    show_details = "--details" in sys.argv
    show_chat = "--chat" in sys.argv
    helpers = runpy.run_path(
        str(Path(__file__).with_name("test_options_strategy_ui.py"))
    )
    option_leg = helpers["_option_leg"]
    candidate = helpers["_candidate"](
        strategy_name="crash_and_squeeze_barbell",
        strategy_display_name="Crash-and-Squeeze Barbell",
        legs=[
            option_leg(
                side="LONG",
                option_type="PUT",
                strike=310.0,
                symbol="AAPL  260828P00310000",
                bid=4.10,
                ask=4.30,
                expiration="2026-08-28T00:00:00Z",
            ),
            option_leg(
                side="SHORT",
                option_type="PUT",
                strike=307.5,
                symbol="AAPL  260828P00307500",
                bid=2.95,
                ask=3.15,
                expiration="2026-08-28T00:00:00Z",
            ),
            option_leg(
                side="SHORT",
                option_type="CALL",
                strike=310.0,
                symbol="AAPL  260828C00310000",
                bid=3.65,
                ask=3.85,
                expiration="2026-08-28T00:00:00Z",
            ),
            option_leg(
                side="LONG",
                option_type="CALL",
                strike=315.0,
                symbol="AAPL  260828C00315000",
                bid=2.55,
                ask=2.75,
                quantity=2,
                expiration="2026-08-28T00:00:00Z",
            ),
        ],
    )
    candidate.update(
        {
            "id": "AAPL|1d|2026-08-23T20:55:00Z|crash_and_squeeze_barbell",
            "symbol": "AAPL",
            "candidate_key": "crash_and_squeeze_barbell|front=2026-08-28",
            "underlying_price": 315.0,
            "front_expiration": pd.Timestamp("2026-08-28T00:00:00Z"),
            "front_days_to_expiration": 5.0,
            "entry_cash_flow": -320.0,
            "entry_net_credit": 0.0,
            "entry_net_debit": 320.0,
            "max_profit": 1_250.0,
            "max_loss": 573.25,
            "capital_required": 573.25,
            "direction_probability_up": 0.6312,
            "raw_profit_probability": 0.4434,
            "calibrated_profit_probability": 0.4434,
            "decision_score": 0.4434,
            "scenario_coverage_score": 0.6007,
            "expected_net_profit": 107.65,
            "expected_return_on_risk": 0.0035,
            "score_basis": OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
            "pricing_mode": "UNAVAILABLE",
            "pricing_status": "Unavailable",
            "pricing_source": "UNAVAILABLE",
            "pricing_leg_coverage": 0.0,
            "pricing_missing_reason": (
                "AAPL 260828P00310000:TARGET_EVENT_STALE;"
                "AAPL 260828P00307500:TARGET_EVENT_STALE;"
                "AAPL 260828C00310000:TARGET_EVENT_STALE;"
                "AAPL 260828C00315000:TARGET_EVENT_STALE"
            ),
            "surface_quality_pass": False,
            "liquidity_policy_pass": False,
        }
    )
    now = datetime.now(timezone.utc)
    snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        synced_at=now,
        status="Schwab synced fake visual account",
        account_facts={
            "account_values": {
                "status": "CURRENT",
                "available_funds": 103_036.04,
                "cash_balance": 44_498.03,
                "buying_power": 217_660.0,
                "liquidation_value": 154_322.40,
            },
            "positions": {
                "items": [
                    {
                        "asset_type": "EQUITY",
                        "symbol": "AAPL",
                        "underlying_symbol": "AAPL",
                        "net_quantity": 15,
                    }
                ]
            },
            "working_orders": {"status": "CURRENT", "items": []},
        },
    )
    past = PastPositionsSnapshot(
        positions=(),
        coverage=HistoryCoverage(),
        range_start=date(2026, 1, 1),
        range_end=date(2026, 8, 23),
        observed_at=now,
        status="Fake visual history",
    )

    class NeverNetwork:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("The visual fixture must never create a live session.")

    with tempfile.TemporaryDirectory(prefix="ducketz-discover-visual-") as temp:
        path = Path(temp) / "strategy-candidates.parquet"
        write_parquet_with_schema(
            pd.DataFrame([candidate]),
            path,
            STRATEGY_CANDIDATE_SCHEMA,
        )
        root = tk.Tk()
        root.title("Duckets Discover visual fixture")
        root.geometry("1900x1000+0+0")
        root.configure(background=BACKGROUND)
        ttk.Style(root).theme_use("clam")
        frame = ttk.Frame(root)
        frame.pack(fill=tk.BOTH, expand=True)
        tab = OptionsStrategiesTab(
            root=root,
            parent=frame,
            candidates_path=path,
            snapshot_loader=lambda: snapshot,
            session_factory=NeverNetwork,
            past_positions_loader=lambda: past,
        )

        def show_candidate() -> None:
            if not tab.visible_candidates:
                root.after(100, show_candidate)
                return
            tab._secondary_notebook.select(0)
            if tab.candidate_table is not None:
                tab.candidate_table.selection_set("0")
            tab._fill_ticket(0)
            if show_details:
                tab._open_decision_details()
            if show_chat:
                tab._open_options_chat(
                    "Decision Details" if show_details else "Options Strategies"
                )
            if probe:
                root.update_idletasks()
                geometry = (
                    f"root={root.winfo_geometry()} requested="
                    f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}"
                )
                if tab._decision_details_window is not None:
                    geometry += (
                        " details="
                        f"{tab._decision_details_window.winfo_geometry()}"
                    )
                if tab.options_chat._window is not None:
                    geometry += (
                        " chat="
                        f"{tab.options_chat._window.winfo_geometry()}"
                    )
                print(geometry, flush=True)
                root.after(500, root.destroy)

        root.after(250, show_candidate)
        root.mainloop()


if __name__ == "__main__":
    main()
