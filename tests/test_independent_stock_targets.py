from __future__ import annotations

import json
from datetime import date

import pandas as pd
import numpy as np
import pytest

from ml.gameplan_evaluation import evaluate_forecasts
from ml.independent_stock_targets import (
    STOCK_TARGET_CONTRACT_VERSION, build_stock_current_groups,
    build_stock_training_groups, stock_target_windows,
    STOCK_CALENDAR_FEATURE_NAMES, STOCK_CALENDAR_FEATURE_CONTRACT, stock_target_calendar_features,
)
from ml.nightly_gameplan import _chronological_partitions, _finalize_forecasts, run_nightly_gameplan_once


def _by_route(day):
    return {row["route"]: row for row in stock_target_windows(day)}


def _sources(days=(date(2026, 8, 17),), symbols=("AAPL", "COST")):
    rows = []
    for day in days:
        gap = _by_route(day)["1h@gap"]
        information = gap["target_window_start"] + pd.Timedelta(minutes=5)
        for index, symbol in enumerate(symbols):
            rows.append({"symbol": symbol, "action_date": day,
                         "decision_timestamp": information, "information_available_at": information,
                         "assumed_round_trip_cost": 0.0, "mr__test": float(index),
                         # Deliberately incorrect existing labels must never supply new targets.
                         "forward_raw_return": 999, "target_cost_adjusted_positive": 1})
    return pd.DataFrame(rows)


def _endpoint_bars(sources):
    points = set()
    for source in sources.to_dict("records"):
        for row in stock_target_windows(source["action_date"]):
            gap = row["target_role"] == "OPENING_GAP_RESEARCH"
            points.add((source["symbol"], row["target_window_start"] - pd.Timedelta(minutes=int(gap))))
            points.add((source["symbol"], row["target_window_end"] - pd.Timedelta(minutes=int(not gap))))
    rows = []
    origin = min(timestamp for _, timestamp in points)
    for symbol, timestamp in sorted(points):
        slope = 0.1 if symbol == "AAPL" else -0.1
        price = 10000 + slope * (timestamp - origin).total_seconds() / 3600
        rows.append({"symbol": symbol, "timestamp": timestamp, "open": price,
                     "close": price + slope / 60})
    return pd.DataFrame(rows)


def test_entry_grid_has_exact_independent_roles_and_forward_hours():
    grid = _by_route(date(2026, 9, 8))
    assert len(grid) == 24
    assert sum(row["execution_eligible"] for row in grid.values()) == 19
    hourly = [row for row in grid.values() if row["model_group"] == "1h" and row["execution_eligible"]]
    assert [row["route"] for row in hourly] == [f"1h@{hour:02d}:00" for hour in range(4, 17)]
    assert all(row["target_window_end"] - row["target_window_start"] == pd.Timedelta(hours=1) for row in hourly)
    assert grid["1h@gap"]["target_role"] == "OPENING_GAP_RESEARCH"
    assert grid["1h@gap"]["execution_eligible"] is False
    assert grid["1d@D+1"]["target_window_start"] == pd.Timestamp("2026-09-08T11:00Z")
    assert grid["1d@D+1"]["target_window_end"] == pd.Timestamp("2026-09-09T00:00Z")
    assert all(grid[f"1d@D+{offset}"]["target_role"] == "OUTLOOK" for offset in range(2, 6))
    assert grid["1w@D+5"]["target_window_end"] == pd.Timestamp("2026-09-15T00:00Z")


def test_four_hour_carry_skips_weekend_holiday_and_handles_dst():
    friday = _by_route(date(2026, 9, 4))
    carry = friday["4h@16:00"]
    assert carry["target_window_start"] == pd.Timestamp("2026-09-04T23:00Z")
    assert carry["target_window_end"] == pd.Timestamp("2026-09-08T14:00Z")
    assert carry["trading_hours"] == 4
    assert json.loads(carry["trading_segments_json"]) == [
        ["2026-09-04T23:00:00+00:00", "2026-09-05T00:00:00+00:00"],
        ["2026-09-08T11:00:00+00:00", "2026-09-08T14:00:00+00:00"],
    ]
    assert friday["1w@D+5"]["target_window_end"] == pd.Timestamp("2026-09-12T00:00Z")
    autumn = _by_route(date(2026, 10, 30))["4h@16:00"]
    assert autumn["target_window_start"] == pd.Timestamp("2026-10-30T23:00Z")
    assert autumn["target_window_end"] == pd.Timestamp("2026-11-02T15:00Z")
    assert autumn["trading_hours"] == 4
    with pytest.raises(ValueError, match="not an XNYS session"):
        stock_target_windows(date(2026, 9, 7))


def test_known_calendar_features_distinguish_intraday_overnight_weekend_and_holiday():
    ordinary = _by_route(date(2026, 9, 8))["4h@16:00"]
    friday = _by_route(date(2026, 8, 28))["4h@16:00"]
    holiday = _by_route(date(2026, 9, 4))["4h@16:00"]
    intraday = _by_route(date(2026, 9, 8))["4h@12:00"]
    maps = [stock_target_calendar_features(row) for row in (ordinary, friday, holiday, intraday)]
    assert [row["target__trading_hours"] for row in maps] == [4.0] * 4
    assert [row["target__closed_market_hours"] for row in maps] == [11.0, 59.0, 83.0, 0.0]
    assert [row["target__log_elapsed_minutes"] for row in maps] == [np.log1p(hours * 60) for hours in (15, 63, 87, 4)]
    assert maps[0]["target__entry_clock_sin"] == maps[1]["target__entry_clock_sin"]
    assert maps[0]["target__weekday_sin"] != maps[1]["target__weekday_sin"]
    assert stock_target_calendar_features({**holiday, "target_close": -999, "observed_return": float("nan"),
                                          "mr__trend_atr": 1000}) == maps[2]


def test_training_and_current_export_the_same_known_calendar_contract():
    sources = _sources(days=(date(2026, 8, 27), date(2026, 8, 28)))
    current = build_stock_current_groups(sources, feature_columns=("mr__test",))
    trained = build_stock_training_groups(sources, feature_columns=("mr__test",),
        minute_bars=_endpoint_bars(sources), available_at="2026-09-15T00:00Z")
    for group, frame in trained.items():
        expected = current[group].set_index(["symbol", "route", "action_date"])
        actual = frame.set_index(["symbol", "route", "action_date"])
        pd.testing.assert_frame_equal(actual[list(STOCK_CALENDAR_FEATURE_NAMES)].sort_index(),
            expected.loc[actual.index, list(STOCK_CALENDAR_FEATURE_NAMES)].sort_index())
        assert frame.target_calendar_feature_contract.eq(STOCK_CALENDAR_FEATURE_CONTRACT).all()


def test_new_labels_use_observed_extended_equity_endpoints_not_regular_labels():
    sources = _sources()
    bars = _endpoint_bars(sources)
    groups = build_stock_training_groups(sources, feature_columns=("mr__test",), minute_bars=bars,
                                          available_at="2026-08-25T00:00Z")
    assert sum(map(len, groups.values())) == 48
    for group, frame in groups.items():
        assert set(frame.target_contract_version) == {STOCK_TARGET_CONTRACT_VERSION}
        assert frame.loc[frame.symbol.eq("AAPL"), "target"].eq(1).all()
        assert frame.loc[frame.symbol.eq("COST"), "target"].eq(0).all()
        assert frame.observed_return.abs().lt(0.1).all()
        assert frame.attrs["target_boundary_quality"]["excluded_rows"] == 0
    day = groups["1d"].query("symbol == 'COST' and route == '1d@D+1'").iloc[0]
    start = bars.loc[bars.symbol.eq("COST") & bars.timestamp.eq(day.target_window_start)].iloc[0]
    finish = bars.loc[bars.symbol.eq("COST") & bars.timestamp.eq(day.target_window_end - pd.Timedelta(minutes=1))].iloc[0]
    assert day.observed_return == pytest.approx(finish.close / start.open - 1)


@pytest.mark.parametrize("damage", ["missing", "shifted", "conflicting"])
def test_missing_or_outside_policy_extended_endpoint_is_excluded(damage):
    sources = _sources()
    bars = _endpoint_bars(sources)
    boundary = _by_route(date(2026, 8, 17))["1d@D+1"]["target_window_start"]
    selected = bars.symbol.eq("COST") & bars.timestamp.eq(boundary)
    if damage == "missing":
        bars = bars.loc[~selected]
    elif damage == "shifted":
        bars.loc[selected, "timestamp"] += pd.Timedelta(minutes=6)
    else:
        conflicting = bars.loc[selected].copy()
        conflicting["open"] += 5
        bars = pd.concat([bars, conflicting], ignore_index=True)
    groups = build_stock_training_groups(sources, feature_columns=(), minute_bars=bars,
                                          available_at="2026-08-25T00:00Z")
    for group, route in (("1h", "1h@04:00"), ("4h", "4h@04:00"), ("1d", "1d@D+1"), ("1w", "1w@D+5")):
        frame = groups[group]
        assert frame.loc[frame.symbol.eq("COST") & frame.route.eq(route)].empty
        assert frame.attrs["target_boundary_quality"]["excluded_rows_by_route"][route] >= 1


@pytest.mark.parametrize("route", ["1h@04:00", "4h@16:00", "1d@D+1", "1w@D+5"])
@pytest.mark.parametrize("entry_minutes,exit_minutes,admitted", [
    (0, 0, True), (5, -5, True), (6, 0, False), (0, -6, False),
    (-1, 0, False), (0, 1, False),
])
def test_independent_windows_keep_native_five_minute_in_window_observations(
    route, entry_minutes, exit_minutes, admitted,
):
    from ml.nightly_gameplan import TARGET_BOUNDARY_TOLERANCE
    from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE

    assert STOCK_TARGET_BOUNDARY_TOLERANCE == TARGET_BOUNDARY_TOLERANCE
    day = date(2026, 9, 4)
    spec = _by_route(day)[route]
    entry = spec["target_window_start"] + pd.Timedelta(minutes=entry_minutes)
    exit_time = spec["target_window_end"] + pd.Timedelta(minutes=exit_minutes)
    bars = pd.DataFrame([
        {"symbol": "COST", "timestamp": entry, "open": 100.0, "close": 901.0},
        {"symbol": "COST", "timestamp": exit_time - pd.Timedelta(minutes=1), "open": 902.0, "close": 102.0},
    ])
    groups = build_stock_training_groups(_sources(days=(day,), symbols=("COST",)),
        feature_columns=(), minute_bars=bars, available_at=spec["target_window_end"] + pd.Timedelta(days=1))
    frame = groups[spec["model_group"]]
    selected = frame.loc[frame.route.eq(route)]
    assert len(selected) == int(admitted)
    assert frame.attrs["target_boundary_quality"]["maximum_boundary_gap_seconds"] == 300
    if admitted:
        row = selected.iloc[0]
        assert row.target_window_start == spec["target_window_start"]
        assert row.target_window_end == spec["target_window_end"]
        assert row.observed_open_timestamp == entry
        assert row.observed_close_timestamp == exit_time
        assert row.target_start_gap_seconds == entry_minutes * 60
        assert row.target_end_gap_seconds == -exit_minutes * 60
        assert row.observed_return == pytest.approx(0.02)


def test_opening_gap_keeps_native_bounded_close_to_open_research_observations():
    day = date(2026, 9, 8)
    spec = _by_route(day)["1h@gap"]
    bars = pd.DataFrame([
        {"symbol": "COST", "timestamp": spec["target_window_start"] - pd.Timedelta(minutes=6), "open": 901.0, "close": 100.0},
        {"symbol": "COST", "timestamp": spec["target_window_end"] + pd.Timedelta(minutes=5), "open": 102.0, "close": 902.0},
    ])
    groups = build_stock_training_groups(_sources(days=(day,), symbols=("COST",)),
        feature_columns=(), minute_bars=bars, available_at=spec["target_window_end"] + pd.Timedelta(hours=1))
    row = groups["1h"].query("route == '1h@gap'").iloc[0]
    assert row.observed_return == pytest.approx(0.02)
    assert row.target_start_gap_seconds == row.target_end_gap_seconds == 300
    assert not row.execution_eligible


def test_future_endpoints_never_become_training_labels():
    sources = _sources()
    groups = build_stock_training_groups(sources, feature_columns=(), minute_bars=_endpoint_bars(sources),
                                          available_at="2026-08-17T12:00Z")
    assert groups["1d"].empty and groups["1w"].empty and groups["4h"].empty
    assert set(groups["1h"].route) == {"1h@gap", "1h@04:00"}


@pytest.mark.parametrize("probability,direction", [(.499999, "BEARISH"), (.5, "NO_EDGE"), (.500001, "BULLISH")])
def test_independent_finalization_preserves_contract_and_action_starts(probability, direction):
    sources = _sources(days=(date(2026, 9, 8),))
    groups = build_stock_current_groups(sources, feature_columns=())
    forecasts = pd.concat(groups.values(), ignore_index=True).assign(calibrated_probability=probability, model_status="PROMOTED")
    unsupported = forecasts.symbol.eq("COST") & forecasts.model_group.isin(["1d", "1w"])
    forecasts.loc[unsupported, "model_status"] = "RESEARCH_NO_TARGET_HISTORY"
    output = _finalize_forecasts(forecasts, symbols=("AAPL", "COST"), action_date=date(2026, 9, 8),
        frozen_at=pd.Timestamp("2026-09-08T05:00Z"), action_start=pd.Timestamp("2026-09-08T11:00Z"),
        action_end=pd.Timestamp("2026-09-09T00:00Z"), opra_freshness={"completed_through": "2026-09-05"},
        target_contract_version=STOCK_TARGET_CONTRACT_VERSION)
    assert len(output) == 48
    assert output.loc[output.route.eq("4h@08:00"), "action_anchor_local"].eq("08:00").all()
    assert output.loc[~output.execution_eligible, "action_anchor_local"].isna().all()
    assert output.target_contract_version.eq(STOCK_TARGET_CONTRACT_VERSION).all()
    assert output.loc[output.model_status.eq("RESEARCH_NO_TARGET_HISTORY"), "direction"].eq("NO_EDGE").all()
    assert output.loc[output.model_status.eq("PROMOTED"), "direction"].eq(direction).all()
    assert output.direction_policy_version.eq("stock-direction-50-v2").all()
    assert output.direction_up_threshold.eq(.5).all() and output.direction_down_threshold.eq(.5).all()


def test_new_and_legacy_evaluation_cannot_exchange_labels():
    start, end = pd.Timestamp("2026-09-08T11:00Z"), pd.Timestamp("2026-09-08T12:00Z")
    base = {"symbol": "COST", "route": "1h@04:00", "model_group": "1h", "action_date": "2026-09-08",
            "target_window_start": start, "target_window_end": end, "calibrated_probability": 0.7,
            "source_gameplan_run": "saved", "model_status": "PROMOTED"}
    forecasts = pd.DataFrame([{**base, "id": "old", "target_contract_version": "overnight-path-targets-v2"},
                             {**base, "id": "new", "target_contract_version": STOCK_TARGET_CONTRACT_VERSION}])
    outcome = pd.DataFrame([{**base, "target": 1, "observed_return": 0.02,
                              "target_contract_version": STOCK_TARGET_CONTRACT_VERSION}])
    result = evaluate_forecasts(forecasts, observed_groups={"new": outcome}, evaluated_at=end)
    assert result.set_index("source_forecast_id").loc["old", "evaluation_status"] == "MATURE_AWAITING_DATA"
    assert result.set_index("source_forecast_id").loc["new", "evaluation_status"] == "EVALUATED"


def test_opt_in_cannot_silently_change_default_options_publication(tmp_path):
    with pytest.raises(ValueError, match="require explicit stock-only"):
        run_nightly_gameplan_once(tmp_path, independent_stock_horizons=True)


def test_independent_labels_keep_native_chronological_partition_and_purging():
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNYS", start="2026-03-01", end="2026-07-01")
    days = [pd.Timestamp(value).date() for value in calendar.sessions[:60]]
    sources = _sources(days=days)
    groups = build_stock_training_groups(sources, feature_columns=("mr__test",),
                                          minute_bars=_endpoint_bars(sources), available_at="2026-07-01T00:00Z")
    for group, frame in groups.items():
        partitions = _chronological_partitions(frame, group=group)
        for left, right in (("train", "selection"), ("selection", "calibration"), ("calibration", "assessment")):
            assert partitions[left].target_window_end.max() < partitions[right].target_window_start.min()
        assert all(set(partition.target) == {0, 1} for partition in partitions.values())
