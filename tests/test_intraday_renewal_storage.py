"""Portable cold-storage tests; only temporary native SQLite and stdlib callbacks."""
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from contextlib import contextmanager
import importlib.util, json, socket, sqlite3
import pytest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'ml/intraday/renewal_storage.py'
spec=importlib.util.spec_from_file_location('tested_renewal_storage',SOURCE)
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
NOW=datetime(2026,10,27,12,16,17,tzinfo=timezone.utc)
CLOSE=datetime(2026,10,28,tzinfo=timezone.utc)
class ContractError(ValueError):pass

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
 def deny(*a,**k):raise AssertionError('Network forbidden in cold-init fixture')
 monkeypatch.setattr(socket,'create_connection',deny);monkeypatch.setattr(socket,'getaddrinfo',deny)
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def renewal():return SimpleNamespace(stopped=lambda:False,session={'session_id':'original-fixture'},state={'original':'cursor'})
def run(action, *, role='scoring', renew=None, clock=lambda:NOW, pause=lambda n:None):
 r=renew or renewal()
 return helper.retry_renewal_storage_setup(action,role=role,stopped=r.stopped,permitted=lambda:role=='candles' or clock()<CLOSE,pause=pause)

def err(code=None,cls=sqlite3.OperationalError):
 e=cls('private sentinel never printed')
 if code is not None:
  e.sqlite_errorcode=code
  e.sqlite_errorname=next((name for name in ('SQLITE_BUSY','SQLITE_BUSY_RECOVERY','SQLITE_BUSY_SNAPSHOT','SQLITE_BUSY_TIMEOUT','SQLITE_LOCKED','SQLITE_LOCKED_SHAREDCACHE','SQLITE_FULL','SQLITE_IOERR','SQLITE_READONLY','SQLITE_CORRUPT') if getattr(sqlite3,name,None)==code),'SQLITE_TEST_UNKNOWN')
 return e

@pytest.mark.parametrize('mode',['busy','locked'])
def test_native_sqlite_contention_releases_original_operation_without_output_changes(tmp_path,mode):
 path=tmp_path/'private.sqlite3';uri=path.as_uri()+'?cache=shared' if mode=='locked' else str(path)
 def connect():return sqlite3.connect(uri,uri=mode=='locked',timeout=0)
 holder=connect();holder.execute('PRAGMA journal_mode=WAL');holder.execute('CREATE TABLE protected(id TEXT PRIMARY KEY,payload TEXT)')
 holder.execute("INSERT INTO protected VALUES('original','original immutable bytes')");holder.commit()
 if mode=='locked':
  holder.execute('BEGIN');holder.execute('SELECT * FROM protected').fetchall()
 else:holder.execute('BEGIN IMMEDIATE')
 calls=[];connections=[]
 def setup():
  calls.append(True);db=connect();connections.append(db)
  try:
   with db:
    db.executescript('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS protected(id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE IF NOT EXISTS queued(id TEXT);')
    db.execute('BEGIN IMMEDIATE')
    return 'complete-original-result'
  finally:db.close()
 try:
  with pytest.raises(sqlite3.OperationalError) as caught:setup()
  assert caught.value.sqlite_errorcode & 255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)
  pauses=[]
  def pause(n):pauses.append(n);holder.rollback()
  assert run(setup,pause=pause)=='complete-original-result'
  assert calls==[True,True,True] and pauses==[1]
  for db in connections:
   with pytest.raises(sqlite3.ProgrammingError):db.execute('SELECT 1')
  db=connect()
  try:
   assert db.execute('SELECT * FROM protected').fetchall()==[('original','original immutable bytes')]
   assert db.execute('SELECT count(*) FROM queued').fetchone()==(0,)
  finally:db.close()
 finally:holder.rollback();holder.close()

@pytest.mark.parametrize('code',[sqlite3.SQLITE_BUSY,sqlite3.SQLITE_BUSY_RECOVERY,sqlite3.SQLITE_BUSY_SNAPSHOT,sqlite3.SQLITE_BUSY_TIMEOUT,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_LOCKED_SHAREDCACHE])
def test_reconstructed_extended_native_code_families_retry_same_action(code,capsys):
 calls=[];r=renewal();identity=json.dumps([r.session,r.state],sort_keys=True)
 def action():
  calls.append(True)
  if len(calls)==1:raise err(code)
  return 'same-original-result'
 pauses=[];assert run(action,renew=r,pause=pauses.append)=='same-original-result'
 assert len(calls)==2 and pauses==[1] and json.dumps([r.session,r.state],sort_keys=True)==identity
 output=capsys.readouterr().out
 assert str(code) in output and 'private sentinel' not in output

@pytest.mark.parametrize('failure',[err(sqlite3.SQLITE_FULL),err(sqlite3.SQLITE_IOERR),err(sqlite3.SQLITE_READONLY),err(sqlite3.SQLITE_CORRUPT),err(),err(True),err(sqlite3.SQLITE_BUSY,sqlite3.DatabaseError),ContractError('original-contract'),OSError('original-os')])
def test_other_failure_exact_object_propagates_without_retry(failure):
 calls=[];pauses=[]
 def action():calls.append(True);raise failure
 with pytest.raises(type(failure)) as caught:run(action,pause=pauses.append)
 assert caught.value is failure and calls==[True] and not pauses

@pytest.mark.parametrize('control',['already-stopped','stop-during-pause','cutoff-during-pause'])
def test_stop_and_original_cutoff_prevent_second_setup(control):
 stopped=[control=='already-stopped'];clock=[NOW];calls=[];pauses=[];r=renewal();r.stopped=lambda:stopped[0]
 def action():calls.append(True);raise err(sqlite3.SQLITE_BUSY_RECOVERY)
 def pause(n):
  pauses.append(n)
  if control=='stop-during-pause':stopped[0]=True
  else:clock[0]=CLOSE
 with pytest.raises(InterruptedError):run(action,renew=r,clock=lambda:clock[0],pause=pause)
 assert calls==([] if control=='already-stopped' else[True])
 assert pauses==([] if control=='already-stopped' else[1])

def test_candles_existing_postclose_policy_and_manual_stop():
 calls=[];assert run(lambda:calls.append(True)or'complete',role='candles',clock=lambda:CLOSE)=='complete'
 r=renewal();r.stopped=lambda:True
 with pytest.raises(InterruptedError):run(lambda:calls.append(True),role='candles',renew=r,clock=lambda:CLOSE)
 assert calls==[True]

def test_no_retry_is_no_extra_action_or_pause():
 calls=[];pauses=[];value=object();assert run(lambda:calls.append(True)or value,pause=pauses.append)is value
 assert calls==[True] and not pauses


@pytest.mark.parametrize('control',['manual-stop','original-cutoff'])
def test_successful_setup_rechecks_stop_and_original_deadline_before_return(control):
 r=renewal();stopped=[False];current=[NOW];r.stopped=lambda:stopped[0];calls=[]
 def action():
  calls.append(True)
  if control=='manual-stop':stopped[0]=True
  else:current[0]=CLOSE
  return 'completed-local-storage'
 with pytest.raises(InterruptedError):run(action,renew=r,clock=lambda:current[0])
 assert calls==[True]

