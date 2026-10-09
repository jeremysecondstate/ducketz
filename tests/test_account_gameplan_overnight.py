import json
import pytest

from ml.overnight_runtime import ACCOUNT_GAMEPLAN_STAGE, ACCOUNT_REVIEW_STAGE, overnight_status
from test_overnight_supervision import run, _synthetic_gameplan_pin
from test_account_gameplan_config import write_config


def setup(monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._pin_stock_gameplan", _synthetic_gameplan_pin)
    monkeypatch.setattr("ml.overnight_runtime._pin_account_trade_plan", lambda *a, **k:
                        {"run_path": "ml/gameplan-trade-plan-runs/frozen", "receipt_sha256": "a" * 64})


def test_configured_fresh_pipeline_coordinates_before_actuals(tmp_path, monkeypatch):
    setup(monkeypatch)
    write_config(tmp_path)
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kw: calls.append(command) or 0)
    path = run(tmp_path, start_at="gameplan_publication", stock_only=True, independent_stock_horizons=True)
    report = json.loads((path / "stage-report.json").read_text())
    assert report["stage_order"] == ["gameplan_publication", "stock_enrichment_training",
        "gameplan_trade_planning", ACCOUNT_GAMEPLAN_STAGE, "gameplan_actuals_review", ACCOUNT_REVIEW_STAGE]
    cmd = next(command for command in calls if "ml.account_gameplan.preparation" in command)
    assert cmd[cmd.index("--deadline") + 1] == report["deadline_at"]
    assert cmd[cmd.index("--expected-config") + 1] == report["account_config_sha256"]
    assert cmd[cmd.index("--trade-plan-run") + 1].endswith("gameplan-trade-plan-runs\\frozen")
    local_plan = next(command for command in calls if "ml.gameplan_trade_planning" in command)
    assert "--account-producer-only" in local_plan


def test_resuming_old_tail_never_adopts_new_account_stage(tmp_path, monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda cmd, **kw:
                        3 if "ml.gameplan_trade_planning" in cmd else 0)
    with pytest.raises(RuntimeError):
        run(tmp_path, start_at="gameplan_publication", stock_only=True, independent_stock_horizons=True)
    prior = overnight_status(tmp_path)["run_path"]
    write_config(tmp_path)
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda cmd, **kw: calls.append(cmd) or 0)
    path = run(tmp_path, resume_run=prior)
    report = json.loads((path / "stage-report.json").read_text())
    assert report["stage_order"] == ["gameplan_trade_planning", "gameplan_actuals_review"]
    assert all("ml.account_gameplan.preparation" not in cmd for cmd in calls)
    assert all("--account-producer-only" not in cmd for cmd in calls)


def test_account_tail_resume_keeps_sources_config_and_successful_upstream(tmp_path, monkeypatch):
    setup(monkeypatch)
    write_config(tmp_path)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda cmd, **kw:
                        4 if "ml.account_gameplan.preparation" in cmd else 0)
    with pytest.raises(RuntimeError):
        run(tmp_path, start_at="gameplan_publication", stock_only=True, independent_stock_horizons=True)
    prior = overnight_status(tmp_path)
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda cmd, **kw: calls.append(cmd) or 0)
    path = run(tmp_path, resume_run=prior["run_path"])
    report = json.loads((path / "stage-report.json").read_text())
    assert report["stage_order"] == [ACCOUNT_GAMEPLAN_STAGE, "gameplan_actuals_review", ACCOUNT_REVIEW_STAGE]
    assert report["deadline_at"] == prior["deadline_at"]
    assert report["account_trade_plan"] == prior["account_trade_plan"]
    assert report["enrichment_gameplan"] == prior["enrichment_gameplan"]
    write_config(tmp_path, machine="pc-new")
    with pytest.raises(ValueError, match="saved combined-account"):
        run(tmp_path, resume_run=prior["run_path"])


def test_missing_peer_accuracy_resumes_only_final_summary(tmp_path, monkeypatch):
    setup(monkeypatch)
    write_config(tmp_path)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda cmd, **kw:
                        2 if "ml.account_gameplan.review" in cmd else 0)
    with pytest.raises(RuntimeError):
        run(tmp_path, start_at="gameplan_publication", stock_only=True, independent_stock_horizons=True)
    prior = overnight_status(tmp_path)
    calls = []
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda cmd, **kw: calls.append(cmd) or 0)
    path = run(tmp_path, resume_run=prior["run_path"])
    report = json.loads((path / "stage-report.json").read_text())
    assert report["stage_order"] == [ACCOUNT_REVIEW_STAGE]
    assert len(calls) == 1 and "ml.account_gameplan.review" in calls[0]
    assert report["deadline_at"] == prior["deadline_at"]
    assert report["enrichment_gameplan"] == prior["enrichment_gameplan"]
