"""Offline ownership, missed-wake, frozen-expiry and interrupted-save checks."""
import json
from pathlib import Path

from filelock import FileLock
import pandas as pd
import pytest

from ml import nightly_workflow as workflow
from ml import nightly_dispatch as dispatch
from ml.artifacts import file_checksum, utc_timestamp


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    repository = tmp_path / "repo"
    watchlist = repository / "datafetching/watchlist.local.txt"
    watchlist.parent.mkdir(parents=True)
    watchlist.write_text("AAPL\nMSFT\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"actor": "Atlas", "checkout": str(repository),
        "symbol_profile_path": str(watchlist), "symbols": ["AAPL", "MSFT"]}))
    root = tmp_path / "data"
    root.mkdir()
    return {"schema_version": workflow.VERSION, "actor": "Atlas", "repository": str(repository),
        "datastore": str(root), "state_root": str(tmp_path / "state"), "local_profile": str(profile),
        "automatic_recovery": {"enabled": True, "authorization": "Local human nightly operating authority", "max_attempts": 3},
        "responsibility_owners": {role: "native-" + role for role in dispatch.RESPONSIBILITIES}}


def identity(_):
    return {"commit": "a" * 40, "source_sha256": "b" * 64}


def run(config, role, callback=lambda *a: {"files": {}}, now="2026-10-10T04:05Z", **kwargs):
    return workflow.run_workflow(config, responsibility=role, catch_up=True, now=now,
        execute_step=callback, identity=identity, supervise=False, **kwargs)


@pytest.mark.parametrize("stamp, source, action, eligible", [
    ("2026-10-10T04:04Z", "2026-10-09", "2026-10-12", False),
    ("2026-10-10T04:05Z", "2026-10-09", "2026-10-12", True),
    ("2026-10-10T08:00Z", "2026-10-09", "2026-10-12", True),
    ("2026-12-25T05:05Z", "2026-12-24", "2026-12-28", True),
    ("2026-11-03T05:05Z", "2026-11-02", "2026-11-03", True),
])
def test_calendar_and_original_kickoff(stamp, source, action, eligible):
    selected = dispatch.intended_session(stamp)
    assert (selected["source_session"], selected["action_date"], selected["eligible"]) == (source, action, eligible)
    assert utc_timestamp(selected["deadline_at"]).tz_convert("America/Los_Angeles").hour == 4


def test_each_responsibility_runs_only_its_stages_and_reuses_prerequisites(config):
    calls = []
    callback = lambda config, state, step, save: calls.append(step) or {"files": {}}
    assert run(config, "model", callback)["status"] == "WAITING_PREREQUISITE"
    assert not calls
    for role in dispatch.RESPONSIBILITIES:
        result = run(config, role, callback)
        expected = [step for step in dispatch.STEPS if dispatch.OWNERS[step] in dispatch.RESPONSIBILITIES[:dispatch.RESPONSIBILITIES.index(role)+1]]
        assert calls == expected
        assert all(entry["owner"] == "native-" + entry["responsibility"] for entry in result["steps"].values() if entry.get("status") == "COMPLETE")
    assert result["status"] == "LOCAL_COMPLETE_PEER_SETUP_PENDING"
    before = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    original = before.read_bytes()
    run(config, "datastore", lambda *a: pytest.fail("duplicate completion"))
    assert before.read_bytes() == original


def test_terminal_history_survives_new_source_without_rewrite(config):
    for role in dispatch.RESPONSIBILITIES:
        run(config, role)
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    original = path.read_bytes()
    result = workflow.run_workflow(config, responsibility="datastore", catch_up=True,
        now="2026-10-12T18:00Z", identity=lambda _: {"changed": True}, supervise=False,
        execute_step=lambda *a: pytest.fail("completed session replay"))
    assert result["status"] == "LOCAL_COMPLETE_PEER_SETUP_PENDING"
    assert path.read_bytes() == original


def test_interrupted_terminal_save_finishes_without_replaying_stages(config):
    for role in dispatch.RESPONSIBILITIES:
        result = run(config, role)
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    result["status"] = "RUNNING"
    workflow._write(path, result)
    decision = workflow.dispatch_status(config, now="2026-10-10T04:30Z")
    assert decision["dispatch"] and decision["responsibility"] == "display"
    resumed = run(config, "display", lambda *a: pytest.fail("No stage replay"))
    assert resumed["status"] == "LOCAL_COMPLETE_PEER_SETUP_PENDING"


def test_after_session_close_waits_for_new_kickoff_without_creating_expired_run(config):
    result = run(config, "datastore", now="2026-10-13T00:30Z")
    assert result["status"] == "WAITING_KICKOFF" and result["action_date"] == "2026-10-13"
    assert not (Path(config["state_root"]) / "runs").exists()


def test_missed_launch_after_four_freezes_original_and_recovery_cutoffs(config):
    result = run(config, "datastore", now="2026-10-12T12:15Z")
    assert result["action_date"] == "2026-10-12"
    assert result["deadline_at"] == "2026-10-12T11:00:00+00:00"
    assert result["recovery_deadline_at"] == "2026-10-12T19:15:00+00:00"
    recovery = Path(result["scheduled_recovery"]["path"])
    original = recovery.read_bytes()
    next_result = run(config, "stats", now="2026-10-12T12:20Z")
    assert next_result["run_id"] == result["run_id"]
    assert next_result["recovery_deadline_at"] == result["recovery_deadline_at"]
    assert recovery.read_bytes() == original
    decision = workflow.dispatch_status(config, now="2026-10-12T19:16Z")
    assert decision["status"] == "RECOVERY_EXPIRED" and not decision["dispatch"]


def test_interrupted_outer_save_keeps_completed_stages(config):
    result = run(config, "datastore")
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    result.update(status="RUNNING", current_step="prepare_stats", owner_pid=999999999)
    result["steps"]["prepare_stats"] = {"status": "RUNNING"}
    workflow._write(path, result)
    calls = []
    resumed = run(config, "stats", lambda config, state, step, save: calls.append(step) or {"files": {}}, now="2026-10-10T08:00Z")
    assert calls == ["prepare_stats"]
    assert resumed["run_id"] == result["run_id"]
    assert resumed["steps"]["datastore_catchup"] == result["steps"]["datastore_catchup"]


def test_dispatch_leaves_healthy_lock_owner_running(config):
    run(config, "datastore")
    with FileLock(str(Path(config["state_root"]) / "workflow.lock")):
        assert workflow.dispatch_status(config, now="2026-10-10T04:10Z")["status"] == "RUNNING"


def test_deterministic_failure_has_one_owner_and_no_repeated_launch(config):
    def fail(*args):
        raise ValueError("broken source fixture")
    with pytest.raises(ValueError):
        run(config, "datastore", fail)
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    original = path.read_bytes()
    result = run(config, "datastore", lambda *a: pytest.fail("unchanged deterministic retry"))
    assert result["status"] == "REPAIR_REQUIRED"
    assert result["failure"]["owner"] == "native-datastore"
    assert path.read_bytes() == original


def test_transient_retry_backoff_limit_and_verified_disposition(config):
    def fail(*args):
        raise ConnectionError("connection reset fixture")
    with pytest.raises(ConnectionError):
        run(config, "datastore", fail)
    assert workflow.dispatch_status(config, now="2026-10-10T04:06Z")["status"] == "RETRY_BACKOFF"
    recovered = run(config, "datastore", now="2026-10-10T04:11Z")
    assert recovered["failure"]["disposition"] == "RESOLVED"
    assert recovered["steps"]["datastore_catchup"]["attempts"] == 2
    assert len(recovered["failure_history"]) == 1


def test_transient_failure_stops_at_three_attempts(config):
    def fail(*args):
        raise ConnectionError("connection reset fixture")
    for minute in (5, 11, 17):
        with pytest.raises(ConnectionError):
            run(config, "datastore", fail, now=f"2026-10-10T04:{minute:02}Z")
    assert workflow.dispatch_status(config, now="2026-10-10T04:23Z")["status"] == "RETRY_LIMIT_REACHED"


def test_audited_repair_epoch_allows_three_new_attempts_without_erasing_history(config):
    def fail(*args):
        raise ConnectionError("connection reset fixture")
    for minute in (5, 11, 17):
        with pytest.raises(ConnectionError):
            run(config, "datastore", fail, now=f"2026-10-10T04:{minute:02}Z")
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    state = workflow._json(path)
    old_history = json.loads(json.dumps(state["failure_history"]))
    original_deadline = state["deadline_at"]
    entry = state["steps"]["datastore_catchup"]
    # Fixture represents the separately audited repair helper's accepted result.
    entry.update(retry_epoch_attempt_start=entry["attempts"], retry_epoch_id="c" * 64)
    state["failure"]["disposition"] = "RESOLVED"
    workflow._write(path, state)
    for minute in (23, 29, 35):
        with pytest.raises(ConnectionError):
            run(config, "datastore", fail, now=f"2026-10-10T04:{minute:02}Z")
    assert workflow.dispatch_status(config, now="2026-10-10T04:41Z")["status"] == "RETRY_LIMIT_REACHED"
    final = workflow._json(path)
    assert final["steps"]["datastore_catchup"]["attempts"] == 6
    assert final["steps"]["datastore_catchup"]["retry_epoch_attempt_start"] == 3
    assert final["failure_history"][:3] == old_history
    assert final["deadline_at"] == original_deadline


@pytest.mark.parametrize("attempts,start,epoch", [(3, -1, "a" * 64), (3, 4, "a" * 64),
    (3, True, "a" * 64), (3, 1, None), (3, 1, "not-a-reviewed-hash"), (True, 0, None)])
def test_invalid_retry_epoch_cannot_expand_budget(config, attempts, start, epoch):
    state = {"failure": {"step": "datastore_catchup", "kind": "TRANSIENT", "disposition": "OPEN"},
             "steps": {"datastore_catchup": {"attempts": attempts, "retry_epoch_attempt_start": start,
                                            "retry_epoch_id": epoch}}}
    with pytest.raises(ValueError, match="Invalid audited retry epoch"):
        dispatch.retry_disposition(config, state)


def test_configuration_requires_explicit_local_recovery_authority(config):
    config.pop("automatic_recovery")
    with pytest.raises(ValueError, match="human authority"):
        workflow.dispatch_status(config)


def test_recovery_evidence_mutation_cannot_extend_frozen_expiry(config):
    state = run(config, "datastore", now="2026-10-12T12:15Z")
    path = Path(state["scheduled_recovery"]["path"])
    record = json.loads(path.read_text())
    record["expires_at"] = "2026-10-12T20:00Z"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="evidence changed"):
        run(config, "stats", now="2026-10-12T12:20Z")


@pytest.mark.parametrize("minute, expected", [(10, "native-stats"), (15, "native-model")])
def test_dispatch_returns_next_real_prerequisite_owner(config, minute, expected):
    run(config, "datastore")
    if minute == 15:
        run(config, "stats")
    result = workflow.dispatch_status(config, now=f"2026-10-10T04:{minute}Z")
    assert result["dispatch"] is True and result["owner"] == expected


@pytest.mark.parametrize("step,start,stop", [
    ("datastore_catchup", None, "stock_target_history"),
    ("prepare_stats", "gameplan_stats", "gameplan_stats"),
    ("train_and_plan", "loop_b_directional_generation", "stock_enrichment_training"),
])
def test_native_responsibility_boundaries(config, monkeypatch, step, start, stop):
    captured = {}
    fake = Path(config["datastore"]) / "native"
    fake.mkdir()
    monkeypatch.setattr("ml.overnight_runtime.run_overnight_pipeline", lambda root, **kw: captured.update(kw) or fake)
    monkeypatch.setattr("ml.overnight_runtime.overnight_status", lambda root: {})
    monkeypatch.setattr(workflow, "_native_outputs", lambda run: {"files": {}})
    state = {"workflow_layout": dispatch.LAYOUT, "deadline_at": "2026-10-12T11:00Z", "source_session": "2026-10-09",
        "steps": {step: {}, "model_review": {"output": {"proposal": str(fake / "proposal.json")}}}}
    workflow._run_native(config, state, step, lambda: None)
    assert captured.get("start_at") == start
    assert captured["stop_after"] == stop


@pytest.mark.parametrize("failure_type", [RuntimeError, TimeoutError])
def test_native_expired_original_resume_preserves_receipts_and_completed_work(tmp_path, monkeypatch, failure_type):
    from ml import overnight_runtime as native
    clock = [pd.Timestamp("2026-10-12T10:30Z")]
    monkeypatch.setattr(native, "utc_timestamp", lambda value=None: utc_timestamp(value) if value is not None else clock[0])
    monkeypatch.setattr(dispatch, "utc_timestamp", lambda value=None: utc_timestamp(value) if value is not None else clock[0])
    calls = []
    def first(command, **kwargs):
        calls.append(command[3])
        kwargs["log_path"].write_text("original failed evidence")
        if command[3] == "ml.stock_target_history":
            raise failure_type("interrupted fixture")
        return 0
    monkeypatch.setattr(native, "_run_stage", first)
    arguments = dict(datastore_argument=("--datastore", str(tmp_path)), repository_root=tmp_path,
        stock_only=True, independent_stock_horizons=True, stats_first=True, archive_history=True,
        stock_price_source="xnas-itch-archive-v1", stop_after="gameplan_stats", review_action_date="2026-10-09",
        deadline="2026-10-12T11:00Z", reporter=None)
    with pytest.raises(failure_type):
        native.run_overnight_pipeline(tmp_path, **arguments)
    failed = Path(native.overnight_status(tmp_path)["run_path"])
    original = {p.name: p.read_bytes() for p in failed.iterdir() if p.is_file()}
    clock[0] = pd.Timestamp("2026-10-12T12:00Z")
    record = {"schema_version": dispatch.RECOVERY, "actor": "Atlas", "workflow_run_id": "fixed",
        "datastore": str(tmp_path.resolve()), "action_date": "2026-10-12", "source_session": "2026-10-09",
        "original_deadline_at": "2026-10-12T11:00Z", "approved_at": "2026-10-12T12:00Z",
        "expires_at": "2026-10-12T19:00Z", "authorization": "Local human authority", "orders_authorized": False,
        "native_attempts": {str(failed): file_checksum(failed / "receipt.json")}}
    evidence = tmp_path / "recovery.json"
    evidence.write_text(json.dumps(record))
    resumed_calls = []
    def resume(command, **kw):
        resumed_calls.append(command)
        assert kw["deadline"] == pd.Timestamp(record["expires_at"])
        return 0
    monkeypatch.setattr(native, "_run_stage", resume)
    resumed = native.run_overnight_pipeline(tmp_path, **arguments, resume_run=failed, workflow_recovery=evidence)
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["deadline_at"] == "2026-10-12T11:00:00+00:00"
    assert report["effective_deadline_at"] == "2026-10-12T19:00:00+00:00"
    assert report["completed_stages_from_previous_attempt"] == ["loop_a_close_fetch"]
    assert [c[3] for c in resumed_calls] == ["ml.stock_target_history", "ml.gameplan_actuals_review"]
    assert resumed_calls[-1][-1] == "2026-10-12T19:00:00+00:00"
    assert {p.name: p.read_bytes() for p in failed.iterdir() if p.is_file()} == original


@pytest.mark.parametrize("late", [True, False])
def test_recovery_planning_uses_exact_prediction_pin_and_correct_publication_route(tmp_path, monkeypatch, late):
    from ml import overnight_runtime as native
    now = pd.Timestamp("2026-10-12T12:15Z")
    monkeypatch.setattr(native, "utc_timestamp", lambda value=None: utc_timestamp(value) if value is not None else now)
    monkeypatch.setattr(dispatch, "utc_timestamp", lambda value=None: utc_timestamp(value) if value is not None else now)
    source = tmp_path / "ml/nightly-gameplan-runs/immutable"
    source.mkdir(parents=True)
    (source / "receipt.json").write_text(json.dumps({"action_date": "2026-10-12"}))
    (source / "manifest.json").write_text(json.dumps({"configuration": {"publication_mode": "LATE_RECOVERY" if late else "NORMAL"}}))
    pinned = {"run_path": source.relative_to(tmp_path).as_posix(), "receipt_sha256": file_checksum(source / "receipt.json"), "action_date": "2026-10-12"}
    def pin(*args, **kwargs):
        assert kwargs["pinned"] == pinned
        return pinned
    monkeypatch.setattr(native, "_pin_stock_gameplan", pin)
    record = {"schema_version": dispatch.RECOVERY, "actor": "Atlas", "workflow_run_id": "fixed",
        "datastore": str(tmp_path.resolve()), "action_date": "2026-10-12", "source_session": "2026-10-09",
        "original_deadline_at": "2026-10-12T11:00Z", "approved_at": "2026-10-12T12:00Z",
        "expires_at": "2026-10-12T19:00Z", "authorization": "Local human authority", "orders_authorized": False,
        "native_attempts": {}}
    evidence = tmp_path / "recovery.json"
    evidence.write_text(json.dumps(record))
    calls = []
    monkeypatch.setattr(native, "_run_stage", lambda command, **kwargs: calls.append(command) or 0)
    result = native.run_overnight_pipeline(tmp_path, datastore_argument=("--datastore", str(tmp_path)),
        repository_root=tmp_path, start_at="gameplan_trade_planning", stock_only=True,
        independent_stock_horizons=True, stats_first=True, review_action_date="2026-10-09",
        deadline="2026-10-12T11:00Z", workflow_recovery=evidence, pinned_gameplan=pinned, reporter=None)
    assert len(calls) == 1 and calls[0][3] == "ml.gameplan_trade_planning"
    command = calls[0]
    assert command[command.index("--gameplan-run") + 1] == str(source)
    if late:
        assert command[command.index("--deadline") + 1] == "2026-10-12T19:00:00+00:00"
        assert command[command.index("--late-action-date") + 1] == "2026-10-12"
        assert "--deadline-exception" not in command
    else:
        from ml.preparation_deadline import preparation_deadline
        assert "--late-action-date" not in command
        assert command[command.index("--deadline") + 1] == "2026-10-12T11:00:00+00:00"
        exception = Path(command[command.index("--deadline-exception") + 1])
        # The planner verifies this exception against exact immutable bytes.
        payload = json.loads(exception.read_text())
        assert payload["gameplan_receipt_sha256"] == pinned["receipt_sha256"]
        assert payload["orders_authorized"] is False
        assert payload["authorization_source"] == str(evidence)
        effective, verified = preparation_deadline(tmp_path, source, record["original_deadline_at"], now, exception)
        assert effective == pd.Timestamp(record["expires_at"])
        assert verified["sha256"] == file_checksum(exception)
    report = json.loads((result / "stage-report.json").read_text())
    assert report["deadline_at"] == "2026-10-12T11:00:00+00:00"
    assert report["effective_deadline_at"] == "2026-10-12T19:00:00+00:00"
