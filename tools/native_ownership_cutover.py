"""Atlas manual Start completes staged native inventory readiness once.

The manual launcher selects technical evidence itself; no extra human approval
document or 04:00 cutover gate is required. Overnight inspection is read-only.
The older explicit --spec adapter remains available for historical records.
Original planning receipts and PREPARING exchange bindings remain immutable.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import uuid
from zoneinfo import ZoneInfo

from filelock import FileLock

from datafetching.runtime_lock import _lock_pid, _pid_is_running, runtime_lock_maintenance_gate
from ml.account_gameplan.config import CONFIG, VERSION as ACCOUNT_VERSION, load_account_config, verify_cutover
from ml.account_gameplan.cutover import reconcile_migration_candidate
from ml.account_gameplan.migration import _COLUMNS, _OPTIONAL, _plain, pin_ledger_files
from ml.artifacts import file_checksum
from ml.stock_trader.contracts import PortfolioState, canonical_sha256, utc
from ml.stock_trader.horizon_ledger import PortfolioEvidence
from tools.native_ownership_export import _OPTIONAL_SCHEMA, _group


VERSION = "native-ownership-cutover-v1"
HUMAN_VERSION = "native-cutover-human-authority-v1"
MANUAL_VERSION = "native-ownership-manual-start-v1"
INVOCATION_VERSION = "gameplan-manual-start-invocation-v1"
LEDGER = Path("state/independent-stock-trader/holdings.sqlite3")
TOOLS = ("native_ownership_cutover.py", "native_ownership_exchange.py", "native_ownership_export.py",
         "gameplan_execution_readiness.py")
FIELDS = {"schema_version", "operation_id", "action_date", "native_exchange_config", "account_config_sha256",
          "candidate_manifest_sha256", "migration_review_sha256", "human_authorization", "nightly", "implementation",
          "runtime_bindings"}
MANUAL_FIELDS = (FIELDS - {"human_authorization"}) | {"manual_invocation", "migration_action_date"}
NIGHTLY = {"exchange_config_sha256", "session_binding_sha256", "session_status_sha256",
           "handoff_selection_sha256", "local_preparation_sha256"}
CONTROL_PATHS = ("controls/stock-trader/operator-intent.txt", "controls/gameplan-stock-trader/operator-intent.txt")
PHASES = {"PREPARING_BACKUP", "PREPARED", "INSTALLING", "LEDGER_INSTALLED", "COMPLETE", "RECOVERY_REQUIRED",
          "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED", "ORIGINALS_PRESERVED_RETRY_REQUIRED"}


def _repository():
    return Path(__file__).resolve().parents[1]


def _manual(spec):
    return spec.get("schema_version") == MANUAL_VERSION


def _migration_date(spec):
    return spec["migration_action_date"] if _manual(spec) else spec["action_date"]


def _operation_root(native, spec):
    return _path(native["state_root"]) / spec["operation_id"]


def _activation_work(native, spec):
    local = _operation_root(native, spec) / "activation"
    return local / "manual-starts" / spec["action_date"] if _manual(spec) else local


def _same_manual_inputs(original, current):
    _shape(original)
    if not _manual(original) or {key: value for key, value in original.items() if key not in {"implementation", "manual_invocation"}} != {
            key: value for key, value in current.items() if key not in {"implementation", "manual_invocation"}}:
        raise ValueError("MANUAL_START_FROZEN_INPUTS_CHANGED")


def _temporal_gate(spec, clock):
    # The historical explicitly dated adapter retains its original semantics.
    # A human's manual Start has no separate opening-time activation deadline.
    if not _manual(spec):
        _deadline(spec["action_date"], clock)


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(path):
    raw = _plain(path).read_bytes()
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("CUTOVER_INPUT_EXCEEDS_BOUND")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("CUTOVER_DUPLICATE_INPUT_KEY")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("CUTOVER_NONFINITE_JSON")))


def _path(value):
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("CUTOVER_REQUIRES_ABSOLUTE_LOCAL_PATH")
    if str(value).startswith(("//", "\\\\")):
        raise ValueError("CUTOVER_REQUIRES_LOCAL_PATH")
    return _plain(value).resolve()


def _hash(value):
    if not isinstance(value, str) or re.fullmatch("[a-f0-9]{64}", value) is None:
        raise ValueError("CUTOVER_REQUIRES_EXACT_SHA256")
    return value


def _pin(path, expected, watched):
    path = _path(path)
    if file_checksum(path) != _hash(expected):
        raise ValueError("CUTOVER_REVIEWED_INPUT_CHANGED")
    watched[str(path)] = expected
    return path


def _selection(value, watched):
    if not isinstance(value, dict) or set(value) != {"path", "file_sha256"}:
        raise ValueError("CUTOVER_REQUIRES_EXACT_FILE_SELECTION")
    return _pin(value["path"], value["file_sha256"], watched)


def _guard(watched):
    if any(file_checksum(_plain(path)) != expected for path, expected in watched.items()):
        raise ValueError("CUTOVER_REVIEWED_INPUT_CHANGED")


def runtime_bindings(root):
    """Read Atlas's existing schedule and controls without changing either."""
    if os.name != "nt":
        raise ValueError("CUTOVER_REQUIRES_REVIEWED_ATLAS_WINDOWS_RUNTIME")
    command = "$ErrorActionPreference='Stop'; $t=Get-ScheduledTask -TaskPath '\\' -TaskName 'Ducketz Independent Stock Session'; [ordered]@{task_path=$t.TaskPath;task_name=$t.TaskName;enabled=[bool]$t.Settings.Enabled;state=[string]$t.State}|ConvertTo-Json -Compress"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=15, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    task = json.loads(result.stdout)
    if task != {"task_path": "\\", "task_name": "Ducketz Independent Stock Session", "enabled": False, "state": "Disabled"}:
        raise ValueError("CUTOVER_EXISTING_SCHEDULE_MUST_REMAIN_DISABLED")
    return {"scheduled_task": task, "manual_control_files": {
        value: file_checksum(_plain(root / value)) if (root / value).is_file() else None for value in CONTROL_PATHS}}


def _control_bindings(root):
    return {value: file_checksum(_plain(root / value)) if (root / value).is_file() else None for value in CONTROL_PATHS}


def _runtime_guard(root, expected, *, manual=False):
    observed = _control_bindings(root) if manual else runtime_bindings(root)
    if observed != expected:
        raise ValueError("CUTOVER_RUNTIME_BINDINGS_CHANGED")


def _journal(value, spec, work):
    if (not isinstance(value, dict) or value.get("schema_version") != VERSION
            or value.get("operation_id") != spec["operation_id"] or type(value.get("number")) is not int
            or not 1 <= value["number"] <= 9999 or value.get("phase") not in PHASES
            or value.get("attempt") != "attempt-" + str(value["number"]).zfill(4)):
        raise ValueError("CUTOVER_RECOVERY_JOURNAL_INVALID")
    attempt = _plain(work / value["attempt"])
    if attempt.parent != work or not attempt.is_dir():
        raise ValueError("CUTOVER_RECOVERY_ATTEMPT_INVALID")
    return attempt


def _atomic(path, raw, *, expected_preimage=None):
    path = _plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_preimage is not None and _plain(path).read_bytes() != expected_preimage:
            raise ValueError("CUTOVER_CONFIG_PREIMAGE_CHANGED")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _once(path, raw):
    path = _plain(path)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("CUTOVER_IMMUTABLE_RECORD_CHANGED")
    else:
        _atomic(path, raw)


def implementation():
    """Return review pins only; this never authorizes their use."""
    from ml.nightly_workflow import source_identity
    repository = _repository()
    return {"source_identity": source_identity(repository),
            "tools": {name: file_checksum(repository / "tools" / name) for name in TOOLS}}


def _load_native(path):
    from tools.native_ownership_exchange import load_config
    return load_config(path)


def _deadline(day, clock):
    now = utc(clock()).to_pydatetime()
    opening = datetime.combine(date.fromisoformat(day), time(4), ZoneInfo("America/Los_Angeles"))
    if now >= opening:
        raise ValueError("CUTOVER_OPENING_DEADLINE_PASSED")
    return now


def _cached_packet(config, session, actor, kind, action, review, watched):
    """Verify already received local bytes; never receive, fetch or republish."""
    from tools import nightly_exchange as exchange
    selector_path = session / "selections" / (actor + "-" + kind + ".json")
    selector = _json(_pin(selector_path, file_checksum(selector_path), watched))
    if (set(selector) != {"schema_version", "content_sha256", "file_sha256", "bytes"}
            or selector["schema_version"] != exchange.SELECTION or type(selector["bytes"]) is not int
            or not 0 < selector["bytes"] <= exchange.MAX_PACKET_BYTES):
        raise ValueError("CUTOVER_COMPLETED_PACKET_SELECTION_INVALID")
    digest = _hash(selector["content_sha256"])
    folder = session / "cache" / actor / kind / digest
    path = _pin(folder / "packet.json", selector["file_sha256"], watched)
    raw = path.read_bytes()
    packet = _json(path)
    metadata = exchange._metadata(config, action, review, actor, kind)
    if (len(raw) != selector["bytes"] or len(raw) > exchange.MAX_PACKET_BYTES
            or set(packet) != {*metadata, "inputs", "files", "content_sha256"}
            or any(packet.get(key) != value for key, value in metadata.items())
            or packet["content_sha256"] != digest
            or exchange.content_sha256({key: value for key, value in packet.items() if key != "content_sha256"}) != digest
            or set(packet["files"]) != exchange.FILES[kind]):
        raise ValueError("CUTOVER_COMPLETED_PACKET_INVALID")
    if not isinstance(packet["inputs"], dict) or any(not isinstance(key, str) for key in packet["inputs"]):
        raise ValueError("CUTOVER_COMPLETED_PACKET_INPUTS_INVALID")
    for value in packet["inputs"].values():
        _hash(value)
    files = {}
    for name, binding in packet["files"].items():
        if set(binding) != {"sha256", "base64"}:
            raise ValueError("CUTOVER_COMPLETED_PACKET_FILE_INVALID")
        decoded = base64.b64decode(binding["base64"], validate=True)
        if len(decoded) > exchange.MAX_FILE_BYTES or _sha(decoded) != binding["sha256"]:
            raise ValueError("CUTOVER_COMPLETED_PACKET_FILE_INVALID")
        files[name] = _pin(folder / name, binding["sha256"], watched)
    return {"digest": digest, "inputs": packet["inputs"], "files": files}


def _completed_handoff(spec, native, account, watched):
    from ml import nightly_workflow as workflow
    from ml.nightly_handoff import preflight_handoff
    from tools import nightly_exchange as exchange
    from tools.gameplan_execution_readiness import _handoff
    root, day = _path(native["datastore_root"]), spec["action_date"]
    exchange_path = _repository() / "scratch/nightly-workflow/exchange-config.json"
    config = _json(_pin(exchange_path, spec["nightly"]["exchange_config_sha256"], watched))
    if (config.get("actor") != "Atlas" or _path(config["account_config"]) != root / CONFIG
            or config["account_scope_sha256"] != account.account_fingerprint):
        raise ValueError("CUTOVER_NIGHTLY_ACCOUNT_BINDING_INVALID")
    session = _path(config["state_root"]) / "sessions" / day
    binding = _json(_pin(session / "binding.json", spec["nightly"]["session_binding_sha256"], watched))
    if binding != exchange._binding(config):
        raise ValueError("CUTOVER_FROZEN_NIGHTLY_BINDING_CHANGED")
    for key, pin_key in (("workflow_config", "workflow_config_sha256"), ("local_profile", "profile_sha256"),
                         ("coordination_active", "coordination_active_sha256")):
        _pin(config[key], binding[pin_key], watched)
    for name, checksum in binding["adapter_sources"].items():
        _pin(_repository() / "tools" / name, checksum, watched)
    status = _json(_pin(session / "status.json", spec["nightly"]["session_status_sha256"], watched))
    if (status.get("status") != "COMPLETE" or status.get("action_date") != day or status.get("actor") != "Atlas"
            or status.get("joint_ready") is not True or status.get("ui_ready") is not True):
        raise ValueError("CUTOVER_MATCHING_HANDOFF_NOT_COMPLETE")
    selected = _json(_pin(session / "handoff-selection.json", spec["nightly"]["handoff_selection_sha256"], watched))
    handoff_spec = selected["spec"]
    if (handoff_spec["action_date"] != day or _path(handoff_spec["datastore_root"]) != root
            or handoff_spec["account_scope_sha256"] != account.account_fingerprint):
        raise ValueError("CUTOVER_HANDOFF_SESSION_DIFFERS")
    preflight_handoff(handoff_spec)
    handoff, pins = _handoff(root, day, account)
    if not handoff["ready"]:
        raise ValueError("CUTOVER_MATCHING_HANDOFF_NOT_COMPLETE")
    watched.update(pins)
    review = handoff_spec["review_session"]
    preparations = {actor: _cached_packet(config, session, actor, "preparation", day, review, watched)
                    for actor in ("atlas", "scout")}
    for actor, selected_preparation in preparations.items():
        if selected_preparation["inputs"]:
            raise ValueError("CUTOVER_PREPARATION_INHERITS_PEER_INPUTS")
        for name, kind in (("plan.json", "plan_package"), ("stats.json", "stats_package")):
            selected_file = handoff_spec["owners"][actor][kind]
            if selected_preparation["files"][name].read_bytes() != _pin(selected_file["path"], selected_file["file_sha256"], watched).read_bytes():
                raise ValueError("CUTOVER_PREPARATION_DIFFERS_FROM_HANDOFF")
    joint = _cached_packet(config, session, "scout", "joint", day, review, watched)
    accepted = _cached_packet(config, session, "atlas", "accepted", day, review, watched)
    expected_inputs = {actor + "_preparation": value["digest"] for actor, value in preparations.items()}
    if (set(joint["inputs"]) != {*expected_inputs, "snapshot"}
            or any(joint["inputs"].get(key) != value for key, value in expected_inputs.items())
            or joint["digest"] != status.get("joint_packet_sha256") or accepted["digest"] != status.get("accepted_packet_sha256")):
        raise ValueError("CUTOVER_MATCHING_PREPARATIONS_NOT_COMPLETE")
    if selected.get("inputs") != {"joint": joint["digest"]}:
        raise ValueError("CUTOVER_FROZEN_HANDOFF_INPUT_DIFFERS")
    for actor, preparation in preparations.items():
        for name in ("plan", "stats"):
            if joint["files"][actor + "-" + name + ".json"].read_bytes() != preparation["files"][name + ".json"].read_bytes():
                raise ValueError("CUTOVER_RETURNED_PREPARATION_DIFFERS")
    exchange._verify_acceptance(config, day, review, joint, accepted)
    local_config = workflow.load_config(Path(config["workflow_config"]))
    state_path = Path(local_config["state_root"]) / "runs" / day / "state.json"
    state = _json(_pin(state_path, spec["nightly"]["local_preparation_sha256"], watched))
    if (state.get("status") != "LOCAL_COMPLETE_PEER_SETUP_PENDING" or state.get("action_date") != day
            or state.get("actor") != "Atlas" or state.get("source_session") != review):
        raise ValueError("CUTOVER_LOCAL_PREPARATION_NOT_COMPLETE")
    workflow._verify_configuration_binding(local_config, state)
    workflow._verify_symbol_binding(local_config, state)
    for step in workflow.STEPS:
        value = state["steps"][step]
        if value.get("status") != "COMPLETE":
            raise ValueError("CUTOVER_LOCAL_PREPARATION_NOT_COMPLETE")
        workflow._verify_outputs(value["output"])
        watched.update(value["output"].get("files", {}))
    workflow._verify_local_preparation(local_config, state)
    return {"session": session, "exchange_lock": _path(config["state_root"]) / "exchange.lock",
            "handoff_completion_id": handoff["completion_id"]}


def _shape(spec):
    if (not isinstance(spec, dict) or set(spec) != (MANUAL_FIELDS if _manual(spec) else FIELDS)
            or spec["schema_version"] not in {VERSION, MANUAL_VERSION}
            or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{7,95}", str(spec["operation_id"]))
            or date.fromisoformat(spec["action_date"]).isoformat() != spec["action_date"]
            or not isinstance(spec["nightly"], dict) or set(spec["nightly"]) != NIGHTLY):
        raise ValueError("CUTOVER_SPECIFICATION_INVALID")
    if _manual(spec) and date.fromisoformat(spec["migration_action_date"]).isoformat() != spec["migration_action_date"]:
        raise ValueError("CUTOVER_ORIGINAL_MIGRATION_DATE_INVALID")


def preflight(spec, *, clock=None, continuation=None):
    """Verify exact local selections without writing or contacting a broker."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    _shape(spec)
    watched = {}
    if spec["implementation"] != implementation():
        raise ValueError("CUTOVER_IMPLEMENTATION_CHANGED")
    native = _load_native(_selection(spec["native_exchange_config"], watched))
    if (native["actor"] != "Atlas" or native["operation_id"] != spec["operation_id"]
            or native["cutover_action_date"] != _migration_date(spec)):
        raise ValueError("CUTOVER_NATIVE_OPERATION_DIFFERS")
    root = _path(native["datastore_root"])
    work = _operation_root(native, spec)
    account_path = _pin(root / CONFIG, spec["account_config_sha256"], watched)
    account = load_account_config(root)
    if account is None or account.role != "coordinator" or account.machine_id != "pc-original" or account.activation["status"] != "PREPARING":
        raise ValueError("CUTOVER_REQUIRES_ATLAS_PREPARING_ACCOUNT")
    runtime = _control_bindings(root) if _manual(spec) else spec["runtime_bindings"]
    _runtime_guard(root, runtime, manual=_manual(spec))
    from tools.native_ownership_exchange import _binding_inputs, resolve_operation_binding
    input_paths = {**_binding_inputs(native), "operation_config": _path(spec["native_exchange_config"]["path"])}
    frozen = {name: file_checksum(_plain(path)) for name, path in input_paths.items()}
    native_binding = {"config": native, "account_binding_sha256": account.fingerprint, "frozen_inputs_sha256": frozen}
    _, binding_pins = resolve_operation_binding(work, native_binding)
    for path, checksum in binding_pins.items():
        _pin(path, checksum, watched)
    for name, path in input_paths.items():
        _pin(path, frozen[name], watched)
    if _manual(spec):
        invocation = spec["manual_invocation"]
        if (not isinstance(invocation, dict) or set(invocation) != {"schema_version", "operation_id", "action_date",
                "migration_action_date", "trigger", "executor", "sole_executor_basis", "requested_at"}
                or invocation["schema_version"] != INVOCATION_VERSION or invocation["operation_id"] != spec["operation_id"]
                or invocation["action_date"] != spec["action_date"] or invocation["migration_action_date"] != _migration_date(spec)
                or invocation["trigger"] not in {"EXPLICIT_MANUAL_START", "READINESS_INSPECTION"}
                or invocation["executor"] != "Atlas" or invocation["sole_executor_basis"] != "LOCAL_HUMAN_ATLAS_ONLY_MANUAL_OPERATION"
                or spec["runtime_bindings"] != {"manual_start_only": True}):
            raise ValueError("MANUAL_START_INVOCATION_INVALID")
        if utc(invocation["requested_at"]).tzinfo is None:
            raise ValueError("MANUAL_START_INVOCATION_INVALID")
    else:
        human_path = _selection(spec["human_authorization"], watched)
        if not human_path.is_relative_to(_repository() / "scratch"):
            raise ValueError("CUTOVER_HUMAN_RECORD_MUST_REMAIN_PRIVATE_LOCAL")
        human = _json(human_path)
        if (set(human) != {"schema_version", "operation_id", "action_date", "basis", "executor", "manual_start_only",
                      "scout_will_not_execute", "authorize_read_only_capture", "authorize_native_installation", "authorize_activation"}
            or human["schema_version"] != HUMAN_VERSION or human["operation_id"] != spec["operation_id"]
            or human["action_date"] != spec["action_date"] or human["basis"] != "DIRECT_ATLAS_LOCAL_HUMAN_INSTRUCTION"
            or human["executor"] != "Atlas" or any(human[key] is not True for key in (
                "manual_start_only", "scout_will_not_execute", "authorize_read_only_capture", "authorize_native_installation", "authorize_activation"))):
            raise ValueError("CUTOVER_EXPLICIT_HUMAN_AUTHORITY_REQUIRED")
    candidate = work / "migration-candidate"
    review = _json(_pin(work / "migration-review.json", spec["migration_review_sha256"], watched))
    manifest = _json(_pin(candidate / "manifest.json", spec["candidate_manifest_sha256"], watched))
    if (manifest.get("status") != "REQUIRES_REVIEWED_ACTIVATION" or manifest.get("cutover_action_date") != _migration_date(spec)
            or manifest.get("account_fingerprint") != account.account_fingerprint
            or manifest.get("symbols") != list(account.symbols)):
        raise ValueError("CUTOVER_CANDIDATE_BINDING_INVALID")
    actual = {path.relative_to(candidate).as_posix(): file_checksum(path) for path in candidate.rglob("*") if path.is_file()}
    if review.get("files") != actual or set(review.get("packets", {})) != {"Atlas", "Scout"}:
        raise ValueError("CUTOVER_REVIEWED_CANDIDATE_CHANGED")
    watched.update({str(candidate / relative): value for relative, value in actual.items()})
    source_export = _json(work / "export/manifest.json")
    ledger = root / LEDGER
    if continuation is None:
        if pin_ledger_files(ledger) != source_export.get("source_pins"):
            raise ValueError("CUTOVER_ATLAS_NATIVE_SOURCE_CHANGED")
    else:
        if not _manual(spec):
            raise ValueError("MANUAL_START_CONTINUATION_REQUIRED")
        prior = _path(continuation["previous_directory"])
        if not prior.is_relative_to(work / "activation/manual-starts") or prior.name != "reconciled":
            raise ValueError("MANUAL_START_CONTINUATION_PATH_INVALID")
        prior_manifest = _json(_pin(prior / "manifest.json", continuation["manifest_sha256"], watched))
        if {path.relative_to(prior).as_posix() for path in prior.rglob("*") if path.is_file()} != (
                set(prior_manifest["output_files"]) | {"manifest.json", "receipt.json"}):
            raise ValueError("MANUAL_START_CONTINUATION_INVENTORY_CHANGED")
        for relative, checksum in prior_manifest["output_files"].items():
            selected = _plain(prior / relative)
            if not selected.resolve().is_relative_to(prior):
                raise ValueError("MANUAL_START_CONTINUATION_PATH_INVALID")
            _pin(selected, checksum, watched)
        prior_receipt = _json(prior / "receipt.json")
        if (prior_receipt.get("status") != "UNION_RECONCILIATION_VERIFIED"
                or prior_receipt.get("manifest_sha256") != continuation["manifest_sha256"]
                or prior_receipt.get("runtime_activation") is not False or prior_receipt.get("orders_placed") != 0):
            raise ValueError("MANUAL_START_CONTINUATION_RECEIPT_INVALID")
        _pin(prior / "receipt.json", file_checksum(prior / "receipt.json"), watched)
        if any(_sha(_bytes(_read_tables(path))) != continuation["logical_sha256"]
               for path in (ledger, prior / "holdings.sqlite3")):
            raise ValueError("MANUAL_START_CONTINUATION_NATIVE_CHANGED")
    watched[str(work / "export/manifest.json")] = file_checksum(work / "export/manifest.json")
    nightly = _completed_handoff(spec, native, account, watched)
    _temporal_gate(spec, clock)
    _guard(watched)
    return {"root": root, "work": _activation_work(native, spec), "operation_root": work,
            "runtime_bindings": runtime, "candidate": candidate, "account": account,
            "account_original": account_path.read_bytes(), "ledger": ledger, "ledger_group": _group(ledger),
            "watched": watched, "native": native, "native_binding": native_binding,
            "native_binding_pins": binding_pins, "continuation": continuation, **nightly}


def _capture(account, clock, *, deadline_guard):
    """Lazy production GET-only acquisition; no order methods are called."""
    from app.services.schwab import SchwabSession
    from ml.stock_trader.state import capture_portfolio_state
    deadline_guard()
    broker = SchwabSession()
    deadline_guard()
    identity = broker.stable_account_fingerprint()
    if identity != account.account_fingerprint:
        raise ValueError("CUTOVER_BROKER_ACCOUNT_MISMATCH")
    deadline_guard()
    observed = capture_portfolio_state(broker, observed_at=clock(), symbols=account.symbols,
        include_order_identities=True, parallel=True, literal_cash_only=True, use_actual_quote_timestamps=True)
    deadline_guard()
    broker.verify_read_snapshot(observed.broker_identity_fingerprint)
    deadline_guard()
    if broker.stable_account_fingerprint() != identity:
        raise ValueError("CUTOVER_BROKER_ACCOUNT_CHANGED")
    deadline_guard()
    return observed


def _portfolio(account, observed, operation):
    if not isinstance(observed, PortfolioState):
        raise ValueError("CUTOVER_NATIVE_PORTFOLIO_REQUIRED")
    return PortfolioEvidence(canonical_sha256([operation, asdict(observed)]), account.account_fingerprint,
        observed.observed_at, observed.held_shares, {symbol: observed.quotes[symbol].ask for symbol in account.symbols},
        dict.fromkeys(account.symbols, 0), observed.source_fingerprint)


def _reclaim_manual_lock(path, root, work, spec):
    """Recover only this operation's dead owner, under the maintenance gate."""
    raw = _plain(path).read_bytes()
    identity = (path.stat().st_dev, path.stat().st_ino)
    pairs = [line.split("=", 1) for line in raw.decode("utf-8").splitlines()]
    if any(len(pair) != 2 for pair in pairs) or len(dict(pairs)) != len(pairs):
        raise ValueError("MANUAL_START_LOCK_OWNER_UNKNOWN")
    owner = dict(pairs)
    pid = _lock_pid(raw.decode("utf-8"))
    if (owner.get("process") != "native-ownership-cutover" or owner.get("operation") != spec["operation_id"]
            or owner.get("work") != str(work) or pid is None or _pid_is_running(pid)):
        raise ValueError("MANUAL_START_LOCK_OWNER_NOT_RECOVERABLE")
    _same_manual_inputs(_json(work / "spec.json"), spec)
    journal_path = work / "journal.json"
    record = _json(journal_path)
    attempt = _journal(record, spec, work)
    status = _recover(root, attempt, record, spec)
    archive = work / "recovered-locks" / _sha(raw)
    _once(archive / "owner.txt", raw)
    _once(archive / (path.name + ".json"), _bytes({"path": str(path), "identity": list(identity),
        "owner_sha256": _sha(raw), "journal_sha256": file_checksum(journal_path), "state": status}))
    if ((path.stat().st_dev, path.stat().st_ino) != identity or path.read_bytes() != raw
            or _pid_is_running(pid)):
        raise ValueError("MANUAL_START_LOCK_OWNER_CHANGED")
    path.unlink()


@contextmanager
def _native_lock(path, root, work, disposition, *, manual_spec=None):
    """Preserve foreign owners; recover a coherent dead manual attempt only."""
    path = _plain(path)
    payload = ("process=native-ownership-cutover\npid=" + str(os.getpid()) + "\nstarted_at=" +
               datetime.now(timezone.utc).isoformat() + "\ntoken=" + uuid.uuid4().hex + "\n").encode()
    if manual_spec is not None:
        payload += ("operation=" + manual_spec["operation_id"] + "\nwork=" + str(work) + "\n").encode()
    with runtime_lock_maintenance_gate(path.parent, timeout=0):
        if path.exists() and manual_spec is not None:
            _reclaim_manual_lock(path, root, work, manual_spec)
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        owned_identity = (path.stat().st_dev, path.stat().st_ino)
    try:
        yield
    finally:
        safe = False
        if disposition["active_config_sha256"] is not None:
            try:
                safe = file_checksum(_plain(root / CONFIG)) == disposition["active_config_sha256"]
            except Exception:
                safe = False
        if not safe:
            try:
                config = load_account_config(root)
                safe = config is not None and config.activation["status"] == "PREPARING"
            except Exception:
                safe = False
        with runtime_lock_maintenance_gate(path.parent, timeout=0):
            if (path.is_file() and (path.stat().st_dev, path.stat().st_ino) == owned_identity
                    and path.read_bytes() == payload):
                if safe:
                    path.unlink()
                else:
                    # Missing PID is deliberately non-recoverable by the native
                    # stale-process mechanism. This is an exclusion marker,
                    # never a process observation or a claim about Scout.
                    retained = ("process=native-ownership-cutover-recovery-required\n" +
                                "operation=" + work.parent.name + "\n" +
                                "journal=" + str(work / "journal.json") + "\n").encode()
                    _atomic(path, retained)


@contextmanager
def _locks(root, work, exchange_lock, *, operation_root=None, manual_spec=None):
    disposition = {"active_config_sha256": None}
    with ExitStack() as stack:
        stack.enter_context(FileLock(str((operation_root or work.parent) / "operation.lock"), timeout=0))
        stack.enter_context(FileLock(str(exchange_lock), timeout=0))
        for name in ("independent-stock-session.lock", "stock-trader-hourly.lock"):
            stack.enter_context(_native_lock(root / "locks" / name, root, work, disposition, manual_spec=manual_spec))
        stack.enter_context(FileLock(str(root / "state/account-gameplan/preparation.lock"), timeout=0))
        yield disposition


def _tables(db):
    schema = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    present = {row["name"] for row in schema if row["type"] == "table"}
    if (present - set(_COLUMNS) or set(_COLUMNS) - _OPTIONAL - present
            or any(row["type"] in {"view", "trigger"} for row in schema)):
        raise ValueError("CUTOVER_LIVE_NATIVE_SCHEMA_INVALID")
    result = {}
    for name, columns in _COLUMNS.items():
        if name not in present:
            result[name] = []
            continue
        if [row["name"] for row in db.execute("PRAGMA table_info(" + name + ")")] != columns.split():
            raise ValueError("CUTOVER_LIVE_NATIVE_COLUMNS_INVALID")
        result[name] = [dict(row) for row in db.execute("SELECT * FROM " + name + " ORDER BY rowid")]
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("CUTOVER_NATIVE_DATABASE_INTEGRITY_INVALID")
    return result


def _read_tables(path):
    # SQLite read marks may mutate SHM even with mode=ro. Never open the raw
    # originals, the pinned candidate or the live ledger for diagnostic reads.
    path = _plain(path)
    pins = _group(path)
    if pins.get("-journal") is not None:
        raise ValueError("CUTOVER_ROLLBACK_JOURNAL_REQUIRES_REVIEW")
    with TemporaryDirectory(prefix="ducketz-cutover-read-") as temporary:
        copied = Path(temporary) / "holdings.sqlite3"
        for suffix, expected in pins.items():
            if expected is not None:
                destination = Path(str(copied) + suffix)
                shutil.copyfile(Path(str(path) + suffix), destination)
                if file_checksum(destination) != expected:
                    raise ValueError("CUTOVER_NATIVE_CHANGED_DURING_READ")
        if _group(path) != pins:
            raise ValueError("CUTOVER_NATIVE_CHANGED_DURING_READ")
        with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            result = _tables(db)
        if _group(path) != pins:
            raise ValueError("CUTOVER_NATIVE_CHANGED_DURING_READ")
        return result


def _install_rows(live, candidate, *, before, guard, checkpoint):
    """One native SQLite transaction; never replace paths or unlink sidecars.

    Exact native logical rows and their insertion chronology are preserved. The
    original database/WAL bytes stay in private backup. SQLite owns commit,
    reader coexistence and crash recovery for the live database.
    """
    expected = _read_tables(candidate)
    with closing(sqlite3.connect(_plain(live).as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            if _tables(db) != before:
                raise ValueError("CUTOVER_NATIVE_PREIMAGE_CHANGED_UNDER_WRITE_LOCK")
            guard()
            present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name, columns in _OPTIONAL_SCHEMA.items():
                if name not in present:
                    db.execute("CREATE TABLE " + name + " (" + columns + ")")
            if "fallback_days" not in present:
                db.execute("CREATE TABLE fallback_days (account TEXT NOT NULL, action_date TEXT NOT NULL, baseline_id TEXT NOT NULL UNIQUE, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), source_fingerprint TEXT NOT NULL, policy TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account,action_date))")
            order = ("metadata", "allocations", "snapshots", "reservations", "fills", "evidence", "blocks",
                     "cancellations", "fallback_days", "inventory_assignments", "inventory_assignment_releases")
            for name in reversed(order):
                db.execute("DELETE FROM " + name)
            for name in order:
                columns = _COLUMNS[name].split()
                db.executemany("INSERT INTO " + name + " (" + ",".join(columns) + ") VALUES (" +
                    ",".join("?" for _ in columns) + ")", [[row[column] for column in columns] for row in expected[name]])
            if _tables(db) != expected:
                raise ValueError("CUTOVER_NATIVE_STAGED_ROWS_DIFFER")
            checkpoint("before_sqlite_commit")
            guard()
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
    checkpoint("sqlite_committed")
    if _read_tables(live) != expected:
        raise ValueError("CUTOVER_NATIVE_COMMITTED_ROWS_DIFFER")
    return expected


def _recover(root, attempt, record, spec):
    """Classify interrupted PREPARING state; never roll back a committed union."""
    original = _plain(attempt / "original/config.json").read_bytes()
    if (_sha(original) != spec["account_config_sha256"]
            or _group(attempt / "original/holdings.sqlite3") != record.get("original_group")):
        raise ValueError("CUTOVER_RECOVERY_ORIGINALS_CHANGED")
    if _plain(root / CONFIG).read_bytes() != original:
        raise ValueError("CUTOVER_RECOVERY_ACCOUNT_CHANGED_REVIEW_REQUIRED")
    current = _sha(_bytes(_read_tables(root / LEDGER)))
    if current == record.get("installed_logical_sha256"):
        return "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED"
    if current == record.get("original_logical_sha256"):
        return "ORIGINALS_PRESERVED_RETRY_REQUIRED"
    raise ValueError("CUTOVER_RECOVERY_NATIVE_CHANGED_REVIEW_REQUIRED")


def apply(spec, *, clock=None, capture=None, checkpoint=None):
    """One explicit transaction; injected capture/checkpoints are offline-test seams.

    Manual retries reconcile the last proven installed union with fresh evidence.
    Legacy explicitly dated retries retain their saved-state-only disposition.
    An exact committed ACTIVE transition is terminal and is never reinstalled.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    checkpoint = checkpoint or (lambda _: None)
    _shape(spec)
    if _manual(spec) and spec["manual_invocation"]["trigger"] != "EXPLICIT_MANUAL_START":
        raise ValueError("MANUAL_START_INVOCATION_REQUIRED")
    # Resolve fixed local paths before preflight so a crash after activation can
    # verify its own exact terminal journal without replaying the old exchange.
    native = _load_native(_selection(spec["native_exchange_config"], {}))
    if (native["actor"] != "Atlas" or native["operation_id"] != spec["operation_id"]
            or native["cutover_action_date"] != _migration_date(spec)):
        raise ValueError("CUTOVER_NATIVE_OPERATION_DIFFERS")
    root = _path(native["datastore_root"])
    work = _activation_work(native, spec)
    config = _json(_repository() / "scratch/nightly-workflow/exchange-config.json")
    exchange_lock = _path(config["state_root"]) / "exchange.lock"
    work.mkdir(parents=True, exist_ok=True)
    with _locks(root, work, exchange_lock, operation_root=_operation_root(native, spec),
                manual_spec=spec if _manual(spec) else None) as disposition:
        if _manual(spec):
            if (work / "spec.json").exists():
                _same_manual_inputs(_json(work / "spec.json"), spec)
            else:
                _once(work / "spec.json", _bytes(spec))
            _once(work / "specifications" / (_sha(_bytes(spec)) + ".json"), _bytes(spec))
        else:
            _once(work / "spec.json", _bytes(spec))
        journal = work / "journal.json"
        previous = _json(journal) if journal.exists() else None
        continuation = None
        if previous:
            attempt = _journal(previous, spec, work)
            active_path = _plain(attempt / "active-config.json")
            active_bytes = active_path.read_bytes() if active_path.is_file() else None
            if active_bytes is not None and (root / CONFIG).read_bytes() == active_bytes:
                verify_cutover(root, load_account_config(root))
                if (previous["phase"] not in {"LEDGER_INSTALLED", "COMPLETE"}
                        or _sha(active_bytes) != previous.get("activation_config_sha256")):
                    raise ValueError("CUTOVER_ACTIVE_TRANSACTION_PHASE_INVALID")
                previous["phase"] = "COMPLETE"
                _atomic(journal, _bytes(previous))
                disposition["active_config_sha256"] = _sha(active_bytes)
                return _result(spec, "ACTIVE_CUTOVER_VERIFIED", changed=False, captured=False)
            if previous["phase"] in {"INSTALLING", "LEDGER_INSTALLED", "RECOVERY_REQUIRED", "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED"}:
                status = _recover(root, attempt, previous, spec)
                previous["phase"] = status
                _atomic(journal, _bytes(previous))
                if not _manual(spec):
                    return _result(spec, status, changed=False, captured=False)
                if status == "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED":
                    continuation = {"previous_directory": str(attempt / "reconciled"),
                                    "manifest_sha256": previous["reconciled_manifest_sha256"],
                                    "logical_sha256": previous["installed_logical_sha256"]}
            if _manual(spec) and continuation is None:
                continuation = previous.get("continuation")
            if previous["phase"] == "COMPLETE":
                raise ValueError("CUTOVER_COMPLETED_ACCOUNT_BINDING_CHANGED")
        prepared = preflight(spec, clock=clock, continuation=continuation)
        if (previous or {}).get("number", 0) >= 9999:
            raise ValueError("CUTOVER_RETRY_BOUND_EXHAUSTED")
        attempt_name = "attempt-" + str((previous or {}).get("number", 0) + 1).zfill(4)
        attempt = work / attempt_name
        attempt.mkdir(exist_ok=False)
        record = {"schema_version": VERSION, "operation_id": spec["operation_id"], "attempt": attempt_name,
                  "number": (previous or {}).get("number", 0) + 1, "phase": "PREPARING_BACKUP",
                  "original_group": prepared["ledger_group"], "human_evidence_kind": (
                      "LOCAL_MANUAL_START_INVOCATION" if _manual(spec) else "HUMAN_ATTESTED_SOLE_EXECUTOR")}
        if _manual(spec):
            record["specification_sha256"] = _sha(_bytes(spec))
        if continuation is not None:
            record["continuation"] = continuation
        _atomic(journal, _bytes(record))
        original = attempt / "original"
        original.mkdir()
        _once(original / "config.json", prepared["account_original"])
        for suffix, expected in prepared["ledger_group"].items():
            if expected is not None:
                source = Path(str(prepared["ledger"]) + suffix)
                destination = original / ("holdings.sqlite3" + suffix)
                with source.open("rb") as source_file, destination.open("xb") as saved:
                    shutil.copyfileobj(source_file, saved)
                    saved.flush()
                    os.fsync(saved.fileno())
                if file_checksum(destination) != expected:
                    raise ValueError("CUTOVER_SOURCE_CHANGED_DURING_BACKUP")
        before = _read_tables(original / "holdings.sqlite3")
        if _group(original / "holdings.sqlite3") != prepared["ledger_group"]:
            raise ValueError("CUTOVER_ORIGINAL_BACKUP_CHANGED")
        record["original_logical_sha256"] = _sha(_bytes(before))
        record["phase"] = "PREPARED"
        _atomic(journal, _bytes(record))
        try:
            _guard(prepared["watched"])
            if _group(prepared["ledger"]) != prepared["ledger_group"]:
                raise ValueError("CUTOVER_ATLAS_NATIVE_SOURCE_CHANGED")
            _temporal_gate(spec, clock)
            if implementation() != spec["implementation"]:
                raise ValueError("CUTOVER_IMPLEMENTATION_CHANGED")
            _runtime_guard(root, prepared["runtime_bindings"], manual=_manual(spec))
            checkpoint("before_capture")
            _temporal_gate(spec, clock)
            observed = (capture(prepared["account"], clock) if capture is not None else
                        _capture(prepared["account"], clock, deadline_guard=lambda: _temporal_gate(spec, clock)))
            portfolio = _portfolio(prepared["account"], observed, spec["operation_id"])
            _guard(prepared["watched"])
            if implementation() != spec["implementation"]:
                raise ValueError("CUTOVER_IMPLEMENTATION_CHANGED")
            if _group(prepared["ledger"]) != prepared["ledger_group"]:
                raise ValueError("CUTOVER_ATLAS_NATIVE_SOURCE_CHANGED")
            from ml.account_gameplan.cutover import reconcile_startup_candidate, reconcile_startup_continuation
            reconcile = reconcile_startup_candidate if _manual(spec) else reconcile_migration_candidate
            source_args = {"candidate_directory": prepared["candidate"], "expected_manifest_sha256": spec["candidate_manifest_sha256"]}
            if continuation is not None:
                reconcile = reconcile_startup_continuation
                source_args = {"previous_directory": Path(continuation["previous_directory"]),
                               "expected_manifest_sha256": continuation["manifest_sha256"]}
            reconciled = reconcile(**source_args, expected_participants=prepared["account"].participants,
                expected_account_fingerprint=prepared["account"].account_fingerprint, portfolio=portfolio,
                expected_portfolio_sha256=canonical_sha256(asdict(portfolio)), broker_snapshot=observed,
                expected_broker_snapshot_sha256=canonical_sha256(asdict(observed)), destination_directory=attempt / "reconciled", clock=clock)
            ready = attempt / "reconciled"
            database = ready / "holdings.sqlite3"
            database_sha = reconciled["report"]["database_sha256"]
            _pin(database, database_sha, prepared["watched"])
            for path in ready.rglob("*"):
                if path.is_file():
                    _pin(path, file_checksum(path), prepared["watched"])
            authority_bytes = (_bytes(spec["manual_invocation"]) if _manual(spec) else
                               _path(spec["human_authorization"]["path"]).read_bytes())
            receipt = {"schema_version": ACCOUNT_VERSION, "status": "VERIFIED",
                "binding_sha256": prepared["account"].fingerprint, "machine_id": "pc-original", "coordinator_id": "pc-original",
                "peer_execution_fenced": True, "peer_fence_receipt_sha256": _sha(authority_bytes),
                "migration_manifest_sha256": spec["candidate_manifest_sha256"],
                "fresh_union_reconciliation_sha256": file_checksum(ready / "report.json"),
                "installed_source_commit": spec["implementation"]["source_identity"]["commit"], "orders_placed": 0}
            receipt_name = spec["operation_id"] + ("-manual-" + spec["action_date"] if _manual(spec) else "")
            receipt_path = root / "state/account-gameplan/cutovers" / (receipt_name + "-" + attempt_name + ".json")
            _once(receipt_path, _bytes(receipt))
            authority_path = attempt / ("manual-invocation.json" if _manual(spec) else "human-authority.json")
            _once(authority_path, authority_bytes)
            active = json.loads(prepared["account_original"])
            active["activation"] = {"status": "ACTIVE", "binding_sha256": prepared["account"].fingerprint,
                "receipt_path": receipt_path.relative_to(root).as_posix(), "receipt_sha256": file_checksum(receipt_path)}
            active_bytes = _bytes(active)
            _once(attempt / "active-config.json", active_bytes)
            for path in (receipt_path, attempt / "active-config.json", authority_path):
                _pin(path, file_checksum(path), prepared["watched"])
            _guard(prepared["watched"])
            _temporal_gate(spec, clock)
            if not 0 <= (utc(clock()) - utc(observed.observed_at)).total_seconds() <= 60:
                raise ValueError("CUTOVER_OBSERVATION_EXPIRED_BEFORE_INSTALL")
            expected_tables = _read_tables(database)
            record.update(phase="INSTALLING", candidate_database_sha256=database_sha,
                          reconciled_manifest_sha256=file_checksum(ready / "manifest.json"),
                          installed_logical_sha256=_sha(_bytes(expected_tables)),
                          activation_config_sha256=_sha(active_bytes), cutover_receipt_sha256=file_checksum(receipt_path),
                          handoff_completion_id=prepared["handoff_completion_id"])
            _atomic(journal, _bytes(record))
            checkpoint("before_install")
            if _group(prepared["ledger"]) != prepared["ledger_group"]:
                raise ValueError("CUTOVER_ATLAS_NATIVE_SOURCE_CHANGED")
            def mutation_guard():
                from tools.native_ownership_exchange import resolve_operation_binding
                _guard(prepared["watched"])
                _, binding_pins = resolve_operation_binding(prepared["operation_root"], prepared["native_binding"])
                if binding_pins != prepared["native_binding_pins"]:
                    raise ValueError("CUTOVER_NATIVE_BINDING_EVIDENCE_CHANGED")
                if implementation() != spec["implementation"]:
                    raise ValueError("CUTOVER_IMPLEMENTATION_CHANGED")
                _temporal_gate(spec, clock)
                if not 0 <= (utc(clock()) - utc(observed.observed_at)).total_seconds() <= 60:
                    raise ValueError("CUTOVER_OBSERVATION_EXPIRED_BEFORE_ACTIVATION")
                if _group(original / "holdings.sqlite3") != prepared["ledger_group"]:
                    raise ValueError("CUTOVER_ORIGINAL_BACKUP_CHANGED")
            _install_rows(prepared["ledger"], database, before=before, guard=mutation_guard, checkpoint=checkpoint)
            record["phase"] = "LEDGER_INSTALLED"
            record["installed_group"] = _group(prepared["ledger"])
            _atomic(journal, _bytes(record))
            checkpoint("ledger_installed")
            mutation_guard()
            if _read_tables(prepared["ledger"]) != expected_tables:
                raise ValueError("CUTOVER_NATIVE_CHANGED_BEFORE_ACTIVATION")
            _runtime_guard(root, prepared["runtime_bindings"], manual=_manual(spec))
            mutation_guard()
            _atomic(root / CONFIG, active_bytes, expected_preimage=prepared["account_original"])
            checkpoint("config_activated")
            verify_cutover(root, load_account_config(root))
            prepared["watched"][str(root / CONFIG)] = _sha(active_bytes)
            mutation_guard()
            _runtime_guard(root, prepared["runtime_bindings"], manual=_manual(spec))
            mutation_guard()
            record["phase"] = "COMPLETE"
            _atomic(journal, _bytes(record))
            disposition["active_config_sha256"] = _sha(active_bytes)
            return _result(spec, "ACTIVE_CUTOVER_VERIFIED", changed=True, captured=True)
        except Exception:
            # Only restore the exact config written by this attempt. Preserve a
            # committed native union; its reconciliation cannot be undone by
            # replacing database files or replaying older ownership.
            if (record["phase"] == "LEDGER_INSTALLED" and "active_bytes" in locals()
                    and (root / CONFIG).read_bytes() == active_bytes):
                _atomic(root / CONFIG, prepared["account_original"], expected_preimage=active_bytes)
            if record["phase"] in {"INSTALLING", "LEDGER_INSTALLED"} and (root / CONFIG).read_bytes() == prepared["account_original"]:
                try:
                    record["phase"] = _recover(root, attempt, record, spec)
                except Exception:
                    record["phase"] = "RECOVERY_REQUIRED"
                _atomic(journal, _bytes(record))
            elif record["phase"] in {"INSTALLING", "LEDGER_INSTALLED"}:
                record["phase"] = "RECOVERY_REQUIRED"
                _atomic(journal, _bytes(record))
            raise


def _result(spec, status, *, changed, captured):
    return {"schema_version": VERSION, "operation_id": spec["operation_id"], "action_date": spec["action_date"],
            "status": status, "activation_changed": changed, "broker_read_performed": captured,
            "trader_started": False, "orders_placed": 0, "manual_start_required": True,
            "nightly_session_rebound": False}


def _manual_spec(root, *, clock, invoke):
    """Select existing local evidence; no user-authored approval document."""
    config_path = _repository() / "scratch/nightly-workflow/native-ownership-exchange-config.json"
    if not config_path.is_file():
        raise ValueError("NATIVE_INVENTORY_CONFIGURATION_MISSING")
    if _path(_json(config_path)["datastore_root"]) != root:
        raise ValueError("NATIVE_INVENTORY_DATASTORE_MISMATCH")
    native = _load_native(config_path)
    if native["actor"] != "Atlas":
        raise ValueError("MANUAL_START_REQUIRES_ATLAS")
    operation = _path(native["state_root"]) / native["operation_id"]
    candidate, review = operation / "migration-candidate/manifest.json", operation / "migration-review.json"
    if not candidate.is_file() or not review.is_file():
        if not (operation / "scout-selection.json").is_file():
            raise ValueError("SCOUT_OWNERSHIP_HISTORY_PENDING")
        raise ValueError("NATIVE_UNION_ASSEMBLY_PENDING")
    # The bootstrap's original completed preparation is provenance, not an
    # expiry date. The worker selects the actual executable session itself.
    day = native["cutover_action_date"]
    exchange_path = _repository() / "scratch/nightly-workflow/exchange-config.json"
    exchange = _json(exchange_path)
    session = _path(exchange["state_root"]) / "sessions" / day
    workflow = _json(_path(exchange["workflow_config"]))
    preparation = _path(workflow["state_root"]) / "runs" / day / "state.json"
    selected = {"exchange_config_sha256": exchange_path, "session_binding_sha256": session / "binding.json",
                "session_status_sha256": session / "status.json", "handoff_selection_sha256": session / "handoff-selection.json",
                "local_preparation_sha256": preparation}
    if any(not path.is_file() for path in selected.values()):
        raise ValueError("GAMEPLAN_HANDOFF_NOT_READY")
    spec = {"schema_version": MANUAL_VERSION, "operation_id": native["operation_id"], "action_date": day,
        "migration_action_date": day, "native_exchange_config": {"path": str(config_path), "file_sha256": file_checksum(config_path)},
        "account_config_sha256": file_checksum(root / CONFIG), "candidate_manifest_sha256": file_checksum(candidate),
        "migration_review_sha256": file_checksum(review), "nightly": {name: file_checksum(path) for name, path in selected.items()},
        "implementation": implementation(), "runtime_bindings": {"manual_start_only": True},
        "manual_invocation": {"schema_version": INVOCATION_VERSION, "operation_id": native["operation_id"],
            "action_date": day, "migration_action_date": day, "trigger": "EXPLICIT_MANUAL_START" if invoke else "READINESS_INSPECTION",
            "executor": "Atlas", "sole_executor_basis": "LOCAL_HUMAN_ATLAS_ONLY_MANUAL_OPERATION",
            "requested_at": utc(clock()).isoformat()}}
    first_spec = _activation_work(native, spec) / "spec.json"
    if first_spec.exists():
        _same_manual_inputs(_json(first_spec), spec)
    return spec, native


def _pending_continuation(spec, native, root):
    work = _activation_work(native, spec)
    if not (work / "journal.json").is_file():
        return None
    previous = _json(work / "journal.json")
    attempt = _journal(previous, spec, work)
    if previous["phase"] in {"INSTALLING", "LEDGER_INSTALLED", "RECOVERY_REQUIRED", "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED"}:
        status = _recover(root, attempt, previous, spec)
        if status == "UNION_INSTALLED_FRESH_RECONCILIATION_REQUIRED":
            return {"previous_directory": str(attempt / "reconciled"), "manifest_sha256": previous["reconciled_manifest_sha256"],
                    "logical_sha256": previous["installed_logical_sha256"]}
    return previous.get("continuation")


def _startup_reason(error):
    message = str(error)
    direct = {"SCOUT_OWNERSHIP_HISTORY_PENDING", "NATIVE_UNION_ASSEMBLY_PENDING", "NATIVE_INVENTORY_CONFIGURATION_MISSING",
              "NATIVE_INVENTORY_DATASTORE_MISMATCH", "GAMEPLAN_HANDOFF_NOT_READY", "MANUAL_START_REQUIRES_ATLAS",
              "MANUAL_START_REQUIRES_GAMEPLAN_POLICY", "MANUAL_START_FROZEN_INPUTS_CHANGED"}
    if message in direct:
        return message
    if "Pending or unknown broker orders" in message:
        return "PENDING_ACCOUNT_ORDERS"
    if ("Producer baseline reconciliation failed" in message or "Union native reconciliation" in message
            or "Union native continuation" in message):
        return "OWNERSHIP_DIFFERS_FROM_CURRENT_ACCOUNT"
    if "stale" in message.lower() or "EXPIRED" in message:
        return "ACCOUNT_SNAPSHOT_EXPIRED_RETRY_START"
    if message == "CUTOVER_ATLAS_NATIVE_SOURCE_CHANGED" or "NATIVE_CHANGED" in message:
        return "NATIVE_OWNERSHIP_CHANGED_SINCE_TRANSFER"
    if (isinstance(error, FileExistsError) or type(error).__name__ == "Timeout"
            or message.startswith("MANUAL_START_LOCK_OWNER_")):
        return "STARTUP_OWNERSHIP_LOCK_PRESENT"
    if "HANDOFF" in message or "PREPARATION" in message:
        return "GAMEPLAN_HANDOFF_NOT_READY"
    return "NATIVE_OWNERSHIP_VALIDATION_FAILED"


def inspect_staged_startup(root, *, action_date, now=None):
    """Read-only overnight inventory readiness, with no invocation or broker."""
    root = _path(root)
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    try:
        spec, native = _manual_spec(root, clock=clock, invoke=False)
        account = load_account_config(root)
        from tools.gameplan_execution_readiness import _handoff
        handoff, current_pins = _handoff(root, action_date, account)
        if not handoff["ready"]:
            raise ValueError("GAMEPLAN_HANDOFF_NOT_READY")
        preflight(spec, clock=clock, continuation=_pending_continuation(spec, native, root))
        _guard(current_pins)
        return {"ready": True, "status": "READY_FOR_MANUAL_START", "preparation_action_date": spec["action_date"],
                "broker_reconciliation_pending": True, "activation_changed": False}
    except (ValueError, OSError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        return {"ready": False, "status": "INVENTORY_SETUP_PENDING", "reason": _startup_reason(error),
                "broker_reconciliation_pending": True, "activation_changed": False}


def ensure_gameplan_account_ready(root, *, sizing_policy, clock=None, capture=None):
    """Explicit manual Start performs inventory bootstrap once, then verifies it.

    Scheduled launchers must not call this function. They retain the ordinary
    read-only PREPARING refusal. No control flag, worker or order is started here.
    """
    root = _path(root)
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
        if sizing_policy != GAMEPLAN_SIZING_POLICY:
            raise ValueError("MANUAL_START_REQUIRES_GAMEPLAN_POLICY")
        account = load_account_config(root)
        if account is None:
            return {"status": "ACCOUNT_READY", "ready": True, "activation_changed": False, "broker_read_performed": False}
        if account.machine_id != "pc-original" or account.role != "coordinator":
            raise ValueError("MANUAL_START_REQUIRES_ATLAS")
        if account.activation["status"] == "ACTIVE":
            verify_cutover(root, account)
            return {"status": "ACCOUNT_READY", "ready": True, "activation_changed": False, "broker_read_performed": False}
        spec, _ = _manual_spec(root, clock=clock, invoke=True)
        result = apply(spec, clock=clock, capture=capture)
        if result["status"] != "ACTIVE_CUTOVER_VERIFIED":
            return {**result, "status": "ACCOUNT_NOT_READY", "ready": False, "reason": "NATIVE_OWNERSHIP_RECOVERY_REQUIRED"}
        return {**result, "status": "ACCOUNT_READY", "ready": True}
    except Exception as error:
        # Exact diagnostics stay local; provider replies never reach the launcher.
        diagnostic = {"schema_version": MANUAL_VERSION, "error_type": type(error).__name__, "detail": str(error)}
        raw = _bytes(diagnostic)
        local = _repository() / "scratch/nightly-workflow/manual-start-failures" / (_sha(raw) + ".json")
        _once(local, raw)
        return {"status": "ACCOUNT_NOT_READY", "ready": False, "reason": _startup_reason(error),
                "activation_changed": False, "broker_read_performed": None, "trader_started": False, "orders_placed": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        spec = _json(_path(args.spec))
        if args.apply:
            result = apply(spec)
        else:
            preflight(spec)
            result = _result(spec, "CUTOVER_INPUTS_VERIFIED", changed=False, captured=False)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] in {"CUTOVER_INPUTS_VERIFIED", "ACTIVE_CUTOVER_VERIFIED"} else 2
    except Exception as error:
        # Provider exceptions can contain private replies. Never print them.
        reason = str(error) if str(error).startswith("CUTOVER_") and re.fullmatch("[A-Z_]+", str(error)) else "CUTOVER_REVIEW_OR_RECOVERY_REQUIRED"
        print(json.dumps({"status": "BLOCKED", "reason": reason, "orders_placed": 0, "trader_started": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
