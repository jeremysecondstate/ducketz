"""Offline regression coverage for a missed nightly launch and bounded recovery."""
import json
from pathlib import Path

import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml.nightly_recovery import make_recovery, session_context, verify_recovery, verify_saved_recovery
from ml.nightly_workflow import run_workflow, status, STEPS


NOW = pd.Timestamp("2026-10-09T14:30:00Z")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    repo = tmp_path / "repo"
    (repo / "datafetching").mkdir(parents=True)
    watchlist = repo / "datafetching/watchlist.local.txt"
    watchlist.write_text("AAPL\nMSFT\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"actor": "Scout", "checkout": str(repo),
        "symbol_profile_path": str(watchlist), "symbols": ["AAPL", "MSFT"]}))
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("ml.nightly_workflow.utc_timestamp", lambda value=None: NOW if value is None else pd.Timestamp(value))
    return {"actor": "Scout", "repository": str(repo), "datastore": str(root),
            "state_root": str(tmp_path / "state"), "local_profile": str(profile)}


def identity(_):
    return {"commit": "a" * 40, "source_sha256": "b" * 64}


def run(config, callback, **kwargs):
    return run_workflow(config, now=NOW, execute_step=callback, identity=identity,
                        supervise=False, catch_up=True, recovery_reason="Human requested missed-run recovery", **kwargs)


def test_new_session_is_not_satisfied_by_yesterdays_completion(configured):
    run_workflow(configured, now="2026-10-08T04:05Z", execute_step=lambda *args: {"files": {}},
                 identity=identity, supervise=False)
    old = Path(configured["state_root"]) / "runs/2026-10-08/state.json"
    old_bytes = old.read_bytes()
    assert status(configured, current_session=True)["status"] == "NOT_STARTED"
    calls = []
    def execute(config, state, step, save):
        calls.append(step)
        assert state["source_session"] == "2026-10-08"
        assert state["action_date"] == "2026-10-09"
        assert state["deadline_at"] == "2026-10-09T11:00:00+00:00"
        assert state["effective_deadline_at"] == "2026-10-09T21:30:00+00:00"
        assert state["recovery"]["authorization"]["requested_at"] == NOW.isoformat()
        return {"files": {}}
    result = run(configured, execute)
    assert calls == list(STEPS)
    assert result["status"] == "LOCAL_COMPLETE_PEER_SETUP_PENDING"
    assert old.read_bytes() == old_bytes
    run(configured, lambda *args: pytest.fail("Must not repeat the same completed cycle"))


def test_failure_resumes_completed_steps_without_renewing_deadline(configured):
    calls = []
    def execute(config, state, step, save):
        calls.append(step)
        if step == "model_review":
            raise RuntimeError("review unavailable")
        return {"files": {}}
    with pytest.raises(RuntimeError, match="review unavailable"):
        run(configured, execute)
    path = Path(configured["state_root"]) / "runs/2026-10-09/state.json"
    before = json.loads(path.read_text())
    resumed = run(configured, lambda c, s, step, save: {"files": {}})
    assert resumed["recovery"] == before["recovery"]
    assert resumed["effective_deadline_at"] == before["effective_deadline_at"]
    assert resumed["steps"]["prepare_stats"] == before["steps"]["prepare_stats"]


def test_retry_cannot_rewrite_expired_recovery(configured, monkeypatch):
    with pytest.raises(RuntimeError):
        run(configured, lambda *args: (_ for _ in ()).throw(RuntimeError("failed")))
    path = Path(configured["state_root"]) / "runs/2026-10-09/recovery.json"
    before = path.read_bytes()
    later = NOW + pd.Timedelta(hours=8)
    monkeypatch.setattr("ml.nightly_workflow.utc_timestamp", lambda value=None: later if value is None else pd.Timestamp(value))
    with pytest.raises(ValueError, match="expired"):
        run_workflow(configured, now=later, catch_up=True, recovery_reason="retry",
                     identity=identity, supervise=False, execute_step=lambda *args: pytest.fail("Expired run launched"))
    assert path.read_bytes() == before


@pytest.mark.parametrize("observed,source,action", [
    ("2026-10-09T04:05Z", "2026-10-08", "2026-10-09"),
    ("2026-10-09T09:00Z", "2026-10-08", "2026-10-09"),
    ("2026-10-09T14:30Z", "2026-10-08", "2026-10-09"),
    ("2026-10-10T04:05Z", "2026-10-09", "2026-10-12"),
    ("2026-10-11T14:30Z", "2026-10-09", "2026-10-12"),
])
def test_latest_completed_session_across_midnight_and_weekend(observed, source, action):
    context = session_context(observed)
    assert (context["source_session"], context["action_date"]) == (source, action)


def test_normal_evening_catchup_does_not_extend_deadline(configured):
    state = run_workflow(configured, now="2026-10-09T04:05Z", catch_up=True,
                         execute_step=lambda *args: {"files": {}}, identity=identity, supervise=False)
    assert "recovery" not in state
    assert state["deadline_at"] == "2026-10-09T11:00:00+00:00"


def test_evidence_is_bound_to_store_session_and_exact_bytes(configured):
    path = Path(configured["state_root"]) / "test-recovery.json"
    path.parent.mkdir(parents=True)
    payload = make_recovery(configured, NOW, reason="human instruction")
    path.write_text(json.dumps(payload))
    evidence = verify_recovery(configured["datastore"], path, NOW, action_date="2026-10-09")
    assert pd.Timestamp(payload["training_information_cutoff"]) < pd.Timestamp("2026-10-09T11:00Z")
    with pytest.raises(ValueError):
        verify_recovery(configured["datastore"], path, NOW, action_date="2026-10-12")
    with pytest.raises(ValueError):
        verify_recovery(Path(configured["datastore"]) / "other", path, NOW)
    payload["authorization_reason"] = "changed"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="changed"):
        verify_saved_recovery(configured["datastore"], evidence, NOW)


def test_native_stages_receive_original_deadline_and_recovery(configured, monkeypatch):
    from ml.nightly_workflow import _run_native
    from ml.overnight_runtime import run_overnight_pipeline
    import ml.overnight_runtime as runtime
    path = Path(configured["datastore"]) / "recovery.json"
    path.write_text(json.dumps(make_recovery(configured, NOW, reason="human instruction")))
    monkeypatch.setattr(runtime, "utc_timestamp", lambda value=None: NOW if value is None else pd.Timestamp(value))
    commands = []
    def stage(command, **kwargs):
        commands.append(command)
        assert kwargs["deadline"] == NOW + pd.Timedelta(hours=7)
        kwargs["log_path"].write_text("offline stage")
        return 0
    monkeypatch.setattr(runtime, "_run_stage", stage)
    result = run_overnight_pipeline(Path(configured["datastore"]),
        datastore_argument=("--datastore", configured["datastore"]), repository_root=Path(configured["repository"]),
        start_at="gameplan_stats", stop_after="gameplan_stats", stats_first=True,
        stock_only=True, independent_stock_horizons=True, review_action_date="2026-10-08",
        deadline="2026-10-09T11:00Z", recovery_spec=path, reporter=None)
    report = json.loads((result / "stage-report.json").read_text())
    assert report["deadline_at"] == "2026-10-09T11:00:00+00:00"
    assert report["recovery"]["sha256"] == file_checksum(path)
    assert commands[0][commands[0].index("--action-date") + 1] == "2026-10-08"
    assert commands[0][commands[0].index("--deadline") + 1] == "2026-10-09T21:30:00+00:00"


def test_late_gameplan_uses_preopen_information_with_real_creation_time(configured, monkeypatch):
    import ml.nightly_gameplan as gameplan
    from types import SimpleNamespace
    root = Path(configured["datastore"])
    spec = root / "recovery.json"
    spec.write_text(json.dumps(make_recovery(configured, NOW, reason="human instruction")))
    loop = root / "loop"
    loop.mkdir()
    samples = pd.DataFrame({"symbol": ["AAPL", "MSFT"], "feature": [1., 2.]})
    samples.to_parquet(loop / "samples.parquet")
    samples.to_parquet(loop / "predictions.parquet")
    monkeypatch.setattr(gameplan, "read_current_publication", lambda _: SimpleNamespace(run_directory=loop, manifest={}))
    monkeypatch.setattr(gameplan, "_configured_symbols", lambda *args: ("AAPL", "MSFT"))
    monkeypatch.setattr(gameplan, "_feature_columns", lambda *args: ("feature",))
    monkeypatch.setattr("ml.gameplan_model_feedback.load_feedback_review", lambda *args, **kwargs: {})
    cutoff = pd.Timestamp("2026-10-09T10:59:59.999999Z")
    def sources(*args, **kwargs):
        assert kwargs["available_at"] == cutoff
        return pd.DataFrame({"symbol": ["AAPL", "MSFT"],
            "action_date": [pd.Timestamp("2026-10-09").date()] * 2,
            "source_action_start": [pd.Timestamp("2026-10-09T11:00Z")] * 2,
            "source_feature_cutoff": [pd.Timestamp("2026-10-09T00:00Z")] * 2})
    monkeypatch.setattr(gameplan, "select_prior_session_sources", sources)
    monkeypatch.setattr(gameplan, "source_selection_contract", lambda _: "fixture")
    monkeypatch.setattr(gameplan, "_verify_opra_history", lambda *args, **kwargs: ((), {}))
    monkeypatch.setattr(gameplan, "_load_equity_minute_bars", lambda *args, **kwargs: (pd.DataFrame(), ()))
    def groups(*args, **kwargs):
        assert kwargs["available_at"] == cutoff
        raise RuntimeError("Reached chronological fitting boundary with preopen information")
    monkeypatch.setattr(gameplan, "build_stock_training_groups", groups)
    with pytest.raises(RuntimeError, match="Reached chronological"):
        gameplan.run_nightly_gameplan_once(root, run_timestamp=NOW, stock_only=True,
            independent_stock_horizons=True, model_feedback=root / "proposal.json", recovery_spec=spec)


def test_native_training_and_tail_forward_same_recovery(configured, monkeypatch):
    import ml.overnight_runtime as runtime
    root = Path(configured["datastore"])
    spec = root / "recovery.json"
    spec.write_text(json.dumps(make_recovery(configured, NOW, reason="human instruction")))
    proposal = root / "proposal.json"
    proposal.write_text("{}")
    monkeypatch.setattr(runtime, "utc_timestamp", lambda value=None: NOW if value is None else pd.Timestamp(value))
    monkeypatch.setattr(runtime, "_pin_stock_gameplan", lambda *args, **kwargs:
        {"run_path": "ml/nightly-gameplan-runs/fixture", "receipt_sha256": "a" * 64, "action_date": "2026-10-09"})
    commands = []
    def stage(command, **kwargs):
        commands.append(command)
        assert kwargs["deadline"] == NOW + pd.Timedelta(hours=7)
        kwargs["log_path"].write_text("offline")
        return 0
    monkeypatch.setattr(runtime, "_run_stage", stage)
    runtime.run_overnight_pipeline(root, datastore_argument=("--datastore", str(root)),
        repository_root=Path(configured["repository"]), start_at="loop_b_directional_generation",
        stock_only=True, independent_stock_horizons=True, stats_first=True,
        review_action_date="2026-10-08", deadline="2026-10-09T11:00Z", model_feedback=proposal,
        recovery_spec=spec, reporter=None)
    publication = next(cmd for cmd in commands if "ml.nightly_gameplan" in cmd)
    assert publication[publication.index("--recovery-spec") + 1] == str(spec)
    planning = next(cmd for cmd in commands if "ml.gameplan_trade_planning" in cmd)
    assert planning[planning.index("--deadline") + 1] == "2026-10-09T11:00:00+00:00"
    assert planning[planning.index("--deadline-exception") + 1] == str(spec)


def test_launcher_forwards_catchup_and_status_ignores_old_latest(configured, monkeypatch):
    import ml.nightly_workflow as workflow
    from types import SimpleNamespace
    monkeypatch.setattr(workflow, "load_config", lambda _: configured)
    monkeypatch.setattr(workflow, "verify_installation", lambda _: None)
    commands = []
    monkeypatch.setattr(workflow.subprocess, "Popen", lambda command, **kwargs: (commands.append(command) or SimpleNamespace(pid=123)))
    assert workflow.main(["--config", "unused", "--launch", "--catch-up", "--recovery-reason", "human instruction"]) == 0
    assert commands[0][-3:] == ["--catch-up", "--recovery-reason", "human instruction"]
    assert status(configured, current_session=True)["status"] == "NOT_STARTED"
