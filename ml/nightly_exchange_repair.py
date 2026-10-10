"""Exact, resumable repairs of an unfinished exchange after completed preparation.

All evidence is private. This module never edits preparation, packet selections,
budgets, adoption receipts or trader controls, and never starts operating work.
An exchange adapter must check pending_claim before dispatch and verify_transition
instead of accepting unreviewed source drift. Source publication remains separate.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import uuid
import pandas as pd

from filelock import FileLock

from datafetching.runtime_lock import exclusive_runtime_lock, _pid_is_running
from ml import nightly_workflow as workflow
from ml import nightly_repair_registry as registry
from ml.artifacts import file_checksum, utc_timestamp

VERSION = "nightly-exchange-source-repair-v1"
# These entrypoints are not imported by the running trader. Shared numerical,
# UI and joint-adoption libraries deliberately need a separate deployment route.
ALLOWED = frozenset(("tools/nightly_exchange.py", "ml/nightly_exchange_repair.py",
                     "ml/nightly_repair_registry.py",
                     "ml/nightly_synthesis.py", "ml/nightly_handoff.py",
                     "ml/nightly_joint_readiness.py"))


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _read(path):
    return json.loads(Path(path).read_bytes())


def _atomic(path, raw, *, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Immutable repair evidence changed: " + path.name)
        return
    temporary = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise ValueError("Immutable repair evidence changed: " + path.name)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(repository):
    return {path.relative_to(repository).as_posix(): file_checksum(path)
            for folder in ("ml", "app", "datafetching", "tools", "fundamentals", "options", "signals", "technicals")
            for path in sorted((repository / folder).rglob("*.py"))}


def _source_from_files(commit, files):
    relevant = {name: value for name, value in files.items()
                if name.startswith(("ml/", "app/", "datafetching/"))}
    return {"commit": commit, "source_sha256": sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()}


def _native(config):
    native = workflow.load_config(Path(config["workflow_config"]))
    workflow.verify_installation(native)
    if config.get("actor") != native.get("actor") or config.get("private_exchange_authorized") is not True:
        raise ValueError("Explicit local exchange authority and matching actor required")
    for key in ("workflow_config", "state_root"):
        if not Path(config[key]).is_absolute():
            raise ValueError("Absolute local bindings required")
    return native


def _session(config, action_date):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(action_date)):
        raise ValueError("Exact action date required")
    return Path(config["state_root"]) / "sessions" / action_date


def _binding(config):
    # Delayed import avoids an adapter/helper import cycle.
    from tools.nightly_exchange import _binding as current_binding
    return current_binding(config)


def _owner_path(native):
    """Single adapter for the shared preparation/exchange repair-owner protocol."""
    return registry.path(native["state_root"])


def _guard(saved, completion_record=None):
    return {"owner": saved["owner"], "repair_id": saved["repair_id"],
            "completion_record": completion_record, "token": sha256(_bytes(saved)).hexdigest(),
            "action_date": saved["action_date"], "domain": "exchange"}


@contextmanager
def _locks(config, native):
    root = Path(native["state_root"])
    with FileLock(str(root / "workflow.lock"), timeout=0):
        with FileLock(str(Path(config["state_root"]) / "exchange.lock"), timeout=0):
            with FileLock(str(root / "stage-repair.lock"), timeout=0):
                with exclusive_runtime_lock(Path(native["datastore"]) / ".ducketz-overnight-runtime.lock",
                                            process_name="Reviewed exchange source repair"):
                    with ExitStack() as direct_runs:
                        for name in ("ownership-launch.lock", "ownership-responder.lock"):
                            direct_runs.enter_context(FileLock(str(Path(config["state_root"]) / name), timeout=0))
                        for status in sorted((Path(config["state_root"]) / "sessions").glob("*/status.json")):
                            if _read(status).get("status") == "COMPLETE":
                                continue
                            for operation in ("synthesis", "handoff"):
                                direct_runs.enter_context(exclusive_runtime_lock(
                                    status.parent / operation / (operation + ".lock"),
                                    process_name="Reviewed exchange source repair"))
                        yield


def _assert_dead(record):
    import psutil
    for key in ("owner_pid", "child_pid", "worker_pid"):
        pid = record.get(key)
        if pid is None:
            continue
        if type(pid) is not int or pid <= 0:
            raise ValueError("Unverifiable operating process prevents installation")
        if not _pid_is_running(pid):
            continue
        saved = record.get(key.removesuffix("pid") + "created_at")
        if type(saved) not in (float, int) or not math.isfinite(saved) or saved <= 0:
            raise ValueError("Live or unverifiable operating process prevents installation")
        try:
            current = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as error:
            raise ValueError("Unverifiable operating process prevents installation") from error
        if current == saved:
            raise ValueError("Live operating process prevents installation")


def _quiescent(native):
    for path in (Path(native["state_root"]) / "runs").glob("*/state.json"):
        value = _read(path)
        _assert_dead(value)
        if value.get("status") == "RUNNING":
            raise ValueError("Record interrupted preparation before source installation")
    for path in (Path(native["datastore"]) / "ml/overnight-runs").glob("*/stage-report.json"):
        value = _read(path)
        if value.get("status") == "RUNNING":
            _assert_dead(value)
            raise ValueError("Record interrupted native attempt before source installation")


def _preparation(config, native, action_date):
    path = Path(native["state_root"]) / "runs" / action_date / "state.json"
    state = _read(path)
    if (state.get("status") != "LOCAL_COMPLETE_PEER_SETUP_PENDING"
            or state.get("action_date") != action_date or state.get("actor") != config["actor"]
            or state.get("schema_version") != workflow.VERSION):
        raise ValueError("Exact completed local preparation required")
    workflow._verify_configuration_binding(native, state)
    workflow._verify_symbol_binding(native, state)
    for step in workflow.workflow_steps(state):
        entry = state.get("steps", {}).get(step, {})
        if entry.get("status") != "COMPLETE":
            raise ValueError("Preparation omits a completed stage")
        workflow._verify_outputs(entry["output"])
    workflow._verify_local_preparation(native, state)
    return path, state


def _protected(config, native, action_date, prep_path, state):
    """Save failure state as well as all immutable inputs; never copy peer paths."""
    session = _session(config, action_date)
    paths = {prep_path, Path(config["workflow_config"])}
    for key in ("local_profile", "coordination_active", "account_config"):
        if config.get(key):
            paths.add(Path(config[key]))
    for entry in state["steps"].values():
        paths.update(Path(name) for name in entry["output"].get("files", {}))
    for key in ("recovery", "scheduled_recovery", "planning_tail_continuation"):
        value = state.get(key)
        if isinstance(value, dict) and value.get("path"):
            path = Path(value["path"])
            if file_checksum(path) != value.get("sha256"):
                raise ValueError("Frozen recovery authority changed")
            paths.add(path)
    for path in session.rglob("*"):
        if (path.is_file() and "source-repairs" not in path.relative_to(session).parts
                and path.name not in {"repair-claim.json", "repair-transitions.json"}
                and not path.name.endswith(".lock") and ".pending-" not in path.name):
            paths.add(path)
    # Include any already adopted local plan/Stats bytes and their dated pins.
    root = Path(native["datastore"]).resolve()
    for folder, day in (("joint-gameplan-by-date", action_date),
                        ("nightly-joint-readiness-by-date", action_date),
                        ("gameplan-actuals-review-by-date", state["source_session"])):
        pin = root / "ml" / folder / day / "run.json"
        if pin.is_file():
            paths.add(pin)
            value = _read(pin)
            run = value.get("run_path")
            if run:
                directory = (root / run).resolve()
                if not directory.is_relative_to(root):
                    raise ValueError("Local publication escapes the datastore")
                paths.update(p for p in directory.rglob("*") if p.is_file())
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Protected evidence must be a regular local file")
        result[str(path.resolve())] = file_checksum(path)
    return result


def _check_hashes(hashes):
    for name, expected in hashes.items():
        path = Path(name)
        if path.is_symlink() or not path.is_file() or file_checksum(path) != expected:
            raise ValueError("Protected evidence changed: " + path.name)


def _transition_entries(session):
    path = session / "repair-transitions.json"
    return _read(path) if path.exists() else []


def original_binding(config, action_date, *, current_binding=None):
    """Read the original anchor, including a repair before the first export."""
    session = _session(config, action_date)
    path = session / "binding.json"
    if path.exists():
        return _read(path)
    entries = _transition_entries(session)
    if entries:
        return entries[0]["before_binding"]
    return _binding(config) if current_binding is None else current_binding


def _installed_binding(config, native):
    """Inspect the target application, even when this repair runs in isolation."""
    result = {"config": config, "workflow_config_sha256": file_checksum(Path(config["workflow_config"])),
              "profile_sha256": file_checksum(Path(config["local_profile"])),
              "coordination_active_sha256": file_checksum(Path(config["coordination_active"])),
              "adapter_sources": {name: file_checksum(Path(native["repository"]) / "tools" / name)
                                  for name in ("nightly_exchange.py", "nightly_account_snapshot.py", "nightly_ownership.py")}}
    if config["actor"] == "Atlas":
        result["account_config_sha256"] = file_checksum(Path(config["account_config"]))
    return result


def _grant_evidence(review, roles, retained=None, *, json_objects=False):
    """Authenticate actual reviewed local records without inferring authority."""
    if not isinstance(review, dict) or set(review) != roles | {"approved_grant"}:
        raise ValueError("Exact local peer-grant review and evidence required")
    if retained is not None and (not isinstance(retained, dict) or set(retained) != roles):
        raise ValueError("Exact retained local peer-grant evidence required")
    for role in roles:
        evidence = review[role]
        saved = evidence if retained is None else retained[role]
        for item in (evidence, saved):
            if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                    or not isinstance(item["path"], str) or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
                raise ValueError("Exact local peer-grant evidence path and digest required")
            path = Path(item["path"])
            if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
                raise ValueError("Absolute local peer-grant evidence required")
        path = Path(saved["path"])
        if (saved["sha256"] != evidence["sha256"] or path.is_symlink() or not path.is_file()
                or path.stat().st_size == 0 or file_checksum(path) != evidence["sha256"]):
            raise ValueError("Local peer-grant evidence missing or changed")
        if json_objects and not isinstance(_read(path), dict):
            raise ValueError("Actual local adoption and installation JSON objects required")


def _peer_grant_review(review, actor, retained=None):
    """Validate reviewed local evidence; metadata never supplies new authority."""
    _grant_evidence(review, {"local_human_instruction", "local_installation_receipt"}, retained)
    grant = review["approved_grant"]
    fields = {"authorized", "peer", "granted_on", "authority_source", "scope", "repeat_human_approval_required",
              "guidance", "completion_record", "boundaries"}
    scope = ["reviewed peer source implementation", "local system and helper installation",
             "local configuration and native bindings", "scheduled-task registration"]
    if (not isinstance(grant, dict) or set(grant) != fields or actor not in {"Atlas", "Scout"}
            or grant["authorized"] is not True or grant["repeat_human_approval_required"] is not False
            or grant["peer"] != {"Atlas": "Scout", "Scout": "Atlas"}[actor]
            or grant["granted_on"] != "2026-10-09" or grant["scope"] != scope
            or any(not isinstance(grant[key], str) or not grant[key].strip()
                   for key in ("authority_source", "guidance", "completion_record", "boundaries"))):
        raise ValueError("Peer-grant object exceeds the reviewed standing installation scope")


def _scout_handoff_review(review, actor, retained=None):
    """Recognize only Scout's evidenced October 9 additive four-string record.

    The standing direct human instruction supplies authority. These actual local
    receipts evidence its reviewed adoption; do not invent a historical message
    file or treat a peer notice as a new grant.
    """
    _grant_evidence(review, {"local_adoption_receipt", "local_installation_record"}, retained, json_objects=True)
    expected = {
        "authority": "Jeremy's direct local instruction to Scout on 2026-10-09",
        "boundaries": "Preserve private export rules, Atlas live execution, Jeremy trader controls, and separate runtime deployment",
        "scope": "Reviewed Atlas handoffs: Scout source, helpers, configuration, native bindings and task registration",
        "source_commit": "6d7795b3bd2df8a324f7bd95adb352750edc71d4",
    }
    if actor != "Scout" or _bytes(review["approved_grant"]) != _bytes(expected):
        raise ValueError("Scout handoff record differs from the exact reviewed standing grant")


def _coordination_documents(before_profile, after_profile, before_active, after_active, peer_grant_review=None,
                            scout_handoff_review=None):
    """Explicit administrative metadata changes, never operating authority."""
    permitted = {"coordination_notification_policy"}
    if peer_grant_review is not None:
        field = "peer_implementation_installation"
        if (field in before_profile or field not in after_profile
                or _bytes(after_profile[field]) != _bytes(peer_grant_review["approved_grant"])):
            raise ValueError("Peer-grant transition must add the exact reviewed absent-before object")
        permitted.add(field)
    if scout_handoff_review is not None:
        before_roles, after_roles = before_profile.get("operating_roles"), after_profile.get("operating_roles")
        field = "peer_handoff_adoption"
        if (not isinstance(before_roles, dict) or not isinstance(after_roles, dict)
                or field in before_roles or field not in after_roles
                or _bytes(after_roles[field]) != _bytes(scout_handoff_review["approved_grant"])):
            raise ValueError("Scout handoff must add the exact absent-before record to existing operating roles")
        # Project out this one evidenced addition, not its operating parent.
        after_profile = {**after_profile, "operating_roles": {key: value for key, value in after_roles.items() if key != field}}
    if (_bytes({key: value for key, value in before_profile.items() if key not in permitted})
            != _bytes({key: value for key, value in after_profile.items() if key not in permitted})
            or before_profile.get("coordination_notification_policy", "git_and_drive") not in {"git_and_drive", "github_only"}
            or after_profile.get("coordination_notification_policy") != "github_only"):
        raise ValueError("Coordination transition changes non-administrative profile fields")
    fields = {"commit", "contract_version", "manifest_sha256", "release_root", "previous"}
    release_files = {}
    for active in (before_active, after_active):
        if set(active) != fields or active["contract_version"] != before_profile.get("contract_version"):
            raise ValueError("Coordination installation identity differs")
        release = Path(active["release_root"])
        if not release.is_absolute() or str(release).startswith(("\\\\", "//")) or release.is_symlink():
            raise ValueError("Absolute local immutable coordination release required")
        manifest_path = release / "installation.json"
        if manifest_path.is_symlink() or file_checksum(manifest_path) != active["manifest_sha256"]:
            raise ValueError("Coordination installation manifest changed")
        manifest = _read(manifest_path)
        release_files[str(manifest_path.resolve())] = active["manifest_sha256"]
        if manifest.get("commit") != active["commit"] or not isinstance(manifest.get("files"), dict) or not 0 < len(manifest["files"]) <= 500:
            raise ValueError("Coordination installation manifest identity differs")
        for name, digest in manifest["files"].items():
            relative = Path(name)
            path = release / relative
            if (relative.is_absolute() or ".." in relative.parts or path.is_symlink()
                    or not path.resolve().is_relative_to(release.resolve()) or file_checksum(path) != digest):
                raise ValueError("Coordination installation file changed")
            release_files[str(path.resolve())] = digest
    if before_active != after_active:
        previous = after_active.get("previous")
        for _ in range(16):
            if previous == before_active:
                break
            if not isinstance(previous, dict):
                raise ValueError("Coordination installation lacks the exact prior pointer")
            previous = previous.get("previous")
        else:
            raise ValueError("Coordination installation ancestry is too deep")
    return release_files


def _verify_coordination_spec(spec):
    before, after = spec["before_binding"], spec["after_binding"]
    permitted = {"profile_sha256", "coordination_active_sha256"}
    if ({key: value for key, value in before.items() if key not in permitted}
            != {key: value for key, value in after.items() if key not in permitted}
            or before == after or spec["before_source"] != spec["after_source"]):
        raise ValueError("Coordination transition changes operating bindings or source")
    docs = {}
    for name, binding, field in (("before_profile", before, "profile_sha256"), ("after_profile", after, "profile_sha256"),
                                 ("before_active", before, "coordination_active_sha256"), ("after_active", after, "coordination_active_sha256")):
        path = Path(spec["documents"][name])
        if path.is_symlink() or file_checksum(path) != binding[field]:
            raise ValueError("Retained coordination document differs from its exact binding")
        docs[name] = _read(path)
    if spec.get("peer_grant_review") is not None and spec.get("scout_handoff_review") is not None:
        raise ValueError("Only one reviewed administrative grant addition is supported")
    for field, retained, validator in (("peer_grant_review", "retained_grant_evidence", _peer_grant_review),
                                        ("scout_handoff_review", "retained_scout_handoff_evidence", _scout_handoff_review)):
        review = spec.get(field)
        if review is not None:
            if "immutable_inputs" in spec and retained not in spec:
                raise ValueError("Sealed peer-grant proof lacks retained local evidence")
            actor = before["config"]["actor"]
            if docs["before_profile"].get("actor") != actor:
                raise ValueError("Peer-grant local actor differs from frozen binding")
            validator(review, actor, spec.get(retained))
        elif retained in spec:
            raise ValueError("Retained peer-grant evidence lacks its reviewed object")
    return _coordination_documents(**docs, peer_grant_review=spec.get("peer_grant_review"),
                                   scout_handoff_review=spec.get("scout_handoff_review"))


def coordination_transition(config, *, action_date, owner, repair_id, reason, completion_record,
                            before_profile, before_active, target_source, target_files, peer_grant_review=None,
                            scout_handoff_review=None,
                            reviewed=False, now=None):
    """Append proven administrative drift only; install no source or operating data.

    Run the reviewed helper in isolation before ordinary source repair. Its target
    inventory is the application's repository, never this candidate's __file__.
    The compatible transition permits the existing installed repair helper to
    claim the subsequent source repair without rewriting the original anchor.
    A grant addition needs the exact approved object plus locally reviewed human
    instruction and installation-receipt files, each with its actual SHA-256.
    Scout's evidenced nested record instead uses its actual reviewed adoption
    receipt and installation record; no historical human-message file is assumed.
    """
    if not reviewed or not owner or not reason or not completion_record or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", repair_id):
        raise ValueError("Reviewed owner, completion identity and rationale required")
    native = _native(config)
    if peer_grant_review is not None and scout_handoff_review is not None:
        raise ValueError("Only one reviewed administrative grant addition is supported")
    if peer_grant_review is not None:
        _peer_grant_review(peer_grant_review, config["actor"])
    if scout_handoff_review is not None:
        _scout_handoff_review(scout_handoff_review, config["actor"])
    session = _session(config, action_date)
    directory = session / "source-repairs" / repair_id
    with _locks(config, native):
        if registry.read(native["state_root"]) is not None or (session / "repair-claim.json").exists():
            raise ValueError("An existing repair owner prevents coordination transition")
        _quiescent(native)
        prep_path, preparation = _preparation(config, native, action_date)
        status = _read(session / "status.json")
        if status.get("status") not in {"PENDING", "FAILED"} or status.get("actor") != config["actor"] or status.get("action_date") != action_date:
            raise ValueError("Only this unfinished exchange can receive coordination transition")
        lease = session / "ownership-responder.json"
        if lease.exists():
            record = _read(lease)
            start, end = pd.Timestamp(record.get("started_at")), pd.Timestamp(record.get("deadline_at"))
            if (pd.isna(start) or pd.isna(end) or start.tzinfo is None or end.tzinfo is None
                    or not 0 < (end - start).total_seconds() <= 360
                    or record.get("action_date") != action_date or record.get("review_session") != preparation["source_session"]):
                raise ValueError("An unverifiable ownership responder prevents coordination transition")
            if end > utc_timestamp(now):
                raise ValueError("An unexpired ownership responder prevents coordination transition")
        current = _installed_binding(config, native)
        original = _read(session / "binding.json")
        source = workflow.source_identity(Path(native["repository"]))
        inventory = _inventory(Path(native["repository"]))
        if (source != preparation["source_identity"] or source != target_source or inventory != target_files):
            raise ValueError("Coordination transition cannot accept application source drift")
        entries = _transition_entries(session)
        if entries:
            if len(entries) != 1 or entries[0]["repair_id"] != repair_id:
                raise ValueError("Coordination transition requires the original unrepaired session")
            verify_transition(config, preparation, original, current_binding=current)
            saved = _read(directory / "spec.json")
            if (saved.get("owner") != owner or saved.get("completion_record") != completion_record
                    or _bytes(saved.get("peer_grant_review")) != _bytes(peer_grant_review)
                    or _bytes(saved.get("scout_handoff_review")) != _bytes(scout_handoff_review)
                    or saved.get("reason") != reason or file_checksum(Path(before_profile)) != original["profile_sha256"]
                    or file_checksum(Path(before_active)) != original["coordination_active_sha256"]):
                raise ValueError("Coordination transition retry identity changed")
            return {"status": "COORDINATION_TRANSITION_ALREADY_APPLIED", **entries[0]}
        spec_path = directory / "spec.json"
        if spec_path.exists():
            spec = _read(spec_path)
            if (spec.get("owner") != owner or spec.get("reason") != reason or spec.get("completion_record") != completion_record
                    or _bytes(spec.get("peer_grant_review")) != _bytes(peer_grant_review)
                    or _bytes(spec.get("scout_handoff_review")) != _bytes(scout_handoff_review)
                    or spec["before_binding"] != original or spec["after_binding"] != current
                    or spec["expected_files"] != inventory or spec["after_source"] != source
                    or spec["claim"]["preparation_sha256"] != file_checksum(prep_path)
                    or file_checksum(Path(before_profile)) != original["profile_sha256"]
                    or file_checksum(Path(before_active)) != original["coordination_active_sha256"]):
                raise ValueError("Interrupted coordination transition evidence changed")
        else:
            inputs = {"before_profile": Path(before_profile), "before_active": Path(before_active),
                      "after_profile": Path(config["local_profile"]), "after_active": Path(config["coordination_active"])}
            for name, path in inputs.items():
                if not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink() or not path.is_file():
                    raise ValueError("Actual retained absolute regular coordination documents required")
                expected = (original if name.startswith("before") else current)["profile_sha256" if name.endswith("profile") else "coordination_active_sha256"]
                if file_checksum(path) != expected:
                    raise ValueError("Retained coordination document differs from its exact binding")
            spec = {"schema_version": "nightly-coordination-transition-v1", "kind": "coordination_only",
                    "owner": owner, "reason": reason, "completion_record": completion_record,
                    "observed_at": utc_timestamp(now).isoformat(), "claim": {"preparation_sha256": file_checksum(prep_path)},
                    "before_source": source, "after_source": source, "before_binding": original, "after_binding": current,
                    "expected_files": inventory, "documents": {name: str(path) for name, path in inputs.items()}}
            if peer_grant_review is not None:
                spec["peer_grant_review"] = peer_grant_review
            if scout_handoff_review is not None:
                spec["scout_handoff_review"] = scout_handoff_review
            # Validate everything before creating even a private evidence directory.
            release_files = _verify_coordination_spec(spec)
            preserved = _protected(config, native, action_date, prep_path, preparation)
            docs = {}
            for name, path in inputs.items():
                target = directory / "documents" / (name + ".json")
                _atomic(target, path.read_bytes(), immutable=True)
                docs[name] = str(target)
            for field, retained_field, roles in (
                    ("peer_grant_review", "retained_grant_evidence", ("local_human_instruction", "local_installation_receipt")),
                    ("scout_handoff_review", "retained_scout_handoff_evidence", ("local_adoption_receipt", "local_installation_record"))):
                review = spec.get(field)
                if review is None:
                    continue
                retained = {}
                for role in roles:
                    evidence = review[role]
                    target = directory / "grant-evidence" / role
                    raw = Path(evidence["path"]).read_bytes()
                    if sha256(raw).hexdigest() != evidence["sha256"]:
                        raise ValueError("Local peer-grant evidence changed during retention")
                    _atomic(target, raw, immutable=True)
                    retained[role] = {"path": str(target), "sha256": evidence["sha256"]}
                spec[retained_field] = retained
            mutable = _mutable_exchange_pointers(session) | {str(Path(config[key]).resolve())
                                                           for key in ("local_profile", "coordination_active")}
            immutable = {name: digest for name, digest in preserved.items() if name not in mutable
                         and Path(name).name not in {"state.json", "status.json", "failure.json", "run.json"}}
            immutable[str(prep_path.resolve())] = file_checksum(prep_path)
            for entry in preparation["steps"].values():
                immutable.update(entry["output"].get("files", {}))
            immutable.update({name: file_checksum(Path(name)) for name in docs.values()})
            immutable.update(release_files)
            for field in ("retained_grant_evidence", "retained_scout_handoff_evidence"):
                immutable.update({item["path"]: item["sha256"] for item in spec.get(field, {}).values()})
            backups = {}
            for name, digest in preserved.items():
                if name in _mutable_exchange_pointers(session) or Path(name).name in {"state.json", "status.json", "failure.json", "run.json"}:
                    target = directory / "preserved" / str(len(backups))
                    raw = Path(name).read_bytes()
                    if sha256(raw).hexdigest() != digest:
                        raise ValueError("Original exchange evidence changed during coordination transition")
                    _atomic(target, raw, immutable=True)
                    backups[name] = {"path": str(target), "sha256": digest}
                    immutable[str(target)] = digest
            spec.update(documents=docs, immutable_inputs=immutable, preserved_inputs=preserved, preserved_documents=backups)
            _check_hashes(preserved)
            _atomic(spec_path, _bytes(spec), immutable=True)
        _verify_coordination_spec(spec)
        _check_hashes(spec["immutable_inputs"])
        if _installed_binding(config, native) != current or _inventory(Path(native["repository"])) != inventory:
            raise ValueError("Target application changed during coordination transition")
        entry = {"repair_id": repair_id, "spec_path": str(spec_path), "spec_sha256": file_checksum(spec_path),
                 "kind": "coordination_only", "owner": owner, "completion_record": completion_record,
                 "applied_at": spec["observed_at"], "before_source": source, "after_source": source,
                 "before_binding": original, "after_binding": current}
        _atomic(directory / "applied.json", _bytes(entry), immutable=True)
        _atomic(session / "repair-transitions.json", _bytes([entry]))
        return {"status": "COORDINATION_TRANSITION_APPLIED", **entry}


def effective_binding(config, action_date, *, current_binding=None, historical_binding=None):
    """Authenticate current and (for an expired lease) exact prior binding bytes."""
    native = _native(config)
    _, preparation = _preparation(config, native, action_date)
    current = _binding(config) if current_binding is None else current_binding
    original = original_binding(config, action_date, current_binding=current)
    verify_transition(config, preparation, original, current_binding=current)
    if historical_binding is not None:
        known = [original] + [entry["after_binding"] for entry in _transition_entries(_session(config, action_date))]
        if historical_binding not in known:
            raise ValueError("Ownership lease has no authenticated historical binding")
    return current


def _mutable_exchange_pointers(session):
    # Each original is retained in protected failure evidence; these protocol
    # cursors may advance normally. Selected specs/cache/receipts stay immutable.
    return {str((session / name).resolve()) for name in ("ownership-request.json", "ownership-responder.json",
                                                       "ownership-responder-result.json", "snapshot-publication.json")}


def verify_transition(config, preparation, original_binding, *, current_binding=None):
    """Validate an append-only source path; never rebind historical preparation."""
    native = _native(config)
    repository = Path(native["repository"])
    current_binding = _binding(config) if current_binding is None else current_binding
    session = _session(config, preparation["action_date"])
    source, binding = preparation["source_identity"], original_binding
    entries = _transition_entries(session)
    seen = set()
    for entry in entries:
        spec_path = Path(entry["spec_path"])
        directory = session / "source-repairs" / entry["repair_id"]
        if (spec_path != directory / "spec.json" or entry["repair_id"] in seen
                or file_checksum(spec_path) != entry["spec_sha256"]):
            raise ValueError("Repair transition identity changed")
        seen.add(entry["repair_id"])
        spec = _read(spec_path)
        if spec.get("kind") == "coordination_only":
            _verify_coordination_spec(spec)
        applied = _read(directory / "applied.json")
        if (applied != entry or spec["claim"]["preparation_sha256"] != file_checksum(
                Path(native["state_root"]) / "runs" / preparation["action_date"] / "state.json")
                or spec["before_source"] != source or spec["before_binding"] != binding
                or entry["before_source"] != source or entry["before_binding"] != binding
                or entry["after_source"] != spec["after_source"]
                or entry["after_binding"] != spec["after_binding"]):
            raise ValueError("Broken reviewed exchange source transition")
        _check_hashes(spec["immutable_inputs"])
        source, binding = entry["after_source"], entry["after_binding"]
    if source != workflow.source_identity(repository) or binding != current_binding:
        raise ValueError("Unreviewed exchange source or immutable private binding change")
    if entries and _inventory(repository) != spec["expected_files"]:
        raise ValueError("Unreviewed exchange dependency source change")
    return {"verified": True, "repairs": len(entries)}


def pending_claim(config, action_date):
    path = _session(config, action_date) / "repair-claim.json"
    if not path.exists():
        return None
    claim = _read(path)
    directory = path.parent / "source-repairs" / claim["repair_id"]
    # Applied is permission for the ordinary exchange to resume; ownership is
    # retained until verified resolution, so another repair cannot race it.
    entries = _transition_entries(path.parent)
    applied = directory / "applied.json"
    return None if applied.exists() and _read(applied) in entries else claim


def claim(config, *, action_date, owner, repair_id, reason, reviewed=False, now=None,
          supersede_applied=False):
    """Reserve one failure before source edits, with immutable private evidence."""
    if not reviewed or not owner or not reason or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", repair_id):
        raise ValueError("Explicit repair owner, rationale and stable identity required")
    native = _native(config)
    session = _session(config, action_date)
    directory = session / "source-repairs" / repair_id
    with _locks(config, native):
        existing = session / "repair-claim.json"
        global_owner = _owner_path(native)
        previous = None
        if existing.exists():
            saved = _read(existing)
            if saved["repair_id"] == repair_id and saved["owner"] == owner:
                _owned(config, native, directory / "claim.json", owner)
                return directory / "claim.json"
            old = session / "source-repairs" / saved["repair_id"] / "resolved.json"
            if not old.is_file():
                applied = old.with_name("applied.json")
                if not (supersede_applied and saved["owner"] == owner and applied.is_file()
                        and _read(applied) in _transition_entries(session)):
                    raise ValueError("Another owner holds this exchange failure")
                active = registry.read(native["state_root"])
                if not ((directory / "claim.json").exists() and active == _guard(_read(directory / "claim.json"))):
                    _owned(config, native, old.with_name("claim.json"), owner)
                previous = saved["repair_id"]
        resuming = (directory / "claim.json").exists()
        if global_owner.exists() and previous is None:
            guard = _read(global_owner)
            if not (resuming and guard.get("repair_id") == repair_id
                    and guard.get("owner") == owner and guard.get("token") == file_checksum(directory / "claim.json")):
                raise ValueError("Another source repair owns the repository")
        _quiescent(native)
        prep_path, state = _preparation(config, native, action_date)
        failure = _read(session / "status.json")
        if failure.get("status") not in {"FAILED", "PENDING"}:
            raise ValueError("Only an unfinished failed or dependency-blocked exchange can be repaired")
        if failure.get("action_date") != action_date or failure.get("actor") != config["actor"]:
            raise ValueError("Exchange failure identity differs")
        if previous is not None:
            old_claim = _read(session / "source-repairs" / previous / "claim.json")
            if file_checksum(session / "status.json") == old_claim["failure_sha256"]:
                raise ValueError("Unchanged failure cannot create another automatic repair attempt")
        binding = _binding(config)
        original = original_binding(config, action_date, current_binding=binding)
        verify_transition(config, state, original, current_binding=binding)
        hashes = _protected(config, native, action_date, prep_path, state)
        record = {"schema_version": VERSION, "repair_id": repair_id, "owner": owner,
                  "actor": config["actor"], "action_date": action_date, "reason": reason,
                  "claimed_at": utc_timestamp(now).isoformat(), "config": config,
                  "preparation_sha256": file_checksum(prep_path), "failure": failure,
                  "failure_sha256": file_checksum(session / "status.json"),
                  "before_source": workflow.source_identity(Path(native["repository"])),
                  "before_binding": binding, "before_files": _inventory(Path(native["repository"])),
                  "protected": hashes, "supersedes_applied_repair": previous}
        if resuming:
            original = _read(directory / "claim.json")
            record["claimed_at"] = original["claimed_at"]
            if record != original:
                raise ValueError("Interrupted claim evidence changed")
        # Preserve original bytes before any public claim can reference them.
        for name in hashes:
            _atomic(directory / "original-evidence" / sha256(name.encode()).hexdigest(), Path(name).read_bytes(), immutable=True)
        _atomic(directory / "claim.json", _bytes(record), immutable=True)
        owner_record = _guard(record)
        if previous is not None:
            # The same owner explicitly verified the previous applied repair
            # still failed. Preserve that disposition before continuing repair.
            prior = _read(session / "source-repairs" / previous / "applied.json")["repository_claim"]
            proof = session / "source-repairs" / previous / "superseded.json"
            _atomic(proof, _bytes({
                "next_repair_id": repair_id, "owner": owner, "failure_sha256": record["failure_sha256"],
                "previous_applied_sha256": file_checksum(proof.with_name("applied.json")),
                "reason": reason}), immutable=True)
            registry.continue_owner(native["state_root"], prior, owner_record,
                                    evidence={"path": str(proof), "sha256": file_checksum(proof)}, verified=True)
        else:
            registry.acquire(native["state_root"], owner_record)
        _atomic(existing, _bytes(record))
        return directory / "claim.json"


def _owned(config, native, claim_path, owner):
    claim_path = Path(claim_path)
    value = _read(claim_path)
    directory = _session(config, value["action_date"]) / "source-repairs" / value["repair_id"]
    if (claim_path != directory / "claim.json" or value["config"] != config or value["owner"] != owner
            or _read(directory.parent.parent / "repair-claim.json") != value):
        raise ValueError("Repair claim identity differs")
    guard = registry.read(native["state_root"])
    if guard is None:
        raise ValueError("Repository repair owner differs")
    registry.assert_owner(native["state_root"], _guard(value, guard["completion_record"]))
    for name, digest in value["protected"].items():
        if file_checksum(directory / "original-evidence" / sha256(name.encode()).hexdigest()) != digest:
            raise ValueError("Original private failure evidence changed")
    return directory, value


def prepare(config, *, claim_path, owner, candidate, changes, completion_record, checks,
            rationale, runtime_implications, risk="source", reviewed=False, now=None):
    """Seal exact source and actual passing test evidence after the owner review."""
    if not reviewed or not completion_record or not checks or not rationale or not runtime_implications:
        raise ValueError("Reviewed completion, checks, rationale and runtime implications required")
    if risk not in {"source", "external_dependency"} or not isinstance(changes, dict):
        raise ValueError("Explicit repair classification required")
    if (risk == "source" and not changes) or (risk == "external_dependency" and changes):
        raise ValueError("Dependency restoration cannot change source")
    if not set(changes) <= ALLOWED or any(op not in {"add", "modify", "delete"} for op in changes.values()):
        raise ValueError("Repair exceeds supported independent entrypoint scope")
    if "delete" in changes.values():
        raise ValueError("Required operating repair entrypoints cannot be deleted")
    native = _native(config)
    repository, candidate = Path(native["repository"]).resolve(), Path(candidate).resolve()
    observed = utc_timestamp(now)
    if repository == candidate or repository in candidate.parents or candidate in repository.parents:
        raise ValueError("Review candidate must be isolated")
    with _locks(config, native):
        directory, saved = _owned(config, native, claim_path, owner)
        _quiescent(native)
        _check_hashes(saved["protected"])
        if _inventory(repository) != saved["before_files"] or _binding(config) != saved["before_binding"]:
            raise ValueError("Installed source changed after repair claim")
        expected = dict(saved["before_files"])
        records = []
        for name, operation in changes.items():
            before = expected.get(name)
            target = candidate / name
            if target.is_symlink() or not target.resolve().is_relative_to(candidate):
                raise ValueError("Candidate source escapes through a link")
            if (operation == "add") != (before is None):
                raise ValueError("Operation differs from original existence")
            if operation == "delete":
                if target.exists():
                    raise ValueError("Deleted candidate source still exists")
                after = None
                expected.pop(name)
            else:
                after = file_checksum(target)
                if before == after:
                    raise ValueError("Source repair must change reviewed bytes")
                expected[name] = after
                _atomic(directory / "candidate" / name, target.read_bytes(), immutable=True)
            if before is not None:
                _atomic(directory / "original-source" / name, (repository / name).read_bytes(), immutable=True)
            records.append({"path": name, "operation": operation, "before": before, "after": after})
        if _inventory(candidate) != expected:
            raise ValueError("Candidate contains missing or unreviewed source dependencies")
        evidence = []
        for check in checks:
            command = check.get("command")
            if (not isinstance(command, list) or not command
                    or any(not isinstance(item, str) or not item.strip() for item in command)
                    or type(check.get("exit_code")) is not int or check["exit_code"] != 0 or check.get("source_files") != expected
                    or not check.get("started_at") or not check.get("completed_at")):
                raise ValueError("Passing check must bind exact candidate source and execution")
            started, completed = pd.Timestamp(check["started_at"]), pd.Timestamp(check["completed_at"])
            if (pd.isna(started) or pd.isna(completed) or started.tzinfo is None or completed.tzinfo is None
                    or completed < started or completed > observed or started < pd.Timestamp(saved["claimed_at"])):
                raise ValueError("Invalid check timing")
            log = Path(check["log"])
            if not log.is_absolute() or log.is_symlink() or not log.is_file() or not log.stat().st_size:
                raise ValueError("Absolute regular passing check log required")
            saved_log = directory / "checks" / str(len(evidence))
            _atomic(saved_log, log.read_bytes(), immutable=True)
            evidence.append({**check, "log": str(saved_log), "log_sha256": file_checksum(saved_log)})
        before_binding = saved["before_binding"]
        after_binding = json.loads(json.dumps(before_binding))
        for item in records:
            if item["path"] == "tools/nightly_exchange.py":
                if item["after"] is None:
                    raise ValueError("The exchange entrypoint cannot be deleted")
                after_binding["adapter_sources"]["nightly_exchange.py"] = item["after"]
        mutable = _mutable_exchange_pointers(_session(config, saved["action_date"]))
        immutable_inputs = {name: digest for name, digest in saved["protected"].items()
                            if name not in mutable and Path(name).name not in {"state.json", "status.json", "failure.json", "run.json"}}
        # Completed prep is immutable even though partial adoption state is not.
        immutable_inputs[str((Path(native["state_root"]) / "runs" / saved["action_date"] / "state.json").resolve())] = saved["preparation_sha256"]
        registry.bind_completion(native["state_root"], _guard(saved), completion_record)
        spec = {"schema_version": VERSION, "claim": saved, "claim_sha256": file_checksum(Path(claim_path)),
                "repository_claim": registry.read(native["state_root"]),
                "prepared_at": observed.isoformat(), "completion_record": completion_record,
                "risk": risk, "rationale": rationale, "runtime_implications": runtime_implications,
                "changes": records, "checks": evidence, "expected_files": expected,
                "before_source": saved["before_source"], "after_source": _source_from_files(saved["before_source"]["commit"], expected),
                "before_binding": before_binding, "after_binding": after_binding,
                "immutable_inputs": immutable_inputs}
        if (directory / "spec.json").exists():
            spec["prepared_at"] = _read(directory / "spec.json")["prepared_at"]
        _atomic(directory / "spec.json", _bytes(spec), immutable=True)
        _atomic(directory / "prepared.json", _bytes({"spec_sha256": file_checksum(directory / "spec.json")}), immutable=True)
        return directory / "spec.json"


def _install_file(target, raw):
    if raw is None:
        target.unlink(missing_ok=True)
    else:
        _atomic(target, raw)


def apply(config, spec_path, *, owner, reviewed=False, now=None):
    """Resume an interrupted exact install; unchanged dependencies create no cycle."""
    if not reviewed:
        raise ValueError("Exact installation review required")
    native = _native(config)
    repository = Path(native["repository"])
    spec_path = Path(spec_path)
    spec = _read(spec_path)
    with _locks(config, native):
        directory, saved = _owned(config, native, spec_path.parent / "claim.json", owner)
        if (spec_path != directory / "spec.json" or spec.get("schema_version") != VERSION
                or spec["claim"] != saved or spec["claim_sha256"] != file_checksum(directory / "claim.json")):
            raise ValueError("Frozen repair specification identity differs")
        if _read(directory / "prepared.json") != {"spec_sha256": file_checksum(spec_path)}:
            raise ValueError("Prepared specification changed after review")
        registry.assert_owner(native["state_root"], _guard(saved, spec["completion_record"]))
        if spec.get("repository_claim") != _guard(saved, spec["completion_record"]):
            raise ValueError("Frozen repository claim differs")
        if workflow.source_identity(repository)["commit"] != saved["before_source"]["commit"]:
            raise ValueError("Operating Git revision changed")
        actual = _inventory(repository)
        normalized = dict(actual)
        for item in spec["changes"]:
            name = item["path"]
            if name not in ALLOWED or actual.get(name) not in {item["before"], item["after"]}:
                raise ValueError("Source differs from exact before/after installation bytes")
            if item["before"] is None:
                normalized.pop(name, None)
            else:
                normalized[name] = item["before"]
                if file_checksum(directory / "original-source" / name) != item["before"]:
                    raise ValueError("Original source evidence changed")
            if item["after"] is not None and file_checksum(directory / "candidate" / name) != item["after"]:
                raise ValueError("Reviewed source evidence changed")
        if normalized != saved["before_files"]:
            raise ValueError("Unrelated installed source changed")
        for check in spec["checks"]:
            if check["exit_code"] != 0 or check["source_files"] != spec["expected_files"] or file_checksum(Path(check["log"])) != check["log_sha256"]:
                raise ValueError("Original offline check evidence changed")
        result = {"schema_version": VERSION, "repair_id": saved["repair_id"], "owner": owner,
                  "repository_claim": spec["repository_claim"],
                  "spec_path": str(spec_path), "spec_sha256": file_checksum(spec_path),
                  "completion_record": spec["completion_record"], "risk": spec["risk"],
                  "before_source": spec["before_source"], "after_source": spec["after_source"],
                  "before_binding": spec["before_binding"], "after_binding": spec["after_binding"]}
        entries = _transition_entries(directory.parent.parent)
        prior = directory / "applied.json"
        if prior.exists() and result in entries:
            if _read(prior) != result:
                raise ValueError("Original applied repair receipt changed")
            session = directory.parent.parent
            preparation = _read(Path(native["state_root"]) / "runs" / saved["action_date"] / "state.json")
            original = original_binding(config, saved["action_date"], current_binding=saved["before_binding"])
            verify_transition(config, preparation, original)
            return {"status": "EXCHANGE_REPAIR_ALREADY_APPLIED", **result}
        if any(item["repair_id"] == saved["repair_id"] for item in entries) and not prior.exists():
            raise ValueError("Immutable applied repair receipt is missing")
        _quiescent(native)
        _check_hashes(saved["protected"])
        intent = directory / "install-intent.json"
        _atomic(intent, _bytes(result), immutable=True)
        for item in spec["changes"]:
            target = repository / item["path"]
            current = file_checksum(target) if target.exists() else None
            if current not in (item["before"], item["after"]):
                raise ValueError("Installed file acquired unreviewed bytes during source installation")
            if current != item["after"]:
                raw = None if item["after"] is None else (directory / "candidate" / item["path"]).read_bytes()
                if raw is not None and sha256(raw).hexdigest() != item["after"]:
                    raise ValueError("Reviewed candidate source changed during installation")
                _install_file(target, raw)
        if (_inventory(repository) != spec["expected_files"] or workflow.source_identity(repository) != spec["after_source"]
                or _binding(config) != spec["after_binding"]):
            raise ValueError("Installed source differs from reviewed expected identity")
        _check_hashes(saved["protected"])
        # Order makes interruption resumable: immutable applied record first,
        # then append exactly once; pending_claim checks both before dispatch.
        _atomic(directory / "applied.json", _bytes(result), immutable=True)
        entries = _transition_entries(directory.parent.parent)
        if not any(entry["repair_id"] == saved["repair_id"] for entry in entries):
            entries.append(result)
            _atomic(directory.parent.parent / "repair-transitions.json", _bytes(entries))
        elif result not in entries:
            raise ValueError("Existing repair transition differs")
        return {"status": "EXCHANGE_REPAIR_APPLIED", **result}


def resolve(config, *, action_date, owner, verified_result, now=None):
    """Release the same owner only after exact exchange completion verification."""
    native = _native(config)
    session = _session(config, action_date)
    with _locks(config, native):
        saved = _read(session / "repair-claim.json")
        directory = session / "source-repairs" / saved["repair_id"]
        if registry.read(native["state_root"]) is not None:
            _owned(config, native, directory / "claim.json", owner)
        elif not (directory / "resolved.json").exists():
            raise ValueError("Repair resolution has no matching owner")
        from tools.nightly_exchange import _completed_result
        actual = _completed_result(config, native, action_date, saved["failure"]["review_session"], {})
        if actual is None or actual != verified_result or actual.get("status") != "COMPLETE":
            raise ValueError("Exact completed exchange verification required")
        if registry.read(native["state_root"]) is not None:
            preparation = _read(Path(native["state_root"]) / "runs" / action_date / "state.json")
            original = original_binding(config, action_date, current_binding=saved["before_binding"])
            verify_transition(config, preparation, original)
        resolution = {"schema_version": VERSION, "repair_id": saved["repair_id"],
                "owner": owner, "verified_result": actual, "resolved_at": utc_timestamp(now).isoformat()}
        if (directory / "resolved.json").exists():
            resolution["resolved_at"] = _read(directory / "resolved.json")["resolved_at"]
        _atomic(directory / "resolved.json", _bytes(resolution), immutable=True)
        guard = registry.read(native["state_root"])
        if guard is not None:
            registry.release(native["state_root"], guard, verified=True)
        return {"status": "EXCHANGE_REPAIR_VERIFIED", "repair_id": saved["repair_id"]}


def dispatch_guard(config, action_date, *, now=None):
    """Cheap durable gate; no inference, providers, synthesis or account reads."""
    native = _native(config)
    owner = registry.read(native["state_root"])
    if owner is not None:
        if owner["domain"] != "exchange" or owner["action_date"] != action_date:
            return {"reason": "SOURCE_REPAIR_OWNED", "owner": owner["owner"], "repair_id": owner["repair_id"]}
        pending = pending_claim(config, action_date)
        if pending is not None:
            return {"reason": "SOURCE_REPAIR_OWNED", "owner": owner["owner"], "repair_id": owner["repair_id"]}
        entry = next((item for item in _transition_entries(_session(config, action_date))
                      if item["repair_id"] == owner["repair_id"]), None)
        if entry is None or entry.get("repository_claim") != owner:
            raise ValueError("Applied exchange repair does not bind its repository owner")
    session = _session(config, action_date)
    path = session / "failure.json"
    if not path.exists():
        return None
    failure = _read(path)
    entries = _transition_entries(session)
    epoch = entries[-1]["spec_sha256"] if entries else None
    if failure["repair_epoch"] != epoch:
        return None
    if failure["kind"] == "TRANSIENT" and failure["attempts"] < 3:
        if utc_timestamp(now) >= pd.Timestamp(failure["retry_after"]):
            return None
        reason = "TRANSIENT_RETRY_COOLDOWN"
    else:
        reason = "REPAIR_REQUIRED" if failure["kind"] == "SOURCE_DEFECT" else "DEPENDENCY_RESTORATION_REQUIRED"
    return {"reason": reason, "owner": failure["owner"], "failure_fingerprint": failure["fingerprint"],
            "corrective_action": failure["corrective_action"]}


def record_failure(config, action_date, error, *, now=None):
    """Caller holds exchange.lock. Keep original diagnostics and one retry epoch."""
    native = _native(config)
    session = _session(config, action_date)
    observed = utc_timestamp(now)
    detail = f"{type(error).__name__}: {error}"
    external = any(word in detail.lower() for word in ("quota", "usage limit", "unauthorized", "authentication", "credentials"))
    transient = isinstance(error, (OSError, TimeoutError, InterruptedError, ConnectionError))
    kind = "EXTERNAL_DEPENDENCY" if external else "TRANSIENT" if transient else "SOURCE_DEFECT"
    entries = _transition_entries(session)
    epoch = entries[-1]["spec_sha256"] if entries else None
    identity = {"error": detail, "repair_epoch": epoch}
    # Diagnostics must retain the original exception even when broken source
    # or unavailable bindings also prevent the optional fingerprint inventory.
    for key, lookup in (("source", lambda: workflow.source_identity(Path(native["repository"]))),
                        ("binding", lambda: _binding(config))):
        try:
            identity[key] = lookup()
        except Exception as evidence_error:
            identity[key] = {"unavailable": f"{type(evidence_error).__name__}: {evidence_error}"}
    fingerprint = sha256(_bytes(identity)).hexdigest()
    path = session / "failure.json"
    previous = _read(path) if path.exists() else None
    attempts = previous["attempts"] + 1 if previous and previous["repair_epoch"] == epoch else 1
    owners = native.get("responsibility_owners", {})
    owner = owners.get("reconciliation") or owners.get("repo") or f"{config['actor']} REPO RECONCILIATION"
    record = {"schema_version": VERSION, "actor": config["actor"], "action_date": action_date,
              "kind": kind, "fingerprint": fingerprint, "error": detail, "owner": owner,
              "attempts": attempts, "repair_epoch": epoch, "observed_at": observed.isoformat(),
              "retry_after": (observed + pd.Timedelta(minutes=5)).isoformat(),
              "corrective_action": "Inspect original evidence; claim once, reproduce, publish reviewed source or attest restored dependency, apply exact repair and resume the same exchange"}
    _atomic(session / "failures" / fingerprint / f"attempt-{attempts}.json", _bytes(record), immutable=True)
    _atomic(path, _bytes(record))
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("operation", choices=("claim", "prepare", "apply", "coordination_transition"))
    args = parser.parse_args(argv)
    from tools.nightly_exchange import load_config
    config = load_config(args.config)
    request = _read(args.request)
    result = globals()[args.operation](config, **request, reviewed=args.reviewed)
    print(json.dumps({"result": str(result)} if isinstance(result, Path) else result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
