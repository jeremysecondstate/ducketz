from __future__ import annotations

from typing import Any

import pytest

import app.services.schwab as schwab_module
from app.models.portfolio import Holding, PortfolioSnapshot
from app.services.aggregate import DucketBucketSnapshot


def test_schwab_cash_and_sweep_includes_short_sale_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_payload: dict[str, Any] = {
        "securitiesAccount": {
            "accountNumber": "12345678",
            "currentBalances": {
                "cashBalance": 55_668.78,
                "shortBalance": 74_799.98,
                "liquidationValue": 130_604.90,
            },
            "positions": [
                {
                    "instrument": {"assetType": "EQUITY", "symbol": "LONGS"},
                    "longQuantity": 1.0,
                    "shortQuantity": 0.0,
                    "marketValue": 73_548.13,
                },
                {
                    "instrument": {"assetType": "EQUITY", "symbol": "MU"},
                    "longQuantity": 0.0,
                    "shortQuantity": 80.0,
                    "marketValue": -73_411.99,
                },
            ],
        }
    }

    class StubSchwabSession:
        def get_account(self) -> dict[str, Any]:
            return account_payload

        def get_open_orders(self) -> list[object]:
            return []

    monkeypatch.setattr(schwab_module, "SchwabSession", StubSchwabSession)

    snapshot = schwab_module.sync_schwab_portfolio()

    assert snapshot.cash_value == pytest.approx(130_468.76)
    assert snapshot.cash[0].bucket == "Cash & sweep"
    assert snapshot.holdings_value == pytest.approx(136.14)
    assert {holding.bucket for holding in snapshot.holdings} == {"Stock"}
    assert snapshot.total_value == pytest.approx(130_604.90)
    assert snapshot.cash_value + snapshot.holdings_value == pytest.approx(
        snapshot.total_value
    )
    assert snapshot.account_facts["account_values"]["short_balance"] == pytest.approx(
        74_799.98
    )


def test_schwab_cash_without_short_credit_remains_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubSchwabSession:
        def get_account(self) -> dict[str, Any]:
            return {
                "securitiesAccount": {
                    "currentBalances": {
                        "cashBalance": 20_000.0,
                        "liquidationValue": 20_000.0,
                    },
                    "positions": [],
                }
            }

        def get_open_orders(self) -> list[object]:
            return []

    monkeypatch.setattr(schwab_module, "SchwabSession", StubSchwabSession)

    snapshot = schwab_module.sync_schwab_portfolio()

    assert snapshot.cash_value == pytest.approx(20_000.0)
    assert snapshot.total_value == pytest.approx(20_000.0)


@pytest.mark.parametrize(
    ("row", "expected_day_pnl"),
    [
        (
            {
                "instrument": {
                    "assetType": "EQUITY",
                    "symbol": "NVDA",
                    "netChange": 18.91,
                },
                "longQuantity": 25.0,
                "shortQuantity": 0.0,
                "marketValue": 5_715.50,
                "currentDayProfitLoss": -41.25,
            },
            472.75,
        ),
        (
            {
                "instrument": {
                    "assetType": "EQUITY",
                    "symbol": "MU",
                    "netChange": -19.283625,
                },
                "longQuantity": 0.0,
                "shortQuantity": 80.0,
                "marketValue": -73_525.60,
                "currentDayProfitLoss": 1_249.20,
            },
            1_542.69,
        ),
        (
            {
                "instrument": {
                    "assetType": "OPTION",
                    "symbol": "AAPL  260828C00312500",
                    "netChange": -0.90,
                },
                "longQuantity": 1.0,
                "shortQuantity": 0.0,
                "marketValue": 238.50,
                "currentDayProfitLoss": 87.50,
            },
            87.50,
        ),
    ],
)
def test_schwab_day_pnl_matches_thinkorswim_semantics(
    row: dict[str, Any],
    expected_day_pnl: float,
) -> None:
    holding = schwab_module._holding_from_schwab(row)

    assert holding is not None
    assert holding.day_pnl == pytest.approx(expected_day_pnl)


def test_schwab_option_uses_option_bucket_and_per_share_quote_mark() -> None:
    holding = schwab_module._holding_from_schwab(
        {
            "instrument": {
                "assetType": "OPTION",
                "symbol": "AAPL  260828C00312500",
            },
            "longQuantity": 1.0,
            "shortQuantity": 0.0,
            "marketValue": 297.50,
        },
        option_quote={"mark": 2.975},
    )

    assert holding is not None
    assert holding.bucket == "Option"
    assert holding.price == pytest.approx(2.975)
    assert holding.value == pytest.approx(297.50)


def test_schwab_summary_separates_stock_etf_and_option_values() -> None:
    snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        holdings=[
            Holding("MU", -80, 934.66, -74_772.80, "schwab", "Stock"),
            Holding("VXUS", 30, 87.90, 2_637.00, "schwab", "ETF"),
            Holding(
                "MU    260831C00940000",
                1,
                16.425,
                1_642.50,
                "schwab",
                "Option",
            ),
        ],
    )
    bucket = DucketBucketSnapshot([snapshot])

    assert bucket.holdings_value_for("Stock", "ETF") == pytest.approx(-72_135.80)
    assert bucket.holdings_value_for("Option") == pytest.approx(1_642.50)
    assert bucket.holdings_value == pytest.approx(-70_493.30)
