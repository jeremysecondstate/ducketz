"""Offline coordinator checks: no Codex calls, acquisition, fitting or trading."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import threading

import pandas as pd

import pytest

from ml.artifacts import file_checksum, write_manifest
from ml.nightly_workflow import (
    run_workflow, STEPS, VERSION, load_config, _native_outputs, _run_native,
    _display, _handoff, run_reviewer,
    _execute_step,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    repository = tmp_path / "repo"
    (repository / "datafetching").mkdir(parents=True)
    (repository / "datafetching/watchlist.local.txt").write_text("AAPL\nMSFT\n")
    root = tmp_path / "data"
    root.mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"actor": "Scout", "checkout": str(repository),
        "symbol_profile_path": str(repository / "datafetching/watchlist.local.txt"), "symbols": ["AAPL", "MSFT"]}))
    config = {"schema_version": VERSION, "actor": "Scout", "repository": str(repository),
        "datastore": str(root), "state_root": str(tmp_path / "state"), "local_profile": str(profile)}
    return config


def _identity(_):
    return {"commit": "a" * 40, "source_sha256": "b" * 64}


def _run(config, callback, **kwargs):
    return run_workflow(config, now="2026-10-06T04:05:00Z", execute_step=callback,
                        identity=_identity, supervise=False, **kwargs)


@pytest.mark.parametrize("actor", ["Atlas", "Scout"])
def test_stages_depend_on_success_and_duplicate_wake_does_no_work(setup, actor):
    setup["actor"] = actor
    profile_path = Path(setup["local_profile"])
    profile = json.loads(profile_path.read_text())
    profile["actor"] = actor
    profile_path.write_text(json.dumps(profile))
    calls = []
    def complete(config, state, step, save):
        assert all(state["steps"][prior]["status"] == "COMPLETE" for prior in STEPS[:STEPS.index(step)])
        calls.append(step)
        return {"files": {}}
    result = _run(setup, complete)
    assert calls == list(STEPS)
    assert result["source_session"] == "2026-10-05"
    assert result["action_date"] == "2026-10-06"
    assert result["status"] == "LOCAL_COMPLETE_PEER_SETUP_PENDING"
    assert result["actor"] == actor
    assert result["joint_status"] == (
        "Local preparation complete; cross-PC enablement pending; no communication or synthesis attempted")
    assert result["joint_ready"] is False
    assert result["orders_placed"] == 0
    _run(setup, complete)
    assert calls == list(STEPS)



@pytest.mark.parametrize("now, expected", [
    ("2026-10-07T04:05Z", "2026-10-07"),
    ("2026-10-07T10:35Z", "2026-10-07"),
    ("2026-10-07T12:30Z", "2026-10-07"),
    ("2026-10-10T04:05Z", "2026-10-12"),
    ("2026-12-25T05:05Z", "2026-12-28"),
    ("2026-11-03T05:05Z", "2026-11-03"),
])
def test_status_does_not_reuse_yesterday_completion(setup, now, expected):
    from ml.nightly_workflow import status
    _run(setup, lambda *args: {"files": {}})
    latest = Path(setup["state_root"]) / "latest.json"
    before = latest.read_bytes()
    result = status(setup, now=now)
    assert result["status"] == "NOT_STARTED"
    assert result["action_date"] == expected
    assert latest.read_bytes() == before


def test_next_night_runs_all_stages_after_previous_completion(setup):
    _run(setup, lambda *args: {"files": {}})
    calls = []
    result = run_workflow(setup, now="2026-10-07T04:05Z", identity=_identity, supervise=False,
        execute_step=lambda config, state, step, save: calls.append(step) or {"files": {}})
    assert calls == list(STEPS)
    assert result["action_date"] == "2026-10-07"


def test_explicit_recovery_preserves_missed_deadline_and_targets_today(setup):
    calls = []
    result = run_workflow(setup, now="2026-10-07T12:30Z", identity=_identity, supervise=False,
        recover_action_date="2026-10-07", recovery_deadline="2026-10-07T19:00Z",
        execute_step=lambda config, state, step, save: calls.append(step) or {"files": {}})
    assert calls == list(STEPS)
    assert result["action_date"] == "2026-10-07"
    assert result["source_session"] == "2026-10-06"
    assert result["deadline_at"] == "2026-10-07T11:00:00+00:00"
    assert result["recovery_deadline_at"] == "2026-10-07T19:00:00+00:00"
    with pytest.raises(ValueError, match="existing run"):
        run_workflow(setup, now="2026-10-07T12:35Z", identity=_identity, supervise=False,
            recover_action_date="2026-10-07", recovery_deadline="2026-10-07T19:05Z")


@pytest.mark.parametrize("day, deadline", [
    ("2026-10-06", "2026-10-07T19:00Z"),
    ("2026-10-08", "2026-10-07T19:00Z"),
    ("2026-10-07", "2026-10-07T12:00Z"),
    ("2026-10-07", "2026-10-08T00:01Z"),
])
def test_recovery_rejects_wrong_date_or_unbounded_deadline(setup, day, deadline):
    with pytest.raises(ValueError, match="today's missed session"):
        run_workflow(setup, now="2026-10-07T12:30Z", identity=_identity, supervise=False,
            recover_action_date=day, recovery_deadline=deadline,
            execute_step=lambda *args: pytest.fail("Invalid recovery launched"))


def test_failure_preserves_completed_stats_and_review_then_retries_only_remaining(setup):
    calls = []
    fail = True
    def action(config, state, step, save):
        calls.append(step)
        if step == "train_and_plan" and fail:
            state["steps"][step]["native_run"] = "saved-native-attempt"
            save()
            raise RuntimeError("fixture failure")
        return {"files": {}}
    with pytest.raises(RuntimeError, match="fixture"):
        _run(setup, action)
    path = Path(setup["state_root"]) / "runs/2026-10-06/state.json"
    failed = json.loads(path.read_text())
    assert failed["status"] == "FAILED"
    assert failed["steps"]["model_review"]["status"] == "COMPLETE"
    assert "verify_display" not in failed["steps"]
    fail = False
    _run(setup, action, resume_action_date="2026-10-06")
    assert calls == ["prepare_stats", "model_review", "train_and_plan", "train_and_plan", "verify_display", "local_handoff"]


def test_changed_output_rejected_before_retry(setup):
    output = Path(setup["datastore"]) / "receipt.json"
    output.write_text("original")
    def action(config, state, step, save):
        if step == "model_review":
            raise RuntimeError("offline reviewer")
        return {"files": {str(output): file_checksum(output)}}
    with pytest.raises(RuntimeError):
        _run(setup, action)
    output.write_text("different")
    with pytest.raises(ValueError, match="output changed"):
        _run(setup, action)


@pytest.mark.parametrize("resume", [False, True])
def test_source_change_preserves_terminal_original_completion(setup, resume):
    original = _run(setup, lambda *args: {"files": {}})
    path = Path(setup["state_root"]) / "runs/2026-10-06/state.json"
    before = path.read_bytes()
    result = run_workflow(setup, now="2026-10-06T04:05:00Z", identity=lambda _: {"commit": "changed"},
        resume_action_date="2026-10-06" if resume else None,
        execute_step=lambda *args: pytest.fail("Must not launch"), supervise=False)
    assert result == original and path.read_bytes() == before


def test_holiday_does_not_launch_or_replace_latest(setup):
    result = run_workflow(setup, now="2026-12-26T05:05:00Z", identity=_identity,
                         execute_step=lambda *args: pytest.fail("Holiday launched"), supervise=False)
    assert result["status"] == "NOOP_NON_SESSION_DATE"
    assert not (Path(setup["state_root"]) / "latest.json").exists()


def test_before_close_cannot_start(setup):
    with pytest.raises(RuntimeError, match="not finished"):
        run_workflow(setup, now="2026-10-05T22:00:00Z", identity=_identity,
                     execute_step=lambda *args: pytest.fail("Early launch"), supervise=False)


def test_resume_keeps_original_deadline(setup):
    with pytest.raises(RuntimeError):
        _run(setup, lambda *args: (_ for _ in ()).throw(RuntimeError("failure")))
    with pytest.raises(TimeoutError, match="Original"):
        run_workflow(setup, resume_action_date="2026-10-06", now="2026-10-06T12:00:00Z",
                     identity=_identity, execute_step=lambda *args: pytest.fail("Late rerun"), supervise=False)


def test_native_success_receipt_requires_matching_report_and_logs(tmp_path):
    report, log = tmp_path / "stage-report.json", tmp_path / "stage.log"
    report.write_text(json.dumps({"status": "COMPLETE"}))
    log.write_text("fixture output")
    (tmp_path / "receipt.json").write_text(json.dumps({"status": "COMPLETE",
        "stage_report_checksum_sha256": file_checksum(report),
        "logs": {log.name: {"checksum_sha256": file_checksum(log)}}}))
    assert len(_native_outputs(tmp_path)["files"]) == 3
    log.write_text("mutated")
    with pytest.raises(ValueError, match="log checksum"):
        _native_outputs(tmp_path)


def test_config_enforces_machine_identity_and_local_rollout_hold(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    profile = tmp_path / "profile.json"
    watchlist = tmp_path / "datafetching/watchlist.txt"
    watchlist.parent.mkdir()
    watchlist.write_text("AAPL\n")
    profile.write_text(json.dumps({"actor": "Scout", "checkout": str(tmp_path),
        "symbol_profile_path": str(watchlist), "symbols": ["AAPL"]}))
    config = {"schema_version": VERSION, "actor": "Scout", "repository": str(tmp_path),
        "datastore": str(tmp_path / "data"), "state_root": str(tmp_path / "state"),
        "local_profile": str(profile), "coordination_active": str(tmp_path / "active.json"),
        "reviewer": {"model": "gpt-6-astra", "reasoning_effort": "high"},
        "peer_communication_enabled": False}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert load_config(path)["actor"] == "Scout"
    config["actor"] = "Atlas"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="identity"):
        load_config(path)
    config.update(actor="Scout", peer_communication_enabled=True)
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="disabled"):
        load_config(path)


def _saved_state(config):
    return json.loads((Path(config["state_root"]) / "runs/2026-10-06/state.json").read_text())


def test_runtime_lock_failure_is_durable_without_running_a_stage(setup, monkeypatch):
    @contextmanager
    def busy(*args, **kwargs):
        raise RuntimeError("other runtime owner")
        yield
    monkeypatch.setattr("ml.nightly_workflow.exclusive_runtime_lock", busy)
    with pytest.raises(RuntimeError, match="other runtime"):
        _run(setup, lambda *args: pytest.fail("A locked runtime cannot launch"))
    failed = _saved_state(setup)
    assert failed["status"] == "FAILED" and failed["owner_pid"] is None
    assert "other runtime owner" in failed["error"]
    assert failed["steps"] == {}


def test_supervision_acquisition_failure_is_durable_and_releases_runtime_lock(setup, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime.claim_supervision", lambda *args, **kwargs: {"status": "DENIED"})
    with pytest.raises(RuntimeError, match="owns overnight supervision"):
        run_workflow(setup, now="2026-10-06T04:05:00Z", identity=_identity,
                     execute_step=lambda *args: pytest.fail("No supervision acquired"))
    assert _saved_state(setup)["status"] == "FAILED"
    assert not (Path(setup["datastore"]) / ".ducketz-overnight-runtime.lock").exists()


@pytest.mark.parametrize("reason", ["source", "supervision"])
def test_between_stage_failure_preserves_completed_outputs_and_records_failure(setup, monkeypatch, reason):
    lost = threading.Event()
    changed = False
    @contextmanager
    def supervision(*args):
        yield lost
    monkeypatch.setattr("ml.nightly_workflow._supervision", supervision)
    def identity(repository):
        return {"changed": True} if changed else _identity(repository)
    calls = []
    def complete(config, state, step, save):
        nonlocal changed
        calls.append(step)
        if reason == "source":
            changed = True
        else:
            lost.set()
        return {"files": {}}
    with pytest.raises((RuntimeError, ValueError), match="source changed|ownership was lost"):
        run_workflow(setup, now="2026-10-06T04:05:00Z", identity=identity, execute_step=complete)
    failed = _saved_state(setup)
    assert calls == ["prepare_stats"]
    assert failed["status"] == "FAILED" and failed["owner_pid"] is None
    assert failed["steps"]["prepare_stats"]["status"] == "COMPLETE"
    assert failed["steps"]["model_review"]["status"] == "FAILED"


def test_interrupt_is_recorded_without_erasing_prior_stage_completion(setup):
    def interrupt(config, state, step, save):
        if step == "model_review":
            raise KeyboardInterrupt("fixture cancellation")
        return {"files": {}}
    with pytest.raises(KeyboardInterrupt):
        _run(setup, interrupt)
    failed = _saved_state(setup)
    assert failed["status"] == "CANCELLED"
    assert failed["steps"]["prepare_stats"]["status"] == "COMPLETE"
    assert failed["steps"]["model_review"]["status"] == "CANCELLED"


def _feedback(root):
    from gameplan_stats_fixture import forecast, write_review
    from ml.gameplan_model_feedback import prepare_feedback, save_feedback_review, REVIEW_VERSION
    stats = write_review(root, [forecast(symbol) for symbol in ("AAPL", "GOOG", "NVDA")])
    feedback = prepare_feedback(root, now="2026-09-12T06:00Z")
    diagnosis = json.loads((feedback / "diagnostics.json").read_text())
    proposal = {"schema_version": REVIEW_VERSION,
        "stats_receipt_sha256": diagnosis["stats"]["receipt_sha256"], "reviewed_by": "Codex/original-fixture-model",
        "groups": {h: {"decision": "KEEP_CURRENT", "rationale": "Insufficient evidence for change.", "candidates": []}
                   for h in ("1h", "4h", "1d", "1w")}}
    saved = save_feedback_review(root, feedback, proposal, now="2026-09-12T06:01Z")
    return stats, feedback, saved


def test_saved_reviewer_response_is_reused_before_cli_lookup_or_inference(tmp_path, monkeypatch):
    _, feedback, saved = _feedback(tmp_path)
    original = saved.read_bytes()
    monkeypatch.setattr("ml.nightly_workflow.subprocess.Popen", lambda *args, **kwargs: pytest.fail("Duplicate inference"))
    monkeypatch.setattr("ml.nightly_workflow.shutil.which", lambda *args: pytest.fail("Saved review needs no CLI lookup"))
    result = run_reviewer({"datastore": str(tmp_path), "reviewer": {"model": "new-config-model"}}, feedback,
                          deadline="2026-09-14T11:00Z")
    assert result["reused"] is True and result["proposal"] == str(saved)
    assert result["model"] == "original-fixture-model"
    assert saved.read_bytes() == original


def test_saved_review_with_republished_stats_cannot_be_reused(tmp_path, monkeypatch):
    from gameplan_stats_fixture import write_review
    _, feedback, _ = _feedback(tmp_path)
    write_review(tmp_path, version="02")
    monkeypatch.setattr("ml.nightly_workflow.subprocess.Popen", lambda *args, **kwargs: pytest.fail("Unreviewed inference"))
    with pytest.raises(ValueError, match="Stats or training implementation changed"):
        run_reviewer({"datastore": str(tmp_path)}, feedback, deadline="2026-09-14T11:00Z")


def _write_native(run, *, pinned=None, status="COMPLETE"):
    run.mkdir(parents=True, exist_ok=True)
    report = {"status": status}
    if pinned is not None:
        report["enrichment_gameplan"] = pinned
    (run / "stage-report.json").write_text(json.dumps(report))
    (run / "receipt.json").write_text(json.dumps({"status": status,
        "stage_report_checksum_sha256": file_checksum(run / "stage-report.json"), "logs": {}}))


def test_native_production_dispatch_pins_segments_feedback_and_resume(setup, monkeypatch):
    root = Path(setup["datastore"])
    proposal = root / "reviewed-proposal.json"
    proposal.write_text("fixture")
    state = {"deadline_at": "2026-10-06T11:00Z", "source_session": "2026-10-05", "steps": {
        "prepare_stats": {}, "model_review": {"output": {"proposal": str(proposal)}}, "train_and_plan": {}}}
    calls = []
    def native(datastore, **kwargs):
        calls.append(kwargs)
        run = root / "ml/overnight-runs" / str(len(calls))
        _write_native(run)
        return run
    monkeypatch.setattr("ml.overnight_runtime.run_overnight_pipeline", native)
    monkeypatch.setattr("ml.overnight_runtime.overnight_status", lambda *_: {"owner_pid": -1, "run_path": "unowned"})
    _run_native(setup, state, "prepare_stats", lambda: None)
    assert calls[0]["stop_after"] == "gameplan_stats"
    assert calls[0]["review_action_date"] == "2026-10-05" and calls[0]["stats_first"] is True
    assert "model_feedback" not in calls[0]
    _run_native(setup, state, "train_and_plan", lambda: None)
    assert calls[1]["start_at"] == "loop_b_directional_generation"
    assert calls[1]["model_feedback"] == proposal
    # A crash after the native completion must not fit again.
    _run_native(setup, state, "train_and_plan", lambda: None)
    assert len(calls) == 2
    previous = Path(state["steps"]["train_and_plan"]["native_run"])
    _write_native(previous, status="FAILED")
    _run_native(setup, state, "train_and_plan", lambda: None)
    assert calls[2]["resume_run"] == previous and calls[2]["model_feedback"] == proposal
    assert calls[2]["deadline"] == state["deadline_at"]


def test_native_attempt_identity_is_durable_before_stage_work_can_run(setup, monkeypatch):
    root = Path(setup["datastore"])
    native = root / "ml/overnight-runs/started"
    state = {"deadline_at": "2026-10-06T11:00Z", "source_session": "2026-10-05",
             "steps": {"prepare_stats": {}}}
    snapshots = []
    monkeypatch.setattr("ml.overnight_runtime.overnight_status",
                        lambda *_: {"owner_pid": os.getpid(), "run_path": str(native)})
    def start_then_stop(datastore, **kwargs):
        _write_native(native, status="RUNNING")
        kwargs["reporter"]("OVERNIGHT RUN fixture")
        assert snapshots[-1]["steps"]["prepare_stats"]["native_run"] == str(native)
        raise RuntimeError("Stopped before fixture stage work")
    monkeypatch.setattr("ml.overnight_runtime.run_overnight_pipeline", start_then_stop)
    with pytest.raises(RuntimeError, match="fixture stage work"):
        _run_native(setup, state, "prepare_stats", lambda: snapshots.append(json.loads(json.dumps(state))))
    assert snapshots[0]["steps"]["prepare_stats"]["native_run"] == str(native)


def _display_fixture(root):
    from gameplan_fixture import write_plan
    stats, feedback, saved = _feedback(root)
    source = root / "ml/nightly-gameplan-runs/accepted"
    source.mkdir(parents=True)
    (source / "receipt.json").write_text(json.dumps({"fixture": "accepted"}))
    source_ref = source.relative_to(root).as_posix()
    source_hash = file_checksum(source / "receipt.json")
    trade = write_plan(root, report_updates={"source_gameplan_run": source_ref})
    report = json.loads((trade / "report.json").read_text())
    report["source_receipt_sha256"] = source_hash
    (trade / "report.json").write_text(json.dumps(report))
    manifest = json.loads((trade / "manifest.json").read_text())
    manifest["configuration"].update(source_receipt_sha256=source_hash, source_gameplan_run=source_ref)
    write_manifest(trade, run_timestamp=manifest["run_timestamp"], input_files=[],
                   output_files=list(manifest["output_files"]), configuration=manifest["configuration"])
    receipt = json.loads((trade / "receipt.json").read_text())
    receipt.update(source_receipt_sha256=source_hash, source_gameplan_run=source_ref,
                   manifest_sha256=file_checksum(trade / "manifest.json"))
    (trade / "receipt.json").write_text(json.dumps(receipt))
    pointer_path = root / "ml/gameplan-trade-plan-latest/run.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["current"].update(source_receipt_sha256=source_hash, receipt_sha256=file_checksum(trade / "receipt.json"))
    pointer_path.write_text(json.dumps(pointer))
    native = root / "ml/overnight-runs/accepted"
    _write_native(native, pinned={"run_path": source_ref, "receipt_sha256": source_hash, "action_date": "2026-09-14"})
    state = {"action_date": "2026-09-14", "source_session": "2026-09-11", "symbols": ["AAPL", "GOOG", "NVDA"],
             "steps": {"model_review": {"output": {"proposal": str(saved), "files": {str(saved): file_checksum(saved)}}},
                       "train_and_plan": {"output": _native_outputs(native)}}}
    return state, trade, stats, source


def test_display_uses_real_ui_readers_and_exact_review_training_artifacts(tmp_path):
    state, trade, stats, source = _display_fixture(tmp_path)
    output = _display({"datastore": str(tmp_path)}, state)
    assert output["plan_run"] == str(trade) and output["stats_run"] == str(stats)
    assert output["source_gameplan_run"] == str(source)
    assert str(source / "receipt.json") in output["files"]


def test_same_session_plan_replacement_cannot_pass_display_verification(tmp_path):
    state, _, _, _ = _display_fixture(tmp_path)
    from gameplan_fixture import write_plan
    write_plan(tmp_path, run_name="another-same-session-plan")
    with pytest.raises(ValueError, match="pinned training publication"):
        _display({"datastore": str(tmp_path)}, state)


def test_local_display_uses_reviewed_stats_before_joint_handoff(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.ui.gameplan_data import load_gameplan, GameplanError
    from gameplan_stats_fixture import write_review
    state, trade, stats, _ = _display_fixture(tmp_path)
    write_review(tmp_path, version="02")
    monkeypatch.setattr("ml.account_gameplan.config.load_account_config",
        lambda root: SimpleNamespace(activation={"status":"ACTIVE"}))
    with pytest.raises(GameplanError, match="shared account Gameplan is not available"):
        load_gameplan(tmp_path)
    output = _display({"datastore": str(tmp_path)}, state)
    assert output["plan_run"] == str(trade)
    assert output["stats_run"] == str(stats)
    with pytest.raises(GameplanError, match="shared account Gameplan is not available"):
        load_gameplan(tmp_path)


def test_handoff_uses_explicit_immutable_stats_filename_and_is_retryable(tmp_path, monkeypatch):
    state, trade, _, source = _display_fixture(tmp_path)
    config = {"datastore": str(tmp_path), "state_root": str(tmp_path / "state"), "actor": "Atlas"}
    state["steps"]["verify_display"] = {"output": _display(config, state)}
    state["steps"]["local_handoff"] = {"started_at": "2026-09-14T09:00Z"}
    def owner_export(root, **kwargs):
        assert kwargs["gameplan_run"] == source and kwargs["trade_plan_run"] == trade
        assert kwargs["created_at"] == "2026-09-14T09:00Z"
        path = kwargs["output_root"] / "fixture-plan.json"
        path.write_text("fixture local package")
        return path
    monkeypatch.setattr("ml.joint_capital_handoff.export_owner_package", owner_export)
    first = _handoff(config, state)
    stats = Path(first["stats_package"])
    assert stats.is_file() and stats.name.startswith("atlas-2026-09-11-") and stats.name.endswith("-stats.json")
    assert json.loads(stats.read_text())["producer"] == "Atlas"
    assert _handoff(config, state) == first


def test_profile_can_bind_shared_watchlist_without_a_local_overlay(setup):
    profile_path = Path(setup["local_profile"])
    profile = json.loads(profile_path.read_text())
    local = Path(profile["symbol_profile_path"])
    shared = local.with_name("watchlist.txt")
    local.rename(shared)
    profile.update(actor="Atlas", symbol_profile_path=str(shared))
    profile_path.write_text(json.dumps(profile))
    setup["actor"] = "Atlas"
    result = _run(setup, lambda *args: {"files": {}})
    assert result["symbols"] == profile["symbols"]
    assert result["symbol_binding"]["path"] == str(shared)
    assert result["symbol_binding"]["sha256"] == file_checksum(shared)


def test_inherited_watchlist_override_cannot_change_machine_authority(setup, monkeypatch):
    alternate = Path(setup["datastore"]) / "other-watchlist.txt"
    alternate.write_text("AAPL\nMSFT\n")
    monkeypatch.setenv("DUCKETS_PRODUCTION_WATCHLIST", str(alternate))
    with pytest.raises(ValueError, match="override differs"):
        _run(setup, lambda *args: pytest.fail("An inherited override cannot launch stages"))


def test_watchlist_edits_cannot_reuse_prior_completed_stats_on_resume(setup):
    def fail_review(config, state, step, save):
        if step == "model_review":
            raise RuntimeError("fixture review unavailable")
        return {"files": {}}
    with pytest.raises(RuntimeError):
        _run(setup, fail_review)
    watchlist = Path(json.loads(Path(setup["local_profile"]).read_text())["symbol_profile_path"])
    watchlist.write_text(watchlist.read_text() + "# changed after Stats\n")
    with pytest.raises(ValueError, match="symbol ownership changed"):
        _run(setup, lambda *args: pytest.fail("Changed membership artifact cannot resume"), resume_action_date="2026-10-06")
    failed = _saved_state(setup)
    assert failed["status"] == "FAILED"
    assert failed["steps"]["prepare_stats"]["status"] == "COMPLETE"
    assert failed["symbols"] == ["AAPL", "MSFT"]


def test_watchlist_must_match_profile_symbols_before_any_work(setup):
    watchlist = Path(json.loads(Path(setup["local_profile"]).read_text())["symbol_profile_path"])
    watchlist.write_text("AAPL\nTSLA\n")
    with pytest.raises(ValueError, match="local profile symbols"):
        _run(setup, lambda *args: pytest.fail("Wrong universe cannot start"))


@pytest.mark.parametrize("change", ["datastore", "stock_price_source", "archive_history", "reviewer", "probability_target_contract"])
def test_operating_configuration_cannot_inherit_another_completed_run(setup, change):
    _run(setup, lambda *args: {"files": {}})
    original = _saved_state(setup)
    replacement = {"datastore": str(Path(setup["datastore"]) / "other"),
                   "stock_price_source": "canonical-equity-minute-v1", "archive_history": False,
                   "reviewer": {"model": "different-model", "reasoning_effort": "high"},
                   "probability_target_contract": "cost-adjusted-positive-return-v1"}
    setup[change] = replacement[change]
    with pytest.raises(ValueError, match="operating configuration changed"):
        _run(setup, lambda *args: pytest.fail("Cannot reuse a completion under a changed configuration"))
    failed = _saved_state(setup)
    assert failed == original
    assert all(stage["status"] == "COMPLETE" for stage in failed["steps"].values())


def test_first_run_empty_stats_reaches_reviewed_raw_training_interface_without_fitting(tmp_path, monkeypatch):
    from ml.gameplan_actuals_review import publish_completed_session_review
    from ml.gameplan_model_feedback import REVIEW_VERSION, load_feedback_review
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    observed = pd.Timestamp("2026-09-12T06:05Z")
    monkeypatch.setattr("ml.nightly_workflow.utc_timestamp", lambda value=None: pd.Timestamp(value) if value is not None else observed)
    monkeypatch.setattr("ml.gameplan_model_feedback.utc_timestamp", lambda value=None: pd.Timestamp(value) if value is not None else observed)
    stats = publish_completed_session_review(tmp_path, action_date="2026-09-11", clock=lambda: observed,
        price_loader=lambda *args, **kwargs: pytest.fail("Baseline must not fetch prices"))
    original = (stats / "forecast-results.parquet").read_bytes()
    binary = tmp_path / "fixture-codex.exe"
    binary.write_text("This fixture executable is never launched")
    config = {"actor": "Scout", "datastore": str(tmp_path), "repository": str(tmp_path),
              "codex_executable": str(binary), "reviewer": {"model": "fixture-model", "reasoning_effort": "high"}}
    state = {"source_session": "2026-09-11", "action_date": "2026-09-14", "deadline_at": "2026-09-14T11:00Z",
             "steps": {"model_review": {}, "train_and_plan": {}}}
    class Reviewer:
        returncode = 0
        def __init__(self, command, **kwargs):
            self.answer = Path(command[command.index("--output-last-message") + 1])
        def communicate(self, prompt, **kwargs):
            text = prompt.decode("utf-8")
            assert "sum to at most 512" in text and "zero completed observations must KEEP_CURRENT" in text
            diagnosis = json.loads((self.answer.parent / "diagnostics.json").read_text())
            assert diagnosis["stats"]["baseline_no_saved_predictions"] is True
            self.answer.write_text(json.dumps({"schema_version": REVIEW_VERSION,
                "stats_receipt_sha256": diagnosis["stats"]["receipt_sha256"], "reviewed_by": "Codex/fixture-model",
                "groups": {h: {"decision": "KEEP_CURRENT", "rationale": "No saved outcomes to justify changes.", "candidates": []}
                           for h in ("1h", "4h", "1d", "1w")}}))
    monkeypatch.setattr("ml.nightly_workflow.subprocess.Popen", Reviewer)
    review_output = _execute_step(config, state, "model_review", lambda: None)
    state["steps"]["model_review"]["output"] = review_output
    called = []
    def train_interface(root, **kwargs):
        called.append(kwargs)
        assert kwargs["probability_target_contract"] == RAW_DIRECTION_TARGET
        reviewed = load_feedback_review(root, kwargs["model_feedback"], probability_target=RAW_DIRECTION_TARGET, as_of=observed)
        assert reviewed["stats"]["probability_target_contract"] is None
        assert all(item["decision"] == "KEEP_CURRENT" for item in reviewed["proposal"]["groups"].values())
        run = root / "ml/overnight-runs/baseline-fixture"
        _write_native(run)
        return run
    monkeypatch.setattr("ml.overnight_runtime.run_overnight_pipeline", train_interface)
    monkeypatch.setattr("ml.overnight_runtime.overnight_status", lambda *_: {})
    _execute_step(config, state, "train_and_plan", lambda: None)
    assert len(called) == 1
    assert (stats / "forecast-results.parquet").read_bytes() == original


def test_frozen_local_preparation_survives_combined_stats_display(tmp_path):
    from ml.nightly_workflow import _verify_local_preparation
    from ml.gameplan_stats_handoff import export_stats_package, adopt_combined_stats
    from gameplan_stats_fixture import write_review, forecast
    state, trade, stats, source = _display_fixture(tmp_path)
    config = {"datastore": str(tmp_path)}
    saved = _display(config, state)
    state["steps"]["verify_display"] = {"output": saved}
    peer = tmp_path / "peer"
    write_review(peer, [forecast("ABCL")])
    selections, hashes = {}, {}
    universes = {"Scout": state["symbols"], "Atlas": ["ABCL"]}
    for actor, root in (("Scout", tmp_path), ("Atlas", peer)):
        selected = export_stats_package(root, producer=actor, symbols=universes[actor],
            destination=tmp_path / f"{actor}-stats.json")
        selections[actor], hashes[actor] = selected, file_checksum(selected)
    adopt_combined_stats(tmp_path, packages=selections, expected_symbols=universes,
                        expected_sha256=hashes, reviewed_at="2026-09-14T09:30Z")
    assert _verify_local_preparation(config, state) == saved
    assert _display(config, state) == saved
    (trade / "direction-ledger.json").write_text("changed")
    with pytest.raises((ValueError, RuntimeError)):
        _verify_local_preparation(config, state)
