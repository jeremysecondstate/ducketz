from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

import pandas as pd
from dotenv import load_dotenv

from app.services.databento_market_data import DatabentoMarketDataProvider
from datafetching.bar_readiness import (
    BarReadiness,
    BarReadinessError,
    publish_frozen_bar_readiness,
    read_bar_readiness,
)
from datafetching.decision_time import cycle_target_decision


READINESS_LANE_VERSION = "loop-a-readiness-lane-v1"
CANONICAL_EQUITY_DATASET = "EQUS.MINI"
CANONICAL_EQUITY_SCHEMA = "ohlcv-1m"
CANONICAL_EQUITY_TIMEFRAME = "1m"
DEFAULT_READINESS_DEADLINE_SECONDS = 420.0


class ReadinessLaneDeadlineMissed(RuntimeError):
    """The exact target could not be frozen before its absolute deadline."""


def materialize_exact_target_readiness(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    target_snapshot_for: object,
    deadline_seconds: float = DEFAULT_READINESS_DEADLINE_SECONDS,
    poll_seconds: float = 10.0,
    provider: DatabentoMarketDataProvider | None = None,
    clock: Callable[[], object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> BarReadiness:
    """Poll Historical availability and freeze exactly one all-symbol 1m bar."""

    if deadline_seconds < 0:
        raise ValueError("Readiness lane deadline cannot be negative")
    if poll_seconds <= 0:
        raise ValueError("Readiness lane poll interval must be positive")
    root = Path(datastore_root).resolve()
    target = _utc(target_snapshot_for, "target_snapshot_for")
    clean_symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        )
    )
    if not clean_symbols:
        raise ValueError("Readiness lane requires at least one symbol")
    try:
        return read_bar_readiness(
            root,
            target_snapshot_for=target,
            required_symbols=clean_symbols,
        )
    except BarReadinessError:
        pass

    now = clock or (lambda: datetime.now(timezone.utc))
    started_at = _utc(now(), "readiness lane clock")
    deadline_at = target + pd.Timedelta(seconds=float(deadline_seconds))
    remaining_at_start = max(0.0, (deadline_at - started_at).total_seconds())
    monotonic_deadline = monotonic_clock() + remaining_at_start
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    effective_provider = provider or DatabentoMarketDataProvider()
    if str(effective_provider.dataset).strip().upper() != CANONICAL_EQUITY_DATASET:
        raise BarReadinessError(
            "Readiness lane requires canonical Databento EQUS.MINI"
        )
    specs = tuple(
        spec
        for spec in effective_provider.native_specs()
        if str(getattr(spec, "schema", "")).strip().lower()
        == CANONICAL_EQUITY_SCHEMA
        and str(getattr(spec, "frequency", "")).strip().lower()
        == CANONICAL_EQUITY_TIMEFRAME
    )
    if len(specs) != 1:
        raise BarReadinessError(
            "Readiness lane requires exactly one Databento EQUS.MINI ohlcv-1m spec"
        )
    spec = specs[0]
    metadata_attempts = 0
    request_attempts = 0
    advertised_end: pd.Timestamp | None = None
    first_delayed_stage: str | None = None
    last_error = "PROVIDER_RANGE_NOT_CHECKED"

    while True:
        observed_at = _utc(now(), "readiness lane clock")
        remaining = monotonic_deadline - monotonic_clock()
        if observed_at > deadline_at or remaining <= 0:
            detail = {
                "schema_version": READINESS_LANE_VERSION,
                "status": "READINESS_DEADLINE_MISSED",
                "target_snapshot_for": target.isoformat(),
                "started_at": started_at.isoformat(),
                "deadline_at": deadline_at.isoformat(),
                "observed_at": max(observed_at, deadline_at).isoformat(),
                "first_delayed_stage": first_delayed_stage or "SCHEDULER_START",
                "last_error": last_error,
                "metadata_attempts": metadata_attempts,
                "request_attempts": request_attempts,
                "provider": "databento",
                "dataset": CANONICAL_EQUITY_DATASET,
                "schema": CANONICAL_EQUITY_SCHEMA,
                "symbols": list(clean_symbols),
            }
            _write_attempt_telemetry(root, target=target, payload=detail)
            raise ReadinessLaneDeadlineMissed(
                "Exact Databento readiness deadline missed; "
                f"target={target.isoformat()}; deadline={deadline_at.isoformat()}; "
                f"first_delayed_stage={detail['first_delayed_stage']}; "
                f"last_error={last_error}"
            )
        try:
            metadata_attempts += 1
            range_payload = effective_provider.dataset_range()
            available_range = effective_provider.available_range_for_schema(
                spec.schema,
                dataset_range=range_payload,
            )
            advertised_end = _utc(
                available_range.end,
                "Databento advertised end",
            )
            if advertised_end < target:
                first_delayed_stage = (
                    first_delayed_stage or "DATABENTO_PROVIDER_AVAILABILITY"
                )
                last_error = (
                    "PROVIDER_TARGET_NOT_YET_AVAILABLE: "
                    f"advertised_end={advertised_end.isoformat()}"
                )
            else:
                request_attempts += 1
                fetched, selected_range = effective_provider.fetch_native_bars_range(
                    clean_symbols,
                    spec,
                    start=target - pd.Timedelta(minutes=1),
                    end=target,
                    available_range=available_range,
                )
                exact_bars = _exact_bar_payloads(
                    fetched,
                    symbols=clean_symbols,
                    target=target,
                )
                ready_at = _utc(now(), "readiness lane clock")
                if ready_at > deadline_at:
                    first_delayed_stage = (
                        first_delayed_stage or "DATABENTO_EXACT_RANGE_REQUEST"
                    )
                    last_error = "EXACT_RANGE_RETURNED_AFTER_DEADLINE"
                else:
                    coordination = {
                        "schema_version": READINESS_LANE_VERSION,
                        "lane": "INDEPENDENT_EXACT_BAR",
                        "scheduled_target": target.isoformat(),
                        "started_at": started_at.isoformat(),
                        "deadline_at": deadline_at.isoformat(),
                        "provider_available_end": advertised_end.isoformat(),
                        "request_start": pd.Timestamp(selected_range.start).isoformat(),
                        "request_end": pd.Timestamp(selected_range.end).isoformat(),
                        "metadata_attempts": metadata_attempts,
                        "request_attempts": request_attempts,
                        "first_delayed_stage": first_delayed_stage or "NONE",
                    }
                    return publish_frozen_bar_readiness(
                        root,
                        target_snapshot_for=target,
                        symbols=clean_symbols,
                        loop_a_generation=(
                            f"readiness-lane-{target.strftime('%Y%m%dT%H%M%SZ')}"
                            f"-pid{os.getpid()}"
                        ),
                        exact_bars=exact_bars,
                        coordination=coordination,
                        clock=lambda: ready_at,
                    )
        except ReadinessLaneDeadlineMissed:
            raise
        except Exception as exc:
            if first_delayed_stage is None:
                first_delayed_stage = (
                    "DATABENTO_EXACT_RANGE_REQUEST"
                    if advertised_end is not None and advertised_end >= target
                    else "DATABENTO_METADATA"
                )
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = monotonic_deadline - monotonic_clock()
        if remaining > 0:
            sleeper(min(float(poll_seconds), remaining))


@dataclass
class LoopAReadinessLane:
    datastore_root: Path
    symbols: tuple[str, ...]
    deadline_seconds: float = DEFAULT_READINESS_DEADLINE_SECONDS
    poll_seconds: float = 10.0
    clock: Callable[[], object] = lambda: datetime.now(timezone.utc)
    materializer: Callable[..., BarReadiness] = materialize_exact_target_readiness
    reporter: Callable[[str], None] = print
    attempted_targets: set[int] = field(default_factory=set)

    def inspect_once(self) -> Mapping[str, object] | None:
        observed_at = _utc(self.clock(), "readiness scheduler clock")
        decision = cycle_target_decision(observed_at)
        if not decision.actionable or decision.target_snapshot_for is None:
            return None
        target = pd.Timestamp(decision.target_snapshot_for)
        if target.value in self.attempted_targets:
            return None
        self.attempted_targets.add(target.value)
        deadline_at = target + pd.Timedelta(seconds=float(self.deadline_seconds))
        if observed_at > deadline_at:
            payload = {
                "schema_version": READINESS_LANE_VERSION,
                "status": "READINESS_DEADLINE_MISSED",
                "target_snapshot_for": target.isoformat(),
                "deadline_at": deadline_at.isoformat(),
                "observed_at": observed_at.isoformat(),
                "first_delayed_stage": "SCHEDULER_WAKE",
                "last_error": "TARGET_ALREADY_EXPIRED_AT_SCHEDULER_WAKE",
                "provider": "databento",
                "dataset": CANONICAL_EQUITY_DATASET,
                "schema": CANONICAL_EQUITY_SCHEMA,
                "symbols": list(self.symbols),
            }
            _write_attempt_telemetry(
                self.datastore_root,
                target=target,
                payload=payload,
            )
            self.reporter(
                "Loop A readiness lane missed an already-expired target: "
                f"target={target.isoformat()}; deadline={deadline_at.isoformat()}; "
                "first_delayed_stage=SCHEDULER_WAKE"
            )
            return payload
        try:
            readiness = self.materializer(
                self.datastore_root,
                symbols=self.symbols,
                target_snapshot_for=target,
                deadline_seconds=self.deadline_seconds,
                poll_seconds=self.poll_seconds,
                clock=self.clock,
            )
        except Exception as exc:
            self.reporter(
                "Loop A readiness lane failed closed: "
                f"target={target.isoformat()}; reason={type(exc).__name__}: {exc}"
            )
            return {
                "status": "FAILED_CLOSED",
                "target_snapshot_for": target.isoformat(),
                "reason": f"{type(exc).__name__}: {exc}",
            }
        self.reporter(
            "Loop A readiness lane published immutable exact bars: "
            f"target={target.isoformat()}; ready_at={readiness.ready_at.isoformat()}; "
            f"deadline={deadline_at.isoformat()}; symbols={len(readiness.symbols)}"
        )
        return {
            "status": "READY",
            "target_snapshot_for": target.isoformat(),
            "ready_at": readiness.ready_at.isoformat(),
        }

    def serve(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.inspect_once()
            stop_event.wait(1.0)


@contextmanager
def running_readiness_lane(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    deadline_seconds: float,
    poll_seconds: float,
    reporter: Callable[[str], None] = print,
) -> Iterator[LoopAReadinessLane]:
    """Run the lightweight target lane inside the canonical Loop A owner."""

    lane = LoopAReadinessLane(
        datastore_root=Path(datastore_root).resolve(),
        symbols=tuple(symbols),
        deadline_seconds=deadline_seconds,
        poll_seconds=poll_seconds,
        reporter=reporter,
    )
    stop_event = threading.Event()
    worker = threading.Thread(
        target=lane.serve,
        args=(stop_event,),
        name="loop-a-readiness-lane",
        daemon=True,
    )
    worker.start()
    reporter(
        "Loop A independent readiness lane started: "
        f"deadline_seconds={deadline_seconds:g}; poll_seconds={poll_seconds:g}"
    )
    try:
        yield lane
    finally:
        stop_event.set()
        worker.join(timeout=5.0)


def _exact_bar_payloads(
    fetched: Mapping[str, tuple[Sequence[object], pd.DataFrame]],
    *,
    symbols: Sequence[str],
    target: pd.Timestamp,
) -> dict[str, dict[str, object]]:
    expected_timestamp = target - pd.Timedelta(minutes=1)
    payloads: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        value = fetched.get(symbol)
        if value is None:
            raise BarReadinessError(
                f"Databento exact range omitted requested symbol {symbol}"
            )
        bars, _raw_frame = value
        exact = [
            bar
            for bar in bars
            if _utc(getattr(bar, "timestamp", None), f"{symbol} bar timestamp")
            == expected_timestamp
        ]
        if len(exact) != 1:
            raise BarReadinessError(
                f"Databento exact range did not resolve one completed bar for {symbol}"
            )
        bar = exact[0]
        payloads[symbol] = {
            "timestamp": expected_timestamp,
            "open": getattr(bar, "open"),
            "high": getattr(bar, "high"),
            "low": getattr(bar, "low"),
            "close": getattr(bar, "close"),
            "volume": getattr(bar, "volume"),
            "provider": "databento",
            "dataset": CANONICAL_EQUITY_DATASET,
            "schema": CANONICAL_EQUITY_SCHEMA,
            "timeframe": CANONICAL_EQUITY_TIMEFRAME,
        }
    return payloads


def _attempt_path(root: Path, target: pd.Timestamp) -> Path:
    return (
        Path(root)
        / "loop-a"
        / "bar-readiness-attempts"
        / str(target.value)
        / "attempt.json"
    )


def _write_attempt_telemetry(
    root: Path,
    *,
    target: pd.Timestamp,
    payload: Mapping[str, object],
) -> None:
    path = _attempt_path(root, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise BarReadinessError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "CANONICAL_EQUITY_DATASET",
    "DEFAULT_READINESS_DEADLINE_SECONDS",
    "LoopAReadinessLane",
    "ReadinessLaneDeadlineMissed",
    "materialize_exact_target_readiness",
    "running_readiness_lane",
]
