"""Native recovery keeps its frozen session and child evidence across retries."""

import json
from pathlib import Path

import pandas as pd
import pytest

from ml import overnight_runtime as native
from ml.artifacts import file_checksum, utc_timestamp
from ml.nightly_recovery import make_recovery, verify_recovery
from test_gameplan_trade_planning import publication_case
from test_nightly_stats_segments import _feedback
from test_overnight_supervision import _evidence_snapshot, run


ACTION = "2026-09-10"
REVIEW = "2026-09-09"
ORIGINAL = "2026-09-10T11:00:00+00:00"
REQUESTED = "2026-09-10T12:00:00+00:00"


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    clock = {"now": pd.Timestamp(REQUESTED)}
    monkeypatch.setattr(native, "utc_timestamp", lambda value=None:
                        utc_timestamp(value) if value is not None else clock["now"])
    spec = tmp_path / "recovery.json"
    spec.write_text(json.dumps(make_recovery(
        {"actor": "Scout", "datastore": str(tmp_path)}, REQUESTED)), encoding="utf-8")
    source = tmp_path / "ml/nightly-gameplan-runs/synthetic"
    source.mkdir(parents=True)
    (source / "receipt.json").write_text(json.dumps({"action_date": ACTION}), encoding="utf-8")
    pin = {"run_path": source.relative_to(tmp_path).as_posix(),
           "receipt_sha256": file_checksum(source / "receipt.json"), "action_date": ACTION}
    monkeypatch.setattr(native, "_pin_stock_gameplan", lambda *args, **kwargs: pin)
    monkeypatch.setattr(native, "_run_stage", lambda *args, **kwargs:
                        pytest.fail("Only synthetic stages are permitted"))
    return tmp_path, spec, clock


def _stages(monkeypatch, *, fail=None):
    calls = []

    def stage(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["log_path"].write_text("synthetic native recovery evidence", encoding="utf-8")
        return 7 if command[3] == fail else 0

    monkeypatch.setattr(native, "_run_stage", stage)
    return calls


def _prepare(prepared, **kwargs):
    root, spec, _ = prepared
    arguments = dict(stock_only=True, independent_stock_horizons=True, stats_first=True,
                     deadline=ORIGINAL, review_action_date=REVIEW, recovery_spec=spec)
    arguments.update(kwargs)
    return run(root, **arguments)


def _report(path):
    return json.loads((path / "stage-report.json").read_text(encoding="utf-8"))


def _option(command, name):
    return command[command.index(name) + 1]


@pytest.mark.parametrize("default_review", [False, True])
@pytest.mark.parametrize("default_deadline", [False, True])
@pytest.mark.parametrize("later", [False, True])
def test_stats_recovery_keeps_original_session_and_frozen_target(prepared, monkeypatch, default_review, default_deadline, later):
    root, spec, clock = prepared
    if later:
        clock["now"] = pd.Timestamp("2026-09-12T12:00:00Z")
    calls = _stages(monkeypatch)
    directory = _prepare(prepared, start_at="gameplan_stats", stop_after="gameplan_stats",
                         **({"deadline": None} if default_deadline else {}),
                         **({"review_action_date": None} if default_review else {}))
    report = _report(directory)
    evidence = verify_recovery(root, spec, REQUESTED)
    assert report["recovery"] == evidence
    assert report["review_action_date"] == REVIEW
    assert report["deadline_at"] == ORIGINAL
    assert report["effective_deadline_at"] == evidence["authorization"]["expires_at"]
    assert report["continuation_action_date"] == ACTION
    assert _option(calls[0][0], "--action-date") == REVIEW
    assert _option(calls[0][0], "--deadline") == report["effective_deadline_at"]
    assert calls[0][1]["deadline"].isoformat() == report["effective_deadline_at"]
    assert calls[0][1]["continuation_action_date"] == ACTION


@pytest.mark.parametrize("continuation", [None, ACTION])
def test_training_and_planning_use_one_recovery_route(prepared, monkeypatch, continuation):
    root, spec, _ = prepared
    calls = _stages(monkeypatch)
    directory = _prepare(prepared, start_at="loop_b_directional_generation",
                         model_feedback=_feedback(root), continuation_action_date=continuation)
    report = _report(directory)
    publication = next(command for command, _ in calls if command[3] == "ml.nightly_gameplan")
    planning = next(command for command, _ in calls if command[3] == "ml.gameplan_trade_planning")
    assert _option(publication, "--recovery-spec") == str(spec)
    assert "--late-action-date" not in publication
    assert _option(planning, "--deadline") == ORIGINAL
    assert _option(planning, "--deadline-exception") == str(spec)
    assert report["recovery"] == verify_recovery(root, spec, REQUESTED)
    assert _option(planning, "--late-action-date") == ACTION
    assert all(kwargs["continuation_action_date"] == ACTION for _, kwargs in calls)
    assert report["orders_placed"] == 0 and report["broker_orders_enabled"] is False


@pytest.mark.parametrize("failure", ["ml.nightly_gameplan", "ml.gameplan_trade_planning"])
@pytest.mark.parametrize("continuation", [None, ACTION])
def test_retry_inherits_frozen_recovery_without_resupplying_path(prepared, monkeypatch, failure, continuation):
    root, spec, clock = prepared
    _stages(monkeypatch, fail=failure)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _prepare(prepared, start_at="loop_b_directional_generation", model_feedback=_feedback(root),
                 continuation_action_date=continuation)
    failed = Path(native.overnight_status(root)["run_path"])
    saved = _report(failed)
    before = _evidence_snapshot(failed)
    spec_bytes = spec.read_bytes()
    clock["now"] = pd.Timestamp("2026-09-12T12:00:00Z")
    calls = _stages(monkeypatch)
    resumed = run(root, resume_run=failed)
    report = _report(resumed)
    for key in ("deadline_at", "effective_deadline_at", "recovery", "review_action_date",
                "model_feedback", "continuation_action_date", "late_action_date", "stats_first"):
        assert report[key] == saved[key]
    completed = [row["stage"] for row in saved["stages"] if row["status"] == "COMPLETE"]
    assert report["completed_stages_from_previous_attempt"] == completed
    assert not set(completed).intersection(report["stage_order"])
    publication = [command for command, _ in calls if command[3] == "ml.nightly_gameplan"]
    if publication:
        assert _option(publication[0], "--recovery-spec") == str(spec)
        assert "--late-action-date" not in publication[0]
    planning = next(command for command, _ in calls if command[3] == "ml.gameplan_trade_planning")
    assert _option(planning, "--deadline-exception") == str(spec)
    assert _option(planning, "--deadline") == ORIGINAL
    assert spec.read_bytes() == spec_bytes
    assert _evidence_snapshot(failed) == before


@pytest.mark.parametrize("damage", ["bytes", "path", "review", "deadline", "store", "scope", "action", "saved-date"])
def test_invalid_recovery_rejects_before_new_attempt_or_child(prepared, monkeypatch, damage):
    root, spec, _ = prepared
    _stages(monkeypatch, fail="ml.gameplan_actuals_review")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _prepare(prepared, start_at="gameplan_stats", stop_after="gameplan_stats")
    failed = Path(native.overnight_status(root)["run_path"])
    if damage == "saved-date":
        report = _report(failed)
        report.pop("continuation_action_date")
        report["late_action_date"] = "2026-09-11"
        (failed / "stage-report.json").write_text(json.dumps(report))
        receipt = json.loads((failed / "receipt.json").read_text())
        receipt["stage_report_checksum_sha256"] = file_checksum(failed / "stage-report.json")
        (failed / "receipt.json").write_text(json.dumps(receipt))
    prior = _evidence_snapshot(failed)
    attempts = set(failed.parent.iterdir())
    options = {"resume_run": failed}
    if damage == "bytes":
        payload = json.loads(spec.read_text())
        payload["authorization_reason"] = "changed after original attempt"
        spec.write_text(json.dumps(payload))
    elif damage == "path":
        replacement = root / "other.json"
        replacement.write_bytes(spec.read_bytes())
        options["recovery_spec"] = replacement
    elif damage == "review":
        options["review_action_date"] = "2026-09-08"
    elif damage == "scope":
        options["stats_first"] = False
    elif damage == "action":
        options["continuation_action_date"] = "2026-09-11"
    elif damage in ("store", "deadline"):
        payload = json.loads(spec.read_text())
        payload["datastore" if damage == "store" else "original_deadline_at"] = (
            str(root / "other") if damage == "store" else "2026-09-10T12:00:00Z")
        spec.write_text(json.dumps(payload))
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError):
        run(root, **options)
    assert not calls
    assert set(failed.parent.iterdir()) == attempts
    assert _evidence_snapshot(failed) == prior


@pytest.mark.parametrize("options", [
    {"review_action_date": "2026-09-08"},
    {"deadline": "2026-09-11T11:00:00Z"},
    {"stats_first": False, "review_action_date": None},
    {"deadline_exception": Path("unused-exception.json")},
])
def test_new_recovery_rejects_wrong_session_scope_or_competing_route(prepared, monkeypatch, options):
    root, _, _ = prepared
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError):
        _prepare(prepared, start_at="gameplan_stats", stop_after="gameplan_stats", **options)
    assert not calls
    assert not (root / "ml/overnight-runs").exists()


def test_saved_recovery_tail_retains_separate_deadline_exception(prepared, monkeypatch):
    from ml.preparation_deadline import RECOVERY_VERSION
    root, spec, clock = prepared
    _stages(monkeypatch, fail="ml.gameplan_trade_planning")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _prepare(prepared, start_at="loop_b_directional_generation", model_feedback=_feedback(root))
    failed = Path(native.overnight_status(root)["run_path"])
    saved = _report(failed)
    prior = _evidence_snapshot(failed)
    pin = saved["enrichment_gameplan"]
    exception = root / "tail-exception.json"
    exception.write_text(json.dumps(dict(schema_version=RECOVERY_VERSION,
        scope="PINNED_STOCK_PLANNING_AND_ACTUALS_ONLY", orders_authorized=False,
        action_date=ACTION, gameplan_run=pin["run_path"], gameplan_receipt_sha256=pin["receipt_sha256"],
        original_session_deadline_at=ORIGINAL, original_deadline_at=saved["effective_deadline_at"],
        approved_at="2026-09-10T19:10:00Z", expires_at="2026-09-10T21:00:00Z")))
    clock["now"] = pd.Timestamp("2026-09-10T19:20:00Z")
    calls = _stages(monkeypatch, fail="ml.gameplan_trade_planning")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(root, resume_run=failed, deadline_exception=exception)
    continued = Path(native.overnight_status(root)["run_path"])
    report = _report(continued)
    assert report["recovery"] == saved["recovery"]
    assert report["deadline_at"] == ORIGINAL
    assert report["effective_deadline_at"] == "2026-09-10T21:00:00+00:00"
    assert _option(calls[0][0], "--deadline") == saved["effective_deadline_at"]
    assert _option(calls[0][0], "--deadline-exception") == str(exception)
    assert _evidence_snapshot(failed) == prior
    continued_bytes = _evidence_snapshot(continued)
    clock["now"] = pd.Timestamp("2026-09-12T12:00:00Z")
    calls = _stages(monkeypatch)
    resumed = run(root, resume_run=continued)
    final = _report(resumed)
    for key in ("recovery", "deadline_exception", "deadline_at", "effective_deadline_at",
                "review_action_date", "continuation_action_date"):
        assert final[key] == report[key]
    assert _option(calls[0][0], "--deadline") == saved["effective_deadline_at"]
    assert _option(calls[0][0], "--deadline-exception") == str(exception)
    assert _evidence_snapshot(continued) == continued_bytes
    calls.clear()
    with pytest.raises(ValueError, match="one frozen native recovery route"):
        run(root, resume_run=failed, recovery_spec=spec, deadline_exception=exception)
    assert not calls
    payload = json.loads(exception.read_text())
    payload["authorization_text"] = "Changed historical annotation supplies no operating authority"
    exception.write_text(json.dumps(payload))
    resumed_after_annotation = run(root, resume_run=continued)
    final_after_annotation = _report(resumed_after_annotation)
    assert calls
    assert final_after_annotation["status"] == "COMPLETE"
    assert final_after_annotation["deadline_at"] == report["deadline_at"]
    assert final_after_annotation["effective_deadline_at"] == report["effective_deadline_at"]
    assert final_after_annotation["orders_placed"] == 0
    assert final_after_annotation["broker_orders_enabled"] is False
    assert _evidence_snapshot(continued) == continued_bytes


def test_recovery_authority_metadata_change_does_not_interrupt_stages(prepared, monkeypatch):
    root, spec, _ = prepared
    captured = verify_recovery(root, spec, REQUESTED)
    calls = []

    def stage(command, **kwargs):
        calls.append(command[3])
        kwargs["log_path"].write_text("synthetic completion before evidence changed")
        payload = json.loads(spec.read_text())
        payload["authorization_reason"] = "changed during native preparation"
        spec.write_text(json.dumps(payload))
        return 0

    monkeypatch.setattr(native, "_run_stage", stage)
    result = _prepare(prepared, start_at="loop_b_directional_generation", model_feedback=_feedback(root))
    report = _report(result)
    assert report["status"] == "COMPLETE"
    assert calls[0] == "ml.prediction_runtime" and calls[-1] == "ml.gameplan_trade_planning"
    assert len(calls) == len(report["stage_order"]) > 1
    assert all(row["status"] == "COMPLETE" for row in report["stages"])
    assert report["recovery"] == captured
    assert json.loads(spec.read_text())["authorization_reason"] == "changed during native preparation"
    assert report["orders_placed"] == 0 and report["broker_orders_enabled"] is False


@pytest.mark.parametrize("damage", [None, "session", "deadline", "receipt", "source", "schema", "missing"])
def test_dated_planner_retains_expired_legacy_tail_exception(publication_case, monkeypatch, damage):
    from ml import gameplan_trade_planning as planning
    from ml.artifacts import verify_manifest
    from ml.nightly_continuation import ENVIRONMENT_KEY
    from ml.preparation_deadline import RECOVERY_VERSION
    from test_preparation_deadline import exception_record

    case = publication_case
    day = "2026-09-09"
    original = "2026-09-09T11:00:00+00:00"
    prior_deadline = "2026-09-09T19:00:00+00:00"
    monkeypatch.setenv(ENVIRONMENT_KEY, day)
    exception, payload = exception_record(case.root, case.source, session=day)
    payload.update(schema_version=RECOVERY_VERSION, original_session_deadline_at=original,
                   original_deadline_at=prior_deadline,
                   approved_at="2026-09-09T19:01:00Z", expires_at="2026-09-09T21:00:00Z")
    if damage == "session":
        payload["original_session_deadline_at"] = "2026-09-09T12:00:00Z"
    elif damage == "deadline":
        payload["original_deadline_at"] = "2026-09-09T18:00:00Z"
    elif damage == "receipt":
        payload["gameplan_receipt_sha256"] = "changed"
    elif damage == "source":
        payload["gameplan_run"] += "-other"
    elif damage == "schema":
        payload["schema_version"] = "unrecognized"
    exception.write_text(json.dumps(payload))
    before = {path.name: path.read_bytes() for path in case.source.iterdir()}
    exception_bytes = exception.read_bytes()
    observed = pd.Timestamp("2026-09-12T16:00:00Z")

    def snapshot(*args, **kwargs):
        if damage:
            pytest.fail("Invalid exception must fail before synthetic snapshot loading")
        return {**case.state, "observed_at": observed.isoformat()}

    arguments = dict(gameplan_run=case.source, deadline=prior_deadline, late_action_date=day,
                     deadline_exception=None if damage == "missing" else exception,
                     snapshot_loader=snapshot, price_loader=case.prices, clock=lambda: observed)
    if damage:
        with pytest.raises(ValueError):
            planning.publish_trade_plan(case.root, **arguments)
        assert not (case.root / "ml/gameplan-trade-plan-runs").exists()
    else:
        result = planning.publish_trade_plan(case.root, **arguments)
        verify_manifest(result)
        report = json.loads((result / "report.json").read_text())
        assert report["deadline_at"] == original
        assert report["effective_deadline_at"] == "2026-09-09T21:00:00+00:00"
        assert report["observed_at"] == observed.isoformat()
        assert report["deadline_exception"]["authorization"] == payload
        rows = pd.read_parquet(result / "trade-plan.parquet")
        assert rows.trade_quantity.eq(0).all()
        assert report["orders_placed"] == 0 and report["broker_orders_enabled"] is False
    assert {path.name: path.read_bytes() for path in case.source.iterdir()} == before
    assert exception.read_bytes() == exception_bytes
