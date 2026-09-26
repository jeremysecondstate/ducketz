"""Offline refresh integration checks for gaps, delayed candles and corrections."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml import hyperliquid_data_pipeline as pipeline


STEP = 900_000
START = 1_700_000_100_000 // STEP * STEP


def candle(index):
    price = 50_000 + index
    return {
        "t": START + index * STEP, "T": START + (index + 1) * STEP - 1,
        "s": "BTC", "i": "15m", "o": str(price - 1), "h": str(price + 5),
        "l": str(price - 5), "c": str(price), "v": str(100 + index % 7), "n": index + 2,
    }


class Exchange:
    def __init__(self, indices):
        self.rows = [candle(i) for i in indices]
        self.requests = []

    def __call__(self, **kwargs):
        self.requests.append(kwargs)
        return [row.copy() for row in self.rows if kwargs["start_ms"] <= row["t"] <= kwargs["end_ms"]]


def run(tmp_path, completed):
    return pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + completed * STEP + 5_000)


def read(result, kind="ohlcv"):
    return pd.read_parquet(result["files"][kind])


def test_refresh_catches_up_multiple_missing_bars_excludes_forming_and_matures_labels(tmp_path, monkeypatch):
    exchange = Exchange(range(100))
    monkeypatch.setattr(pipeline, "fetch_candles", exchange)
    before = run(tmp_path, 100)
    assert read(before, "labels")["target_up_4bar"].iloc[-4:].isna().all()

    exchange.rows = [candle(i) for i in range(97, 106)] + [candle(103)]
    result = run(tmp_path, 105)
    assert result["new_candles"] == result["appended_candles"] == 5
    assert result["corrected_candles"] == result["repaired_candles"] == 0
    assert result["latest_candle_lag_intervals"] == 0
    assert result["expected_latest_close_utc"] == pd.Timestamp(START + 105 * STEP, unit="ms", tz="UTC").isoformat()
    raw = read(result)
    assert len(raw) == 105
    assert raw.timestamp.is_unique and raw.timestamp.is_monotonic_increasing
    assert raw.timestamp.iloc[-1] == pd.Timestamp(START + 104 * STEP, unit="ms", tz="UTC")
    for kind in ("features", "labels"):
        assert read(result, kind).timestamp.equals(raw.timestamp)
    assert read(result, "labels")["target_up_4bar"].iloc[96:100].notna().all()
    assert read(result, "labels")["target_up_4bar"].iloc[-4:].isna().all()
    assert len(read(before)) == 100


def test_delayed_latest_does_not_claim_freshness_and_unchanged_reuses_features(tmp_path, monkeypatch):
    exchange = Exchange(range(100))
    monkeypatch.setattr(pipeline, "fetch_candles", exchange)
    before = run(tmp_path, 100)
    saved_pointer = Path(before["latest_pointer"]).read_bytes()
    feature_builder = pipeline.calculate_features

    def unexpected_features(*args, **kwargs):
        pytest.fail("An unchanged refresh must reuse the completed feature snapshot")

    monkeypatch.setattr(pipeline, "calculate_features", unexpected_features)
    delayed = run(tmp_path, 102)
    assert delayed["status"] == "unchanged"
    assert delayed["latest_candle_lag_intervals"] == 2
    assert delayed["latest_candle_available"] is False
    assert delayed["new_candles"] == delayed["corrected_candles"] == 0
    assert delayed["fetched_at_utc"] != before["fetched_at_utc"]
    assert Path(before["latest_pointer"]).read_bytes() == saved_pointer

    monkeypatch.setattr(pipeline, "calculate_features", feature_builder)
    exchange.rows.extend([candle(100), candle(101)])
    arrived = run(tmp_path, 102)
    assert arrived["status"] == "published"
    assert arrived["new_candles"] == 2
    assert arrived["latest_candle_lag_intervals"] == 0

    monkeypatch.setattr(pipeline, "calculate_features", unexpected_features)
    repeated = run(tmp_path, 102)
    assert repeated["status"] == "unchanged"
    assert repeated["run_id"] == arrived["run_id"]
    assert repeated["new_candles"] == repeated["corrected_candles"] == repeated["repaired_candles"] == 0


def test_empty_incremental_response_preserves_history_and_reports_lag(tmp_path, monkeypatch):
    exchange = Exchange(range(100))
    monkeypatch.setattr(pipeline, "fetch_candles", exchange)
    before = run(tmp_path, 100)
    exchange.rows = []
    result = run(tmp_path, 102)
    assert result["status"] == "unchanged"
    assert result["incoming_closed_candles"] == 0
    assert result["run_id"] == before["run_id"]
    assert result["latest_candle_lag_intervals"] == 2


def test_corrected_overlap_replaces_existing_values_without_counting_as_new(tmp_path, monkeypatch):
    exchange = Exchange(range(100))
    monkeypatch.setattr(pipeline, "fetch_candles", exchange)
    before = run(tmp_path, 100)
    exchange.rows[-2]["c"] = str(float(exchange.rows[-2]["c"]) + 2)
    after = run(tmp_path, 100)
    assert after["status"] == "published"
    assert after["corrected_candles"] == 1
    assert after["new_candles"] == after["appended_candles"] == after["repaired_candles"] == 0
    assert read(after).close.iloc[-2] == read(before).close.iloc[-2] + 2
    assert read(after, "labels")["future_return_1bar"].iloc[-3] != read(before, "labels")["future_return_1bar"].iloc[-3]
    repeated = run(tmp_path, 100)
    assert repeated["status"] == "unchanged"
    assert repeated["corrected_candles"] == 0


def test_internal_gap_requests_earliest_recoverable_missing_candle(tmp_path, monkeypatch):
    exchange = Exchange(i for i in range(100) if i not in (70, 71, 90))
    monkeypatch.setattr(pipeline, "fetch_candles", exchange)
    before = run(tmp_path, 100)
    assert before["recoverable_gap_count"] == 3
    assert before["unrecoverable_gap_count"] == 0
    assert before["latest_candle_lag_intervals"] == 0
    exchange.rows = [candle(i) for i in range(101)]
    after = run(tmp_path, 101)
    assert exchange.requests[-1]["start_ms"] == START + 70 * STEP
    assert after["repaired_candles"] == 3
    assert after["appended_candles"] == 1
    assert after["new_candles"] == 4
    assert after["recoverable_gap_count"] == after["unrecoverable_gap_count"] == 0
    assert after["gap_count"] == 0
    assert len(read(after)) == 101


def test_long_downtime_retains_old_history_clamps_request_and_reports_retention_gap(tmp_path, monkeypatch):
    exchange = Exchange(range(100))
    monkeypatch.setattr(pipeline, "fetch_candles", exchange)
    before = run(tmp_path, 100)
    # The endpoint cannot recover the 5,200 missing bars preceding its newest 5,000.
    exchange.rows = [candle(i) for i in range(5_300, 10_301)]
    after = run(tmp_path, 10_300)
    assert exchange.requests[-1]["start_ms"] == START + 5_300 * STEP
    assert after["rows"] == 5_100
    assert after["new_candles"] == 5_000
    assert after["missing_candle_count"] == after["unrecoverable_gap_count"] == 5_200
    assert after["recoverable_gap_count"] == 0
    assert after["api_window_exhausted"] is True
    assert after["latest_candle_lag_intervals"] == 0
    pd.testing.assert_frame_equal(read(before), read(after).iloc[:100].reset_index(drop=True))
    assert read(after, "features")["log_return_1"].iloc[100:101].isna().all()
    repeated = run(tmp_path, 10_300)
    assert repeated["status"] == "unchanged"
    assert exchange.requests[-1]["start_ms"] == START + 10_297 * STEP
    assert repeated["unrecoverable_gap_count"] == 5_200
    assert json.loads(Path(after["latest_pointer"]).read_text())["run_id"] == after["run_id"]


def test_empty_long_downtime_response_reports_unrecoverable_tail(tmp_path, monkeypatch):
    exchange = Exchange(range(100))
    monkeypatch.setattr(pipeline, "fetch_candles", exchange)
    run(tmp_path, 100)
    exchange.rows = []
    after = run(tmp_path, 10_300)
    assert after["status"] == "unchanged"
    assert after["latest_candle_lag_intervals"] == 10_200
    assert after["unrecoverable_trailing_candles"] == 5_200
    assert after["api_window_exhausted"] is True
