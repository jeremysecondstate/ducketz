"""Native first-read WAL recovery retains exact evidence and effect gates."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ml.intraday import local_forecast as reader
from ml.intraday import scout_local_execution as execution
from ml.intraday.prediction import ContractError, instant
from test_intraday_scout_local_execution import env


def read(env, module=reader, path=None):
    return module._saved_forecast(path or env.store.path, env.forecast,
        session_id=env.executor.session_id, session_started=env.executor.session_started_at)


def instrument(monkeypatch, *, first_error=None, setup_elapsed=0):
    """Observe real native connections without replacing the connect boundary."""
    traces, closed, clock = [], [], SimpleNamespace(now=0.0)
    monkeypatch.setattr(reader, 'time', SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda delay: setattr(clock, 'now', clock.now + delay)))
    class Connection:
        def __init__(self, db):
            self.db = db
            self.sql = []
            traces.append(self.sql)
        def execute(self, sql, *args):
            self.sql.append(sql)
            if sql == 'BEGIN':
                clock.now += setup_elapsed
            if (sql.startswith('SELECT ' + reader._FORECAST_COLUMNS)
                    and len(traces) == 1 and first_error is not None):
                raise first_error
            return self.db.execute(sql, *args)
        def close(self):
            self.db.close(); closed.append(self)
    monkeypatch.setattr(reader, 'closing', lambda db: closing(Connection(db)))
    return traces, closed, clock


@pytest.mark.parametrize('setup_elapsed,expected_ms', [(0, 10), (.246, 4), (.2495, 0), (.3, 0)])
def test_only_first_select_uses_existing_pause_remaining_after_zero_wait_setup(
        env, monkeypatch, setup_elapsed, expected_ms):
    traces, closed, _ = instrument(monkeypatch, setup_elapsed=setup_elapsed)
    assert read(env) is None
    sql = traces[0]
    assert sql[:2] == ['PRAGMA query_only=ON', 'BEGIN']
    busy = [value for value in sql if value.startswith('PRAGMA busy_timeout=')]
    assert busy == ([f'PRAGMA busy_timeout={expected_ms}', 'PRAGMA busy_timeout=0'] if expected_ms else [])
    if expected_ms:
        first = next(i for i, value in enumerate(sql) if value.startswith('SELECT ' + reader._FORECAST_COLUMNS))
        assert sql[first - 1] == f'PRAGMA busy_timeout={expected_ms}'
        assert sql[first + 1] == 'PRAGMA busy_timeout=0'
    assert len(closed) == 1 and env.broker.posts == []


def test_retry_returns_to_zero_wait_and_preserves_original_error_contract(env, monkeypatch):
    error = sqlite3.OperationalError('database is locked')
    error.sqlite_errorcode = 261
    cause = ValueError('fixture original cause'); error.__cause__ = cause
    traces, closed, clock = instrument(monkeypatch, first_error=error)
    assert read(env) is None
    assert len(traces) == len(closed) == 2 and clock.now == .01
    assert 'PRAGMA busy_timeout=10' in traces[0]
    assert not any(sql.startswith('PRAGMA busy_timeout=') for sql in traces[1])
    assert error.__cause__ is cause and not hasattr(error, 'scout_saved_forecast_retry')
    assert env.broker.posts == []


def test_legacy_once_receives_original_signature_only(env, monkeypatch):
    calls = []
    def once(path, forecast, *, session_id, session_started):
        calls.append((path, forecast, session_id, session_started))
        return 'custom exact result'
    monkeypatch.setattr(reader, '_saved_forecast_once', once)
    assert read(env) == 'custom exact result' and len(calls) == 1


def test_later_custom_connect_retains_zero_wait_and_original_statements(env, monkeypatch):
    native, kwargs, traces = sqlite3.connect, [], []
    def connect(*args, **options):
        kwargs.append(options)
        db = native(*args, **options); db.set_trace_callback(traces.append)
        return db
    monkeypatch.setattr(reader.sqlite3, 'connect', connect)
    assert read(env) is None
    assert kwargs == [dict(uri=True, timeout=0)]
    assert not any(sql.startswith('PRAGMA busy_timeout=') for sql in traces)


def test_preimport_python_connect_is_never_admitted_as_native(env, monkeypatch):
    native, options = sqlite3.connect, []
    def connect(*args, **kwargs):
        options.append(kwargs)
        return native(*args, **kwargs)
    with monkeypatch.context() as nested:
        nested.setattr(sqlite3, 'connect', connect)
        spec = importlib.util.spec_from_file_location('ml.intraday._native_busy_custom_import', reader.__file__)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        assert module._NATIVE_READ_CONNECT_IS_BUILTIN is False
        assert read(env, module) is None
    assert options == [dict(uri=True, timeout=0)]


@pytest.mark.parametrize('change', ['target_expired', 'manual_stop'])
def test_successful_native_read_cannot_authorize_effects_after_time_or_stop_change(env, monkeypatch, change):
    if change == 'target_expired':
        env.state.now = instant(env.forecast['target_close']) - timedelta(seconds=1)
    original, seen = reader._safe_path, []
    def safe_path(path):
        result = original(path)
        if not seen:
            seen.append(True)
            if change == 'manual_stop':
                env.state.stop = True
            else:
                env.state.now += timedelta(seconds=2)
        return result
    monkeypatch.setattr(reader, '_safe_path', safe_path)
    try:
        result = env.executor.step(env.forecast)
    except ContractError:
        result = None
    assert seen, result
    assert env.broker.posts == [] and env.broker.deletes == []
    if result is not None:
        assert result.get('disposition') != 'submitted'


def test_real_persistent_lock_never_reconfigures_the_pause_on_later_attempts(env, monkeypatch):
    locker = sqlite3.connect(env.store.path)
    locker.execute('PRAGMA journal_mode=DELETE'); locker.execute('BEGIN EXCLUSIVE')
    traces, connections = [], []
    class Connection:
        def __init__(self, db):
            self.db, self.sql = db, []
            traces.append(self.sql)
        def execute(self, sql, *args):
            self.sql.append(sql)
            return self.db.execute(sql, *args)
        def close(self):
            self.db.close(); connections.append(self)
    monkeypatch.setattr(reader, 'closing', lambda db: closing(Connection(db)))
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            read(env)
    finally:
        locker.rollback(); locker.close()
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    retry = caught.value.scout_saved_forecast_retry
    assert retry['retry_budget_seconds'] == .25 and retry['maximum_attempts'] == 8
    assert 1 <= retry['attempt_count'] <= 8
    assert len(connections) == len(traces) == retry['attempt_count']
    assert traces[0][:3] == ['PRAGMA query_only=ON', 'BEGIN', 'PRAGMA busy_timeout=10']
    assert not any(sql.startswith('PRAGMA busy_timeout=') for trace in traces[1:] for sql in trace)
    assert env.broker.posts == [] and env.broker.deletes == []


def test_real_wal_recovery_first_read_and_complete_exact_validation(env, tmp_path, monkeypatch):
    # Copy only this synthetic Scout fixture. The deliberately unclean private
    # child exit leaves a WAL for real recovery; it never touches a trader.
    master = tmp_path / 'recovery-master.sqlite3'
    shutil.copyfile(env.store.path, master)
    child = '''import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1]);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
db.execute('CREATE TABLE recovery_work(data BLOB)')
db.executemany('INSERT INTO recovery_work VALUES(zeroblob(4096))',(()for _ in range(16384)))
db.commit();os._exit(0)
'''
    subprocess.run([sys.executable, '-B', '-c', child, str(master)], check=True, timeout=30)
    wal = Path(str(master) + '-wal'); assert wal.stat().st_size > 64 * 1024 * 1024
    counts, observations = [], []
    for arm in ('zero-wait-first-read', 'complete-native-retry'):
        path = tmp_path / (arm + '.sqlite3')
        shutil.copyfile(master, path); shutil.copyfile(wal, Path(str(path) + '-wal'))
        barrier = threading.Barrier(16)
        def run(index):
            # Test synchronization must precede the reader's original deadline;
            # a rendezvous inside SELECT would consume its production budget.
            barrier.wait(timeout=10)
            started = time.monotonic()
            try:
                if arm == 'zero-wait-first-read':
                    reader._saved_forecast_once(path, env.forecast,
                        session_id=env.executor.session_id, session_started=env.executor.session_started_at)
                else:
                    read(env, path=path)
                return dict(index=index, result='validated', elapsed_seconds=time.monotonic()-started)
            except sqlite3.OperationalError as error:
                return dict(index=index, result=error.sqlite_errorcode, sqlite_errorname=error.sqlite_errorname,
                    elapsed_seconds=time.monotonic()-started,
                    saved_forecast_retry=getattr(error, 'scout_saved_forecast_retry', None))
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(run, range(16)))
        counts.append([record['result'] for record in results])
        observations.append(dict(arm=arm, records=results))
    with (tmp_path/'native-recovery-observations.json').open('x') as stream:
        json.dump(observations, stream, indent=2)
    assert 261 in counts[0], observations
    assert counts[1] == ['validated'] * 16, observations
    assert env.broker.posts == [] and env.broker.deletes == []
