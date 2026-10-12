"""Loaded proof is required for a living worker; stale status is not absence."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from ml.intraday import runtime_compatibility as compatibility
from ml.intraday.prediction import instant
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.horizon_ledger import HorizonLedger

REPOSITORY = Path(__file__).resolve().parents[1]
NOW = "2026-10-12T17:20:00+00:00"
PID = 444
BIRTH = (instant(NOW)-timedelta(seconds=100)).timestamp()


def inputs():
    proof = compatibility.emit_loaded_attestation(vars(runtime),HorizonLedger,
        observed_at=instant(NOW)-timedelta(seconds=2),session_managed=True,pid=PID,process_created_at=BIRTH)
    status = dict(schema_version="independent-stock-session-status-v1",status="RUNNING",pid=PID,
        execute=True,sizing_policy="gameplan-direction-current-market-v1",
        started_at=(instant(NOW)-timedelta(seconds=95)).isoformat(),
        heartbeat_at=(instant(NOW)-timedelta(seconds=1)).isoformat(),
        last_cycle={"intraday_runtime_compatibility":proof})
    lock = dict(process="independent-stock-session",pid=str(PID),owner_created_at=str(BIRTH),
        started_at=(instant(NOW)-timedelta(seconds=90)).isoformat(),token="a"*32)
    executable = str(REPOSITORY/".venv/Scripts/python.exe")
    process = dict(pid=PID,created_at=BIRTH,executable=executable,parent_pid=333,
        command_line=[executable,"-u","-m","ml.gameplan_stock_trader","--datastore-target","pc",
            "--execute","--target-horizon","all","--sizing-policy","gameplan-direction-current-market-v1",
            "--run-session","--wait-for-open"])
    return status,lock,process


def verify(status,lock,process,**changes):
    return compatibility.verify_runtime_compatibility(repository=REPOSITORY,status=status,lock=lock,
        as_of=NOW,maximum_heartbeat_age_seconds=60,process_reader=lambda _:process,**changes)


def save(tmp_path,status,lock):
    status_path = tmp_path/"state/independent-stock-trader/session-status.json"
    lock_path = tmp_path/"locks/independent-stock-session.lock"
    status_path.parent.mkdir(parents=True,exist_ok=True)
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    status_path.write_text(json.dumps(status),encoding="utf-8")
    lock_path.write_text("".join(f"{key}={value}\n" for key,value in lock.items()),encoding="utf-8")


def test_actual_loaded_functions_match_compiled_reviewed_source_without_path_dependence():
    status,lock,process = inputs()
    proof = status["last_cycle"]["intraday_runtime_compatibility"]
    assert proof["compatible"] and proof["fingerprints"] == compatibility.expected_fingerprints(REPOSITORY)
    result = verify(status,lock,process)
    assert result["ready"] and result["loaded_code_proven"]
    assert not result["workers_changed"] and result["orders_placed"] == 0


def test_additive_result_preserves_existing_session_fields_and_carries_loaded_proof(tmp_path):
    status,_,_ = inputs()
    proof = status["last_cycle"]["intraday_runtime_compatibility"]
    result = runtime._StreamCompatibleStockTraderRunResult("NO_ORDERS",tmp_path,0,0,0,False,True,True,
        intraday_runtime_compatibility=proof)
    output = result.to_dict()
    assert output["intraday_runtime_compatibility"] == proof
    assert output["submitted_orders"] == 0 and output["activation_active"] is True
    assert output["broker_state_capture"] is None


def test_updated_disk_does_not_prove_old_loaded_cancellation_logic():
    status,lock,process = inputs()
    old_namespace = dict(vars(runtime))
    old_namespace["_cancel_expired_entries"] = lambda *_args,**_kwargs: (0,None)
    status["last_cycle"]["intraday_runtime_compatibility"] = compatibility.emit_loaded_attestation(
        old_namespace,HorizonLedger,observed_at=instant(NOW)-timedelta(seconds=2),session_managed=True,
        pid=PID,process_created_at=BIRTH)
    result = verify(status,lock,process)
    assert not result["ready"]
    assert result["reason_codes"] == ["LOADED_GAMEPLAN_CODE_DIFFERS_FROM_REVIEWED_SOURCE"]


def test_old_loaded_native_ledger_cannot_pass_with_new_disk_interfaces():
    status,lock,process = inputs()
    class Legacy:
        _reservation = HorizonLedger._reservation
        snapshot = HorizonLedger.snapshot
        due_exits = HorizonLedger.due_exits
    status["last_cycle"]["intraday_runtime_compatibility"] = compatibility.emit_loaded_attestation(
        vars(runtime),Legacy,observed_at=instant(NOW)-timedelta(seconds=2),session_managed=True,
        pid=PID,process_created_at=BIRTH)
    assert not verify(status,lock,process)["ready"]


def test_old_loaded_account_authority_cannot_pass_updated_disk(monkeypatch):
    from ml.account_gameplan.authority import AccountAuthority
    status,lock,process=inputs()
    monkeypatch.setattr(AccountAuthority,"recover_submission_receipts",lambda *_a,**_k:None)
    status["last_cycle"]["intraday_runtime_compatibility"]=compatibility.emit_loaded_attestation(
        vars(runtime),HorizonLedger,observed_at=instant(NOW)-timedelta(seconds=2),session_managed=True,
        pid=PID,process_created_at=BIRTH)
    result=verify(status,lock,process)
    assert not result["ready"]
    assert result["reason_codes"]==["LOADED_GAMEPLAN_CODE_DIFFERS_FROM_REVIEWED_SOURCE"]


@pytest.mark.parametrize("component", ["timestamp_validator", "broker_fill_parser", "gameplan_evidence_bridge"])
def test_loaded_fill_time_logic_must_match_reviewed_source(monkeypatch, component):
    from ml.stock_trader import horizon_broker, horizon_ledger
    status, lock, process = inputs()
    if component == "timestamp_validator":
        from ml.account_gameplan import authority
        legacy_validator = lambda *a, **k: (None, frozenset())
        # A coherent old loaded helper in all adapters still cannot use newer disk code as proof.
        monkeypatch.setattr(horizon_ledger, "validate_fill_time_provenance", legacy_validator)
        monkeypatch.setattr(horizon_broker, "validate_fill_time_provenance", legacy_validator)
        monkeypatch.setattr(authority, "validate_fill_time_provenance", legacy_validator)
    elif component == "broker_fill_parser":
        monkeypatch.setattr(horizon_broker, "_fills", lambda *a, **k: ())
    else:
        from ml.account_gameplan.execution import AccountExecutionContext
        monkeypatch.setattr(AccountExecutionContext, "_reconcile", lambda *a, **k: None)
    status["last_cycle"]["intraday_runtime_compatibility"] = compatibility.emit_loaded_attestation(
        vars(runtime), HorizonLedger, observed_at=instant(NOW)-timedelta(seconds=2),
        session_managed=True, pid=PID, process_created_at=BIRTH)
    result = verify(status, lock, process)
    assert not result["ready"]
    assert result["reason_codes"] == ["LOADED_GAMEPLAN_CODE_DIFFERS_FROM_REVIEWED_SOURCE"]


@pytest.mark.parametrize("change,reason", [
    ("missing_proof","LOADED_GAMEPLAN_COMPATIBILITY_PROOF_REQUIRED"),
    ("proof_pid","LOADED_PROOF_SESSION_OR_TIME_MISMATCH"),
    ("proof_birth","LOADED_PROOF_PROCESS_BIRTH_MISMATCH"),
    ("actual_birth","LIVING_GAMEPLAN_COMMAND_OR_BIRTH_UNVERIFIED"),
    ("wrong_command","LIVING_GAMEPLAN_COMMAND_OR_BIRTH_UNVERIFIED"),
    ("wrong_executable","LIVING_GAMEPLAN_COMMAND_OR_BIRTH_UNVERIFIED"),
    ("stale","GAMEPLAN_HEARTBEAT_STALE_OR_FUTURE"),
    ("future_heartbeat","GAMEPLAN_HEARTBEAT_STALE_OR_FUTURE"),
    ("future_proof","LOADED_PROOF_SESSION_OR_TIME_MISMATCH"),
    ("one_shot","LOADED_GAMEPLAN_COMPATIBILITY_PROOF_REQUIRED"),
    ("compiler","LOADED_RUNTIME_COMPILER_DIFFERS"),
    ("inactive","RUNNING_GAMEPLAN_SESSION_REQUIRED"),
    ("degraded","RUNNING_GAMEPLAN_SESSION_REQUIRED"),
    ("lock_pid","GAMEPLAN_PROCESS_IDENTITY_UNVERIFIED"),
    ("legacy_lock","GAMEPLAN_SESSION_LOCK_UNVERIFIED"),
])
def test_missing_stale_wrong_identity_and_incompatible_proofs_block(change,reason):
    status,lock,process = inputs()
    proof = status["last_cycle"]["intraday_runtime_compatibility"]
    if change == "missing_proof": status["last_cycle"] = {}
    elif change == "proof_pid": proof["pid"] = PID+1
    elif change == "proof_birth": proof["process_created_at"] = BIRTH-1
    elif change == "actual_birth": process["created_at"] = BIRTH+1
    elif change == "wrong_command": process["command_line"][3] = "ml.intraday.live_session"
    elif change == "wrong_executable": process["executable"] = "C:/unreviewed/python.exe"
    elif change == "stale": status["heartbeat_at"] = (instant(NOW)-timedelta(seconds=61)).isoformat()
    elif change == "future_heartbeat": status["heartbeat_at"] = (instant(NOW)+timedelta(seconds=1)).isoformat()
    elif change == "future_proof": proof["observed_at"] = NOW
    elif change == "one_shot": proof["session_managed"] = False
    elif change == "compiler": proof["python_version"] = [0,0]
    elif change == "inactive": status["execute"] = False
    elif change == "degraded": status["status"] = "DEGRADED"
    elif change == "lock_pid": lock["pid"] = str(PID+1)
    else: lock.pop("owner_created_at")
    result = verify(status,lock,process)
    assert not result["ready"] and result["reason_codes"] == [reason]


def test_complete_inventory_proves_absence_independently_of_stale_status_and_preserves_files(tmp_path):
    status,lock,_ = inputs()
    status["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
    status["last_cycle"] = {}
    save(tmp_path,status,lock)
    before = {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = compatibility.read_gameplan_runtime_compatibility(tmp_path,REPOSITORY,as_of=NOW,
        maximum_heartbeat_age_seconds=60,process_inventory=lambda:[],
        process_reader=lambda _:pytest.fail("No process must be presumed from stale status"))
    assert result["ready"] and result["mode"] == "verified_gameplan_absent"
    assert result["loaded_code_proven"] is False and result["reviewed_installed_code_checked"] is True
    assert {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_missing_status_alone_is_not_absence_and_incomplete_inventory_blocks(tmp_path):
    def unavailable(): raise ValueError("AccessDenied private process information")
    result = compatibility.read_gameplan_runtime_compatibility(tmp_path,REPOSITORY,as_of=NOW,
        maximum_heartbeat_age_seconds=60,process_inventory=unavailable)
    assert not result["ready"] and not result["loaded_code_proven"]
    assert "private" not in json.dumps(result) and not list(tmp_path.rglob("*"))


def test_scheduled_one_shot_process_blocks_absence_even_without_session_status(tmp_path):
    _,_,process = inputs()
    process["command_line"].remove("--run-session")
    result = compatibility.read_gameplan_runtime_compatibility(tmp_path,REPOSITORY,as_of=NOW,
        maximum_heartbeat_age_seconds=60,process_inventory=lambda:[process])
    assert not result["ready"] and result["reason_codes"] == ["GAMEPLAN_STATUS_OR_LOCK_UNAVAILABLE"]


def test_worker_appearing_between_absence_inventory_snapshots_blocks(tmp_path):
    _,_,process = inputs()
    inventories = iter([[],[process]])
    result = compatibility.read_gameplan_runtime_compatibility(tmp_path,REPOSITORY,as_of=NOW,
        maximum_heartbeat_age_seconds=60,process_inventory=lambda:next(inventories))
    assert not result["ready"] and result["reason_codes"] == ["GAMEPLAN_INVENTORY_CHANGED_DURING_CHECK"]


def test_living_verified_worker_and_virtualenv_parent_pair_are_bound_to_loaded_proof(tmp_path):
    status,lock,process = inputs()
    save(tmp_path,status,lock)
    parent = {**deepcopy(process),"pid":333,"created_at":BIRTH-1,"parent_pid":222}
    result = compatibility.read_gameplan_runtime_compatibility(tmp_path,REPOSITORY,as_of=NOW,
        maximum_heartbeat_age_seconds=60,process_inventory=lambda:[process,parent],process_reader=lambda _:process)
    assert result["ready"] and result["mode"] == "loaded_gameplan_verified"
    assert result["loaded_code_proven"] and result["worker_pid"] == PID


def test_second_unverified_gameplan_process_is_not_hidden_by_one_good_heartbeat(tmp_path):
    status,lock,process = inputs()
    save(tmp_path,status,lock)
    other = {**deepcopy(process),"pid":555,"created_at":BIRTH-1,"parent_pid":222}
    result = compatibility.read_gameplan_runtime_compatibility(tmp_path,REPOSITORY,as_of=NOW,
        maximum_heartbeat_age_seconds=60,process_inventory=lambda:[process,other],process_reader=lambda _:process)
    assert not result["ready"] and result["reason_codes"] == ["OTHER_GAMEPLAN_WORKER_UNVERIFIED"]


def test_missing_future_compatible_source_blocks_absence(tmp_path):
    result = compatibility.read_gameplan_runtime_compatibility(tmp_path,tmp_path,as_of=NOW,
        maximum_heartbeat_age_seconds=60,process_inventory=lambda:[])
    assert not result["ready"] and not result["loaded_code_proven"]


def test_duplicate_or_legacy_lock_is_not_silently_rewritten():
    with pytest.raises(ValueError):
        compatibility.parse_session_lock("process=independent-stock-session\npid=444\npid=555\n")
