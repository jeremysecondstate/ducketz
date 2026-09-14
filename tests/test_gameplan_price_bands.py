from __future__ import annotations

import json
from datetime import date

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from ml.gameplan_price_bands import build_entry_price_bands, build_planning_price_path
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


def _short_closing_gap(minutes=13):
    prices = _history()
    prices = _prices(prices.to_dict("records") + [
        _bar(f"2026-09-{day} {hour:02d}:00", 100)
        for day in ("03", "04", "08") for hour in range(5, 17)])
    last_start = pd.Timestamp("2026-09-08T23:59Z")
    prices.loc[prices.timestamp.eq(last_start), "timestamp"] -= pd.Timedelta(minutes=minutes)
    prices.attrs["stock_price_source"].update(schema="ohlcv-1m", native_archive_partitions_verified=1, partitions=[{
        "symbol": "COST", "start": "2026-09-08T00:00:00+00:00", "end": "2026-09-09T00:00:00+00:00",
        "published_at": "2026-09-09T01:00:00+00:00", "manifest_path": "verified-test-manifest.json"}])
    return prices


def test_bounded_reference_completion_unblocks_all_clocks_without_filling_observed_samples():
    from ml.gameplan_price_bands import _observation
    prices, forecasts = _short_closing_gap(), pd.DataFrame([_forecast()])
    original = prices.copy(deep=True)
    strict = _build(prices, forecasts, minimum_samples=2)
    bands = _build(prices, forecasts, minimum_samples=2, allow_reference_forward_fill=True)
    path = build_planning_price_path(prices, forecasts, observed_at=bands["observed_at"],
                                     entry_bands=bands, allow_reference_forward_fill=True)
    assert strict["rows"][0]["price_band_status"] == "UNAVAILABLE_REFERENCE_PRICE"
    assert bands["rows"][0]["price_band_status"] == "AVAILABLE"
    assert bands["rows"][0]["price_reference_is_synthetic"] is True
    assert bands["rows"][0]["price_reference_observed_at"] == "2026-09-08T23:47:00+00:00"
    assert bands["rows"][0]["price_reference_effective_at"] == "2026-09-09T00:00:00+00:00"
    assert next(iter(bands["statistics"].values()))["samples"] == next(iter(strict["statistics"].values()))["samples"]
    assert bands["reference_completion"] == path["reference_completion"]
    assert len(path["points"]) == 14
    assert all(point["status"] == "AVAILABLE" and point["reference_price"] == 200
               and point["reference_fill_count"] == 13 and point["reference_is_synthetic"]
               for point in path["points"].values())
    assert len(bands["reference_completion"]["synthetic_bars"]) == 13
    assert _observation(prices.sort_values("timestamp"), pd.Timestamp("2026-09-09T00:00Z"), close=True) is None
    pd.testing.assert_frame_equal(prices, original)
    json.dumps(path, allow_nan=False)


@pytest.mark.parametrize("gap,status", [(15, "AVAILABLE"), (16, "UNAVAILABLE_REFERENCE_PRICE")])
def test_explicit_reference_completion_is_bounded_at_fifteen_minutes(gap, status):
    assert _build(_short_closing_gap(gap), allow_reference_forward_fill=True)["rows"][0]["price_band_status"] == status


def test_reused_bands_cannot_change_completion_policy_or_inject_a_synthetic_reference():
    prices, forecasts = _short_closing_gap(), pd.DataFrame([_forecast()])
    bands = _build(prices, forecasts, allow_reference_forward_fill=True)
    with pytest.raises(ValueError, match="exact source"):
        build_planning_price_path(prices, forecasts, observed_at=bands["observed_at"], entry_bands=bands)
    bands["reference_completion"]["references"]["COST|2026-09-09"]["price"] = 99999
    with pytest.raises(ValueError, match="exact source"):
        build_planning_price_path(prices, forecasts, observed_at=bands["observed_at"], entry_bands=bands,
                                  allow_reference_forward_fill=True)


def _sparse_closing_history():
    prices = _short_closing_gap(48)
    local = prices.timestamp.dt.tz_convert("America/Los_Angeles")
    prices.loc[local.dt.hour.eq(16) & local.dt.minute.eq(59), "timestamp"] -= pd.Timedelta(minutes=48)
    prices.attrs["stock_price_source"]["partitions"][0]["start"] = "2026-09-01T00:00:00Z"
    return prices


def test_sparse_closing_policy_restores_planning_pairs_with_full_provenance_and_native_entries():
    prices = _sparse_closing_history()
    before = prices.copy(deep=True)
    forecasts = pd.DataFrame([_forecast()])
    legacy = _build(prices, forecasts, allow_reference_forward_fill=True)
    assert legacy["rows"][0]["price_band_status"] == "UNAVAILABLE_REFERENCE_PRICE"
    bands = _build(prices, forecasts, allow_reference_forward_fill=True, allow_sparse_session_references=True)
    path = build_planning_price_path(prices, forecasts, observed_at=bands["observed_at"], entry_bands=bands,
                                     allow_reference_forward_fill=True, allow_sparse_session_references=True)
    assert len(path["points"]) == 14
    assert all(p["status"] == "AVAILABLE" and p["sample_count"] == 3 for p in path["points"].values())
    point = path["points"]["COST|2026-09-09|04:00"]
    assert point["synthetic_close_sample_count"] == 3
    assert point["observed_only_sample_count"] == 0
    assert point["reference_gap_minutes"] == 48
    assert point["ratio_median"] == 1.05
    assert point["planned_price_mid"] == 210
    for sample in point["samples"]:
        ref = bands["reference_completion"]["historical_references"][sample["prior_close_reference_key"]]
        assert ref["source_coverage"]["native_partition_verified"] is True
        assert sample["prior_close_is_synthetic"] is True
        assert sample["prior_close_observed_at"] == ref["observed_at"]
        assert sample["prior_close_effective_at"] == ref["effective_at"]
        assert pd.Timestamp(sample["entry_open_observed_at"]).tz_convert("America/Los_Angeles").hour == 4
    assert path["points"]["COST|2026-09-09|17:00"]["samples"][-1]["endpoint_is_synthetic"] is True
    pd.testing.assert_frame_equal(prices, before)
    json.dumps(path, allow_nan=False)


def test_sparse_policy_keeps_entry_gaps_missing_and_ignores_forecast_day_outcomes():
    prices = _sparse_closing_history()
    local = prices.timestamp.dt.tz_convert("America/Los_Angeles")
    prices = prices.loc[~local.dt.hour.eq(4)].copy()
    bands = _build(prices, allow_reference_forward_fill=True, allow_sparse_session_references=True)
    assert bands["rows"][0]["price_band_status"] == "UNAVAILABLE_MINIMUM_SAMPLES"
    assert bands["rows"][0]["price_band_sample_count"] == 0
    # Today's and later outcomes must not change the historical planning path.
    original = _sparse_closing_history()
    baseline = _build(original, allow_reference_forward_fill=True, allow_sparse_session_references=True)
    poisoned = pd.concat([original, _prices([_bar("2026-09-09 04:00", 99999), _bar("2026-09-09 16:59", 1)])], ignore_index=True)
    poisoned.attrs = original.attrs.copy()
    changed = _build(poisoned, allow_reference_forward_fill=True, allow_sparse_session_references=True,
                     observed_at="2026-09-10T04:00Z")
    assert changed["statistics"] == baseline["statistics"]


def test_sparse_policy_reuse_cannot_inject_historical_closes_or_switch_policies():
    prices = _sparse_closing_history()
    bands = _build(prices, allow_reference_forward_fill=True, allow_sparse_session_references=True)
    forecasts = pd.DataFrame([_forecast()])
    with pytest.raises(ValueError, match="exact source"):
        build_planning_price_path(prices, forecasts, observed_at=bands["observed_at"], entry_bands=bands,
                                  allow_reference_forward_fill=True)
    bands["reference_completion"]["historical_references"]["COST|2026-09-04"]["price"] = 1
    with pytest.raises(ValueError, match="exact source"):
        build_planning_price_path(prices, forecasts, observed_at=bands["observed_at"], entry_bands=bands,
                                  allow_reference_forward_fill=True, allow_sparse_session_references=True)


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


def test_conditional_working_path_uses_median_and_keeps_historical_stress_range():
    forecasts = pd.DataFrame([_forecast()])
    entry_bands = _build(forecasts=forecasts)
    report = build_planning_price_path(_history(), forecasts, observed_at="2026-09-09T04:00Z", entry_bands=entry_bands)
    assert len(report["points"]) == 14
    assert {point["clock_local"] for point in report["points"].values()} == {f"{hour:02d}:00" for hour in range(4, 18)}
    point = report["points"]["COST|2026-09-09|04:00"]
    assert point["status"] == "AVAILABLE"
    assert point["planned_price_low"] == 209.58
    assert point["planned_price_mid"] == 210
    assert point["planned_price_high"] == 210.42
    assert point["ratio_median"] == pytest.approx(1.05)
    assert point["historical_price_low"] == entry_bands["rows"][0]["trade_price_low"]
    assert point["historical_price_high"] == entry_bands["rows"][0]["trade_price_high"]
    assert point["planned_price_low"] > point["historical_price_low"]
    assert point["planned_price_high"] < point["historical_price_high"]
    assert "not a future confidence interval" in report["working_range_semantics"]
    json.dumps(report, allow_nan=False)


def test_final_clock_uses_minute_completed_close_and_ignores_after_close_prices():
    prices = _history()
    extras = [_bar(f"2026-09-{day} 17:00", 9000) for day in ("02", "03", "04", "08")]
    poisoned = _prices(prices.to_dict("records") + extras)
    report = build_planning_price_path(poisoned, pd.DataFrame([_forecast()]), observed_at="2026-09-09T04:00Z")
    point = report["points"]["COST|2026-09-09|17:00"]
    assert point["endpoint_kind"] == "observed_close"
    assert point["timestamp"] == "2026-09-10T00:00:00+00:00"
    assert point["sample_count"] == 3
    assert [sample["endpoint_price"] for sample in point["samples"]] == [100, 100, 200]
    assert point["planned_price_mid"] == 200
    assert point["samples"][-1]["endpoint_observed_at"] == "2026-09-09T00:00:00+00:00"
    assert point["samples"][-1]["prior_session"] == "2026-09-04"


@pytest.mark.parametrize("pairs", [0, 1])
def test_working_path_does_not_invent_missing_or_single_observation_prices(pairs):
    bars = [_bar("2026-09-08 16:59", 100)]
    if pairs:
        bars.extend([_bar("2026-09-04 16:59", 100), _bar("2026-09-08 04:00", 101)])
    report = build_planning_price_path(_prices(bars), pd.DataFrame([_forecast()]), observed_at="2026-09-09T04:00Z")
    for clock in ("04:00", "17:00"):
        point = report["points"][f"COST|2026-09-09|{clock}"]
        assert point["sample_count"] == pairs
        assert point["status"] == "UNAVAILABLE_MINIMUM_SAMPLES"
        assert point["planned_price_low"] is point["planned_price_mid"] is point["planned_price_high"] is None


def test_working_path_reuses_complete_entry_statistics_without_rebuilding_them(monkeypatch):
    from ml import gameplan_price_bands
    forecasts = pd.DataFrame([_forecast(route=row["route"]) for row in stock_target_windows(date(2026, 9, 9))])
    bands = _build(forecasts=forecasts)
    def unexpected(*args, **kwargs):
        raise AssertionError("Complete in-memory entry statistics should be reused")
    monkeypatch.setattr(gameplan_price_bands, "build_entry_price_bands", unexpected)
    report = build_planning_price_path(_history(), forecasts, observed_at=bands["observed_at"], entry_bands=bands)
    assert len(report["points"]) == 14
    assert report["points"]["COST|2026-09-09|04:00"]["samples"] == bands["statistics"]["COST|2026-09-09|04:00"]["samples"]


def test_working_path_cannot_reuse_different_source_or_asof_statistics():
    bands = _build()
    with pytest.raises(ValueError, match="source and observation time"):
        build_planning_price_path(_history(), pd.DataFrame([_forecast()]), observed_at="2026-09-09T05:00Z", entry_bands=bands)
    bands["price_dataset"] = "EQUS.MINI"
    with pytest.raises(ValueError, match="source and observation time"):
        build_planning_price_path(_history(), pd.DataFrame([_forecast()]), observed_at=bands["observed_at"], entry_bands=bands)


@pytest.mark.parametrize("width", [0, -1, 10000, float("nan"), float("inf"), True])
def test_working_path_rejects_invalid_price_allowance(width):
    with pytest.raises(ValueError, match="half-width"):
        build_planning_price_path(_history(), pd.DataFrame([_forecast()]), observed_at="2026-09-09T04:00Z", working_half_width_bps=width)


def test_working_path_never_consumes_forecast_action_day_outcomes():
    forecasts = pd.DataFrame([_forecast()])
    baseline = build_planning_price_path(_history(), forecasts, observed_at="2026-09-09T04:00Z")
    prices = _prices(_history().to_dict("records") + [_bar("2026-09-09 04:00", 99999), _bar("2026-09-09 16:59", 0.01)])
    changed = build_planning_price_path(prices, forecasts, observed_at="2026-09-11T04:00Z")
    assert changed["points"] == baseline["points"]
