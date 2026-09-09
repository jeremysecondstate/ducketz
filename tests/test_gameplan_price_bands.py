from __future__ import annotations

import json
from datetime import date

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from ml.gameplan_price_bands import build_entry_price_bands
from ml.independent_stock_targets import stock_target_windows
from ml.stock_target_prices import XNAS_STOCK_PRICE_SOURCE


def _forecast(day="2026-09-09", route="1h@04:00", symbol="COST"):
    window = next(row for row in stock_target_windows(date.fromisoformat(day)) if row["route"] == route)
    return {**window, "forecast_id": f"{symbol}-{day}-{route}", "symbol": symbol, "action_date": day,
            "target_price_source_contract": XNAS_STOCK_PRICE_SOURCE, "target_price_dataset": "XNAS.ITCH"}


def _bar(local, price, *, symbol="COST", close=None):
    return {"symbol": symbol, "timestamp": pd.Timestamp(local, tz="America/Los_Angeles").tz_convert("UTC"),
            "open": price, "close": price if close is None else close}


def _prices(rows):
    frame = pd.DataFrame(rows, columns=["symbol", "timestamp", "open", "close"])
    frame.attrs["stock_price_source"] = {"source_contract": XNAS_STOCK_PRICE_SOURCE, "dataset": "XNAS.ITCH"}
    return frame


def _history():
    return _prices([
        _bar("2026-09-02 16:59", 100),
        _bar("2026-09-03 04:00", 95), _bar("2026-09-03 16:59", 100),
        _bar("2026-09-04 04:00", 105), _bar("2026-09-04 16:59", 100),
        _bar("2026-09-08 04:00", 110), _bar("2026-09-08 16:59", 200),
    ])


def _build(prices=None, forecasts=None, **kwargs):
    return build_entry_price_bands(_history() if prices is None else prices,
        pd.DataFrame([_forecast()]) if forecasts is None else forecasts,
        observed_at=kwargs.pop("observed_at", "2026-09-09T04:00:00Z"),
        lookback_sessions=kwargs.pop("lookback_sessions", 3), minimum_samples=kwargs.pop("minimum_samples", 3),
        **kwargs)


def test_band_uses_actual_prior_close_entry_ratios_and_skips_exchange_holiday():
    report = _build()
    row = report["rows"][0]
    stats = next(iter(report["statistics"].values()))
    assert row["forecast_id"] == "COST-2026-09-09-1h@04:00"
    assert row["price_band_status"] == "AVAILABLE"
    assert row["price_band_sample_count"] == row["price_band_candidate_sessions"] == 3
    assert row["price_band_coverage"] == 1
    assert row["price_reference"] == 200
    assert row["price_reference_session"] == "2026-09-08"
    assert row["price_reference_observed_at"] == "2026-09-09T00:00:00+00:00"
    low, high = np.quantile([0.95, 1.05, 1.1], [0.05, 0.95]) * 200
    assert row["trade_price_low"] == pytest.approx(np.floor(low * 100) / 100)
    assert row["trade_price_high"] == pytest.approx(np.ceil(high * 100) / 100)
    assert stats["long_gap_sample_count"] == 1
    assert stats["samples"][-1]["prior_session"] == "2026-09-04"
    assert stats["samples"][-1]["session"] == "2026-09-08"
    assert stats["samples"][-1]["calendar_gap_days"] == 4
    json.dumps(report, allow_nan=False)


def test_daily_weekly_share_opening_band_but_each_entry_clock_uses_its_own_history():
    prices = _history()
    extra = _prices([_bar(f"2026-09-{day} 08:00", price) for day, price in (("03", 99), ("04", 100), ("08", 101))])
    prices = _prices(pd.concat([prices, extra]).to_dict("records"))
    forecasts = pd.DataFrame([_forecast(route=route) for route in
        ("1h@04:00", "4h@04:00", "1d@D+1", "1w@D+5", "4h@08:00", "1h@gap", "1d@D+2")])
    report = _build(prices, forecasts)
    assert len(report["rows"]) == len(forecasts)
    assert len(report["statistics"]) == 2
    assert {row["trade_price_low"] for row in report["rows"][:4]} == {report["rows"][0]["trade_price_low"]}
    assert report["rows"][4]["trade_price_low"] != report["rows"][0]["trade_price_low"]
    assert [row["price_band_status"] for row in report["rows"][-2:]] == ["NOT_ENTRY", "NOT_ENTRY"]
    assert all(row["trade_price_low"] is None for row in report["rows"][-2:])


@pytest.mark.parametrize("close_offset,entry_offset,count", [(-5, 5, 3), (-6, 0, 2), (0, 6, 2)])
def test_endpoint_tolerance_is_causal_and_close_timestamp_is_minute_completion(close_offset, entry_offset, count):
    prices = _history()
    prior = prices.timestamp.eq(pd.Timestamp("2026-09-05T00:00Z") - pd.Timedelta(minutes=1))
    entry = prices.timestamp.eq(pd.Timestamp("2026-09-08T11:00Z"))
    prices.loc[prior, "timestamp"] += pd.Timedelta(minutes=close_offset)
    prices.loc[entry, "timestamp"] += pd.Timedelta(minutes=entry_offset)
    report = _build(prices)
    row = report["rows"][0]
    assert row["price_band_sample_count"] == count
    assert row["price_band_status"] == ("AVAILABLE" if count == 3 else "UNAVAILABLE_MINIMUM_SAMPLES")
    assert (row["trade_price_low"] is not None) == (count == 3)


def test_outside_window_prices_cannot_replace_missing_exact_endpoint():
    prices = _history()
    prior = prices.timestamp.eq(pd.Timestamp("2026-09-05T00:00Z") - pd.Timedelta(minutes=1))
    entry = prices.timestamp.eq(pd.Timestamp("2026-09-08T11:00Z"))
    prices.loc[prior, "timestamp"] += pd.Timedelta(minutes=1)
    prices.loc[entry, "timestamp"] -= pd.Timedelta(minutes=1)
    row = _build(prices)["rows"][0]
    assert row["price_band_sample_count"] == 2
    assert row["price_band_status"] == "UNAVAILABLE_MINIMUM_SAMPLES"


def test_reference_cannot_use_older_close_or_after_17_observation():
    prices = _history()
    prices.loc[prices.timestamp.eq(pd.Timestamp("2026-09-09T00:00Z") - pd.Timedelta(minutes=1)), "timestamp"] += pd.Timedelta(minutes=1)
    row = _build(prices)["rows"][0]
    assert row["price_band_sample_count"] == 3
    assert row["price_band_status"] == "UNAVAILABLE_REFERENCE_PRICE"
    assert row["price_reference"] is row["trade_price_low"] is row["trade_price_high"] is None


def test_future_rows_and_forecast_action_outcomes_cannot_change_bands():
    prices = _history()
    baseline = _build(prices)
    extra = [_bar("2026-09-09 04:00", 1_000_000), _bar("2026-09-09 16:59", 0.001),
             _bar("2026-09-10 04:00", 0.001), _bar("2026-09-10 16:59", 1_000_000)]
    enriched = _prices(prices.to_dict("records") + extra)
    assert _build(enriched) == baseline
    # Even a later observation time cannot train on the forecast's own action date.
    later = _build(enriched, observed_at="2026-09-11T04:00Z")
    assert later["rows"] == baseline["rows"]
    assert later["statistics"] == baseline["statistics"]


def test_unfinished_bar_is_unavailable_even_when_its_open_timestamp_has_arrived():
    prices = _history()
    row = _build(prices, observed_at="2026-09-08T11:00:30Z")["rows"][0]
    assert row["price_band_sample_count"] == 2
    assert row["price_band_status"] == "UNAVAILABLE_REFERENCE_PRICE"


def test_lookback_is_exchange_sessions_and_old_prices_do_not_supply_minimum():
    row = _build(lookback_sessions=2, minimum_samples=2)["rows"][0]
    assert row["price_band_history_first_session"] == "2026-09-04"
    assert row["price_band_sample_count"] == 2
    missing = _history()
    missing = missing.loc[missing.timestamp.ne(pd.Timestamp("2026-09-08T11:00Z"))]
    row = _build(missing, lookback_sessions=2, minimum_samples=2)["rows"][0]
    assert row["price_band_sample_count"] == 1
    assert row["price_band_coverage"] == 0.5
    assert row["price_band_status"] == "UNAVAILABLE_MINIMUM_SAMPLES"


def test_symbol_history_is_never_borrowed_and_research_status_does_not_remove_range():
    forecasts = pd.DataFrame([_forecast(), _forecast(symbol="AAPL")])
    forecasts["promotion_status"] = "RESEARCH_NO_TARGET_HISTORY"
    rows = _build(forecasts=forecasts)["rows"]
    assert rows[0]["price_band_status"] == "AVAILABLE"
    assert rows[1]["price_band_status"] == "UNAVAILABLE_REFERENCE_PRICE"
    assert rows[1]["price_band_sample_count"] == 0
    assert rows[1]["trade_price_low"] is None
    assert forecasts.promotion_status.eq("RESEARCH_NO_TARGET_HISTORY").all()


def test_dst_uses_pacific_close_and_next_session_clock():
    prices = _prices([_bar("2026-10-29 16:59", 100), _bar("2026-10-30 04:00", 100),
                      _bar("2026-10-30 16:59", 100), _bar("2026-11-02 04:00", 110),
                      _bar("2026-11-02 16:59", 200)])
    report = _build(prices, pd.DataFrame([_forecast("2026-11-03")]),
                    observed_at="2026-11-03T05:00Z", lookback_sessions=2, minimum_samples=2)
    stats = next(iter(report["statistics"].values()))
    assert stats["reference_observed_at"] == "2026-11-03T01:00:00+00:00"
    assert stats["samples"][-1]["prior_close_observed_at"] == "2026-10-31T00:00:00+00:00"
    assert stats["samples"][-1]["entry_open_observed_at"] == "2026-11-02T12:00:00+00:00"


@pytest.mark.parametrize("damage", ["missing_source", "wrong_dataset", "wrong_forecast", "conflicting"])
def test_source_identity_and_conflicting_minutes_fail_closed(damage):
    prices, forecasts = _history(), pd.DataFrame([_forecast()])
    if damage == "missing_source":
        prices.attrs.clear()
    elif damage == "wrong_dataset":
        prices["provider_dataset"] = "EQUS.MINI"
    elif damage == "wrong_forecast":
        forecasts["target_price_source_contract"] = "canonical-equity-minute-v1"
        forecasts["target_price_dataset"] = "EQUS.MINI"
    else:
        duplicate = prices.iloc[:1].copy()
        duplicate["close"] += 1
        prices = _prices(pd.concat([prices, duplicate]).to_dict("records"))
    with pytest.raises(ValueError):
        _build(prices, forecasts)


def test_invalid_numeric_prices_remain_missing_not_filled():
    prices = _history()
    prices.loc[prices.timestamp.eq(pd.Timestamp("2026-09-08T11:00Z")), "open"] = float("nan")
    report = _build(prices)
    assert report["rows"][0]["price_band_sample_count"] == 2
    json.dumps(report, allow_nan=False)


def test_defaults_use_available_history_without_a_training_sample_gate_and_preserve_inputs():
    prices, forecasts = _history(), pd.DataFrame([_forecast()])
    original_prices, original_forecasts = prices.copy(deep=True), forecasts.copy(deep=True)
    report = build_entry_price_bands(prices, forecasts, observed_at="2026-09-09T04:00Z")
    assert report["lookback_sessions"] == 120
    assert report["minimum_samples"] == 2
    assert report["rows"][0]["price_band_candidate_sessions"] == 120
    assert report["rows"][0]["price_band_status"] == "AVAILABLE"
    pd.testing.assert_frame_equal(prices, original_prices)
    pd.testing.assert_frame_equal(forecasts, original_forecasts)


def test_naive_planning_time_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        _build(observed_at="2026-09-09T04:00")


def test_native_forecast_ids_are_preserved_in_order():
    forecasts = pd.DataFrame([_forecast(), _forecast(route="4h@04:00")]).rename(columns={"forecast_id": "id"})
    rows = _build(forecasts=forecasts)["rows"]
    assert [row["forecast_id"] for row in rows] == list(forecasts.id)


def test_twenty_seven_observed_pairs_produce_a_price_range_by_default():
    calendar = xcals.get_calendar("XNYS", start="2026-07-01", end="2026-09-10")
    days = calendar.sessions_in_range("2026-07-01", "2026-09-08")[-28:]
    bars = [_bar(f"{day.date()} 16:59", 100) for day in days]
    bars.extend(_bar(f"{day.date()} 04:00", 100 + index / 10) for index, day in enumerate(days[1:]))
    report = build_entry_price_bands(_prices(bars), pd.DataFrame([_forecast()]), observed_at="2026-09-09T04:00Z")
    row = report["rows"][0]
    assert row["price_band_sample_count"] == 27
    assert row["price_band_status"] == "AVAILABLE"
    # Outward cent rounding may conservatively include one extra cent when
    # a binary floating-point percentile lies immediately below/above a cent.
    assert 0 <= 100.13 - row["trade_price_low"] <= 0.01001
    assert 0 <= row["trade_price_high"] - 102.47 <= 0.01001


@pytest.mark.parametrize("pairs", [0, 1])
def test_zero_or_one_pair_remains_unavailable_with_a_specific_reason(pairs):
    bars = [_bar("2026-09-08 16:59", 100)]
    if pairs:
        bars.extend([_bar("2026-09-04 16:59", 100), _bar("2026-09-08 04:00", 101)])
    report = build_entry_price_bands(_prices(bars), pd.DataFrame([_forecast()]), observed_at="2026-09-09T04:00Z")
    row = report["rows"][0]
    assert row["price_band_sample_count"] == pairs
    assert row["price_band_status"] == "UNAVAILABLE_MINIMUM_SAMPLES"
    assert row["trade_price_low"] is row["trade_price_high"] is None
    assert ("No observed" if pairs == 0 else "Only one observed") in row["price_band_reason"]
