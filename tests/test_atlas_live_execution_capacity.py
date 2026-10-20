"""Fresh Gameplan execution uses original forecasts and synthetic live capacity."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ml import gameplan_stock_trader as cli
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY, LEARNED_SIZING_POLICY
from test_independent_stock_runtime import environment, _decisions
from test_gameplan_direction_runtime import prepare
from test_gameplan_direction_engine import inputs, build


@pytest.mark.parametrize("kind", ["accepted_current", "accepted_historical", "native_directional"])
@pytest.mark.parametrize("probability,side", [(.5001, "BUY"), (.4999, "SELL")])
def test_fresh_runtime_keeps_original_probabilities_and_ids_without_projected_catchup(environment, monkeypatch, kind, probability, side):
    from ml.stock_trader import catchup
    env = environment
    prepare(env, monkeypatch, probability=probability)
    synthetic_capture = runtime.capture_portfolio_state
    def capture(*args, symbols=None, **kwargs):
        return synthetic_capture(*args, **kwargs)
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    key = ("AAPL", "1h")
    original = replace(env.signals[key], prediction_id="original-probability-and-target",
                       planned_quantity=1)
    env.signals = {key: original}
    env.held = {symbol: 20. for symbol in env.held}
    if kind.startswith("accepted"):
        source = env.root / "ml/joint-gameplan-runs/selected"
        source.mkdir(parents=True)
        sources = (source / "accepted-plan.json", source / "joint-plan.json")
    else:
        source = env.root / "ml/nightly-gameplan-runs/scientific-source"
        source.mkdir(parents=True)
        derived = env.root / "ml/gameplan-trade-plan-runs/current-directions"
        derived.mkdir(parents=True)
        sources = (source / "receipt.json", derived / "receipt.json")
    for path in sources:
        path.write_text('{"plan_sha256":"' + "a" * 64 + '"}' if path.name == "accepted-plan.json" else "{}")
    before = {path: path.read_bytes() for path in sources}

    def forbidden(*args, **kwargs):
        pytest.fail("Fresh execution must not reconstruct historical projected fill intentions")

    monkeypatch.setattr(catchup, "catchup_signals", forbidden)
    monkeypatch.setattr(catchup, "native_catchup_plan", forbidden)
    monkeypatch.setattr("ml.stock_trader.price_comparison.attach_price_comparisons", forbidden)
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", lambda *a, **k: (env.signals, sources))
    monkeypatch.setattr(runtime, "_loaded_gameplan_run", lambda *a, **k: source)
    monkeypatch.setattr(runtime, "_loaded_fallback_policy", lambda *a, **k: (None, None))
    monkeypatch.setattr(runtime, "_assert_execution_deployment", lambda *a, **k: None)
    monkeypatch.setattr("ml.joint_capital_adoption.assert_accepted_execution", lambda *a, **k: False)
    result = runtime.run_independent_stock_trader_once(env.root, execute=True,
        session=env.broker, runtime_clock=lambda: env.now,
        session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == 1
    decision, = _decisions(result)["decisions"]
    assert decision["action"] == side and decision["quantity"] > 1
    assert decision["prediction"]["calibrated_probability"] == probability
    assert decision["prediction"]["prediction_id"] == original.prediction_id
    assert decision["prediction"]["target_definition_version"] == original.target_definition_version
    assert decision["prediction"]["target_window_start"] == original.target_window_start
    assert decision["prediction"]["target_window_end"] == original.target_window_end
    assert decision["prediction"]["planned_quantity"] is None
    assert decision["prediction"]["catchup_components"] == []
    assert "price_comparison" not in decision["enrichment"]
    audit = _decisions(result)["prediction_handoff"]["direction_only_execution"]
    assert audit == {"plan_sha256": "a" * 64 if kind.startswith("accepted") else None,
        "policy": "current-due-direction-live-broker-capacity-v1",
        "forecast_ids": [original.prediction_id], "planned_quantities": False}
    assert all(path.read_bytes() == raw for path, raw in before.items())
    # The genuine original forecast ID, not a derived catch-up ID, deduplicates
    # another poll against this trader's own submitted reservation.
    again = runtime.run_independent_stock_trader_once(env.root, execute=True,
        session=env.broker, runtime_clock=lambda: env.now,
        session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY)
    assert again.submitted_orders == 0 and len(env.broker.submissions) == 1


@pytest.mark.parametrize("probability,side", [(.5001, "BUY"), (.4999, "SELL")])
@pytest.mark.parametrize("old_quantity", [0, 1, 100000])
def test_direction_engine_uses_live_capacity_instead_of_saved_quantity_cap(probability, side, old_quantity):
    signals, portfolio = inputs(probability=probability)
    key = ("AAPL", "1h")
    original = signals[key]
    signals = {key: replace(original, planned_quantity=old_quantity)}
    portfolio = replace(portfolio, held_shares={"AAPL": 20.}, symbol_exposure={"AAPL": 2000.}, gross_exposure=2000.)
    decision, = build(signals, portfolio, bearish_sell_capacities={key: 20})
    assert decision.action == side and decision.quantity > 1
    assert decision.prediction["calibrated_probability"] == probability
    assert decision.prediction["target_definition_version"] == original.target_definition_version
    assert decision.prediction["prediction_id"] == original.prediction_id


def test_archived_synthetic_catchup_cannot_become_a_fresh_forecast():
    signals, portfolio = inputs(probability=1.)
    key = ("AAPL", "1h")
    signals = {key: replace(signals[key], target_definition_version="accepted-gameplan-catchup-v1",
                            catchup_components=("old-projected-event",), planned_quantity=1)}
    decision, = build(signals, portfolio)
    assert decision.quantity == 0 and decision.decision_reason_code == "LEGACY_PROJECTED_INTENTION"
    assert decision.prediction["calibrated_probability"] == 1.
    assert decision.prediction["target_definition_version"] == "accepted-gameplan-catchup-v1"


def test_combined_direction_plan_keeps_saved_longer_horizon_ownership_policy(tmp_path, monkeypatch):
    from ml.artifacts import file_checksum
    from ml.stock_trader.cross_horizon_fallback import policy_for_action_date
    source = tmp_path / "ml/joint-gameplan-runs/accepted-original"
    source.mkdir(parents=True)
    receipt = source / "accepted-plan.json"
    receipt.write_bytes(b'{"original-acceptance-receipt":true}')
    policy = policy_for_action_date("2026-10-20")
    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan", lambda *a: (
        {"cross_horizon_fallback_policy": policy}, {}, source))
    actual, binding = runtime._loaded_fallback_policy(tmp_path, source, "2026-10-20")
    assert actual == policy
    assert binding == {"source_run": source.relative_to(tmp_path).as_posix(),
                       "source_receipt_sha256": file_checksum(receipt)}
    assert receipt.read_bytes() == b'{"original-acceptance-receipt":true}'


def test_cli_and_public_function_default_to_all_horizons_and_live_capacity(tmp_path, monkeypatch):
    options = cli._parser().parse_args(["--root-dir", str(tmp_path)])
    assert options.target_horizon == "all" and options.sizing_policy == GAMEPLAN_SIZING_POLICY
    calls = []
    monkeypatch.setattr(runtime, "run_independent_stock_trader_once",
        lambda root, **kwargs: calls.append((root, kwargs)) or "synthetic-result")
    assert cli.run_gameplan_stock_trader_once(tmp_path) == "synthetic-result"
    assert calls == [(tmp_path, {"decided_at": None, "execute": False, "session": None,
        "runtime_clock": None, "sizing_policy": GAMEPLAN_SIZING_POLICY})]


def test_cli_retains_explicit_legacy_selection_after_default_change(tmp_path, monkeypatch):
    calls = []
    result = SimpleNamespace(status="DRY_RUN", to_dict=lambda: {"status": "DRY_RUN"})
    monkeypatch.setattr(cli, "run_gameplan_stock_trader_once",
        lambda root, **kwargs: calls.append(kwargs) or result)
    assert cli.main(["--root-dir", str(tmp_path), "--sizing-policy", LEARNED_SIZING_POLICY]) == 0
    assert calls[0]["sizing_policy"] == LEARNED_SIZING_POLICY
    assert calls[0]["target_horizon"] == "all" and calls[0]["execute"] is False


@pytest.mark.parametrize("requested", ["qualified-enrichment", "fixed-horizon-budget-v1"])
@pytest.mark.parametrize("probability,side", [(.5001, "BUY"), (.48, "SELL")])
def test_current_legacy_selection_uses_original_probability_direction_engine(environment, monkeypatch, requested, probability, side):
    import pandas as pd
    env = environment
    prepare(env, monkeypatch, probability=probability)
    old_now = env.now
    env.now = pd.Timestamp("2026-10-20T11:01:00Z")
    shift = env.now - old_now
    key = ("AAPL", "1h")
    original = env.signals[key]
    original = replace(original, prediction_id="original-current-research-probability",
        decision_timestamp=(pd.Timestamp(original.decision_timestamp) + shift).isoformat(),
        prediction_created_at=(pd.Timestamp(original.prediction_created_at) + shift).isoformat(),
        target_window_start=(pd.Timestamp(original.target_window_start) + shift).isoformat(),
        target_window_end=(pd.Timestamp(original.target_window_end) + shift).isoformat(),
        actionable_until=(pd.Timestamp(original.actionable_until) + shift).isoformat())
    env.signals = {key: original}
    env.held = {symbol: 20. for symbol in env.held}
    original_capture = runtime.capture_portfolio_state
    monkeypatch.setattr(runtime, "capture_portfolio_state",
        lambda *args, symbols=None, **kwargs: original_capture(*args, **kwargs))
    calls = []
    def load(root, **kwargs):
        calls.append(kwargs)
        assert kwargs == {"as_of": env.now, "execution_ready_plan": True}
        return env.signals, ()
    monkeypatch.setattr(runtime, "load_current_independent_gameplan_signals", load)
    monkeypatch.setattr(runtime, "load_current_enrichment_model",
        lambda *a: pytest.fail("A historical sizing selection must not impose model qualification on a current instruction"))
    # This synthetic publication has no saved longer-horizon fallback policy.
    monkeypatch.setattr(runtime, "_loaded_fallback_policy", lambda *a, **k: (None, None))
    result = runtime.run_independent_stock_trader_once(env.root, execute=True,
        session=env.broker, runtime_clock=lambda: env.now, session_managed=True,
        sizing_policy=requested)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == 1 and len(calls) == 1
    saved = _decisions(result)
    decision, = saved["decisions"]
    assert decision["action"] == side and decision["quantity"] > 0
    assert decision["prediction"]["calibrated_probability"] == probability
    assert decision["prediction"]["prediction_id"] == original.prediction_id
    assert decision["prediction"]["target_window_end"] == original.target_window_end
    assert saved["prediction_handoff"]["requested_sizing_policy"] == requested
    assert saved["prediction_handoff"]["sizing_policy"] == GAMEPLAN_SIZING_POLICY
    assert decision["prediction"]["sizing_policy"] == GAMEPLAN_SIZING_POLICY


@pytest.mark.parametrize("requested", ["qualified-enrichment", "fixed-horizon-budget-v1", GAMEPLAN_SIZING_POLICY])
def test_effective_policy_is_dated_without_relabelling_historical_selection(requested):
    from datetime import date
    from ml.stock_trader.sizing_policy import effective_sizing_policy
    assert effective_sizing_policy(requested, date(2026, 10, 18)) == requested
    assert effective_sizing_policy(requested, "2026-10-19") == GAMEPLAN_SIZING_POLICY
    assert effective_sizing_policy(requested, "2026-10-20") == GAMEPLAN_SIZING_POLICY


@pytest.mark.parametrize("requested", ["qualified-enrichment", "fixed-horizon-budget-v1"])
def test_current_session_preflight_uses_direction_policy_and_preserves_requested_choice(tmp_path, monkeypatch, requested):
    import json
    import pandas as pd
    from ml.stock_trader import independent_session as worker
    from test_independent_stock_session import Clock
    clock = Clock("2026-10-20T11:01:00Z")
    active = {"value": True}
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent",
        lambda root: SimpleNamespace(active=active["value"]))
    monkeypatch.setattr(worker, "_has_inventory", lambda path: False)
    def forbidden(*a, **k):
        pytest.fail("Current session must not require learned or promoted-model preflight")
    monkeypatch.setattr(worker, "_independent_forecast_preflight", forbidden)
    monkeypatch.setattr(worker, "_independent_enrichment_preflight", forbidden)
    checks = []
    monkeypatch.setattr("ml.stock_trader.gameplan_execution.execution_preflight",
        lambda root, **kwargs: checks.append(kwargs) or {"status": "READY"})
    calls = []
    def runner(root, **kwargs):
        calls.append(kwargs)
        active["value"] = False
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: {"status": "SYNTHETIC_NO_ORDERS"})
    result = worker.run_independent_stock_session(tmp_path, execute=False,
        clock=clock, sleep=clock.sleep, runner=runner, reporter=lambda message: None,
        sizing_policy=requested)
    assert result["status"] == "SESSION_STOPPED_TRADER_INACTIVE"
    assert checks == [{"action_date": pd.Timestamp("2026-10-20").date()}]
    assert len(calls) == 1 and calls[0].get("sizing_policy", LEARNED_SIZING_POLICY) == requested
    saved = json.loads((tmp_path / "state/independent-stock-trader/session-status.json").read_text())
    assert saved["requested_sizing_policy"] == requested
    assert saved["sizing_policy"] == GAMEPLAN_SIZING_POLICY
    assert saved["calls"] == 1 and saved["orders_submitted"] == 0


def test_historical_single_horizon_wrapper_retains_its_original_engine(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_stock_trader_once",
        lambda *a, **kwargs: calls.append(kwargs) or "historical-result")
    assert cli.run_gameplan_stock_trader_once(tmp_path, decided_at="2026-10-18T16:00:00Z",
        target_horizon="1h", sizing_policy=LEARNED_SIZING_POLICY) == "historical-result"
    assert calls[0]["target_horizon"] == "1h"


@pytest.mark.parametrize("requested", ["qualified-enrichment", "fixed-horizon-budget-v1"])
def test_current_single_horizon_wrapper_cannot_bypass_permanent_direction_policy(tmp_path, monkeypatch, requested):
    monkeypatch.setattr(cli, "run_stock_trader_once",
        lambda *a, **k: pytest.fail("Current execution cannot enter the legacy confidence engine"))
    with pytest.raises(ValueError, match="require --target-horizon all"):
        cli.run_gameplan_stock_trader_once(tmp_path, decided_at="2026-10-20T16:00:00Z",
            target_horizon="1h", sizing_policy=requested)
