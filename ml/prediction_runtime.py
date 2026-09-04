from __future__ import annotations

import argparse
import errno
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

from datafetching.loop_a_cycle import (
    datastore_cycle_lock,
    require_complete_loop_a_cycle,
)
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.current_publication import CurrentPublicationError, read_current_publication
from ml.horizons import (
    DEFAULT_FEATURE_PROFILE,
    FEATURE_PROFILES,
    HORIZON_ORDER,
    horizon_specifications_for_profile,
)
from ml.runtime_pipeline import RuntimeConfig, discover_symbols, run_loop_b_once
from ml.universe import read_watchlist


DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_PHASE_OFFSET_MINUTES = 6
DEFAULT_FAILURE_RETRY_ATTEMPTS = 1
DEFAULT_FAILURE_RETRY_DELAY_SECONDS = 60.0
DEFAULT_STALE_RECOVERY_MINUTES = 35.0

_TRANSIENT_OS_ERROR_NUMBERS = frozenset(
    value
    for value in (
        errno.EAGAIN,
        errno.EBUSY,
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.EINTR,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ETIMEDOUT,
    )
    if value is not None
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run Loop B: combine Loop A features, construct targets, train or "
            "reuse models, predict, reconcile matured predictions, evaluate, "
            "monitor, and refresh current intelligence outputs."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=None,
        help=(
            "Text file containing one equity symbol per line. --symbols "
            "overrides this file. If neither option is supplied, Loop B "
            "discovers datastore symbol directories."
        ),
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Explicit Loop B symbols; overrides --watchlist.",
    )
    parser.add_argument("--provider", default="databento")
    parser.add_argument(
        "--horizons",
        nargs="+",
        choices=HORIZON_ORDER,
        default=list(HORIZON_ORDER),
    )
    parser.add_argument(
        "--feature-profile",
        "--feature-set-profile",
        dest="feature_profile",
        choices=tuple(FEATURE_PROFILES),
        default=DEFAULT_FEATURE_PROFILE,
        help=(
            "Closed versioned feature profile. loop-a-all-v1 is the default "
            "integrated Loop A set; production-v1 and technical-all-v2 retain "
            "the legacy technical-only sets."
        ),
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
    )
    parser.add_argument(
        "--phase-offset-minutes",
        type=int,
        default=DEFAULT_PHASE_OFFSET_MINUTES,
        help=(
            "UTC phase inside each recurring interval."
        ),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--failure-retry-attempts",
        type=int,
        default=DEFAULT_FAILURE_RETRY_ATTEMPTS,
        help=(
            "Bounded retries for a recurring cycle that fails with a "
            "classified transient error. Production permits at most one."
        ),
    )
    parser.add_argument(
        "--failure-retry-delay-seconds",
        type=float,
        default=DEFAULT_FAILURE_RETRY_DELAY_SECONDS,
    )
    parser.add_argument(
        "--stale-recovery-minutes",
        type=float,
        default=DEFAULT_STALE_RECOVERY_MINUTES,
        help=(
            "On supervisor startup, run immediately when the last verified "
            "publication has been authoritative for at least this long."
        ),
    )
    parser.add_argument(
        "--model-family",
        choices=("logistic", "lightgbm", "xgboost"),
        default="logistic",
    )
    parser.add_argument(
        "--calibration",
        choices=("none", "platt", "isotonic"),
        default="platt",
    )
    parser.add_argument("--balanced-class-weight", action="store_true")
    parser.add_argument("--minimum-train-clusters", type=int, default=None)
    parser.add_argument("--calibration-clusters", type=int, default=None)
    parser.add_argument("--assessment-clusters", type=int, default=None)
    parser.add_argument("--lockbox-clusters", type=int, default=None)
    parser.add_argument(
        "--require-all-routes",
        action="store_true",
        help=(
            "Fail the complete cycle when any requested symbol/horizon route is "
            "unavailable. By default, publish valid routes and record limitations."
        ),
    )
    parser.add_argument("--round-trip-cost", type=float, default=0.001)
    args = parser.parse_args(argv)

    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if not 0 <= args.failure_retry_attempts <= 1:
        parser.error("--failure-retry-attempts must be 0 or 1")
    if not 0.0 <= args.failure_retry_delay_seconds <= 300.0:
        parser.error(
            "--failure-retry-delay-seconds must satisfy 0 <= delay <= 300"
        )
    if args.stale_recovery_minutes <= 0.0:
        parser.error("--stale-recovery-minutes must be positive")
    if not args.once:
        if not 0 <= args.phase_offset_minutes < args.interval_minutes:
            parser.error(
                "--phase-offset-minutes must satisfy "
                "0 <= phase < interval-minutes"
            )
    if (
        args.minimum_train_clusters is not None
        and args.minimum_train_clusters < 1
    ):
        parser.error("--minimum-train-clusters must be at least 1")
    if (
        args.calibration_clusters is not None
        and args.calibration_clusters < 1
    ):
        parser.error("--calibration-clusters must be at least 1")
    if (
        args.assessment_clusters is not None
        and args.assessment_clusters < 1
    ):
        parser.error("--assessment-clusters must be at least 1")
    if args.lockbox_clusters is not None and args.lockbox_clusters < 1:
        parser.error("--lockbox-clusters must be at least 1")
    if not 0.0 <= args.round_trip_cost < 1.0:
        parser.error("--round-trip-cost must satisfy 0 <= cost < 1")

    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    try:
        selected_symbols = resolve_prediction_symbols(
            symbols=args.symbols,
            watchlist=args.watchlist,
            datastore_root=root,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    specifications = horizon_specifications_for_profile(
        args.feature_profile,
        horizons=args.horizons,
    )
    config = RuntimeConfig(
        provider=args.provider,
        model_family=args.model_family,
        calibration_method=args.calibration,
        class_weight="balanced" if args.balanced_class_weight else None,
        assumed_round_trip_cost=args.round_trip_cost,
        minimum_train_clusters=args.minimum_train_clusters,
        calibration_clusters=args.calibration_clusters,
        assessment_clusters=args.assessment_clusters,
        lockbox_clusters=args.lockbox_clusters,
        require_all_routes=args.require_all_routes,
        feature_profile=args.feature_profile,
    )
    print("DUCKETS LOOP B")
    print("==============")
    print(f"DATASTORE: {root}")
    print(f"Symbols: {', '.join(selected_symbols)}")
    print(f"Provider: {config.provider}")
    print(f"Horizons: {', '.join(specifications)}")
    print(f"Feature profile: {config.feature_profile}")
    print(f"Model: {config.model_family}; calibration: {config.calibration_method}")
    print(
        "Option Pricing features: independent ml.option_pricing_runtime; "
        "coverage-gated"
    )
    print("Options strategy analytics: independent ml.strategy_runtime process")
    print(f"Interval: {args.interval_minutes} minutes")
    if not args.once:
        print(f"UTC phase: +{args.phase_offset_minutes} minutes")
        print(
            "Transient recovery: "
            f"{args.failure_retry_attempts} retry after "
            f"{args.failure_retry_delay_seconds:g} seconds"
        )
        print(
            "Startup freshness recovery: "
            f"{args.stale_recovery_minutes:g} minutes"
        )
    print("Loop A dependency: latest complete datastore inputs")
    print("Stop: Ctrl+C")
    print()

    lock_path = root / ".duckets-ml-prediction-runtime.lock"
    with runtime_lock(lock_path):
        try:
            first_recurring_cycle = True
            while True:
                if not args.once:
                    run_immediately = False
                    if first_recurring_cycle:
                        first_recurring_cycle = False
                        run_immediately, recovery_reason = (
                            publication_recovery_due(
                                root,
                                now=datetime.now(timezone.utc),
                                stale_after_minutes=args.stale_recovery_minutes,
                            )
                        )
                        if recovery_reason:
                            print(f"Startup freshness check: {recovery_reason}")
                    if not run_immediately:
                        next_run = next_boundary(
                            datetime.now(timezone.utc),
                            interval_minutes=args.interval_minutes,
                            phase_offset_minutes=args.phase_offset_minutes,
                        )
                        print(f"Next Loop B cycle: {next_run.isoformat()}")
                        print()
                        time.sleep(
                            max(
                                0.0,
                                (
                                    next_run - datetime.now(timezone.utc)
                                ).total_seconds(),
                            )
                        )
                    else:
                        print("Loop B startup recovery is running immediately.")
                        print()

                failure = _run_cycle(
                    root,
                    symbols=selected_symbols,
                    config=config,
                    specifications=specifications,
                )

                if args.once:
                    return 1 if failure is not None else 0

                if failure is not None and args.failure_retry_attempts:
                    retryable, retry_reason = failure_retry_decision(failure)
                    if retryable:
                        print(
                            "Loop B bounded recovery retry 1/1 scheduled in "
                            f"{args.failure_retry_delay_seconds:g} seconds: "
                            f"{retry_reason}"
                        )
                        time.sleep(args.failure_retry_delay_seconds)
                        retry_failure = _run_cycle(
                            root,
                            symbols=selected_symbols,
                            config=config,
                            specifications=specifications,
                        )
                        if retry_failure is None:
                            print("Loop B bounded recovery retry succeeded.")
                        else:
                            print(
                                "Loop B bounded recovery retry exhausted; "
                                "the prior verified publication remains "
                                "authoritative."
                            )
                    else:
                        print(
                            "Loop B recovery retry suppressed: "
                            f"{retry_reason}. The prior verified publication "
                            "remains authoritative."
                        )
        except KeyboardInterrupt:
            print("Loop B stopped.")
            return 0


def _run_cycle(
    root: Path,
    *,
    symbols: Sequence[str],
    config: RuntimeConfig,
    specifications: Sequence[str],
) -> Exception | None:
    try:
        with datastore_cycle_lock(root, reporter=print):
            loop_a_cycle = require_complete_loop_a_cycle(root)
            started_at = datetime.now(timezone.utc)
            print(f"LOOP B CYCLE {started_at.isoformat()}")
            print("-" * 48)
            print(f"Loop A datastore cycle: {loop_a_cycle.generation}")
            result = run_loop_b_once(
                root,
                symbols=symbols,
                config=config,
                specifications=specifications,
                run_timestamp=started_at,
                input_available_at=loop_a_cycle.finished_at,
                runtime_clock=lambda: datetime.now(timezone.utc),
                enforce_publication_deadline=True,
            )
        print(
            f"{result.status}: samples={result.sample_rows}; "
            "backtest_prediction_rows="
            f"{result.backtest_prediction_rows}; "
            "fresh_live_rows="
            f"{result.fresh_live_prediction_rows}; "
            "carried_active_live_rows="
            f"{result.carried_active_live_prediction_rows}; "
            "retained_frozen_weekly_live_rows="
            f"{result.retained_weekly_live_prediction_rows}; "
            "actionable_ordinary_routes="
            f"{result.actionable_ordinary_routes}; "
            "in_progress_ordinary_routes="
            f"{result.in_progress_ordinary_routes}; "
            f"evaluations={result.evaluation_rows}; "
            f"models_trained={result.models_trained}; "
            f"models_reused={result.models_reused}; "
            "directional publication complete"
        )
        print(f"Run: {result.run_directory}")
        print(f"Current: {result.latest_intelligence_path}")
        return None
    except Exception as exc:
        print(f"Loop B failed: {type(exc).__name__}: {exc}")
        return exc


def publication_recovery_due(
    root: Path,
    *,
    now: datetime,
    stale_after_minutes: float,
) -> tuple[bool, str]:
    """Decide whether a newly started supervisor needs one immediate cycle."""

    pointer_path = Path(root) / "ml" / "latest" / "run.json"
    if not pointer_path.is_file():
        return True, "no authoritative Loop B publication exists"
    try:
        publication = read_current_publication(root)
        timestamp_value = (
            publication.receipt.get("promoted_at")
            if publication.receipt is not None
            else publication.manifest.get("run_timestamp")
        )
        authoritative_at = _as_utc_datetime(
            timestamp_value,
            label="Loop B authoritative timestamp",
        )
    except (CurrentPublicationError, OSError, TypeError, ValueError) as exc:
        return (
            False,
            "authoritative publication integrity could not be verified; "
            f"fail-closed ({type(exc).__name__}: {exc})",
        )

    age = max(timedelta(0), now.astimezone(timezone.utc) - authoritative_at)
    age_minutes = age.total_seconds() / 60.0
    if age_minutes >= stale_after_minutes:
        return (
            True,
            f"verified publication age {age_minutes:.1f} minutes meets the "
            f"{stale_after_minutes:g}-minute recovery threshold",
        )
    return (
        False,
        f"verified publication age {age_minutes:.1f} minutes is below the "
        f"{stale_after_minutes:g}-minute recovery threshold",
    )


def failure_retry_decision(failure: Exception) -> tuple[bool, str]:
    """Allow one retry only for failures with credible transient semantics."""

    message = str(failure)
    folded = message.casefold()
    if "publication deadline" in folded or "actionable_until" in folded:
        return False, "the failed decision window is already terminal"
    if any(
        marker in folded
        for marker in (
            "checksum",
            "current publication",
            "publication receipt",
            "pointer",
            "integrity",
        )
    ):
        return False, "publication integrity failures require diagnosis"
    if "loop a" in folded and "writing" in folded:
        return True, "Loop A was still completing its atomic generation"
    if "loop a" in folded and "failed" in folded:
        return False, "the current Loop A generation is explicitly failed"
    if isinstance(failure, (TimeoutError, ConnectionError)):
        return True, "a bounded transient timeout or connection failure occurred"
    if isinstance(failure, OSError) and failure.errno in _TRANSIENT_OS_ERROR_NUMBERS:
        return True, "a bounded transient operating-system error occurred"
    return False, "the failure is not classified as safely retryable"


def _as_utc_datetime(value: object, *, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"{label} is not a timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{label} is timezone-naive")
    return parsed.astimezone(timezone.utc)


def resolve_prediction_symbols(
    *,
    symbols: Sequence[str] | None,
    watchlist: Path | None,
    datastore_root: Path,
) -> tuple[str, ...]:
    """Resolve one fixed Loop B scope before the recurring supervisor starts."""

    if symbols is not None:
        configured = _normalize_symbols(symbols)
    elif watchlist is not None:
        configured = read_watchlist(Path(watchlist))
    else:
        configured = discover_symbols(Path(datastore_root))
    if not configured:
        raise ValueError(
            "No Loop B symbols were configured. Add symbols to --watchlist, "
            "pass --symbols, or populate DATASTORE/stocks."
        )
    return configured


def _normalize_symbols(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in values
            if str(value).strip()
        )
    )


def next_boundary(
    now: datetime,
    *,
    interval_minutes: int,
    phase_offset_minutes: int = 0,
) -> datetime:
    if interval_minutes < 1:
        raise ValueError("interval_minutes must be positive")
    if not 0 <= phase_offset_minutes < interval_minutes:
        raise ValueError(
            "phase_offset_minutes must satisfy 0 <= phase < interval_minutes"
        )
    current = now.astimezone(timezone.utc)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    anchor = midnight + timedelta(minutes=phase_offset_minutes)
    if current < anchor:
        return anchor
    elapsed_minutes = int((current - anchor).total_seconds() // 60)
    next_slot = ((elapsed_minutes // interval_minutes) + 1) * interval_minutes
    boundary = anchor + timedelta(minutes=next_slot)
    if boundary <= current:
        boundary += timedelta(minutes=interval_minutes)
    return boundary


@contextmanager
def runtime_lock(path: Path) -> Iterator[None]:
    with exclusive_runtime_lock(
        path,
        process_name="Duckets Loop B process",
    ):
        yield


if __name__ == "__main__":
    raise SystemExit(main())
