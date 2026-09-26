"""Behavioral checks for the causal Hyperliquid shared feature calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from technicals.hyperliquid_features import build_features


def _bars(count: int = 360, *, regime: str = "variable") -> pd.DataFrame:
    steps = np.arange(count, dtype=float)
    if regime == "up":
        close = 100.0 * 1.002**steps
    elif regime == "down":
        close = 100.0 * 0.998**steps
    elif regime in {"flat", "zero_volume"}:
        close = np.full(count, 100.0)
    else:
        close = 100.0 + 0.04 * steps + 2.0 * np.sin(steps / 7.0)
    previous = np.r_[close[0], close[:-1]]
    volume = np.zeros(count) if regime == "zero_volume" else 20 + steps % 13
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=count, freq="15min", tz="UTC"),
            "open": previous,
            "high": np.maximum(previous, close) + 2.0,
            "low": np.minimum(previous, close) - 2.0,
            "close": close,
            "volume": volume,
            "trade_count": (steps % 17 + 1).astype("int64"),
        },
        index=pd.Index(np.arange(count) * 7 + 31, name="candle_id"),
    )


def test_publishing_new_candles_does_not_revise_historical_features() -> None:
    bars = _bars()
    cutoff = 240
    historical = build_features(bars.iloc[:cutoff]).frame
    extended = build_features(bars).frame.iloc[:cutoff]

    pd.testing.assert_frame_equal(historical, extended, rtol=1e-10, atol=1e-10)


def test_future_price_and_volume_changes_cannot_affect_earlier_features() -> None:
    bars = _bars()
    changed = bars.copy(deep=True)
    cutoff = 240
    later = changed.index[cutoff:]
    changed.loc[later, ["open", "high", "low", "close"]] *= 8.0
    changed.loc[later, "volume"] *= 1000.0

    original = build_features(bars).frame.iloc[:cutoff]
    after_change = build_features(changed).frame.iloc[:cutoff]

    pd.testing.assert_frame_equal(original, after_change, rtol=1e-10, atol=1e-10)


def test_shared_cache_reuses_work_without_changing_results() -> None:
    bars = _bars()
    shared = build_features(bars, use_cache=True)
    independent = build_features(bars, use_cache=False)

    pd.testing.assert_frame_equal(shared.frame, independent.frame)
    assert shared.diagnostics["cache_hits"] > 0
    assert independent.diagnostics["cache_hits"] == 0
    assert shared.diagnostics["cache_misses"] < independent.diagnostics["cache_misses"]


def test_features_preserve_input_data_and_row_identity() -> None:
    bars = _bars()
    original = bars.copy(deep=True)

    result = build_features(bars)

    pd.testing.assert_frame_equal(bars, original)
    pd.testing.assert_index_equal(result.frame.index, bars.index)
    assert len(result.frame) == len(bars)
    assert result.frame.columns.is_unique
    assert all(pd.api.types.is_numeric_dtype(dtype) for dtype in result.frame.dtypes)
    assert not any("target" in name or "future" in name for name in result.frame.columns)


@pytest.mark.parametrize("regime", ["variable", "up", "down", "flat", "zero_volume"])
def test_features_do_not_emit_infinite_values(regime: str) -> None:
    result = build_features(_bars(regime=regime))

    assert not np.isinf(result.frame.to_numpy(dtype=float)).any()
    assert not result.frame["ema_close_10"].tail(30).isna().any()


def test_constant_geometric_growth_has_known_log_returns_and_momentum() -> None:
    result = build_features(_bars(regime="up")).frame

    for lag in (1, 3, 6):
        np.testing.assert_allclose(
            result[f"log_return_{lag}"].iloc[lag:],
            lag * np.log(1.002),
            rtol=1e-9,
            atol=1e-12,
        )
    np.testing.assert_allclose(result["momentum_6"].iloc[6:], 1.002**6 - 1, atol=1e-12)
    assert result["log_return_6"].iloc[:6].isna().all()


@pytest.mark.parametrize("regime,expected", [("up", 100.0), ("down", 0.0), ("flat", 50.0)])
def test_rsi_has_defined_one_direction_and_flat_limits(regime: str, expected: float) -> None:
    result = build_features(_bars(regime=regime)).frame

    np.testing.assert_allclose(result["rsi_14"].tail(100), expected, atol=1e-9)


def test_flat_price_has_known_ema_range_and_bollinger_width() -> None:
    result = build_features(_bars(regime="flat")).frame

    np.testing.assert_allclose(result["ema_close_10"].tail(100), 100.0, atol=1e-12)
    np.testing.assert_allclose(result["atr_14"].tail(100), 4.0, atol=1e-12)
    np.testing.assert_allclose(result["bb_mid_20"].tail(100), 100.0, atol=1e-12)
    np.testing.assert_allclose(result["bb_width_20"].tail(100), 0.0, atol=1e-12)


def test_initial_unavailable_history_is_not_backfilled() -> None:
    frame = build_features(_bars()).frame

    assert frame["log_return_1"].iloc[:1].isna().all()
    assert frame["log_return_3"].iloc[:3].isna().all()
    assert frame["bb_mid_20"].iloc[:19].isna().all()
    assert frame["bb_mid_20"].iloc[19:].notna().all()


def test_separate_symbols_do_not_share_cached_price_values() -> None:
    first = _bars()
    second = first.copy(deep=True)
    second[["open", "high", "low", "close"]] *= 2.0

    first_features = build_features(first).frame
    second_features = build_features(second).frame

    np.testing.assert_allclose(
        second_features["ema_close_10"],
        first_features["ema_close_10"] * 2.0,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        second_features["log_return_1"], first_features["log_return_1"], equal_nan=True
    )


@pytest.mark.parametrize("count", [0, 1, 19])
def test_insufficient_history_preserves_rows_without_fabricated_features(count: int) -> None:
    bars = _bars().iloc[:count]

    result = build_features(bars)

    pd.testing.assert_index_equal(result.frame.index, bars.index)
    assert len(result.frame) == count
    assert result.frame["ema_close_50"].isna().all()
    assert not np.isinf(result.frame.to_numpy(dtype=float)).any()


def test_feature_documentation_matches_output_and_warmed_features_are_usable() -> None:
    result = build_features(_bars())

    assert [spec["name"] for spec in result.feature_specs] == list(result.frame.columns)
    assert all(spec["formula"] for spec in result.feature_specs)
    assert all(spec["availability"] for spec in result.feature_specs)
    assert np.isfinite(result.frame.tail(30).to_numpy(dtype=float)).all()
