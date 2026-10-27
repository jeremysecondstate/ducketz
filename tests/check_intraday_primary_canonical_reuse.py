"""Offline exact-hunk and primary-wrapper parity checks; no provider actions.

The portable projection retains the actual changed functions and checks every
native/session call. Real validator integration is separately tested against
the complete installed application; stubs here are not that integration proof.
"""
from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime
import ast, copy, hashlib, json, math
from pathlib import Path
import re, socket, types

BEFORE = 'def _wrap_primary(candle, original_canonical):\n    """Reuse the verifier\'s exact canonical text for the first owned copy."""\n    if candle.get("population_contract") == POPULATION_CONTRACT:\n        if not verified_recovery_candle(candle):\n            raise ContractError("Invalid existing recovered population candle")\n        return _plain(candle)\n    if not verified_source_observation(candle):\n        raise ContractError("Only a verified genuine primary candle can be wrapped")\n    original = _plain(candle) if original_canonical is None else json.loads(original_canonical)\n    _session(original, original["opened_at"])\n    native_canonical = _json(original)\n    original = json.loads(native_canonical)\n    row = dict(original, native_source_id=original["source_id"], native_source_dataset=original["source_dataset"],\n               native_interval_seconds=60, observation_kind="primary_observed")\n    evidence = {"aggregation": POPULATION_CONTRACT, "kind": "primary_observed",\n        "native_primary": original, "native_primary_sha256": hashlib.sha256(native_canonical.encode()).hexdigest(),\n        "absence_policy": "original_verified_primary_observations_preserved"}\n    # Native proof encoding already normalizes the complete owned JSON tree.\n    # Preserve wrapper key order without encoding that full row again.\n    row = dict(sorted(row.items()))\n    row.update(source_id=SOURCE_ID, source_dataset=SOURCE_DATASET,\n               population_contract=POPULATION_CONTRACT, evidence_json=_json(evidence))\n    return row\n\ndef verified_recovery_candle(candle):\n    """Verify frozen source evidence; never reinterpret an older contract."""\n    try:\n        if (candle.get("population_contract") != POPULATION_CONTRACT or candle.get("source_id") != SOURCE_ID\n                or candle.get("source_dataset") != SOURCE_DATASET):\n            return False\n        if any(not isinstance(candle.get(key), (bool, np.bool_)) for key in\n               ("synthetic", "no_trade", "synthetic_open", "synthetic_close")):\n            return False\n        for key in (*PRICE_FIELDS, "observed_constituents", "synthetic_constituents", "source_empty_constituents"):\n            number = candle.get(key)\n            if isinstance(number, (bool, np.bool_)) or not isinstance(number, (int, float, np.integer, np.floating)) or not math.isfinite(float(number)):\n                return False\n        evidence = candle["evidence_json"]\n        evidence = evidence if isinstance(evidence, Mapping) else json.loads(evidence)\n        if evidence.get("aggregation") != POPULATION_CONTRACT:\n            return False\n        if evidence.get("kind") == "primary_observed":\n            original = evidence["native_primary"]\n            original_canonical = _json(original)\n            if hashlib.sha256(original_canonical.encode()).hexdigest() != evidence["native_primary_sha256"]:\n                return False\n            # Preserve the full native/session proof while reusing its canonical text.\n            expected = _wrap_primary(original, original_canonical)\n        else:\n            expected = recovery_candle(evidence["assessment"], evidence["session"])\n            if expected is None:\n                return False\n        # Compare all contract-owned fields, including exact retained evidence.\n        # Callers may add storage/evaluation identifiers without changing meaning.\n        actual = {key: candle.get(key) for key in expected}\n        # Pandas can represent an intentionally absent native clock as NaT/NaN\n        # after mixing derived and observed rows. Only these two nullable fields\n        # may normalize, and only when the frozen evidence expects absence.\n        for key in ("native_first_opened_at", "native_last_opened_at"):\n            if expected.get(key) is None and pd.isna(actual.get(key)):\n                actual[key] = None\n        if isinstance(actual.get("evidence_json"), Mapping):\n            actual["evidence_json"] = _json(actual["evidence_json"])\n        for key in ("opened_at", "available_at", "session_open", "session_close"):\n            actual[key], expected[key] = _utc(actual[key]).isoformat(), _utc(expected[key]).isoformat()\n        # Both expected constructors return canonical JSON values;\n        # only canonical clock strings are assigned above. Validate/normalize\n        # the supplied actual value, retaining the same semantic comparison.\n        return _plain(actual) == expected\n    except (KeyError, TypeError, ValueError, OverflowError, ContractError):\n        return False\n'

def main():
    root=Path(__file__).resolve().parents[1]
    stem=root/'docs/development/patches/intraday-primary-canonical-reuse'
    meta=json.loads(stem.with_suffix('.json').read_bytes())
    patch=stem.with_suffix('.patch').read_bytes()
    checks=[]
    def check(value,label):
        assert value,label
        checks.append(label)
    def deny(*a,**k):
        raise AssertionError('Primary canonical reuse checks prohibit network')
    socket.socket.connect=deny;socket.create_connection=deny
    check(hashlib.sha256(patch).hexdigest()==meta['patch_sha256'],'patch exact bytes')
    projected=BEFORE
    for hunk in re.split(r'^@@.*?@@[^\n]*\n',patch.decode(),flags=re.M)[1:]:
        old=''.join(line[1:] for line in hunk.splitlines(True)if line.startswith((' ','-')))
        new=''.join(line[1:] for line in hunk.splitlines(True)if line.startswith((' ','+')))
        check(projected.count(old)==1,'unique function hunk preimage')
        projected=projected.replace(old,new)
    expected=hashlib.sha256(projected.encode()).hexdigest()
    candidate=root/meta['path'];mode='portable function projection'
    if candidate.exists():
        raw=candidate.read_bytes();text=raw.decode().replace('\r\n','\n')
        if hashlib.sha256(raw).hexdigest()==meta['after_sha256']:
            tr=ast.parse(text)
            actual='\n\n'.join(ast.get_source_segment(text,n)for n in tr.body if isinstance(n,ast.FunctionDef)and n.name in ('_wrap_primary','_primary_wrapper','verified_recovery_candle'))+'\n'
            check(ast.dump(ast.parse(actual))==ast.dump(ast.parse(projected)),'actual complete-source changed functions')
            mode='complete exact source plus function projection'
    class NativeBool:pass
    np=types.SimpleNamespace(bool_=NativeBool,integer=int,floating=float)
    pd=types.SimpleNamespace(isna=lambda x:x is None or isinstance(x,float)and math.isnan(x))
    class ContractError(ValueError):pass
    def utc(x):
        return x if isinstance(x,datetime)else datetime.fromisoformat(x.replace('Z','+00:00'))
    def canonical(v):
        return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False,default=lambda x:utc(x).isoformat())
    def make(src):
        calls=[]
        def native(v):calls.append('native');return v.get('native_valid',True)
        def session(v,at):
            calls.append('session')
            if not v.get('session_valid',True):raise ContractError('Invalid session')
        env={'Mapping':Mapping,'json':json,'math':math,'hashlib':hashlib,'np':np,'pd':pd,'ContractError':ContractError,
             'POPULATION_CONTRACT':'fixture-recovery','SOURCE_ID':'fixture-source','SOURCE_DATASET':'fixture-dataset',
             'PRICE_FIELDS':('open','high','low','close','volume'),'_json':canonical,'_plain':lambda v:json.loads(canonical(v)),
             '_utc':utc,'_session':session,'verified_source_observation':native,'recovery_candle':lambda *a:None}
        exec(compile(src,'primary-canonical-projection','exec'),env)
        return env,calls
    old,oc=make(BEFORE);new,nc=make(projected)
    native={'source_id':'native-source','source_dataset':'native-dataset','opened_at':'2026-10-27T10:00:00+00:00',
            'available_at':'2026-10-27T10:15:00+00:00','session_open':'2026-10-27T08:00:00+00:00','session_close':'2026-10-28T00:00:00+00:00',
            'synthetic':False,'no_trade':False,'synthetic_open':False,'synthetic_close':False,
            'open':1.,'high':2.,'low':.5,'close':1.5,'volume':10.,'observed_constituents':1,'synthetic_constituents':0,'source_empty_constituents':14,
            'native_first_opened_at':'2026-10-27T10:00:00+00:00','native_last_opened_at':'2026-10-27T10:00:00+00:00','evidence_json':'full retained native proof'}
    def outcome(env,call,*a):
        try:return ('value',env[call](*a))
        except Exception as e:return('error',type(e).__name__,str(e))
    for extra in ({},{'nested':{'unicode':'Ω','list':[None,True,-0.0,1e100]}},{'extra':{2:'two',10:'ten'}},{'opened_at':datetime.fromisoformat(native['opened_at'])}):
        v={**native,**extra};a=outcome(old,'_wrap_primary',copy.deepcopy(v),None);b=outcome(new,'_wrap_primary',copy.deepcopy(v),None)
        check(a==b,'generic wrapper exact canonical parity')
    wrapped=old['_wrap_primary'](native,None)
    for mapping in (False,True):
        cases=[('valid',lambda x:None,True)]
        for key,value in [('close',900),('synthetic',True),('no_trade',True),('source_id','wrong'),('population_contract','wrong'),('observed_constituents',True),('native_interval_seconds',300),('opened_at','2026-10-27T09:00:00+00:00')]:
            cases.append((key,lambda x,k=key,v=value:x.update({k:v}),False))
        def proof_mutation(x,key,value):
            e=x['evidence_json'] if isinstance(x['evidence_json'],Mapping)else json.loads(x['evidence_json'])
            e['native_primary'][key]=value;e['native_primary_sha256']=hashlib.sha256(canonical(e['native_primary']).encode()).hexdigest()
            x['evidence_json']=e if mapping else canonical(e)
        cases.extend([('native rejected',lambda x:proof_mutation(x,'native_valid',False),False),('session rejected',lambda x:proof_mutation(x,'session_valid',False),False)])
        for label,change,expected_valid in cases:
            x=copy.deepcopy(wrapped)
            if mapping:x['evidence_json']=json.loads(x['evidence_json'])
            change(x);oc.clear();nc.clear();a=outcome(old,'verified_recovery_candle',x);b=outcome(new,'verified_recovery_candle',x)
            check(a==b,'verifier parity '+label+str(mapping));check(a==('value',expected_valid),'explicit validity '+label+str(mapping));check(oc==nc,'native/session proof calls '+label+str(mapping))
    print(json.dumps({'checks_passed':len(checks),'source_validation':mode,'function_projection_sha256':expected,'network_attempts':0,'limitation':'Portable proof stubs verify call preservation; actual native/session integration is separate.'}))
    return 0

if __name__=='__main__':raise SystemExit(main())
