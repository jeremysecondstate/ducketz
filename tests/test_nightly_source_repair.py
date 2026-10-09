"""Source repair uses fixtures only; no providers, models or production launch."""
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from filelock import FileLock, Timeout

from ml import nightly_workflow as workflow
from ml.artifacts import file_checksum
from ml.nightly_recovery import make_recovery, verify_recovery
from tools import nightly_source_repair as repair

NOW = pd.Timestamp("2026-10-09T18:00Z")
NAME = "ml/nightly_gameplan.py"


@pytest.fixture
def example(tmp_path, monkeypatch):
    repository, candidate, data, state_root = (tmp_path / n for n in ("repo", "candidate", "data", "state"))
    for p in (repository, candidate, data, state_root):
        p.mkdir()
    for p, raw in ((repository, b"old training code\n"), (candidate, b"reviewed training code\n")):
        (p / "ml").mkdir()
        (p / NAME).write_bytes(raw)
    (repository / "datafetching").mkdir()
    watch = repository / "datafetching/watchlist.local.txt"
    watch.write_text("AAPL\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"actor": "Scout", "checkout": str(repository),
        "symbol_profile_path": str(watch), "symbols": ["AAPL"]}))
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    config = {"actor": "Scout", "repository": str(repository), "datastore": str(data),
              "state_root": str(state_root), "local_profile": str(profile)}
    def identity(repo):
        return {"commit": "a" * 40, "source_sha256": hashlib.sha256(
            json.dumps(repair._inventory(repo), sort_keys=True).encode()).hexdigest()}
    monkeypatch.setattr(workflow, "source_identity", identity)
    day = state_root / "runs/2026-10-09"
    day.mkdir(parents=True)
    recovery = day / "recovery.json"
    recovery.write_text(json.dumps(make_recovery(config, NOW, reason="operator authorized recovery")))
    native = data / "ml/overnight-runs/failed"
    native.mkdir(parents=True)
    (native / "stage-report.json").write_text(json.dumps({"status": "FAILED", "failed_stage": "gameplan_publication"}))
    (native / "publication.log").write_text("cutoff failure")
    (native / "receipt.json").write_text(json.dumps({"status": "FAILED", "failed_stage": "gameplan_publication",
        "stage_report_checksum_sha256": file_checksum(native / "stage-report.json"),
        "logs": {"publication.log": {"checksum_sha256": file_checksum(native / "publication.log")}}}))
    output = data / "stats-evidence"
    output.write_bytes(b"frozen stats")
    complete = {"status": "COMPLETE", "output": {"files": {str(output): file_checksum(output)}}}
    state = {"action_date": "2026-10-09", "source_session": "2026-10-08", "status": "FAILED",
        "owner_pid": None, "current_step": "train_and_plan", "run_id": "original-session",
        "deadline_at": "2026-10-09T11:00:00+00:00", "source_identity": identity(repository),
        "configuration_binding": workflow._configuration_binding(config),
        "symbol_binding": workflow._symbol_binding(config), "symbols": ["AAPL"],
        "recovery": verify_recovery(data, recovery, NOW),
        "steps": {"prepare_stats": complete, "model_review": complete,
                  "train_and_plan": {"status": "FAILED", "native_run": str(native)}}}
    state["effective_deadline_at"] = state["recovery"]["authorization"]["expires_at"]
    workflow._write(day / "state.json", state)
    check = tmp_path / "pytest.log"
    check.write_text("10 passed in 1.00s\n")
    return config, candidate, check, day


def prepare(example):
    config, candidate, check, _ = example
    return repair.prepare(config, action_date="2026-10-09", repair_id="reviewed-repair-001",
        candidate=candidate, paths=[NAME], completion_record="reviewed-source-record",
        checks=[check], reviewed=True, now=NOW)


def test_install_rebind_preserves_originals_stats_and_expiry_requires_fresh_review(example):
    config, candidate, _, day = example
    before = (day / "state.json").read_bytes()
    spec = prepare(example)
    result = repair.apply(config, spec, reviewed=True, now=NOW)
    after = json.loads((day / "state.json").read_text())
    original = json.loads(before)
    assert result["status"] == "SOURCE_REPAIR_APPLIED"
    assert (spec.parent / "before-state.json").read_bytes() == before
    assert (spec.parent / "originals" / NAME).read_bytes() == b"old training code\n"
    assert (Path(config["repository"]) / NAME).read_bytes() == (candidate / NAME).read_bytes()
    assert after["steps"] == {"prepare_stats": original["steps"]["prepare_stats"]}
    for key in ("run_id", "deadline_at", "effective_deadline_at", "recovery", "symbol_binding", "configuration_binding"):
        assert after[key] == original[key]
    assert after["current_step"] == "model_review" and after["status"] == "READY"
    assert after["source_identity"] != original["source_identity"]
    assert repair.apply(config, spec, reviewed=True, now=NOW)["status"] == "SOURCE_REPAIR_ALREADY_APPLIED"


@pytest.mark.parametrize("changed", ["state", "source", "unrelated", "candidate", "checks", "stats", "native"])
def test_changed_evidence_is_rejected_before_installation(example, changed):
    config, _, _, day = example
    spec = prepare(example)
    target = Path(config["repository"]) / NAME
    if changed == "state":
        (day / "state.json").write_text((day / "state.json").read_text() + " ")
    elif changed == "source":
        target.write_text("unreviewed third version")
    elif changed == "unrelated":
        (target.parent / "other.py").write_text("unrelated change")
    elif changed == "candidate":
        (spec.parent / "candidate" / NAME).write_text("changed reviewed bytes")
    elif changed == "checks":
        (spec.parent / "checks/0").write_text("different tests")
    elif changed == "stats":
        (Path(config["datastore"]) / "stats-evidence").write_text("changed stats")
    else:
        (Path(config["datastore"]) / "ml/overnight-runs/failed/publication.log").write_text("changed log")
    with pytest.raises(ValueError):
        repair.apply(config, spec, reviewed=True, now=NOW)
    assert target.read_bytes() != b"reviewed training code\n"


def test_expired_recovery_cannot_install_or_renew_deadline(example):
    config, _, _, day = example
    spec = prepare(example)
    before = (day / "state.json").read_bytes()
    with pytest.raises(ValueError, match="expired"):
        repair.apply(config, spec, reviewed=True, now=NOW + pd.Timedelta(hours=7))
    assert (day / "state.json").read_bytes() == before


def test_live_workflow_lock_prevents_repair(example):
    config, _, _, _ = example
    spec = prepare(example)
    with FileLock(str(Path(config["state_root"]) / "workflow.lock")):
        with pytest.raises(Timeout):
            repair.apply(config, spec, reviewed=True, now=NOW)


def test_partial_install_resumes_exact_original_transition(example):
    config, candidate, _, _ = example
    spec = prepare(example)
    (Path(config["repository"]) / NAME).write_bytes((candidate / NAME).read_bytes())
    assert repair.apply(config, spec, reviewed=True, now=NOW)["status"] == "SOURCE_REPAIR_APPLIED"


@pytest.fixture
def tail_example(example, monkeypatch):
    from ml import overnight_runtime
    config, candidate, check, day = example
    name = "ml/gameplan_trade_planning.py"
    for root, text in ((Path(config["repository"]), "old planner"), (candidate, "reviewed planner")):
        (root / name).write_text(text)
    state = workflow._json(day / "state.json")
    native = Path(state["steps"]["train_and_plan"]["native_run"])
    report = {"status": "FAILED", "failed_stage": "gameplan_trade_planning",
              "recovery": state["recovery"], "enrichment_gameplan": {"run_path": "ml/nightly-gameplan-runs/original"},
              "stages": [{"stage": stage, "status": "COMPLETE"} for stage in
                         ("loop_b_directional_generation", "gameplan_evaluation", "gameplan_publication", "stock_enrichment_training")]}
    workflow._write(native / "stage-report.json", report)
    receipt = workflow._json(native / "receipt.json")
    receipt.update(failed_stage="gameplan_trade_planning",
                   stage_report_checksum_sha256=file_checksum(native / "stage-report.json"))
    workflow._write(native / "receipt.json", receipt)
    state["source_identity"] = workflow.source_identity(Path(config["repository"]))
    workflow._write(day / "state.json", state)
    pins = []
    def verify_pin(root, **kwargs):
        pins.append(kwargs)
        return kwargs["pinned"]
    monkeypatch.setattr(overnight_runtime, "_pin_stock_gameplan", verify_pin)
    return example, name, pins


def prepare_tail(tail_example):
    (config, candidate, check, _), name, _ = tail_example
    return repair.prepare(config, action_date="2026-10-09", repair_id="reviewed-tail-001",
        candidate=candidate, paths=[name], completion_record="reviewed-tail-record",
        checks=[check], reviewed=True, now=NOW, retain_completed_preparation=True)


def test_tail_repair_retains_exact_numerical_attempt_and_original_review(tail_example):
    (config, _, _, day), _, pins = tail_example
    before = workflow._json(day / "state.json")
    native = Path(before["steps"]["train_and_plan"]["native_run"])
    evidence = {p.name: p.read_bytes() for p in native.iterdir()}
    spec = prepare_tail(tail_example)
    repair.apply(config, spec, reviewed=True, now=NOW)
    after = workflow._json(day / "state.json")
    assert after["steps"] == before["steps"]
    assert after["current_step"] == "train_and_plan" and after["status"] == "READY"
    assert after["recovery"] == before["recovery"]
    assert after["effective_deadline_at"] == before["effective_deadline_at"]
    assert {p.name: p.read_bytes() for p in native.iterdir()} == evidence
    assert len(pins) == 2 and pins[0]["pinned"] == pins[1]["pinned"]
    assert after["source_identity"] != before["source_identity"]


@pytest.mark.parametrize("condition", ["missing_stage", "changed_recovery", "missing_pin", "unreviewed_model"])
def test_tail_repair_requires_complete_original_numerical_evidence(tail_example, condition):
    (config, _, _, day), _, _ = tail_example
    state = workflow._json(day / "state.json")
    native = Path(state["steps"]["train_and_plan"]["native_run"])
    report = workflow._json(native / "stage-report.json")
    if condition == "missing_stage":
        report["stages"].pop()
    elif condition == "changed_recovery":
        report["recovery"] = {}
    elif condition == "missing_pin":
        report.pop("enrichment_gameplan")
    else:
        state["steps"]["model_review"]["status"] = "FAILED"
        workflow._write(day / "state.json", state)
    workflow._write(native / "stage-report.json", report)
    receipt = workflow._json(native / "receipt.json")
    receipt["stage_report_checksum_sha256"] = file_checksum(native / "stage-report.json")
    workflow._write(native / "receipt.json", receipt)
    with pytest.raises(ValueError, match="completed numerical"):
        prepare_tail(tail_example)


def test_tail_mode_cannot_retain_models_when_training_source_changes(tail_example):
    (config, candidate, check, _), _, _ = tail_example
    with pytest.raises(ValueError, match="scope"):
        repair.prepare(config, action_date="2026-10-09", repair_id="invalid-tail-001",
            candidate=candidate, paths=[NAME], completion_record="record", checks=[check],
            reviewed=True, now=NOW, retain_completed_preparation=True)


def test_tail_rechecks_immutable_gameplan_before_installing(tail_example, monkeypatch):
    from ml import overnight_runtime
    (config, _, _, _), name, _ = tail_example
    spec = prepare_tail(tail_example)
    def reject(*args, **kwargs):
        raise ValueError("Pinned stock training publication receipt changed")
    monkeypatch.setattr(overnight_runtime, "_pin_stock_gameplan", reject)
    with pytest.raises(ValueError, match="receipt changed"):
        repair.apply(config, spec, reviewed=True, now=NOW)
    assert (Path(config["repository"]) / name).read_text() == "old planner"
