"""Bounded private Drive-folder exchange of completed nightly artifacts.

The folder is an explicitly authorized private data channel, separate from Git
coordination notices. Peers supply data, never commands, executable paths or new
authority. This worker does not prepare models or start account execution.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic, sleep
import uuid

import pandas as pd
from filelock import FileLock, Timeout

from ml import nightly_workflow as workflow
from ml.artifacts import file_checksum, utc_timestamp
from ml.gameplan_actuals_review import completed_session_context
from ml.gameplan_stats_handoff import read_stats_package
from ml.joint_capital_plan import content_sha256, load_owner_package


VERSION = "nightly-exchange-config-v1"
PACKET = "nightly-exchange-packet-v1"
SELECTION = "nightly-exchange-selection-v1"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PACKET_BYTES = 40 * 1024 * 1024
FIELDS = {"schema_version", "actor", "workflow_config", "local_profile", "coordination_active",
          "exchange_root", "state_root", "account_scope_sha256", "owners", "private_exchange_authorized"}
FILES = {"preparation": {"plan.json", "stats.json"}, "snapshot": {"snapshot.json"},
         "joint": {"joint-plan.json", "scout-receipt.json", "atlas-plan.json", "atlas-stats.json",
                   "scout-plan.json", "scout-stats.json"}, "accepted": {"atlas-receipt.json"},
         "ownership_request": {"request.json"}, "ownership": {"ownership.json"}}
SENDERS = {"preparation": {"atlas", "scout"}, "snapshot": {"atlas"}, "joint": {"scout"}, "accepted": {"atlas"},
           "ownership_request": {"atlas"}, "ownership": {"scout"}}
OWNERSHIP_KINDS = {"ownership_request", "ownership"}


class Pending(Exception):
    """A named input has not arrived completely; retry the same selection."""


def _bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("An explicit SHA-256 is required")
    return value


def _object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _read(path, limit=MAX_FILE_BYTES):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Exchange input exceeds its byte limit")
    return data


def _write(path, data, *, replace=False):
    """Create immutable bytes, or atomically update an explicitly mutable pointer."""
    path = Path(path)
    if path.exists():
        if _read(path, MAX_PACKET_BYTES) == data:
            return
        if not replace:
            raise ValueError("An immutable exchange selection or local input changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _path(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or value.startswith(("\\\\", "//")):
        raise ValueError("Exchange bindings require explicit absolute local paths")
    return Path(value).resolve()


def load_config(path: Path) -> dict:
    config = _object(_read(path))
    fields = FIELDS | ({"account_config"} if config.get("actor") == "Atlas" else set())
    if set(config) != fields or config.get("schema_version") != VERSION or config.get("actor") not in ("Scout", "Atlas"):
        raise ValueError("Unsupported nightly exchange configuration")
    if config["private_exchange_authorized"] is not True:
        raise ValueError("Private exchange requires direct local authorization")
    _digest(config["account_scope_sha256"])
    for key in ("workflow_config", "local_profile", "coordination_active", "exchange_root", "state_root"):
        _path(config[key])
    exchange = _path(config["exchange_root"])
    if ([part.lower() for part in exchange.parts[-2:]] != ["ducketz-nightly-exchange", "v1"]
            or "codexstore" not in [part.lower() for part in exchange.parts[:-2]]):
        raise ValueError("Private exchange must use the explicitly bound CODEXSTORE/ducketz-nightly-exchange/v1 folder")
    state = _path(config["state_root"])
    if state == exchange or exchange in state.parents or state in exchange.parents:
        raise ValueError("Private exchange and durable local state must be separate")
    owners = config["owners"]
    if not isinstance(owners, dict) or set(owners) != {"atlas", "scout"}:
        raise ValueError("Exactly Atlas and Scout ownership is required")
    for symbols in owners.values():
        if (not isinstance(symbols, list) or len(symbols) != 11 or len(set(symbols)) != 11
                or any(not isinstance(s, str) or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", s) is None for s in symbols)):
            raise ValueError("Each owner must bind eleven distinct normalized symbols")
    if set(owners["atlas"]) & set(owners["scout"]):
        raise ValueError("Research ownership must be disjoint")
    native = workflow.load_config(_path(config["workflow_config"]))
    workflow.verify_installation(native)
    for key in ("local_profile", "coordination_active"):
        if _path(config[key]) != _path(native[key]):
            raise ValueError("Exchange and preparation must use the same installed bindings")
    profile = _object(_read(_path(config["local_profile"])))
    repository = _path(native["repository"])
    if (repository != Path(__file__).resolve().parents[1]
            or _path(config["coordination_active"]) != repository / "scratch/cross-pc/active.json"
            or config["actor"] != native["actor"] or profile.get("actor") != config["actor"]
            or profile.get("contract_version") != "cross-pc-v2"
            or profile.get("machine") != {"Scout": "pc-new", "Atlas": "pc-original"}[config["actor"]]
            or _path(profile.get("checkout")) != repository
            or _path(config["local_profile"]) != repository / "scratch/cross-pc/local-profile.json"
            or profile.get("symbols") != owners[config["actor"].lower()]):
        raise ValueError("Exchange identity or symbols differ from the installed local profile")
    if config["actor"] == "Atlas":
        from ml.account_gameplan.config import CONFIG, load_account_config
        if _path(config["account_config"]) != _path(native["datastore"]) / CONFIG:
            raise ValueError("Atlas must bind its existing local account configuration")
        account = load_account_config(_path(native["datastore"]))
        if (account is None or account.machine_id != "pc-original" or account.coordinator_id != "pc-original"
                or account.account_fingerprint != config["account_scope_sha256"]
                or set(account.participants["pc-original"]) != set(owners["atlas"])
                or set(account.participants["pc-new"]) != set(owners["scout"])):
            raise ValueError("Atlas account scope or producer partitions differ")
    return config


def _context(now):
    # At any wake use the successor of the newest completed 17:00 PT session.
    # This handles midnight/weekends without discovering older run directories.
    context = completed_session_context(now)
    return context["successor_action_date"], context["action_date"]


def _local_state(config, native, action, review):
    path = Path(native["state_root"]) / "runs" / action / "state.json"
    if not path.exists():
        raise Pending("LOCAL_PREPARATION")
    state = _object(_read(path))
    if (state.get("schema_version") != workflow.VERSION or state.get("actor") != config["actor"]
            or state.get("action_date") != action or state.get("source_session") != review):
        raise ValueError("Local preparation identity or session differs")
    if state.get("status") != "LOCAL_COMPLETE_PEER_SETUP_PENDING":
        raise Pending("LOCAL_PREPARATION")
    workflow._verify_configuration_binding(native, state)
    workflow._verify_symbol_binding(native, state)
    if state.get("source_identity") != workflow.source_identity(Path(native["repository"])):
        raise ValueError("Application source changed since local preparation")
    for step in workflow.workflow_steps(state):
        entry = state.get("steps", {}).get(step, {})
        if entry.get("status") != "COMPLETE":
            raise ValueError("Local completion omits a required preparation stage")
        workflow._verify_outputs(entry["output"])
    workflow._verify_local_preparation(native, state)
    return state


def _folder(config, action, actor, kind):
    return Path(config["exchange_root"]) / "sessions" / action / actor / kind


def _metadata(config, action, review, actor, kind):
    if actor not in SENDERS.get(kind, set()):
        raise ValueError("Packet kind does not belong to this actor")
    return {"schema_version": PACKET, "kind": kind, "actor": actor, "action_date": action,
            "review_session": review, "account_scope_sha256": config["account_scope_sha256"], "owners": config["owners"]}


def _publish(config, action, review, kind, files, inputs=None, *, refresh=False):
    actor = config["actor"].lower()
    if set(files) != FILES[kind]:
        raise ValueError("Packet contains files outside the private export allowlist")
    encoded = {}
    for name, value in files.items():
        data = value if isinstance(value, bytes) else _read(value)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Private file exceeds its byte limit")
        _object(data)
        encoded[name] = {"sha256": _sha(data), "base64": base64.b64encode(data).decode("ascii")}
    packet = {**_metadata(config, action, review, actor, kind), "inputs": inputs or {}, "files": encoded}
    digest = content_sha256(packet)
    packet["content_sha256"] = digest
    data = _bytes(packet)
    if len(data) > MAX_PACKET_BYTES:
        raise ValueError("Private packet exceeds its byte limit")
    folder = _folder(config, action, actor, kind)
    if refresh and (kind not in {"snapshot", *OWNERSHIP_KINDS} or _folder(config, action, "scout", "joint").joinpath("selection.json").exists()):
        raise ValueError("A selected joint plan freezes its account snapshot")
    selector = {"schema_version": SELECTION, "content_sha256": digest, "file_sha256": _sha(data), "bytes": len(data)}
    if not refresh and (folder / "selection.json").exists() and _read(folder / "selection.json", 2048) != _bytes(selector):
        raise ValueError("An immutable exchange selection changed")
    if kind in OWNERSHIP_KINDS:
        own = Path(config["state_root"]) / "sessions" / action / "own-ownership" / kind
        if (folder / "selection.json").exists():
            previous_bytes = _read(folder / "selection.json", 2048)
            previous = _object(previous_bytes)
            known = own / (_digest(previous.get("content_sha256")) + ".selection.json")
            if not known.is_file() or _read(known, 2048) != previous_bytes:
                raise ValueError("Unknown ownership publication cannot be overwritten")
        _write(own / (digest + ".packet.json"), data)
        _write(own / (digest + ".selection.json"), _bytes(selector))
    if kind == "snapshot":
        # This is origin evidence, distinct from receive caches/history. Only
        # Atlas's explicitly requested capture reaches this publication path.
        # Commit it locally before any shared bytes can claim our observation.
        origin = Path(config["state_root"]) / "sessions" / action / "own-snapshots" / digest
        snapshot_bytes = base64.b64decode(encoded["snapshot.json"]["base64"], validate=True)
        _write(origin / "packet.json", data)
        _write(origin / "snapshot.json", snapshot_bytes)
        _write(origin / "receipt.json", _bytes({"schema_version": "nightly-exchange-own-snapshot-v1",
            "actor": "atlas", "action_date": action, "review_session": review,
            "account_scope_sha256": config["account_scope_sha256"], "inputs": inputs,
            "content_sha256": digest, "packet_file_sha256": _sha(data), "packet_bytes": len(data),
            "snapshot_file_sha256": _sha(snapshot_bytes)}))
        _write(origin.parent.parent / "snapshot-publication.json", _bytes(selector), replace=refresh)
    _write(folder / "packets" / (digest + ".json"), data)
    _write(folder / "selection.json", _bytes(selector), replace=refresh)
    return _receive(config, action, review, actor, kind)


def _receive(config, action, review, actor, kind, *, local_only=False):
    session = Path(config["state_root"]) / "sessions" / action
    folder = _folder(config, action, actor, kind)
    selected = session / "selections" / f"{actor}-{kind}.json" if local_only else folder / "selection.json"
    if not selected.exists():
        raise Pending(kind.upper() + "_" + actor.upper())
    selector_bytes = _read(selected, 2048)
    try:
        selector = _object(selector_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Pending("SELECTION_SYNC") from None
    if set(selector) != {"schema_version", "content_sha256", "file_sha256", "bytes"} or selector["schema_version"] != SELECTION:
        raise ValueError("Invalid completed packet selection")
    digest, file_digest = _digest(selector["content_sha256"]), _digest(selector["file_sha256"])
    size = selector["bytes"]
    if type(size) is not int or not 0 < size <= MAX_PACKET_BYTES:
        raise ValueError("Invalid selected packet size")
    cache = session / "cache" / actor / kind / digest
    path = cache / "packet.json" if local_only else folder / "packets" / (digest + ".json")
    if not path.exists():
        raise Pending("PACKET_SYNC")
    data = _read(path, MAX_PACKET_BYTES)
    if len(data) < size:
        raise Pending("PACKET_SYNC")
    if len(data) != size or _sha(data) != file_digest:
        raise ValueError("Complete selected packet bytes differ from their SHA-256")
    packet = _object(data)
    metadata = _metadata(config, action, review, actor, kind)
    if (set(packet) != {*metadata, "inputs", "files", "content_sha256"}
            or any(packet.get(key) != value for key, value in metadata.items())
            or packet.get("content_sha256") != digest
            or content_sha256({k: v for k, v in packet.items() if k != "content_sha256"}) != digest):
        raise ValueError("Packet identity, scope, universe or content digest differs")
    if not isinstance(packet["inputs"], dict) or any(not isinstance(k, str) for k in packet["inputs"]):
        raise ValueError("Invalid packet input bindings")
    for value in packet["inputs"].values():
        _digest(value)
    if not isinstance(packet["files"], dict) or set(packet["files"]) != FILES[kind]:
        raise ValueError("Packet file names differ from their fixed allowlist")
    decoded = {}
    for name, binding in packet["files"].items():
        if not isinstance(binding, dict) or set(binding) != {"sha256", "base64"}:
            raise ValueError("Invalid embedded file binding")
        try:
            value = base64.b64decode(binding["base64"], validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid embedded file encoding") from error
        if len(value) > MAX_FILE_BYTES or _sha(value) != _digest(binding["sha256"]):
            raise ValueError("Embedded file bytes differ or exceed the limit")
        _object(value)
        decoded[name] = value
    if _read(selected, 2048) != selector_bytes:
        raise Pending("SELECTION_SYNC")
    if local_only:
        for name, value in decoded.items():
            if _read(cache / name) != value:
                raise ValueError("Completed cached exchange file changed")
    else:
        _write(session / "selection-history" / f"{actor}-{kind}-{digest}.json", selector_bytes)
        if kind not in {"snapshot", *OWNERSHIP_KINDS}:
            _write(session / "selections" / f"{actor}-{kind}.json", selector_bytes)
        _write(cache / "packet.json", data)
        for name, value in decoded.items():
            _write(cache / name, value)
    return {"digest": digest, "inputs": packet["inputs"], "files": {name: cache / name for name in decoded}}


def _bind(path, **extra):
    return {"path": str(path), "file_sha256": file_checksum(path), **extra}


def _owners(config, action, review, preparations):
    owners = {}
    for actor, selected in preparations.items():
        if selected["inputs"]:
            raise ValueError("Preparation must not inherit peer inputs")
        plan, stats = selected["files"]["plan.json"], selected["files"]["stats.json"]
        payload = _object(_read(plan))
        package = load_owner_package(plan.parent, plan, expected_sha256=payload["package_sha256"])
        scored, _ = read_stats_package(stats, expected_sha256=file_checksum(stats))
        if (package["owner_id"] != actor or package["action_date"] != action
                or package["frozen_symbols"] != config["owners"][actor]
                or scored["producer"] != actor.title() or scored["action_date"] != review
                or scored["symbols"] != config["owners"][actor]):
            raise ValueError("Preparation package owner, session or symbols differ")
        owners[actor] = {"symbols": config["owners"][actor],
            "plan_package": _bind(plan, root=str(plan.parent), package_sha256=package["package_sha256"]),
            "stats_package": _bind(stats)}
    return owners


def _snapshot_age(selected, now):
    value = _object(_read(selected["files"]["snapshot.json"]))
    observed = pd.Timestamp(value.get("observed_at"))
    if pd.isna(observed) or observed.tzinfo is None:
        raise ValueError("Account snapshot requires an explicit observation time")
    age = (utc_timestamp(now) - observed.tz_convert("UTC")).total_seconds()
    if age < 0:
        raise ValueError("Account snapshot observation is in the future")
    return age


def _completion(action, actor, inputs):
    return f"exchange-{action}-{actor}-{content_sha256(inputs)[:16]}"


def _frozen(path, spec):
    _write(path, _bytes(spec))
    return spec


def _verify_synthesis_freshness(spec, now):
    from ml.joint_capital_adoption import read_accepted_joint_plan
    selected = read_accepted_joint_plan(Path(spec["datastore_root"]), spec["action_date"])
    snapshot = _object(_read(Path(spec["snapshot"]["path"])))
    if selected is not None:
        if (selected[0].get("account_snapshot_sha256") != content_sha256(snapshot)
                or selected[1].get("local_actor") != "scout"):
            raise ValueError("Existing local adoption differs from the frozen snapshot")
        return
    if _snapshot_age({"files": {"snapshot.json": Path(spec["snapshot"]["path"])}}, now) > 900:
        raise Pending("FROZEN_SNAPSHOT_STALE_BEFORE_ADOPTION_REVIEW_REQUIRED")


def _binding(config):
    result = {"config": config, "workflow_config_sha256": file_checksum(Path(config["workflow_config"])),
              "profile_sha256": file_checksum(Path(config["local_profile"])),
              "coordination_active_sha256": file_checksum(Path(config["coordination_active"])),
              "adapter_sources": {name: file_checksum(Path(__file__).resolve().with_name(name))
                                  for name in ("nightly_exchange.py", "nightly_account_snapshot.py", "nightly_ownership.py")}}
    if config["actor"] == "Atlas":
        result["account_config_sha256"] = file_checksum(Path(config["account_config"]))
    return result


def _request_document(selected, inputs, now):
    value = _object(_read(selected["files"]["request.json"]))
    if (selected["inputs"] != inputs or set(value) != {"schema_version", "challenge", "requested_at", "expires_at"}
            or value["schema_version"] != "nightly-ownership-request-v1"):
        raise ValueError("Ownership request identity or preparation bindings differ")
    _digest(value["challenge"])
    start, end = pd.Timestamp(value["requested_at"]), pd.Timestamp(value["expires_at"])
    if start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 50 or start > now:
        raise ValueError("Invalid ownership request time window")
    return value, end


def _ownership_observations(config, action, review, inputs, clock):
    from tools.nightly_ownership import capture_ownership, validate_observations
    session = Path(config["state_root"]) / "sessions" / action
    request_path = session / "ownership-request.json"
    request = _object(_read(request_path)) if request_path.exists() else None
    now = clock()
    if request is None or pd.Timestamp(request["expires_at"]) <= now:
        request = {"schema_version": "nightly-ownership-request-v1", "challenge": uuid.uuid4().hex + uuid.uuid4().hex,
                   "requested_at": now.isoformat(), "expires_at": (now + pd.Timedelta(seconds=50)).isoformat()}
        _write(request_path, _bytes(request), replace=True)
    selected = _publish(config, action, review, "ownership_request", {"request.json": _bytes(request)}, inputs, refresh=True)
    request, deadline = _request_document(selected, inputs, clock())
    while clock() < deadline:
        try:
            response = _receive(config, action, review, "scout", "ownership")
            if response["inputs"] == {**inputs, "ownership_request": selected["digest"]}:
                scout = _object(_read(response["files"]["ownership.json"]))
                if clock() >= deadline:
                    break
                stamp = pd.Timestamp(scout.get("ledger_observed_at"))
                if stamp.tzinfo is None or not pd.Timestamp(request["requested_at"]) <= stamp <= clock():
                    raise ValueError("Ownership response predates its challenge or is future-dated")
                _check_ownership_binding(config, action)
                capture_path = session / "ownership-captures" / (selected["digest"] + ".json")
                if capture_path.exists():
                    saved = _object(_read(capture_path))
                    if saved["request"] != selected["digest"] or saved["response"] != response["digest"]:
                        raise ValueError("Ownership response changed for this challenge")
                    observations = saved["observations"]
                else:
                    observations = [capture_ownership(config, now=clock()), scout]
                if clock() >= deadline:
                    break
                _check_ownership_binding(config, action)
                validate_observations(config, observations, observed_at=clock())
                _write(capture_path,
                       _bytes({"request": selected["digest"], "response": response["digest"], "observations": observations}))
                if clock() >= deadline:
                    break
                return observations
        except Pending:
            pass
        sleep(min(.25, max(0, (deadline - clock()).total_seconds())))
    raise Pending("FRESH_SCOUT_OWNERSHIP_RESPONSE")


def _check_ownership_binding(config, action):
    frozen = Path(config["state_root"]) / "sessions" / action / "binding.json"
    if _object(_read(frozen)) != _binding(config):
        raise ValueError("Ownership source or operating bindings changed")


def _respond_ownership(config, native, action, review, inputs, clock, *, responder_deadline=None):
    try:
        selected = _receive(config, action, review, "atlas", "ownership_request")
    except Pending:
        return False
    request, deadline = _request_document(selected, inputs, clock())
    if responder_deadline is not None:
        deadline = min(deadline, responder_deadline)
    if clock() >= deadline:
        return False
    _local_state(config, native, action, review)
    _check_ownership_binding(config, action)
    path = Path(config["state_root"]) / "sessions" / action / "ownership-responses" / (selected["digest"] + ".json")
    if path.exists():
        value = _object(_read(path))
    else:
        from tools.nightly_ownership import capture_ownership
        value = capture_ownership(config, now=clock())
        _write(path, _bytes(value))
    if clock() >= deadline:
        return False
    _check_ownership_binding(config, action)
    if clock() >= deadline:
        return False
    _publish(config, action, review, "ownership", {"ownership.json": _bytes(value)},
             {**inputs, "ownership_request": selected["digest"]}, refresh=True)
    return True


def _spawn_responder(config_path, repository):
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "cwd": str(repository)}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return subprocess.Popen([sys.executable, "-B", "-m", "tools.nightly_exchange", "--config", str(config_path),
                             "--serve-ownership"], **options).pid


def _ensure_ownership_responder(config, action, review, inputs, now):
    state_root = Path(config["state_root"])
    with FileLock(str(state_root / "ownership-launch.lock"), timeout=0):
        try:
            with FileLock(str(state_root / "ownership-responder.lock"), timeout=0):
                pass
        except Timeout:
            return
        session = state_root / "sessions" / action
        path = session / "ownership-responder.json"
        record = _object(_read(path)) if path.exists() else None
        binding = _binding(config)
        if record is not None and record["binding"] != binding:
            raise ValueError("Ownership responder source or configuration changed")
        if record is None or pd.Timestamp(record["deadline_at"]) <= now:
            record = {"action_date": action, "review_session": review, "inputs": inputs, "binding": binding,
                      "started_at": now.isoformat(), "deadline_at": (now + pd.Timedelta(seconds=360)).isoformat()}
        elif record.get("last_launch_at") and (now - pd.Timestamp(record["last_launch_at"])).total_seconds() < 10:
            return
        if record["inputs"] != inputs:
            raise ValueError("Ownership responder preparations changed")
        config_path = session / "ownership-responder-config.json"
        _write(config_path, _bytes(config))
        record["last_launch_at"] = now.isoformat()
        _write(path, _bytes(record), replace=True)
        try:
            _spawn_responder(config_path, Path(_object(_read(config["workflow_config"]))["repository"]))
        except Exception as error:
            _write(session / "ownership-responder-result.json",
                   _bytes({"status": "FAILED", "error_type": type(error).__name__, "orders_placed": 0}), replace=True)
            raise


def _serve_ownership(config, *, now=None):
    if config["actor"] != "Scout" or config.get("private_exchange_authorized") is not True:
        raise ValueError("Ownership responder is explicitly authorized Scout-only work")
    initial, timer = utc_timestamp(now), monotonic()
    clock = lambda: initial + pd.Timedelta(microseconds=int(max(0, monotonic() - timer) * 1_000_000))
    action, review = _context(initial)
    state_root = Path(config["state_root"])
    session = state_root / "sessions" / action
    record = _object(_read(session / "ownership-responder.json"))
    deadline = pd.Timestamp(record["deadline_at"])
    start = pd.Timestamp(record["started_at"])
    if (record["action_date"] != action or record["review_session"] != review or start.tzinfo is None
            or deadline.tzinfo is None or not 0 < (deadline - start).total_seconds() <= 360):
        raise ValueError("Invalid saved ownership responder deadline")
    try:
        with FileLock(str(state_root / "ownership-responder.lock"), timeout=0):
            native = workflow.load_config(Path(config["workflow_config"]))
            workflow.verify_installation(native)
            _local_state(config, native, action, review)
            preparations = {actor: _receive(config, action, review, actor, "preparation") for actor in ("atlas", "scout")}
            _owners(config, action, review, preparations)
            inputs = {f"{actor}_preparation": selected["digest"] for actor, selected in preparations.items()}
            if record["inputs"] != inputs:
                raise ValueError("Ownership responder preparations changed")
            while clock() < deadline:
                if _binding(config) != record["binding"] or _context(clock()) != (action, review):
                    raise ValueError("Ownership responder source, bindings or session changed")
                if (_folder(config, action, "scout", "joint") / "selection.json").exists():
                    break
                if _respond_ownership(config, native, action, review, inputs, clock, responder_deadline=deadline):
                    result = {"status": "OWNERSHIP_SERVED", "action_date": action, "orders_placed": 0}
                    _write(session / "ownership-responder-result.json", _bytes(result), replace=True)
                    return result
                sleep(min(.25, max(0, (deadline - clock()).total_seconds())))
    except Timeout:
        return {"status": "OWNERSHIP_RESPONDER_ALREADY_RUNNING", "orders_placed": 0}
    result = {"status": "OWNERSHIP_RESPONDER_FINISHED", "action_date": action, "orders_placed": 0}
    _write(session / "ownership-responder-result.json", _bytes(result), replace=True)
    return result


def serve_ownership(config, *, now=None):
    """Run one bounded loop; disk I/O is not an OS-enforced process timeout."""
    try:
        return _serve_ownership(config, now=now)
    except Exception as error:
        action, _ = _context(utc_timestamp(now))
        result = {"status": "PENDING" if isinstance(error, Pending) else "FAILED",
                  "error_type": type(error).__name__, "orders_placed": 0}
        _write(Path(config["state_root"]) / "sessions" / action / "ownership-responder-result.json",
               _bytes(result), replace=True)
        raise


def _own_snapshot(config, action, review, digest, inputs):
    origin = Path(config["state_root"]) / "sessions" / action / "own-snapshots" / _digest(digest)
    if not (origin / "receipt.json").is_file():
        raise ValueError("Scout selected an account snapshot not locally published by this Atlas attempt")
    snapshot_bytes = _read(origin / "snapshot.json")
    packet_bytes = _read(origin / "packet.json", MAX_PACKET_BYTES)
    packet = _object(packet_bytes)
    expected = {"schema_version": "nightly-exchange-own-snapshot-v1", "actor": "atlas",
        "action_date": action, "review_session": review, "account_scope_sha256": config["account_scope_sha256"],
        "inputs": inputs, "content_sha256": digest, "packet_file_sha256": _sha(packet_bytes),
        "packet_bytes": len(packet_bytes), "snapshot_file_sha256": _sha(snapshot_bytes)}
    if (_object(_read(origin / "receipt.json")) != expected or packet.get("content_sha256") != digest
            or content_sha256({k: v for k, v in packet.items() if k != "content_sha256"}) != digest
            or any(packet.get(k) != v for k, v in _metadata(config, action, review, "atlas", "snapshot").items())
            or packet["inputs"] != inputs or packet["files"]["snapshot.json"]["sha256"] != _sha(snapshot_bytes)):
        raise ValueError("Scout synthesis differs from Atlas's exact captured snapshot")
    return origin / "snapshot.json", packet_bytes


def _resume_snapshot_publication(config, action, review, inputs, now):
    session = Path(config["state_root"]) / "sessions" / action
    selected = session / "snapshot-publication.json"
    if not selected.exists():
        return
    selector_bytes = _read(selected, 2048)
    selector = _object(selector_bytes)
    digest = _digest(selector["content_sha256"])
    snapshot_path, packet_bytes = _own_snapshot(config, action, review, digest, inputs)
    expected = {"schema_version": SELECTION, "content_sha256": digest,
                "file_sha256": _sha(packet_bytes), "bytes": len(packet_bytes)}
    if selector != expected:
        raise ValueError("Local snapshot publication intent changed")
    folder = _folder(config, action, "atlas", "snapshot")
    if (folder / "selection.json").exists():
        prior_bytes = _read(folder / "selection.json", 2048)
        if prior_bytes == selector_bytes:
            return
        try:
            prior = _object(prior_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise Pending("SELECTION_SYNC") from None
        prior_path, prior_packet = _own_snapshot(config, action, review, _digest(prior.get("content_sha256")), inputs)
        prior_expected = {"schema_version": SELECTION, "content_sha256": prior["content_sha256"],
                          "file_sha256": _sha(prior_packet), "bytes": len(prior_packet)}
        if (prior != prior_expected or _snapshot_age({"files": {"snapshot.json": prior_path}}, now)
                <= _snapshot_age({"files": {"snapshot.json": snapshot_path}}, now)):
            raise ValueError("Shared snapshot selection changed outside the saved publication intent")
    if _snapshot_age({"files": {"snapshot.json": snapshot_path}}, now) > 900:
        return  # A new explicitly allowed capture may supersede an unconsumed stale observation.
    if (_folder(config, action, "scout", "joint") / "selection.json").exists():
        raise Pending("JOINT_ARRIVED_DURING_CAPTURE")
    _write(folder / "packets" / (digest + ".json"), packet_bytes)
    _write(folder / "selection.json", selector_bytes, replace=True)


def _run(config, native, action, review, clock, allow_snapshot_refresh):
    actor = config["actor"].lower()
    local = _local_state(config, native, action, review)
    session = Path(config["state_root"]) / "sessions" / action
    # Waiting for preparation must not freeze a setup that has exported nothing.
    # Once local evidence is complete, bind the operating/source bytes before
    # the first private export and preserve that binding across every retry.
    _frozen(session / "binding.json", _binding(config))
    exported = local["steps"]["local_handoff"]["output"]
    _owners(config, action, review, {actor: {"inputs": {}, "files": {
        "plan.json": Path(exported["package"]), "stats.json": Path(exported["stats_package"])}}})
    local_files = {}
    for name, source in (("plan.json", exported["package"]), ("stats.json", exported["stats_package"])):
        data = _read(source)
        if exported["files"].get(source) != _sha(data):
            raise ValueError("Completed preparation export changed before copying")
        local_files[name] = data
    own = _publish(config, action, review, "preparation", local_files)
    peer = "atlas" if actor == "scout" else "scout"
    preparations = {actor: own, peer: _receive(config, action, review, peer, "preparation")}
    owners = _owners(config, action, review, preparations)
    inputs = {f"{owner}_preparation": item["digest"] for owner, item in preparations.items()}
    if actor == "scout":
        from ml.nightly_synthesis import run_synthesis, validate_local_profile
        spec_path = session / "synthesis-selection.json"
        if spec_path.exists():
            frozen = _object(_read(spec_path))
            spec, frozen_inputs = frozen["spec"], frozen["inputs"]
            if any(frozen_inputs.get(k) != v for k, v in inputs.items()):
                raise ValueError("Frozen synthesis preparations changed")
            inputs = frozen_inputs
        else:
            # Scout is a research producer. The single account snapshot comes
            # from Atlas's native account/horizon history, never a peer ledger.
            snapshot = _receive(config, action, review, "atlas", "snapshot")
            if snapshot["inputs"] != inputs:
                raise ValueError("Snapshot belongs to different preparation packages")
            now = clock()
            if _snapshot_age(snapshot, now) > 900:
                raise Pending("FRESH_ACCOUNT_SNAPSHOT")
            inputs = {**inputs, "snapshot": snapshot["digest"]}
            spec = {"schema_version": "nightly-gameplan-synthesis-v1", "completion_id": _completion(action, actor, inputs),
                "datastore_root": native["datastore"], "state_root": str(session / "synthesis"),
                "local_actor": actor, "executor_owner": "atlas", "action_date": action, "review_session": review,
                "account_scope_sha256": config["account_scope_sha256"], "as_of": now.isoformat(), "accepted_at": now.isoformat(),
                "snapshot": _bind(snapshot["files"]["snapshot.json"]), "owners": owners}
            _frozen(spec_path, {"spec": spec, "inputs": inputs})
        validate_local_profile(spec, Path(config["local_profile"]))
        now = clock()
        _verify_synthesis_freshness(spec, now)
        receipt = run_synthesis(spec, now=now)
        files = {"joint-plan.json": Path(receipt["ui_evidence"]["plan_run"]) / "joint-plan.json",
                 "scout-receipt.json": Path(receipt["receipt_path"])}
        for owner, selected in preparations.items():
            files.update({f"{owner}-plan.json": selected["files"]["plan.json"], f"{owner}-stats.json": selected["files"]["stats.json"]})
        joint = _publish(config, action, review, "joint", files, inputs)
        accepted = _receive(config, action, review, "atlas", "accepted")
        _verify_acceptance(config, action, review, joint, accepted)
        return {"status": "COMPLETE", "local_status": receipt["status"], "joint_packet_sha256": joint["digest"],
                "accepted_packet_sha256": accepted["digest"], "peer_verified": True, "ui_ready": True, "joint_ready": True}
    try:
        joint = _receive(config, action, review, "scout", "joint")
    except Pending:
        if (_folder(config, action, "scout", "joint") / "selection.json").exists():
            raise
        _resume_snapshot_publication(config, action, review, inputs, clock())
        try:
            snapshot = _receive(config, action, review, "atlas", "snapshot")
        except Pending:
            snapshot = None
        if snapshot is not None and snapshot["inputs"] != inputs:
            raise ValueError("Selected account snapshot preparations changed")
        now = clock()
        if snapshot is None or _snapshot_age(snapshot, now) > 900:
            if not allow_snapshot_refresh:
                raise Pending("ACCOUNT_SNAPSHOT_REFRESH_REQUIRED")
            from tools.nightly_account_snapshot import capture_snapshot
            from ml.account_gameplan.config import load_account_config
            _check_ownership_binding(config, action)
            account = load_account_config(native["datastore"])
            if account is None:
                raise ValueError("Atlas account configuration is missing")
            _check_ownership_binding(config, action)
            value = capture_snapshot(config, now=clock())
            _check_ownership_binding(config, action)
            capture_completed = clock()
            if (_folder(config, action, "scout", "joint") / "selection.json").exists():
                raise Pending("JOINT_ARRIVED_DURING_CAPTURE")
            snapshot = _publish(config, action, review, "snapshot", {"snapshot.json": _bytes(value)}, inputs, refresh=True)
            if _snapshot_age(snapshot, capture_completed) > 900:
                raise ValueError("Snapshot helper returned stale account data")
        raise Pending("JOINT_SCOUT")
    if set(joint["inputs"]) != {*inputs, "snapshot"} or any(joint["inputs"].get(k) != v for k, v in inputs.items()):
        raise ValueError("Joint plan belongs to different preparation selections")
    snapshot_digest = joint["inputs"]["snapshot"]
    # Receiving an Atlas-named shared packet can populate a cache, but can
    # never establish that this PC captured it. Require our prior local proof.
    snapshot_path, _ = _own_snapshot(config, action, review, snapshot_digest, inputs)
    snapshot = _object(_read(snapshot_path))
    joint_plan = _object(_read(joint["files"]["joint-plan.json"]))
    if (joint_plan.get("account_snapshot_sha256") != content_sha256(snapshot)
            or joint_plan.get("account_snapshot_observed_at") != snapshot.get("observed_at")):
        raise ValueError("Scout synthesis differs from Atlas's exact captured snapshot")
    # Do not follow source paths or select packages from the receipt. Every
    # handed-back source must equal our independently selected preparation.
    for owner, selected in preparations.items():
        for name in ("plan", "stats"):
            if _read(joint["files"][f"{owner}-{name}.json"]) != _read(selected["files"][f"{name}.json"]):
                raise ValueError("Joint packet substituted an owner preparation")
    from ml.nightly_handoff import run_handoff
    now = clock()
    spec_path = session / "handoff-selection.json"
    handoff_inputs = {"joint": joint["digest"]}
    if spec_path.exists():
        frozen = _object(_read(spec_path))
        spec = frozen["spec"]
        if frozen["inputs"] != handoff_inputs:
            raise ValueError("Frozen handoff selection changed")
    else:
        plan_path = joint["files"]["joint-plan.json"]
        plan = _object(_read(plan_path))
        spec = {"schema_version": "nightly-gameplan-handoff-v1", "completion_id": _completion(action, actor, handoff_inputs),
            "datastore_root": native["datastore"], "state_root": str(session / "handoff"), "local_actor": actor,
            "executor_owner": "atlas", "action_date": action, "review_session": review,
            "account_scope_sha256": config["account_scope_sha256"], "accepted_at": now.isoformat(),
            "local_profile": _bind(Path(config["local_profile"])), "account_config": _bind(Path(config["account_config"])),
            "scout_receipt": _bind(joint["files"]["scout-receipt.json"]),
            "joint_plan": _bind(plan_path, root=str(plan_path.parent), plan_sha256=plan["plan_sha256"]), "owners": owners}
        _frozen(spec_path, {"spec": spec, "inputs": handoff_inputs})
    receipt = run_handoff(spec, local_profile=Path(config["local_profile"]), now=now)
    accepted = _publish(config, action, review, "accepted", {"atlas-receipt.json": Path(receipt["receipt_path"])}, handoff_inputs)
    _verify_acceptance(config, action, review, joint, accepted)
    return {"status": "COMPLETE", "local_status": receipt["status"], "joint_packet_sha256": joint["digest"],
            "accepted_packet_sha256": accepted["digest"], "peer_verified": True, "ui_ready": True, "joint_ready": True}


def _verify_acceptance(config, action, review, joint, accepted):
    receipt = _object(_read(accepted["files"]["atlas-receipt.json"]))
    plan = _object(_read(joint["files"]["joint-plan.json"]))
    scout = _object(_read(joint["files"]["scout-receipt.json"]))
    evidence = receipt.get("ui_evidence", {})
    expected_plan_path = str(evidence.get("plan_run", "")).replace("\\", "/").rstrip("/") + "/joint-plan.json"
    original_files = {str(path).replace("\\", "/"): value for path, value in evidence.get("files", {}).items()}
    if (accepted["inputs"] != {"joint": joint["digest"]}
            or receipt.get("schema_version") != "nightly-gameplan-handoff-v1"
            or receipt.get("status") != "HANDOFF_VERIFIED_LOCAL" or receipt.get("local_actor") != "atlas"
            or receipt.get("executor_owner") != "atlas" or receipt.get("action_date") != action
            or receipt.get("review_session") != review or receipt.get("account_scope_sha256") != config["account_scope_sha256"]
            or receipt.get("orders_placed") != 0 or receipt.get("activation_changed") is not False
            or receipt.get("joint_ready") is not True or receipt.get("ui_ready") is not True
            or receipt.get("execution_authorized") is not False
            or receipt.get("scout_receipt_sha256") != file_checksum(joint["files"]["scout-receipt.json"])
            or receipt.get("owner_packages") != scout.get("owner_packages")
            or receipt.get("stats_packages") != scout.get("stats_packages")
            or original_files.get(expected_plan_path) != file_checksum(joint["files"]["joint-plan.json"])
            or evidence.get("plan_sha256") != plan.get("plan_sha256")):
        raise ValueError("Atlas acceptance does not bind this exact Scout synthesis")


def _completed_result(config, native, action, review, common):
    """Verify terminal local evidence without replaying any operating stage.

    A later source installation or unavailable peer cannot invalidate completed
    history. Corrupt retained evidence still fails closed, without replacing the
    terminal status or letting a later wake attempt preparation/adoption again.
    """
    session = Path(config["state_root"]) / "sessions" / action
    status_path = session / "status.json"
    if not status_path.exists():
        return None
    result = _object(_read(status_path))
    if result.get("status") != "COMPLETE":
        return None
    expected = {**common, "joint_ready": True, "ui_ready": True, "peer_verified": True}
    if any(result.get(key) != value for key, value in expected.items()):
        raise ValueError("Completed exchange status identity changed")
    joint = _receive(config, action, review, "scout", "joint", local_only=True)
    accepted = _receive(config, action, review, "atlas", "accepted", local_only=True)
    if (joint["digest"] != result.get("joint_packet_sha256")
            or accepted["digest"] != result.get("accepted_packet_sha256")):
        raise ValueError("Completed exchange packet selections changed")
    _verify_acceptance(config, action, review, joint, accepted)
    from app.ui.gameplan_data import load_gameplan
    from app.ui.gameplan_stats_data import load_gameplan_stats
    from ml.joint_capital_adoption import read_accepted_joint_plan
    from ml.nightly_joint_readiness import FIELDS, VERSION as READINESS_VERSION, _receipt_identity
    actor = config["actor"].lower()
    root = Path(native["datastore"]).resolve()
    pin = _object(_read(root / "ml/nightly-joint-readiness-by-date" / action / "run.json"))
    receipt = _object(_read((accepted if actor == "atlas" else joint)["files"][f"{actor}-receipt.json"]))
    _receipt_identity(receipt, actor=actor, action_date=action, review_session=review)
    operation = "handoff" if actor == "atlas" else "synthesis"
    receipt_path = session / operation / receipt["completion_id"] / "receipt.json"
    if (set(pin) != FIELDS or pin.get("schema_version") != READINESS_VERSION
            or pin.get("local_actor") != actor or pin.get("action_date") != action
            or pin.get("review_session") != review or pin.get("completion_id") != receipt["completion_id"]
            or Path(pin.get("receipt_path", "")).resolve() != receipt_path.resolve()
            or file_checksum(receipt_path) != pin.get("receipt_sha256")
            or _object(_read(receipt_path)) != receipt or result.get("local_status") != receipt["status"]):
        raise ValueError("Completed exchange local receipt changed")
    selected = read_accepted_joint_plan(root, action)
    plan, stats = load_gameplan(root, action), load_gameplan_stats(root, review)
    evidence = receipt["ui_evidence"]
    if (selected is None or selected[0]["plan_sha256"] != evidence["plan_sha256"]
            or selected[1]["local_actor"] != actor or selected[1]["executor_owner"] != "atlas"
            or selected[1]["account_scope_sha256"] != config["account_scope_sha256"]
            or selected[1]["owner_packages"] != receipt["owner_packages"]
            or plan.run_directory != selected[2] or str(plan.run_directory) != evidence["plan_run"]
            or str(stats.run_directory) != evidence["stats_run"]
            or set(plan.symbols) != set(config["owners"]["atlas"] + config["owners"]["scout"])):
        raise ValueError("Completed exchange dated publications changed")
    paths = (plan.run_directory / "accepted-plan.json", plan.run_directory / "joint-plan.json",
             stats.run_directory / "receipt.json", stats.run_directory / "manifest.json",
             stats.run_directory / "forecast-results.parquet")
    if (evidence["files"] != {str(path): file_checksum(path) for path in paths}
            or evidence["stats_receipt_sha256"] != file_checksum(stats.run_directory / "receipt.json")):
        raise ValueError("Completed exchange publication bytes changed")
    return result


def run_once(config, *, now=None, allow_snapshot_refresh=False):
    """Perform one bounded exchange wake; missing inputs are durable pending states."""
    if config.get("private_exchange_authorized") is not True:
        raise ValueError("Private exchange requires direct local authorization")
    if allow_snapshot_refresh and config["actor"] != "Atlas":
        raise ValueError("Only Atlas may explicitly refresh the account snapshot")
    observed = utc_timestamp(now)
    timer = monotonic()
    clock = lambda: observed + pd.Timedelta(microseconds=int(max(0, monotonic() - timer) * 1_000_000))
    native = workflow.load_config(Path(config["workflow_config"]))
    workflow.verify_installation(native)
    action, review = _context(observed)
    state_root = Path(config["state_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(state_root / "exchange.lock"), timeout=0):
        session = state_root / "sessions" / action
        common = {"action_date": action, "review_session": review, "actor": config["actor"],
                  "orders_placed": 0, "activation_changed": False, "execution_authorized": False,
                  "joint_ready": False, "ui_ready": False, "peer_verified": False}
        completed = _completed_result(config, native, action, review, common)
        if completed is not None:
            return completed
        try:
            result = {**common, **_run(config, native, action, review, clock, allow_snapshot_refresh)}
        except Pending as pending:
            result = {**common, "status": "PENDING", "reason": str(pending)}
        except Exception as error:
            _write(session / "status.json", _bytes({**common, "status": "FAILED", "error_type": type(error).__name__}), replace=True)
            raise
        _write(session / "status.json", _bytes(result), replace=True)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--allow-snapshot-refresh", action="store_true")
    parser.add_argument("--serve-ownership", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.serve_ownership and (args.check or args.allow_snapshot_refresh):
            raise ValueError("Ownership responder mode cannot check or capture accounts")
        if args.allow_snapshot_refresh and config["actor"] != "Atlas":
            raise ValueError("Only Atlas may explicitly refresh the account snapshot")
        result = ({"status": "CONFIGURATION_VERIFIED", "actor": config["actor"], "orders_placed": 0}
                  if args.check else serve_ownership(config) if args.serve_ownership
                  else run_once(config, allow_snapshot_refresh=args.allow_snapshot_refresh))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        if args.serve_ownership and args.config.is_absolute() and args.config.name == "ownership-responder-config.json":
            _write(args.config.parent / "ownership-responder-result.json",
                   _bytes({"status": "FAILED", "error_type": type(error).__name__, "orders_placed": 0}), replace=True)
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
