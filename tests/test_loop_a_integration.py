from __future__ import annotations

from datetime import timezone
from math import sin
from pathlib import Path
import re

import pandas as pd
import pytest

from app.models.market_data import MarketBar
from datafetching import FetchResult
import datafetching.main as fetching_main
from datafetching import orchestrate
from datafetching.parquet_store import ParquetStore
from ml.horizons import horizon_specification
from ml.parquet_contracts import forbidden_identity_columns
from ml.runtime_pipeline import RuntimeConfig, run_loop_b_once

_HASH_ID = re.compile(
    r"(?:[0-9a-f]{32,}|[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def test_clean_datastore_runs_loop_a_once_and_writes_readable_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, provider_calls = _run_loop_a_once(tmp_path, monkeypatch)

    assert result == 0
    assert provider_calls == ["databento", "fmp"]
    assert not (tmp_path / ".ducketz-orchestration.lock").exists()

    stock_root = tmp_path / "stocks" / "NVDA"
    normalized_bars = tuple((stock_root / "bars").glob("**/normalized/*.parquet"))
    normalized_statements = tuple(
        (stock_root / "corporate").glob("**/normalized/*.parquet")
    )
    fundamental_outputs = tuple(
        (stock_root / "fundamentals").glob("**/*.parquet")
    )
    technical_outputs = tuple((stock_root / "technicals").glob("**/*.parquet"))
    signal_outputs = tuple((stock_root / "signals").glob("**/*.parquet"))

    assert len(normalized_bars) == 1
    assert len(normalized_statements) == 6
    assert len(fundamental_outputs) == 4
    assert {path.parent.parent.name for path in fundamental_outputs} == {
        "fundamental-direction",
        "point-in-time",
    }
    assert {path.parent.parent.name for path in technical_outputs} == {
        "bar-shape",
        "breakout-pressure",
        "market-regime",
    }
    assert {path.parent.parent.name for path in signal_outputs} == {
        "fundamental-technical-lifecycle",
        "technical-lifecycle",
    }

    fundamentals = pd.read_parquet(
        stock_root
        / "fundamentals"
        / "fundamental-direction"
        / "fmp"
        / "quarterly.parquet"
    )
    market_regime = pd.read_parquet(
        stock_root
        / "technicals"
        / "market-regime"
        / "databento"
        / "1d.parquet"
    )
    point_in_time = pd.read_parquet(
        stock_root
        / "fundamentals"
        / "point-in-time"
        / "fmp"
        / "quarterly.parquet"
    )
    bar_shape = pd.read_parquet(
        stock_root
        / "technicals"
        / "bar-shape"
        / "databento"
        / "1d.parquet"
    )
    signals = pd.read_parquet(
        stock_root
        / "signals"
        / "fundamental-technical-lifecycle"
        / "consensus"
        / "daily.parquet"
    )
    technical_lifecycle = pd.read_parquet(
        stock_root
        / "signals"
        / "technical-lifecycle"
        / "consensus"
        / "daily.parquet"
    )
    assert {"fundamental_score", "revenue_growth"}.issubset(fundamentals.columns)
    assert {"atr_14", "trend_score", "volatility_ratio"}.issubset(
        market_regime.columns
    )
    assert {"lifecycle_phase", "setup_quality"}.issubset(signals.columns)
    assert {
        "period_end_date",
        "accepted_at",
        "available_at",
        "revenue_growth_yoy",
    }.issubset(point_in_time.columns)
    assert {
        "overnight_gap_atr",
        "intrabar_range_atr",
        "close_location",
    }.issubset(bar_shape.columns)
    assert {
        "constituent_available_at",
        "available_at",
        "technical_consensus_change_5d",
    }.issubset(technical_lifecycle.columns)
    assert not fundamentals.empty
    assert not market_regime.empty
    assert not signals.empty
    assert not point_in_time.empty
    assert not bar_shape.empty
    assert not technical_lifecycle.empty

    parquet_paths = tuple(sorted(tmp_path.glob("**/*.parquet")))
    assert parquet_paths
    for path in parquet_paths:
        _assert_one_readable_id(path)


def test_clean_datastore_runs_loop_a_once_then_loop_b_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_a_exit, _provider_calls = _run_loop_a_once(tmp_path, monkeypatch)
    assert loop_a_exit == 0

    loop_b = run_loop_b_once(
        tmp_path,
        symbols=("NVDA",),
        config=RuntimeConfig(
            provider="databento",
            model_family="logistic",
            calibration_method="platt",
            minimum_train_clusters=30,
            calibration_clusters=10,
            assessment_clusters=10,
            lockbox_clusters=5,
        ),
        specifications={"1d": horizon_specification("1d")},
        run_timestamp=pd.Timestamp("2024-09-03T12:00:00Z"),
        input_available_at=pd.Timestamp("2024-09-03T12:00:00Z"),
        reporter=None,
    )

    assert loop_b.status == "COMPLETED"
    assert loop_b.route_errors == {}
    assert loop_b.models_trained == 1
    assert loop_b.sample_rows > 50
    assert loop_b.prediction_rows >= 10
    assert loop_b.evaluation_rows >= 10
    assert loop_b.intelligence_rows == 1
    for path in sorted(tmp_path.glob("**/*.parquet")):
        _assert_one_readable_id(path)


def _run_loop_a_once(
    datastore_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str = "NVDA",
) -> tuple[int, list[str]]:
    """Run real Loop A once while replacing only provider network boundaries."""

    provider_calls: list[str] = []

    def fetch_fmp(
        symbol: str,
        store: ParquetStore,
        *,
        include_macro: bool,
    ) -> FetchResult:
        assert include_macro
        provider_calls.append("fmp")
        changed = _seed_fmp_statements(store, symbol)
        return FetchResult("fmp", changed, 0)

    def fetch_databento(
        symbol: str,
        store: ParquetStore,
        *,
        include_cme: bool,
        profile: str,
        minute_bars_completed=None,
    ) -> FetchResult:
        assert not include_cme
        assert profile == "continuation"
        provider_calls.append("databento")
        changed = _seed_databento_daily_bars(store, symbol)
        result = FetchResult("databento", changed, 0)
        if minute_bars_completed is not None:
            minute_bars_completed({symbol: result})
        return result

    monkeypatch.setattr(fetching_main, "fetch_fmp", fetch_fmp)
    monkeypatch.setattr(fetching_main, "fetch_databento", fetch_databento)

    result = orchestrate.main(
        [
            "--datastore",
            str(datastore_root),
            "--symbols",
            symbol,
            "--providers",
            "fmp",
            "databento",
            "--skip-cme",
            "--bar-readiness-recovery-timeout-seconds",
            "0",
            "--once",
        ]
    )
    return result, provider_calls


def _seed_fmp_statements(store: ParquetStore, symbol: str) -> int:
    quarterly_dates = pd.to_datetime(
        [
            "2022-03-31",
            "2022-06-30",
            "2022-09-30",
            "2022-12-31",
            "2023-03-31",
            "2023-06-30",
            "2023-09-30",
            "2023-12-31",
        ],
        utc=True,
    )
    annual_dates = pd.to_datetime(
        [
            "2019-12-31",
            "2020-12-31",
            "2021-12-31",
            "2022-12-31",
            "2023-12-31",
        ],
        utc=True,
    )
    changed = 0
    for cadence, dates in (
        ("quarterly", quarterly_dates),
        ("annual", annual_dates),
    ):
        rows = _statement_rows(symbol, dates, quarterly=cadence == "quarterly")
        for dataset_key, statement_rows in (
            (f"income_statement_{cadence}", rows["income"]),
            (f"balance_sheet_statement_{cadence}", rows["balance"]),
            (f"cash_flow_statement_{cadence}", rows["cash"]),
        ):
            path = store.save_corporate_rows(
                "fmp",
                symbol,
                dataset_key,
                statement_rows,
            )
            changed += int(path is not None)
    return changed


def _statement_rows(
    symbol: str,
    dates: pd.DatetimeIndex,
    *,
    quarterly: bool,
) -> dict[str, list[dict[str, object]]]:
    output: dict[str, list[dict[str, object]]] = {
        "income": [],
        "balance": [],
        "cash": [],
    }
    for index, period_end in enumerate(dates):
        scale = 1.0 + index * 0.08
        period = f"Q{period_end.quarter}" if quarterly else "FY"
        shared = {
            "symbol": symbol,
            "date": period_end.date().isoformat(),
            "period": period,
            "filingDate": (period_end + pd.Timedelta(days=40)).date().isoformat(),
            "acceptedDate": (
                period_end + pd.Timedelta(days=45, hours=16)
            ).isoformat(),
            "fetched_at": "2025-01-02T00:00:00Z",
        }
        output["income"].append(
            {
                **shared,
                "revenue": 10_000_000_000 * scale,
                "costOfRevenue": 4_000_000_000 * scale,
                "operatingIncome": 3_000_000_000 * scale,
                "netIncome": 2_400_000_000 * scale,
                "incomeBeforeTax": 2_900_000_000 * scale,
                "incomeTaxExpense": 600_000_000 * scale,
                "researchAndDevelopmentExpenses": 1_000_000_000 * scale,
                "weightedAverageShsOutDil": 2_500_000_000 - index * 2_000_000,
            }
        )
        output["balance"].append(
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
        output["cash"].append(
            {
                **shared,
                "netCashProvidedByOperatingActivities": 3_200_000_000 * scale,
                "freeCashFlow": 2_500_000_000 * scale,
                "capitalExpenditure": -700_000_000 * scale,
                "stockBasedCompensation": 450_000_000 * scale,
            }
        )
    return output


def _seed_databento_daily_bars(store: ParquetStore, symbol: str) -> int:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar(
        "XNAS",
        start="2024-01-01",
        end="2024-09-30",
    )
    sessions = calendar.sessions[
        (calendar.sessions >= pd.Timestamp("2024-01-02"))
        & (calendar.sessions <= pd.Timestamp("2024-08-30"))
    ]
    timestamps = pd.DatetimeIndex(sessions)
    if timestamps.tz is None:
        timestamps = timestamps.tz_localize("UTC")
    else:
        timestamps = timestamps.tz_convert("UTC")

    bars: list[MarketBar] = []
    for index, timestamp in enumerate(timestamps):
        open_price = 120.0 + index * 0.18 + sin(index / 5.0)
        direction = -0.008 if index % 2 == 0 else 0.010
        close = open_price * (1.0 + direction)
        bars.append(
            MarketBar(
                symbol=symbol,
                source="databento",
                timeframe="1d",
                timestamp=timestamp.to_pydatetime(),
                open=open_price,
                high=max(open_price, close) + 0.80,
                low=min(open_price, close) - 0.75,
                close=close,
                volume=1_000_000.0 + index * 5_000.0,
            )
        )

    raw_path = store.save_raw_frame(
        source="databento",
        category="bars",
        symbol=symbol,
        endpoint="ohlcv_1d",
        timeframe="1d",
        frame=pd.DataFrame(
            {
                "id": ["databento-native-bar-1", "databento-native-bar-2"],
                "symbol": [symbol, symbol],
                "ts_event": timestamps[:2],
                "open": [bars[0].open, bars[1].open],
                "close": [bars[0].close, bars[1].close],
            }
        ),
    )
    normalized_path = store.save_bars(
        "databento",
        symbol,
        "1d",
        bars,
        request_key="ohlcv_1d",
        as_of=pd.Timestamp("2025-01-01T00:00:00Z"),
    )
    return int(raw_path is not None) + int(normalized_path is not None)


def _assert_one_readable_id(path: Path) -> None:
    frame = pd.read_parquet(path)
    assert frame.columns.tolist().count("id") == 1, path
    if "raw" not in {part.lower() for part in path.parts}:
        assert forbidden_identity_columns(frame.columns) == [], path
    identifiers = frame["id"].astype("string")
    assert identifiers.notna().all(), path
    assert identifiers.str.strip().ne("").all(), path
    assert identifiers.is_unique, path
    assert identifiers.str.len().le(160).all(), path
    if identifiers.empty:
        return
    assert not identifiers.map(lambda value: bool(_HASH_ID.fullmatch(value))).any(), path
