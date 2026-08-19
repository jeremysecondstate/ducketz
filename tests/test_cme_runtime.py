from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.services.databento_cme_context import DatabentoCmeContextSpec
from datafetching.cme_history import (
    CmeCursor,
    cme_normalized_event_paths,
    five_minute_boundary,
    persist_cme_event_history,
    publish_cme_cursor,
    publish_cme_l2_snapshot,
    read_cme_cursor,
)
from datafetching.cme_runtime import (
    load_repository_environment,
    query_chunks,
    run_cme_cycle,
)
from datafetching.parquet_store import ParquetStore


UTC = timezone.utc


def test_cme_runtime_loads_repository_env_without_overriding_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable = "DUCKETS_TEST_DATABENTO_ENV_LOAD"
    env_file = tmp_path / ".env"
    env_file.write_text(f"{variable}=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv(variable, raising=False)

    assert load_repository_environment(env_file)
    assert os.environ[variable] == "from-dotenv"

    monkeypatch.setenv(variable, "from-shell")
    assert load_repository_environment(env_file)
    assert os.environ[variable] == "from-shell"


def test_partitioned_l2_history_preserves_nanoseconds_and_causal_receipts(
    tmp_path: Path,
) -> None:
    spec = _spec(
        start="2026-08-05T10:00:00Z",
        end="2026-08-05T10:05:02Z",
        symbols=("NQ.c.0", "ES.c.0"),
    )
    first_event = pd.Timestamp("2026-08-05T10:04:59.123456789Z")
    first_receipt = pd.Timestamp("2026-08-05T10:04:59.500000001Z")
    later_receipt = pd.Timestamp("2026-08-05T10:05:01.000000002Z")
    first = _event("NQ.c.0", first_event, first_receipt, sequence=1, price=100.0)

    initial = persist_cme_event_history(
        tmp_path,
        spec=spec,
        normalized_rows=(first,),
        raw_frame=pd.DataFrame(
            [
                {
                    "symbol": "NQ.c.0",
                    "ts_event": first_event,
                    "sequence": 1,
                    "action": "M",
                    "side": "B",
                    "depth": 0,
                    "price": 100.0,
                    "size": 10,
                }
            ]
        ),
    )
    assert initial.written == 2
    assert initial.rows == 1

    later_event = pd.Timestamp("2026-08-05T10:05:00.000000001Z")
    late_old_event = pd.Timestamp("2026-08-05T10:04:58.999999999Z")
    overlap = (
        _event(
            "NQ.c.0",
            first_event,
            later_receipt,
            sequence=1,
            price=100.0,
        ),
        _event(
            "NQ.c.0",
            later_event,
            later_receipt,
            sequence=2,
            price=101.0,
        ),
        _event(
            "ES.c.0",
            late_old_event,
            later_receipt,
            sequence=3,
            price=200.0,
        ),
    )
    continued = persist_cme_event_history(
        tmp_path,
        spec=spec,
        normalized_rows=overlap,
        raw_frame=None,
    )
    assert continued.written == 1
    replay = persist_cme_event_history(
        tmp_path,
        spec=spec,
        normalized_rows=overlap,
        raw_frame=None,
    )
    assert replay.written == 0
    assert replay.reused == 1

    paths = cme_normalized_event_paths(
        tmp_path,
        group_key="context",
        schema="mbp-10",
    )
    assert len(paths) == 1
    stored = pd.read_parquet(paths[0]).sort_values("sequence")
    assert len(stored) == 3
    assert pd.Timestamp(stored.iloc[0]["timestamp"]).value == first_event.value
    assert pd.Timestamp(stored.iloc[0]["fetched_at"]).value == first_receipt.value
    assert "received_at" not in stored.columns
    raw_path = next(
        (tmp_path / "pools" / "cme" / "events" / "databento" / "context" / "mbp-10" / "raw").rglob(
            "events.parquet"
        )
    )
    raw = pd.read_parquet(raw_path)
    assert pd.Timestamp(raw.loc[0, "ts_event"]).value == first_event.value
    assert pd.Timestamp(raw.loc[0, "fetched_at"]).value == first_receipt.value

    publish_cme_cursor(
        tmp_path,
        spec=spec,
        queried_through=spec.end,
        successful_at="2026-08-05T10:05:02Z",
        last_event_at=later_event,
        row_count=3,
    )
    snapshot = publish_cme_l2_snapshot(
        tmp_path,
        snapshot_for="2026-08-05T10:07:49.999999999Z",
    )
    assert snapshot is not None
    assert snapshot.snapshot_for == pd.Timestamp("2026-08-05T10:05:00Z")
    frame = pd.read_parquet(snapshot.snapshot_path)
    assert frame["provider_symbol"].tolist() == ["NQ.c.0"]
    assert pd.Timestamp(frame.iloc[0]["timestamp"]).value == first_event.value
    assert frame.iloc[0]["event_age_seconds"] == pytest.approx(0.876543211)
    assert bool(frame.iloc[0]["causally_available"])
    assert "received_at" not in frame.columns

    receipt = json.loads(snapshot.receipt_path.read_text(encoding="utf-8"))
    assert pd.Timestamp(receipt["snapshot_for"]) == snapshot.snapshot_for
    assert pd.Timestamp(receipt["available_at"]) >= snapshot.snapshot_for
    pointer_path = snapshot.directory.parent / "latest.json"
    pointer_path.unlink()
    recovered = publish_cme_l2_snapshot(
        tmp_path,
        snapshot_for="2026-08-05T10:07:49.999999999Z",
    )
    assert recovered is not None and recovered.reused
    assert pointer_path.is_file()

    later_snapshot = publish_cme_l2_snapshot(
        tmp_path,
        snapshot_for="2026-08-05T10:10:59Z",
    )
    assert later_snapshot is not None
    later = pd.read_parquet(later_snapshot.snapshot_path)
    assert set(later["provider_symbol"]) == {"NQ.c.0", "ES.c.0"}
    assert later.loc[later["provider_symbol"].eq("NQ.c.0"), "timestamp"].iloc[0] == later_event


def test_quiet_market_cursor_advances_by_successful_endpoint(tmp_path: Path) -> None:
    first_spec = _spec(
        start="2026-08-05T10:00:00Z",
        end="2026-08-05T10:01:00Z",
    )
    provider = _QuietProvider(first_spec)
    store = ParquetStore(tmp_path)

    first = run_cme_cycle(
        store,
        provider=provider,
        retry_attempts=1,
        retry_delay_seconds=0,
        now=lambda: datetime(2026, 8, 5, 10, 1, 1, tzinfo=UTC),
        reporter=None,
    )
    assert first.schemas_succeeded == 1
    assert first.event_rows == 0
    cursor = read_cme_cursor(
        tmp_path,
        group_key="context",
        schema="mbp-10",
    )
    assert cursor is not None
    assert cursor.queried_through == pd.Timestamp(first_spec.end)
    assert cursor.last_event_at is None

    provider.spec = replace(
        first_spec,
        start=datetime(2026, 8, 5, 10, 1, 30, tzinfo=UTC),
        end=datetime(2026, 8, 5, 10, 2, 0, tzinfo=UTC),
    )
    second = run_cme_cycle(
        store,
        provider=provider,
        retry_attempts=1,
        retry_delay_seconds=0,
        now=lambda: datetime(2026, 8, 5, 10, 2, 1, tzinfo=UTC),
        reporter=None,
    )
    assert second.schemas_succeeded == 1
    assert provider.requests[-1] == (
        pd.Timestamp("2026-08-05T10:00:58Z"),
        pd.Timestamp("2026-08-05T10:02:00Z"),
    )
    cursor = read_cme_cursor(
        tmp_path,
        group_key="context",
        schema="mbp-10",
    )
    assert cursor is not None
    assert cursor.queried_through == pd.Timestamp("2026-08-05T10:02:00Z")


def test_large_recovery_gap_is_chunked_with_safety_overlap() -> None:
    spec = _spec(
        start="2026-08-05T00:15:30Z",
        end="2026-08-05T00:16:00Z",
    )
    cursor = CmeCursor(
        "context",
        "mbp-10",
        pd.Timestamp("2026-08-05T00:00:00Z"),
        pd.Timestamp("2026-08-05T00:00:01Z"),
        None,
        0,
    )
    ranges = query_chunks(
        spec,
        cursor=cursor,
        overlap=timedelta(minutes=1),
        maximum_chunk=timedelta(minutes=5),
    )
    assert ranges[0][0] == pd.Timestamp("2026-08-04T23:59:00Z")
    assert ranges[-1][1] == pd.Timestamp("2026-08-05T00:16:00Z")
    assert len(ranges) == 4
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))


def test_large_recovery_gap_publishes_latest_lane_and_checkpoints_one_chunk(
    tmp_path: Path,
) -> None:
    mbp_spec = _spec(
        start="2026-08-05T10:19:30Z",
        end="2026-08-05T10:20:27Z",
    )
    bbo_spec = replace(
        mbp_spec,
        schema="bbo-1m",
        end=pd.Timestamp("2026-08-05T10:25:27Z").to_pydatetime(),
    )
    publish_cme_cursor(
        tmp_path,
        spec=mbp_spec,
        queried_through="2026-08-05T10:00:00Z",
        successful_at="2026-08-05T10:00:01Z",
        last_event_at="2026-08-05T09:59:59Z",
        row_count=1,
    )
    provider = _SaturatingExactProvider((bbo_spec, mbp_spec))

    result = run_cme_cycle(
        ParquetStore(tmp_path),
        provider=provider,
        retry_attempts=1,
        retry_delay_seconds=0,
        now=lambda: datetime(2026, 8, 5, 10, 20, 1, tzinfo=UTC),
        reporter=None,
    )

    assert [
        (pd.Timestamp(item.start), pd.Timestamp(item.end))
        for item in provider.requests[:2]
    ] == [
        (
            pd.Timestamp("2026-08-05T10:15:00Z"),
            pd.Timestamp("2026-08-05T10:20:00Z"),
        ),
        (
            pd.Timestamp("2026-08-05T10:19:55Z"),
            pd.Timestamp("2026-08-05T10:20:00Z"),
        ),
    ]
    assert (pd.Timestamp(provider.requests[-1].start), pd.Timestamp(provider.requests[-1].end)) == (
        pd.Timestamp("2026-08-05T09:59:58Z"),
        pd.Timestamp("2026-08-05T10:04:58Z"),
    )
    cursor = read_cme_cursor(tmp_path, group_key="context", schema="mbp-10")
    assert cursor is not None
    assert cursor.queried_through == pd.Timestamp("2026-08-05T10:04:58Z")
    pointer = json.loads(
        (
            tmp_path
            / "pools"
            / "cme"
            / "snapshots"
            / "l2"
            / "databento"
            / "5m"
            / "latest.json"
        ).read_text(encoding="utf-8")
    )
    assert pd.Timestamp(pointer["snapshot_for"]) == pd.Timestamp(
        "2026-08-05T10:20:00Z"
    )
    snapshot = pd.read_parquet(tmp_path / pointer["run_path"] / "snapshot.parquet")
    assert snapshot["quality_status"].eq("FRESH").all()
    assert set(snapshot["provider_schema"]) == {"bbo-1m", "mbp-10"}
    assert result.l2_snapshot_rows == len(snapshot)


def test_strict_l2_uses_current_configured_symbols_over_stale_cursor(
    tmp_path: Path,
) -> None:
    current = _spec(
        start="2026-08-05T10:19:55Z",
        end="2026-08-05T10:20:00Z",
        symbols=("NQ.c.0",),
    )
    event_at = pd.Timestamp("2026-08-05T10:19:59Z")
    persist_cme_event_history(
        tmp_path,
        spec=current,
        normalized_rows=(
            _event(
                "NQ.c.0",
                event_at,
                event_at + pd.Timedelta(milliseconds=1),
                sequence=1,
                price=100.0,
            ),
        ),
        raw_frame=None,
    )
    publish_cme_cursor(
        tmp_path,
        spec=replace(current, symbols=("OLD",)),
        queried_through=current.end,
        successful_at="2026-08-05T10:20:01Z",
        last_event_at=event_at,
        row_count=1,
    )

    assert (
        publish_cme_l2_snapshot(
            tmp_path,
            snapshot_for=current.end,
            available_not_after="2026-08-05T10:20:02Z",
            require_all_fresh=True,
        )
        is None
    )
    snapshot = publish_cme_l2_snapshot(
        tmp_path,
        snapshot_for=current.end,
        available_not_after="2026-08-05T10:20:02Z",
        require_all_fresh=True,
        expected_stream_symbols={("context", "mbp-10"): current.symbols},
    )

    assert snapshot is not None
    frame = pd.read_parquet(snapshot.snapshot_path)
    assert set(frame["provider_symbol"]) == {"NQ.c.0"}
    assert frame["quality_status"].eq("FRESH").all()


def test_saturated_cme_request_splits_without_advancing_past_missing_rows(
    tmp_path: Path,
) -> None:
    spec = _spec(
        start="2026-08-05T10:00:00Z",
        end="2026-08-05T10:00:05Z",
        symbols=("NQ.c.0", "ES.c.0"),
    )
    provider = _SaturatingExactProvider(spec)

    result = run_cme_cycle(
        ParquetStore(tmp_path),
        provider=provider,
        record_limits={"mbp-10": 2},
        retry_attempts=1,
        retry_delay_seconds=0,
        now=lambda: datetime(2026, 8, 5, 10, 0, 6, tzinfo=UTC),
        reporter=None,
    )

    assert result.schemas_succeeded == 1
    assert result.event_rows == 2
    assert [len(request.symbols) for request in provider.requests] == [2, 1, 1]
    assert all(request.limit == 2 for request in provider.requests)
    cursor = read_cme_cursor(tmp_path, group_key="context", schema="mbp-10")
    assert cursor is not None
    assert cursor.queried_through == pd.Timestamp(spec.end)
    paths = cme_normalized_event_paths(
        tmp_path,
        group_key="context",
        schema="mbp-10",
    )
    stored = pd.concat(
        [pd.read_parquet(path) for path in paths],
        ignore_index=True,
    )
    assert set(stored["provider_symbol"]) == {"NQ.c.0", "ES.c.0"}


def test_cme_concurrency_two_is_bounded_and_faster_on_independent_requests(
    tmp_path: Path,
) -> None:
    serial_provider = _DelayedQuietProvider(delay=0.07, count=4)
    started = time.perf_counter()
    serial = run_cme_cycle(
        ParquetStore(tmp_path / "serial"),
        provider=serial_provider,
        max_concurrency=1,
        retry_attempts=1,
        retry_delay_seconds=0,
        reporter=None,
    )
    serial_elapsed = time.perf_counter() - started

    concurrent_provider = _DelayedQuietProvider(delay=0.07, count=4)
    started = time.perf_counter()
    concurrent = run_cme_cycle(
        ParquetStore(tmp_path / "concurrent"),
        provider=concurrent_provider,
        max_concurrency=2,
        retry_attempts=1,
        retry_delay_seconds=0,
        reporter=None,
    )
    concurrent_elapsed = time.perf_counter() - started

    assert serial.schemas_failed == concurrent.schemas_failed == 0
    assert serial_provider.maximum_active == 1
    assert concurrent_provider.maximum_active == 2
    assert concurrent_elapsed < serial_elapsed * 0.8


def test_five_minute_boundaries_are_utc_aligned() -> None:
    assert five_minute_boundary("2026-08-05T10:04:59.999999999Z") == pd.Timestamp(
        "2026-08-05T10:00:00Z"
    )
    assert five_minute_boundary("2026-08-05T10:05:00.000000001Z") == pd.Timestamp(
        "2026-08-05T10:05:00Z"
    )


def _spec(
    *,
    start: str,
    end: str,
    symbols: tuple[str, ...] = ("NQ.c.0",),
) -> DatabentoCmeContextSpec:
    return DatabentoCmeContextSpec(
        group_key="context",
        output_symbol="CME_CONTEXT",
        symbols=symbols,
        dataset="GLBX.MDP3",
        schema="mbp-10",
        stype_in="continuous",
        start=pd.Timestamp(start).to_pydatetime(),
        end=pd.Timestamp(end).to_pydatetime(),
        limit=None,
    )


def _event(
    symbol: str,
    event_at: pd.Timestamp,
    fetched_at: pd.Timestamp,
    *,
    sequence: int,
    price: float,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "provider_symbol": symbol,
        "timestamp": event_at,
        "ts_event": event_at,
        "sequence": sequence,
        "action": "M",
        "side": "B",
        "depth": 0,
        "price": price,
        "size": 10,
        "fetched_at": fetched_at,
        "provider_schema": "mbp-10",
        "cme_context_group": "context",
    }


class _QuietProvider:
    def __init__(self, spec: DatabentoCmeContextSpec) -> None:
        self.spec = spec
        self.requests: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def specs(self) -> tuple[DatabentoCmeContextSpec, ...]:
        return (self.spec,)

    def fetch_cme_context(
        self,
        spec: DatabentoCmeContextSpec,
    ) -> tuple[list[dict[str, object]], pd.DataFrame, DatabentoCmeContextSpec]:
        self.requests.append((pd.Timestamp(spec.start), pd.Timestamp(spec.end)))
        return (
            [
                {
                    "cme_row_kind": "schema_status",
                    "timestamp": spec.end,
                    "fetched_at": spec.end,
                }
            ],
            pd.DataFrame(),
            spec,
        )


class _DelayedQuietProvider:
    def __init__(self, *, delay: float, count: int) -> None:
        self.delay = delay
        self.count = count
        self.active = 0
        self.maximum_active = 0
        self._lock = threading.Lock()
        self._end = datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC)

    def specs(self) -> tuple[DatabentoCmeContextSpec, ...]:
        return tuple(
            replace(
                _spec(
                    start="2026-08-05T09:59:55Z",
                    end="2026-08-05T10:00:00Z",
                ),
                group_key=f"group-{index}",
            )
            for index in range(self.count)
        )

    def fetch_cme_context(
        self,
        spec: DatabentoCmeContextSpec,
    ) -> tuple[list[dict[str, object]], pd.DataFrame, DatabentoCmeContextSpec]:
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.active -= 1
        return ([{"cme_row_kind": "schema_status"}], pd.DataFrame(), spec)


class _SaturatingExactProvider:
    def __init__(
        self,
        spec: DatabentoCmeContextSpec | tuple[DatabentoCmeContextSpec, ...],
    ) -> None:
        self._specs = spec if isinstance(spec, tuple) else (spec,)
        self.requests: list[DatabentoCmeContextSpec] = []

    def specs(self) -> tuple[DatabentoCmeContextSpec, ...]:
        return self._specs

    def fetch_cme_context(
        self,
        spec: DatabentoCmeContextSpec,
    ) -> tuple[list[dict[str, object]], pd.DataFrame, DatabentoCmeContextSpec]:
        raise AssertionError("The complete-history runtime must use the exact fetch path")

    def fetch_cme_context_exact(
        self,
        spec: DatabentoCmeContextSpec,
    ) -> tuple[list[dict[str, object]], pd.DataFrame, DatabentoCmeContextSpec]:
        self.requests.append(spec)
        timestamp = pd.Timestamp(spec.start) + pd.Timedelta(seconds=1)
        if len(spec.symbols) > 1:
            raw = pd.DataFrame(
                {
                    "symbol": list(spec.symbols),
                    "ts_event": [timestamp] * len(spec.symbols),
                    "sequence": range(1, len(spec.symbols) + 1),
                }
            )
            return [], raw, replace(spec, limit_saturated=True)

        symbol = spec.symbols[0]
        sequence = 1 if symbol.startswith("NQ") else 2
        row = _event(
            symbol,
            timestamp,
            timestamp + pd.Timedelta(milliseconds=1),
            sequence=sequence,
            price=100.0 + sequence,
        )
        raw = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "ts_event": timestamp,
                    "sequence": sequence,
                    "action": "M",
                    "side": "B",
                    "depth": 0,
                    "price": 100.0 + sequence,
                    "size": 10,
                }
            ]
        )
        return [row], raw, replace(spec, limit_saturated=False)
