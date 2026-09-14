from datetime import date

import pandas as pd
import pytest

from ml.gameplan_source_selection import (
    GAMEPLAN_SOURCE_SELECTION_VERSION,
    SOURCE_SELECTION_COLUMNS,
    select_prior_session_sources,
    source_selection_contract,
)


def _row(day="2026-01-06", *, hour=12, symbol="COST", value=1.0):
    start = pd.Timestamp(f"{day} {hour:02d}:00", tz="America/Los_Angeles").tz_convert("UTC")
    end = start + pd.Timedelta(hours=1)
    available = end + pd.Timedelta(minutes=5)
    return {
        "id": f"{symbol}-{start}", "symbol": symbol, "provider": "databento",
        "venue": "NASDAQ", "currency": "USD", "timeframe": "1h", "horizon": "1h",
        "exchange_calendar": "XNAS", "exchange_session": pd.Timestamp(day, tz="UTC"),
        "bar_timestamp": start, "bar_end_timestamp": end,
        "information_available_at": available, "decision_timestamp": available,
        "target_window_start": available + pd.Timedelta(hours=1),
        "target_window_end": available + pd.Timedelta(hours=2),
        "label_status": "COMPLETE", "target_open": 99.0,
        "assumed_round_trip_cost": 0.001, "mr__technical_score": value,
    }


def _select(rows, *, now="2026-12-31T23:00:00Z", symbols=("COST",)):
    return select_prior_session_sources(
        pd.DataFrame(rows), symbols=symbols, available_at=pd.Timestamp(now),
        feature_columns=["mr__technical_score"],
    )


def test_regular_close_source_does_not_require_a_rolling_overnight_target():
    row = _row()
    frame = pd.DataFrame([row])
    original = frame.copy(deep=True)
    result = select_prior_session_sources(frame, symbols=["COST"],
                                          available_at=pd.Timestamp("2026-01-07T04:00Z"),
                                          feature_columns=["mr__technical_score"])
    selected = result.iloc[0]
    assert selected.action_date == date(2026, 1, 7)
    assert selected.source_session == date(2026, 1, 6)
    assert selected.source_action_start == pd.Timestamp("2026-01-07T12:00Z")
    assert selected.source_regular_close == row["bar_end_timestamp"]
    assert selected.target_window_start == row["target_window_start"]
    assert selected.information_available_at == row["information_available_at"]
    assert selected.decision_timestamp == row["decision_timestamp"]
    assert set(SOURCE_SELECTION_COLUMNS).issubset(result.columns)
    assert selected.source_bar_end_timestamp == row["bar_end_timestamp"]
    assert result.attrs["source_selection"]["source_selection_contract"] == GAMEPLAN_SOURCE_SELECTION_VERSION
    pd.testing.assert_frame_equal(frame, original)


def test_latest_completed_source_selected_independently_for_each_symbol():
    result = _select([_row(hour=12), _row(hour=16, value=2),
                      _row(hour=13, symbol="AAPL", value=3)], symbols=("COST", "AAPL"))
    assert result.set_index("symbol")["mr__technical_score"].to_dict() == {"AAPL": 3, "COST": 2}
    assert result.attrs["source_selection"]["selected_rows_by_symbol"] == {"AAPL": 1, "COST": 1}


@pytest.mark.parametrize("source_day,action_day,action_start", [
    ("2026-01-16", "2026-01-20", "2026-01-20T12:00Z"),  # MLK holiday
    ("2026-03-06", "2026-03-09", "2026-03-09T11:00Z"),  # spring DST
    ("2026-10-30", "2026-11-02", "2026-11-02T12:00Z"),  # fall DST
    ("2026-09-04", "2026-09-08", "2026-09-08T11:00Z"),  # Labor Day
])
def test_next_exchange_session_handles_holidays_weekends_and_dst(source_day, action_day, action_start):
    result = _select([_row(source_day)]).iloc[0]
    assert result.action_date == pd.Timestamp(action_day).date()
    assert result.source_action_start == pd.Timestamp(action_start)


def test_early_close_regular_bar_is_fresh_and_no_missing_session_is_filled():
    selected = _select([_row("2026-11-27", hour=9)]).iloc[0]
    assert selected.source_regular_close == pd.Timestamp("2026-11-27T18:00Z")
    assert selected.action_date == date(2026, 11, 30)
    result = _select([_row("2026-01-05"), _row("2026-01-07")])
    assert result.action_date.tolist() == [date(2026, 1, 6), date(2026, 1, 8)]
    assert date(2026, 1, 7) not in result.action_date.tolist()


def test_17_hour_bar_and_17_05_availability_are_inclusive():
    row = _row(hour=16)
    selected = _select([row]).iloc[0]
    assert selected.source_bar_end_timestamp == pd.Timestamp("2026-01-07T01:00Z")
    assert selected.source_feature_cutoff == pd.Timestamp("2026-01-07T01:05Z")
    assert selected.information_available_at == selected.source_feature_cutoff


def test_asof_preserves_processing_delay_and_selects_earlier_available_row():
    latest = _row(hour=16, value=2)
    result = _select([_row(value=1), latest], now="2026-01-07T01:04:59.999999999Z")
    assert result.iloc[0]["mr__technical_score"] == 1
    assert result.iloc[0].source_effective_cutoff == pd.Timestamp("2026-01-07T01:04:59.999999999Z")
    result = _select([latest], now="2026-01-07T01:05Z")
    assert result.iloc[0]["mr__technical_score"] == 2


@pytest.mark.parametrize("change", [
    {"exchange_session": pd.Timestamp("2026-01-07", tz="UTC")},
    {"exchange_session": pd.Timestamp("2026-01-06T01:00Z")},
    {"exchange_calendar": "XLON"},
    {"bar_timestamp": pd.Timestamp("2026-01-06T19:00Z")},  # two-hour source
    {"bar_timestamp": pd.Timestamp("2026-01-06T20:00")},  # naive clock
    {"bar_end_timestamp": pd.NaT},
    {"information_available_at": pd.Timestamp("2026-01-06T20:59Z")},
    {"decision_timestamp": pd.Timestamp("2026-01-06T21:04Z")},
    {"decision_timestamp": pd.Timestamp("2026-01-07T01:05:00.000000001Z")},
])
def test_invalid_noncausal_or_late_sources_are_rejected(change):
    row = {**_row(), **change}
    with pytest.raises(RuntimeError, match="No independent Gameplan"):
        _select([row])


@pytest.mark.parametrize("row", [
    _row(hour=11),  # latest bar ends before regular close
    _row(hour=17),  # bar ends after extended session
    _row("2026-01-10"),  # weekend
    _row("2026-01-19"),  # holiday
])
def test_stale_outside_session_or_closed_day_rows_do_not_supply_a_source(row):
    with pytest.raises(RuntimeError, match="No independent Gameplan"):
        _select([row])


def test_rolling_target_copies_collapse_without_using_label_values():
    row = _row()
    duplicate = {**row, "id": "another target", "target_window_start": pd.NaT,
                 "target_window_end": pd.NaT, "target_open": 200,
                 "label_status": "INCOMPLETE_LABEL"}
    result = _select([row, duplicate])
    assert len(result) == 1
    assert result.attrs["source_selection"]["eligible_expanded_rows"] == 2
    assert result.attrs["source_selection"]["unique_eligible_observations"] == 1


@pytest.mark.parametrize("changed_column,new_value", [
    ("mr__technical_score", 2), ("provider", "other-provider"),
    ("information_available_at", pd.Timestamp("2026-01-06T21:04Z")),
])
def test_conflicting_source_copies_cannot_be_selected_arbitrarily(changed_column, new_value):
    row = _row()
    changed = {**row, changed_column: new_value}
    with pytest.raises(RuntimeError, match="Conflicting independent Gameplan"):
        _select([row, changed])


def test_repeated_dataframe_indices_do_not_change_selection():
    frame = pd.DataFrame([_row(hour=12), _row(hour=16, value=2)], index=[0, 0])
    result = select_prior_session_sources(frame, symbols=["COST"],
                                          available_at=pd.Timestamp("2026-01-07T04:00Z"),
                                          feature_columns=["mr__technical_score"])
    assert len(result) == 1
    assert result.iloc[0]["mr__technical_score"] == 2


def test_required_metadata_and_aware_asof_are_explicit():
    frame = pd.DataFrame([_row()])
    with pytest.raises(ValueError, match="missing columns"):
        select_prior_session_sources(frame.drop(columns="bar_end_timestamp"), symbols=["COST"],
                                     available_at=pd.Timestamp("2026-01-07T04:00Z"), feature_columns=[])
    with pytest.raises(ValueError, match="timezone-aware"):
        select_prior_session_sources(frame, symbols=["COST"],
                                     available_at=pd.Timestamp("2026-01-07T04:00"), feature_columns=[])


def test_contract_helper_distinguishes_legacy_and_rejects_mixed_or_missing_versions():
    assert source_selection_contract(pd.DataFrame({"symbol": ["COST"]})) is None
    assert source_selection_contract(_select([_row()])) == GAMEPLAN_SOURCE_SELECTION_VERSION
    for values in ([None], ["unknown"], [GAMEPLAN_SOURCE_SELECTION_VERSION, None],
                   [GAMEPLAN_SOURCE_SELECTION_VERSION, "unknown"]):
        with pytest.raises(ValueError, match="source selection contract"):
            source_selection_contract(pd.DataFrame({"source_selection_contract": values}))
