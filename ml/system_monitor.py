from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from app.models.portfolio import PortfolioSnapshot
from app.ui.options_strategy_data import load_strategy_candidates
from app.ui.rolling_forecast_data import (
    STANDARD_HORIZON_ORDER,
    load_forecast_dashboard,
)
from datafetching.bar_readiness import read_bar_readiness
from datafetching.decision_time import (
    cycle_target_decision,
    latest_eligible_option_target,
)
from datafetching.orchestrate import DEFAULT_WATCHLIST, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum
from ml.current_publication import CurrentPublication, read_current_publication
from ml.horizons import (
    INTERNAL_HORIZON_ORDER,
    INTERNAL_HORIZON_SPECIFICATIONS,
    WEEKLY_HORIZON_ORDER,
)
from ml.option_pricing.publication import read_current_option_pricing_publication
from ml.option_pricing.target_outcome import (
    read_current_target_outcome,
    target_outcome_pointer_path,
)
from ml.prediction_runtime import (
    DEFAULT_INTERVAL_MINUTES as LOOP_B_INTERVAL_MINUTES,
    DEFAULT_PHASE_OFFSET_MINUTES as LOOP_B_PHASE_OFFSET_MINUTES,
)
from ml.loop_c.publication import (
    LoopCPublicationError,
    loop_c_pointer_path,
    read_current_loop_c_publication,
)
from ml.loop_c.paper_ledger import (
    paper_ledger_pointer_path,
    read_current_paper_ledger,
)
from ml.sequence_encoder.publication import (
    SequencePublicationError,
    read_current_sequence_publication,
    resolve_current_sequence_output,
    sequence_pointer_path,
)
from ml.strategy_pricing_canary import (
    _strategy_score_evidence_masks,
    run_canary,
)
from ml.strategy_publication import (
    StrategyPublication,
    read_current_strategy_publication,
)
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_COVERAGE_SCORE_BASIS,
)
from ml.strategy_selection.slow_model import load_promoted_strategy_model
from options.publication import read_option_snapshot


MONITOR_SCHEMA_VERSION = "loops-system-monitor-v1"
OVERNIGHT_SCHEDULE_SCHEMA_VERSION = "loops-overnight-accuracy-schedule-v2"
_PASS = "PASS"
_INFO = "INFO"
_WARN = "WARN"
_FAIL = "FAIL"
_EXPECTED_PUBLICATION_AGE = pd.Timedelta(minutes=45)
_LOOP_B_PUBLICATION_WARNING_AGE = pd.Timedelta(minutes=35)
_TARGET_SETTLE_GRACE = pd.Timedelta(minutes=14)
_RECENT_STDERR_WINDOW = pd.Timedelta(hours=2)
_WEEKLY_MINIMUM_INDEPENDENT_OBSERVATIONS = 30
_STRATEGY_VALUE_AUDIT_POLICY_VERSION = "strategy-candidate-value-audit-v1"
_STRATEGY_EXPECTED_RETURN_EXTREME = 5.0
_STRATEGY_HIGH_RETURN_LOW_PROBABILITY_RETURN = 3.0
_STRATEGY_LOW_PROFIT_PROBABILITY = 0.02
_STRATEGY_TAIL_PAYOFF_FLOOR = 100.0
_STRATEGY_ROUTE_OUTLIER_MINIMUM_RETURN = 1.0
_STRATEGY_ROUTE_ROBUST_Z = 10.0
_STRATEGY_FORMULA_RELATIVE_TOLERANCE = 1e-9
_STRATEGY_FORMULA_ABSOLUTE_TOLERANCE = 1e-6
_STRATEGY_VALUE_AUDIT_TOP_FINDINGS = 12
_WEEKLY_CONTRACT_COLUMNS = (
    "provider",
    "model_name",
    "prediction_mode",
    "target_definition_version",
    "target_specification",
    "assumed_round_trip_cost",
)
_WEEKLY_CLUSTER_COLUMNS = (
    "symbol",
    "decision_timestamp",
    "target_window_start",
    "target_window_end",
)


@dataclass(frozen=True)
class OvernightStageSpec:
    stage_id: str
    title: str
    objective: str
    action_scope: str = "READ_ONLY_EVIDENCE"
    shadow_experiment_allowed: bool = False
    production_freeze: bool = False


_OVERNIGHT_LOCAL_HOURS = (
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    0,
    1,
    2,
    3,
    4,
    5,
)
OVERNIGHT_ACCURACY_STAGES = (
    OvernightStageSpec(
        "seal-core-options-session",
        "Seal the completed core and options session",
        "Verify final regular-session bars, options publications, fingerprints, and target boundaries while keeping PRE/REGULAR/POST stock evidence provisional until the 17:00 Pacific actionable close.",
    ),
    OvernightStageSpec(
        "audit-input-quality",
        "Audit input quality and lineage",
        "Measure missing or duplicate bars, stale inputs, timestamp and schema errors, nulls, latency, coverage, and lineage drift.",
    ),
    OvernightStageSpec(
        "audit-ui-output-parity",
        "Audit Duckets UI output parity",
        "Verify Rolling Forecast and Options Strategy values, labels, units, freshness, empty states, and visual presentation against receipt-valid publications.",
        action_scope="UI_VALIDATION",
    ),
    OvernightStageSpec(
        "evaluate-directional-1h",
        "Evaluate mature 1h predictions",
        "Evaluate causally mature 1h coverage, calibration, proper scores, hit rate, and PRE/REGULAR/POST cohort-specific errors without treating the still-open stock day as complete.",
    ),
    OvernightStageSpec(
        "evaluate-directional-4h",
        "Seal the extended stock day and evaluate mature 4h predictions",
        "First verify the complete PRE/REGULAR/POST stock day through the 17:00 Pacific actionable close, then evaluate causally mature 4h coverage, calibration, proper scores, hit rate, and checkpoint-specific failure patterns.",
    ),
    OvernightStageSpec(
        "evaluate-directional-1d",
        "Evaluate mature 1d predictions",
        "Evaluate prior causally mature 1d vintages for calibration, drift, coverage, and regime-specific errors.",
    ),
    OvernightStageSpec(
        "evaluate-directional-1w",
        "Evaluate mature 1w predictions",
        "Evaluate weekly vintages only when sufficient independent causally mature outcomes exist.",
    ),
    OvernightStageSpec(
        "audit-cross-horizon-coherence",
        "Audit cross-horizon coherence",
        "Find unexplained 1h, 4h, 1d, and 1w disagreements without forcing distinct horizons to agree.",
    ),
    OvernightStageSpec(
        "audit-options-inputs",
        "Audit options inputs",
        "Check option-chain completeness, quote age, spreads, strikes, expirations, Greeks clocks, liquidity evidence, and missing reasons.",
    ),
    OvernightStageSpec(
        "audit-pricing-execution",
        "Audit pricing and execution evidence",
        "Test Pricing coverage, conservative fills, fees, slippage, execution haircuts, and outlier valuations.",
    ),
    OvernightStageSpec(
        "evaluate-strategy-outcomes",
        "Evaluate exact options-strategy outcomes",
        "Evaluate mature 1d and 1w exact-strategy positive net return after modeled execution and fees.",
    ),
    OvernightStageSpec(
        "audit-probability-calibration",
        "Audit probability calibration",
        "Compare raw and calibrated probabilities, reliability bins, ECE, Brier, log loss, base rates, and probability collapse.",
    ),
    OvernightStageSpec(
        "select-nightly-bottleneck",
        "Select the nightly accuracy bottleneck",
        "Choose one evidence-backed problem and preregister its hypothesis, cohort, primary and safety metrics, and rollback rule.",
    ),
    OvernightStageSpec(
        "run-shadow-ablation",
        "Run one bounded shadow ablation",
        "Run the session's sole new isolated data, feature, calibration, pooled-sequence, or model experiment without production wiring.",
        action_scope="SHADOW_ONLY_BUILD",
        shadow_experiment_allowed=True,
    ),
    OvernightStageSpec(
        "compare-challenger",
        "Compare champion and challenger",
        "Compare identical chronological evidence, including any pooled-sequence challenger, with assessment isolation, reproducible fingerprints, and no new tuning.",
        action_scope="READ_ONLY_VALIDATION",
    ),
    OvernightStageSpec(
        "stress-and-gate-review",
        "Stress results and review gates",
        "Stress symbols, regimes, windows, missing-data conditions, fees, and execution assumptions, then accept, reject, or propose.",
        action_scope="READ_ONLY_VALIDATION",
    ),
    OvernightStageSpec(
        "preopen-freeze",
        "Freeze and verify before market open",
        "Perform final health and rollback verification, summarize the overnight cycle, and make no new experimental production change.",
        action_scope="PREOPEN_FREEZE",
        production_freeze=True,
    ),
)

if len(_OVERNIGHT_LOCAL_HOURS) != len(OVERNIGHT_ACCURACY_STAGES):
    raise RuntimeError("Overnight stage hours and specifications must remain aligned")


@dataclass(frozen=True)
class RuntimeSpec:
    name: str
    module: str
    required_arguments: tuple[str, ...]
    lock_name: str
    log_aliases: tuple[str, ...]
    quiet_when_market_closed: bool = False
    maximum_log_age: pd.Timedelta | None = _EXPECTED_PUBLICATION_AGE


@dataclass(frozen=True)
class _MonitorPublicationSnapshot:
    """One invocation's immutable Loop B and Strategy authorities."""

    loop_b: CurrentPublication | None
    strategy: StrategyPublication | None
    loop_b_error: str | None = None
    strategy_error: str | None = None

    def require_loop_b(self) -> CurrentPublication:
        if self.loop_b is None:
            raise RuntimeError(
                "Pinned Loop B publication is unavailable: "
                f"{self.loop_b_error or 'unknown capture error'}"
            )
        return self.loop_b

    def require_strategy(self) -> StrategyPublication:
        if self.strategy is None:
            raise RuntimeError(
                "Pinned Strategy publication is unavailable: "
                f"{self.strategy_error or 'unknown capture error'}"
            )
        return self.strategy


def _capture_monitor_publications(root: Path) -> _MonitorPublicationSnapshot:
    """Read each mutable current pointer once before any dependent check runs."""

    loop_b: CurrentPublication | None = None
    strategy: StrategyPublication | None = None
    loop_b_error: str | None = None
    strategy_error: str | None = None
    try:
        loop_b = read_current_publication(root)
    except Exception as exc:
        loop_b_error = _error_text(exc)
    try:
        strategy = read_current_strategy_publication(root)
    except Exception as exc:
        strategy_error = _error_text(exc)
    return _MonitorPublicationSnapshot(
        loop_b=loop_b,
        strategy=strategy,
        loop_b_error=loop_b_error,
        strategy_error=strategy_error,
    )


def _publication_snapshot_details(
    root: Path,
    snapshot: _MonitorPublicationSnapshot,
) -> dict[str, object]:
    def details(
        publication: CurrentPublication | StrategyPublication | None,
        error: str | None,
    ) -> dict[str, object]:
        if publication is None:
            return {"status": "UNAVAILABLE", "reason": error}
        return {
            "status": "PINNED",
            "run_path": publication.run_directory.relative_to(root).as_posix(),
            "run_timestamp": _utc(
                publication.manifest.get("run_timestamp"),
                "pinned publication run_timestamp",
            ).isoformat(),
        }

    return {
        "capture_policy": "READ_EACH_CURRENT_POINTER_ONCE",
        "loop_b": details(snapshot.loop_b, snapshot.loop_b_error),
        "strategy": details(snapshot.strategy, snapshot.strategy_error),
    }


RUNTIMES = (
    RuntimeSpec(
        "cme",
        "datafetching.cme_runtime",
        ("--datastore-target pc", "--max-concurrency 1"),
        ".ducketz-cme-writer.lock",
        ("cme-l2", "cme"),
    ),
    RuntimeSpec(
        "alfred",
        "datafetching.fred_alfred_runtime",
        ("--datastore-target pc", "--utc-hour 7"),
        ".ducketz-fred-alfred-import.lock",
        ("daily-alfred", "alfred"),
        maximum_log_age=pd.Timedelta(hours=30),
    ),
    RuntimeSpec(
        "loop_a",
        "datafetching.orchestrate",
        (
            "--datastore-target pc",
            "--providers databento fmp fred schwab sec",
            "--cme-mode external",
            "--options-mode external",
            "--bar-readiness-recovery-timeout-seconds 420",
            "--bar-readiness-recovery-poll-seconds 10",
        ),
        ".ducketz-orchestration.lock",
        ("loop-a",),
    ),
    RuntimeSpec(
        "pricing",
        "ml.option_pricing_runtime",
        (
            "--datastore-target pc",
            "--phase-offset-minutes 1",
            "--bar-readiness-mode required",
            "--bar-readiness-timeout-seconds 480",
        ),
        ".ducketz-option-pricing-runtime.lock",
        ("active-pricing", "pricing"),
        quiet_when_market_closed=True,
    ),
    RuntimeSpec(
        "loop_b",
        "ml.prediction_runtime",
        (
            "--datastore-target pc",
            "--horizons 1h 4h 1d 1w",
            "--feature-profile loop-a-all-bsgp-active-v3",
            "--calibration platt",
            f"--interval-minutes {LOOP_B_INTERVAL_MINUTES}",
            f"--phase-offset-minutes {LOOP_B_PHASE_OFFSET_MINUTES}",
            "--failure-retry-attempts 1",
            "--failure-retry-delay-seconds 60",
            "--stale-recovery-minutes 35",
        ),
        ".duckets-ml-prediction-runtime.lock",
        ("directional-loop-b", "loop-b"),
    ),
    RuntimeSpec(
        "options",
        "datafetching.options_runtime",
        (
            "--datastore-target pc",
            "--provider-mode opra-canonical",
            "--phase-offset-minutes 6",
            "--pricing-barrier-timeout-seconds 45",
            "--bar-readiness-mode required",
        ),
        ".ducketz-options-writer.lock",
        ("options-prospective", "options-capture", "options"),
        quiet_when_market_closed=True,
    ),
    RuntimeSpec(
        "strategy",
        "ml.strategy_runtime",
        (
            "--datastore-target pc",
            "--phase-offset-minutes 10",
            "--pricing-mode active",
        ),
        ".ducketz-strategy-runtime.lock",
        ("strategy",),
    ),
    RuntimeSpec(
        "strategy_profit_training",
        "ml.strategy_profit_training_runtime",
        ("--datastore-target pc", "--utc-hour 22"),
        ".ducketz-strategy-profit-training-runtime.lock",
        ("strategy-profit-training",),
        maximum_log_age=pd.Timedelta(hours=30),
    ),
)


def build_monitor_report(
    datastore_root: Path,
    *,
    mode: str,
    observed_at: object | None = None,
    process_rows: Sequence[Mapping[str, object]] | None = None,
    symbols: Sequence[str] | None = None,
) -> dict[str, object]:
    """Build one read-only, fail-contained system or production-quality report."""

    clean_mode = str(mode).strip().lower()
    if clean_mode not in {"hourly", "daily", "weekly"}:
        raise ValueError("mode must be hourly, daily, or weekly")
    root = Path(datastore_root).resolve()
    now = _utc(observed_at if observed_at is not None else _now(), "observed_at")
    watchlist = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in (symbols or read_watchlist(DEFAULT_WATCHLIST))
            if str(value).strip()
        )
    )
    if not watchlist:
        raise ValueError("The production watchlist is empty")
    market = cycle_target_decision(now)
    latest_target = latest_eligible_option_target(now)
    rows = tuple(process_rows) if process_rows is not None else _windows_process_rows()
    publications = _capture_monitor_publications(root)

    checks: list[dict[str, object]] = []
    checks.extend(_safe_many("runtime_processes", lambda: _process_checks(rows)))
    checks.append(_safe("runtime_locks", lambda: _lock_check(root, rows)))
    checks.append(
        _safe(
            "runtime_logs",
            lambda: _log_activity_check(root, now=now, market_actionable=market.actionable),
        )
    )
    checks.append(_safe("loop_a_cycle", lambda: _loop_a_cycle_check(root, now, watchlist)))
    checks.append(
        _safe(
            "loop_a_bar_readiness",
            lambda: _bar_readiness_check(
                root,
                now=now,
                expected_target=latest_target,
                symbols=watchlist,
                market_actionable=market.actionable,
            ),
        )
    )
    checks.append(_safe("cme_publication", lambda: _cme_snapshot_check(root)))
    checks.append(_safe("alfred_publication", lambda: _alfred_pointer_check(root, now)))
    checks.append(
        _safe(
            "options_publications",
            lambda: _options_check(
                root,
                now=now,
                expected_target=latest_target,
                symbols=watchlist,
                market_actionable=market.actionable,
            ),
        )
    )
    checks.append(
        _safe(
            "loop_b_publication",
            lambda: _loop_b_check(
                root,
                now,
                watchlist,
                publication=publications.require_loop_b(),
            ),
        )
    )
    checks.append(
        _safe(
            "pricing_publications",
            lambda: _pricing_check(
                root,
                now=now,
                expected_target=latest_target,
                market_actionable=market.actionable,
            ),
        )
    )
    checks.append(
        _safe(
            "strategy_publication",
            lambda: _strategy_check(
                root,
                now,
                publication=publications.require_strategy(),
            ),
        )
    )
    checks.append(
        _safe(
            "strategy_candidate_value_sanity",
            lambda: _strategy_candidate_value_check(
                root,
                publication=publications.require_strategy(),
            ),
        )
    )
    checks.append(
        _safe(
            "strategy_profit_model_authority",
            lambda: _strategy_profit_model_check(root),
        )
    )
    checks.append(
        _safe(
            "strategy_value_shadow",
            lambda: _strategy_value_shadow_check(
                root,
                now=now,
                publication=publications.require_strategy(),
            ),
        )
    )
    checks.append(
        _safe(
            "sequence_encoder_loop_c",
            lambda: _sequence_encoder_loop_c_check(root, now=now),
        )
    )
    checks.append(
        _safe(
            "cross_loop_lineage",
            lambda: _lineage_check(
                root,
                loop_b=publications.require_loop_b(),
                strategy=publications.require_strategy(),
            ),
        )
    )
    checks.append(
        _safe(
            "ui_contracts",
            lambda: _ui_check(
                root,
                now,
                watchlist,
                loop_b=publications.require_loop_b(),
                strategy_publication=publications.require_strategy(),
            ),
        )
    )
    checks.append(_safe("storage_capacity", lambda: _storage_check(root)))

    if clean_mode in {"daily", "weekly"}:
        checks.append(_safe("alfred_full_evidence", lambda: _alfred_full_check(root)))
        checks.append(
            _safe(
                "directional_prediction_quality",
                lambda: _directional_quality_check(
                    root,
                    publication=publications.require_loop_b(),
                ),
            )
        )
        checks.append(
            _safe(
                "strategy_prediction_quality",
                lambda: _strategy_quality_check(
                    root,
                    publication=publications.require_strategy(),
                ),
            )
        )
        checks.append(
            _safe(
                "pricing_strategy_canary",
                lambda: _pricing_strategy_canary_check(
                    root,
                    now=now,
                    target=latest_target,
                    symbols=watchlist,
                    strategy_publication=publications.require_strategy(),
                ),
            )
        )
    if clean_mode == "weekly":
        checks.append(
            _safe(
                "weekly_evaluation_rollup",
                lambda: _weekly_evaluation_rollup_check(
                    root,
                    now=now,
                    publication=publications.require_loop_b(),
                ),
            )
        )

    report_status = _overall_status(checks)
    counts = {
        status: sum(check.get("status") == status for check in checks)
        for status in (_PASS, _INFO, _WARN, _FAIL)
    }
    return {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "mode": clean_mode,
        "status": report_status,
        "checked_at": now.isoformat(),
        "datastore": str(root),
        "market": {
            "cycle_mode": market.cycle_mode,
            "target_state": market.target_state.value,
            "current_cycle_target": (
                market.target_snapshot_for.isoformat()
                if market.target_snapshot_for is not None
                else None
            ),
            "latest_completed_regular_target": latest_target.isoformat(),
            "next_eligible_target": market.next_eligible_target.isoformat(),
            "reason": market.reason,
        },
        "watchlist": list(watchlist),
        "publication_generations": _publication_snapshot_details(
            root,
            publications,
        ),
        "summary": {
            "check_count": len(checks),
            "counts": counts,
            "attention_required": [
                str(check["name"])
                for check in checks
                if check.get("status") in {_WARN, _FAIL}
            ],
        },
        "checks": checks,
        "read_only": True,
        "orders_placed": 0,
        "automated_action_allowed": False,
    }


def _process_checks(
    process_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    normalized = [_normalize_process(row) for row in process_rows]
    checks: list[dict[str, object]] = []
    for spec in RUNTIMES:
        matches = [
            row for row in normalized if _command_owns_module(row["command_line"], spec.module)
        ]
        pids = sorted(int(row["pid"]) for row in matches)
        if len(matches) != 2:
            checks.append(
                _check(
                    f"process.{spec.name}",
                    _FAIL,
                    f"Expected one launcher/worker pair; observed {len(matches)} process(es).",
                    module=spec.module,
                    pids=pids,
                )
            )
            continue
        by_pid = {int(row["pid"]): row for row in matches}
        parent_child = [
            row
            for row in matches
            if int(row["ppid"]) in by_pid and int(row["ppid"]) != int(row["pid"])
        ]
        command_lines = [_normalize_spaces(str(row["command_line"]).lower()) for row in matches]
        missing_arguments = sorted(
            {
                argument
                for argument in spec.required_arguments
                if any(argument.lower() not in command for command in command_lines)
            }
        )
        if len(parent_child) != 1 or missing_arguments:
            checks.append(
                _check(
                    f"process.{spec.name}",
                    _FAIL,
                    "Runtime ownership or required arguments do not match production.",
                    module=spec.module,
                    pids=pids,
                    parent_child_pairs=[
                        [int(row["ppid"]), int(row["pid"])] for row in parent_child
                    ],
                    missing_arguments=missing_arguments,
                )
            )
            continue
        worker = parent_child[0]
        launcher_pid = int(worker["ppid"])
        checks.append(
            _check(
                f"process.{spec.name}",
                _PASS,
                "Exactly one production launcher/worker pair is running.",
                module=spec.module,
                launcher_pid=launcher_pid,
                worker_pid=int(worker["pid"]),
                worker_started_at=worker.get("created_at"),
            )
        )
    return checks


def _lock_check(
    root: Path,
    process_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    normalized = [_normalize_process(row) for row in process_rows]
    results: dict[str, object] = {}
    failures: list[str] = []
    for spec in RUNTIMES:
        owners = [
            row
            for row in normalized
            if _command_owns_module(row["command_line"], spec.module)
        ]
        owner_pids = {int(row["pid"]) for row in owners}
        path = root / spec.lock_name
        if not path.is_file():
            failures.append(f"{spec.name}:missing")
            results[spec.name] = {"path": str(path), "status": "MISSING"}
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?m)^pid=(\d+)\s*$", text)
        pid = int(match.group(1)) if match else None
        valid = pid in owner_pids
        results[spec.name] = {
            "path": str(path),
            "pid": pid,
            "status": "OWNED" if valid else "STALE_OR_MISMATCHED",
        }
        if not valid:
            failures.append(f"{spec.name}:pid={pid}")
    return _check(
        "runtime_locks",
        _FAIL if failures else _PASS,
        (
            "Every singleton lock is owned by its matching runtime."
            if not failures
            else "One or more singleton locks are missing or owned by another PID."
        ),
        failures=failures,
        locks=results,
    )


def _log_activity_check(
    root: Path,
    *,
    now: pd.Timestamp,
    market_actionable: bool,
) -> dict[str, object]:
    primary_log_root = root / "logs" / "ducketz"
    legacy_log_root = root / "runtime-logs"
    log_roots = tuple(
        path for path in (primary_log_root, legacy_log_root) if path.is_dir()
    )
    stdout_files = tuple(
        path
        for log_root in log_roots
        for pattern in ("**/*.stdout.log", "**/*.out.log")
        for path in log_root.glob(pattern)
    )
    problems: list[str] = []
    results: dict[str, object] = {}
    for spec in RUNTIMES:
        candidates = [
            path
            for path in stdout_files
            if any(
                _runtime_log_name_matches(path.name, alias)
                for alias in spec.log_aliases
            )
        ]
        if not candidates:
            problems.append(f"{spec.name}:stdout-missing")
            results[spec.name] = {"status": "MISSING"}
            continue
        stdout = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        modified = pd.Timestamp(stdout.stat().st_mtime, unit="s", tz="UTC")
        age = max(pd.Timedelta(0), now - modified)
        quiet_allowed = spec.quiet_when_market_closed and not market_actionable
        stale = (
            spec.maximum_log_age is not None
            and age > spec.maximum_log_age
            and not quiet_allowed
        )
        stderr = _paired_stderr_path(stdout)
        recent_stderr = False
        stderr_tail = None
        if stderr.is_file() and stderr.stat().st_size > 0:
            stderr_modified = pd.Timestamp(stderr.stat().st_mtime, unit="s", tz="UTC")
            recent_stderr = now - stderr_modified <= _RECENT_STDERR_WINDOW
            if recent_stderr:
                stderr_tail = _last_nonempty_line(stderr)
        if stale:
            problems.append(f"{spec.name}:stdout-stale")
        if recent_stderr:
            problems.append(f"{spec.name}:recent-stderr")
        results[spec.name] = {
            "stdout": str(stdout),
            "log_authority": (
                "PRIMARY"
                if primary_log_root in stdout.resolve().parents
                else "LEGACY_RUNTIME_LOGS"
            ),
            "stdout_age_minutes": _minutes(age),
            "quiet_allowed": quiet_allowed,
            "stderr": str(stderr) if stderr.exists() else None,
            "recent_stderr": recent_stderr,
            "stderr_tail": stderr_tail,
            "status": "WARN" if stale or recent_stderr else "CURRENT",
        }
    return _check(
        "runtime_logs",
        _WARN if problems else _PASS,
        (
            "Active logs are current and have no recent stderr."
            if not problems
            else "At least one runtime log needs inspection."
        ),
        problems=problems,
        runtimes=results,
        primary_log_root=str(primary_log_root),
        legacy_log_root=str(legacy_log_root),
    )


def _paired_stderr_path(stdout: Path) -> Path:
    name = stdout.name
    if name.endswith(".stdout.log"):
        return stdout.with_name(name[: -len(".stdout.log")] + ".stderr.log")
    if name.endswith(".out.log"):
        return stdout.with_name(name[: -len(".out.log")] + ".err.log")
    raise ValueError(f"Unsupported runtime stdout log name: {stdout}")


def _runtime_log_name_matches(name: str, alias: str) -> bool:
    """Match one exact log stem, allowing only a timestamp/prefix separator.

    Substring matching lets the `strategy` runtime accidentally claim the
    newer `strategy-profit-training` log.  Primary launch logs use the exact
    stem; legacy logs may prepend a timestamp followed by `-` or `_`.
    """

    clean_name = str(name).strip().lower()
    clean_alias = str(alias).strip().lower()
    for suffix in (".stdout.log", ".out.log"):
        if clean_name.endswith(suffix):
            stem = clean_name[: -len(suffix)]
            return (
                stem == clean_alias
                or stem.endswith(f"-{clean_alias}")
                or stem.endswith(f"_{clean_alias}")
            )
    return False


def _loop_a_cycle_check(
    root: Path,
    now: pd.Timestamp,
    symbols: Sequence[str],
) -> dict[str, object]:
    cycle_path = root / ".ducketz-loop-a-cycle.json"
    complete_path = root / ".ducketz-loop-a-complete.json"
    cycle = _read_json(cycle_path)
    complete = _read_json(complete_path)
    cycle_status = str(cycle.get("status", "UNKNOWN"))
    finished = _utc(complete.get("finished_at"), "Loop A complete finished_at")
    age = max(pd.Timedelta(0), now - finished)
    expected_symbols = set(symbols)
    observed_symbols = {
        str(value).strip().upper() for value in complete.get("symbols", ())
    }
    failures = []
    if complete.get("status") != "COMPLETE" or int(complete.get("failure_count", -1)) != 0:
        failures.append("last-complete-authority-invalid")
    if cycle_status == "WRITING":
        started = _utc(cycle.get("started_at"), "Loop A active-cycle started_at")
        active_age = max(pd.Timedelta(0), now - started)
        if active_age > _EXPECTED_PUBLICATION_AGE:
            failures.append("active-cycle-running-too-long")
    elif cycle_status == "COMPLETE":
        active_age = pd.Timedelta(0)
        if cycle != complete:
            failures.append("completed-cycle-and-authority-disagree")
    else:
        active_age = pd.Timedelta(0)
        failures.append(f"active-cycle-status-{cycle_status.lower()}")
    if observed_symbols != expected_symbols:
        failures.append("watchlist-scope-mismatch")
    if age > _EXPECTED_PUBLICATION_AGE:
        failures.append("latest-complete-cycle-stale")
    return _check(
        "loop_a_cycle",
        _FAIL if failures else _PASS,
        (
            "Loop A has a fresh zero-failure complete cycle."
            if not failures
            else "Loop A's latest complete-cycle authority is unhealthy."
        ),
        generation=cycle.get("generation"),
        active_cycle_status=cycle_status,
        active_cycle_age_minutes=_minutes(active_age),
        last_complete_generation=complete.get("generation"),
        finished_at=finished.isoformat(),
        age_minutes=_minutes(age),
        failure_count=complete.get("failure_count"),
        symbols=sorted(observed_symbols),
        failures=failures,
    )


def _bar_readiness_check(
    root: Path,
    *,
    now: pd.Timestamp,
    expected_target: pd.Timestamp,
    symbols: Sequence[str],
    market_actionable: bool,
) -> dict[str, object]:
    pointer_path = root / "loop-a" / "bar-readiness-latest" / "run.json"
    attempt_path = (
        root
        / "loop-a"
        / "bar-readiness-attempts"
        / str(expected_target.value)
        / "attempt.json"
    )
    attempt = _read_json(attempt_path) if attempt_path.is_file() else None
    if not pointer_path.is_file():
        return _check(
            "loop_a_bar_readiness",
            _FAIL if market_actionable else _INFO,
            "No Loop A target-bar readiness has been published yet.",
            expected_target=expected_target.isoformat(),
            target_attempt=attempt,
        )
    pointer = _read_json(pointer_path)
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise ValueError("Loop A bar-readiness pointer has no current record")
    target = _utc(current.get("target_snapshot_for"), "bar-readiness target")
    readiness = read_bar_readiness(
        root,
        target_snapshot_for=target,
        required_symbols=symbols,
    )
    expected_path = readiness.directory.relative_to(root).as_posix()
    pointer_valid = (
        current.get("run_path") == expected_path
        and current.get("receipt_checksum_sha256")
        == readiness.receipt_checksum_sha256
    )
    target_matches = target == expected_target
    settling = market_actionable and now - expected_target <= _TARGET_SETTLE_GRACE
    if not pointer_valid:
        status = _FAIL
        summary = "Loop A bar-readiness pointer does not match its receipt."
    elif target_matches:
        status = _PASS
        summary = "Loop A readiness is checksum-valid for the latest regular target."
    elif settling:
        status = _INFO
        summary = "The latest regular target is still inside its pipeline settle window."
    elif market_actionable:
        status = _FAIL
        summary = "Loop A readiness is behind the latest actionable regular target."
    else:
        status = _INFO
        summary = "Market is closed; the last verified readiness is retained without backdating."
    return _check(
        "loop_a_bar_readiness",
        status,
        summary,
        target_snapshot_for=target.isoformat(),
        expected_target=expected_target.isoformat(),
        ready_at=readiness.ready_at.isoformat(),
        symbol_count=len(readiness.symbols),
        pointer_valid=pointer_valid,
        settling=settling,
        coordination=dict(readiness.coordination),
        immutable_bar_file_count=sum(
            bool(raw.get("source_file_checksum_sha256"))
            for raw in readiness.bars.values()
            if isinstance(raw, Mapping)
        ),
        target_attempt=attempt,
    )


def _cme_snapshot_check(root: Path) -> dict[str, object]:
    pointer_path = root / "pools" / "cme" / "snapshots" / "l2" / "databento" / "5m" / "latest.json"
    pointer = _read_json(pointer_path)
    relative = _safe_relative_path(pointer.get("run_path"), label="CME run_path")
    run = (root / relative).resolve()
    authority = (root / "pools" / "cme" / "snapshots" / "l2" / "databento" / "5m").resolve()
    if run.parent != authority:
        raise ValueError("CME pointer escapes its immutable authority")
    receipt_path = run / "receipt.json"
    manifest_path = run / "manifest.json"
    snapshot_path = run / "snapshot.parquet"
    receipt = _read_json(receipt_path)
    valid = (
        pointer.get("schema_version") == "cme-l2-pointer-v1"
        and pointer.get("receipt_checksum_sha256") == file_checksum(receipt_path)
        and receipt.get("schema_version") == "cme-l2-snapshot-v1"
        and receipt.get("run_path") == relative.as_posix()
        and receipt.get("manifest_checksum_sha256") == file_checksum(manifest_path)
        and receipt.get("snapshot_checksum_sha256") == file_checksum(snapshot_path)
        and pointer.get("snapshot_for") == receipt.get("snapshot_for")
    )
    if not valid:
        raise ValueError("CME L2 pointer, receipt, manifest, or snapshot checksum disagrees")
    return _check(
        "cme_publication",
        _PASS,
        "The current CME L2 publication is checksum-valid.",
        snapshot_for=receipt.get("snapshot_for"),
        available_at=receipt.get("available_at"),
        row_count=receipt.get("row_count"),
        run_path=relative.as_posix(),
    )


def _alfred_pointer_check(root: Path, now: pd.Timestamp) -> dict[str, object]:
    pointer_path = root / "ml" / "fred-alfred-runtime-latest" / "run.json"
    pointer = _read_json(pointer_path)
    relative = _safe_relative_path(pointer.get("receipt_path"), label="ALFRED receipt_path")
    receipt_path = (root / relative).resolve()
    authority = (root / "ml" / "fred-alfred-runtime").resolve()
    if authority not in receipt_path.parents:
        raise ValueError("ALFRED pointer escapes its immutable authority")
    receipt = _read_json(receipt_path)
    valid = (
        pointer.get("schema_version") == "fred-alfred-daily-pointer-v1"
        and pointer.get("receipt_checksum_sha256") == file_checksum(receipt_path)
        and receipt.get("schema_version") == "fred-alfred-daily-runtime-v1"
        and receipt.get("status") == "COMPLETE"
        and receipt.get("run_date_utc") == pointer.get("run_date_utc")
        and receipt.get("loop_b_consumption_authorized") is True
        and receipt.get("current_revised_history_used") is False
        and receipt.get("automated_action_allowed") is False
    )
    if not valid:
        raise ValueError("ALFRED runtime pointer or receipt verification failed")
    run_date = pd.Timestamp(str(pointer["run_date_utc"]), tz="UTC")
    age = now.normalize() - run_date
    status = _PASS if age <= pd.Timedelta(days=1) else _WARN
    return _check(
        "alfred_publication",
        status,
        (
            "The daily ALFRED receipt is current and consumption-authorized."
            if status == _PASS
            else "The verified ALFRED receipt is more than one UTC date behind."
        ),
        run_date_utc=pointer.get("run_date_utc"),
        receipt_path=relative.as_posix(),
        age_days=float(age / pd.Timedelta(days=1)),
    )


def _options_check(
    root: Path,
    *,
    now: pd.Timestamp,
    expected_target: pd.Timestamp,
    symbols: Sequence[str],
    market_actionable: bool,
) -> dict[str, object]:
    snapshots = []
    providers: dict[str, int] = {}
    missing: list[str] = []
    for symbol in symbols:
        selected = None
        for provider in ("databento-opra", "schwab"):
            pointer_path = (
                root
                / "stocks"
                / symbol
                / "options"
                / "latest"
                / f"{provider}.json"
            )
            if not pointer_path.is_file():
                continue
            pointer = _read_json(pointer_path)
            relative = _safe_relative_path(
                pointer.get("run_path"), label=f"{symbol} option run_path"
            )
            run = (root / relative).resolve()
            authority = (
                root / "stocks" / symbol / "options" / "snapshots" / provider
            ).resolve()
            if run.parent != authority:
                raise ValueError(f"{symbol} option pointer escapes {provider} authority")
            snapshot = read_option_snapshot(run)
            if pointer.get("receipt_checksum_sha256") != file_checksum(
                snapshot.receipt_path
            ):
                raise ValueError(f"{symbol} option pointer checksum disagrees")
            if (
                snapshot.symbol != symbol
                or snapshot.provider != provider
                or pointer.get("target_snapshot_for")
                != snapshot.snapshot_for.isoformat()
            ):
                raise ValueError(f"{symbol} option pointer identity disagrees")
            selected = snapshot
            break
        if selected is None:
            missing.append(symbol)
            continue
        snapshots.append(selected)
        providers[selected.provider] = providers.get(selected.provider, 0) + 1
    if missing:
        return _check(
            "options_publications",
            _FAIL,
            "One or more watchlist symbols have no verified current option snapshot.",
            missing_symbols=missing,
            expected_target=expected_target.isoformat(),
        )
    targets = sorted({snapshot.snapshot_for for snapshot in snapshots})
    all_current = targets == [expected_target]
    settling = market_actionable and now - expected_target <= _TARGET_SETTLE_GRACE
    if all_current:
        status = _PASS
        summary = "All symbols have one verified option snapshot for the latest target."
    elif settling:
        status = _INFO
        summary = "The latest option target is still inside its settle window."
    elif market_actionable:
        status = _FAIL
        summary = "Current option snapshots are behind the actionable regular target."
    else:
        status = _WARN
        summary = "Closed-market option authorities do not all end at the last regular target."
    return _check(
        "options_publications",
        status,
        summary,
        expected_target=expected_target.isoformat(),
        observed_targets=[target.isoformat() for target in targets],
        provider_counts=providers,
        symbol_count=len(snapshots),
        settling=settling,
    )


def _loop_b_check(
    root: Path,
    now: pd.Timestamp,
    symbols: Sequence[str],
    *,
    publication: CurrentPublication,
) -> dict[str, object]:
    run_timestamp = _utc(publication.manifest.get("run_timestamp"), "Loop B run timestamp")
    authoritative_timestamp = (
        _utc(publication.receipt.get("promoted_at"), "Loop B promoted_at")
        if publication.receipt is not None
        else run_timestamp
    )
    age = max(pd.Timedelta(0), now - authoritative_timestamp)
    intelligence = pd.read_parquet(publication.run_directory / "intelligence.parquet")
    observed_symbols = set(intelligence["symbol"].astype("string").str.upper())
    observed_horizons = set(intelligence["horizon"].astype("string").str.lower())
    expected_routes = len(set(symbols)) * len(INTERNAL_HORIZON_ORDER)
    failures = []
    warnings = []
    if age > _EXPECTED_PUBLICATION_AGE:
        failures.append("publication-stale")
    elif age > _LOOP_B_PUBLICATION_WARNING_AGE:
        warnings.append("publication-approaching-stale")
    if observed_symbols != set(symbols):
        failures.append("symbol-scope-mismatch")
    if observed_horizons != set(INTERNAL_HORIZON_ORDER):
        failures.append("horizon-scope-mismatch")
    if len(intelligence) != expected_routes:
        failures.append("route-count-mismatch")
    status = _FAIL if failures else (_WARN if warnings else _PASS)
    return _check(
        "loop_b_publication",
        status,
        (
            "Loop B has a fresh verified all-route prediction publication."
            if status == _PASS
            else (
                "Loop B's verified publication is approaching its hard "
                "freshness limit."
                if status == _WARN
                else (
                    "Loop B's current prediction publication is incomplete "
                    "or stale."
                )
            )
        ),
        run_path=publication.run_directory.relative_to(root).as_posix(),
        run_timestamp=run_timestamp.isoformat(),
        authoritative_timestamp=authoritative_timestamp.isoformat(),
        freshness_basis=(
            "receipt_promoted_at"
            if publication.receipt is not None
            else "legacy_run_timestamp"
        ),
        age_minutes=_minutes(age),
        intelligence_rows=len(intelligence),
        expected_routes=expected_routes,
        failures=failures,
        warnings=warnings,
    )


def _pricing_check(
    root: Path,
    *,
    now: pd.Timestamp,
    expected_target: pd.Timestamp,
    market_actionable: bool,
) -> dict[str, object]:
    settling = market_actionable and now - expected_target <= _TARGET_SETTLE_GRACE
    target_pointer = target_outcome_pointer_path(root)
    target_view: dict[str, object]
    if target_pointer.is_file():
        target = read_current_target_outcome(root)
        prediction_rows = len(target.predictions(include_proof=False))
        shadow_rows = len(target.shadow_predictions())
        target_view = {
            "target_snapshot_for": target.target_snapshot_for.isoformat(),
            "published_at": target.published_at.isoformat(),
            "terminal_status": target.terminal_status,
            "prediction_rows": prediction_rows,
            "shadow_rows": shadow_rows,
        }
        current_target = target.target_snapshot_for == expected_target
        predictive = target.terminal_status in {
            "PREDICTIONS_PUBLISHED",
            "MIXED_TERMINAL",
        }
        if current_target and predictive and prediction_rows and shadow_rows:
            status = _PASS
            summary = "Pricing has exact-target baseline and sidecar prediction authority."
        elif current_target:
            status = _WARN
            summary = "Pricing owns the latest target but ended without complete predictions."
        elif settling:
            status = _INFO
            summary = "The latest Pricing target is still inside its settle window."
        elif market_actionable:
            status = _FAIL
            summary = "Pricing target authority is behind the actionable regular target."
        else:
            status = _INFO
            summary = "Market is closed; Pricing retains its last verified target authority."
    else:
        target_view = {"status": "MISSING"}
        if market_actionable and not settling:
            status = _FAIL
            summary = "No target-scoped Pricing authority exists for an actionable session."
        else:
            status = _INFO
            summary = "No Pricing target exists yet; closed/settling cycles do not backdate one."

    full_view: dict[str, object]
    try:
        full = read_current_option_pricing_publication(root)
        full_timestamp = _utc(full.receipt.get("published_at"), "Pricing published_at")
        full_view = {
            "status": "VERIFIED",
            "run_path": full.run_directory.relative_to(root).as_posix(),
            "published_at": full_timestamp.isoformat(),
            "age_minutes": _minutes(max(pd.Timedelta(0), now - full_timestamp)),
        }
    except Exception as exc:
        full_view = {"status": "MISSING_OR_INVALID", "reason": _error_text(exc)}
    return _check(
        "pricing_publications",
        status,
        summary,
        expected_target=expected_target.isoformat(),
        settling=settling,
        target_authority=target_view,
        full_generation=full_view,
    )


def _strategy_check(
    root: Path,
    now: pd.Timestamp,
    *,
    publication: StrategyPublication,
) -> dict[str, object]:
    run_timestamp = _utc(publication.manifest.get("run_timestamp"), "Strategy run timestamp")
    age = max(pd.Timedelta(0), now - run_timestamp)
    candidates = pd.read_parquet(publication.run_directory / "strategy-candidates.parquet")
    audit = pd.read_parquet(publication.run_directory / "strategy-audit.parquet")
    status = _PASS if age <= _EXPECTED_PUBLICATION_AGE and not candidates.empty else _FAIL
    return _check(
        "strategy_publication",
        status,
        (
            "Strategy has a fresh verified candidate publication."
            if status == _PASS
            else "Strategy's candidate publication is empty or stale."
        ),
        run_path=publication.run_directory.relative_to(root).as_posix(),
        run_timestamp=run_timestamp.isoformat(),
        age_minutes=_minutes(age),
        candidate_rows=len(candidates),
        audit_rows=len(audit),
    )


def _strategy_candidate_value_check(
    root: Path,
    *,
    publication: StrategyPublication,
) -> dict[str, object]:
    source = publication.run_directory / "strategy-candidates.parquet"
    analysis = dict(summarize_strategy_candidate_values(pd.read_parquet(source)))
    status = str(analysis.pop("status"))
    summary = str(analysis.pop("summary"))
    return _check(
        "strategy_candidate_value_sanity",
        status,
        summary,
        source=source.relative_to(root).as_posix(),
        **analysis,
    )


def _strategy_value_shadow_check(
    root: Path,
    *,
    now: pd.Timestamp,
    publication: StrategyPublication,
) -> dict[str, object]:
    """Verify the latest scheduled shadow receipt without treating it as authority."""

    # Local import avoids a module cycle: the challenger reuses this module's
    # candidate-value audit, while the monitor only needs its receipt reader.
    from ml.strategy_value_challenger import (
        read_current_strategy_value_challenger,
        strategy_value_source_fingerprint,
    )

    try:
        current_fingerprint = strategy_value_source_fingerprint(
            root,
            strategy_publication=publication,
        )
        shadow = read_current_strategy_value_challenger(root)
    except Exception as exc:
        return _check(
            "strategy_value_shadow",
            _WARN,
            "The scheduled Strategy value shadow has no valid current receipt.",
            scope="SHADOW_OBSERVABILITY_ONLY",
            production_authority=False,
            reason=_error_text(exc),
        )
    created_at = _utc(shadow.report.get("created_at"), "shadow created_at")
    age = max(pd.Timedelta(0), now - created_at)
    observed_fingerprint = str(
        shadow.report.get("source_fingerprint_sha256", "")
    )
    source_current = observed_fingerprint == current_fingerprint
    latest_required_wake = _latest_completed_strategy_value_shadow_wake(now)
    receipt_covers_latest_stage = (
        latest_required_wake is None or created_at >= latest_required_wake
    )
    if source_current:
        status = _PASS
        summary = "Strategy value shadow has a current checksum-valid receipt."
    elif receipt_covers_latest_stage:
        status = _INFO
        summary = (
            "Strategy value shadow is awaiting its next stage-gated fingerprint run."
        )
    else:
        status = _WARN
        summary = "Strategy value shadow is behind its scheduled source fingerprint."
    horizon_health: dict[str, object] = {}
    raw_horizons = shadow.report.get("horizons")
    if isinstance(raw_horizons, Mapping):
        for horizon in ("1d", "1w"):
            raw = raw_horizons.get(horizon)
            if isinstance(raw, Mapping):
                horizon_health[horizon] = {
                    "model_data_feature_health": raw.get(
                        "model_data_feature_health"
                    ),
                    "challenger_promotion_status": raw.get(
                        "challenger_promotion_status"
                    ),
                }
    return _check(
        "strategy_value_shadow",
        status,
        summary,
        scope="SHADOW_OBSERVABILITY_ONLY",
        production_authority=False,
        run_path=shadow.directory.relative_to(root).as_posix(),
        receipt_path=shadow.receipt_path.relative_to(root).as_posix(),
        created_at=created_at.isoformat(),
        age_minutes=_minutes(age),
        source_fingerprint_current=source_current,
        source_fingerprint_sha256=observed_fingerprint,
        latest_required_stage_wake=(
            latest_required_wake.isoformat()
            if latest_required_wake is not None
            else None
        ),
        receipt_covers_latest_stage=receipt_covers_latest_stage,
        decision=shadow.report.get("decision"),
        promotion_eligible=shadow.report.get("promotion_eligible"),
        promotion_performed=False,
        orders_placed=0,
        horizons=horizon_health,
    )


def _latest_completed_strategy_value_shadow_wake(
    now: pd.Timestamp,
) -> pd.Timestamp | None:
    """Return the latest stage-14 wake whose 45-minute window has completed."""

    shadow_stage_index = next(
        index
        for index, stage in enumerate(OVERNIGHT_ACCURACY_STAGES)
        if stage.stage_id == "run-shadow-ablation"
    )
    shadow_stage_hour = _OVERNIGHT_LOCAL_HOURS[shadow_stage_index]
    local_now = now.tz_convert("America/Los_Angeles")
    local_date = local_now.tz_localize(None).normalize()
    calendar = _xnys_monitor_calendar(
        local_date - pd.Timedelta(days=14),
        local_date + pd.Timedelta(days=1),
    )
    completed_wakes: list[pd.Timestamp] = []
    for session in calendar.sessions:
        session_date = pd.Timestamp(session).tz_localize(None).normalize()
        wake_date = session_date + pd.Timedelta(
            days=1 if shadow_stage_hour <= 5 else 0
        )
        wake_local = wake_date.tz_localize("America/Los_Angeles") + pd.Timedelta(
            hours=shadow_stage_hour,
            minutes=42,
        )
        if wake_local + pd.Timedelta(minutes=45) <= local_now:
            completed_wakes.append(wake_local)
    if not completed_wakes:
        return None
    return max(completed_wakes).tz_convert("UTC")


def summarize_strategy_candidate_values(frame: pd.DataFrame) -> dict[str, object]:
    """Audit candidate value integrity and report unusual payoff-tail profiles."""

    required = {
        "symbol",
        "horizon",
        "candidate_key",
        "candidate_rank",
        "strategy_name",
        "model_status",
        "scenario_coverage_score",
        "calibrated_profit_probability",
        "expected_net_profit",
        "expected_return_on_risk",
        "capital_required",
        "max_profit",
        "max_loss",
    }
    missing = sorted(required.difference(frame.columns))
    common = {
        "policy_version": _STRATEGY_VALUE_AUDIT_POLICY_VERSION,
        "candidate_rows": len(frame),
        "read_only": True,
        "automated_action": "REPORT_ONLY_NO_MODEL_OR_CANDIDATE_MUTATION",
        "interpretation": (
            "Expected Return is return on risk, not a probability, so values above "
            "100% can be valid. Alerts identify payoff-tail dependence or cross-model "
            "coherence questions; they do not clamp, rerank, or delete candidates."
        ),
    }
    if missing:
        return {
            "status": _FAIL,
            "summary": (
                "Strategy candidate value sanity cannot verify its required fields."
            ),
            **common,
            "missing_columns": missing,
        }
    if frame.empty:
        return {
            "status": _FAIL,
            "summary": (
                "Strategy candidate value sanity cannot inspect an empty publication."
            ),
            **common,
            "missing_columns": [],
        }

    numeric = {
        column: pd.to_numeric(frame[column], errors="coerce").astype(float)
        for column in (
            "candidate_rank",
            "scenario_coverage_score",
            "calibrated_profit_probability",
            "expected_net_profit",
            "expected_return_on_risk",
            "capital_required",
            "max_profit",
            "max_loss",
        )
    }

    def finite(values: pd.Series) -> pd.Series:
        return pd.Series(
            np.isfinite(values.to_numpy(dtype=float)),
            index=values.index,
            dtype=bool,
        )

    scenario = numeric["scenario_coverage_score"]
    probability = numeric["calibrated_profit_probability"]
    expected_profit = numeric["expected_net_profit"]
    expected_return = numeric["expected_return_on_risk"]
    capital = numeric["capital_required"]
    maximum_profit = numeric["max_profit"]
    maximum_loss = numeric["max_loss"]
    model_status = frame["model_status"].astype("string").fillna("")
    modeled = model_status.eq("MODEL_FIT")
    heuristic = model_status.eq("HEURISTIC_ONLY")

    finite_scenario = finite(scenario)
    finite_probability = finite(probability)
    finite_expected_profit = finite(expected_profit)
    finite_expected_return = finite(expected_return)
    finite_capital = finite(capital)
    finite_maximum_loss = finite(maximum_loss)
    finite_maximum_profit = finite(maximum_profit)

    route_identity = (
        frame["symbol"].astype("string").fillna("").str.strip()
        + "|"
        + frame["horizon"].astype("string").fillna("").str.strip()
        + "|"
        + frame["candidate_key"].astype("string").fillna("").str.strip()
    )
    invalid_route_identity = (
        route_identity.str.startswith("|")
        | route_identity.str.contains(r"\|\|", regex=True)
        | route_identity.str.endswith("|")
    )
    invalid_rank = ~finite(numeric["candidate_rank"]) | numeric[
        "candidate_rank"
    ].lt(1.0) | ~numeric["candidate_rank"].mod(1.0).eq(0.0)
    invalid_model_status = ~(modeled | heuristic)
    invalid_scenario = ~finite_scenario | ~scenario.between(0.0, 1.0)
    invalid_modeled_probability = modeled & (
        ~finite_probability | ~probability.between(0.0, 1.0)
    )
    heuristic_probability_present = heuristic & frame[
        "calibrated_profit_probability"
    ].notna()
    nonfinite_expected_profit = ~finite_expected_profit
    nonfinite_expected_return = ~finite_expected_return
    invalid_capital = ~finite_capital | capital.le(0.0)
    invalid_maximum_loss = ~finite_maximum_loss | maximum_loss.lt(0.0)

    formula_eligible = (
        finite_expected_profit
        & finite_expected_return
        & finite_capital
        & capital.gt(0.0)
    )
    formula_close = pd.Series(False, index=frame.index, dtype=bool)
    formula_close.loc[formula_eligible] = np.isclose(
        expected_profit.loc[formula_eligible].to_numpy(dtype=float),
        (
            expected_return.loc[formula_eligible]
            * capital.loc[formula_eligible]
        ).to_numpy(dtype=float),
        rtol=_STRATEGY_FORMULA_RELATIVE_TOLERANCE,
        atol=_STRATEGY_FORMULA_ABSOLUTE_TOLERANCE,
    )
    formula_mismatch = formula_eligible & ~formula_close

    lower_tolerance = (
        _STRATEGY_FORMULA_ABSOLUTE_TOLERANCE
        + _STRATEGY_FORMULA_RELATIVE_TOLERANCE * maximum_loss.abs()
    )
    expected_below_loss_bound = (
        finite_expected_profit
        & finite_maximum_loss
        & expected_profit.lt(-maximum_loss - lower_tolerance)
    )
    upper_tolerance = (
        _STRATEGY_FORMULA_ABSOLUTE_TOLERANCE
        + _STRATEGY_FORMULA_RELATIVE_TOLERANCE * maximum_profit.abs()
    )
    expected_above_profit_bound = (
        finite_expected_profit
        & finite_maximum_profit
        & expected_profit.gt(maximum_profit + upper_tolerance)
    )

    integrity_masks = {
        "invalid_route_identity": invalid_route_identity,
        "invalid_candidate_rank": invalid_rank,
        "invalid_model_status": invalid_model_status,
        "invalid_scenario_coverage": invalid_scenario,
        "invalid_modeled_probability": invalid_modeled_probability,
        "heuristic_probability_present": heuristic_probability_present,
        "nonfinite_expected_net_profit": nonfinite_expected_profit,
        "nonfinite_expected_return_on_risk": nonfinite_expected_return,
        "invalid_capital_required": invalid_capital,
        "invalid_max_loss": invalid_maximum_loss,
        "expected_return_formula_mismatch": formula_mismatch,
        "expected_profit_below_loss_bound": expected_below_loss_bound,
        "expected_profit_above_profit_bound": expected_above_profit_bound,
    }
    integrity_union = pd.concat(integrity_masks, axis=1).any(axis=1)

    route_symbol = frame["symbol"].astype("string").fillna("").str.upper().str.strip()
    route_horizon = frame["horizon"].astype("string").fillna("").str.lower().str.strip()
    grouping = [route_symbol, route_horizon]
    route_median = expected_return.groupby(grouping, dropna=False).transform("median")
    route_mad = expected_return.groupby(grouping, dropna=False).transform(
        lambda values: (values - values.median()).abs().median()
    )
    route_robust_z = (
        0.6744897501960817
        * (expected_return - route_median)
        / route_mad.where(route_mad.gt(0.0))
    )
    route_return_outlier = (
        finite_expected_return
        & expected_return.ge(_STRATEGY_ROUTE_OUTLIER_MINIMUM_RETURN)
        & route_robust_z.ge(_STRATEGY_ROUTE_ROBUST_Z)
    )

    modeled_valid = (
        modeled
        & finite_probability
        & probability.between(0.0, 1.0)
        & finite_expected_return
    )
    positive_probability = modeled_valid & probability.gt(0.0)
    payoff_floor = pd.Series(np.nan, index=frame.index, dtype=float)
    payoff_floor.loc[positive_probability] = (
        expected_return.loc[positive_probability].clip(lower=0.0)
        / probability.loc[positive_probability]
    )
    positive_return_zero_probability = (
        modeled_valid & probability.eq(0.0) & expected_return.gt(0.0)
    )
    tail_payoff_dependency = positive_probability & payoff_floor.ge(
        _STRATEGY_TAIL_PAYOFF_FLOOR
    )
    high_return_low_probability = (
        modeled_valid
        & expected_return.ge(_STRATEGY_HIGH_RETURN_LOW_PROBABILITY_RETURN)
        & probability.le(_STRATEGY_LOW_PROFIT_PROBABILITY)
    )
    extreme_expected_return = finite_expected_return & expected_return.ge(
        _STRATEGY_EXPECTED_RETURN_EXTREME
    )
    alert_masks = {
        "positive_return_with_zero_probability": positive_return_zero_probability,
        "tail_payoff_dependency": tail_payoff_dependency,
        "high_return_low_probability": high_return_low_probability,
        "extreme_expected_return": extreme_expected_return,
        "route_return_outlier": route_return_outlier,
    }
    alert_union = pd.concat(alert_masks, axis=1).any(axis=1)

    priority = (
        positive_return_zero_probability.astype(int) * 6
        + high_return_low_probability.astype(int) * 5
        + tail_payoff_dependency.astype(int) * 4
        + extreme_expected_return.astype(int) * 3
        + route_return_outlier.astype(int) * 2
    )

    def rounded(values: pd.Series, index: object, digits: int) -> float | None:
        value = values.loc[index]
        return round(float(value), digits) if math.isfinite(float(value)) else None

    def integer_or_none(values: pd.Series, index: object) -> int | None:
        value = values.loc[index]
        return int(value) if math.isfinite(float(value)) else None

    def clean_text(value: object) -> str | None:
        if value is None or value is pd.NA or pd.isna(value):
            return None
        text = str(value).strip()
        return text or None

    ranked_findings = frame.loc[alert_union].copy()
    ranked_findings["__priority"] = priority.loc[alert_union]
    ranked_findings["__payoff_floor"] = payoff_floor.loc[alert_union]
    ranked_findings["__expected_return"] = expected_return.loc[alert_union]
    ranked_findings = ranked_findings.sort_values(
        ["__priority", "__payoff_floor", "__expected_return", "candidate_key"],
        ascending=[False, False, False, True],
        na_position="last",
        kind="mergesort",
    ).head(_STRATEGY_VALUE_AUDIT_TOP_FINDINGS)
    top_findings: list[dict[str, object]] = []
    for index, row in ranked_findings.iterrows():
        rules = [name for name, mask in alert_masks.items() if bool(mask.loc[index])]
        display_name = (
            row.get("strategy_display_name")
            if "strategy_display_name" in frame.columns
            else None
        )
        top_findings.append(
            {
                "symbol": clean_text(row.get("symbol")),
                "horizon": clean_text(row.get("horizon")),
                "strategy": clean_text(display_name)
                or clean_text(row.get("strategy_name")),
                "candidate_rank": integer_or_none(numeric["candidate_rank"], index),
                "candidate_key": clean_text(row.get("candidate_key")),
                "rules": rules,
                "ml_profit_probability_percent": rounded(probability * 100.0, index, 4),
                "scenario_coverage_percent": rounded(scenario * 100.0, index, 4),
                "expected_return_percent": rounded(expected_return * 100.0, index, 3),
                "expected_net_profit": rounded(expected_profit, index, 4),
                "capital_required": rounded(capital, index, 4),
                "implied_profitable_return_floor_x": rounded(payoff_floor, index, 3),
                "route_return_robust_z": rounded(route_robust_z, index, 3),
            }
        )

    def percentile_percent(values: pd.Series, quantile: float) -> float | None:
        clean = values[finite(values)]
        return round(float(clean.quantile(quantile)) * 100.0, 4) if len(clean) else None

    by_horizon: list[dict[str, object]] = []
    for horizon in sorted(set(route_horizon)):
        horizon_mask = route_horizon.eq(horizon)
        modeled_horizon = horizon_mask & modeled_valid
        by_horizon.append(
            {
                "horizon": horizon,
                "candidate_rows": int(horizon_mask.sum()),
                "modeled_rows": int(modeled_horizon.sum()),
                "ml_probability_median_percent": percentile_percent(
                    probability.loc[modeled_horizon], 0.5
                ),
                "expected_return_p99_percent": percentile_percent(
                    expected_return.loc[horizon_mask], 0.99
                ),
                "expected_return_max_percent": percentile_percent(
                    expected_return.loc[horizon_mask], 1.0
                ),
                "alert_rows": int((horizon_mask & alert_union).sum()),
            }
        )

    formula_error = (expected_profit - expected_return * capital).abs()
    eligible_formula_error = formula_error.loc[formula_eligible]
    signal_gap = (scenario - probability).abs()
    integrity_failure_rows = int(integrity_union.sum())
    alert_rows = int(alert_union.sum())
    if integrity_failure_rows:
        status = _FAIL
        summary = (
            f"{integrity_failure_rows} Strategy candidate row(s) "
            "violate value-integrity or payoff-bound contracts."
        )
    elif alert_rows:
        status = _WARN
        summary = (
            f"{alert_rows} Strategy candidate row(s) have mathematically allowed but "
            "tail-dependent or route-outlier profiles that need model review."
        )
    else:
        status = _PASS
        summary = (
            "Strategy candidate values pass integrity and payoff-tail sanity checks."
        )

    return {
        "status": status,
        "summary": summary,
        **common,
        "modeled_rows": int(modeled.sum()),
        "heuristic_rows": int(heuristic.sum()),
        "integrity_failure_rows": integrity_failure_rows,
        "integrity_failure_counts": {
            name: int(mask.sum()) for name, mask in integrity_masks.items()
        },
        "formula_max_absolute_error": (
            round(float(eligible_formula_error.max()), 12)
            if len(eligible_formula_error)
            else None
        ),
        "alert_rows": alert_rows,
        "alert_counts": {name: int(mask.sum()) for name, mask in alert_masks.items()},
        "context_counts": {
            "modeled_probability_at_or_below_2_percent": int(
                (modeled_valid & probability.le(0.02)).sum()
            ),
            "model_scenario_signal_gap_at_least_50_points": int(
                (modeled_valid & signal_gap.ge(0.50)).sum()
            ),
        },
        "thresholds": {
            "extreme_expected_return_on_risk": _STRATEGY_EXPECTED_RETURN_EXTREME,
            "high_return_low_probability_return": (
                _STRATEGY_HIGH_RETURN_LOW_PROBABILITY_RETURN
            ),
            "low_profit_probability": _STRATEGY_LOW_PROFIT_PROBABILITY,
            "implied_profitable_return_floor_x": _STRATEGY_TAIL_PAYOFF_FLOOR,
            "route_outlier_minimum_return": _STRATEGY_ROUTE_OUTLIER_MINIMUM_RETURN,
            "route_robust_z": _STRATEGY_ROUTE_ROBUST_Z,
        },
        "expected_return_distribution_percent": {
            "minimum": percentile_percent(expected_return, 0.0),
            "median": percentile_percent(expected_return, 0.5),
            "p95": percentile_percent(expected_return, 0.95),
            "p99": percentile_percent(expected_return, 0.99),
            "maximum": percentile_percent(expected_return, 1.0),
        },
        "modeled_probability_distribution_percent": {
            "minimum": percentile_percent(probability.loc[modeled_valid], 0.0),
            "median": percentile_percent(probability.loc[modeled_valid], 0.5),
            "p95": percentile_percent(probability.loc[modeled_valid], 0.95),
            "maximum": percentile_percent(probability.loc[modeled_valid], 1.0),
        },
        "by_horizon": by_horizon,
        "top_findings": top_findings,
        "recommended_follow_up": [
            (
                "Compare the listed payoff-tail candidates with realized outcomes "
                "before promotion."
            ),
            (
                "Check expected-return target scale and capital denominators for "
                "recurring route outliers."
            ),
            (
                "Use daily/weekly evaluation gates for retraining or promotion; keep "
                "hourly handling report-only."
            ),
        ],
    }


def _strategy_profit_model_check(root: Path) -> dict[str, object]:
    loaded = {
        horizon: load_promoted_strategy_model(root, horizon=horizon)
        for horizon in ("1d", "1w")
    }
    missing = [horizon for horizon, value in loaded.items() if value is None]
    status = _WARN if missing else _PASS
    return _check(
        "strategy_profit_model_authority",
        status,
        (
            "Daily/weekly Strategy profit model authority is unavailable."
            if missing
            else "Daily/weekly Strategy profit models and receipts verify."
        ),
        missing_horizons=missing,
        models={
            horizon: (
                {
                    "canonical_horizon": value.canonical_horizon,
                    "artifact_directory": str(value.model.artifact_directory),
                    "promotion_gate": dict(
                        value.report.get("promotion_gate", {})
                    ),
                }
                if value is not None
                else None
            )
            for horizon, value in loaded.items()
        },
        orders_placed=0,
    )


def _lineage_check(
    root: Path,
    *,
    loop_b: CurrentPublication,
    strategy: StrategyPublication,
) -> dict[str, object]:
    configuration = strategy.manifest.get("configuration")
    source = configuration.get("source_loop_b") if isinstance(configuration, Mapping) else None
    if not isinstance(source, Mapping):
        raise ValueError("Strategy manifest has no Loop B source record")
    relative = _safe_relative_path(source.get("run_path"), label="Strategy Loop B source")
    source_run = (root / relative).resolve()
    if source_run.parent != (root / "ml" / "runs").resolve():
        raise ValueError("Strategy Loop B source escapes immutable runs")
    source_manifest = source_run / "manifest.json"
    source_receipt = source_run / "publication.json"
    valid = (
        source.get("manifest_checksum_sha256") == file_checksum(source_manifest)
        and source.get("receipt_checksum_sha256") == file_checksum(source_receipt)
    )
    current_record = loop_b.pointer.get("current")
    current_source = isinstance(current_record, Mapping) and dict(source) == dict(current_record)
    lag = _utc(loop_b.manifest.get("run_timestamp"), "current Loop B timestamp") - _utc(
        source.get("run_timestamp"), "Strategy source timestamp"
    )
    if not valid:
        status = _FAIL
        summary = "Strategy's Loop B lineage hashes do not verify."
    elif lag > _EXPECTED_PUBLICATION_AGE:
        status = _WARN
        summary = "Strategy is consuming a verified but overly old Loop B generation."
    else:
        status = _PASS
        summary = "Strategy is bound to a verified current/recent Loop B generation."
    return _check(
        "cross_loop_lineage",
        status,
        summary,
        source_loop_b_run=relative.as_posix(),
        current_loop_b_run=loop_b.run_directory.relative_to(root).as_posix(),
        source_is_current=current_source,
        lag_minutes=max(0.0, _minutes(lag)),
        checksums_valid=valid,
    )


def _ui_check(
    root: Path,
    now: pd.Timestamp,
    symbols: Sequence[str],
    *,
    loop_b: CurrentPublication,
    strategy_publication: StrategyPublication,
) -> dict[str, object]:
    forecast = load_forecast_dashboard(
        loop_b.run_directory / "intelligence.parquet",
        loaded_at=now.to_pydatetime(),
    )
    strategy = load_strategy_candidates(
        strategy_publication.run_directory / "strategy-candidates.parquet",
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="read-only system monitor",
            synced_at=now.to_pydatetime(),
            status="read-only monitor",
        ),
        loaded_at=now.to_pydatetime(),
    )
    forecast_parity = _forecast_ui_value_parity(
        pd.read_parquet(forecast.source_path),
        forecast,
    )
    strategy_parity = _strategy_ui_value_parity(
        pd.read_parquet(strategy.source_path),
        strategy,
    )
    expected_routes = len(set(symbols)) * len(INTERNAL_HORIZON_ORDER)
    forecast_ok = (
        forecast.source_row_count == expected_routes
        and forecast.published_route_count == expected_routes
    )
    heuristic_actionable = sum(
        candidate.manual_order_actionable
        and candidate.score_basis == "Scenario Coverage"
        for candidate in strategy.candidates
    )
    if heuristic_actionable:
        raise ValueError("The UI made a heuristic Strategy row manually actionable")
    status = _PASS if forecast_ok and strategy.candidates else _FAIL
    return _check(
        "ui_contracts",
        status,
        (
            "Both production UI adapters load and preserve authoritative "
            "output values and visibility semantics."
            if status == _PASS
            else "A production UI adapter returned incomplete current output."
        ),
        forecast_source=str(forecast.source_path),
        forecast_rows=forecast.source_row_count,
        forecast_published_routes=forecast.published_route_count,
        forecast_actionable_routes=forecast.actionable_route_count,
        forecast_operational_statuses=list(forecast.operational_statuses),
        strategy_source=str(strategy.source_path),
        strategy_candidates=len(strategy.candidates),
        strategy_symbols=list(strategy.symbols),
        manually_actionable_candidates=sum(
            candidate.manual_order_actionable for candidate in strategy.candidates
        ),
        heuristic_manually_actionable_candidates=heuristic_actionable,
        forecast_value_parity=forecast_parity,
        strategy_value_parity=strategy_parity,
        visual_render_checked=False,
        visual_render_scope=(
            "Backend contract check only; the dedicated overnight UI stage may "
            "inspect an already-open Duckets desktop window."
        ),
    )


def _forecast_ui_value_parity(
    frame: pd.DataFrame,
    forecast: object,
) -> dict[str, object]:
    """Prove published-value parity and deterministic effective UI state."""

    loaded_at = _utc(
        getattr(forecast, "loaded_at", None),
        "forecast UI loaded-at timestamp",
    )

    raw_rows: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in frame.to_dict("records"):
        key = (
            str(row.get("symbol") or "").strip().upper(),
            str(row.get("horizon") or "").strip().lower(),
        )
        if not all(key) or key in raw_rows:
            raise ValueError("Forecast UI parity found a blank or duplicate source route")
        raw_rows[key] = row

    visible_routes: dict[tuple[str, str], object] = {}
    for symbol_view in getattr(forecast, "symbols", ()):
        for route in getattr(symbol_view, "all_routes", ()):
            if bool(getattr(route, "is_missing", False)):
                continue
            key = (
                str(getattr(route, "symbol", "")).strip().upper(),
                str(getattr(route, "horizon", "")).strip().lower(),
            )
            if not all(key) or key in visible_routes:
                raise ValueError("Forecast UI parity found a blank or duplicate adapter route")
            visible_routes[key] = route

    missing = sorted(set(raw_rows).difference(visible_routes))
    extra = sorted(set(visible_routes).difference(raw_rows))
    intentionally_omitted: list[tuple[str, str]] = []
    for key in missing:
        row = raw_rows[key]
        source_up = _optional_finite_ui_number(
            row.get("probability_up"),
            label=f"{key[0]} {key[1]} omitted source probability up",
        )
        source_down = _optional_finite_ui_number(
            row.get("probability_down"),
            label=f"{key[0]} {key[1]} omitted source probability down",
        )
        if (
            key[1].startswith("1w-d")
            and str(row.get("actionability_status") or "") == "NOT_ACTIONABLE"
            and str(row.get("intelligence_status") or "")
            == "NOT_APPLICABLE_TO_REMAINING_WEEK"
            and source_up is None
            and source_down is None
        ):
            intentionally_omitted.append(key)
    unexpected_missing = sorted(set(missing).difference(intentionally_omitted))
    if unexpected_missing or extra:
        raise ValueError(
            "Forecast UI route parity failed: "
            f"missing={unexpected_missing[:5]!r}, extra={extra[:5]!r}"
        )

    displayed_routes = 0
    intentionally_hidden_routes = 0
    published_status_routes_verified = 0
    effective_lifecycle_routes_verified = 0
    wall_clock_transition_routes = 0
    expected_actionable_routes = 0
    expected_automated_action_allowed = False
    for key, route in visible_routes.items():
        row = raw_rows[key]
        if str(getattr(route, "id", "") or "") != str(row.get("id") or ""):
            raise ValueError(f"Forecast UI ID mismatch for {key[0]} {key[1]}")

        expected_lifecycle = _expected_forecast_ui_lifecycle(
            row,
            horizon=key[1],
            loaded_at=loaded_at,
        )
        source_actionability = expected_lifecycle[
            "published_actionability_status"
        ]
        source_intelligence = expected_lifecycle["published_intelligence_status"]
        source_automated_action_allowed = expected_lifecycle[
            "published_automated_action_allowed"
        ]
        if (
            getattr(route, "published_actionability_status", None)
            != source_actionability
        ):
            raise ValueError(
                f"Forecast UI published actionability mismatch for {key[0]} {key[1]}"
            )
        if (
            getattr(route, "published_intelligence_status", None)
            != source_intelligence
        ):
            raise ValueError(
                f"Forecast UI published intelligence mismatch for {key[0]} {key[1]}"
            )
        if (
            getattr(route, "published_automated_action_allowed", None)
            is not source_automated_action_allowed
        ):
            raise ValueError(
                f"Forecast UI published automation mismatch for {key[0]} {key[1]}"
            )
        published_status_routes_verified += 1

        if getattr(route, "actionability_status", None) != expected_lifecycle[
            "actionability_status"
        ]:
            raise ValueError(
                f"Forecast UI effective actionability mismatch for {key[0]} {key[1]}"
            )
        if getattr(route, "intelligence_status", None) != expected_lifecycle[
            "intelligence_status"
        ]:
            raise ValueError(
                f"Forecast UI effective intelligence mismatch for {key[0]} {key[1]}"
            )
        if (
            getattr(route, "automated_action_allowed", None)
            is not expected_lifecycle["automated_action_allowed"]
        ):
            raise ValueError(
                f"Forecast UI effective automation mismatch for {key[0]} {key[1]}"
            )
        expected_is_actionable = (
            expected_lifecycle["actionability_status"] == "ACTIONABLE"
        )
        if (
            bool(getattr(route, "is_actionable", False))
            is not expected_is_actionable
        ):
            raise ValueError(
                f"Forecast UI effective actionability flag mismatch for {key[0]} {key[1]}"
            )
        expected_actionable_routes += int(expected_is_actionable)
        expected_automated_action_allowed = (
            expected_automated_action_allowed
            or expected_lifecycle["automated_action_allowed"] is True
        )
        effective_lifecycle_routes_verified += 1
        wall_clock_transition_routes += int(
            bool(expected_lifecycle["wall_clock_transition"])
        )

        source_up = _optional_finite_ui_number(
            row.get("probability_up"),
            label=f"{key[0]} {key[1]} source probability up",
        )
        source_down = _optional_finite_ui_number(
            row.get("probability_down"),
            label=f"{key[0]} {key[1]} source probability down",
        )
        visible_up = _optional_finite_ui_number(
            getattr(route, "probability_up", None),
            label=f"{key[0]} {key[1]} UI probability up",
        )
        visible_down = _optional_finite_ui_number(
            getattr(route, "probability_down", None),
            label=f"{key[0]} {key[1]} UI probability down",
        )
        if (visible_up is None) != (visible_down is None):
            raise ValueError(f"Forecast UI exposed a partial probability for {key}")
        should_show = bool(expected_lifecycle["probability_visible"])
        if should_show and visible_up is None:
            raise ValueError(f"Forecast UI hid a current probability for {key}")
        if visible_up is None:
            intentionally_hidden_routes += 1
            continue
        if source_up is None or source_down is None:
            raise ValueError(f"Forecast UI invented a probability for {key}")
        if not (
            math.isclose(visible_up, source_up, abs_tol=1e-12)
            and math.isclose(visible_down, source_down, abs_tol=1e-12)
        ):
            raise ValueError(f"Forecast UI probability value mismatch for {key}")
        if not (
            0.0 <= visible_up <= 1.0
            and 0.0 <= visible_down <= 1.0
            and math.isclose(visible_up + visible_down, 1.0, abs_tol=1e-8)
        ):
            raise ValueError(f"Forecast UI exposed an invalid probability pair for {key}")
        displayed_routes += 1

    if getattr(forecast, "actionable_route_count", None) != expected_actionable_routes:
        raise ValueError("Forecast UI effective actionable-route count mismatch")
    if (
        getattr(forecast, "automated_action_allowed", None)
        is not expected_automated_action_allowed
    ):
        raise ValueError("Forecast UI effective automation banner mismatch")

    published_operational_statuses = tuple(
        sorted(
            {
                status
                for status in (
                    _optional_ui_text(row.get("operational_status"))
                    for row in raw_rows.values()
                )
                if status is not None
            }
        )
    )
    if (
        tuple(getattr(forecast, "operational_statuses", ()))
        != published_operational_statuses
    ):
        raise ValueError("Forecast UI published operational-status mismatch")
    _verify_forecast_ui_banner_parity(forecast)

    return {
        "status": "PASS",
        "source_routes": len(raw_rows),
        "adapter_routes": len(visible_routes),
        "calendar_inapplicable_routes_intentionally_omitted": len(
            intentionally_omitted
        ),
        "probability_routes_displayed": displayed_routes,
        "probability_routes_intentionally_hidden": intentionally_hidden_routes,
        "published_status_routes_verified": published_status_routes_verified,
        "effective_lifecycle_routes_verified": effective_lifecycle_routes_verified,
        "wall_clock_transition_routes": wall_clock_transition_routes,
        "effective_actionable_routes": expected_actionable_routes,
        "effective_automated_action_allowed": expected_automated_action_allowed,
        "probability_scale": "0_TO_1_FORMATTED_AS_PERCENT",
    }


def _expected_forecast_ui_lifecycle(
    row: Mapping[str, object],
    *,
    horizon: str,
    loaded_at: pd.Timestamp,
) -> dict[str, object]:
    """Derive the only wall-clock transition the UI may apply to a row."""

    published_actionability = (
        _optional_ui_text(row.get("actionability_status"))
        or "STATUS_UNAVAILABLE"
    )
    published_intelligence = (
        _optional_ui_text(row.get("intelligence_status"))
        or "INTELLIGENCE_STATUS_UNAVAILABLE"
    )
    published_automation = _optional_ui_bool(
        row.get("automated_action_allowed"),
        label=f"{horizon} published automated-action flag",
    )
    actionability = published_actionability
    intelligence = published_intelligence
    automated_action_allowed = published_automation
    wall_clock_transition = False
    derived_in_progress = False

    target_start = _optional_ui_timestamp(row.get("target_window_start"))
    target_end = _optional_ui_timestamp(row.get("target_window_end"))
    actionable_until = _optional_ui_timestamp(row.get("actionable_until"))
    forecast_created_at = _optional_ui_timestamp(row.get("forecast_created_at"))
    actionable_lifecycle_timestamps_valid = (
        forecast_created_at is not None
        and actionable_until is not None
        and target_start is not None
        and target_end is not None
        and forecast_created_at <= loaded_at
        and forecast_created_at < actionable_until
        and actionable_until <= target_start
        and target_start < target_end
    )

    if horizon in STANDARD_HORIZON_ORDER and published_actionability == "ACTIONABLE":
        if not actionable_lifecycle_timestamps_valid:
            actionability = "TARGET_TIMESTAMP_INVALID"
            automated_action_allowed = False
            wall_clock_transition = True
        elif loaded_at >= target_end:
            actionability = "TARGET_WINDOW_PASSED"
            automated_action_allowed = False
            wall_clock_transition = True
        elif loaded_at >= actionable_until:
            actionability = "TARGET_WINDOW_STARTED"
            intelligence = "FORECAST_IN_PROGRESS"
            automated_action_allowed = False
            wall_clock_transition = True
            derived_in_progress = True
    elif (
        horizon in STANDARD_HORIZON_ORDER
        and published_actionability == "TARGET_WINDOW_STARTED"
        and published_intelligence == "FORECAST_IN_PROGRESS"
        and actionable_lifecycle_timestamps_valid
        and loaded_at >= target_end
    ):
        actionability = "TARGET_WINDOW_PASSED"
        automated_action_allowed = False
        wall_clock_transition = True

    trusted_in_progress = (
        horizon in STANDARD_HORIZON_ORDER
        and actionability == "TARGET_WINDOW_STARTED"
        and intelligence == "FORECAST_IN_PROGRESS"
        and automated_action_allowed is False
        and forecast_created_at is not None
        and actionable_until is not None
        and target_start is not None
        and target_end is not None
        and actionable_lifecycle_timestamps_valid
        and (
            target_start <= loaded_at < target_end
            or (
                derived_in_progress
                and actionable_until <= loaded_at < target_end
            )
        )
    )
    frozen_weekly_probability = (
        horizon in WEEKLY_HORIZON_ORDER
        and actionability == "FROZEN_WEEKLY_SNAPSHOT"
        and _optional_ui_text(row.get("target_definition_version"))
        == INTERNAL_HORIZON_SPECIFICATIONS[horizon].target_definition_version
    )
    probability_visible = (
        actionability == "ACTIONABLE"
        or frozen_weekly_probability
        or trusted_in_progress
    )
    return {
        "published_actionability_status": published_actionability,
        "published_intelligence_status": published_intelligence,
        "published_automated_action_allowed": published_automation,
        "actionability_status": actionability,
        "intelligence_status": intelligence,
        "automated_action_allowed": automated_action_allowed,
        "wall_clock_transition": wall_clock_transition,
        "probability_visible": probability_visible,
    }


def _verify_forecast_ui_banner_parity(forecast: object) -> None:
    standard_routes = tuple(
        route
        for symbol in getattr(forecast, "symbols", ())
        for route in getattr(symbol, "routes", ())
        if getattr(route, "horizon", None) in STANDARD_HORIZON_ORDER
    )
    has_route_gaps = any(
        bool(getattr(route, "is_missing", False))
        or getattr(route, "probability_up", None) is None
        or getattr(route, "probability_down", None) is None
        for route in standard_routes
    )
    statuses = tuple(getattr(forecast, "operational_statuses", ()))
    expected_freshness = _expected_forecast_freshness(statuses)
    expected_operational = _expected_forecast_operational_summary(statuses)
    if has_route_gaps and expected_freshness[1] == "success":
        expected_freshness = ("Current Outlooks with Route Gaps", "warning")
    if has_route_gaps and expected_operational[1] == "success":
        expected_operational = (
            "Operational with Route Timing Gaps",
            "warning",
        )
    actual_freshness = (
        getattr(forecast, "freshness_label", None),
        getattr(forecast, "freshness_tone", None),
    )
    actual_operational = (
        getattr(forecast, "operational_label", None),
        getattr(forecast, "operational_tone", None),
    )
    if actual_freshness != expected_freshness:
        raise ValueError("Forecast UI effective freshness banner mismatch")
    if actual_operational != expected_operational:
        raise ValueError("Forecast UI effective operational banner mismatch")

    automation_enabled = bool(
        getattr(forecast, "automated_action_allowed", False)
    )
    expected_automation = (
        (
            "Automation flag reported on; this dashboard remains read-only",
            "danger",
        )
        if automation_enabled
        else ("Automated action is off", "neutral")
    )
    actual_automation = (
        getattr(forecast, "automation_label", None),
        getattr(forecast, "automation_tone", None),
    )
    if actual_automation != expected_automation:
        raise ValueError("Forecast UI effective automation label mismatch")


def _expected_forecast_freshness(
    statuses: tuple[str, ...],
) -> tuple[str, str]:
    if not statuses:
        return "No Forecast Data", "neutral"
    if "REFRESH_FAILED" in statuses:
        return "Latest Refresh Failed", "danger"
    if "REFRESH_IN_PROGRESS" in statuses:
        return "Refresh in Progress", "warning"
    has_stale = any("STALE" in status for status in statuses)
    has_current = any(
        status in {"OPERATIONAL", "OPERATIONALLY_CURRENT"}
        for status in statuses
    )
    if has_stale and has_current:
        return "Current Outlooks with Route Gaps", "warning"
    if has_stale:
        return "Data Is Stale", "danger"
    if all(
        status in {"OPERATIONAL", "OPERATIONALLY_CURRENT"}
        for status in statuses
    ):
        return "Data Pipeline Is Current", "success"
    return "Data Pipeline Has Limitations", "warning"


def _expected_forecast_operational_summary(
    statuses: tuple[str, ...],
) -> tuple[str, str]:
    if not statuses:
        return "Operational Status Unavailable", "neutral"
    if "REFRESH_FAILED" in statuses:
        return "Refresh Failed", "danger"
    if "REFRESH_IN_PROGRESS" in statuses:
        return "Refreshing Current Output", "warning"
    has_stale = any("STALE" in status for status in statuses)
    has_current = any(
        status in {"OPERATIONAL", "OPERATIONALLY_CURRENT"}
        for status in statuses
    )
    if has_stale and has_current:
        return "Operational with Route Timing Gaps", "warning"
    if has_stale:
        return "Operational Data Is Stale", "danger"
    if all(
        status in {"OPERATIONAL", "OPERATIONALLY_CURRENT"}
        for status in statuses
    ):
        return "Operationally Current", "success"
    return "Operational with Limitations", "warning"


def _strategy_ui_value_parity(
    frame: pd.DataFrame,
    strategy: object,
) -> dict[str, object]:
    """Prove that strategy display units and rows match the publication."""

    raw_rows = {
        str(row.get("id") or ""): row for row in frame.to_dict("records")
    }
    if "" in raw_rows or len(raw_rows) != len(frame):
        raise ValueError("Strategy UI parity found a blank or duplicate candidate ID")
    candidates = {
        str(getattr(candidate, "candidate_id", "") or ""): candidate
        for candidate in getattr(strategy, "candidates", ())
    }
    if "" in candidates or len(candidates) != len(
        tuple(getattr(strategy, "candidates", ()))
    ):
        raise ValueError("Strategy UI parity found a blank or duplicate adapter ID")
    if set(raw_rows) != set(candidates):
        missing = sorted(set(raw_rows).difference(candidates))
        extra = sorted(set(candidates).difference(raw_rows))
        raise ValueError(
            "Strategy UI candidate parity failed: "
            f"missing={missing[:5]!r}, extra={extra[:5]!r}"
        )

    calibrated = 0
    scenario_only = 0
    for candidate_id, row in raw_rows.items():
        candidate = candidates[candidate_id]
        checks = (
            (
                "direction probability",
                getattr(candidate, "direction_probability_up", None),
                row.get("direction_probability_up"),
                100.0,
            ),
            (
                "scenario coverage",
                getattr(candidate, "scenario_coverage", None),
                row.get("scenario_coverage_score"),
                100.0,
            ),
            (
                "expected return",
                getattr(candidate, "expected_return", None),
                row.get("expected_return_on_risk"),
                1.0,
            ),
            (
                "expected net profit",
                getattr(candidate, "expected_net_profit", None),
                row.get("expected_net_profit"),
                1.0,
            ),
        )
        for label, visible, source, scale in checks:
            visible_number = _optional_finite_ui_number(
                visible,
                label=f"{candidate_id} UI {label}",
            )
            source_number = _optional_finite_ui_number(
                source,
                label=f"{candidate_id} source {label}",
            )
            if visible_number is None or source_number is None or not math.isclose(
                visible_number,
                source_number * scale,
                rel_tol=1e-12,
                abs_tol=1e-10,
            ):
                raise ValueError(f"Strategy UI {label} mismatch for {candidate_id}")

        source_probability = _optional_finite_ui_number(
            row.get("decision_score"),
            label=f"{candidate_id} source calibrated probability",
        )
        visible_probability = _optional_finite_ui_number(
            getattr(candidate, "predictive_score", None),
            label=f"{candidate_id} UI calibrated probability",
        )
        if source_probability is None:
            if visible_probability is not None:
                raise ValueError(
                    f"Strategy UI invented calibrated probability for {candidate_id}"
                )
            scenario_only += 1
        else:
            if visible_probability is None or not math.isclose(
                visible_probability,
                source_probability * 100.0,
                rel_tol=1e-12,
                abs_tol=1e-10,
            ):
                raise ValueError(
                    f"Strategy UI calibrated probability mismatch for {candidate_id}"
                )
            calibrated += 1

    return {
        "status": "PASS",
        "source_candidates": len(raw_rows),
        "adapter_candidates": len(candidates),
        "calibrated_probability_candidates": calibrated,
        "scenario_coverage_only_candidates": scenario_only,
        "probability_scale": "SOURCE_0_TO_1_ADAPTER_PERCENT_POINTS",
        "expected_return_scale": "SOURCE_FRACTION_FORMATTED_AS_PERCENT",
    }


def _optional_finite_ui_number(value: object, *, label: str) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} is non-finite")
    return number


def _optional_ui_bool(value: object, *, label: str) -> bool | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise ValueError(f"{label} is not Boolean")


def _optional_ui_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _optional_ui_timestamp(value: object) -> pd.Timestamp | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(timestamp) else pd.Timestamp(timestamp)


def _storage_check(root: Path) -> dict[str, object]:
    usage = shutil.disk_usage(root)
    free_ratio = usage.free / usage.total if usage.total else 0.0
    log_roots = (root / "logs" / "ducketz", root / "runtime-logs")
    log_bytes = sum(
        path.stat().st_size
        for log_root in log_roots
        if log_root.is_dir()
        for path in log_root.glob("**/*")
        if path.is_file()
    )
    if usage.free < 10 * 1024**3 or free_ratio < 0.03:
        status = _FAIL
        summary = "Datastore disk capacity is critically low."
    elif usage.free < 25 * 1024**3 or free_ratio < 0.08 or log_bytes > 50 * 1024**3:
        status = _WARN
        summary = "Datastore capacity or log growth needs attention."
    else:
        status = _PASS
        summary = "Datastore capacity and aggregate log size are within guardrails."
    return _check(
        "storage_capacity",
        status,
        summary,
        total_gib=round(usage.total / 1024**3, 2),
        free_gib=round(usage.free / 1024**3, 2),
        free_percent=round(free_ratio * 100.0, 2),
        log_gib=round(log_bytes / 1024**3, 3),
    )


def _alfred_full_check(root: Path) -> dict[str, object]:
    from datafetching.fred_alfred_readiness import read_verified_macro_evidence

    evidence = read_verified_macro_evidence(root)
    coverage = evidence.readiness.coverage
    horizons = coverage.get("horizons") if isinstance(coverage, Mapping) else None
    feature_coverages: list[float] = []
    failed_features: list[str] = []
    if isinstance(horizons, Mapping):
        for horizon, raw_horizon in horizons.items():
            features = (
                raw_horizon.get("features")
                if isinstance(raw_horizon, Mapping)
                else None
            )
            if not isinstance(features, Mapping):
                continue
            for feature, raw_feature in features.items():
                if not isinstance(raw_feature, Mapping):
                    continue
                value = _finite_or_none(raw_feature.get("coverage"))
                if value is not None:
                    feature_coverages.append(value)
                if raw_feature.get("status") != "PASS":
                    failed_features.append(f"{horizon}|{feature}")
    return _check(
        "alfred_full_evidence",
        _FAIL if failed_features else _PASS,
        (
            "Full ALFRED vintage lineage and causal-readiness evidence verify."
            if not failed_features
            else "One or more ALFRED feature-coverage gates failed."
        ),
        verified_at=evidence.readiness.verified_at.isoformat(),
        vintage_rows=len(evidence.vintages),
        release_context_rows=len(evidence.release_context),
        source_file_count=len(evidence.source_files),
        readiness_status=coverage.get("status"),
        minimum_required_coverage=coverage.get("minimum_coverage"),
        observed_minimum_coverage=(min(feature_coverages) if feature_coverages else None),
        observed_maximum_coverage=(max(feature_coverages) if feature_coverages else None),
        lookahead_violation_count=coverage.get("lookahead_violation_count"),
        horizon_count=len(horizons) if isinstance(horizons, Mapping) else 0,
        feature_gate_count=len(feature_coverages),
        failed_features=failed_features,
    )


def _directional_quality_check(
    root: Path,
    *,
    publication: CurrentPublication,
) -> dict[str, object]:
    monitoring = pd.read_parquet(publication.run_directory / "monitoring.parquet")
    evaluations = pd.read_parquet(publication.run_directory / "evaluations.parquet")
    summary = summarize_directional_quality(monitoring, evaluations)
    return _check(
        "directional_prediction_quality",
        str(summary.pop("status")),
        str(summary.pop("summary")),
        **summary,
    )


def summarize_directional_quality(
    monitoring: pd.DataFrame,
    evaluations: pd.DataFrame,
) -> dict[str, object]:
    """Summarize published offline quality separately from immature live evidence."""

    required_monitoring = {
        "category",
        "metric_name",
        "scope_type",
        "scope_value",
        "status",
        "observed_value",
        "reference_value",
        "evidence_row_count",
    }
    missing = sorted(required_monitoring.difference(monitoring.columns))
    if missing:
        raise ValueError("Directional monitoring is missing: " + ", ".join(missing))
    if evaluations.empty:
        return {
            "status": _FAIL,
            "summary": "No directional evaluation rows were published.",
            "evaluation_rows": 0,
        }
    horizons = set(evaluations["horizon"].astype("string").str.lower())
    missing_horizons = sorted(set(INTERNAL_HORIZON_ORDER).difference(horizons))
    performance = monitoring.loc[
        monitoring["category"].astype("string").eq("performance")
        & monitoring["scope_type"].astype("string").eq("horizon")
    ].copy()
    key_metrics = {
        "accuracy_at_0_5",
        "mean_brier_score",
        "mean_log_loss",
        "roc_auc",
        "calibration_gap",
        "observed_positive_rate",
        "mean_calibrated_probability",
    }
    metrics_by_horizon: dict[str, dict[str, object]] = {}
    warnings: list[dict[str, object]] = []
    for row in performance.to_dict("records"):
        metric = str(row.get("metric_name"))
        horizon = str(row.get("scope_value"))
        if metric not in key_metrics:
            continue
        value = _finite_or_none(row.get("observed_value"))
        status = str(row.get("status"))
        metrics_by_horizon.setdefault(horizon, {})[metric] = value
        if status != "OK":
            warnings.append(
                {
                    "horizon": horizon,
                    "metric": metric,
                    "status": status,
                    "observed": value,
                    "reference": _finite_or_none(row.get("reference_value")),
                    "evidence_rows": int(row.get("evidence_row_count", 0)),
                }
            )
    live = monitoring.loc[
        monitoring["category"].astype("string").eq("live_evidence")
        & monitoring["metric_name"].astype("string").eq("completed_live_forecasts")
    ]
    live_status_counts = {
        str(key): int(value)
        for key, value in live["status"].value_counts(dropna=False).items()
    }
    live_completed = int(
        pd.to_numeric(live["observed_value"], errors="coerce").fillna(0).sum()
    )
    uncertainty_by_horizon = _directional_uncertainty_by_horizon(evaluations)
    if missing_horizons:
        status = _FAIL
        text = "Directional evaluations are missing one or more production horizons."
    elif warnings:
        status = _WARN
        text = "Offline directional evaluation is complete but quality warnings are present."
    else:
        status = _PASS
        text = "Offline directional evaluation passes all published quality references."
    return {
        "status": status,
        "summary": text,
        "evidence_scope": "OFFLINE_BACKTEST_AND_AVAILABLE_EVALUATIONS",
        "evaluation_rows": len(evaluations),
        "missing_horizons": missing_horizons,
        "metrics_by_horizon": metrics_by_horizon,
        "uncertainty_by_horizon": uncertainty_by_horizon,
        "quality_warnings": warnings,
        "live_evidence": {
            "completed_forecasts": live_completed,
            "status_counts": live_status_counts,
            "interpretation": (
                "INSUFFICIENT_LIVE_LABELS"
                if live_completed == 0
                else "LIVE_LABELS_AVAILABLE"
            ),
        },
    }


def _directional_uncertainty_by_horizon(
    evaluations: pd.DataFrame,
    *,
    bootstrap_replicates: int = 300,
) -> dict[str, object]:
    """Cluster-bootstrap the same published horizon cohorts, deterministically."""

    required = {
        "horizon",
        "evaluation_status",
        "target_window_start",
        "observed_target",
        "calibrated_probability",
        "prediction_correct_0_5",
        "brier_score",
        "log_loss",
    }
    missing = sorted(required.difference(evaluations.columns))
    if missing:
        return {
            horizon: {
                "status": "UNAVAILABLE_MISSING_COLUMNS",
                "missing_columns": missing,
            }
            for horizon in INTERNAL_HORIZON_ORDER
        }
    evaluated = evaluations.loc[
        evaluations["evaluation_status"]
        .astype("string")
        .str.upper()
        .eq("EVALUATED")
    ].copy()
    output: dict[str, object] = {}
    for horizon_index, horizon in enumerate(INTERNAL_HORIZON_ORDER):
        cohort = evaluated.loc[
            evaluated["horizon"].astype("string").str.lower().eq(horizon)
        ].copy()
        cohort["target_window_start"] = pd.to_datetime(
            cohort["target_window_start"], utc=True, errors="coerce"
        )
        cohort = cohort.dropna(subset=["target_window_start"])
        clusters = [
            group.reset_index(drop=True)
            for _target, group in cohort.groupby("target_window_start", sort=True)
        ]
        if not clusters:
            output[horizon] = {
                "status": "UNAVAILABLE_NO_EVALUATED_CLUSTERS",
                "evaluated_rows": len(cohort),
                "independent_target_clusters": 0,
            }
            continue
        point = _directional_cohort_metrics(cohort)
        rng = np.random.default_rng(73_000 + horizon_index)
        samples: dict[str, list[float]] = {
            metric: [] for metric in point if point[metric] is not None
        }
        for _iteration in range(bootstrap_replicates):
            selected = rng.integers(0, len(clusters), size=len(clusters))
            resample = pd.concat(
                [clusters[int(index)] for index in selected],
                ignore_index=True,
            )
            metrics = _directional_cohort_metrics(resample)
            for metric in samples:
                value = metrics.get(metric)
                if value is not None and math.isfinite(float(value)):
                    samples[metric].append(float(value))
        intervals: dict[str, object] = {}
        for metric, point_value in point.items():
            draws = samples.get(metric, [])
            intervals[metric] = {
                "point": point_value,
                "lower_95": (
                    round(float(np.quantile(draws, 0.025)), 12) if draws else None
                ),
                "upper_95": (
                    round(float(np.quantile(draws, 0.975)), 12) if draws else None
                ),
                "successful_replicates": len(draws),
            }
        output[horizon] = {
            "status": "AVAILABLE",
            "method": "TARGET_WINDOW_CLUSTER_BOOTSTRAP_PERCENTILE_95",
            "bootstrap_replicates": bootstrap_replicates,
            "evaluated_rows": len(cohort),
            "independent_target_clusters": len(clusters),
            "intervals": intervals,
        }
    return output


def _directional_cohort_metrics(frame: pd.DataFrame) -> dict[str, float | None]:
    paired = frame.loc[
        :, ["observed_target", "calibrated_probability"]
    ].apply(pd.to_numeric, errors="coerce").dropna()
    auc: float | None = None
    calibration_gap: float | None = None
    if not paired.empty:
        labels = paired["observed_target"].astype(int)
        probabilities = paired["calibrated_probability"]
        calibration_gap = float(abs(probabilities.mean() - labels.mean()))
        positive_count = int(labels.eq(1).sum())
        negative_count = int(labels.eq(0).sum())
        if positive_count and negative_count:
            ranks = probabilities.rank(method="average")
            auc = float(
                (
                    ranks.loc[labels.eq(1)].sum()
                    - positive_count * (positive_count + 1) / 2.0
                )
                / (positive_count * negative_count)
            )
    return {
        "accuracy_at_0_5": _mean_or_none(frame["prediction_correct_0_5"]),
        "mean_brier_score": _mean_or_none(frame["brier_score"]),
        "mean_log_loss": _mean_or_none(frame["log_loss"]),
        "roc_auc": round(auc, 12) if auc is not None else None,
        "calibration_gap": (
            round(calibration_gap, 12) if calibration_gap is not None else None
        ),
        "observed_positive_rate": _mean_or_none(frame["observed_target"]),
        "mean_calibrated_probability": _mean_or_none(
            frame["calibrated_probability"]
        ),
    }


def _strategy_quality_check(
    root: Path,
    *,
    publication: StrategyPublication,
) -> dict[str, object]:
    candidates = pd.read_parquet(publication.run_directory / "strategy-candidates.parquet")
    reports = _read_json(publication.run_directory / "strategy-model-reports.json")
    summary = summarize_strategy_quality(candidates, reports)
    return _check(
        "strategy_prediction_quality",
        str(summary.pop("status")),
        str(summary.pop("summary")),
        **summary,
    )


def summarize_strategy_quality(
    candidates: pd.DataFrame,
    reports: Mapping[str, object],
) -> dict[str, object]:
    """Classify calibrated output, heuristic fallback, and label availability."""

    required = {
        "symbol",
        "horizon",
        "score_basis",
        "pricing_status",
        "pricing_source",
        "pricing_leg_coverage",
        "surface_quality_pass",
        "liquidity_policy_pass",
        "all_option_quotes_valid",
    }
    missing = sorted(required.difference(candidates.columns))
    if missing:
        raise ValueError("Strategy candidates are missing: " + ", ".join(missing))
    if candidates.empty:
        return {
            "status": _FAIL,
            "summary": "No Strategy candidate rows were published.",
            "candidate_rows": 0,
        }
    basis = candidates["score_basis"].astype("string")
    fitted = basis.isin(
        {
            BSGP_CALIBRATED_MODEL_SCORE_BASIS,
            BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
            OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
        }
    )
    heuristic = basis.eq(SCENARIO_COVERAGE_SCORE_BASIS)
    evidence = _strategy_score_evidence_masks(candidates)
    fully_priced = evidence["fully_priced"]
    opra_execution_scored = evidence["opra_execution_scored"]
    opra_execution_quality = evidence["opra_execution_quality"]
    calibrated_evidence_quality = evidence["calibrated_evidence_quality"]
    model_reports = reports.get("model_reports")
    if not isinstance(model_reports, Mapping):
        raise ValueError("Strategy model reports have no model_reports object")
    missing_horizons = sorted(set(INTERNAL_HORIZON_ORDER).difference(model_reports))
    complete_outcomes = sum(
        int(report.get("complete_outcome_rows", 0))
        for report in model_reports.values()
        if isinstance(report, Mapping)
    )
    report_statuses: dict[str, int] = {}
    exclusion_reasons: dict[str, int] = {}
    pricing_eligibility_by_horizon: dict[str, object] = {}
    total_pricing_eligible = 0
    total_pricing_excluded = 0
    for horizon, report in model_reports.items():
        if not isinstance(report, Mapping):
            continue
        value = str(report.get("status", "UNKNOWN"))
        report_statuses[value] = report_statuses.get(value, 0) + 1
        eligible_rows = int(report.get("pricing_eligible_outcome_rows", 0))
        excluded_rows = int(report.get("pricing_excluded_outcome_rows", 0))
        total_pricing_eligible += eligible_rows
        total_pricing_excluded += excluded_rows
        reasons = report.get("pricing_exclusion_reason_counts")
        if isinstance(reasons, Mapping):
            for reason, count in reasons.items():
                exclusion_reasons[str(reason)] = (
                    exclusion_reasons.get(str(reason), 0) + int(count)
                )
        pricing_eligibility_by_horizon[str(horizon)] = {
            "complete_outcome_rows": int(report.get("complete_outcome_rows", 0)),
            "pricing_eligible_outcome_rows": eligible_rows,
            "pricing_excluded_outcome_rows": excluded_rows,
            "usable_decision_clusters": int(
                report.get("usable_decision_clusters", 0)
            ),
            "required_decision_clusters": int(
                report.get("required_decision_clusters", 0)
            ),
            "pricing_exclusion_reason_counts": (
                {str(key): int(count) for key, count in reasons.items()}
                if isinstance(reasons, Mapping)
                else {}
            ),
        }
    calibrated_rows = int(fitted.sum())
    heuristic_rows = int(heuristic.sum())
    if missing_horizons:
        status = _FAIL
        text = "Strategy model evidence is missing production horizons."
    elif calibrated_rows == 0:
        status = _WARN
        text = "Strategy is publishing research-only Scenario Coverage, not calibrated probabilities."
    elif not bool((fitted & calibrated_evidence_quality).any()):
        status = _WARN
        text = (
            "Calibrated Strategy rows exist but none pass their declared "
            "pricing/execution and quality gates."
        )
    else:
        status = _PASS
        text = (
            "Strategy publishes calibrated, evidence-backed, "
            "quality-passing candidates."
        )
    return {
        "status": status,
        "summary": text,
        "candidate_rows": len(candidates),
        "route_count": int(candidates[["symbol", "horizon"]].drop_duplicates().shape[0]),
        "score_basis_counts": {
            str(key): int(value) for key, value in basis.value_counts().items()
        },
        "pricing_status_counts": {
            str(key): int(value)
            for key, value in candidates["pricing_status"].value_counts(dropna=False).items()
        },
        "calibrated_candidate_rows": calibrated_rows,
        "scenario_coverage_candidate_rows": heuristic_rows,
        "fully_priced_rows": int(fully_priced.sum()),
        "opra_execution_scored_rows": int(opra_execution_scored.sum()),
        "opra_execution_quality_passing_rows": int(
            opra_execution_quality.sum()
        ),
        "quality_passing_rows": int(calibrated_evidence_quality.sum()),
        "complete_observed_outcome_rows": complete_outcomes,
        "pricing_eligible_observed_outcome_rows": total_pricing_eligible,
        "pricing_excluded_observed_outcome_rows": total_pricing_excluded,
        "pricing_exclusion_reason_counts": dict(sorted(exclusion_reasons.items())),
        "pricing_eligibility_by_horizon": pricing_eligibility_by_horizon,
        "model_status_counts": report_statuses,
        "model_evidence_interpretation": (
            "INSUFFICIENT_OBSERVED_OPTION_OUTCOMES"
            if complete_outcomes == 0
            else (
                "NO_PRICING_ELIGIBLE_OBSERVED_OUTCOMES"
                if total_pricing_eligible == 0
                else (
                    "INSUFFICIENT_CHRONOLOGICAL_PRICING_CLUSTERS"
                    if any(
                        int(value.get("usable_decision_clusters", 0))
                        < int(value.get("required_decision_clusters", 0))
                        for value in pricing_eligibility_by_horizon.values()
                        if int(value.get("required_decision_clusters", 0)) > 0
                    )
                    else "PRICING_ELIGIBLE_OBSERVED_OUTCOMES_AVAILABLE"
                )
            )
        ),
        "missing_horizons": missing_horizons,
    }


def _weekly_evaluation_rollup_check(
    root: Path,
    *,
    now: pd.Timestamp,
    publication: CurrentPublication,
) -> dict[str, object]:
    evaluations_path = publication.run_directory / "evaluations.parquet"
    evaluations = pd.read_parquet(evaluations_path)
    completed_weeks = _completed_xnys_weeks(now, count=2)
    source = {
        "run_path": publication.run_directory.relative_to(root).as_posix(),
        "publication_receipt": str(publication.run_directory / "publication.json"),
        "publication_receipt_sha256": file_checksum(
            publication.run_directory / "publication.json"
        ),
        "evaluations_sha256": file_checksum(evaluations_path),
        "receipt_verified": publication.receipt is not None,
    }
    if len(completed_weeks) < 2:
        return _check(
            "weekly_evaluation_rollup",
            _INFO,
            "INSUFFICIENT_WEEKLY_EVIDENCE: two completed XNYS weeks are unavailable.",
            evidence_state="INSUFFICIENT_WEEKLY_EVIDENCE",
            source=source,
            completed_weeks=completed_weeks,
        )
    previous, current = completed_weeks
    summary = summarize_weekly_evidence(
        evaluations,
        previous_week_start=previous["week_start"],
        current_week_start=current["week_start"],
        minimum_observations=_WEEKLY_MINIMUM_INDEPENDENT_OBSERVATIONS,
    )
    status = str(summary.pop("status"))
    text = str(summary.pop("summary"))
    return _check(
        "weekly_evaluation_rollup",
        status,
        text,
        source=source,
        periods={"previous": previous, "current": current},
        **summary,
    )


def summarize_weekly_evidence(
    evaluations: pd.DataFrame,
    *,
    previous_week_start: object,
    current_week_start: object,
    minimum_observations: int = _WEEKLY_MINIMUM_INDEPENDENT_OBSERVATIONS,
) -> dict[str, object]:
    """Compare two completed exchange weeks without mixing contracts or maturity."""

    required = {
        "symbol",
        "provider",
        "horizon",
        "decision_timestamp",
        "target_window_start",
        "target_window_end",
        "prediction_created_at",
        "model_name",
        "model_version",
        "prediction_mode",
        "evaluation_status",
        "target_definition_version",
        "target_specification",
        "assumed_round_trip_cost",
        "observed_target",
        "calibrated_probability",
        "log_loss",
        "brier_score",
        "prediction_correct_0_5",
    }
    missing = sorted(required.difference(evaluations.columns))
    if missing:
        raise ValueError("Weekly evaluations are missing: " + ", ".join(missing))
    minimum = int(minimum_observations)
    if minimum < 1:
        raise ValueError("minimum_observations must be positive")
    previous_start = _week_start(previous_week_start)
    current_start = _week_start(current_week_start)
    if previous_start >= current_start:
        raise ValueError("Weekly comparison periods are not chronological")

    frame = evaluations.copy()
    for column in (
        "decision_timestamp",
        "target_window_start",
        "target_window_end",
        "prediction_created_at",
    ):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    frame = frame.loc[
        frame["prediction_mode"].astype("string").str.upper().eq("LIVE")
        & frame["target_window_end"].notna()
    ].copy()
    local_target_dates = (
        frame["target_window_end"]
        .dt.tz_convert("America/New_York")
        .dt.normalize()
        .dt.tz_localize(None)
    )
    frame["_week_start"] = local_target_dates - pd.to_timedelta(
        local_target_dates.dt.weekday, unit="D"
    )

    routes: dict[str, object] = {}
    comparable_horizons: list[str] = []
    state_counts: dict[str, int] = {}
    for horizon in INTERNAL_HORIZON_ORDER:
        route = frame.loc[frame["horizon"].astype("string").str.lower().eq(horizon)]
        previous_all = route.loc[route["_week_start"].eq(previous_start)]
        current_all = route.loc[route["_week_start"].eq(current_start)]
        previous_rows = _canonical_weekly_evaluations(previous_all)
        current_rows = _canonical_weekly_evaluations(current_all)
        previous_contracts = _weekly_contracts(previous_rows)
        current_contracts = _weekly_contracts(current_rows)
        previous_payload = _weekly_period_payload(previous_all, previous_rows)
        current_payload = _weekly_period_payload(current_all, current_rows)

        if len(previous_rows) < minimum or len(current_rows) < minimum:
            evidence_state = "INSUFFICIENT_WEEKLY_EVIDENCE"
            comparison = None
        elif (
            len(previous_contracts) != 1
            or len(current_contracts) != 1
            or previous_contracts != current_contracts
        ):
            evidence_state = "INCOMPATIBLE_WEEKLY_DEFINITIONS"
            comparison = None
        else:
            evidence_state = "COMPARABLE_WEEKLY_EVIDENCE"
            comparable_horizons.append(horizon)
            comparison = _weekly_metric_deltas(
                previous_payload["metrics"], current_payload["metrics"]
            )
        state_counts[evidence_state] = state_counts.get(evidence_state, 0) + 1
        routes[horizon] = {
            "evidence_state": evidence_state,
            "minimum_independent_observations": minimum,
            "previous": previous_payload,
            "current": current_payload,
            "previous_contracts": previous_contracts,
            "current_contracts": current_contracts,
            "comparison": comparison,
        }

    if comparable_horizons:
        status = _PASS
        evidence_state = "COMPARABLE_WEEKLY_EVIDENCE"
        text = (
            "Comparable immutable live evaluation evidence exists for "
            f"{len(comparable_horizons)} production horizon(s); non-comparable "
            "routes remain explicitly classified."
        )
    else:
        status = _INFO
        evidence_state = "INSUFFICIENT_WEEKLY_EVIDENCE"
        text = (
            "INSUFFICIENT_WEEKLY_EVIDENCE: no production horizon has two "
            "compatible completed XNYS weeks with enough independent live outcomes."
        )
    return {
        "status": status,
        "summary": text,
        "evidence_state": evidence_state,
        "evidence_scope": "IMMUTABLE_LIVE_EVALUATIONS_BY_COMPLETED_XNYS_WEEK",
        "previous_week_start": previous_start.strftime("%Y-%m-%d"),
        "current_week_start": current_start.strftime("%Y-%m-%d"),
        "minimum_independent_observations": minimum,
        "comparable_horizons": comparable_horizons,
        "state_counts": state_counts,
        "routes": routes,
    }


def _canonical_weekly_evaluations(frame: pd.DataFrame) -> pd.DataFrame:
    completed = frame.loc[
        frame["evaluation_status"].astype("string").str.upper().eq("EVALUATED")
    ].copy()
    if completed.empty:
        return completed
    return (
        completed.sort_values("prediction_created_at", kind="stable")
        .drop_duplicates(list(_WEEKLY_CLUSTER_COLUMNS), keep="last")
        .reset_index(drop=True)
    )


def _weekly_contracts(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    contracts = frame.loc[:, list(_WEEKLY_CONTRACT_COLUMNS)].drop_duplicates()
    return sorted(
        (
            {
                column: _jsonable(row[column])
                for column in _WEEKLY_CONTRACT_COLUMNS
            }
            for row in contracts.to_dict("records")
        ),
        key=lambda value: json.dumps(value, sort_keys=True, default=str),
    )


def _weekly_period_payload(
    all_rows: pd.DataFrame,
    completed_rows: pd.DataFrame,
) -> dict[str, object]:
    statuses = {
        str(key): int(value)
        for key, value in all_rows["evaluation_status"]
        .astype("string")
        .str.upper()
        .value_counts(dropna=False)
        .items()
    }
    versions = sorted(
        str(value)
        for value in completed_rows["model_version"].dropna().astype("string").unique()
    )
    return {
        "published_evaluation_rows": len(all_rows),
        "evaluation_status_counts": statuses,
        "independent_evaluated_observations": len(completed_rows),
        "model_versions": versions,
        "metrics": _weekly_metrics(completed_rows),
    }


def _weekly_metrics(frame: pd.DataFrame) -> dict[str, float | None]:
    return {
        "accuracy_at_0_5": _mean_or_none(frame["prediction_correct_0_5"]),
        "mean_brier_score": _mean_or_none(frame["brier_score"]),
        "mean_log_loss": _mean_or_none(frame["log_loss"]),
        "observed_positive_rate": _mean_or_none(frame["observed_target"]),
        "mean_calibrated_probability": _mean_or_none(
            frame["calibrated_probability"]
        ),
    }


def _weekly_metric_deltas(
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> dict[str, float | None]:
    return {
        f"{metric}_delta": _difference_or_none(current.get(metric), previous.get(metric))
        for metric in (
            "accuracy_at_0_5",
            "mean_brier_score",
            "mean_log_loss",
            "observed_positive_rate",
            "mean_calibrated_probability",
        )
    }


def _mean_or_none(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return round(float(numeric.mean()), 12) if not numeric.empty else None


def _difference_or_none(current: object, previous: object) -> float | None:
    current_value = _finite_or_none(current)
    previous_value = _finite_or_none(previous)
    if current_value is None or previous_value is None:
        return None
    return round(current_value - previous_value, 12)


def _week_start(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("America/New_York").tz_localize(None)
    timestamp = timestamp.normalize()
    return timestamp - pd.Timedelta(days=int(timestamp.weekday()))


def _completed_xnys_weeks(
    now: pd.Timestamp,
    *,
    count: int,
) -> list[dict[str, object]]:
    calendar = _xnys_monitor_calendar(
        now - pd.Timedelta(days=max(35, count * 14)),
        now + pd.Timedelta(days=7),
    )
    by_week: dict[pd.Timestamp, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for session in calendar.sessions:
        label = pd.Timestamp(session).tz_localize(None).normalize()
        start = label - pd.Timedelta(days=int(label.weekday()))
        close = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")
        by_week.setdefault(start, []).append((label, close))
    completed: list[dict[str, object]] = []
    for start, sessions in sorted(by_week.items()):
        final_session, final_close = max(sessions, key=lambda value: value[0])
        if now < final_close:
            continue
        completed.append(
            {
                "week_start": start.strftime("%Y-%m-%d"),
                "final_eligible_session": final_session.strftime("%Y-%m-%d"),
                "final_session_close": final_close.isoformat(),
                "session_count": len(sessions),
            }
        )
    return completed[-count:]


def _xnys_monitor_calendar(start: pd.Timestamp, end: pd.Timestamp):
    try:
        import exchange_calendars as xcals
    except ImportError as exc:  # pragma: no cover - required project dependency
        raise RuntimeError("exchange-calendars is required for Loops monitoring") from exc
    return xcals.get_calendar(
        "XNYS",
        start=start.date().isoformat(),
        end=end.date().isoformat(),
    )


def _pricing_strategy_canary_check(
    root: Path,
    *,
    now: pd.Timestamp,
    target: pd.Timestamp,
    symbols: Sequence[str],
    strategy_publication: StrategyPublication,
) -> dict[str, object]:
    try:
        result = run_canary(
            root,
            target_snapshot_for=target,
            symbols=symbols,
            timeout_seconds=0.0,
            clock=lambda: now.to_pydatetime(),
            strategy_publication=strategy_publication,
        )
    except Exception as exc:
        return _check(
            "pricing_strategy_canary",
            _WARN,
            "The latest regular target does not yet pass the read-only Pricing-to-Strategy canary.",
            target_snapshot_for=target.isoformat(),
            evidence_state="NOT_PROVEN",
            reason=_error_text(exc),
        )
    details = dict(result)
    canary_status = details.pop("status", None)
    return _check(
        "pricing_strategy_canary",
        _PASS,
        "The latest regular target passes the exact Pricing-to-Strategy canary.",
        canary_status=canary_status,
        **details,
    )


def _sequence_encoder_loop_c_check(
    root: Path,
    *,
    now: pd.Timestamp,
) -> dict[str, object]:
    """Monitor optional shadow sequence and Loop C observe authorities.

    Absence is informational until an authority has been published.  Once a
    pointer exists, corruption or any order-authority drift is a warning even
    though neither shadow component is part of production model authority.
    """

    sequence_pointer = sequence_pointer_path(root)
    loop_c_pointer = loop_c_pointer_path(root)
    paper_pointer = paper_ledger_pointer_path(root)
    if (
        not sequence_pointer.is_file()
        and not loop_c_pointer.is_file()
        and not paper_pointer.is_file()
    ):
        return _check(
            "sequence_encoder_loop_c",
            _INFO,
            "The pooled sequence encoder and Loop C observe lane are not yet published.",
            sequence_status="NOT_PUBLISHED",
            loop_c_status="NOT_PUBLISHED",
            paper_ledger_status="NOT_PUBLISHED",
            authority="NONE",
            automated_action_allowed=False,
            orders_enabled=False,
            orders_placed=0,
        )

    details: dict[str, object] = {
        "sequence_status": "NOT_PUBLISHED",
        "loop_c_status": "NOT_PUBLISHED",
        "paper_ledger_status": "NOT_PUBLISHED",
        "automated_action_allowed": False,
        "orders_enabled": False,
        "orders_placed": 0,
    }
    warnings: list[str] = []
    if sequence_pointer.is_file():
        try:
            publication = read_current_sequence_publication(root)
            distributions_path = resolve_current_sequence_output(
                root, "distributions.parquet"
            )
            distributions = pd.read_parquet(
                distributions_path,
                columns=[
                    "prediction_created_at",
                    "prediction_mode",
                    "prediction_status",
                    "automated_action_allowed",
                ],
            )
            if distributions["automated_action_allowed"].astype("boolean").fillna(True).any():
                raise SequencePublicationError(
                    "a sequence shadow row authorizes automated action"
                )
            published_at = _utc(publication.receipt.get("published_at"), "sequence published_at")
            details.update(
                {
                    "sequence_status": "VERIFIED_SHADOW",
                    "sequence_run": str(publication.run_directory),
                    "sequence_published_at": published_at.isoformat(),
                    "sequence_age_minutes": _minutes(now - published_at),
                    "sequence_distribution_rows": len(distributions),
                    "sequence_authority": publication.receipt.get("authority"),
                }
            )
        except Exception as exc:
            details["sequence_status"] = "INVALID"
            warnings.append(_error_text(exc))

    if paper_pointer.is_file():
        try:
            paper = read_current_paper_ledger(root)
            tracked_at = _utc(
                paper.report.get("tracked_at"), "Loop C paper-ledger tracked_at"
            )
            summary = paper.report.get("summary")
            if not isinstance(summary, Mapping):
                raise ValueError("Loop C paper ledger has no summary")
            details.update(
                {
                    "paper_ledger_status": "VERIFIED_OBSERVE_ONLY",
                    "paper_ledger_run": str(paper.run_directory),
                    "paper_ledger_tracked_at": tracked_at.isoformat(),
                    "paper_ledger_age_minutes": _minutes(now - tracked_at),
                    "paper_trade_count": int(summary.get("paper_trade_count", 0)),
                    "paper_mature_trade_count": int(
                        summary.get("mature_trade_count", 0)
                    ),
                    "paper_pending_trade_count": int(
                        summary.get("pending_trade_count", 0)
                    ),
                    "paper_open_trade_count": int(
                        summary.get("open_paper_trade_count", 0)
                    ),
                    "paper_open_gross_potential_share_obligation": summary.get(
                        "open_gross_potential_share_obligation", 0.0
                    ),
                    "paper_maximum_single_open_trade_gross_share_obligation": summary.get(
                        "maximum_single_open_trade_gross_share_obligation", 0.0
                    ),
                    "paper_open_potential_buy_share_obligation": summary.get(
                        "open_potential_buy_share_obligation", 0.0
                    ),
                    "paper_open_potential_sell_share_obligation": summary.get(
                        "open_potential_sell_share_obligation", 0.0
                    ),
                    "paper_earliest_open_option_expiration": summary.get(
                        "earliest_open_option_expiration"
                    ),
                    "paper_counterfactual_realized_net_pnl": summary.get(
                        "counterfactual_realized_net_pnl", 0.0
                    ),
                }
            )
        except Exception as exc:
            details["paper_ledger_status"] = "INVALID"
            warnings.append(_error_text(exc))

    if loop_c_pointer.is_file():
        try:
            publication = read_current_loop_c_publication(root)
            outputs = publication.manifest.get("output_files")
            if not isinstance(outputs, Mapping) or "decisions.parquet" not in outputs:
                raise LoopCPublicationError("Loop C decisions output is missing")
            decisions = pd.read_parquet(
                publication.run_directory / "decisions.parquet",
                columns=["automated_action_allowed", "orders_enabled", "orders_placed"],
            )
            if (
                decisions["automated_action_allowed"].astype("boolean").fillna(True).any()
                or decisions["orders_enabled"].astype("boolean").fillna(True).any()
                or pd.to_numeric(decisions["orders_placed"], errors="coerce").fillna(1).ne(0).any()
            ):
                raise LoopCPublicationError("Loop C observe output violates zero-order safety")
            published_at = _utc(publication.receipt.get("published_at"), "Loop C published_at")
            details.update(
                {
                    "loop_c_status": "VERIFIED_OBSERVE_ONLY",
                    "loop_c_run": str(publication.run_directory),
                    "loop_c_published_at": published_at.isoformat(),
                    "loop_c_age_minutes": _minutes(now - published_at),
                    "loop_c_decision_rows": len(decisions),
                    "loop_c_authority": publication.receipt.get("authority"),
                }
            )
        except Exception as exc:
            details["loop_c_status"] = "INVALID"
            warnings.append(_error_text(exc))

    if warnings:
        return _check(
            "sequence_encoder_loop_c",
            _WARN,
            "A published sequence-encoder, Loop C observe, or paper-ledger artifact is invalid.",
            warnings=warnings,
            **details,
        )
    status = _PASS if details["sequence_status"] == "VERIFIED_SHADOW" else _INFO
    if details["loop_c_status"] == "VERIFIED_OBSERVE_ONLY":
        summary = (
            "The pooled sequence encoder, Loop C observe, and paper ledger verify "
            "with zero order authority."
            if details["paper_ledger_status"] == "VERIFIED_OBSERVE_ONLY"
            else "The pooled sequence encoder and Loop C observe authority verify; the daily paper ledger is not yet published."
        )
    elif details["sequence_status"] == "VERIFIED_SHADOW":
        summary = "The pooled sequence encoder verifies; Loop C observe is not yet published."
    else:
        summary = (
            "The paper ledger verifies; the pooled sequence encoder and Loop C "
            "observe lane are not yet published."
        )
    return _check(
        "sequence_encoder_loop_c",
        status,
        summary,
        **details,
    )


def _windows_process_rows() -> tuple[Mapping[str, object], ...]:
    if os.name != "nt":
        raise RuntimeError("Production process discovery is implemented for Windows")
    modules = "|".join(re.escape(spec.module) for spec in RUNTIMES)
    script = rf"""
$pattern = '(?i)(?:^|\s)-m\s+({modules})(?:\s|$)'
$rows = Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -match $pattern }} | ForEach-Object {{
  [pscustomobject]@{{
    pid = [int]$_.ProcessId
    ppid = [int]$_.ParentProcessId
    created_at = if ($_.CreationDate) {{ $_.CreationDate.ToUniversalTime().ToString('o') }} else {{ $null }}
    command_line = [string]$_.CommandLine
  }}
}}
@($rows) | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown PowerShell error"
        raise RuntimeError(f"Windows process discovery failed: {detail}")
    payload = json.loads(completed.stdout or "[]")
    if isinstance(payload, Mapping):
        return (dict(payload),)
    if not isinstance(payload, list):
        raise RuntimeError("Windows process discovery returned an invalid payload")
    return tuple(dict(value) for value in payload if isinstance(value, Mapping))


def _safe(name: str, callback: Callable[[], Mapping[str, object]]) -> dict[str, object]:
    try:
        value = dict(callback())
        value["name"] = name
        return value
    except Exception as exc:
        return _check(
            name,
            _FAIL,
            "The read-only check could not verify its contract.",
            reason=_error_text(exc),
        )


def _safe_many(
    name: str,
    callback: Callable[[], Sequence[Mapping[str, object]]],
) -> list[dict[str, object]]:
    try:
        return [dict(value) for value in callback()]
    except Exception as exc:
        return [
            _check(
                name,
                _FAIL,
                "The read-only check group could not run.",
                reason=_error_text(exc),
            )
        ]


def _check(
    name: str,
    status: str,
    summary: str,
    **details: object,
) -> dict[str, object]:
    if status not in {_PASS, _INFO, _WARN, _FAIL}:
        raise ValueError(f"Unsupported check status: {status}")
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "details": _jsonable(details),
    }


def _overall_status(checks: Sequence[Mapping[str, object]]) -> str:
    statuses = {str(check.get("status")) for check in checks}
    if _FAIL in statuses:
        return "UNHEALTHY"
    if _WARN in statuses:
        return "DEGRADED"
    return "HEALTHY"


def _normalize_process(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "pid": int(row.get("pid", row.get("ProcessId", -1))),
        "ppid": int(row.get("ppid", row.get("ParentProcessId", -1))),
        "created_at": row.get("created_at", row.get("CreationDate")),
        "command_line": str(row.get("command_line", row.get("CommandLine", ""))),
    }


def _command_owns_module(command_line: object, module: str) -> bool:
    return bool(
        re.search(
            rf"(?i)(?:^|\s)-m\s+{re.escape(module)}(?:\s|$)",
            str(command_line),
        )
    )


def _normalize_spaces(value: str) -> str:
    return " ".join(value.split())


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON authority is unreadable: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON authority is not an object: {path}")
    return value


def _safe_relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} is not a safe relative path")
    return path


def _last_nonempty_line(path: Path) -> str | None:
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - 65536))
        text = handle.read().decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:500] if lines else None


def _finite_or_none(value: object) -> float | None:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return float(number) if pd.notna(number) else None


def _minutes(value: pd.Timedelta) -> float:
    return round(float(value / pd.Timedelta(minutes=1)), 3)


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.total_seconds()
    if value is pd.NA or (not isinstance(value, (str, bytes)) and pd.isna(value)):
        return None
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except (TypeError, ValueError):
            pass
    return value


def _error_text(exc: Exception) -> str:
    text = _normalize_spaces(str(exc))
    return f"{type(exc).__name__}: {text}"[:1000]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scheduled_monitor_context(value: datetime | None = None) -> dict[str, object]:
    """Select the monitor layer and deterministic overnight accuracy stage."""

    supplied = value or datetime.now().astimezone()
    if supplied.tzinfo is None:
        raise ValueError("scheduled monitor clock must be timezone-aware")
    local = pd.Timestamp(supplied).tz_convert("America/Los_Angeles")
    observed = local.tz_convert("UTC")
    base: dict[str, object] = {
        "schema_version": OVERNIGHT_SCHEDULE_SCHEMA_VERSION,
        "monitor_mode": "hourly",
        "lane": "STANDARD_OPERATIONS",
        "timezone": "America/Los_Angeles",
        "observed_at": observed.isoformat(),
        "local_observed_at": local.isoformat(),
        "session_date": None,
        "session_close": None,
        "session_close_local": None,
        "equity_actionable_close": None,
        "equity_actionable_close_local": None,
        "equity_actionable_day_complete": None,
        "next_session_open": None,
        "next_session_open_local": None,
        "final_eligible_session_of_week": None,
        "overnight_stage": None,
    }
    local_hour = int(local.hour)
    if local_hour not in _OVERNIGHT_LOCAL_HOURS:
        base["reason"] = "OUTSIDE_REGULAR_OVERNIGHT_STAGE_HOURS"
        return base

    session_date = local.tz_localize(None).normalize()
    if local_hour <= 5:
        session_date -= pd.Timedelta(days=1)
    calendar = _xnys_monitor_calendar(
        session_date - pd.Timedelta(days=7),
        session_date + pd.Timedelta(days=10),
    )
    sessions = [
        session
        for session in calendar.sessions
        if pd.Timestamp(session).tz_localize(None).normalize() == session_date
    ]
    if len(sessions) != 1:
        base["reason"] = "NO_ELIGIBLE_XNYS_SESSION_FOR_OVERNIGHT_WINDOW"
        return base
    session = sessions[0]
    close = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")
    if observed < close:
        base["reason"] = "ELIGIBLE_XNYS_SESSION_NOT_YET_CLOSED"
        return base

    week_start = session_date - pd.Timedelta(days=int(session_date.weekday()))
    week_end = week_start + pd.Timedelta(days=6)
    week_sessions = [
        candidate
        for candidate in calendar.sessions
        if week_start
        <= pd.Timestamp(candidate).tz_localize(None).normalize()
        <= week_end
    ]
    final_session = bool(week_sessions and session == max(week_sessions))
    opened = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
    opened_eastern = opened.tz_convert("America/New_York")
    close_eastern = close.tz_convert("America/New_York")
    standard_equity_day = (
        opened_eastern.hour,
        opened_eastern.minute,
        close_eastern.hour,
        close_eastern.minute,
    ) == (9, 30, 16, 0)
    equity_actionable_close = (
        close + pd.Timedelta(hours=4) if standard_equity_day else close
    )
    monitor_mode = "hourly"
    if local_hour == 14:
        monitor_mode = "weekly" if final_session else "daily"

    later_sessions = [candidate for candidate in calendar.sessions if candidate > session]
    next_open = (
        pd.Timestamp(calendar.session_open(min(later_sessions))).tz_convert("UTC")
        if later_sessions
        else None
    )
    stage_index = _OVERNIGHT_LOCAL_HOURS.index(local_hour)
    stage = OVERNIGHT_ACCURACY_STAGES[stage_index]
    scheduled_wake = local.normalize() + pd.Timedelta(
        hours=local_hour,
        minutes=42,
    )
    window_start = session_date.tz_localize("America/Los_Angeles") + pd.Timedelta(
        hours=13,
        minutes=42,
    )
    window_end = window_start + pd.Timedelta(hours=16)
    base.update(
        {
            "monitor_mode": monitor_mode,
            "lane": "OVERNIGHT_ACCURACY",
            "session_date": session_date.strftime("%Y-%m-%d"),
            "session_close": close.isoformat(),
            "session_close_local": close.tz_convert("America/Los_Angeles").isoformat(),
            "equity_actionable_close": equity_actionable_close.isoformat(),
            "equity_actionable_close_local": equity_actionable_close.tz_convert(
                "America/Los_Angeles"
            ).isoformat(),
            "equity_actionable_day_complete": observed >= equity_actionable_close,
            "next_session_open": next_open.isoformat() if next_open is not None else None,
            "next_session_open_local": (
                next_open.tz_convert("America/Los_Angeles").isoformat()
                if next_open is not None
                else None
            ),
            "final_eligible_session_of_week": final_session,
            "overnight_window_start_local": window_start.isoformat(),
            "overnight_window_end_local": window_end.isoformat(),
            "overnight_stage": {
                "index": stage_index + 1,
                "count": len(OVERNIGHT_ACCURACY_STAGES),
                "id": stage.stage_id,
                "title": stage.title,
                "objective": stage.objective,
                "action_scope": stage.action_scope,
                "scheduled_local_wake": scheduled_wake.isoformat(),
                "requires_healthy_baseline": True,
                "max_runtime_minutes": 45,
                "shadow_experiment_allowed": stage.shadow_experiment_allowed,
                "production_freeze": stage.production_freeze,
            },
            "reason": "ELIGIBLE_COMPLETED_XNYS_SESSION_OVERNIGHT_STAGE",
        }
    )
    return base


def scheduled_monitor_mode(value: datetime | None = None) -> str:
    """Select the scheduled monitor layer while preserving the legacy API."""

    return str(scheduled_monitor_context(value)["monitor_mode"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run read-only hourly, daily, or weekly checks across the Loops system."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    parser.add_argument(
        "--mode",
        choices=("hourly", "daily", "weekly", "scheduled"),
        required=True,
        help=(
            "Scheduled selects daily after an eligible XNYS close, weekly after "
            "the final eligible XNYS session of the week, and hourly otherwise."
        ),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON instead of the default indented report.",
    )
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    schedule = scheduled_monitor_context() if args.mode == "scheduled" else None
    selected_mode = str(schedule["monitor_mode"]) if schedule is not None else args.mode
    report = build_monitor_report(root, mode=selected_mode)
    if schedule is not None:
        report["requested_mode"] = "scheduled"
        report["schedule"] = schedule
    print(
        json.dumps(
            report,
            indent=None if args.compact else 2,
            sort_keys=True,
            default=str,
        )
    )
    return 2 if report["status"] == "UNHEALTHY" else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MONITOR_SCHEMA_VERSION",
    "OVERNIGHT_ACCURACY_STAGES",
    "OVERNIGHT_SCHEDULE_SCHEMA_VERSION",
    "RUNTIMES",
    "build_monitor_report",
    "main",
    "scheduled_monitor_context",
    "scheduled_monitor_mode",
    "summarize_directional_quality",
    "summarize_strategy_candidate_values",
    "summarize_strategy_quality",
    "summarize_weekly_evidence",
]
