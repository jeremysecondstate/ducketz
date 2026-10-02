"""One-off post-close installation of the reviewed September 30 candidate.

The CLI has fixed production roots. It never claims supervision, stops a
process, clears a foreign lock, changes Git state, or starts the trader. Tests
call apply_package with temporary roots and injected guards only.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import uuid


PACKAGE_ID = "cross-horizon-fallback-20260930"
ACTION_DATE = "2026-10-01"
TARGET = Path("C:/dev/ducketz")
DATASTORE = Path("C:/DATASTORE")
WINDOW_START = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 10, 1, 11, tzinfo=timezone.utc)
LOCKS = ("locks/independent-stock-session.lock", "locks/stock-trader-hourly.lock",
         ".ducketz-overnight-runtime.lock")
HEX = re.compile(r"[a-f0-9]{64}\Z")
DEVICES = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)


class DeploymentBlocked(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise DeploymentBlocked("Naive evidence timestamp")
    return value.astimezone(timezone.utc)


def plain_path(path):
    """Reject symlinks/reparse points in every existing path component."""
    path = Path(path).absolute()
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DeploymentBlocked("Symlink or reparse path: " + str(item))
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise DeploymentBlocked("Non-directory parent: " + str(item))
    return path


def relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or value.startswith("/") or "\x00" in value):
        raise DeploymentBlocked("Invalid relative path")
    parts = value.split("/")
    if any(not p or p in {".", ".."} or p.rstrip(" .") != p or DEVICES.fullmatch(p) for p in parts):
        raise DeploymentBlocked("Unsafe relative path: " + value)
    return PurePosixPath(value)


def safe_path(root, value):
    root = plain_path(root)
    result = plain_path(root.joinpath(*relative(value).parts))
    if not result.is_relative_to(root):
        raise DeploymentBlocked("Path escapes root")
    return result


def read_file(path):
    path = plain_path(path)
    if not path.is_file():
        raise DeploymentBlocked("Required regular file is missing: " + str(path))
    return path.read_bytes()


def json_file(path):
    value = json.loads(read_file(path).decode("utf-8"))
    if not isinstance(value, dict):
        raise DeploymentBlocked("Expected JSON object: " + str(path))
    return value


def file_hash(path):
    path = plain_path(path)
    return sha(read_file(path)) if path.exists() else None


def validate_package(package, target):
    package, target = plain_path(package), plain_path(target)
    raw = read_file(package / "candidate-manifest.json")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("package_id") != PACKAGE_ID
            or manifest.get("effective_action_date") != ACTION_DATE
            or not re.fullmatch(r"[a-f0-9]{40}",str(manifest.get("base_head_sha")))):
        raise DeploymentBlocked("Unexpected candidate identity")
    records = manifest.get("files")
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise DeploymentBlocked("Invalid bounded candidate file list")
    prepared, names = [], set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "base_sha256", "sha256"}:
            raise DeploymentBlocked("Invalid candidate file record")
        rel = relative(record["path"])
        if (rel.parts[0] not in {"app", "ml", "datafetching", "tests", "docs"}
                or rel.suffix not in {".py", ".md"} or rel.as_posix().casefold() in names):
            raise DeploymentBlocked("Unapproved or duplicate source path: " + str(rel))
        names.add(rel.as_posix().casefold())
        final, base = record["sha256"], record["base_sha256"]
        if not isinstance(final, str) or not HEX.fullmatch(final) or (base is not None and (not isinstance(base, str) or not HEX.fullmatch(base))):
            raise DeploymentBlocked("Invalid file checksum")
        payload = read_file(safe_path(package / "files", str(rel)))
        if len(payload) > 20_000_000 or sha(payload) != final:
            raise DeploymentBlocked("Candidate payload checksum differs: " + str(rel))
        if base is not None and sha(read_file(safe_path(package / "base", str(rel)))) != base:
            raise DeploymentBlocked("Base backup checksum differs: " + str(rel))
        destination = safe_path(target, str(rel))
        current = file_hash(destination)
        if current not in {base, final}:
            raise DeploymentBlocked("Concurrent source drift: " + str(rel))
        prepared.append({**record, "destination": destination, "payload": payload})
    dependencies = manifest.get("source_dependency_fingerprints")
    if not isinstance(dependencies, dict) or "datafetching/runtime_lock.py" not in dependencies:
        raise DeploymentBlocked("Missing supporting-source fingerprints")
    seen = set()
    for rel, expected in dependencies.items():
        key = str(relative(rel)).casefold()
        if key in seen or key in names or not isinstance(expected, str) or not HEX.fullmatch(expected):
            raise DeploymentBlocked("Invalid supporting-source fingerprint")
        seen.add(key)
        if file_hash(safe_path(target, rel)) != expected:
            raise DeploymentBlocked("Supporting source changed: " + rel)
    evidence = manifest.get("test_evidence", {})
    for field, digest in (("path", "sha256"), ("log_path", "log_sha256")):
        if not isinstance(evidence.get(digest), str) or not HEX.fullmatch(evidence[digest]):
            raise DeploymentBlocked("Missing verified test evidence")
        if sha(read_file(safe_path(package, evidence.get(field)))) != evidence[digest]:
            raise DeploymentBlocked("Test evidence checksum differs")
    report = json_file(safe_path(package, evidence["path"]))
    if (report.get("status") != "PASS" or report.get("exit_code") != 0
            or report.get("diff_check_exit_code") != 0 or report.get("source_drift") != []
            or report.get("log_sha256") != evidence["log_sha256"] or report.get("production_actions") is not False):
        raise DeploymentBlocked("Candidate does not have a clean combined test result")
    tested = report.get("source_fingerprints", {})
    if any(tested.get(item["path"]) != item["sha256"] for item in prepared):
        raise DeploymentBlocked("Payload differs from combined-tested source")
    for name in ("apply-progress.json", "apply-receipt.json"):
        path = safe_path(package, name)
        if path.exists() and json_file(path).get("manifest_sha256") != sha(raw):
            raise DeploymentBlocked("Existing installer record belongs to another package")
    return manifest, sha(raw), prepared


def guard_state(datastore, token, now, processes):
    now = timestamp(now)
    if not WINDOW_START <= now < WINDOW_END:
        raise DeploymentBlocked("Outside Sep 30 post-close / Oct 1 pre-open deployment window")
    if str(uuid.UUID(token)) != token:
        raise DeploymentBlocked("Supervision UUID must be canonical")
    claim_path = safe_path(datastore, "ml/overnight-supervision.json")
    claim = json_file(claim_path)
    updated, expires = timestamp(claim.get("updated_at")), timestamp(claim.get("expires_at"))
    if (claim.get("owner_token") != token or not updated <= now < expires
            or not 0 < (expires-updated).total_seconds() <= 180):
        raise DeploymentBlocked("Own active native supervision claim required")
    session_path = safe_path(datastore, "state/independent-stock-trader/session-status.json")
    session = json_file(session_path)
    if (session.get("schema_version") != "independent-stock-session-status-v1"
            or session.get("status") not in {"FINISHED", "FINISHED_WITH_ERRORS"}
            or session.get("action_date") != "2026-09-30" or session.get("execute") is not True
            or session.get("sizing_policy") != "gameplan-direction-current-market-v1"
            or type(session.get("pid")) is not int or session["pid"] <= 0
            or timestamp(session.get("closes_at")) != WINDOW_START
            or not WINDOW_START <= timestamp(session.get("heartbeat_at")) <= now):
        raise DeploymentBlocked("Verified terminal September 30 manual session required")
    if not isinstance(processes, list) or not processes:
        raise DeploymentBlocked("Complete native process inventory required")
    pids = set()
    for row in processes:
        pid, name, command = row.get("ProcessId"), row.get("Name"), row.get("CommandLine")
        if type(pid) is not int or pid < 0 or not isinstance(name, str) or pid in pids:
            raise DeploymentBlocked("Ambiguous native process inventory")
        pids.add(pid)
        if name.lower() in {"python.exe", "pythonw.exe", "python", "python3"} and not isinstance(command, str):
            raise DeploymentBlocked("Python process command is unavailable")
        command = command or ""
        trader = re.search(r'(?:^|\s)-m\s+"?ml\.(?:gameplan_stock_trader|stock_trader(?:\.[\w]+)*)"?(?:\s|$)', command, re.I)
        trader_script = re.search(r'[\\/](?:gameplan_stock_trader|independent_session|independent_runtime)\.py(?:"|\s|$)',command,re.I)
        launcher = re.search(r'(?:start_stock_session\.ps1|Start-Gameplan-Trader\.cmd)', command, re.I)
        overnight = re.search(r'(?:^|\s)-m\s+"?ml\.overnight_runtime"?(?:\s|$)', command, re.I)
        claim_only = re.search(r'--(?:claim-supervision|release-supervision|status)(?:\s|$)', command)
        if trader or trader_script or launcher or (overnight and not claim_only):
            raise DeploymentBlocked("Trader/launcher/pipeline process is still present: PID " + str(pid))
    if session["pid"] in pids:
        raise DeploymentBlocked("Terminal session PID is still present")
    return {"checked_at":now.isoformat(), "claim_updated_at":updated.isoformat(), "claim_expires_at":expires.isoformat(),
        "claim_sha256":sha(read_file(claim_path)), "session_sha256":sha(read_file(session_path)),
        "session_status":session["status"], "session_pid":session["pid"], "session_action_date":session["action_date"],
        "session_heartbeat_at":session["heartbeat_at"], "process_count":len(processes), "trader_processes":0}


def atomic_bytes(path, data, expected_hash):
    path = plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    plain_path(path.parent)
    fd, temporary = tempfile.mkstemp(prefix="."+path.name+".candidate-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if file_hash(path) != expected_hash:
            raise DeploymentBlocked("Target changed before atomic replacement: " + str(path))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)  # Only this invocation's unique temporary file.


def write_record(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True)+"\n").encode(), file_hash(path))


def rollback_changes(attempted):
    """Restore only this invocation's exact writes, with all native locks held.

    Cleanup deliberately has no lease, clock or dependency gate: those may be
    the reason installation failed. An unrelated edit/reparse point is never
    overwritten, and one failed restoration must not prevent the others.
    """
    results = []
    for item in reversed(attempted):
        result = {"path":item["path"], "pre_invocation_sha256":item["before_sha256"],
                  "installed_sha256":item["sha256"]}
        try:
            destination = plain_path(item["destination"])
            current = file_hash(destination)
            if current == item["before_sha256"]:
                result["status"] = "ALREADY_PREVIOUS"
            elif current != item["sha256"]:
                raise DeploymentBlocked("Concurrent target drift; rollback will not overwrite it")
            elif item["before_bytes"] is None:
                # Remove only this invocation's exact newly-created file.
                if file_hash(destination) != item["sha256"]:
                    raise DeploymentBlocked("New target changed before rollback removal")
                destination.unlink()
                result["status"] = "REMOVED_NEW_FILE"
            else:
                atomic_bytes(destination,item["before_bytes"],item["sha256"])
                result["status"] = "RESTORED_PREVIOUS"
            if file_hash(destination) != item["before_sha256"]:
                raise DeploymentBlocked("Rollback verification differs from pre-invocation bytes")
        except Exception as exc:
            result.update(status="NOT_RESTORED",error=f"{type(exc).__name__}: {exc}")
        results.append(result)
    return {"status":"INCOMPLETE" if any(r["status"]=="NOT_RESTORED" for r in results) else "COMPLETE",
            "scope":"THIS_INVOCATION_ONLY", "native_locks_held":True, "files":results}


@contextmanager
def strict_native_lock(path, *, gate_factory):
    """Use native gate/payload protocol; never recover or clear an existing lock."""
    path = plain_path(path)
    payload = (f"process=cross-horizon-candidate-install\npid={os.getpid()}\n"
               f"started_at={datetime.now(timezone.utc).isoformat()}\ntoken={secrets.token_hex(16)}\n").encode()
    with gate_factory(path.parent, timeout=0):
        plain_path(path)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os,"O_BINARY",0))
        with os.fdopen(descriptor,"wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    try:
        yield
    finally:
        with gate_factory(path.parent, timeout=0):
            if file_hash(path) != sha(payload):
                raise DeploymentBlocked("Owned runtime lock changed; refusing to unlink it")
            path.unlink()


def apply_package(package, target, datastore, token, *, now_fn, processes_fn, lock_factory):
    """Apply only prevalidated exact bytes; injectable guards exist for temp tests."""
    package, target, datastore = map(plain_path, (package,target,datastore))
    manifest, manifest_hash, files = validate_package(package,target)
    evidence = guard_state(datastore,token,now_fn(),processes_fn())
    record = {"schema_version":1,"package_id":PACKAGE_ID,"manifest_sha256":manifest_hash,
        "target":str(target),"datastore":str(datastore),"supervision_uuid":token,
        "started_at":timestamp(now_fn()).isoformat(),"status":"IN_PROGRESS","completed_files":[],
        "initial_guard":evidence,"orders_placed_by_helper":0}
    def unchanged_dependencies():
        if sha(read_file(package/"candidate-manifest.json")) != manifest_hash:
            raise DeploymentBlocked("Candidate manifest changed during installation")
        for rel, expected in manifest["source_dependency_fingerprints"].items():
            if file_hash(safe_path(target,rel)) != expected:
                raise DeploymentBlocked("Supporting source changed during installation: " + rel)
    attempted = []
    with ExitStack() as stack:
        for rel in LOCKS:
            stack.enter_context(lock_factory(safe_path(datastore,rel)))
        try:
            evidence = guard_state(datastore,token,now_fn(),processes_fn())
            unchanged_dependencies()
            write_record(package/"apply-progress.json",record)
            for item in files:
                evidence = guard_state(datastore,token,now_fn(),processes_fn())
                unchanged_dependencies()
                destination = safe_path(target,item["path"])
                current = file_hash(destination)
                if current not in {item["base_sha256"],item["sha256"]}:
                    raise DeploymentBlocked("Concurrent source drift: " + item["path"])
                if current != item["sha256"]:
                    before = None if current is None else read_file(destination)
                    if (None if before is None else sha(before)) != current:
                        raise DeploymentBlocked("Target changed while saving rollback bytes: " + item["path"])
                    # Track before replacement: an exception can occur after
                    # os.replace succeeds. Already-final files are excluded.
                    attempted.append({**item,"before_bytes":before,"before_sha256":current})
                    atomic_bytes(destination,item["payload"],current)
                if file_hash(destination) != item["sha256"]:
                    raise DeploymentBlocked("Applied file verification failed")
                record["completed_files"].append({"path":item["path"],"sha256":item["sha256"],
                    "status":"ALREADY_FINAL" if current==item["sha256"] else "APPLIED"})
                record["last_guard"]=evidence
                write_record(package/"apply-progress.json",record)
            unchanged_dependencies()
            # Revalidate saved test evidence, payloads and backups as well as
            # targets; an altered package cannot receive a completion receipt.
            validate_package(package,target)
            if any(file_hash(safe_path(target,f["path"]))!=f["sha256"] for f in files):
                raise DeploymentBlocked("Final source inventory differs")
            # The last precommit guard belongs inside the rollback boundary.
            evidence = guard_state(datastore,token,now_fn(),processes_fn())
            unchanged_dependencies()
            if any(file_hash(safe_path(target,f["path"]))!=f["sha256"] for f in files):
                raise DeploymentBlocked("Precommit source inventory differs")
            record.update(status="FILES_APPLIED_LOCKED",precommit_guard=evidence)
            write_record(package/"apply-progress.json",record)
        except BaseException as exc:
            # Restore first, before any error-evidence write that could itself
            # fail (for example a full disk). The native locks are still held.
            rollback = rollback_changes(attempted)
            status = "ROLLBACK_INCOMPLETE" if rollback["status"]=="INCOMPLETE" else ("ROLLED_BACK" if attempted else "BLOCKED")
            record.update(status=status,error=f"{type(exc).__name__}: {exc}",rollback=rollback)
            try:
                write_record(package/"apply-progress.json",record)
            except Exception as evidence_error:
                raise DeploymentBlocked(f"{record['error']}; rollback={json.dumps(rollback,sort_keys=True)}; "
                    f"could not save rollback evidence: {type(evidence_error).__name__}: {evidence_error}") from exc
            raise
    try:
        evidence = guard_state(datastore,token,now_fn(),processes_fn())
        unchanged_dependencies()
        if any(file_hash(safe_path(target,f["path"]))!=f["sha256"] for f in files):
            raise DeploymentBlocked("Source inventory changed after lock release")
        record.update(status="APPLIED",completed_at=timestamp(now_fn()).isoformat(),final_guard=evidence,
            source_dependency_fingerprints=manifest["source_dependency_fingerprints"],locks_released=True)
        write_record(package/"apply-progress.json",record)
        write_record(package/"apply-receipt.json",record)
    except Exception as exc:
        # All files passed the locked precommit checkpoint. Do not attempt
        # rollback after releasing the locks; report an unreceipted commit.
        record.update(status="APPLIED_UNRECEIPTED",locks_released=True,error=f"{type(exc).__name__}: {exc}")
        try:
            write_record(package/"apply-progress.json",record)
        except Exception as evidence_error:
            raise DeploymentBlocked(f"APPLIED_UNRECEIPTED: {record['error']}; could not save evidence: {evidence_error}") from exc
        raise DeploymentBlocked("APPLIED_UNRECEIPTED: " + record["error"]) from exc
    return record


def native_processes():
    command = "Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Depth 3 -Compress"
    result = subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command],
                            check=True,capture_output=True,text=True,timeout=30)
    return json.loads(result.stdout)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package",type=Path,required=True)
    parser.add_argument("--supervision-uuid",required=True)
    args=parser.parse_args(argv)
    try:
        manifest=json_file(plain_path(args.package)/"candidate-manifest.json")
        if manifest.get("apply_helper_sha256") != sha(read_file(Path(__file__))):
            raise DeploymentBlocked("Installer differs from the sealed package helper")
        # Validate supporting code before importing the native coordination gate.
        validate_package(args.package,TARGET)
        sys.path.insert(0,str(TARGET))
        from datafetching.runtime_lock import runtime_lock_maintenance_gate
        result=apply_package(args.package,TARGET,DATASTORE,args.supervision_uuid,
            now_fn=lambda:datetime.now(timezone.utc),processes_fn=native_processes,
            lock_factory=lambda path:strict_native_lock(path,gate_factory=runtime_lock_maintenance_gate))
        print(json.dumps({"status":result["status"],"receipt":str(args.package/"apply-receipt.json"),
                          "manifest_sha256":result["manifest_sha256"],"orders_placed_by_helper":0}))
        return 0
    except Exception as exc:
        print(json.dumps({"status":"BLOCKED","error":f"{type(exc).__name__}: {exc}","orders_placed_by_helper":0}))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
