from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml import hyperliquid_data_pipeline as pipeline
from datafetching.hyperliquid_candles import normalize_candles


STEP = 900_000
START = 1_700_000_100_000 // STEP * STEP


def candles(count=100):
    output = []
    for i in range(count):
        value = 50_000 + 7 * i + 60 * np.sin(i / 3)
        output.append({
            "t": START + i * STEP, "T": START + (i + 1) * STEP - 1,
            "s": "BTC", "i": "15m", "o": str(value - 2),
            "h": str(value + 8), "l": str(value - 9), "c": str(value),
            "v": str(100 + i % 17), "n": 50 + i,
        })
    return output


def normalize(rows):
    return normalize_candles(rows, coin="BTC", interval="15m", as_of_ms=START + 200 * STEP)


def test_labels_use_exact_horizon_and_unknown_tail_stays_nullable():
    bars = normalize(candles(6))
    labels = pipeline.build_labels(bars, interval="15m", horizons=(1, 4))
    assert str(labels["target_up_4bar"].dtype) == "Int8"
    assert labels["target_up_4bar"].iloc[-4:].isna().all()
    assert labels["future_return_4bar"].iloc[-4:].isna().all()
    assert labels["future_return_4bar"].iloc[0] == pytest.approx(bars.close.iloc[4] / bars.close.iloc[0] - 1)
    with_gap = bars.drop(index=2).reset_index(drop=True)
    gap_labels = pipeline.build_labels(with_gap, interval="15m", horizons=(1,))
    assert pd.isna(gap_labels["target_up_1bar"].iloc[1])
    assert gap_labels["target_up_1bar"].iloc[2] == int(with_gap.close.iloc[3] > with_gap.close.iloc[2])


def test_gap_resets_feature_lookbacks():
    bars = normalize(candles(100)).drop(index=80).reset_index(drop=True)
    frame, _, diagnostics = pipeline.calculate_features(bars, interval="15m")
    assert diagnostics["contiguous_segments"] == 2
    assert pd.isna(frame.loc[80, "log_return_1"])
    assert pd.isna(frame.loc[80, "ema_close_20"])
    assert frame.loc[79, "ema_close_20"] > 0


def test_live_style_run_publishes_roundtrip_and_refresh_reuses_unchanged_data(tmp_path, monkeypatch):
    rows = candles(100)
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return rows

    monkeypatch.setattr(pipeline, "fetch_candles", fetch)
    result = pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 100 * STEP)
    assert result["status"] == "published"
    assert result["rows"] == 100
    assert result["new_candles"] == 100
    raw = pd.read_parquet(result["files"]["ohlcv"])
    featured = pd.read_parquet(result["files"]["features"])
    labels = pd.read_parquet(result["files"]["labels"])
    assert raw.timestamp.equals(featured.timestamp)
    assert raw.timestamp.equals(labels.timestamp)
    assert not any(name.startswith(("target_", "future_")) for name in featured.columns)
    assert labels["target_up_16bar"].iloc[-16:].isna().all()
    assert featured.columns.is_unique
    assert len(featured) == 100
    assert result["rows_with_all_features"] > 0
    pointer = json.loads(Path(result["latest_pointer"]).read_text())
    assert pointer["run_id"] == result["run_id"]
    repeated = pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 100 * STEP)
    assert repeated["status"] == "unchanged"
    assert repeated["run_id"] == result["run_id"]
    assert calls[1]["start_ms"] == START + 97 * STEP
    assert len(list((tmp_path / "BTC" / "15m" / "runs").iterdir())) == 1


def test_refresh_merges_new_and_corrected_rows_and_keeps_old_snapshot(tmp_path, monkeypatch):
    rows = candles(100)
    monkeypatch.setattr(pipeline, "fetch_candles", lambda **kwargs: rows)
    first = pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 100 * STEP)
    first_raw = pd.read_parquet(first["files"]["ohlcv"])
    rows = candles(102)[97:]
    rows[1]["c"] = str(float(rows[1]["c"]) + 1)
    result = pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 102 * STEP)
    assert result["new_candles"] == 2
    assert result["rows"] == 102
    merged = pd.read_parquet(result["files"]["ohlcv"])
    assert merged.close.iloc[98] == pytest.approx(first_raw.close.iloc[98] + 1)
    pd.testing.assert_frame_equal(first_raw, pd.read_parquet(first["files"]["ohlcv"]))
    assert merged.timestamp.is_unique


def test_calculation_failure_keeps_previous_snapshot_current(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_candles", lambda **kwargs: candles(100))
    first = pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 100 * STEP)

    def fail(*args, **kwargs):
        raise RuntimeError("deliberate calculation failure")

    monkeypatch.setattr(pipeline, "calculate_features", fail)
    with pytest.raises(RuntimeError, match="deliberate"):
        pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 100 * STEP, rebuild=True)
    assert json.loads(Path(first["latest_pointer"]).read_text())["run_id"] == first["run_id"]
    assert Path(first["files"]["features"]).is_file()


def test_endpoint_change_does_not_mix_histories(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_candles", lambda **kwargs: candles(100))
    pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 100 * STEP)
    with pytest.raises(ValueError, match="different market or API endpoint"):
        pipeline.run_pipeline(output_root=tmp_path, info_url="https://api.hyperliquid-testnet.xyz/info")


def test_earlier_cutoff_does_not_republish_future_archive_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_candles", lambda **kwargs: candles(100))
    first = pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 100 * STEP)
    with pytest.raises(ValueError, match="cutoff precedes"):
        pipeline.run_pipeline(output_root=tmp_path, as_of_ms=START + 99 * STEP, rebuild=True)
    assert json.loads(Path(first["latest_pointer"]).read_text())["run_id"] == first["run_id"]


def test_benchmark_verifies_equivalent_outputs():
    bars = normalize(candles(100))
    frame, _, _ = pipeline.calculate_features(bars, interval="15m")
    result = pipeline._benchmark(bars, interval="15m", reference=frame)
    assert result["outputs_match"]
    assert len(result["samples_seconds"]["cached"]) == 3
    assert result["legacy_50_indicator_pipeline_comparison"] is False


@pytest.mark.parametrize("coin", ["../BTC", "BTC/USDC", "", "BTC:other"])
def test_symbol_paths_cannot_escape_output_root(tmp_path, coin):
    with pytest.raises(ValueError, match="symbol"):
        pipeline.run_pipeline(coin=coin, output_root=tmp_path)
    assert not list(tmp_path.iterdir())
