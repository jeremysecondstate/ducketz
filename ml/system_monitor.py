from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from app.models.portfolio import PortfolioSnapshot
from app.ui.options_strategy_data import load_strategy_candidates
from app.ui.rolling_forecast_data import load_forecast_dashboard
from datafetching.bar_readiness import read_bar_readiness
from datafetching.decision_time import (
    cycle_target_decision,
    latest_eligible_option_target,
)
from datafetching.orchestrate import DEFAULT_WATCHLIST, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum
from ml.current_publication import read_current_publication
from ml.horizons import INTERNAL_HORIZON_ORDER
from ml.option_pricing.publication import read_current_option_pricing_publication
from ml.option_pricing.target_outcome import (
    read_current_target_outcome,
    target_outcome_pointer_path,
)
from ml.strategy_pricing_canary import run_canary
from ml.strategy_publication import read_current_strategy_publication
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_COVERAGE_SCORE_BASIS,
)
from options.publication import read_option_snapshot


MONITOR_SCHEMA_VERSION = "loops-system-monitor-v1"
_PASS = "PASS"
_INFO = "INFO"
_WARN = "WARN"
_FAIL = "FAIL"
_EXPECTED_PUBLICATION_AGE = pd.Timedelta(minutes=45)
_TARGET_SETTLE_GRACE = pd.Timedelta(minutes=14)
_RECENT_STDERR_WINDOW = pd.Timedelta(hours=2)


@dataclass(frozen=True)
class RuntimeSpec:
    name: str
    module: str
    required_arguments: tuple[str, ...]
    lock_name: str
    log_aliases: tuple[str, ...]
    quiet_when_market_closed: bool = False
    maximum_log_age: pd.Timedelta | None = _EXPECTED_PUBLICATION_AGE


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
            "--phase-offset-minutes 5",
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
    if clean_mode not in {"hourly", "daily"}:
        raise ValueError("mode must be hourly or daily")
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
    checks.append(_safe("loop_b_publication", lambda: _loop_b_check(root, now, watchlist)))
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
    checks.append(_safe("strategy_publication", lambda: _strategy_check(root, now)))
    checks.append(_safe("cross_loop_lineage", lambda: _lineage_check(root)))
    checks.append(_safe("ui_contracts", lambda: _ui_check(root, now, watchlist)))
    checks.append(_safe("storage_capacity", lambda: _storage_check(root)))

    if clean_mode == "daily":
        checks.append(_safe("alfred_full_evidence", lambda: _alfred_full_check(root)))
        checks.append(
            _safe(
                "directional_prediction_quality",
                lambda: _directional_quality_check(root),
            )
        )
        checks.append(
            _safe(
                "strategy_prediction_quality",
                lambda: _strategy_quality_check(root),
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
    log_root = root / "logs" / "ducketz"
    stdout_files = tuple(log_root.glob("**/*.stdout.log")) if log_root.is_dir() else ()
    problems: list[str] = []
    results: dict[str, object] = {}
    for spec in RUNTIMES:
        candidates = [
            path
            for path in stdout_files
            if any(alias in path.name.lower() for alias in spec.log_aliases)
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
        stderr = stdout.with_name(stdout.name.replace(".stdout.log", ".stderr.log"))
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
    )


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
    if not pointer_path.is_file():
        return _check(
            "loop_a_bar_readiness",
            _FAIL if market_actionable else _INFO,
            "No Loop A target-bar readiness has been published yet.",
            expected_target=expected_target.isoformat(),
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
) -> dict[str, object]:
    publication = read_current_publication(root)
    run_timestamp = _utc(publication.manifest.get("run_timestamp"), "Loop B run timestamp")
    age = max(pd.Timedelta(0), now - run_timestamp)
    intelligence = pd.read_parquet(publication.run_directory / "intelligence.parquet")
    observed_symbols = set(intelligence["symbol"].astype("string").str.upper())
    observed_horizons = set(intelligence["horizon"].astype("string").str.lower())
    expected_routes = len(set(symbols)) * len(INTERNAL_HORIZON_ORDER)
    failures = []
    if age > _EXPECTED_PUBLICATION_AGE:
        failures.append("publication-stale")
    if observed_symbols != set(symbols):
        failures.append("symbol-scope-mismatch")
    if observed_horizons != set(INTERNAL_HORIZON_ORDER):
        failures.append("horizon-scope-mismatch")
    if len(intelligence) != expected_routes:
        failures.append("route-count-mismatch")
    return _check(
        "loop_b_publication",
        _FAIL if failures else _PASS,
        (
            "Loop B has a fresh verified all-route prediction publication."
            if not failures
            else "Loop B's current prediction publication is incomplete or stale."
        ),
        run_path=publication.run_directory.relative_to(root).as_posix(),
        run_timestamp=run_timestamp.isoformat(),
        age_minutes=_minutes(age),
        intelligence_rows=len(intelligence),
        expected_routes=expected_routes,
        failures=failures,
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


def _strategy_check(root: Path, now: pd.Timestamp) -> dict[str, object]:
    publication = read_current_strategy_publication(root)
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


def _lineage_check(root: Path) -> dict[str, object]:
    loop_b = read_current_publication(root)
    strategy = read_current_strategy_publication(root)
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
) -> dict[str, object]:
    forecast = load_forecast_dashboard(
        root / "ml-intelligence" / "latest" / "rolling-predictions.parquet",
        loaded_at=now.to_pydatetime(),
    )
    strategy = load_strategy_candidates(
        root / "ml" / "strategy-latest" / "strategy-candidates.parquet",
        snapshot=PortfolioSnapshot(
            source="schwab",
            account_label="read-only system monitor",
            synced_at=now.to_pydatetime(),
            status="read-only monitor",
        ),
        loaded_at=now.to_pydatetime(),
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
            "Both production UI adapters load and enforce their visibility contracts."
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
    )


def _storage_check(root: Path) -> dict[str, object]:
    usage = shutil.disk_usage(root)
    free_ratio = usage.free / usage.total if usage.total else 0.0
    log_root = root / "logs" / "ducketz"
    log_bytes = sum(
        path.stat().st_size
        for path in log_root.glob("**/*")
        if path.is_file()
    ) if log_root.is_dir() else 0
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


def _directional_quality_check(root: Path) -> dict[str, object]:
    publication = read_current_publication(root)
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


def _strategy_quality_check(root: Path) -> dict[str, object]:
    publication = read_current_strategy_publication(root)
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
        }
    )
    heuristic = basis.eq(SCENARIO_COVERAGE_SCORE_BASIS)
    coverage = pd.to_numeric(candidates["pricing_leg_coverage"], errors="coerce")
    quality = (
        candidates["surface_quality_pass"].fillna(False).astype(bool)
        & candidates["liquidity_policy_pass"].fillna(False).astype(bool)
        & candidates["all_option_quotes_valid"].fillna(False).astype(bool)
    )
    fully_priced = coverage.ge(1.0 - 1e-12) & candidates[
        "pricing_source"
    ].astype("string").str.upper().isin({"BSGP", "BLACK_SCHOLES"})
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
    for report in model_reports.values():
        if not isinstance(report, Mapping):
            continue
        value = str(report.get("status", "UNKNOWN"))
        report_statuses[value] = report_statuses.get(value, 0) + 1
    calibrated_rows = int(fitted.sum())
    heuristic_rows = int(heuristic.sum())
    if missing_horizons:
        status = _FAIL
        text = "Strategy model evidence is missing production horizons."
    elif calibrated_rows == 0:
        status = _WARN
        text = "Strategy is publishing research-only Scenario Coverage, not calibrated probabilities."
    elif not bool((fitted & fully_priced & quality).any()):
        status = _WARN
        text = "Calibrated Strategy rows exist but none pass full pricing and quality gates."
    else:
        status = _PASS
        text = "Strategy publishes calibrated, fully priced, quality-passing candidates."
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
        "quality_passing_rows": int(quality.sum()),
        "complete_observed_outcome_rows": complete_outcomes,
        "model_status_counts": report_statuses,
        "model_evidence_interpretation": (
            "INSUFFICIENT_OBSERVED_OPTION_OUTCOMES"
            if complete_outcomes == 0
            else "OBSERVED_OPTION_OUTCOMES_AVAILABLE"
        ),
        "missing_horizons": missing_horizons,
    }


def _pricing_strategy_canary_check(
    root: Path,
    *,
    now: pd.Timestamp,
    target: pd.Timestamp,
    symbols: Sequence[str],
) -> dict[str, object]:
    try:
        result = run_canary(
            root,
            target_snapshot_for=target,
            symbols=symbols,
            timeout_seconds=0.0,
            clock=lambda: now.to_pydatetime(),
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
    return _check(
        "pricing_strategy_canary",
        _PASS,
        "The latest regular target passes the exact Pricing-to-Strategy canary.",
        **dict(result),
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


def scheduled_monitor_mode(value: datetime | None = None) -> str:
    """Select the daily layer for the weekday 14:42 local heartbeat."""

    local = value or datetime.now().astimezone()
    if local.tzinfo is None:
        raise ValueError("scheduled monitor clock must be timezone-aware")
    return "daily" if local.weekday() < 5 and local.hour == 14 else "hourly"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only hourly or daily health checks across the Loops system."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    parser.add_argument(
        "--mode",
        choices=("hourly", "daily", "scheduled"),
        required=True,
        help=(
            "Scheduled selects daily during the weekday 2 PM local heartbeat and "
            "hourly at every other wake."
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
    selected_mode = scheduled_monitor_mode() if args.mode == "scheduled" else args.mode
    report = build_monitor_report(root, mode=selected_mode)
    if args.mode == "scheduled":
        report["requested_mode"] = "scheduled"
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
    "RUNTIMES",
    "build_monitor_report",
    "main",
    "scheduled_monitor_mode",
    "summarize_directional_quality",
    "summarize_strategy_quality",
]
