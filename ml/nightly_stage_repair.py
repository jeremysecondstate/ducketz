"""Audited, resumable source installation for one stopped nightly stage.

This module never launches preparation, a provider, a model or a trader. A
reviewed local specification binds candidate bytes and actual offline evidence.
Installation preserves original failure/state/receipts and never renews a date's
deadline. Run the existing coordinator separately after SOURCE_REPAIR_APPLIED.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re

from filelock import FileLock

from datafetching.runtime_lock import exclusive_runtime_lock, _pid_is_running
from ml.artifacts import file_checksum, utc_timestamp
from ml import nightly_workflow as workflow
from ml import nightly_repair_registry as registry

VERSION = "nightly-stage-source-repair-v1"
LEGACY_SOURCE_ROOTS = ("ml", "app", "datafetching", "tools")
SOURCE_ROOTS = (*LEGACY_SOURCE_ROOTS, "fundamentals", "options", "signals", "technicals")
LEGACY = ("prepare_stats", "model_review", "train_and_plan", "verify_display", "local_handoff")
SPLIT = ("datastore_catchup", "prepare_stats", "model_review", "train_and_plan",
         "local_gameplan", "verify_display", "local_handoff")
ROLES = {"datastore": ("datastore_catchup",), "stats": ("prepare_stats",),
         "model": ("model_review", "train_and_plan"), "gameplan": ("local_gameplan",),
         "display": ("verify_display", "local_handoff")}
STOPPED = {"FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED", "STOPPED"}
ORCHESTRATION = frozenset(("ml/nightly_workflow.py", "ml/nightly_dispatch.py", "ml/nightly_stage_repair.py",
                          "ml/overnight_runtime.py", "ml/nightly_recovery.py",
                          "tools/nightly_exchange.py"))
# These entry points do not run in the trader process. Other application edits
# require a quiescent trader; this helper never stops or restarts one.
LIVE_TRADER_SAFE = frozenset(("ml/nightly_workflow.py", "ml/nightly_dispatch.py", "ml/nightly_stage_repair.py",
                            "tools/nightly_exchange.py"))


def _encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _repository_claim(owner, repair_id, action_date, state_sha256, completion_record=None):
    identity = {"owner": owner, "repair_id": repair_id, "action_date": action_date,
                "domain": "preparation", "state_sha256": state_sha256}
    return {key: identity[key] for key in ("owner", "repair_id", "action_date", "domain")} | {
        "completion_record": completion_record, "token": sha256(_encoded(identity)).hexdigest()}


def _acquire_repository(config, record):
    """Retain a later completion binding when a null pre-edit claim retries."""
    current = registry.read(config["state_root"])
    if (current is not None and record["completion_record"] is None
            and all(current[key] == record[key] for key in registry.FIELDS - {"completion_record"})):
        registry.assert_owner(config["state_root"], current)
        return current
    return registry.acquire(config["state_root"], record)


def _same_original_owner(original, current):
    return (isinstance(original, dict) and isinstance(current, dict)
            and set(original) == set(current)
            and all(original[key] == current[key] for key in original if key != "completion_record")
            and original.get("completion_record") in (None, current.get("completion_record")))


def _immutable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Immutable repair evidence differs: " + path.name)
        return
    # Write once through an atomic link, so a crash cannot leave a partial
    # evidence file which another invocation mistakes for committed evidence.
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("Immutable repair evidence differs: " + path.name)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(repository, roots=SOURCE_ROOTS):
    return {path.relative_to(repository).as_posix(): file_checksum(path)
            for folder in roots
            for path in sorted((repository / folder).rglob("*.py"))}


def _spec_inventory(repository, spec):
    # Frozen legacy transactions retain their original four-root policy. New
    # transactions explicitly bind all production dependency roots; historical
    # specs and their evidence are never expanded or rewritten retroactively.
    roots = tuple(spec.get("source_roots", LEGACY_SOURCE_ROOTS))
    if roots not in (LEGACY_SOURCE_ROOTS, SOURCE_ROOTS):
        raise ValueError("Unknown frozen repair source inventory policy")
    return _inventory(repository, roots)


def _verify_selector_snapshot(config, spec, directory):
    selector = Path(config["datastore"]).resolve() / "ml/overnight-latest/run.json"
    frozen = spec.get("native_selector")
    if frozen is not None:
        if (frozen != {"path": str(selector), "snapshot": "selectors/overnight-latest-run.json",
                       "sha256": spec["native_files"].get(str(selector))}
                or file_checksum(directory / frozen["snapshot"]) != frozen["sha256"]):
            raise ValueError("Original native selector snapshot changed")
    return selector


def _relative(name):
    path = Path(name)
    if (not isinstance(name, str) or "\\" in name or ":" in name or path.is_absolute()
            or ".." in path.parts or path.as_posix() != name
            or path.suffix != ".py" or path.parts[0] not in ("ml", "app", "datafetching", "tools")
            or name.startswith("tools/cross_pc/")):
        raise ValueError("Repair paths must be explicit application Python source; pinned helpers stay untouched")
    return path


def _regular(root, name, *, optional=False):
    path = root / _relative(name)
    if root.resolve() not in path.resolve().parents or any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise ValueError("Repair source cannot escape through links")
    if not path.is_file() and not (optional and not path.exists()):
        raise ValueError("Repair source requires a regular file")
    return path


@contextmanager
def _locks(config):
    state_root = Path(config["state_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(state_root / "workflow.lock"), timeout=0):
        with FileLock(str(state_root / "stage-repair.lock"), timeout=0):
            with exclusive_runtime_lock(Path(config["datastore"]) / ".ducketz-overnight-runtime.lock",
                                        process_name="Audited nightly stage repair"):
                yield


def _process_created_at(pid):
    import psutil
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as error:
        raise ValueError("Process identity is unavailable") from error


def _assert_dead(payload):
    # A reused PID with a different recorded birth proves the original owner
    # exited. Missing birth evidence remains conservative. Never signal either.
    for field in ("owner_pid", "child_pid", "worker_pid"):
        pid = payload.get(field)
        if pid is None:
            continue
        if type(pid) is not int or pid <= 0:
            raise ValueError("Live or unverifiable " + field + " prevents source installation")
        if _pid_is_running(pid):
            saved_birth = payload.get(field.removesuffix("pid") + "created_at")
            if type(saved_birth) not in (int, float) or not math.isfinite(saved_birth) or saved_birth <= 0:
                raise ValueError("Live or unverifiable " + field + " prevents source installation")
            actual_birth = _process_created_at(pid)
            if actual_birth is not None and actual_birth == saved_birth:
                raise ValueError("Live or unverifiable " + field + " prevents source installation")


def _native_process_names():
    """Read authoritative names for Windows processes protected from psutil."""
    import subprocess
    if os.name != "nt":
        raise ValueError("Unknown process name; trader quiescence is unverified")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,Name | ConvertTo-Json -Compress"],
        capture_output=True, text=True, check=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rows = json.loads(result.stdout)
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list):
        raise ValueError("Native process inventory is unavailable")
    return {int(row["ProcessId"]): row["Name"] for row in rows}


def _trader_running(repository):
    import psutil
    native_names = None
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = process.info["cmdline"] or []
            text = " ".join(command).lower().replace("\\", "/")
            if any(marker in text for marker in ("ml.stock_trader", "start-gameplan-trader", "stock_trader/")):
                return True
            name = process.info["name"]
            if not name:
                if native_names is None:
                    native_names = _native_process_names()
                name = native_names.get(process.pid)
                if not isinstance(name, str) or not name:
                    if not process.is_running():
                        continue
                    raise ValueError("Unknown process name; trader quiescence is unverified")
            if "python" in name.lower() and not command:
                raise ValueError("Python process command unavailable; trader quiescence is unverified")
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied as error:
            raise ValueError("Process access denied; trader quiescence is unverified") from error
    return False


def _layout(state):
    return SPLIT if ("datastore_catchup" in state.get("steps", {})
                     or "local_gameplan" in state.get("steps", {})
                     or state.get("current_step") in ("datastore_catchup", "local_gameplan")
                     or state.get("workflow_layout") in ("responsibilities", "split")) else LEGACY


def _validate_state(config, state, now, *, check_binding=True):
    if state.get("status") not in STOPPED or state.get("current_step") not in _layout(state):
        raise ValueError("Only a failed or stopped unfinished nightly stage can be repaired")
    if state.get("actor") != config["actor"] or state.get("schema_version") != workflow.VERSION:
        raise ValueError("Repair workflow identity differs")
    _assert_dead(state)
    if check_binding:
        workflow._verify_configuration_binding(config, state)
        workflow._verify_symbol_binding(config, state)
    for entry in state.get("steps", {}).values():
        if entry.get("status") == "COMPLETE":
            workflow._verify_outputs(entry["output"])
    # An expired run can receive a repair but cannot gain continuation authority
    # from installation. Its frozen deadline/recovery record remains unchanged.
    native_files = {}
    native_runs = set()
    def bind_recovery(record, fields):
        for field in fields:
            recovery = record.get(field)
            if recovery is None:
                continue
            if not isinstance(recovery, dict) or not isinstance(recovery.get("path"), str):
                raise ValueError("Original recovery evidence has no frozen path: " + field)
            recovery_path = Path(recovery["path"])
            if (not recovery_path.is_absolute() or not recovery_path.is_file() or recovery_path.is_symlink()
                    or file_checksum(recovery_path) != recovery.get("sha256")):
                raise ValueError("Original recovery evidence changed: " + field)
            native_files[str(recovery_path)] = file_checksum(recovery_path)
    bind_recovery(state, ("recovery", "scheduled_recovery", "planning_tail_continuation"))
    for other in (Path(config["state_root"]) / "runs").glob("*/state.json"):
        candidate = workflow._json(other)
        if candidate.get("status") == "LOCAL_COMPLETE_PEER_SETUP_PENDING":
            continue
        _assert_dead(candidate)
        if candidate.get("status") == "RUNNING":
            raise ValueError("Interrupted workflow must record its stopped state before source repair")
        for entry in candidate.get("steps", {}).values():
            native = entry.get("native_run") or entry.get("output", {}).get("native_run")
            if not native:
                continue
            native_runs.add(Path(native).resolve())
    datastore = Path(config["datastore"]).resolve()
    latest = datastore / "ml/overnight-latest/run.json"
    if latest.exists():
        native_runs.add((datastore / workflow._json(latest)["run_path"]).resolve())
        native_files[str(latest)] = file_checksum(latest)
    # Native/legacy invocations can survive a dead parent before the outer
    # state saved their identity. An unrelated pointer must not hide that child.
    for report_path in (datastore / "ml/overnight-runs").glob("*/stage-report.json"):
        if workflow._json(report_path).get("status") == "RUNNING":
            native_runs.add(report_path.parent.resolve())
    for directory in sorted(native_runs):
        if directory.parent != (datastore / "ml/overnight-runs").resolve():
            raise ValueError("Native evidence escapes the datastore")
        report_path, receipt_path = directory / "stage-report.json", directory / "receipt.json"
        report = workflow._json(report_path)
        bind_recovery(report, ("recovery", "workflow_recovery", "deadline_exception"))
        _assert_dead(report)
        if report.get("status") == "RUNNING" or not receipt_path.is_file():
            raise ValueError("Native attempt needs a stopped/completed receipt before repair")
        receipt = workflow._json(receipt_path)
        if (receipt.get("status") != report.get("status")
                or receipt.get("stage_report_checksum_sha256") != file_checksum(report_path)):
            raise ValueError("Native report and receipt differ")
        for name, metadata in receipt.get("logs", {}).items():
            if Path(name).name != name or file_checksum(directory / name) != metadata["checksum_sha256"]:
                raise ValueError("Native failure log changed")
            native_files[str(directory / name)] = file_checksum(directory / name)
        native_files.update({str(path): file_checksum(path) for path in (report_path, receipt_path)})
    return native_files


def _validate_scope(state, changes, risk, invalidate_from, review_binding):
    layout = _layout(state)
    if risk not in ("orchestration", "data", "model", "planning", "display", "external_dependency"):
        raise ValueError("Explicit repair risk classification required")
    names = set(changes)
    if risk == "external_dependency":
        if (names or invalidate_from is not None or review_binding != "preserved"
                or state.get("failure", {}).get("kind") not in ("EXTERNAL_DEPENDENCY", "TRANSIENT")):
            raise ValueError("External resolution requires unchanged source and a saved dependency failure")
        return []
    if not names:
        raise ValueError("Source repairs need explicit changed paths")
    if risk == "orchestration" and not names <= ORCHESTRATION:
        raise ValueError("Orchestration repairs cannot relabel numerical/source dependencies")
    if risk == "planning" and not all(name.startswith(("ml/gameplan_trade_", "ml/joint_capital_", "ml/account_gameplan/"))
                                      or name in ORCHESTRATION or name == "ml/preparation_deadline.py" for name in names):
        raise ValueError("Planning repairs cannot relabel data/model dependencies")
    if risk == "display" and not all(name.startswith("app/ui/") or name in ORCHESTRATION for name in names):
        raise ValueError("Display repairs cannot relabel data/model dependencies")
    from ml.gameplan_model_feedback import _POLICY_FILES
    impacts_review = bool(names.intersection(_POLICY_FILES))
    if review_binding not in ("preserved", "invalidated") or (impacts_review and review_binding != "invalidated"):
        raise ValueError("Changed review policy requires explicit review-binding invalidation")
    required = (layout[0] if risk == "data" else "model_review"
                if risk == "model" or impacts_review or review_binding == "invalidated" else None)
    if invalidate_from is not None and invalidate_from not in layout:
        raise ValueError("Invalidation boundary is absent from this workflow layout")
    if required and (invalidate_from is None or layout.index(invalidate_from) > layout.index(required)):
        raise ValueError("Data/model dependencies require an explicit complete downstream invalidation")
    if invalidate_from is not None and layout.index(invalidate_from) > layout.index(state["current_step"]):
        raise ValueError("Invalidation must include the failed stage")
    return list(layout[layout.index(invalidate_from):]) if invalidate_from else []


def _retry_epochs(state, steps):
    result = {}
    for step in steps:
        entry = state.get("steps", {}).get(step, {})
        attempts = entry.get("attempts", 0)
        start = entry.get("retry_epoch_attempt_start", 0)
        if type(attempts) is not int or type(start) is not int or not 0 <= start <= attempts:
            raise ValueError("Saved retry epoch counters are invalid")
        result[step] = {"attempts": attempts, "retry_epoch_attempt_start": start,
                        "retry_epoch_id": entry.get("retry_epoch_id")}
    return result


def _claim_repository(config, record, state, raw, directory):
    """Continue a proved new failure without releasing the global owner fence."""
    previous = registry.read(config["state_root"])
    if previous is None or all(previous[key] == record[key]
                               for key in registry.FIELDS - {"completion_record"}):
        return _acquire_repository(config, record)
    if any(previous[key] != record[key] for key in ("owner", "domain", "action_date")):
        raise ValueError("Another repair owner holds the repository")
    if not verified_resume(config, previous, state):
        raise ValueError("Previous repair has no verified applied audit")
    prior_directory = directory.parent / previous["repair_id"]
    prior_spec = workflow._json(prior_directory / "spec.json")
    prior = workflow._json(prior_directory / "applied.json")
    step = state["current_step"]
    epochs = prior_spec["prior_retry_epochs"]
    entry = state.get("steps", {}).get(step, {})
    failure = state.get("failure") or {}
    if (step not in epochs or entry.get("status") not in STOPPED
            or type(entry.get("attempts")) is not int
            or entry["attempts"] <= epochs[step]["attempts"]
            or failure.get("disposition") != "OPEN"
            or failure.get("source_identity") != state["source_identity"]
            or not state.get("failed_at") or not failure.get("at")
            or utc_timestamp(state["failed_at"]) <= utc_timestamp(prior["applied_at"])
            or utc_timestamp(failure["at"]) <= utc_timestamp(prior["applied_at"])):
        raise ValueError("Continuation requires a newly failed resumed attempt with exact source evidence")
    # The original state and proof are committed before the atomic owner
    # replacement. A crash on either side resumes this exact new attempt.
    _immutable(directory / "before-state.json", raw)
    proof = {"schema_version": "nightly-stage-repair-continuation-v1",
             "previous": previous, "replacement": record,
             "previous_spec": {"path": str(prior_directory / "spec.json"),
                               "sha256": file_checksum(prior_directory / "spec.json")},
             "previous_applied": {"path": str(prior_directory / "applied.json"),
                                  "sha256": file_checksum(prior_directory / "applied.json")},
             "new_failed_state": {"path": str(directory / "before-state.json"),
                                  "sha256": sha256(raw).hexdigest()},
             "failure": failure, "step": step, "attempts": entry["attempts"],
             "source_identity": state["source_identity"]}
    proof_path = directory / "continuation.json"
    _immutable(proof_path, _encoded(proof))
    return registry.continue_owner(config["state_root"], previous, record,
        evidence={"path": str(proof_path), "sha256": file_checksum(proof_path)}, verified=True)


def claim(config, *, action_date, repair_id, owner, completion_record=None, now=None):
    """Reserve one failed stage before editing an isolated repair candidate.

    The stable repair identity precedes the queue's real completion identity. A
    crashed claimant is resumed using those identities; claims never expire into
    an automatic competing writer or silently extend a recovery deadline.
    """
    if (not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", repair_id)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", action_date)
            or not isinstance(owner, str) or not owner.strip()
            or (completion_record is not None and
                (not isinstance(completion_record, str) or not completion_record.strip()))):
        raise ValueError("Stable repair, owner, action date and completion identities required")
    state_path = Path(config["state_root"]).resolve() / "runs" / action_date / "state.json"
    directory = state_path.parent / "source-repairs" / repair_id
    with _locks(config):
        state = workflow._json(state_path)
        raw = state_path.read_bytes()
        repair_claim = {"owner": owner, "token": repair_id,
                        "failure_fingerprint": state.get("failure", {}).get("fingerprint")}
        if state.get("repair_claim") is not None:
            if state["repair_claim"] != repair_claim:
                raise ValueError("Another repair owner holds this failure")
            original_path = directory / "before-state.json"
            if not original_path.is_file():
                raise ValueError("Existing repair claim has no original evidence")
            original = workflow._json(original_path)
            if state != {**original, "repair_claim": repair_claim}:
                raise ValueError("Claimed failed state changed")
            state, raw = original, original_path.read_bytes()
        native_files = _validate_state(config, state, utc_timestamp(now))
        repository = Path(config["repository"]).resolve()
        if state.get("action_date") != action_date or state["source_identity"] != workflow.source_identity(repository):
            raise ValueError("Original source/action date differs")
        owner_record = {"repair_id": repair_id, "owner": owner, "completion_record": completion_record,
                        "failure": state.get("failure"), "state_sha256": sha256(raw).hexdigest()}
        owner_path = state_path.parent / "repair-owner.json"
        if owner_path.exists() and not _same_original_owner(owner_record, workflow._json(owner_path)):
            previous = workflow._json(owner_path)
            if not (state_path.parent / "source-repairs" / previous["repair_id"] / "applied.json").exists():
                raise ValueError("Another repair owner holds this failure")
        claim_path = directory / "claim.json"
        claimed_at = (workflow._json(claim_path)["claimed_at"] if claim_path.exists()
                      else utc_timestamp(now).isoformat())
        record = {"schema_version": VERSION, "config": config, "action_date": action_date,
                  "claim": owner_record, "repair_claim": repair_claim, "claimed_at": claimed_at,
                  "before_source": state["source_identity"], "before_files": _inventory(repository),
                  "native_files": native_files}
        if claim_path.exists():
            frozen = workflow._json(claim_path)
            if (not _same_original_owner(frozen.get("claim"), owner_record)
                    or any(frozen.get(key) != value for key, value in record.items() if key != "claim")):
                raise ValueError("Original pre-edit claim changed")
            record = frozen
        repository_claim = _repository_claim(owner, repair_id, action_date,
            owner_record["state_sha256"], record["claim"]["completion_record"])
        acquired = _claim_repository(config, repository_claim, state, raw, directory)
        _immutable(directory / "before-state.json", raw)
        _immutable(claim_path, _encoded(record))
        if not owner_path.exists() or not _same_original_owner(owner_record, workflow._json(owner_path)):
            workflow._write(owner_path, owner_record)
        workflow._write(state_path, {**state, "repair_claim": repair_claim})
        return {"status": "REPAIR_CLAIMED", "claim_path": str(claim_path),
                "repository_claim": acquired, **repair_claim}


def prepare(config, *, action_date, repair_id, candidate, changes, completion_record,
            checks, owner, risk, rationale, runtime_implications, review_binding="preserved",
            invalidate_from=None, reviewed=False, now=None):
    """Freeze review and evidence. checks entries bind runner exit code and log.

    This does not execute supplied commands. Actual offline commands and exit
    codes are supplied by the reviewed publication runner; logs and exact tested
    candidate source hashes are independently checked and frozen here.
    """
    if (not reviewed or not all(isinstance(value, str) and value.strip() for value in
            (completion_record, owner, rationale, runtime_implications))):
        raise ValueError("Reviewed completion, repair owner, rationale and runtime implications required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", repair_id) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", action_date):
        raise ValueError("Exact action date and stable repair identity required")
    if not isinstance(changes, dict) or not checks:
        raise ValueError("Exact source changes and actual passing checks required")
    if not Path(candidate).is_absolute() or str(candidate).startswith(("\\\\", "//")):
        raise ValueError("Reviewed candidate must be an absolute local path")
    repository, candidate = Path(config["repository"]).resolve(), Path(candidate).resolve()
    if repository == candidate:
        raise ValueError("Review candidate must be isolated from installed source")
    state_path = Path(config["state_root"]).resolve() / "runs" / action_date / "state.json"
    destination = state_path.parent / "source-repairs" / repair_id
    observed = utc_timestamp(now)
    with _locks(config):
        state = workflow._json(state_path)
        current_raw = state_path.read_bytes()
        existing_claim = state.get("repair_claim")
        repair_claim = {"owner": owner, "token": repair_id,
                        "failure_fingerprint": state.get("failure", {}).get("fingerprint")}
        if existing_claim is not None:
            if existing_claim != repair_claim:
                raise ValueError("Another repair owner holds this failure")
            saved_original = destination / "before-state.json"
            if not saved_original.is_file():
                raise ValueError("Existing repair claim has no original evidence")
            original = workflow._json(saved_original)
            if state != {**original, "repair_claim": repair_claim}:
                raise ValueError("Claimed failed state changed")
            state, current_raw = original, saved_original.read_bytes()
        native_files = _validate_state(config, state, observed)
        if state["action_date"] != action_date or state["source_identity"] != workflow.source_identity(repository):
            raise ValueError("Reviewed original source/action date differs")
        if changes and not (destination / "claim.json").is_file():
            raise ValueError("Source-changing preparation requires the original pre-edit claim")
        claim_path = state_path.parent / "repair-owner.json"
        claim = {"repair_id": repair_id, "owner": owner, "completion_record": completion_record,
                 "failure": state.get("failure"), "state_sha256": sha256(current_raw).hexdigest()}
        if claim_path.exists() and not _same_original_owner(workflow._json(claim_path), claim):
            prior = workflow._json(claim_path)
            previous = state_path.parent / "source-repairs" / prior["repair_id"] / "applied.json"
            if not previous.exists():
                raise ValueError("Another repair owner holds this failure")
        invalidated = _validate_scope(state, changes, risk, invalidate_from, review_binding)
        retry_epochs = _retry_epochs(state, invalidated or [state["current_step"]])
        if not set(changes) <= LIVE_TRADER_SAFE and _trader_running(repository):
            raise ValueError("Active trader requires a separately supported safe installation transition")
        before_files = _inventory(repository)
        if (destination / "claim.json").exists():
            frozen_claim = workflow._json(destination / "claim.json")
            if (frozen_claim.get("config") != config or frozen_claim.get("action_date") != action_date
                    or not _same_original_owner(frozen_claim.get("claim"), claim)
                    or frozen_claim.get("repair_claim") != repair_claim
                    or frozen_claim.get("before_source") != state["source_identity"]
                    or frozen_claim.get("before_files") != before_files
                    or frozen_claim.get("native_files") != native_files):
                raise ValueError("Original claim source, state or recovery evidence changed")
        initial_completion = (frozen_claim["claim"]["completion_record"]
                              if (destination / "claim.json").exists() else None)
        repository_claim = _repository_claim(owner, repair_id, action_date,
            claim["state_sha256"], initial_completion)
        _claim_repository(config, repository_claim, state, current_raw, destination)
        expected_files = dict(before_files)
        records, blobs = [], {}
        selector = Path(config["datastore"]).resolve() / "ml/overnight-latest/run.json"
        native_selector = None
        if str(selector) in native_files:
            frozen_selector = "selectors/overnight-latest-run.json"
            blobs[frozen_selector] = selector.read_bytes()
            if sha256(blobs[frozen_selector]).hexdigest() != native_files[str(selector)]:
                raise ValueError("Original native selector changed during review")
            native_selector = {"path": str(selector), "snapshot": frozen_selector,
                               "sha256": native_files[str(selector)]}
        for name, operation in changes.items():
            if operation not in ("add", "modify", "delete"):
                raise ValueError("Explicit add/modify/delete operation required")
            target = _regular(repository, name, optional=True)
            if target.exists() != (operation != "add"):
                raise ValueError("Source operation differs from installed existence")
            before = file_checksum(target) if target.exists() else None
            source = _regular(candidate, name, optional=operation == "delete")
            if operation == "delete" and source.exists():
                raise ValueError("Deleted source still exists in reviewed candidate")
            after = None if operation == "delete" else file_checksum(source)
            if after == before:
                raise ValueError("Repair must change reviewed source bytes")
            records.append({"path": name, "operation": operation, "before_sha256": before, "after_sha256": after})
            if before is not None:
                blobs["originals/" + name] = target.read_bytes()
            if after is not None:
                blobs["candidate/" + name] = source.read_bytes()
                expected_files[name] = after
            else:
                expected_files.pop(name)
        # Candidate must be the exact source dependency closure being installed.
        if _inventory(candidate) != expected_files:
            raise ValueError("Candidate contains missing or unreviewed source dependency changes")
        check_records = []
        for index, check in enumerate(checks):
            log = Path(check["log"]).resolve()
            command = check.get("command")
            if (check.get("exit_code") != 0 or type(check.get("exit_code")) is not int
                    or not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command)
                    or check.get("source_files") != expected_files
                    or not log.is_file() or log.is_symlink()):
                raise ValueError("Passing offline check must bind command, exit code, log and exact candidate source")
            for key in ("started_at", "completed_at"):
                if utc_timestamp(check[key]) > observed:
                    raise ValueError("Check evidence cannot be future dated")
            if utc_timestamp(check["started_at"]) > utc_timestamp(check["completed_at"]):
                raise ValueError("Check timestamps are reversed")
            blobs[f"checks/{index}.log"] = log.read_bytes()
            check_records.append({**check, "log": f"checks/{index}.log", "sha256": file_checksum(log)})
        repository_claim = registry.bind_completion(config["state_root"], repository_claim, completion_record)
        # The intent is written first. An interrupted prepare can resume exactly
        # these frozen inputs, but cannot silently take new candidate/test bytes.
        spec = {"schema_version": VERSION, "repair_id": repair_id, "owner": owner,
            "completion_record": completion_record, "config": config, "action_date": action_date,
            "state_path": str(state_path), "before_state_sha256": claim["state_sha256"],
            "before_source": state["source_identity"], "before_files": before_files,
            "expected_files": expected_files, "changes": records, "checks": check_records,
            "native_files": native_files, "risk": risk, "rationale": rationale,
            "runtime_implications": runtime_implications, "review_binding": review_binding,
            "invalidate_from": invalidate_from, "invalidated_steps": invalidated,
            "failure": state.get("failure"), "claim": claim, "repair_claim": repair_claim,
            "prior_retry_epochs": retry_epochs, "repository_claim": repository_claim,
            "source_roots": list(SOURCE_ROOTS), "native_selector": native_selector}
        _immutable(destination / "intent.json", _encoded(spec))
        workflow._write(claim_path, claim)
        _immutable(destination / "before-state.json", current_raw)
        for name, raw in blobs.items():
            _immutable(destination / name, raw)
        _immutable(destination / "spec.json", _encoded(spec))
        workflow._write(state_path, {**state, "repair_claim": repair_claim})
        return destination / "spec.json"


def _install_file(target, raw):
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        target.unlink()
    else:
        temporary = target.with_name(target.name + ".nightly-repair.tmp")
        with temporary.open("wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)


def apply(config, spec_path, *, owner, reviewed=False, now=None):
    """Apply one frozen transaction. Retry the exact spec after interrupted I/O."""
    if not reviewed:
        raise ValueError("Exact source repair review required")
    spec_path = Path(spec_path).resolve()
    spec = workflow._json(spec_path)
    if spec.get("schema_version") != VERSION or spec.get("config") != config or spec.get("owner") != owner:
        raise ValueError("Repair owner/configuration identity differs")
    if (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(spec.get("action_date", "")))
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", str(spec.get("repair_id", "")))):
        raise ValueError("Repair action date and identity differ")
    state_path = Path(config["state_root"]).resolve() / "runs" / spec["action_date"] / "state.json"
    directory = state_path.parent / "source-repairs" / spec["repair_id"]
    if spec_path != directory / "spec.json" or spec["state_path"] != str(state_path):
        raise ValueError("Repair evidence path differs")
    if (directory / "intent.json").read_bytes() != spec_path.read_bytes():
        raise ValueError("Repair specification differs from the original intent")
    repository = Path(config["repository"]).resolve()
    with _locks(config):
        _verify_selector_snapshot(config, spec, directory)
        repository_claim = spec.get("repository_claim") or _repository_claim(
            owner, spec["repair_id"], spec["action_date"], spec["before_state_sha256"], spec["completion_record"])
        registry.validate(repository_claim)
        if repository_claim != _repository_claim(owner, spec["repair_id"], spec["action_date"],
                                                 spec["before_state_sha256"], spec["completion_record"]):
            raise ValueError("Repository repair ownership differs from the frozen specification")
        current_repository_claim = registry.read(config["state_root"])
        if current_repository_claim is not None:
            registry.assert_owner(config["state_root"], repository_claim)
        state = workflow._json(state_path)
        original = workflow._json(directory / "before-state.json")
        spec_sha = file_checksum(spec_path)
        transitions = [*state.get("source_repairs", []), *state.get("dependency_restorations", [])]
        completed = next((entry for entry in transitions if entry.get("spec_sha256") == spec_sha), None)
        if completed:
            if (file_checksum(Path(completed["evidence"])) != completed["evidence_sha256"]
                    or file_checksum(directory / "before-state.json") != spec["before_state_sha256"]
                    or any(file_checksum(directory / check["log"]) != check["sha256"] for check in spec["checks"])):
                raise ValueError("Completed repair evidence changed")
            if _spec_inventory(repository, spec) != spec["expected_files"] or state["source_identity"] != workflow.source_identity(repository):
                raise ValueError("Installed repaired source changed")
            _immutable(directory / "applied.json", _encoded(completed))
            return {"status": "SOURCE_REPAIR_ALREADY_APPLIED", **completed}
        if spec.get("repository_claim") is None:
            # A legacy frozen transaction may resume without rewriting its
            # immutable specification. It must still acquire today's global fence.
            _acquire_repository(config, repository_claim)
        else:
            registry.assert_owner(config["state_root"], repository_claim)
        if (file_checksum(directory / "before-state.json") != spec["before_state_sha256"]
                or state_path.read_bytes() not in ((directory / "before-state.json").read_bytes(),
                                                  _encoded({**original, "repair_claim": spec["repair_claim"]}))):
            raise ValueError("Original failed state changed after review")
        if workflow._json(state_path.parent / "repair-owner.json") != spec["claim"]:
            raise ValueError("Repair ownership changed")
        if state.get("repair_claim") != spec["repair_claim"]:
            state["repair_claim"] = spec["repair_claim"]
            workflow._write(state_path, state)
        _validate_state(config, state, utc_timestamp(now))
        if workflow.source_identity(repository)["commit"] != spec["before_source"]["commit"]:
            raise ValueError("Operating Git revision changed")
        names = {record["path"]: record["operation"] for record in spec["changes"]}
        if len(names) != len(spec["changes"]) or _validate_scope(state, names, spec["risk"], spec["invalidate_from"], spec["review_binding"]) != spec["invalidated_steps"]:
            raise ValueError("Repair risk/invalidation differs")
        if _retry_epochs(state, spec["invalidated_steps"] or [state["current_step"]]) != spec["prior_retry_epochs"]:
            raise ValueError("Retry epoch changed since the repair review")
        if not set(names) <= LIVE_TRADER_SAFE and _trader_running(repository):
            raise ValueError("Active trader requires a separately supported safe installation transition")
        actual = _spec_inventory(repository, spec)
        for record in spec["changes"]:
            name = record["path"]
            target = _regular(repository, name, optional=True)
            present = file_checksum(target) if target.exists() else None
            if present not in (record["before_sha256"], record["after_sha256"]):
                raise ValueError("Installed file has unreviewed bytes")
            if record["before_sha256"] is None:
                actual.pop(name, None)
            else:
                actual[name] = record["before_sha256"]
                if file_checksum(directory / "originals" / name) != record["before_sha256"]:
                    raise ValueError("Original source backup changed")
            if record["after_sha256"] is not None and file_checksum(_regular(directory / "candidate", name)) != record["after_sha256"]:
                raise ValueError("Frozen candidate source changed")
        if actual != spec["before_files"]:
            raise ValueError("Unrelated application source changed")
        for check in spec["checks"]:
            if file_checksum(directory / check["log"]) != check["sha256"]:
                raise ValueError("Offline check evidence changed")
        for name, digest in spec["native_files"].items():
            if file_checksum(Path(name)) != digest:
                raise ValueError("Original native failure evidence changed")
        # Persist the exact future transition before touching any source. If a
        # crash lands after state save, applied.json can be finished identically.
        transition_path = directory / "transition.json"
        if transition_path.exists():
            transition = workflow._json(transition_path)
            if (transition.get("spec_sha256") != spec_sha or transition.get("owner") != owner
                    or transition.get("repair_id") != spec["repair_id"]
                    or transition.get("before_state_sha256") != spec["before_state_sha256"]
                    or transition.get("before_source") != spec["before_source"]
                    or transition.get("invalidated_steps") != spec["invalidated_steps"]
                    or transition.get("prior_retry_epochs") != spec["prior_retry_epochs"]
                    or transition.get("original_failure") != spec["failure"]
                    or transition.get("authorization") != spec["rationale"]
                    or transition.get("failed_state") != str(directory / "before-state.json")
                    or transition.get("failed_state_sha256") != spec["before_state_sha256"]
                    or transition.get("original_source_identity") != spec["before_source"]
                    or transition.get("at") != transition.get("applied_at")
                    or transition.get("deadline_at") != state["deadline_at"]
                    or transition.get("effective_deadline_at") != state.get("effective_deadline_at")):
                raise ValueError("Prepared transition differs from the frozen repair")
        else:
            transition = {"repair_id": spec["repair_id"], "owner": owner, "completion_record": spec["completion_record"],
                "spec_path": str(spec_path), "spec_sha256": spec_sha, "before_source": spec["before_source"],
                "before_state_sha256": spec["before_state_sha256"], "applied_at": utc_timestamp(now).isoformat(),
                "invalidated_steps": spec["invalidated_steps"], "retained_steps": [step for step, entry in state["steps"].items()
                    if entry.get("status") == "COMPLETE" and step not in spec["invalidated_steps"]],
                "original_failure": spec["failure"], "review_binding": spec["review_binding"],
                "prior_retry_epochs": spec["prior_retry_epochs"],
                "deadline_at": state["deadline_at"], "effective_deadline_at": state.get("effective_deadline_at"),
                "repository_claim": repository_claim}
            transition.update(original_source_identity=spec["before_source"],
                              evidence=str(spec_path), evidence_sha256=spec_sha,
                              at=transition["applied_at"], authorization=spec["rationale"],
                              failed_state=str(directory / "before-state.json"),
                              failed_state_sha256=spec["before_state_sha256"])
            _immutable(transition_path, _encoded(transition))
        for record in spec["changes"]:
            target = _regular(repository, record["path"], optional=True)
            present = file_checksum(target) if target.exists() else None
            if present not in (record["before_sha256"], record["after_sha256"]):
                raise ValueError("Installed file acquired unreviewed bytes during source installation")
            if present != record["after_sha256"]:
                raw = None if record["after_sha256"] is None else (directory / "candidate" / record["path"]).read_bytes()
                _install_file(target, raw)
        if _spec_inventory(repository, spec) != spec["expected_files"]:
            raise ValueError("Installed source inventory differs from reviewed candidate")
        transition["after_source"] = workflow.source_identity(repository)
        transition["reviewed_source_identity"] = transition["after_source"]
        # The immutable audit binds the completed exact-byte installation to its
        # original failed state. Frozen continuation validators consume this
        # schema; the prepare spec alone cannot bind the installed identity.
        audit_path = directory / "audit.json"
        _immutable(audit_path, _encoded(transition))
        transition["evidence"] = str(audit_path)
        transition["evidence_sha256"] = file_checksum(audit_path)
        for step in spec["invalidated_steps"]:
            if step in state["steps"]:
                prior = state["steps"][step]
                state["steps"][step] = {**{key: prior[key] for key in ("owner", "attempts") if key in prior},
                                        "status": "READY", "invalidated_by": spec_sha}
        for step, prior in spec["prior_retry_epochs"].items():
            if step in state["steps"] or step == (spec["invalidate_from"] or original["current_step"]):
                entry = state["steps"].setdefault(step, {})
                entry["retry_epoch_attempt_start"] = prior["attempts"]
                entry["retry_epoch_id"] = spec_sha
        # Availability restoration must not introduce a source-chain self-edge.
        # The full reviewed transaction still remains independently auditable.
        collection = ("dependency_restorations" if transition["after_source"] == spec["before_source"]
                      else "source_repairs")
        state.setdefault(collection, []).append(transition)
        state["source_identity"] = transition["after_source"]
        state.update(status="READY", owner_pid=None,
                     current_step=spec["invalidate_from"] or original["current_step"])
        # These are archived in before-state and the transition before removal.
        for key in ("error", "failed_at", "repair_claim"):
            state.pop(key, None)
        if spec["failure"] is not None:
            history = state.setdefault("failure_history", [])
            if spec["failure"] not in history:
                history.append(spec["failure"])
            state["failure"] = {**spec["failure"], "disposition": "RESOLVED", "resolved_at": transition["at"],
                                "evidence": str(spec_path), "evidence_sha256": spec_sha,
                                "corrective_action": spec["rationale"]}
        workflow._write(state_path, state)
        _immutable(directory / "applied.json", _encoded(transition))
        return {"status": "SOURCE_REPAIR_APPLIED", **transition,
                "continuation": "Use existing coordinator; original deadlines and authority still apply"}


def verified_resume(config, registry_record, state):
    """Caller holds workflow.lock; pending claims never authorize dispatch."""
    registry.validate(registry_record)
    if (registry_record["domain"] != "preparation"
            or registry_record["action_date"] != state.get("action_date")):
        return False
    current = registry.read(config["state_root"])
    if current is not None:
        registry.assert_owner(config["state_root"], registry_record)
    directory = (Path(config["state_root"]).resolve() / "runs" /
                 registry_record["action_date"] / "source-repairs" / registry_record["repair_id"])
    spec_path = directory / "spec.json"
    transitions = [*state.get("source_repairs", []), *state.get("dependency_restorations", [])]
    matched = [entry for entry in transitions if entry.get("repair_id") == registry_record["repair_id"]]
    if not matched:
        return False
    if len(matched) != 1:
        raise ValueError("Repair transition identity is duplicated")
    transition = matched[0]
    spec = workflow._json(spec_path)
    expected = _repository_claim(spec["owner"], spec["repair_id"], spec["action_date"],
                                 spec["before_state_sha256"], spec["completion_record"])
    if (registry_record != expected or spec.get("repository_claim", expected) != expected
            or spec.get("config") != config or spec.get("schema_version") != VERSION
            or spec_path.read_bytes() != (directory / "intent.json").read_bytes()
            or transition.get("spec_sha256") != file_checksum(spec_path)
            or transition.get("owner") != registry_record["owner"]
            or transition.get("completion_record") != registry_record["completion_record"]
            or transition.get("repository_claim", expected) != expected
            or transition.get("before_state_sha256") != spec["before_state_sha256"]
            or file_checksum(directory / "before-state.json") != spec["before_state_sha256"]):
        raise ValueError("Applied repair owner, specification or original state changed")
    original = workflow._json(directory / "before-state.json")
    retained = ("run_id", "action_date", "source_session", "actor", "deadline_at",
                "recovery_deadline_at", "effective_deadline_at", "scheduled_recovery",
                "recovery", "planning_tail_continuation")
    if any(state.get(key) != original.get(key) for key in retained):
        raise ValueError("Original workflow identity or frozen recovery deadline changed")
    invalidated = set(spec["invalidated_steps"])
    for step, entry in original.get("steps", {}).items():
        if entry.get("status") == "COMPLETE" and step not in invalidated:
            if state.get("steps", {}).get(step) != entry:
                raise ValueError("Original completed stage changed after source repair")
            workflow._verify_outputs(entry["output"])
    audit_path = directory / "audit.json"
    # Older reviewed transactions used the spec itself as evidence. Retain the
    # exact original reference, but never accept an arbitrary external path.
    evidence = Path(transition["evidence"])
    if evidence not in (audit_path, spec_path) or file_checksum(evidence) != transition["evidence_sha256"]:
        raise ValueError("Applied repair audit changed")
    if evidence == audit_path:
        audit = workflow._json(audit_path)
        expected_audit = {**transition, "evidence": str(spec_path),
                          "evidence_sha256": file_checksum(spec_path)}
        if audit != expected_audit:
            raise ValueError("Applied repair audit differs from saved transition")
    applied = directory / "applied.json"
    if applied.exists() and workflow._json(applied) != transition:
        raise ValueError("Applied repair completion record changed")
    if (_spec_inventory(Path(config["repository"]), spec) != spec["expected_files"]
            or state["source_identity"] != workflow.source_identity(Path(config["repository"]))
            or transition.get("after_source") != state["source_identity"]):
        raise ValueError("Installed repaired source changed before continuation")
    for check in spec["checks"]:
        if file_checksum(directory / check["log"]) != check["sha256"]:
            raise ValueError("Original repair check evidence changed")
    selector = _verify_selector_snapshot(config, spec, directory)
    for name, digest in spec["native_files"].items():
        if Path(name) == selector:
            # This is an advancing selector, not an original attempt's receipt.
            # New specs retain its pre-install bytes; legacy frozen specs keep
            # their original audit/hash and are never rewritten. Completion is
            # verified from the saved stage's explicit native output below.
            if not selector.is_file() or selector.is_symlink():
                raise ValueError("Native selector is unavailable or linked")
            pointer = workflow._json(selector)
            candidate = Path(config["datastore"]).resolve() / str(pointer.get("run_path", ""))
            run = candidate.resolve()
            if (run.parent != selector.parent.parent / "overnight-runs" or candidate.is_symlink()
                    or not (run / "stage-report.json").is_file()):
                raise ValueError("Advanced native selector escapes its retained run directory")
            continue
        if file_checksum(Path(name)) != digest:
            raise ValueError("Original native repair evidence changed")
    return True


def resolve_verified(config, registry_record, state):
    """Release only after saved failed-stage success; caller holds workflow.lock."""
    state_path = (Path(config["state_root"]).resolve() / "runs" /
                  registry_record["action_date"] / "state.json")
    if workflow._json(state_path) != state:
        raise ValueError("Verified repair resolution requires the saved workflow state")
    if not verified_resume(config, registry_record, state):
        return False
    directory = (Path(config["state_root"]).resolve() / "runs" /
                 registry_record["action_date"] / "source-repairs" / registry_record["repair_id"])
    original = workflow._json(directory / "before-state.json")
    failed_step = original["current_step"]
    entry = state.get("steps", {}).get(failed_step, {})
    if entry.get("status") != "COMPLETE":
        return False
    workflow._verify_outputs(entry["output"])
    if entry["output"].get("native_run"):
        run = Path(entry["output"]["native_run"]).resolve()
        if (run.parent != Path(config["datastore"]).resolve() / "ml/overnight-runs"
                or workflow._native_outputs(run) != entry["output"]):
            raise ValueError("Verified recovery native output differs from its completed receipt")
    resolution = {"schema_version": VERSION, "repository_claim": registry_record,
                  "action_date": state["action_date"], "run_id": state["run_id"],
                  "failed_step": failed_step, "verified_output": entry["output"],
                  "spec_sha256": file_checksum(directory / "spec.json"),
                  "source_identity": state["source_identity"]}
    _immutable(directory / "resolved.json", _encoded(resolution))
    current = registry.read(config["state_root"])
    if current is not None:
        registry.release(config["state_root"], registry_record, verified=True)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--reviewed", action="store_true")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--claim", type=Path, help="Reserve failure before editing; JSON action_date, repair_id, optional null completion_record")
    modes.add_argument("--prepare", type=Path, help="Reviewed local request JSON")
    modes.add_argument("--apply", type=Path, help="Exact frozen repair spec")
    args = parser.parse_args(argv)
    config = workflow.load_config(args.config)
    workflow.verify_installation(config)
    if args.claim:
        result = claim(config, owner=args.owner, **workflow._json(args.claim))
    elif args.prepare:
        request = workflow._json(args.prepare)
        result = {"spec": str(prepare(config, owner=args.owner, reviewed=args.reviewed, **request))}
    else:
        result = apply(config, args.apply, owner=args.owner, reviewed=args.reviewed)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
