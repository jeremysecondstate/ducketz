"""Controller integration using actual isolated Windows process generations.

The only worker is a small fake iteration adapter. Its imports are the lifecycle
and release validators plus private fixture modules. No application entrypoint,
model fitting, broker/provider, or production datastore is opened.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from tools.scout_iteration_controller import Controller
from tools.scout_process import ManagedWorker
from tools.scout_release import PROTOCOL, atomic_json, select_release, sha256


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Actual Windows process generation contract")
ROOT = Path(__file__).resolve().parents[1]


FAKE_WORKER = r'''
import importlib.util, json, pathlib, sqlite3, sys, time
sys.path.insert(0, sys.argv[3])
from tools.scout_iteration_controller import WorkerLifecycle, PLANNED_EXIT
from tools.scout_release import atomic_json, validate_release
from datafetching.runtime_lock import exclusive_runtime_lock

request=json.loads(pathlib.Path(sys.argv[1]).read_bytes())
plan=json.loads(pathlib.Path(sys.argv[2]).read_bytes())
root=pathlib.Path(plan['root'])
lifecycle=WorkerLifecycle(request)
generation=request['generation']
selected=validate_release(request['release']['release_root'],request['release']['manifest_sha256'])
spec=importlib.util.spec_from_file_location('fixture_version',pathlib.Path(selected['source_root'])/'fixture_version.py')
version=importlib.util.module_from_spec(spec);spec.loader.exec_module(version)
atomic_json(root/('startup-%s.json'%generation),{'generation':generation,'loaded':version.VERSION,**lifecycle.header()})

def finish(status,code,**extra):
    atomic_json(pathlib.Path(request['generation_root'])/'result.json',
                {'status':status,'authority_failure':getattr(lifecycle,'stop_detail',None),**extra,**lifecycle.header()})
    raise SystemExit(code)

if plan.get('stop_startup_generation')==generation:
    deadline=time.monotonic()+10
    while not lifecycle.stop_reason():
        if time.monotonic()>deadline: raise RuntimeError('Test stop never arrived')
        time.sleep(.01)
    finish('STOPPED',0,stop_reason=lifecycle.stop_reason())

lifecycle.ready()
fault_mode=plan.get('authority_fault')
if fault_mode:
    # Force the exact transition observed in the isolated failure: ready has
    # been published, and the parent now replaces STARTING with RUNNING.
    native_read_bytes=pathlib.Path.read_bytes
    active=pathlib.Path(request['active_path'])
    until=time.monotonic()+10
    while True:
        try:
            if json.loads(native_read_bytes(active))['status']=='RUNNING': break
        except PermissionError:
            pass
        if time.monotonic()>until: raise RuntimeError('Fixture parent never published RUNNING')
        time.sleep(.01)
    fault={'reads':0}
    def authority_read(path):
        if path==active:
            fault['reads']+=1
            if fault['reads']==1:
                atomic_json(root/'authority-fault-started.json',{'mode':fault_mode})
            if fault_mode=='stale_once' and fault['reads']==1:
                stale=json.loads(native_read_bytes(path)); stale['token']='retired-generation-token'
                return json.dumps(stale).encode()
            if fault_mode in {'permanent_denial','manual_stop_during_denial'} or (fault_mode=='transient_denial' and fault['reads']<=2):
                error=PermissionError(13,'private fixture controller read sharing denial')
                # CPython's Windows CRT file open can report errno EACCES
                # without winerror, as observed during actual atomic replace.
                if plan.get('authority_winerror',32) is not None:
                    error.winerror=plan['authority_winerror'] if 'authority_winerror' in plan else 32
                raise error
        return native_read_bytes(path)
    pathlib.Path.read_bytes=authority_read
    begin=time.monotonic()
    first_reason=lifecycle.stop_reason()
    first_reads=fault['reads']
    second_reason=lifecycle.stop_reason()
    elapsed=time.monotonic()-begin
    atomic_json(root/'authority-check.json',{'first':first_reason,'second':second_reason,
        'first_reads':first_reads,'total_reads':fault['reads'],'elapsed':elapsed,
        'detail':getattr(lifecycle,'stop_detail',None)})
    if first_reason or second_reason:
        reason=first_reason or second_reason
        manual=reason in {'manual_stop','session_end'}
        finish('STOPPED' if manual else 'FAILED',0 if manual else 1,stop_reason=reason,
               authority_failure=getattr(lifecycle,'stop_detail',None))
with exclusive_runtime_lock(root/'fixture-effects.lock',process_name='private-fixture-effects'):
    with (root/'intervals.jsonl').open('a') as f:
        f.write(json.dumps({'event':'begin','generation':generation,'version':version.VERSION,'time':time.time(),**lifecycle.header()})+'\n')
    if generation==1 and plan.get('wait_for_release_switch'):
        deadline=time.monotonic()+10
        while not (root/'release-switched').exists():
            if lifecycle.stop_reason(): finish('STOPPED',0,stop_reason=lifecycle.stop_reason())
            if time.monotonic()>deadline: raise RuntimeError('Test release switch never arrived')
            time.sleep(.01)
    if lifecycle.stop_reason(): finish('STOPPED',0,stop_reason=lifecycle.stop_reason())
    with sqlite3.connect(root/'fixture-ledger.sqlite3') as db:
        # The fake adapter uses durable identities, never process-local ones.
        # Replay the predecessor's fake delivery to prove its identity survives.
        for n in sorted(set([max(1,generation-1),generation])):
            for symbol in selected['symbols']:
                if lifecycle.stop_reason(): finish('STOPPED',0,stop_reason=lifecycle.stop_reason())
                db.execute('INSERT OR IGNORE INTO effects VALUES(?,?,?)',
                           (f'cycle-{n}:{symbol}',n,symbol))
        assert db.execute('SELECT payload FROM orders WHERE id=?',('unchanged-working-order',)).fetchone()[0]==plan['order_payload']
    with (root/'intervals.jsonl').open('a') as f:
        f.write(json.dumps({'event':'end','generation':generation,'time':time.time(),**lifecycle.header()})+'\n')
lifecycle.phase('draining')
lifecycle.checkpoint({'schema':'scout-iteration-checkpoint-v1','owned_workers_drained':True,
    'boundary_kind':'full_symbol_attempt_with_durable_deferrals','boundary_cycle_id':f'cycle-{generation}',
    'cycles':[{'cycle_id':f'cycle-{generation}','status':'completed','symbols':selected['symbols']}],
    'completed_cycle_cursor':generation})
if generation==plan.get('final_generation'):
    atomic_json(request['stop_path'],{'reason':'manual_stop','session_id':request['session_id']})
finish('PLANNED_RESTART',PLANNED_EXIT)
'''


def release(tmp_path, name):
    release_root = tmp_path / ("release-" + name)
    source = release_root / "source"
    (source / "datafetching").mkdir(parents=True)
    (source / "fixture_version.py").write_text("VERSION=" + repr(name) + "\n")
    (source / "datafetching/runtime_lock.py").write_bytes((ROOT / "datafetching/runtime_lock.py").read_bytes())
    recipe_path = release_root / "recipe.json"
    atomic_json(recipe_path, {"symbols": [f"S{n:02}" for n in range(11)]})
    manifest = {"source_root": str(source), "lifecycle_protocol": PROTOCOL,
                "original_repository": str(tmp_path), "recipe_path": str(recipe_path),
                "recipe_file_sha256": sha256(recipe_path.read_bytes()),
                "files": {p.relative_to(source).as_posix(): sha256(p.read_bytes())
                          for p in source.rglob("*") if p.is_file()}}
    atomic_json(release_root / "manifest.json", manifest)
    return release_root, sha256((release_root / "manifest.json").read_bytes())


def prepare(tmp_path, **plan_values):
    first, second = release(tmp_path, "A"), release(tmp_path, "B")
    selector = tmp_path / "selected.json"
    select_release(selector, *first)
    fake = tmp_path / "fake_worker.py"
    fake.write_text(FAKE_WORKER)
    payload = json.dumps({"broker_order_id": "fixture-123", "state": "PARTIAL",
                          "filled": 3, "reserved": 7, "unknown_send": "fixture-uncertain-id"}, sort_keys=True)
    with sqlite3.connect(tmp_path / "fixture-ledger.sqlite3") as db:
        db.executescript("CREATE TABLE effects(id TEXT PRIMARY KEY,cycle INTEGER,symbol TEXT);"
                         "CREATE TABLE orders(id TEXT PRIMARY KEY,payload TEXT);")
        db.execute("INSERT INTO orders VALUES(?,?)", ("unchanged-working-order", payload))
    plan_path = tmp_path / "fixture-plan.json"
    atomic_json(plan_path, {"root": str(tmp_path), "order_payload": payload, **plan_values})
    instances = []

    def factory(command, **kwargs):
        assert command[-2] == "--worker"
        child = ManagedWorker([sys.executable, "-B", str(fake), command[-1], str(plan_path), str(ROOT)], **kwargs)
        instances.append(child)
        return child

    now = datetime.now(timezone.utc)
    controller = Controller(selector=selector, repository=tmp_path, datastore=tmp_path / "datastore",
        wake_at=now, deadline=now + timedelta(minutes=2), worker_factory=factory,
        startup_timeout=15, drain_timeout=5, backoff_seconds=0.02)
    return controller, instances, selector, first, second, payload


def test_actual_generations_reload_b_then_rotate_unchanged_b_without_overlap(tmp_path):
    controller, children, selector, first, second, original = prepare(
        tmp_path, final_generation=3, wait_for_release_switch=True)
    switched = False
    duplicate_rejected = False

    def observe_sleep(seconds):
        nonlocal switched, duplicate_rejected
        startup = tmp_path / "startup-1.json"
        if startup.exists() and not switched:
            record = json.loads(startup.read_bytes())
            assert record["loaded"] == "A"
            assert record["release"]["manifest_sha256"] == first[1]
            # The actual A worker remains alive while installation commits B.
            assert children[0].poll() is None
            select_release(selector, *second, expected_selection_sha256=sha256(selector.read_bytes()))
            switched = True
            (tmp_path / "release-switched").touch()
        if switched and not duplicate_rejected:
            duplicate = Controller(selector=selector, repository=tmp_path, datastore=controller.datastore,
                wake_at=controller.state["wake_at"], deadline=controller.state["deadline"])
            with pytest.raises(RuntimeError, match="already running|lock|owns|active|Another"):
                duplicate.run()
            duplicate_rejected = True
        time.sleep(seconds)

    controller.sleep = observe_sleep
    assert controller.run() == 0
    assert switched and duplicate_rejected
    assert len(children) == 3
    assert all(child.job_empty() and child.poll() is not None for child in children)
    rows = [json.loads(line) for line in (tmp_path / "intervals.jsonl").read_text().splitlines()]
    assert [(r["event"], r["generation"]) for r in rows] == [
        ("begin", 1), ("end", 1), ("begin", 2), ("end", 2), ("begin", 3), ("end", 3)]
    assert [r["version"] for r in rows if r["event"] == "begin"] == ["A", "B", "B"]
    assert len({r["session_id"] for r in rows}) == 1
    assert len({r["worker_pid"] for r in rows}) == 3
    with sqlite3.connect(tmp_path / "fixture-ledger.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 33
        assert db.execute("SELECT payload FROM orders").fetchone()[0] == original
    requests = [json.loads((controller.root / str(n) / "request.json").read_bytes()) for n in (1, 2, 3)]
    assert len({r["started_at"] for r in requests}) == 1
    assert len({r["deadline"] for r in requests}) == 1
    assert [r["previous_checkpoint"]["completed_cycle_cursor"] for r in requests[1:]] == [1, 2]
    assert requests[1]["release"]["manifest_sha256"] == requests[2]["release"]["manifest_sha256"] == second[1]


def test_stop_during_real_successor_startup_prevents_effects_and_next_generation(tmp_path):
    controller, children, *_ = prepare(tmp_path, stop_startup_generation=2)
    stop_sent = False

    def stop_at_startup(seconds):
        nonlocal stop_sent
        if not stop_sent and (tmp_path / "startup-2.json").exists():
            controller.request_stop()
            stop_sent = True
        time.sleep(seconds)

    controller.sleep = stop_at_startup
    assert controller.run() == 0
    assert stop_sent and len(children) == 2
    assert all(child.job_empty() for child in children)
    with sqlite3.connect(tmp_path / "fixture-ledger.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 11
    state = json.loads(controller.active_path.read_bytes())
    assert state["status"] == "STOPPED" and state["stop_reason"] == "manual_stop"
    assert controller.stop_path.exists()
    assert not (controller.root / "3").exists()


@pytest.mark.parametrize("winerror", [32, None])
def test_actual_parent_running_transition_survives_bounded_transient_read_denial(tmp_path, winerror):
    controller, children, *_ = prepare(tmp_path, authority_fault="transient_denial",
                                      authority_winerror=winerror, final_generation=1)
    assert controller.run() == 0
    evidence = json.loads((tmp_path / "authority-check.json").read_bytes())
    assert evidence["first"] is None and evidence["second"] is None
    assert evidence["first_reads"] == 3 and evidence["total_reads"] == 4
    assert evidence["elapsed"] < 2
    with sqlite3.connect(tmp_path / "fixture-ledger.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 11
    assert len(children) == 1 and children[0].job_empty()


@pytest.mark.parametrize("winerror", [32, None])
def test_manual_stop_during_native_authority_read_retry_prevents_any_effect(tmp_path, winerror):
    controller, children, *_ = prepare(tmp_path, authority_fault="manual_stop_during_denial",
                                      authority_winerror=winerror)
    stop_sent = False

    def stop_during_read(seconds):
        nonlocal stop_sent
        if not stop_sent and (tmp_path / "authority-fault-started.json").exists():
            controller.request_stop()
            stop_sent = True
        time.sleep(seconds)

    controller.sleep = stop_during_read
    assert controller.run() == 0
    evidence = json.loads((tmp_path / "authority-check.json").read_bytes())
    assert stop_sent and evidence["first"] == evidence["second"] == "manual_stop"
    assert evidence["first_reads"] == evidence["total_reads"]
    assert evidence["elapsed"] < 2
    with sqlite3.connect(tmp_path / "fixture-ledger.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    assert len(children) == 1 and children[0].job_empty()
    assert not (controller.root / "2").exists()


@pytest.mark.parametrize("fault,winerror", [("stale_once", 32), ("permanent_denial", 32),
                                          ("permanent_denial", None)])
def test_real_worker_revocation_stays_latched_and_reports_exact_failure(tmp_path, fault, winerror):
    controller, children, *_ = prepare(tmp_path, authority_fault=fault, authority_winerror=winerror)
    with pytest.raises(RuntimeError, match="did not complete a planned boundary"):
        controller.run()
    evidence = json.loads((tmp_path / "authority-check.json").read_bytes())
    assert evidence["first"] == evidence["second"] == "controller_failure"
    assert evidence["first_reads"] == evidence["total_reads"]
    assert evidence["elapsed"] < 2
    if fault == "permanent_denial":
        assert evidence["detail"]["stage"] == "authority_read"
        assert evidence["detail"]["error_type"] == "PermissionError"
        assert evidence["detail"]["winerror"] == winerror
        assert "private fixture controller read sharing denial" in evidence["detail"]["error"]
    else:
        assert evidence["first_reads"] == 1  # The next actual file read would be valid.
    with sqlite3.connect(tmp_path / "fixture-ledger.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    assert len(children) == 1 and children[0].job_empty()
    assert not (controller.root / "2").exists()
    state = json.loads(controller.active_path.read_bytes())
    assert state["status"] == "FAILED" and state["automatic_retry"] is False
