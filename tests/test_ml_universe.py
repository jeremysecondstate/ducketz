from __future__ import annotations

import pandas as pd

from ml.universe import initial_universe_membership


def test_research_universe_accepts_selected_us_equities_without_registration() -> None:
    effective_from = pd.Timestamp("2026-08-03T14:00:00Z")

    membership = initial_universe_membership(
        ("AAPL", "NEWCO"),
        effective_from_by_symbol={
            "AAPL": effective_from,
            "NEWCO": effective_from,
        },
    ).set_index("symbol")

    assert membership.loc["AAPL", "venue"] == "NASDAQ"
    assert membership.loc["AAPL", "exchange_calendar"] == "XNAS"
    assert membership.loc["NEWCO", "venue"] == "US_EQUITY"
    assert membership.loc["NEWCO", "currency"] == "USD"
    assert membership.loc["NEWCO", "exchange_calendar"] == "XNYS"
