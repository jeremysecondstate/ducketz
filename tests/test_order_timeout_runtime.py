"""Offline account/runtime coverage for the two five-minute order attempts."""
from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.catchup import catchup_signals
from ml.stock_trader.horizon_ledger import FillEvidence, OrderEvidence
from test_account_gameplan_execution import account_env, native, run, state
from test_independent_stock_runtime import ACCOUNT, SYMBOLS, _decisions, environment


def _accepted_orders(env, monkeypatch, side):
    """Use real temporary ledgers and synthetic current broker observations."""
    env.initial_time = env.now
    env.initial_held = 10 if side == "SELL" else 0
    env.held = dict.fromkeys(SYMBOLS, 0.)
    env.held["AAPL"] = env.initial_held
    env.quote = (100., 100.)
    env.broker_states = {}
    env.fill_times = {}
    source = {
        "action_date": env.now.tz_convert("America/Los_Angeles").date().isoformat(),
        "as_of": (env.now - pd.Timedelta(hours=2)).isoformat(),
        "plan_sha256": "a" * 64,
        "ledger": {"events": [
            {"forecast_id": "initial-instruction", "symbol": "AAPL", "horizon": "1h",
             "action": side, "quantity": 5, "timestamp": env.now.isoformat(), "sequence": 1},
            {"forecast_id": "future-instruction", "symbol": "AAPL", "horizon": "1h",
             "action": side, "quantity": 2,
             "timestamp": (env.now + pd.Timedelta(minutes=20)).isoformat(), "sequence": 2},
        ]},
    }
    env.plan.report.update(accepted_joint_plan=True, joint_composition=source)
    (env.plan.path / "accepted-plan.json").write_text("{}", encoding="utf-8")
    env.signals = catchup_signals(source, as_of=env.now, source_fingerprint=env.plan.path.name)
    monkeypatch.setattr(runtime, "_loaded_gameplan_run", lambda *a, **k: env.plan.path)
    monkeypatch.setattr(runtime, "_loaded_fallback_policy", lambda *a, **k: (None, None))
    monkeypatch.setattr(runtime, "_assert_execution_deployment", lambda *a, **k: None)
    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan",
                        lambda *a, **k: (source, {"execution_symbols": list(SYMBOLS)}, env.plan.path))
    monkeypatch.setattr("ml.joint_capital_adoption.assert_accepted_execution", lambda *a, **k: None)

    def orders():
        return native(env).snapshot().reservations

    def evidence(*args, **kwargs):
        rows = []
        for order in orders():
            status, filled = env.broker_states.get(order.broker_order_id, ("WORKING", 0))
            fills = ((FillEvidence("fill-" + order.broker_order_id, filled, order.limit_price,
                                   env.fill_times[order.broker_order_id]),) if filled else ())
            rows.append(OrderEvidence("observation-" + order.broker_order_id + "-" + env.now.isoformat(),
                order.reservation_id, ACCOUNT, env.now.isoformat(), order.broker_order_id, status,
                order.quantity, filled, order.quantity - filled if status in {"WORKING", "PARTIAL"} else 0,
                fills))
        return tuple(rows)

    monkeypatch.setattr("ml.stock_trader.horizon_broker.capture_order_evidence", evidence)

    def portfolio(current):
        working = []
        pending_buy = {}
        pending_sell = {}
        held = env.initial_held
        for order in orders():
            status, filled = env.broker_states.get(order.broker_order_id, ("WORKING", 0))
            held += filled if order.side == "BUY" else -filled
            if status not in {"WORKING", "PARTIAL"}:
                continue
            remaining = order.quantity - filled
            pending = pending_buy if order.side == "BUY" else pending_sell
            pending[order.symbol] = pending.get(order.symbol, 0) + remaining
            working.append({"order_id": order.broker_order_id, "symbol": order.symbol,
                "instruction": order.side, "asset_type": "EQUITY", "status": "CURRENT",
                "remaining_quantity": remaining, "filled_quantity": filled,
                "limit_price": order.limit_price,
                "reserved_cash": str(Decimal(order.limit_price) * remaining if order.side == "BUY" else 0)})
        env.held["AAPL"] = held
        bid, ask = env.quote
        return replace(current, held_shares=dict(env.held),
            symbol_exposure={symbol: quantity * ask for symbol, quantity in env.held.items()},
            pending_buy_shares=pending_buy, pending_sell_shares=pending_sell,
            working_order_count=len(working), broker_working_orders=tuple(working),
            quotes={symbol: replace(quote, bid=bid, ask=ask, observed_at=env.now.isoformat())
                    for symbol, quote in current.quotes.items()})

    env.portfolio_transform = portfolio
    return source


def _observe(env, order, *, status, filled=0):
    env.broker_states[order.broker_order_id] = (status, filled)
    if filled:
        env.fill_times.setdefault(order.broker_order_id, env.now.isoformat())


def _assert_account_parity(env):
    owned = {order.reservation_id: order for order in native(env).snapshot().reservations}
    records = state(env)["reservations"]
    assert len(records) == len(owned)
    for record in records:
        order = owned[record["request"]["native_reservation_id"]]
        assert record["broker_order_id"] == order.broker_order_id
        assert record["status"] == order.status
        assert record["filled_quantity"] == order.filled_quantity
        assert record["request"]["quantity"] == order.quantity


@pytest.mark.parametrize("side,refreshed_price", [("BUY", "110.04"), ("SELL", "110.00")])
def test_two_five_minute_attempts_preserve_fills_and_only_future_delta_can_resume(account_env, monkeypatch,
                                                                               side, refreshed_price):
    env = account_env
    source = _accepted_orders(env, monkeypatch, side)
    first = run(env)
    assert first.status == "ORDERS_SUBMITTED", first.error
    assert first.submitted_orders == len(env.broker.submissions) == 1
    original, = native(env).snapshot().reservations
    assert original.quantity == 5 and original.side == side and original.submitted_at

    env.now = pd.Timestamp(original.submitted_at) + pd.Timedelta(seconds=299)
    assert run(env).submitted_orders == 0
    assert env.broker.cancellations == []
    assert len(env.broker.submissions) == 1

    env.now = pd.Timestamp(original.submitted_at) + pd.Timedelta(seconds=300)
    timeout = run(env)
    assert timeout.status == "ENTRY_CANCELLATION_AWAITING_RECONCILIATION", timeout.error
    assert timeout.submitted_orders == 0
    assert env.broker.cancellations == [original.broker_order_id]
    pending = native(env).lookup_reservation(original.reservation_id)
    assert pending.timeout_requested_at and pending.reserved_quantity == 5

    # A successful DELETE is only a request; repeated wakes must wait for the
    # exact order's terminal evidence while its full remainder stays reserved.
    for delay in (1, 30):
        env.now += pd.Timedelta(seconds=delay)
        assert run(env).submitted_orders == 0
        assert len(env.broker.submissions) == 1
        assert env.broker.cancellations == [original.broker_order_id]
    _assert_account_parity(env)

    env.now += pd.Timedelta(seconds=1)
    _observe(env, original, status="CANCELLED", filled=2)
    env.quote = (110., 110.04)
    retry = run(env)
    assert retry.status == "ORDERS_SUBMITTED", retry.error
    assert retry.submitted_orders == 1 and len(env.broker.submissions) == 2
    replacement = next(order for order in native(env).snapshot().reservations
                       if order.reservation_id != original.reservation_id)
    assert replacement.quantity == 3 and replacement.side == side
    assert replacement.retry_parent_id == original.reservation_id
    assert Decimal(replacement.limit_price) == Decimal(refreshed_price)
    assert replacement.submitted_at != original.submitted_at
    _assert_account_parity(env)

    env.now = pd.Timestamp(replacement.submitted_at) + pd.Timedelta(seconds=299)
    assert run(env).submitted_orders == 0
    assert env.broker.cancellations == [original.broker_order_id]
    env.now = pd.Timestamp(replacement.submitted_at) + pd.Timedelta(seconds=300)
    second_timeout = run(env)
    assert second_timeout.status == "ENTRY_CANCELLATION_AWAITING_RECONCILIATION", second_timeout.error
    assert env.broker.cancellations == [original.broker_order_id, replacement.broker_order_id]
    assert second_timeout.submitted_orders == 0 and len(env.broker.submissions) == 2

    env.now += pd.Timedelta(seconds=1)
    _observe(env, replacement, status="CANCELLED", filled=1)
    for _ in range(3):
        env.now += pd.Timedelta(seconds=1)
        stopped = run(env)
        assert stopped.submitted_orders == 0, stopped.error
        assert len(env.broker.submissions) == 2
        # Reopen from durable storage each time, as a restarted worker does.
        persisted = native(env).snapshot()
        assert sum(order.filled_quantity for order in persisted.reservations) == 3
        assert all(order.status == "CANCELLED" and order.reserved_quantity == 0 for order in persisted.reservations)
        abandoned = []
        assert catchup_signals(source, as_of=env.now, source_fingerprint=env.plan.path.name,
                               reservations=persisted.reservations, abandoned=abandoned) == {}
        assert sum(row["abandoned_quantity"] for row in abandoned) == 2
        assert _decisions(stopped)["prediction_handoff"]["catchup"]["abandoned_intentions"] == abandoned
        _assert_account_parity(env)
    assert env.held["AAPL"] == (3 if side == "BUY" else 7)

    env.now = env.initial_time + pd.Timedelta(minutes=20)
    future = run(env)
    assert future.status == "ORDERS_SUBMITTED", future.error
    assert future.submitted_orders == 1 and len(env.broker.submissions) == 3
    third = next(order for order in native(env).snapshot().reservations
                 if order.reservation_id not in {original.reservation_id, replacement.reservation_id})
    assert third.quantity == 2 and third.side == side and third.retry_parent_id is None
    _assert_account_parity(env)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_uncertain_timeout_cancellation_keeps_remainder_reserved_without_retry(account_env, monkeypatch, side):
    env = account_env
    _accepted_orders(env, monkeypatch, side)
    assert run(env).submitted_orders == 1
    original, = native(env).snapshot().reservations

    def uncertain_cancel(order_id, context, *, before_delete):
        before_delete()
        env.broker.cancellations.append(order_id)
        raise TimeoutError("synthetic cancellation response lost")

    env.broker.cancel_prepared_order = uncertain_cancel
    env.now = pd.Timestamp(original.submitted_at) + pd.Timedelta(minutes=5)
    result = run(env)
    assert result.status == "ENTRY_CANCELLATION_AWAITING_RECONCILIATION"
    assert "TimeoutError" in result.error
    for delay in (1, 300):
        env.now += pd.Timedelta(seconds=delay)
        assert run(env).submitted_orders == 0
        pending = native(env).lookup_reservation(original.reservation_id)
        assert pending.status == "WORKING" and pending.reserved_quantity == 5
        assert pending.timeout_requested_at and pending.cancel_requested_at
        assert len(native(env).snapshot().reservations) == len(env.broker.submissions) == 1
        assert env.broker.cancellations == [original.broker_order_id]
        _assert_account_parity(env)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("retry_terminal", ["FILLED", "CANCELLED"])
def test_new_opposite_instruction_before_retry_is_not_counted_twice(account_env, monkeypatch, side,
                                                                  retry_terminal):
    env = account_env
    source = _accepted_orders(env, monkeypatch, side)
    source["ledger"]["events"][-1]["action"] = "SELL" if side == "BUY" else "BUY"
    assert run(env).submitted_orders == 1
    original, = native(env).snapshot().reservations
    env.now = pd.Timestamp(original.submitted_at) + pd.Timedelta(minutes=5)
    assert run(env).status == "ENTRY_CANCELLATION_AWAITING_RECONCILIATION"

    # The first cancellation takes time to confirm. During that wait a genuine
    # opposite instruction reduces the due net target from five shares to three.
    env.now = env.initial_time + pd.Timedelta(minutes=20)
    _observe(env, original, status="CANCELLED", filled=2)
    retried = run(env)
    assert retried.status == "ORDERS_SUBMITTED", retried.error
    replacement = next(order for order in native(env).snapshot().reservations
                       if order.reservation_id != original.reservation_id)
    assert replacement.quantity == 1 and replacement.side == side
    assert replacement.retry_parent_id == original.reservation_id

    env.now += pd.Timedelta(seconds=1)
    waiting = run(env)
    assert waiting.submitted_orders == 0, waiting.error
    assert len(env.broker.submissions) == 2
    assert _decisions(waiting)["prediction_handoff"]["catchup"]["net_quantities"] == {}
    _assert_account_parity(env)

    if retry_terminal == "CANCELLED":
        env.now = pd.Timestamp(replacement.submitted_at) + pd.Timedelta(minutes=5)
        assert run(env).status == "ENTRY_CANCELLATION_AWAITING_RECONCILIATION"
        assert env.broker.cancellations == [original.broker_order_id, replacement.broker_order_id]
    env.now += pd.Timedelta(seconds=1)
    _observe(env, replacement, status=retry_terminal, filled=int(retry_terminal == "FILLED"))
    for _ in range(2):
        env.now += pd.Timedelta(seconds=1)
        settled = run(env)
        assert settled.submitted_orders == 0, settled.error
        assert len(env.broker.submissions) == 2
        persisted = native(env).snapshot()
        abandoned = []
        assert catchup_signals(source, as_of=env.now, source_fingerprint=env.plan.path.name,
                               reservations=persisted.reservations, abandoned=abandoned) == {}
        assert sum(row["abandoned_quantity"] for row in abandoned) == int(retry_terminal == "CANCELLED")
        _assert_account_parity(env)
