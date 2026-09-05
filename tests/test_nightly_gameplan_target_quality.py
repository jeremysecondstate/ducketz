from datetime import date

import pandas as pd
import pytest

from ml.nightly_gameplan import _intraday_outcomes


def source_rows():
    return pd.DataFrame([{
        "symbol": "AAPL", "action_date": date(2026, 9, 4),
        "decision_timestamp": pd.Timestamp("2026-09-04T00:05:00Z"),
    }])


def minute_rows(*timestamps):
    return pd.DataFrame({
        "symbol": "AAPL", "timestamp": pd.to_datetime(timestamps, utc=True),
        "open": 100.0, "close": 102.0,
    })


@pytest.mark.parametrize("first,last,admitted", [
    ("19:00", "22:59", True),
    ("19:05", "22:54", True),  # Five minutes at each boundary is inclusive.
    ("19:06", "22:59", False),
    ("19:00", "22:53", False),
    ("19:00", "19:59", False),  # The observed return ends at 13:00, not 16:00 PT.
])
def test_four_hour_labels_require_prices_near_both_boundaries(first, last, admitted):
    bars = minute_rows(f"2026-09-04T{first}:00Z", f"2026-09-04T{last}:00Z")
    _, four = _intraday_outcomes(sources=source_rows(), feature_columns=(), minute_bars=bars)
    assert len(four) == int(admitted)
    quality = four.attrs["target_boundary_quality"]
    assert quality["candidate_rows"] == 1
    assert quality["excluded_rows"] == int(not admitted)
    if admitted:
        row = four.iloc[0]
        assert row.target_window_end == pd.Timestamp("2026-09-04T23:00:00Z")
        assert row.observed_close_timestamp == bars.iloc[-1].timestamp + pd.Timedelta(minutes=1)
        assert row.observed_return == pytest.approx(0.02)
    else:
        assert quality["misaligned_rows_by_route"] == {"4h@16:00": 1}


@pytest.mark.parametrize("prior_last,current_first,admitted", [
    ("23:59", "11:00", True),
    ("23:54", "11:05", True),
    ("23:53", "11:00", False),
    ("23:59", "11:06", False),
])
def test_overnight_gap_checks_prior_close_and_current_open(prior_last, current_first, admitted):
    bars = minute_rows(f"2026-09-03T{prior_last}:00Z", f"2026-09-04T{current_first}:00Z")
    hourly, four = _intraday_outcomes(sources=source_rows(), feature_columns=(), minute_bars=bars)
    for group, prefix in ((hourly, "1h"), (four, "4h")):
        gap = group.loc[group.route.eq(f"{prefix}@04:00")]
        assert len(gap) == int(admitted)
        if admitted:
            assert gap.iloc[0].target_window_start == pd.Timestamp("2026-09-04T00:00:00Z")
            assert gap.iloc[0].target_window_end == pd.Timestamp("2026-09-04T11:00:00Z")
            assert gap.iloc[0].observed_return == pytest.approx(100 / 102 - 1)


def test_stale_prices_are_retained_only_in_explicit_historical_reconstruction():
    bars = minute_rows("2026-09-04T19:00:00Z", "2026-09-04T19:59:00Z")
    _, four = _intraday_outcomes(
        sources=source_rows(), feature_columns=(), minute_bars=bars,
        enforce_boundary_alignment=False,
    )
    assert len(four) == 1
    assert not four.iloc[0].target_boundary_aligned
    assert four.iloc[0].target_end_gap_seconds == 180 * 60
    assert four.attrs["target_boundary_quality"]["enforced"] is False


def test_single_bar_cannot_stand_in_for_a_four_hour_window():
    bars = minute_rows("2026-09-04T19:00:00Z")
    _, four = _intraday_outcomes(sources=source_rows(), feature_columns=(), minute_bars=bars)
    assert four.empty
    assert four.attrs["target_boundary_quality"]["excluded_rows"] == 1


def test_unusable_prices_leave_empty_outcomes_with_required_columns():
    bars = minute_rows("2026-09-04T19:00:00Z", "2026-09-04T22:59:00Z")
    bars["close"] = -1.0
    hourly, four = _intraday_outcomes(sources=source_rows(), feature_columns=(), minute_bars=bars)
    for group in (hourly, four):
        assert group.empty
        assert {"symbol", "route", "target", "target_window_end"} <= set(group.columns)
