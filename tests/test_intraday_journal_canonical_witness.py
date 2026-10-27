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
    classes = [(kind, {key: vars(kind).get(key) for key in
                ("__new__", "__getattribute__", "__getattr__")})
               for kind in (json.JSONDecoder, json.JSONEncoder)]
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
    for kind, attributes in classes:
        for key, value in attributes.items():
            if value is None:
                if key in vars(kind): delattr(kind, key)
            else:
                setattr(kind, key, value)


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


@pytest.mark.parametrize("hook",["normalizer","canonical_bytes","finite","encoder_init",
    "encoder_encode","encoder_iterencode","c_make_encoder","encode_basestring"])
def test_preimport_skipped_validator_callbacks_keep_cold_and_warm_error_chains(monkeypatch,hook):
    import importlib.util
    protocol=journal._scout_protocol
    raw,digest=payload({"n":1.25})
    targets={"normalizer":(protocol,"_json_value"),"canonical_bytes":(protocol,"_canonical_bytes"),
        "finite":(protocol.math,"isfinite"),"encoder_init":(json.JSONEncoder,"__init__"),
        "encoder_encode":(json.JSONEncoder,"encode"),"encoder_iterencode":(json.JSONEncoder,"iterencode"),
        "c_make_encoder":(json.encoder,"c_make_encoder"),"encode_basestring":(json.encoder,"encode_basestring")}
    module,name=targets[hook];native=getattr(module,name);fail=[False];calls=[]
    def wrapped(*args,**kwargs):
        calls.append(1)
        if fail[0]:raise ValueError("pre-import callback became invalid")
        return native(*args,**kwargs)
    monkeypatch.setattr(module,name,wrapped)
    spec=importlib.util.spec_from_file_location("ml.intraday._private_skipped_validator_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    oracle=type(original_checked_json)(original_checked_json.__code__,
        dict(original_checked_json.__globals__,journal=candidate))
    assert outcome(candidate._checked_json,raw,digest)==outcome(oracle,raw,digest)
    assert not candidate._canonical_witnesses and calls
    fail[0]=True
    actual=outcome(candidate._checked_json,raw,digest);expected=outcome(oracle,raw,digest)
    assert actual==expected and actual[0]=="raised"
    assert not candidate._native_canonical_validation()


@pytest.mark.parametrize("hook",["default","_make_iterencode","encode_basestring_ascii"])
def test_preimport_optional_encoder_callbacks_disable_cache_even_on_unused_branch(monkeypatch,hook):
    import importlib.util
    raw,digest=payload({"n":1.25})
    module=json.JSONEncoder if hook=="default"else json.encoder
    native=getattr(module,hook)
    def wrapped(*args,**kwargs):return native(*args,**kwargs)
    monkeypatch.setattr(module,hook,wrapped)
    spec=importlib.util.spec_from_file_location("ml.intraday._private_optional_encoder_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    assert candidate._checked_json(raw,digest)==candidate._checked_json(raw,digest)=={"n":1.25}
    assert not candidate._canonical_witnesses and not candidate._native_canonical_validation()


@pytest.mark.parametrize("hook",["decode","raw_decode"])
@pytest.mark.parametrize("restore",[False,True])
def test_preimport_decoder_class_method_wrapper_keeps_warm_validation(monkeypatch,hook,restore):
    import importlib.util
    raw,digest=payload({"a":1});native=getattr(json.JSONDecoder,hook);fail=[False]
    def wrapped(self,*args,**kwargs):
        if fail[0]:
            if restore:monkeypatch.setattr(json.JSONDecoder,hook,native)
            value={"injected":object()}
            return value if hook=="decode"else(value,len(raw))
        return native(self,*args,**kwargs)
    monkeypatch.setattr(json.JSONDecoder,hook,wrapped)
    spec=importlib.util.spec_from_file_location("ml.intraday._private_decoder_method_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    oracle=type(original_checked_json)(original_checked_json.__code__,
        dict(original_checked_json.__globals__,journal=candidate))
    assert outcome(candidate._checked_json,raw,digest)==outcome(oracle,raw,digest)
    assert not candidate._canonical_witnesses
    fail[0]=True
    actual=outcome(candidate._checked_json,raw,digest)
    monkeypatch.setattr(json.JSONDecoder,hook,wrapped)
    expected=outcome(oracle,raw,digest)
    assert actual==expected and actual[0]=="raised"
    assert not candidate._native_canonical_validation()


def test_slots_only_preimport_default_decoder_preserves_original_support(monkeypatch):
    import importlib.util
    raw,digest=payload({"a":1});native=json._default_decoder;fail=[False]
    class Decoder:
        __slots__=()
        def decode(self,value):
            return {"injected":object()} if fail[0] else native.decode(value)
    monkeypatch.setattr(json,"_default_decoder",Decoder())
    spec=importlib.util.spec_from_file_location("ml.intraday._private_slots_decoder_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    oracle=type(original_checked_json)(original_checked_json.__code__,
        dict(original_checked_json.__globals__,journal=candidate))
    assert outcome(candidate._checked_json,raw,digest)==outcome(oracle,raw,digest)
    assert not candidate._canonical_witnesses and not candidate._native_canonical_validation()
    fail[0]=True
    assert outcome(candidate._checked_json,raw,digest)==outcome(oracle,raw,digest)


@pytest.mark.parametrize("hook",["__new__","__getattribute__"])
def test_preimport_encoder_construction_and_attribute_callbacks_are_not_native(monkeypatch,hook):
    if hook == "__new__":
        _encoder_new_in_child("preimport")
        return
    import importlib.util
    raw,digest=payload({"a":1});native=getattr(json.JSONEncoder,hook);fail=[False]
    def wrapped(first,*args,**kwargs):
        if fail[0]:raise ValueError("pre-import encoder construction changed")
        return native(first) if hook=="__new__"else native(first,*args,**kwargs)
    monkeypatch.setattr(json.JSONEncoder,hook,staticmethod(wrapped) if hook=="__new__"else wrapped)
    spec=importlib.util.spec_from_file_location("ml.intraday._private_encoder_construction_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    oracle=type(original_checked_json)(original_checked_json.__code__,
        dict(original_checked_json.__globals__,journal=candidate))
    assert outcome(candidate._checked_json,raw,digest)==outcome(oracle,raw,digest)
    assert not candidate._canonical_witnesses
    fail[0]=True
    actual=outcome(candidate._checked_json,raw,digest);expected=outcome(oracle,raw,digest)
    assert actual==expected and actual[0]=="raised"


def test_unverifiable_preimport_encoder_factory_does_not_break_module_import(monkeypatch):
    import importlib.util
    raw,digest=payload({"a":1});native=json.JSONEncoder
    class Factory:
        __slots__=()
        def __call__(self,*args,**kwargs):return native(*args,**kwargs)
    monkeypatch.setattr(json,"JSONEncoder",Factory())
    spec=importlib.util.spec_from_file_location("ml.intraday._private_encoder_factory_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    assert candidate._checked_json(raw,digest)=={"a":1}
    assert not candidate._canonical_witnesses and not candidate._native_canonical_validation()


@pytest.mark.parametrize("hook",["__new__","__getattribute__"])
def test_later_encoder_dunder_callback_cannot_use_a_warm_native_witness(monkeypatch,hook):
    if hook == "__new__":
        _encoder_new_in_child("later")
        return
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    def fail(*args,**kwargs):raise ValueError("later encoder callback")
    monkeypatch.setattr(json.JSONEncoder,hook,staticmethod(fail) if hook=="__new__"else fail)
    actual=outcome(journal._checked_json,raw,digest);expected=outcome(original_checked_json,raw,digest)
    assert actual==expected and actual[0]=="raised"


def _encoder_new_in_child(mode):
    # CPython retains a changed type slot even after __new__ is deleted. Keep
    # these legitimate override cases out of pytest's own global JSON encoder.
    import subprocess
    from pathlib import Path
    code=r"""
import hashlib,importlib.util,json,sys
from ml.intraday import scout_journal as journal
def original(payload,digest):
    try:
        value=json.loads(payload);encoded=journal._encoded(value)
        if encoded!=payload or hashlib.sha256(encoded.encode()).hexdigest()!=digest:
            raise journal.JournalCorruptionError('Stored payload hash or canonical bytes differ')
        return value
    except(ValueError,TypeError,UnicodeError,RecursionError)as error:
        raise journal.JournalCorruptionError('Invalid stored canonical payload')from error
def chain(error):return(type(error),str(error),chain(error.__cause__)if error.__cause__ else None)
def outcome(call,raw,digest):
    try:return('returned',call(raw,digest))
    except Exception as error:return('raised',chain(error))
raw=journal._encoded({'a':1});digest=hashlib.sha256(raw.encode()).hexdigest()
mode=sys.argv[1];native=json.JSONEncoder.__new__;fail=[False]
def wrapped(cls,*args,**kwargs):
    if fail[0]:raise ValueError('encoder new callback changed')
    return native(cls)
if mode=='preimport':
    json.JSONEncoder.__new__=staticmethod(wrapped)
    spec=importlib.util.spec_from_file_location('ml.intraday._new_probe',journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    original.__globals__['journal']=candidate
    assert outcome(candidate._checked_json,raw,digest)==outcome(original,raw,digest)
    assert not candidate._canonical_witnesses
    fail[0]=True
else:
    candidate=journal
    assert candidate._checked_json(raw,digest)=={'a':1}
    json.JSONEncoder.__new__=staticmethod(wrapped)
    if mode=='restored':
        json.JSONEncoder.__new__=native
        del json.JSONEncoder.__new__
    else:fail[0]=True
actual=outcome(candidate._checked_json,raw,digest);expected=outcome(original,raw,digest)
assert actual==expected and actual[0]=='raised',(actual,expected)
print('PASS')
"""
    result=subprocess.run([sys.executable,"-B","-c",code,mode],
        cwd=Path(journal.__file__).resolve().parents[2],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert result.stdout.strip()=="PASS"


def test_restored_hidden_encoder_constructor_slot_uses_original_error_path():
    _encoder_new_in_child("restored")


def test_later_decoder_attribute_hook_gets_only_original_business_reads(monkeypatch):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    native=json.JSONDecoder.__getattribute__;trace=[];metadata=[False]
    def attributes(self,name):
        trace.append(name)
        if name=="__dict__":metadata[0]=True
        if name=="scan_once"and metadata[0]:raise ValueError("extra observer metadata read")
        return native(self,name)
    monkeypatch.setattr(json.JSONDecoder,"__getattribute__",attributes)
    actual=outcome(journal._checked_json,raw,digest);actual_trace=list(trace)
    trace.clear();metadata[0]=False
    expected=outcome(original_checked_json,raw,digest)
    assert actual==expected==("returned",{"a":1})
    assert actual_trace==trace==["decode","raw_decode","scan_once"]


def test_decoder_metadata_dict_subclass_never_receives_optional_guard_callbacks(monkeypatch):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest);calls=[]
    class Metadata(dict):
        def get(self,*args,**kwargs):
            calls.append(args)
            raise ValueError("optional metadata callback")
    monkeypatch.setattr(json._default_decoder,"__dict__",Metadata(vars(json._default_decoder)))
    assert outcome(journal._checked_json,raw,digest)==outcome(original_checked_json,raw,digest)==("returned",{"a":1})
    assert not calls


@pytest.mark.parametrize("name",["_getframe","getrecursionlimit","is_"])
def test_preimport_guard_intrinsic_wrappers_are_never_called(monkeypatch,name):
    import importlib.util,operator
    raw,digest=payload({"a":1});module=operator if name=="is_"else sys
    native=getattr(module,name);calls=[]
    def wrapped(*args,**kwargs):
        calls.append(name)
        return native(*args,**kwargs)
    monkeypatch.setattr(module,name,wrapped)
    spec=importlib.util.spec_from_file_location("ml.intraday._private_guard_intrinsic_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    assert candidate._checked_json(raw,digest)=={"a":1}
    assert not candidate._canonical_witnesses and not candidate._native_canonical_validation()
    assert calls==[]


def test_later_identity_guard_alias_wrapper_is_not_called(monkeypatch):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest);calls=[]
    def wrapped(*args):
        calls.append(args)
        raise ValueError("guard introduced callback")
    monkeypatch.setattr(journal,"_same_identity",wrapped)
    assert journal._checked_json(raw,digest)=={"a":1}
    assert not calls


@pytest.mark.parametrize("mode",["preimport","later"])
@pytest.mark.parametrize("mechanism",["setter","descriptor"])
def test_codec_constructor_first_failure_is_never_consumed_by_a_probe(mode,mechanism):
    import subprocess
    from pathlib import Path
    code=r"""
import hashlib,importlib.util,json,sys
from ml.intraday import scout_journal as journal
def original(payload,digest):
    try:
        value=json.loads(payload);encoded=journal._encoded(value)
        if encoded!=payload or hashlib.sha256(encoded.encode()).hexdigest()!=digest:
            raise journal.JournalCorruptionError('Stored payload hash or canonical bytes differ')
        return value
    except(ValueError,TypeError,UnicodeError,RecursionError)as error:
        raise journal.JournalCorruptionError('Invalid stored canonical payload')from error
def chain(error):return(type(error),str(error),chain(error.__cause__)if error.__cause__ else None)
def outcome(call,raw,digest):
    try:return('returned',call(raw,digest))
    except Exception as error:return('raised',chain(error))
raw=journal._encoded({'a':1});digest=hashlib.sha256(raw.encode()).hexdigest();calls=[]
mode,mechanism=sys.argv[1:]
if mode=='later':assert journal._checked_json(raw,digest)=={'a':1}
native=json.JSONEncoder.__setattr__
def setter(self,name,value):
    calls.append(name)
    if len(calls)==1:raise ValueError('first constructor attribute failure')
    return native(self,name,value)
def property_setter(self,value):setter(self,'ensure_ascii',value)
if mechanism=='setter':json.JSONEncoder.__setattr__=setter
else:json.JSONEncoder.ensure_ascii=property(lambda self:False,property_setter)
if mode=='preimport':
    spec=importlib.util.spec_from_file_location('ml.intraday._first_setter_probe',journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    original.__globals__['journal']=candidate
else:candidate=journal
calls.clear();actual=outcome(candidate._checked_json,raw,digest);actual_trace=list(calls)
calls.clear();expected=outcome(original,raw,digest);expected_trace=list(calls)
assert actual==expected and actual[0]=='raised',(actual,expected)
assert actual_trace==expected_trace and len(actual_trace)==1,(actual_trace,expected_trace)
print('PASS')
"""
    result=subprocess.run([sys.executable,"-B","-c",code,mode,mechanism],
        cwd=Path(journal.__file__).resolve().parents[2],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert result.stdout.strip()=="PASS"


def test_added_codec_field_or_changed_default_always_disables_a_witness(monkeypatch):
    raw,digest=payload({"a":1});journal._checked_json(raw,digest)
    monkeypatch.setattr(json.JSONEncoder,"unrelated_added_field",1,raising=False)
    assert not journal._native_canonical_validation()
    assert journal._checked_json(raw,digest)=={"a":1}


def test_initial_unknown_codec_variant_uses_original_processing(monkeypatch):
    import importlib.util
    raw,digest=payload({"a":1})
    monkeypatch.setattr(json.JSONEncoder,"unrelated_added_field",1,raising=False)
    spec=importlib.util.spec_from_file_location("ml.intraday._unknown_codec_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    assert candidate._checked_json(raw,digest)=={"a":1}
    assert not candidate._canonical_witnesses and not candidate._native_canonical_validation()


@pytest.mark.parametrize("timing",["preimport","later"])
@pytest.mark.parametrize("dependency",["json","math"])
@pytest.mark.parametrize("kind",["namespace","proxy"])
def test_protocol_module_aliases_keep_original_callbacks_and_error_chains(monkeypatch,timing,dependency,kind):
    import importlib.util,types,math
    raw,digest=payload({"n":1.25});protocol=journal._scout_protocol
    name="dumps"if dependency=="json"else"isfinite"
    native=getattr(json if dependency=="json"else math,name);trace=[];fail=[False]
    def wrapped(*args,**kwargs):
        trace.append("call:"+name)
        if fail[0]:raise ValueError("configured protocol module callback failed")
        return native(*args,**kwargs)
    class Proxy:
        def __getattribute__(self,field):
            if field!=name:raise AttributeError(field)
            trace.append("get:"+field)
            return wrapped
    replacement=types.SimpleNamespace(**{name:wrapped}) if kind=="namespace"else Proxy()
    if timing=="later":journal._checked_json(raw,digest)
    monkeypatch.setattr(protocol,dependency,replacement)
    if timing=="preimport":
        spec=importlib.util.spec_from_file_location("ml.intraday._protocol_module_alias_probe",journal.__file__)
        candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    else:candidate=journal
    oracle=type(original_checked_json)(original_checked_json.__code__,
        dict(original_checked_json.__globals__,journal=candidate))
    trace.clear();actual=outcome(candidate._checked_json,raw,digest);actual_trace=list(trace)
    trace.clear();expected=outcome(oracle,raw,digest)
    assert actual==expected==("returned",{"n":1.25}) and actual_trace==trace
    assert not candidate._native_canonical_validation()
    fail[0]=True
    trace.clear();actual=outcome(candidate._checked_json,raw,digest);actual_trace=list(trace)
    trace.clear();expected=outcome(oracle,raw,digest)
    assert actual==expected and actual[0]=="raised" and actual_trace==trace


@pytest.mark.parametrize("name",["json","hashlib","sys","math","protocol","encoder","decoder","scanner"])
def test_preimport_module_proxy_adds_no_guard_metadata_calls(monkeypatch,name):
    import importlib.util,types,math
    raw,digest=payload({"a":1});trace=[]
    modules={"json":json,"hashlib":hashlib,"sys":sys,"math":math,"protocol":journal._scout_protocol,
             "encoder":json.encoder,"decoder":json.decoder,"scanner":json.scanner}
    native=modules[name]
    watched={"loads","dumps","sha256","isfinite","getrecursionlimit","_getframe",
             "JSONEncoder","JSONDecoder","c_make_scanner","c_make_encoder"}
    class Proxy(types.ModuleType):
        def __getattribute__(self,field):
            if field in watched:trace.append(field)
            return getattr(native,field)
    proxy=Proxy(native.__name__)
    if name in {"encoder","decoder","scanner"}:monkeypatch.setattr(json,name,proxy)
    else:monkeypatch.setitem(sys.modules,native.__name__,proxy)
    if name=="protocol":
        from ml import intraday
        monkeypatch.setattr(intraday,"scout_protocol",proxy)
    spec=importlib.util.spec_from_file_location("ml.intraday._pure_module_proxy_probe",journal.__file__)
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
    oracle=type(original_checked_json)(original_checked_json.__code__,
        dict(original_checked_json.__globals__,journal=candidate,
             json=candidate.json,hashlib=candidate.hashlib))
    trace.clear();actual=outcome(candidate._checked_json,raw,digest);actual_trace=list(trace)
    trace.clear();expected=outcome(oracle,raw,digest)
    assert actual==expected==("returned",{"a":1}) and actual_trace==trace
    assert not candidate._canonical_witnesses and not candidate._native_canonical_validation()


@pytest.mark.parametrize("name",["json","hashlib","sys","math","protocol","encoder","decoder","scanner"])
def test_later_pep562_module_hook_is_not_called_by_optional_guard(monkeypatch,name):
    import math
    raw,digest=payload({"a":1});journal._checked_json(raw,digest);calls=[]
    modules={"json":json,"hashlib":hashlib,"sys":sys,"math":math,"protocol":journal._scout_protocol,
             "encoder":json.encoder,"decoder":json.decoder,"scanner":json.scanner}
    def hook(field):
        calls.append(field)
        raise AttributeError(field)
    monkeypatch.setattr(modules[name],"__getattr__",hook,raising=False)
    assert journal._checked_json(raw,digest)=={"a":1}
    assert not journal._native_canonical_validation() and not calls
