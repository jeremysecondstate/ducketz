"""Synthetic own-order observations precede slow portfolio/native capture work."""
from dataclasses import asdict, replace

import pandas as pd
import pytest

from test_intraday_account_execution import env, native, account, advance
from test_intraday_unknown_commitment_containment import uncertain
from ml.account_gameplan.authority import AccountAuthority, AuthorityError
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError
from ml.stock_trader.horizon_ledger import LedgerError


def filled_order(state, *, symbol="AMZN"):
    forecast = state.forecast(symbol=symbol)
    assert state.executor.step(forecast)["disposition"] == "submitted"
    reservation = next(r for r in native(state).snapshot().reservations
                       if r.forecast_id == forecast["forecast_id"])
    advance(state)
    state.orders[reservation.broker_order_id].update(status="FILLED", filled=reservation.quantity,
                                                    fill_at=state.clock())
    state.held[symbol] = reservation.quantity
    return forecast, reservation


def capacity_count(state):
    with account(state)._transaction() as db:
        return db.execute("SELECT COUNT(*) FROM intraday_capacity_snapshots").fetchone()[0]


def authoritative(state, reservation):
    return next(r for r in account(state).read_state()["reservations"]
                if r["request"]["native_reservation_id"] == reservation.reservation_id)


def test_fresh_terminal_order_is_reconciled_before_later_seventy_second_work(env, monkeypatch):
    _, order = filled_order(env)
    original_capture = execution.capture_portfolio_state
    original_native = execution.HorizonLedger.reconcile
    original_authority = AccountAuthority.reconcile
    order_ages = []

    def portfolio(*args, **kwargs):
        advance(env, 40)
        return original_capture(*args, **kwargs)

    def ledger(*args, **kwargs):
        result = original_native(*args, **kwargs)
        advance(env, 30)
        return result

    def authority(worker, token, evidence):
        order_ages.append((pd.Timestamp(env.clock()) - pd.Timestamp(evidence.observed_at)).total_seconds())
        return original_authority(worker, token, evidence)

    monkeypatch.setattr(execution, "capture_portfolio_state", portfolio)
    monkeypatch.setattr(execution.HorizonLedger, "reconcile", ledger)
    monkeypatch.setattr(AccountAuthority, "reconcile", authority)
    before = len(env.broker.submissions)
    result = env.executor.recover()
    assert result and order_ages and max(order_ages) < 1
    record = authoritative(env, order)
    assert record["status"] == "FILLED" and record["filled_quantity"] == order.quantity
    assert native(env).lookup_reservation(order.reservation_id).status == "FILLED"
    assert (pd.Timestamp(env.clock()) - pd.Timestamp(record["last_evidence_at"])).total_seconds() >= 70
    assert len(env.broker.submissions) == before and not env.broker.cancellations


@pytest.mark.parametrize("failed_stage", ["portfolio", "native"])
def test_authority_fill_survives_later_failure_and_native_recovers_exactly_once(env, monkeypatch, failed_stage):
    _, order = filled_order(env)
    before_capacity = capacity_count(env)
    original_capture = execution.capture_portfolio_state
    original_native = execution.HorizonLedger.reconcile
    failure = RuntimeError("synthetic later capture failure")
    if failed_stage == "portfolio":
        monkeypatch.setattr(execution, "capture_portfolio_state", lambda *a, **k: (_ for _ in ()).throw(failure))
    else:
        monkeypatch.setattr(execution.HorizonLedger, "reconcile", lambda *a, **k: (_ for _ in ()).throw(failure))
    before_submissions = len(env.broker.submissions)
    with pytest.raises(RuntimeError, match="synthetic later capture failure"):
        env.executor.recover()
    assert authoritative(env, order)["status"] == "FILLED"
    assert native(env).lookup_reservation(order.reservation_id).status == "SUBMITTED"
    assert capacity_count(env) == before_capacity
    monkeypatch.setattr(execution, "capture_portfolio_state", original_capture)
    monkeypatch.setattr(execution.HorizonLedger, "reconcile", original_native)
    advance(env)
    env.executor.recover()
    assert authoritative(env, order)["status"] == "FILLED"
    assert native(env).lookup_reservation(order.reservation_id).status == "FILLED"
    with account(env)._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    with native(env)._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert capacity_count(env) == before_capacity + 1
    assert len(env.broker.submissions) == before_submissions and not env.broker.cancellations


def test_old_order_observation_is_rejected_before_portfolio_or_authority_mutation(env, monkeypatch):
    _, order = filled_order(env)
    advance(env, 70)
    capture = execution.capture_order_evidence
    before = authoritative(env, order)
    captures = env.captures

    def old(*args, **kwargs):
        return tuple(replace(item, observed_at=(env.now - pd.Timedelta(seconds=61)).isoformat())
                     for item in capture(*args, **kwargs))

    monkeypatch.setattr(execution, "capture_order_evidence", old)
    with pytest.raises(AuthorityError, match="ORDER_EVIDENCE_STALE_OR_INVALID"):
        env.executor.recover()
    assert authoritative(env, order) == before and env.captures == captures
    assert native(env).lookup_reservation(order.reservation_id).status == "SUBMITTED"


@pytest.mark.parametrize("changed", ["manual", "stopped", "binding", "lease"])
def test_capture_manual_binding_and_fence_changes_prevent_early_mutation(env, monkeypatch, changed):
    _, order = filled_order(env)
    capture = execution.capture_order_evidence
    before = authoritative(env, order)
    before_capacity = capacity_count(env)

    def interrupted(*args, **kwargs):
        result = capture(*args, **kwargs)
        if changed == "manual":
            env.active = False
        elif changed == "stopped":
            env.executor.stopped = lambda: True
        elif changed == "binding":
            env.config = replace(env.config, fingerprint="changed-local-fingerprint")
        else:
            env.executor.authority.release_lease(env.executor.lease.token)
        return result

    monkeypatch.setattr(execution, "capture_order_evidence", interrupted)
    with pytest.raises((ContractError, AuthorityError)):
        env.executor.recover()
    assert authoritative(env, order) == before
    assert capacity_count(env) == before_capacity
    assert native(env).lookup_reservation(order.reservation_id).status == "SUBMITTED"
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


@pytest.mark.parametrize("field", ["native_reservation_id", "native_allocation_id", "broker_order_id"])
def test_corrupt_terminal_identity_fails_before_order_capture_or_other_projection(env, monkeypatch, field):
    _, terminal = filled_order(env)
    env.executor.recover()
    advance(env)
    _, pending = filled_order(env, symbol="AAPL")
    before = authoritative(env, pending)
    read = AccountAuthority.read_state
    observed = []
    capture = execution.capture_order_evidence

    def changed(worker):
        result = read(worker)
        record = next(r for r in result["reservations"]
                      if r["request"]["native_reservation_id"] == terminal.reservation_id)
        if field == "broker_order_id":
            record[field] = "synthetic-other-order"
        else:
            record["request"][field] = "synthetic-other-identity"
        return result

    def monitored(*args, **kwargs):
        observed.append(True)
        return capture(*args, **kwargs)

    monkeypatch.setattr(AccountAuthority, "read_state", changed)
    monkeypatch.setattr(execution, "capture_order_evidence", monitored)
    with pytest.raises((ContractError, LedgerError)):
        env.executor.recover()
    monkeypatch.setattr(AccountAuthority, "read_state", read)
    assert not observed and authoritative(env, pending) == before


def test_unknown_full_commitment_survives_known_fill_and_slow_capture(env, monkeypatch):
    env.cash = 350.
    _, known = filled_order(env)
    _, unknown = uncertain(env, symbol="AAPL")
    unknown_native = asdict(native(env).lookup_reservation(unknown.reservation_id))
    unknown_authority = authoritative(env, unknown)
    capture = execution.capture_portfolio_state

    def slow(*args, **kwargs):
        advance(env, 40)
        return capture(*args, **kwargs)

    monkeypatch.setattr(execution, "capture_portfolio_state", slow)
    advance(env)
    env.executor.recover()
    assert authoritative(env, known)["status"] == "FILLED"
    assert authoritative(env, unknown) == unknown_authority
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == unknown_native
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


def test_incomplete_later_order_blocks_before_any_captured_fill_is_projected(env, monkeypatch):
    env.cash = 500.
    for symbol in ("AMZN", "AAPL"):
        assert env.executor.step(env.forecast(symbol=symbol))["disposition"] == "submitted"
    orders = native(env).snapshot().reservations
    advance(env)
    for order in orders:
        env.orders[order.broker_order_id].update(status="FILLED", filled=order.quantity, fill_at=env.clock())
        env.held[order.symbol] = order.quantity
    before = {order.reservation_id: authoritative(env, order) for order in orders}
    before_capacity = capacity_count(env)
    capture = execution.capture_order_evidence

    def incomplete(*args, **kwargs):
        rows = capture(*args, **kwargs)
        return (*rows[:-1], replace(rows[-1], fills=()))

    monkeypatch.setattr(execution, "capture_order_evidence", incomplete)
    with pytest.raises(LedgerError, match="COMPLETE_PER_ORDER_FILL_EVIDENCE_REQUIRED"):
        env.executor.recover()
    assert {order.reservation_id: authoritative(env, order) for order in orders} == before
    assert capacity_count(env) == before_capacity
    assert all(native(env).lookup_reservation(order.reservation_id).filled_quantity == 0 for order in orders)
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations
