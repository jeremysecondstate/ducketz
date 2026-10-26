"""Frozen scientific input reuse keeps canonical bytes and first-writer proof."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import hashlib
import json

import pytest

from ml.intraday import prediction
from ml.intraday import store as storemod
from ml.intraday.prediction import ContractError, encoded, digest


def sample():return {'scientific_tree':{'nested':[[i,{'probability':i/1000,'target':'original'}]for i in range(20)]},'models':('original',),'numeric_keys':{2:'two',10:'ten'},'unicode':'μ'}

def test_new_snapshot_canonicalizes_twice_not_four_times(tmp_path,monkeypatch):
 store=storemod.ShadowStore(tmp_path/'evidence.sqlite3');value=sample();original=encoded;calls=[]
 def count(v):
  if isinstance(v,dict)and'scientific_tree'in v:calls.append(type(v['models']).__name__)
  return original(v)
 monkeypatch.setattr(storemod,'encoded',count);monkeypatch.setattr(prediction,'encoded',count)
 key,result=store.freeze_inputs('cycle','AAPL',value)
 assert result==json.loads(original(value))
 assert key==hashlib.sha256(original(result).encode()).hexdigest()
 with store.connect()as db:assert db.execute('select id,payload from snapshots').fetchone()==(key,original(result))
 assert len(calls)==2, calls

def test_first_binding_wins_and_remains_fully_validated(tmp_path):
 store=storemod.ShadowStore(tmp_path/'evidence.sqlite3');a=sample();key,original=store.freeze_inputs('cycle','AAPL',a);later=sample();later['scientific_tree']['nested'][0][1]['probability']=.99
 assert store.freeze_inputs('cycle','AAPL',later)==(key,original)
 with store.connect()as db:db.execute("update snapshots set payload='{}' where id=?",(key,))
 with pytest.raises(ContractError,match='hash changed'):store.freeze_inputs('cycle','AAPL',later)

@pytest.mark.parametrize('value',[[1,2],None,'text',4])
def test_new_nonmapping_still_rejected_and_rolled_back(tmp_path,value):
 store=storemod.ShadowStore(tmp_path/'evidence.sqlite3')
 with pytest.raises(ContractError,match='hash changed'):store.freeze_inputs('cycle','AAPL',value)
 with store.connect()as db:assert db.execute('select count(*)from snapshots').fetchone()[0]==0;assert db.execute('select count(*)from symbol_inputs').fetchone()[0]==0

@pytest.mark.parametrize('case',['different','noncanonical','trigger'])
def test_stored_readback_still_checks_existing_and_changed_bytes(tmp_path,case):
 store=storemod.ShadowStore(tmp_path/'evidence.sqlite3');value=json.loads(encoded(sample()));key=digest(value)
 with store.connect()as db:
  if case=='different':db.execute('insert into snapshots values(?,?)',(key,'{"changed":true}'))
  elif case=='noncanonical':db.execute('insert into snapshots values(?,?)',(key,json.dumps(value,indent=2)))
  else:db.execute("create trigger damage after insert on snapshots begin update snapshots set payload='{}'where id=new.id;end")
 if case=='noncanonical':assert store.freeze_inputs('cycle','AAPL',value)==(key,value)
 else:
  with pytest.raises(ContractError,match='hash changed'):store.freeze_inputs('cycle','AAPL',value)
  with store.connect()as db:assert db.execute('select count(*)from symbol_inputs').fetchone()[0]==0

@pytest.mark.parametrize('same_binding',[True,False])
def test_two_real_sqlite_writers_preserve_first_snapshot(tmp_path,same_binding):
 store=storemod.ShadowStore(tmp_path/'evidence.sqlite3');barrier=Barrier(2);a=sample();b=sample()
 if same_binding:b['scientific_tree']['nested'][0][1]['probability']=.8
 def freeze(i):barrier.wait(timeout=5);return store.freeze_inputs('cycle'if same_binding else'cycle'+str(i),'AAPL',(a,b)[i])
 with ThreadPoolExecutor(max_workers=2)as pool:results=list(pool.map(freeze,[0,1]))
 assert results[0]==results[1]
 key,value=results[0];assert key==digest(value);assert value in [json.loads(encoded(a)),json.loads(encoded(b))]
 with store.connect()as db:
  assert db.execute('select count(*)from snapshots').fetchone()[0]==1
  assert db.execute('select count(*)from symbol_inputs').fetchone()[0]==(1 if same_binding else 2)
  assert db.execute('select payload from snapshots where id=?',(key,)).fetchone()[0]==encoded(value)

def test_nonfinite_upfront_validation_preserved_for_existing_binding(tmp_path):
 store=storemod.ShadowStore(tmp_path/'evidence.sqlite3');store.freeze_inputs('cycle','AAPL',sample())
 with pytest.raises(ValueError):store.freeze_inputs('cycle','AAPL',{'bad':float('nan')})
