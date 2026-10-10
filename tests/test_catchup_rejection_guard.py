"""Confirmed broker rejection must not manufacture repeated trade intentions."""
from dataclasses import replace

import pandas as pd
import pytest

from ml.stock_trader.catchup import catchup_signals
from ml.stock_trader.horizon_ledger import HorizonLedger, ReservationState, OrderEvidence, FillEvidence
from test_gameplan_catchup import NOW, plan
from test_independent_stock_runtime import environment, ACCOUNT, _decisions
from test_gameplan_direction_runtime import prepare, run
from ml.stock_trader import independent_runtime as runtime
from test_account_gameplan_execution import account_env, run as account_run, state as account_state, native


def working_order(*, side="SELL", price="100.00", symbol="AAPL", remaining=1, identity="manual-working"):
    return {"order_id": identity, "instruction": side, "asset_type": "EQUITY", "symbol": symbol,
        "remaining_quantity": remaining, "filled_quantity": 0, "limit_price": price,
        "reserved_cash": "0", "status": "CURRENT"}


def test_account_manual_opposing_same_price_order_defers_only_conflicting_intention(account_env):
    env = account_env
    row = working_order()
    env.held["AAPL"] = 3.
    env.pending_sell["AAPL"] = 1.
    env.portfolio_transform = lambda p: replace(p, broker_working_orders=(row,), working_order_count=1)
    result = account_run(env)
    assert result.status == "ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT", result.error
    assert result.submitted_orders == 1
    assert [p["orderLegCollection"][0]["instrument"]["symbol"] for p in env.broker.submissions] == ["NVDA"]
    assert env.broker.cancellations == []
    assert {r.symbol for r in native(env).snapshot().reservations} == {"NVDA"}
    assert {r["request"]["symbol"] for r in account_state(env)["reservations"]} == {"NVDA"}
    saved = _decisions(result)
    deferred = next(d for d in saved["decisions"] if d["symbol"] == "AAPL")
    assert deferred["quantity"] == 0 and deferred["order_payload"] is None
    assert deferred["prediction"]["prediction_id"] == env.signals["AAPL", "1h"].prediction_id
    assert deferred["decision_reason_code"] == "OPPOSING_SAME_PRICE_WORKING_ORDER"
    assert saved["prediction_handoff"]["self_trade_prevention"]["deferred"][0]["conflicts"][0]["order_id"] == row["order_id"]


@pytest.mark.parametrize("side,price,blocked", [("SELL", "100.000", True), ("SELL", "100.01", False),
                                                ("BUY", "100.00", False)])
def test_account_conflict_uses_exact_decimal_price_side_and_partial_remaining(account_env, side, price, blocked):
    env = account_env
    row = {**working_order(side=side, price=price), "filled_quantity": 2}
    env.held["AAPL"] = 3.
    env.pending_sell["AAPL"] = 1. if side == "SELL" else 0.
    env.portfolio_transform = lambda p: replace(p, broker_working_orders=(row,), working_order_count=1)
    result = account_run(env)
    assert result.submitted_orders == (1 if blocked else 2), result.error
    assert result.status == ("ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT" if blocked else "ORDERS_SUBMITTED")
    assert env.broker.cancellations == []


def test_account_deferred_intention_can_resume_after_current_conflict_disappears(account_env):
    env = account_env
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    row = working_order()
    env.held["AAPL"] = 3.
    env.pending_sell["AAPL"] = 1.
    env.portfolio_transform = lambda p: replace(p, broker_working_orders=(row,), working_order_count=1)
    first = account_run(env)
    assert first.status == "SELF_TRADE_CONFLICT_DEFERRED" and first.submitted_orders == 0
    prior = next(d for d in _decisions(first)["decisions"] if d["symbol"] == "AAPL")
    assert native(env).snapshot().reservations == () and account_state(env)["reservations"] == []
    env.now += pd.Timedelta(seconds=1)
    env.pending_sell.clear()
    env.portfolio_transform = lambda p: p
    second = account_run(env)
    assert second.status == "ORDERS_SUBMITTED" and second.submitted_orders == 1, second.error
    resumed = next(d for d in _decisions(second)["decisions"] if d["symbol"] == "AAPL")
    assert resumed["prediction"]["prediction_id"] == prior["prediction"]["prediction_id"]
    assert resumed["decision_id"] == prior["decision_id"]
    assert env.broker.cancellations == []


def test_account_earlier_sale_in_batch_defers_opposite_horizon_without_netting_owners(account_env):
    env = account_env
    env.held["AAPL"] = 5.
    env.signals["AAPL", "4h"] = replace(env.signals["NVDA", "4h"], symbol="AAPL",
        prediction_id="AAPL:4h:separate-sale", calibrated_probability=.3)
    env.plan.rows = pd.DataFrame([{"id": s.prediction_id, "symbol": s.symbol, "model_group": s.primary_horizon,
        "calibrated_probability": s.calibrated_probability, "target_window_start": s.target_window_start,
        "target_window_end": s.target_window_end,
        "producer_id": "pc-original" if s.symbol == "AAPL" else "pc-new"} for s in env.signals.values()])
    result = account_run(env)
    assert result.status == "ORDERS_SUBMITTED_WITH_SELF_TRADE_CONFLICT", result.error
    assert result.submitted_orders == 2
    legs = [p["orderLegCollection"][0] for p in env.broker.submissions]
    assert [(leg["instrument"]["symbol"], leg["instruction"]) for leg in legs] == [("AAPL", "SELL"), ("NVDA", "BUY")]
    orders = native(env).snapshot().reservations
    assert {(r.symbol, r.horizon, r.side) for r in orders} == {("AAPL", "4h", "SELL"), ("NVDA", "4h", "BUY")}
    deferred = _decisions(result)["prediction_handoff"]["self_trade_prevention"]["deferred"]
    assert deferred[0]["horizon"] == "1h" and deferred[0]["conflicts"][0]["source"] == "EARLIER_BATCH_DECISION"
    assert env.broker.cancellations == []


@pytest.mark.parametrize("kind", ["none", "missing_row", "missing_price", "bad_side", "noncurrent", "zero_remaining"])
def test_native_gameplan_missing_or_invalid_working_evidence_never_means_no_orders(environment, monkeypatch, kind):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original = runtime.capture_portfolio_state
    row = working_order()
    if kind == "missing_price": row["limit_price"] = None
    if kind == "bad_side": row["instruction"] = None
    if kind == "noncurrent": row["status"] = "UNKNOWN"
    if kind == "zero_remaining": row["remaining_quantity"] = 0
    def capture(*args, **kwargs):
        assert kwargs["include_order_identities"] is True
        return replace(original(*args, **kwargs), working_order_count=1,
            broker_working_orders=None if kind == "none" else () if kind == "missing_row" else (row,))
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    result = run(env)
    assert result.status == "WORKING_ORDER_EVIDENCE_UNAVAILABLE", result.error
    assert result.submitted_orders == 0 and env.broker.submissions == env.broker.cancellations == []
    assert HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot().reservations == ()


def test_native_option_order_does_not_become_same_equity_conflict(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original = runtime.capture_portfolio_state
    row = {**working_order(), "asset_type": "OPTION"}
    monkeypatch.setattr(runtime, "capture_portfolio_state", lambda *a, **k:
        replace(original(*a, **k), broker_working_orders=(row,), working_order_count=1))
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED" and result.submitted_orders == 1, result.error
    assert env.broker.cancellations == []


def rejected(signal, *, broker_id="broker-rejected", side="BUY"):
    return ReservationState("rejected-one", "allocation-one", signal.symbol, signal.primary_horizon,
        signal.prediction_id, side, 4, "100", 0, "REJECTED", broker_id, "decision-one", "batch-one", NOW)


def test_confirmed_rejection_blocks_unchanged_family_across_repeated_wakes():
    source = plan(buy=5, sell=1)
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="source")["AAPL", "1h"]
    original = rejected(signal)
    for _ in range(6):
        blocked = []
        assert catchup_signals(source, as_of=NOW, source_fingerprint="source", reservations=[original], blocked=blocked) == {}
        assert blocked == [{"symbol": "AAPL", "horizon": "1h", "direction": "BUY",
            "reason_code": "CONFIRMED_BROKER_REJECTION_REQUIRES_REVIEW", "reservation_id": "rejected-one",
            "observed_at": NOW, "rejected_attempts": 1}]


def test_local_pre_post_rejection_does_not_claim_a_broker_failure():
    source = plan(buy=5, sell=1)
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="source")["AAPL", "1h"]
    blocked = []
    residual = catchup_signals(source, as_of=NOW, source_fingerprint="source",
        reservations=[rejected(signal, broker_id=None)], blocked=blocked)
    assert residual["AAPL", "1h"].planned_quantity == 4
    assert blocked == []


def test_rejected_buy_does_not_block_due_opposite_sale_or_other_family():
    source = plan(buy=5, sell=1)
    first = catchup_signals(source, as_of=NOW, source_fingerprint="source")["AAPL", "1h"]
    order = rejected(first)
    source["ledger"]["events"].append({"forecast_id": "later-sale", "symbol": "AAPL", "horizon": "1h",
        "action": "SELL", "quantity": 6, "timestamp": NOW, "sequence": 3})
    source["ledger"]["events"].append({"forecast_id": "other", "symbol": "COST", "horizon": "1h",
        "action": "BUY", "quantity": 1, "timestamp": NOW, "sequence": 4})
    signals = catchup_signals(source, as_of=NOW, source_fingerprint="source", reservations=[order])
    assert signals["AAPL", "1h"].calibrated_probability == 0
    assert signals["AAPL", "1h"].planned_quantity == 2
    assert signals["COST", "1h"].planned_quantity == 1


def test_historical_component_rejection_blocks_after_catchup_reader_upgrade():
    source = plan(buy=5, sell=1)
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="source")["AAPL", "1h"]
    prior = replace(rejected(signal), forecast_id="opening")
    assert catchup_signals(source, as_of=NOW, source_fingerprint="source", reservations=[prior]) == {}


def test_separate_action_date_does_not_inherit_unrelated_old_rejection_identity():
    source = plan(buy=5, sell=1)
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="source")["AAPL", "1h"]
    source["plan_sha256"] = "b" * 64
    source["action_date"] = "2026-09-09"
    source["as_of"] = "2026-09-09T09:00:00Z"
    for event in source["ledger"]["events"]:
        event["forecast_id"] += "-next-session"
        event["timestamp"] = event["timestamp"].replace("2026-09-08", "2026-09-09")
    assert catchup_signals(source, as_of="2026-09-09T12:15:00Z", source_fingerprint="source", reservations=[rejected(signal)])


@pytest.mark.parametrize("opposite_exit", [False, True])
def test_runtime_restart_retains_rejection_diagnostics_and_reconciles_other_fills(environment, monkeypatch, opposite_exit):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.held = {symbol: 0. for symbol in env.held}
    source = {"action_date": "2026-09-08", "as_of": "2026-09-08T09:00:00Z", "plan_sha256": "a" * 64,
        "ledger": {"events": [
            {"forecast_id": "buy-a", "symbol": "AAPL", "horizon": "1h", "action": "BUY", "quantity": 3,
             "timestamp": "2026-09-08T11:00:00Z", "sequence": 1},
            {"forecast_id": "buy-c", "symbol": "COST", "horizon": "1h", "action": "BUY", "quantity": 3,
             "timestamp": "2026-09-08T11:00:00Z", "sequence": 2}]}}
    if opposite_exit:
        source["ledger"]["events"].append({"forecast_id": "sell-c", "symbol": "COST", "horizon": "1h",
            "action": "SELL", "quantity": 1, "timestamp": "2026-09-08T11:01:30Z", "sequence": 3})
    accepted = env.root / "ml/joint-gameplan-runs/fixture"
    accepted.mkdir(parents=True)
    (accepted / "accepted-plan.json").write_text("{}")
    env.signals = catchup_signals(source, as_of=env.now, source_fingerprint=accepted.name)
    monkeypatch.setattr(runtime, "_loaded_gameplan_run", lambda *a, **k: accepted)
    monkeypatch.setattr(runtime, "_loaded_fallback_policy", lambda *a, **k: (None, None))
    monkeypatch.setattr(runtime, "_assert_execution_deployment", lambda *a, **k: None)
    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan",
                        lambda *a, **k: (source, {"execution_symbols": ["AAPL", "COST"]}, accepted))
    monkeypatch.setattr("ml.joint_capital_adoption.assert_accepted_execution", lambda *a, **k: None)
    capture = runtime.capture_portfolio_state
    def synthetic_capture(*args, **kwargs):
        kwargs.pop("symbols", None)
        return capture(*args, **kwargs)
    monkeypatch.setattr(runtime, "capture_portfolio_state", synthetic_capture)
    first = run(env)
    assert first.status == "ORDERS_SUBMITTED", first.error
    assert len(env.broker.submissions) == 2
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    before = ledger.snapshot().reservations
    env.now += pd.Timedelta(minutes=1)
    env.held = {symbol: 0. for symbol in env.held}
    env.held["COST"] = 3.
    def evidence(*args, **kwargs):
        return tuple(OrderEvidence("order-" + row.symbol, row.reservation_id, ACCOUNT, env.now.isoformat(),
            row.broker_order_id, "REJECTED" if row.symbol == "AAPL" else "FILLED", row.quantity,
            0 if row.symbol == "AAPL" else row.quantity, 0,
            () if row.symbol == "AAPL" else (FillEvidence("fill-c", row.quantity, "100", env.now.isoformat()),),
            "REJECTED" if row.symbol == "AAPL" else "FILLED",
            "Synthetic rejection requiring review" if row.symbol == "AAPL" else None) for row in before)
    monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence", evidence)
    second = run(env)
    assert second.status == ("ORDERS_SUBMITTED_WITH_REJECTION_REVIEW_REQUIRED" if opposite_exit
                             else "CATCHUP_BROKER_REJECTION_REVIEW_REQUIRED"), second.error
    assert second.submitted_orders == int(opposite_exit)
    assert len(env.broker.submissions) == 2 + int(opposite_exit)
    if opposite_exit:
        assert env.broker.submissions[-1]["orderLegCollection"][0]["instruction"] == "SELL"
    saved = _decisions(second)
    assert saved["prediction_handoff"]["catchup"]["blocked_intentions"][0]["reason_code"] == "CONFIRMED_BROKER_REJECTION_REQUIRES_REVIEW"
    after = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot()
    assert next(row for row in after.reservations if row.symbol == "AAPL").status == "REJECTED"
    assert next(row for row in after.allocations if row.symbol == "COST").filled_shares == 3
    # A newly opened ledger and a later duplicate wake still use the original
    # persisted rejection. No in-memory counter or process lifetime is required.
    blocked = []
    assert ("AAPL", "1h") not in catchup_signals(source, as_of=env.now, source_fingerprint=accepted.name,
        reservations=after.reservations, blocked=blocked)
    assert len(blocked) == 1
