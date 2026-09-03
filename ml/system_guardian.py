from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd
from filelock import FileLock, Timeout

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import runtime_lock_maintenance_gate
from ml.system_monitor import (
    RUNTIMES,
    RuntimeSpec,
    _command_owns_module,
    _normalize_process,
    _normalize_spaces,
    _process_checks,
    _windows_process_rows,
    build_monitor_report,
    scheduled_monitor_context,
)


GUARDIAN_SCHEMA_VERSION = "loops-system-guardian-v1"
_COOLDOWN = pd.Timedelta(hours=2)
_HANG_CONFIRMATION_MINIMUM = pd.Timedelta(minutes=30)
_HANG_CONFIRMATION_MAXIMUM = pd.Timedelta(hours=3)
_DEFAULT_VERIFY_SECONDS = 30.0
_CODEX_FORCED_UPDATE_MAXIMUM_AGE = pd.Timedelta(hours=2)
_CODEX_FORCED_UPDATE_FUTURE_TOLERANCE = pd.Timedelta(seconds=5)
_CODEX_FORCED_UPDATE_LOG_TOLERANCE = pd.Timedelta(seconds=5)
_CODEX_FORCED_UPDATE_ACTIVE_LOG_WINDOW = pd.Timedelta(minutes=45)
_CODEX_PACKAGE_FAMILY = "OpenAI.Codex_2p2nqsd0c76g0"
_CODEX_APPLICATION_NAME = f"{_CODEX_PACKAGE_FAMILY}!App"
_CODEX_IMAGE_NAME = "ChatGPT.exe"
_PRODUCTION_DATASTORE_ROOT = Path(r"C:\DATASTORE")
_FORCE_TARGET_APPLICATION_SHUTDOWN_FLAG_HIGH = 0x40000
_ALWAYS_ACTIVE_LOG_RUNTIMES = frozenset({"cme", "loop_a", "loop_b", "strategy"})
_CODEX_FORCED_UPDATE_REQUIRED_PASS_CHECKS = frozenset(
    {
        "loop_a_bar_readiness",
        "cme_publication",
        "alfred_publication",
        "options_publications",
        "pricing_publications",
        "strategy_profit_model_authority",
        "cross_loop_lineage",
        "ui_contracts",
        "storage_capacity",
    }
)
_GRACEFUL_STOP_MARKERS = (
    "alfred daily owner stopped.",
    "cme runtime stopped.",
    "loop b stopped.",
    "option pricing runtime stopped.",
    "options runtime stopped.",
    "orchestration stopped.",
    "strategy runtime stopped.",
)
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
            "30",
            "--phase-offset-minutes",
            "5",
            "--failure-retry-attempts",
            "1",
            "--failure-retry-delay-seconds",
            "60",
            "--stale-recovery-minutes",
            "35",
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
    repair_codex_forced_update: bool = False,
    observed_at: object | None = None,
    process_rows: Sequence[Mapping[str, object]] | None = None,
    process_reader: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    pid_exists: Callable[[int], bool] | None = None,
    stop_process_tree: Callable[[int, GuardianLaunchSpec], Sequence[int]] | None = None,
    start_runtime: Callable[
        [Path, Path, GuardianLaunchSpec, pd.Timestamp], int
    ]
    | None = None,
    codex_update_event_reader: Callable[
        [pd.Timestamp], Sequence[Mapping[str, object]]
    ]
    | None = None,
    all_runtime_launcher: Callable[[Path, pd.Timestamp], Mapping[str, object]]
    | None = None,
    sleep: Callable[[float], None] = time.sleep,
    verify_seconds: float = _DEFAULT_VERIFY_SECONDS,
) -> dict[str, object]:
    """Run the monitor and apply at most one allowlisted recovery operation."""

    clean_mode = str(mode).strip().lower()
    if clean_mode not in {"hourly", "daily", "weekly"}:
        raise ValueError("mode must be hourly, daily, or weekly")
    root = Path(datastore_root).resolve()
    now = _utc(observed_at if observed_at is not None else datetime.now(timezone.utc))
    read_processes = process_reader or _windows_process_rows
    check_pid = pid_exists or _windows_pid_exists
    stop_tree = stop_process_tree or _stop_windows_process_tree
    launch = start_runtime or _start_windows_runtime
    read_update_events = codex_update_event_reader or _windows_codex_update_events
    launch_all = all_runtime_launcher or _run_checked_in_all_runtime_launcher
    rows = tuple(process_rows) if process_rows is not None else tuple(read_processes())
    before = build_monitor_report(
        root,
        mode=clean_mode,
        observed_at=now,
        process_rows=rows,
    )
    audit_directory = _audit_directory(root)
    update_events: tuple[Mapping[str, object], ...] = ()
    update_event_error: str | None = None
    if repair_codex_forced_update and _all_runtime_modules_absent(rows):
        try:
            _require_canonical_recovery_root(root)
            update_events = tuple(read_update_events(now))
        except Exception as exc:
            update_event_error = (
                f"{type(exc).__name__}: {_normalize_spaces(str(exc))}"
            )[:1000]
    decision = plan_guarded_recovery(
        root,
        monitor_report=before,
        process_rows=rows,
        observed_at=now,
        pid_exists=check_pid,
        audit_directory=audit_directory,
        allow_codex_forced_update=repair_codex_forced_update,
        codex_update_events=update_events,
        codex_update_event_error=update_event_error,
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
    if (
        decision.get("status") == "ELIGIBLE_CODEX_APPX_FORCED_UPDATE_ALL_EIGHT"
        and repair_codex_forced_update
    ):
        remediation, final_rows = _execute_codex_forced_update_recovery(
            root,
            decision=decision,
            observed_at=now,
            process_reader=read_processes,
            pid_exists=check_pid,
            launch_all_runtimes=launch_all,
            audit_directory=audit_directory,
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
    elif decision.get("status") == "ELIGIBLE" and repair_liveness:
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
            "scope": (
                "ONE_ALLOWLISTED_RUNTIME_OR_ONE_EXACT_CODEX_FORCED_UPDATE_RECOVERY_PER_RUN"
            ),
            "repair_enabled": bool(repair_liveness),
            "codex_forced_update_repair_enabled": bool(repair_codex_forced_update),
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
    allow_codex_forced_update: bool = False,
    codex_update_events: Sequence[Mapping[str, object]] = (),
    codex_update_event_error: str | None = None,
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
        if len(failed_runtimes) == len(RUNTIMES):
            if not allow_codex_forced_update:
                return _decision(
                    "REPORT_ONLY",
                    "Multiple runtime ownership failures are ambiguous; no process was changed.",
                    affected_runtimes=[spec.name for spec in failed_runtimes],
                )
            return _plan_codex_forced_update_recovery(
                root,
                monitor_report=monitor_report,
                process_rows=rows,
                observed_at=now,
                pid_exists=pid_exists,
                audit_directory=audit,
                events=codex_update_events,
                event_error=codex_update_event_error,
            )
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


def _all_runtime_modules_absent(
    rows: Sequence[Mapping[str, object]],
) -> bool:
    normalized = tuple(_normalize_process(row) for row in rows)
    return all(not _matching_runtime_rows(normalized, runtime) for runtime in RUNTIMES)


def _plan_codex_forced_update_recovery(
    root: Path,
    *,
    monitor_report: Mapping[str, object],
    process_rows: Sequence[Mapping[str, object]],
    observed_at: pd.Timestamp,
    pid_exists: Callable[[int], bool],
    audit_directory: Path,
    events: Sequence[Mapping[str, object]],
    event_error: str | None,
) -> dict[str, object]:
    if not _all_runtime_modules_absent(process_rows):
        return _decision(
            "REPORT_ONLY",
            "At least one runtime module still has a process; whole-stack recovery is forbidden.",
        )
    if event_error:
        return _decision(
            "REPORT_ONLY",
            "The Codex AppX event log could not be verified; whole-stack recovery is forbidden.",
            event_error=event_error,
        )
    try:
        candidates = _validated_codex_forced_update_events(events, now=observed_at)
    except (TypeError, ValueError) as exc:
        return _decision(
            "REPORT_ONLY",
            "No recent exact Codex forced-update event passed the recovery contract.",
            event_error=f"{type(exc).__name__}: {_normalize_spaces(str(exc))}"[:1000],
        )
    if not candidates:
        return _decision(
            "REPORT_ONLY",
            "No recent exact Codex forced-update event passed the recovery contract.",
        )
    unconsumed_candidates: list[dict[str, object]] = []
    consumed_candidates: list[tuple[dict[str, object], str, Path]] = []
    for event in candidates:
        fingerprint = _codex_update_fingerprint(event)
        consumed = _codex_update_attempt_path(audit_directory, fingerprint)
        if consumed.exists():
            consumed_candidates.append((event, fingerprint, consumed))
        else:
            unconsumed_candidates.append(event)
    if not unconsumed_candidates:
        event, fingerprint, consumed = consumed_candidates[0]
        return _decision(
            "REPORT_ONLY",
            "This exact Codex forced-update event already has a terminal recovery attempt.",
            event=event,
            event_fingerprint=fingerprint,
            prior_attempt=str(consumed),
        )
    candidates = tuple(unconsumed_candidates)

    blockers = _codex_forced_update_monitor_blockers(monitor_report)
    if blockers:
        return _decision(
            "REPORT_ONLY",
            "Storage, order-safety, or receipt-integrity evidence blocks whole-stack recovery.",
            blockers=blockers,
            candidate_event_record_ids=[
                int(event["event_record_id"]) for event in candidates
            ],
        )

    matches: list[tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]] = []
    signature_errors: list[dict[str, object]] = []
    for event in candidates:
        try:
            locks, logs = _codex_forced_update_signature_for_event(
                root,
                monitor_report=monitor_report,
                process_rows=process_rows,
                pid_exists=pid_exists,
                event=event,
            )
            matches.append((event, locks, logs))
        except Exception as exc:
            signature_errors.append(
                {
                    "event_record_id": event.get("event_record_id"),
                    "error": f"{type(exc).__name__}: {_normalize_spaces(str(exc))}"[:1000],
                }
            )
    if len(matches) != 1:
        return _decision(
            "REPORT_ONLY",
            (
                "The exact dead-lock and abrupt-log signature did not verify."
                if not matches
                else "More than one update event matches the lock/log signature; attribution is ambiguous."
            ),
            matching_event_record_ids=[
                int(item[0]["event_record_id"]) for item in matches
            ],
            signature_errors=signature_errors,
        )
    event, locks, logs = matches[0]
    fingerprint = _codex_update_fingerprint(event)
    return _decision(
        "ELIGIBLE_CODEX_APPX_FORCED_UPDATE_ALL_EIGHT",
        "All eight owners share one exact, unconsumed Codex AppX forced-update signature.",
        runtime="all_runtimes",
        fault="CODEX_APPX_FORCED_UPDATE_ALL_EIGHT",
        affected_runtimes=[runtime.name for runtime in RUNTIMES],
        event=event,
        event_fingerprint=fingerprint,
        locks=locks,
        logs=logs,
    )


def _validated_codex_forced_update_events(
    events: Sequence[Mapping[str, object]], *, now: pd.Timestamp
) -> tuple[dict[str, object], ...]:
    valid: list[dict[str, object]] = []
    for raw in events:
        try:
            event = dict(raw)
            occurred = _utc(event.get("occurred_at"))
            age = now - occurred
            if not (
                -_CODEX_FORCED_UPDATE_FUTURE_TOLERANCE
                <= age
                <= _CODEX_FORCED_UPDATE_MAXIMUM_AGE
            ):
                continue
            if int(event.get("event_id", -1)) != 603:
                continue
            if int(event.get("deployment_operation", -1)) != 20:
                continue
            if str(event.get("package_family")) != _CODEX_PACKAGE_FAMILY:
                continue
            if not int(event.get("flags_high", 0)) & _FORCE_TARGET_APPLICATION_SHUTDOWN_FLAG_HIGH:
                continue
            if "wuauserv" not in str(event.get("calling_process", "")).lower():
                continue
            old_package = str(event.get("old_package", ""))
            new_package = str(event.get("new_package", ""))
            if (
                not _is_exact_codex_package_full_name(old_package)
                or not _is_exact_codex_package_full_name(new_package)
                or old_package == new_package
            ):
                continue
            deployment_activity = _normalized_activity_id(
                event.get("deployment_activity_id")
            )
            if not deployment_activity or any(
                _normalized_activity_id(event.get(name)) != deployment_activity
                for name in ("update_activity_id", "register_activity_id")
            ):
                continue
            if int(event.get("update_event_id", -1)) != 855:
                continue
            if int(event.get("destroyed_event_id", -1)) != 217:
                continue
            if int(event.get("register_event_id", -1)) != 400:
                continue
            if int(event.get("replacement_container_event_id", -1)) != 210:
                continue
            if int(event.get("replacement_process_event_id", -1)) != 201:
                continue
            if int(event.get("register_deployment_operation", -1)) != 6:
                continue
            if "wuauserv" not in str(
                event.get("register_calling_process", "")
            ).lower():
                continue
            if any(
                int(event.get(name, -1)) <= 0
                for name in (
                    "event_record_id",
                    "update_event_record_id",
                    "destroyed_event_record_id",
                    "register_event_record_id",
                    "replacement_container_event_record_id",
                    "replacement_process_event_record_id",
                )
            ):
                continue
            if not (
                int(event["event_record_id"])
                < int(event["update_event_record_id"])
                < int(event["register_event_record_id"])
            ):
                continue
            if not (
                int(event["destroyed_event_record_id"])
                < int(event["replacement_container_event_record_id"])
                < int(event["replacement_process_event_record_id"])
            ):
                continue
            if any(
                str(event.get(name, "")) != expected
                for name, expected in (
                    ("update_old_package", old_package),
                    ("destroyed_package", old_package),
                    ("update_new_package", new_package),
                    ("register_package", new_package),
                    ("replacement_container_package", new_package),
                    ("replacement_process_package", new_package),
                    ("replacement_application_name", _CODEX_APPLICATION_NAME),
                    ("replacement_image_name", _CODEX_IMAGE_NAME),
                )
            ):
                continue
            if not str(event.get("replacement_container_id", "")).strip():
                continue
            update_time = _utc(event.get("update_occurred_at"))
            destroyed_time = _utc(event.get("destroyed_occurred_at"))
            register_time = _utc(event.get("register_occurred_at"))
            created_time = _utc(event.get("replacement_container_occurred_at"))
            launched_time = _utc(event.get("replacement_process_occurred_at"))
            last_boot = _utc(event.get("last_boot_up_at"))
            if not (
                last_boot
                < occurred
                <= update_time
                <= destroyed_time
                <= register_time
                <= created_time
                <= launched_time
                <= occurred + pd.Timedelta(seconds=30)
            ):
                continue
            if event.get("boundary_exclusions_verified") is not True:
                continue
            system_boundaries = event.get("competing_system_boundary_events")
            logoff_boundaries = event.get("competing_logoff_events")
            if (
                not isinstance(system_boundaries, (list, tuple))
                or not isinstance(logoff_boundaries, (list, tuple))
                or system_boundaries
                or logoff_boundaries
            ):
                continue
            if not all(
                bool(event.get(name))
                for name in (
                    "old_container_destroyed",
                    "registration_succeeded",
                    "replacement_container_created",
                    "replacement_process_launched",
                )
            ):
                continue
            event["occurred_at"] = occurred.isoformat()
            valid.append(event)
        except (TypeError, ValueError):
            continue
    return tuple(
        sorted(valid, key=lambda value: _utc(value["occurred_at"]), reverse=True)
    )


def _normalized_activity_id(value: object) -> str:
    return str(value or "").strip().lower()


def _is_exact_codex_package_full_name(value: object) -> bool:
    return bool(
        re.fullmatch(
            r"OpenAI\.Codex_\d+(?:\.\d+){3}_x64__2p2nqsd0c76g0",
            str(value or ""),
        )
    )


def _codex_forced_update_signature_for_event(
    root: Path,
    *,
    monitor_report: Mapping[str, object],
    process_rows: Sequence[Mapping[str, object]],
    pid_exists: Callable[[int], bool],
    event: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    event_time = _utc(event["occurred_at"])
    last_boot = _utc(event["last_boot_up_at"])
    locks: list[dict[str, object]] = []
    for runtime in RUNTIMES:
        if _matching_runtime_rows(process_rows, runtime):
            raise RuntimeError(f"{runtime.name}:competing-owner")
        path = _exact_lock_path(root, runtime)
        record = _read_exact_lock_record(path)
        if record is None:
            raise RuntimeError(f"{runtime.name}:missing-or-invalid-lock")
        started_at = _utc(record["started_at"])
        if pid_exists(int(record["pid"])):
            raise RuntimeError(f"{runtime.name}:lock-pid-still-alive")
        if started_at <= last_boot:
            raise RuntimeError(f"{runtime.name}:lock-does-not-postdate-last-boot")
        if started_at >= event_time:
            raise RuntimeError(f"{runtime.name}:lock-does-not-predate-event")
        locks.append(
            {
                "runtime": runtime.name,
                "path": str(path),
                "pid": int(record["pid"]),
                "started_at": str(record["started_at"]),
                "sha256": _sha256_file(path),
                "file_identity": _file_identity(path),
            }
        )
    logs = _codex_forced_update_log_evidence(
        root,
        monitor_report=monitor_report,
        event_time=event_time,
    )
    return locks, logs


def _codex_forced_update_monitor_blockers(
    monitor_report: Mapping[str, object],
) -> list[str]:
    blockers = list(_integrity_blockers(monitor_report))
    try:
        orders_placed = int(monitor_report.get("orders_placed", -1))
    except (TypeError, ValueError):
        orders_placed = -1
    if orders_placed != 0:
        blockers.append("orders_placed")
    if monitor_report.get("automated_action_allowed") is not False:
        blockers.append("automated_action_allowed")
    if monitor_report.get("read_only") is not True:
        blockers.append("read_only")
    checks = {
        str(check.get("name")): check
        for check in monitor_report.get("checks", [])
        if isinstance(check, Mapping)
    }
    for name in _CODEX_FORCED_UPDATE_REQUIRED_PASS_CHECKS:
        check = checks.get(name)
        if not isinstance(check, Mapping) or check.get("status") != "PASS":
            blockers.append(name)
    if not _forced_update_pricing_authorities_are_valid(
        checks.get("pricing_publications")
    ):
        blockers.append("pricing_publications")
    if not _forced_update_loop_a_publication_is_valid(checks.get("loop_a_cycle")):
        blockers.append("loop_a_cycle")
    if not _forced_update_loop_b_publication_is_valid(
        checks.get("loop_b_publication")
    ):
        blockers.append("loop_b_publication")
    if not _forced_update_strategy_publication_is_valid(
        checks.get("strategy_publication")
    ):
        blockers.append("strategy_publication")
    if not _forced_update_sequence_authority_is_valid(
        checks.get("sequence_encoder_loop_c")
    ):
        blockers.append("sequence_encoder_loop_c")
    return sorted(set(blockers))


def _forced_update_pricing_authorities_are_valid(check: object) -> bool:
    if not isinstance(check, Mapping) or check.get("status") != "PASS":
        return False
    details = check.get("details")
    if not isinstance(details, Mapping):
        return False
    target = details.get("target_authority")
    full = details.get("full_generation")
    if not isinstance(target, Mapping) or not isinstance(full, Mapping):
        return False
    try:
        expected_target = _utc(details["expected_target"])
        target_snapshot = _utc(target["target_snapshot_for"])
        _utc(target["published_at"])
        prediction_rows = int(target.get("prediction_rows", 0))
        shadow_rows = int(target.get("shadow_rows", 0))
        _utc(full["published_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        target_snapshot == expected_target
        and target.get("terminal_status")
        in {"PREDICTIONS_PUBLISHED", "MIXED_TERMINAL"}
        and prediction_rows > 0
        and shadow_rows > 0
        and full.get("status") == "VERIFIED"
        and bool(full.get("run_path"))
    )


def _forced_update_sequence_authority_is_valid(check: object) -> bool:
    if not isinstance(check, Mapping):
        return False
    details = check.get("details")
    if not isinstance(details, Mapping):
        return False
    try:
        orders_placed = int(details.get("orders_placed", -1))
    except (TypeError, ValueError):
        return False
    if (
        details.get("automated_action_allowed") is not False
        or details.get("orders_enabled") is not False
        or orders_placed != 0
    ):
        return False
    sequence_status = details.get("sequence_status")
    loop_c_status = details.get("loop_c_status")
    if check.get("status") not in {"INFO", "PASS"}:
        return False
    if details.get("warnings") not in (None, [], ()):
        return False
    if sequence_status not in {"NOT_PUBLISHED", "VERIFIED_SHADOW"}:
        return False
    if loop_c_status not in {"NOT_PUBLISHED", "VERIFIED_OBSERVE_ONLY"}:
        return False
    expected_status = "PASS" if sequence_status == "VERIFIED_SHADOW" else "INFO"
    if check.get("status") != expected_status:
        return False
    if sequence_status == "VERIFIED_SHADOW" and not details.get("sequence_run"):
        return False
    if loop_c_status == "VERIFIED_OBSERVE_ONLY" and not details.get("loop_c_run"):
        return False
    if sequence_status == loop_c_status == "NOT_PUBLISHED":
        return details.get("authority") == "NONE"
    return True


def _forced_update_loop_a_publication_is_valid(check: object) -> bool:
    if not isinstance(check, Mapping):
        return False
    if check.get("status") == "PASS":
        return True
    details = check.get("details")
    if check.get("status") != "FAIL" or not isinstance(details, Mapping):
        return False
    failures = {str(value) for value in details.get("failures", [])}
    allowed = {"active-cycle-running-too-long", "latest-complete-cycle-stale"}
    if not failures or not failures <= allowed:
        return False
    try:
        failure_count = int(details.get("failure_count", -1))
        active_age = float(details.get("active_cycle_age_minutes", -1.0))
        complete_age = float(details.get("age_minutes", -1.0))
        _utc(details["finished_at"])
    except (KeyError, TypeError, ValueError):
        return False
    active_status = details.get("active_cycle_status")
    if failure_count != 0 or not details.get("last_complete_generation"):
        return False
    if active_status not in {"WRITING", "COMPLETE"}:
        return False
    if "active-cycle-running-too-long" in failures and (
        active_status != "WRITING" or active_age <= 45.0
    ):
        return False
    if "latest-complete-cycle-stale" in failures and complete_age <= 45.0:
        return False
    if active_status == "COMPLETE" and failures != {"latest-complete-cycle-stale"}:
        return False
    return True


def _forced_update_loop_b_publication_is_valid(check: object) -> bool:
    if not isinstance(check, Mapping):
        return False
    if check.get("status") == "PASS":
        return True
    details = check.get("details")
    if not isinstance(details, Mapping):
        return False
    failures = {str(value) for value in details.get("failures", [])}
    warnings = {str(value) for value in details.get("warnings", [])}
    try:
        rows = int(details.get("intelligence_rows", -1))
        expected = int(details.get("expected_routes", -1))
        age = float(details.get("age_minutes", -1.0))
        _utc(details["run_timestamp"])
        _utc(details["authoritative_timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    if (
        not details.get("run_path")
        or expected <= 0
        or rows != expected
    ):
        return False
    if check.get("status") == "WARN":
        return (
            not failures
            and warnings == {"publication-approaching-stale"}
            and 35.0 < age <= 45.0
        )
    return (
        check.get("status") == "FAIL"
        and failures == {"publication-stale"}
        and not warnings
        and age > 45.0
    )


def _forced_update_strategy_publication_is_valid(check: object) -> bool:
    if not isinstance(check, Mapping):
        return False
    if check.get("status") == "PASS":
        return True
    details = check.get("details")
    if check.get("status") != "FAIL" or not isinstance(details, Mapping):
        return False
    try:
        candidate_rows = int(details.get("candidate_rows", 0))
        audit_rows = int(details.get("audit_rows", -1))
        age = float(details.get("age_minutes", -1.0))
        _utc(details["run_timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        bool(details.get("run_path"))
        and candidate_rows > 0
        and audit_rows >= 0
        and age > 45.0
    )


def _read_exact_lock_record(path: Path) -> dict[str, object] | None:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    pid_match = re.search(r"(?m)^pid=(\d+)\s*$", text)
    started_match = re.search(r"(?m)^started_at=([^\r\n]+)\s*$", text)
    if pid_match is None or started_match is None:
        return None
    try:
        started_at = _utc(started_match.group(1)).isoformat()
    except ValueError:
        return None
    return {"pid": int(pid_match.group(1)), "started_at": started_at}


def _codex_forced_update_log_evidence(
    root: Path,
    *,
    monitor_report: Mapping[str, object],
    event_time: pd.Timestamp,
) -> list[dict[str, object]]:
    checks = {
        str(check.get("name")): check
        for check in monitor_report.get("checks", [])
        if isinstance(check, Mapping)
    }
    log_check = checks.get("runtime_logs")
    details = log_check.get("details") if isinstance(log_check, Mapping) else None
    runtime_views = details.get("runtimes") if isinstance(details, Mapping) else None
    if not isinstance(runtime_views, Mapping):
        raise RuntimeError("runtime log inventory is unavailable")
    authority = (Path(root).resolve() / "logs" / "ducketz").resolve()
    evidence: list[dict[str, object]] = []
    clustered: set[str] = set()
    for runtime in RUNTIMES:
        view = runtime_views.get(runtime.name)
        if not isinstance(view, Mapping):
            raise RuntimeError(f"{runtime.name}:log-view-missing")
        stdout_value = view.get("stdout")
        stderr_value = view.get("stderr")
        if not isinstance(stdout_value, str) or not isinstance(stderr_value, str):
            raise RuntimeError(f"{runtime.name}:paired-log-path-missing")
        stdout = Path(stdout_value).resolve()
        stderr = Path(stderr_value).resolve()
        if authority not in stdout.parents or authority not in stderr.parents:
            raise RuntimeError(f"{runtime.name}:log-outside-primary-authority")
        if not stdout.is_file() or not stderr.is_file():
            raise RuntimeError(f"{runtime.name}:paired-log-file-missing")
        if stdout.stat().st_size == 0:
            raise RuntimeError(f"{runtime.name}:stdout-empty")
        if stderr.stat().st_size != 0:
            raise RuntimeError(f"{runtime.name}:stderr-not-empty")
        stdout_time = pd.Timestamp(stdout.stat().st_mtime, unit="s", tz="UTC")
        stderr_time = pd.Timestamp(stderr.stat().st_mtime, unit="s", tz="UTC")
        if stdout_time > event_time + _CODEX_FORCED_UPDATE_LOG_TOLERANCE:
            raise RuntimeError(f"{runtime.name}:stdout-after-update-event")
        if stderr_time > event_time + _CODEX_FORCED_UPDATE_LOG_TOLERANCE:
            raise RuntimeError(f"{runtime.name}:stderr-after-update-event")
        tail = _last_log_line(stdout).lower()
        if any(tail.endswith(marker) for marker in _GRACEFUL_STOP_MARKERS):
            raise RuntimeError(f"{runtime.name}:graceful-stop-observed")
        if (
            runtime.name in _ALWAYS_ACTIVE_LOG_RUNTIMES
            and pd.Timedelta(0)
            <= event_time - stdout_time
            <= _CODEX_FORCED_UPDATE_ACTIVE_LOG_WINDOW
        ):
            clustered.add(runtime.name)
        evidence.append(
            {
                "runtime": runtime.name,
                "stdout": str(stdout),
                "stdout_mtime": stdout_time.isoformat(),
                "stdout_size": int(stdout.stat().st_size),
                "stderr": str(stderr),
                "stderr_size": 0,
            }
        )
    if clustered != _ALWAYS_ACTIVE_LOG_RUNTIMES:
        missing = sorted(_ALWAYS_ACTIVE_LOG_RUNTIMES - clustered)
        raise RuntimeError(f"active-log-cluster-incomplete:{','.join(missing)}")
    return evidence


def _last_log_line(path: Path) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 8192))
        tail = handle.read().decode("utf-8", errors="replace")
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, int]:
    stat = Path(path).stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _codex_update_fingerprint(event: Mapping[str, object]) -> str:
    fields = {
        name: event.get(name)
        for name in (
            "event_id",
            "event_record_id",
            "occurred_at",
            "package_family",
            "deployment_operation",
            "flags",
            "flags_high",
            "calling_process",
            "deployment_activity_id",
            "update_event_record_id",
            "update_activity_id",
            "update_old_package",
            "update_new_package",
            "destroyed_event_record_id",
            "destroyed_package",
            "register_event_record_id",
            "register_activity_id",
            "register_package",
            "replacement_container_event_record_id",
            "replacement_container_package",
            "replacement_container_id",
            "replacement_process_event_record_id",
            "replacement_process_package",
            "replacement_application_name",
            "replacement_image_name",
            "old_package",
            "new_package",
            "last_boot_up_at",
        )
    }
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _codex_update_attempt_path(audit_directory: Path, fingerprint: str) -> Path:
    return Path(audit_directory).resolve() / f"forced-update-attempt-{fingerprint}.json"


def _revalidate_forced_update_lock(
    root: Path,
    *,
    runtime: RuntimeSpec,
    expected: Mapping[str, object],
    pid_exists: Callable[[int], bool],
) -> None:
    path = _exact_lock_path(root, runtime)
    record = _read_exact_lock_record(path)
    if record is None:
        raise RuntimeError(f"{runtime.name}:lock changed or disappeared")
    if str(path) != str(expected.get("path")):
        raise RuntimeError(f"{runtime.name}:lock path changed")
    if int(record["pid"]) != int(expected.get("pid", -1)):
        raise RuntimeError(f"{runtime.name}:lock PID changed")
    if str(record["started_at"]) != str(expected.get("started_at")):
        raise RuntimeError(f"{runtime.name}:lock start time changed")
    if _file_identity(path) != expected.get("file_identity"):
        raise RuntimeError(f"{runtime.name}:lock file identity changed")
    if _sha256_file(path) != str(expected.get("sha256")):
        raise RuntimeError(f"{runtime.name}:lock content changed")
    if pid_exists(int(record["pid"])):
        raise RuntimeError(f"{runtime.name}:lock PID became live")


def _execute_codex_forced_update_recovery(
    root: Path,
    *,
    decision: Mapping[str, object],
    observed_at: pd.Timestamp,
    process_reader: Callable[[], Sequence[Mapping[str, object]]],
    pid_exists: Callable[[int], bool],
    launch_all_runtimes: Callable[[Path, pd.Timestamp], Mapping[str, object]],
    audit_directory: Path,
    expected_recovery_root_for_test: Path | None = None,
) -> tuple[dict[str, object], Sequence[Mapping[str, object]] | None]:
    fingerprint = str(decision["event_fingerprint"])
    result: dict[str, object] = {
        "status": "CODEX_FORCED_UPDATE_RECOVERY_FAILED",
        "summary": "The exact forced-update recovery failed closed.",
        "runtime": "all_runtimes",
        "fault": "CODEX_APPX_FORCED_UPDATE_ALL_EIGHT",
        "attempted_at": observed_at.isoformat(),
        "event_fingerprint": fingerprint,
        "quarantined_locks": [],
        "launcher_invocations": 0,
        "launcher": None,
        "verification": "NOT_RUN",
        "orders_placed": 0,
    }
    final_rows: Sequence[Mapping[str, object]] | None = None
    try:
        if expected_recovery_root_for_test is None:
            _require_canonical_recovery_root(root)
        elif Path(root).resolve() != Path(expected_recovery_root_for_test).resolve():
            raise RuntimeError("The injected recovery datastore does not match the caller root")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {_normalize_spaces(str(exc))}"[:1000]
        result["verification"] = "NONCANONICAL_DATASTORE"
        return result, final_rows
    audit = Path(audit_directory).resolve()
    audit.mkdir(parents=True, exist_ok=True)
    mutex = FileLock(str(audit / "forced-update-recovery.lock"), timeout=0)
    moved: list[tuple[Path, Path]] = []
    lock_phase = "RECOVERY_MUTEX"
    try:
        with mutex:
            lock_phase = "LOCK_MAINTENANCE"
            with runtime_lock_maintenance_gate(Path(root).resolve(), timeout=5.0):
                attempt = _codex_update_attempt_path(audit, fingerprint)
                _atomic_write_unique_json(
                    attempt,
                    {
                        "schema_version": GUARDIAN_SCHEMA_VERSION,
                        "status": "CONSUMED_BEFORE_REVALIDATION",
                        "event_fingerprint": fingerprint,
                        "attempted_at": observed_at.isoformat(),
                        "orders_placed": 0,
                    },
                )
                result["event_attempt_receipt"] = str(attempt)
                current_rows = tuple(process_reader())
                if not _all_runtime_modules_absent(current_rows):
                    raise RuntimeError("A runtime owner appeared before recovery")
                expected_locks = decision.get("locks")
                if not isinstance(expected_locks, list) or len(expected_locks) != len(RUNTIMES):
                    raise RuntimeError("The recovery decision does not name exactly eight locks")
                by_runtime = {
                    str(value.get("runtime")): value
                    for value in expected_locks
                    if isinstance(value, Mapping)
                }
                if set(by_runtime) != {runtime.name for runtime in RUNTIMES}:
                    raise RuntimeError("The recovery decision lock set is incomplete")
                for runtime in RUNTIMES:
                    _revalidate_forced_update_lock(
                        root,
                        runtime=runtime,
                        expected=by_runtime[runtime.name],
                        pid_exists=pid_exists,
                    )

                lock_phase = "MUTATION"
                quarantine = (
                    Path(root).resolve()
                    / "logs"
                    / "ducketz"
                    / "manual-recovery"
                    / f"{_timestamp_slug(observed_at)}-codex-appx-update"
                ).resolve()
                quarantine.mkdir(parents=True, exist_ok=False)
                try:
                    for runtime in RUNTIMES:
                        source = _exact_lock_path(root, runtime)
                        _revalidate_forced_update_lock(
                            root,
                            runtime=runtime,
                            expected=by_runtime[runtime.name],
                            pid_exists=pid_exists,
                        )
                        target = quarantine / source.name
                        source.replace(target)
                        moved.append((source, target))
                except Exception:
                    for source, target in reversed(moved):
                        if target.exists() and not source.exists():
                            target.replace(source)
                    if moved:
                        result["quarantine_rolled_back"] = True
                        moved.clear()
                    raise
                result["quarantined_locks"] = [str(target) for _source, target in moved]

            result["launcher_invocations"] = 1
            launcher = dict(launch_all_runtimes(root, observed_at))
            result["launcher"] = launcher
            if int(launcher.get("exit_code", -1)) != 0:
                raise RuntimeError("The all-runtime launcher returned a nonzero exit code")
            owners = launcher.get("owners")
            if not isinstance(owners, list):
                raise RuntimeError("The all-runtime launcher returned no owner inventory")
            statuses = {
                str(owner.get("runtime")): str(owner.get("status"))
                for owner in owners
                if isinstance(owner, Mapping)
            }
            expected_names = {runtime.name for runtime in RUNTIMES}
            if set(statuses) != expected_names or any(
                statuses[name] != "STARTED_VERIFIED" for name in expected_names
            ):
                raise RuntimeError("The all-runtime launcher did not verify all eight starts")
            if launcher.get("issues") not in ([], (), None):
                raise RuntimeError("The all-runtime launcher reported issues")
            if launcher.get("audit_only") is True:
                raise RuntimeError("The all-runtime launcher unexpectedly ran audit-only")
            if launcher.get("require_all_missing") is not True:
                raise RuntimeError("The launcher did not attest its all-missing preflight")
            if Path(str(launcher.get("datastore", ""))).resolve() != Path(root).resolve():
                raise RuntimeError("The all-runtime launcher used a different datastore")
            canonical_log_root = Path(str(launcher.get("canonical_log_root", ""))).resolve()
            expected_log_root = (
                Path(root).resolve() / "logs" / "ducketz" / "background-launch"
            ).resolve()
            if canonical_log_root != expected_log_root:
                raise RuntimeError("The all-runtime launcher used a noncanonical log root")
            for owner in owners:
                if not isinstance(owner, Mapping):
                    raise RuntimeError("The launcher owner inventory is malformed")
                stderr_value = owner.get("stderr")
                if not isinstance(stderr_value, str):
                    raise RuntimeError(f"{owner.get('runtime')}:startup-stderr-path-missing")
                stderr = Path(stderr_value).resolve()
                if canonical_log_root not in stderr.parents or not stderr.is_file():
                    raise RuntimeError(f"{owner.get('runtime')}:startup-stderr-not-canonical")
                if stderr.stat().st_size != 0:
                    raise RuntimeError(f"{owner.get('runtime')}:startup-stderr-not-empty")

            final_rows = tuple(process_reader())
            for runtime in RUNTIMES:
                matches = _matching_runtime_rows(final_rows, runtime)
                launch_spec = _LAUNCH_BY_RUNTIME[runtime.name]
                if len(matches) != 2 or not all(
                    _is_exact_allowlisted_command(row, launch_spec) for row in matches
                ):
                    raise RuntimeError(
                        f"{runtime.name}:post-launch canonical command verification failed"
                    )
                if not _runtime_pair_and_lock_ready(
                    root, runtime, final_rows, pid_exists
                ):
                    raise RuntimeError(f"{runtime.name}:post-launch pair/lock verification failed")
            result["status"] = "CODEX_FORCED_UPDATE_RECOVERED"
            result["summary"] = (
                "The exact update event was consumed, eight dead locks were quarantined, "
                "and one canonical launcher invocation verified all eight owners."
            )
            result["verification"] = "ALL_PAIRS_AND_LOCKS_OWNED"
    except Timeout:
        if lock_phase == "LOCK_MAINTENANCE":
            result["summary"] = "Runtime lock maintenance is busy; no event was consumed."
            result["verification"] = "LOCK_MAINTENANCE_BUSY"
        else:
            result["summary"] = "Another forced-update recovery owns the guardian mutex."
            result["verification"] = "RECOVERY_MUTEX_BUSY"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {_normalize_spaces(str(exc))}"[:1000]
        result["verification"] = "TERMINAL_FAILURE"
    return result, final_rows


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
            process_reader=process_reader,
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
    return _is_exact_allowlisted_command(row, launch)


def _is_exact_allowlisted_command(
    row: Mapping[str, object], launch: GuardianLaunchSpec
) -> bool:
    argv = _windows_command_line_argv(str(row.get("command_line", "")))
    expected_python = (
        Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    ).resolve()
    return (
        len(argv) == len(launch.arguments) + 1
        and Path(argv[0]).resolve() == expected_python
        and tuple(argv[1:]) == launch.arguments
    )


def _windows_command_line_argv(command_line: str) -> tuple[str, ...]:
    command = str(command_line or "").strip()
    if not command:
        return ()
    if os.name != "nt":
        return tuple(
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
            else token
            for token in shlex.split(command, posix=False)
        )
    argc = ctypes.c_int()
    command_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_to_argv.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
    command_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_to_argv(command, ctypes.byref(argc))
    if not argv:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return tuple(argv[index] for index in range(argc.value))
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


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
    process_reader: Callable[[], Sequence[Mapping[str, object]]],
    pid_exists: Callable[[int], bool],
) -> bool:
    if not isinstance(expected, Mapping):
        raise RuntimeError("The pre-restart lock assessment is missing")
    with runtime_lock_maintenance_gate(Path(root).resolve(), timeout=0):
        if _matching_runtime_rows(tuple(process_reader()), runtime):
            raise RuntimeError("A runtime owner appeared before lock preparation")
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


def _windows_codex_update_events(
    now: pd.Timestamp,
) -> Sequence[Mapping[str, object]]:
    if os.name != "nt":
        raise RuntimeError("Codex AppX event verification is implemented for Windows")
    start = (
        now
        - _CODEX_FORCED_UPDATE_MAXIMUM_AGE
        - _CODEX_FORCED_UPDATE_ACTIVE_LOG_WINDOW
    ).isoformat()
    end = (now + _CODEX_FORCED_UPDATE_FUTURE_TOLERANCE).isoformat()
    script = rf"""
$ErrorActionPreference = 'Stop'
$diagnosticsModule = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.Diagnostics\Microsoft.PowerShell.Diagnostics.psd1'
Import-Module -Name $diagnosticsModule -Force -ErrorAction Stop
$start = ([DateTimeOffset]::Parse('{_powershell_quote(start)}')).LocalDateTime
$end = ([DateTimeOffset]::Parse('{_powershell_quote(end)}')).LocalDateTime
function Convert-EventData($event) {{
  [xml]$xml = $event.ToXml()
  $data = @{{}}
  foreach ($item in @($xml.Event.EventData.Data)) {{
    $data[[string]$item.Name] = [string]$item.'#text'
  }}
  return $data
}}
function Get-ActivityId($event) {{
  [xml]$xml = $event.ToXml()
  return ([string]$xml.Event.System.Correlation.ActivityID).Trim().ToLowerInvariant()
}}
function Get-TerminalEventUser($event) {{
  [xml]$xml = $event.ToXml()
  return [string]$xml.Event.UserData.EventXML.User
}}
function Get-OptionalEvents($filter) {{
  try {{
    return @(Get-WinEvent -FilterHashtable $filter -ErrorAction Stop)
  }} catch {{
    if ([string]$_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') {{ return @() }}
    throw
  }}
}}
$deployments = @(Get-WinEvent -FilterHashtable @{{LogName='Microsoft-Windows-AppXDeploymentServer/Operational'; Id=603; StartTime=$start; EndTime=$end}})
$updates = @(Get-WinEvent -FilterHashtable @{{LogName='Microsoft-Windows-AppXDeploymentServer/Operational'; Id=855; StartTime=$start; EndTime=$end}})
$registers = @(Get-WinEvent -FilterHashtable @{{LogName='Microsoft-Windows-AppXDeploymentServer/Operational'; Id=400; StartTime=$start; EndTime=$end}})
$appEvents = @(Get-WinEvent -FilterHashtable @{{LogName='Microsoft-Windows-AppModel-Runtime/Admin'; Id=201,210,217; StartTime=$start; EndTime=$end}})
$systemEvents = @(Get-OptionalEvents (@{{LogName='System'; Id=12,13,41,1074,6005,6006,6008,6009,7002; StartTime=$start; EndTime=$end}}))
$terminalLogName = 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'
$terminalLog = Get-WinEvent -ListLog $terminalLogName -ErrorAction Stop
if (-not $terminalLog.IsEnabled) {{ throw 'Terminal Services logoff authority is disabled' }}
$terminalLogoffs = @(Get-OptionalEvents (@{{LogName=$terminalLogName; Id=23; StartTime=$start; EndTime=$end}}))
try {{
  $securityLogoffs = @(Get-OptionalEvents (@{{LogName='Security'; Id=4647; StartTime=$start; EndTime=$end}}))
}} catch {{
  $securityLogoffs = @()
}}
$lastBoot = (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime()
$packagePattern = '^OpenAI\.Codex_\d+(?:\.\d+){{3}}_x64__2p2nqsd0c76g0$'
$updatePattern = '^\s*updateList:\s*(?<old>OpenAI\.Codex_\d+(?:\.\d+){{3}}_x64__2p2nqsd0c76g0)\s+is updating to\s+(?<new>OpenAI\.Codex_\d+(?:\.\d+){{3}}_x64__2p2nqsd0c76g0)\s*$'
$result = @()
foreach ($deployment in $deployments) {{
  $data = Convert-EventData $deployment
  if ([string]$data.Path -ne '{_CODEX_PACKAGE_FAMILY}') {{ continue }}
  $when = $deployment.TimeCreated.ToUniversalTime()
  $nearStart = $when.AddSeconds(-5)
  $nearEnd = $when.AddSeconds(30)
  $boundaryStart = $when.AddMinutes(-45)
  $boundaryEnd = $end.ToUniversalTime()
  $activity = Get-ActivityId $deployment

  $update = $null
  $updateData = $null
  $oldPackage = ''
  $newPackage = ''
  foreach ($candidate in @($updates | Where-Object {{
    $_.TimeCreated.ToUniversalTime() -ge $when -and
    $_.TimeCreated.ToUniversalTime() -le $nearEnd -and
    (Get-ActivityId $_) -eq $activity
  }} | Sort-Object TimeCreated)) {{
    $candidateData = Convert-EventData $candidate
    if ([string]$candidateData.PackageMoniker -match $updatePattern) {{
      $update = $candidate
      $updateData = $candidateData
      $oldPackage = [string]$Matches.old
      $newPackage = [string]$Matches.new
      break
    }}
  }}

  $destroyed = @($appEvents | Where-Object {{
    $eventData = Convert-EventData $_
    $_.Id -eq 217 -and $_.TimeCreated.ToUniversalTime() -ge $nearStart -and
    $_.TimeCreated.ToUniversalTime() -le $nearEnd -and
    [string]$eventData.PackageName -eq $oldPackage
  }} | Sort-Object TimeCreated | Select-Object -First 1)
  $registered = @($registers | Where-Object {{
    $eventData = Convert-EventData $_
    $_.TimeCreated.ToUniversalTime() -ge $when -and
    $_.TimeCreated.ToUniversalTime() -le $nearEnd -and
    (Get-ActivityId $_) -eq $activity -and
    [string]$eventData.PackageFullName -eq $newPackage
  }} | Sort-Object TimeCreated | Select-Object -First 1)
  $created = @($appEvents | Where-Object {{
    $eventData = Convert-EventData $_
    $_.Id -eq 210 -and $_.TimeCreated.ToUniversalTime() -ge $when -and
    $_.TimeCreated.ToUniversalTime() -le $nearEnd -and
    [string]$eventData.PackageName -eq $newPackage
  }} | Sort-Object TimeCreated | Select-Object -First 1)
  $launched = @($appEvents | Where-Object {{
    $eventData = Convert-EventData $_
    $_.Id -eq 201 -and $_.TimeCreated.ToUniversalTime() -ge $when -and
    $_.TimeCreated.ToUniversalTime() -le $nearEnd -and
    [string]$eventData.PackageName -eq $newPackage -and
    [string]$eventData.ApplicationName -eq '{_CODEX_APPLICATION_NAME}' -and
    [string]$eventData.ImageName -eq '{_CODEX_IMAGE_NAME}'
  }} | Sort-Object TimeCreated | Select-Object -First 1)

  $destroyedData = if ($destroyed.Count) {{ Convert-EventData $destroyed[0] }} else {{ @{{}} }}
  $registeredData = if ($registered.Count) {{ Convert-EventData $registered[0] }} else {{ @{{}} }}
  $createdData = if ($created.Count) {{ Convert-EventData $created[0] }} else {{ @{{}} }}
  $launchedData = if ($launched.Count) {{ Convert-EventData $launched[0] }} else {{ @{{}} }}
  $systemBoundaries = @($systemEvents | Where-Object {{
    $provider = [string]$_.ProviderName
    $inBoundary = $_.TimeCreated.ToUniversalTime() -ge $boundaryStart -and $_.TimeCreated.ToUniversalTime() -le $boundaryEnd
    $isBoundary = ($_.Id -in 12,13 -and $provider -eq 'Microsoft-Windows-Kernel-General') -or
      ($_.Id -eq 41 -and $provider -eq 'Microsoft-Windows-Kernel-Power') -or
      ($_.Id -eq 1074 -and $provider -eq 'User32') -or
      ($_.Id -in 6005,6006,6008,6009 -and $provider -eq 'EventLog') -or
      ($_.Id -eq 7002 -and $provider -eq 'Microsoft-Windows-Winlogon')
    $inBoundary -and $isBoundary
  }} | ForEach-Object {{
    [pscustomobject]@{{event_id=[int]$_.Id; event_record_id=[long]$_.RecordId; occurred_at=$_.TimeCreated.ToUniversalTime().ToString('o'); provider=[string]$_.ProviderName}}
  }})
  $terminalBoundaries = @()
  foreach ($terminalEvent in @($terminalLogoffs | Where-Object {{
    $_.TimeCreated.ToUniversalTime() -ge $boundaryStart -and $_.TimeCreated.ToUniversalTime() -le $boundaryEnd
  }})) {{
    $eventUser = (Get-TerminalEventUser $terminalEvent).Trim()
    if (-not $eventUser) {{ throw 'Terminal Services logoff event has ambiguous user identity' }}
    $sameUser = $eventUser -eq $env:USERNAME -or $eventUser.EndsWith("\$($env:USERNAME)", [System.StringComparison]::OrdinalIgnoreCase)
    if ($sameUser) {{
      $terminalBoundaries += [pscustomobject]@{{event_id=[int]$terminalEvent.Id; event_record_id=[long]$terminalEvent.RecordId; occurred_at=$terminalEvent.TimeCreated.ToUniversalTime().ToString('o'); provider=[string]$terminalEvent.ProviderName; user=$eventUser}}
    }}
  }}
  $securityBoundaries = @($securityLogoffs | Where-Object {{
    $_.TimeCreated.ToUniversalTime() -ge $boundaryStart -and $_.TimeCreated.ToUniversalTime() -le $boundaryEnd
  }} | ForEach-Object {{
    [pscustomobject]@{{event_id=[int]$_.Id; event_record_id=[long]$_.RecordId; occurred_at=$_.TimeCreated.ToUniversalTime().ToString('o'); provider=[string]$_.ProviderName}}
  }})
  $logoffBoundaries = @($terminalBoundaries) + @($securityBoundaries)

  $result += [pscustomobject]@{{
    event_id = 603
    event_record_id = [long]$deployment.RecordId
    occurred_at = $when.ToString('o')
    package_family = [string]$data.Path
    deployment_operation = [int]$data.DeploymentOperation
    flags = [long]$data.Flags
    flags_high = [long]$data.FlagsHigh
    calling_process = [string]$data.CallingProcess
    deployment_activity_id = $activity
    update_event_id = if ($null -ne $update) {{ 855 }} else {{ $null }}
    update_event_record_id = if ($null -ne $update) {{ [long]$update.RecordId }} else {{ $null }}
    update_occurred_at = if ($null -ne $update) {{ $update.TimeCreated.ToUniversalTime().ToString('o') }} else {{ $null }}
    update_activity_id = if ($null -ne $update) {{ Get-ActivityId $update }} else {{ '' }}
    update_old_package = $oldPackage
    update_new_package = $newPackage
    old_container_destroyed = [bool]$destroyed.Count
    destroyed_event_id = if ($destroyed.Count) {{ 217 }} else {{ $null }}
    destroyed_event_record_id = if ($destroyed.Count) {{ [long]$destroyed[0].RecordId }} else {{ $null }}
    destroyed_occurred_at = if ($destroyed.Count) {{ $destroyed[0].TimeCreated.ToUniversalTime().ToString('o') }} else {{ $null }}
    destroyed_package = [string]$destroyedData.PackageName
    registration_succeeded = [bool]$registered.Count
    register_event_id = if ($registered.Count) {{ 400 }} else {{ $null }}
    register_event_record_id = if ($registered.Count) {{ [long]$registered[0].RecordId }} else {{ $null }}
    register_occurred_at = if ($registered.Count) {{ $registered[0].TimeCreated.ToUniversalTime().ToString('o') }} else {{ $null }}
    register_activity_id = if ($registered.Count) {{ Get-ActivityId $registered[0] }} else {{ '' }}
    register_package = [string]$registeredData.PackageFullName
    register_deployment_operation = if ($registered.Count) {{ [int]$registeredData.DeploymentOperation }} else {{ $null }}
    register_calling_process = [string]$registeredData.CallingProcess
    replacement_container_created = [bool]$created.Count
    replacement_container_event_id = if ($created.Count) {{ 210 }} else {{ $null }}
    replacement_container_event_record_id = if ($created.Count) {{ [long]$created[0].RecordId }} else {{ $null }}
    replacement_container_occurred_at = if ($created.Count) {{ $created[0].TimeCreated.ToUniversalTime().ToString('o') }} else {{ $null }}
    replacement_container_package = [string]$createdData.PackageName
    replacement_container_id = [string]$createdData.ContainerId
    replacement_process_launched = [bool]$launched.Count
    replacement_process_event_id = if ($launched.Count) {{ 201 }} else {{ $null }}
    replacement_process_event_record_id = if ($launched.Count) {{ [long]$launched[0].RecordId }} else {{ $null }}
    replacement_process_occurred_at = if ($launched.Count) {{ $launched[0].TimeCreated.ToUniversalTime().ToString('o') }} else {{ $null }}
    replacement_process_package = [string]$launchedData.PackageName
    replacement_application_name = [string]$launchedData.ApplicationName
    replacement_image_name = [string]$launchedData.ImageName
    old_package = $oldPackage
    new_package = $newPackage
    boundary_exclusions_verified = $true
    competing_system_boundary_events = @($systemBoundaries)
    competing_logoff_events = @($logoffBoundaries)
    last_boot_up_at = $lastBoot.ToString('o')
  }}
}}
@($result) | ConvertTo-Json -Depth 8 -Compress
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_windows_powershell_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown Windows event-log query error"
        raise RuntimeError(detail)
    payload = json.loads(completed.stdout or "[]")
    if isinstance(payload, Mapping):
        return (dict(payload),)
    if not isinstance(payload, list):
        raise RuntimeError("Windows event query returned an invalid payload")
    return tuple(dict(value) for value in payload if isinstance(value, Mapping))


def _windows_powershell_environment() -> dict[str, str]:
    environment = dict(os.environ)
    windows_root = Path(environment.get("WINDIR", r"C:\Windows"))
    module_paths = [
        windows_root / "System32" / "WindowsPowerShell" / "v1.0" / "Modules",
    ]
    user_profile = environment.get("USERPROFILE")
    if user_profile:
        module_paths.append(
            Path(user_profile) / "Documents" / "WindowsPowerShell" / "Modules"
        )
    environment["PSModulePath"] = os.pathsep.join(str(path) for path in module_paths)
    return environment


def _run_checked_in_all_runtime_launcher(
    root: Path, now: pd.Timestamp
) -> Mapping[str, object]:
    del now
    if os.name != "nt":
        raise RuntimeError("The all-runtime launcher is implemented for Windows")
    expected_root = _require_canonical_recovery_root(root)
    repository = Path(__file__).resolve().parents[1]
    script = (repository / "docs" / "datafetch-ml" / "start_all_loops.ps1").resolve()
    if not script.is_file():
        raise FileNotFoundError(f"The checked-in all-runtime launcher is missing: {script}")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-RequireAllMissing",
        ],
        cwd=str(repository),
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"The all-runtime launcher returned invalid JSON: {detail[:500]}") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("The all-runtime launcher returned a non-object payload")
    result = dict(payload)
    result["exit_code"] = int(completed.returncode)
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()[:1000]
    if Path(str(result.get("datastore", ""))).resolve() != expected_root:
        raise RuntimeError("The all-runtime launcher resolved an unexpected datastore")
    return result


def _require_canonical_recovery_root(
    root: Path,
) -> Path:
    configured = resolve_datastore_dir(target="pc").resolve()
    immutable = _PRODUCTION_DATASTORE_ROOT.resolve()
    if configured != immutable:
        raise RuntimeError(
            "The configured PC datastore does not resolve exactly to C:\\DATASTORE"
        )
    if Path(root).resolve() != immutable:
        raise RuntimeError(
            "Codex forced-update recovery is restricted to C:\\DATASTORE"
        )
    return immutable


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
    repository = Path(__file__).resolve().parents[1]
    python = (repository / ".venv" / "Scripts" / "python.exe").resolve()
    expected_literal = ",".join(
        f"'{_powershell_quote(value)}'" for value in (str(python), *launch.arguments)
    )
    script = rf"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class DucketzGuardianNativeCommandLine {{
  [DllImport("shell32.dll", SetLastError = true)]
  private static extern IntPtr CommandLineToArgvW(
    [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
    out int argumentCount
  );
  [DllImport("kernel32.dll")]
  private static extern IntPtr LocalFree(IntPtr memory);
  public static string[] Split(string commandLine) {{
    int count;
    IntPtr values = CommandLineToArgvW(commandLine, out count);
    if (values == IntPtr.Zero) {{
      throw new Win32Exception(Marshal.GetLastWin32Error());
    }}
    try {{
      string[] result = new string[count];
      for (int index = 0; index < count; index++) {{
        result[index] = Marshal.PtrToStringUni(
          Marshal.ReadIntPtr(values, index * IntPtr.Size)
        );
      }}
      return result;
    }} finally {{
      LocalFree(values);
    }}
  }}
}}
'@
$targetProcessId = {int(root_process_id)}
$rootProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $targetProcessId"
if ($null -eq $rootProcess) {{ throw 'Target process disappeared before stop' }}
$expected = @({expected_literal})
$actual = @([DucketzGuardianNativeCommandLine]::Split([string]$rootProcess.CommandLine))
if ($actual.Count -ne $expected.Count) {{
  throw 'Target command no longer matches the restart allowlist'
}}
if (-not [string]::Equals(
  [System.IO.Path]::GetFullPath([string]$actual[0]),
  [string]$expected[0],
  [System.StringComparison]::OrdinalIgnoreCase
)) {{ throw 'Target executable no longer matches the restart allowlist' }}
for ($index = 1; $index -lt $expected.Count; $index++) {{
  if (-not [string]::Equals(
    [string]$actual[$index],
    [string]$expected[$index],
    [System.StringComparison]::Ordinal
  )) {{ throw 'Target arguments no longer match the restart allowlist' }}
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
        env=_windows_powershell_environment(),
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
    parser.add_argument(
        "--repair-codex-forced-update",
        action="store_true",
        help=(
            "Allow one all-eight recovery only for an exact, recent, unconsumed "
            "Codex AppX forced-update signature."
        ),
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    schedule = scheduled_monitor_context() if args.mode == "scheduled" else None
    selected_mode = str(schedule["monitor_mode"]) if schedule is not None else args.mode
    try:
        root = resolve_datastore_dir(
            root_dir=args.datastore,
            target=None if args.datastore is not None else args.datastore_target,
        )
        report = run_guardian(
            root,
            mode=selected_mode,
            repair_liveness=args.repair_liveness,
            repair_codex_forced_update=args.repair_codex_forced_update,
        )
        if schedule is not None:
            report["requested_mode"] = "scheduled"
            report["schedule"] = schedule
            monitor = report.get("monitor")
            if isinstance(monitor, dict):
                monitor["schedule"] = schedule
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
    "GUARDIAN_LAUNCHES",
    "GUARDIAN_SCHEMA_VERSION",
    "GuardianLaunchSpec",
    "main",
    "plan_guarded_recovery",
    "run_guardian",
]
