"""All installer tests use temporary roots, fake processes and fake native gates."""
from contextlib import contextmanager
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC=importlib.util.spec_from_file_location("apply_candidate_under_test",Path(__file__).with_name("apply_candidate.py"))
installer=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
TOKEN="c89b46b1-af11-45a3-b069-42600b62cb27"
NOW=installer.WINDOW_START+timedelta(hours=4)


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding="utf-8")


@contextmanager
def fake_gate(parent,**kwargs):
    parent.mkdir(parents=True,exist_ok=True)
    yield


@pytest.fixture
def env(tmp_path):
    target=tmp_path/"repo"; package=tmp_path/"package"; data=tmp_path/"datastore"
    for root in (target,package,data): root.mkdir()
    files=[]
    for rel,base,payload in (("ml/existing.py",b"old\n",b"new\n"),("ml/new.py",None,b"added\n")):
        (package/"files"/rel).parent.mkdir(parents=True,exist_ok=True)
        (package/"files"/rel).write_bytes(payload)
        if base is not None:
            (target/rel).parent.mkdir(parents=True,exist_ok=True); (target/rel).write_bytes(base)
            (package/"base"/rel).parent.mkdir(parents=True,exist_ok=True); (package/"base"/rel).write_bytes(base)
        files.append({"path":rel,"base_sha256":None if base is None else installer.sha(base),"sha256":installer.sha(payload)})
    dependency=target/"datafetching/runtime_lock.py"; dependency.parent.mkdir(); dependency.write_bytes(b"fake support\n")
    log=package/"combined-tests.log"; log.write_bytes(b"126 passed\n")
    report={"status":"PASS","exit_code":0,"diff_check_exit_code":0,"source_drift":[],
            "production_actions":False,"source_fingerprints":{f["path"]:f["sha256"] for f in files},"log_sha256":installer.file_hash(log)}
    write(package/"combined-verification.json",report)
    manifest={"schema_version":1,"package_id":installer.PACKAGE_ID,"effective_action_date":installer.ACTION_DATE,
        "base_head_sha":"a"*40,"files":files,"source_dependency_fingerprints":{"datafetching/runtime_lock.py":installer.file_hash(dependency)},
        "test_evidence":{"path":"combined-verification.json","sha256":installer.file_hash(package/"combined-verification.json"),
                         "log_path":"combined-tests.log","log_sha256":installer.file_hash(log)}}
    write(package/"candidate-manifest.json",manifest)
    claim={"owner_token":TOKEN,"updated_at":(NOW-timedelta(seconds=5)).isoformat(),"expires_at":(NOW+timedelta(seconds=175)).isoformat()}
    write(data/"ml/overnight-supervision.json",claim)
    session={"schema_version":"independent-stock-session-status-v1","status":"FINISHED","action_date":"2026-09-30",
        "execute":True,"sizing_policy":"gameplan-direction-current-market-v1","pid":1234,
        "closes_at":installer.WINDOW_START.isoformat(),"heartbeat_at":(installer.WINDOW_START+timedelta(seconds=1)).isoformat()}
    write(data/"state/independent-stock-trader/session-status.json",session)
    processes=[{"ProcessId":1,"Name":"system","CommandLine":None},
               {"ProcessId":777,"Name":"python.exe","CommandLine":"python apply_candidate.py --package temp --supervision-uuid fixture"}]
    return SimpleNamespace(target=target,package=package,data=data,manifest=manifest,claim=claim,session=session,
        processes=processes,now=NOW,locks=[],original={f["path"]:installer.file_hash(target/f["path"]) for f in files})


def apply(env,**kwargs):
    @contextmanager
    def lock(path):
        env.locks.append(path)
        with installer.strict_native_lock(path,gate_factory=fake_gate): yield
    return installer.apply_package(env.package,env.target,env.data,TOKEN,
        now_fn=kwargs.get("now_fn",lambda:env.now),processes_fn=kwargs.get("processes_fn",lambda:env.processes),
        lock_factory=kwargs.get("lock_factory",lock))


def unchanged(env):
    assert {rel:installer.file_hash(env.target/rel) for rel in env.original}==env.original
    assert not (env.package/"apply-receipt.json").exists()


def test_success_and_idempotent_resume_preserve_nonpackage_files(env):
    untouched=env.target/"controls/operator-intent.txt"; untouched.parent.mkdir(); untouched.write_bytes(b"ACTIVE\n")
    first=apply(env)
    assert first["status"]=="APPLIED" and first["locks_released"] and first["orders_placed_by_helper"]==0
    assert len(first["completed_files"])==2 and untouched.read_bytes()==b"ACTIVE\n"
    assert all(not p.exists() for p in env.locks)
    again=apply(env)
    assert all(item["status"]=="ALREADY_FINAL" for item in again["completed_files"])
    assert installer.json_file(env.package/"apply-receipt.json")["status"]=="APPLIED"


@pytest.mark.parametrize("case",["payload","backup","target","dependency","test_log","test_report","tested_source"])
def test_every_payload_backup_target_dependency_and_test_binding_precedes_writes(env,case):
    selected={"payload":env.package/"files/ml/new.py","backup":env.package/"base/ml/existing.py",
        "target":env.target/"ml/existing.py","dependency":env.target/"datafetching/runtime_lock.py",
        "test_log":env.package/"combined-tests.log","test_report":env.package/"combined-verification.json"}
    if case=="tested_source":
        report=installer.json_file(env.package/"combined-verification.json")
        report["source_fingerprints"]["ml/new.py"]="f"*64
        write(env.package/"combined-verification.json",report)
        env.manifest["test_evidence"]["sha256"]=installer.file_hash(env.package/"combined-verification.json")
        write(env.package/"candidate-manifest.json",env.manifest)
    else:
        selected[case].write_bytes(b"concurrent change")
        if case=="target": env.original["ml/existing.py"]=installer.file_hash(selected[case])
    with pytest.raises(installer.DeploymentBlocked): apply(env)
    unchanged(env); assert not env.locks


@pytest.mark.parametrize("path",["../escape.py","ml/../../escape.py","/tmp/escape.py","C:/escape.py","ml\\escape.py",
                                  "ml/NUL.py","ml/file.py ","ml//file.py",".git/config","controls/operator.py","ml/override.json"])
def test_paths_cannot_escape_or_target_controls_git_or_non_source(env,path):
    env.manifest["files"][0]["path"]=path; write(env.package/"candidate-manifest.json",env.manifest)
    with pytest.raises(installer.DeploymentBlocked): apply(env)
    unchanged(env)


def test_case_insensitive_duplicate_destinations_are_rejected(env):
    env.manifest["files"].append({**env.manifest["files"][0],"path":"ML/EXISTING.py"})
    write(env.package/"candidate-manifest.json",env.manifest)
    with pytest.raises(installer.DeploymentBlocked): apply(env)
    unchanged(env)


@pytest.mark.parametrize("which",["target_parent","payload_parent"])
def test_symlink_directory_is_rejected_before_any_write(env,tmp_path,which):
    outside=tmp_path/"outside"; outside.mkdir(); (outside/"existing.py").write_bytes(b"old\n")
    if which=="target_parent":
        (env.target/"ml/existing.py").unlink(); (env.target/"ml").rmdir(); link=env.target/"ml"
    else:
        (env.package/"files/ml/existing.py").unlink(); (env.package/"files/ml/new.py").unlink()
        (env.package/"files/ml").rmdir(); link=env.package/"files/ml"
    try: link.symlink_to(outside,target_is_directory=True)
    except OSError as exc: pytest.skip("OS denies fixture symlink creation: "+str(exc))
    with pytest.raises(installer.DeploymentBlocked,match="Symlink|reparse"): apply(env)
    assert (outside/"existing.py").read_bytes()==b"old\n" and not env.locks


@pytest.mark.parametrize("case",["early","deadline","wrong_owner","expired","future_claim","running_status","wrong_day","stale_terminal","live_pid","live_worker","waiting_worker","direct_worker","live_launcher","pipeline","unknown_python","empty_inventory"])
def test_time_claim_terminal_and_process_guards_fail_closed(env,case):
    if case=="early": env.now=installer.WINDOW_START-timedelta(seconds=1)
    if case=="deadline": env.now=installer.WINDOW_END
    if case=="wrong_owner": env.claim["owner_token"]="ba337af6-7f58-4216-bad2-29147d7e97e1"
    if case=="expired": env.claim["expires_at"]=env.now.isoformat()
    if case=="future_claim": env.claim["updated_at"]=(env.now+timedelta(seconds=1)).isoformat()
    if case=="running_status": env.session["status"]="RUNNING"
    if case=="wrong_day": env.session["action_date"]="2026-10-01"
    if case=="stale_terminal": env.session["heartbeat_at"]=(installer.WINDOW_START-timedelta(seconds=1)).isoformat()
    if case=="live_pid": env.processes.append({"ProcessId":1234,"Name":"unrelated.exe","CommandLine":"unrelated"})
    if case=="live_worker": env.processes.append({"ProcessId":22,"Name":"python.exe","CommandLine":"python -u -m ml.gameplan_stock_trader --execute --run-session"})
    if case=="waiting_worker": env.processes.append({"ProcessId":22,"Name":"python.exe","CommandLine":"python -u -m ml.gameplan_stock_trader --execute --run-session --wait-for-open"})
    if case=="direct_worker": env.processes.append({"ProcessId":22,"Name":"python.exe","CommandLine":"python C:/repo/ml/stock_trader/independent_session.py"})
    if case=="live_launcher": env.processes.append({"ProcessId":22,"Name":"powershell.exe","CommandLine":"powershell -File C:/repo/docs/datafetch-ml/start_stock_session.ps1"})
    if case=="pipeline": env.processes.append({"ProcessId":22,"Name":"python.exe","CommandLine":"python -u -m ml.overnight_runtime --once"})
    if case=="unknown_python": env.processes.append({"ProcessId":22,"Name":"python.exe","CommandLine":None})
    if case=="empty_inventory": env.processes=[]
    write(env.data/"ml/overnight-supervision.json",env.claim)
    write(env.data/"state/independent-stock-trader/session-status.json",env.session)
    with pytest.raises(installer.DeploymentBlocked): apply(env)
    unchanged(env); assert not env.locks


def test_active_claim_renewal_process_is_not_a_pipeline(env):
    env.processes.append({"ProcessId":22,"Name":"python.exe","CommandLine":"python -m ml.overnight_runtime --datastore-target pc --claim-supervision fixture"})
    assert apply(env)["status"]=="APPLIED"


def test_existing_foreign_lock_is_not_recovered_or_deleted(env):
    foreign=env.data/installer.LOCKS[1]; foreign.parent.mkdir(parents=True); foreign.write_bytes(b"pid=99999999\ntoken=foreign\n")
    with pytest.raises(FileExistsError): apply(env)
    unchanged(env); assert foreign.read_bytes()==b"pid=99999999\ntoken=foreign\n"
    assert not (env.data/installer.LOCKS[0]).exists()


def test_release_never_unlinks_a_replaced_foreign_lock(tmp_path):
    path=tmp_path/"lock"
    with pytest.raises(installer.DeploymentBlocked,match="refusing to unlink"):
        with installer.strict_native_lock(path,gate_factory=fake_gate): path.write_bytes(b"foreign owner")
    assert path.read_bytes()==b"foreign owner"


def test_partial_install_rolls_back_and_resumes_from_exact_originals(env,monkeypatch):
    original=installer.atomic_bytes
    failed=[False]
    def fail_once(path,data,expected):
        if Path(path)==env.target/"ml/new.py" and not failed[0]:
            failed[0]=True
            raise OSError("synthetic disk interruption")
        return original(path,data,expected)
    monkeypatch.setattr(installer,"atomic_bytes",fail_once)
    with pytest.raises(OSError): apply(env)
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="ROLLED_BACK" and len(progress["completed_files"])==1
    assert progress["rollback"]["status"]=="COMPLETE" and progress["rollback"]["native_locks_held"]
    unchanged(env)
    assert not any(p.exists() for p in env.locks)
    assert apply(env)["status"]=="APPLIED"


def test_target_change_between_validation_and_replacement_is_preserved(env,monkeypatch):
    original=installer.atomic_bytes
    def drift(path,data,expected):
        if Path(path)==env.target/"ml/new.py": Path(path).write_bytes(b"concurrent edit")
        return original(path,data,expected)
    monkeypatch.setattr(installer,"atomic_bytes",drift)
    with pytest.raises(installer.DeploymentBlocked,match="changed before atomic"): apply(env)
    assert (env.target/"ml/new.py").read_bytes()==b"concurrent edit"
    assert (env.target/"ml/existing.py").read_bytes()==b"old\n"
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="ROLLBACK_INCOMPLETE"
    assert progress["rollback"]["files"][0]["status"]=="NOT_RESTORED"
    assert not (env.package/"apply-receipt.json").exists()


@pytest.mark.parametrize("expire_on_call",[5,6])
def test_expiring_claim_mid_install_restores_originals_and_removes_new_files(env,expire_on_call):
    calls=[0]
    def now():
        calls[0]+=1
        return NOW if calls[0]<expire_on_call else NOW+timedelta(minutes=4)
    with pytest.raises(installer.DeploymentBlocked,match="claim"): apply(env,now_fn=now)
    unchanged(env)
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="ROLLED_BACK" and progress["rollback"]["status"]=="COMPLETE"
    assert len(progress["rollback"]["files"])==expire_on_call-4
    assert not any(p.exists() for p in env.locks)


@pytest.mark.parametrize("which",["target","payload","dependency"])
def test_windows_reparse_attribute_is_rejected_without_link_privileges(env,monkeypatch,which):
    selected={"target":env.target/"ml","payload":env.package/"files/ml",
              "dependency":env.target/"datafetching"}[which]
    original=Path.lstat
    def lstat(path,*args,**kwargs):
        observed=original(path,*args,**kwargs)
        if path==selected:
            return SimpleNamespace(st_mode=observed.st_mode,st_file_attributes=0x400)
        return observed
    monkeypatch.setattr(Path,"lstat",lstat)
    with pytest.raises(installer.DeploymentBlocked,match="reparse"): apply(env)
    assert not (env.package/"apply-receipt.json").exists() and not env.locks


@pytest.mark.parametrize("drift_on_call",[4,5])
def test_supporting_dependency_drift_restores_originals_and_removes_new_files(env,drift_on_call):
    calls=[0]
    def processes():
        calls[0]+=1
        if calls[0]==drift_on_call:
            (env.target/"datafetching/runtime_lock.py").write_bytes(b"concurrent supporting edit")
        return env.processes
    with pytest.raises(installer.DeploymentBlocked,match="Supporting source changed during"): apply(env,processes_fn=processes)
    unchanged(env)
    assert (env.target/"datafetching/runtime_lock.py").read_bytes()==b"concurrent supporting edit"
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="ROLLED_BACK" and progress["rollback"]["status"]=="COMPLETE"


def test_exception_after_atomic_replace_restores_both_files_under_all_locks(env,monkeypatch):
    original=installer.atomic_bytes
    original_unlink=Path.unlink
    def after_replace(path,data,expected):
        result=original(path,data,expected)
        if Path(path)==env.target/"ml/new.py":
            assert all((env.data/p).exists() for p in installer.LOCKS)
            raise OSError("failure after os.replace")
        if Path(path)==env.target/"ml/existing.py" and data==b"old\n":
            assert all((env.data/p).exists() for p in installer.LOCKS)
        return result
    def remove_new(path,*args,**kwargs):
        if path==env.target/"ml/new.py":
            assert all((env.data/p).exists() for p in installer.LOCKS)
        return original_unlink(path,*args,**kwargs)
    monkeypatch.setattr(installer,"atomic_bytes",after_replace)
    monkeypatch.setattr(Path,"unlink",remove_new)
    with pytest.raises(OSError,match="after os.replace"): apply(env)
    unchanged(env)
    rollback=installer.json_file(env.package/"apply-progress.json")["rollback"]
    assert [r["status"] for r in rollback["files"]]==["REMOVED_NEW_FILE","RESTORED_PREVIOUS"]


def test_rollback_preserves_files_already_final_before_this_invocation(env,monkeypatch):
    (env.target/"ml/existing.py").write_bytes(b"new\n")
    env.original["ml/existing.py"]=installer.sha(b"new\n")
    original=installer.atomic_bytes
    def after_replace(path,data,expected):
        result=original(path,data,expected)
        if Path(path)==env.target/"ml/new.py": raise OSError("after new file installation")
        return result
    monkeypatch.setattr(installer,"atomic_bytes",after_replace)
    with pytest.raises(OSError): apply(env)
    unchanged(env)
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["completed_files"][0]["status"]=="ALREADY_FINAL"
    assert [r["path"] for r in progress["rollback"]["files"]]==["ml/new.py"]


def test_concurrent_edit_of_installed_file_is_preserved_and_rollback_incomplete(env,monkeypatch):
    original=installer.atomic_bytes
    def change_previous(path,data,expected):
        result=original(path,data,expected)
        if Path(path)==env.target/"ml/new.py":
            (env.target/"ml/existing.py").write_bytes(b"independent edit")
            raise OSError("interrupted after concurrent edit")
        return result
    monkeypatch.setattr(installer,"atomic_bytes",change_previous)
    with pytest.raises(OSError): apply(env)
    assert (env.target/"ml/existing.py").read_bytes()==b"independent edit"
    assert not (env.target/"ml/new.py").exists()
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="ROLLBACK_INCOMPLETE"
    assert [r["status"] for r in progress["rollback"]["files"]]==["REMOVED_NEW_FILE","NOT_RESTORED"]
    assert "Concurrent target drift" in progress["rollback"]["files"][1]["error"]


def test_rollback_continues_other_restorations_after_new_file_removal_failure(env,monkeypatch):
    original_atomic=installer.atomic_bytes
    original_unlink=Path.unlink
    def after_replace(path,data,expected):
        result=original_atomic(path,data,expected)
        if Path(path)==env.target/"ml/new.py": raise OSError("synthetic apply error")
        return result
    def fail_remove(path,*args,**kwargs):
        if path==env.target/"ml/new.py": raise OSError("synthetic rollback removal failure")
        return original_unlink(path,*args,**kwargs)
    monkeypatch.setattr(installer,"atomic_bytes",after_replace)
    monkeypatch.setattr(Path,"unlink",fail_remove)
    with pytest.raises(OSError,match="apply error"): apply(env)
    assert (env.target/"ml/existing.py").read_bytes()==b"old\n"
    assert (env.target/"ml/new.py").read_bytes()==b"added\n"
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="ROLLBACK_INCOMPLETE"
    assert [r["status"] for r in progress["rollback"]["files"]]==["NOT_RESTORED","RESTORED_PREVIOUS"]


@pytest.mark.parametrize("persistent",[False,True])
def test_failed_progress_evidence_never_prevents_rollback(env,monkeypatch,persistent):
    original=installer.write_record
    failed=[False]
    def unavailable(path,value):
        if (value["status"]=="IN_PROGRESS" and len(value["completed_files"])==2) or (persistent and failed[0]):
            failed[0]=True
            raise OSError("synthetic evidence disk full")
        return original(path,value)
    monkeypatch.setattr(installer,"write_record",unavailable)
    error=installer.DeploymentBlocked if persistent else OSError
    with pytest.raises(error,match="disk full") as caught: apply(env)
    unchanged(env)
    if persistent:
        assert '"status": "COMPLETE"' in str(caught.value)
        assert "could not save rollback evidence" in str(caught.value)
    else:
        assert installer.json_file(env.package/"apply-progress.json")["status"]=="ROLLED_BACK"


def test_post_release_guard_failure_is_fully_applied_but_unreceipted(env):
    calls=[0]
    def now():
        calls[0]+=1
        return NOW if calls[0]<7 else NOW+timedelta(minutes=4)
    with pytest.raises(installer.DeploymentBlocked,match="APPLIED_UNRECEIPTED"): apply(env,now_fn=now)
    assert (env.target/"ml/existing.py").read_bytes()==b"new\n"
    assert (env.target/"ml/new.py").read_bytes()==b"added\n"
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="APPLIED_UNRECEIPTED" and progress["locks_released"]
    assert "precommit_guard" in progress and "rollback" not in progress
    assert not (env.package/"apply-receipt.json").exists()


def test_post_release_receipt_write_failure_preserves_full_install_and_reports_unreceipted(env,monkeypatch):
    original=installer.write_record
    def fail_receipt(path,value):
        if Path(path)==env.package/"apply-receipt.json":
            assert not any((env.data/p).exists() for p in installer.LOCKS)
            raise OSError("synthetic receipt disk failure")
        return original(path,value)
    monkeypatch.setattr(installer,"write_record",fail_receipt)
    with pytest.raises(installer.DeploymentBlocked,match="APPLIED_UNRECEIPTED"): apply(env)
    assert all(installer.file_hash(env.target/f["path"])==f["sha256"] for f in env.manifest["files"])
    progress=installer.json_file(env.package/"apply-progress.json")
    assert progress["status"]=="APPLIED_UNRECEIPTED" and progress["locks_released"]
    assert "receipt disk failure" in progress["error"] and "rollback" not in progress
    assert not (env.package/"apply-receipt.json").exists()
