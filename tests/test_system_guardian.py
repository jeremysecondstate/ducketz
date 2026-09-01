from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Mapping

import pandas as pd
import pytest

import ml.system_guardian as system_guardian
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
