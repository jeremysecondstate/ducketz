from __future__ import annotations

import inspect
import json
import os
import subprocess
from pathlib import Path
from typing import Mapping

import pandas as pd
import pytest

import ml.system_guardian as system_guardian
from ml.prediction_runtime import (
    DEFAULT_INTERVAL_MINUTES as LOOP_B_INTERVAL_MINUTES,
    DEFAULT_PHASE_OFFSET_MINUTES as LOOP_B_PHASE_OFFSET_MINUTES,
)
from ml.system_guardian import (
    GUARDIAN_LAUNCHES,
    _audit_directory,
    _execute_guarded_restart,
    _write_hang_observation,
    plan_guarded_recovery,
)
from ml.system_monitor import RUNTIMES


_LAUNCH_BY_RUNTIME = {spec.runtime: spec for spec in GUARDIAN_LAUNCHES}
_RUNTIME_BY_NAME = {spec.name: spec for spec in RUNTIMES}


def _production_process_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    next_pid = 1000
    for launch in GUARDIAN_LAUNCHES:
        command = (
            '"C:\\dev\\ducketz\\.venv\\Scripts\\python.exe" '
            + " ".join(launch.arguments)
        )
        rows.extend(
            (
                {
                    "pid": next_pid,
                    "ppid": 42,
                    "created_at": "2026-08-19T00:00:00Z",
                    "command_line": command,
                },
                {
                    "pid": next_pid + 1,
                    "ppid": next_pid,
                    "created_at": "2026-08-19T00:00:01Z",
                    "command_line": command,
                },
            )
        )
        next_pid += 10
    return rows


def _monitor(
    *, status: str = "HEALTHY", checks: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "status": status,
        "checked_at": "2026-08-19T18:42:00Z",
        "checks": checks or [],
    }


def _plan(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    monitor: Mapping[str, object] | None = None,
    now: str = "2026-08-19T18:42:00Z",
    live_pids: set[int] | None = None,
    allow_codex_forced_update: bool = False,
    codex_update_events: tuple[Mapping[str, object], ...] = (),
) -> dict[str, object]:
    alive = live_pids if live_pids is not None else {
        int(row["pid"]) for row in rows
    }
    return plan_guarded_recovery(
        tmp_path,
        monitor_report=monitor or _monitor(),
        process_rows=rows,
        observed_at=now,
        pid_exists=lambda process_id: process_id in alive,
        audit_directory=_audit_directory(tmp_path),
        allow_codex_forced_update=allow_codex_forced_update,
        codex_update_events=codex_update_events,
    )


def _forced_update_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": 603,
        "event_record_id": 481728,
        "occurred_at": "2026-08-19T18:41:00Z",
        "package_family": "OpenAI.Codex_2p2nqsd0c76g0",
        "deployment_operation": 20,
        "flags": 18496,
        "flags_high": 262144,
        "calling_process": "svchost.exe,wuauserv",
        "deployment_activity_id": "{d2c996d9-394a-000f-9eee-3fd34a39dd01}",
        "update_event_id": 855,
        "update_event_record_id": 481732,
        "update_occurred_at": "2026-08-19T18:41:00.020000Z",
        "update_activity_id": "{d2c996d9-394a-000f-9eee-3fd34a39dd01}",
        "update_old_package": "OpenAI.Codex_26.831.1445.0_x64__2p2nqsd0c76g0",
        "update_new_package": "OpenAI.Codex_26.901.1978.0_x64__2p2nqsd0c76g0",
        "old_container_destroyed": True,
        "destroyed_event_id": 217,
        "destroyed_event_record_id": 387443,
        "destroyed_occurred_at": "2026-08-19T18:41:00.600000Z",
        "destroyed_package": "OpenAI.Codex_26.831.1445.0_x64__2p2nqsd0c76g0",
        "registration_succeeded": True,
        "register_event_id": 400,
        "register_event_record_id": 481745,
        "register_occurred_at": "2026-08-19T18:41:00.900000Z",
        "register_activity_id": "{d2c996d9-394a-000f-9eee-3fd34a39dd01}",
        "register_package": "OpenAI.Codex_26.901.1978.0_x64__2p2nqsd0c76g0",
        "register_deployment_operation": 6,
        "register_calling_process": "svchost.exe,wuauserv",
        "replacement_container_created": True,
        "replacement_container_event_id": 210,
        "replacement_container_event_record_id": 387446,
        "replacement_container_occurred_at": "2026-08-19T18:41:01Z",
        "replacement_container_package": "OpenAI.Codex_26.901.1978.0_x64__2p2nqsd0c76g0",
        "replacement_container_id": "{8EA303BC-A719-11F1-92AE-4C82A910044C}",
        "replacement_process_launched": True,
        "replacement_process_event_id": 201,
        "replacement_process_event_record_id": 387448,
        "replacement_process_occurred_at": "2026-08-19T18:41:01.100000Z",
        "replacement_process_package": "OpenAI.Codex_26.901.1978.0_x64__2p2nqsd0c76g0",
        "replacement_application_name": "OpenAI.Codex_2p2nqsd0c76g0!App",
        "replacement_image_name": "ChatGPT.exe",
        "old_package": "OpenAI.Codex_26.831.1445.0_x64__2p2nqsd0c76g0",
        "new_package": "OpenAI.Codex_26.901.1978.0_x64__2p2nqsd0c76g0",
        "boundary_exclusions_verified": True,
        "competing_system_boundary_events": [],
        "competing_logoff_events": [],
        "last_boot_up_at": "2026-08-01T00:00:00Z",
    }
    event.update(overrides)
    return event


def _forced_update_monitor(tmp_path: Path) -> dict[str, object]:
    runtime_views: dict[str, object] = {}
    log_root = tmp_path / "logs" / "ducketz" / "background-launch" / "prior"
    log_root.mkdir(parents=True)
    modified = pd.Timestamp("2026-08-19T18:40:00Z").timestamp()
    for runtime in RUNTIMES:
        stdout = log_root / f"{runtime.name}.stdout.log"
        stderr = log_root / f"{runtime.name}.stderr.log"
        stdout.write_text(f"{runtime.name} active work\n", encoding="utf-8")
        stderr.write_bytes(b"")
        os.utime(stdout, (modified, modified))
        os.utime(stderr, (modified, modified))
        runtime_views[runtime.name] = {
            "stdout": str(stdout),
            "stderr": str(stderr),
            "recent_stderr": False,
        }
    return {
        "status": "UNHEALTHY",
        "checked_at": "2026-08-19T18:42:00Z",
        "read_only": True,
        "orders_placed": 0,
        "automated_action_allowed": False,
        "checks": [
            {
                "name": "runtime_logs",
                "status": "WARN",
                "summary": "Runtime logs stopped at a shared boundary.",
                "details": {"runtimes": runtime_views},
            },
            {
                "name": "loop_a_cycle",
                "status": "FAIL",
                "summary": "Loop A's latest complete-cycle authority is unhealthy.",
                "details": {
                    "generation": "loop-a-writing",
                    "active_cycle_status": "WRITING",
                    "active_cycle_age_minutes": 47.0,
                    "last_complete_generation": "loop-a-complete",
                    "finished_at": "2026-08-19T17:50:00Z",
                    "age_minutes": 52.0,
                    "failure_count": 0,
                    "failures": [
                        "active-cycle-running-too-long",
                        "latest-complete-cycle-stale",
                    ],
                },
            },
            {
                "name": "loop_b_publication",
                "status": "FAIL",
                "summary": "Loop B's current prediction publication is incomplete or stale.",
                "details": {
                    "run_path": "loop-b/runs/prior",
                    "run_timestamp": "2026-08-19T17:35:00Z",
                    "authoritative_timestamp": "2026-08-19T17:40:00Z",
                    "age_minutes": 62.0,
                    "intelligence_rows": 63,
                    "expected_routes": 63,
                    "failures": ["publication-stale"],
                    "warnings": [],
                },
            },
            {
                "name": "strategy_publication",
                "status": "FAIL",
                "summary": "Strategy's candidate publication is empty or stale.",
                "details": {
                    "run_path": "strategy/runs/prior",
                    "run_timestamp": "2026-08-19T17:40:00Z",
                    "age_minutes": 62.0,
                    "candidate_rows": 21,
                    "audit_rows": 21,
                },
            },
            {
                "name": "pricing_publications",
                "status": "PASS",
                "summary": "Pricing authorities are verified.",
                "details": {
                    "expected_target": "2026-08-19T18:30:00Z",
                    "target_authority": {
                        "target_snapshot_for": "2026-08-19T18:30:00Z",
                        "published_at": "2026-08-19T18:35:00Z",
                        "terminal_status": "PREDICTIONS_PUBLISHED",
                        "prediction_rows": 21,
                        "shadow_rows": 21,
                    },
                    "full_generation": {
                        "status": "VERIFIED",
                        "run_path": "pricing/runs/prior",
                        "published_at": "2026-08-19T18:35:00Z",
                        "age_minutes": 7.0,
                    },
                },
            },
            {
                "name": "sequence_encoder_loop_c",
                "status": "INFO",
                "summary": "The pooled sequence encoder and Loop C observe lane are not yet published.",
                "details": {
                    "sequence_status": "NOT_PUBLISHED",
                    "loop_c_status": "NOT_PUBLISHED",
                    "authority": "NONE",
                    "automated_action_allowed": False,
                    "orders_enabled": False,
                    "orders_placed": 0,
                },
            },
            *[
                {
                    "name": name,
                    "status": "PASS",
                    "summary": f"{name} is verified.",
                    "details": {},
                }
                for name in (
                    "loop_a_bar_readiness",
                    "cme_publication",
                    "alfred_publication",
                    "options_publications",
                    "strategy_profit_model_authority",
                    "cross_loop_lineage",
                    "ui_contracts",
                )
            ],
            {
                "name": "storage_capacity",
                "status": "PASS",
                "summary": "Storage is healthy.",
                "details": {},
            },
        ],
    }


def _mark_pricing_and_options_stale_from_update_outage(
    monitor: dict[str, object],
) -> None:
    pricing = next(
        check
        for check in monitor["checks"]
        if check.get("name") == "pricing_publications"
    )
    pricing.update(
        {
            "status": "FAIL",
            "summary": "Pricing target authority is behind the actionable regular target.",
        }
    )
    pricing["details"].update(
        {
            "expected_target": "2026-08-19T19:30:00Z",
            "settling": False,
        }
    )
    options = next(
        check
        for check in monitor["checks"]
        if check.get("name") == "options_publications"
    )
    options.update(
        {
            "status": "FAIL",
            "summary": "Current option snapshots are behind the actionable regular target.",
            "details": {
                "expected_target": "2026-08-19T19:30:00Z",
                "observed_targets": ["2026-08-19T18:30:00Z"],
                "provider_counts": {"databento-opra": 6},
                "symbol_count": 6,
                "settling": False,
            },
        }
    )


def _write_forced_update_locks(tmp_path: Path) -> None:
    for runtime in RUNTIMES:
        (tmp_path / runtime.lock_name).write_text(
            f"pid={90000 + len(runtime.name)}\nstarted_at=2026-08-19T17:00:00Z\n",
            encoding="utf-8",
        )


def test_launch_allowlist_covers_each_runtime_once() -> None:
    assert [launch.runtime for launch in GUARDIAN_LAUNCHES] == [
        runtime.name for runtime in RUNTIMES
    ]
    assert all(
        launch.module == _RUNTIME_BY_NAME[launch.runtime].module
        for launch in GUARDIAN_LAUNCHES
    )
    assert all(launch.arguments[0] == "-u" for launch in GUARDIAN_LAUNCHES)
    assert all(
        all(required in launch.command_signature for required in runtime.required_arguments)
        for launch in GUARDIAN_LAUNCHES
        for runtime in (_RUNTIME_BY_NAME[launch.runtime],)
    )
    assert "--skip-historical-catchup" in _LAUNCH_BY_RUNTIME["options"].arguments


def test_loop_b_canonical_cadence_matches_runtime_monitor_guardian_and_command(
) -> None:
    runtime = _RUNTIME_BY_NAME["loop_b"]
    launch = _LAUNCH_BY_RUNTIME["loop_b"]
    repository = Path(__file__).resolve().parents[1]
    start_command = (
        repository / "docs" / "datafetch-ml" / "current_start_command"
    ).read_text(encoding="utf-8")

    assert LOOP_B_INTERVAL_MINUTES == 30
    assert LOOP_B_PHASE_OFFSET_MINUTES == 6
    assert (
        f"--interval-minutes {LOOP_B_INTERVAL_MINUTES}"
        in runtime.required_arguments
    )
    assert (
        f"--phase-offset-minutes {LOOP_B_PHASE_OFFSET_MINUTES}"
        in runtime.required_arguments
    )
    interval_index = launch.arguments.index("--interval-minutes")
    phase_index = launch.arguments.index("--phase-offset-minutes")
    assert launch.arguments[interval_index + 1] == str(LOOP_B_INTERVAL_MINUTES)
    assert launch.arguments[phase_index + 1] == str(LOOP_B_PHASE_OFFSET_MINUTES)
    assert f"--interval-minutes {LOOP_B_INTERVAL_MINUTES} `" in start_command
    assert (
        f"--phase-offset-minutes {LOOP_B_PHASE_OFFSET_MINUTES} `"
        in start_command
    )


def test_all_runtime_lock_paths_share_the_maintenance_gate() -> None:
    repository = Path(__file__).resolve().parents[1]
    shared_lock_owners = (
        "datafetching/cme_runtime.py",
        "datafetching/fred_alfred_runtime.py",
        "datafetching/orchestrate.py",
        "ml/option_pricing_runtime.py",
        "ml/prediction_runtime.py",
        "datafetching/options_runtime.py",
        "ml/strategy_runtime.py",
        "ml/strategy_profit_training_runtime.py",
    )
    for relative in shared_lock_owners:
        assert "exclusive_runtime_lock" in (repository / relative).read_text(
            encoding="utf-8"
        )


def test_forced_update_command_verification_rejects_trailing_override() -> None:
    launch = _LAUNCH_BY_RUNTIME["strategy"]
    canonical = {
        "command_line": '"C:\\dev\\ducketz\\.venv\\Scripts\\python.exe" '
        + " ".join(launch.arguments)
    }
    overridden = {
        "command_line": str(canonical["command_line"]) + " --once"
    }
    foreign_python = {
        "command_line": '"C:\\Python\\python.exe" ' + " ".join(launch.arguments)
    }

    assert system_guardian._is_exact_allowlisted_command(canonical, launch)
    assert not system_guardian._is_exact_allowlisted_command(overridden, launch)
    assert not system_guardian._is_exact_allowlisted_command(foreign_python, launch)
    assert not system_guardian._is_allowlisted_command(overridden, launch)


def test_stop_side_revalidates_exact_argv_before_process_termination() -> None:
    source = inspect.getsource(system_guardian._stop_windows_process_tree)

    assert "CommandLineToArgvW" in source
    assert "Target executable no longer matches" in source
    assert "Target arguments no longer match" in source
    assert "only rethrow if it still lives" in source
    assert ".Contains(" not in source


def test_scheduled_main_surfaces_overnight_stage_in_guardian_and_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schedule = {
        "schema_version": "loops-overnight-accuracy-schedule-v2",
        "monitor_mode": "hourly",
        "lane": "OVERNIGHT_ACCURACY",
        "overnight_stage": {
            "index": 1,
            "count": 17,
            "id": "seal-core-options-session",
        },
    }
    monkeypatch.setattr(
        system_guardian,
        "scheduled_monitor_context",
        lambda: schedule,
    )
    monkeypatch.setattr(
        system_guardian,
        "resolve_datastore_dir",
        lambda **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        system_guardian,
        "run_guardian",
        lambda *_args, **_kwargs: {
            "schema_version": "loops-system-guardian-v1",
            "mode": "hourly",
            "status": "HEALTHY",
            "monitor": {"status": "HEALTHY"},
            "orders_placed": 0,
        },
    )

    exit_code = system_guardian.main(
        ["--datastore", str(tmp_path), "--mode", "scheduled", "--compact"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["requested_mode"] == "scheduled"
    assert payload["schedule"] == schedule
    assert payload["monitor"]["schedule"] == schedule


def test_main_forwards_explicit_codex_forced_update_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}

    def fake_guardian(*_args: object, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "schema_version": "loops-system-guardian-v1",
            "mode": "hourly",
            "status": "HEALTHY",
            "checked_at": "2026-08-19T18:42:00Z",
            "monitor": {"status": "HEALTHY"},
            "orders_placed": 0,
        }

    monkeypatch.setattr(system_guardian, "resolve_datastore_dir", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(system_guardian, "run_guardian", fake_guardian)

    exit_code = system_guardian.main(
        [
            "--datastore",
            str(tmp_path),
            "--mode",
            "hourly",
            "--repair-codex-forced-update",
            "--compact",
        ]
    )
    json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert observed["repair_codex_forced_update"] is True


def test_checked_in_launcher_is_hidden_idempotent_and_allowlist_owned() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "datafetch-ml"
        / "start_all_loops.ps1"
    )
    source = path.read_text(encoding="utf-8")

    assert "GUARDIAN_LAUNCHES" in source
    assert "Get-CimInstance Win32_Process" in source
    assert "ValidPairAndLock" in source
    assert "-WindowStyle Hidden" in source
    assert "-WorkingDirectory $repoRoot" in source
    assert "-RedirectStandardOutput $stdout" in source
    assert "-RedirectStandardError $stderr" in source
    assert "logs\\ducketz\\background-launch" in source
    assert "[switch]$RequireAllMissing" in source
    assert "MISSING_VERIFIED" in source
    assert "BLOCKED_NOT_MISSING" in source
    assert "CommandLineToArgvW" in source
    assert "$executable," in source
    assert "$python," in source
    assert "Global\\DucketzAllLoopsCanonicalLauncherV1" in source
    assert "$launcherMutex.WaitOne(0)" in source
    assert "function Get-LockOwnerState" in source
    assert "$before.LockOwnerState -ne 'DEAD'" in source
    assert "dead_lock_observed_before_start" in source
    assert "[int]$after.LauncherPid -ne [int]$started.Id" in source
    assert "-NoExit" not in source


def test_guardian_start_detaches_without_captured_startup_pipes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Guarded startup is Windows-only")
    observed: dict[str, object] = {}

    class _Process:
        pid = 43210

    def fake_popen(arguments: list[str], **kwargs: object) -> _Process:
        observed["arguments"] = arguments
        observed["cwd"] = kwargs["cwd"]
        observed["stdin"] = kwargs["stdin"]
        observed["stdout"] = getattr(kwargs["stdout"], "name", None)
        observed["stderr"] = getattr(kwargs["stderr"], "name", None)
        observed["close_fds"] = kwargs["close_fds"]
        observed["creationflags"] = kwargs["creationflags"]
        return _Process()

    monkeypatch.setattr(system_guardian.subprocess, "Popen", fake_popen)
    log_directory = (
        tmp_path / "logs" / "ducketz" / "system-guardian" / "20260820"
    )

    pid = system_guardian._start_windows_runtime(
        tmp_path,
        log_directory,
        _LAUNCH_BY_RUNTIME["cme"],
        pd.Timestamp("2026-08-20T01:43:16Z"),
    )

    assert pid == 43210
    assert observed["arguments"][1:] == list(_LAUNCH_BY_RUNTIME["cme"].arguments)
    assert observed["stdin"] == subprocess.DEVNULL
    assert observed["stdout"] == str(log_directory / "014316-cme-l2.stdout.log")
    assert observed["stderr"] == str(log_directory / "014316-cme-l2.stderr.log")
    assert observed["close_fds"] is True
    assert int(observed["creationflags"]) & subprocess.CREATE_NO_WINDOW
    assert int(observed["creationflags"]) & subprocess.DETACHED_PROCESS


def test_healthy_owner_pairs_need_no_action(tmp_path: Path) -> None:
    decision = _plan(tmp_path, _production_process_rows())

    assert decision["status"] == "NO_ACTION"


def test_one_missing_runtime_with_proven_dead_lock_is_eligible(tmp_path: Path) -> None:
    rows = [
        row
        for row in _production_process_rows()
        if "-m ml.strategy_runtime " not in str(row["command_line"])
    ]
    (tmp_path / ".ducketz-strategy-runtime.lock").write_text(
        "pid=90909\n", encoding="utf-8"
    )

    decision = _plan(tmp_path, rows, live_pids=set())

    assert decision["status"] == "ELIGIBLE"
    assert decision["runtime"] == "strategy"
    assert decision["fault"] == "PROCESS_MISSING"
    assert decision["lock"]["status"] == "STALE_DEAD"


def test_one_allowlisted_orphan_is_eligible_for_targeted_replacement(
    tmp_path: Path,
) -> None:
    rows = _production_process_rows()
    strategy_rows = [
        row for row in rows if "-m ml.strategy_runtime " in str(row["command_line"])
    ]
    orphan = strategy_rows[1]
    rows.remove(strategy_rows[0])
    (tmp_path / ".ducketz-strategy-runtime.lock").write_text(
        f"pid={orphan['pid']}\n", encoding="utf-8"
    )

    decision = _plan(tmp_path, rows)

    assert decision["status"] == "ELIGIBLE"
    assert decision["fault"] == "PARTIAL_OWNER_PAIR"
    assert decision["pids"] == [orphan["pid"]]


def test_multiple_missing_runtimes_are_report_only(tmp_path: Path) -> None:
    rows = [
        row
        for row in _production_process_rows()
        if not any(
            module in str(row["command_line"])
            for module in ("-m ml.strategy_runtime ", "-m ml.prediction_runtime ")
        )
    ]

    decision = _plan(tmp_path, rows)

    assert decision["status"] == "REPORT_ONLY"
    assert decision["affected_runtimes"] == ["loop_b", "strategy"]


def test_exact_codex_forced_update_all_eight_signature_is_eligible(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    event = _forced_update_event()

    decision = _plan(
        tmp_path,
        [],
        monitor=_forced_update_monitor(tmp_path),
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(event,),
    )

    assert decision["status"] == "ELIGIBLE_CODEX_APPX_FORCED_UPDATE_ALL_EIGHT"
    assert decision["fault"] == "CODEX_APPX_FORCED_UPDATE_ALL_EIGHT"
    assert decision["affected_runtimes"] == [runtime.name for runtime in RUNTIMES]
    assert len(decision["locks"]) == 8
    assert len(decision["logs"]) == 8
    assert len(str(decision["event_fingerprint"])) == 64


def test_codex_forced_update_allows_verified_target_gaps_caused_by_outage(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    _mark_pricing_and_options_stale_from_update_outage(monitor)

    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        now="2026-08-19T19:42:00Z",
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )

    assert decision["status"] == "ELIGIBLE_CODEX_APPX_FORCED_UPDATE_ALL_EIGHT"
    assert decision["fault"] == "CODEX_APPX_FORCED_UPDATE_ALL_EIGHT"


def test_codex_forced_update_target_gap_requires_exact_event_signature(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    _mark_pricing_and_options_stale_from_update_outage(monitor)

    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        now="2026-08-19T19:42:00Z",
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(
            _forced_update_event(package_family="Other.App_family"),
        ),
    )

    assert decision["status"] == "REPORT_ONLY"
    assert "forced-update event" in str(decision["summary"])


@pytest.mark.parametrize(
    "unsafe_mutation",
    (
        "pricing-integrity",
        "pricing-zero-rows",
        "options-coverage",
        "orders-placed",
        "automated-action-enabled",
        "not-read-only",
    ),
)
def test_codex_forced_update_target_gap_never_waives_integrity_or_order_safety(
    tmp_path: Path,
    unsafe_mutation: str,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    _mark_pricing_and_options_stale_from_update_outage(monitor)
    if unsafe_mutation == "pricing-integrity":
        pricing = next(
            check
            for check in monitor["checks"]
            if check.get("name") == "pricing_publications"
        )
        pricing["details"]["full_generation"] = {
            "status": "MISSING_OR_INVALID",
            "reason": "receipt checksum disagrees",
        }
    elif unsafe_mutation == "pricing-zero-rows":
        pricing = next(
            check
            for check in monitor["checks"]
            if check.get("name") == "pricing_publications"
        )
        pricing["details"]["target_authority"]["prediction_rows"] = 0
    elif unsafe_mutation == "options-coverage":
        options = next(
            check
            for check in monitor["checks"]
            if check.get("name") == "options_publications"
        )
        options["details"]["missing_symbols"] = ["NVDA"]
    elif unsafe_mutation == "orders-placed":
        monitor["orders_placed"] = 1
    elif unsafe_mutation == "automated-action-enabled":
        monitor["automated_action_allowed"] = True
    else:
        monitor["read_only"] = False

    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        now="2026-08-19T19:42:00Z",
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )

    assert decision["status"] == "REPORT_ONLY"
    expected = {
        "pricing-integrity": "pricing_publications",
        "pricing-zero-rows": "pricing_publications",
        "options-coverage": "options_publications",
        "orders-placed": "orders_placed",
        "automated-action-enabled": "automated_action_allowed",
        "not-read-only": "read_only",
    }[unsafe_mutation]
    assert expected in decision["blockers"]


def test_codex_forced_update_does_not_excuse_target_lag_that_predates_event(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    _mark_pricing_and_options_stale_from_update_outage(monitor)
    pricing = next(
        check
        for check in monitor["checks"]
        if check.get("name") == "pricing_publications"
    )
    pricing["details"]["expected_target"] = "2026-08-19T18:40:00Z"

    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        now="2026-08-19T19:42:00Z",
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )

    assert decision["status"] == "REPORT_ONLY"
    assert "pricing_publications" in decision["blockers"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_id", 604),
        ("package_family", "Other.App_family"),
        ("deployment_operation", 6),
        ("flags_high", 0),
        ("update_event_id", 0),
        ("register_activity_id", "{different-activity}"),
        ("destroyed_package", "OpenAI.Codex_1.2.3.4_x64__2p2nqsd0c76g0"),
        ("replacement_application_name", "OpenAI.Codex_2p2nqsd0c76g0!Other"),
        ("destroyed_event_id", 0),
        ("register_event_id", 0),
        ("replacement_container_event_id", 0),
        ("replacement_process_event_id", 0),
    ),
)
def test_codex_forced_update_requires_every_exact_event_field(
    tmp_path: Path, field: str, value: object
) -> None:
    _write_forced_update_locks(tmp_path)

    decision = _plan(
        tmp_path,
        [],
        monitor=_forced_update_monitor(tmp_path),
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(**{field: value}),),
    )

    assert decision["status"] == "REPORT_ONLY"
    assert "forced-update event" in str(decision["summary"])


def test_codex_forced_update_rejects_shutdown_or_logoff_boundary(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    for field, boundary in (
        (
            "competing_system_boundary_events",
            [{"event_id": 1074, "occurred_at": "2026-08-19T18:20:00Z"}],
        ),
        (
            "competing_logoff_events",
            [{"event_id": 4647, "occurred_at": "2026-08-19T18:20:00Z"}],
        ),
    ):
        decision = _plan(
            tmp_path,
            [],
            monitor=monitor,
            live_pids=set(),
            allow_codex_forced_update=True,
            codex_update_events=(_forced_update_event(**{field: boundary}),),
        )
        assert decision["status"] == "REPORT_ONLY"
        assert "forced-update event" in str(decision["summary"])


def test_codex_forced_update_evaluates_all_events_before_attribution(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    correct = _forced_update_event()
    decoy = _forced_update_event(
        event_record_id=481828,
        update_event_record_id=481832,
        register_event_record_id=481845,
        occurred_at="2026-08-19T18:41:30Z",
        update_occurred_at="2026-08-19T18:41:30.020000Z",
        destroyed_occurred_at="2026-08-19T18:41:30.600000Z",
        register_occurred_at="2026-08-19T18:41:30.900000Z",
        replacement_container_occurred_at="2026-08-19T18:41:31Z",
        replacement_process_occurred_at="2026-08-19T18:41:31.100000Z",
        last_boot_up_at="2026-08-19T17:30:00Z",
    )

    decision = _plan(
        tmp_path,
        [],
        monitor=_forced_update_monitor(tmp_path),
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(decoy, correct),
    )

    assert decision["status"] == "ELIGIBLE_CODEX_APPX_FORCED_UPDATE_ALL_EIGHT"
    assert decision["event"]["event_record_id"] == correct["event_record_id"]
    assert decision["event_fingerprint"] == system_guardian._codex_update_fingerprint(
        decision["event"]
    )


def test_consumed_older_update_does_not_block_distinct_newer_update(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    newer = _forced_update_event()
    older = _forced_update_event(
        event_record_id=481628,
        update_event_record_id=481632,
        destroyed_event_record_id=387343,
        register_event_record_id=481645,
        replacement_container_event_record_id=387346,
        replacement_process_event_record_id=387348,
        occurred_at="2026-08-19T18:40:30Z",
        update_occurred_at="2026-08-19T18:40:30.020000Z",
        destroyed_occurred_at="2026-08-19T18:40:30.600000Z",
        register_occurred_at="2026-08-19T18:40:30.900000Z",
        replacement_container_occurred_at="2026-08-19T18:40:31Z",
        replacement_process_occurred_at="2026-08-19T18:40:31.100000Z",
        deployment_activity_id="{d2c996d9-394a-000f-9eee-3fd34a39dd00}",
        update_activity_id="{d2c996d9-394a-000f-9eee-3fd34a39dd00}",
        register_activity_id="{d2c996d9-394a-000f-9eee-3fd34a39dd00}",
    )
    audit = _audit_directory(tmp_path)
    validated_older = system_guardian._validated_codex_forced_update_events(
        (older,), now=pd.Timestamp("2026-08-19T18:42:00Z")
    )[0]
    older_fingerprint = system_guardian._codex_update_fingerprint(validated_older)
    older_attempt = system_guardian._codex_update_attempt_path(
        audit, older_fingerprint
    )
    system_guardian._atomic_write_unique_json(
        older_attempt,
        {"event_fingerprint": older_fingerprint, "status": "TERMINAL_FAILURE"},
    )

    decision = _plan(
        tmp_path,
        [],
        monitor=_forced_update_monitor(tmp_path),
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(older, newer),
    )

    assert decision["status"] == "ELIGIBLE_CODEX_APPX_FORCED_UPDATE_ALL_EIGHT"
    assert decision["event"]["event_record_id"] == newer["event_record_id"]
    assert decision["event_fingerprint"] != older_fingerprint


def test_codex_forced_update_rejects_unverified_retained_authority(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    cme = next(
        check for check in monitor["checks"] if check.get("name") == "cme_publication"
    )
    cme.update(
        {
            "status": "FAIL",
            "summary": "No CME snapshot exists.",
            "details": {},
        }
    )

    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )

    assert decision["status"] == "REPORT_ONLY"
    assert "cme_publication" in decision["blockers"]


def test_codex_forced_update_rejects_invalid_full_pricing_generation(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    pricing = next(
        check
        for check in monitor["checks"]
        if check.get("name") == "pricing_publications"
    )
    pricing["details"]["full_generation"] = {
        "status": "MISSING_OR_INVALID",
        "reason": "not published",
    }

    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )

    assert decision["status"] == "REPORT_ONLY"
    assert "pricing_publications" in decision["blockers"]


def test_codex_forced_update_rejects_loop_c_zero_order_warning(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    loop_c = next(
        check
        for check in monitor["checks"]
        if check.get("name") == "sequence_encoder_loop_c"
    )
    loop_c.update(
        {
            "status": "WARN",
            "summary": "A published sequence-encoder or Loop C observe authority is invalid.",
            "details": {
                "sequence_status": "VERIFIED_SHADOW",
                "loop_c_status": "INVALID",
                "warnings": ["Loop C observe output violates zero-order safety"],
                "automated_action_allowed": False,
                "orders_enabled": False,
                "orders_placed": 0,
            },
        }
    )

    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )

    assert decision["status"] == "REPORT_ONLY"
    assert "sequence_encoder_loop_c" in decision["blockers"]


def test_codex_forced_update_rejects_live_changed_lock_and_nonempty_stderr(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    strategy_lock = tmp_path / _RUNTIME_BY_NAME["strategy"].lock_name
    strategy_lock.write_text(
        "pid=77777\nstarted_at=2026-08-19T17:00:00Z\n", encoding="utf-8"
    )
    monitor = _forced_update_monitor(tmp_path)

    live = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids={77777},
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )
    assert live["status"] == "REPORT_ONLY"
    assert "lock-pid-still-alive" in str(live["signature_errors"])

    strategy_lock.write_text(
        "pid=77777\nstarted_at=2026-08-19T19:00:00Z\n", encoding="utf-8"
    )
    future = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )
    assert future["status"] == "REPORT_ONLY"
    assert "lock-does-not-predate-event" in str(future["signature_errors"])

    strategy_lock.write_text(
        "pid=77777\nstarted_at=2026-08-19T17:00:00Z\n", encoding="utf-8"
    )
    stderr = Path(
        monitor["checks"][0]["details"]["runtimes"]["strategy"]["stderr"]
    )
    stderr.write_text("credential failure\n", encoding="utf-8")
    dirty = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )
    assert dirty["status"] == "REPORT_ONLY"
    assert "stderr-not-empty" in str(dirty["signature_errors"])


def test_codex_forced_update_executes_one_launcher_and_quarantines_exact_locks(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    decision = _plan(
        tmp_path,
        [],
        monitor=_forced_update_monitor(tmp_path),
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(_forced_update_event(),),
    )
    rows: list[dict[str, object]] = []
    alive: set[int] = set()
    calls: list[Path] = []

    def launcher(root: Path, _now: pd.Timestamp) -> Mapping[str, object]:
        calls.append(root)
        log_root = root / "logs" / "ducketz" / "background-launch"
        launch_dir = log_root / "new"
        launch_dir.mkdir(parents=True)
        owners: list[dict[str, object]] = []
        for index, launch in enumerate(GUARDIAN_LAUNCHES):
            parent = 20000 + index * 10
            worker = parent + 1
            command = (
                '"C:\\dev\\ducketz\\.venv\\Scripts\\python.exe" '
                + " ".join(launch.arguments)
            )
            rows.extend(
                (
                    {
                        "pid": parent,
                        "ppid": 42,
                        "created_at": "2026-08-19T18:42:01Z",
                        "command_line": command,
                    },
                    {
                        "pid": worker,
                        "ppid": parent,
                        "created_at": "2026-08-19T18:42:02Z",
                        "command_line": command,
                    },
                )
            )
            alive.update({parent, worker})
            runtime = _RUNTIME_BY_NAME[launch.runtime]
            (root / runtime.lock_name).write_text(
                f"pid={worker}\nstarted_at=2026-08-19T18:42:02Z\n",
                encoding="utf-8",
            )
            stderr = launch_dir / f"{launch.log_stem}.stderr.log"
            stdout = launch_dir / f"{launch.log_stem}.stdout.log"
            stderr.write_bytes(b"")
            stdout.write_text("started\n", encoding="utf-8")
            owners.append(
                {
                    "runtime": launch.runtime,
                    "status": "STARTED_VERIFIED",
                    "stderr": str(stderr),
                    "stdout": str(stdout),
                }
            )
        return {
            "schema_version": "ducketz-background-launch-v1",
            "exit_code": 0,
            "audit_only": False,
            "require_all_missing": True,
            "datastore": str(root),
            "canonical_log_root": str(log_root),
            "issues": [],
            "owners": owners,
        }

    result, final_rows = system_guardian._execute_codex_forced_update_recovery(
        tmp_path,
        decision=decision,
        observed_at=pd.Timestamp("2026-08-19T18:42:00Z"),
        process_reader=lambda: list(rows),
        pid_exists=lambda process_id: process_id in alive,
        launch_all_runtimes=launcher,
        audit_directory=_audit_directory(tmp_path),
        expected_recovery_root_for_test=tmp_path,
    )

    assert result["status"] == "CODEX_FORCED_UPDATE_RECOVERED"
    assert result["launcher_invocations"] == 1
    assert calls == [tmp_path]
    assert len(result["quarantined_locks"]) == 8
    assert all("logs\\ducketz\\manual-recovery" in path for path in result["quarantined_locks"])
    assert len(list(final_rows or [])) == 16
    assert Path(result["event_attempt_receipt"]).is_file()


def test_codex_forced_update_attempt_is_consumed_even_when_launcher_fails(
    tmp_path: Path,
) -> None:
    _write_forced_update_locks(tmp_path)
    monitor = _forced_update_monitor(tmp_path)
    event = _forced_update_event()
    decision = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(event,),
    )
    calls = 0

    def failed_launcher(_root: Path, _now: pd.Timestamp) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {"exit_code": 2, "owners": [], "issues": ["failed"]}

    result, _rows = system_guardian._execute_codex_forced_update_recovery(
        tmp_path,
        decision=decision,
        observed_at=pd.Timestamp("2026-08-19T18:42:00Z"),
        process_reader=lambda: [],
        pid_exists=lambda _process_id: False,
        launch_all_runtimes=failed_launcher,
        audit_directory=_audit_directory(tmp_path),
        expected_recovery_root_for_test=tmp_path,
    )
    assert result["status"] == "CODEX_FORCED_UPDATE_RECOVERY_FAILED"
    assert result["launcher_invocations"] == 1
    assert calls == 1
    assert Path(result["event_attempt_receipt"]).is_file()

    repeated = _plan(
        tmp_path,
        [],
        monitor=monitor,
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(event,),
    )
    assert repeated["status"] == "REPORT_ONLY"
    assert repeated["prior_attempt"] == result["event_attempt_receipt"]


def test_codex_forced_update_gate_contention_does_not_consume_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_forced_update_locks(tmp_path)
    event = _forced_update_event()
    decision = _plan(
        tmp_path,
        [],
        monitor=_forced_update_monitor(tmp_path),
        live_pids=set(),
        allow_codex_forced_update=True,
        codex_update_events=(event,),
    )

    class BusyGate:
        def __enter__(self) -> None:
            raise system_guardian.Timeout("maintenance busy")

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        system_guardian,
        "runtime_lock_maintenance_gate",
        lambda *_args, **_kwargs: BusyGate(),
    )

    result, _rows = system_guardian._execute_codex_forced_update_recovery(
        tmp_path,
        decision=decision,
        observed_at=pd.Timestamp("2026-08-19T18:42:00Z"),
        process_reader=lambda: [],
        pid_exists=lambda _process_id: False,
        launch_all_runtimes=lambda *_args: pytest.fail("launcher must not run"),
        audit_directory=_audit_directory(tmp_path),
        expected_recovery_root_for_test=tmp_path,
    )

    assert result["verification"] == "LOCK_MAINTENANCE_BUSY"
    assert "event_attempt_receipt" not in result
    assert all((tmp_path / runtime.lock_name).is_file() for runtime in RUNTIMES)


def test_duplicate_owner_is_never_auto_killed(tmp_path: Path) -> None:
    rows = _production_process_rows()
    loop_a = next(
        row
        for row in rows
        if "-m datafetching.orchestrate " in str(row["command_line"])
    )
    rows.append({**loop_a, "pid": 99001, "ppid": 42})

    decision = _plan(tmp_path, rows)

    assert decision["status"] == "REPORT_ONLY"
    assert decision["runtime"] == "loop_a"
    assert decision["process_count"] == 3


def test_live_foreign_lock_blocks_a_missing_runtime_restart(tmp_path: Path) -> None:
    rows = [
        row
        for row in _production_process_rows()
        if "-m ml.strategy_runtime " not in str(row["command_line"])
    ]
    (tmp_path / ".ducketz-strategy-runtime.lock").write_text(
        "pid=77777\n", encoding="utf-8"
    )

    decision = _plan(tmp_path, rows, live_pids={77777})

    assert decision["status"] == "REPORT_ONLY"
    assert decision["lock"]["status"] == "FOREIGN_LIVE"


def test_targeted_lock_prepare_preserves_a_racing_live_owner(tmp_path: Path) -> None:
    runtime = _RUNTIME_BY_NAME["strategy"]
    lock = tmp_path / runtime.lock_name
    lock.write_text("pid=77777\n", encoding="utf-8")
    launch = _LAUNCH_BY_RUNTIME[runtime.name]

    def racing_processes() -> list[dict[str, object]]:
        lock.write_text("pid=88888\n", encoding="utf-8")
        return [
            {
                "pid": 88888,
                "ppid": 42,
                "created_at": "2026-08-19T18:42:00Z",
                "command_line": '"C:\\dev\\ducketz\\.venv\\Scripts\\python.exe" '
                + " ".join(launch.arguments),
            }
        ]

    with pytest.raises(RuntimeError, match="owner appeared"):
        system_guardian._prepare_exact_lock_for_restart(
            tmp_path,
            runtime,
            expected={"status": "STALE_DEAD", "pid": 77777},
            process_reader=racing_processes,
            pid_exists=lambda process_id: process_id == 88888,
        )

    assert lock.read_text(encoding="utf-8") == "pid=88888\n"


def test_integrity_failure_blocks_even_an_unambiguous_missing_runtime(
    tmp_path: Path,
) -> None:
    rows = [
        row
        for row in _production_process_rows()
        if "-m ml.strategy_runtime " not in str(row["command_line"])
    ]
    monitor = _monitor(
        status="UNHEALTHY",
        checks=[
            {
                "name": "strategy_publication",
                "status": "FAIL",
                "summary": "The read-only check could not verify its contract.",
                "details": {"reason": "ValueError: receipt checksum disagrees"},
            }
        ],
    )

    decision = _plan(tmp_path, rows, monitor=monitor)

    assert decision["status"] == "REPORT_ONLY"
    assert decision["integrity_blockers"] == ["strategy_publication"]


def test_credential_error_blocks_missing_runtime_restart(tmp_path: Path) -> None:
    rows = [
        row
        for row in _production_process_rows()
        if "-m datafetching.options_runtime " not in str(row["command_line"])
    ]
    monitor = _monitor(
        status="UNHEALTHY",
        checks=[
            {
                "name": "runtime_logs",
                "status": "WARN",
                "summary": "At least one runtime log needs inspection.",
                "details": {
                    "problems": ["options:recent-stderr"],
                    "runtimes": {
                        "options": {
                            "recent_stderr": True,
                            "stderr_tail": "Authentication failed: API key rejected",
                        }
                    },
                },
            }
        ],
    )

    decision = _plan(tmp_path, rows, monitor=monitor)

    assert decision["status"] == "REPORT_ONLY"
    assert "API key" in str(decision["stderr_tail"])


def test_recent_restart_receipt_enforces_cooldown(tmp_path: Path) -> None:
    rows = [
        row
        for row in _production_process_rows()
        if "-m ml.strategy_runtime " not in str(row["command_line"])
    ]
    audit = _audit_directory(tmp_path)
    audit.mkdir(parents=True)
    receipt = audit / "remediation-strategy-20260819T180000000000Z.json"
    receipt.write_text(
        json.dumps(
            {
                "runtime": "strategy",
                "attempted_at": "2026-08-19T18:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    decision = _plan(tmp_path, rows)

    assert decision["status"] == "COOLDOWN"
    assert decision["prior_receipt"] == str(receipt)


def _loop_b_hang_monitor(stdout: Path) -> dict[str, object]:
    return _monitor(
        status="UNHEALTHY",
        checks=[
            {
                "name": "runtime_logs",
                "status": "WARN",
                "summary": "At least one runtime log needs inspection.",
                "details": {
                    "problems": ["loop_b:stdout-stale"],
                    "runtimes": {
                        "loop_b": {
                            "stdout": str(stdout),
                            "stderr_tail": None,
                        }
                    },
                },
            },
            {
                "name": "loop_b_publication",
                "status": "FAIL",
                "summary": "Loop B's current prediction publication is incomplete or stale.",
                "details": {"failures": ["publication-stale"]},
            },
        ],
    )


def test_hang_requires_two_unchanged_scheduled_observations(tmp_path: Path) -> None:
    stdout = tmp_path / "logs" / "ducketz" / "directional-loop-b.stdout.log"
    stdout.parent.mkdir(parents=True)
    stdout.write_text("last progress\n", encoding="utf-8")
    rows = _production_process_rows()
    first = _plan(tmp_path, rows, monitor=_loop_b_hang_monitor(stdout))

    assert first["status"] == "OBSERVING_HANG"
    observation = first["observations_to_record"][0]
    _write_hang_observation(
        _audit_directory(tmp_path),
        observation,
        pd.Timestamp("2026-08-19T18:42:00Z"),
    )

    second = _plan(
        tmp_path,
        rows,
        monitor=_loop_b_hang_monitor(stdout),
        now="2026-08-19T19:42:00Z",
    )

    assert second["status"] == "REPORT_ONLY"
    assert second["runtime"] == "loop_b"
    assert second["lock"]["status"] == "MISSING"


def test_confirmed_hang_is_eligible_only_with_owned_lock(tmp_path: Path) -> None:
    stdout = tmp_path / "logs" / "ducketz" / "directional-loop-b.stdout.log"
    stdout.parent.mkdir(parents=True)
    stdout.write_text("last progress\n", encoding="utf-8")
    rows = _production_process_rows()
    first = _plan(tmp_path, rows, monitor=_loop_b_hang_monitor(stdout))
    _write_hang_observation(
        _audit_directory(tmp_path),
        first["observations_to_record"][0],
        pd.Timestamp("2026-08-19T18:42:00Z"),
    )
    loop_b_worker = next(
        row
        for row in rows
        if "-m ml.prediction_runtime " in str(row["command_line"])
        and int(row["ppid"]) != 42
    )
    (tmp_path / ".duckets-ml-prediction-runtime.lock").write_text(
        f"pid={loop_b_worker['pid']}\n", encoding="utf-8"
    )

    second = _plan(
        tmp_path,
        rows,
        monitor=_loop_b_hang_monitor(stdout),
        now="2026-08-19T19:42:00Z",
    )

    assert second["status"] == "ELIGIBLE"
    assert second["fault"] == "CONFIRMED_HANG"


def test_integrity_or_ui_failure_is_report_only_even_with_healthy_processes(
    tmp_path: Path,
) -> None:
    decision = _plan(
        tmp_path,
        _production_process_rows(),
        monitor=_monitor(
            status="UNHEALTHY",
            checks=[
                {
                    "name": "ui_contracts",
                    "status": "FAIL",
                    "summary": "A UI adapter contract failed verification.",
                    "details": {},
                }
            ],
        ),
    )

    assert decision["status"] == "REPORT_ONLY"
    assert "outside" in str(decision["summary"])


def test_execute_restart_stops_one_tree_clears_only_its_dead_lock_and_verifies(
    tmp_path: Path,
) -> None:
    launch = _LAUNCH_BY_RUNTIME["strategy"]
    command = (
        '"C:\\dev\\ducketz\\.venv\\Scripts\\python.exe" '
        + " ".join(launch.arguments)
    )
    old_row = {
        "pid": 101,
        "ppid": 42,
        "created_at": "2026-08-19T18:00:00Z",
        "command_line": command,
    }
    rows: list[dict[str, object]] = [old_row]
    alive = {101}
    lock = tmp_path / ".ducketz-strategy-runtime.lock"
    lock.write_text("pid=101\n", encoding="utf-8")
    stopped_roots: list[int] = []

    def stop_tree(root_process_id: int, _launch: object) -> list[int]:
        stopped_roots.append(root_process_id)
        alive.discard(root_process_id)
        rows.clear()
        return [root_process_id]

    def start_runtime(
        _root: Path, _logs: Path, _launch: object, _now: pd.Timestamp
    ) -> int:
        rows.extend(
            (
                {
                    "pid": 201,
                    "ppid": 42,
                    "created_at": "2026-08-19T18:42:01Z",
                    "command_line": command,
                },
                {
                    "pid": 202,
                    "ppid": 201,
                    "created_at": "2026-08-19T18:42:02Z",
                    "command_line": command,
                },
            )
        )
        alive.update({201, 202})
        lock.write_text("pid=202\n", encoding="utf-8")
        return 201

    decision = {
        "status": "ELIGIBLE",
        "runtime": "strategy",
        "fault": "PARTIAL_OWNER_PAIR",
        "processes": [old_row],
        "lock": {
            "status": "OWNED_BY_TARGET",
            "path": str(lock),
            "pid": 101,
        },
    }

    result, final_rows = _execute_guarded_restart(
        tmp_path,
        decision=decision,
        observed_at=pd.Timestamp("2026-08-19T18:42:00Z"),
        process_reader=lambda: list(rows),
        pid_exists=lambda process_id: process_id in alive,
        stop_process_tree=stop_tree,
        start_runtime=start_runtime,
        sleep=lambda _seconds: None,
        verify_seconds=0,
    )

    assert result["status"] == "RESTARTED_VERIFIED"
    assert result["removed_lock"] is True
    assert result["after_pids"] == [201, 202]
    assert stopped_roots == [101]
    assert list(final_rows or []) == rows
    assert lock.read_text(encoding="utf-8") == "pid=202\n"
