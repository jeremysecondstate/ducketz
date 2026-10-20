"""Real temporary own ledgers; uncertain orders retain their full commitment."""
from dataclasses import asdict, replace
import json

import pandas as pd
import pytest

from test_intraday_account_execution import env, native, account, advance
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError
from ml.stock_trader.horizon_broker import OrderHistoryError
from ml.stock_trader.horizon_ledger import LedgerError


def uncertain(env, *, symbol="AAPL"):
    env.broker.unknown_submission = True
    forecast = env.forecast(symbol=symbol)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submission_recovery_required"
    env.broker.unknown_submission = False
    advance(env)
    return forecast, next(r for r in native(env).snapshot().reservations
                          if r.forecast_id == forecast['forecast_id'])


def test_unknown_withholds_full_cash_while_another_symbol_uses_only_remainder(env):
    env.cash = 250.
    original, unknown = uncertain(env)
    before_native = asdict(unknown)
    before_authority = account(env).read_state()["reservations"][0]
    next_forecast = env.forecast(symbol="AMZN")
    result = env.executor.step(next_forecast)
    assert result["disposition"] == "submitted"
    assert result["desired_quantity"] == 4 and result["adjusted_quantity"] == 1
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == before_native
    assert account(env).read_state()["reservations"][0] == before_authority
    assert env.store.forecast(original["cycle_id"], original["symbol"]) == original
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


def test_known_id_fill_reconciles_while_unknown_is_retained(env):
    env.cash = 350.
    known_forecast = env.forecast(symbol="AMZN")
    env.executor.step(known_forecast)
    known = native(env).snapshot().reservations[0]
    _, unknown = uncertain(env)
    before_unknown = asdict(unknown)
    env.orders[known.broker_order_id].update(status="FILLED", filled=known.quantity, fill_at=env.clock())
    env.held["AMZN"] = known.quantity
    env.cash = 150.
    results = env.executor.recover()
    assert len(results) == 2
    dispositions = {r['evidence']['native_reservation_id']:r for r in results}
    assert dispositions[known.reservation_id]['disposition'] == 'order_status_reconciled'
    assert dispositions[unknown.reservation_id]['disposition'] == 'unresolved_submission_retained'
    assert dispositions[unknown.reservation_id]['evidence']['commitment_retained'] is True
    assert dispositions[unknown.reservation_id]['evidence']['complete_broker_order_evidence'] is False
    assert native(env).lookup_reservation(known.reservation_id).status == "FILLED"
    records = {r["request"]["native_reservation_id"]:r for r in account(env).read_state()["reservations"]}
    assert records[known.reservation_id]["status"] == "FILLED"
    assert records[known.reservation_id]["filled_quantity"] == known.quantity
    assert records[unknown.reservation_id]["status"] == "UNKNOWN"
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == before_unknown
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


def test_same_forecast_and_new_same_symbol_never_repost_or_cancel_unknown(env):
    original, unknown = uncertain(env)
    result = env.executor.step(original)
    assert result["disposition"] == "recover_existing_intention"
    newer = env.forecast(offset=15)
    env.now = pd.Timestamp(newer["issued_at"])
    result = env.executor.step(newer)
    assert result["disposition"] == "previous_intention_reconciliation"
    assert native(env).lookup_reservation(unknown.reservation_id).status == "UNKNOWN"
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_unknown_sale_withholds_all_reserved_shares_and_never_creates_cash(env):
    env.held['AAPL'] = 5
    env.broker.unknown_submission = True
    forecast = env.forecast(probability=.2)
    env.executor.step(forecast)
    unknown = native(env).snapshot().reservations[0]
    env.broker.unknown_submission = False
    advance(env)
    with env.executor._account():
        env.executor._capture()
        capacity = env.executor.authority.available_intraday_capacity(env.executor.lease.token, env.executor.snapshot_id)
    assert capacity['eligible_shares']['AAPL'] == 5 - unknown.quantity
    assert capacity['cash_available'] == '100'
    assert unknown.side == 'SELL' and unknown.quantity > 0
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_interrupted_native_reserved_authority_unknown_keeps_full_commitment(env):
    env.cash = 250.
    _, unknown = uncertain(env)
    ledger = native(env)
    # Retained real state pair: authority UNKNOWN/native RESERVED with no ID.
    # The original interrupted stage was not recorded; this simulates state.
    with ledger._transaction() as db:
        db.execute("UPDATE reservations SET status='RESERVED' WHERE id=?", (unknown.reservation_id,))
    result = env.executor.step(env.forecast(symbol="AMZN"))
    assert result["disposition"] == "submitted" and result["adjusted_quantity"] == 1
    assert native(env).lookup_reservation(unknown.reservation_id).status == "RESERVED"
    assert account(env).read_state()["reservations"][0]["status"] == "UNKNOWN"
    assert account(env).read_state()["reservations"][0]["broker_order_id"] is None


def test_known_missing_history_still_blocks_instead_of_hiding_with_unknown(env, monkeypatch):
    env.cash = 350.
    env.executor.step(env.forecast(symbol="AMZN"))
    known = native(env).snapshot().reservations[0]
    uncertain(env)
    captures_before = env.captures
    def missing(broker, owner, **kwargs):
        assert [r.broker_order_id for r in owner.pending_reservations()] == [known.broker_order_id]
        raise OrderHistoryError("TRACKED_ORDER_MISSING_FROM_BOUNDED_HISTORY")
    monkeypatch.setattr(execution, "capture_order_evidence", missing)
    with pytest.raises(OrderHistoryError, match="MISSING_FROM_BOUNDED_HISTORY"):
        env.executor.recover()
    assert env.captures == captures_before
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


def test_unbound_native_unknown_cannot_be_contained(env):
    _, unknown = uncertain(env)
    authority = account(env)
    with authority._transaction() as db:
        db.execute("DELETE FROM reservations WHERE native_id=?", (unknown.reservation_id,))
    with pytest.raises(ContractError, match="Uncertain native commitment lacks matching local authority"):
        env.executor.recover()
    assert native(env).lookup_reservation(unknown.reservation_id).status == "UNKNOWN"
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_authority_submitting_native_reserved_is_fully_withheld(env):
    env.cash = 250.
    _, unknown = uncertain(env)
    with native(env)._transaction() as db:
        db.execute("UPDATE reservations SET status='RESERVED' WHERE id=?", (unknown.reservation_id,))
    with account(env)._transaction() as db:
        db.execute("UPDATE reservations SET status='SUBMITTING' WHERE native_id=?", (unknown.reservation_id,))
    result = env.executor.step(env.forecast(symbol='AMZN'))
    assert result['disposition'] == 'submitted' and result['adjusted_quantity'] == 1
    assert native(env).lookup_reservation(unknown.reservation_id).status == 'RESERVED'
    assert account(env).read_state()['reservations'][0]['status'] == 'SUBMITTING'
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


@pytest.mark.parametrize('field,value', [
    ('limit_price','1'), ('exposure_price','1'), ('account_fingerprint','f'*64),
    ('participant_id','pc-new'), ('local_symbols',['AAPL']),
    ('forecast_snapshot_sha256','d'*64), ('forecast_sha256','d'*64),
])
def test_unknown_scope_or_economic_commitment_mismatch_still_blocks(env,field,value):
    _, unknown = uncertain(env)
    authority = account(env)
    with authority._transaction() as db:
        row = db.execute('SELECT id,request FROM reservations').fetchone()
        request = json.loads(row['request'])
        request[field] = value
        db.execute('UPDATE reservations SET request=? WHERE id=?', (json.dumps(request),row['id']))
    with pytest.raises(ContractError, match='Uncertain local commitment identities differ'):
        env.executor.recover()
    assert native(env).lookup_reservation(unknown.reservation_id).status == 'UNKNOWN'
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_known_incomplete_fill_evidence_still_blocks_with_unknown(env, monkeypatch):
    env.cash = 350.
    env.executor.step(env.forecast(symbol='AMZN'))
    uncertain(env)
    observe = execution.capture_order_evidence
    def incomplete(*args, **kwargs):
        rows = observe(*args, **kwargs)
        return tuple(replace(row, status='PARTIAL', cumulative_filled_quantity=1,
                     remaining_quantity=row.order_quantity-1, fills=()) for row in rows)
    monkeypatch.setattr(execution,'capture_order_evidence',incomplete)
    with pytest.raises(LedgerError):
        env.executor.recover()
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


@pytest.mark.parametrize('field,value', [
    ('price','1'), ('quantity',1), ('side','SELL'), ('symbol','AMZN'),
    ('forecast','d'*64), ('forecast_sha256','d'*64),
])
def test_unknown_native_raw_request_cannot_disagree_with_indexed_commitment(env,field,value):
    _, unknown = uncertain(env)
    with native(env)._transaction() as db:
        row = db.execute('SELECT request FROM reservations WHERE id=?',(unknown.reservation_id,)).fetchone()
        request = json.loads(row['request'])
        request[field] = value
        db.execute('UPDATE reservations SET request=? WHERE id=?', (json.dumps(request),unknown.reservation_id))
    with pytest.raises(ContractError,match='Uncertain local commitment identities differ'):
        env.executor.recover()
    assert native(env).lookup_reservation(unknown.reservation_id).status == 'UNKNOWN'
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_false_reconciliation_without_reasons_cannot_be_contained(env, monkeypatch):
    uncertain(env)
    reconcile = execution.HorizonLedger.reconcile
    def invalid(self, *args, **kwargs):
        return replace(reconcile(self,*args,**kwargs),ready=False,reasons=())
    monkeypatch.setattr(execution.HorizonLedger,'reconcile',invalid)
    with pytest.raises(ContractError, match='Native account reconciliation'):
        env.executor.recover()
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
