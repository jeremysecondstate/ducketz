"""Offline stale-review recovery using real immutable Stats and review evidence."""
from copy import deepcopy
import json
from pathlib import Path

from filelock import FileLock, Timeout
import pandas as pd
import pytest

from gameplan_stats_fixture import forecast, write_review
from ml import gameplan_model_feedback as feedback
from ml import nightly_workflow as workflow
from ml.artifacts import file_checksum
from ml.nightly_dispatch import LAYOUT


def _proposal(diagnostics):
    return {"schema_version": feedback.REVIEW_VERSION,
        "stats_receipt_sha256": diagnostics["stats"]["receipt_sha256"], "reviewed_by": "fixture-reviewer",
        "groups": {group: {"decision": "KEEP_CURRENT", "rationale": "Keep the existing candidates.", "candidates": []}
                   for group in ("1h", "4h", "1d", "1w")}}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    stamp = pd.Timestamp("2026-09-12T06:05Z")
    clock = lambda value=None: pd.Timestamp(value) if value is not None else stamp
    monkeypatch.setattr(workflow, "utc_timestamp", clock)
    monkeypatch.setattr(feedback, "utc_timestamp", clock)
    repository = tmp_path / "repository"
    watchlist = repository / "datafetching/watchlist.local.txt"
    watchlist.parent.mkdir(parents=True)
    watchlist.write_text("AAPL\nMSFT\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"actor": "Scout", "checkout": str(repository),
        "symbol_profile_path": str(watchlist), "symbols": ["AAPL", "MSFT"]}))
    root = tmp_path / "data"
    stats = write_review(root, [forecast("AAPL"), forecast("MSFT")])
    review = feedback.prepare_feedback(root, now="2026-09-12T06:00Z")
    diagnostics = json.loads((review / "diagnostics.json").read_text())
    saved = feedback.save_feedback_review(root, review, _proposal(diagnostics), now="2026-09-12T06:01Z")
    native = root / "ml/overnight-runs/original-failed"
    native.mkdir(parents=True)
    (native / "receipt.json").write_text('{"status":"FAILED"}')
    (native / "fitted-model.fixture").write_bytes(b"saved numerical work must remain")
    config = {"actor": "Scout", "repository": str(repository), "datastore": str(root),
        "state_root": str(tmp_path / "state"), "local_profile": str(profile),
        "continue_until_complete": True, "probability_target_contract": "cost-adjusted-positive-return-v1",
        "reviewer": {"model": "fixture", "reasoning_effort": "high"}}
    state = {"schema_version": workflow.VERSION, "actor": "Scout", "run_id": "original-workflow-id",
        "action_date": "2026-09-14", "source_session": "2026-09-11", "deadline_at": "2026-09-14T11:00Z",
        "source_identity": {"commit": "a" * 40, "source_sha256": "b" * 64},
        "configuration_binding": workflow._configuration_binding(config),
        "symbol_binding": workflow._symbol_binding(config), "symbols": ["AAPL", "MSFT"],
        "workflow_layout": LAYOUT, "status": "FAILED", "current_step": "train_and_plan", "owner_pid": None,
        "error": "original failed publication", "orders_placed": 0, "broker_orders_enabled": False,
        "failure": {"step": "train_and_plan", "disposition": "OPEN", "kind": "SOURCE_DEFECT"},
        "failure_history": [{"step": "train_and_plan", "error": "original failed publication"}],
        "steps": {
            "datastore_catchup": {"status": "COMPLETE", "attempts": 1, "output": {"files": {}}},
            "prepare_stats": {"status": "COMPLETE", "attempts": 1,
                "output": {"files": {str(path): file_checksum(path) for path in stats.iterdir()}}},
            "model_review": {"status": "COMPLETE", "attempts": 1, "feedback_run": str(review),
                "completed_at": "2026-09-12T06:01Z",
                "output": workflow._review_outputs(review, saved, "fixture-reviewer", reused=False)},
            "train_and_plan": {"status": "FAILED", "attempts": 2, "native_run": str(native)}}}
    path = Path(config["state_root"]) / "runs/2026-09-14/state.json"
    workflow._write(path, state)
    return config, state, path, (stats, review, native)


def _bytes(directories):
    return {path: path.read_bytes() for root in directories for path in root.rglob("*") if path.is_file()}


def _change_code(monkeypatch):
    code = feedback._code_binding()
    code["ml/nightly_gameplan.py"] = "c" * 64
    monkeypatch.setattr(feedback, "_code_binding", lambda: code)


def _executor(monkeypatch, calls):
    def reviewer(config, run, deadline):
        calls.append("reviewer")
        diagnostics = json.loads((run / "diagnostics.json").read_text())
        saved = feedback.save_feedback_review(Path(config["datastore"]), run, _proposal(diagnostics))
        return workflow._review_outputs(run, saved, "fixture-reviewer", reused=False)
    monkeypatch.setattr(workflow, "run_reviewer", reviewer)
    def execute(config, state, step, save):
        calls.append(step)
        if step == "model_review":
            return workflow._execute_step(config, state, step, save)
        assert step == "train_and_plan"
        feedback.load_feedback_review(Path(config["datastore"]), Path(state["steps"]["model_review"]["output"]["proposal"]))
        return {"files": {}}
    return execute


def _resume(config, execute):
    return workflow.run_workflow(config, resume_action_date="2026-09-14", responsibility="model",
        execute_step=execute, now="2026-09-12T06:05Z", supervise=False,
        identity=lambda _: {"commit": "a" * 40, "source_sha256": "b" * 64})


@pytest.mark.parametrize("changed", ["code", "stats", "policy_added", "policy_removed"])
def test_stale_review_refreshes_once_and_preserves_completed_work(prepared, monkeypatch, changed):
    config, original, path, directories = prepared
    original_bytes = path.read_bytes()
    artifacts = _bytes(directories)
    if changed == "code":
        _change_code(monkeypatch)
    elif changed == "stats":
        write_review(Path(config["datastore"]), [forecast("AAPL"), forecast("MSFT")], version="02")
    else:
        code = feedback._code_binding()
        if changed == "policy_added":
            code["ml/additional_policy.py"] = "d" * 64
        else:
            code.pop("ml/calibration.py")
        monkeypatch.setattr(feedback, "_code_binding", lambda: code)
    calls = []
    execute = _executor(monkeypatch, calls)
    def checked(config, state, step, save):
        assert Path(config["datastore"], ".ducketz-overnight-runtime.lock").exists()
        with pytest.raises(Timeout):
            with FileLock(str(Path(config["state_root"]) / "workflow.lock"), timeout=0):
                pass
        if step == "train_and_plan":
            assert "native_run" not in state["steps"][step]
        return execute(config, state, step, save)
    result = _resume(config, checked)
    assert calls == ["model_review", "reviewer", "train_and_plan"]
    assert result["steps"]["model_review"]["attempts"] == 2
    assert result["steps"]["train_and_plan"]["attempts"] == 3
    for key in ("run_id", "source_session", "action_date", "deadline_at", "source_identity", "symbol_binding"):
        assert result[key] == original[key]
    for step in ("datastore_catchup", "prepare_stats"):
        assert result["steps"][step] == original["steps"][step]
    assert result["failure_history"] == original["failure_history"]
    assert result["failure"]["disposition"] == "RESOLVED"
    assert _bytes(directories) == artifacts
    recovery = Path(result["operational_recoveries"][0]["path"])
    assert (recovery.parent / "before-state.json").read_bytes() == original_bytes
    assert json.loads(recovery.read_text())["before_state_sha256"] == file_checksum(recovery.parent / "before-state.json")
    assert result["steps"]["model_review"]["feedback_run"] != str(directories[1])
    _resume(config, lambda *args: pytest.fail("Duplicate inference or completed training"))
    assert len(json.loads(path.read_text())["operational_recoveries"]) == 1


def test_unchanged_basis_reuses_review_and_native_attempt(prepared, monkeypatch):
    config, original, _, _ = prepared
    calls = []
    execute = _executor(monkeypatch, calls)
    def checked(config, state, step, save):
        assert state["steps"]["model_review"] == original["steps"]["model_review"]
        assert state["steps"]["train_and_plan"]["native_run"] == original["steps"]["train_and_plan"]["native_run"]
        return execute(config, state, step, save)
    result = _resume(config, checked)
    assert calls == ["train_and_plan"]
    assert "operational_recoveries" not in result


def test_mid_fit_change_recovers_on_next_wake(prepared, monkeypatch):
    config, _, path, directories = prepared
    def interrupted_fit(config, state, step, save):
        assert step == "train_and_plan"
        _change_code(monkeypatch)
        feedback.load_feedback_review(Path(config["datastore"]), Path(state["steps"]["model_review"]["output"]["proposal"]))
    with pytest.raises(ValueError, match="Stats or training implementation changed"):
        _resume(config, interrupted_fit)
    failed = json.loads(path.read_text())
    assert failed["status"] == "FAILED"
    assert "operational_recoveries" not in failed
    saved_artifacts = _bytes(directories)
    calls = []
    result = _resume(config, _executor(monkeypatch, calls))
    assert calls == ["model_review", "reviewer", "train_and_plan"]
    assert result["steps"]["train_and_plan"]["status"] == "COMPLETE"
    assert result["failure_history"] == failed["failure_history"]
    assert _bytes(directories) == saved_artifacts


@pytest.mark.parametrize("corrupt", ["proposal", "diagnostics", "stats", "malformed_binding"])
def test_corrupt_original_evidence_is_not_reclassified_as_stale(prepared, monkeypatch, corrupt):
    config, state, path, directories = prepared
    stats, review, _ = directories
    _change_code(monkeypatch)
    target = {"proposal": review / "reviewed-proposal.json", "diagnostics": review / "diagnostics.json",
              "stats": stats / "forecast-results.parquet"}.get(corrupt)
    if target:
        target.write_bytes(target.read_bytes() + b"corrupt")
    else:
        diagnosis = json.loads((review / "diagnostics.json").read_text())
        diagnosis["training_code"]["ml/nightly_gameplan.py"] = "not-a-hash"
        workflow._write(review / "diagnostics.json", diagnosis)
        payload = json.loads((review / "reviewed-proposal.json").read_text())
        payload["diagnostics_sha256"] = file_checksum(review / "diagnostics.json")
        workflow._write(review / "reviewed-proposal.json", payload)
        state["steps"]["model_review"]["output"] = workflow._review_outputs(
            review, review / "reviewed-proposal.json", "fixture-reviewer", reused=False)
        workflow._write(path, state)
    with pytest.raises(ValueError):
        _resume(config, lambda *args: pytest.fail("Corrupt evidence cannot launch work"))
    failed = json.loads(path.read_text())
    assert "operational_recoveries" not in failed
    assert failed["steps"]["model_review"]["feedback_run"] == str(review)
    assert failed["steps"]["train_and_plan"]["native_run"] == str(directories[2])


def test_unrelated_stats_error_is_not_recovered(prepared, monkeypatch):
    config, _, path, _ = prepared
    original_stats = feedback._stats
    def failing_stats(root, review_run=None, *, frozen_binding=None):
        if frozen_binding is None:
            raise ValueError("Unrelated current Stats error")
        return original_stats(root, review_run, frozen_binding=frozen_binding)
    monkeypatch.setattr(feedback, "_stats", failing_stats)
    with pytest.raises(ValueError, match="Unrelated current Stats error"):
        _resume(config, lambda *args: pytest.fail("No launch"))
    assert "operational_recoveries" not in json.loads(path.read_text())


def test_newer_session_is_not_substituted_for_original_session(prepared, monkeypatch):
    config, _, path, _ = prepared
    _change_code(monkeypatch)
    write_review(Path(config["datastore"]), [forecast("AAPL", session="2026-09-14")], session="2026-09-14")
    with pytest.raises(ValueError, match="different workflow session"):
        _resume(config, lambda *args: pytest.fail("No launch"))
    assert "operational_recoveries" not in json.loads(path.read_text())


def test_terminal_workflow_does_not_refresh_or_rewrite(prepared, monkeypatch):
    config, state, path, _ = prepared
    state["status"] = "COMPLETE"
    state["steps"]["train_and_plan"] = {"status": "COMPLETE", "output": {"files": {}}}
    workflow._write(path, state)
    before = path.read_bytes()
    _change_code(monkeypatch)
    result = _resume(config, lambda *args: pytest.fail("Terminal workflow replayed"))
    assert result == state
    assert path.read_bytes() == before


def test_completed_training_uses_frozen_review_for_display(tmp_path, monkeypatch):
    from test_nightly_workflow import _display_fixture
    state, _, _, _ = _display_fixture(tmp_path)
    state["steps"]["train_and_plan"].update(status="COMPLETE", completed_at="2026-09-12T06:05Z")
    output = state["steps"]["model_review"]["output"]
    diagnostic_path = Path(output["proposal"]).parent / "diagnostics.json"
    output["files"][str(diagnostic_path)] = file_checksum(diagnostic_path)
    _change_code(monkeypatch)
    displayed = workflow._display({"datastore": str(tmp_path)}, state)
    assert displayed["source_gameplan_run"].endswith("accepted")
    # The following handoff also consumes the frozen accepted evidence.
    reviewed = workflow._completed_feedback(tmp_path, state)
    assert reviewed["checksum_sha256"] == file_checksum(Path(output["proposal"]))


def test_completed_native_receipt_survives_crash_before_workflow_save(prepared, monkeypatch):
    from ml import overnight_runtime
    from test_nightly_workflow import _write_native
    config, state, path, directories = prepared
    native = directories[2]
    _write_native(native)
    state["status"] = "RUNNING"
    state["steps"]["train_and_plan"]["status"] = "RUNNING"
    workflow._write(path, state)
    original_review = deepcopy(state["steps"]["model_review"])
    originals = _bytes(directories)
    _change_code(monkeypatch)
    monkeypatch.setattr(overnight_runtime, "run_overnight_pipeline", lambda *args, **kwargs: pytest.fail("Completed native segment was refit"))
    monkeypatch.setattr(overnight_runtime, "overnight_status", lambda *args: {})
    result = _resume(config, workflow._execute_step)
    assert result["steps"]["train_and_plan"]["status"] == "COMPLETE"
    assert result["steps"]["train_and_plan"]["output"] == workflow._native_outputs(native)
    assert result["steps"]["model_review"] == original_review
    assert "operational_recoveries" not in result
    assert _bytes(directories) == originals


def test_malformed_completed_native_receipt_does_not_trigger_refit(prepared, monkeypatch):
    config, _, path, directories = prepared
    native = directories[2]
    (native / "receipt.json").write_text('{"status":"COMPLETE"}')
    (native / "stage-report.json").write_text('{"status":"FAILED"}')
    _change_code(monkeypatch)
    with pytest.raises(ValueError, match="Native stage segment did not complete"):
        _resume(config, lambda *args: pytest.fail("No refit of malformed completion"))
    assert "operational_recoveries" not in json.loads(path.read_text())


def test_published_gameplan_with_failed_tail_retains_native_resume(prepared, monkeypatch):
    from ml.overnight_runtime import OVERNIGHT_RUNTIME_VERSION
    config, state, _, directories = prepared
    native = directories[2]
    report = {"status": "FAILED", "deadline_at": state["deadline_at"],
        "stats_first": True, "stock_only": True, "independent_stock_horizons": True,
        "failed_stage": "stock_enrichment_training", "enrichment_gameplan": {"run_path": "saved-gameplan"},
        "stages": [{"stage": "gameplan_publication", "status": "COMPLETE"},
                   {"stage": "stock_enrichment_training", "status": "FAILED"}]}
    workflow._write(native / "stage-report.json", report)
    workflow._write(native / "receipt.json", {"schema_version": OVERNIGHT_RUNTIME_VERSION,
        "status": "FAILED", "run_path": native.relative_to(Path(config["datastore"])).as_posix(),
        "stage_report_checksum_sha256": file_checksum(native / "stage-report.json"),
        "orders_placed": 0, "broker_orders_enabled": False, "logs": {}})
    _change_code(monkeypatch)
    originals = _bytes(directories)
    calls = []
    def resume_tail(config, current, step, save):
        calls.append(step)
        assert current["steps"][step]["native_run"] == str(native)
        assert current["steps"]["model_review"] == state["steps"]["model_review"]
        return {"files": {}}
    result = _resume(config, resume_tail)
    assert calls == ["train_and_plan"]
    assert "operational_recoveries" not in result
    assert _bytes(directories) == originals


def test_unsealed_native_tail_reaches_existing_interruption_recovery(prepared, monkeypatch):
    from ml import overnight_runtime
    from test_nightly_workflow import _write_native
    config, original, _, directories = prepared
    native = directories[2]
    (native / "receipt.json").unlink()
    workflow._write(native / "stage-report.json", {"status": "FAILED",
        "failed_stage": "stock_enrichment_training", "enrichment_gameplan": {"run_path": "saved-gameplan"}})
    _change_code(monkeypatch)
    calls = []
    def seal(root, run, reason):
        calls.append("seal")
        assert run == native
        _write_native(run, status="FAILED")
    def resume(root, **kwargs):
        calls.append("resume")
        assert kwargs["resume_run"] == native
        assert str(kwargs["model_feedback"]) == original["steps"]["model_review"]["output"]["proposal"]
        completed = root / "ml/overnight-runs/completed-tail"
        _write_native(completed)
        return completed
    monkeypatch.setattr(overnight_runtime, "recover_interrupted_run", seal)
    monkeypatch.setattr(overnight_runtime, "run_overnight_pipeline", resume)
    monkeypatch.setattr(overnight_runtime, "overnight_status", lambda *args: {})
    result = _resume(config, workflow._execute_step)
    assert calls == ["seal", "resume"]
    assert result["steps"]["train_and_plan"]["status"] == "COMPLETE"
    assert result["steps"]["model_review"] == original["steps"]["model_review"]
    assert "operational_recoveries" not in result


def test_recovery_archive_is_reused_after_interrupted_state_save(prepared, monkeypatch):
    config, state, path, _ = prepared
    _change_code(monkeypatch)
    before = path.read_bytes()
    def interrupted_save():
        raise InterruptedError("before state replacement")
    with pytest.raises(InterruptedError):
        workflow._recover_stale_model_feedback(config, deepcopy(state), path, interrupted_save)
    assert path.read_bytes() == before
    refreshed = deepcopy(state)
    assert workflow._recover_stale_model_feedback(config, refreshed, path, lambda: workflow._write(path, refreshed))
    assert len(list((path.parent / "operational-recoveries").iterdir())) == 1
    assert len(refreshed["operational_recoveries"]) == 1


def test_stale_interrupted_review_refreshes_its_recorded_diagnostics(prepared, monkeypatch):
    config, state, path, directories = prepared
    review = directories[1]
    entry = state["steps"]["model_review"]
    entry.pop("output")
    entry.update(status="FAILED", feedback_diagnostics_sha256=file_checksum(review / "diagnostics.json"))
    state["current_step"] = "model_review"
    workflow._write(path, state)
    _change_code(monkeypatch)
    calls = []
    result = _resume(config, _executor(monkeypatch, calls))
    assert calls == ["model_review", "reviewer", "train_and_plan"]
    assert result["steps"]["model_review"]["feedback_run"] != str(review)
    assert (review / "reviewed-proposal.json").exists()
