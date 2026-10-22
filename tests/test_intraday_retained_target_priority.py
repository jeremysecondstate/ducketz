"""Offline behavioral fixtures for exact saved-target scoring responsiveness."""
import pytest
from ml.intraday import live_session
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


def old_forecast(symbol):
    return forecast(mode='live',symbol=symbol,cycle_id='old-'+symbol,
        reference_at='2026-10-08T16:45:00+00:00',data_through='2026-10-08T17:00:00+00:00',
        issued_at='2026-10-08T17:01:00+00:00',target_open='2026-10-08T17:15:00+00:00',
        information_available_at='2026-10-08T17:00:03+00:00',
        target_close='2026-10-08T17:30:00+00:00')


def own(store,value):
    store.begin_cycle(value['cycle_id'],dict(pc=value['pc'],mode='live',experiment='manual',
        symbols=[value['symbol']],reference_at=value['reference_at'],configuration_hash='fixture',
        started_at=value['issued_at']))
    return store.save_forecast(value)


def test_saved_ready_target_scores_before_earlier_missing_provider_fetch(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/'ready-first.sqlite3')
    old=own(store,{**old_forecast('OLD'),'diagnostics':{'large':'retained model tree '*10000}})
    ready=own(store,forecast(mode='live',symbol='READY',cycle_id='ready'))
    store.record_candle(candle(symbol='READY',close='101'))
    validate=store._forecast_value
    def validated(row):
        if row[0]==old['forecast_id']:
            assert store.revisions(ready['forecast_id']), 'Unrelated history validation must follow ready-target scoring'
        return validate(row)
    monkeypatch.setattr(store,'_forecast_value',validated)
    calls=[];done=[]
    class Incoming:
        def __init__(self,**kw):pass
        def bind_cycle(self,key):assert key=='score:'+old['forecast_id']
        def get(self,symbol):
            calls.append((symbol,bool(store.revisions(ready['forecast_id']))))
            raise ContractError('Fixture historical target remains actually unavailable')
    monkeypatch.setattr(live_session,'LiveHistories',Incoming)
    live_session.scoring_loop(store,histories={'OLD':object()},provider=object(),journal=object(),
        session_id='manual',stopped=lambda:bool(done),clock=lambda:'2026-10-08T18:00:00Z',pause=lambda _:done.append(True))
    assert calls==[('OLD',True)], 'A durable ready target must score before historical acquisition'
    assert store.revisions(ready['forecast_id'])[-1]['target_not_down'] is True
    assert store.revisions(old['forecast_id'])==[]


def test_newly_matured_saved_target_scores_between_historical_acquisition_steps(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/'mature-between.sqlite3')
    old={s:own(store,old_forecast(s)) for s in ('OLD1','OLD2')}
    ready=own(store,forecast(mode='live',symbol='READY',cycle_id='ready'))
    clock=['2026-10-08T17:44:00Z'];calls=[];done=[]
    class Incoming:
        def __init__(self,**kw):pass
        def bind_cycle(self,key):pass
        def get(self,symbol):
            calls.append((symbol,bool(store.revisions(ready['forecast_id']))))
            if symbol=='OLD1':
                clock[0]='2026-10-08T17:46:00Z'
                store.record_candle(candle(symbol='READY',close='101'))
            raise ContractError('Fixture historical target remains actually unavailable')
    monkeypatch.setattr(live_session,'LiveHistories',Incoming)
    live_session.scoring_loop(store,histories={s:object() for s in old},provider=object(),journal=object(),
        session_id='manual',stopped=lambda:bool(done),clock=lambda:clock[0],pause=lambda _:done.append(True))
    assert calls==[('OLD1',False),('OLD2',True)], 'Newly matured ready target must score between historical fetches'
    assert store.revisions(ready['forecast_id'])[-1]['status']=='scored'
    assert all(store.revisions(v['forecast_id'])==[] for v in old.values())


@pytest.mark.parametrize('damage',['forecast','prior_revision'])
def test_ready_priority_preserves_exact_forecast_and_ancestry_validation(tmp_path,monkeypatch,damage):
    store=ShadowStore(tmp_path/'corrupt.sqlite3')
    ready=own(store,forecast(mode='live',symbol='READY',cycle_id='ready'))
    store.observe(ready,as_of='2026-10-08T17:46:00Z')
    store.record_candle(candle(symbol='READY',close='101'))
    with store.connect() as db:
        if damage=='forecast':db.execute("UPDATE forecasts SET payload=json_set(payload,'$.reference_open','999')")
        else:db.execute("UPDATE outcomes SET payload=json_set(payload,'$.status','wrong')")
    monkeypatch.setattr(live_session,'LiveHistories',lambda **kw:pytest.fail('Corrupt ready evidence cannot invoke a provider'))
    with pytest.raises(ContractError,match='hash or indexed identity'):
        live_session.scoring_loop(store,histories={},provider=object(),journal=object(),
            session_id='manual',stopped=lambda:False,clock=lambda:'2026-10-08T18:00:00Z',pause=lambda _:pytest.fail('must reject corruption'))


@pytest.mark.parametrize('change',[
    {'source_id':'other'}, {'source_dataset':'other'}, {'population_contract':'other'},
    {'available_at':'2026-10-08T18:01:00+00:00'}, {'interval_seconds':60},
    {'synthetic':True,'evidence_json':'{"legacy_confirmations":2}'},
])
def test_priority_excludes_unmatched_future_and_unverified_targets(tmp_path,monkeypatch,change):
    store=ShadowStore(tmp_path/'guards.sqlite3')
    ready=own(store,forecast(mode='live',symbol='READY',cycle_id='ready',source_id='source',source_dataset='EQUS.MINI'))
    target=candle(**{'symbol':'READY','source_dataset':'EQUS.MINI',**change})
    if 'population_contract' in change:
        # Preserve a legacy/foreign population row without asking the current
        # writer to publish an unsupported population contract.
        with store.connect() as db:
            db.execute('INSERT INTO candles VALUES(?,?,?,?,?)',
                (digest(target),target['symbol'],target['opened_at'],target['available_at'],encoded(target)))
    else:
        store.record_candle(target)
    done=[]
    monkeypatch.setattr(live_session,'LiveHistories',lambda **kw:pytest.fail('No histories/provider for guard fixture'))
    live_session.scoring_loop(store,histories={},provider=object(),journal=object(),
        session_id='manual',stopped=lambda:bool(done),clock=lambda:'2026-10-08T18:00:00Z',pause=lambda _:done.append(True))
    assert store.revisions(ready['forecast_id'])==[]


def test_ready_correction_preserves_probability_ancestry_and_actual_instants(tmp_path):
    store=ShadowStore(tmp_path/'correction.sqlite3')
    ready=own(store,forecast(mode='live',symbol='READY',cycle_id='ready',
        reference_at='2026-10-08T10:00:00-07:00',probability_not_down=.5001,
        models=[dict(name='a',status='success',probability_not_down=.5001),dict(name='b',status='success',probability_not_down=.5001)]))
    first=store.observe(ready,as_of='2026-10-08T17:46:00Z',candle=candle(symbol='READY',close='99'))
    corrected=candle(symbol='READY',close='101',available_at='2026-10-08T17:58:00+00:00')
    store.record_candle(corrected)
    selected=store.retained_session_scoring_targets(pc='Atlas',session_id='manual',as_of='2026-10-08T18:00:00Z')
    assert selected==[(ready,corrected)]
    result=store.observe(selected[0][0],as_of='2026-10-08T18:00:00Z',candle=selected[0][1])
    assert store.revisions(ready['forecast_id'])==[first,result]
    assert result['target_not_down'] is True
    assert store.forecast('ready','READY')==ready and ready['probability_not_down']==.5001
    assert store.retained_session_scoring_targets(pc='Atlas',session_id='manual',as_of='2026-10-08T18:00:00Z')==[]
    assert store.retained_session_scoring_targets(pc='Scout',session_id='manual',as_of='2026-10-08T18:00:00Z')==[]
    assert store.retained_session_scoring_targets(pc='Atlas',session_id='different',as_of='2026-10-08T18:00:00Z')==[]


@pytest.mark.parametrize('damage',['body','indexed_issue'])
def test_unchanged_scored_target_reuses_header_but_changed_evidence_revalidates(tmp_path,monkeypatch,damage):
    store=ShadowStore(tmp_path/'cached.sqlite3')
    ready=own(store,forecast(mode='live',symbol='READY',cycle_id='ready',diagnostics={'large':'model tree '*10000}))
    store.record_candle(candle(symbol='READY',close='101'))
    original=store._forecast_value;decodes=[]
    def checked(row):
        decodes.append(row[0]);return original(row)
    monkeypatch.setattr(store,'_forecast_value',checked)
    selected=store.retained_session_scoring_targets(pc='Atlas',session_id='manual',as_of='2026-10-08T18:00:00Z')
    store.observe(selected[0][0],as_of='2026-10-08T18:00:00Z',candle=selected[0][1])
    decodes.clear()
    assert store.retained_session_scoring_targets(pc='Atlas',session_id='manual',as_of='2026-10-08T18:00:00Z')==[]
    assert decodes==[]
    with store.connect() as db:
        if damage=='body':db.execute("UPDATE forecasts SET payload=json_set(payload,'$.reference_open','999')")
        else:db.execute("UPDATE forecasts SET issued_at='2026-10-08T17:16:03+00:00'")
    with pytest.raises(ContractError,match='hash or indexed identity'):
        store.retained_session_scoring_targets(pc='Atlas',session_id='manual',as_of='2026-10-08T18:00:00Z')
    assert decodes==[ready['forecast_id']]


@pytest.mark.parametrize('interrupt',['manual_stop','close'])
def test_interrupt_during_ready_drain_prevents_ordinary_scan_and_provider(tmp_path,monkeypatch,interrupt):
    store=ShadowStore(tmp_path/'drain-interrupt.sqlite3')
    ready=own(store,forecast(mode='live',symbol='READY',cycle_id='ready'))
    store.record_candle(candle(symbol='READY',close='101'))
    clock=['2026-10-08T18:00:00Z'];stopped=[False];observe=store.observe
    def observed(*args,**kwargs):
        result=observe(*args,**kwargs)
        if interrupt=='manual_stop':stopped[0]=True
        else:clock[0]='2026-10-09T00:00:00Z'
        return result
    monkeypatch.setattr(store,'observe',observed)
    monkeypatch.setattr(store,'scoring_forecasts',lambda **kw:pytest.fail('Interrupted drain must not start historical scanning'))
    monkeypatch.setattr(live_session,'LiveHistories',lambda **kw:pytest.fail('Interrupted drain must not construct acquisition'))
    live_session.scoring_loop(store,histories={},provider=object(),journal=object(),
        session_id='manual',stopped=lambda:stopped[0],clock=lambda:clock[0],
        close_at='2026-10-09T00:00:00Z',pause=lambda _:pytest.fail('Interrupted drain must return'))
    assert store.revisions(ready['forecast_id'])[-1]['status']=='scored'
