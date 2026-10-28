"""Synthetic, network-free terminal diagnostics; no live process is started."""
import errno
import json
import socket
from pathlib import Path

import pytest

from tools import intraday_data_renewal as protocol


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args,**kwargs):
        raise AssertionError('Network is forbidden in offline diagnostics fixtures')
    monkeypatch.setattr(socket,'socket',denied)
    monkeypatch.setattr(socket,'create_connection',denied)


def synthetic_service(tmp_path,monkeypatch):
    calls=[]
    directory=tmp_path/'state/intraday-data-renewal/synthetic'
    envelope={'lifetime_id':'synthetic','datastore':str(tmp_path),
              'session':{'session_id':'data-atlas-1a:synthetic'}}
    class Child:
        def __init__(self,argv,**kwargs):
            role,request=argv;request=Path(request);r=json.loads(request.read_bytes())
            self.pid=70000+len(calls);calls.append((role,r['generation']))
            value={'protocol':protocol.PROTOCOL,'token':r['token'],'turn':r['turn'],
                   'role':role,'worker_pid':self.pid}
            if role=='initialize':
                session={**envelope['session'],'actor':'Atlas','symbols':['SYNTH'],
                         'config':{'symbol_workers':1},'started_at':'2026-10-28T10:55:00+00:00',
                         'close_at':'2026-10-29T00:00:00+00:00'}
                value.update(disposition='COMPLETED',result={'session':session,'roles':['prep-0']})
                self.returncode=0
            elif r['generation']==1:
                value.update(disposition='PLANNED_RENEWAL',state={'count':1},next_poll_at=None)
                self.returncode=protocol.PLANNED_EXIT
            else:
                assert r['state']=={'count':1}
                value.update(disposition='COMPLETED',result={'status':'COMPLETED'})
                self.returncode=0
            protocol.atomic_json(r['response'],value)
        def poll(self):return self.returncode
        def wait(self):return self.returncode
        def terminate(self):raise AssertionError('Completed fake process must not be terminated')
    monkeypatch.setattr(protocol.subprocess,'Popen',Child)
    monkeypatch.setattr(protocol,'birth',lambda pid:'synthetic-birth')
    command=lambda role,request:[role,str(request)]
    return directory,envelope,command,calls


def terminal_records(tmp_path,directory):
    return [json.loads(p.read_bytes()) for p in [directory/'outcome.json',
        tmp_path/'state/intraday-data-renewal/active.json',
        tmp_path/'intraday/system-1a/status.json']]


def test_response_read_and_active_replace_permission_errors_are_distinct(tmp_path,monkeypatch):
    results=[]
    for mode in ('read','replace'):
        root=tmp_path/mode
        with monkeypatch.context() as patch:
            directory,envelope,command,calls=synthetic_service(root,patch)
            original_read=Path.read_bytes;original_replace=protocol.os.replace
            injected=[]
            def read(path):
                if mode=='read' and path.name.startswith('prep-0-1-') and path.name.endswith('.response.json') and not injected:
                    injected.append(True);raise PermissionError(errno.EACCES,'PRIVATE MESSAGE',str(root/'PRIVATE ACCOUNT'))
                return original_read(path)
            def replace(source,destination):
                if mode=='replace' and Path(destination).name=='active.json' and calls==[('initialize',1),('prep-0',1)] and not injected:
                    injected.append(True);raise PermissionError(errno.EACCES,'PRIVATE MESSAGE',str(root/'PRIVATE ACCOUNT'))
                return original_replace(source,destination)
            patch.setattr(Path,'read_bytes',read);patch.setattr(protocol.os,'replace',replace)
            assert protocol.run_protocol(directory=directory,envelope=envelope,command=command,
                poll_seconds=0,controller_generation='a'*64)==1
            records=terminal_records(root,directory);failures=[r['failure'] for r in records]
            assert failures[0]==failures[1]==failures[2]
            failure=failures[0];results.append(failure)
            assert failure['error_type']=='PermissionError'
            assert failure['role']=='prep-0' and failure['generation']==1 and len(failure['turn'])==32
            assert failure['controller_generation']=='a'*64
            assert failure['exceptions'][0]['errno']==errno.EACCES
            assert records[0]['disposition']=='FAILED' and records[1]['disposition']=='FAILED' and records[2]['status']=='FAILED'
            assert calls==[('initialize',1),('prep-0',1)]  # No next turn or restart.
            encoded=json.dumps(failure);assert 'PRIVATE' not in encoded and str(root) not in encoded
    assert results[0]['operation']=='worker_receipt_read'
    assert 'atomic_stage' not in results[0]['exceptions'][0]
    assert results[1]['operation']=='active_checkpoint_publish'
    assert results[1]['exceptions'][0]['atomic_stage']=='replace'


def test_successful_completed_handoff_keeps_state_and_no_failure(tmp_path,monkeypatch):
    directory,envelope,command,calls=synthetic_service(tmp_path,monkeypatch)
    controls=tmp_path/'intraday/manual-session.json';controls.parent.mkdir(parents=True)
    controls.write_bytes(b'preserve original 1B manual stop')
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=command,poll_seconds=0)==0
    assert calls==[('initialize',1),('prep-0',1),('prep-0',2)]
    records=terminal_records(tmp_path,directory)
    assert records[0]['generations']=={'initialize':1,'prep-0':2}
    assert records[0]['disposition']=='COMPLETED' and all(r['failure'] is None for r in records)
    assert controls.read_bytes()==b'preserve original 1B manual stop'


def test_manual_stop_after_handoff_does_not_admit_successor(tmp_path,monkeypatch):
    directory,envelope,command,calls=synthetic_service(tmp_path,monkeypatch)
    def phase(kind,role):
        if (kind,role)==('handoff','prep-0'):
            protocol.atomic_json(directory/'stop.json',{'reason':'MANUAL_STOP'})
    assert protocol.run_protocol(directory=directory,envelope=envelope,command=command,
        phase=phase,poll_seconds=0)==0
    assert calls==[('initialize',1),('prep-0',1)]
    records=terminal_records(tmp_path,directory)
    assert records[0]['disposition']=='MANUAL_STOP' and all(r['failure'] is None for r in records)


def test_exception_chain_respects_explicit_cause_context_and_suppression():
    original=OSError(errno.EBUSY,'PRIVATE ERROR','PRIVATE PATH');original.winerror=32
    try:
        try:raise original
        except OSError:raise RuntimeError('PRIVATE WRAPPER')
    except RuntimeError as wrapper:
        value=protocol.failure_diagnostics(wrapper,{'operation':'worker_receipt_read'})
    assert [x['relation'] for x in value['exceptions']]==['raised','context']
    assert value['exceptions'][1]['errno']==errno.EBUSY and value['exceptions'][1]['winerror']==32
    try:
        try:raise original
        except OSError:raise ValueError('PRIVATE WRAPPER') from None
    except ValueError as wrapper:
        hidden=protocol.failure_diagnostics(wrapper,{'operation':'worker_receipt_read'})
    assert len(hidden['exceptions'])==1
    try:raise RuntimeError('PRIVATE WRAPPER') from original
    except RuntimeError as wrapper:
        explicit=protocol.failure_diagnostics(wrapper,{'operation':'worker_receipt_read'})
    assert [x['relation'] for x in explicit['exceptions']]==['raised','cause']
    assert 'PRIVATE' not in json.dumps([value,hidden,explicit])


def test_chain_cycles_and_depth_are_bounded():
    first=ValueError('PRIVATE');second=RuntimeError('PRIVATE');first.__cause__=second;second.__cause__=first
    value=protocol.failure_diagnostics(first,{'operation':'worker_receipt_decode'})
    assert len(value['exceptions'])==2 and value['chain_truncated']=='cycle'
    chain=[RuntimeError('PRIVATE') for _ in range(10)]
    for a,b in zip(chain,chain[1:]):a.__cause__=b
    value=protocol.failure_diagnostics(chain[0],{'operation':'worker_receipt_decode'})
    assert len(value['exceptions'])==4 and value['chain_truncated']=='limit'


def test_traceback_uses_only_bounded_basename_function_and_line():
    namespace={};exec(compile("def recurse(n):\n if n: return recurse(n-1)\n raise OSError(13,'PRIVATE MESSAGE','PRIVATE FILE')\n",'C:/PRIVATE DIRECTORY/source.py','exec'),namespace)
    try:namespace['recurse'](12)
    except OSError as exc:value=protocol.failure_diagnostics(exc,{'operation':'x'*200,'role':'r'*200,'secret':'PRIVATE'})
    item=value['exceptions'][0]
    assert len(item['frames'])==6 and item['frames_omitted']>=7
    assert all(f['source']=='source.py' and f['function']=='recurse' and type(f['line']) is int for f in item['frames'])
    assert len(value['operation'])==80 and len(value['role'])==64
    assert 'PRIVATE' not in json.dumps(value) and 'secret' not in value


@pytest.mark.parametrize('stage',['temporary_open','replace','cleanup'])
def test_atomic_failure_preserves_exception_object_and_fixed_substage(tmp_path,monkeypatch,stage):
    original_open=Path.open;original_replace=protocol.os.replace;original_unlink=Path.unlink
    error=PermissionError(errno.EACCES,'PRIVATE MESSAGE',str(tmp_path/'PRIVATE'))
    def opening(path,*args,**kwargs):
        if stage=='temporary_open' and path.name.endswith('.partial'):raise error
        return original_open(path,*args,**kwargs)
    def replacing(source,destination):
        if stage=='replace':raise error
        return original_replace(source,destination)
    def unlinking(path,*args,**kwargs):
        if stage=='cleanup' and path.name.endswith('.partial'):raise error
        return original_unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',opening);monkeypatch.setattr(protocol.os,'replace',replacing);monkeypatch.setattr(Path,'unlink',unlinking)
    with pytest.raises(PermissionError) as captured:protocol.atomic_json(tmp_path/'synthetic.json',{})
    assert captured.value is error
    value=protocol.failure_diagnostics(captured.value,{'operation':'active_checkpoint_publish'})
    assert value['exceptions'][0]['atomic_stage']==stage and 'PRIVATE' not in json.dumps(value)


def test_atomic_cleanup_error_retains_original_replace_exception_context(tmp_path,monkeypatch):
    first=PermissionError(errno.EACCES,'PRIVATE REPLACE');second=PermissionError(errno.EPERM,'PRIVATE CLEANUP')
    def replacing(*args):raise first
    def unlinking(*args,**kwargs):raise second
    monkeypatch.setattr(protocol.os,'replace',replacing);monkeypatch.setattr(Path,'unlink',unlinking)
    with pytest.raises(PermissionError) as captured:protocol.atomic_json(tmp_path/'synthetic.json',{})
    assert captured.value is second and second.__context__ is first
    value=protocol.failure_diagnostics(second,{'operation':'active_checkpoint_publish'})
    assert [x['atomic_stage'] for x in value['exceptions']]==['cleanup','replace']
    assert 'PRIVATE' not in json.dumps(value)


def test_numeric_errors_exclude_bool_and_bad_custom_attributes():
    class Unreadable(OSError):
        def __getattribute__(self,key):
            if key=='winerror':raise RuntimeError('PRIVATE ATTRIBUTE')
            if key in ('__traceback__','_intraday_data_atomic_stage'):return []
            return super().__getattribute__(key)
    exc=Unreadable('PRIVATE');exc.errno=True
    value=protocol.failure_diagnostics(exc,{'operation':'worker_receipt_read'})
    assert 'errno' not in value['exceptions'][0] and 'winerror' not in value['exceptions'][0]
    assert 'PRIVATE' not in json.dumps(value)
