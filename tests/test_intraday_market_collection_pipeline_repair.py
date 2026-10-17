from datetime import datetime,timedelta,timezone
import json
import threading
from ml.intraday.market_collection import QuarterHourMarketCollector, CollectionResult, SourceRequest, _select_tasks, canonical
from ml.intraday.market_collection_schwab_stream import stream_requests, SchwabStreamCollector
START=datetime(2026,10,16,19,45,tzinfo=timezone.utc)
NOW=START+timedelta(minutes=15,seconds=10)

def req(provider='fmp',kind='stock_quote',endpoint='quote',schema='quote',symbol='SDGR'):
    return SourceRequest(provider,kind,symbol,'fixture',schema,endpoint)

def test_fresh_market_lanes_and_background_make_progress():
    requests=[req(schema=str(i),kind='fundamentals') for i in range(20)]
    requests += [req('schwab',endpoint='stream'), req('schwab'),req('databento'),req()]
    due=[(str(i),{'request':r.__dict__,'quarter_start':START.isoformat(),'attempts':0}) for i,r in enumerate(requests)]
    selected=_select_tasks(due,8)
    assert len(selected)==8 and len({k for k,_ in selected})==8
    assert all(any(t['request']==r.__dict__ for _,t in selected) for r in requests[-4:])
    assert any(t['request']['source_kind']=='fundamentals' for _,t in selected)

def test_slow_stream_does_not_hold_rest_slot(tmp_path):
    entered,rest=threading.Event(),threading.Event()
    def adapter(request,*args):
        if request.endpoint=='stream':
            entered.set()
            assert rest.wait(2), 'REST quote was starved by stream bootstrap'
        else:
            assert entered.wait(2)
            rest.set()
        return CollectionResult([])
    requests=[req('schwab',endpoint='stream'),req('schwab')]
    col=QuarterHourMarketCollector(tmp_path,symbols=['SDGR'],requests=requests,adapters={'schwab':adapter},clock=lambda:NOW)
    col.enqueue(START)
    assert len(col.step(max_requests=2))==2

def test_partial_current_window_settles_and_recovers_without_replaying(tmp_path):
    calls=[]
    request=req('schwab',kind='options',endpoint='stream',schema='OPTIONS_BOOK')
    def adapter(*args):
        calls.append(1)
        return CollectionResult([{'symbol':'SDGR','event_at':START.isoformat()}], 'PARTIAL', 'scope bounded',
            {'mode':'current_stream_snapshot','completed':True,'omitted_scope_count':20})
    clock=[NOW]
    col=QuarterHourMarketCollector(tmp_path,symbols=['SDGR'],requests=[request],adapters={'schwab':adapter},clock=lambda:clock[0])
    col.enqueue(START); first=col.step()[0]
    assert first['status']=='PARTIAL' and first['window_finalized']
    clock[0]+=timedelta(minutes=1)
    assert col.step()==[]
    state=json.loads(col.state_path.read_text()); task=next(iter(state['tasks'].values()))
    task.update(status='QUEUED',window_finalized=False,attempts=0,snapshots=[],due_at=NOW.isoformat())
    col.state_path.write_bytes(canonical(state))
    assert col.step()[0]['snapshot_id']==first['snapshot_id'] and len(calls)==1
    clock[0]+=timedelta(minutes=15); col.enqueue(START+timedelta(minutes=15))
    assert col.step()[0]['status']=='PARTIAL' and len(calls)==2

def test_partial_failed_capture_remains_retryable(tmp_path):
    count=[];clock=[NOW]
    def adapter(*args):
        count.append(1)
        return CollectionResult([], 'PARTIAL', 'interrupted', {'mode':'current_stream_snapshot','completed':False})
    col=QuarterHourMarketCollector(tmp_path,symbols=['SDGR'],requests=[req('schwab',endpoint='stream')],adapters={'schwab':adapter},clock=lambda:clock[0])
    col.enqueue(START);assert not col.step()[0]['window_finalized']
    clock[0]+=timedelta(seconds=31);col.step();assert len(count)==2

def test_stream_chain_bootstrap_is_bounded_parallel_and_failure_scoped(tmp_path):
    first,second=threading.Event(),threading.Event()
    requests=stream_requests(['SDGR','RR'])
    def chain(session,symbol):
        if symbol=='RR':
            first.set();assert second.wait(2)
            raise TimeoutError()
        second.set();assert first.wait(2)
        return {'callExpDateMap':{'date':{'strike':[{'symbol':'SDGR  261120C00030000'}]}},'putExpDateMap':{}}
    def capture(session,scopes,*args,**kwargs):
        assert list(scopes['LEVELONE_OPTIONS'].values())==['SDGR']
        return {},{'service_results':{name:{'status':'COMPLETE'} for name in scopes}}
    col=SchwabStreamCollector(tmp_path,requests,capture_fn=capture,chain_fetch=chain)
    request=next(r for r in requests if r.symbol=='RR' and r.schema=='LEVELONE_OPTIONS')
    result=col(None,request,START,START+timedelta(minutes=15),100)
    assert result.status=='UNAVAILABLE'
    assert result.evidence['excluded_option_scopes']['RR']['status']=='UNAVAILABLE'

def test_requests_timeouts_and_http_denials_are_distinguished():
    import requests
    from ml.intraday.market_collection import classify_error
    assert classify_error(requests.exceptions.ReadTimeout('private authenticated URL'))[0]=='UNAVAILABLE'
    for code,needle in [(401,'authentication'),(402,'subscription'),(429,'rate limit')]:
        response=requests.Response();response.status_code=code
        status,reason=classify_error(requests.HTTPError('private authenticated URL',response=response))
        assert needle in reason.lower() and 'private' not in reason

def test_complete_fmp_fixed_slice_is_partial_but_not_retried(tmp_path):
    from ml.intraday.market_collection_extended import collect_extended
    request=SourceRequest('fmp','fundamentals','SDGR','stable','ratings-historical','ratings-historical',{'symbol':'SDGR','limit':100})
    calls=[];clock=[NOW]
    def adapter(request,start,end,bound):
        def fetch(*args):
            calls.append(1)
            return CollectionResult([{'symbol':'SDGR','raw':{'score':i}} for i in range(100)])
        return collect_extended(request,start,end,bound,fmp_fetch_page=fetch)
    col=QuarterHourMarketCollector(tmp_path,symbols=['SDGR'],requests=[request],adapters={'fmp':adapter},clock=lambda:clock[0])
    col.enqueue(START);receipt=col.step()[0]
    assert receipt['status']=='PARTIAL' and receipt['record_count']==100 and receipt['window_finalized']
    assert receipt['evidence']['bounded_response_complete']
    clock[0]+=timedelta(minutes=1);assert col.step()==[] and len(calls)==1

def test_option_native_type_mapping_keeps_cumulative_volume_explicit():
    from ml.intraday.market_collection_schwab_stream import normalize,NORMALIZATION_VERSION
    option='SDGR  261120C00030000'
    for value,expected in [('C','call'),('P','put'),('CALL','call'),('PUT','put'),('?',None)]:
        row=normalize('LEVELONE_OPTIONS',{'key':option,'21':value,'8':57},receipt_at=NOW.isoformat(),scope={option:'SDGR'})
        assert row['option_type']==expected
        assert row['day_volume']==57 and 'volume' not in row
        assert 'not quarter' in row['volume_basis'] and row['normalization_version']==NORMALIZATION_VERSION
        assert row['raw']['21']==value

def test_normalization_revision_never_reuses_old_projection_cache(tmp_path,monkeypatch):
    import ml.intraday.market_collection_schwab_stream as stream
    request=next(r for r in stream.stream_requests(['SDGR']) if r.schema=='LEVELONE_EQUITIES')
    calls=[]
    def fake(*args,**kwargs):
        calls.append(1)
        return {},{'service_results':{'LEVELONE_EQUITIES':{'status':'COMPLETE'}}}
    first=stream.SchwabStreamCollector(tmp_path,[request],capture_fn=fake)
    first(None,request,START,NOW,100)
    monkeypatch.setattr(stream,'NORMALIZATION_VERSION','next-reviewed-revision')
    second=stream.SchwabStreamCollector(tmp_path,[request],capture_fn=fake)
    second(None,request,START,NOW,100)
    assert len(calls)==2 and len(list((tmp_path/'responses').rglob('*.json')))==2

def test_book_rotation_keeps_small_underlyings_present_and_covers_their_tails():
    from ml.intraday.market_collection_schwab_stream import _scope_keys
    scopes={f'STOCK{s:02d}-{c:04d}':f'STOCK{s:02d}' for s in range(11) for c in range(13 if s==10 else 100)}
    seen=set()
    for n in range(20):
        selected=_scope_keys('OPTIONS_BOOK',scopes,START+timedelta(minutes=15*n))[:100]
        assert {scopes[k] for k in selected}==set(scopes.values())
        seen.update(selected)
    assert seen==set(scopes)

def test_fmp_wrapped_http_status_retained_without_private_payload(tmp_path):
    from ml.intraday.market_collection_fmp_commodities import native_commodity_requests,collect
    class Denied:
        def _get_json(self,*args): raise RuntimeError('FMP quote request failed with HTTP 402: private key payload')
    request=native_commodity_requests(['BZUSD'])[0]
    def adapter(*args): return collect(*args,provider_factory=Denied)
    col=QuarterHourMarketCollector(tmp_path,symbols=['SDGR'],requests=[request],adapters={'fmp':adapter},clock=lambda:NOW)
    col.enqueue(START);receipt=col.step()[0]
    assert receipt['status']=='NOT_AUTHORIZED' and receipt['evidence']['http_status']==402
    assert 'private key' not in json.dumps(receipt)


def test_stream_bootstrap_preserves_full_json_and_reuses_verified_archive_after_restart(tmp_path):
    from pathlib import Path
    import hashlib
    import pytest
    requests=stream_requests(['TEST'])
    request=next(r for r in requests if r.schema=='LEVELONE_OPTIONS')
    raw={'symbol':'TEST','status':'SUCCESS','numberOfContracts':1,
         'callExpDateMap':{'fixture':{'10':[{'symbol':'TEST  261120C00010000','bid':1.25}]}},
         'putExpDateMap':{},'underlying':{'symbol':'TEST','last':10.5}}
    calls=[]
    def chain(*args):calls.append(1);return raw
    def capture(session,scopes,*args,**kwargs):
        return {},{'service_results':{name:{'status':'COMPLETE'} for name in scopes}}
    first=SchwabStreamCollector(tmp_path,requests,capture_fn=capture,chain_fetch=chain)
    a=first(None,request,START,NOW,100)
    ref=a.evidence['chain_bootstrap_receipts']['TEST']
    path=Path(ref['path']);original=path.read_bytes();stored=json.loads(original)
    assert stored['raw_payload']==raw and stored['response_received'] is True
    assert stored['params']=={'symbol':'TEST','contractType':'ALL','includeUnderlyingQuote':'true','strategy':'SINGLE','range':'ALL'}
    assert stored['request_started_at']<=stored['request_ended_at']==stored['receipt_at']
    assert ref['sha256']==hashlib.sha256(original).hexdigest()
    assert first(None,request,START,NOW,100).evidence['chain_bootstrap_receipts']==a.evidence['chain_bootstrap_receipts']
    restarted=SchwabStreamCollector(tmp_path,requests,capture_fn=lambda *a,**k:pytest.fail('capture repeated'),
                                   chain_fetch=lambda *a:pytest.fail('bootstrap repeated'))
    assert restarted(None,request,START,NOW,100).evidence['chain_bootstrap_receipts']==a.evidence['chain_bootstrap_receipts']
    assert len(calls)==1 and path.read_bytes()==original
    first(None,request,START+timedelta(minutes=15),NOW+timedelta(minutes=15),100)
    assert len(calls)==2 and path.read_bytes()==original
    assert len(list((tmp_path/'raw').rglob('schwab-chain-bootstrap-*.json')))==2
    path.write_bytes(original+b' ')
    with pytest.raises(ValueError,match='bootstrap archive'):
        SchwabStreamCollector(tmp_path,requests,capture_fn=capture,chain_fetch=chain)(None,request,START,NOW,100)


def test_native_bootstrap_invalid_response_and_transport_error_are_durable(tmp_path,monkeypatch):
    from pathlib import Path
    from ml.intraday import market_collection_extended as extended
    requests=stream_requests(['TEST'])
    request=next(r for r in requests if r.schema=='LEVELONE_OPTIONS')
    raw={'symbol':'TEST','status':'FAILED','provider_detail':'native market response'}
    monkeypatch.setattr(extended,'_market_get',lambda *a,**k:raw)
    def capture(session,scopes,*args,**kwargs):
        return {},{'service_results':{name:{'status':'COMPLETE'} for name in scopes}}
    result=SchwabStreamCollector(tmp_path,requests,capture_fn=capture)(None,request,START,NOW,100)
    ref=result.evidence['chain_bootstrap_receipts']['TEST']
    stored=json.loads(Path(ref['path']).read_bytes())
    assert stored['raw_payload']==raw and stored['response_received'] is True
    assert stored['status']=='ERROR' and result.status=='ERROR'
    def failed(*args):raise TimeoutError('private authenticated error text')
    second=SchwabStreamCollector(tmp_path/'transport',requests,capture_fn=capture,chain_fetch=failed)(None,request,START,NOW,100)
    error=json.loads(Path(second.evidence['chain_bootstrap_receipts']['TEST']['path']).read_bytes())
    assert error['status']=='UNAVAILABLE' and error['response_received'] is False and error['raw_payload'] is None
    assert 'private authenticated' not in json.dumps(error)


def test_failed_stream_capture_keeps_completed_bootstrap_receipts(tmp_path):
    from pathlib import Path
    import pytest
    from ml.intraday.market_collection import CollectionFailure
    requests=stream_requests(['TEST'])
    request=next(r for r in requests if r.schema=='LEVELONE_OPTIONS')
    def chain(*args):return {'callExpDateMap':{'fixture':{'10':[{'symbol':'TEST  261120C00010000'}]}},'putExpDateMap':{}}
    def failed(*args,**kwargs):raise TimeoutError()
    with pytest.raises(CollectionFailure) as error:
        SchwabStreamCollector(tmp_path,requests,capture_fn=failed,chain_fetch=chain)(None,request,START,NOW,100)
    ref=error.value.collection_evidence['chain_bootstrap_receipts']['TEST']
    assert json.loads(Path(ref['path']).read_bytes())['status']=='COMPLETE'


def test_bootstrap_archive_capacity_failure_is_scoped_and_cache_does_not_refetch_healthy_symbols(tmp_path,monkeypatch):
    import pytest
    from ml.intraday import market_collection_schwab_stream as stream
    monkeypatch.setattr(stream,'MAX_BOOTSTRAP_BYTES',2000)
    requests=stream_requests(['LARGE','SMALL'])
    calls=[]
    def chain(session,symbol):
        calls.append(symbol)
        return {'callExpDateMap':{'fixture':{'10':[{'symbol':symbol+'  261120C00010000'}]}},
                'putExpDateMap':{},'extra':'x'*(3000 if symbol=='LARGE' else 0)}
    def capture(session,scopes,*args,**kwargs):
        assert set(scopes['LEVELONE_OPTIONS'].values())=={'SMALL'}
        assert set(scopes['LEVELONE_EQUITIES'])=={'SMALL','LARGE'}
        return {},{'service_results':{name:{'status':'COMPLETE'} for name in scopes}}
    request=next(r for r in requests if r.symbol=='LARGE' and r.schema=='LEVELONE_OPTIONS')
    result=SchwabStreamCollector(tmp_path,requests,capture_fn=capture,chain_fetch=chain)(None,request,START,NOW,100)
    assert result.status=='CAPACITY_BLOCKED'
    assert set(result.evidence['chain_bootstrap_receipts'])=={'SMALL'}
    error=result.evidence['bootstrap_archive_errors']['LARGE']
    assert error['raw_preserved'] is False and error['unpersisted_bytes']>2000 and len(error['unpersisted_sha256'])==64
    restarted=SchwabStreamCollector(tmp_path,requests,capture_fn=lambda *a,**k:pytest.fail('capture repeated'),
        chain_fetch=lambda *a:pytest.fail('bootstrap repeated'))
    assert restarted(None,request,START,NOW,100).status=='CAPACITY_BLOCKED'
    assert sorted(calls)==['LARGE','SMALL']
