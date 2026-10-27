"""Exact native read snapshots, concurrent changes and lock boundaries; fixtures only."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import types

from filelock import FileLock, Timeout
import pytest

from ml.intraday import market_collection as M
NOW = datetime(2026, 10, 27, 14, 45, tzinfo=timezone.utc)


def collector(tmp_path, cls=None, adapter=None):
    request = M.SourceRequest(provider='fmp', source_kind='stock_quote', symbol='SDGR',
        dataset='stable', schema='quote', endpoint='quote', params={'symbol': 'SDGR'})
    col = (cls or M.QuarterHourMarketCollector)(tmp_path, symbols=['SDGR'], requests=[request],
        adapters={'fmp': adapter or (lambda *args: M.CollectionResult([], 'EMPTY'))}, clock=lambda: NOW)
    col.enqueue(NOW - timedelta(minutes=15))
    return col


def state(col):
    return json.loads(col.state_path.read_text(encoding='utf8'))


def publish(col, value):
    col.state_path.write_text(json.dumps(value), encoding='utf8')


def test_native_decode_outside_lock_and_final_reader_inside(tmp_path, monkeypatch):
    col = collector(tmp_path)
    original = M.json.loads
    calls = []
    def decode(raw, *args, **kwargs):
        calls.append(col.lock.is_locked)
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(M.json, 'loads', decode)
    result = col._read_queue('task_recheck', lambda value: (col.lock.is_locked, value['tasks']))
    assert calls == [False]
    assert result[0] is True and len(result[1]) == 1
    assert col.lock.is_locked is False


def test_every_operation_rereads_rows_without_a_cache(tmp_path):
    col = collector(tmp_path)
    first = col._read_queue('step_due', lambda value: value)
    updated = state(col)
    updated['added-evidence'] = {'nested': ['new', 2]}
    publish(col, updated)
    second = col._read_queue('step_due', lambda value: value)
    assert 'added-evidence' not in first
    assert second == updated and second is not first


@pytest.mark.parametrize('phase', ['task_recheck', 'source_block', 'step_due', 'capability_manifest'])
def test_change_during_decode_uses_latest_original_state_once(tmp_path, monkeypatch, phase):
    col = collector(tmp_path)
    before = state(col)
    after = json.loads(json.dumps(before))
    key = next(iter(after['tasks']))
    after['tasks'][key]['status'] = 'COMPLETE'
    after['source_blocks'][col.requests[0].identity] = {'observed_at': NOW.isoformat(), 'snapshot': 'fixture'}
    original = M.json.loads
    calls = []
    def decode(raw, *args, **kwargs):
        calls.append(col.lock.is_locked)
        value = original(raw, *args, **kwargs)
        if len(calls) == 1:
            assert col.lock.is_locked is False
            publish(col, after)
        return value
    monkeypatch.setattr(M.json, 'loads', decode)
    result = col._read_queue(phase, lambda value: (col.lock.is_locked, value))
    assert calls == [False, True]
    assert result == (True, after)
    assert col.lock.is_locked is False


def test_instance_custom_state_is_called_once_under_original_lock(tmp_path):
    col = collector(tmp_path)
    calls = []
    custom = {'schema_version': 'custom', 'private-field': object()}
    def overridden(self):
        calls.append(self.lock.is_locked)
        return custom
    col._state = types.MethodType(overridden, col)
    assert col._read_queue('task_recheck', lambda value: value) is custom
    assert calls == [True]


def test_subclass_state_remains_on_original_locked_path(tmp_path):
    class Custom(M.QuarterHourMarketCollector):
        def _state(self):
            assert self.lock.is_locked
            value = super()._state()
            value['custom'] = 1
            return value
    col = collector(tmp_path, cls=Custom)
    assert col._read_queue('task_recheck', lambda value: value)['custom'] == 1


def test_mid_decode_state_override_is_not_bypassed(tmp_path, monkeypatch):
    col = collector(tmp_path)
    original = M.json.loads
    custom = {'current-custom': object()}
    calls = []
    def overridden(self):
        calls.append(self.lock.is_locked)
        return custom
    def decode(raw, *args, **kwargs):
        col._state = types.MethodType(overridden, col)
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(M.json, 'loads', decode)
    assert col._read_queue('source_block', lambda value: value) is custom
    assert calls == [True]


def test_class_state_override_is_not_mistaken_for_native(tmp_path, monkeypatch):
    col = collector(tmp_path)
    calls = []
    def custom(self):
        calls.append(self.lock.is_locked)
        return {'class-custom': 1}
    monkeypatch.setattr(M.QuarterHourMarketCollector, '_state', custom)
    assert col._read_queue('source_block', lambda value: value) == {'class-custom': 1}
    assert calls == [True]


@pytest.mark.parametrize('raw', ['{bad', '[]', '{"schema_version":"wrong"}', 'null'])
def test_original_malformed_queue_exception_is_preserved(tmp_path, raw):
    col = collector(tmp_path)
    col.state_path.write_text(raw)
    with col.lock:
        try:
            col._state()
        except Exception as error:
            expected = (type(error), str(error))
        else:
            pytest.fail('Original fixture unexpectedly accepted')
    with pytest.raises(expected[0]) as raised:
        col._read_queue('source_block', lambda value: pytest.fail('Malformed state reached reader'))
    assert str(raised.value) == expected[1]
    assert col.lock.is_locked is False


def test_decode_error_keeps_precedence_when_file_changes(tmp_path, monkeypatch):
    col = collector(tmp_path)
    good = state(col)
    original = M.json.loads
    error = ValueError('original decoded evidence error')
    def decode(raw, *args, **kwargs):
        publish(col, good)
        raise error
    monkeypatch.setattr(M.json, 'loads', decode)
    with pytest.raises(ValueError) as raised:
        col._read_queue('source_block', lambda value: pytest.fail('Reader called'))
    assert raised.value is error and not col.lock.is_locked
    monkeypatch.setattr(M.json, 'loads', original)


def test_first_lock_timeout_is_native_without_decode_or_retry(tmp_path, monkeypatch):
    col = collector(tmp_path)
    col.lock.timeout = 0
    holder = FileLock(col.lock.lock_file, timeout=0)
    monkeypatch.setattr(M.json, 'loads', lambda *args: pytest.fail('Timeout decoded state'))
    before = len(col.queue_lock_timing.snapshot()['recent'])
    with holder, pytest.raises(Timeout) as raised:
        col._read_queue('task_recheck', lambda value: pytest.fail('Timeout reached reader'))
    assert raised.value.lock_file == col.lock.lock_file
    assert col.lock.timeout == 0
    rows = col.queue_lock_timing.snapshot()['recent'][before:]
    assert len(rows) == 1 and rows[0]['outcome'] == 'acquire_error'


def test_second_lock_timeout_is_native_bounded_and_no_reader(tmp_path, monkeypatch):
    col = collector(tmp_path)
    col.lock.timeout = 0
    holder = FileLock(col.lock.lock_file, timeout=0)
    original = M.json.loads
    def decode(raw, *args, **kwargs):
        assert not col.lock.is_locked
        holder.acquire()
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(M.json, 'loads', decode)
    before = len(col.queue_lock_timing.snapshot()['recent'])
    try:
        with pytest.raises(Timeout) as raised:
            col._read_queue('task_recheck', lambda value: pytest.fail('Contended final read reached reader'))
        assert raised.value.lock_file == col.lock.lock_file and holder.is_locked
        rows = col.queue_lock_timing.snapshot()['recent'][before:]
        assert [row['outcome'] for row in rows] == ['completed', 'acquire_error']
    finally:
        holder.release()


def test_final_reader_error_identity_and_release_preserved(tmp_path):
    col = collector(tmp_path)
    error = RuntimeError('reader original')
    def fail(value):
        assert col.lock.is_locked
        raise error
    with pytest.raises(RuntimeError) as raised:
        col._read_queue('source_block', fail)
    assert raised.value is error and not col.lock.is_locked


def test_provider_adapter_and_receipt_work_stay_outside_global_lock(tmp_path):
    calls = []
    col = collector(tmp_path)
    def adapter(*args):
        assert not col.lock.is_locked
        calls.append(args)
        return M.CollectionResult([], 'EMPTY')
    col.adapters = {'fmp': adapter}
    before = state(col)
    key = next(iter(before['tasks']))
    receipts = col.step(max_requests=1)
    assert len(calls) == len(receipts) == 1
    after = state(col)
    assert after['tasks'][key]['status'] == 'EMPTY'
    assert after['tasks'][key]['attempts'] == 1
    assert len(after['tasks'][key]['snapshots']) == 1


@pytest.mark.parametrize('phase', ['task_recheck', 'source_block'])
def test_actual_step_respects_concurrent_settlement_or_source_block(tmp_path, monkeypatch, phase):
    col = collector(tmp_path)
    latest = state(col)
    key = next(iter(latest['tasks']))
    if phase == 'task_recheck':
        latest['tasks'][key]['status'] = 'COMPLETE'
    else:
        latest['source_blocks'][col.requests[0].identity] = {'observed_at': NOW.isoformat(), 'snapshot': 'fixture'}
    active, updates = [], []
    original_read = col._read_queue
    original_decode = M.json.loads
    def scoped_read(name, reader):
        active.append(name)
        try:
            return original_read(name, reader)
        finally:
            active.pop()
    def decode(raw, *args, **kwargs):
        value = original_decode(raw, *args, **kwargs)
        if active and active[-1] == phase and not updates:
            assert not col.lock.is_locked
            publish(col, latest)
            updates.append(phase)
        return value
    col._read_queue = scoped_read
    col.adapters = {'fmp': lambda *args: pytest.fail('Concurrent disposition reached provider')}
    monkeypatch.setattr(M.json, 'loads', decode)
    receipts = col.step(max_requests=1)
    assert updates == [phase]
    if phase == 'task_recheck':
        assert receipts == []
        assert state(col)['tasks'][key] == latest['tasks'][key]
    else:
        assert len(receipts) == 1 and receipts[0]['status'] == 'NOT_AUTHORIZED'
        assert receipts[0]['records'] == []
        assert state(col)['tasks'][key]['status'] == 'NOT_AUTHORIZED'

