from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from ml.contracts import FeatureSet, MLContractError
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.loop_b_research_features import (
    FEATURE_COLUMNS,
    FEATURE_FAMILIES,
    PATH_LOOKBACK,
    RESEARCH_FEATURE_SPECS,
    calculate_research_ohlcv_features,
    semantic_contract,
)
from ml.model_features import model_matrix_for_feature_set
from ml.preprocessing import SemanticFeatureTransformer


def _bars(start="2026-01-05", end="2026-03-20", *, timeframe="1d") -> pd.DataFrame:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNYS", start=start, end=end)
    schedule = calendar.schedule.loc[start:end]
    if timeframe == "1d":
        stamps = pd.DatetimeIndex(schedule.index).tz_localize("UTC")
    else:
        stamps = pd.DatetimeIndex([
            stamp
            for label in schedule.index
            for stamp in pd.date_range(
                pd.Timestamp(label).tz_localize("America/New_York") + pd.Timedelta(hours=4),
                periods=16, freq="h",
            )
        ]).tz_convert("UTC")
    values = 100 * np.exp(np.arange(len(stamps)) * 0.001 + 0.002 * np.sin(np.arange(len(stamps))))
    return pd.DataFrame({
        "timestamp": stamps,
        "open": values * 0.999,
        "high": values * 1.004,
        "low": values * 0.995,
        "close": values,
        "volume": 1000.0,
        "is_native": True,
    })


def _calculate(bars: pd.DataFrame, timeframe="1d", **kwargs) -> pd.DataFrame:
    return calculate_research_ohlcv_features(bars, symbol="TEST", timeframe=timeframe, **kwargs)


@pytest.mark.parametrize("timeframe", ["1h", "1d"])
def test_prefix_invariance_under_appended_and_modified_future_bars(timeframe) -> None:
    bars = _bars(timeframe=timeframe)
    cutoff = len(bars) // 2
    prefix = _calculate(bars.iloc[:cutoff], timeframe)
    full = _calculate(bars, timeframe)
    pdt.assert_frame_equal(prefix, full.iloc[:cutoff].reset_index(drop=True))
    changed = bars.copy()
    changed.loc[cutoff:, ["open", "high", "low", "close"]] *= 3
    changed.loc[cutoff:, "volume"] *= 500
    changed.loc[cutoff:, "is_native"] = False
    pdt.assert_frame_equal(prefix, _calculate(changed, timeframe).iloc[:cutoff].reset_index(drop=True))


def test_fixed_specifications_are_quarantined_and_cannot_enter_production() -> None:
    assert PATH_LOOKBACK == 10
    contract = semantic_contract()
    assert contract["range_lookback_sessions"] == 20
    assert contract["range_minimum_native_comparators"] == 10
    assert set(FEATURE_COLUMNS).isdisjoint(DEFAULT_FEATURE_REGISTRY.feature_names)
    assert sum(map(len, FEATURE_FAMILIES.values())) == 4
    versions = {spec.name: spec.transform_version for spec in RESEARCH_FEATURE_SPECS}
    measurements = _calculate(_bars())[list(FEATURE_COLUMNS)]
    pdt.assert_frame_equal(SemanticFeatureTransformer(versions).fit_transform(measurements), measurements)
    feature_set = FeatureSet("research-fixture", RESEARCH_FEATURE_SPECS)
    with pytest.raises(MLContractError, match="unavailable features"):
        model_matrix_for_feature_set(pd.DataFrame(columns=FEATURE_COLUMNS), feature_set)
    # Changing activation in an isolated research profile cannot waive the
    # existing target/future-column safeguard.
    bad = replace(RESEARCH_FEATURE_SPECS[0], activation_status="ACTIVE", source_column="future_return")
    with pytest.raises(MLContractError, match="post-decision"):
        model_matrix_for_feature_set(pd.DataFrame({bad.name: [1.0]}), FeatureSet("bad", (bad,)))


def test_daily_warmup_formulas_and_direction_are_exact() -> None:
    bars = _bars()
    returns = np.array([0.01, -0.02, 0.005, 0.01, -0.015, 0.01, 0.01, -0.005, 0.01, 0.01])
    close = 100 * np.exp(np.r_[0, np.cumsum(returns)])
    bars = bars.iloc[:11].copy()
    for column in ("open", "high", "low", "close"):
        bars[column] = close
    bars["high"] *= 1.01
    bars["low"] *= 0.99
    result = _calculate(bars)
    assert result.loc[:9, list(FEATURE_COLUMNS)].isna().all().all()
    row = result.iloc[10]
    assert row[FEATURE_COLUMNS[0]] == pytest.approx(0, abs=1e-12)
    assert row[FEATURE_COLUMNS[1]] == pytest.approx(np.square(np.minimum(returns, 0)).sum() / np.square(returns).sum())
    assert row[FEATURE_COLUMNS[2]] == pytest.approx(np.square(returns).sum() / np.abs(returns).sum() ** 2)
    assert row[FEATURE_COLUMNS[3]] == pytest.approx(returns.sum() * abs(np.sign(returns).mean()))
    falling = bars.copy()
    for column in ("open", "high", "low", "close"):
        falling[column] = 100 * np.exp(-np.arange(11) * 0.01)
    assert _calculate(falling).iloc[-1][FEATURE_COLUMNS[3]] == pytest.approx(-0.1)


def test_activity_has_no_session_or_global_fallback_and_excludes_current_session() -> None:
    bars = _bars(timeframe="1h")
    local = bars["timestamp"].dt.tz_convert("America/New_York")
    # Same-slot normalized range is constant, while other slots are very wide.
    slot = local.dt.hour.eq(11)
    bars.loc[~slot, "high"] *= 2
    result = _calculate(bars, "1h")
    selected = result.loc[slot]
    assert selected.iloc[:10][FEATURE_COLUMNS[0]].isna().all()
    assert selected.iloc[10][FEATURE_COLUMNS[0]] == pytest.approx(0, abs=1e-12)
    idx = selected.index[10]
    changed = bars.copy()
    changed.loc[idx, "high"] = changed.loc[idx, "close"] * 1.013
    assert _calculate(changed, "1h").loc[idx, FEATURE_COLUMNS[0]] == pytest.approx(np.log(2))
    assert selected.iloc[10]["range_comparator_count"] == 10


@pytest.mark.parametrize("change", ["missing", "derived", "unknown", "incomplete", "invalid"])
def test_missing_or_ineligible_native_rth_bars_break_path_without_filling(change) -> None:
    bars = _bars(timeframe="1h")
    regular = bars["timestamp"].dt.tz_convert("America/New_York").dt.hour.between(10, 15)
    index = bars.index[regular][15]
    stamp = bars.loc[index, "timestamp"]
    if change == "missing":
        bars = bars.drop(index=index)
    elif change == "derived":
        bars.loc[index, "is_native"] = False
    elif change == "unknown":
        bars["is_native"] = bars["is_native"].astype("boolean")
        bars.loc[index, "is_native"] = pd.NA
    elif change == "incomplete":
        bars["bar_complete"] = True
        bars.loc[index, "bar_complete"] = False
    else:
        bars.loc[index, "close"] = np.nan
    result = _calculate(bars, "1h")
    later = result.loc[result["path_slot_eligible"] & result["bar_timestamp"].gt(stamp)]
    assert later.iloc[:10][list(FEATURE_COLUMNS[1:])].isna().all().all()
    assert later.iloc[10][list(FEATURE_COLUMNS[1:])].notna().all()


def test_absent_extended_hours_do_not_break_regular_path_but_regular_missing_does() -> None:
    bars = _bars(timeframe="1h")
    local_hour = bars["timestamp"].dt.tz_convert("America/New_York").dt.hour
    regular = bars.loc[local_hour.between(10, 15)]
    full = _calculate(bars, "1h").set_index("bar_timestamp")
    result = _calculate(regular, "1h").set_index("bar_timestamp")
    pdt.assert_frame_equal(result[list(FEATURE_COLUMNS[1:])], full.loc[result.index, list(FEATURE_COLUMNS[1:])])
    assert full.loc[~full["path_slot_eligible"], list(FEATURE_COLUMNS[1:])].isna().all().all()


def test_same_slot_baseline_is_not_allowed_to_use_stale_old_sessions() -> None:
    bars = _bars()
    # Retain ten old sessions and a much later session. Last-twenty-observations
    # rolling would fabricate coverage; the scheduled-session window does not.
    sparse = pd.concat([bars.iloc[:10], bars.iloc[-1:]])
    assert _calculate(sparse).iloc[-1]["range_comparator_count"] == 0
    assert pd.isna(_calculate(sparse).iloc[-1][FEATURE_COLUMNS[0]])


@pytest.mark.parametrize("timeframe", ["1h", "1d"])
def test_availability_cutoff_and_later_cutoff_do_not_change_earlier_features(timeframe) -> None:
    bars = _bars(timeframe=timeframe)
    full = _calculate(bars, timeframe)
    index = len(full) // 2
    cutoff = full.loc[index, "available_at"] - pd.Timedelta(seconds=1)
    limited = _calculate(bars, timeframe, as_of=cutoff)
    pdt.assert_frame_equal(limited.iloc[:index], full.iloc[:index])
    assert limited.loc[index:, list(FEATURE_COLUMNS)].isna().all().all()
    with pytest.raises(ValueError, match="timezone"):
        _calculate(bars, timeframe, as_of="2026-02-01")


def test_holiday_early_close_and_dst_are_exchange_aware() -> None:
    bars = _bars("2026-10-05", "2026-11-30", timeframe="1h")
    result = _calculate(bars, "1h").set_index("bar_timestamp")
    assert result.loc[pd.Timestamp("2026-10-30T14:00Z"), "session_type"] == "REGULAR"
    assert result.loc[pd.Timestamp("2026-11-02T15:00Z"), "session_type"] == "REGULAR"
    assert result.loc[pd.Timestamp("2026-11-27T17:00Z"), "available_at"] == pd.Timestamp("2026-11-27T18:05Z")
    assert not result.loc[pd.Timestamp("2026-11-27T18:00Z"), "session_eligible"]
    # An observed provider row on an exchange holiday is never manufactured
    # into a session and never contributes to later calculations.
    holiday = bars.iloc[:1].copy()
    holiday["timestamp"] = pd.Timestamp("2026-11-26T15:00Z")
    observed = _calculate(pd.concat([bars, holiday]), "1h")
    holiday_row = observed.loc[observed["bar_timestamp"].eq(holiday.iloc[0]["timestamp"])].iloc[0]
    assert not holiday_row["session_eligible"]
    assert holiday_row[list(FEATURE_COLUMNS)].isna().all()
    daily = _calculate(_bars("2026-10-05", "2026-11-30"))
    shortened = daily.loc[daily["session_date"].eq("2026-11-27")].iloc[0]
    assert shortened["available_at"] == pd.Timestamp("2026-11-27T18:05Z")
    assert shortened["range_comparator_count"] == 0
    assert pd.isna(shortened[FEATURE_COLUMNS[0]])


def test_flat_prices_zero_range_and_invalid_volume_have_explicit_missing_behavior() -> None:
    bars = _bars()
    bars[["open", "high", "low", "close"]] = 100.0
    bars["volume"] = 0.0
    result = _calculate(bars)
    assert result[list(FEATURE_COLUMNS[:3])].isna().all().all()
    assert result[FEATURE_COLUMNS[3]].iloc[10:].eq(0).all()
    bars.loc[20, "volume"] = -1
    assert not _calculate(bars).loc[20, "native_input_eligible"]


def test_validation_is_fail_closed_and_source_frames_are_not_modified() -> None:
    bars = _bars()
    before = bars.copy(deep=True)
    _calculate(bars)
    pdt.assert_frame_equal(bars, before)
    with pytest.raises(ValueError, match="is_native"):
        _calculate(bars.drop(columns="is_native"))
    bad = bars.copy()
    bad["is_native"] = "true"
    with pytest.raises(ValueError, match="explicit boolean"):
        _calculate(bad)
    with pytest.raises(ValueError, match="unique timestamps"):
        _calculate(pd.concat([bars, bars.iloc[:1]]))
    bad = bars.copy()
    bad["bar_end_timestamp"] = bad["timestamp"] + pd.Timedelta(days=1)
    with pytest.raises(ValueError, match="bar ends conflict"):
        _calculate(bad)
    assert len(_calculate(bars.iloc[:0])) == 0


def test_split_scale_invariance_and_input_order_invariance() -> None:
    bars = _bars()
    scaled = bars.copy()
    scaled[["open", "high", "low", "close"]] *= 7
    expected = _calculate(bars)
    pdt.assert_frame_equal(_calculate(scaled)[list(FEATURE_COLUMNS)], expected[list(FEATURE_COLUMNS)], atol=1e-12, rtol=1e-12)
    pdt.assert_frame_equal(_calculate(bars.iloc[::-1]), expected)


def test_bar_timestamp_alias_is_exact_and_daily_labels_must_be_midnight() -> None:
    bars = _bars()
    pdt.assert_frame_equal(_calculate(bars), _calculate(bars.rename(columns={"timestamp": "bar_timestamp"})))
    bars.loc[20, "timestamp"] += pd.Timedelta(hours=1)
    row = _calculate(bars).loc[20]
    assert not row["session_eligible"]
    assert row[list(FEATURE_COLUMNS)].isna().all()
