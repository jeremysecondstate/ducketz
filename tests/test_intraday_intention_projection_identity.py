"""Immutable projections distinguish first sizing while preserving uncertain orders."""
from dataclasses import asdict
from test_intraday_account_execution import env,native,account,advance
from test_intraday_unknown_commitment_containment import uncertain
from test_intraday_intentions import setup as intention_setup
from ml.intraday.prediction import ContractError,digest,encoded
from ml.intraday.technical_sizing import SizingResult
from ml.stock_trader.horizon_ledger import ReservationState
import pandas as pd
import pytest


def test_reconciliation_projection_survives_unsized_then_sized(env):
    original,unknown=uncertain(env)
    # Match exact retained original pair; temporary fake ledgers only.
    with native(env)._transaction() as db:
        db.execute("UPDATE reservations SET status='RESERVED' WHERE id=?",(unknown.reservation_id,))
    original_native=asdict(native(env).lookup_reservation(unknown.reservation_id))
    original_authority=account(env).read_state()['reservations']
    newer=env.forecast(offset=15)
    env.now=pd.Timestamp(newer['issued_at'])
    env.quote_age=120
    first=env.executor.step(newer)
    assert first['disposition']=='previous_intention_reconciliation'
    assert first['desired_quantity'] is None
    assert env.executor.intents.get(newer['forecast_id']) is None
    env.quote_age=0
    advance(env)
    second=env.executor.step(newer)
    assert second['disposition']=='previous_intention_reconciliation'
    assert second['desired_quantity']>0
    assert second['event_identity']!=first['event_identity']
    assert env.executor.intents.history(newer['forecast_id'])[1]==first
    advance(env)
    third=env.executor.step(newer)
    assert third==second
    assert asdict(native(env).lookup_reservation(unknown.reservation_id))==original_native
    assert account(env).read_state()['reservations']==original_authority
    assert len(env.broker.submissions)==1 and not env.broker.cancellations
    assert env.store.forecast(original['cycle_id'],original['symbol'])==original


def test_unsized_projection_keeps_legacy_key_and_sized_projection_is_idempotent(env):
    forecast=env.forecast()
    evidence={'reason_code':'EXACT_BROKER_ORDER_ID_UNAVAILABLE','status':'RESERVED'}
    result=env.executor._record(forecast,'previous_intention_reconciliation',evidence=evidence)
    legacy=digest(['execution_disposition','previous_intention_reconciliation',evidence,None,None])
    assert result['event_identity']==digest([forecast['forecast_id'],legacy])
    advance(env)
    assert env.executor._record(forecast,'previous_intention_reconciliation',evidence=evidence)==result


def test_store_rejects_changed_evidence_for_exact_identity(env):
    forecast=env.forecast()
    store=env.executor.intents
    original=store.record(forecast,event_key='explicit-original-event',recorded_at=env.clock(),
        disposition='previous_intention_reconciliation',evidence={'status':'RESERVED'},kind='execution_disposition')
    advance(env)
    with pytest.raises(ContractError,match='identity reused with changed evidence'):
        store.record(forecast,event_key='explicit-original-event',recorded_at=env.clock(),
            disposition='previous_intention_reconciliation',evidence={'status':'UNKNOWN'},kind='execution_disposition')
    assert store.history(forecast['forecast_id'])==[original]


def test_direct_default_record_still_rejects_same_key_after_first_sizing(env):
    forecast=env.forecast()
    store=env.executor.intents
    kwargs=dict(event_key='unchanged-direct-default',disposition='awaiting_capacity',evidence={'reason':'fixture'},kind='execution_disposition')
    first=store.record(forecast,recorded_at=env.clock(),**kwargs)
    env.executor.step(forecast)
    assert store.get(forecast['forecast_id']) is not None
    advance(env)
    with pytest.raises(ContractError,match='identity reused with changed evidence'):
        store.record(forecast,recorded_at=env.clock(),**kwargs)
    assert first in store.history(forecast['forecast_id'])


def test_scoped_sized_identity_uses_validated_tuple_and_rejects_changed_evidence(env):
    forecast=env.forecast()
    env.executor.step(forecast)
    store=env.executor.intents
    evidence={'reason':'fixture'}
    kwargs=dict(event_key='scoped-sized',disposition='awaiting_capacity',evidence=evidence,kind='execution_disposition',scope_to_intention=True)
    first=store.record(forecast,recorded_at=env.clock(),**kwargs)
    with store.store.connect() as db:
        immutable_id=db.execute('SELECT id FROM intraday_intentions WHERE forecast_id=?',(forecast['forecast_id'],)).fetchone()[0]
    assert first['event_identity']==digest([forecast['forecast_id'],'scoped-sized',{'intention_id':immutable_id}])
    advance(env)
    assert store.record(forecast,recorded_at=env.clock(),**kwargs)==first
    with pytest.raises(ContractError,match='identity reused with changed evidence'):
        store.record(forecast,recorded_at=env.clock(),**{**kwargs,'evidence':{'reason':'changed'}})


def test_scoped_sizing_requires_original_payload_hash(env):
    forecast=env.forecast()
    env.executor.step(forecast)
    store=env.executor.intents
    original=store.get(forecast['forecast_id'])
    with store.store.connect() as db:
        db.execute('UPDATE intraday_intentions SET payload=? WHERE forecast_id=?',(encoded({**original,'desired_quantity':original['desired_quantity']+1}),forecast['forecast_id']))
    with pytest.raises(ContractError,match='Persisted sizing intention changed'):
        store.record(forecast,event_key='scope-tamper',recorded_at=env.clock(),disposition='awaiting_capacity',
            evidence={'reason':'fixture'},kind='execution_disposition',scope_to_intention=True)


def _interrupted_first_sizing(tmp_path, monkeypatch):
    store, forecast, intents, sized = intention_setup(tmp_path)
    unsized = intents.record(forecast, event_key="before-sizing",
        recorded_at="2026-10-08T17:16:04Z", disposition="awaiting_capacity",
        kind="execution_disposition", evidence={"reason":"fixture"})
    value = sized.payload() | {"calculated_at":"2026-10-08T17:16:05Z"}
    sized = SizingResult(digest(value), encoded(value))

    def interrupted(*args, **kwargs):
        assert kwargs["event_key"] == "sized:" + sized.result_id
        raise RuntimeError("fixture crash before sized receipt")

    # Exercise the documented durable boundary without deleting saved receipts.
    with monkeypatch.context() as patch:
        patch.setattr(intents, "record", interrupted)
        with pytest.raises(RuntimeError, match="fixture crash before sized receipt"):
            intents.save_sizing(forecast, sized)
    assert intents.get(forecast["forecast_id"])["sizing"] == value
    assert intents.history(forecast["forecast_id"]) == [unsized]
    return store, forecast, intents, sized


def _sizing_projection(intents, forecast, mode, recorded_at):
    kwargs = dict(event_key="after-sizing", recorded_at=recorded_at,
        disposition="awaiting_capacity", kind="execution_disposition",
        evidence={"reason":"fixture"})
    if mode == "recovery":
        order = ReservationState("fixture-order", "fixture-allocation", "AAPL", "15m",
            forecast["forecast_id"], "BUY", 1, "50", 0, "RESERVED", None,
            "fixture-key", "fixture-batch", None)
        return intents.record_recovery(forecast["forecast_id"], actor="Atlas", order=order, **kwargs)
    return intents.record(forecast, scope_to_intention=mode == "scoped", **kwargs)


def _intention_rows(store):
    with store.connect() as db:
        return {table:db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in ("forecasts", "intraday_intentions", "intraday_intention_events", "intents")}


@pytest.mark.parametrize("mode", ["scoped", "default", "recovery"])
@pytest.mark.parametrize("sized_receipt_saved", [False, True])
def test_fresh_projection_cannot_predate_immutable_sizing(tmp_path, monkeypatch, mode, sized_receipt_saved):
    store, forecast, intents, sized = _interrupted_first_sizing(tmp_path, monkeypatch)
    if sized_receipt_saved:
        intents.save_sizing(forecast, sized)
    before = _intention_rows(store)
    with pytest.raises(ContractError, match="backdated"):
        _sizing_projection(intents, forecast, mode, "2026-10-08T17:16:04.75Z")
    assert _intention_rows(store) == before


@pytest.mark.parametrize("mode", ["scoped", "default", "recovery"])
@pytest.mark.parametrize("recorded_at", ["2026-10-08T17:16:05Z", "2026-10-08T10:16:05-07:00"])
def test_fresh_projection_accepts_exact_sizing_time(tmp_path, monkeypatch, mode, recorded_at):
    store, forecast, intents, sized = _interrupted_first_sizing(tmp_path, monkeypatch)
    original = intents.get(forecast["forecast_id"])
    result = _sizing_projection(intents, forecast, mode, recorded_at)
    assert result["desired_quantity"] == original["desired_quantity"] == 4
    assert result["recorded_at"] == "2026-10-08T17:16:05+00:00"
    assert len(intents.history(forecast["forecast_id"])) == 2
    assert intents.get(forecast["forecast_id"]) == original
    assert store.forecast(forecast["cycle_id"], forecast["symbol"]) == forecast


@pytest.mark.parametrize("mode", ["scoped", "default", "recovery"])
def test_exact_sized_replay_preserves_receipt_with_earlier_caller_clock(tmp_path, monkeypatch, mode):
    store, forecast, intents, sized = _interrupted_first_sizing(tmp_path, monkeypatch)
    original = _sizing_projection(intents, forecast, mode, "2026-10-08T17:16:06Z")
    before = _intention_rows(store)
    replay = _sizing_projection(intents, forecast, mode, "2026-10-08T17:16:04.75Z")
    assert replay == original and replay["recorded_at"] == "2026-10-08T17:16:06+00:00"
    assert _intention_rows(store) == before


def test_interrupted_sizing_receipt_recovers_at_original_sizing_time(tmp_path, monkeypatch):
    store, forecast, intents, sized = _interrupted_first_sizing(tmp_path, monkeypatch)
    original = intents.get(forecast["forecast_id"])
    assert intents.save_sizing(forecast, sized) == original
    history = intents.history(forecast["forecast_id"])
    assert len(history) == 2 and history[-1]["kind"] == "sized"
    assert history[-1]["recorded_at"] == "2026-10-08T17:16:05+00:00"
    before = _intention_rows(store)
    assert intents.save_sizing(forecast, sized) == original
    assert _intention_rows(store) == before
