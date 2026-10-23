"""Actual native clocks survive restore; historical revisions never backdate."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import data
from ml.intraday.data import History
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.native_interval import CONTRACT, merge_native_history_frames, resolve_native_interval
from ml.intraday.runtime import LiveHistories
from tests.test_intraday_native_interval import CLOSE, OPEN, SESSION, native_rows, stable_responses


def candle(offset=0, changed=False):
    rows=native_rows()
    if changed:
        # The fifteen-minute OHLCV remains identical: identity must include
        # the internal native minute whose close changed.
        rows=({**rows[0],'close':101.0},rows[1])
    attempts=[replace(a,attempt_id=a.attempt_id+'-'+str(offset),rows=rows,
        request_started_at=(pd.Timestamp(a.request_started_at)+pd.Timedelta(seconds=offset)).isoformat(),
        request_ended_at=(pd.Timestamp(a.request_ended_at)+pd.Timedelta(seconds=offset)).isoformat())
        for a in stable_responses()]
    return resolve_native_interval(attempts,session=SESSION)['candle']


def frame(values):
    result=pd.DataFrame(values)
    for name in ('opened_at','available_at'):
        result[name]=pd.to_datetime(result[name],utc=True)
    return result


@pytest.mark.parametrize('sequence,expected',[
    ([(0,False),(20,False)],0),
    ([(20,False),(0,False)],0),
    ([(0,False),(20,True)],20),
    ([(0,False),(20,True),(40,False)],40),
    ([(0,False),(20,True),(40,True)],20),
])
def test_native_merge_earliest_actual_in_latest_contiguous_revision(sequence,expected):
    values=[candle(offset,changed) for offset,changed in sequence]
    original,addition=frame(values[:1]),frame(values[1:])
    original_copy,addition_copy=original.copy(deep=True),addition.copy(deep=True)
    merged=merge_native_history_frames(original,addition)
    assert len(merged)==1
    assert merged.iloc[0].available_at==CLOSE+pd.Timedelta(seconds=8+expected)
    pd.testing.assert_frame_equal(original,original_copy)
    pd.testing.assert_frame_equal(addition,addition_copy)


def test_legacy_merge_keeps_existing_latest_receipt_selection():
    values=[candle(),candle(20)]
    for value in values:
        value.pop('native_interval_contract')
    merged=merge_native_history_frames(frame(values[:1]),frame(values[1:]))
    assert merged.iloc[0].available_at==CLOSE+pd.Timedelta(seconds=28)


def test_native_merge_respects_retained_revision_boundary_after_projection():
    first,current=candle(),candle(40)
    evidence=json.loads(current['evidence_json'])
    evidence['coverage']['revision_boundary']={'kind':'observed_native_revision_v1','attempt_id':'changed',
                                             'available_at':(CLOSE+pd.Timedelta(seconds=28)).isoformat()}
    current['evidence_json']=json.dumps(evidence)
    merged=merge_native_history_frames(frame([first]),frame([current]))
    assert merged.iloc[0].available_at==pd.Timestamp(current['available_at'])
    # An unannotated later store repeat must not hide the projection's
    # retained boundary and reconnect the current revision to superseded A.
    later=candle(60)
    merged=merge_native_history_frames(frame([first,current]),frame([later]))
    assert merged.iloc[0].available_at==pd.Timestamp(current['available_at'])
    assert json.loads(merged.iloc[0].evidence_json)['coverage']['revision_boundary']==evidence['coverage']['revision_boundary']


@pytest.mark.parametrize('path',['minute_feed','store_restore','acquisition_merge'])
def test_normal_runtime_siblings_preserve_earliest_identical_native_clock(path,monkeypatch):
    first,later=candle(),candle(20)
    histories={'TEST':History(frame([first]),{'source_dataset':'EQUS.MINI','native_interval_contract':CONTRACT})}
    if path=='minute_feed':
        feed=QuarterHourMinuteFeed.__new__(QuarterHourMinuteFeed)
        feed.histories=histories
        feed.dataset='EQUS.MINI'
        feed.native_interval_symbols=('TEST',)
        feed.native_interval_contract=CONTRACT
        feed._merge('TEST',[later])
    else:
        writes=[]
        store=SimpleNamespace(observed_candles=lambda symbol:[later],record_candle=writes.append,
                              event=lambda *args,**kwargs:None)
        live=LiveHistories(histories=histories,provider=SimpleNamespace(dataset='EQUS.MINI'),
            store=store,journal=SimpleNamespace(responses=lambda **kwargs:()),reference_at=OPEN,
            clock=lambda:CLOSE+pd.Timedelta(minutes=1))
        if path=='acquisition_merge':
            monkeypatch.setattr(data,'recover_databento_candle_step',lambda *args,**kwargs:{'status':'observed','candle':later})
            live._get_step('TEST')
            assert writes==[later]
    actual=histories['TEST'].frame.iloc[0]
    assert actual.available_at==pd.Timestamp(first['available_at'])
    assert actual.evidence_json==first['evidence_json']
