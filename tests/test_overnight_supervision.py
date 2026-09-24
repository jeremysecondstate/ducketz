import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from ml.artifacts import file_checksum, utc_timestamp
from ml.overnight_runtime import (
    STAGE_ORDER, _run_stage, next_action_deadline, overnight_status,
    claim_supervision, recover_interrupted_run, request_stage_stop, run_overnight_pipeline,
)


def run(root, **kwargs):
    return run_overnight_pipeline(root, datastore_argument=("--datastore", str(root)),
                                  repository_root=root, reporter=None, **kwargs)


def test_weekend_and_holiday_preserve_long_training_window():
    assert next_action_deadline("2026-09-05T00:05:00Z") == pd.Timestamp("2026-09-08T11:00:00Z")
    assert next_action_deadline("2026-09-09T00:05:00Z") == pd.Timestamp("2026-09-09T11:00:00Z")
    assert next_action_deadline("2026-09-09T09:00:00Z") == pd.Timestamp("2026-09-09T11:00:00Z")


@pytest.mark.parametrize("saved_target", [None, "cost-adjusted-positive-return-v1", "raw-price-direction-v1"])
def test_probability_target_resume_keeps_original_identity(tmp_path, monkeypatch, saved_target):
    def fail(command, **kwargs):
        kwargs["log_path"].write_text("synthetic publication failure")
        return 9
    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail)
    with pytest.raises(RuntimeError, match="exited with code 9"):
        run(tmp_path, start_at="gameplan_publication", stop_after="gameplan_publication", stock_only=True,
            independent_stock_horizons=True, probability_target_contract=saved_target)
    prior = Path(overnight_status(tmp_path)["run_path"])
    if saved_target is None:
        # Simulate an immutable attempt created before probability versioning.
        report = json.loads((prior/"stage-report.json").read_text())
        report.pop("probability_target_contract")
        report.pop("gameplan_variant")
        (prior/"stage-report.json").write_text(json.dumps(report))
        receipt = json.loads((prior/"receipt.json").read_text())
        receipt["stage_report_checksum_sha256"] = file_checksum(prior/"stage-report.json")
        (prior/"receipt.json").write_text(json.dumps(receipt))
    before = _evidence_snapshot(prior)
    calls = []
    def complete(command, **kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("synthetic completion")
        return 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", complete)
    resumed = run(tmp_path, resume_run=prior)
    expected = saved_target or "cost-adjusted-positive-return-v1"
    assert calls[0][-2:] == ("--probability-target-contract", expected)
    assert json.loads((resumed/"stage-report.json").read_text())["probability_target_contract"] == expected
    assert _evidence_snapshot(prior) == before
    alternate = "raw-price-direction-v1" if expected != "raw-price-direction-v1" else "cost-adjusted-positive-return-v1"
    with pytest.raises(ValueError, match="preserve its verified probability target"):
        run(tmp_path, resume_run=prior, probability_target_contract=alternate)


def test_failed_stage_has_receipt_and_resume_skips_completed_stages(tmp_path, monkeypatch):
    calls = []
    def fail_training(command, **kwargs):
        calls.append(command[3])
        kwargs["log_path"].write_text("test output")
        return 7 if command[3] == "ml.strategy_profit_training_runtime" else 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_training)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path)
    state = overnight_status(tmp_path)
    failed = tmp_path / "ml/overnight-runs" / pd.Timestamp(state["run_timestamp"]).strftime("%Y%m%dT%H%M%S.%fZ")
    receipt = json.loads((failed / "receipt.json").read_text())
    assert state["status"] == receipt["status"] == "FAILED"
    assert state["failed_stage"] == "strategy_profit_training"
    assert len(calls) == 4
    assert receipt["stage_report_checksum_sha256"] == file_checksum(failed / "stage-report.json")
    assert receipt["logs"]["strategy_profit_training.log"]["checksum_sha256"] == file_checksum(failed / "strategy_profit_training.log")
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kw: calls.append(command[3]) or 0)
    resumed = run(tmp_path, resume_run=failed)
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["status"] == "COMPLETE"
    assert report["stage_order"] == list(STAGE_ORDER[3:])
    assert report["completed_stages_from_previous_attempt"] == list(STAGE_ORDER[:3])
    assert report["deadline_at"] == state["deadline_at"]
    assert len(calls) == 7
    with pytest.raises(RuntimeError, match="verified failed or stopped"):
        run(tmp_path, resume_run=resumed)


def test_launch_exception_still_writes_terminal_receipt(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("test missing executable")
    monkeypatch.setattr("ml.overnight_runtime.subprocess.Popen", missing)
    with pytest.raises(FileNotFoundError):
        run(tmp_path)
    state = overnight_status(tmp_path)
    assert state["status"] == "FAILED"
    assert state["receipt_present"]


def test_live_output_reports_error_before_process_finishes(tmp_path):
    updates = []
    code = "import time; print('RuntimeError: test early diagnostic', flush=True); time.sleep(0.3); print('finished', flush=True)"
    result = _run_stage([sys.executable, "-u", "-c", code], repository=tmp_path,
        log_path=tmp_path / "child.log", deadline=utc_timestamp() + pd.Timedelta(minutes=1),
        stop_request=tmp_path / "stop.json", progress=lambda p: updates.append(dict(p)), poll_seconds=0.03)
    assert result == 0
    alerted = [p for p in updates if p["stage_health"] == "ATTENTION_REQUIRED"]
    assert len(alerted) >= 2
    assert any("test early diagnostic" in line for line in alerted[0]["recent_issues"])
    assert "finished" not in str(alerted[0]["recent_output"])


def test_deadline_stops_only_owned_child_and_writes_receipt(tmp_path, monkeypatch):
    original = _run_stage
    def slow(_command, **kwargs):
        return original([sys.executable, "-u", "-c", "import time; time.sleep(30)"], **kwargs)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", slow)
    with pytest.raises(TimeoutError):
        run(tmp_path, deadline=utc_timestamp() + pd.Timedelta(seconds=0.5), poll_seconds=0.03)
    state = overnight_status(tmp_path)
    assert state["status"] == "TIMED_OUT"
    assert state["receipt_present"]


def test_controlled_stop_is_receipted_and_resumable(tmp_path, monkeypatch):
    original = _run_stage
    def stop(_command, **kwargs):
        request_stage_stop(tmp_path, kwargs["log_path"].parent, "test verified stalled stage")
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", stop)
    with pytest.raises(InterruptedError):
        run(tmp_path, poll_seconds=0.03)
    state = overnight_status(tmp_path)
    assert state["status"] == "CANCELLED"
    assert state["receipt_present"]


def test_recovery_refuses_to_interrupt_a_living_owner(tmp_path, monkeypatch):
    def recover(_command, **kwargs):
        with pytest.raises(RuntimeError, match="owner is still alive"):
            recover_interrupted_run(tmp_path, kwargs["log_path"].parent, "test")
        return 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", recover)
    assert run(tmp_path).is_dir()


def test_dead_owner_recovery_does_not_replay_completed_stage(tmp_path, monkeypatch):
    def failed(_command, **kwargs):
        return 9
    monkeypatch.setattr("ml.overnight_runtime._run_stage", failed)
    with pytest.raises(RuntimeError):
        run(tmp_path)
    state = overnight_status(tmp_path)
    from pathlib import Path
    run_path = Path(state["run_path"])
    report_path = run_path / "stage-report.json"
    report = json.loads(report_path.read_text())
    # Simulate a parent exiting between completed first stage and the next launch.
    report["status"] = "RUNNING"
    report["stages"][0]["status"] = "COMPLETE"
    report["owner_pid"] = 99999999
    report_path.write_text(json.dumps(report))
    (run_path / "receipt.json").unlink()
    recover_interrupted_run(tmp_path, run_path, "verified supervisor exited")
    result = json.loads(report_path.read_text())
    assert result["failed_stage"] == STAGE_ORDER[1]
    assert result["status"] == "CANCELLED"


def test_recovery_refuses_reused_child_pid(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda *a, **kw: 9)
    with pytest.raises(RuntimeError):
        run(tmp_path)
    from pathlib import Path
    import os
    state = overnight_status(tmp_path)
    run_path = Path(state["run_path"])
    report_path = run_path / "stage-report.json"
    report = json.loads(report_path.read_text())
    report.update(status="RUNNING", owner_pid=99999999, child_pid=os.getpid(), child_created_at=0)
    report_path.write_text(json.dumps(report))
    (run_path / "receipt.json").unlink()
    with pytest.raises(RuntimeError, match="PID was reused"):
        recover_interrupted_run(tmp_path, run_path, "test")


def test_only_one_scheduled_operator_can_claim_or_renew_supervision(tmp_path):
    import uuid
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    assert claim_supervision(tmp_path, first, observed_at="2026-09-05T01:00Z")["status"] == "ACQUIRED"
    assert claim_supervision(tmp_path, second, observed_at="2026-09-05T01:01Z")["status"] == "BUSY"
    assert claim_supervision(tmp_path, first, observed_at="2026-09-05T01:02Z")["status"] == "ACQUIRED"
    assert claim_supervision(tmp_path, second, observed_at="2026-09-05T01:04Z")["status"] == "BUSY"
    assert claim_supervision(tmp_path, second, observed_at="2026-09-05T01:05Z")["status"] == "ACQUIRED"
    assert claim_supervision(tmp_path, first, release=True, observed_at="2026-09-05T01:06Z")["status"] == "BUSY"
    assert claim_supervision(tmp_path, second, release=True, observed_at="2026-09-05T01:06Z")["status"] == "RELEASED"


def _terminal_attempt(tmp_path, monkeypatch, *, status="FAILED"):
    def stage(command, **kwargs):
        kwargs["log_path"].write_text("preserved stage evidence")
        return 9 if command[3] == "ml.prediction_runtime" else 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", stage)
    with pytest.raises(RuntimeError, match="exited with code 9"):
        run(tmp_path)
    run_path = Path(overnight_status(tmp_path)["run_path"])
    report_path = run_path / "stage-report.json"
    report = json.loads(report_path.read_text())
    report.update(status=status, owner_pid=11111111, owner_created_at=11.0,
                  child_pid=22222222, child_created_at=22.0)
    report_path.write_text(json.dumps(report))
    receipt_path = run_path / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(status=status, stage_report_size=report_path.stat().st_size,
                   stage_report_checksum_sha256=file_checksum(report_path))
    receipt_path.write_text(json.dumps(receipt))
    return run_path


def _evidence_snapshot(run_path):
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in run_path.iterdir() if path.is_file()}


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_terminal_recovery_is_idempotent_and_preserves_all_evidence(tmp_path, monkeypatch, status):
    run_path = _terminal_attempt(tmp_path, monkeypatch, status=status)
    before = _evidence_snapshot(run_path)
    monkeypatch.setattr("ml.overnight_runtime._process_created_at", lambda _: None)
    monkeypatch.setattr("ml.overnight_runtime._write_json_atomic", lambda *a, **k: pytest.fail("terminal evidence is immutable"))
    monkeypatch.setattr("psutil.Process", lambda *a, **k: pytest.fail("terminal recovery cannot manipulate processes"))

    assert recover_interrupted_run(tmp_path, run_path, "verified supervisor exit") == run_path
    assert recover_interrupted_run(tmp_path, run_path, "repeat verified recovery") == run_path
    assert _evidence_snapshot(run_path) == before


@pytest.mark.parametrize("live,reason", [
    ({11111111: 11.0}, "owner is still alive"),
    ({22222222: 22.0}, "child is still alive"),
    ({22222222: 99.0}, "Child PID was reused"),
])
def test_terminal_recovery_refuses_living_or_reused_processes_without_stopping_them(tmp_path, monkeypatch, live, reason):
    run_path = _terminal_attempt(tmp_path, monkeypatch)
    before = _evidence_snapshot(run_path)
    monkeypatch.setattr("ml.overnight_runtime._process_created_at", lambda pid: live.get(pid))
    monkeypatch.setattr("psutil.Process", lambda *a, **k: pytest.fail("no process may be stopped"))
    with pytest.raises(RuntimeError, match=reason):
        recover_interrupted_run(tmp_path, run_path, "verified supervisor exit")
    assert _evidence_snapshot(run_path) == before


def test_terminal_recovery_leaves_reused_owner_pid_untouched(tmp_path, monkeypatch):
    run_path = _terminal_attempt(tmp_path, monkeypatch)
    before = _evidence_snapshot(run_path)
    monkeypatch.setattr("ml.overnight_runtime._process_created_at", lambda pid: 99.0 if pid == 11111111 else None)
    monkeypatch.setattr("psutil.Process", lambda *a, **k: pytest.fail("unrelated owner PID must not be manipulated"))
    assert recover_interrupted_run(tmp_path, run_path, "original owner creation identity exited") == run_path
    assert _evidence_snapshot(run_path) == before


@pytest.mark.parametrize("damage", ["report", "log", "receipt", "missing_receipt", "escaping_log"])
def test_terminal_recovery_rejects_invalid_saved_evidence(tmp_path, monkeypatch, damage):
    run_path = _terminal_attempt(tmp_path, monkeypatch)
    receipt_path = run_path / "receipt.json"
    if damage == "report":
        with (run_path / "stage-report.json").open("a") as stream:
            stream.write(" ")
    elif damage == "log":
        (run_path / "loop_a_close_fetch.log").write_text("changed log")
    elif damage == "missing_receipt":
        receipt_path.unlink()
    else:
        receipt = json.loads(receipt_path.read_text())
        if damage == "receipt":
            receipt["broker_orders_enabled"] = True
        else:
            receipt["logs"]["../unrelated.log"] = {"checksum_sha256": "0" * 64, "size": 0}
        receipt_path.write_text(json.dumps(receipt))
    before = _evidence_snapshot(run_path)
    monkeypatch.setattr("ml.overnight_runtime._process_created_at", lambda _: pytest.fail("invalid evidence must fail before process checks"))
    with pytest.raises((RuntimeError, OSError)):
        recover_interrupted_run(tmp_path, run_path, "verified supervisor exit")
    assert _evidence_snapshot(run_path) == before


def test_terminal_recovery_preserves_deadline_and_completed_stages_for_resume(tmp_path, monkeypatch):
    run_path = _terminal_attempt(tmp_path, monkeypatch)
    original = json.loads((run_path / "stage-report.json").read_text())
    monkeypatch.setattr("ml.overnight_runtime._process_created_at", lambda _: None)
    recover_interrupted_run(tmp_path, run_path, "verified supervisor exit")
    from ml.overnight_runtime import _resume_configuration
    configuration = _resume_configuration(tmp_path, run_path)
    assert configuration["deadline_at"] == original["deadline_at"]
    assert configuration["completed_stages"] == ["loop_a_close_fetch"]
    assert configuration["failed_stage"] == "loop_b_directional_generation"


def test_terminal_recovery_refuses_expired_deadline(tmp_path, monkeypatch):
    run_path = _terminal_attempt(tmp_path, monkeypatch)
    before = _evidence_snapshot(run_path)
    deadline = json.loads((run_path / "stage-report.json").read_text())["deadline_at"]
    monkeypatch.setattr("ml.overnight_runtime.utc_timestamp", lambda value=None: pd.Timestamp(value if value is not None else deadline))
    with pytest.raises(RuntimeError, match="deadline has passed"):
        recover_interrupted_run(tmp_path, run_path, "verified supervisor exit")
    assert _evidence_snapshot(run_path) == before


@pytest.mark.parametrize("status,reason", [("COMPLETE", "verified supervisor exit"), ("FAILED", " ")])
def test_recovery_requires_resumable_status_and_reason(tmp_path, monkeypatch, status, reason):
    run_path = _terminal_attempt(tmp_path, monkeypatch, status=status)
    before = _evidence_snapshot(run_path)
    with pytest.raises(RuntimeError, match="Recovery needs"):
        recover_interrupted_run(tmp_path, run_path, reason)
    assert _evidence_snapshot(run_path) == before


@pytest.mark.parametrize("stock_only", [False, True])
def test_stock_only_selection_leaves_default_pipeline_unchanged(tmp_path, monkeypatch, stock_only):
    calls = []

    def stage(command, **kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("synthetic stage complete")
        return 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", stage)
    run_path = run(tmp_path, stock_only=stock_only)
    report = json.loads((run_path / "stage-report.json").read_text())
    receipt = json.loads((run_path / "receipt.json").read_text())
    options = {"strategy_profit_training", "strategy_generation"}
    expected = [stage for stage in STAGE_ORDER if not stock_only or stage not in options]
    assert report["stage_order"] == expected
    assert [row["stage"] for row in report["stages"]] == expected
    assert len(calls) == len(expected)
    assert ("--stock-only" in calls[-1]) is stock_only
    assert all("--stock-only" not in command for command in calls[:-1])
    assert receipt["status"] == "COMPLETE"
    assert receipt["orders_placed"] == 0
    assert receipt["broker_orders_enabled"] is False
    assert receipt["stage_report_checksum_sha256"] == file_checksum(run_path / "stage-report.json")
    if stock_only:
        assert report["stock_only"] is True
        assert report["preparation_scope"] == "STOCK_ONLY"
        assert set(report["omitted_option_stages"]) == options
    else:
        assert "stock_only" not in report
        assert "omitted_option_stages" not in report


@pytest.mark.parametrize("independent", [False, True])
def test_stock_only_resume_narrows_failed_options_and_inherits_scope(tmp_path, monkeypatch, independent):
    deadline = utc_timestamp() + pd.Timedelta(hours=1)

    def fail_options(command, **kwargs):
        kwargs["log_path"].write_text("original preserved evidence")
        return 7 if command[3] == "ml.strategy_profit_training_runtime" else 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_options)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, deadline=deadline)
    original = Path(overnight_status(tmp_path)["run_path"])
    original_evidence = _evidence_snapshot(original)
    calls = []

    def fail_publication(command, **kwargs):
        calls.append((command, kwargs["deadline"]))
        kwargs["log_path"].write_text("stock publication test failure")
        return 9

    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_publication)
    with pytest.raises(RuntimeError, match="exited with code 9"):
        run(tmp_path, resume_run=original, stock_only=True,
            independent_stock_horizons=independent,
            deadline=deadline + pd.Timedelta(hours=5))
    narrowed = Path(overnight_status(tmp_path)["run_path"])
    narrowed_evidence = _evidence_snapshot(narrowed)

    def complete_publication(command, **kwargs):
        calls.append((command, kwargs["deadline"]))
        kwargs["log_path"].write_text("stock publication test success")
        return 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", complete_publication)
    resumed = run(tmp_path, resume_run=narrowed)
    for path in (narrowed, resumed):
        report = json.loads((path / "stage-report.json").read_text())
        assert report["stage_order"] == ["gameplan_publication"]
        assert report["stock_only"] is True
        assert report["preparation_scope"] == "STOCK_ONLY"
        assert report.get("independent_stock_horizons", False) is independent
        assert report["omitted_option_stages"] == ["strategy_profit_training", "strategy_generation"]
        assert report["completed_stages_from_previous_attempt"] == list(STAGE_ORDER[:3])
        assert report["deadline_at"] == deadline.isoformat()
        assert [row["stage"] for row in report["stages"]] == ["gameplan_publication"]
    assert len(calls) == 2
    assert all(("--independent-stock-horizons" in command) is independent for command, _ in calls)
    assert all(command[3] == "ml.nightly_gameplan" and "--stock-only" in command
               and bound_deadline == deadline for command, bound_deadline in calls)
    assert _evidence_snapshot(original) == original_evidence
    assert _evidence_snapshot(narrowed) == narrowed_evidence


@pytest.mark.parametrize("damage", ["report", "expired"])
def test_stock_only_resume_keeps_receipt_and_deadline_guards(tmp_path, monkeypatch, damage):
    failed = _terminal_attempt(tmp_path, monkeypatch)
    if damage == "report":
        with (failed / "stage-report.json").open("a") as stream:
            stream.write(" ")
        reason = "verified failed or stopped"
    else:
        deadline = json.loads((failed / "stage-report.json").read_text())["deadline_at"]
        monkeypatch.setattr("ml.overnight_runtime.utc_timestamp",
                            lambda value=None: pd.Timestamp(value if value is not None else deadline))
        reason = "deadline has passed"
    before = _evidence_snapshot(failed)
    monkeypatch.setattr("ml.overnight_runtime._run_stage",
                        lambda *a, **k: pytest.fail("invalid resume cannot launch work"))
    with pytest.raises(RuntimeError, match=reason):
        run(tmp_path, resume_run=failed, stock_only=True)
    assert _evidence_snapshot(failed) == before


def test_stock_only_empty_stage_selection_does_not_create_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._run_stage",
                        lambda *a, **k: pytest.fail("empty selection cannot launch work"))
    with pytest.raises(ValueError, match="No overnight stages remain"):
        run(tmp_path, start_at="strategy_profit_training", stop_after="strategy_generation",
            stock_only=True)
    assert not (tmp_path / "ml" / "overnight-runs").exists()
    assert not (tmp_path / "ml" / "overnight-latest" / "run.json").exists()


def test_stock_only_cli_passes_explicit_scope(tmp_path, monkeypatch):
    from ml import overnight_runtime

    calls = []
    monkeypatch.setattr(overnight_runtime, "run_overnight_pipeline",
                        lambda root, **kwargs: calls.append(kwargs) or tmp_path)
    assert overnight_runtime.main(["--datastore", str(tmp_path), "--once", "--stock-only"]) == 0
    assert len(calls) == 1
    assert calls[0]["stock_only"] is True


def test_independent_pipeline_trains_sizing_then_plans_trades_from_same_publication(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)

    def stage(command, **kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("synthetic stage complete")
        return 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", stage)
    directory = run(tmp_path, start_at="gameplan_publication", stock_only=True,
                    independent_stock_horizons=True, stock_price_source="xnas-itch-archive-v1")
    report = json.loads((directory / "stage-report.json").read_text())
    assert report["stage_order"] == ["gameplan_publication", "stock_enrichment_training", "gameplan_trade_planning", "gameplan_actuals_review"]
    assert report["stock_price_source"] == "xnas-itch-archive-v1"
    assert calls[0][3] == "ml.nightly_gameplan"
    assert calls[0][-4:] == ("--stock-price-source", "xnas-itch-archive-v1",
                              "--probability-target-contract", "raw-price-direction-v1")
    assert report["probability_target_contract"] == "raw-price-direction-v1"
    assert report["gameplan_variant"] == "YG"
    assert calls[1][3] == "ml.stock_trader.independent_training"
    assert calls[2][3] == "ml.gameplan_trade_planning"
    pinned_path = str(tmp_path / report["enrichment_gameplan"]["run_path"])
    assert calls[1][-2:] == ("--gameplan-run", pinned_path)
    assert calls[2][-4:] == ("--gameplan-run", pinned_path, "--deadline", report["deadline_at"])
    assert calls[3][3] == "ml.gameplan_actuals_review"
    assert calls[3][-4:] == ("--gameplan-run", pinned_path, "--deadline", report["deadline_at"])
    assert all("--execute" not in command for command in calls)
    assert report["status"] == "COMPLETE"
    assert report["orders_placed"] == 0
    receipt = json.loads((directory / "receipt.json").read_text())
    assert receipt["logs"]["gameplan_trade_planning.log"]["checksum_sha256"] == file_checksum(directory / "gameplan_trade_planning.log")


@pytest.mark.parametrize("enabled", [False, True])
def test_archive_history_routes_stage_flags_and_preserves_failed_attempt(tmp_path, monkeypatch, enabled):
    calls = []
    def fail_publication(command, **kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("synthetic stage evidence")
        return 7 if command[3] == "ml.nightly_gameplan" else 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_publication)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, start_at="stock_target_history", stop_after="gameplan_publication",
            stock_only=True, independent_stock_horizons=True,
            stock_price_source="xnas-itch-archive-v1", archive_history=enabled)
    failed = Path(overnight_status(tmp_path)["run_path"])
    saved = json.loads((failed / "stage-report.json").read_text())
    assert saved["archive_history"] is enabled
    assert ("--extend-to-feature-history" in calls[0]) is enabled
    assert ("--archive-history" in calls[-1]) is enabled
    preserved = _evidence_snapshot(failed)
    with pytest.raises(ValueError, match="preserve its verified archive history policy"):
        run(tmp_path, resume_run=failed, archive_history=not enabled)
    calls.clear()
    def success(command, **kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("synthetic stage complete")
        return 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", success)
    resumed = run(tmp_path, resume_run=failed)
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["archive_history"] is enabled
    assert report["deadline_at"] == saved["deadline_at"]
    assert [c[3] for c in calls] == ["ml.nightly_gameplan"]
    assert ("--archive-history" in calls[0]) is enabled
    assert _evidence_snapshot(failed) == preserved


def test_independent_sizing_failure_resumes_without_republishing_forecasts(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)
    def stage(command, **kwargs):
        kwargs["log_path"].write_text("synthetic sizing failure")
        return 7 if command[3] == "ml.stock_trader.independent_training" else 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", stage)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, start_at="gameplan_publication", stock_only=True,
            independent_stock_horizons=True, stock_price_source="xnas-itch-archive-v1")
    failed = Path(overnight_status(tmp_path)["run_path"])
    preserved = _evidence_snapshot(failed)
    with pytest.raises(ValueError, match="preserve its verified stock price source"):
        run(tmp_path, resume_run=failed, stock_price_source="canonical-equity-minute-v1")
    assert _evidence_snapshot(failed) == preserved
    calls = []

    def succeed(command, **kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("synthetic sizing complete")
        return 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", succeed)
    resumed = run(tmp_path, resume_run=failed)
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["stage_order"] == ["stock_enrichment_training", "gameplan_trade_planning", "gameplan_actuals_review"]
    assert report["completed_stages_from_previous_attempt"] == ["gameplan_publication"]
    assert report["stock_price_source"] == "xnas-itch-archive-v1"
    assert report["enrichment_gameplan"] == json.loads((failed / "stage-report.json").read_text())["enrichment_gameplan"]
    assert calls[0][-2:] == ("--gameplan-run", str(tmp_path / "ml/nightly-gameplan-runs/synthetic-pinned-run"))
    assert report["deadline_at"] == json.loads((failed / "stage-report.json").read_text())["deadline_at"]
    assert [command[3] for command in calls] == ["ml.stock_trader.independent_training", "ml.gameplan_trade_planning", "ml.gameplan_actuals_review"]
    assert _evidence_snapshot(failed) == preserved


def test_trade_planning_failure_resumes_only_planning_with_original_pin_and_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)

    def fail_planning(command, **kwargs):
        kwargs["log_path"].write_text("synthetic trade planning failure")
        return 7 if command[3] == "ml.gameplan_trade_planning" else 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_planning)
    with pytest.raises(RuntimeError, match="gameplan_trade_planning exited with code 7"):
        run(tmp_path, start_at="gameplan_publication", stock_only=True,
            independent_stock_horizons=True, stock_price_source="xnas-itch-archive-v1")
    failed = Path(overnight_status(tmp_path)["run_path"])
    original = json.loads((failed / "stage-report.json").read_text())
    preserved = _evidence_snapshot(failed)
    calls = []

    def keep_original_pin(root, *, pinned=None, **kwargs):
        assert pinned == original["enrichment_gameplan"]
        assert kwargs["deadline_at"].isoformat() == original["deadline_at"]
        assert kwargs["stock_price_source"] == "xnas-itch-archive-v1"
        return pinned

    def complete_planning(command, **kwargs):
        calls.append(command)
        assert kwargs["deadline"].isoformat() == original["deadline_at"]
        kwargs["log_path"].write_text("synthetic trade planning complete")
        return 0

    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", keep_original_pin)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", complete_planning)
    resumed = run(tmp_path, resume_run=failed, deadline=utc_timestamp() + pd.Timedelta(days=1))
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["stage_order"] == ["gameplan_trade_planning", "gameplan_actuals_review"]
    assert report["completed_stages_from_previous_attempt"] == ["gameplan_publication", "stock_enrichment_training"]
    assert report["enrichment_gameplan"] == original["enrichment_gameplan"]
    assert len(calls) == 2 and calls[0][3] == "ml.gameplan_trade_planning" and calls[1][3] == "ml.gameplan_actuals_review"
    assert calls[0][-4:] == ("--gameplan-run", str(tmp_path / original["enrichment_gameplan"]["run_path"]),
                            "--deadline", original["deadline_at"])
    assert _evidence_snapshot(failed) == preserved


def test_older_explicit_enrichment_boundary_stays_narrow_on_resume(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)

    def fail_sizing(command, **kwargs):
        kwargs["log_path"].write_text("synthetic old enrichment boundary failure")
        return 7 if command[3] == "ml.stock_trader.independent_training" else 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_sizing)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, start_at="gameplan_publication", stop_after="stock_enrichment_training",
            stock_only=True, independent_stock_horizons=True)
    failed = Path(overnight_status(tmp_path)["run_path"])
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kwargs: calls.append(command) or 0)
    resumed = run(tmp_path, resume_run=failed)
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["stage_order"] == ["stock_enrichment_training"]
    assert len(calls) == 1 and calls[0][3] == "ml.stock_trader.independent_training"


def test_actuals_failure_resumes_only_review_after_completed_successor(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)

    def fail_review(command, **kwargs):
        kwargs["log_path"].write_text("synthetic actuals review failure")
        return 7 if command[3] == "ml.gameplan_actuals_review" else 0

    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_review)
    with pytest.raises(RuntimeError, match="gameplan_actuals_review exited"):
        run(tmp_path, start_at="gameplan_publication", stock_only=True, independent_stock_horizons=True)
    failed = Path(overnight_status(tmp_path)["run_path"])
    original = json.loads((failed / "stage-report.json").read_text())
    preserved = _evidence_snapshot(failed)
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kwargs: calls.append(command) or 0)
    resumed = run(tmp_path, resume_run=failed)
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["stage_order"] == ["gameplan_actuals_review"]
    assert report["completed_stages_from_previous_attempt"] == ["gameplan_publication", "stock_enrichment_training", "gameplan_trade_planning"]
    assert report["enrichment_gameplan"] == original["enrichment_gameplan"]
    assert report["deadline_at"] == original["deadline_at"]
    assert len(calls) == 1 and calls[0][3] == "ml.gameplan_actuals_review"
    assert _evidence_snapshot(failed) == preserved


def test_explicit_trade_plan_boundary_does_not_gain_actuals_review_on_resume(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kwargs: 7)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, start_at="gameplan_trade_planning", stop_after="gameplan_trade_planning", stock_only=True,
            independent_stock_horizons=True)
    failed = Path(overnight_status(tmp_path)["run_path"])
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kwargs: calls.append(command) or 0)
    resumed = run(tmp_path, resume_run=failed)
    assert json.loads((resumed / "stage-report.json").read_text())["stage_order"] == ["gameplan_trade_planning"]
    assert len(calls) == 1 and calls[0][3] == "ml.gameplan_trade_planning"


@pytest.mark.parametrize("stage", ["stock_enrichment_training", "gameplan_trade_planning", "gameplan_actuals_review"])
def test_independent_tail_resume_without_saved_pin_cannot_select_current_gameplan(tmp_path, monkeypatch, stage):
    def fail_pin(*args, **kwargs):
        raise ValueError("synthetic unavailable Gameplan pin")

    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", fail_pin)
    with pytest.raises(ValueError, match="unavailable Gameplan pin"):
        run(tmp_path, start_at=stage, stock_only=True, independent_stock_horizons=True)
    failed = Path(overnight_status(tmp_path)["run_path"])
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan",
                        lambda *args, **kwargs: pytest.fail("resume cannot select another Gameplan"))
    monkeypatch.setattr("ml.overnight_runtime._run_stage",
                        lambda *args, **kwargs: pytest.fail("missing pin cannot launch any stage"))
    with pytest.raises(ValueError, match="has no pinned Gameplan"):
        run(tmp_path, resume_run=failed)


def test_explicit_publication_boundary_does_not_start_sizing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kwargs: calls.append(command) or 0)
    directory = run(tmp_path, start_at="gameplan_publication", stop_after="gameplan_publication",
                    stock_only=True, independent_stock_horizons=True)
    assert len(calls) == 1
    assert json.loads((directory / "stage-report.json").read_text())["stage_order"] == ["gameplan_publication"]


def test_xnas_full_pipeline_refreshes_targets_before_evaluation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kwargs: calls.append(command[3]) or 0)
    directory = run(tmp_path, stock_only=True, independent_stock_horizons=True,
                    stock_price_source="xnas-itch-archive-v1")
    assert calls == ["datafetching.orchestrate", "ml.prediction_runtime", "ml.stock_target_history",
                     "ml.gameplan_evaluation", "ml.nightly_gameplan", "ml.stock_trader.independent_training",
                     "ml.gameplan_trade_planning", "ml.gameplan_actuals_review"]
    report = json.loads((directory / "stage-report.json").read_text())
    assert report["stage_order"][2] == "stock_target_history"


def _synthetic_gameplan_pin(root, *, pinned=None, **kwargs):
    return pinned or {"run_path": "ml/nightly-gameplan-runs/synthetic-pinned-run",
                      "receipt_sha256": "synthetic-receipt", "action_date": "2026-09-08"}


def test_enrichment_pin_uses_original_run_when_current_pointer_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ml.overnight_runtime import _pin_stock_gameplan
    source = tmp_path / "ml/nightly-gameplan-runs/first"
    source.mkdir(parents=True)
    (source / "receipt.json").write_text("original immutable receipt")
    publication = SimpleNamespace(run_directory=source,
        manifest={"configuration": {"action_date": "2026-09-08",
             "target_contract_version": "independent-stock-targets-v1",
             "target_price_source_contract": "xnas-itch-archive-v1"}},
        receipt={"action_date": "2026-09-08"})
    monkeypatch.setattr("ml.nightly_gameplan.read_current_gameplan", lambda root: publication)
    with pytest.raises(ValueError, match="archive history policy"):
        _pin_stock_gameplan(tmp_path, stock_price_source="xnas-itch-archive-v1",
            deadline_at=pd.Timestamp("2026-09-08T11:00Z"), archive_history=True)
    selected = _pin_stock_gameplan(tmp_path, stock_price_source="xnas-itch-archive-v1",
                                  deadline_at=pd.Timestamp("2026-09-08T11:00Z"))
    monkeypatch.setattr("ml.nightly_gameplan.read_current_gameplan", lambda root: pytest.fail("resume cannot read current pointer"))
    seen = []
    monkeypatch.setattr("ml.nightly_gameplan.read_gameplan_run", lambda root, path: seen.append(path) or publication)
    assert _pin_stock_gameplan(tmp_path, stock_price_source="xnas-itch-archive-v1",
        deadline_at=pd.Timestamp("2026-09-08T11:00Z"), pinned=selected) == selected
    assert seen == [source]
    with pytest.raises(ValueError, match="source or original action deadline"):
        _pin_stock_gameplan(tmp_path, stock_price_source="canonical-equity-minute-v1",
            deadline_at=pd.Timestamp("2026-09-08T11:00Z"), pinned=selected)
    (source / "receipt.json").write_text("modified receipt")
    with pytest.raises(ValueError, match="receipt changed"):
        _pin_stock_gameplan(tmp_path, stock_price_source="xnas-itch-archive-v1",
            deadline_at=pd.Timestamp("2026-09-08T11:00Z"), pinned=selected)
