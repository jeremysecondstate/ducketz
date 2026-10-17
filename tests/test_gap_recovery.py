"""Offline production-adapter checks; no provider or trading operations."""
from datetime import datetime,timedelta,timezone
import hashlib,json,sqlite3

import pytest

from ml.intraday.gap_recovery import GapRecovery

START=datetime(2026,10,12,14,tzinfo=timezone.utc)
END=START+timedelta(minutes=15)
NOW=END+timedelta(minutes=10)

def row(stamp,volume=1):
    return {'open':100,'high':100,'low':100,'close':100,'volume':volume,'opened_at':stamp.isoformat()}

def attempt(start=START,rows=None):
    return dict(attempt_id='test-'+start.isoformat(),symbol='TEST',source_id='databento:EQUS.MINI:ohlcv-1m',
        source_dataset='EQUS.MINI',interval_open=start.isoformat(),interval_close=(start+timedelta(minutes=15)).isoformat(),
        request_started_at=NOW.isoformat(),request_ended_at=NOW.isoformat(),status='success',
        coverage_open=(START-timedelta(days=1)).isoformat(),coverage_close=NOW.isoformat(),rows=rows or [])

class Schwab:
    def __init__(self,rows):self.rows=rows;self.calls=[]
    def get_price_history(self,symbol,**kwargs):
        self.calls.append((symbol,kwargs))
        return {'candles':[{k:v for k,v in r.items() if k!='opened_at'}|{'datetime':int(datetime.fromisoformat(r['opened_at']).timestamp()*1000)} for r in self.rows]}

class Fmp:
    api_key='fake-secret'

def engine(tmp_path,monkeypatch,schwab_rows=(),fmp_rows=()):
    from ml.intraday import provider_recovery
    def response(provider,endpoint,params):
        from zoneinfo import ZoneInfo
        return [{k:v for k,v in r.items() if k!='opened_at'}|{'date':datetime.fromisoformat(r['opened_at']).astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')} for r in fmp_rows]
    monkeypatch.setattr(provider_recovery,'fmp_request',response)
    return GapRecovery(tmp_path,clock=lambda:NOW,schwab=Schwab(schwab_rows),fmp=Fmp())

def previous():
    return {'symbol':'TEST','opened_at':(START-timedelta(minutes=15)).isoformat(),'available_at':START.isoformat(),
        'close':100,'synthetic':False,'source_id':'databento:EQUS.MINI:ohlcv-1m',
        'native_last_opened_at':(START-timedelta(minutes=1)).isoformat(),'native_interval_seconds':60}

def primary():return {'evidence':[attempt()]}

def test_actual_native_schwab_recovery_preserves_source_and_raw_receipt_without_prior_anchor(tmp_path,monkeypatch):
    g=engine(tmp_path,monkeypatch,[row(START),row(START+timedelta(minutes=12))])
    result=g.recover('TEST',START,None,primary())
    assert result['status']=='recovered'
    candidate=result['assessment']['observed_candidates'][0]
    assert candidate['source']=='schwab:pricehistory:1m' and len(candidate['records'])==2
    assert not result['assessment']['derived_allowed']
    raw=open(result['proof_path'],'rb').read()
    assert hashlib.sha256(raw).hexdigest()==result['proof_sha256']
    proof=json.loads(raw)
    assert proof['requests'][0]['references'][0]['path']
    assert len(g.schwab.calls)==1 and g.schwab.calls[0][1]['frequency']==1

def test_five_minute_recovery_retains_coarse_native_intervals(tmp_path,monkeypatch):
    g=engine(tmp_path,monkeypatch,[],[row(START),row(START+timedelta(minutes=10))])
    candidate=g.recover('TEST',START,previous(),primary())['assessment']['observed_candidates'][0]
    assert candidate['native_interval_seconds']==300 and len(candidate['records'])==2
    assert candidate['source']=='fmp:historical-chart:5min'

def test_three_empty_without_later_brackets_are_explicitly_probable(tmp_path,monkeypatch):
    result=engine(tmp_path,monkeypatch).recover('TEST',START,previous(),primary())
    assert result['status']=='recovered' and result['assessment']['derived_allowed']
    assert len(result['assessment']['limitations'])==6

def test_three_actual_empty_responses_with_indexed_native_brackets_allow_flagged_carry(tmp_path,monkeypatch):
    folder=tmp_path/'intraday';folder.mkdir()
    with sqlite3.connect(folder/'acquisition.sqlite3') as db:
        db.execute('CREATE TABLE attempts(id TEXT PRIMARY KEY,slot TEXT,payload TEXT)')
        db.execute('CREATE INDEX exact_slot ON attempts(slot)')
        for op,rows in ((START-timedelta(minutes=15),[row(START-timedelta(minutes=1))]),(END,[row(END)])):
            item=attempt(op,rows)
            slot=json.dumps(['TEST',item['source_id'],op.isoformat(),(op+timedelta(minutes=15)).isoformat()],separators=(',',':'))
            db.execute('INSERT INTO attempts VALUES(?,?,?)',(item['attempt_id'],slot,json.dumps(item)))
    g=engine(tmp_path,monkeypatch,[row(START-timedelta(minutes=1)),row(END)],
             [row(START-timedelta(minutes=5)),row(END)])
    result=g.recover('TEST',START,previous(),primary())
    assert result['assessment']['derived_allowed']
    assert result['assessment']['available_at']==NOW.isoformat()
    assert result['assessment']['agreeing_providers']==['databento','fmp','schwab']

def test_native_flag_or_conflicting_clock_rejects_observed_recovery(tmp_path,monkeypatch):
    bad=row(START)|{'is_synthetic':True}
    result=engine(tmp_path,monkeypatch,[bad]).recover('TEST',START,previous(),primary())
    assert not result['assessment']['observed_candidates']
    assert result['requests'][0]['status']=='malformed_native_response'
    assert result['requests'][0]['references']  # Raw retained before parsing fails.

def test_completed_quarter_stop_and_invalid_symbol_prevent_requests(tmp_path,monkeypatch):
    g=engine(tmp_path,monkeypatch)
    with pytest.raises(ValueError):g.recover('../TEST',START,previous(),primary())
    with pytest.raises(ValueError):g.recover('TEST',NOW,previous(),primary())
    g.stopped=lambda:True
    assert g.recover('TEST',START,previous(),primary())['status']=='stopped'
    assert not g.schwab.calls and not (tmp_path/'intraday').exists()

def test_exact_same_clock_duplicates_do_not_double_volume(tmp_path,monkeypatch):
    result=engine(tmp_path,monkeypatch,[row(START),row(START)]).recover('TEST',START,previous(),primary())
    records=result['assessment']['observed_candidates'][0]['records']
    assert len(records)==1 and records[0]['volume']==1

def test_failed_provider_is_durable_pending_and_never_an_empty_vote(tmp_path,monkeypatch):
    g=engine(tmp_path,monkeypatch)
    def fail(*args,**kwargs):raise TimeoutError('price request timed out')
    g.schwab.get_price_history=fail
    # An advancing fake clock makes the durable two-second retry deterministic.
    from ml.intraday.provider_recovery import RetryLedger
    clock=[NOW]
    def wait(seconds):clock[0]+=timedelta(seconds=seconds);return False
    g.clock=lambda:clock[0]
    g.ledger=RetryLedger(g.root/'retry-ledger.json',clock=g.clock,wait=wait)
    result=g.recover('TEST',START,previous(),primary())
    assert result['status']=='retry' and result['retry_after_seconds']==2
    assert len(result['requests'][0]['references'])==2
    assert 'schwab' not in result['assessment']['agreeing_providers']


@pytest.mark.parametrize('where,change', [
    ('row', {'synthetic_open': True}),
    ('row', {'synthetic_constituents': 1}),
    ('envelope', {'synthetic': True}),
    ('envelope', {'symbol': 'OTHER'}),
    ('row', {'symbol': 'OTHER'}),
])
def test_metadata_is_rejected_before_native_projection(tmp_path,monkeypatch,where,change):
    g=engine(tmp_path,monkeypatch,[row(START)])
    payload=g.schwab.get_price_history('TEST')
    payload['symbol']='TEST'
    (payload if where=='envelope' else payload['candles'][0]).update(change)
    g.schwab.get_price_history=lambda *args,**kwargs:payload
    result=g.recover('TEST',START,previous(),primary())
    assert result['status']=='unavailable'
    assert not result['assessment']['observed_candidates']
    assert not result['assessment']['derived_allowed']
    assert result['requests'][0]['status']=='malformed_native_response'
    saved=json.loads(open(result['requests'][0]['references'][0]['path'],'rb').read())
    assert saved['payload']==payload  # Invalid raw evidence remains intact.


@pytest.mark.parametrize('provider', ['schwab','fmp','databento'])
@pytest.mark.parametrize('change', [
    {'synthetic_close': True}, {'is_derived': True},
    {'synthetic_constituents': False}, {'synthetic_constituents': '0'},
    {'synthetic_constituents': None}, {'symbol':'OTHER'},
])
def test_all_native_provider_rows_validate_explicit_metadata(provider,change):
    from ml.intraday.gap_recovery import _parse
    native=row(START)|change
    if provider=='schwab':
        native['datetime']=int(START.timestamp()*1000)
        payload={'symbol':'TEST','candles':[native]}
    elif provider=='fmp':
        native['date']='2026-10-12 10:00:00'
        payload=[native]
    else:
        payload=[native]
    with pytest.raises(ValueError):
        _parse(provider,payload,NOW,symbol='TEST')
