"""Build public Hyperliquid OHLCV, causal features, and separate research labels.

Run with ``python -m ml.hyperliquid_data_pipeline --benchmark``. No account,
signing, execution, or model-training APIs are used by this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from filelock import FileLock
import numpy as np
import pandas as pd

from datafetching.hyperliquid_candles import (
    DEFAULT_INFO_URL,
    INTERVAL_MS,
    fetch_candles,
    merge_candles,
    normalize_candles,
)
from technicals.hyperliquid_features import FEATURE_SCHEMA_VERSION, build_features


DEFAULT_OUTPUT_ROOT = Path("C:/DATASTORE/hyperliquid")
FEATURE_REVISION = FEATURE_SCHEMA_VERSION
DEFAULT_HORIZONS = (1, 4, 16)
API_HISTORY_LIMIT_CANDLES = 5000


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (Path, pd.Timestamp, datetime)):
        return str(value)
    return value


def build_labels(bars: pd.DataFrame, *, interval: str, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """Exact-time forward labels; missing future candles remain genuinely unknown."""
    labels = bars[["timestamp", "symbol", "interval"]].copy()
    close_by_time = bars.set_index("timestamp")["close"]
    for horizon in horizons:
        if not isinstance(horizon, int) or horizon < 1:
            raise ValueError("Label horizons must be positive integer candle counts.")
        future_times = bars["timestamp"] + pd.Timedelta(milliseconds=INTERVAL_MS[interval] * horizon)
        future_close = close_by_time.reindex(future_times).to_numpy(dtype=float)
        current_close = bars["close"].to_numpy(dtype=float)
        known = np.isfinite(future_close) & np.isfinite(current_close) & (current_close > 0)
        returns = np.full(len(bars), np.nan)
        returns[known] = future_close[known] / current_close[known] - 1.0
        direction = pd.Series(pd.NA, index=bars.index, dtype="Int8")
        direction.loc[known] = (future_close[known] > current_close[known]).astype("int8")
        labels[f"future_return_{horizon}bar"] = returns
        labels[f"target_up_{horizon}bar"] = direction
    return labels


def calculate_features(bars: pd.DataFrame, *, interval: str, use_cache: bool = True):
    """Reset feature history across actual candle gaps; never invent missing prices."""
    discontinuity = bars["timestamp"].diff().ne(pd.Timedelta(milliseconds=INTERVAL_MS[interval]))
    groups = discontinuity.cumsum()
    frames, diagnostics = [], []
    specs = None
    started = time.perf_counter()
    for _, segment in bars.groupby(groups, sort=False):
        result = build_features(segment.reset_index(drop=True), use_cache=use_cache)
        features = result.frame.copy()
        if len(features) != len(segment):
            raise ValueError("Feature calculation changed the input row count.")
        if features.columns.duplicated().any():
            raise ValueError("Feature names must be unique.")
        features.index = segment.index
        frames.append(features)
        diagnostics.append(result.diagnostics)
        if specs is None:
            specs = result.feature_specs
        elif [s["name"] for s in specs] != [s["name"] for s in result.feature_specs]:
            raise ValueError("Feature definitions differ between history segments.")
    features = pd.concat(frames).sort_index()
    if np.isinf(features.to_numpy(dtype=float)).any():
        raise ValueError("A feature produced infinity; unknown values must remain NaN.")
    return features, specs, {
        "elapsed_seconds": time.perf_counter() - started,
        "contiguous_segments": len(frames),
        "segments": diagnostics,
    }


def _data_summary(bars: pd.DataFrame, features: pd.DataFrame, *, interval: str) -> dict:
    step = pd.Timedelta(milliseconds=INTERVAL_MS[interval])
    differences = bars["timestamp"].diff()
    gaps = differences[differences > step]
    finite = np.isfinite(features.to_numpy(dtype=float))
    complete = finite.all(axis=1)
    return {
        "rows": len(bars),
        "feature_count": len(features.columns),
        "first_candle_utc": bars["timestamp"].iloc[0].isoformat(),
        "last_candle_utc": bars["timestamp"].iloc[-1].isoformat(),
        "last_close_utc": bars["close_time"].iloc[-1].isoformat(),
        "duplicate_timestamps": int(bars["timestamp"].duplicated().sum()),
        "gap_count": len(gaps),
        "missing_candle_count": int(sum(int(gap / step) - 1 for gap in gaps)),
        "rows_with_all_features": int(complete.sum()),
        "first_complete_feature_row_utc": (
            bars.loc[complete, "timestamp"].iloc[0].isoformat() if complete.any() else None
        ),
        "latest_row_missing_features": features.columns[~finite[-1]].tolist(),
        "feature_missing_counts": {name: int(features[name].isna().sum()) for name in features},
        "feature_constant_columns": [name for name in features if features[name].nunique(dropna=True) <= 1],
    }


def _load_previous(dataset_dir: Path):
    pointer = dataset_dir / "latest.json"
    if not pointer.exists():
        return None, None
    metadata = json.loads(pointer.read_text(encoding="utf-8"))
    run_id = metadata.get("run_id", "")
    if not re.fullmatch(r"[0-9TZ-]+-[a-f0-9]{8}", run_id):
        raise ValueError("Invalid local snapshot identifier in latest.json.")
    run_dir = dataset_dir / "runs" / run_id
    previous = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    bars = pd.read_parquet(run_dir / "ohlcv.parquet")
    return previous, bars


def _gap_coverage(bars: pd.DataFrame, *, step_ms: int, window_start_ms: int) -> dict:
    """Count missing internal intervals without materializing a large date range.

    A recoverable gap is within the nominal recent API window; availability
    is still subject to the exchange response. Counts refer to missing candles,
    not contiguous gap spans. History before the first locally stored row is
    unknown, not automatically a gap in the archive.
    """
    stamps = pd.DatetimeIndex(bars["timestamp"]).as_unit("ms").asi8
    starts, ends = stamps[:-1] + step_ms, stamps[1:]
    gaps = ends > starts
    starts, ends = starts[gaps], ends[gaps]
    recoverable_starts = np.maximum(starts, window_start_ms)
    recoverable_counts = np.maximum(0, (ends - recoverable_starts) // step_ms)
    total = int(((ends - starts) // step_ms).sum())
    recoverable = int(recoverable_counts.sum())
    eligible = recoverable_counts > 0
    return {
        "recoverable_gap_count": recoverable,
        "unrecoverable_gap_count": total - recoverable,
        "earliest_recoverable_gap_ms": int(recoverable_starts[eligible].min()) if eligible.any() else None,
    }


def _update_counts(existing: pd.DataFrame | None, incoming: pd.DataFrame) -> dict:
    if existing is None or existing.empty:
        return {"new_candles": len(incoming), "appended_candles": len(incoming),
                "repaired_candles": 0, "corrected_candles": 0}
    old, new = existing.set_index("timestamp"), incoming.set_index("timestamp")
    overlap = old.index.intersection(new.index)
    corrected = int(old.loc[overlap].ne(new.loc[overlap]).any(axis=1).sum())
    additions = new.index.difference(old.index)
    repaired = int((additions < old.index.max()).sum())
    return {"new_candles": len(additions), "appended_candles": len(additions) - repaired,
            "repaired_candles": repaired, "corrected_candles": corrected}


def _refresh_summary(bars, incoming, *, existing, step_ms, as_of_ms, start_ms, window_start_ms):
    expected_close_ms = as_of_ms // step_ms * step_ms
    last_close_ms = int(bars["close_time"].iloc[-1].value // 1_000_000)
    coverage = _gap_coverage(bars, step_ms=step_ms, window_start_ms=window_start_ms)
    coverage.pop("earliest_recoverable_gap_ms")
    utc = lambda stamp: pd.Timestamp(stamp, unit="ms", tz="UTC").isoformat()
    lag = max(0, (expected_close_ms - last_close_ms) // step_ms)
    outside_window_tail = max(0, (window_start_ms - last_close_ms) // step_ms)
    return {
        **_update_counts(existing, incoming), **coverage,
        "previous_rows": len(existing) if existing is not None else 0,
        "incoming_closed_candles": len(incoming),
        "fetched_at_utc": utc(as_of_ms),
        "expected_latest_open_utc": utc(expected_close_ms - step_ms),
        "expected_latest_close_utc": utc(expected_close_ms),
        "latest_candle_lag_intervals": lag,
        "latest_candle_available": lag == 0,
        "requested_start_utc": utc(start_ms),
        "requested_end_utc": utc(as_of_ms),
        "api_recoverable_window_start_utc": utc(window_start_ms),
        "api_history_limit_candles": API_HISTORY_LIMIT_CANDLES,
        "api_window_exhausted": coverage["unrecoverable_gap_count"] > 0 or outside_window_tail > 0,
        "unrecoverable_trailing_candles": outside_window_tail,
        "gap_count_unit": "Missing internal candle intervals, classified against the nominal latest-5000 window; actual API availability may differ.",
    }


def _benchmark(bars: pd.DataFrame, *, interval: str, reference: pd.DataFrame) -> dict:
    measurements = {"cached": [], "uncached": []}
    for repeat in range(3):
        # Alternate order to reduce first-run and warming bias.
        modes = (True, False) if repeat % 2 == 0 else (False, True)
        for cached in modes:
            started = time.perf_counter()
            frame, _, _ = calculate_features(bars, interval=interval, use_cache=cached)
            measurements["cached" if cached else "uncached"].append(time.perf_counter() - started)
            pd.testing.assert_frame_equal(reference, frame, rtol=1e-10, atol=1e-12)
    medians = {key: float(np.median(values)) for key, values in measurements.items()}
    return {
        "method": "Three alternating cached/uncached builds of the same implemented feature set.",
        "rows": len(bars),
        "samples_seconds": measurements,
        "median_seconds": medians,
        "cache_speedup_ratio": medians["uncached"] / medians["cached"],
        "outputs_match": True,
        "legacy_50_indicator_pipeline_comparison": False,
    }


def run_pipeline(
    *, coin: str = "BTC", interval: str = "15m", output_root: Path = DEFAULT_OUTPUT_ROOT,
    info_url: str = DEFAULT_INFO_URL, benchmark: bool = False, rebuild: bool = False,
    refresh_history: bool = False, horizons=DEFAULT_HORIZONS, as_of_ms: int | None = None,
) -> dict:
    coin = coin.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,20}", coin):
        raise ValueError("Use a simple perpetual coin symbol, such as BTC.")
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported fixed candle interval: {interval}")
    horizons = tuple(sorted(set(horizons)))
    if not horizons or any(not isinstance(h, int) or h < 1 for h in horizons):
        raise ValueError("At least one positive integer label horizon is required.")
    output_root = Path(output_root).resolve()
    dataset_dir = output_root / coin / interval
    dataset_dir.mkdir(parents=True, exist_ok=True)
    # One writer per symbol/interval. Readers only use the completed latest snapshot.
    with FileLock(str(dataset_dir / ".update.lock"), timeout=1):
        return _run_locked(
            coin=coin, interval=interval, dataset_dir=dataset_dir, info_url=info_url,
            benchmark=benchmark, rebuild=rebuild, refresh_history=refresh_history,
            horizons=horizons, as_of_ms=as_of_ms,
        )


def _run_locked(*, coin, interval, dataset_dir, info_url, benchmark, rebuild, refresh_history, horizons, as_of_ms):
    started = time.perf_counter()
    as_of_ms = int(time.time() * 1000) if as_of_ms is None else int(as_of_ms)
    step_ms = INTERVAL_MS[interval]
    expected_close_ms = as_of_ms // step_ms * step_ms
    # Include the earliest potentially recoverable completed candle. The API's
    # actual retained window determines which requested candles are returned.
    window_start_ms = max(0, expected_close_ms - API_HISTORY_LIMIT_CANDLES * step_ms)
    previous, existing = _load_previous(dataset_dir)
    if previous and (previous["coin"] != coin or previous["interval"] != interval or previous["info_url"] != info_url):
        raise ValueError("Existing history belongs to a different market or API endpoint; choose another output root.")
    if existing is not None and not existing.empty and existing["close_time"].max() > pd.Timestamp(as_of_ms, unit="ms", tz="UTC"):
        raise ValueError("Requested cutoff precedes stored completed candles; use a separate output root for a historical replay.")
    if existing is not None and not existing.empty and not refresh_history:
        start_ms = max(0, int(existing["timestamp"].iloc[-1].timestamp() * 1000) - 2 * step_ms)
        gaps = _gap_coverage(existing, step_ms=step_ms, window_start_ms=window_start_ms)
        if gaps["earliest_recoverable_gap_ms"] is not None:
            start_ms = min(start_ms, gaps["earliest_recoverable_gap_ms"])
        start_ms = max(start_ms, window_start_ms)
    else:
        start_ms = max(0, as_of_ms - API_HISTORY_LIMIT_CANDLES * step_ms)
    fetch_started = time.perf_counter()
    rows = fetch_candles(coin=coin, interval=interval, start_ms=start_ms, end_ms=as_of_ms, info_url=info_url)
    incoming = normalize_candles(rows, coin=coin, interval=interval, as_of_ms=as_of_ms)
    fetch_seconds = time.perf_counter() - fetch_started
    if incoming.empty and (existing is None or existing.empty):
        raise ValueError("Hyperliquid returned no completed candles for the requested period; previous files are untouched.")
    bars = merge_candles(existing, incoming) if existing is not None else incoming
    bars = bars.reset_index(drop=True)
    refresh = _refresh_summary(
        bars, incoming, existing=existing, step_ms=step_ms, as_of_ms=as_of_ms,
        start_ms=start_ms, window_start_ms=window_start_ms,
    )
    if existing is not None and bars.equals(existing.reset_index(drop=True)) and previous.get("feature_revision") == FEATURE_REVISION and previous.get("label_horizons_bars") == list(horizons) and not (rebuild or benchmark):
        elapsed = time.perf_counter() - started
        return {
            **previous, **refresh, "status": "unchanged",
            "check_seconds": elapsed,
            "fetch_seconds": fetch_seconds,
            "timings_seconds": {
                "fetch_and_normalize": fetch_seconds, "feature_build": 0.0,
                "parquet_and_catalog_write": 0.0, "total_before_return": elapsed,
            },
            "note": ("No new completed candles were returned; the saved snapshot was reused."
                     if incoming.empty else "Overlapping API candles matched the saved history; features were reused."),
        }
    features, specs, diagnostics = calculate_features(bars, interval=interval)
    overlaps = set(features.columns) & set(bars.columns)
    if overlaps:
        raise ValueError(f"Feature names overlap raw data columns: {sorted(overlaps)}")
    combined = pd.concat([bars, features], axis=1)
    labels = build_labels(bars, interval=interval, horizons=horizons)
    stats = _data_summary(bars, features, interval=interval)
    benchmark_result = _benchmark(bars, interval=interval, reference=features) if benchmark else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = dataset_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    write_started = time.perf_counter()
    paths = {name: run_dir / f"{name}.parquet" for name in ("ohlcv", "features", "labels")}
    bars.to_parquet(paths["ohlcv"], index=False, compression="zstd")
    combined.to_parquet(paths["features"], index=False, compression="zstd")
    labels.to_parquet(paths["labels"], index=False, compression="zstd")
    catalog = {
        "feature_revision": FEATURE_REVISION,
        "features": specs,
        "legacy_exclusions": diagnostics["segments"][0].get("legacy_exclusions", []),
        "scaling": "No scaler is fit across history. Fit any model scaler on its training fold only.",
        "warmup": "Insufficient history and undefined ratios remain null; no backfill from future rows.",
        "gaps": "Rolling/recursive state restarts at candle gaps. Gaps are reported, never fabricated.",
        "labels": {f"target_up_{h}bar": f"1 if close exactly {h} candles later exceeds current close; 0 otherwise; null if unknown." for h in horizons},
    }
    _write_json(run_dir / "feature_catalog.json", _jsonable(catalog))
    summary = {
        "status": "published", "run_id": run_id, "coin": coin, "market_type": "perpetual",
        "interval": interval, "info_url": info_url, "feature_revision": FEATURE_REVISION,
        "source": "Hyperliquid public candleSnapshot; local history retained across updates.",
        "label_horizons_bars": list(horizons),
        "label_horizons_minutes": [h * step_ms // 60000 for h in horizons],
        **refresh,
        **stats,
        "timings_seconds": {
            "fetch_and_normalize": fetch_seconds,
            "feature_build": diagnostics["elapsed_seconds"],
            "parquet_and_catalog_write": time.perf_counter() - write_started,
            "total_before_publish": time.perf_counter() - started,
        },
        "feature_calculations": diagnostics,
        "benchmark": benchmark_result,
        "files": {name: str(path) for name, path in paths.items()},
        "file_sizes_bytes": {name: path.stat().st_size for name, path in paths.items()},
        "feature_catalog": str(run_dir / "feature_catalog.json"),
        "summary_path": str(run_dir / "summary.json"),
        "latest_pointer": str(dataset_dir / "latest.json"),
    }
    summary = _jsonable(summary)
    _write_json(run_dir / "summary.json", summary)
    pointer_temp = dataset_dir / f".latest-{uuid4().hex}.tmp"
    _write_json(pointer_temp, {"run_id": run_id, "summary": str(run_dir / "summary.json"), "files": summary["files"]})
    os.replace(pointer_temp, dataset_dir / "latest.json")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--interval", choices=tuple(INTERVAL_MS), default="15m")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--info-url", default=None)
    parser.add_argument("--benchmark", action="store_true", help="Compare cached and uncached feature builds with output equality checks.")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild features even if the candle overlap is unchanged.")
    parser.add_argument("--refresh-history", action="store_true", help="Refetch the available 5000-candle window and retain older local history.")
    parser.add_argument("--horizons", nargs="+", type=int, default=list(DEFAULT_HORIZONS), help="Separate forward-label horizons in candle counts.")
    args = parser.parse_args(argv)
    # This existing accessor loads .env but reads only the public info URL here.
    from app.config import hyperliquid_info_url
    result = run_pipeline(
        coin=args.coin, interval=args.interval, output_root=args.output_root,
        info_url=args.info_url or hyperliquid_info_url(), benchmark=args.benchmark,
        rebuild=args.rebuild, refresh_history=args.refresh_history, horizons=args.horizons,
    )
    compact = {key: result[key] for key in (
        "status", "run_id", "coin", "interval", "rows", "feature_count", "new_candles",
        "first_candle_utc", "last_candle_utc", "rows_with_all_features", "gap_count",
        "latest_row_missing_features", "timings_seconds", "benchmark", "files", "summary_path",
    )}
    if "check_seconds" in result:
        compact["check_seconds"] = result["check_seconds"]
    print(json.dumps(compact, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
