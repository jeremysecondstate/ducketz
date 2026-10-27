"""New publications seed optional exact-byte scalar proofs without execution IO."""
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import re
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday.forecast_header_cache import DurableForecastHeaders
from ml.intraday.prediction import ContractError,encoded,instant
from ml.intraday.store import ShadowStore
from ml.intraday.account_execution import IntradayAccountExecutor
from ml.intraday import intentions
from test_intraday_store import forecast

IntentionStore=intentions.IntentionStore
_RECOVERY_ROW_SCHEMA=getattr(intentions,'_RECOVERY_ROW_SCHEMA',None)


def owner(store):
    return SimpleNamespace(store=store,actor='Atlas',session_started_at=instant('2026-10-08T17:15:00Z'))


def selected(executor,current):return IntradayAccountExecutor._newer_forecasts(executor,current)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args,**kwargs):pytest.fail('Publication header proofs must remain offline')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(socket,'create_connection',denied)


def issued(store,name='current',offset=0,**changes):
    payload=forecast(cycle_id=name,mode='live',**changes)
    for field in ('reference_at','issued_at','information_available_at','data_through','target_open','target_close'):
        payload[field]=(instant(payload[field])+timedelta(minutes=offset)).isoformat()
    return store.save_forecast(payload)


def headers(store):return store.execution_forecast_headers(pc='Atlas',symbol='AAPL')


def reads(store,monkeypatch):
    result=[];original=store._forecast_value
    def observed(row):result.append(row[0]);return original(row)
    monkeypatch.setattr(store,'_forecast_value',observed)
    return result


@pytest.mark.parametrize('with_intentions',[False,True])
def test_new_scientific_publication_needs_no_duplicate_execution_tree_validation(tmp_path,monkeypatch,with_intentions):
    path=tmp_path/'new.sqlite3';producer=ShadowStore(path)
    if with_intentions:IntentionStore(producer)
    with producer.connect()as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]==(16 if with_intentions and _RECOVERY_ROW_SCHEMA is not None else 8)
    first=issued(producer)
    reader=ShadowStore(path);headers(reader)
    full=reads(reader,monkeypatch)
    later=issued(producer,'later',15,model_diagnostics={'synthetic_tree':[i/1000 for i in range(20000)]})
    frozen=encoded(later)
    assert selected(owner(reader),first)==[later['forecast_id']]
    assert full==[]
    assert encoded(producer.forecast('later','AAPL'))==frozen
    assert later['probability_not_down']==.72
    if 'direction' in later:
        assert later['direction']=='BULLISH'
    else:
        assert later['stock_action']=='BUY'


@pytest.mark.parametrize('with_intentions',[False,True])
def test_fresh_successor_uses_producer_proof_without_payload_read(tmp_path,monkeypatch,with_intentions):
    path=tmp_path/'fresh.sqlite3';producer=ShadowStore(path)
    if with_intentions:IntentionStore(producer)
    original=issued(producer)
    successor=ShadowStore(path);full=reads(successor,monkeypatch)
    assert headers(successor)[0]['forecast_id']==original['forecast_id'] and full==[]


def test_producer_does_not_reread_its_inserted_model_payload(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/'readback.sqlite3');statements=[];connect=store.connect
    @contextmanager
    def observed():
        with connect() as db:db.set_trace_callback(statements.append);yield db
    monkeypatch.setattr(store,'connect',observed)
    issued(store)
    insertion=next(i for i,s in enumerate(statements)if s.lstrip().startswith('INSERT INTO forecasts'))
    rereads=[s for s in statements[insertion+1:]if re.search(r'\bSELECT\b.*\bpayload\b.*\bFROM forecasts\b',s,re.I|re.S)]
    assert rereads==[]


@pytest.mark.parametrize('error',[OSError('optional cache unavailable'),sqlite3.OperationalError('database is locked'),RuntimeError('optional cache adapter failed')])
def test_postcommit_cache_failure_cannot_fail_original_scientific_publication(tmp_path,monkeypatch,error):
    store=ShadowStore(tmp_path/'optional.sqlite3')
    def unavailable(*args,**kwargs):raise error
    monkeypatch.setattr(DurableForecastHeaders,'save_many',unavailable)
    original=issued(store)
    assert store.forecast('current','AAPL')==original
    successor=ShadowStore(store.path);full=reads(successor,monkeypatch)
    # The ordinary reader's optional cache has its original exception contract;
    # remove this injected adapter failure before exercising full fallback.
    monkeypatch.undo()
    full=reads(successor,monkeypatch)
    assert headers(successor)[0]['forecast_id']==original['forecast_id']
    assert full==[original['forecast_id']]


@pytest.mark.parametrize('table,event,body',[
    ('forecasts','INSERT',"UPDATE forecasts SET payload='{}' WHERE id=NEW.id;"),
    ('intents','INSERT',"UPDATE forecasts SET payload='{}' WHERE id=NEW.forecast_id;"),
    ('intents','DELETE','SELECT 1;'),
])
def test_unfamiliar_direct_or_indirect_source_trigger_disables_attestation(tmp_path,monkeypatch,table,event,body):
    store=ShadowStore(tmp_path/'trigger.sqlite3')
    with store.connect() as db:
        db.execute(f'CREATE TRIGGER unexpected_mutation AFTER {event} ON {table} BEGIN {body} END')
    cache_writes=[];save=DurableForecastHeaders.save_many
    monkeypatch.setattr(DurableForecastHeaders,'save_many',lambda *a,**k:cache_writes.append(True))
    original=issued(store)
    assert cache_writes==[]
    monkeypatch.setattr(DurableForecastHeaders,'save_many',save)
    cold=ShadowStore(store.path);full=reads(cold,monkeypatch)
    if event=='INSERT':
        with pytest.raises(ContractError,match='hash or indexed identity'):headers(cold)
    else:
        assert headers(cold)[0]['forecast_id']==original['forecast_id']
    assert full==[original['forecast_id']]


def test_changed_original_revision_cannot_reuse_producer_proof(tmp_path):
    store=ShadowStore(tmp_path/'changed.sqlite3');original=issued(store)
    with sqlite3.connect(store.path)as db:db.execute("UPDATE forecasts SET payload='{}' WHERE id=?",(original['forecast_id'],))
    with pytest.raises(ContractError,match='hash or indexed identity'):headers(ShadowStore(store.path))


def test_unavailable_source_identity_skips_proof_without_holding_science(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/'uncertain.sqlite3');capture=store._published_forecast_header_proof
    def uncertain(db,value):
        store._connection_source_identities[id(db)]=None
        return capture(db,value)
    monkeypatch.setattr(store,'_published_forecast_header_proof',uncertain)
    original=issued(store);cold=ShadowStore(store.path);full=reads(cold,monkeypatch)
    assert headers(cold)[0]['forecast_id']==original['forecast_id'] and full==[original['forecast_id']]


def test_missing_cache_proof_retains_full_original_fallback(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/'missing.sqlite3');original=issued(store)
    helper=DurableForecastHeaders(store.path);helper.key_path.unlink()
    cold=ShadowStore(store.path);full=reads(cold,monkeypatch)
    assert headers(cold)[0]['forecast_id']==original['forecast_id'] and full==[original['forecast_id']]


def test_invalid_scientific_consensus_still_fails_before_any_optional_proof(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/'invalid.sqlite3');cache=[]
    monkeypatch.setattr(DurableForecastHeaders,'save_many',lambda *a,**k:cache.append(True))
    with pytest.raises(ContractError,match='equal arithmetic mean'):
        issued(store,probability_not_down=.9)
    assert cache==[]
    with store.connect()as db:assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0]==0


def test_incoming_fake_proof_fields_never_replace_observed_scientific_values(tmp_path):
    store=ShadowStore(tmp_path/'injected.sqlite3')
    original=issued(store,header_proof={'mode':'shadow','reference_open':'999','issued_at':'wrong'},
                    target_elapsed_before_broker_capture={'fake':True})
    scalar=headers(ShadowStore(store.path))[0]
    assert scalar['mode']=='live' and scalar['reference_open']=='100'
    assert scalar['issued_at']==original['issued_at']
    assert store.forecast('current','AAPL')==original


@pytest.mark.parametrize('damage',['partial_schema','missing_trigger','missing_revisions_table','changed_table','missing_metadata'])
def test_partial_or_malformed_known_recovery_schema_skips_optional_proof(tmp_path,monkeypatch,damage):
    store=ShadowStore(tmp_path/'incomplete.sqlite3')
    if damage=='partial_schema':
        with store.connect()as db:db.execute('CREATE TABLE recovery_sizing_generation(singleton INTEGER PRIMARY KEY,generation INTEGER,epoch TEXT)')
    else:
        if _RECOVERY_ROW_SCHEMA is None:
            pytest.skip('This base has no installed recovery revision capability; full16-trigger cases are validated separately on the actual Atlas composition')
        IntentionStore(store)
        with store.connect()as db:
            if damage=='missing_trigger':db.execute('DROP TRIGGER recovery_sizing_delete')
            elif damage=='missing_revisions_table':db.execute('DROP TABLE recovery_sizing_revisions')
            elif damage=='changed_table':db.execute('ALTER TABLE recovery_sizing_generation ADD COLUMN unexpected TEXT')
            else:
                db.execute('DROP TRIGGER recovery_generation_delete')
                db.execute('DELETE FROM recovery_sizing_generation')
                db.execute(_RECOVERY_ROW_SCHEMA['recovery_generation_delete'])
    cache_writes=[];save=DurableForecastHeaders.save_many
    monkeypatch.setattr(DurableForecastHeaders,'save_many',lambda *a,**k:cache_writes.append(True))
    original=issued(store)
    assert cache_writes==[]
    monkeypatch.setattr(DurableForecastHeaders,'save_many',save)
    cold=ShadowStore(store.path);full=reads(cold,monkeypatch)
    assert headers(cold)[0]['forecast_id']==original['forecast_id'] and full==[original['forecast_id']]
