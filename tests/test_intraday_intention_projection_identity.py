"""Immutable projections distinguish first sizing while preserving uncertain orders."""
from dataclasses import asdict
from test_intraday_account_execution import env,native,account,advance
from test_intraday_unknown_commitment_containment import uncertain
from ml.intraday.prediction import ContractError,digest,encoded
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
