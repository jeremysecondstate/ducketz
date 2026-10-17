from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

from datafetching.fmp_price_history_receipts import (
    ENDPOINT, SOURCE, capture_native_five_minute_history, load_native_five_minute_history,
)


@pytest.fixture(autouse=True)
def fixed_acquisition_clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = cls(2026, 10, 17, 13, tzinfo=timezone.utc)
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)
    monkeypatch.setattr('datafetching.fmp_price_history_receipts.datetime', FixedDatetime)


def candle(clock='2026-10-16 19:55:00', **changes):
    return {'date': clock, 'open': 10.0, 'high': 12.0, 'low': 9.0, 'close': 11.0, 'volume': 20, **changes}


def reused(payload, *, symbol='TWST', start='2026-10-16', end='2026-10-16', received='2026-10-17T13:00:00Z'):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return {symbol: {'payload': payload, 'endpoint': ENDPOINT, 'params': {
        'symbol':symbol, 'from':start, 'to':end, 'extended':'true', 'nonadjusted':'true'},
        'payload_sha256':hashlib.sha256(encoded).hexdigest(),
        'request_started_at':received, 'received_at':received}}


def retain(root, payload, *, symbol='TWST', day='2026-10-16', received='2026-10-17T13:00:00Z'):
    return capture_native_five_minute_history(root, [symbol], start_date=day, end_date=day,
        reused_receipts=reused(payload,symbol=symbol,start=day,end=day,received=received))


def test_existing_adapter_request_identity_and_immutable_reuse(tmp_path):
    class Provider:
        api_key='private-test-key'
        calls=[]
        def _get_json(self, endpoint, params):
            self.calls.append((endpoint,dict(params)))
            return [candle()]
    provider=Provider()
    report=capture_native_five_minute_history(tmp_path,['TWST'],start_date='2026-10-16',end_date='2026-10-16',provider=provider)
    assert report['actual_provider_requests']==1
    assert provider.calls==[(ENDPOINT,{'symbol':'TWST','from':'2026-10-16','to':'2026-10-16','nonadjusted':'true','extended':'true'})]
    assert report['receipts'][0]['status']=='ok'
    frame,paths,inventory=load_native_five_minute_history(tmp_path,symbol='TWST')
    assert len(frame)==1 and frame.iloc[0].observation_source==SOURCE
    assert frame.iloc[0].timestamp.isoformat()=='2026-10-16T23:55:00+00:00'
    assert frame.iloc[0].observation_recorded_at > frame.iloc[0].timestamp
    assert inventory[0]['response_adjustment_and_session_flags_attested'] is False
    assert provider.api_key not in paths[0].read_text()


def test_original_receipt_reuse_is_idempotent_and_no_request(tmp_path):
    first=retain(tmp_path,[candle()]); second=retain(tmp_path,[candle()])
    assert first==second
    assert first['actual_provider_requests']==0
    frame,paths,inventory=load_native_five_minute_history(tmp_path,symbol='TWST')
    assert len(paths)==1 and len(frame)==1
    assert inventory[0]['received_at']=='2026-10-17T13:00:00+00:00'


@pytest.mark.parametrize('day,clock,expected',[
    ('2026-10-16','2026-10-16 07:00:00','2026-10-16T11:00:00+00:00'),
    ('2026-01-16','2026-01-16 07:00:00','2026-01-16T12:00:00+00:00'),
])
def test_exchange_local_clock_obeys_dst(tmp_path,day,clock,expected):
    retain(tmp_path,[candle(clock)],day=day)
    frame,_,_=load_native_five_minute_history(tmp_path,symbol='TWST')
    assert frame.iloc[0].timestamp.isoformat()==expected


def test_invalid_native_observations_and_synthetic_markers_are_unavailable(tmp_path):
    values=[candle(synthetic=False),candle(volume=0,synthetic=False),candle(high=5,synthetic=False),
        candle(close=-1,synthetic=False),candle('2026-10-16 19:56:00',synthetic=False),
        candle('2026-10-16T19:55:00Z',synthetic=False),candle('bad-clock',synthetic=False),
        candle('2026-10-15 19:55:00',synthetic=False),candle(synthetic=True),
        candle(synthetic=0),candle(synthetic='false'),candle(synthetic=None),
        candle(volume=True,synthetic=False)]
    retain(tmp_path,values)
    frame,_,inventory=load_native_five_minute_history(tmp_path,symbol='TWST')
    assert len(frame)==1
    assert inventory[0]['invalid_or_zero_volume_rows_excluded']==12


def test_incomplete_candle_excluded_and_future_receipt_not_backdated(tmp_path):
    retain(tmp_path,[candle()],received='2026-10-16T23:59:00Z')
    frame,_,inventory=load_native_five_minute_history(tmp_path,symbol='TWST')
    assert frame.empty and inventory[0]['invalid_or_zero_volume_rows_excluded']==1


def test_revisions_remain_distinct_for_consumer_cutoff(tmp_path):
    retain(tmp_path,[candle()],received='2026-10-17T01:00:00Z')
    retain(tmp_path,[candle(close=10.5)],received='2026-10-17T13:00:00Z')
    frame,paths,_=load_native_five_minute_history(tmp_path,symbol='TWST')
    assert len(paths)==2 and len(frame)==2
    assert frame.observation_recorded_at.nunique()==2
    assert frame.observation_input_sha256.nunique()==2


def test_empty_response_is_explicit_and_absent_store_is_empty(tmp_path):
    assert load_native_five_minute_history(tmp_path,symbol='TWST')[0].empty
    report=retain(tmp_path,[])
    assert report['receipts'][0]['status']=='empty'
    frame,paths,inventory=load_native_five_minute_history(tmp_path,symbol='TWST')
    assert frame.empty and len(paths)==1 and inventory[0]['rows']==0


def test_hash_corruption_rejected(tmp_path):
    retain(tmp_path,[candle()])
    _,paths,_=load_native_five_minute_history(tmp_path,symbol='TWST')
    paths[0].write_bytes(paths[0].read_bytes()+b' ')
    with pytest.raises(RuntimeError,match='checksum'):
        load_native_five_minute_history(tmp_path,symbol='TWST')


def test_complete_bytes_published_atomically(tmp_path,monkeypatch):
    import os
    original=os.link
    observed=[]
    def inspect(source,destination):
        assert not destination.exists()
        encoded=source.read_bytes()
        assert hashlib.sha256(encoded).hexdigest()==destination.stem
        assert json.loads(encoded)['payload']==[candle()]
        observed.append(True)
        original(source,destination)
    monkeypatch.setattr('datafetching.fmp_price_history_receipts.os.link',inspect)
    assert retain(tmp_path,[candle()])['receipts'][0]['status']=='ok'
    assert observed==[True]
    assert not list(tmp_path.rglob('*.pending-*'))


def test_bound_identity_checked_even_with_recomputed_outer_hash(tmp_path):
    retain(tmp_path,[candle()])
    _,paths,_=load_native_five_minute_history(tmp_path,symbol='TWST')
    old=paths[0];row=json.loads(old.read_bytes());row['interval_seconds']=60
    encoded=json.dumps(row,sort_keys=True,separators=(',',':')).encode()
    old.unlink()
    old.with_name(hashlib.sha256(encoded).hexdigest()+'.json').write_bytes(encoded)
    with pytest.raises(ValueError,match='identity'):
        load_native_five_minute_history(tmp_path,symbol='TWST')


def test_reused_request_or_payload_mismatch_never_persisted(tmp_path):
    item=reused([candle()]);item['TWST']['params']['extended']='false'
    report=capture_native_five_minute_history(tmp_path,['TWST'],start_date='2026-10-16',end_date='2026-10-16',reused_receipts=item)
    assert report['receipts'][0]['status']=='error'
    assert load_native_five_minute_history(tmp_path,symbol='TWST')[0].empty


def test_provider_error_does_not_block_other_symbol_and_redacts_details(tmp_path):
    class Provider:
        def _get_json(self, endpoint, params):
            if params['symbol']=='CROX':
                raise RuntimeError('https://example.invalid?apikey=private-test-key')
            return [candle()]
    report=capture_native_five_minute_history(tmp_path,['CROX','TWST'],start_date='2026-10-16',end_date='2026-10-16',provider=Provider())
    assert [r['status'] for r in report['receipts']]==['error','ok']
    assert 'private-test-key' not in json.dumps(report)


def test_window_and_symbol_bounds_are_checked_before_requests(tmp_path):
    for symbols,start,end in [(['TWST'],'2026-10-01','2026-10-16'),(['../TWST'],'2026-10-16','2026-10-16'),([],'2026-10-16','2026-10-16')]:
        with pytest.raises(ValueError):
            capture_native_five_minute_history(tmp_path,symbols,start_date=start,end_date=end,provider=object())


def test_credential_shaped_or_unbounded_payload_is_not_persisted(tmp_path):
    for payload in [[candle(apikey='private-test-key')],[candle()]*(7*288+1)]:
        report=retain(tmp_path,payload)
        assert report['receipts'][0]['status']=='error'
    assert not list(tmp_path.rglob('*.json'))
