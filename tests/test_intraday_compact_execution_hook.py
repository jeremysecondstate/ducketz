"""The split service may supply verified small inputs to the existing executor."""
import pytest

from ml.intraday import scout_local_execution as execution
from test_intraday_scout_local_execution import env


def test_verified_compact_input_reader_replaces_only_full_sizing_snapshot_read(env, monkeypatch):
    original = env.store.inputs(env.forecast['cycle_id'], env.forecast['symbol'])
    calls = []
    def read(cycle_id, symbol, *, forecast):
        calls.append((cycle_id, symbol, forecast['forecast_id']))
        return original
    monkeypatch.setattr(env.store, 'execution_inputs', read, raising=False)
    def forbidden(*_):
        raise AssertionError('The compact-capable store must not reread the full matrix for sizing')
    monkeypatch.setattr(execution, '_saved_inputs', forbidden)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'submitted'
    assert calls == [(env.forecast['cycle_id'], 'DOCU', env.forecast['forecast_id'])]
    assert len(env.broker.posts) == 1


@pytest.mark.parametrize('reader', [None, 7])
def test_legacy_noncallable_reader_keeps_original_validated_sizing_inputs(env, monkeypatch, reader):
    monkeypatch.setattr(env.store, 'execution_inputs', reader, raising=False)
    native = execution._saved_inputs
    calls = []
    def read(*args):
        calls.append(args[1:])
        return native(*args)
    monkeypatch.setattr(execution, '_saved_inputs', read)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'submitted'
    assert calls == [(env.forecast['cycle_id'], 'DOCU')]
    assert len(env.broker.posts) == 1
