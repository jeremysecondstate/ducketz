"""Adopt exact reviewed source between existing nightly responsibilities.

This local installer never starts/stops a worker. The workflow lock serializes
installation with preparation; busy ownership leaves ordinary dispatch alone.
Sealed completion evidence and a local human instruction authorize each byte.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from filelock import FileLock, Timeout


VERSION = "nightly-source-adoption-v1"
SOURCE_ROOTS = ("ml", "datafetching", "app")
TERMINAL = {"LOCAL_COMPLETE_PEER_SETUP_PENDING", "COMPLETE", "HANDOFF_VERIFIED_LOCAL"}
RECORD_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{32}\Z")


def _digest(raw):
    return sha256(raw).hexdigest()


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate adoption evidence field")
            result[key] = value
        return result
    return json.loads(Path(path).read_bytes(), object_pairs_hook=unique)


def _atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _immutable(path, raw):
    path = Path(path)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Original adoption evidence changed")
    else:
        _atomic_bytes(path, raw)


def source_inventory(repository):
    repository = Path(repository).resolve()
    result = {}
    for directory in SOURCE_ROOTS:
        for path in sorted((repository / directory).rglob("*.py")):
            if path.is_symlink() or not path.resolve().is_relative_to(repository):
                raise ValueError("Source inventory contains a linked path")
            result[path.relative_to(repository).as_posix()] = _digest(path.read_bytes())
    return result


def _identity(repository, inventory):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    return {"commit": commit, "source_sha256": _digest(json.dumps(inventory, sort_keys=True).encode())}


def source_identity(repository):
    return _identity(repository, source_inventory(repository))


def _installed_core(config):
    active_path = Path(config["coordination_active"])
    active = _read(active_path)
    release = Path(active["release_root"])
    manifest_path = release / "installation.json"
    if _digest(manifest_path.read_bytes()) != active["manifest_sha256"]:
        raise ValueError("Pinned adoption helper manifest changed")
    manifest = _read(manifest_path)
    if manifest.get("commit") != active["commit"]:
        raise ValueError("Pinned adoption helper revision changed")
    for name, digest in manifest["files"].items():
        path = release / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink()
                or not path.resolve().is_relative_to(release.resolve())
                or _digest(path.read_bytes()) != digest):
            raise ValueError("Pinned adoption helper file changed")
    required = {"tools/cross_pc/__init__.py", "tools/cross_pc/core.py", "tools/cross_pc/artifact_publish.py"}
    if not required.issubset(manifest["files"]):
        raise ValueError("Pinned adoption helpers are incomplete")
    package = "_nightly_adoption_cross_pc_" + active["commit"]
    if package not in sys.modules:
        folder = release / "tools/cross_pc"
        spec = importlib.util.spec_from_file_location(package, folder / "__init__.py",
                                                      submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module(package + ".core")


def _validated_spec(config, spec_path):
    raw = spec_path.read_bytes()
    spec = _read(spec_path)
    if (spec.get("schema_version") != VERSION or not RECORD_ID.fullmatch(spec.get("completion_record", ""))
            or Path(spec.get("repository", "")).resolve() != Path(config["repository"]).resolve()):
        raise ValueError("Pending adoption repository/record identity differs")
    core = _installed_core(config)
    profile = core.load(config["local_profile"])
    core.validate_profile(profile)
    if Path(profile["checkout"]).resolve() != Path(config["repository"]).resolve():
        raise ValueError("Adoption profile belongs to another checkout")
    receipts = core.state(profile)
    entry = receipts["records"][spec["completion_record"]]
    if entry["record_sha256"] != spec.get("record_sha256"):
        raise ValueError("Pending adoption sealed record digest differs")
    record = core.validate_record(profile, spec["completion_record"], receipts, current=False)
    if record.get("scope") != "shared":
        raise ValueError("Nightly source adoption requires reviewed shared source")
    authority = spec["installation_authority"]
    authority_path = Path(authority["path"])
    if (not authority_path.is_absolute() or authority_path.is_symlink()
            or _digest(authority_path.read_bytes()) != authority["sha256"]):
        raise ValueError("Direct human installation evidence changed")
    grant = _read(authority_path)
    files = spec.get("files", [])
    paths = [item["path"] for item in files]
    if (not files or len(set(path.casefold() for path in paths)) != len(paths)
            or grant.get("issuer") != "Jeremy" or grant.get("completion_record") != record["id"]
            or sorted(grant.get("authorized_paths", [])) != sorted(paths)
            or not isinstance(grant.get("instruction"), str) or not grant["instruction"].strip()
            or not isinstance(grant.get("recorded_at"), str) or not grant["recorded_at"]):
        raise ValueError("Direct human installation instruction does not bind these paths")
    owned = {item["path"]: item for item in record["files"]}
    private_values = core.source_private_values(profile)
    for item in files:
        name = item["path"]
        core.shareable(name)
        core.safe_path(config["repository"], name)
        if name not in owned or owned[name]["operation"] == "delete":
            raise ValueError("Adoption contains an unowned/deleted source path")
        digest = record["fingerprints"][name]
        payload = Path(item["payload_path"])
        if (not payload.is_absolute() or payload.is_symlink()
                or payload.resolve() != core.blob_path(profile, digest).resolve()
                or item["payload_sha256"] != digest or _digest(payload.read_bytes()) != digest):
            raise ValueError("Adoption payload differs from immutable reviewed snapshot")
        expected = item.get("expected_sha256")
        if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Adoption requires an exact live baseline hash")
        core.reject_source_private_values(payload.read_bytes(), private_values)
    effective = core.fingerprints(config["repository"], record["fingerprints"])
    for item in files:
        effective[item["path"]] = item["payload_sha256"]
    if effective != record["fingerprints"]:
        changed = sorted(name for name, digest in record["fingerprints"].items() if effective[name] != digest)
        raise ValueError("Installed dependency differs from sealed passing checks: " + ", ".join(changed))
    prior_ids = spec.get("reviewed_prior_source_records", [])
    run_inventory = spec.get("run_source_inventory", spec["baseline_source_inventory"])
    run_identity = spec.get("run_source_identity", spec["baseline_source_identity"])
    if _digest(json.dumps(run_inventory, sort_keys=True).encode()) != run_identity.get("source_sha256"):
        raise ValueError("Reviewed original run inventory does not match its identity")
    if not isinstance(prior_ids, list) or len(set(prior_ids)) != len(prior_ids):
        raise ValueError("Reviewed prior source record identities are ambiguous")
    delta = {name for name in set(run_inventory) | set(spec["baseline_source_inventory"])
             if run_inventory.get(name) != spec["baseline_source_inventory"].get(name)}
    covered = set()
    for record_id in prior_ids:
        if not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id):
            raise ValueError("Invalid prior source completion record identity")
        prior = core.validate_record(profile, record_id, receipts, current=False)
        if prior.get("scope") != "shared":
            raise ValueError("Prior source adoption requires reviewed shared source")
        for owned_file in prior["files"]:
            name = owned_file["path"]
            if name in delta and prior["fingerprints"][name] == spec["baseline_source_inventory"].get(name):
                covered.add(name)
    if delta != covered:
        raise ValueError("Unreviewed source delta between original run and live baseline")
    if delta or run_identity != spec["baseline_source_identity"]:
        if (not prior_ids or grant.get("reviewed_prior_source_records") != prior_ids
                or grant.get("run_source_identity") != run_identity
                or grant.get("baseline_source_identity") != spec["baseline_source_identity"]):
            raise ValueError("Direct human installation evidence does not authorize the prior source delta")
    if spec_path.read_bytes() != raw:
        raise ValueError("Pending adoption changed while validating evidence")
    return spec, _digest(raw), core, record


def _file_hash(path):
    if path.exists() and not path.is_file():
        raise ValueError("Adoption target is not a regular file")
    return _digest(path.read_bytes()) if path.is_file() else None


def _safe_local(repository, name):
    root = Path(repository).resolve()
    if not isinstance(name, str) or "\\" in name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("Adoption path escapes its source root")
    target = root / name
    if not target.resolve().is_relative_to(root):
        raise ValueError("Adoption path escapes its source root")
    for item in (target, *target.parents):
        if item == root:
            break
        if item.is_symlink() or item.exists() and getattr(item.stat(), "st_file_attributes", 0) & 1024:
            raise ValueError("Adoption path contains a linked target")
    return target


def _current_state(config, spec):
    root = Path(config["state_root"]).resolve()
    binding = spec["run_binding"]
    action = binding["action_date"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", action):
        raise ValueError("Adoption needs an exact action-date binding")
    path = root / "runs" / action / "state.json"
    if Path(binding["state_path"]).resolve() != path or path.is_symlink():
        raise ValueError("Adoption state path differs from its action-date binding")
    state = _read(path)
    if (state.get("run_id") != binding["run_id"] or state.get("action_date") != action
            or state.get("source_session") != binding["source_session"]
            or state.get("actor") != config["actor"]):
        raise ValueError("Adoption belongs to another current workflow run")
    latest = _read(root / "latest.json")
    if latest.get("run_id") != state["run_id"] or Path(latest.get("state_path", "")).resolve() != path:
        raise ValueError("Adoption current run selector changed")
    if any(state.get(key) for key in ("recovery", "scheduled_recovery", "planning_tail_continuation")):
        raise ValueError("Source-bound frozen recovery requires its separate reviewed adoption")
    if state.get("repair_claim") or (root / "repair-owner.json").exists():
        raise ValueError("An existing genuine repair claim retains its ownership")
    return path, state


def _prepare(config, spec, spec_sha, core, directory):
    path, state = _current_state(config, spec)
    if state.get("status") == "RUNNING" or state.get("owner_pid") is not None:
        return None
    inventory = source_inventory(config["repository"])
    before = _identity(config["repository"], inventory)
    if inventory != spec["baseline_source_inventory"] or before != spec["baseline_source_identity"]:
        raise ValueError("Live source inventory changed outside the approved baseline")
    run_identity = spec.get("run_source_identity", before)
    if state.get("source_identity") != run_identity:
        raise ValueError("Saved workflow source differs from the approved baseline")
    items = sorted(spec["files"], key=lambda item: (item.get("expected_sha256") is not None, item["path"]))
    for item in items:
        target = core.safe_path(config["repository"], item["path"])
        if _file_hash(target) != item.get("expected_sha256"):
            raise ValueError("Unknown live edit in " + item["path"])
    attempt = directory / "attempts" / uuid.uuid4().hex
    originals = []
    after_inventory = dict(inventory)
    for index, item in enumerate(items):
        target = core.safe_path(config["repository"], item["path"])
        original = attempt / f"original-{index}.bin"
        if item.get("expected_sha256") is not None:
            _immutable(original, target.read_bytes())
        originals.append({**item, "original_path": str(original)})
        if item["path"].split("/")[0] in SOURCE_ROOTS and item["path"].endswith(".py"):
            after_inventory[item["path"]] = item["payload_sha256"]
    after = _identity(config["repository"], after_inventory)
    original_state = path.read_bytes()
    audit = {"schema_version": VERSION, "completion_record": spec["completion_record"],
             "at": datetime.now(timezone.utc).isoformat(), "run_id": state["run_id"],
             "action_date": state["action_date"], "spec_sha256": spec_sha,
             "before_source": run_identity, "baseline_source": before, "after_source": after,
             "before_inventory": inventory, "after_inventory": after_inventory,
             "run_source_inventory": spec.get("run_source_inventory", inventory),
             "reviewed_prior_source_records": spec.get("reviewed_prior_source_records", []),
             "before_state_sha256": _digest(original_state), "paths": [item["path"] for item in items],
             "runtime_loaded": "Existing workers are unchanged; subsequent workers load the installed source"}
    audit_path = attempt / "audit.json"
    _immutable(audit_path, _encoded(audit))
    updated = dict(state)
    if state["status"] not in TERMINAL:
        updated["source_identity"] = after
        updated["source_adoptions"] = [*state.get("source_adoptions", []),
                                      {"path": str(audit_path), "sha256": _digest(audit_path.read_bytes())}]
        failure = state.get("failure")
        source_drift_errors = {
            "ValueError: Application source changed since this run; preserve it for explicit review",
            "ValueError: Application source changed during the nightly run",
        }
        if (run_identity != before and failure and failure.get("kind") == "SOURCE_DEFECT"
                and failure.get("error") in source_drift_errors
                and failure.get("source_identity") == run_identity):
            history = list(state.get("failure_history", []))
            if failure not in history:
                history.append(failure)
            updated["failure_history"] = history
            updated["failure"] = {**failure, "disposition": "RESOLVED", "resolved_at": audit["at"],
                                  "evidence": str(audit_path), "evidence_sha256": _digest(audit_path.read_bytes()),
                                  "corrective_action": "Adopt exact locally reviewed prior source records and this repair"}
            updated["status"] = "READY"
    state_before, state_after = attempt / "state-before.json", attempt / "state-after.json"
    _immutable(state_before, original_state)
    _immutable(state_after, original_state if updated == state else _encoded(updated))
    spec_path = attempt / "spec.json"
    spec_raw = (Path(config["state_root"]) / "pending-source-adoption.json").read_bytes()
    if _digest(spec_raw) != spec_sha:
        raise ValueError("Pending adoption changed before journaling")
    _immutable(spec_path, spec_raw)
    intent = {"schema_version": VERSION, "spec_sha256": spec_sha, "files": originals,
              "spec_path": str(spec_path),
              "state_path": str(path), "state_before": str(state_before), "state_after": str(state_after),
              "before_state_sha256": _digest(state_before.read_bytes()),
              "after_state_sha256": _digest(state_after.read_bytes()), "audit_path": str(audit_path),
              "audit_sha256": _digest(audit_path.read_bytes()), "before_source": before, "after_source": after,
              "before_inventory": inventory, "after_inventory": after_inventory}
    intent_path = attempt / "intent.json"
    _immutable(intent_path, _encoded(intent))
    journal = {"schema_version": VERSION, "completion_record": spec["completion_record"],
               "spec_sha256": spec_sha, "intent_path": str(intent_path),
               "intent_sha256": _digest(intent_path.read_bytes()), "stage": "PREPARED"}
    _atomic_bytes(directory / "journal.json", _encoded(journal))
    return journal


def _intent(directory, journal):
    path = Path(journal["intent_path"])
    if (not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink()
            or _digest(path.read_bytes()) != journal["intent_sha256"]):
        raise ValueError("Adoption journal intent changed")
    intent = _read(path)
    if intent["spec_sha256"] != journal["spec_sha256"]:
        raise ValueError("Adoption journal specification changed")
    for key in ("state_before", "state_after", "audit_path", "spec_path"):
        item = Path(intent[key])
        digest_key = {"state_before": "before_state_sha256", "state_after": "after_state_sha256",
                      "audit_path": "audit_sha256", "spec_path": "spec_sha256"}[key]
        if (not item.resolve().is_relative_to(directory.resolve()) or item.is_symlink()
                or _digest(item.read_bytes()) != intent[digest_key]):
            raise ValueError("Adoption journal snapshot changed")
    for item in intent["files"]:
        if item.get("expected_sha256") is None:
            continue
        original = Path(item["original_path"])
        if (not original.resolve().is_relative_to(directory.resolve()) or original.is_symlink()
                or _digest(original.read_bytes()) != item["expected_sha256"]):
            raise ValueError("Adoption original source snapshot changed")
    return intent


def _retire(config, directory, journal, pending):
    """Retire only this exact selector; installed history is never re-executed."""
    intent = _intent(directory, journal)
    raw = Path(intent["spec_path"]).read_bytes()
    spec = _read(intent["spec_path"])
    if (journal.get("stage") != "INSTALLED" or spec.get("schema_version") != VERSION
            or spec.get("completion_record") != journal["completion_record"]
            or Path(spec.get("repository", "")).resolve() != Path(config["repository"]).resolve()
            or _digest(raw) != journal["spec_sha256"]):
        raise ValueError("Installed adoption retirement identity differs from its journal")
    _immutable(directory / "applied-spec.json", raw)
    if pending.exists() and pending.read_bytes() == raw:
        pending.unlink()
        return True
    return False  # A newer selector retains its separate identity and work.


def _rollback(config, directory, journal):
    intent = _intent(directory, journal)
    journal["stage"] = "ROLLING_BACK"
    _atomic_bytes(directory / "journal.json", _encoded(journal))
    conflicts = []
    for item in reversed(intent["files"]):
        try:
            target = _safe_local(config["repository"], item["path"])
            current = _file_hash(target)
        except ValueError:
            conflicts.append(item["path"])
            continue
        expected = item.get("expected_sha256")
        if current == expected:
            continue
        if target.is_symlink() or current != item["payload_sha256"]:
            conflicts.append(item["path"])
            continue
        if expected is None:
            target.unlink()
        else:
            original = Path(item["original_path"])
            if _digest(original.read_bytes()) != expected:
                raise ValueError("Adoption original bytes changed")
            _atomic_bytes(target, original.read_bytes())
    path = Path(intent["state_path"])
    current = _file_hash(path)
    if current == intent["after_state_sha256"]:
        _atomic_bytes(path, Path(intent["state_before"]).read_bytes())
    elif current != intent["before_state_sha256"]:
        conflicts.append("workflow state changed")
    journal.update(stage="ROLLED_BACK", conflicts=conflicts)
    _atomic_bytes(directory / "journal.json", _encoded(journal))
    return conflicts


def _install(config, spec, directory, journal, core, record):
    intent = _intent(directory, journal)
    if ([{key: item[key] for key in ("path", "expected_sha256", "payload_path", "payload_sha256")}
         for item in intent["files"]] != sorted(spec["files"], key=lambda item: (
             item.get("expected_sha256") is not None, item["path"]))
            or intent["before_inventory"] != spec["baseline_source_inventory"]
            or intent["before_source"] != spec["baseline_source_identity"]):
        raise ValueError("Adoption journal differs from the authorized source specification")
    path, state = _current_state(config, spec)
    if _file_hash(path) not in {intent["before_state_sha256"], intent["after_state_sha256"]}:
        raise ValueError("Current run changed during interrupted source adoption")
    actual = source_inventory(config["repository"])
    expected = dict(intent["before_inventory"])
    for item in intent["files"]:
        target = core.safe_path(config["repository"], item["path"])
        current = _file_hash(target)
        if current not in {item.get("expected_sha256"), item["payload_sha256"]}:
            raise ValueError("Unknown live edit during interrupted adoption: " + item["path"])
        if item["path"].split("/")[0] in SOURCE_ROOTS and item["path"].endswith(".py"):
            if current is None:
                expected.pop(item["path"], None)
            else:
                expected[item["path"]] = current
    if actual != expected or _identity(config["repository"], actual)["commit"] != intent["before_source"]["commit"]:
        raise ValueError("Unowned source changed during interrupted adoption")
    journal["stage"] = "INSTALLING"
    _atomic_bytes(directory / "journal.json", _encoded(journal))
    for item in intent["files"]:
        target = core.safe_path(config["repository"], item["path"])
        current = _file_hash(target)
        if current not in {item.get("expected_sha256"), item["payload_sha256"]}:
            raise ValueError("Live file changed before its atomic adoption: " + item["path"])
        payload = Path(item["payload_path"]).read_bytes()
        if _digest(payload) != item["payload_sha256"]:
            raise ValueError("Reviewed source payload changed before atomic adoption")
        if current != item["payload_sha256"]:
            _atomic_bytes(target, payload)
    for item in intent["files"]:
        if _file_hash(core.safe_path(config["repository"], item["path"])) != item["payload_sha256"]:
            raise ValueError("Installed owned file differs from reviewed source")
    if source_inventory(config["repository"]) != intent["after_inventory"]:
        raise ValueError("Installed source inventory differs from reviewed payloads")
    if core.fingerprints(config["repository"], record["fingerprints"]) != record["fingerprints"]:
        raise ValueError("Installed dependency changed during reviewed adoption")
    current = _file_hash(path)
    if current not in {intent["before_state_sha256"], intent["after_state_sha256"]}:
        raise ValueError("Workflow state changed before atomic identity adoption")
    if current != intent["after_state_sha256"]:
        _atomic_bytes(path, Path(intent["state_after"]).read_bytes())
    journal.update(stage="INSTALLED", installed_at=datetime.now(timezone.utc).isoformat())
    _atomic_bytes(directory / "journal.json", _encoded(journal))
    return {"status": "INSTALLED", "completion_record": spec["completion_record"],
            "audit": intent["audit_path"], "source_identity": intent["after_source"]}


def attempt_pending(config):
    """Attempt one local adoption; every result leaves ordinary dispatch available."""
    root = Path(config["state_root"])
    pending = root / "pending-source-adoption.json"
    if not pending.exists():
        return {"status": "NO_PENDING"}
    try:
        with FileLock(str(root / "workflow.lock"), timeout=0):
            spec = _read(pending)
            record_id = spec.get("completion_record", "")
            if not RECORD_ID.fullmatch(record_id):
                raise ValueError("Invalid pending adoption completion identity")
            directory = root / "source-adoptions" / record_id
            journal_path = directory / "journal.json"
            journal = _read(journal_path) if journal_path.exists() else None
            try:
                if journal is not None and journal.get("stage") == "INSTALLED":
                    return {"status": "INSTALLED", "completion_record": record_id,
                            "selector_retired": _retire(config, directory, journal, pending),
                            "recovered_retirement": True}
                spec, spec_sha, core, record = _validated_spec(config, pending)
                if journal is not None and journal["spec_sha256"] != spec_sha:
                    raise ValueError("Pending adoption changed after its journal was written")
                if journal is not None and journal["stage"] == "ROLLING_BACK":
                    _rollback(config, directory, journal)
                if journal is None or journal["stage"] == "ROLLED_BACK":
                    journal = _prepare(config, spec, spec_sha, core, directory)
                    if journal is None:
                        return {"status": "PENDING", "reason": "Existing preparation retains ownership"}
                result = _install(config, spec, directory, journal, core, record)
                result["selector_retired"] = _retire(config, directory, journal, pending)
                return result
            except Exception:
                if journal is not None and journal.get("stage") in {"PREPARED", "INSTALLING", "ROLLING_BACK"}:
                    _rollback(config, directory, journal)
                raise
    except Timeout:
        return {"status": "PENDING", "reason": "Existing preparation owns workflow.lock"}
    except Exception as error:
        return {"status": "REFUSED", "error": f"{type(error).__name__}: {error}"}
