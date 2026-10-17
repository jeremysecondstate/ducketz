"""Continuous saved-data preparation uses no provider or trading connection."""
from dataclasses import replace
import hashlib
import json
import sqlite3

import pandas as pd
import pytest

from ml.intraday.data import AcquisitionJournal, IntervalResponse
from ml.intraday.preparation_watch import SavedHistorySource, current_reference, run_preparation
from ml.intraday.prediction import ContractError
from ml.intraday.runtime import ResearchConfig
from ml.intraday import live_model_recipe as recipes
from tests.test_intraday_market_recipe import candidate, frame, save


def publish(journal, candle):
    opened=pd.Timestamp(candle['opened_at'])
    closed=opened+pd.Timedelta(minutes=15)
    observations=json.loads(candle['evidence_json'])['observations']
    first=IntervalResponse(str(opened)+'-first',candle['symbol'],'databento:EQUS.MINI:ohlcv-1m','EQUS.MINI',
        opened.isoformat(),closed.isoformat(),(closed+pd.Timedelta(seconds=1)).isoformat(),
        (closed+pd.Timedelta(seconds=2)).isoformat(),'success',tuple(observations),opened.isoformat(),closed.isoformat())
    journal.save(first)
    journal.save(replace(first,attempt_id=str(opened)+'-second',
        request_started_at=(closed+pd.Timedelta(seconds=6)).isoformat(),
        request_ended_at=(closed+pd.Timedelta(seconds=7)).isoformat()))


def test_saved_journal_three_quarters_prepare_new_labels_and_restart_without_refetch(tmp_path):
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    base=data.iloc[:-3].copy()
    fresh=data.iloc[-3:].to_dict('records')
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    state={'index':0,'stop':False}
    results=[]
    publish(journal,fresh[0])
    def clock():
        return pd.Timestamp(fresh[state['index']]['opened_at'])+pd.Timedelta(minutes=16)
    def pause(seconds):
        state['index']+=1
        publish(journal,fresh[state['index']])
    def emit(result):
        results.append(result)
        state['stop']=len(results)==3
    options=dict(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':base},
                 config=config,model_runtime=runtime,clock=clock,stopped=lambda:state['stop'])
    result=run_preparation(**options,pause=pause,emit=emit)
    assert result['cycles_prepared']==3 and result['trading'] is False
    counts=[r['results']['TEST']['training_rows'] for r in results]
    assert counts[0]<counts[1]<counts[2]
    assert all(r['results']['TEST']['status']=='prepared' for r in results)
    assert len({r['results']['TEST']['latest_mature_label'] for r in results})==3
    assert all(r['results']['TEST']['missing_inputs'] for r in results)
    assert not (tmp_path/'intraday/shadow.sqlite3').exists()
    # A new reader recovers saved actual candle clocks and skips a durable completed cycle.
    before=hashlib.sha256(journal.path.read_bytes()).hexdigest()
    state['stop']=False
    restarted=run_preparation(**options,pause=lambda seconds:state.update(stop=True),emit=results.append)
    assert restarted['cycles_prepared']==0 and len(results)==3
    assert hashlib.sha256(journal.path.read_bytes()).hexdigest()==before
    restored,receipt=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),
        histories={'TEST':base}).refresh(clock())
    assert receipt['source_rows_read']==0
    assert restored['TEST'].frame.iloc[-1].available_at==pd.Timestamp(fresh[-1]['opened_at'])+pd.Timedelta(minutes=15,seconds=7)


def test_saved_source_batches_and_rejects_journal_replacement(tmp_path, monkeypatch):
    from ml.intraday import preparation_watch as watch
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    for candle in data.iloc[-3:].to_dict('records'):
        publish(journal,candle)
    monkeypatch.setattr(watch,'BATCH_ROWS',2)
    reader=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data.iloc[:-3]})
    cutoff=pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16)
    for expected_backlog in (True,True,False):
        histories,evidence=reader.refresh(cutoff)
        assert evidence['source_rows_read']==2 and evidence['backlog'] is expected_backlog
    assert len(histories['TEST'].frame)==len(data)
    with sqlite3.connect(journal.path) as db:
        db.execute('DELETE FROM attempts WHERE rowid=(SELECT max(rowid) FROM attempts)')
    with pytest.raises(ContractError,match='reset or revised'):
        reader.refresh(cutoff)


def test_invalid_saved_native_observation_does_not_starve_later_valid_quarter(tmp_path):
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    bad=data.iloc[-2].to_dict()
    evidence=json.loads(bad['evidence_json'])
    evidence['observations'][0]['open']=0
    bad['evidence_json']=json.dumps(evidence)
    publish(journal,bad)
    publish(journal,data.iloc[-1].to_dict())
    reader=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data.iloc[:-2]})
    histories,receipt=reader.refresh(pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16))
    assert len(histories['TEST'].frame)==len(data)-1
    assert any(o['status']=='unusable' for o in receipt['outcomes'])
    assert receipt['outcomes'][-1]['status']=='observed'


def test_exact_repeated_snapshot_preserves_earliest_receipt_and_conflict_is_excluded(tmp_path,monkeypatch):
    from ml.intraday import preparation_watch as watch
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    candle=data.iloc[-1].to_dict()
    publish(journal,candle)
    reader=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data})
    cutoff=pd.Timestamp(candle['opened_at'])+pd.Timedelta(minutes=17)
    histories,first=reader.refresh(cutoff)
    earliest=histories['TEST'].frame.iloc[-1].available_at
    with sqlite3.connect(journal.path) as db:
        value=json.loads(db.execute('SELECT payload FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0])
    # More retries than one page cannot make the watcher fail or move the clock.
    monkeypatch.setattr(watch,'MAX_SLOT_ATTEMPTS',2)
    for index in range(3):
        later=replace(IntervalResponse(**value),attempt_id='repeat-'+str(index),
            request_started_at=(earliest+pd.Timedelta(seconds=5+5*index)).isoformat(),
            request_ended_at=(earliest+pd.Timedelta(seconds=6+5*index)).isoformat())
        journal.save(later)
    histories,again=reader.refresh(cutoff)
    assert histories['TEST'].frame.iloc[-1].available_at==earliest
    assert again['projection_revision']==first['projection_revision']
    broken=replace(later,attempt_id='invalid-revision',
        request_started_at=(earliest+pd.Timedelta(seconds=30)).isoformat(),
        request_ended_at=(earliest+pd.Timedelta(seconds=31)).isoformat(),
        rows=({**later.rows[0],'open':0},))
    journal.save(broken)
    histories,rejected=reader.refresh(cutoff)
    assert pd.Timestamp(candle['opened_at']) not in set(histories['TEST'].frame.opened_at)
    assert rejected['projection_revision']>again['projection_revision']


def test_reverted_native_snapshot_retains_new_revision_clock_on_restart(tmp_path):
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    original=data.iloc[-1].to_dict()
    publish(journal,original)
    reader=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data})
    close=pd.Timestamp(original['opened_at'])+pd.Timedelta(minutes=15)
    reader.refresh(close+pd.Timedelta(seconds=10))
    with sqlite3.connect(journal.path) as db:
        responses=[IntervalResponse(**json.loads(row[0])) for row in db.execute('SELECT payload FROM attempts ORDER BY rowid')]
    for offset,changed in ((20,True),(40,False)):
        for response in responses:
            rows=response.rows
            if changed:
                rows=({**rows[0],**{name:rows[0][name]+1 for name in ('open','high','low','close')}},*rows[1:])
            journal.save(replace(response,attempt_id=response.attempt_id+'-'+str(offset),rows=rows,
                request_started_at=(pd.Timestamp(response.request_started_at)+pd.Timedelta(seconds=offset)).isoformat(),
                request_ended_at=(pd.Timestamp(response.request_ended_at)+pd.Timedelta(seconds=offset)).isoformat()))
    expected=close+pd.Timedelta(seconds=47)
    for source in (reader,SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data})):
        histories,_=source.refresh(close+pd.Timedelta(minutes=1))
        final=histories['TEST'].frame.iloc[-1]
        assert final.available_at==expected
        assert json.loads(final.evidence_json)['coverage']['revision_boundary']['available_at']==(close+pd.Timedelta(seconds=27)).isoformat()
    # A separately pinned current-revision candle may already have the right
    # clock but lack the newly derived revision boundary. Retain both proofs.
    pinned=histories['TEST'].frame.copy()
    evidence=json.loads(pinned.evidence_json.iloc[-1])
    evidence['coverage'].pop('revision_boundary')
    pinned.loc[pinned.index[-1],'evidence_json']=json.dumps(evidence)
    fresh=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':pinned},
                             output_root=tmp_path/'separate-checkpoint')
    restored,_=fresh.refresh(close+pd.Timedelta(minutes=1))
    assert restored['TEST'].frame.iloc[-1].available_at==expected
    assert 'revision_boundary' in json.loads(restored['TEST'].frame.iloc[-1].evidence_json)['coverage']


def test_malformed_unrelated_and_selected_attempts_do_not_starve_valid_observations(tmp_path):
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    with sqlite3.connect(journal.path) as db:
        for symbol in ('OTHER','TEST'):
            db.execute('INSERT INTO attempts VALUES(?,?,?)',(symbol,'invalid',json.dumps({'symbol':symbol,
                'source_dataset':'EQUS.MINI','source_id':'databento:EQUS.MINI:ohlcv-1m'})))
    publish(journal,data.iloc[-1].to_dict())
    reader=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data.iloc[:-1]})
    histories,receipt=reader.refresh(pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16))
    assert len(histories['TEST'].frame)==len(data)
    assert [item['symbol'] for item in receipt['quarantined']]==['TEST']
    assert receipt['cursor']['rowid']==4


def test_already_read_future_receipt_matures_without_cursor_change(tmp_path):
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    candle=data.iloc[-1].to_dict()
    publish(journal,candle)
    reader=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data.iloc[:-1]})
    close=pd.Timestamp(candle['opened_at'])+pd.Timedelta(minutes=15)
    before,first=reader.refresh(close+pd.Timedelta(seconds=3))
    after,second=reader.refresh(close+pd.Timedelta(seconds=8))
    assert first['cursor']==second['cursor']
    assert len(before['TEST'].frame)+1==len(after['TEST'].frame)
    assert second['projection_revision']>first['projection_revision']


def test_earlier_verified_same_snapshot_replaces_later_base_clock(tmp_path):
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    publish(journal,data.iloc[-1].to_dict())
    actual=pd.Timestamp(data.available_at.iloc[-1])
    later=actual+pd.Timedelta(seconds=20)
    data.loc[data.index[-1],'available_at']=later.isoformat()
    evidence=json.loads(data.evidence_json.iloc[-1])
    evidence['coverage']['request_ended_at']=later.isoformat()
    data.loc[data.index[-1],'evidence_json']=json.dumps(evidence)
    reader=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data})
    histories,_=reader.refresh(later+pd.Timedelta(seconds=1))
    assert histories['TEST'].frame.iloc[-1].available_at==actual


def test_normal_opted_loader_bridge_restores_journal_clock_without_scratch_dependency(tmp_path):
    from ml.intraday.data import History
    from ml.intraday.preparation_watch import overlay_saved_native_history
    data=frame()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    publish(journal,data.iloc[-1].to_dict())
    installed=tmp_path/'intraday/scout/minute-recovery/installed.json'
    installed.parent.mkdir(parents=True)
    raw=json.dumps({'pc':'Scout','symbols':['TEST']}).encode()
    installed.write_bytes(raw)
    history=History(data.iloc[:-1],{'recovery':{'manifest_sha256':hashlib.sha256(raw).hexdigest()}})
    first=overlay_saved_native_history(history,root=tmp_path,symbol='TEST',pc='Scout')
    second=overlay_saved_native_history(history,root=tmp_path,symbol='TEST',pc='Scout')
    assert len(first.frame)==len(data)
    pd.testing.assert_frame_equal(first.frame,second.frame)
    assert first.frame.iloc[-1].available_at==pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=15,seconds=7)
    assert (tmp_path/'intraday/scout/native-actual/saved-observations.sqlite3').exists()


def test_later_loaded_pinned_base_preserves_earlier_identical_actual_receipt(tmp_path):
    data=frame()
    candle=data.iloc[-1].to_dict()
    journal=AcquisitionJournal(tmp_path/'intraday/acquisition.sqlite3')
    publish(journal,candle)
    # Normal startup can checkpoint journal slots before a sibling's pinned
    # base is loaded. Give the journal two genuine later identical attempts.
    with sqlite3.connect(journal.path) as db:
        rows=db.execute('SELECT payload FROM attempts ORDER BY rowid').fetchall()
        db.execute('DELETE FROM attempts')
    for row in rows:
        response=IntervalResponse(**json.loads(row[0]))
        journal.save(replace(response,
            request_started_at=(pd.Timestamp(response.request_started_at)+pd.Timedelta(seconds=20)).isoformat(),
            request_ended_at=(pd.Timestamp(response.request_ended_at)+pd.Timedelta(seconds=20)).isoformat()))
    cutoff=pd.Timestamp(candle['opened_at'])+pd.Timedelta(minutes=16)
    empty=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={})
    _,first=empty.refresh(cutoff)
    loaded=SavedHistorySource(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data})
    histories,second=loaded.refresh(cutoff)
    assert first['cursor']==second['cursor'] and second['source_rows_read']==0
    assert histories['TEST'].frame.iloc[-1].available_at==pd.Timestamp(candle['available_at'])
    assert histories['TEST'].frame.iloc[-1].evidence_json==candle['evidence_json']


def test_transient_fit_retries_same_inputs_with_bounded_backoff(tmp_path):
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    state={'now':pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16),'stop':False}
    results=[]
    calls=[]
    def trainer(*args,**kwargs):
        calls.append(kwargs['run_id'])
        if len(calls)==1:
            return {'complete':False,'outputs':[{'name':'fixture','status':'failed',
                'error':{'type':'OSError','phase':'persistence','message':'temporary locked file'}}]}
        path=tmp_path/'model.bin'
        path.write_bytes(b'model')
        return {'complete':True,'outputs':[{'name':'fixture','status':'success','artifact_path':str(path),
            'artifact_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}
    def pause(seconds):
        state['now']+=pd.Timedelta(seconds=seconds)
    def emit(result):
        results.append(result)
        state['stop']=len(results)==2
    run_preparation(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},
        config=config,model_runtime={**runtime,'trainer':trainer},fit=True,
        stopped=lambda:state['stop'],clock=lambda:state['now'],pause=pause,emit=emit)
    first,second=[r['results']['TEST'] for r in results]
    assert first['status']=='fit_incomplete' and second['status']=='prepared'
    assert first['training_key']==second['training_key'] and len(calls)==2
    assert pd.Timestamp(results[1]['cutoff'])-pd.Timestamp(results[0]['cutoff'])>=pd.Timedelta(seconds=60)


def test_watcher_lifetime_lock_prevents_second_instance_and_releases_on_exit(tmp_path):
    from filelock import FileLock, Timeout
    root=tmp_path/'watch'
    root.mkdir()
    with FileLock(str(root/'watch.lock'),timeout=0):
        with pytest.raises(Timeout):
            run_preparation(datastore=tmp_path,pc='Scout',output_root=root)
    # A failed startup also releases the lifetime lock.
    with pytest.raises(TypeError):
        run_preparation(datastore=tmp_path,pc='Scout',output_root=root)
    with FileLock(str(root/'watch.lock'),timeout=0):
        pass


@pytest.mark.parametrize('retryable',[False,True])
def test_restart_promotes_final_verified_attempt_after_marker_write_crash(tmp_path,monkeypatch,retryable):
    from ml.intraday import preparation
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    state={'now':pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16),'stop':False}
    calls=[]
    def trainer(*args,**kwargs):
        calls.append(kwargs['run_id'])
        if not retryable:
            raise ContractError('deterministic fixture rejection')
        return {'complete':False,'outputs':[{'name':'fixture','status':'failed',
            'error':{'type':'OSError' if retryable else 'InsufficientTrainingData',
                     'phase':'fit','message':'fixture failure'}}]}
    original=preparation._write_once
    def write(path,value):
        if path.parent.name=='watch-completed':
            raise RuntimeError('simulated crash before completion marker')
        return original(path,value)
    monkeypatch.setattr(preparation,'_write_once',write)
    def pause(seconds):
        state['now']+=pd.Timedelta(seconds=seconds)
    options=dict(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},
        config=config,model_runtime={**runtime,'trainer':trainer},fit=True,
        stopped=lambda:state['stop'],clock=lambda:state['now'])
    with pytest.raises(RuntimeError,match='simulated crash'):
        run_preparation(**options,pause=pause)
    expected=3 if retryable else 1
    assert len(calls)==expected
    monkeypatch.setattr(preparation,'_write_once',original)
    result=run_preparation(**options,pause=lambda seconds:state.update(stop=True))
    assert result['cycles_prepared']==0 and len(calls)==expected
    assert len(list((tmp_path/'intraday/scout/preparation/watch-completed').glob('*.json')))==1


def test_late_context_receipt_prepares_same_quarter_again(tmp_path):
    from ml.intraday.market_collection import canonical
    from ml.intraday.market_collection_index import initialize_receipt_index,index_receipt
    from tests.test_intraday_market_context import sealed,quote
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    state={'now':pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16),'stop':False}
    results=[]
    def pause(seconds):
        state['now']+=pd.Timedelta(seconds=1)
        root=tmp_path/'intraday/scout/market-data'
        initialize_receipt_index(root)
        snapshot=sealed(symbol='TEST',source_id='schwab:quote',receipt_at=state['now'].isoformat(),
            available_at=state['now'].isoformat(),rows=[quote(symbol='TEST',event_at=state['now'].isoformat())])
        path=root/'snapshots/2026-05-11/receipt-1/receipt-1.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(canonical(snapshot))
        index_receipt(root,path,snapshot)
    def emit(result):
        results.append(result)
        state['stop']=len(results)==2
    run_preparation(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},
        config=config,model_runtime=runtime,stopped=lambda:state['stop'],clock=lambda:state['now'],
        pause=pause,emit=emit)
    assert results[0]['reference_at']==results[1]['reference_at']
    assert results[0]['results']['TEST']['training_key']==results[1]['results']['TEST']['training_key']
    assert results[0]['results']['TEST']['inference_matrix_sha256']!=results[1]['results']['TEST']['inference_matrix_sha256']


def test_delayed_calendar_reference_prepares_latest_saved_row_and_restart_skips(tmp_path):
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame()
    now=pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=46)
    state={'stop':False};results=[]
    options=dict(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},
        config=config,model_runtime=runtime,stopped=lambda:state['stop'],clock=lambda:now)
    run_preparation(**options,pause=lambda seconds:state.update(stop=True),emit=results.append)
    assert len(results)==1
    item=results[0]['results']['TEST']
    assert item['status']=='prepared' and item['preparation_only']
    assert pd.Timestamp(item['reference_at'])==pd.Timestamp(data.opened_at.iloc[-1])
    assert item['expected_reference_at']==current_reference(now).isoformat()
    assert item['missing_expected_reference']
    assert any(v['status']=='missing_expected_reference' for v in item['missing_inputs'])
    state['stop']=False
    restarted=run_preparation(**options,pause=lambda seconds:state.update(stop=True),emit=results.append)
    assert restarted['cycles_prepared']==0 and len(results)==1


def test_saved_reference_selector_keeps_recovered_provenance_and_excludes_future_synthetic():
    from ml.intraday.preparation_watch import latest_saved_references
    data=frame().iloc[-3:].copy()
    cutoff=pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16)
    data.loc[data.index[-2],'availability_kind']='actual_historical_recovery'
    data.loc[data.index[-1],'available_at']=(cutoff+pd.Timedelta(minutes=1)).isoformat()
    selected=latest_saved_references({'TEST':data},expected_reference_at=cutoff,cutoff=cutoff)
    assert selected['TEST']==pd.Timestamp(data.opened_at.iloc[-2])
    data.loc[data.index[-2],'synthetic']=True
    selected=latest_saved_references({'TEST':data},expected_reference_at=cutoff,cutoff=cutoff)
    assert selected['TEST']==pd.Timestamp(data.opened_at.iloc[-3])


def test_empty_selected_reference_map_records_missing_expected_observation(tmp_path):
    from ml.intraday.preparation import prepare_cycle
    from ml.intraday.preparation_watch import latest_saved_references
    save(tmp_path,candidate())
    config,runtime=recipes.prepare_manual_models(ResearchConfig(),datastore=tmp_path,pc='Scout',symbols=('TEST',))
    data=frame().iloc[-1:].copy()
    cutoff=pd.Timestamp(data.opened_at.iloc[-1])+pd.Timedelta(minutes=16)
    data.loc[data.index[-1],'available_at']=(cutoff+pd.Timedelta(minutes=1)).isoformat()
    selected=latest_saved_references({'TEST':data},expected_reference_at=cutoff,cutoff=cutoff)
    assert selected=={}
    result=prepare_cycle(datastore=tmp_path,pc='Scout',symbols=('TEST',),histories={'TEST':data},
        reference_at=cutoff,cutoff=cutoff,config=config,model_runtime=runtime,reference_by_symbol=selected)
    item=result['results']['TEST']
    assert item['status']=='unavailable' and item['reference_at'] is None
    assert item['missing_expected_reference'] and item['no_eligible_saved_reference']


@pytest.mark.parametrize('at,expected',[
    ('2026-05-11T14:16Z','2026-05-11T14:00Z'),
    ('2026-10-17T14:16Z','2026-10-16T23:45Z'),
    ('2026-12-25T14:16Z','2026-12-24T17:45Z'),
    ('2026-11-27T18:01Z','2026-11-27T17:45Z'),
    ('2026-03-06T09:16Z','2026-03-06T09:00Z'),
    ('2026-03-09T08:16Z','2026-03-09T08:00Z'),
])
def test_current_reference_respects_calendar_dst_extended_and_target(at,expected):
    actual=current_reference(at)
    assert actual==(pd.Timestamp(expected) if expected else None)
