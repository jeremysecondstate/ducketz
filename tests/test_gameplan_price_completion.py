from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from ml.gameplan_price_completion import complete_planning_reference_gaps
from ml.stock_target_prices import XNAS_STOCK_PRICE_SOURCE


def _bar(local="2026-09-10 16:46", price=902.2, symbol="COST", **extra):
    return {"symbol": symbol, "timestamp": pd.Timestamp(local, tz="America/Los_Angeles").tz_convert("UTC"),
            "open": price, "close": price, **extra}


def _prices(rows=None):
    frame = pd.DataFrame([_bar()] if rows is None else rows,
                         columns=None if rows else ["symbol", "timestamp", "open", "close"])
    partitions = []
    for row in frame.to_dict("records"):
        session = pd.Timestamp(row["timestamp"]).tz_convert("America/Los_Angeles").normalize()
        boundary = (session + pd.Timedelta(hours=17)).tz_convert("UTC")
        evidence = {"symbol": row["symbol"], "start": session.tz_convert("UTC").isoformat(),
                    "end": boundary.isoformat(), "published_at": (boundary + pd.Timedelta(hours=1)).isoformat(),
                    "manifest_path": f"C:/verified/{row['symbol']}/{session.date().isoformat()}/manifest.json",
                    "request_id": f"{row['symbol']}-{session.date().isoformat()}"}
        if evidence not in partitions:
            partitions.append(evidence)
    frame.attrs["stock_price_source"] = {"source_contract": XNAS_STOCK_PRICE_SOURCE, "dataset": "XNAS.ITCH",
        "schema": "ohlcv-1m", "native_archive_partitions_verified": len(partitions), "partitions": partitions}
    return frame


def _forecasts(day="2026-09-11", symbol="COST"):
    return pd.DataFrame([{"symbol": symbol, "action_date": day,
                          "target_price_source_contract": XNAS_STOCK_PRICE_SOURCE, "target_price_dataset": "XNAS.ITCH"}])


def _complete(prices=None, forecasts=None, **kwargs):
    return complete_planning_reference_gaps(_prices() if prices is None else prices,
        _forecasts() if forecasts is None else forecasts,
        observed_at=kwargs.pop("observed_at", "2026-09-11T05:30:00Z"), **kwargs)


def test_cost_thirteen_minute_gap_gets_explicit_synthetic_bars_without_mutating_native_history():
    prices = _prices([_bar("2026-09-09 16:59", 900), _bar(), _bar("2026-09-10 16:44", 901)])
    before = prices.copy(deep=True)
    attrs = copy.deepcopy(prices.attrs)
    report = _complete(prices)
    ref = report["references"]["COST|2026-09-11"]
    assert ref["status"] == "AVAILABLE_SYNTHETIC"
    assert ref["price"] == 902.2
    assert ref["observed_at"] == "2026-09-10T23:47:00+00:00"
    assert ref["effective_at"] == "2026-09-11T00:00:00+00:00"
    assert ref["session"] == "2026-09-10"
    assert ref["gap_minutes"] == ref["fill_count"] == 13
    assert ref["source_coverage"]["native_partition_verified"] is True
    assert ref["source_coverage"]["end"] == ref["boundary_at"]
    synthetic = report["synthetic_bars"]
    assert len(synthetic) == 13
    assert synthetic[0]["timestamp"] == "2026-09-10T23:47:00+00:00"
    assert synthetic[-1]["timestamp"] == "2026-09-10T23:59:00+00:00"
    for bar in synthetic:
        assert [bar[field] for field in ("open", "high", "low", "close")] == [902.2] * 4
        assert bar["volume"] == 0
        assert bar["is_synthetic"] is True
        assert bar["reason"] == "ASSUMED_NO_TRADES"
        assert bar["origin_source_contract"] == XNAS_STOCK_PRICE_SOURCE
        assert bar["origin_dataset"] == "XNAS.ITCH"
        assert bar["source_contract"] != XNAS_STOCK_PRICE_SOURCE
        assert bar["origin_bar_start"] == "2026-09-10T23:46:00+00:00"
        assert bar["original_observed_at"] == ref["observed_at"]
    assert report["historical_samples_modified"] is report["native_prices_modified"] is False
    assert_frame_equal(prices, before)
    assert prices.attrs == attrs
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("gap,status,fills", [(0, "AVAILABLE_OBSERVED", 0), (5, "AVAILABLE_OBSERVED", 0),
    (6, "AVAILABLE_SYNTHETIC", 6), (15, "AVAILABLE_SYNTHETIC", 15), (16, "UNAVAILABLE", 0)])
def test_freshness_and_hard_gap_boundaries(gap, status, fills):
    row = _bar("2026-09-10 16:59")
    row["timestamp"] -= pd.Timedelta(minutes=gap)
    report = _complete(_prices([row]))
    ref = report["references"]["COST|2026-09-11"]
    assert ref["status"] == status
    assert ref["fill_count"] == len(report["synthetic_bars"]) == fills
    if status == "UNAVAILABLE":
        assert ref["price"] is ref["effective_at"] is None
        assert ref["reason"] == "GAP_EXCEEDS_MAXIMUM"


@pytest.mark.parametrize("rows,reason", [
    ([_bar("2026-09-09 16:59")], "NO_PRIOR_SESSION_OBSERVATION"),
    ([_bar(symbol="AAPL")], "NO_PRIOR_SESSION_OBSERVATION"),
    ([_bar("2026-09-10 17:00")], "NO_PRIOR_SESSION_OBSERVATION"),
    ([], "NO_PRIOR_SESSION_OBSERVATION"),
])
def test_cannot_borrow_another_session_symbol_or_post_boundary_observation(rows, reason):
    report = _complete(_prices(rows))
    assert report["references"]["COST|2026-09-11"]["reason"] == reason
    assert report["synthetic_bars"] == []


def test_unfinished_boundary_cannot_be_filled_even_when_latest_native_bar_is_complete():
    report = _complete(observed_at="2026-09-10T23:59:59Z")
    ref = report["references"]["COST|2026-09-11"]
    assert ref["status"] == "UNAVAILABLE"
    assert ref["reason"] == "PRIOR_SESSION_BOUNDARY_NOT_COMPLETED"
    assert report["synthetic_bars"] == []


def test_native_historical_date_bounds_mean_utc_calendar_boundaries():
    prices = _prices()
    prices.attrs["stock_price_source"]["partitions"][0].update(start="2026-09-08", end="2026-09-11")
    reference = _complete(prices)["references"]["COST|2026-09-11"]
    assert reference["status"] == "AVAILABLE_SYNTHETIC"
    assert reference["source_coverage"]["start"] == "2026-09-08T00:00:00+00:00"
    assert reference["source_coverage"]["end"] == "2026-09-11T00:00:00+00:00"


def test_future_and_post_boundary_rows_do_not_leak_into_reference():
    baseline = _complete()
    prices = _prices([_bar(), _bar("2026-09-10 17:00", 999), _bar("2026-09-11 16:59", 0.01)])
    assert _complete(prices) == baseline


@pytest.mark.parametrize("damage", ["missing", "not_verified", "wrong_symbol", "truncated", "late_start",
    "future_publication", "premature_publication", "missing_manifest", "naive_interval", "wrong_schema"])
def test_synthetic_fill_requires_completed_verified_native_acquisition_through_boundary(damage):
    prices = _prices()
    source = prices.attrs["stock_price_source"]
    partition = source["partitions"][0]
    if damage == "missing":
        source.pop("partitions")
    elif damage == "not_verified":
        source["native_archive_partitions_verified"] = 0
    elif damage == "wrong_symbol":
        partition["symbol"] = "AAPL"
    elif damage == "truncated":
        partition["end"] = "2026-09-10T23:59:00Z"
    elif damage == "late_start":
        partition["start"] = "2026-09-10T23:47:00Z"
    elif damage == "future_publication":
        partition["published_at"] = "2026-09-11T05:30:01Z"
    elif damage == "premature_publication":
        partition["published_at"] = "2026-09-10T23:59:59Z"
    elif damage == "missing_manifest":
        partition.pop("manifest_path")
    elif damage == "naive_interval":
        partition["end"] = "2026-09-11T00:00:00"
    elif damage == "wrong_schema":
        source["schema"] = "ohlcv-5m"
    report = _complete(prices)
    reference = report["references"]["COST|2026-09-11"]
    assert reference["status"] == "UNAVAILABLE"
    assert reference["reason"] == "UNAVAILABLE_SOURCE_COVERAGE"
    assert reference["price"] is reference["effective_at"] is reference["source_coverage"] is None
    assert report["synthetic_bars"] == []


def test_latest_native_minute_wins_and_never_gets_replaced():
    rows = [_bar(), _bar("2026-09-10 16:50", 903), _bar("2026-09-10 16:50", 903)]
    report = _complete(_prices(rows))
    assert report["references"]["COST|2026-09-11"]["price"] == 903
    assert len(report["synthetic_bars"]) == 9
    native_times = {row["timestamp"].isoformat() for row in rows}
    assert not native_times.intersection(bar["timestamp"] for bar in report["synthetic_bars"])


@pytest.mark.parametrize("day,local,boundary", [
    ("2026-09-08", "2026-09-04 16:46", "2026-09-05T00:00:00+00:00"),
    ("2026-11-03", "2026-11-02 16:46", "2026-11-03T01:00:00+00:00"),
])
def test_exchange_holidays_weekends_and_dst_preserve_exact_prior_session(day, local, boundary):
    report = _complete(_prices([_bar(local)]), _forecasts(day), observed_at=pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=5))
    ref = report["references"][f"COST|{day}"]
    assert ref["status"] == "AVAILABLE_SYNTHETIC"
    assert ref["session"] == local[:10]
    assert ref["effective_at"] == boundary
    assert ref["fill_count"] == 13


@pytest.mark.parametrize("damage", ["conflict", "nan_close", "zero_close", "negative_open", "bool_close",
    "naive_time", "subminute", "missing_time", "bad_high", "bad_low", "negative_volume", "fractional_volume",
    "synthetic", "source", "dataset", "forecast_source"])
def test_malformed_conflicting_or_wrong_source_input_fails_closed(damage):
    prices, forecasts = _prices(), _forecasts()
    if damage == "conflict":
        prices = _prices([_bar(), _bar(price=1)])
    elif damage in ("nan_close", "zero_close", "bool_close"):
        prices["close"] = {"nan_close": np.nan, "zero_close": 0, "bool_close": True}[damage]
    elif damage == "negative_open":
        prices["open"] = -1
    elif damage == "naive_time":
        prices["timestamp"] = pd.to_datetime(["2026-09-10T23:46:00"])
    elif damage == "subminute":
        prices["timestamp"] += pd.Timedelta(seconds=1)
    elif damage == "missing_time":
        prices["timestamp"] = pd.NaT
    elif damage == "bad_high":
        prices["high"] = 800
    elif damage == "bad_low":
        prices["low"] = 1000
    elif damage == "negative_volume":
        prices["volume"] = -1
    elif damage == "fractional_volume":
        prices["volume"] = 1.5
    elif damage == "synthetic":
        prices["is_synthetic"] = True
    elif damage == "source":
        prices.attrs.clear()
    elif damage == "dataset":
        prices["provider_dataset"] = "EQUS.MINI"
    elif damage == "forecast_source":
        forecasts["target_price_dataset"] = "EQUS.MINI"
    with pytest.raises(ValueError):
        _complete(prices, forecasts)


@pytest.mark.parametrize("maximum", [True, 4, 16, 15.5, "15"])
def test_maximum_cannot_relax_the_hard_limit(maximum):
    with pytest.raises(ValueError, match="integer between 5 and 15"):
        _complete(max_gap_minutes=maximum)


def test_configured_limit_can_be_tighter_but_cannot_relabel_a_longer_gap():
    ref = _complete(max_gap_minutes=10)["references"]["COST|2026-09-11"]
    assert ref["status"] == "UNAVAILABLE"
    assert ref["reason"] == "GAP_EXCEEDS_MAXIMUM"
    assert ref["max_gap_minutes"] == 10


def test_duplicate_forecasts_share_one_reference_and_one_fill_sequence():
    forecasts = pd.concat([_forecasts(), _forecasts()], ignore_index=True)
    report = _complete(forecasts=forecasts)
    assert len(report["references"]) == 1
    assert len(report["synthetic_bars"]) == 13


def test_empty_forecasts_are_json_safe_and_do_not_require_price_rows():
    report = _complete(_prices([]), pd.DataFrame())
    assert report["references"] == {}
    assert report["synthetic_bars"] == []
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("symbol,local,gap", [
    ("CROX", "2026-09-11 14:36", 143), ("TWST", "2026-09-11 16:11", 48),
    ("CROX", "2026-09-11 12:59", 240),
])
def test_explicit_after_hours_policy_carries_sparse_same_session_close(symbol, local, gap):
    prices = _prices([_bar(local, 100, symbol=symbol)])
    before = prices.copy(deep=True)
    report = _complete(prices, _forecasts("2026-09-14", symbol),
                       observed_at="2026-09-14T11:00Z", allow_extended_hours=True, historical_sessions=3)
    ref = report["references"][f"{symbol}|2026-09-14"]
    assert ref["status"] == "AVAILABLE_SYNTHETIC"
    assert ref["gap_minutes"] == ref["fill_count"] == gap
    assert ref["max_gap_minutes"] == 240
    assert ref["observed_at"] == (pd.Timestamp(local, tz="America/Los_Angeles") + pd.Timedelta(minutes=1)).tz_convert("UTC").isoformat()
    assert report["historical_references"][f"{symbol}|2026-09-11"] == ref
    assert len(report["synthetic_bars"]) == gap
    assert report["historical_samples_modified"] is True
    assert report["native_prices_modified"] is report["model_training_prices_modified"] is False
    assert_frame_equal(prices, before)
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("damage,reason", [
    ("regular_session", "GAP_EXCEEDS_MAXIMUM"), ("older_session", "NO_PRIOR_SESSION_OBSERVATION"),
    ("incomplete", "UNAVAILABLE_SOURCE_COVERAGE"), ("undefined", "UNDEFINED_NATIVE_PRICE_OBSERVATIONS"),
])
def test_extended_policy_does_not_disguise_missing_acquisitions_or_regular_session_gaps(damage, reason):
    local = {"regular_session": "2026-09-11 12:58", "older_session": "2026-09-10 16:59"}.get(damage, "2026-09-11 14:36")
    prices = _prices([_bar(local, symbol="CROX")])
    if damage == "incomplete":
        prices.attrs["stock_price_source"]["partitions"][0]["end"] = "2026-09-11T23:00Z"
    if damage == "undefined":
        prices.attrs["stock_price_source"]["missing_price_rows_by_symbol"] = {"CROX": 1}
    report = _complete(prices, _forecasts("2026-09-14", "CROX"),
                       observed_at="2026-09-14T11:00Z", allow_extended_hours=True)
    ref = report["references"]["CROX|2026-09-14"]
    assert ref["status"] == "UNAVAILABLE"
    assert ref["reason"] == reason
    assert ref["price"] is None
    assert report["synthetic_bars"] == []


@pytest.mark.parametrize("kwargs", [
    {"allow_extended_hours": "true"}, {"allow_extended_hours": True, "max_gap_minutes": 241},
    {"historical_sessions": 1}, {"allow_extended_hours": True, "historical_sessions": True},
    {"allow_extended_hours": True, "historical_sessions": -1},
])
def test_extended_policy_has_explicit_validated_bounds(kwargs):
    with pytest.raises(ValueError):
        _complete(**kwargs)
