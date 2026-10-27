from pathlib import Path
from datetime import datetime,timedelta,timezone
from copy import deepcopy
from dataclasses import asdict
import importlib,json,sys,sqlite3
import pytest
from filelock import FileLock,Timeout
import ml.intraday.market_collection as m
import ml.intraday.market_collection_index as idx
NOW=datetime(2026,10,26,23,tzinfo=timezone.utc)
END=NOW-timedelta(hours=49)
def REQUEST(n):return m.SourceRequest(provider='fmp',source_kind='fundamentals',symbol='FIXTURE',dataset='fixture',schema='fixture',params={'fixture_i':n})
def KEY(n):return (END-timedelta(minutes=15)).strftime('%Y%m%dT%H%M%SZ')+'-'+REQUEST(n).identity

def make(tmp_path,n=2,age=timedelta(hours=49),collector_class=None):
 request=REQUEST(0)
 def forbidden(*args):raise AssertionError('No provider adapter may enter maintenance')
 c=(collector_class or m.QuarterHourMarketCollector)(tmp_path,symbols=['FIXTURE'],requests=[request],adapters={'fmp':forbidden},clock=lambda:END+age)
 end=END;tasks={}
 for i in range(n):tasks[KEY(i)]={'request':asdict(REQUEST(i)),'quarter_start':(end-timedelta(minutes=15)).isoformat(),'quarter_end':end.isoformat(),'status':'QUEUED','attempts':0,'due_at':end.isoformat(),'snapshots':[]}
 state={'schema_version':m.QUEUE_VERSION,'symbols':['FIXTURE'],'tasks':tasks,'source_blocks':{'keep':{'private_fixture':'yes'}},'extra':{'keep':[True,1,1.0]}}
 m._atomic(c.state_path,state);return c,state,request

def read(c):return json.loads(c.state_path.read_bytes())
def files(c):return sorted((c.root/'snapshots').glob('*/*/*.json'))
def native_orphan(c,t,request,status='COMPLETE',evidence=None):
 return m.QuarterHourMarketCollector._write_collection_result(c,KEY(0),t,request,m.instant(NOW),m.CollectionResult([],status,'fixture',evidence or{}))
def validate_index(c):
 entries=idx.selected_receipt_entries(c.root,symbol='FIXTURE',cutoff=NOW,since=NOW-timedelta(seconds=1),source_ids=None)
 return idx.read_indexed_snapshots(c.root,entries)

@pytest.mark.parametrize('maximum,keys',[(0,[]),(129,[]),(True,[]),(1,['key','key']),(1,['../bad']),(1,['bad/name']),(1,['ok','other']),(1,'key')])
def test_reject_bad_before_io(tmp_path,monkeypatch,maximum,keys):
 c,_,_=make(tmp_path);before=c.state_path.read_bytes();monkeypatch.setattr(m,'FileLock',lambda *a,**k:(_ for _ in()).throw(AssertionError('unexpected IO')))
 with pytest.raises(ValueError):c.expire_tasks(keys,maximum=maximum)
 assert c.state_path.read_bytes()==before and files(c)==[]

@pytest.mark.parametrize('age',[timedelta(hours=47),timedelta(hours=48)])
def test_inclusive_boundary_does_not_collect_or_mutate(tmp_path,age):
 c,_,_=make(tmp_path,age=age);before=c.state_path.read_bytes();r=c.expire_tasks([KEY(0)]);assert r['committed']==0 and r['skipped_ineligible']==[KEY(0)] and c.state_path.read_bytes()==before and not files(c)

def test_nanosecond_strict_boundary(tmp_path):
 c,s,_=make(tmp_path,1);c.clock=lambda:m.instant(END)+__import__('pandas').Timedelta(hours=48,nanoseconds=1);assert c.expire_tasks([KEY(0)])['committed']==1

@pytest.mark.parametrize('status,finalized',[('COMPLETE',False),('EMPTY',False),('EXPIRED',False),('UNSUPPORTED',False),('NOT_AUTHORIZED',False),('CAPACITY_BLOCKED',False),('PARTIAL',True)])
def test_settled_untouched(tmp_path,status,finalized):
 c,s,_=make(tmp_path,1);s['tasks'][KEY(0)].update(status=status,window_finalized=finalized);m._atomic(c.state_path,s);before=c.state_path.read_bytes();assert c.expire_tasks([KEY(0)])['committed']==0 and c.state_path.read_bytes()==before and not files(c)

def test_fresh_future_due_and_missing_key(tmp_path):
 c,s,_=make(tmp_path,1);s['tasks'][KEY(0)]['due_at']=(NOW+timedelta(seconds=1)).isoformat();m._atomic(c.state_path,s);assert c.expire_tasks([KEY(0),'20261020T131500Z-ffffffffffffffffffffffff'])['skipped_ineligible']==sorted([KEY(0),'20261020T131500Z-ffffffffffffffffffffffff']);assert not files(c)

def test_native_index_schema_and_fresh_field_parity(tmp_path):
 c,s,request=make(tmp_path,2);report=c.expire_tasks([KEY(1),KEY(0)]);q=read(c);assert report['committed']==2 and len(validate_index(c))==2
 assert q['source_blocks']==s['source_blocks'] and q['extra']==s['extra']
 for key in [KEY(0),KEY(1)]:
  t=q['tasks'][key];assert t['attempts']==1 and t['status']=='EXPIRED'and t['window_finalized']is False and len(t['snapshots'])==1 and t['due_at']==(NOW+timedelta(seconds=30)).isoformat()
  receipt=json.loads((c.root/t['snapshots'][0]).read_bytes());request=m.SourceRequest(**t['request']);assert receipt['records']==[]and receipt['record_count']==0 and receipt['request_identity']==request.identity and receipt['configuration_sha256']==c.configuration_sha256 and receipt['source_id']==request.source_id and receipt['available_at']==receipt['receipt_at']==receipt['request_started_at']
 assert c.expire_tasks([KEY(0),KEY(1)])['committed']==0 and len(files(c))==2

@pytest.mark.parametrize('status,evidence',[('COMPLETE',{}),('NOT_AUTHORIZED',{}),('UNSUPPORTED',{}),('CAPACITY_BLOCKED',{}),('PARTIAL',{'mode':'bounded_current_symbol_research','bounded_response_complete':True})])
def test_matching_native_orphan_preserves_due_blocks(tmp_path,status,evidence):
 c,s,req=make(tmp_path,1);path,snapshot,_=native_orphan(c,s['tasks'][KEY(0)],req,status,evidence);before=path.read_bytes();r=c.expire_tasks([KEY(0)]);q=read(c)['tasks'][KEY(0)];assert r['committed']==1 and q['status']==snapshot['status'] and q['attempts']==1 and q['due_at']==s['tasks'][KEY(0)]['due_at'] and read(c)['source_blocks']==s['source_blocks'] and path.read_bytes()==before and len(files(c))==1
 assert len(validate_index(c))==1

def test_orphan_already_referenced_attempts_once(tmp_path):
 c,s,req=make(tmp_path,1);path,_,_=native_orphan(c,s['tasks'][KEY(0)],req);s['tasks'][KEY(0)].update(attempts=7,snapshots=[str(path.relative_to(c.root))]);m._atomic(c.state_path,s);c.expire_tasks([KEY(0)]);q=read(c)['tasks'][KEY(0)];assert q['attempts']==7 and len(q['snapshots'])==1

def test_foreign_valid_orphan_does_not_replace_expiry(tmp_path):
 c,s,req=make(tmp_path,1);foreign=m.SourceRequest(provider='fmp',source_kind='fundamentals',symbol='FIXTURE',dataset='other',schema='fixture');path,_,_=native_orphan(c,s['tasks'][KEY(0)],foreign);before=path.read_bytes();assert c.expire_tasks([KEY(0)])['receipts'][0]['status']=='EXPIRED'and path.read_bytes()==before and len(files(c))==2 and len(validate_index(c))==2

def test_reverse_corrupt_orphan_does_not_starve_other_success(tmp_path):
 c,s,req=make(tmp_path,2);path,snap,_=native_orphan(c,s['tasks'][KEY(0)],req);corrupt=dict(snap);corrupt['reason']='changed';bad=path.parent/('z'*32+'.json');bad.write_bytes(m.canonical(corrupt))
 with pytest.raises(ValueError,match='checksum'):c.expire_tasks([KEY(0),KEY(1)])
 q=read(c);assert q['tasks'][KEY(0)]==s['tasks'][KEY(0)]and q['tasks'][KEY(1)]['status']=='EXPIRED';assert len(files(c))==3

@pytest.mark.parametrize('change',['bool_int','remove','other_task'])
def test_fresh_byte_fence_and_unrelated_merge(tmp_path,monkeypatch,change):
 c,s,_=make(tmp_path,2);s['tasks'][KEY(0)]['attempts']=1;m._atomic(c.state_path,s);original=idx.index_receipt
 def hook(root,path,snapshot):
  result=original(root,path,snapshot)
  with c.lock:
   q=read(c)
   if change=='bool_int':q['tasks'][KEY(0)]['attempts']=True
   elif change=='remove':del q['tasks'][KEY(0)]
   else:q['tasks'][KEY(1)]['custom_newer']='preserve'
   q['source_blocks']['newer']={'preserve':1};m._atomic(c.state_path,q)
  return result
 monkeypatch.setattr(idx,'index_receipt',hook);r=c.expire_tasks([KEY(0)]);q=read(c)
 if change!='other_task':assert r['committed']==0 and r['fenced']==[KEY(0)]
 else:assert r['committed']==1 and q['tasks'][KEY(1)]['custom_newer']=='preserve'
 assert q['source_blocks']['newer']=={'preserve':1}

@pytest.mark.parametrize('stage',['before_queue','after_queue','index'])
def test_crash_receipt_retry_idempotency(tmp_path,monkeypatch,stage):
 c,s,_=make(tmp_path,1);original_atomic=m._atomic;original_index=idx.index_receipt
 def atomic(path,state):
  if stage=='after_queue':original_atomic(path,state)
  raise RuntimeError('injected queue crash')
 def index(root,path,snapshot):raise RuntimeError('injected index crash')
 with monkeypatch.context()as patch:
  if stage=='index':patch.setattr(idx,'index_receipt',index)
  else:patch.setattr(m,'_atomic',atomic)
  with pytest.raises(RuntimeError,match='injected'):c.expire_tasks([KEY(0)])
 assert len(files(c))==1
 r=c.expire_tasks([KEY(0)]);q=read(c)['tasks'][KEY(0)];assert q['attempts']==1 and len(q['snapshots'])==1 and q['status']=='EXPIRED'and len(files(c))==1 and len(validate_index(c))==1
 assert r['committed']==(0 if stage=='after_queue'else 1)
 assert q['due_at']==((NOW+timedelta(seconds=30)).isoformat()if stage=='after_queue'else s['tasks'][KEY(0)]['due_at'])

def test_real_task_lock_skip_and_all_owned_through_durable_commit(tmp_path,monkeypatch):
 c,s,_=make(tmp_path,3);held=FileLock(str(c.root/(KEY(0)+'.lock')),timeout=0);original_index=idx.index_receipt;original_atomic=m._atomic;observed=[]
 def assert_owned():
  for key in [KEY(1),KEY(2)]:
   with pytest.raises(Timeout):FileLock(str(c.root/(key+'.lock')),timeout=0).acquire()
  observed.append(True)
 def index(root,path,snapshot):assert_owned();return original_index(root,path,snapshot)
 def atomic(path,state):assert_owned();return original_atomic(path,state)
 monkeypatch.setattr(idx,'index_receipt',index);monkeypatch.setattr(m,'_atomic',atomic)
 with held:r=c.expire_tasks([KEY(2),KEY(0),KEY(1)])
 assert r['skipped_locked']==[KEY(0)]and r['committed']==2 and len(observed)==3
 for key in [KEY(1),KEY(2)]:
  with FileLock(str(c.root/(key+'.lock')),timeout=0):pass

def test_concurrent_collector_cannot_enter_owned_task(tmp_path,monkeypatch):
 c,s,req=make(tmp_path,1);other=m.QuarterHourMarketCollector(tmp_path,symbols=['FIXTURE'],requests=[req],adapters={},clock=lambda:NOW);original=idx.index_receipt;observed=[]
 def hook(root,path,snapshot):
  result=original(root,path,snapshot);observed.append(other.step(max_requests=1));return result
 monkeypatch.setattr(idx,'index_receipt',hook);assert c.expire_tasks([KEY(0)])['committed']==1 and observed==[[]]and len(files(c))==1

def test_subclass_and_instance_override_reject_without_io(tmp_path,monkeypatch):
 class Custom(m.QuarterHourMarketCollector):pass
 c,_,_=make(tmp_path/'subclass',1,collector_class=Custom);d,_,_=make(tmp_path/'instance',1);d._collect=lambda *a:None
 monkeypatch.setattr(m,'FileLock',lambda *a,**k:(_ for _ in()).throw(AssertionError('unexpected IO')))
 for obj in [c,d]:
  with pytest.raises(TypeError,match='unmodified'):obj.expire_tasks([KEY(0)])

def test_queue_io_amortization(tmp_path,monkeypatch):
 c,_,_=make(tmp_path,32);counts={'reads':0,'writes':0};original_state=m.QuarterHourMarketCollector._state;original_atomic=m._atomic
 def state(self):counts['reads']+=1;return original_state(self)
 def atomic(path,value):counts['writes']+=1;return original_atomic(path,value)
 monkeypatch.setattr(m.QuarterHourMarketCollector,'_state',state);monkeypatch.setattr(m,'_atomic',atomic);assert c.expire_tasks([KEY(i) for i in range(32)])['committed']==32;assert counts=={'reads':2,'writes':1}and len(validate_index(c))==32

@pytest.mark.parametrize('key',['/absolute','C:/absolute','bad\\path','bad\x00value','20261320T131500Z-'+'a'*24,'20261020T131501Z-'+'a'*24,'20261020T131600Z-'+'a'*24,'20261020T131500Z-'+'A'*24])
def test_additional_input_rejections_before_locks(tmp_path,monkeypatch,key):
 c,_,_=make(tmp_path,1);monkeypatch.setattr(m,'FileLock',lambda *a,**k:(_ for _ in()).throw(AssertionError('unexpected lock effects')))
 with pytest.raises(ValueError):c.expire_tasks([key])
 assert not files(c)

def test_unbounded_iterable_rejected_without_iteration(tmp_path):
 c,_,_=make(tmp_path,1)
 def bad():raise AssertionError('iterated');yield ''
 with pytest.raises(ValueError):c.expire_tasks(bad())

@pytest.mark.parametrize('interrupt',[KeyboardInterrupt,SystemExit])
def test_interrupt_immediately_unwinds_without_committing_or_other_preparation(tmp_path,monkeypatch,interrupt):
 c,s,_=make(tmp_path,2);before=c.state_path.read_bytes();original=idx.index_receipt
 def hook(root,path,snapshot):original(root,path,snapshot);raise interrupt('injected interruption')
 with monkeypatch.context()as patch:
  patch.setattr(idx,'index_receipt',hook)
  with pytest.raises(interrupt):c.expire_tasks([KEY(0),KEY(1)])
 assert c.state_path.read_bytes()==before and len(files(c))==1 and len(validate_index(c))==1
 for n in range(2):
  with FileLock(str(c.root/(KEY(n)+'.lock')),timeout=0):pass
 assert c.expire_tasks([KEY(0),KEY(1)])['committed']==2

def test_receipt_fsync_has_task_locks_without_global_lock(tmp_path,monkeypatch):
 import os
 c,_,_=make(tmp_path,2);original_fsync=os.fsync;original_atomic=m._atomic;state={'queue_atomic':False,'receipts':0,'queue':0}
 def check_owned():
  for n in range(2):
   with pytest.raises(Timeout):FileLock(str(c.root/(KEY(n)+'.lock')),timeout=0).acquire()
 def fsync(fd):
  check_owned()
  if state['queue_atomic']:assert c.lock.is_locked;state['queue']+=1
  else:assert not c.lock.is_locked;state['receipts']+=1
  return original_fsync(fd)
 def atomic(path,q):
  state['queue_atomic']=True
  try:return original_atomic(path,q)
  finally:state['queue_atomic']=False
 monkeypatch.setattr(os,'fsync',fsync);monkeypatch.setattr(m,'_atomic',atomic);c.expire_tasks([KEY(0),KEY(1)]);assert state=={'queue_atomic':False,'receipts':2,'queue':1}

def test_task_locks_never_acquired_under_global_lock(tmp_path,monkeypatch):
 c,_,_=make(tmp_path,2);original=m.FileLock;seen=[]
 def factory(*args,**kw):assert not c.lock.is_locked;seen.append(args[0]);return original(*args,**kw)
 monkeypatch.setattr(m,'FileLock',factory);c.expire_tasks([KEY(1),KEY(0)]);assert len(seen)==2 and seen==sorted(seen)

def test_unrelated_native_collector_progresses_while_receipts_are_prepared(tmp_path,monkeypatch):
 c,s,req=make(tmp_path,2);other=m.QuarterHourMarketCollector(tmp_path,symbols=['FIXTURE'],requests=[req],adapters={},clock=lambda:NOW);original=idx.index_receipt;observed=[]
 def hook(root,path,snapshot):
  result=original(root,path,snapshot)
  if Path(path).parent.name==KEY(0):
   assert not c.lock.is_locked;observed.extend(other.step(max_requests=1))
  return result
 monkeypatch.setattr(idx,'index_receipt',hook);r=c.expire_tasks([KEY(0)]);q=read(c);assert len(observed)==1 and observed[0]['status']=='EXPIRED'and r['committed']==1 and q['tasks'][KEY(1)]['status']=='EXPIRED'and len(files(c))==2

def test_per_candidate_actual_clocks_and_report_counts(tmp_path):
 c,s,req=make(tmp_path,2);native_orphan(c,s['tasks'][KEY(0)],req);ticks=iter([NOW,NOW+timedelta(seconds=1),NOW+timedelta(seconds=2)]);c.clock=lambda:next(ticks)
 r=c.expire_tasks([KEY(0),KEY(1)]);assert r['recovered']==1 and r['expired']==1 and r['committed']==2 and len(r['committed_keys'])==2;expiry=next(x for x in r['receipts']if x['status']=='EXPIRED');assert expiry['request_started_at']==(NOW+timedelta(seconds=1)).isoformat()and expiry['receipt_at']==(NOW+timedelta(seconds=2)).isoformat()

@pytest.mark.parametrize('change',['request','quarter_start','quarter_end','key_identity'])
def test_wrong_native_binding_cannot_write_receipt_or_queue(tmp_path,change):
 c,s,_=make(tmp_path,1);key=KEY(0);task=s['tasks'][key]
 if change=='request':task['request']['params']['fixture_i']=777
 elif change=='quarter_start':task['quarter_start']=(END-timedelta(minutes=14)).isoformat()
 elif change=='quarter_end':task['quarter_end']=(END+timedelta(minutes=1)).isoformat()
 else:s['tasks'][key[:-24]+'f'*24]=s['tasks'].pop(key);key=key[:-24]+'f'*24
 m._atomic(c.state_path,s);before=c.state_path.read_bytes()
 with pytest.raises(ValueError,match='binding'):c.expire_tasks([key])
 assert c.state_path.read_bytes()==before and files(c)==[]

@pytest.mark.parametrize('damage',['schema','availability','index_identity','missing_clock'])
def test_bad_native_orphan_schema_or_index_never_changes_queue(tmp_path,damage):
 c,state,request=make(tmp_path,1);path,snapshot,_=native_orphan(c,state['tasks'][KEY(0)],request);snapshot=dict(snapshot)
 if damage=='schema':snapshot['schema_version']='wrong'
 elif damage=='availability':snapshot['available_at']=(NOW-timedelta(seconds=1)).isoformat()
 elif damage=='index_identity':snapshot['reason']='changed_since_index'
 else:del snapshot['receipt_at']
 snapshot.pop('snapshot_sha256');snapshot['snapshot_sha256']=m.snapshot_digest(snapshot);path.write_bytes(m.canonical(snapshot));before=c.state_path.read_bytes()
 with pytest.raises((ValueError,KeyError)):c.expire_tasks([KEY(0)])
 assert c.state_path.read_bytes()==before and len(files(c))==1

def test_partial_unfinalized_orphan_cannot_become_recovered_success(tmp_path):
 c,state,request=make(tmp_path,1);path,_,_=native_orphan(c,state['tasks'][KEY(0)],request,status='PARTIAL');before=path.read_bytes();report=c.expire_tasks([KEY(0)]);assert report['expired']==1 and report['recovered']==0 and report['committed']==1 and len(files(c))==2 and path.read_bytes()==before

@pytest.mark.parametrize('attempts,delay',[(3,240),(20,1800)])
def test_attempt_backoff_and_cap_unchanged(tmp_path,attempts,delay):
 c,state,_=make(tmp_path,1);state['tasks'][KEY(0)]['attempts']=attempts;m._atomic(c.state_path,state);c.expire_tasks([KEY(0)]);saved=read(c)['tasks'][KEY(0)];assert saved['attempts']==attempts+1 and saved['due_at']==(NOW+timedelta(seconds=delay)).isoformat()

def test_custom_subclass_regular_step_route_remains_available(tmp_path):
 calls=[]
 class Custom(m.QuarterHourMarketCollector):
  def _collect(self,key,task):calls.append(key);return {'custom':'preserved'}
 c,_,_=make(tmp_path,1,collector_class=Custom);assert c.step(max_requests=1)==[{'custom':'preserved'}]and calls==[KEY(0)]
 with pytest.raises(TypeError):c.expire_tasks([KEY(0)])

def test_backward_receipt_clock_retains_original_error_and_orphan_boundaries(tmp_path):
 c,_,_=make(tmp_path,1);ticks=iter([NOW,NOW-timedelta(seconds=1)]);c.clock=lambda:next(ticks);before=c.state_path.read_bytes()
 with pytest.raises(ValueError,match='clock moved backwards'):c.expire_tasks([KEY(0)])
 assert c.state_path.read_bytes()==before and files(c)==[]

@pytest.mark.parametrize('nonfinite',[float('nan'),float('inf'),float('-inf')])
def test_initial_nonfinite_task_rejected_before_own_receipt_queue_unchanged(tmp_path,nonfinite):
 c,state,_=make(tmp_path,2);state['tasks'][KEY(0)]['custom_nonfinite']=nonfinite
 c.state_path.write_text(json.dumps(state,sort_keys=True,separators=(',',':'),allow_nan=True),encoding='utf-8');before=c.state_path.read_bytes()
 with pytest.raises(ValueError,match='compliant')as caught:c.expire_tasks([KEY(0),KEY(1)])
 assert c.state_path.read_bytes()==before and caught.value.__cause__ is not None
 assert len(files(c))==1 and not(c.root/'snapshots'/state['tasks'][KEY(0)]['quarter_start'][:10]/KEY(0)).exists()
 assert read(c)['tasks'][KEY(1)]['status']=='QUEUED'

@pytest.mark.parametrize('nonfinite',[float('nan'),float('inf'),float('-inf')])
def test_fresh_nonfinite_whole_queue_aborts_and_preserves_bytes(tmp_path,monkeypatch,nonfinite):
 c,state,_=make(tmp_path,2);state['tasks'][KEY(0)]['custom_nonfinite']=None;m._atomic(c.state_path,state);original=idx.index_receipt;changed=[]
 def hook(root,path,snapshot):
  result=original(root,path,snapshot)
  if not changed:
   with c.lock:
    current=read(c);current['tasks'][KEY(0)]['custom_nonfinite']=nonfinite
    c.state_path.write_text(json.dumps(current,sort_keys=True,separators=(',',':'),allow_nan=True),encoding='utf-8');changed.append(c.state_path.read_bytes())
  return result
 monkeypatch.setattr(idx,'index_receipt',hook)
 def forbidden_atomic(*args):raise AssertionError('Invalid fresh queue must never be published')
 monkeypatch.setattr(m,'_atomic',forbidden_atomic)
 with pytest.raises(ValueError,match='compliant'):c.expire_tasks([KEY(0),KEY(1)])
 assert c.state_path.read_bytes()==changed[0]and len(files(c))==2 and len(validate_index(c))==2
 assert all(t['status']=='QUEUED'and t['attempts']==0 and t['snapshots']==[]for t in read(c)['tasks'].values())
 for n in range(2):
  with FileLock(str(c.root/(KEY(n)+'.lock')),timeout=0):pass

@pytest.mark.parametrize('nonfinite',[float('nan'),float('inf'),float('-inf')])
def test_unselected_nonfinite_whole_queue_prevents_silent_normalization(tmp_path,nonfinite):
 c,state,_=make(tmp_path,2);state['unselected_private_field']=nonfinite;c.state_path.write_text(json.dumps(state,allow_nan=True),encoding='utf-8');before=c.state_path.read_bytes()
 with pytest.raises(ValueError,match='compliant'):c.expire_tasks([KEY(0)])
 assert c.state_path.read_bytes()==before and len(files(c))==1 and len(validate_index(c))==1

@pytest.mark.parametrize('badmap',[{},[],{'other':'a'*64},{'replace-key':None},{'replace-key':'A'*64},{'replace-key':'a'*63},{'replace-key':1}])
def test_reviewed_task_bad_mapping_rejects_before_io(tmp_path,monkeypatch,badmap):
 c,_,_=make(tmp_path,1);badmap=dict(badmap)if type(badmap)is dict else badmap
 if type(badmap)is dict and'replace-key'in badmap:badmap[KEY(0)]=badmap.pop('replace-key')
 monkeypatch.setattr(m,'FileLock',lambda *a,**k:(_ for _ in()).throw(AssertionError('unexpected lock IO')))
 with pytest.raises(ValueError,match='SHA256'):c.expire_tasks([KEY(0)],reviewed_tasks=badmap)
 assert files(c)==[]

def reviewed_hash(task):
 import hashlib
 return hashlib.sha256(json.dumps(task,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()

def test_matching_reviewed_hashes_commit_native_receipts(tmp_path):
 c,state,_=make(tmp_path,2);reviewed={k:reviewed_hash(t)for k,t in state['tasks'].items()};report=c.expire_tasks([KEY(0),KEY(1)],reviewed_tasks=reviewed);assert report['committed']==2 and report['fenced']==[]and len(validate_index(c))==2

def test_changed_before_initial_read_fenced_before_own_receipt(tmp_path):
 c,state,_=make(tmp_path,2);reviewed={k:reviewed_hash(t)for k,t in state['tasks'].items()};state['tasks'][KEY(0)]['new_writer_field']='preserve';m._atomic(c.state_path,state);report=c.expire_tasks([KEY(0),KEY(1)],reviewed_tasks=reviewed);current=read(c)
 assert report['fenced']==[KEY(0)]and report['committed_keys']==[KEY(1)]and current['tasks'][KEY(0)]==state['tasks'][KEY(0)]and len(files(c))==1
 assert not(c.root/'snapshots'/state['tasks'][KEY(0)]['quarter_start'][:10]/KEY(0)).exists()


def test_reviewed_hash_mapping_is_frozen_at_entry(tmp_path,monkeypatch):
 c,state,_=make(tmp_path,1);reviewed={KEY(0):reviewed_hash(state['tasks'][KEY(0)])};original=m.FileLock;changed=[]
 def factory(*args,**kwargs):
  if not changed:
   state['tasks'][KEY(0)]['later_change']='must_fence';m._atomic(c.state_path,state);reviewed[KEY(0)]=reviewed_hash(state['tasks'][KEY(0)]);changed.append(True)
  return original(*args,**kwargs)
 monkeypatch.setattr(m,'FileLock',factory);report=c.expire_tasks([KEY(0)],reviewed_tasks=reviewed)
 assert report['fenced']==[KEY(0)]and report['committed']==0 and files(c)==[]
