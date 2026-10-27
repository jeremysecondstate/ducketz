"""Offline exact-patch checks and changed sizing-write function projection.

The transaction/source validators below are explicit fixture stubs. Actual
application integration, physical/schema guards and live loading are separate.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from threading import RLock, get_ident
from pathlib import Path
import ast,hashlib,json,re,socket
BEFORE='def save_sizing(self, forecast, result):\n        value = result.payload()\n        if value["forecast_id"] != forecast["forecast_id"] or value["symbol"] != forecast["symbol"]:\n            raise ContractError("Sizing belongs to another forecast")\n        existing = self.get(forecast["forecast_id"])\n        side = (existing["side"] if existing is not None else\n                {"BULLISH": "BUY", "BEARISH": "SELL"}.get(stock_direction(forecast["probability_not_down"]), "HOLD"))\n        if value["side"] != side or instant(value["calculated_at"]) < instant(forecast["issued_at"]):\n            raise ContractError("Sizing direction or availability differs from forecast")\n        if side == "HOLD" and value["status"] == "sized":\n            raise ContractError("A neutral forecast cannot request a trade")\n        if value["status"] != "sized":\n            return self.record(forecast, event_key="unavailable:" + result.result_id,\n                recorded_at=value["calculated_at"], disposition=value["reason"],\n                evidence={"sizing": value}, kind="sizing_unavailable")\n        desired = quantity(value["desired_quantity"])\n        payload = {"version": VERSION, "forecast_id": forecast["forecast_id"], "symbol": forecast["symbol"],\n                   "side": side, "desired_quantity": desired, "sizing_id": result.result_id, "sizing": value}\n        canonical = encoded(payload)\n        key = hashlib.sha256(canonical.encode()).hexdigest()\n        with self.store._execution_forecast_lock:\n            marker, original = self._ordinary_forecast_preflight(forecast)\n            with self.store.connect() as db:\n                db.execute("BEGIN IMMEDIATE")\n                self._ordinary_forecast_recheck(db, forecast, marker, original)\n                del original  # Release the full tree before reading sizing research.\n                old = self._value(db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",\n                                            (forecast["forecast_id"],)).fetchone())\n                if old is not None and old != payload:\n                    raise ContractError("Original desired quantity and sizing evidence are immutable")\n                db.execute("INSERT OR IGNORE INTO intraday_intentions VALUES(?,?,?)",\n                           (forecast["forecast_id"], key, canonical))\n        # Recovery can repeat this after a crash between the two transactions.\n        self.record(forecast, event_key="sized:" + result.result_id, recorded_at=value["calculated_at"],\n                    disposition="awaiting_capacity", evidence={"sizing_id": result.result_id}, kind="sized")\n        return payload\n'
AFTER='def save_sizing(self, forecast, result):\n        value = result.payload()\n        if value["forecast_id"] != forecast["forecast_id"] or value["symbol"] != forecast["symbol"]:\n            raise ContractError("Sizing belongs to another forecast")\n        existing = self.get(forecast["forecast_id"])\n        side = (existing["side"] if existing is not None else\n                {"BULLISH": "BUY", "BEARISH": "SELL"}.get(stock_direction(forecast["probability_not_down"]), "HOLD"))\n        if value["side"] != side or instant(value["calculated_at"]) < instant(forecast["issued_at"]):\n            raise ContractError("Sizing direction or availability differs from forecast")\n        if side == "HOLD" and value["status"] == "sized":\n            raise ContractError("A neutral forecast cannot request a trade")\n        if value["status"] != "sized":\n            return self.record(forecast, event_key="unavailable:" + result.result_id,\n                recorded_at=value["calculated_at"], disposition=value["reason"],\n                evidence={"sizing": value}, kind="sizing_unavailable")\n        desired = quantity(value["desired_quantity"])\n        payload = {"version": VERSION, "forecast_id": forecast["forecast_id"], "symbol": forecast["symbol"],\n                   "side": side, "desired_quantity": desired, "sizing_id": result.result_id, "sizing": value}\n        canonical = encoded(payload)\n        key = hashlib.sha256(canonical.encode()).hexdigest()\n        # Native first insertion can reuse its independently validated forecast\n        # for the immediate projection. Custom callbacks retain their original\n        # reads; existing/cold sizing clears the proof before decoding research.\n        reuse_forecast = _sizing_save_forecast_reuse(self)\n        with (_forecast_record_scope(self) if reuse_forecast else nullcontext()):\n            with self.store._execution_forecast_lock:\n                marker, original = self._ordinary_forecast_preflight(forecast)\n                with self.store.connect() as db:\n                    db.execute("BEGIN IMMEDIATE")\n                    self._ordinary_forecast_recheck(db, forecast, marker, original)\n                    del original  # Release the full tree before reading sizing research.\n                    old_row = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",\n                                         (forecast["forecast_id"],)).fetchone()\n                    if reuse_forecast and (old_row is not None or not _sizing_save_forecast_reuse(self)):\n                        _clear_forecast_record_scope(self)\n                    old = self._value(old_row)\n                    if old is not None and old != payload:\n                        raise ContractError("Original desired quantity and sizing evidence are immutable")\n                    db.execute("INSERT OR IGNORE INTO intraday_intentions VALUES(?,?,?)",\n                               (forecast["forecast_id"], key, canonical))\n            # Recovery can repeat this after a crash between the two transactions.\n            if reuse_forecast and not _sizing_save_forecast_reuse(self):\n                _clear_forecast_record_scope(self)\n            self.record(forecast, event_key="sized:" + result.result_id, recorded_at=value["calculated_at"],\n                        disposition="awaiting_capacity", evidence={"sizing_id": result.result_id}, kind="sized")\n        return payload\n'
HELPER='# Capture the original descriptors once; comparing with mutable class attributes\n# would mistake a later callback replacement for native validation.\n\n_SIZING_SAVE_INTENTION_CLASS = IntentionStore\n_SIZING_SAVE_INTENTION_METHODS = {name: IntentionStore.__dict__[name] for name in (\n    "get", "_value", "record", "_forecast", "_ordinary_forecast_marker",\n    "_ordinary_forecast_preflight", "_ordinary_forecast_recheck", "save_sizing",\n    "_revision_sizing", "_recovery_sizing_reader", "_revision_sizing_row")}\n_SIZING_SAVE_STORE_METHODS = {name: _SizingSaveShadowStore.__dict__[name] for name in (\n    "connect", "_forecast_value", "_execution_forecast_generation", "_execution_forecast_schema")}\n\n\ndef _sizing_save_forecast_reuse(owner):\n    """Custom validation and connection callbacks keep their original reads."""\n    if type(owner) is not _SIZING_SAVE_INTENTION_CLASS or type(owner.store) is not _SizingSaveShadowStore:\n        return False\n    for instance, expected in ((owner, _SIZING_SAVE_INTENTION_METHODS),\n                               (owner.store, _SIZING_SAVE_STORE_METHODS)):\n        if any(name in instance.__dict__ or type(instance).__dict__.get(name) is not native\n               for name, native in expected.items()):\n            return False\n    return True\n'

def main():
 root=Path(__file__).resolve().parents[1];stem=root/'docs/development/patches/intraday-sizing-save-forecast-reuse'
 meta=json.loads(stem.with_suffix('.json').read_bytes());patch=stem.with_suffix('.patch').read_bytes();checks=[]
 def check(ok,label):assert ok,label;checks.append(label)
 def deny(*a,**k):raise AssertionError('Sizing persistence checks prohibit network')
 socket.socket.connect=deny;socket.create_connection=deny
 check(hashlib.sha256(patch).hexdigest()==meta['patch_sha256'],'exact patch fingerprint')
 projected=BEFORE;helper_hunks=[]
 for h in re.split(r'^@@.*?@@[^\n]*\n',patch.decode(),flags=re.M)[1:]:
  old=''.join(l[1:]for l in h.splitlines(True)if l.startswith((' ','-')));new=''.join(l[1:]for l in h.splitlines(True)if l.startswith((' ','+')))
  if old:
   check(projected.count(old)==1,'unique owned function hunk');projected=projected.replace(old,new)
  elif '# Capture the original descriptors once;'in new:helper_hunks.append(new)
  else:
   # Added lines immediately before an old body region may be separate zero-context hunks.
   check(new.strip()in AFTER,'owned addition appears in candidate')
 check(ast.dump(ast.parse(projected))==ast.dump(ast.parse(AFTER)),'complete changed function projection')
 check(len(helper_hunks)==1 and helper_hunks[0].replace('from .store import ShadowStore as _SizingSaveShadowStore\n','').strip()==HELPER.strip(),'complete helper addition')
 mode='portable function projection'
 p=root/meta['path']
 if p.exists()and hashlib.sha256(p.read_bytes()).hexdigest()==meta['after_sha256']:
  text=p.read_text(encoding='utf8');tree=ast.parse(text);c=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='IntentionStore');method=next(n for n in c.body if isinstance(n,ast.FunctionDef)and n.name=='save_sizing')
  check(ast.dump(method)==ast.dump(ast.parse(AFTER).body[0]),'actual installed changed function');mode='complete exact source plus function projection'
 class ContractError(ValueError):pass
 canonical=lambda v:json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
 def make(src):
  scope=ContextVar('portable_sizing_scope',default=None)
  @contextmanager
  def scoped(owner):
   previous=scope.get()
   if previous is not None:previous[1].clear()
   proofs,active={},[True];token=scope.set((owner,proofs,get_ident(),active))
   try:yield
   finally:active[0]=False;proofs.clear();scope.reset(token)
  def clear(owner):
   s=scope.get()
   if s is not None and s[0]is owner:s[1].clear()
  class Cursor:
   def __init__(self,row=None):self.row=row
   def fetchone(self):return self.row
  class DB:
   def __init__(self,store):self.store=store
   def execute(self,sql,params=()):
    if sql=='BEGIN IMMEDIATE':self.store.write=True;return Cursor()
    if sql.startswith('SELECT forecast_id,id,payload'):return Cursor(self.store.row)
    if sql.startswith('INSERT OR IGNORE'):
     if self.store.row is None:self.store.row=tuple(params)
     return Cursor()
    raise AssertionError(sql)
  class Store:
   def __init__(self,f):self.forecast=deepcopy(f);self.generation=0;self.row=None;self.write=False;self.commits=0;self.seam=None;self._execution_forecast_lock=RLock();self.forecast_calls=0
   @contextmanager
   def connect(self):
    before=self.row;self.write=False
    try:yield DB(self)
    except BaseException:self.row=before;raise
    else:
     if self.write:
      self.commits+=1
      if self.seam:self.seam(self)
   def _forecast_value(self,row):self.forecast_calls+=1;return deepcopy(self.forecast)
   @classmethod
   def _execution_forecast_generation(cls,db):return db.store.generation
   @staticmethod
   def _execution_forecast_schema(db):return True
  class Intent:
   def __init__(self,s):self.store=s;self.events=[];self.read_scopes=[]
   def get(self,fid):return self._value(self.store.row)
   def _value(self,row):
    s=scope.get();self.read_scopes.append((row is None,len(s[1])if s else 0))
    if row is None:return None
    v=json.loads(row[2])
    if hashlib.sha256(canonical(v).encode()).hexdigest()!=row[1]:raise ContractError('Persisted sizing intention changed')
    return v
   def _ordinary_forecast_marker(self,db,fid):return self.store.generation
   def _ordinary_forecast_preflight(self,f):
    s=scope.get();proofs=s[1]if s and s[0]is self else None;m=self.store.generation;old=proofs.get(f['forecast_id'])if proofs is not None else None
    v=old[1]if old is not None and old[0]==m else self.store._forecast_value(None)
    if v!=f:raise ContractError('Intention requires the original saved forecast')
    if proofs is not None:proofs.clear();proofs[f['forecast_id']]=(m,v)
    return m,v
   def _ordinary_forecast_recheck(self,db,f,m,v):
    if m!=self.store.generation:v=self.store._forecast_value(None)
    if v!=f:raise ContractError('Intention requires the original saved forecast')
   def _forecast(self,db,f):return self.store._forecast_value(None)
   def _revision_sizing(self):clear(self);return self._value(self.store.row)
   def _recovery_sizing_reader(self):pass
   def _revision_sizing_row(self):pass
   def record(self,f,**kw):
    m,v=self._ordinary_forecast_preflight(f)
    with self.store.connect()as db:self._ordinary_forecast_recheck(db,f,m,v);del v;self._revision_sizing()
    if kw not in self.events:self.events.append(deepcopy(kw))
    return kw
  env={'hashlib':hashlib,'encoded':canonical,'quantity':lambda v:v,'instant':lambda v:v,'stock_direction':lambda p:'BULLISH'if p>=.5001 else'BEARISH'if p<=.4999 else'NEUTRAL','ContractError':ContractError,'VERSION':'fixture-intention','nullcontext':nullcontext,'_forecast_record_scope':scoped,'_clear_forecast_record_scope':clear,'IntentionStore':Intent,'_SizingSaveShadowStore':Store}
  exec(compile(src,'sizing-save-projection','exec'),env);Intent.save_sizing=env['save_sizing'];exec(compile(HELPER,'sizing-save-helper','exec'),env)
  class Result:
   def __init__(self,v):self.value=v;self.result_id=hashlib.sha256(canonical(v).encode()).hexdigest()
   def payload(self):return deepcopy(self.value)
  return Store,Intent,Result,env,scope
 f={'forecast_id':'fixture-forecast','symbol':'FIXTURE','probability_not_down':.7,'issued_at':'2026-01-01T00:00:00Z','nested':{'proof':[1,2]}}
 v={'forecast_id':f['forecast_id'],'symbol':f['symbol'],'side':'BUY','status':'sized','desired_quantity':4,'calculated_at':'2026-01-01T00:00:01Z'}
 for existing in (False,True):
  results=[]
  for src in(BEFORE,AFTER):
   S,I,Q,e,sc=make(src);s=S(f);i=I(s)
   if existing:i.save_sizing(f,Q(v));s.forecast_calls=0;i.read_scopes=[]
   out=i.save_sizing(f,Q(v));results.append((out,s.row,i.events))
   check(s.forecast_calls==(2 if existing or src==BEFORE else 1),'bounded original forecast validations')
   check(all(n==0 for empty,n in i.read_scopes if not empty),'clear before existing sizing research')
   check(sc.get()is None,'scope ended')
  check(results[0]==results[1],'exact intention bytes and projection parity')
 S,I,Q,e,sc=make(AFTER)
 for owner_key,names in(('intent',e['_SIZING_SAVE_INTENTION_METHODS']),('store',e['_SIZING_SAVE_STORE_METHODS'])):
  for name in names:
   s=S(f);i=I(s);owner=i if owner_key=='intent'else s
   setattr(owner,name,getattr(owner,name));check(not e['_sizing_save_forecast_reuse'](i),'instance callback fallback '+name)
   i.save_sizing(f,Q(v));check(s.forecast_calls==2,'custom callbacks retain reads '+name)
 for kind in('caller','source','record_callback'):
  S,I,Q,e,sc=make(AFTER);s=S(f);i=I(s);caller=deepcopy(f);seen=[]
  def seam(store):
   store.seam=None
   if kind=='caller':caller['nested']['proof'].append(9)
   elif kind=='source':store.forecast['nested']['proof'].append(9);store.generation+=1
   else:
    old=i.record
    def changed(*a,**kw):seen.append(len(sc.get()[1]));return old(*a,**kw)
    i.record=changed
  s.seam=seam
  try:i.save_sizing(caller,Q(v));result='value'
  except ContractError:result='error'
  check(result==('value'if kind=='record_callback'else'error'),'between-commit mutation '+kind)
  check(sc.get()is None,'failure scope cleanup '+kind)
  if kind=='record_callback':check(seen==[0],'new callback sees no retained proof')
 print(json.dumps({'checks_passed':len(checks),'source_validation':mode,'network_attempts':0,'limitation':'Changed function and eligibility projection use explicit unchanged transaction/source validator stubs; real installed integration separately required.'}))
 return 0
if __name__=='__main__':raise SystemExit(main())
