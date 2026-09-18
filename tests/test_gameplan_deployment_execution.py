"""Deployment identity checks with saved fixtures and synthetic brokers only."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, _decisions, _fixed_signals, _owned_allocation, ACCOUNT
from test_gameplan_direction_runtime import prepare
from ml.stock_trader import gameplan_execution, independent_signals, independent_session, independent_runtime as runtime
from ml.stock_trader.horizon_ledger import HorizonLedger
from ml.stock_trader.sizing_policy import FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY


def _registered_deployment(root, monkeypatch, *, status="ACTIVE"):
    from ml import gameplan_deployment as deployment
    og, yg = (root / "ml/nightly-gameplan-runs" / name for name in ("OG", "YG"))
    for run in (og, yg):
        run.mkdir(parents=True)
        (run / "receipt.json").write_text(json.dumps({"action_date": "2026-09-08", "name": run.name}))
    monkeypatch.setattr(deployment, "_before_open", lambda action_date: pd.Timestamp("2026-09-08T09:00:00Z"))
    report = dict(schema_version=deployment.VERSION, action_date="2026-09-08", status=status,
                  OG=deployment._ref(root, og), YG=deployment._ref(root, yg) if status == "ACTIVE" else None)
    saved = deployment._publish(root, report, inputs=[og / "receipt.json", yg / "receipt.json"])
    return og, yg, saved


def test_unregistered_deployment_retains_legacy_minimal_reader_contract(tmp_path):
    from ml.gameplan_deployment import assert_execution_gameplan
    assert_execution_gameplan(tmp_path, None, action_date="2026-09-08")
    assert_execution_gameplan(tmp_path, tmp_path / "missing", action_date="2026-09-08")


@pytest.mark.parametrize("status", ["PREPARING", "ACTIVE"])
def test_native_deployment_guard_requires_exact_active_source(tmp_path, monkeypatch, status):
    from ml.gameplan_deployment import assert_execution_gameplan
    og, yg, _ = _registered_deployment(tmp_path, monkeypatch, status=status)
    for candidate in (og, None):
        with pytest.raises(ValueError):
            assert_execution_gameplan(tmp_path, candidate, action_date="2026-09-08")
    if status == "ACTIVE":
        assert_execution_gameplan(tmp_path, yg, action_date="2026-09-08")
        assert_execution_gameplan(tmp_path, SimpleNamespace(run_directory=yg), action_date="2026-09-08")
    else:
        with pytest.raises(ValueError, match="NOT_ACTIVE"):
            assert_execution_gameplan(tmp_path, yg, action_date="2026-09-08")
    # The explicit date limits this operator-selected deployment to its session.
    assert_execution_gameplan(tmp_path, og, action_date="2026-09-09")


@pytest.mark.parametrize("tamper", ["source_receipt", "deployment_report"])
def test_native_deployment_guard_rejects_changed_immutable_evidence(tmp_path, monkeypatch, tamper):
    from ml.gameplan_deployment import assert_execution_gameplan
    _, yg, saved = _registered_deployment(tmp_path, monkeypatch)
    path = yg / "receipt.json" if tamper == "source_receipt" else saved / "deployment.json"
    payload = json.loads(path.read_text())
    payload["changed"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises((ValueError, RuntimeError)):
        assert_execution_gameplan(tmp_path, yg, action_date="2026-09-08")


def _manual_plan(root, name):
    run = root / "ml/nightly-gameplan-runs" / name
    run.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(id=name + ":AAPL:1h", symbol="AAPL", model_group="1h", direction="BULLISH",
        calibrated_probability=.6, target_window_start="2026-09-08T11:00:00Z",
        target_window_end="2026-09-08T12:00:00Z", execution_eligible=True,
        action_date="2026-09-08", frozen_at="2026-09-08T09:00:00Z")]).to_parquet(run / "forecasts.parquet")
    pointer = root / "ml/nightly-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"current": {"run_path": run.relative_to(root).as_posix()}}))
    return run


def test_manual_reader_and_preflight_reject_og_then_accept_pinned_yg(tmp_path, monkeypatch):
    og = _manual_plan(tmp_path, "OG")
    yg = tmp_path / "ml/nightly-gameplan-runs/YG"
    seen = []
    def guard(root, publication, *, action_date):
        seen.append((Path(publication), action_date))
        if Path(publication) != yg:
            raise ValueError("Registered deployment requires YG")
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan", guard)
    with pytest.raises(ValueError, match="requires YG"):
        gameplan_execution.load_execution_signals(tmp_path, as_of="2026-09-08T11:01:00Z")
    assert gameplan_execution.execution_preflight(tmp_path, action_date=pd.Timestamp("2026-09-08").date())["status"] == "NOT_READY"
    _manual_plan(tmp_path, "YG")
    signals, _ = gameplan_execution.load_execution_signals(tmp_path, as_of="2026-09-08T11:01:00Z")
    assert signals["AAPL", "1h"].source_fingerprint == "YG"
    assert seen == [(og, "2026-09-08"), (og, "2026-09-08"), (yg, "2026-09-08")]


def test_fixed_reader_and_preflight_check_deployment_before_model_consumption(tmp_path, monkeypatch):
    publication = SimpleNamespace(run_directory=tmp_path / "ml/nightly-gameplan-runs/OG")
    calls = []
    def guard(root, loaded, *, action_date):
        assert loaded is publication
        calls.append(action_date)
        raise ValueError("Registered deployment requires YG")
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan", guard)
    monkeypatch.setattr(independent_signals, "read_current_gameplan", lambda root: publication)
    monkeypatch.setattr(independent_session, "read_current_gameplan", lambda root: publication)
    with pytest.raises(ValueError, match="requires YG"):
        independent_signals.load_current_independent_gameplan_signals(
            tmp_path, as_of="2026-09-08T11:01:00Z", require_promoted_model_reports=True)
    result = independent_session._independent_forecast_preflight(tmp_path, action_date=pd.Timestamp("2026-09-08").date())
    assert result["status"] == "NOT_READY" and "requires YG" in result["error"]
    assert calls == ["2026-09-08", "2026-09-08"]


def test_quote_recovery_cannot_fall_back_to_og(tmp_path, monkeypatch):
    from ml.stock_trader import quote_recovery
    publication = SimpleNamespace(run_directory=tmp_path / "ml/nightly-gameplan-runs/OG")
    monkeypatch.setattr(independent_signals, "read_current_gameplan", lambda root: publication)
    def guard(root, loaded, *, action_date):
        assert loaded is publication and action_date == "2026-09-08"
        raise ValueError("Registered deployment requires YG")
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan", guard)
    decision = dict(symbol="AAPL", decision_reason_code="CURRENT_QUOTE_TOO_OLD", quantity=0,
                    order_payload=None, decision_id="skipped", prediction={"primary_horizon": "1h"})
    monkeypatch.setattr(quote_recovery, "read_decision_run", lambda *a: (
        {"decisions": [decision]}, {"decided_at": "2026-09-08T11:01:00Z"}))
    with pytest.raises(ValueError, match="requires YG"):
        quote_recovery.load_quote_recovery(tmp_path, "20260908T110100.000000Z", "AAPL", as_of="2026-09-08T11:30:00Z")
    assert not (tmp_path / "state/independent-stock-trader/quote-recovery-slots").exists()


@pytest.mark.parametrize("policy", [FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY])
def test_unapproved_source_stops_before_broker_reads_or_entry_slot_claim(environment, monkeypatch, policy):
    env = environment
    env.signals = _fixed_signals()
    def reject(*args, **kwargs):
        raise ValueError("Registered deployment requires YG")
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan", reject)
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=policy)
    assert result.status == "INDEPENDENT_TARGET_PLAN_UNAVAILABLE"
    assert result.submitted_orders == env.captures == 0 and "requires YG" in result.error
    assert env.broker.calls == []
    assert not (env.root / "state/independent-stock-trader/entry-slots").exists()
    assert not (env.root / runtime.LEDGER_RELATIVE_PATH).exists()


@pytest.mark.parametrize("policy", [FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY])
@pytest.mark.parametrize("switch_at", ["capture", "before_post"])
def test_loaded_og_is_rechecked_after_capture_and_before_post(environment, monkeypatch, policy, switch_at):
    env = environment
    if policy == GAMEPLAN_SIZING_POLICY:
        prepare(env, monkeypatch, probability=.7)
    else:
        env.signals = _fixed_signals()
    og = env.root / "ml/nightly-gameplan-runs/OG"
    og.mkdir(parents=True)
    (og / "receipt.json").write_text("{}")
    env.signals = {("AAPL", "1h"): replace(env.signals["AAPL", "1h"], source_fingerprint="OG" if policy == GAMEPLAN_SIZING_POLICY else "immutable-source-hash")}
    sources = () if policy == GAMEPLAN_SIZING_POLICY else (og / "receipt.json",)
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", lambda *a, **kw: (env.signals, sources))
    deployment = {"yg_required": False}
    seen = []
    def guard(root, loaded, *, action_date):
        assert loaded == og and action_date == "2026-09-08"
        seen.append(loaded)
        if deployment["yg_required"]:
            raise ValueError("Registered deployment requires YG")
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan", guard)
    def switch():
        deployment["yg_required"] = True
        # A newer current pointer must never replace the loaded OG identity.
        pointer = env.root / "ml/nightly-gameplan-latest/run.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"current": {"run_path": "ml/nightly-gameplan-runs/YG"}}))
    if switch_at == "capture":
        env.after_capture = switch
    else:
        env.broker.before_post_hook = switch
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=policy)
    assert env.broker.submissions == [] and result.submitted_orders == 0
    assert len(seen) >= 2
    assert _decisions(result)["prediction_handoff"]["source_gameplan_run"] == "ml/nightly-gameplan-runs/OG"
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot()
    if switch_at == "capture":
        assert "requires YG" in _decisions(result)["prediction_handoff"]["entry_input_error"]
        assert result.status == "INDEPENDENT_TARGET_PLAN_UNAVAILABLE" and "requires YG" in result.error
        assert not ledger.reservations
    else:
        assert "GAMEPLAN_DEPLOYMENT_NOT_APPROVED" in result.error
        assert all(r.status == "REJECTED" for r in ledger.reservations)


def test_deployment_rejection_does_not_block_existing_owned_exit(environment, monkeypatch):
    env = environment
    ledger = _owned_allocation(env, manual=0)
    env.now = pd.Timestamp("2026-09-08T12:00:01Z")
    def reject(*args, **kwargs):
        raise gameplan_execution.GameplanDeploymentUnavailable("Registered deployment requires YG")
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", reject)
    monkeypatch.setattr("ml.gameplan_deployment.assert_execution_gameplan", lambda *a, **kw: pytest.fail("Owned exit is independent of entry deployment"))
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=FIXED_SIZING_POLICY)
    assert result.submitted_orders == 1
    assert result.status == "OWNED_EXITS_SUBMITTED_WITH_ENTRY_PLAN_UNAVAILABLE"
    assert "requires YG" in result.error
    assert env.broker.submissions[0]["orderLegCollection"][0]["instruction"] == "SELL"
    assert ledger.snapshot().allocations[0].reserved_sell_shares == 10
