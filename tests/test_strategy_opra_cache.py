from __future__ import annotations

from pathlib import Path

import pandas as pd

from ml.strategy_selection.candidates import _prepare_contracts
from ml.strategy_selection.chain import load_option_chain_history
from ml.strategy_selection.contracts import StrategySelectionPolicy
from ml.strategy_selection.opra_cache import (
    _StrategyInterval,
    _publish_cache,
    _selected_surface_times,
    _strategy_intervals,
)
from options.features import (
    OPTION_FEATURE_SCHEMA_VERSION,
    OPTION_FEATURE_VERSION,
    OPTION_SURFACE_QUALITY_POLICY_VERSION,
)
from options.snapshot import OPTION_CHAIN_SCHEMA_VERSION


def test_compact_opra_cache_is_used_when_full_replay_is_disabled(
    tmp_path: Path,
) -> None:
    timestamp = pd.Timestamp("2026-08-14T15:01:00Z")
    contracts = _contracts(timestamp)
    surfaces = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "snapshot_for": timestamp,
                "available_at": timestamp,
                "surface_quality_pass": True,
                "source_provider": "databento-opra",
                "surface_quality_basis": "OPRA_VALID_BBO_CALL_PUT_COVERAGE",
                "fallback_used": False,
                "surface_quality_policy_version": OPTION_SURFACE_QUALITY_POLICY_VERSION,
                "calculation_version": OPTION_FEATURE_VERSION,
                "schema_version": OPTION_FEATURE_SCHEMA_VERSION,
            }
        ]
    )
    _publish_cache(
        tmp_path,
        cache_key="a" * 64,
        source_fingerprint="b" * 64,
        request_fingerprint="c" * 64,
        cbbo_schema="cbbo-1m",
        contracts=contracts,
        surfaces=surfaces,
        partitions=(),
        bar_files=(),
        published_at=timestamp,
    )

    history = load_option_chain_history(
        tmp_path,
        symbol="AAPL",
        available_not_after=timestamp + pd.Timedelta(minutes=1),
        allow_historical_opra_replay=False,
    )

    assert history.provider == "databento-opra-cache"
    assert len(history.contracts) == 2
    assert len(history.surfaces) == 1
    assert any("strategy-opra-history-runs" in path.parts for path in history.source_files)


def test_opra_bbo_liquidity_is_not_misrepresented_as_open_interest() -> None:
    timestamp = pd.Timestamp("2026-08-14T15:01:00Z")
    opra = _contracts(timestamp)
    prepared = _prepare_contracts(
        opra,
        horizon="1d",
        policy=StrategySelectionPolicy(),
    )
    assert prepared["__liquidity_policy_pass"].all()
    assert prepared["open_interest"].isna().all()

    schwab_without_open_interest = opra.assign(source_provider="schwab")
    rejected = _prepare_contracts(
        schwab_without_open_interest,
        horizon="1d",
        policy=StrategySelectionPolicy(),
    )
    assert not rejected["__liquidity_policy_pass"].any()


def test_strategy_entry_surface_is_strictly_after_prediction_availability(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-07-27T15:00:00Z")
    path = tmp_path / "quotes.parquet"
    pd.DataFrame(
        {"value": [1, 2, 3]},
        index=pd.DatetimeIndex(
            [
                "2026-07-27T15:06:00Z",
                "2026-07-27T15:07:00Z",
                "2026-07-27T15:08:00Z",
            ],
            name="ts_recv",
        ),
    ).to_parquet(path)

    selected = _selected_surface_times(
        path,
        (
            _StrategyInterval(
                "AAPL",
                pd.Timestamp("2026-07-27T15:06:00.000000001Z"),
                pd.Timestamp("2026-07-27T15:59:59.999999999Z"),
                target,
                "ENTRY_AFTER_PREDICTION",
            ),
        ),
    )

    assert selected == ((pd.Timestamp("2026-07-27T15:07:00Z"), target),)


def test_strategy_intervals_keep_completed_bar_target_separate_from_quote_clock() -> None:
    sample = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "horizon": "1h",
                "bar_end_timestamp": pd.Timestamp("2026-07-27T15:00:00Z"),
                "decision_timestamp": pd.Timestamp("2026-07-27T15:05:00Z"),
                "information_available_at": pd.Timestamp(
                    "2026-07-27T15:05:00Z"
                ),
                "target_window_start": pd.Timestamp("2026-07-27T16:00:00Z"),
                "target_window_end": pd.Timestamp("2026-07-27T17:00:00Z"),
                "label_status": "COMPLETE",
            }
        ]
    )

    intervals = _strategy_intervals(
        sample,
        symbols=("AAPL",),
        archive_start=pd.Timestamp("2026-07-27T13:30:00Z"),
        archive_end=pd.Timestamp("2026-07-27T20:00:01Z"),
    )
    entry = next(value for value in intervals if value.purpose.startswith("ENTRY"))

    assert entry.snapshot_for == pd.Timestamp("2026-07-27T15:00:00Z")
    assert entry.lower == pd.Timestamp("2026-07-27T15:06:00.000000001Z")


def _contracts(timestamp: pd.Timestamp) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["AAPL", "AAPL"],
            "snapshot_for": [timestamp, timestamp],
            "available_at": [timestamp, timestamp],
            "contract_symbol": [
                "AAPL  260918C00200000",
                "AAPL  260918P00200000",
            ],
            "call_put": ["CALL", "PUT"],
            "expiration_date": pd.to_datetime(
                ["2026-09-18", "2026-09-18"], utc=True
            ),
            "strike": [200.0, 200.0],
            "underlying_price": [200.0, 200.0],
            "bid": [5.0, 4.5],
            "ask": [5.2, 4.7],
            "open_interest": [float("nan"), float("nan")],
            "volume": [float("nan"), float("nan")],
            "delta": [float("nan"), float("nan")],
            "gamma": [float("nan"), float("nan")],
            "theta": [float("nan"), float("nan")],
            "vega": [float("nan"), float("nan")],
            "multiplier": [100.0, 100.0],
            "mini": [False, False],
            "non_standard": [False, False],
            "quote_valid": [True, True],
            "relative_bid_ask_spread": [0.04, 0.04],
            "quote_staleness_seconds": [0.0, 0.0],
            "quote_timestamp": [timestamp, timestamp],
            "source_provider": ["databento-opra", "databento-opra"],
            "liquidity_evidence_basis": [
                "OPRA_VALID_BBO_SPREAD",
                "OPRA_VALID_BBO_SPREAD",
            ],
            "fallback_used": [False, False],
            "schema_version": [OPTION_CHAIN_SCHEMA_VERSION] * 2,
        }
    )
