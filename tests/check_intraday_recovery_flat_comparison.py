"""Standalone offline exact-hunk and normalization parity checks.

The changed functions and unchanged wrapper constructors are extracted exactly.
Source/session validators and clock adapters are explicit fixture stubs; full
installed source/dependency integration is separately tested. No provider calls.
"""
from collections.abc import Mapping
from collections import UserDict
from datetime import datetime
from pathlib import Path
import ast,copy,hashlib,json,math,re,socket,types

BEFORE = '\ndef _plain(value):\n    return json.loads(_json(value))\n\n\ndef _digest(value):\n    return hashlib.sha256(_json(value).encode()).hexdigest()\n\ndef _wrap_primary(candle, original_canonical):\n    """Reuse the verifier\'s exact canonical text for the first owned copy."""\n    if candle.get("population_contract") == POPULATION_CONTRACT:\n        if not verified_recovery_candle(candle):\n            raise ContractError("Invalid existing recovered population candle")\n        return _plain(candle)\n    if not verified_source_observation(candle):\n        raise ContractError("Only a verified genuine primary candle can be wrapped")\n    original = _plain(candle) if original_canonical is None else json.loads(original_canonical)\n    _session(original, original["opened_at"])\n    native_canonical = _json(original)\n    original = json.loads(native_canonical)\n    return _primary_wrapper(original, native_canonical)\n\ndef _primary_wrapper(original, native_canonical):\n    """Build the same wrapper from an owned, normalized native JSON tree."""\n    row = dict(original, native_source_id=original["source_id"], native_source_dataset=original["source_dataset"],\n               native_interval_seconds=60, observation_kind="primary_observed")\n    evidence = {"aggregation": POPULATION_CONTRACT, "kind": "primary_observed",\n        "native_primary": original, "native_primary_sha256": hashlib.sha256(native_canonical.encode()).hexdigest(),\n        "absence_policy": "original_verified_primary_observations_preserved"}\n    # Native proof encoding already normalizes the complete owned JSON tree.\n    # Preserve wrapper key order without encoding that full row again.\n    row = dict(sorted(row.items()))\n    row.update(source_id=SOURCE_ID, source_dataset=SOURCE_DATASET,\n               population_contract=POPULATION_CONTRACT, evidence_json=_json(evidence))\n    return row\n\ndef _decoded_primary_wrapper(original, native_canonical):\n    """Reuse this call\'s freshly verified canonical native text once more."""\n    row = dict(original, native_source_id=original["source_id"], native_source_dataset=original["source_dataset"],\n               native_interval_seconds=60, observation_kind="primary_observed")\n    # The verifier computed this exact canonical text from a freshly decoded\n    # owned JSON tree immediately before its hash, native and session checks.\n    # Keep the generic constructor\'s independently supplied text semantics.\n    evidence_json = (\'{"absence_policy":"original_verified_primary_observations_preserved",\'\n        \'"aggregation":\' + _json(POPULATION_CONTRACT) + \',"kind":"primary_observed",\'\n        \'"native_primary":\' + native_canonical + \',"native_primary_sha256":"\' +\n        hashlib.sha256(native_canonical.encode()).hexdigest() + \'"}\')\n    row = dict(sorted(row.items()))\n    row.update(source_id=SOURCE_ID, source_dataset=SOURCE_DATASET,\n               population_contract=POPULATION_CONTRACT, evidence_json=evidence_json)\n    return row\n\ndef verified_recovery_candle(candle):\n    """Verify frozen source evidence; never reinterpret an older contract."""\n    try:\n        if (candle.get("population_contract") != POPULATION_CONTRACT or candle.get("source_id") != SOURCE_ID\n                or candle.get("source_dataset") != SOURCE_DATASET):\n            return False\n        if any(not isinstance(candle.get(key), (bool, np.bool_)) for key in\n               ("synthetic", "no_trade", "synthetic_open", "synthetic_close")):\n            return False\n        for key in (*PRICE_FIELDS, "observed_constituents", "synthetic_constituents", "source_empty_constituents"):\n            number = candle.get(key)\n            if isinstance(number, (bool, np.bool_)) or not isinstance(number, (int, float, np.integer, np.floating)) or not math.isfinite(float(number)):\n                return False\n        evidence = candle["evidence_json"]\n        decoded_evidence = not isinstance(evidence, Mapping)\n        evidence = json.loads(evidence) if decoded_evidence else evidence\n        if evidence.get("aggregation") != POPULATION_CONTRACT:\n            return False\n        if evidence.get("kind") == "primary_observed":\n            original = evidence["native_primary"]\n            original_canonical = _json(original)\n            if hashlib.sha256(original_canonical.encode()).hexdigest() != evidence["native_primary_sha256"]:\n                return False\n            # A freshly decoded proof already owns a plain JSON tree. Retain\n            # every native/session check while avoiding a second JSON roundtrip.\n            # Mapping evidence keeps the generic normalization path, including\n            # its handling of custom mappings and non-JSON Python values.\n            if decoded_evidence:\n                if not verified_source_observation(original):\n                    raise ContractError("Only a verified genuine primary candle can be wrapped")\n                _session(original, original["opened_at"])\n                expected = _decoded_primary_wrapper(original, original_canonical)\n            else:\n                expected = _wrap_primary(original, original_canonical)\n        else:\n            expected = recovery_candle(evidence["assessment"], evidence["session"])\n            if expected is None:\n                return False\n        # Compare all contract-owned fields, including exact retained evidence.\n        # Callers may add storage/evaluation identifiers without changing meaning.\n        actual = {key: candle.get(key) for key in expected}\n        # Pandas can represent an intentionally absent native clock as NaT/NaN\n        # after mixing derived and observed rows. Only these two nullable fields\n        # may normalize, and only when the frozen evidence expects absence.\n        for key in ("native_first_opened_at", "native_last_opened_at"):\n            if expected.get(key) is None and pd.isna(actual.get(key)):\n                actual[key] = None\n        if isinstance(actual.get("evidence_json"), Mapping):\n            actual["evidence_json"] = _json(actual["evidence_json"])\n        for key in ("opened_at", "available_at", "session_open", "session_close"):\n            actual[key], expected[key] = _utc(actual[key]).isoformat(), _utc(expected[key]).isoformat()\n        # Both expected constructors return canonical JSON values;\n        # only canonical clock strings are assigned above. Validate/normalize\n        # the supplied actual value, retaining the same semantic comparison.\n        return _plain(actual) == expected\n    except (KeyError, TypeError, ValueError, OverflowError, ContractError):\n        return False\n\n'

def main():
 root=Path(__file__).resolve().parents[1]
 stem=root/'docs/development/patches/intraday-recovery-flat-comparison'
 meta=json.loads(stem.with_suffix('.json').read_bytes());patch=stem.with_suffix('.patch').read_bytes()
 checks=[]
 def check(value,name):
  assert value,name
  checks.append(name)
 def deny(*a,**k):raise AssertionError('Portable comparison checks prohibit network')
 socket.socket.connect=deny;socket.create_connection=deny
 digest=lambda b:hashlib.sha256(b).hexdigest()
 check(digest(patch)==meta['patch_sha256'],'patch exact bytes')
 check(digest(BEFORE.encode())==meta['before_projection_sha256'],'exact embedded preimage')
 projected=BEFORE
 for hunk in re.split(r'^@@.*?@@[^\n]*\n',patch.decode(),flags=re.M)[1:]:
  old=''.join(x[1:]for x in hunk.splitlines(True)if x.startswith((' ','-')))
  new=''.join(x[1:]for x in hunk.splitlines(True)if x.startswith((' ','+')))
  check(projected.count(old)==1,'unique exact hunk preimage')
  projected=projected.replace(old,new,1)
 check(digest(projected.encode())==meta['after_projection_sha256'],'exact reconstructed function projection')
 mode='portable exact function projection'
 candidate=root/meta['path']
 if candidate.exists() and digest(candidate.read_bytes())==meta['after_sha256']:
  text=candidate.read_text(encoding='utf8');tree=ast.parse(text);nodes={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
  first=text[text.index('def _plain(value):'):text.index('def _session(session, opened_at):')].rstrip()
  actual='\n'+first+'\n\n'+'\n\n'.join(ast.get_source_segment(text,nodes[n])for n in tuple(n for n in ('_wrap_primary','_primary_wrapper','_decoded_primary_wrapper','verified_recovery_candle')if n in nodes))+'\n\n'
  check(actual==projected,'actual exact source projection')
  mode='exact complete-source bytes plus function projection'
 class ContractError(ValueError):pass
 class NativeBool:pass
 def utc(value):
  stamp=value if isinstance(value,datetime)else datetime.fromisoformat(value.replace('Z','+00:00'))
  if stamp.tzinfo is None:raise ContractError('Recovery clocks must be timezone aware')
  return stamp
 def default(value):
  if isinstance(value,datetime):return utc(value).isoformat()
  raise TypeError(type(value).__name__)
 def make(code,json_module=json):
  calls=[]
  def native(value):calls.append('native');return value.get('native_valid',True)
  def session(value,clock):
   calls.append('session')
   if not value.get('session_valid',True):raise ContractError('Invalid session')
  env={'__name__':'portable_recovery_comparison','json':json_module,'math':math,'Mapping':Mapping,'hashlib':hashlib,
       'np':types.SimpleNamespace(bool_=NativeBool,integer=int,floating=float),
       'pd':types.SimpleNamespace(isna=lambda v:v is None or isinstance(v,float)and math.isnan(v)),
       'POPULATION_CONTRACT':'fixture-population','SOURCE_ID':'fixture-multi-source','SOURCE_DATASET':'fixture-multi-dataset',
       'PRICE_FIELDS':('open','high','low','close','volume'),'ContractError':ContractError,'_default':default,
       '_utc':utc,'_session':session,'verified_source_observation':native,'recovery_candle':lambda assessment,session:copy.deepcopy(assessment['expected'])}
  exec('def _json(value):\n return json.dumps(value,sort_keys=True,separators=(",",":"),default=_default,allow_nan=False)',env)
  exec(compile(code,'portable-comparison-projection','exec'),env)
  return env,calls
 old,old_calls=make(BEFORE);new,new_calls=make(projected)
 check(new['_PLAIN_COMPARISON_STATE'] is not None,'ordinary stdlib enables conservative shortcut')
 def outcome(function):
  try:return('value',function())
  except Exception as exc:return('exception',type(exc).__name__,str(exc))
 class IntSub(int):pass
 class StrSub(str):pass
 class DictSub(dict):pass
 cases=[('normal',{'x':1},{'x':1}),('empty',{},{}),('null',{'x':None},{'x':None}),
  ('bool',{'x':True},{'x':1}),('numeric-equality',{'x':1},{'x':1.0}),('signed-zero',{'x':-0.0},{'x':0}),
  ('finite-extreme',{'x':1.7976931348623157e308},{'x':1.7976931348623157e308}),
  ('bounded-int',{'x':2**1023},{'x':2**1023}),('large-int-fallback',{'x':2**1024},{'x':2**1024}),
  ('oversized-int-error',{'x':10**5000},{'x':10**5000}),('long-proof-string',{'evidence':'a'*65536},{'evidence':'a'*65536}),
  ('surrogate-pair',{'x':'\ud800\udc00'},{'x':'\U00010000'}),('unicode-key',{'\u03bc':'a'},{'\u03bc':'a'}),
  ('unicode-value',{'x':'\u03bc'},{'x':'\u03bc'}),('numeric-key',{2:'two',10:'ten'},{'2':'two','10':'ten'}),
  ('bool-key',{False:'false',2:'two'},{'false':'false','2':'two'}),('null-key',{None:'nil'},{'null':'nil'}),
  ('mixed-keys',{2:'two','x':'x'},{}),('nan',{'x':float('nan')},{}),('inf',{'x':float('inf')},{}),
  ('nested',{'x':{'n':[1,2]}},{'x':{'n':[1,2]}}),('tuple',{'x':(1,2)},{'x':[1,2]}),
  ('int-subclass',{'x':IntSub(2)},{'x':2}),('str-subclass',{'x':StrSub('str')},{'x':'str'}),
  ('dict-subclass',DictSub(x=1),{'x':1}),('custom-mapping',{'x':UserDict(a=1)},{'x':{'a':1}}),
  ('custom-expected',{'x':1},DictSub(x=1)),('datetime',{'x':datetime.fromisoformat('2026-10-27T00:00+00:00')},{'x':'2026-10-27T00:00:00+00:00'}),
  ('different-value',{'x':1},{'x':2}),('different-keys',{'x':1},{'y':1})]
 for name,actual,expected in cases:
  check(outcome(lambda:old['_plain'](actual)==expected)==outcome(lambda:new['_plain_equal'](actual,expected)),'differential '+name)
 def native_row():
  return {'symbol':'TEST','source_id':'fixture-native','source_dataset':'fixture-primary','interval_seconds':900,'native_first_opened_at':None,'native_last_opened_at':None,
    'opened_at':'2026-10-27T10:00:00+00:00','available_at':'2026-10-27T10:15:00+00:00',
    'session_open':'2026-10-27T08:00:00+00:00','session_close':'2026-10-28T00:00:00+00:00','session_id':'fixture-session',
    'synthetic':False,'no_trade':False,'synthetic_open':False,'synthetic_close':False,
    'open':1.,'high':2.,'low':.5,'close':1.5,'volume':10.,'observed_constituents':1,'synthetic_constituents':0,'source_empty_constituents':14}
 for kind in ['normal','mapping','nested','tuple','unicode','huge-int']:
  native=native_row()
  if kind=='nested':native['extra']={'a':[1,2]}
  if kind=='tuple':native['extra']=(1,2)
  if kind=='unicode':native['extra']='\ud800\udc00'
  if kind=='huge-int':native['extra']=2**1024
  row=old['_wrap_primary'](native,None)
  if kind=='mapping':row['evidence_json']=json.loads(row['evidence_json'])
  old_calls.clear();new_calls.clear()
  check(old['verified_recovery_candle'](copy.deepcopy(row)) is True and new['verified_recovery_candle'](copy.deepcopy(row)) is True,'wrapper parity '+kind)
  check(old_calls==new_calls==['native','session'],'complete unchanged validators '+kind)
 row=old['_wrap_primary'](native_row(),None)
 for key,value in [('close',999),('available_at','2026-10-12T14:14:00Z'),('synthetic',True),('no_trade',True),('native_interval_seconds',300),('source_id','other'),('observed_constituents',15),('session_id','changed'),('volume',False)]:
  changed=copy.deepcopy(row);changed[key]=value
  check(old['verified_recovery_candle'](changed) is False and new['verified_recovery_candle'](changed) is False,'tampered retained proof '+key)
 changed=copy.deepcopy(row);proof=json.loads(changed['evidence_json']);proof['native_primary_sha256']='0'*64;changed['evidence_json']=new['_json'](proof)
 check(old['verified_recovery_candle'](changed) is False and new['verified_recovery_candle'](changed) is False,'native proof digest remains mandatory')
 for name in ['verified_source_observation','_session']:
  keep=new[name]
  if name=='verified_source_observation':new[name]=lambda *a:False
  else:
   def reject(*a):raise ContractError('session rejection')
   new[name]=reject
  check(new['verified_recovery_candle'](row)is False,'original callback rejection '+name)
  new[name]=keep
 # Replacement serializers before import must disable shortcut admission.
 for name in ['dumps','loads']:
  keep=getattr(json,name)
  def wrapper(*a,_keep=keep,**kw):return _keep(*a,**kw)
  setattr(json,name,wrapper)
  try:
   changed,_=make(projected)
   check(changed['_PLAIN_COMPARISON_STATE']is None,'preimport serializer fallback '+name)
   check(changed['_plain_equal']({'x':1},{'x':1})is True,'preimport generic comparison '+name)
  finally:setattr(json,name,keep)
 # Replacements after import must not reach even the flat-value guard.
 seams=[(new,'_plain'),(new,'_json'),(new,'_default'),(json,'dumps'),(json,'loads'),(json,'JSONEncoder'),(json,'JSONDecoder'),
  (json.JSONEncoder,'__init__'),(json.JSONEncoder,'encode'),(json.JSONEncoder,'iterencode'),(json.JSONEncoder,'default'),
  (json.encoder,'c_make_encoder'),(json.encoder,'encode_basestring_ascii'),(json.encoder,'_make_iterencode'),
  (json.JSONDecoder,'__init__'),(json.JSONDecoder,'decode'),(json.JSONDecoder,'raw_decode'),(json.decoder,'scanstring'),(json.scanner,'make_scanner'),
  (json,'_default_decoder'),(json._default_decoder,'parse_int'),(json._default_decoder,'parse_float'),(json._default_decoder,'parse_constant'),
  (json._default_decoder,'parse_object'),(json._default_decoder,'parse_array'),(json._default_decoder,'parse_string'),
  (json._default_decoder,'object_hook'),(json._default_decoder,'object_pairs_hook'),(json._default_decoder,'strict'),
  (json._default_decoder,'scan_once'),(json._default_decoder,'decode'),(json._default_decoder,'raw_decode')]
 def get(owner,name):return owner[name]if isinstance(owner,dict)else getattr(owner,name)
 def put(owner,name,value):owner.__setitem__(name,value)if isinstance(owner,dict)else setattr(owner,name,value)
 for index,(owner,name)in enumerate(seams):
  keep=get(owner,name);had=name in (owner if isinstance(owner,dict)else getattr(owner,'__dict__',{}))
  if name in('JSONEncoder','JSONDecoder'):replacement=type('Probe',(keep,),{})
  elif name=='_default_decoder':replacement=json.JSONDecoder()
  elif name=='strict':replacement=not keep
  elif keep is None:replacement=lambda v:v
  else:
   def replacement(*a,_keep=keep,**kw):return _keep(*a,**kw)
  guard=new['_flat_native_json_scalars']
  def forbidden(*a):raise AssertionError('Changed serializer reached fast guard')
  put(owner,name,replacement);new['_flat_native_json_scalars']=forbidden
  try:check(outcome(lambda:new['_plain']({'x':1})=={'x':1})==outcome(lambda:new['_plain_equal']({'x':1},{'x':1})),'postimport normalizer fallback '+str(index)+' '+name)
  finally:
   new['_flat_native_json_scalars']=guard
   if had:put(owner,name,keep)
   elif isinstance(owner,dict):del owner[name]
   else:delattr(owner,name)
 print(json.dumps({'checks_passed':len(checks),'source_validation':mode,'network_attempts':0,'limitations':'Explicit unchanged native/session and clock fixture adapters; actual complete-source integration and retained-data parity recorded separately.'},sort_keys=True))

if __name__=='__main__':main()
