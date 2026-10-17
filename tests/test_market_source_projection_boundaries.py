"""Offline identity/marker preservation at task-owned native parser boundaries."""
from datetime import datetime,timezone
import hashlib,json

import pytest

from ml.intraday.market_input_research import extract_features
from ml.intraday.market_input_schwab_stream import normalize
from ml.intraday.market_inputs import MarketInputCollector,_requested_identity
from datafetching.fmp_price_history_receipts import capture_native_five_minute_history,load_native_five_minute_history,ENDPOINT

NOW=datetime(2026,10,16,18,tzinfo=timezone.utc)
CHANGES=[{'is_derived':True},{'synthetic_open':True},{'synthetic_close':True},
 {'synthetic_constituents':1},{'synthetic_constituents':False},{'synthetic_constituents':'0'}]

@pytest.mark.parametrize('change',CHANGES)
def test_research_and_stream_do_not_project_marked_records(change):
    dcf={'symbol':'AAPL','dcf':150,'stockPrice':100}|change
    assert extract_features('fmp_dcf',[dcf],NOW,symbol='AAPL')=={}
    assert extract_features('fmp_dcf',{'data':[{'dcf':150,'stockPrice':100}]}|change,NOW,symbol='AAPL')=={}
    native={'key':'AAPL','1':100,'2':102,'3':101,'4':20,'5':10,'8':1000,'34':int(NOW.timestamp()*1000)}|change
    assert normalize('LEVELONE_EQUITIES',native,received_at=NOW,scope={'AAPL':'AAPL'}) is None

def test_research_foreign_envelope_is_not_relabelled():
    assert extract_features('fmp_dcf',{'symbol':'OTHER','data':[{'dcf':150,'stockPrice':100}]},NOW,symbol='AAPL')=={}

@pytest.mark.parametrize('change',CHANGES+[{'symbol':'OTHER'}])
def test_native_fmp_receipt_preserves_raw_but_rejects_invalid_identity_or_markers(tmp_path,change):
    payload=[{'date':'2026-10-16 13:55:00','open':100,'high':101,'low':99,'close':100,'volume':1}|change]
    raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
    params={'symbol':'AAPL','from':'2026-10-16','to':'2026-10-16','extended':'true','nonadjusted':'true'}
    result=capture_native_five_minute_history(tmp_path,['AAPL'],start_date='2026-10-16',end_date='2026-10-16',
      reused_receipts={'AAPL':{'payload':payload,'endpoint':ENDPOINT,'params':params,'payload_sha256':hashlib.sha256(raw).hexdigest(),
       'request_started_at':NOW.isoformat(),'received_at':NOW.isoformat()}})
    assert result['actual_provider_requests']==0
    frame,paths,inventory=load_native_five_minute_history(tmp_path,symbol='AAPL')
    assert frame.empty and len(paths)==1 and inventory[0]['raw_rows']==1

@pytest.mark.parametrize('source,payload',[
 ('fmp_profile',[{'symbol':'OTHER','beta':1}]),
 ('fmp_quote',{'symbol':'OTHER','data':[{'price':100}]}),
 ('schwab_minutes',{'symbol':'OTHER','candles':[]}),
 ('schwab_minutes',{'candles':[{'symbol':'OTHER'}]}),
 ('schwab_options',{'symbol':'AAPL','callExpDateMap':{'day':{'100':[{'symbol':'OTHER  261016C00100000'}]}}}),
])
def test_collector_rejects_returned_identity_before_extraction(tmp_path,source,payload):
    c=MarketInputCollector(tmp_path,['AAPL'],clock=lambda:NOW,streams=False)
    result=c._capture('AAPL',source,{},lambda:payload,lambda p,n:pytest.fail('foreign identity projected'),
       _attempt={'status':'ok','payload':payload,'started_at':NOW.isoformat(),'received_at':NOW.isoformat(),'attempt_number':1})
    assert result['status']=='extraction_unavailable'
    assert all(value is None for value in result['features'].values())
    assert result['request']['returned_identity_mismatch'] is True
    c.close()

def test_provider_specific_native_identities_are_preserved():
    assert _requested_identity('fmp_profile',[{'beta':1}],'AAPL')
    assert _requested_identity('schwab_options',{'symbol':'AAPL','callExpDateMap':{'day':{'100':[{'symbol':'AAPL  261016C00100000'}]}}},'AAPL')
    option={'key':'AAPL  261016C00100000','0':'AAPL  261016C00100000','2':1,'3':2,'4':1.5,'38':int(NOW.timestamp()*1000)}
    assert normalize('LEVELONE_OPTIONS',option,received_at=NOW,scope={option['key']:'AAPL'})
    future={'key':'/ESZ26','0':'/ESZ26','1':100,'2':101,'3':100.5,'10':int(NOW.timestamp()*1000)}
    assert normalize('LEVELONE_FUTURES',future,received_at=NOW,scope={'/ESZ26':None})
