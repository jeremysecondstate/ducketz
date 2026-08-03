from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from fundamentals.calculation import calculate_fundamental_direction


@pytest.mark.parametrize("period_type", ("quarterly", "annual"))
def test_wide_statements_do_not_fragment_fundamental_calculation(
    period_type: str,
) -> None:
    statements = _statement_frames(period_type)
    expected = calculate_fundamental_direction(
        *statements,
        symbol="MU",
        period_type=period_type,
    )
    wide = tuple(
        _with_unused_columns(frame, family)
        for frame, family in zip(
            statements,
            ("income", "balance", "cash"),
            strict=True,
        )
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", pd.errors.PerformanceWarning)
        actual = calculate_fundamental_direction(
            *wide,
            symbol="MU",
            period_type=period_type,
        )

    pd.testing.assert_frame_equal(
        actual.drop(columns="calculated_at"),
        expected.drop(columns="calculated_at"),
        check_exact=True,
    )


def _statement_frames(
    period_type: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if period_type == "quarterly":
        dates = pd.date_range("2022-03-31", periods=8, freq="QE-DEC", tz="UTC")
        periods = [f"Q{value.quarter}" for value in dates]
    else:
        dates = pd.date_range("2019-12-31", periods=5, freq="YE-DEC", tz="UTC")
        periods = ["FY"] * len(dates)
    scale = 1.0 + np.arange(len(dates), dtype=float) * 0.08
    shared = {
        "symbol": ["MU"] * len(dates),
        "date": [value.date().isoformat() for value in dates],
        "period": periods,
        "filingDate": [
            (value + pd.Timedelta(days=40)).date().isoformat() for value in dates
        ],
        "acceptedDate": [
            (value + pd.Timedelta(days=45, hours=16)).isoformat()
            for value in dates
        ],
        "fetched_at": ["2026-07-29T20:00:00Z"] * len(dates),
    }
    income = pd.DataFrame(
        {
            **shared,
            "revenue": 10_000_000_000 * scale,
            "costOfRevenue": 4_000_000_000 * scale,
            "operatingIncome": 3_000_000_000 * scale,
            "netIncome": 2_400_000_000 * scale,
            "incomeBeforeTax": 2_900_000_000 * scale,
            "incomeTaxExpense": 600_000_000 * scale,
            "researchAndDevelopmentExpenses": 1_000_000_000 * scale,
            "weightedAverageShsOutDil": (
                2_500_000_000 - np.arange(len(dates)) * 2_000_000
            ),
        }
    )
    balance = pd.DataFrame(
        {
            **shared,
            "netReceivables": 1_200_000_000 * scale,
            "inventory": 900_000_000 * scale,
            "cashAndCashEquivalents": 5_000_000_000 * scale,
            "shortTermInvestments": 2_000_000_000 * scale,
            "totalCurrentAssets": 12_000_000_000 * scale,
            "totalCurrentLiabilities": 5_000_000_000 * scale,
            "totalDebt": 3_000_000_000 * scale,
            "totalStockholdersEquity": 18_000_000_000 * scale,
            "propertyPlantEquipmentNet": 4_000_000_000 * scale,
            "totalAssets": 30_000_000_000 * scale,
        }
    )
    cash = pd.DataFrame(
        {
            **shared,
            "netCashProvidedByOperatingActivities": 3_200_000_000 * scale,
            "freeCashFlow": 2_500_000_000 * scale,
            "capitalExpenditure": -700_000_000 * scale,
            "stockBasedCompensation": 450_000_000 * scale,
        }
    )
    return income, balance, cash


def _with_unused_columns(frame: pd.DataFrame, family: str) -> pd.DataFrame:
    unused = pd.DataFrame(
        {
            f"{family}_unused_{index}": np.full(len(frame), index, dtype="int64")
            for index in range(70)
        }
    )
    return pd.concat([frame.reset_index(drop=True), unused], axis=1)
