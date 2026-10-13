"""Offline process observations and temporary private heartbeat files only."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
from types import FunctionType, SimpleNamespace
import threading

import pytest

from ml.intraday import atlas_writer as writer
from ml.intraday import runtime_compatibility as runtime
from ml.intraday.account_execution import IntradayAccountExecutor
from ml.intraday.prediction import instant

ROOT = Path(__file__).resolve().parents[1]
NOW = instant("2026-10-13T17:20:00+00:00")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def callback(code, name, values):
    function = runtime._source_function(code, "run_manual."+name)
    def cell(value):
        return (lambda: value).__closure__[0]
    return FunctionType(function, {"os": os, "json": json}, name,
                        closure=tuple(cell(values[key]) for key in function.co_freevars))


@pytest.fixture
def case(tmp_path):
    pid = os.getpid()
    birth = (NOW-timedelta(seconds=100)).timestamp()
    start = NOW-timedelta(seconds=90)
    status = dict(schema_version="manual-atlas-15m-session-v1", pc="Atlas", status="RUNNING",
        manual_start=True, pid=pid, process_create_time=birth, session_id="manual-atlas-15m:"+"a"*32,
        started_at=start.isoformat(), heartbeat_at=NOW.isoformat())
    lock = dict(process="intraday-manual-session", pid=str(pid), owner_created_at=str(birth),
        started_at=(start+timedelta(seconds=1)).isoformat(), token="b"*32)
    write_json(tmp_path/writer.STATUS_PATH, status)
    (tmp_path/writer.LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path/writer.LOCK_PATH).write_text("".join(f"{key}={value}\n" for key,value in lock.items()))
    executable = str(ROOT/".venv/Scripts/python.exe")
    process = dict(pid=pid, created_at=birth, parent_pid=pid+1000, executable=executable,
        command_line=[executable,"-B","-m","ml.intraday.live_session","--live",
                      "--repository",str(ROOT),"--datastore",str(tmp_path)])
    executor = object.__new__(IntradayAccountExecutor)
    executor.root, executor.session_started_at = tmp_path, start
    executor.clock = lambda: NOW
    stop = threading.Event()
    executor.stopped = stop.is_set
    code = compile((ROOT/"ml/intraday/live_session.py").read_bytes(), "live_session", "exec", dont_inherit=True)
    cells = dict(status_path=tmp_path/writer.STATUS_PATH, session_id=status["session_id"], pc="Atlas",
                 process_create_time=birth, stopped=stop, root=tmp_path, repository=ROOT,
                 clock=executor.clock, read_gameplan_runtime_compatibility=runtime.read_gameplan_runtime_compatibility)
    executor.manual_authority = callback(code, "manual_authority", cells)
    executor.compatibility = callback(code, "compatibility", cells)
    result = SimpleNamespace(root=tmp_path, executor=executor, pid=pid, process=process,
                             status=status, lock=lock, stop=stop)
    result.emit = lambda: writer.emit_atlas_writer_heartbeat(executor, datastore=tmp_path, repository=ROOT,
        now=NOW, pid=pid, process_reader=lambda _: process)
    return result


def read(case, **changes):
    return writer.read_atlas_runtime_compatibility(case.root, ROOT, as_of=NOW,
        maximum_heartbeat_age_seconds=60, process_reader=changes.pop("process_reader", lambda _: case.process),
        process_inventory=changes.pop("process_inventory", lambda: [case.process]), **changes)


def test_real_bound_executor_and_manual_callbacks_emit_valid_private_proof(case):
    proof = case.emit()
    assert proof["compatible"] is True
    result = read(case)
    assert result["ready"] and result["compatibility_state"] == "compatible"
    assert result["loaded_code_proven"] and len(result["proof_sha256"]) == 64
    assert result["orders_placed"] == 0 and result["workers_changed"] is False
    assert "fingerprints" not in result and "session_id" not in result


def test_missing_proof_for_active_old_writer_blocks_even_with_current_disk(case):
    assert not read(case)["ready"]
    assert not (case.root/writer.HEARTBEAT_PATH).exists()


@pytest.mark.parametrize("method", ["step", "_binding", "_capture", "_assert_current", "_submit",
                                    "recover", "manual_authority", "compatibility", "stopped"])
def test_replaced_live_method_or_callback_overwrites_success_with_blocked_proof(case, method):
    assert case.emit()["compatible"]
    setattr(case.executor, method, lambda *a, **kw: True)
    assert case.emit()["compatible"] is False
    assert not read(case)["ready"]


def test_replaced_manual_closure_cannot_borrow_matching_code(case):
    function = case.executor.manual_authority
    values = dict(zip(function.__code__.co_freevars, (cell.cell_contents for cell in function.__closure__)))
    values["session_id"] = "manual-atlas-15m:"+"c"*32
    code = compile((ROOT/"ml/intraday/live_session.py").read_bytes(), "live_session", "exec", dont_inherit=True)
    case.executor.manual_authority = callback(code, "manual_authority", values)
    assert case.emit()["compatible"] is False
    assert not read(case)["ready"]


def test_fresh_module_alias_cannot_hide_replaced_retained_compatibility_reader(case, monkeypatch):
    code = compile((ROOT/"ml/intraday/live_session.py").read_bytes(), "live_session", "exec", dont_inherit=True)
    function = case.executor.compatibility
    values = dict(zip(function.__code__.co_freevars, (cell.cell_contents for cell in function.__closure__)))
    monkeypatch.setattr(runtime, "read_gameplan_runtime_compatibility", lambda *_a, **_k: {"ready": True})
    values["read_gameplan_runtime_compatibility"] = runtime.read_gameplan_runtime_compatibility
    case.executor.compatibility = callback(code, "compatibility", values)
    assert case.emit()["compatible"] is False
    assert not read(case)["ready"]


@pytest.mark.parametrize("damage", ["proof_stale", "proof_future", "status_stale", "stopped",
    "proof_pid", "proof_birth", "pid_reuse", "token", "legacy_lock", "duplicate_lock",
    "wrong_command", "wrong_code", "compiler", "malformed", "duplicate_json", "missing_birth"])
def test_malformed_stale_stopped_reused_or_changed_identity_fails_closed(case, damage):
    proof = case.emit()
    if damage == "proof_stale": proof["observed_at"] = (NOW-timedelta(seconds=61)).isoformat()
    elif damage == "proof_future": proof["observed_at"] = (NOW+timedelta(seconds=1)).isoformat()
    elif damage == "status_stale": case.status["heartbeat_at"] = (NOW-timedelta(seconds=61)).isoformat()
    elif damage == "stopped": case.status["status"] = "STOPPED"
    elif damage == "proof_pid": proof["pid"] += 1
    elif damage == "proof_birth": proof["process_created_at"] += 1
    elif damage == "pid_reuse": case.process["created_at"] += 1
    elif damage == "token": proof["lock_token"] = "c"*32
    elif damage == "wrong_command": case.process["command_line"].remove("--live")
    elif damage == "wrong_code": proof["fingerprints"] = {}
    elif damage == "compiler": proof["python_version"] = [0,0]
    elif damage == "missing_birth": case.status.pop("process_create_time")
    write_json(case.root/writer.HEARTBEAT_PATH, proof)
    write_json(case.root/writer.STATUS_PATH, case.status)
    if damage == "legacy_lock": (case.root/writer.LOCK_PATH).write_text("pid=1\n")
    elif damage == "duplicate_lock":
        with (case.root/writer.LOCK_PATH).open("a") as output: output.write("pid=123\n")
    elif damage == "malformed": (case.root/writer.HEARTBEAT_PATH).write_text("{")
    elif damage == "duplicate_json": (case.root/writer.HEARTBEAT_PATH).write_text('{"compatible":true,"compatible":false}')
    result = read(case)
    assert not result["ready"] and result["compatibility_state"] == "blocked"
    assert "proof_sha256" not in result


def test_unknown_liveness_or_additional_writer_cannot_hide_behind_one_good_proof(case):
    case.emit()
    def unknown(_pid): raise ValueError("private process access denied")
    result = read(case, process_reader=unknown)
    assert not result["ready"] and "private" not in json.dumps(result)
    other = deepcopy(case.process)
    other["pid"] += 1
    assert not read(case, process_inventory=lambda:[case.process,other])["ready"]


def test_matching_virtualenv_launcher_parent_is_not_a_second_writer(case):
    case.emit()
    parent = deepcopy(case.process)
    parent.update(pid=case.process["parent_pid"], parent_pid=0, created_at=case.process["created_at"]-1)
    assert read(case, process_inventory=lambda:[parent,case.process])["ready"]


def test_observation_race_or_stop_during_verification_blocks(case):
    case.emit()
    inventories = iter([[case.process], []])
    assert not read(case, process_inventory=lambda:next(inventories))["ready"]
    calls = []
    def process(_pid):
        calls.append(1)
        if len(calls) == 2:
            write_json(case.root/writer.STATUS_PATH, {**case.status,"status":"STOPPED"})
        return case.process
    assert not read(case, process_reader=process)["ready"]


@pytest.mark.parametrize("age_source", ["proof", "manual_status"])
def test_near_expiry_preserves_attested_time_and_expiry_not_reader_time(case, age_source):
    proof = case.emit()
    earlier = (NOW-timedelta(seconds=59)).isoformat()
    if age_source == "proof":
        proof["observed_at"] = earlier
        write_json(case.root/writer.HEARTBEAT_PATH, proof)
    else:
        case.status["heartbeat_at"] = earlier
        write_json(case.root/writer.STATUS_PATH, case.status)
    result = read(case)
    assert result["ready"]
    assert result["observed_at"] == proof["observed_at"]
    assert result["checked_at"] == NOW.isoformat()
    assert result["expires_at"] == (NOW+timedelta(seconds=1)).isoformat()


@pytest.mark.parametrize("seconds", [-61, 1])
def test_latest_status_heartbeat_is_revalidated_when_only_timestamp_changes(case, seconds):
    case.emit()
    calls = []
    def process(_pid):
        calls.append(1)
        if len(calls) == 2:
            write_json(case.root/writer.STATUS_PATH,
                       {**case.status,"heartbeat_at":(NOW+timedelta(seconds=seconds)).isoformat()})
        return case.process
    result = read(case, process_reader=process)
    assert not result["ready"]
    assert result["reason_codes"] == ["ATLAS_MANUAL_HEARTBEAT_STALE_OR_FUTURE"]


def test_explicitly_stopped_session_with_complete_absence_is_not_loaded_code_proof(case):
    case.emit()
    case.status["status"] = "STOPPED"
    write_json(case.root/writer.STATUS_PATH, case.status)
    (case.root/writer.LOCK_PATH).unlink()
    before = {str(path):path.read_bytes() for path in case.root.rglob("*") if path.is_file()}
    result = read(case, process_inventory=lambda:[])
    assert result["ready"] and result["compatibility_state"] == "absent"
    assert result["loaded_code_proven"] is False
    assert {str(path):path.read_bytes() for path in case.root.rglob("*") if path.is_file()} == before


def test_missing_heartbeat_alone_and_stale_or_malformed_lock_never_prove_absence(case):
    for text in ("pid=123\n", "", (case.root/writer.LOCK_PATH).read_text()):
        (case.root/writer.LOCK_PATH).write_text(text)
        assert not read(case, process_inventory=lambda:[])["ready"]
    (case.root/writer.LOCK_PATH).unlink()
    assert not read(case, process_inventory=lambda:[])["ready"]  # RUNNING status remains unresolved.


def test_fresh_empty_datastore_requires_two_complete_inventories_and_writes_nothing(tmp_path):
    calls = []
    def inventory(): calls.append(1); return []
    result = writer.read_atlas_runtime_compatibility(tmp_path, ROOT, now=NOW, process_inventory=inventory,
        process_reader=lambda _:pytest.fail("No active process was identified"))
    assert result["ready"] and result["compatibility_state"] == "absent"
    assert len(calls) == 2 and not list(tmp_path.iterdir())
    def unknown(): raise ValueError("inventory inaccessible")
    assert not writer.read_atlas_runtime_compatibility(tmp_path, ROOT, now=NOW, process_inventory=unknown)["ready"]


def test_python_process_without_readable_command_cannot_prove_writer_absence(tmp_path):
    result = writer.read_atlas_runtime_compatibility(tmp_path, ROOT, now=NOW,
        process_inventory=lambda:[{"pid":123,"command_line":[]}])
    assert not result["ready"] and result["compatibility_state"] == "blocked"
    assert not list(tmp_path.iterdir())


def test_supported_executor_hook_is_before_account_config_or_provider_work(case, monkeypatch):
    from ml.intraday import account_execution
    calls = []
    monkeypatch.setattr(case.executor, "emit_runtime_heartbeat", lambda:calls.append("loaded-proof"))
    def config(_root):
        assert calls == ["loaded-proof"]
        raise ValueError("synthetic boundary after proof")
    monkeypatch.setattr(account_execution, "load_account_config", config)
    with pytest.raises(ValueError, match="synthetic boundary"):
        case.executor._binding()


def test_stopped_manual_authority_cannot_emit_success(case):
    case.stop.set()
    assert case.emit()["compatible"] is False
