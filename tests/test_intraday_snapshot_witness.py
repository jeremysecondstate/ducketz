"""Scout fresh inputs reuse native exact-byte validation, never mutable objects."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import sys

import pytest

from ml.intraday import local_forecast as module
from ml.intraday import prediction, store as store_module
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore


@pytest.fixture(autouse=True)
def clear_witnesses():
    with module._SNAPSHOT_VALIDATIONS_LOCK:
        module._SNAPSHOT_VALIDATIONS.clear()
    yield
    with module._SNAPSHOT_VALIDATIONS_LOCK:
        module._SNAPSHOT_VALIDATIONS.clear()


def case(tmp_path, payload=None):
    store = ShadowStore(tmp_path / 'inputs.sqlite3')
    payload = {'candles': [{'value': 'original'}], 'nested': {'a': [1, 2]}} if payload is None else payload
    raw = encoded(payload)
    key = digest(payload)
    with store.connect() as db:
        db.execute('INSERT INTO snapshots VALUES (?,?)', (key, raw))
        db.execute('INSERT INTO symbol_inputs VALUES (?,?,?)', ('cycle', 'DOCU', key))
    return store, key, raw


def write_raw(store, key, raw):
    with store.connect() as db:
        db.execute('UPDATE snapshots SET payload=? WHERE id=?', (raw, key))


def native_calls(function):
    calls = []
    code = ShadowStore._snapshot_value.__code__
    old = sys.getprofile()
    def observe(frame, event, arg):
        if event == 'call' and frame.f_code is code:
            calls.append(None)
        if old is not None:
            old(frame, event, arg)
    sys.setprofile(observe)
    try:
        result = function()
    finally:
        sys.setprofile(old)
    return result, len(calls)


def test_first_load_keeps_native_full_validation(tmp_path):
    store, key, raw = case(tmp_path)
    result, calls = native_calls(lambda: module._saved_inputs(store, 'cycle', 'DOCU'))
    assert result == store.inputs('cycle', 'DOCU')
    assert calls == 1


def test_witness_load_decodes_fresh_owned_data_without_duplicate_canonical_hash(tmp_path):
    store, key, raw = case(tmp_path)
    assert module._validate_saved_snapshot(key, raw) is None
    result, calls = native_calls(lambda: module._saved_inputs(store, 'cycle', 'DOCU'))
    assert calls == 0
    assert result == store.inputs('cycle', 'DOCU')
    result[1]['nested']['a'].append(999)
    again, calls = native_calls(lambda: module._saved_inputs(store, 'cycle', 'DOCU'))
    assert calls == 0
    assert again == store.inputs('cycle', 'DOCU')
    assert result[1] is not again[1] and result[1]['nested'] is not again[1]['nested']


@pytest.mark.parametrize('replacement', ['{"changed":true}', '[]', 'null', '{broken', ''])
def test_changed_current_raw_bytes_do_not_reuse_witness(tmp_path, replacement):
    store, key, raw = case(tmp_path)
    module._validate_saved_snapshot(key, raw)
    write_raw(store, key, replacement)
    with pytest.raises((ValueError, TypeError)):
        module._saved_inputs(store, 'cycle', 'DOCU')


def test_fresh_link_and_missing_row_are_observed(tmp_path):
    store, key, raw = case(tmp_path)
    module._validate_saved_snapshot(key, raw)
    with store.connect() as db:
        db.execute('DELETE FROM symbol_inputs')
    assert module._saved_inputs(store, 'cycle', 'DOCU') is None
    with store.connect() as db:
        db.execute('INSERT INTO symbol_inputs VALUES (?,?,?)', ('cycle', 'DOCU', 'absent'))
    assert module._saved_inputs(store, 'cycle', 'DOCU') is None


@pytest.mark.parametrize('raw', [b'{"input":"yes"}', bytearray(b'{"input":"yes"}')])
def test_binary_rows_keep_native_decoder_and_validator(tmp_path, raw):
    store, key, _ = case(tmp_path, {'input':'yes'})
    write_raw(store, key, raw)
    expected = store.inputs('cycle', 'DOCU')
    assert module._validate_saved_snapshot(key, bytes(raw)) is None
    result, calls = native_calls(lambda: module._saved_inputs(store, 'cycle', 'DOCU'))
    assert result == expected and calls == 1


def test_text_bom_keeps_native_rejection(tmp_path):
    store, key, raw = case(tmp_path)
    module._validate_saved_snapshot(key, raw)
    write_raw(store, key, '\ufeff' + raw)
    with pytest.raises(ValueError):
        module._saved_inputs(store, 'cycle', 'DOCU')


def test_valid_noncanonical_text_requires_successful_native_witness(tmp_path):
    store, key, raw = case(tmp_path)
    raw = json.dumps(json.loads(raw), indent=2)
    assert hashlib.sha256(raw.encode()).hexdigest() != key
    write_raw(store, key, raw)
    module._validate_saved_snapshot(key, raw)
    result, calls = native_calls(lambda: module._saved_inputs(store, 'cycle', 'DOCU'))
    assert result == store.inputs('cycle', 'DOCU') and calls == 0


def test_raw_hash_identity_alone_never_establishes_canonical_validity(tmp_path):
    store = ShadowStore(tmp_path/'raw.sqlite3')
    raw = '{ "x": 1 }'
    key = hashlib.sha256(raw.encode()).hexdigest()
    assert key != digest(json.loads(raw))
    with store.connect() as db:
        db.execute('INSERT INTO snapshots VALUES (?,?)', (key, raw))
        db.execute('INSERT INTO symbol_inputs VALUES (?,?,?)', ('cycle','DOCU',key))
    with pytest.raises(ContractError):
        module._validate_saved_snapshot(key, raw)
    with pytest.raises(ContractError):
        module._saved_inputs(store, 'cycle', 'DOCU')


def test_subclass_and_instance_input_overrides_keep_original_calls(tmp_path):
    class CustomStore(ShadowStore):
        def inputs(self, cycle, symbol):
            return ('custom', (cycle, symbol))
    custom = CustomStore(tmp_path/'custom.sqlite3')
    assert module._saved_inputs(custom, 'x','Y') == ('custom', ('x','Y'))
    store, key, raw = case(tmp_path/'native')
    store.inputs = lambda cycle,symbol: ('instance',(cycle,symbol))
    assert module._saved_inputs(store,'x','Y') == ('instance', ('x','Y'))


def test_instance_validator_override_keeps_original_calls(tmp_path):
    store, key, raw = case(tmp_path)
    module._validate_saved_snapshot(key, raw)
    calls=[]
    store._snapshot_value=lambda k,r: calls.append((k,r)) or {'custom':True}
    assert module._saved_inputs(store,'cycle','DOCU') == (key,{'custom':True})
    assert calls == [(key,raw)]


def test_custom_connection_keeps_original_reader_boundary(tmp_path):
    store, key, raw = case(tmp_path)
    original=store.connect
    calls=[]
    @contextmanager
    def custom():
        calls.append('connect')
        with original() as db:
            yield db
    store.connect=custom
    assert module._saved_inputs(store,'cycle','DOCU') == (key,json.loads(raw))
    assert calls == ['connect']


def test_warm_native_witness_does_not_suppress_later_custom_validator(tmp_path,monkeypatch):
    store,key,raw=case(tmp_path)
    module._validate_saved_snapshot(key,raw)
    calls=[]
    def custom(k,r):
        calls.append((k,r))
        raise RuntimeError('custom validator retained')
    monkeypatch.setattr(ShadowStore,'_snapshot_value',staticmethod(custom))
    with pytest.raises(RuntimeError,match='custom validator retained'):
        module._saved_inputs(store,'cycle','DOCU')
    assert calls == [(key,raw)]


@pytest.mark.parametrize('callback', ['validator','digest','loads'])
def test_custom_to_native_transition_cannot_poison_native_witness(tmp_path,monkeypatch,callback):
    store,key,raw=case(tmp_path)
    changed='{"counterfeit":true}'
    write_raw(store,key,changed)
    with monkeypatch.context() as patch:
        if callback == 'validator':
            patch.setattr(ShadowStore,'_snapshot_value',staticmethod(lambda k,r:{}))
        elif callback == 'digest':
            patch.setattr(store_module,'digest',lambda v:key)
        else:
            patch.setattr(json,'loads',lambda v:json.JSONDecoder().decode(raw))
        module._validate_saved_snapshot(key,changed)
        assert list(module._SNAPSHOT_VALIDATIONS.values()) == [False]
    with pytest.raises(ContractError):
        module._saved_inputs(store,'cycle','DOCU')


@pytest.mark.parametrize('callback', ['loads','encoded','digest','decoder','encoder'])
def test_current_serializer_overrides_decline_warm_fastpath(tmp_path,monkeypatch,callback):
    store,key,raw=case(tmp_path)
    module._validate_saved_snapshot(key,raw)
    calls=[]
    if callback == 'loads':
        original=json.loads
        monkeypatch.setattr(json,'loads',lambda *a,**k:calls.append(callback) or original(*a,**k))
    elif callback == 'encoded':
        original=prediction.encoded
        monkeypatch.setattr(prediction,'encoded',lambda v:calls.append(callback) or original(v))
    elif callback == 'digest':
        original=store_module.digest
        monkeypatch.setattr(store_module,'digest',lambda v:calls.append(callback) or original(v))
    elif callback == 'decoder':
        original=json._default_decoder.decode
        monkeypatch.setattr(json._default_decoder,'decode',lambda *a,**k:calls.append(callback) or original(*a,**k))
    else:
        original=json.JSONEncoder.default
        monkeypatch.setattr(json.JSONEncoder,'default',lambda *a,**k:calls.append(callback) or original(*a,**k))
    result, validations = native_calls(lambda: module._saved_inputs(store,'cycle','DOCU'))
    assert result == (key,json.JSONDecoder().decode(raw))
    assert validations == 1
    assert calls or callback == 'encoder'


def test_witness_limit_eviction_forces_original_validation(tmp_path):
    store,key,raw=case(tmp_path)
    module._validate_saved_snapshot(key,raw)
    for i in range(module._SNAPSHOT_CACHE_LIMIT):
        other={'i':i}
        module._validate_saved_snapshot(digest(other),encoded(other))
    assert len(module._SNAPSHOT_VALIDATIONS) == module._SNAPSHOT_CACHE_LIMIT
    _,calls=native_calls(lambda:module._saved_inputs(store,'cycle','DOCU'))
    assert calls == 1


def test_concurrent_loads_preserve_ownership_and_cache_bound(tmp_path):
    store,key,raw=case(tmp_path)
    module._validate_saved_snapshot(key,raw)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results=list(workers.map(lambda _:module._saved_inputs(store,'cycle','DOCU'), range(20)))
    assert all(result==(key,json.loads(raw)) for result in results)
    assert len({id(result[1]) for result in results}) == 20
    assert len({id(result[1]['nested']) for result in results}) == 20
    assert len(module._SNAPSHOT_VALIDATIONS) <= module._SNAPSHOT_CACHE_LIMIT


@pytest.mark.parametrize('field,replacement', [('parse_float',str),('strict',False),('memo',{'x':'custom'})])
def test_default_decoder_state_change_cannot_create_native_witness(tmp_path,monkeypatch,field,replacement):
    store,key,raw=case(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(json._default_decoder,field,replacement)
        module._validate_saved_snapshot(key,raw)
        assert list(module._SNAPSHOT_VALIDATIONS.values()) == [False]
    result,calls=native_calls(lambda:module._saved_inputs(store,'cycle','DOCU'))
    assert result == store.inputs('cycle','DOCU') and calls == 1


@pytest.mark.parametrize('field,replacement', [('parse_float',str),('strict',False),('memo',{'x':'custom'})])
def test_default_decoder_state_change_declines_existing_native_witness(tmp_path,monkeypatch,field,replacement):
    store,key,raw=case(tmp_path)
    module._validate_saved_snapshot(key,raw)
    monkeypatch.setattr(json._default_decoder,field,replacement)
    result,calls=native_calls(lambda:module._saved_inputs(store,'cycle','DOCU'))
    assert result == store.inputs('cycle','DOCU') and calls == 1


def test_fresh_changed_link_returns_only_new_verified_snapshot(tmp_path):
    store,key,raw=case(tmp_path)
    module._validate_saved_snapshot(key,raw)
    replacement={'alternate':[1,2,3]}
    alternate=digest(replacement)
    with store.connect() as db:
        db.execute('INSERT INTO snapshots VALUES (?,?)',(alternate,encoded(replacement)))
        db.execute('UPDATE symbol_inputs SET snapshot_id=? WHERE cycle_id=? AND symbol=?',
                   (alternate,'cycle','DOCU'))
    result,calls=native_calls(lambda:module._saved_inputs(store,'cycle','DOCU'))
    assert result == (alternate,replacement) and result[0] != key and calls == 1


def test_store_json_module_override_cannot_seed_native_witness(tmp_path,monkeypatch):
    from types import SimpleNamespace
    store,key,raw=case(tmp_path)
    changed='{"counterfeit":true}'
    write_raw(store,key,changed)
    custom=SimpleNamespace(loads=lambda value:json.loads(raw))
    with monkeypatch.context() as patch:
        patch.setattr(store_module,'json',custom)
        module._validate_saved_snapshot(key,changed)
        assert list(module._SNAPSHOT_VALIDATIONS.values()) == [False]
    with pytest.raises(ContractError):
        module._saved_inputs(store,'cycle','DOCU')


def test_custom_key_cannot_seed_native_witness(tmp_path):
    store,key,raw=case(tmp_path)
    class CustomKey(str):
        pass
    module._validate_saved_snapshot(CustomKey(key),raw)
    assert list(module._SNAPSHOT_VALIDATIONS.values()) == [False]
    result,calls=native_calls(lambda:module._saved_inputs(store,'cycle','DOCU'))
    assert result == store.inputs('cycle','DOCU') and calls == 1


@pytest.mark.parametrize('target',['class','instance'])
def test_sqlite_factory_validator_change_after_entry_keeps_current_native_calls(tmp_path,monkeypatch,target):
    store,key,raw=case(tmp_path)
    module._validate_saved_snapshot(key,raw)
    connect=store_module.sqlite3.connect
    results=[]
    for reader in (store.inputs, lambda cycle,symbol:module._saved_inputs(store,cycle,symbol)):
        calls=[]
        with monkeypatch.context() as patch:
            def custom(k,r):
                calls.append((k,r))
                return {'current_validator':'preserved'}
            def factory(*args,**kwargs):
                db=connect(*args,**kwargs)
                if target=='class':
                    patch.setattr(ShadowStore,'_snapshot_value',staticmethod(custom))
                else:
                    patch.setitem(vars(store),'_snapshot_value',custom)
                return db
            patch.setattr(store_module.sqlite3,'connect',factory)
            results.append(reader('cycle','DOCU'))
            assert calls==[(key,raw)]
    assert results == [(key,{'current_validator':'preserved'})]*2
