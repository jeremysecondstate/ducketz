"""Both PCs share one coordinator while retaining original accepted evidence."""
import json
from pathlib import Path

import pytest
from filelock import FileLock

from ml import nightly_workflow as workflow
from ml.artifacts import file_checksum
from test_nightly_dispatch import config, run, identity
from test_nightly_workflow import _display_fixture, _write_native


@pytest.mark.parametrize("source_changed", [False, True])
def test_prepared_repair_fences_dispatch_and_direct_catchup(config, source_changed):
    result = run(config, "datastore")
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    result["repair_claim"] = {"owner": "repair-owner", "token": "saved-token", "spec": "frozen-repair.json"}
    if source_changed:
        result["source_identity"] = {"commit": "f" * 40, "source_sha256": "e" * 64}
    workflow._write(path, result)
    original = path.read_bytes()
    with FileLock(str(path.parents[2] / "workflow.lock")):
        decision = workflow.dispatch_status(config, now="2026-10-10T04:15Z")
        assert decision["status"] == "REPAIR_IN_PROGRESS" and not decision["dispatch"]
    actual = run(config, "stats", lambda *args: pytest.fail("repair owner must retain the stage"))
    assert actual["status"] == "REPAIR_IN_PROGRESS"
    assert path.read_bytes() == original


def test_reviewer_usage_exhaustion_keeps_one_dependency_failure_and_completed_stats(config):
    run(config, "datastore")
    run(config, "stats")
    def fail(config, state, step, save):
        assert step == "model_review"
        directory = Path(config["state_root"]) / "review"
        directory.mkdir()
        (directory / "codex-answer-fixture.jsonl").write_text('{"error":"usage limit reached; rate limit"}')
        state["steps"][step]["feedback_run"] = str(directory)
        raise RuntimeError("Model reviewer failed (exit 1); inspect its local log")
    with pytest.raises(RuntimeError):
        run(config, "model", fail)
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    original = path.read_bytes()
    state = json.loads(original)
    assert state["failure"]["kind"] == "EXTERNAL_DEPENDENCY"
    assert "restore the dependency" in state["failure"]["corrective_action"]
    assert state["failure"]["owner"] == "native-model"
    for observed in ("2026-10-10T04:20Z", "2026-10-10T05:00Z"):
        decision = workflow.dispatch_status(config, now=observed)
        assert decision["status"] == "DEPENDENCY_REQUIRED" and not decision["dispatch"]
        result = run(config, "model", lambda *a: pytest.fail("unchanged usage limit retry"), now=observed)
        assert result["status"] == "DEPENDENCY_REQUIRED"
    assert path.read_bytes() == original
    assert state["steps"]["prepare_stats"]["status"] == "COMPLETE"


def test_retry_cooldown_begins_at_actual_failure_after_long_stage(config, monkeypatch):
    tick = [100.0]
    monkeypatch.setattr(workflow, "monotonic", lambda: tick[0])
    def fail(*args):
        tick[0] += 3 * 60 * 60
        raise ConnectionError("transient failure after three hours of work")
    with pytest.raises(ConnectionError):
        run(config, "datastore", fail, now="2026-10-10T04:05Z")
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    state = json.loads(path.read_bytes())
    assert state["failed_at"] == state["failure"]["at"] == "2026-10-10T07:05:00+00:00"
    assert state["failure"]["retry_after"] == "2026-10-10T07:10:00+00:00"
    decision = workflow.dispatch_status(config, now="2026-10-10T07:06Z")
    assert not decision["dispatch"] and decision["status"] == "RETRY_BACKOFF"
    assert workflow.dispatch_status(config, now="2026-10-10T07:11Z")["dispatch"]


def _terminal_review(root, recovered):
    state, *_ = _display_fixture(root)
    saved = workflow._display({"datastore": str(root), "actor": "Atlas"}, state)
    state["steps"]["verify_display"] = {"output": saved}
    for name in workflow.STEPS:
        state["steps"].setdefault(name, {"output": {"files": {}}})["status"] = "COMPLETE"
    state.update(status="LOCAL_COMPLETE_PEER_SETUP_PENDING", source_identity=identity(None),
                 completed_at="2026-09-14T13:00:00Z")
    if recovered:
        state["recovery"] = {"path": "original-immutable-recovery.json", "sha256": "d" * 64}
        state["effective_deadline_at"] = "2026-09-14T14:00:00Z"
    review = state["steps"]["model_review"]["output"]
    diagnostic = Path(review["proposal"]).parent / "diagnostics.json"
    review["files"][str(diagnostic)] = file_checksum(diagnostic)
    return state, saved


@pytest.mark.parametrize("recovered", [False, True], ids=["ordinary-oct8-format", "recovered-oct9-format"])
def test_terminal_historical_review_uses_original_binding_after_policy_change(tmp_path, monkeypatch, recovered):
    from ml import gameplan_model_feedback as feedback
    state, saved = _terminal_review(tmp_path, recovered)
    originals = {path: Path(path).read_bytes() for entry in state["steps"].values()
                 for path in entry["output"].get("files", {})}
    original_state = json.dumps(state, sort_keys=True)
    monkeypatch.setattr(feedback, "_code_binding", lambda: {name: "f" * 64 for name in feedback._POLICY_FILES})
    assert workflow._verify_local_preparation({"datastore": str(tmp_path)}, state) == saved
    assert json.dumps(state, sort_keys=True) == original_state
    assert all(Path(path).read_bytes() == value for path, value in originals.items())
    with pytest.raises(ValueError, match="implementation changed"):
        feedback.load_feedback_review(tmp_path, Path(state["steps"]["model_review"]["output"]["proposal"]),
                                      require_latest_stats=False)
    state["status"] = "FAILED"
    with pytest.raises(ValueError, match="implementation changed"):
        workflow._verify_local_preparation({"datastore": str(tmp_path)}, state)


@pytest.mark.parametrize("damage", ["hash", "missing-stage", "source", "future-time", "policy-schema"])
def test_frozen_review_does_not_accept_incomplete_or_changed_evidence(tmp_path, damage):
    state, _ = _terminal_review(tmp_path, False)
    output = state["steps"]["model_review"]["output"]
    path = Path(output["proposal"]).parent / "diagnostics.json"
    if damage == "hash":
        output["files"][str(path)] = "f" * 64
    elif damage == "missing-stage":
        state["steps"]["local_handoff"]["status"] = "RUNNING"
    elif damage == "source":
        state["source_identity"] = {}
    elif damage == "future-time":
        state["completed_at"] = "2026-09-11T00:00:00Z"
    else:
        diagnostics = json.loads(path.read_text())
        diagnostics["training_code"] = {"fake": "f" * 64}
        path.write_text(json.dumps(diagnostics))
        output["files"][str(path)] = file_checksum(path)
        proposal_path = Path(output["proposal"])
        proposal = json.loads(proposal_path.read_text())
        proposal["diagnostics_sha256"] = file_checksum(path)
        proposal_path.write_text(json.dumps(proposal))
        output["files"][str(proposal_path)] = file_checksum(proposal_path)
    with pytest.raises(ValueError):
        workflow._completed_feedback(tmp_path, state)


@pytest.mark.parametrize("actor,research", [("Scout", True), ("Atlas", False)])
@pytest.mark.parametrize("step", ["train_and_plan", "local_gameplan"])
def test_split_native_dispatch_retains_research_role(config, tmp_path, monkeypatch, actor, research, step):
    from ml import overnight_runtime as native
    config["actor"] = actor
    training = tmp_path / "data/ml/overnight-runs/training"
    _write_native(training, pinned={"run_path": "ml/nightly-gameplan-runs/fixture",
        "receipt_sha256": "a" * 64, "action_date": "2026-10-12"})
    state = {"workflow_layout": "nightly-responsibilities-v1", "deadline_at": "2026-10-12T11:00Z",
        "source_session": "2026-10-09", "action_date": "2026-10-12",
        "steps": {"model_review": {"output": {"proposal": str(tmp_path / "review.json")}},
                  "train_and_plan": {"output": workflow._native_outputs(training)}, "local_gameplan": {}}}
    called = []
    def execute(root, **kwargs):
        called.append(kwargs)
        return training
    monkeypatch.setattr(native, "run_overnight_pipeline", execute)
    monkeypatch.setattr(native, "overnight_status", lambda _: {})
    workflow._run_native(config, state, step, lambda: None)
    assert called[0]["research_producer_only"] is research
    if step == "local_gameplan":
        assert called[0]["start_at"] == "gameplan_trade_planning"
        assert called[0]["pinned_gameplan"]["action_date"] == "2026-10-12"
    else:
        assert called[0]["stop_after"] == "stock_enrichment_training"
