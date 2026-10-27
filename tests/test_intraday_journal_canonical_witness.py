from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import sqlite3
import sys

import pytest

from ml.intraday import scout_journal as journal
from ml.intraday.scout_protocol import Request


def original_checked_json(payload, digest):
    # Frozen pre-change source. Custom callbacks remain active in the oracle.
    try:
        value = json.loads(payload)
        encoded = journal._encoded(value)
        if encoded != payload or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise journal.JournalCorruptionError("Stored payload hash or canonical bytes differ")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise journal.JournalCorruptionError("Invalid stored canonical payload") from exc


def error_chain(error):
    return (type(error), str(error), error_chain(error.__cause__) if error.__cause__ else None)


def outcome(call, raw, digest):
    try:
        return ("returned", call(raw, digest))
    except Exception as error:
        return ("raised", error_chain(error))


def payload(value):
    raw = journal._encoded(value)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture(autouse=True)
def empty_witnesses():
    decoder = json._default_decoder
    initial = {key: vars(decoder).get(key) for key in ("decode", "raw_decode")}
    absent = {key for key in initial if key not in vars(decoder)}
    with journal._canonical_witness_lock:
        journal._canonical_witnesses.clear()
        journal._canonical_witness_bytes = 0
    yield
    # pytest's setattr restoration can install a class-bound method as a new
    # instance attribute; remove that fixture artifact after hook tests.
    for key in initial:
        if key in absent:
            vars(decoder).pop(key, None)
        else:
            setattr(decoder, key, initial[key])


@pytest.mark.parametrize("value", [
    {}, {"unicode": "雪é", "newline": "a\nb"}, {"n": -(2**63)}, {"n": 2**63-1},
    {"n": -0.0}, {"n": 5e-324}, {"v": True}, {"v": None},
    {"list": [1, False, None, {"x": "ok"}]},
])
def test_native_hit_reparses_and_never_reuses_mutable_values(value, monkeypatch):
    raw, digest = payload(value)
    calls = []
    native = journal._canonical_witness_shallow
    def shallow(current):
        calls.append(current)
        return native(current)
    monkeypatch.setattr(journal, "_canonical_witness_shallow", shallow)
    first = journal._checked_json(raw, digest)
    assert (raw, digest) in journal._canonical_witnesses
    first["changed"] = {"x": 1}
    second = journal._checked_json(raw, digest)
    assert second == value and second is not first
    assert len(calls) == 1  # The second call used a native raw-byte witness.
    assert outcome(journal._checked_json, raw, digest) == outcome(original_checked_json, raw, digest)


@pytest.mark.parametrize("raw,digest", [
    ('{"a":1}', '0'*64), ('{ "a":1}', '0'*64), ('{"a":NaN}', '0'*64),
    ('{"a":Infinity}', '0'*64), ('{"a":9223372036854775808}', '0'*64),
    ('{"a":1,"a":2}', '0'*64), ('[1]', '0'*64), ('bad', '0'*64),
    ('{"x":"\\ud800"}', '0'*64),
])
def test_malformed_or_changed_row_after_hit_retains_exact_exception_chain(raw, digest):
    good = payload({"a": 1})
    journal._checked_json(*good)
    journal._checked_json(*good)
    assert outcome(journal._checked_json, raw, digest) == outcome(original_checked_json, raw, digest)
    assert (raw, digest) not in journal._canonical_witnesses


@pytest.mark.parametrize("hook", ["encoded", "normalizer", "hash", "dumps", "decoder"])
def test_warm_witness_never_bypasses_replaced_validation_hooks(monkeypatch, hook):
    raw, digest = payload({"a": 1})
    journal._checked_json(raw, digest)
    def fail(*args, **kwargs):
        raise ValueError("custom validation failure")
    if hook == "encoded": monkeypatch.setattr(journal, "_encoded", fail)
    if hook == "normalizer": monkeypatch.setattr(journal._scout_protocol, "_json_value", fail)
    if hook == "hash": monkeypatch.setattr(journal.hashlib, "sha256", fail)
    if hook == "dumps": monkeypatch.setattr(journal.json, "dumps", fail)
    if hook == "decoder": monkeypatch.setattr(journal.json, "loads", fail)
    assert outcome(journal._checked_json, raw, digest) == outcome(original_checked_json, raw, digest)


def test_decoder_callback_changing_later_validator_keeps_original_order(monkeypatch):
    raw, digest = payload({"a": 1})
    journal._checked_json(raw, digest)
    native_loads, native_encoded = json.loads, journal._encoded
    traces=[]
    def fail(value):
        traces.append("encoded")
        raise TypeError("decoder changed later validator")
    def decode(value):
        traces.append("decode")
        monkeypatch.setattr(journal, "_encoded", fail)
        return native_loads(value)
    monkeypatch.setattr(json, "loads", decode)
    actual=outcome(journal._checked_json,raw,digest);actual_trace=list(traces)
    monkeypatch.setattr(journal,"_encoded",native_encoded);traces.clear()
    expected=outcome(original_checked_json,raw,digest)
    assert actual==expected and actual_trace==traces==["decode","encoded"]


def test_custom_default_decoder_keeps_its_calls_on_every_warm_read(monkeypatch):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    calls=[]
    class Decoder(json.JSONDecoder):
        def decode(self, value):
            calls.append(value)
            return super().decode(value)
    monkeypatch.setattr(json,"_default_decoder",Decoder())
    assert journal._checked_json(raw,digest)=={"a":1}
    assert calls==[raw]


def test_recursion_limit_and_deep_callers_use_original_path():
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    saved=sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(400)
        assert not journal._canonical_witness_eligible(raw,digest)
        assert outcome(journal._checked_json,raw,digest)==outcome(original_checked_json,raw,digest)
    finally:
        sys.setrecursionlimit(saved)
    def recurse(depth, call):
        if depth: return recurse(depth-1,call)
        assert not journal._canonical_witness_eligible(raw,digest)
        return outcome(call,raw,digest)
    assert recurse(80,journal._checked_json)==recurse(80,original_checked_json)


def test_deep_or_large_native_rows_are_checked_but_not_remembered():
    deep={"x":1}
    for _ in range(20): deep={"x":deep}
    for value in (deep,{"x":"a"*70000}):
        raw,digest=payload(value)
        assert journal._checked_json(raw,digest)==value
        assert (raw,digest) not in journal._canonical_witnesses


def test_both_entry_and_utf8_byte_bounds_evict_exact_old_witnesses(monkeypatch):
    monkeypatch.setattr(journal,"_CANONICAL_WITNESS_ENTRIES",3)
    monkeypatch.setattr(journal,"_CANONICAL_WITNESS_BYTES",240)
    keys=[payload({"n":i,"text":"雪"})for i in range(8)]
    for key in keys:
        assert journal._checked_json(*key)==json.loads(key[0])
        assert len(journal._canonical_witnesses)<=3
        assert journal._canonical_witness_bytes<=240
    assert keys[0] not in journal._canonical_witnesses
    assert journal._canonical_witness_bytes==sum(journal._canonical_witnesses.values())


def test_concurrent_hits_return_independent_values_and_reject_bad_hashes():
    raw,digest=payload({"list":[1,{"x":2}]})
    journal._checked_json(raw,digest)
    def read(index):
        value=journal._checked_json(raw,digest)
        value["list"][1]["x"]=index
        bad=outcome(journal._checked_json,raw,"0"*64)
        assert bad[0]=="raised"
        return value
    with ThreadPoolExecutor(max_workers=8)as pool:values=list(pool.map(read,range(64)))
    assert [x["list"][1]["x"]for x in values]==list(range(64))
    assert journal._checked_json(raw,digest)=={"list":[1,{"x":2}]}
    assert journal._canonical_witness_bytes==sum(journal._canonical_witnesses.values())


def test_optional_witness_failure_preserves_successful_validation(monkeypatch):
    raw,digest=payload({"x":1})
    class Busy:
        def __enter__(self): raise RuntimeError("private witness unavailable")
        def __exit__(self,*args): pass
    monkeypatch.setattr(journal,"_canonical_witness_lock",Busy())
    assert journal._checked_json(raw,digest)=={"x":1}


def test_complete_native_audit_rejects_new_corruption_after_warm_hits(tmp_path):
    binding={"account_fingerprint":"a"*64,"session_id":"canonical-witness-offline",
             "participant_id":"pc-new","stream":"scout-15m"}
    ledger=journal.ScoutJournal(tmp_path/'own.sqlite3',binding=binding)
    request=Request('canonical-witness-request','a'*64,binding['session_id'],'DOCU','BUY',1,'10',
        'b'*64,'b'*64,'c'*64,'d'*64,'2026-10-27T18:00:00Z','2026-10-27T18:00:01Z',
        '2026-10-27T18:15:00Z','2026-10-27T18:15:00Z')
    ledger.prepare(request)
    assert ledger.records()==ledger.records()
    with sqlite3.connect(ledger.path)as db:
        raw=db.execute('SELECT payload FROM records').fetchone()[0]
        value=json.loads(raw);value['state']='NOT_SENT';value['revoke_reason']='changed'
        changed=journal._encoded(value)
        db.execute('UPDATE records SET payload=?,sha256=?',(changed,hashlib.sha256(changed.encode()).hexdigest()))
    with pytest.raises(journal.JournalCorruptionError,match='Current record differs'):
        ledger.records()


def test_self_restoring_decoder_cannot_use_a_prior_native_witness(monkeypatch):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    native=json.loads
    def changed(value):
        monkeypatch.setattr(json,"loads",native)
        return {"injected":object()}
    monkeypatch.setattr(json,"loads",changed)
    actual=outcome(journal._checked_json,raw,digest)
    monkeypatch.setattr(json,"loads",changed)
    expected=outcome(original_checked_json,raw,digest)
    assert actual==expected and actual[0]=="raised"


@pytest.mark.parametrize("hook",["decode","raw_decode"])
@pytest.mark.parametrize("restore",[False,True])
def test_decoder_instance_override_and_self_restore_keep_original_errors(monkeypatch,hook,restore):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    decoder=json._default_decoder
    def changed(*args,**kwargs):
        if restore: monkeypatch.delattr(decoder,hook)
        value={"injected":object()}
        return value if hook=="decode"else(value,len(raw))
    monkeypatch.setattr(decoder,hook,changed)
    actual=outcome(journal._checked_json,raw,digest)
    monkeypatch.setattr(decoder,hook,changed)
    expected=outcome(original_checked_json,raw,digest)
    assert actual==expected and actual[0]=="raised"


def test_decoder_instance_class_override_cannot_use_native_witness(monkeypatch):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    class Decoder(json.JSONDecoder):
        def decode(self,value):return {"injected":object()}
    monkeypatch.setattr(json._default_decoder,"__class__",Decoder)
    assert outcome(journal._checked_json,raw,digest)==outcome(original_checked_json,raw,digest)
    assert outcome(journal._checked_json,raw,digest)[0]=="raised"


def test_custom_decoder_installed_before_module_import_is_never_native(monkeypatch):
    import importlib.util
    calls=[]
    def hook(value):
        calls.append(value)
        return value if len(calls)==1 else {"injected":object()}
    raw,digest=payload({"a":1})
    monkeypatch.setattr(json,"_default_decoder",json.JSONDecoder(object_hook=hook))
    spec=importlib.util.spec_from_file_location("ml.intraday._private_canonical_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    assert candidate._checked_json(raw,digest)=={"a":1}
    assert not candidate._canonical_witnesses
    actual=outcome(candidate._checked_json,raw,digest)
    oracle=type(original_checked_json)(original_checked_json.__code__,
        dict(original_checked_json.__globals__,journal=candidate))
    expected=outcome(oracle,raw,digest)
    assert actual==expected and actual[0]=="raised"
    assert len(calls)==3


@pytest.mark.parametrize("kind",["function","c-scanner-callback"])
def test_preimport_scanner_callback_is_not_a_native_plain_decoder(monkeypatch,kind):
    import importlib.util
    raw,digest=payload({"a":1});calls=[]
    native=json._default_decoder.scan_once
    def changed(*args,**kwargs):
        calls.append(1)
        return native(*args,**kwargs) if len(calls)==1 else ({"injected":object()},len(raw))
    def hook(value):
        calls.append(1)
        return value if len(calls)==1 else {"injected":object()}
    scanner=(changed if kind=="function" else json.JSONDecoder(object_hook=hook).scan_once)
    monkeypatch.setattr(json._default_decoder,"scan_once",scanner)
    spec=importlib.util.spec_from_file_location("ml.intraday._private_scanner_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    assert candidate._checked_json(raw,digest)=={"a":1}
    assert not candidate._canonical_witnesses
    actual=outcome(candidate._checked_json,raw,digest)
    assert actual[0]=="raised" and actual[1][0].__name__=="JournalCorruptionError"
    assert not candidate._native_canonical_validation()


@pytest.mark.parametrize("name",["loads","dumps","hash"])
def test_preimport_wrapped_stdlib_functions_disable_witnesses(monkeypatch,name):
    import importlib.util
    raw,digest=payload({"a":1});calls=[]
    module=hashlib if name=="hash"else json
    attribute="sha256"if name=="hash"else name
    native=getattr(module,attribute)
    def wrapped(*args,**kwargs):
        calls.append(1)
        return native(*args,**kwargs)
    monkeypatch.setattr(module,attribute,wrapped)
    spec=importlib.util.spec_from_file_location("ml.intraday._private_function_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    assert candidate._checked_json(raw,digest)=={"a":1}
    assert candidate._checked_json(raw,digest)=={"a":1}
    assert not candidate._canonical_witnesses and calls
    monkeypatch.setattr(module,attribute,native)
    assert not candidate._native_canonical_validation()
