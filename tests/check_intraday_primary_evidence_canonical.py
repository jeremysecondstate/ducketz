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

BEFORE = 'def _wrap_primary(candle, original_canonical):\n    """Reuse the verifier\'s exact canonical text for the first owned copy."""\n    if candle.get("population_contract") == POPULATION_CONTRACT:\n        if not verified_recovery_candle(candle):\n            raise ContractError("Invalid existing recovered population candle")\n        return _plain(candle)\n    if not verified_source_observation(candle):\n        raise ContractError("Only a verified genuine primary candle can be wrapped")\n    original = _plain(candle) if original_canonical is None else json.loads(original_canonical)\n    _session(original, original["opened_at"])\n    native_canonical = _json(original)\n    original = json.loads(native_canonical)\n    return _primary_wrapper(original, native_canonical)\n\n\ndef _primary_wrapper(original, native_canonical):\n    """Build the same wrapper from an owned, normalized native JSON tree."""\n    row = dict(original, native_source_id=original["source_id"], native_source_dataset=original["source_dataset"],\n               native_interval_seconds=60, observation_kind="primary_observed")\n    evidence = {"aggregation": POPULATION_CONTRACT, "kind": "primary_observed",\n        "native_primary": original, "native_primary_sha256": hashlib.sha256(native_canonical.encode()).hexdigest(),\n        "absence_policy": "original_verified_primary_observations_preserved"}\n    # Native proof encoding already normalizes the complete owned JSON tree.\n    # Preserve wrapper key order without encoding that full row again.\n    row = dict(sorted(row.items()))\n    row.update(source_id=SOURCE_ID, source_dataset=SOURCE_DATASET,\n               population_contract=POPULATION_CONTRACT, evidence_json=_json(evidence))\n    return row\n\n\ndef _assessment_valid(assessment):\n    if not isinstance(assessment, Mapping) or assessment.get("policy") != POLICY:\n        return False\n    body = {key: value for key, value in assessment.items() if key != "assessment_sha256"}\n    if _digest(body) != assessment.get("assessment_sha256"):\n        return False\n    start, end, decision = map(_utc, (assessment["start"], assessment["end"], assessment["as_of"]))\n    return bool(assessment.get("symbol")) and start == start.floor("15min") and end == start+STEP and end <= decision\n\n\ndef verified_recovery_candle(candle):\n    """Verify frozen source evidence; never reinterpret an older contract."""\n    try:\n        if (candle.get("population_contract") != POPULATION_CONTRACT or candle.get("source_id") != SOURCE_ID\n                or candle.get("source_dataset") != SOURCE_DATASET):\n            return False\n        if any(not isinstance(candle.get(key), (bool, np.bool_)) for key in\n               ("synthetic", "no_trade", "synthetic_open", "synthetic_close")):\n            return False\n        for key in (*PRICE_FIELDS, "observed_constituents", "synthetic_constituents", "source_empty_constituents"):\n            number = candle.get(key)\n            if isinstance(number, (bool, np.bool_)) or not isinstance(number, (int, float, np.integer, np.floating)) or not math.isfinite(float(number)):\n                return False\n        evidence = candle["evidence_json"]\n        decoded_evidence = not isinstance(evidence, Mapping)\n        evidence = json.loads(evidence) if decoded_evidence else evidence\n        if evidence.get("aggregation") != POPULATION_CONTRACT:\n            return False\n        if evidence.get("kind") == "primary_observed":\n            original = evidence["native_primary"]\n            original_canonical = _json(original)\n            if hashlib.sha256(original_canonical.encode()).hexdigest() != evidence["native_primary_sha256"]:\n                return False\n            # A freshly decoded proof already owns a plain JSON tree. Retain\n            # every native/session check while avoiding a second JSON roundtrip.\n            # Mapping evidence keeps the generic normalization path, including\n            # its handling of custom mappings and non-JSON Python values.\n            if decoded_evidence:\n                if not verified_source_observation(original):\n                    raise ContractError("Only a verified genuine primary candle can be wrapped")\n                _session(original, original["opened_at"])\n                expected = _primary_wrapper(original, original_canonical)\n            else:\n                expected = _wrap_primary(original, original_canonical)\n        else:\n            expected = recovery_candle(evidence["assessment"], evidence["session"])\n            if expected is None:\n                return False\n        # Compare all contract-owned fields, including exact retained evidence.\n        # Callers may add storage/evaluation identifiers without changing meaning.\n        actual = {key: candle.get(key) for key in expected}\n        # Pandas can represent an intentionally absent native clock as NaT/NaN\n        # after mixing derived and observed rows. Only these two nullable fields\n        # may normalize, and only when the frozen evidence expects absence.\n        for key in ("native_first_opened_at", "native_last_opened_at"):\n            if expected.get(key) is None and pd.isna(actual.get(key)):\n                actual[key] = None\n        if isinstance(actual.get("evidence_json"), Mapping):\n            actual["evidence_json"] = _json(actual["evidence_json"])\n        for key in ("opened_at", "available_at", "session_open", "session_close"):\n            actual[key], expected[key] = _utc(actual[key]).isoformat(), _utc(expected[key]).isoformat()\n        # Both expected constructors return canonical JSON values;\n        # only canonical clock strings are assigned above. Validate/normalize\n        # the supplied actual value, retaining the same semantic comparison.\n        return _plain(actual) == expected\n    except (KeyError, TypeError, ValueError, OverflowError, ContractError):\n        return False\n'

def main():
    root=Path(__file__).resolve().parents[1]
    stem=root/'docs/development/patches/intraday-primary-evidence-canonical'
    meta=json.loads(stem.with_suffix('.json').read_bytes())
    patch=stem.with_suffix('.patch').read_bytes()
    checks=[]
    def check(value,label):
        assert value,label
        checks.append(label)
    def deny(*a,**k):
        raise AssertionError('Primary canonical reuse checks prohibit network')
    socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny
    check(hashlib.sha256(patch).hexdigest()==meta['patch_sha256'],'patch exact bytes')
    check(hashlib.sha256(BEFORE.encode()).hexdigest()==meta['before_function_projection_sha256'],'before function projection exact bytes')
    projected=BEFORE
    for hunk in re.split(r'^@@.*?@@[^\n]*\n',patch.decode(),flags=re.M)[1:]:
        old=''.join(line[1:] for line in hunk.splitlines(True)if line.startswith((' ','-')))
        new=''.join(line[1:] for line in hunk.splitlines(True)if line.startswith((' ','+')))
        check(projected.count(old)==1,'unique function hunk preimage')
        projected=projected.replace(old,new)
    expected=hashlib.sha256(projected.encode()).hexdigest()
    check(expected==meta['after_function_projection_sha256'],'after function projection exact bytes')
    oldnodes={n.name:ast.dump(n) for n in ast.parse(BEFORE).body if isinstance(n,ast.FunctionDef)}
    newnodes={n.name:ast.dump(n) for n in ast.parse(projected).body if isinstance(n,ast.FunctionDef)}
    check(oldnodes['_primary_wrapper']==newnodes['_primary_wrapper'],'generic constructor AST unchanged')
    check(oldnodes['_wrap_primary']==newnodes['_wrap_primary'],'generic normalization AST unchanged')
    check('_decoded_primary_wrapper' in newnodes,'decoded helper present')
    candidate=root/meta['path'];mode='portable function projection'
    if candidate.exists():
        raw=candidate.read_bytes();text=raw.decode().replace('\r\n','\n')
        if hashlib.sha256(raw).hexdigest()==meta['after_sha256']:
            tr=ast.parse(text)
            actual='\n\n\n'.join(ast.get_source_segment(text,n)for n in tr.body if isinstance(n,ast.FunctionDef)and n.name in ('_wrap_primary', '_primary_wrapper', '_decoded_primary_wrapper', '_assessment_valid', 'verified_recovery_candle'))+'\n'
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
    for args in [({},None),({'source_id':'x'},'x'),(native,'inconsistent canonical')]:
        check(outcome(old,'_primary_wrapper',*copy.deepcopy(args))==outcome(new,'_primary_wrapper',*copy.deepcopy(args)),
              'generic constructor exception and inconsistent canonical parity')
    for key in (1,False,1.5):
        v=copy.deepcopy(native);v['extra']={key:'value'}
        a=outcome(old,'_wrap_primary',copy.deepcopy(v),None);b=outcome(new,'_wrap_primary',copy.deepcopy(v),None)
        check(a==b,'raw numeric/boolean key generic normalization')
        check(old['verified_recovery_candle'](a[1])==new['verified_recovery_candle'](b[1])==True,'normalized raw-key wrapper verifies')
    for contract in ('escaped"contract\nΩ','fixture-recovery'):
        old['POPULATION_CONTRACT']=new['POPULATION_CONTRACT']=contract
        a=old['_wrap_primary'](copy.deepcopy(native),None);b=new['_wrap_primary'](copy.deepcopy(native),None)
        check(a==b,'dynamic contract canonical wrapper parity')
        check(old['verified_recovery_candle'](a)==new['verified_recovery_candle'](b)==True,'dynamic contract verifies')
    print(json.dumps({'checks_passed':len(checks),'source_validation':mode,'function_projection_sha256':expected,'network_attempts':0,'limitation':'Portable proof stubs verify call preservation; actual native/session integration is separate.'}))
    return 0

if __name__=='__main__':raise SystemExit(main())
