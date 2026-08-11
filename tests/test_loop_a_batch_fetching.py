from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.services.databento_market_data import (
    DatabentoAvailableRange,
    DatabentoMarketDataProvider,
)
from app.services.market_fetch_specs import DatabentoAnalysisSourceSpec
from app.services.schwab import SchwabSession
from datafetching import FetchResult
from datafetching import (
    databento_fetch,
    fmp_fetch,
    main as fetching_main,
    orchestrate,
    schwab_fetch,
)
from datafetching.parquet_store import ParquetStore
from technicals.parquet_io import _schwab_history_file_sort_key


SYMBOLS = tuple(f"S{index:02d}" for index in range(15))


def test_default_watchlist_and_runtime_commands_use_the_same_configured_symbols() -> None:
    root = Path(__file__).resolve().parents[1]
    watchlist = orchestrate.read_watchlist(root / "datafetching" / "watchlist.txt")
    loop_a_command = (root / "docs" / "datafetch-ml" / "current_start_command").read_text(
        encoding="utf-8"
    )
    loop_b_command = (
        root / "docs" / "datafetch-ml" / "current_prediction_command"
    ).read_text(encoding="utf-8")

    assert watchlist
    assert len(set(watchlist)) == len(watchlist)
    assert "--watchlist datafetching\\watchlist.txt" in loop_a_command
    pricing_command = loop_a_command.split("python -m ml.option_pricing_runtime", 1)[1]
    assert "--watchlist datafetching\\watchlist.txt" in pricing_command
    assert "--cme-mode external" in loop_a_command
    assert "--options-mode external" in loop_a_command
    assert all(symbol in loop_b_command for symbol in watchlist)
    assert "python -m ml.strategy_runtime" in loop_b_command
    assert "--pricing-mode shadow" in loop_b_command
    assert "capture-current-rate" in loop_a_command
    assert "option_pricing_admin" in loop_a_command
    assert "--bar-readiness-timeout-seconds 120" in loop_a_command
    assert "--pricing-barrier-timeout-seconds 150" in loop_a_command


def test_databento_provider_splits_one_multi_symbol_response_by_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class _Store:
        def to_df(self, *, map_symbols: bool) -> pd.DataFrame:
            observed["map_symbols"] = map_symbols
            return pd.DataFrame(
                {
                    "symbol": ["NVDA", "GOOG"],
                    "open": [100.0, 200.0],
                    "high": [101.0, 201.0],
                    "low": [99.0, 199.0],
                    "close": [100.5, 200.5],
                    "volume": [10.0, 20.0],
                },
                index=pd.DatetimeIndex(
                    ["2026-08-03T15:00:00Z", "2026-08-03T15:00:00Z"],
                    name="ts_event",
                ),
            )

    class _Timeseries:
        def get_range(self, **kwargs: Any) -> _Store:
            observed["request"] = kwargs
            return _Store()

    class _Client:
        timeseries = _Timeseries()

    provider = DatabentoMarketDataProvider(
        api_key="test",
        dataset="EQUS.MINI",
        native_schemas=("ohlcv-1m",),
    )
    monkeypatch.setattr(provider, "_client", lambda: _Client())
    spec = DatabentoAnalysisSourceSpec(
        key="source_1d_1m",
        schema="ohlcv-1m",
        frequency="1m",
        lookback=timedelta(days=1),
    )
    available = DatabentoAvailableRange(
        schema=spec.schema,
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )

    results, selected = provider.fetch_native_bars_many(
        ("nvda", "goog"),
        spec,
        available_range=available,
    )

    assert observed["map_symbols"] is True
    assert observed["request"]["symbols"] == ["NVDA", "GOOG"]
    assert selected.start == datetime(2026, 8, 2, tzinfo=timezone.utc)
    assert list(results) == ["NVDA", "GOOG"]
    assert [bar.symbol for bar in results["NVDA"][0]] == ["NVDA"]
    assert [bar.symbol for bar in results["GOOG"][0]] == ["GOOG"]
    assert results["NVDA"][1]["symbol"].tolist() == ["NVDA"]
    assert results["GOOG"][1]["symbol"].tolist() == ["GOOG"]


def test_databento_fetch_groups_fifteen_initial_symbols_into_one_schema_request(
    tmp_path,
) -> None:
    spec = DatabentoAnalysisSourceSpec(
        key="source_1d_1m",
        schema="ohlcv-1m",
        frequency="1m",
        lookback=timedelta(days=1),
    )
    available = DatabentoAvailableRange(
        schema=spec.schema,
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )

    class _Provider:
        calls: list[tuple[str, ...]] = []

        def native_specs(self):
            return (spec,)

        def dataset_range(self):
            return {"start": available.start, "end": available.end}

        def available_range_for_schema(self, _schema, *, dataset_range):
            return available

        def fetch_native_bars_many(self, symbols, _spec, *, available_range):
            clean_symbols = tuple(symbols)
            self.calls.append(clean_symbols)
            return {
                symbol: ([], pd.DataFrame(columns=["symbol"]))
                for symbol in clean_symbols
            }, available

    provider = _Provider()
    results = databento_fetch._fetch_native_results_many(
        provider,
        SYMBOLS,
        "continuation",
        ParquetStore(tmp_path),
    )

    assert provider.calls == [SYMBOLS]
    assert set(results) == set(SYMBOLS)
    assert all(symbol_results[0][-1] is None for symbol_results in results.values())


def test_fmp_prefetches_quote_and_market_cap_for_fifteen_symbols() -> None:
    class _Provider:
        calls: list[tuple[str, dict[str, str]]] = []

        def _get_json(self, endpoint: str, params: dict[str, str]):
            self.calls.append((endpoint, params))
            return [
                {"symbol": symbol, "price": index + 1.0}
                for index, symbol in enumerate(SYMBOLS)
            ]

    provider = _Provider()
    prefetched = fmp_fetch._fetch_batched_corporate_data(provider, SYMBOLS)

    assert provider.calls == [
        ("batch-quote", {"symbols": ",".join(SYMBOLS)}),
        ("market-capitalization-batch", {"symbols": ",".join(SYMBOLS)}),
    ]
    assert all(
        set(prefetched[symbol]) == {"quote", "market_capitalization"}
        for symbol in SYMBOLS
    )


def test_schwab_quotes_fifteen_symbols_in_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, dict[str, object]]:
            return {
                symbol: {"quote": {"lastPrice": index + 1.0}}
                for index, symbol in enumerate(SYMBOLS)
            }

    def get(_url: str, **kwargs: Any) -> _Response:
        observed.update(kwargs)
        return _Response()

    session = SchwabSession()
    monkeypatch.setattr(session, "_headers", lambda: {"Authorization": "test"})
    monkeypatch.setattr("app.services.schwab.requests.get", get)

    quotes = session.get_equity_quotes(SYMBOLS)

    assert observed["params"] == {
        "symbols": ",".join(SYMBOLS),
        "fields": "quote",
    }
    assert len(quotes) == 15
    assert quotes[SYMBOLS[-1]]["lastPrice"] == 15.0


def test_schwab_loop_a_uses_only_maximal_non_overlapping_history_windows() -> None:
    specs = schwab_fetch._specs_for_profile("continuation")

    assert {spec.key for spec in specs} == {
        "day_10_minute_1",
        "day_10_minute_5",
        "day_10_minute_10",
        "day_10_minute_15",
        "day_10_minute_30",
        "year_20_daily_1",
        "year_20_weekly_1",
        "year_20_monthly_1",
    }
    assert _schwab_history_file_sort_key(
        Path("GOOG_day_10_minute_1.parquet")
    ) > _schwab_history_file_sort_key(Path("GOOG_day_5_minute_1.parquet"))
    assert _schwab_history_file_sort_key(
        Path("GOOG_year_20_daily_1.parquet")
    ) > _schwab_history_file_sort_key(Path("GOOG_ytd_1_daily_1.parquet"))


def test_run_symbols_fetch_routes_fifteen_symbols_through_batch_lanes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, tuple[str, ...]]] = []

    def batch(provider: str):
        def run(symbols, _store, **_kwargs):
            clean_symbols = tuple(symbols)
            observed.append((provider, clean_symbols))
            return {
                symbol: FetchResult(provider, 0, 0)
                for symbol in clean_symbols
            }

        return run

    isolated: list[tuple[str, str]] = []

    def run_provider(provider, symbol, _store, **_kwargs):
        isolated.append((provider, symbol))
        return FetchResult(provider, 0, 0)

    monkeypatch.setattr(fetching_main, "fetch_databento_many", batch("databento"))
    monkeypatch.setattr(fetching_main, "fetch_fmp_many", batch("fmp"))
    monkeypatch.setattr(fetching_main, "fetch_schwab_many", batch("schwab"))
    monkeypatch.setattr(fetching_main, "run_provider_fetch", run_provider)

    results = fetching_main.run_symbols_fetch(
        SYMBOLS,
        ParquetStore(tmp_path),
        providers=("databento", "fmp", "fred", "schwab", "sec"),
    )

    assert observed == [
        ("databento", SYMBOLS),
        ("fmp", SYMBOLS),
        ("schwab", SYMBOLS),
    ]
    assert isolated == [("fred", SYMBOLS[0]), *[("sec", symbol) for symbol in SYMBOLS]]
    assert len(results) == 15
    assert [result.provider for result in results[SYMBOLS[0]]] == [
        "databento",
        "fmp",
        "fred",
        "schwab",
        "sec",
    ]
    assert [result.provider for result in results[SYMBOLS[1]]] == [
        "databento",
        "fmp",
        "schwab",
        "sec",
    ]
