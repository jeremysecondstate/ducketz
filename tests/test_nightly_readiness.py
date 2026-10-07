"""Readiness reads saved bytes and never starts operational work."""
from copy import deepcopy
from pathlib import Path

import pytest

from ml import nightly_readiness as module
from ml.artifacts import file_checksum


@pytest.fixture
def saved(monkeypatch, tmp_path):
    config = {"actor": "Atlas", "repository": str(tmp_path)}
    artifact = tmp_path / "completed.json"
    artifact.write_text("frozen")
    state = {"schema_version": module.workflow.VERSION, "actor": "Atlas", "action_date": "2026-10-06",
             "source_session": "2026-10-05", "source_identity": {"commit": "reviewed"},
             "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING", "run_id": "saved-id", "completed_at": "2026-10-06T10:00:00Z",
             "steps": {step: {"status": "COMPLETE", "output": {"files": {str(artifact): file_checksum(artifact)}}}
                       for step in module.workflow.STEPS}}
    state["steps"]["local_handoff"]["output"]["delivery"] = "HELD_FOR_SEPARATE_PC_SETUP"
    monkeypatch.setattr(module.workflow, "verify_installation", lambda config: None)
    monkeypatch.setattr(module.workflow, "status", lambda config: deepcopy(state))
    monkeypatch.setattr(module.workflow, "_verify_configuration_binding", lambda *args: None)
    monkeypatch.setattr(module.workflow, "_verify_symbol_binding", lambda *args: None)
    monkeypatch.setattr(module.workflow, "source_identity", lambda root: {"commit": "reviewed"})
    monkeypatch.setattr(module.workflow, "_display", lambda *args: deepcopy(state["steps"]["verify_display"]["output"]))
    return config, state, artifact


def test_verified_local_completion_keeps_peer_and_trader_unready(saved):
    config, state, artifact = saved
    before = artifact.read_bytes()
    result = module.readiness(config, now="2026-10-06T10:35:00Z")
    assert result["local_ready"] and result["action_date"] == "2026-10-06"
    assert not result["joint_ready"] and not result["execution_authorized"]
    assert artifact.read_bytes() == before


def test_previous_success_cannot_satisfy_today(saved):
    config, state, _ = saved
    state["action_date"] = "2026-10-05"
    result = module.readiness(config, now="2026-10-06T10:35:00Z")
    assert result["status"] == "NOT_READY" and not result["local_ready"]


def test_output_changed_after_completion_fails(saved):
    config, _, artifact = saved
    artifact.write_text("later edit")
    with pytest.raises(ValueError, match="output changed"):
        module.readiness(config, now="2026-10-06T10:35:00Z")


def test_changed_default_ui_selection_fails(saved, monkeypatch):
    config, _, _ = saved
    monkeypatch.setattr(module.workflow, "_display", lambda *args: {"files": {}})
    with pytest.raises(ValueError, match="UI selection"):
        module.readiness(config, now="2026-10-06T10:35:00Z")


def test_failed_training_retains_actual_failure(saved):
    config, state, _ = saved
    state["status"] = "FAILED"
    state["steps"]["train_and_plan"].update(status="FAILED", error="fixture model assessment failed")
    result = module.readiness(config, now="2026-10-06T10:35:00Z")
    assert result["step"] == "train_and_plan" and "assessment failed" in result["reason"]


@pytest.mark.parametrize("stamp", ["2026-10-10T10:35:00Z", "2026-12-25T11:35:00Z"])
def test_closed_session_never_claims_readiness(saved, stamp):
    result = module.readiness(saved[0], now=stamp)
    assert result["status"] == "NOOP_NON_SESSION_DATE" and not result["local_ready"]


def test_evening_selects_next_exchange_session(saved):
    result = module.readiness(saved[0], now="2026-10-10T04:05:00Z")
    assert result["action_date"] == "2026-10-12" and result["status"] == "NOT_READY"
