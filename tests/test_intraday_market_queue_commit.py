"""Native due scans release the queue lock; custom/freshness paths stay original."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import types

from filelock import FileLock, Timeout
import pytest

from ml.intraday import market_collection as M

NOW = datetime(2026, 10, 27, 15, 30, tzinfo=timezone.utc)


def collector(tmp_path, cls=None):
    request = M.SourceRequest(provider="fmp", source_kind="stock_quote", symbol="SDGR",
        dataset="fixture", schema="quote", endpoint="quote", params={"symbol": "SDGR"})
    col = (cls or M.QuarterHourMarketCollector)(tmp_path, symbols=["SDGR"],
        requests=[request], adapters={}, clock=lambda: NOW)
    col.enqueue(NOW - timedelta(minutes=15))
    return col


def state(col):
    return json.loads(col.state_path.read_text(encoding="utf8"))


def publish(col, value):
    col.state_path.write_text(json.dumps(value), encoding="utf8")


def capture(col, monkeypatch):
    calls = []
    def select(due, maximum):
        calls.append((due, maximum, col.lock.is_locked))
        return []
    monkeypatch.setattr(M, "_select_tasks", select)
    return calls


def timestamp_locks(col, monkeypatch):
    calls = []
    original = M.instant
    def traced(value):
        calls.append((value, col.lock.is_locked))
        return original(value)
    monkeypatch.setattr(M, "instant", traced)
    return calls


def test_native_due_scan_runs_outside_lock_with_original_clock_inside(tmp_path, monkeypatch):
    col = collector(tmp_path)
    value = state(col)
    first = next(iter(value["tasks"].values()))
    for index in range(20):
        value["tasks"][str(index)] = {**first, "due_at": (NOW + timedelta(seconds=index-10)).isoformat()}
    value["tasks"]["settled"] = {**first, "status": "COMPLETE", "due_at": "invalid-unused"}
    value["tasks"]["finalized"] = {**first, "status": "PARTIAL", "window_finalized": True,
        "due_at": "invalid-unused"}
    publish(col, value)
    selected = capture(col, monkeypatch)
    locks = timestamp_locks(col, monkeypatch)
    assert col.step(max_requests=4) == []
    due, maximum, selection_locked = selected[0]
    assert maximum == 4 and not selection_locked
    assert [key for key, _ in due] == [next(iter(value["tasks"])), *map(str, range(11))]
    assert locks[0] == (NOW, True)
    assert all(not locked for _, locked in locks[1:])
    assert len(locks) == 22 and not col.lock.is_locked


@pytest.mark.parametrize("kind", ["instance", "subclass", "class"])
def test_custom_state_keeps_due_scan_under_original_lock(tmp_path, monkeypatch, kind):
    class Custom(M.QuarterHourMarketCollector):
        pass
    col = collector(tmp_path, cls=Custom if kind == "subclass" else None)
    original = M.QuarterHourMarketCollector._native_state_reader
    called = []
    def custom(self):
        called.append(self.lock.is_locked)
        return original(self)
    if kind == "class":
        monkeypatch.setattr(M.QuarterHourMarketCollector, "_state", custom)
    elif kind == "subclass":
        monkeypatch.setattr(Custom, "_state", custom)
    else:
        col._state = types.MethodType(custom, col)
    capture(col, monkeypatch)
    locks = timestamp_locks(col, monkeypatch)
    assert col.step(max_requests=1) == []
    assert called == [True] and all(locked for _, locked in locks)


@pytest.mark.parametrize("restore", [False, True])
def test_mid_decode_state_override_uses_original_reader_even_if_it_restores_native(tmp_path, monkeypatch, restore):
    col = collector(tmp_path)
    custom_value = state(col)
    original_state = M.QuarterHourMarketCollector._native_state_reader
    original_decode = M.json.loads
    called = []
    def custom(self):
        called.append(self.lock.is_locked)
        if restore:
            self._state = types.MethodType(original_state, self)
        return custom_value
    def decode(raw, *args, **kwargs):
        value = original_decode(raw, *args, **kwargs)
        col._state = types.MethodType(custom, col)
        return value
    monkeypatch.setattr(M.json, "loads", decode)
    capture(col, monkeypatch)
    locks = timestamp_locks(col, monkeypatch)
    assert col.step(max_requests=1) == []
    assert called == [True] and all(locked for _, locked in locks)


@pytest.mark.parametrize("kind", ["instance", "class"])
def test_two_argument_read_queue_override_keeps_original_call(tmp_path, monkeypatch, kind):
    col = collector(tmp_path)
    original = col._read_queue
    calls = []
    def wrapped(phase, reader):
        calls.append(phase)
        return original(phase, reader)
    if kind == "instance":
        col._read_queue = wrapped
    else:
        monkeypatch.setattr(M.QuarterHourMarketCollector, "_read_queue", lambda self, phase, reader: wrapped(phase, reader))
    capture(col, monkeypatch)
    locks = timestamp_locks(col, monkeypatch)
    assert col.step(max_requests=1) == []
    assert calls == ["step_due"] and all(locked for _, locked in locks)


def test_changed_queue_during_decode_uses_current_native_snapshot_due_scan(tmp_path, monkeypatch):
    col = collector(tmp_path)
    current = state(col)
    key = next(iter(current["tasks"]))
    current["tasks"][key]["due_at"] = (NOW + timedelta(seconds=1)).isoformat()
    original = M.json.loads
    def decode(raw, *args, **kwargs):
        result = original(raw, *args, **kwargs)
        publish(col, current)
        return result
    monkeypatch.setattr(M.json, "loads", decode)
    selected = capture(col, monkeypatch)
    locks = timestamp_locks(col, monkeypatch)
    assert col.step(max_requests=1) == []
    assert selected[0][0] == [] and locks[0] == (NOW, True)
    assert all(not locked for _, locked in locks[1:])


@pytest.mark.parametrize("effect", ["settled", "future", "source_block"])
def test_due_snapshot_does_not_bypass_current_task_or_source_recheck(tmp_path, monkeypatch, effect):
    col = collector(tmp_path)
    current = state(col)
    key = next(iter(current["tasks"]))
    def select(due, maximum):
        assert not col.lock.is_locked
        latest = state(col)
        if effect == "settled":
            latest["tasks"][key]["status"] = "COMPLETE"
        elif effect == "future":
            latest["tasks"][key]["due_at"] = (NOW + timedelta(seconds=1)).isoformat()
        else:
            latest["source_blocks"][col.requests[0].identity] = {"observed_at": NOW.isoformat(), "snapshot": "fixture"}
        publish(col, latest)
        return due[:maximum]
    monkeypatch.setattr(M, "_select_tasks", select)
    col.adapters = {"fmp": lambda *args: pytest.fail("Current task/source disposition reached provider")}
    result = col.step(max_requests=1)
    if effect == "source_block":
        assert len(result) == 1 and result[0]["status"] == "NOT_AUTHORIZED"
        assert state(col)["tasks"][key]["attempts"] == 1
    else:
        assert result == [] and state(col)["tasks"][key]["attempts"] == 0


@pytest.mark.parametrize("custom", [False, True])
def test_due_error_identity_and_native_lock_release_preserved(tmp_path, monkeypatch, custom):
    col = collector(tmp_path)
    if custom:
        original = col._state
        col._state = lambda: original()
    error = ValueError("original due timestamp error")
    original_instant = M.instant
    def fail_due(value):
        if isinstance(value, str):
            assert col.lock.is_locked is custom
            raise error
        return original_instant(value)
    monkeypatch.setattr(M, "instant", fail_due)
    with pytest.raises(ValueError) as raised:
        col.step(max_requests=1)
    assert raised.value is error and not col.lock.is_locked


def test_clock_error_stays_inside_final_lock_with_original_identity(tmp_path):
    col = collector(tmp_path)
    error = RuntimeError("original clock failure")
    def fail():
        assert col.lock.is_locked
        raise error
    col.clock = fail
    with pytest.raises(RuntimeError) as raised:
        col.step(max_requests=1)
    assert raised.value is error and not col.lock.is_locked


def test_due_scan_does_not_run_after_second_lock_timeout(tmp_path, monkeypatch):
    col = collector(tmp_path)
    col.lock.timeout = 0
    holder = FileLock(col.lock.lock_file, timeout=0)
    original = M.json.loads
    def decode(raw, *args, **kwargs):
        value = original(raw, *args, **kwargs)
        holder.acquire()
        return value
    monkeypatch.setattr(M.json, "loads", decode)
    col.clock = lambda: pytest.fail("Timed-out final lock reached due clock")
    try:
        with pytest.raises(Timeout) as raised:
            col.step(max_requests=1)
        assert raised.value.lock_file == col.lock.lock_file and holder.is_locked
    finally:
        holder.release()


"""Golden canonical bytes retained across exact native container normalization."""
from collections import UserDict
from datetime import datetime, timezone
import math
import numpy as np
import pandas as pd
import pytest
from ml.intraday.market_collection import canonical as container_canonical

class _ContainerDict(dict): pass
class _ContainerList(list): pass
class _ContainerTuple(tuple): pass
class _ContainerInt(int): pass
class _ContainerString(str): pass
class _ContainerFloat(float): pass
class _ContainerItem:
    def item(self): return {'nested': [1, float('nan')]}
class _ContainerFallback:
    def __str__(self): return 'fallback-object'

@pytest.mark.parametrize('value,expected', [
    (None, 'null'), (True, 'true'), (False, 'false'), (0, '0'),
    (2**90, '1237940039285380274899124224'), (-5, '-5'), (0.0, '0.0'), (1.4, '1.4'),
    (float('nan'), 'null'), (float('inf'), 'null'), (-float('inf'), 'null'),
    ('snowman☃', '"snowman☃"'), ('quote"', '"quote\\\""'), ([], '[]'), ({}, '{}'),
    ({'a': {'b': [1, float('nan'), None]}}, '{"a":{"b":[1,null,null]}}'),
    ({1: 'int', True: 'bool', (1, 2): 'tuple'}, '{"(1, 2)":"tuple","1":"bool"}'),
    ([[], {}, (), [{'k': [2]}]], '[[],{},[],[{"k":[2]}]]'),
    (_ContainerDict({1: _ContainerList([None, _ContainerFloat(float('nan'))])}), '{"1":[null,null]}'),
    (_ContainerList([1, _ContainerDict(a=2)]), '[1,{"a":2}]'),
    (_ContainerTuple((1, {}, [2])), '[1,{},[2]]'),
    (UserDict({1: UserDict(x=[float('inf')])}), '{"1":{"x":[null]}}'),
    ((1, 2, [3]), '[1,2,[3]]'), (_ContainerInt(4), '4'), (_ContainerString('s'), '"s"'),
    (_ContainerFloat(2.1), '2.1'), (_ContainerFloat(float('nan')), 'null'),
    (datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc), '"2026-10-27T15:00:00+00:00"'),
    (datetime(2026, 10, 27, 15, 0), '"2026-10-27T15:00:00"'),
    (pd.Timestamp('2026-10-27T15:00:00.000000123Z'), '"2026-10-27T15:00:00.000000123+00:00"'),
    (pd.NaT, 'null'), (pd.NA, 'null'), (np.int64(4), '4'),
    (np.float64(float('nan')), 'null'), (np.float64(1.2), '1.2'), (np.array(3), '3'),
    (_ContainerItem(), '{"nested":[1,null]}'), (_ContainerFallback(), '"fallback-object"'),
])
def test_native_container_golden_bytes_preserve_original_normalization(value, expected):
    # These explicit byte strings were checked against the original normalizer.
    assert container_canonical(value) == expected.encode('utf-8')

class _ContainerPresentedDict(dict):
    def items(self): return [('presented', [float('nan')])]
class _ContainerPresentedList(list):
    def __iter__(self): return iter(('presented', float('inf')))

@pytest.mark.parametrize('value,expected', [
    (_ContainerPresentedDict(hidden='must not select underlying storage'), '{"presented":[null]}'),
    (_ContainerPresentedList(['hidden']), '["presented",null]'),
    ({float('inf'): 'positive', -float('inf'): 'negative', float('nan'): 'missing'},
     '{"-inf":"negative","inf":"positive","nan":"missing"}'),
    ({1: 'number', '1': 'text'}, '{"1":"text"}'),
    ({'1': 'text', 1: 'number'}, '{"1":"number"}'),
])
def test_container_custom_iteration_and_key_stringification_preserved(value, expected):
    assert container_canonical(value) == expected.encode('utf-8')

def test_native_container_normalization_keeps_original_nested_evidence():
    missing = float('nan')
    values = [missing, {'missing': float('inf')}]
    source = {'task': {'nested': values}}
    assert container_canonical(source) == b'{"task":{"nested":[null,{"missing":null}]}}'
    assert source['task']['nested'] is values and values[0] is missing and math.isnan(missing)
    assert math.isinf(values[1]['missing'])


def _refine_changing_decode(col,monkeypatch,value,second_effect=None):
 original=M.json.loads;calls=[]
 def decode(raw,*args,**kwargs):
  calls.append(col.lock.is_locked)
  result=original(raw,*args,**kwargs)
  if len(calls)==1:publish(col,value)
  elif second_effect is not None:second_effect()
  return result
 monkeypatch.setattr(M.json,'loads',decode)
 return calls



def _refine_trace_instant(col,monkeypatch):
 original=M.instant;calls=[]
 def traced(value):calls.append((value,col.lock.is_locked));return original(value)
 monkeypatch.setattr(M,'instant',traced);return calls



def test_changed_native_reads_exact_current_state_once_then_scans_unlocked(tmp_path,monkeypatch):
 col=collector(tmp_path);value=state(col);task=next(iter(value['tasks'].values()));value['tasks']['new']={**task,'due_at':(NOW-timedelta(seconds=1)).isoformat()}
 decodes=_refine_changing_decode(col,monkeypatch,value);timestamps=_refine_trace_instant(col,monkeypatch)
 selected=[]
 monkeypatch.setattr(M,'_select_tasks',lambda due,maximum:selected.extend(due) or [])
 assert col.step(max_requests=1)==[]
 assert [key for key,_ in selected]==list(value['tasks'])
 assert decodes==[False,True]
 assert timestamps[0]==(NOW,True) and all(not lock for _,lock in timestamps[1:])



@pytest.mark.parametrize('restore',[False,True])
def test_override_installed_by_final_comparison_keeps_original_reader(tmp_path,monkeypatch,restore):
 col=collector(tmp_path);value=state(col);value['extra']='changed'
 decodes=_refine_changing_decode(col,monkeypatch,value);timestamps=_refine_trace_instant(col,monkeypatch)
 original_read=Path.read_text;native=M.QuarterHourMarketCollector._native_state_reader;reads=[];custom_calls=[]
 def custom(self):
  custom_calls.append(self.lock.is_locked)
  if restore:self._state=types.MethodType(native,self)
  return value
 def read(path,*args,**kwargs):
  result=original_read(path,*args,**kwargs)
  if path==col.state_path:
   reads.append(col.lock.is_locked)
   if len(reads)==2:col._state=types.MethodType(custom,col)
  return result
 monkeypatch.setattr(Path,'read_text',read);monkeypatch.setattr(M,'_select_tasks',lambda *args:[])
 assert col.step(max_requests=1)==[]
 assert custom_calls==[True] and all(locked for _,locked in timestamps)
 assert decodes==[False] and reads==[True,True]



def test_override_installed_during_fresh_native_decode_keeps_original_reader(tmp_path,monkeypatch):
 col=collector(tmp_path);value=state(col);value['extra']='changed';custom_calls=[]
 def custom(self):custom_calls.append(True);return value
 def install():col._state=types.MethodType(custom,col)
 decodes=_refine_changing_decode(col,monkeypatch,value,install);timestamps=_refine_trace_instant(col,monkeypatch)
 monkeypatch.setattr(M,'_select_tasks',lambda *args:[])
 assert col.step(max_requests=1)==[]
 assert custom_calls==[] and decodes==[False,True]
 assert all(locked for _,locked in timestamps)



def test_missing_default_returned_by_fresh_native_read_uses_original_reader(tmp_path,monkeypatch):
 col=collector(tmp_path);value=state(col);value['extra']='changed'
 decodes=_refine_changing_decode(col,monkeypatch,value);timestamps=_refine_trace_instant(col,monkeypatch)
 original_exists=Path.exists;calls=[]
 def exists(path):
  if path==col.state_path:
   calls.append(col.lock.is_locked)
   if len(calls)==3:return False
  return original_exists(path)
 monkeypatch.setattr(Path,'exists',exists);selected=[]
 monkeypatch.setattr(M,'_select_tasks',lambda due,maximum:selected.extend(due) or [])
 assert col.step(max_requests=1)==[]
 assert calls==[True,True,True] and decodes==[False]
 assert timestamps==[(NOW,True)] and selected==[]



@pytest.mark.parametrize('container',[None,[],5,'malformed'])
def test_changed_malformed_task_container_retains_original_error_inside_lock(tmp_path,monkeypatch,container):
 col=collector(tmp_path);value=state(col);value['tasks']=container
 decodes=_refine_changing_decode(col,monkeypatch,value);timestamps=_refine_trace_instant(col,monkeypatch)
 with pytest.raises(AttributeError):col.step(max_requests=1)
 assert timestamps==[(NOW,True)] and decodes==[False,True]
 assert not col.lock.is_locked



def test_changed_missing_task_container_retains_original_keyerror_inside_lock(tmp_path,monkeypatch):
 col=collector(tmp_path);value=state(col);del value['tasks']
 decodes=_refine_changing_decode(col,monkeypatch,value);timestamps=_refine_trace_instant(col,monkeypatch)
 with pytest.raises(KeyError) as error:col.step(max_requests=1)
 assert error.value.args==('tasks',) and timestamps==[(NOW,True)]
 assert decodes==[False,True] and not col.lock.is_locked



def test_changed_native_empty_map_uses_original_reader_without_extra_io(tmp_path,monkeypatch):
 col=collector(tmp_path);value=state(col);value['tasks']={}
 decodes=_refine_changing_decode(col,monkeypatch,value);calls=[]
 original=lambda state:calls.append(('original',col.lock.is_locked)) or state
 native=lambda state:calls.append(('native',col.lock.is_locked)) or state
 assert col._read_queue('step_due',original,native_reader=native)==value
 assert decodes==[False,True] and calls==[('original',True)]



def test_changed_native_decode_error_identity_precedes_clock_and_callback(tmp_path,monkeypatch):
 col=collector(tmp_path);value=state(col);value['extra']='changed'
 error=ValueError('fresh-native-original-error');original=M.json.loads;calls=[]
 def decode(raw,*args,**kwargs):
  calls.append(col.lock.is_locked)
  if len(calls)==1:result=original(raw,*args,**kwargs);publish(col,value);return result
  raise error
 monkeypatch.setattr(M.json,'loads',decode);col.clock=lambda:pytest.fail('Decode error reached clock')
 with pytest.raises(ValueError) as raised:col.step(max_requests=1)
 assert raised.value is error and calls==[False,True] and not col.lock.is_locked




def test_changed_read_keeps_stale_map_cleanup_after_lock_release(tmp_path, monkeypatch):
    col = collector(tmp_path)
    changed = state(col)
    changed["new-evidence"] = "fixture"
    original_decode = M.json.loads
    decodes, released = [], []
    class StaleMap(dict):
        def __del__(self):
            released.append(col.lock.is_locked)
    def decode(raw, *args, **kwargs):
        value = original_decode(raw, *args, **kwargs)
        decodes.append(col.lock.is_locked)
        if len(decodes) == 1:
            publish(col, changed)
            return StaleMap(value)
        return value
    monkeypatch.setattr(M.json, "loads", decode)
    monkeypatch.setattr(M, "_select_tasks", lambda *args: [])
    assert col.step(max_requests=1) == []
    assert decodes == [False, True]
    assert released == [False]
