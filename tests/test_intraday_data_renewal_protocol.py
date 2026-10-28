"""Real local subprocesses exercise only the data continuation protocol."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
PYTHON = getattr(sys, "_base_executable", sys.executable)  # Sealed layout uses the real interpreter, not a venv launcher.
from tools import intraday_data_renewal as protocol

WORKER = r"""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from tools import intraday_data_renewal as p
mode=sys.argv[2]
if '--continue' in sys.argv:
    handoff=Path(sys.argv[sys.argv.index('--continue')+1])
    if mode.startswith('badterminal'):
        h=json.loads(handoff.read_bytes());e=json.loads(Path(h['envelope']).read_bytes())
        p.atomic_json(handoff.with_suffix('.claim.json'),{'handoff_id':h['handoff_id'],'pid':os.getpid(),'birth':p.birth(os.getpid())})
        p.atomic_json(handoff.with_suffix('.result.json'),{'protocol':p.PROTOCOL,'token':h['token'],'handoff_id':h['handoff_id'],
            'controller_pid':os.getpid(),'controller_birth':p.birth(os.getpid()),'disposition':'WRONG' if mode=='badterminal-disposition' else 'COMPLETED',
            'generations':h['turns'],'completed_roles':h['done'],'session':{} if mode=='badterminal-session' else e['session'],'failure':None})
        raise SystemExit(0)
    argv=[sys.executable,'-B',__file__,sys.argv[1],mode]
    raise SystemExit(p.resume_controller(handoff,command=lambda role,request:[*argv,str(request)],controller_identity='new'))
r=p.Renewal(sys.argv[3]); role=r.request['role']
call_dir=Path(r.envelope['datastore'])/'calls';call_dir.mkdir(exist_ok=True)
(call_dir/(role+'-'+r.request['turn']+'.json')).write_text(json.dumps({'role':role,'pid':os.getpid(),'state':r.state,'session':r.session,'at':time.time()}))
if mode=='unknown' and role!='initialize':raise SystemExit(9)
if mode=='badreceipt' and role!='initialize':
    p.atomic_json(r.request['response'],{'protocol':p.PROTOCOL,'token':'bad','turn':r.request['turn'],'role':role,'worker_pid':os.getpid(),'disposition':'PLANNED_RENEWAL','state':{},'next_poll_at':None})
    raise SystemExit(75)
if mode=='plainzero' and role!='initialize':raise SystemExit(0)
if role=='initialize':
    r.finish({'session':{**r.session,'started_at':'2026-10-28T10:55:00+00:00','close_at':'2026-10-29T00:00:00+00:00','original':'frozen','actor':'Atlas','symbols':['AAPL','IONQ'],'config':{'symbol_workers':2}},'roles':['fast','slow']})
    raise SystemExit(0)
if mode=='stop':
    p.atomic_json(r.envelope['stop_path'],{'reason':'MANUAL_STOP'})
    raise SystemExit(0)
if role=='slow':time.sleep(.6);r.finish({'status':'COMPLETED'});raise SystemExit(0)
count=r.state.get('count',0)+1
if count==3:r.finish({'status':'COMPLETED'});raise SystemExit(0)
try:r.checkpoint({'count':count,'reference_at':'2026-10-28T21:30:00+00:00'},None)
except p.PlannedRenewal:raise SystemExit(p.PLANNED_EXIT)
"""


def fixture(tmp_path):
    script=tmp_path/'worker.py';script.write_text(WORKER)
    root=str(Path(protocol.__file__).resolve().parents[1])
    lifetime='data-fixture'
    directory=tmp_path/'state/intraday-data-renewal'/lifetime
    envelope={'lifetime_id':lifetime,'datastore':str(tmp_path),'session':{'session_id':'data-atlas-1a:fixture'}}
    def commands(mode):return lambda role,request:[PYTHON,'-B',str(script),root,mode,str(request)]
    return script,root,directory,envelope,commands


def calls(tmp_path):
    return sorted((json.loads(p.read_bytes()) for p in (tmp_path/'calls').glob('*.json')),key=lambda x:x['at'])


def test_real_fresh_pids_and_slow_lane_does_not_block_fast_completed_iterations(tmp_path):
    script,root,directory,envelope,commands=fixture(tmp_path)
    controls=tmp_path/'intraday/manual-session.json';controls.parent.mkdir();controls.write_bytes(b'original1Bstop')
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=commands('normal'),poll_seconds=.005)==0
    observed=calls(tmp_path);fast=[x for x in observed if x['role']=='fast'];slow=[x for x in observed if x['role']=='slow']
    assert len(fast)==3 and len({x['pid'] for x in fast})==3 and len(slow)==1
    assert [x['state'].get('count',0) for x in fast]==[0,1,2]
    assert fast[-1]['at']<slow[0]['at']+.6
    assert all(x['session']['started_at']=='2026-10-28T10:55:00+00:00' for x in fast)
    assert controls.read_bytes()==b'original1Bstop'
    assert json.loads((directory/'outcome.json').read_bytes())['disposition']=='COMPLETED'
    status=json.loads((tmp_path/'intraday/system-1a/status.json').read_bytes())
    assert status['status']=='EXITED' and status['session_id']=='data-atlas-1a:fixture'
    assert status['loaded_source_scope']=='controller_only' and status['symbols']==['AAPL','IONQ']
    assert status['configuration']=={'symbol_workers':2} and status['manual_trader_stop_dependency'] is False


@pytest.mark.parametrize('mode',['unknown','badreceipt','plainzero'])
def test_unknown_exit_missing_receipt_and_wrong_identity_never_restart(tmp_path,mode):
    _,_,directory,envelope,commands=fixture(tmp_path)
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=commands(mode),poll_seconds=.005)==1
    observed=calls(tmp_path)
    assert len([x for x in observed if x['role']=='fast'])==1
    assert json.loads((directory/'outcome.json').read_bytes())['disposition']=='FAILED'


def test_stop_between_iterations_latches_entire_data_lifetime_without_touching_trading(tmp_path):
    _,_,directory,envelope,commands=fixture(tmp_path)
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=commands('stop'),poll_seconds=.005)==0
    observed=calls(tmp_path)
    assert len([x for x in observed if x['role']=='fast'])==1
    assert json.loads((directory/'outcome.json').read_bytes())['disposition']=='MANUAL_STOP'


def test_controller_source_relay_preserves_original_owner_session_and_checkpoint(tmp_path):
    script,root,directory,envelope,commands=fixture(tmp_path)
    switched=[]
    def phase(kind,role):
        if kind=='handoff' and role=='fast':switched.append(True)
    def selector():
        return {'identity':'new' if switched else 'old','command':[PYTHON,'-B',str(script),root,'normal']}
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=commands('normal'),poll_seconds=.005,
        controller_identity='old',relay_selector=selector,phase=phase)==0
    handoff=list(directory.glob('controller-handoff-*.json'));handoff=[p for p in handoff if not any(x in p.name for x in ('.claim.','.result.'))]
    assert len(handoff)==1
    saved=json.loads(handoff[0].read_bytes());terminal=json.loads(handoff[0].with_suffix('.result.json').read_bytes())
    session=json.loads((directory/'session.json').read_bytes())
    assert terminal['handoff_id']==saved['handoff_id'] and terminal['controller_pid']!=os.getpid()
    assert session['owner_pid']==os.getpid() and terminal['session']['original']=='frozen'
    fast=[x for x in calls(tmp_path) if x['role']=='fast']
    assert [x['state'].get('count',0) for x in fast]==[0,1,2]


def test_duplicate_live_data_lock_refuses_and_matching_owner_releases_only_own_bytes(tmp_path):
    with protocol.own_data_session(tmp_path):
        original=(tmp_path/'locks/intraday-system-1a.lock').read_bytes()
        with pytest.raises((RuntimeError,OSError)):
            with protocol.own_data_session(tmp_path):pass
        assert (tmp_path/'locks/intraday-system-1a.lock').read_bytes()==original
    assert not (tmp_path/'locks/intraday-system-1a.lock').exists()


def test_stale_data_lock_is_retained_before_new_owned_lifetime(tmp_path,monkeypatch):
    lock=tmp_path/'locks/intraday-system-1a.lock';lock.parent.mkdir();raw=b'process=intraday-system-1a\npid=99999999\n';lock.write_bytes(raw)
    real=protocol.birth;monkeypatch.setattr(protocol,'birth',lambda pid:None if pid==99999999 else real(pid))
    with protocol.own_data_session(tmp_path):
        assert lock.read_bytes()!=raw
    assert [p.read_bytes() for p in lock.parent.glob('*.stale-*')]==[raw]


@pytest.mark.skipif(os.name!='nt',reason='Windows kernel job semantics')
def test_closing_kernel_anchor_removes_child_and_grandchild(tmp_path):
    script=tmp_path/'anchor.py';root=str(Path(protocol.__file__).resolve().parents[1]);output=tmp_path/'pids.json'
    script.write_text("import sys,subprocess,os,time,json\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom tools import intraday_data_renewal as p\np.bind_kernel_lifetime()\nchild=subprocess.Popen([sys.executable,'-c',\"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)\"],stdout=subprocess.PIPE,text=True)\nPath(sys.argv[2]).write_text(json.dumps([child.pid,int(child.stdout.readline())]))\ntime.sleep(60)\n")
    anchor=subprocess.Popen([PYTHON,'-B',str(script),root,str(output)],creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        until=time.monotonic()+15
        while not output.exists():
            assert time.monotonic()<until;time.sleep(.02)
        children=json.loads(output.read_bytes());assert all(protocol.birth(pid) is not None for pid in children)
        anchor.terminate();anchor.wait(timeout=10)
        until=time.monotonic()+10
        while any(protocol.birth(pid) is not None for pid in children):
            assert time.monotonic()<until;time.sleep(.02)
    finally:
        if anchor.poll() is None:anchor.terminate();anchor.wait(timeout=10)


def test_fresh_authorized_lifetime_replaces_ended_status_without_reusing_stop(tmp_path):
    _,_,directory,envelope,commands=fixture(tmp_path)
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=commands('stop'),poll_seconds=.005)==0
    before=json.loads((tmp_path/'intraday/system-1a/status.json').read_bytes())
    original_stop=(directory/'stop.json').read_bytes()
    second=dict(envelope,lifetime_id='data-second',session={'session_id':'data-atlas-1a:second'})
    second_dir=tmp_path/'state/intraday-data-renewal/data-second'
    assert protocol.run_protocol(directory=second_dir,envelope=second,command=commands('normal'),poll_seconds=.005)==0
    after=json.loads((tmp_path/'intraday/system-1a/status.json').read_bytes())
    assert before['renewal_token']!=after['renewal_token'] and after['session_id']=='data-atlas-1a:second'
    assert after['status']=='EXITED' and (directory/'stop.json').read_bytes()==original_stop
    with pytest.raises(FileExistsError):
        protocol.run_protocol(directory=directory,envelope=envelope,command=commands('normal'))


def test_stop_at_completed_handoff_does_not_admit_another_iteration(tmp_path):
    _,_,directory,envelope,commands=fixture(tmp_path)
    def phase(kind,role):
        if kind=='handoff' and role=='fast':protocol.atomic_json(directory/'stop.json',{'reason':'MANUAL_STOP'})
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=commands('normal'),phase=phase,poll_seconds=.005)==0
    assert len([x for x in calls(tmp_path) if x['role']=='fast'])==1
    assert json.loads((directory/'outcome.json').read_bytes())['disposition']=='MANUAL_STOP'


def test_unrelated_process_cannot_consume_a_living_parents_handoff(tmp_path):
    _,_,directory,envelope,commands=fixture(tmp_path);directory.mkdir(parents=True)
    token='original-token';birth=protocol.birth(os.getpid())
    saved={**envelope,'protocol':protocol.PROTOCOL,'token':token,'trading_authority':False,
        'owner_pid':os.getpid(),'owner_birth':birth,'stop_path':str(directory/'stop.json')}
    protocol.atomic_json(directory/'session.json',saved)
    handoff=directory/'handoff.json'
    protocol.atomic_json(handoff,{'protocol':protocol.PROTOCOL,'token':token,'envelope':str(directory/'session.json'),
        'target_identity':'new','parent_pid':os.getpid(),'parent_birth':birth,'handoff_id':'single-use'})
    with pytest.raises(ValueError,match='exact living owner'):
        protocol.resume_controller(handoff,command=commands('normal'),controller_identity='new')
    assert not handoff.with_suffix('.claim.json').exists()


@pytest.mark.parametrize('mode',['badterminal-disposition','badterminal-session'])
def test_invalid_relay_terminal_cannot_complete_original_lifetime(tmp_path,mode):
    script,root,directory,envelope,commands=fixture(tmp_path);changed=[]
    def phase(kind,role):
        if kind=='handoff' and role=='fast':changed.append(True)
    def selector():return {'identity':'new' if changed else 'old','command':[PYTHON,'-B',str(script),root,mode]}
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=commands('normal'),
        controller_identity='old',relay_selector=selector,phase=phase,poll_seconds=.005)==1
    assert json.loads((directory/'outcome.json').read_bytes())['disposition']=='FAILED'


@pytest.mark.parametrize('format',['exact','legacy-created','legacy-started'])
def test_reused_pid_reclaims_only_proven_original_data_owner(tmp_path,monkeypatch,format):
    lock=tmp_path/'locks/intraday-system-1a.lock';lock.parent.mkdir()
    recorded={'pid':'424242','process':'intraday-system-1a'}
    if format=='exact':recorded['owner_birth']='123'
    elif format=='legacy-created':recorded['owner_created_at']='1000.000000'
    else:recorded['started_at']='2020-01-01T00:00:00+00:00'
    raw=('\n'.join(k+'='+v for k,v in recorded.items())+'\n').encode();lock.write_bytes(raw)
    real_birth=protocol.birth;real_created=protocol.created_at
    monkeypatch.setattr(protocol,'birth',lambda pid:'456' if pid==424242 else real_birth(pid))
    monkeypatch.setattr(protocol,'created_at',lambda value:1700000000.0 if value=='456' else real_created(value))
    with protocol.own_data_session(tmp_path):assert lock.read_bytes()!=raw
    assert [p.read_bytes() for p in lock.parent.glob('*.stale-*')]==[raw]


@pytest.mark.parametrize('recorded',['1000.000000','1000.0000005'])
def test_legacy_live_owner_float_precision_does_not_authorize_reclaim(tmp_path,monkeypatch,recorded):
    lock=tmp_path/'locks/intraday-system-1a.lock';lock.parent.mkdir();raw=('pid=424242\nowner_created_at='+recorded+'\n').encode();lock.write_bytes(raw)
    monkeypatch.setattr(protocol,'birth',lambda pid:'same-live')
    monkeypatch.setattr(protocol,'created_at',lambda value:1000.0)
    with pytest.raises(RuntimeError,match='live or unproven'):
        with protocol.own_data_session(tmp_path):pass
    assert lock.read_bytes()==raw and not list(lock.parent.glob('*.stale-*'))
