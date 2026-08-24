from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.system_monitor import (
    RUNTIMES,
    RuntimeSpec,
    _command_owns_module,
    _normalize_process,
    _normalize_spaces,
    _process_checks,
    _windows_process_rows,
    build_monitor_report,
    scheduled_monitor_mode,
)


GUARDIAN_SCHEMA_VERSION = "loops-system-guardian-v1"
_COOLDOWN = pd.Timedelta(hours=2)
_HANG_CONFIRMATION_MINIMUM = pd.Timedelta(minutes=30)
_HANG_CONFIRMATION_MAXIMUM = pd.Timedelta(hours=3)
_DEFAULT_VERIFY_SECONDS = 30.0
_CREDENTIAL_OR_CAPACITY_MARKERS = (
    "api key",
    "authentication",
    "credential",
    "entitlement",
    "forbidden",
    "rate limit",
    "status 401",
    "status 403",
    "status 429",
    "unauthorized",
    "capacity",
)
_INTEGRITY_MARKERS = (
    "checksum",
    "corrupt",
    "disagrees",
    "does not match",
    "escapes",
    "hashes do not verify",
    "identity disagrees",
    "invalid",
    "unreadable",
    "verification failed",
)


@dataclass(frozen=True)
class GuardianLaunchSpec:
    runtime: str
    log_stem: str
    arguments: tuple[str, ...]

    @property
    def module(self) -> str:
        module_index = self.arguments.index("-m") + 1
        return self.arguments[module_index]

    @property
    def command_signature(self) -> str:
        return _normalize_spaces(" ".join(self.arguments)).lower()


# These are a deliberately closed restart allowlist. They match the checked-in
# production commands. Options recovery keeps the current prospective-only
# posture so an unattended liveness repair cannot initiate historical catch-up.
GUARDIAN_LAUNCHES = (
    GuardianLaunchSpec(
        "cme",
        "cme-l2",
        (
            "-u",
            "-m",
            "datafetching.cme_runtime",
            "--datastore-target",
            "pc",
            "--max-concurrency",
            "1",
        ),
    ),
    GuardianLaunchSpec(
        "alfred",
        "daily-alfred",
        (
            "-u",
            "-m",
            "datafetching.fred_alfred_runtime",
            "--datastore-target",
            "pc",
            "--utc-hour",
            "7",
        ),
    ),
    GuardianLaunchSpec(
        "loop_a",
        "loop-a",
        (
            "-u",
            "-m",
            "datafetching.orchestrate",
            "--datastore-target",
            "pc",
            "--watchlist",
            r"datafetching\watchlist.txt",
            "--providers",
            "databento",
            "fmp",
            "fred",
            "schwab",
            "sec",
            "--cme-mode",
            "external",
            "--options-mode",
            "external",
            "--interval-minutes",
            "15",
            "--bar-readiness-recovery-timeout-seconds",
            "420",
            "--bar-readiness-recovery-poll-seconds",
            "10",
        ),
    ),
    GuardianLaunchSpec(
        "pricing",
        "active-pricing",
        (
            "-u",
            "-m",
            "ml.option_pricing_runtime",
            "--datastore-target",
            "pc",
            "--watchlist",
            r"datafetching\watchlist.txt",
            "--interval-minutes",
            "15",
            "--phase-offset-minutes",
            "1",
            "--bar-readiness-mode",
            "required",
            "--bar-readiness-timeout-seconds",
            "480",
        ),
    ),
    GuardianLaunchSpec(
        "loop_b",
        "directional-loop-b",
        (
            "-u",
            "-m",
            "ml.prediction_runtime",
            "--datastore-target",
            "pc",
            "--watchlist",
            r"datafetching\watchlist.txt",
            "--provider",
            "databento",
            "--horizons",
            "1h",
            "4h",
            "1d",
            "1w",
            "--feature-profile",
            "loop-a-all-bsgp-active-v3",
            "--model-family",
            "logistic",
            "--calibration",
            "platt",
            "--round-trip-cost",
            "0.001",
            "--interval-minutes",
            "15",
            "--phase-offset-minutes",
            "5",
        ),
    ),
    GuardianLaunchSpec(
        "options",
        "options-capture",
        (
            "-u",
            "-m",
            "datafetching.options_runtime",
            "--datastore-target",
            "pc",
            "--watchlist",
            r"datafetching\watchlist.txt",
            "--provider-mode",
            "opra-canonical",
            "--interval-minutes",
            "15",
            "--phase-offset-minutes",
            "6",
            "--pricing-barrier-timeout-seconds",
            "45",
            "--bar-readiness-mode",
            "required",
            "--skip-historical-catchup",
        ),
    ),
    GuardianLaunchSpec(
        "strategy",
        "strategy",
        (
            "-u",
            "-m",
            "ml.strategy_runtime",
            "--datastore-target",
            "pc",
            "--interval-minutes",
            "15",
            "--phase-offset-minutes",
            "10",
            "--pricing-mode",
            "active",
        ),
    ),
    GuardianLaunchSpec(
        "strategy_profit_training",
        "strategy-profit-training",
        (
            "-u",
            "-m",
            "ml.strategy_profit_training_runtime",
            "--datastore-target",
            "pc",
            "--utc-hour",
            "22",
        ),
    ),
)

_LAUNCH_BY_RUNTIME = {spec.runtime: spec for spec in GUARDIAN_LAUNCHES}
_RUNTIME_BY_NAME = {spec.name: spec for spec in RUNTIMES}


def run_guardian(
    datastore_root: Path,
    *,
    mode: str,
    repair_liveness: bool,
    observed_at: object | None = None,
    process_rows: Sequence[Mapping[str, object]] | None = None,
    process_reader: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    pid_exists: Callable[[int], bool] | None = None,
    stop_process_tree: Callable[[int, GuardianLaunchSpec], Sequence[int]] | None = None,
    start_runtime: Callable[
        [Path, Path, GuardianLaunchSpec, pd.Timestamp], int
    ]
    | None = None,
    sleep: Callable[[float], None] = time.sleep,
    verify_seconds: float = _DEFAULT_VERIFY_SECONDS,
) -> dict[str, object]:
    """Run the monitor and apply at most one allowlisted liveness repair."""

    clean_mode = str(mode).strip().lower()
    if clean_mode not in {"hourly", "daily", "weekly"}:
        raise ValueError("mode must be hourly, daily, or weekly")
    root = Path(datastore_root).resolve()
    now = _utc(observed_at if observed_at is not None else datetime.now(timezone.utc))
    read_processes = process_reader or _windows_process_rows
    check_pid = pid_exists or _windows_pid_exists
    stop_tree = stop_process_tree or _stop_windows_process_tree
    launch = start_runtime or _start_windows_runtime
    rows = tuple(process_rows) if process_rows is not None else tuple(read_processes())
    before = build_monitor_report(
        root,
        mode=clean_mode,
        observed_at=now,
        process_rows=rows,
    )
    audit_directory = _audit_directory(root)
    decision = plan_guarded_recovery(
        root,
        monitor_report=before,
        process_rows=rows,
        observed_at=now,
        pid_exists=check_pid,
        audit_directory=audit_directory,
    )
    observations: list[str] = []
    if repair_liveness:
        for observation in decision.pop("observations_to_record", []):
            path = _write_hang_observation(audit_directory, observation, now)
            observations.append(str(path))
    else:
        decision.pop("observations_to_record", None)
    if observations:
        decision["observation_receipts"] = observations

    final_monitor = before
    remediation = dict(decision)
    if decision.get("status") == "ELIGIBLE" and repair_liveness:
        remediation, final_rows = _execute_guarded_restart(
            root,
            decision=decision,
            observed_at=now,
            process_reader=read_processes,
            pid_exists=check_pid,
            stop_process_tree=stop_tree,
            start_runtime=launch,
            sleep=sleep,
            verify_seconds=max(0.0, float(verify_seconds)),
        )
        receipt = _write_remediation_receipt(audit_directory, remediation, now)
        remediation["audit_receipt"] = str(receipt)
        post_rows = tuple(final_rows) if final_rows is not None else tuple(read_processes())
        final_monitor = build_monitor_report(
            root,
            mode=clean_mode,
            observed_at=datetime.now(timezone.utc),
            process_rows=post_rows,
        )
    elif decision.get("status") == "ELIGIBLE":
        remediation["status"] = "DRY_RUN_ELIGIBLE"
        remediation["summary"] = (
            "A targeted liveness repair is eligible, but --repair-liveness was not set."
        )

    return {
        "schema_version": GUARDIAN_SCHEMA_VERSION,
        "mode": clean_mode,
        "status": final_monitor["status"],
        "checked_at": final_monitor["checked_at"],
        "before_monitor_status": before["status"],
        "monitor": final_monitor,
        "remediation": remediation,
        "safety": {
            "scope": "ONE_ALLOWLISTED_RUNTIME_LIVENESS_REPAIR_PER_RUN",
            "repair_enabled": bool(repair_liveness),
            "cooldown_minutes": int(_COOLDOWN / pd.Timedelta(minutes=1)),
            "code_edits_allowed": False,
            "authority_pointer_changes_allowed": False,
            "historical_backfills_allowed": False,
            "model_promotions_allowed": False,
            "orders_allowed": False,
        },
        "orders_placed": 0,
    }


def plan_guarded_recovery(
    datastore_root: Path,
    *,
    monitor_report: Mapping[str, object],
    process_rows: Sequence[Mapping[str, object]],
    observed_at: object,
    pid_exists: Callable[[int], bool],
    audit_directory: Path | None = None,
) -> dict[str, object]:
    """Return a fail-closed, JSON-ready liveness decision."""

    root = Path(datastore_root).resolve()
    now = _utc(observed_at)
    rows = tuple(_normalize_process(row) for row in process_rows)
    process_checks = {check["name"]: check for check in _process_checks(rows)}
    failed_runtimes = [
        spec
        for spec in RUNTIMES
        if process_checks[f"process.{spec.name}"]["status"] == "FAIL"
    ]
    audit = Path(audit_directory or _audit_directory(root))

    if failed_runtimes:
        if len(failed_runtimes) != 1:
            return _decision(
                "REPORT_ONLY",
                "Multiple runtime ownership failures are ambiguous; no process was changed.",
                affected_runtimes=[spec.name for spec in failed_runtimes],
            )
        runtime = failed_runtimes[0]
        integrity_blockers = _integrity_blockers(monitor_report)
        if integrity_blockers:
            return _decision(
                "REPORT_ONLY",
                "An integrity or authority-verification failure blocks unattended process changes.",
                runtime=runtime.name,
                integrity_blockers=integrity_blockers,
            )
        blocking_stderr = _blocking_runtime_stderr(monitor_report, runtime.name)
        if blocking_stderr is not None:
            return _decision(
                "REPORT_ONLY",
                "A credential, entitlement, rate-limit, or capacity error needs operator review.",
                runtime=runtime.name,
                stderr_tail=blocking_stderr,
            )
        launch = _LAUNCH_BY_RUNTIME[runtime.name]
        matches = _matching_runtime_rows(rows, runtime)
        if len(matches) not in {0, 1}:
            return _decision(
                "REPORT_ONLY",
                "Duplicate or malformed runtime ownership is not eligible for automatic repair.",
                runtime=runtime.name,
                process_count=len(matches),
                pids=_row_pids(matches),
            )
        if matches and not all(_is_allowlisted_command(row, launch) for row in matches):
            return _decision(
                "REPORT_ONLY",
                (
                    "The remaining process command is not the guardian's "
                    "allowlisted production command."
                ),
                runtime=runtime.name,
                pids=_row_pids(matches),
            )
        lock = _assess_lock(root, runtime, matches, pid_exists)
        if lock["status"] not in {"MISSING", "OWNED_BY_TARGET", "STALE_DEAD"}:
            return _decision(
                "REPORT_ONLY",
                "The singleton lock is invalid or belongs to a live non-target process.",
                runtime=runtime.name,
                pids=_row_pids(matches),
                lock=lock,
            )
        cooldown = _recent_remediation(audit, runtime.name, now)
        if cooldown is not None:
            return _decision(
                "COOLDOWN",
                "A prior restart attempt is still inside the two-hour cooldown.",
                runtime=runtime.name,
                pids=_row_pids(matches),
                lock=lock,
                prior_receipt=str(cooldown),
            )
        return _decision(
            "ELIGIBLE",
            (
                "The runtime is absent and may be started once."
                if not matches
                else "One allowlisted owner process remains and may be replaced once."
            ),
            runtime=runtime.name,
            fault="PROCESS_MISSING" if not matches else "PARTIAL_OWNER_PAIR",
            processes=[dict(row) for row in matches],
            pids=_row_pids(matches),
            lock=lock,
        )

    hang_candidates = _hang_candidates(root, monitor_report, rows)
    if len(hang_candidates) > 1:
        return _decision(
            "REPORT_ONLY",
            "Multiple possible hangs are ambiguous; observations were recorded without restarting.",
            affected_runtimes=[str(item["runtime"]) for item in hang_candidates],
            observations_to_record=hang_candidates,
        )
    if len(hang_candidates) == 1:
        candidate = hang_candidates[0]
        runtime_name = str(candidate["runtime"])
        previous = _matching_hang_observation(audit, candidate, now)
        if previous is None:
            return _decision(
                "OBSERVING_HANG",
                "A possible hang needs an unchanged observation on a later scheduled run.",
                runtime=runtime_name,
                pids=candidate["fingerprint"]["pids"],
                liveness_failures=candidate["fingerprint"]["liveness_failures"],
                observations_to_record=[candidate],
            )
        runtime = _RUNTIME_BY_NAME[runtime_name]
        matches = _matching_runtime_rows(rows, runtime)
        lock = _assess_lock(root, runtime, matches, pid_exists)
        if lock["status"] != "OWNED_BY_TARGET":
            return _decision(
                "REPORT_ONLY",
                (
                    "A confirmed hang is not repairable because its lock is not "
                    "owned by the target pair."
                ),
                runtime=runtime_name,
                pids=_row_pids(matches),
                lock=lock,
                confirming_observation=str(previous),
            )
        cooldown = _recent_remediation(audit, runtime_name, now)
        if cooldown is not None:
            return _decision(
                "COOLDOWN",
                "A confirmed hang is inside the two-hour restart cooldown.",
                runtime=runtime_name,
                pids=_row_pids(matches),
                lock=lock,
                prior_receipt=str(cooldown),
                confirming_observation=str(previous),
            )
        return _decision(
            "ELIGIBLE",
            (
                "The same liveness failure, process pair, and log fingerprint "
                "were unchanged across two scheduled runs."
            ),
            runtime=runtime_name,
            fault="CONFIRMED_HANG",
            processes=[dict(row) for row in matches],
            pids=_row_pids(matches),
            lock=lock,
            confirming_observation=str(previous),
        )

    status = "NO_ACTION" if monitor_report.get("status") != "UNHEALTHY" else "REPORT_ONLY"
    return _decision(
        status,
        (
            "No allowlisted liveness repair is needed."
            if status == "NO_ACTION"
            else "The unhealthy findings are outside the automatic liveness-repair policy."
        ),
    )


def _hang_candidates(
    root: Path,
    monitor_report: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    checks = {
        str(check.get("name")): check
        for check in monitor_report.get("checks", [])
        if isinstance(check, Mapping)
    }
    runtime_logs = checks.get("runtime_logs")
    if not isinstance(runtime_logs, Mapping):
        return []
    log_details = runtime_logs.get("details")
    if not isinstance(log_details, Mapping):
        return []
    problems = {str(value) for value in log_details.get("problems", [])}
    runtime_views = log_details.get("runtimes")
    if not isinstance(runtime_views, Mapping):
        return []

    candidates: list[dict[str, object]] = []
    process_checks = {check["name"]: check for check in _process_checks(rows)}
    for runtime in RUNTIMES:
        if f"{runtime.name}:stdout-stale" not in problems:
            continue
        if process_checks[f"process.{runtime.name}"]["status"] != "PASS":
            continue
        matches = _matching_runtime_rows(rows, runtime)
        launch = _LAUNCH_BY_RUNTIME[runtime.name]
        if len(matches) != 2 or not all(
            _is_allowlisted_command(row, launch) for row in matches
        ):
            continue
        liveness_failures = _liveness_contract_failures(runtime.name, checks)
        if not liveness_failures:
            continue
        view = runtime_views.get(runtime.name)
        if not isinstance(view, Mapping):
            continue
        stderr_tail = str(view.get("stderr_tail") or "").lower()
        if any(marker in stderr_tail for marker in _CREDENTIAL_OR_CAPACITY_MARKERS):
            continue
        stdout_value = view.get("stdout")
        if not isinstance(stdout_value, str) or not stdout_value.strip():
            continue
        stdout = Path(stdout_value).resolve()
        log_authority = (root / "logs" / "ducketz").resolve()
        if log_authority not in stdout.parents or not stdout.is_file():
            continue
        stat = stdout.stat()
        candidates.append(
            {
                "runtime": runtime.name,
                "observed_at": str(monitor_report.get("checked_at")),
                "fingerprint": {
                    "pids": _row_pids(matches),
                    "created_at": [str(row.get("created_at")) for row in matches],
                    "stdout": str(stdout),
                    "stdout_size": int(stat.st_size),
                    "stdout_mtime_ns": int(stat.st_mtime_ns),
                    "liveness_failures": liveness_failures,
                },
            }
        )
    return candidates


def _integrity_blockers(
    monitor_report: Mapping[str, object],
) -> list[str]:
    blockers: list[str] = []
    for value in monitor_report.get("checks", []):
        if not isinstance(value, Mapping) or value.get("status") != "FAIL":
            continue
        name = str(value.get("name", "unknown"))
        if name.startswith("process.") or name in {
            "runtime_processes",
            "runtime_locks",
        }:
            continue
        evidence = " ".join(
            (
                str(value.get("summary", "")),
                json.dumps(value.get("details", {}), sort_keys=True, default=str),
            )
        ).lower()
        if any(marker in evidence for marker in _INTEGRITY_MARKERS):
            blockers.append(name)
    return sorted(set(blockers))


def _blocking_runtime_stderr(
    monitor_report: Mapping[str, object], runtime: str
) -> str | None:
    checks = {
        str(check.get("name")): check
        for check in monitor_report.get("checks", [])
        if isinstance(check, Mapping)
    }
    logs = checks.get("runtime_logs")
    details = logs.get("details") if isinstance(logs, Mapping) else None
    runtimes = details.get("runtimes") if isinstance(details, Mapping) else None
    view = runtimes.get(runtime) if isinstance(runtimes, Mapping) else None
    if not isinstance(view, Mapping) or not view.get("recent_stderr"):
        return None
    tail = str(view.get("stderr_tail") or "")
    lowered = tail.lower()
    return tail if any(marker in lowered for marker in _CREDENTIAL_OR_CAPACITY_MARKERS) else None


def _liveness_contract_failures(
    runtime: str,
    checks: Mapping[str, Mapping[str, object]],
) -> list[str]:
    failures: list[str] = []
    if runtime == "loop_a":
        cycle = checks.get("loop_a_cycle", {})
        cycle_details = cycle.get("details") if isinstance(cycle, Mapping) else None
        cycle_failures = {
            str(value)
            for value in (
                cycle_details.get("failures", [])
                if isinstance(cycle_details, Mapping)
                else []
            )
        }
        allowed = {"active-cycle-running-too-long", "latest-complete-cycle-stale"}
        if cycle.get("status") == "FAIL" and cycle_failures and cycle_failures <= allowed:
            failures.append("loop_a_cycle_stale")
        readiness = checks.get("loop_a_bar_readiness", {})
        if readiness.get("status") == "FAIL" and readiness.get("summary") in {
            "No Loop A target-bar readiness has been published yet.",
            "Loop A readiness is behind the latest actionable regular target.",
        }:
            if cycle.get("details", {}).get("active_cycle_status") != "WRITING":
                failures.append("loop_a_readiness_behind")
    elif runtime == "pricing":
        check = checks.get("pricing_publications", {})
        if check.get("status") == "FAIL" and check.get("summary") in {
            "No target-scoped Pricing authority exists for an actionable session.",
            "Pricing target authority is behind the actionable regular target.",
        }:
            failures.append("pricing_target_behind")
    elif runtime == "loop_b":
        check = checks.get("loop_b_publication", {})
        details = check.get("details") if isinstance(check, Mapping) else None
        check_failures = {
            str(value)
            for value in (
                details.get("failures", []) if isinstance(details, Mapping) else []
            )
        }
        if check.get("status") == "FAIL" and check_failures == {"publication-stale"}:
            failures.append("loop_b_publication_stale")
    elif runtime == "options":
        check = checks.get("options_publications", {})
        if (
            check.get("status") == "FAIL"
            and check.get("summary")
            == "Current option snapshots are behind the actionable regular target."
        ):
            failures.append("options_publications_behind")
    elif runtime == "strategy":
        check = checks.get("strategy_publication", {})
        details = check.get("details") if isinstance(check, Mapping) else None
        if (
            check.get("status") == "FAIL"
            and isinstance(details, Mapping)
            and int(details.get("candidate_rows", 0)) > 0
            and float(details.get("age_minutes", 0.0)) > 45.0
        ):
            failures.append("strategy_publication_stale")
    return failures


def _execute_guarded_restart(
    root: Path,
    *,
    decision: Mapping[str, object],
    observed_at: pd.Timestamp,
    process_reader: Callable[[], Sequence[Mapping[str, object]]],
    pid_exists: Callable[[int], bool],
    stop_process_tree: Callable[[int, GuardianLaunchSpec], Sequence[int]],
    start_runtime: Callable[[Path, Path, GuardianLaunchSpec, pd.Timestamp], int],
    sleep: Callable[[float], None],
    verify_seconds: float,
) -> tuple[dict[str, object], Sequence[Mapping[str, object]] | None]:
    runtime_name = str(decision["runtime"])
    runtime = _RUNTIME_BY_NAME[runtime_name]
    launch = _LAUNCH_BY_RUNTIME[runtime_name]
    before_processes = [
        _normalize_process(value)
        for value in decision.get("processes", [])
        if isinstance(value, Mapping)
    ]
    result: dict[str, object] = {
        "status": "RESTART_FAILED",
        "summary": "The targeted restart did not complete.",
        "runtime": runtime_name,
        "fault": decision.get("fault"),
        "attempted_at": observed_at.isoformat(),
        "before_pids": _row_pids(before_processes),
        "stopped_pids": [],
        "removed_lock": False,
        "launcher_pid": None,
        "after_pids": [],
        "verification": "NOT_RUN",
        "orders_placed": 0,
    }
    final_rows: Sequence[Mapping[str, object]] | None = None
    try:
        if before_processes:
            before_ids = set(_row_pids(before_processes))
            roots = [
                row for row in before_processes if int(row["ppid"]) not in before_ids
            ]
            if len(roots) != 1:
                raise RuntimeError("The target process tree has no unique root")
            stopped = list(stop_process_tree(int(roots[0]["pid"]), launch))
            result["stopped_pids"] = sorted(int(value) for value in stopped)
            deadline = time.monotonic() + 10.0
            while any(pid_exists(value) for value in before_ids):
                if time.monotonic() >= deadline:
                    raise RuntimeError("The old runtime process tree did not stop")
                sleep(0.25)
        result["removed_lock"] = _prepare_exact_lock_for_restart(
            root,
            runtime,
            expected=decision.get("lock"),
            pid_exists=pid_exists,
        )
        log_directory = _guardian_log_directory(root, observed_at)
        result["launcher_pid"] = int(
            start_runtime(root, log_directory, launch, observed_at)
        )
        deadline = time.monotonic() + verify_seconds
        while True:
            final_rows = tuple(process_reader())
            if _runtime_pair_and_lock_ready(root, runtime, final_rows, pid_exists):
                result["status"] = "RESTARTED_VERIFIED"
                result["summary"] = (
                    "The exact runtime was restarted once and its new "
                    "launcher/worker pair and lock verify."
                )
                result["after_pids"] = _row_pids(
                    _matching_runtime_rows(final_rows, runtime)
                )
                result["verification"] = "PAIR_AND_LOCK_OWNED"
                break
            if time.monotonic() >= deadline:
                result["summary"] = (
                    "The restart command ran, but a valid pair and owned lock "
                    "did not appear before the deadline."
                )
                result["after_pids"] = _row_pids(
                    _matching_runtime_rows(final_rows, runtime)
                )
                result["verification"] = "PAIR_OR_LOCK_NOT_READY"
                break
            sleep(min(1.0, max(0.05, deadline - time.monotonic())))
    except Exception as exc:
        result["summary"] = "The guarded restart failed closed."
        result["verification"] = "ERROR"
        result["error"] = f"{type(exc).__name__}: {_normalize_spaces(str(exc))}"[:1000]
    return result, final_rows


def _runtime_pair_and_lock_ready(
    root: Path,
    runtime: RuntimeSpec,
    rows: Sequence[Mapping[str, object]],
    pid_exists: Callable[[int], bool],
) -> bool:
    process = {
        check["name"]: check for check in _process_checks(rows)
    }[f"process.{runtime.name}"]
    if process["status"] != "PASS":
        return False
    matches = _matching_runtime_rows(rows, runtime)
    lock = _assess_lock(root, runtime, matches, pid_exists)
    return lock["status"] == "OWNED_BY_TARGET"


def _matching_runtime_rows(
    rows: Sequence[Mapping[str, object]], runtime: RuntimeSpec
) -> list[dict[str, object]]:
    return [
        _normalize_process(row)
        for row in rows
        if _command_owns_module(
            _normalize_process(row)["command_line"], runtime.module
        )
    ]


def _is_allowlisted_command(
    row: Mapping[str, object], launch: GuardianLaunchSpec
) -> bool:
    command = _normalize_spaces(str(row.get("command_line", ""))).lower()
    return launch.command_signature in command


def _assess_lock(
    root: Path,
    runtime: RuntimeSpec,
    matches: Sequence[Mapping[str, object]],
    pid_exists: Callable[[int], bool],
) -> dict[str, object]:
    path = _exact_lock_path(root, runtime)
    if not path.is_file():
        return {"status": "MISSING", "path": str(path), "pid": None}
    lock_pid = _read_lock_pid(path)
    if lock_pid is None:
        return {"status": "INVALID", "path": str(path), "pid": None}
    target_pids = set(_row_pids(matches))
    if lock_pid in target_pids:
        status = "OWNED_BY_TARGET"
    elif pid_exists(lock_pid):
        status = "FOREIGN_LIVE"
    else:
        status = "STALE_DEAD"
    return {"status": status, "path": str(path), "pid": lock_pid}


def _prepare_exact_lock_for_restart(
    root: Path,
    runtime: RuntimeSpec,
    *,
    expected: object,
    pid_exists: Callable[[int], bool],
) -> bool:
    if not isinstance(expected, Mapping):
        raise RuntimeError("The pre-restart lock assessment is missing")
    path = _exact_lock_path(root, runtime)
    expected_status = str(expected.get("status"))
    expected_pid = expected.get("pid")
    if not path.exists():
        return False
    current_pid = _read_lock_pid(path)
    if current_pid is None:
        raise RuntimeError("The exact singleton lock became unreadable")
    if expected_status == "MISSING":
        raise RuntimeError("A singleton lock appeared after the recovery decision")
    if expected_pid is None or int(expected_pid) != current_pid:
        raise RuntimeError("The singleton lock owner changed after the recovery decision")
    if pid_exists(current_pid):
        raise RuntimeError("The singleton lock PID is still alive")
    path.unlink()
    return True


def _exact_lock_path(root: Path, runtime: RuntimeSpec) -> Path:
    authority = Path(root).resolve()
    path = (authority / runtime.lock_name).resolve()
    if path.parent != authority or path.name != runtime.lock_name:
        raise ValueError("Runtime lock path escapes the datastore root")
    return path


def _read_lock_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?m)^pid=(\d+)\s*$", text)
    return int(match.group(1)) if match else None


def _matching_hang_observation(
    audit_directory: Path,
    candidate: Mapping[str, object],
    now: pd.Timestamp,
) -> Path | None:
    runtime = str(candidate["runtime"])
    if not audit_directory.is_dir():
        return None
    for path in sorted(
        audit_directory.glob(f"observation-{runtime}-*.json"), reverse=True
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            observed = _utc(value.get("observed_at"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        age = now - observed
        if age < _HANG_CONFIRMATION_MINIMUM:
            continue
        if age > _HANG_CONFIRMATION_MAXIMUM:
            break
        if value.get("fingerprint") == candidate.get("fingerprint"):
            return path
    return None


def _recent_remediation(
    audit_directory: Path, runtime: str, now: pd.Timestamp
) -> Path | None:
    if not audit_directory.is_dir():
        return None
    for path in sorted(
        audit_directory.glob(f"remediation-{runtime}-*.json"), reverse=True
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            attempted = _utc(value.get("attempted_at"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            modified = pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
            if now - modified <= _COOLDOWN:
                return path
            continue
        age = now - attempted
        if pd.Timedelta(0) <= age <= _COOLDOWN:
            return path
        if age > _COOLDOWN:
            break
    return None


def _write_hang_observation(
    audit_directory: Path, value: Mapping[str, object], now: pd.Timestamp
) -> Path:
    runtime = str(value["runtime"])
    path = audit_directory / f"observation-{runtime}-{_timestamp_slug(now)}.json"
    payload = dict(value)
    payload["schema_version"] = GUARDIAN_SCHEMA_VERSION
    payload["observed_at"] = now.isoformat()
    return _atomic_write_unique_json(path, payload)


def _write_remediation_receipt(
    audit_directory: Path, value: Mapping[str, object], now: pd.Timestamp
) -> Path:
    runtime = str(value["runtime"])
    path = audit_directory / f"remediation-{runtime}-{_timestamp_slug(now)}.json"
    payload = dict(value)
    payload["schema_version"] = GUARDIAN_SCHEMA_VERSION
    payload["receipt_written_at"] = datetime.now(timezone.utc).isoformat()
    return _atomic_write_unique_json(path, payload)


def _atomic_write_unique_json(path: Path, value: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _audit_directory(root: Path) -> Path:
    return (Path(root).resolve() / "logs" / "ducketz" / "system-guardian" / "audit").resolve()


def _guardian_log_directory(root: Path, now: pd.Timestamp) -> Path:
    return (
        Path(root).resolve()
        / "logs"
        / "ducketz"
        / "system-guardian"
        / now.strftime("%Y%m%d")
    ).resolve()


def _start_windows_runtime(
    root: Path,
    log_directory: Path,
    launch: GuardianLaunchSpec,
    now: pd.Timestamp,
) -> int:
    if os.name != "nt":
        raise RuntimeError("Guarded runtime startup is implemented for Windows")
    repository = Path(__file__).resolve().parents[1]
    python = (repository / ".venv" / "Scripts" / "python.exe").resolve()
    if not python.is_file():
        raise FileNotFoundError(f"Ducketz virtual-environment Python is missing: {python}")
    authority = (Path(root).resolve() / "logs" / "ducketz" / "system-guardian").resolve()
    directory = Path(log_directory).resolve()
    if authority not in directory.parents:
        raise ValueError("Guardian restart log directory escapes its authority")
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%H%M%S')}-{launch.log_stem}"
    stdout = directory / f"{stem}.stdout.log"
    stderr = directory / f"{stem}.stderr.log"
    creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    with stdout.open("ab", buffering=0) as stdout_handle, stderr.open(
        "ab", buffering=0
    ) as stderr_handle:
        process = subprocess.Popen(
            [str(python), *launch.arguments],
            cwd=str(repository),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            close_fds=True,
            creationflags=creation_flags,
        )
    return int(process.pid)


def _stop_windows_process_tree(
    root_process_id: int, launch: GuardianLaunchSpec
) -> Sequence[int]:
    if os.name != "nt":
        raise RuntimeError("Guarded process stop is implemented for Windows")
    signature = _powershell_quote(launch.command_signature)
    script = f"""
$ErrorActionPreference = 'Stop'
$targetProcessId = {int(root_process_id)}
$rootProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $targetProcessId"
if ($null -eq $rootProcess) {{ throw 'Target process disappeared before stop' }}
$normalized = (($rootProcess.CommandLine -replace '\\s+', ' ').Trim()).ToLowerInvariant()
$signature = '{signature}'
if (-not $normalized.Contains($signature)) {{
  throw 'Target command no longer matches the restart allowlist'
}}
$allProcesses = @(Get-CimInstance Win32_Process)
$selected = @([pscustomobject]@{{ process_id = $targetProcessId; depth = 0 }})
$frontier = @($targetProcessId)
$depth = 0
while ($frontier.Count -gt 0) {{
  $depth += 1
  $children = @($allProcesses | Where-Object {{ $frontier -contains [int]$_.ParentProcessId }})
  $frontier = @($children | ForEach-Object {{ [int]$_.ProcessId }})
  $selected += @(
    $frontier | ForEach-Object {{
      [pscustomobject]@{{ process_id = $_; depth = $depth }}
    }}
  )
}}
$selected | Sort-Object depth -Descending | ForEach-Object {{
  if (Get-Process -Id $_.process_id -ErrorAction SilentlyContinue) {{
    Stop-Process -Id $_.process_id -Force -ErrorAction Stop
  }}
}}
@($selected.process_id) | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown PowerShell stop error"
        raise RuntimeError(detail)
    payload = json.loads(completed.stdout or "[]")
    if isinstance(payload, int):
        return (payload,)
    if not isinstance(payload, list):
        raise RuntimeError("PowerShell returned an invalid stopped-process list")
    return tuple(int(value) for value in payload)


def _windows_pid_exists(process_id: int) -> bool:
    script = f"""
$ErrorActionPreference = 'Stop'
$process = Get-CimInstance Win32_Process -Filter "ProcessId = {int(process_id)}" -ErrorAction Stop
if ($null -eq $process) {{ exit 1 }}
exit 0
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1 and not completed.stderr.strip():
        return False
    detail = completed.stderr.strip() or "unknown Windows PID-query error"
    raise RuntimeError(detail)


def _decision(status: str, summary: str, **details: object) -> dict[str, object]:
    return {"status": status, "summary": summary, **details}


def _row_pids(rows: Sequence[Mapping[str, object]]) -> list[int]:
    return sorted(int(_normalize_process(row)["pid"]) for row in rows)


def _timestamp_slug(value: pd.Timestamp) -> str:
    return value.strftime("%Y%m%dT%H%M%S%fZ")


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Invalid guardian timestamp")
    return pd.Timestamp(timestamp)


def _powershell_quote(value: object) -> str:
    return str(value).replace("'", "''")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic Loops monitoring with optional guarded liveness repair."
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
    )
    parser.add_argument(
        "--repair-liveness",
        action="store_true",
        help="Allow one fail-closed, allowlisted process repair when eligible.",
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    selected_mode = scheduled_monitor_mode() if args.mode == "scheduled" else args.mode
    try:
        root = resolve_datastore_dir(
            root_dir=args.datastore,
            target=None if args.datastore is not None else args.datastore_target,
        )
        report = run_guardian(
            root,
            mode=selected_mode,
            repair_liveness=args.repair_liveness,
        )
        if args.mode == "scheduled":
            report["requested_mode"] = "scheduled"
    except Exception as exc:
        report = {
            "schema_version": GUARDIAN_SCHEMA_VERSION,
            "mode": selected_mode,
            "status": "UNHEALTHY",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "monitor": None,
            "remediation": {
                "status": "GUARDIAN_FAILED",
                "summary": "The guardian failed before it could make a safe decision.",
                "error": f"{type(exc).__name__}: {_normalize_spaces(str(exc))}"[:1000],
            },
            "orders_placed": 0,
        }
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
    "GUARDIAN_LAUNCHES",
    "GUARDIAN_SCHEMA_VERSION",
    "GuardianLaunchSpec",
    "main",
    "plan_guarded_recovery",
    "run_guardian",
]
