"""Fake-only visual fixture for Options Command Center > Discover.

Usage: pythonw tests/visual_options_strategy_discover_fixture.py [--probe]
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
    helpers = runpy.run_path(
        str(Path(__file__).with_name("test_options_strategy_ui.py"))
    )
    option_leg = helpers["_option_leg"]
    candidate = helpers["_candidate"](
        strategy_name="cash_secured_put",
        strategy_display_name="Cash-Secured Put",
        cash_requirement="STRIKE_TIMES_MULTIPLIER",
        legs=[
            option_leg(
                side="SHORT",
                option_type="PUT",
                strike=307.5,
                symbol="AAPL  260831P00307500",
                bid=3.25,
                ask=3.35,
                expiration="2026-08-31T00:00:00Z",
            )
        ],
    )
    candidate.update(
        {
            "id": "AAPL|1d|2026-08-23T20:55:00Z|cash_secured_put",
            "symbol": "AAPL",
            "candidate_key": "cash_secured_put|front=2026-08-31",
            "underlying_price": 315.0,
            "entry_cash_flow": 325.0,
            "entry_net_credit": 325.0,
            "entry_net_debit": 0.0,
            "max_profit": 325.0,
            "max_loss": 30_425.0,
            "capital_required": 30_755.0,
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
            "pricing_missing_reason": (
                "AAPL 260831P00307500:TARGET_EVENT_STALE"
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
                "available_funds": 103_046.94,
                "available_funds_non_marginable_trade": 50_000.0,
                "cash_balance": 24_184.12,
                "buying_power": 206_093.88,
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
            if probe:
                root.update_idletasks()
                print(
                    f"root={root.winfo_geometry()} requested="
                    f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}",
                    flush=True,
                )
                root.after(500, root.destroy)

        root.after(250, show_candidate)
        root.mainloop()


if __name__ == "__main__":
    main()
