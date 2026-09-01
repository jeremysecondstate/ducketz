from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from ml.stock_trader.contracts import (
    PredictionSignal,
    STOCK_TRADER_SYMBOLS,
    utc,
)
from ml.stock_trader.inputs import load_current_prediction_signals
from ml.stock_trader.inputs import PRIMARY_STOCK_HORIZONS
from ml.stock_trader.publication import read_decision_run
from ml.stock_trader.session import next_stock_target_start


PREDICTION_HANDOFF_SCHEMA_VERSION = "stock-trader-prediction-handoff-v2"
DEFAULT_POLL_SECONDS = 15.0
DEFAULT_CUTOFF_LEAD_SECONDS = 90.0
DEFAULT_MAXIMUM_TARGET_LEAD_SECONDS = 45.0 * 60.0
EXPECTED_FRESH_GENERATION_LEAD_SECONDS = 25.0 * 60.0


@dataclass(frozen=True)
class PredictionHandoffResult:
    status: str
    signals: Mapping[str, PredictionSignal]
    source_files: tuple[Path, ...]
    started_at: pd.Timestamp
    completed_at: pd.Timestamp
    expected_target_window_start: pd.Timestamp | None
    deadline: pd.Timestamp | None
    fresh_generation_not_before: pd.Timestamp | None
    poll_count: int
    fallback_used: bool
    fallback_candidate_observed: bool
    source_run_path: str | None
    source_run_timestamp: pd.Timestamp | None
    source_promoted_at: pd.Timestamp | None
    source_fingerprint: str | None
    selected_prediction_ids: tuple[str, ...]
    consumed_prediction_ids: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    publication_error_count: int
    last_error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PREDICTION_HANDOFF_SCHEMA_VERSION,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "wait_seconds": max(
                0.0,
                (self.completed_at - self.started_at).total_seconds(),
            ),
            "expected_target_window_start": _optional_iso(
                self.expected_target_window_start
            ),
            "deadline": _optional_iso(self.deadline),
            "fresh_generation_not_before": _optional_iso(
                self.fresh_generation_not_before
            ),
            "poll_count": self.poll_count,
            "poll_policy": {
                "receipt_authority": "checksum-verified-current-Loop-B-publication",
                "primary_horizons": list(PRIMARY_STOCK_HORIZONS),
                "fallback_age_feature": "prediction_age_minutes",
            },
            "fallback_used": self.fallback_used,
            "fallback_candidate_observed": self.fallback_candidate_observed,
            "source_run_path": self.source_run_path,
            "source_run_timestamp": _optional_iso(self.source_run_timestamp),
            "source_promoted_at": _optional_iso(self.source_promoted_at),
            "source_fingerprint": self.source_fingerprint,
            "selected_prediction_ids": list(self.selected_prediction_ids),
            "consumed_prediction_ids": list(self.consumed_prediction_ids),
            "missing_symbols": list(self.missing_symbols),
            "publication_error_count": self.publication_error_count,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class _Candidate:
    signals: Mapping[str, PredictionSignal]
    source_files: tuple[Path, ...]
    run_path: str
    run_timestamp: pd.Timestamp
    promoted_at: pd.Timestamp
    source_fingerprint: str


def wait_for_actionable_prediction(
    datastore_root: Path,
    *,
    started_at: object | None = None,
    expected_target_window_start: object | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    cutoff_lead_seconds: float = DEFAULT_CUTOFF_LEAD_SECONDS,
    maximum_target_lead_seconds: float = DEFAULT_MAXIMUM_TARGET_LEAD_SECONDS,
    generation_lead_seconds: float = EXPECTED_FRESH_GENERATION_LEAD_SECONDS,
    clock: Callable[[], object] = utc,
    sleeper: Callable[[float], None] = time.sleep,
    consumed_prediction_ids: set[str] | None = None,
    signal_loader: Callable[..., tuple[dict[str, PredictionSignal], tuple[Path, ...]]] = (
        load_current_prediction_signals
    ),
) -> PredictionHandoffResult:
    """Wait for the newest unconsumed actionable Loop B receipt for one target.

    The expected fresh generation is the Loop B generation 25 minutes before
    the selected 1h or 4h checkpoint (``:35`` for a top-of-hour target and
    ``:05`` for a half-hour target). An older receipt that still targets the
    same future window is retained as an age-aware fallback.
    """

    if poll_seconds <= 0.0:
        raise ValueError("prediction poll_seconds must be positive")
    if cutoff_lead_seconds < 0.0:
        raise ValueError("prediction cutoff_lead_seconds cannot be negative")
    if maximum_target_lead_seconds <= 0.0:
        raise ValueError("prediction maximum_target_lead_seconds must be positive")
    if generation_lead_seconds <= 0.0:
        raise ValueError("prediction generation_lead_seconds must be positive")

    root = Path(datastore_root).resolve()
    start = utc(started_at if started_at is not None else clock())
    target = (
        utc(expected_target_window_start)
        if expected_target_window_start is not None
        else next_stock_target_start(start)
    )
    if target is None:
        return _empty_result(
            "NO_UPCOMING_INTRADAY_TARGET",
            start=start,
            completed=start,
            target=None,
            deadline=None,
            fresh_not_before=None,
        )
    deadline = target - pd.Timedelta(seconds=cutoff_lead_seconds)
    fresh_not_before = target - pd.Timedelta(seconds=generation_lead_seconds)
    if (target - start).total_seconds() > maximum_target_lead_seconds:
        return _empty_result(
            "NO_NEAR_TERM_INTRADAY_TARGET",
            start=start,
            completed=start,
            target=target,
            deadline=deadline,
            fresh_not_before=fresh_not_before,
        )
    if start > deadline:
        return _empty_result(
            "PREDICTION_EXECUTION_DEADLINE_PASSED",
            start=start,
            completed=start,
            target=target,
            deadline=deadline,
            fresh_not_before=fresh_not_before,
        )

    consumed = (
        set(consumed_prediction_ids)
        if consumed_prediction_ids is not None
        else consumed_live_prediction_ids(root)
    )
    consumed_seen: set[str] = set()
    fallback: _Candidate | None = None
    polls = 0
    errors = 0
    last_error: str | None = None

    while True:
        now = utc(clock())
        polls += 1
        try:
            signals, source_files = signal_loader(root, as_of=now)
            run_path, run_timestamp, promoted_at = _receipt_metadata(
                root, source_files
            )
            matching = _matching_target_signals(signals, target=target, as_of=now)
            consumed_seen.update(
                signal.prediction_id
                for signal in matching.values()
                if signal.prediction_id in consumed
            )
            available = {
                symbol: signal
                for symbol, signal in matching.items()
                if signal.prediction_id not in consumed
            }
            if available:
                older_fallback_observed = (
                    fallback is not None
                    and fallback.run_timestamp < fresh_not_before
                )
                fingerprint = next(iter(available.values())).source_fingerprint
                candidate = _Candidate(
                    signals=available,
                    source_files=tuple(source_files),
                    run_path=run_path,
                    run_timestamp=run_timestamp,
                    promoted_at=promoted_at,
                    source_fingerprint=fingerprint,
                )
                if fallback is None or candidate.run_timestamp > fallback.run_timestamp:
                    fallback = candidate
                if run_timestamp >= fresh_not_before and now <= deadline:
                    return _selected_result(
                        "FRESH_ACTIONABLE_RECEIPT",
                        candidate,
                        start=start,
                        completed=now,
                        target=target,
                        deadline=deadline,
                        fresh_not_before=fresh_not_before,
                        polls=polls,
                        fallback_used=False,
                        fallback_observed=older_fallback_observed,
                        consumed_seen=consumed_seen,
                        errors=errors,
                        last_error=last_error,
                    )
            last_error = None
        except Exception as exc:
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"

        if now >= deadline:
            if fallback is not None:
                return _selected_result(
                    "FALLBACK_ACTIONABLE_RECEIPT",
                    fallback,
                    start=start,
                    completed=now,
                    target=target,
                    deadline=deadline,
                    fresh_not_before=fresh_not_before,
                    polls=polls,
                    fallback_used=True,
                    fallback_observed=True,
                    consumed_seen=consumed_seen,
                    errors=errors,
                    last_error=last_error,
                )
            status = (
                "PREDICTION_GENERATION_ALREADY_CONSUMED"
                if consumed_seen
                else "PREDICTION_DEADLINE_EXPIRED"
            )
            return _empty_result(
                status,
                start=start,
                completed=now,
                target=target,
                deadline=deadline,
                fresh_not_before=fresh_not_before,
                polls=polls,
                consumed_seen=consumed_seen,
                errors=errors,
                last_error=last_error,
            )

        remaining = max(0.0, (deadline - now).total_seconds())
        sleeper(min(poll_seconds, remaining))


def consumed_live_prediction_ids(datastore_root: Path) -> set[str]:
    """Return prediction IDs already used by an execution-requested LIVE run."""

    root = Path(datastore_root).resolve()
    runs_root = root / "ml" / "stock-trader-decision-runs"
    consumed: set[str] = set()
    if not runs_root.is_dir():
        return consumed
    for run in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        payload, _receipt = read_decision_run(root, run)
        if not bool(payload.get("execution_requested")):
            continue
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError(f"Stock trader decision run has no decisions array: {run}")
        for decision in decisions:
            if not isinstance(decision, Mapping):
                raise ValueError(f"Stock trader decision is not an object: {run}")
            if str(decision.get("decision_lane") or "LIVE").upper() != "LIVE":
                continue
            prediction = decision.get("prediction")
            prediction_id = (
                str(prediction.get("prediction_id") or "")
                if isinstance(prediction, Mapping)
                else ""
            )
            if prediction_id:
                consumed.add(prediction_id)
    return consumed


def _receipt_metadata(
    root: Path, source_files: Sequence[Path]
) -> tuple[str, pd.Timestamp, pd.Timestamp]:
    publication_path = next(
        (Path(path) for path in source_files if Path(path).name == "publication.json"),
        None,
    )
    if publication_path is None:
        raise ValueError("Current Loop B source set has no publication receipt")
    payload = json.loads(publication_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Current Loop B publication receipt is not an object")
    run_timestamp = utc(payload.get("run_timestamp"))
    promoted_at = utc(payload.get("promoted_at"))
    if promoted_at < run_timestamp:
        raise ValueError("Current Loop B publication promotion precedes its run")
    run_path = publication_path.parent.resolve().relative_to(root).as_posix()
    if payload.get("run_path") != run_path:
        raise ValueError("Current Loop B publication path differs from its receipt")
    return run_path, run_timestamp, promoted_at


def _matching_target_signals(
    signals: Mapping[str, PredictionSignal],
    *,
    target: pd.Timestamp,
    as_of: pd.Timestamp,
) -> dict[str, PredictionSignal]:
    matching: dict[str, PredictionSignal] = {}
    for symbol in STOCK_TRADER_SYMBOLS:
        signal = signals.get(symbol)
        if signal is None or signal.primary_horizon not in PRIMARY_STOCK_HORIZONS:
            continue
        try:
            signal_target = utc(signal.target_window_start)
            actionable_until = utc(signal.actionable_until)
        except (TypeError, ValueError):
            continue
        if (
            abs((signal_target - target).total_seconds()) < 1.0
            and actionable_until > as_of
        ):
            matching[symbol] = signal
    return matching


def _selected_result(
    status: str,
    candidate: _Candidate,
    *,
    start: pd.Timestamp,
    completed: pd.Timestamp,
    target: pd.Timestamp,
    deadline: pd.Timestamp,
    fresh_not_before: pd.Timestamp,
    polls: int,
    fallback_used: bool,
    fallback_observed: bool,
    consumed_seen: set[str],
    errors: int,
    last_error: str | None,
) -> PredictionHandoffResult:
    prediction_ids = tuple(
        candidate.signals[symbol].prediction_id
        for symbol in STOCK_TRADER_SYMBOLS
        if symbol in candidate.signals
    )
    return PredictionHandoffResult(
        status=status,
        signals=dict(candidate.signals),
        source_files=candidate.source_files,
        started_at=start,
        completed_at=completed,
        expected_target_window_start=target,
        deadline=deadline,
        fresh_generation_not_before=fresh_not_before,
        poll_count=polls,
        fallback_used=fallback_used,
        fallback_candidate_observed=fallback_observed,
        source_run_path=candidate.run_path,
        source_run_timestamp=candidate.run_timestamp,
        source_promoted_at=candidate.promoted_at,
        source_fingerprint=candidate.source_fingerprint,
        selected_prediction_ids=prediction_ids,
        consumed_prediction_ids=tuple(sorted(consumed_seen)),
        missing_symbols=tuple(
            symbol for symbol in STOCK_TRADER_SYMBOLS if symbol not in candidate.signals
        ),
        publication_error_count=errors,
        last_error=last_error,
    )


def _empty_result(
    status: str,
    *,
    start: pd.Timestamp,
    completed: pd.Timestamp,
    target: pd.Timestamp | None,
    deadline: pd.Timestamp | None,
    fresh_not_before: pd.Timestamp | None,
    polls: int = 0,
    consumed_seen: set[str] | None = None,
    errors: int = 0,
    last_error: str | None = None,
) -> PredictionHandoffResult:
    return PredictionHandoffResult(
        status=status,
        signals={},
        source_files=(),
        started_at=start,
        completed_at=completed,
        expected_target_window_start=target,
        deadline=deadline,
        fresh_generation_not_before=fresh_not_before,
        poll_count=polls,
        fallback_used=False,
        fallback_candidate_observed=False,
        source_run_path=None,
        source_run_timestamp=None,
        source_promoted_at=None,
        source_fingerprint=None,
        selected_prediction_ids=(),
        consumed_prediction_ids=tuple(sorted(consumed_seen or ())),
        missing_symbols=STOCK_TRADER_SYMBOLS,
        publication_error_count=errors,
        last_error=last_error,
    )


def _optional_iso(value: pd.Timestamp | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "DEFAULT_CUTOFF_LEAD_SECONDS",
    "DEFAULT_MAXIMUM_TARGET_LEAD_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "EXPECTED_FRESH_GENERATION_LEAD_SECONDS",
    "PREDICTION_HANDOFF_SCHEMA_VERSION",
    "PredictionHandoffResult",
    "consumed_live_prediction_ids",
    "wait_for_actionable_prediction",
]
