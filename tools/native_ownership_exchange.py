"""One bounded, separately authorized native-accounting exchange for cutover.

This is private CODEXSTORE transport, never Git publication. It exports only the
reviewed normalized ledger format, preserves originals locally, and stages an
unreconciled union candidate. It cannot call a broker, activate or start trading.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from filelock import FileLock, Timeout

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.config import load_account_config, verify_cutover
from ml.account_gameplan.migration import consolidate_ownership_ledgers, pin_ledger_files
from ml.account_gameplan.migration import _plain
from tools.native_ownership_export import export_ownership, read_export, _private_scan, _installed_private_values

VERSION = "native-ownership-exchange-v1"
MAX_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
ACTORS = {"Atlas": "pc-original", "Scout": "pc-new"}
FIELDS = {"schema_version", "actor", "local_profile", "coordination_active", "datastore_root",
          "exchange_root", "state_root", "operation_id", "cutover_action_date",
          "private_native_ledger_exchange_authorized"}


class Pending(Exception):
    pass


def _bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _file_sha(path, limit=2 * MAX_BYTES):
    digest, total = hashlib.sha256(), 0
    with _plain(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if total > limit:
                raise ValueError("Native candidate exceeds its reviewed byte bound")
            digest.update(chunk)
    return digest.hexdigest()


def _read(path, limit=MAX_BYTES):
    with _plain(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Native accounting packet exceeds its byte limit")
    return raw


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate native accounting JSON field")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("Native accounting object required")
    return value


def _write(path, raw, *, replace=False, guard=None):
    path = _plain(path)
    if guard is not None:
        guard()
    if path.exists():
        if _read(path) == raw:
            return
        if not replace:
            raise ValueError("Frozen native accounting selection changed; retain and review")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if guard is not None:
            guard()
        _plain(path)
        if replace:
            os.replace(temporary, path)
        else:
            try:
                # Windows rename refuses existing destinations. POSIX link
                # provides the same no-clobber publication of complete bytes.
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
            except FileExistsError:
                if _read(path) != raw:
                    raise ValueError("Frozen native accounting selection changed; retain and review") from None
    finally:
        temporary.unlink(missing_ok=True)


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or value.startswith(("//", "\\\\")):
        raise ValueError("Explicit local absolute native accounting path required")
    if ".." in Path(value).parts:
        raise ValueError("Native accounting paths cannot traverse parents")
    return _plain(value).resolve()


def _repository():
    return Path(__file__).resolve().parents[1]


def _reviewed_paths():
    folder = _repository() / "scratch/nightly-workflow"
    return {"workflow": folder / "config.json", "exchange": folder / "exchange-config.json"}


def _verify_installation(active_path):
    active = _json(_read(active_path, 128 * 1024))
    release = _absolute(active["release_root"])
    result = subprocess.run([sys.executable, "-B", str(release / "tools/cross_pc/cli.py"),
                             "verify-installation", "--active", str(active_path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode or _json(result.stdout).get("verified") is not True:
        raise ValueError("Pinned coordination installation failed verification")


def load_config(path):
    config = _json(_read(path, 128 * 1024))
    if (set(config) != FIELDS or config["schema_version"] != VERSION or config["actor"] not in ACTORS
            or config["private_native_ledger_exchange_authorized"] is not True):
        raise ValueError("Native accounting exchange requires its own explicit local authorization")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,95}", str(config["operation_id"])):
        raise ValueError("Stable native cutover operation identity required")
    if date.fromisoformat(config["cutover_action_date"]).isoformat() != config["cutover_action_date"]:
        raise ValueError("Exact cutover action date required")
    paths = {key: _absolute(config[key]) for key in (
        "local_profile", "coordination_active", "datastore_root", "exchange_root", "state_root")}
    exchange, state = paths["exchange_root"], paths["state_root"]
    if (tuple(p.lower() for p in exchange.parts[-2:]) != ("ducketz-nightly-exchange", "v1")
            or "codexstore" not in [p.lower() for p in exchange.parts[:-2]]
            or state == exchange or state.is_relative_to(exchange) or exchange.is_relative_to(state)):
        raise ValueError("Use authorized private CODEXSTORE exchange with separate local state")
    profile = _json(_read(paths["local_profile"], 128 * 1024))
    repository = _repository()
    if (profile.get("actor") != config["actor"] or profile.get("machine") != ACTORS[config["actor"]]
            or _absolute(profile.get("checkout")) != repository
            or paths["local_profile"] != repository / "scratch/cross-pc/local-profile.json"
            or paths["coordination_active"] != repository / "scratch/cross-pc/active.json"):
        raise ValueError("Native accounting exchange differs from this machine's private profile")
    if not state.is_relative_to(repository / "scratch"):
        raise ValueError("Native accounting local staging must remain inside this checkout's ignored scratch")
    reviewed = {name: _json(_read(source, 128 * 1024)) for name, source in _reviewed_paths().items()}
    workflow, shared = reviewed["workflow"], reviewed["exchange"]
    if (workflow.get("actor") != config["actor"] or shared.get("actor") != config["actor"]
            or _absolute(workflow.get("repository")) != repository
            or _absolute(workflow.get("datastore")) != paths["datastore_root"]
            or _absolute(shared.get("exchange_root")) != exchange
            or _absolute(shared.get("workflow_config")) != _reviewed_paths()["workflow"]
            or shared.get("private_exchange_authorized") is not True
            or any(_absolute(saved.get(key)) != paths[key] for saved in (workflow, shared)
                   for key in ("local_profile", "coordination_active"))):
        raise ValueError("Native accounting roots differ from the existing reviewed nightly bindings")
    _verify_installation(paths["coordination_active"])
    account = load_account_config(paths["datastore_root"])
    if account is None or account.machine_id != ACTORS[config["actor"]]:
        raise ValueError("Native accounting account belongs to another machine")
    owners = shared.get("owners")
    if (set(account.participants) != set(ACTORS.values())
            or any(len(symbols) != 11 for symbols in account.participants.values())
            or set(profile.get("symbols", [])) != set(account.participants[account.machine_id])
            or shared.get("account_scope_sha256") != account.account_fingerprint
            or not isinstance(owners, dict) or set(owners) != {"atlas", "scout"}
            or any(not isinstance(owners[actor.lower()], list) or len(owners[actor.lower()]) != 11
                   or set(owners[actor.lower()]) != set(account.participants[machine]) for actor, machine in ACTORS.items())
            or (shared.get("account_config") is not None and
                _absolute(shared["account_config"]) != paths["datastore_root"] / "state/account-gameplan/config.json")):
        raise ValueError("Native accounting producer partitions differ from reviewed profiles")
    return config


def _private_values(config):
    """Read existing local scan inputs in memory; never print or transport them."""
    return _installed_private_values(_repository() / ".env")


def _binding_inputs(config):
    return {"profile": Path(config["local_profile"]), "active": Path(config["coordination_active"]),
            "private_environment": _repository() / ".env", **_reviewed_paths(),
            **{name: _repository() / name for name in (
                "tools/native_ownership_exchange.py", "tools/native_ownership_export.py",
                "ml/account_gameplan/migration.py", "ml/account_gameplan/config.py",
                "ml/account_gameplan/snapshot.py", "ml/stock_trader/horizon_ledger.py",
                "ml/stock_trader/contracts.py", "ml/stock_trader/cross_horizon_fallback.py",
                "datafetching/runtime_lock.py")}}


def _folder(config, actor):
    return Path(config["exchange_root"]) / "cutovers" / config["operation_id"] / actor.lower()


def _packet_members(directory, *, pending_reason=None):
    directory = _plain(directory)
    expected = {"holdings.sqlite3", "manifest.json"}
    if not directory.is_dir():
        if pending_reason:
            raise Pending(pending_reason)
        raise ValueError("Native accounting packet directory is missing")
    members = {path.name for path in directory.iterdir()}
    if members - expected:
        raise ValueError("Native accounting packet is invalid or contains unexpected files")
    if members != expected:
        if pending_reason:
            raise Pending(pending_reason)
        raise ValueError("Native accounting packet is incomplete")
    if any(not _plain(directory / name).is_file() for name in expected):
        raise ValueError("Native accounting packet members must be ordinary files")


def _selection(config, actor, account, buffers):
    files = {name: {"sha256": _sha(raw), "bytes": len(raw)} for name, raw in buffers.items()}
    return {"schema_version": VERSION, "operation_id": config["operation_id"],
            "cutover_action_date": config["cutover_action_date"], "actor": actor,
            "producer": ACTORS[actor], "account_fingerprint": account.account_fingerprint,
            "symbols": list(account.participants[ACTORS[actor]]), "files": files}


def _prepared_packet(config, account, directory, *, verified_source, forbidden_values):
    # Read exactly once after semantic validation and compare to those verified
    # pins. Only these scanned immutable buffers may leave private local staging.
    _packet_members(directory)
    buffers = {name: _read(directory / name, MAX_JSON_BYTES if name == "manifest.json" else MAX_BYTES)
               for name in ("holdings.sqlite3", "manifest.json")}
    if (_sha(buffers["holdings.sqlite3"]) != verified_source["pins"]["database_sha256"]
            or _sha(buffers["manifest.json"]) != verified_source["manifest_sha256"]):
        raise ValueError("Verified native export changed before transport")
    _packet_members(directory)
    for value in buffers.values():
        _private_scan(value, forbidden_values)
    selected = _selection(config, config["actor"], account, buffers)
    raw = _bytes(selected)
    _private_scan(raw, forbidden_values)
    digest = _sha(raw)
    return selected, raw, digest, buffers


def _select_local(config, account, directory, local, *, verified_source, forbidden_values, guard=None):
    selected, raw, digest, _ = _prepared_packet(config, account, directory,
        verified_source=verified_source, forbidden_values=forbidden_values)
    _write(local / (config["actor"].lower() + "-selection.json"), raw, guard=guard)
    return selected, digest


def _publish(config, account, directory, *, verified_source, forbidden_values, guard=None):
    if config["actor"] != "Scout":
        raise ValueError("Atlas native ownership stays local; only Scout may publish its approved packet")
    selected, raw, digest, buffers = _prepared_packet(config, account, directory,
        verified_source=verified_source, forbidden_values=forbidden_values)
    folder = _folder(config, config["actor"])
    packet = folder / "packets" / digest
    for name, value in buffers.items():
        _write(packet / name, value, guard=guard)
    _packet_members(packet)
    if any(_read(packet / name) != value for name, value in buffers.items()):
        raise ValueError("Published native accounting packet differs from verified bytes")
    _write(folder / "selection.json", raw, guard=guard)
    if guard is not None:
        guard()
    _packet_members(packet)
    if (_read(folder / "selection.json") != raw
            or any(_read(packet / name) != value for name, value in buffers.items())):
        raise ValueError("Native accounting publication changed during verification")
    return selected, digest


def _receive(config, actor, account, local, forbidden_values=(), guard=None):
    folder = _folder(config, actor)
    try:
        raw = _read(folder / "selection.json", 128 * 1024)
    except FileNotFoundError:
        raise Pending("NATIVE_" + actor.upper() + "_LEDGER_EXPORT") from None
    selected = _json(raw)
    if (set(selected) != {"schema_version", "operation_id", "cutover_action_date", "actor", "producer",
                          "account_fingerprint", "symbols", "files"}
            or selected["schema_version"] != VERSION or selected["operation_id"] != config["operation_id"]
            or selected["cutover_action_date"] != config["cutover_action_date"] or selected["actor"] != actor
            or selected["producer"] != ACTORS[actor] or selected["account_fingerprint"] != account.account_fingerprint
            or selected["symbols"] != list(account.participants[ACTORS[actor]])
            or set(selected["files"]) != {"holdings.sqlite3", "manifest.json"}):
        raise ValueError("Native accounting selection identity differs")
    digest = _sha(raw)
    packet = folder / "packets" / digest
    incomplete = "NATIVE_" + actor.upper() + "_PACKET_INCOMPLETE"
    _packet_members(packet, pending_reason=incomplete)
    files = {}
    for name, pin in selected["files"].items():
        limit = MAX_JSON_BYTES if name == "manifest.json" else MAX_BYTES
        if (set(pin) != {"sha256", "bytes"} or type(pin["bytes"]) is not int or not 0 < pin["bytes"] <= limit
                or not re.fullmatch(r"[0-9a-f]{64}", str(pin["sha256"]))):
            raise ValueError("Invalid native accounting file pin")
        try:
            value = _read(packet / name, limit)
        except FileNotFoundError:
            raise Pending("NATIVE_" + actor.upper() + "_PACKET_INCOMPLETE") from None
        if len(value) < pin["bytes"]:
            raise Pending("NATIVE_" + actor.upper() + "_PACKET_INCOMPLETE")
        if len(value) != pin["bytes"] or _sha(value) != pin["sha256"]:
            raise ValueError("Native accounting packet changed")
        files[name] = value
    _packet_members(packet, pending_reason=incomplete)
    if _read(folder / "selection.json", 128 * 1024) != raw:
        raise ValueError("Native accounting remote selection changed during receipt")
    cache = local / "received" / actor.lower() / digest
    for name, value in files.items():
        _write(cache / name, value, guard=guard)
    source = read_export(cache, expected_producer=ACTORS[actor],
                         expected_symbols=account.participants[ACTORS[actor]],
                         expected_account_fingerprint=account.account_fingerprint,
                         expected_manifest_sha256=selected["files"]["manifest.json"]["sha256"],
                         forbidden_values=forbidden_values)
    _packet_members(packet, pending_reason=incomplete)
    if (_read(folder / "selection.json", 128 * 1024) != raw
            or any(_read(packet / name) != value for name, value in files.items())):
        raise ValueError("Native accounting remote packet changed during receipt")
    _write(local / (actor.lower() + "-selection.json"), raw, guard=guard)
    return source, digest


def _record_failure(local, result, exc, clock):
    # Exact diagnostic text stays in an ignored local receipt. Stdout and
    # transport contain only the fixed failure classification.
    failure = {"schema_version": VERSION, "operation_id": result["operation_id"],
               "error_type": type(exc).__name__, "detail": str(exc)}
    raw = _bytes(failure)
    receipt = local / "failures" / (_sha(raw) + ".json")
    _write(receipt, raw)
    failed = {**result, "status": "FAILED", "reason": "NATIVE_ACCOUNTING_VALIDATION_FAILED",
              "failure_receipt": str(receipt)}
    _write(local / "status.json", _bytes({**failed, "observed_at": clock().isoformat()}), replace=True)


def run(config, *, clock=None, config_path=None):
    """Run a previously loaded local specification; no operating activation."""
    config = deepcopy(config)
    clock = clock or (lambda: datetime.now(timezone.utc))
    if (config.get("actor") not in ACTORS or config.get("private_native_ledger_exchange_authorized") is not True):
        raise ValueError("Native accounting exchange is not locally authorized")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,95}", str(config.get("operation_id"))):
        raise ValueError("Stable native cutover operation identity required")
    root = _absolute(config["datastore_root"])
    local = _plain(_absolute(config["state_root"]) / config["operation_id"])
    result = {"schema_version": VERSION, "operation_id": config["operation_id"],
              "actor": config["actor"], "cutover_action_date": config["cutover_action_date"],
              "orders_placed": 0, "broker_calls": 0, "activation_changed": False,
              "manual_start_required": True, "execution_authorized": False}
    local.mkdir(parents=True, exist_ok=True)
    operation = FileLock(str(local / "operation.lock"))
    try:
        operation.acquire(timeout=0)
    except Timeout:
        # A losing wake must never overwrite the current owner's durable result.
        return {**result, "status": "PENDING", "reason": "NATIVE_ACCOUNTING_OPERATION_LOCKED"}
    try:
        try:
            account_path = root / "state/account-gameplan/config.json"
            account_bytes = _read(account_path)
            account = load_account_config(root)
            if account is None or account.machine_id != ACTORS[config["actor"]]:
                raise ValueError("Native account configuration missing")
            inputs = _binding_inputs(config)
            if config_path is not None:
                inputs["operation_config"] = _plain(config_path)
                if _json(_read(config_path)) != config:
                    raise ValueError("Native accounting operation configuration changed")
            frozen = {name: _sha(_read(path)) for name, path in inputs.items()}

            def unchanged():
                if _read(account_path) != account_bytes or any(
                        _sha(_read(path)) != frozen[name] for name, path in inputs.items()):
                    raise ValueError("Frozen native accounting bindings changed during this operation")

            unchanged()
            binding = {"config": config, "account_binding_sha256": account.fingerprint,
                       "frozen_inputs_sha256": frozen}
            _write(local / "binding.json", _bytes(binding), guard=unchanged)
            if account.activation["status"] == "ACTIVE":
                verify_cutover(root, account)
                unchanged()
                result.update(status="ACTIVE_CUTOVER_RECEIPT_VERIFIED")
            else:
                forbidden_values = _private_values(config)
                unchanged()
                ledger = root / "state/independent-stock-trader/holdings.sqlite3"
                exported = local / "export"
                # Preserve native exclusion through exact-byte validation and
                # publication, preventing a new writer between the source check
                # and the outgoing selection. Peer waiting never holds these locks.
                with ExitStack() as stack:
                    for name in ("independent-stock-session.lock", "stock-trader-hourly.lock"):
                        stack.enter_context(exclusive_runtime_lock(root / "locks" / name,
                                                                  process_name="native-ownership-export"))
                    stack.enter_context(FileLock(str(root / "state/account-gameplan/preparation.lock"), timeout=0))
                    unchanged()
                    if not exported.exists():
                        export_ownership(source=ledger, pins=pin_ledger_files(ledger),
                                         account_fingerprint=account.account_fingerprint, producer=account.machine_id,
                                         symbols=account.participants[account.machine_id], output_directory=exported,
                                         backup_directory=local / "original-backup", clock=clock,
                                         forbidden_values=forbidden_values)
                    manifest_pin = _sha(_read(exported / "manifest.json"))
                    exported_source = read_export(exported, expected_producer=account.machine_id,
                                expected_symbols=account.participants[account.machine_id],
                                expected_account_fingerprint=account.account_fingerprint,
                                expected_manifest_sha256=manifest_pin, forbidden_values=forbidden_values)

                    def unchanged_source():
                        unchanged()
                        if pin_ledger_files(ledger) != exported_source["source_pins"]:
                            raise ValueError("Native source ledger changed after export; a new reviewed selection is required")

                    unchanged_source()
                    if config["actor"] == "Scout":
                        _, own_packet = _publish(config, account, exported, verified_source=exported_source,
                                                 forbidden_values=forbidden_values, guard=unchanged_source)
                    else:
                        _, own_packet = _select_local(config, account, exported, local, verified_source=exported_source,
                                                      forbidden_values=forbidden_values, guard=unchanged_source)
                    unchanged_source()
                if config["actor"] == "Scout":
                    result.update(status="NATIVE_LEDGER_EXPORTED", ledger_manifest_sha256=manifest_pin)
                else:
                    sources = [{key: exported_source[key] for key in ("producer", "path", "symbols", "pins")}]
                    packet_ids = {"Atlas": own_packet}
                    for actor in ("Scout",):
                        source, packet = _receive(config, actor, account, local, forbidden_values, guard=unchanged)
                        sources.append({key: source[key] for key in ("producer", "path", "symbols", "pins")})
                        packet_ids[actor] = packet
                    candidate = local / "migration-candidate"
                    unchanged()
                    if not candidate.exists():
                        consolidate_ownership_ledgers(sources=sources, destination_directory=candidate,
                            expected_account_fingerprint=account.account_fingerprint,
                            observed_at=clock().isoformat(), cutover_action_date=config["cutover_action_date"])
                        pins = {p.relative_to(candidate).as_posix(): _file_sha(p)
                                for p in candidate.rglob("*") if p.is_file()}
                        _write(local / "migration-review.json", _bytes({"packets": packet_ids, "files": pins}), guard=unchanged)
                    review = _json(_read(local / "migration-review.json"))
                    if (review["packets"] != packet_ids or review["files"] != {
                            p.relative_to(candidate).as_posix(): _file_sha(p) for p in candidate.rglob("*") if p.is_file()}):
                        raise ValueError("Staged native migration changed")
                    result.update(status="CUTOVER_RECONCILIATION_REQUIRED", reason="STAGED_UNION_IS_NOT_ACTIVE",
                                  migration_candidate=str(candidate))
                unchanged()
        except Pending as exc:
            result.update(status="PENDING", reason=str(exc))
        except Timeout:
            result.update(status="PENDING", reason="NATIVE_ACCOUNTING_WRITER_LOCKED")
        except Exception as exc:
            _record_failure(local, result, exc, clock)
            raise
        try:
            unchanged()
            _write(local / "status.json", _bytes({**result, "observed_at": clock().isoformat()}), replace=True, guard=unchanged)
        except Exception as exc:
            _record_failure(local, result, exc, clock)
            raise
        return result
    finally:
        operation.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Verify configuration only; no export or writes")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        value = {"status": "CONFIGURATION_VERIFIED", "actor": config["actor"]} if args.check else run(config, config_path=args.config)
        code = 0
    except Exception as exc:
        value, code = {"status": "FAILED", "reason": "NATIVE_ACCOUNTING_VALIDATION_FAILED", "error_type": type(exc).__name__, "orders_placed": 0, "broker_calls": 0,
                       "activation_changed": False}, 1
    print(json.dumps(value, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
