"""Standard-library offline checks for bounded forecast-record validation reuse.

Runs only the owned extracted scope/preflight methods with SQLite and a strict
fake original validator. Existing marker and writer-recheck implementations are
bound by unchanged hashes; their complete application checks stay separate.
"""
from __future__ import annotations
import argparse
import ast
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace

PREFIX='intraday-forecast-record-reuse'
TARGETS=('ml/intraday/intentions.py','ml/intraday/account_execution.py')
CHECKS=[]
def check(name,condition):
 if not condition:raise AssertionError(name)
 CHECKS.append(name)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)
def dump(node):return ast.dump(node,include_attributes=False)
def extract(raw,name,owner=None):
 text=raw.decode();tree=ast.parse(text)
 body=next(n.body for n in tree.body if isinstance(n,ast.ClassDef)and n.name==owner)if owner else tree.body
 n=next(n for n in body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))and n.name==name)
 start=min([n.lineno]+[d.lineno for d in n.decorator_list])
 lines=text.splitlines()[start-1:n.end_lineno]
 if owner:lines=[line[4:]if line.startswith('    ')else line for line in lines]
 return '\n'.join(lines)+'\n'

def package(root):
 directory=root/'docs/development/patches'; meta=json.loads((directory/(PREFIX+'.json')).read_bytes());patch=(directory/(PREFIX+'.patch')).read_bytes()
 check('metadata schema',meta['schema']==PREFIX+'-v1')
 check('only two reviewed source targets',tuple(meta['targets'])==TARGETS)
 check('exact patch hash',sha(patch)==meta['patch_sha256'])
 lines=patch.decode().splitlines()
 check('exact old headers',[x[6:]for x in lines if x.startswith('--- a/')]==list(TARGETS))
 check('exact new headers',[x[6:]for x in lines if x.startswith('+++ b/')]==list(TARGETS))
 check('exact hunk count',sum(x.startswith('@@ ')for x in lines)==meta['expected_hunks'])
 check('source byte identity fields',all(len(v[k])==64 and int(v[k],16)>=0 for v in meta['targets'].values()for k in('before_sha256','after_sha256')))
 check('exact context variable declaration',dump(ast.parse(meta['contextvar_source']))==dump(ast.parse('_FORECAST_RECORD_SCOPE=ContextVar("intraday_forecast_record_scope",default=None)')))
 for name,item in meta['owned_functions'].items():
  check('owned function hash '+name,sha(item['source'].encode())==item['sha256'])
  nodes=ast.parse(item['source']).body
  check('one owned function '+name,len(nodes)==1 and isinstance(nodes[0],ast.FunctionDef)and nodes[0].name==name)
 scope=meta['owned_functions']['_forecast_record_scope']['source'];preflight=meta['owned_functions']['_ordinary_forecast_preflight']['source']
 check('scope always clears tree',any(isinstance(n,ast.Try)and n.finalbody for n in ast.walk(ast.parse(scope))))
 check('full caller comparison remains','original != forecast' in preflight)
 check('one retained forecast tree','proofs.clear()'in preflight)
 check('source identity uncertain full fallback','marker is not None and previous is not None and previous[0] == marker'in preflight)
 fragment=ast.parse(meta['step_scope_source']).body
 check('one final capacity submission scope',len(fragment)==1 and isinstance(fragment[0],ast.With))
 call=fragment[0].items[0].context_expr
 check('scope binds exact intention store',dump(call)==dump(ast.parse('_forecast_record_scope(self.intents)',mode='eval').body))
 attrs=[n.attr for n in ast.walk(fragment[0])if isinstance(n,ast.Attribute)]
 check('final scope contains capacity and submit','_submission_capacity'in attrs and '_submit'in attrs)
 check('science and cancellation outside scope',all(v not in attrs for v in('save_sizing','size_forecast','_capture','_expired_without_order_work')))
 added=[line[1:]for line in lines if line.startswith('+')and not line.startswith('+++')]
 check('ContextVar import in patch','from contextvars import ContextVar'in added)
 check('scope function in patch','def _forecast_record_scope(owner):'in added)
 check('scope wiring in patch',any('with _forecast_record_scope(self.intents):'in line for line in added))
 return meta

class ContractError(ValueError):pass
class Store:
 def __init__(self,path):
  self.path=path;self.calls=0;self.uncertain=False;self.generation=0;self.source='physical-source-one'
  self.fail_validation=False
  with self.connect()as db:db.execute('CREATE TABLE forecasts(id TEXT PRIMARY KEY,pc TEXT,symbol TEXT,cycle_id TEXT,reference_at TEXT,issued_at TEXT,payload TEXT)')
 @contextmanager
 def connect(self):
  db=sqlite3.connect(self.path)
  try:
   yield db
  finally:db.close()
 def write(self,forecast):
  columns=tuple(forecast[k]for k in('forecast_id','pc','symbol','cycle_id','reference_at','issued_at'))
  with self.connect()as db:
   db.execute('INSERT OR REPLACE INTO forecasts VALUES(?,?,?,?,?,?,?)',columns+(canonical(forecast),));db.commit()
  self.generation+=1
 def _forecast_value(self,row):
  self.calls+=1
  if self.fail_validation:raise ContractError('full validation failure')
  value=json.loads(row[6]);plain=dict(value);key=plain.pop('forecast_id')
  if key!=sha(canonical(plain).encode())or tuple(value[k]for k in('forecast_id','pc','symbol','cycle_id','reference_at','issued_at'))!=row[:6]:raise ContractError('invalid full original')
  return value

def make(meta,path):
 ordinary=SimpleNamespace(context=lambda manager,*args:manager,phase=lambda *args:nullcontext(),payload=lambda *args:None)
 ns=dict(ContextVar=ContextVar,get_ident=threading.get_ident,contextmanager=contextmanager,_ordinary=ordinary,ContractError=ContractError)
 exec(meta['contextvar_source'],ns)
 for name in('_forecast_record_scope','_clear_forecast_record_scope','_ordinary_forecast_preflight','_ordinary_forecast_recheck'):exec(meta['owned_functions'][name]['source'],ns)
 class Harness:
  _ordinary_forecast_preflight=ns['_ordinary_forecast_preflight']
  _ordinary_forecast_recheck=ns['_ordinary_forecast_recheck']
  def __init__(self,store):self.store=store
  def _ordinary_forecast_marker(self,db,forecast_id):
   if self.store.uncertain:return None
   row=db.execute('SELECT id,pc,symbol,cycle_id,reference_at,issued_at FROM forecasts WHERE id=?',(forecast_id,)).fetchone()
   if row is None:return None
   return(self.store.source,self.store.generation,tuple(row))
  def _forecast(self,db,forecast):
   row=db.execute('SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE id=?',(forecast['forecast_id'],)).fetchone()
   if row is None or self.store._forecast_value(row)!=forecast:raise ContractError('invalid full original')
 s=Store(path);h=Harness(s)
 return s,h,ns['_forecast_record_scope'],ns['_FORECAST_RECORD_SCOPE'],ns

def forecast(symbol='TEST'):
 value=dict(pc='fixture',symbol=symbol,cycle_id='fixture-cycle',reference_at='2026-10-27T20:00:00+00:00',issued_at='2026-10-27T20:00:01+00:00',nested=dict(models=[dict(probability=.5001,target='original-target')]),probability=.5001)
 return dict(forecast_id=sha(canonical(value).encode()),**value)
def rejects(fn,label):
 try:fn()
 except ContractError:check(label,True)
 else:check(label,False)

def semantics(meta):
 with tempfile.TemporaryDirectory(prefix='forecast-record-proof-')as temp:
  s,h,scope,var,ns=make(meta,Path(temp)/'originals.sqlite3');f=forecast();s.write(f)
  a=h._ordinary_forecast_preflight(f);b=h._ordinary_forecast_preflight(f)
  check('outside scope always fully validates',s.calls==2 and a==b and a[1]is not b[1])
  calls=s.calls
  with scope(h):
   first=h._ordinary_forecast_preflight(f); second=h._ordinary_forecast_preflight(deepcopy(f))
   check('warm scope one full validation',s.calls==calls+1 and first[1]is second[1])
   check('decoded original never caller tree',first[1]is not f and first[1]['nested']is not f['nested'])
   check('exact independent tree retained',first[1]==f and len(var.get()[1])==1)
   for label,mutate in [('nested probability',lambda x:x['nested']['models'][0].update(probability=.9)),('extra field',lambda x:x.update(extra='not original')),('missing field',lambda x:x.pop('nested')),('top probability',lambda x:x.update(probability=.9))]:
    changed=deepcopy(f);mutate(changed)
    rejects(lambda:h._ordinary_forecast_preflight(changed),'full caller equality rejects '+label)
   check('rejected caller cannot mutate proof',h._ordinary_forecast_preflight(f)[1]==f)
   proofs=var.get()[1]
  check('normal exit clears tree and context',proofs=={} and var.get()is None)
  calls=s.calls
  with scope(h):h._ordinary_forecast_preflight(f)
  check('next scope cannot inherit prior proof',s.calls==calls+1)
  calls=s.calls
  with scope(h):
   h._ordinary_forecast_preflight(f);s.generation+=1;h._ordinary_forecast_preflight(f)
  check('revision marker change full revalidation',s.calls==calls+2)
  calls=s.calls
  with scope(h):
   h._ordinary_forecast_preflight(f);s.source='physical-source-two';h._ordinary_forecast_preflight(f)
  check('physical marker change full revalidation',s.calls==calls+2)
  calls=s.calls
  with scope(h):
   s.uncertain=True;h._ordinary_forecast_preflight(f);h._ordinary_forecast_preflight(f)
   check('uncertain source never retains proof',var.get()[1]=={})
  check('uncertain source preserves full fallback',s.calls==calls+2);s.uncertain=False
  calls=s.calls
  with scope(h):
   h._ordinary_forecast_preflight(f);s.uncertain=True;h._ordinary_forecast_preflight(f)
   check('uncertainty clears earlier proof',var.get()[1]=={});s.uncertain=False;h._ordinary_forecast_preflight(f)
  check('certainty after uncertainty validates again',s.calls==calls+3)
  calls=s.calls
  with scope(h):
   h._ordinary_forecast_preflight(f)
   with scope(h):h._ordinary_forecast_preflight(f)
   h._ordinary_forecast_preflight(f)
  check('reentrant nested work drops earlier large proof',s.calls==calls+3 and var.get()is None)
  calls=s.calls
  try:
   with scope(h):
    h._ordinary_forecast_preflight(f);proofs=var.get()[1];raise LookupError('expected scope exit')
  except LookupError:pass
  check('exception exit clears context and tree',var.get()is None and proofs=={})
  with scope(h):h._ordinary_forecast_preflight(f)
  check('exception does not authorize future reuse',s.calls==calls+2)
  h2=type(h)(s);calls=s.calls
  with scope(h):h._ordinary_forecast_preflight(f);h2._ordinary_forecast_preflight(f);h2._ordinary_forecast_preflight(f);h._ordinary_forecast_preflight(f)
  check('owner identity isolates other intention instance',s.calls==calls+3)
  f2=forecast('OTHER');s.write(f2);calls=s.calls
  with scope(h):
   h._ordinary_forecast_preflight(f);h._ordinary_forecast_preflight(f2)
   check('one forecast bounded retention',len(var.get()[1])==1 and f2['forecast_id']in var.get()[1]);h._ordinary_forecast_preflight(f)
  check('changed forecast identity full validates',s.calls==calls+3)
  with scope(h):
   h._ordinary_forecast_preflight(f)
   with s.connect()as db:db.execute('DELETE FROM forecasts WHERE id=?',(f['forecast_id'],));db.commit()
   rejects(lambda:h._ordinary_forecast_preflight(f),'missing original cannot reuse cached proof')
  s.write(f)
  with scope(h):
   h._ordinary_forecast_preflight(f);s.generation+=1
   with s.connect()as db:db.execute('UPDATE forecasts SET symbol=? WHERE id=?',('FORGED',f['forecast_id']));db.commit()
   rejects(lambda:h._ordinary_forecast_preflight(f),'changed indexed original retains full validation')
  s.write(f)
  with scope(h):
   h._ordinary_forecast_preflight(f);s.generation+=1
   with s.connect()as db:db.execute('UPDATE forecasts SET payload=? WHERE id=?',(canonical(dict(f,probability=.9)),f['forecast_id']));db.commit()
   rejects(lambda:h._ordinary_forecast_preflight(f),'changed full scientific payload retains hash validation')
  s.write(f)
  with scope(h):
   h._ordinary_forecast_preflight(f);s.generation+=1;s.fail_validation=True
   rejects(lambda:h._ordinary_forecast_preflight(f),'validation error propagates')
  check('validation failure exits scope cleanly',var.get()is None);s.fail_validation=False
  seen=[]
  with scope(h):
   h._ordinary_forecast_preflight(f)
   worker=threading.Thread(target=lambda:seen.append(var.get()));worker.start();worker.join()
  check('new thread cannot inherit active proof',seen==[None])
  with scope(h):
   h._ordinary_forecast_preflight(f);captured=copy_context();retained=var.get()[1];active=var.get()[3]
  calls=s.calls
  captured.run(h._ordinary_forecast_preflight,f);captured.run(h._ordinary_forecast_preflight,f)
  check('copied context after exit cannot extend lifetime',s.calls==calls+2 and retained=={} and active==[False])
  seen=[];failures=[]
  with scope(h):
   h._ordinary_forecast_preflight(f);captured=copy_context();retained=var.get()[1];calls=s.calls
   def copied_thread():
    try:captured.run(h._ordinary_forecast_preflight,f);captured.run(h._ordinary_forecast_preflight,f)
    except BaseException as exc:failures.append(repr(exc))
   worker=threading.Thread(target=copied_thread);worker.start();worker.join()
   check('copied context on foreign thread full validates',not failures and s.calls==calls+2)
   check('foreign thread does not replace owners proof',var.get()[1]is retained and len(retained)==1)
  calls=s.calls
  with scope(h):
   marker,original=h._ordinary_forecast_preflight(f);h._ordinary_forecast_preflight(f)
   with s.connect()as db:h._ordinary_forecast_recheck(db,f,marker,original)
   check('unchanged writer recheck retains full equality without decode',s.calls==calls+1)
   changed=deepcopy(f);changed['nested']['models'][0]['probability']=.9
   with s.connect()as db:rejects(lambda:h._ordinary_forecast_recheck(db,changed,marker,original),'writer rejects nested caller mutation')
   s.generation+=1
   with s.connect()as db:h._ordinary_forecast_recheck(db,f,marker,original)
   check('changed writer clears retained proof before full fallback',var.get()[1]=={} and s.calls==calls+2)
  calls=s.calls
  with scope(h):
   h._ordinary_forecast_preflight(f);ns['_clear_forecast_record_scope'](h)
   check('cold-sizing clear drops research overlap proof',var.get()[1]=={})
   h._ordinary_forecast_preflight(f)
  check('cold-sizing clear requires subsequent full forecast validation',s.calls==calls+2)
  function='def final_capacity_path(self,forecast,original,window):\n'+''.join('    '+line+'\n'for line in meta['step_scope_source'].splitlines())
  exec(function,ns);finish=ns['final_capacity_path']
  class Executor:
   def __init__(self,quantity,error=False):self.intents=h;self.quantity=quantity;self.error=error;self.snapshot_id='fixture-snapshot';self.events=[]
   def observe(self,event):
    check('step scope active at '+event,var.get()is not None and var.get()[0]is h)
    self.events.append(event);h._ordinary_forecast_preflight(f)
   def _submission_capacity(self,forecast,original):self.observe('capacity');return SimpleNamespace(adjusted_quantity=self.quantity,disposition='fixture-capacity'),object()
   def _record(self,*args,**kwargs):self.observe('zero-capacity-record');return'zero'
   def _submit(self,*args):
    self.observe('submit')
    if self.error:raise LookupError('fixture submission failure')
    return'submitted'
  for quantity,expected in((0,'zero'),(1,'submitted')):
   executor=Executor(quantity);calls=s.calls
   check('step fragment returns '+expected,finish(executor,f,{},object())==expected)
   check('step final records share one validated original '+expected,s.calls==calls+1 and var.get()is None)
  executor=Executor(1,error=True)
  try:finish(executor,f,{},object())
  except LookupError:pass
  check('step submission exception releases scope',var.get()is None)

def installed(root,meta):
 raw=(root/'manifest.json').read_bytes();manifest=json.loads(raw)
 check('installed content address exact',sha(raw)==root.name)
 for target,info in meta['targets'].items():
  source=(root/'source'/target).read_bytes()
  check('installed exact source '+target,sha(source)==info['after_sha256']==manifest['source_files'][target])
  check('installed byte length '+target,len(source)==info['after_bytes'])
  for name,item in meta['owned_functions'].items():
   if item['target']==target:check('installed exact owned function '+name,extract(source,name,item.get('owner'))==item['source'])
  for name,item in info['unchanged_methods'].items():check('installed protected method '+name,sha(extract(source,name,item.get('owner')).encode())==item['sha256'])

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--installed-root',type=Path);a=p.parse_args()
 meta=package(Path(__file__).resolve().parents[1]);semantics(meta)
 if a.installed_root:installed(a.installed_root,meta)
 print(json.dumps(dict(passed=len(CHECKS),failed=0,production_imports=0,network_attempts=0,scope='Owned extracted ContextVar scope and original preflight reuse semantics; SQLite-backed fake original/hash/index validation. Marker/recheck underlying proofs and complete application behavior have separate evidence.')))
if __name__=='__main__':main()
