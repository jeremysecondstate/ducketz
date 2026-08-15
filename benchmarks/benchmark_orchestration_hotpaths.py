from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from datafetching.bar_schema import read_normalized_bar_parquet
from datafetching.parquet_store import _time_keyed_upsert, _upsert
from ml.strategy_selection.chain import OptionChainHistory, entry_chain_receipt


def main() -> int:
    results: dict[str, dict[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix="ducketz-hotpaths-") as temporary:
        root = Path(temporary)
        results["analytical_bar_read"] = _benchmark_bar_reads(root)
        results["continuation_upsert"] = _benchmark_upserts()
        results["strategy_receipt_lookup"] = _benchmark_strategy_lookup()
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


def _benchmark_bar_reads(root: Path) -> dict[str, float]:
    count = 120_000
    path = root / "legacy-bars-without-ids.parquet"
    timestamps = pd.date_range("2026-01-01", periods=count, freq="1min", tz="UTC")
    values = np.arange(count, dtype=float)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 100.0 + values / 100_000.0,
            "high": 101.0 + values / 100_000.0,
            "low": 99.0 + values / 100_000.0,
            "close": 100.5 + values / 100_000.0,
            "volume": 1_000.0 + values,
        }
    ).to_parquet(path, index=False)

    before = _median_seconds(
        lambda: read_normalized_bar_parquet(path, include_ids=True)[0].drop(
            columns="id"
        ),
        repeats=2,
    )
    after = _median_seconds(
        lambda: read_normalized_bar_parquet(path, include_ids=False)[0],
        repeats=4,
    )
    return _result(before, after, rows=count)


def _benchmark_upserts() -> dict[str, float]:
    count = 60_000
    timestamps = pd.date_range("2026-01-01", periods=count, freq="1min", tz="UTC")
    values = np.arange(count, dtype=float)
    existing = pd.DataFrame(
        {
            "id": timestamps.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timestamp": timestamps,
            "open": values,
            "high": values + 1.0,
            "low": values - 1.0,
            "close": values + 0.5,
            "volume": values + 1_000.0,
        }
    )
    incoming = pd.concat(
        (
            existing.tail(60),
            pd.DataFrame(
                {
                    "id": pd.date_range(
                        timestamps[-1] + pd.Timedelta(minutes=1),
                        periods=10,
                        freq="1min",
                        tz="UTC",
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "timestamp": pd.date_range(
                        timestamps[-1] + pd.Timedelta(minutes=1),
                        periods=10,
                        freq="1min",
                        tz="UTC",
                    ),
                    "open": np.arange(count, count + 10, dtype=float),
                    "high": np.arange(count, count + 10, dtype=float) + 1.0,
                    "low": np.arange(count, count + 10, dtype=float) - 1.0,
                    "close": np.arange(count, count + 10, dtype=float) + 0.5,
                    "volume": np.arange(count, count + 10, dtype=float) + 1_000.0,
                }
            ),
        ),
        ignore_index=True,
    )
    legacy, _ = _upsert(existing, incoming, ("timestamp",))
    optimized, _ = _time_keyed_upsert(existing, incoming, key="timestamp")
    pd.testing.assert_frame_equal(
        legacy.reset_index(drop=True),
        optimized.reset_index(drop=True),
        check_dtype=False,
    )
    before = _median_seconds(
        lambda: _upsert(existing, incoming, ("timestamp",)),
        repeats=2,
    )
    after = _median_seconds(
        lambda: _time_keyed_upsert(existing, incoming, key="timestamp"),
        repeats=5,
    )
    return _result(before, after, rows=count)


def _benchmark_strategy_lookup() -> dict[str, float]:
    receipt_count = 1_200
    start = pd.Timestamp("2026-01-01T13:00:00Z")
    snapshots = pd.date_range(start, periods=receipt_count, freq="15min")
    available = snapshots + pd.Timedelta(minutes=2)
    surfaces = pd.DataFrame(
        {
            "symbol": "GOOG",
            "snapshot_for": snapshots,
            "available_at": available,
            "surface_quality_pass": True,
        }
    )
    contracts = pd.DataFrame(
        [
            {
                "symbol": "GOOG",
                "snapshot_for": snapshot,
                "available_at": receipt,
                "contract_symbol": f"GOOG-{index}-{leg}",
                "expiration_date": snapshot + pd.Timedelta(days=30),
                "strike": 95.0 + leg,
                "call_put": "CALL" if leg % 2 == 0 else "PUT",
            }
            for index, (snapshot, receipt) in enumerate(zip(snapshots, available))
            for leg in range(6)
        ]
    )
    history = OptionChainHistory(
        symbol="GOOG",
        contracts=contracts,
        surfaces=surfaces,
        quotes=pd.DataFrame(),
        source_files=(),
    )
    queries = tuple(
        start + pd.Timedelta(minutes=15 * index)
        for index in range(100, 1_100, 4)
    )

    def legacy() -> tuple[tuple[int, int], ...]:
        return tuple(_legacy_receipt(history, query) for query in queries)

    def indexed() -> tuple[tuple[int, int], ...]:
        output: list[tuple[int, int]] = []
        for query in queries:
            receipt = entry_chain_receipt(
                history,
                minimum_snapshot_for=query - pd.Timedelta(minutes=30),
                information_available_at=query,
                target_window_start=query + pd.Timedelta(hours=1),
                known_at=query + pd.Timedelta(minutes=59),
            )
            if receipt is None:
                output.append((-1, 0))
            else:
                output.append((int(receipt.available_at.value), len(receipt.contracts)))
        return tuple(output)

    assert legacy() == indexed()
    before = _median_seconds(legacy, repeats=2)
    after = _median_seconds(indexed, repeats=5)
    return _result(before, after, rows=len(queries))


def _legacy_receipt(
    history: OptionChainHistory,
    query: pd.Timestamp,
) -> tuple[int, int]:
    cutoff = query + pd.Timedelta(minutes=59)
    eligible = history.surfaces.loc[
        history.surfaces["available_at"].ge(query)
        & history.surfaces["available_at"].le(cutoff)
        & history.surfaces["snapshot_for"].ge(query - pd.Timedelta(minutes=30))
        & history.surfaces["snapshot_for"].le(cutoff)
    ].sort_values(["available_at", "snapshot_for"], kind="mergesort")
    if eligible.empty:
        return (-1, 0)
    surface = eligible.iloc[-1]
    matching = history.contracts.loc[
        history.contracts["symbol"].eq(surface["symbol"])
        & history.contracts["snapshot_for"].eq(surface["snapshot_for"])
        & history.contracts["available_at"].eq(surface["available_at"])
    ]
    return (int(pd.Timestamp(surface["available_at"]).value), len(matching))


def _median_seconds(operation: Callable[[], object], *, repeats: int) -> float:
    measurements: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        measurements.append(time.perf_counter() - started)
    return statistics.median(measurements)


def _result(before: float, after: float, *, rows: int) -> dict[str, float]:
    return {
        "rows_or_queries": float(rows),
        "before_milliseconds": round(before * 1_000.0, 3),
        "after_milliseconds": round(after * 1_000.0, 3),
        "speedup": round(before / after, 2),
    }


if __name__ == "__main__":
    raise SystemExit(main())
