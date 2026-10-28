"""Data-only lifetime and exact completed-iteration handoffs.

No trading imports, controls, order state or scheduler admission live here.
Only a matching durable receipt and planned exit renew an admitted lane.
"""
from __future__ import annotations
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

PROTOCOL = "intraday-data-renewal-v1"
PLANNED_EXIT = 75

class PlannedRenewal(BaseException):
    pass


def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.partial')
    try:
        with temporary.open('x',encoding='utf-8',newline='\n') as stream:
            json.dump(value,stream,sort_keys=True,allow_nan=False);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


_JOB = None

def birth(pid):
    # Kept within the small stdlib anchor so editable application imports cannot
    # enter the parent. Representation matches runtime_lock's psutil timestamp.
    if os.name != "nt":
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    k.OpenProcess.restype = w.HANDLE
    k.GetProcessTimes.argtypes = (w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4))
    k.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.OpenProcess(0x1000, False, int(pid))
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise RuntimeError("Cannot inspect existing session owner")
    try:
        values = [w.FILETIME() for _ in range(4)]
        code = w.DWORD()
        if not k.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)) or not k.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise RuntimeError("Cannot verify existing session owner")
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime) if code.value == 259 else None
    finally:
        k.CloseHandle(handle)

def created_at(identity):
    """Convert our exact OS birth identity only for reviewed legacy locks."""
    if os.name=='nt':return int(identity)/10000000-11644473600
    started=int(next(line.split()[1] for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime ')))
    return started+int(identity)/os.sysconf('SC_CLK_TCK')


def original_owner_gone(owner,pid):
    current=birth(pid)
    if current is None:return True
    if 'owner_birth' in owner:
        recorded=owner['owner_birth']
        if not isinstance(recorded,str) or not recorded.isdigit():return False
        return current!=recorded
    try:
        actual=created_at(current)
        if 'owner_created_at' in owner:
            original=float(owner['owner_created_at'])
            return math.isfinite(original) and original>0 and abs(actual-original)>.001
        started=datetime.fromisoformat(owner['started_at'].replace('Z','+00:00'))
        if started.tzinfo is None or started.utcoffset() is None:return False
        original=started.timestamp()
        return (math.isfinite(original) and 0<original<=datetime.now(timezone.utc).timestamp()
                and actual>original+.001)
    except (ValueError,KeyError,OverflowError,OSError,StopIteration):
        return False  # Unavailable or malformed legacy evidence is not PID reuse.


def bind_kernel_lifetime():
    """Assign this anchor before spawning anything; all descendants inherit it.

    The non-inheritable job handle exists only here. Abrupt anchor termination
    or console closure kills every descendant, including virtualenv launchers.
    """
    global _JOB
    if os.name != "nt" or _JOB is not None:
        return
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [("ProcessTime", c.c_longlong), ("JobTime", c.c_longlong),
                    ("Flags", w.DWORD), ("Minimum", c.c_size_t), ("Maximum", c.c_size_t),
                    ("Active", w.DWORD), ("Affinity", c.c_size_t), ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_ulonglong) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
    class Limits(c.Structure):
        _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", c.c_size_t),
                    ("JobMemory", c.c_size_t), ("PeakProcess", c.c_size_t), ("PeakJob", c.c_size_t)]
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = (c.c_void_p, w.LPCWSTR)
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = (w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
    k.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
    k.GetCurrentProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.CreateJobObjectW(None, None)
    limits = Limits()
    limits.Basic.Flags = 0x2000
    if not handle or not k.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)) or not k.AssignProcessToJobObject(handle, k.GetCurrentProcess()):
        error = c.get_last_error()
        if handle:
            k.CloseHandle(handle)
        raise OSError(error, "Cannot establish kernel data lifetime; no worker started")
    _JOB = handle

@contextlib.contextmanager
def byte_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()

@contextlib.contextmanager
def own_data_session(root):
    """Retain the existing 1A lock across all worker gaps; never adopt a PID."""
    root=Path(root); target=root/'locks/intraday-system-1a.lock'
    target.parent.mkdir(parents=True,exist_ok=True)
    with byte_lock(target.with_name(target.name+'.renewal-owner')):
        with byte_lock(root/'locks/.ducketz-runtime-lock-maintenance.lock'):
            if target.exists():
                raw=target.read_bytes()
                try:
                    pairs=[line.split('=',1) for line in raw.decode().splitlines()]
                    owner=dict(pairs)
                    if len(owner)!=len(pairs):raise ValueError('Duplicate lock fields')
                    pid=int(owner['pid'])
                    if pid<=0:raise ValueError('Invalid lock PID')
                except (ValueError,KeyError,UnicodeError):
                    raise RuntimeError('Cannot establish existing data-service ownership')
                if not original_owner_gone(owner,pid):
                    raise RuntimeError('A live or unproven data service already owns this lifetime')
                archive=target.with_name(target.name+'.stale-'+uuid.uuid4().hex)
                with archive.open('xb') as stream:stream.write(raw)
                if target.read_bytes()!=raw:raise RuntimeError('Data owner changed during stale-lock review')
                target.unlink()
            token=uuid.uuid4().hex
            raw=('process=intraday-system-1a\npid='+str(os.getpid())+'\nstarted_at='+datetime.now(timezone.utc).isoformat()+'\nowner_birth='+str(birth(os.getpid()))+'\nowner_created_at='+str(created_at(birth(os.getpid())))+'\ntoken='+token+'\n').encode()
            with target.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try:yield
        finally:
            with byte_lock(root/'locks/.ducketz-runtime-lock-maintenance.lock'):
                if target.exists() and target.read_bytes()==raw:target.unlink()


class Renewal:
    def __init__(self, request):
        self.request_path=Path(request).resolve()
        self.request=json.loads(self.request_path.read_bytes())
        self.envelope_path=Path(self.request['envelope']).resolve()
        self.envelope=json.loads(self.envelope_path.read_bytes())
        expected=Path(self.envelope['datastore']).resolve()/'state/intraday-data-renewal'/self.envelope['lifetime_id']
        if (self.envelope.get('protocol')!=PROTOCOL or self.envelope.get('trading_authority') is not False
                or self.envelope_path!=expected/'session.json' or self.request_path.parent!=expected
                or Path(self.request['response']).resolve()!=self.request_path.with_suffix('.response.json')
                or self.request.get('token')!=self.envelope.get('token')):
            raise ValueError('Invalid data-only continuation identity')
        self.session=self.envelope['session'];self.state=self.request.get('state',{})
        self.next_poll_at=self.request.get('next_poll_at')
        self.claim=self.request_path.with_suffix('.claim.json')
        with self.claim.open('x',encoding='utf-8') as stream:
            json.dump({'pid':os.getpid(),'birth':birth(os.getpid()),'turn':self.request['turn'],'token':self.request['token']},stream)
        if self.stopped():raise InterruptedError('Data lifetime ended before worker admission')

    def stopped(self):
        if Path(self.envelope['stop_path']).exists():return True
        return birth(self.envelope['owner_pid'])!=self.envelope['owner_birth']

    def wait_due(self, *, clock=lambda:datetime.now(timezone.utc), sleep=time.sleep):
        if not self.next_poll_at:return
        due=datetime.fromisoformat(str(self.next_poll_at).replace('Z','+00:00'))
        while not self.stopped():
            now=clock();now=datetime.fromisoformat(now.replace('Z','+00:00')) if isinstance(now,str) else now
            remaining=(due-now).total_seconds()
            if remaining<=0:return
            sleep(min(.1,remaining))

    def _receipt(self, disposition, **fields):
        if self.stopped():raise InterruptedError('Data lifetime stopped before handoff')
        atomic_json(self.request['response'],{'protocol':PROTOCOL,'token':self.request['token'],
            'turn':self.request['turn'],'role':self.request['role'],'disposition':disposition,
            'worker_pid':os.getpid(),**fields})

    def checkpoint(self,state,next_poll_at):
        self._receipt('PLANNED_RENEWAL',state=state,next_poll_at=str(next_poll_at) if next_poll_at is not None else None)
        raise PlannedRenewal()

    def finish(self,result):self._receipt('COMPLETED',result=result)


def resume_controller(handoff, *, command, controller_identity, relay_selector=None, phase=lambda *a:None, controller_generation=None):
    """Continue exactly one live parent's sealed handoff; never new admission."""
    handoff=Path(handoff).resolve();payload=json.loads(handoff.read_bytes())
    envelope_path=Path(payload['envelope']).resolve();envelope=json.loads(envelope_path.read_bytes())
    directory=envelope_path.parent
    expected=Path(envelope['datastore']).resolve()/'state/intraday-data-renewal'/envelope['lifetime_id']
    if (payload.get('protocol')!=PROTOCOL or payload.get('token')!=envelope.get('token')
            or payload.get('target_identity')!=controller_identity or directory!=expected
            or handoff.parent!=directory or os.getppid()!=payload['parent_pid']
            or birth(payload['parent_pid'])!=payload['parent_birth']
            or birth(envelope['owner_pid'])!=envelope['owner_birth']
            or Path(envelope['stop_path']).exists()):
        raise ValueError('Data controller continuation lost its exact living owner')
    with handoff.with_suffix('.claim.json').open('x',encoding='utf-8') as stream:
        json.dump({'handoff_id':payload['handoff_id'],'pid':os.getpid(),'birth':birth(os.getpid())},stream)
    return run_protocol(directory=directory,envelope=envelope,command=command,continuation=payload,
        controller_identity=controller_identity,relay_selector=relay_selector,phase=phase,
        terminal_path=handoff.with_suffix('.result.json'),controller_generation=controller_generation)


def run_protocol(*, directory, envelope, command, poll_seconds=.05, phase=lambda *a:None,
                 continuation=None, controller_identity=None, relay_selector=None, terminal_path=None, controller_generation=None):
    """Independent lanes, fresh selection on each exact acknowledged completion."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    session_path=directory/'session.json'
    if continuation is None:
        if session_path.exists():raise FileExistsError('Data lifetime already has an original envelope')
        envelope=dict(envelope,protocol=PROTOCOL,owner_pid=os.getpid(),owner_birth=birth(os.getpid()),
                      token=uuid.uuid4().hex,trading_authority=False,stop_path=str(directory/'stop.json'))
        atomic_json(session_path,envelope)
    active_path=Path(envelope['datastore'])/'state/intraday-data-renewal/active.json'
    active={**envelope,'status':'RUNNING','controller_pid':os.getpid()};atomic_json(active_path,active)
    roles=['initialize'] if continuation is None else list(continuation['roles'])
    states={} if continuation is None else dict(continuation['states'])
    turns={} if continuation is None else dict(continuation['turns'])
    done=set() if continuation is None else set(continuation['done'])
    children={};reason=None;failure=None;relay_target=None;relay_child=None;relayed_terminal=False
    previous={}
    loaded_generations={} if continuation is None else dict(continuation.get('loaded_generations',{}))
    status_path=Path(envelope['datastore'])/'intraday/system-1a/status.json'
    last_status=0.0
    def _record_status(status):
        nonlocal last_status
        if 'actor' not in envelope['session']:
            return  # Initializer has not frozen a service yet.
        for role,(child,response,turn) in children.items():
            request=response.with_name(response.name.replace('.response.json','.json'))
            loaded_path=request.with_suffix('.loaded.json')
            if loaded_path.exists():
                value=json.loads(loaded_path.read_bytes())
                if (value.get('turn')==turn and value.get('role')==role and value.get('worker_pid')==child.pid
                        and value.get('request_sha256')==hashlib.sha256(request.read_bytes()).hexdigest()):
                    loaded_generations[role]={'generation':value['generation'],'runtime_id':value['runtime_id'],
                        'worker_pid':value['worker_pid'],'turn':turn}
        if status_path.exists():
            old=json.loads(status_path.read_bytes())
            if old.get('renewal_token') not in (None,envelope['token']):
                # A fresh authorized lifetime may replace an ended predecessor;
                # a different live data owner keeps its diagnostic surface.
                ended=old.get('status') in {'EXITED','FAILED','ENDED'}
                old_pid=old.get('pid')
                gone=isinstance(old_pid,int) and birth(old_pid) is None
                if not ended and not gone:
                    raise RuntimeError('Independent data status ownership changed')
        session=envelope['session']
        passes={role:value.get('state',{}).get('last_pass') for role,value in states.items() if value.get('state',{}).get('last_pass') is not None}
        close=max([session['close_at'],*[value.get('state',{}).get('data_close_at',session['close_at']) for value in states.values()]])
        value={'schema_version':'intraday-system-1a-service-v1','actor':session['actor'],
            'session_id':session['session_id'],'started_at':session['started_at'],'symbols':session['symbols'],
            'configuration':session['config'],'daily_data_wake':session.get('daily_data_wake'),
            'data_weekdays':session.get('data_weekdays',[]),'new_preparation_close_at':close,
            'status':status,'heartbeat_at':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),
            'loaded_source_generation':controller_generation,'loaded_source_scope':'controller_only',
            'worker_loaded_generations':loaded_generations,'renewal_token':envelope['token'],
            'trading_authority':False,'manual_trader_stop_dependency':False,'consumer_acknowledgement_required':False,
            'latest_pass':{'status':'INDEPENDENT_RENEWABLE_LANES','in_flight_roles':list(children),
                'completed_passes':passes,'completed_roles':sorted(done)}}
        atomic_json(status_path,value);last_status=time.monotonic()
    def record_status(status):
        nonlocal last_status
        try:
            _record_status(status)
        except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
            # Diagnostic storage is not a new prerequisite for acquisition.
            print('Data service status unavailable: '+type(exc).__name__,flush=True)
            last_status=time.monotonic()
    def stop(why):
        nonlocal reason
        if reason is None:
            reason=why;atomic_json(directory/'stop.json',{'reason':why,'token':envelope['token'],'at':datetime.now(timezone.utc).isoformat()})
    def signalled(*args):stop('MANUAL_STOP')
    for sig in set((signal.SIGINT,signal.SIGTERM,getattr(signal,'SIGBREAK',signal.SIGTERM))):
        previous[sig]=signal.signal(sig,signalled)
    try:
        while reason is None:
            if birth(envelope["owner_pid"]) != envelope["owner_birth"]:
                stop("FAILED"); break
            if (directory/'stop.json').exists():
                stop('MANUAL_STOP');break
            if (relay_selector is not None and relay_target is None and roles!=['initialize']
                    and any(role not in done and role not in children for role in roles)):
                candidate=relay_selector()
                if candidate['identity']!=controller_identity:
                    relay_target=candidate;phase('controller_draining','controller')
            for role in roles:
                if role in done or role in children or relay_target is not None:continue
                index=turns.get(role,0)+1;turns[role]=index;turn=uuid.uuid4().hex
                request=directory/(role+'-'+str(index)+'-'+turn+'.json');response=request.with_suffix('.response.json')
                atomic_json(request,{'envelope':str(session_path),'token':envelope['token'],'role':role,
                    'generation':index,'turn':turn,'response':str(response),**states.get(role,{})})
                argv=command(role,request)
                if (directory/'stop.json').exists():stop('MANUAL_STOP');break
                child=subprocess.Popen(argv,close_fds=True)
                children[role]=(child,response,turn);phase('started',role)
            if reason is not None:break
            for role,(child,response,turn) in list(children.items()):
                code=child.poll()
                if code is None:continue
                del children[role];phase('exited',role)
                if (directory/'stop.json').exists():stop('MANUAL_STOP');break
                value=json.loads(response.read_bytes())
                if (value.get('protocol'),value.get('token'),value.get('turn'),value.get('role'),value.get('worker_pid'))!=(PROTOCOL,envelope['token'],turn,role,child.pid):
                    raise ValueError('Data handoff receipt identity mismatch')
                if code==PLANNED_EXIT and value['disposition']=='PLANNED_RENEWAL' and role!='initialize':
                    states[role]={'state':value['state'],'next_poll_at':value['next_poll_at']};phase('handoff',role)
                elif code==0 and value['disposition']=='COMPLETED':
                    if role=='initialize':
                        result=value['result'];envelope['session']=result['session'];roles=result['roles']
                        if not roles or len(set(roles))!=len(roles) or 'initialize' in roles:raise ValueError('Invalid data lane ownership')
                        atomic_json(session_path,envelope)
                    else:done.add(role)
                else:raise RuntimeError('Unacknowledged data worker exit '+str(code))
                atomic_json(active_path,{**active,'session':envelope['session'],'heartbeat_at':datetime.now(timezone.utc).isoformat(),
                    'generations':turns,'completed_roles':sorted(done)})
            if time.monotonic()-last_status>=1:record_status('RUNNING')
            if all(role in done for role in roles):reason='COMPLETED';break
            if relay_target is not None and not children:
                # A full current pass has exited in every admitted lane. The old
                # controller retains kernel/lock ownership and becomes an anchor.
                relay_target=relay_selector()
                if relay_target['identity']==controller_identity:
                    relay_target=None;continue
                handoff=directory/('controller-handoff-'+uuid.uuid4().hex+'.json')
                payload={'protocol':PROTOCOL,'token':envelope['token'],'envelope':str(session_path),
                    'handoff_id':uuid.uuid4().hex,'parent_pid':os.getpid(),'parent_birth':birth(os.getpid()),
                    'target_identity':relay_target['identity'],'roles':roles,'states':states,'turns':turns,'done':sorted(done),'loaded_generations':loaded_generations}
                atomic_json(handoff,payload);phase('controller_handoff','controller')
                if (directory/'stop.json').exists():stop('MANUAL_STOP');break
                relay_child=subprocess.Popen([*relay_target['command'],'--continue',str(handoff)],close_fds=True)
                relay_birth=birth(relay_child.pid)
                while relay_child.poll() is None and reason is None:
                    if (directory/'stop.json').exists():stop('MANUAL_STOP');break
                    time.sleep(poll_seconds)
                if reason is not None:break
                value=json.loads(handoff.with_suffix('.result.json').read_bytes())
                claim=json.loads(handoff.with_suffix('.claim.json').read_bytes())
                if (value.get('protocol'),value.get('token'),value.get('handoff_id'),value.get('controller_pid'),claim.get('pid'))!=(PROTOCOL,envelope['token'],payload['handoff_id'],relay_child.pid,relay_child.pid):
                    raise ValueError('Data controller terminal identity mismatch')
                if (value.get('disposition') not in {'COMPLETED','MANUAL_STOP','FAILED'}
                        or value.get('controller_birth')!=relay_birth or claim.get('birth')!=relay_birth
                        or value.get('session')!=envelope['session']
                        or not isinstance(value.get('generations'),dict)
                        or any(type(count) is not int or count<turns.get(role,0)
                               for role,count in value['generations'].items())
                        or not set(turns)<=set(value['generations'])<=set(roles)|{'initialize'}
                        or not isinstance(value.get('completed_roles'),list)
                        or not done<=set(value['completed_roles'])<=set(roles)
                        or value.get('failure') is not None and not isinstance(value['failure'],dict)):
                    raise ValueError('Data controller terminal state changed original ownership')
                expected=1 if value['disposition']=='FAILED' else 0
                if relay_child.returncode!=expected:raise ValueError('Data controller terminal exit mismatch')
                reason=value['disposition'];turns=value['generations'];done=set(value['completed_roles']);failure=value['failure']
                relayed_terminal=True
                phase('controller_relay_complete','controller');break
            time.sleep(poll_seconds)
    except BaseException as exc:
        failure={'error_type':type(exc).__name__};stop('FAILED')
    finally:
        owned=[child for child,_,_ in children.values()]
        if relay_child is not None:owned.append(relay_child)
        if owned:
            if reason is None:stop('FAILED')
            deadline=time.monotonic()+10
            while any(child.poll() is None for child in owned) and time.monotonic()<deadline:time.sleep(.05)
            for child in owned:
                if child.poll() is None:child.terminate()
            for child in owned:child.wait()
        for sig,handler in previous.items():signal.signal(sig,handler)
        result={'protocol':PROTOCOL,'token':envelope['token'],'controller_pid':os.getpid(),
            'controller_birth':birth(os.getpid()),'handoff_id':continuation['handoff_id'] if continuation else None,'disposition':reason,
            'generations':turns,'completed_roles':sorted(done),'session':envelope['session'],'failure':failure}
        if not relayed_terminal:record_status('FAILED' if reason=='FAILED' else 'EXITED')
        atomic_json(directory/'outcome.json',result)
        if terminal_path is not None:atomic_json(terminal_path,result)
        if not relayed_terminal:
            atomic_json(active_path,{**active,'session':envelope['session'],'status':'ENDED','disposition':reason,'failure':failure})
    return 1 if reason=='FAILED' else 0
