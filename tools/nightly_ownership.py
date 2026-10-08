"""Fresh private ownership observations from existing native ledgers, read-only.

Held quantities are saved-baseline expectations, not fresh broker observations.
The coordinator must prove them against current account holdings before planning.
No database, raw native identity, local path or account number is exported.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from tempfile import TemporaryDirectory
from time import monotonic

import pandas as pd

from ml import nightly_workflow
from ml.artifacts import file_checksum, utc_timestamp
from ml.gameplan_trade_snapshot import _ownership
from ml.joint_capital_handoff import _object, _read
from ml.stock_trader.contracts import canonical_sha256
from tools.nightly_account_snapshot import _hash, _number, _path, _stamp

VERSION = "nightly-ownership-observation-v1"
FIELDS = {"schema_version", "actor", "held_basis", "ledger_observed_at", "saved_baseline_at", "envelope"}
ENVELOPE_FIELDS = {"producer", "source_fingerprint", "account_fingerprint", "observed_at", "symbols", "held_shares", "ownership"}
OWNERSHIP_FIELDS = {"status", "safe_for_planning", "account_matches", "active_allocations", "blocked_symbols",
    "owned_shares", "reason_codes", "last_saved_reconciliation_at", "last_saved_reconciliation_ready",
    "current_broker_reconciliation_performed"}
ALLOCATION_FIELDS = {"allocation_id_sha256", "symbol", "horizon", "status", "owned_shares", "reserved_buy_shares",
                     "reserved_sell_shares", "target_start", "target_end"}
LEDGER = Path("state/independent-stock-trader/holdings.sqlite3")


def _partitions(config):
    owners = config.get("owners")
    if not isinstance(owners, dict) or set(owners) != {"atlas", "scout"}:
        raise ValueError("Ownership requires exactly both producer partitions")
    for values in owners.values():
        if (not isinstance(values, list) or len(values) != 11
                or any(not isinstance(v, str) or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", v) is None for v in values)
                or len(set(values)) != 11):
            raise ValueError("Ownership requires eleven explicit unique symbols per producer")
    if set(owners["atlas"]) & set(owners["scout"]):
        raise ValueError("Ownership producer partitions overlap")
    return owners


def _fingerprint(record):
    return canonical_sha256({key: value for key, value in record.items() if key != "source_fingerprint"})


def validate_observation(config, value, *, observed_at, actor=None):
    """Validate a fresh transport observation without acquiring any account data."""
    owners, scope, now = _partitions(config), _hash(config.get("account_scope_sha256")), _stamp(observed_at)
    if (not isinstance(value, dict) or set(value) != FIELDS or value["schema_version"] != VERSION
            or value["actor"] not in {"Atlas", "Scout"} or (actor and value["actor"].lower() != actor.lower())
            or value["held_basis"] != "SAVED_READY_RECONCILIATION"):
        raise ValueError("Unrecognized ownership observation or producer")
    stamp, baseline = _stamp(value["ledger_observed_at"]), _stamp(value["saved_baseline_at"])
    if not 0 <= (now - stamp).total_seconds() <= 60 or baseline > stamp:
        raise ValueError("Ownership observation is stale, future or has an invalid baseline")
    record = value["envelope"]
    producer = value["actor"].lower()
    symbols = owners[producer]
    if (not isinstance(record, dict) or set(record) != ENVELOPE_FIELDS or record["producer"] != producer
            or record["account_fingerprint"] != scope or record["symbols"] != symbols
            or _stamp(record["observed_at"]) != stamp or _hash(record["source_fingerprint"]) != _fingerprint(record)):
        raise ValueError("Ownership observation content or account binding differs")
    held, ownership = record["held_shares"], record["ownership"]
    if not isinstance(held, dict) or set(held) != set(symbols):
        raise ValueError("Saved ownership holdings coverage is incomplete")
    for quantity in held.values():
        _number(quantity)
    if (not isinstance(ownership, dict) or set(ownership) != OWNERSHIP_FIELDS
            or ownership["status"] != "OBSERVED_CONSISTENT" or ownership["safe_for_planning"] is not True
            or ownership["account_matches"] is not True or ownership["blocked_symbols"] != []
            or ownership["reason_codes"] != [] or ownership["last_saved_reconciliation_ready"] is not True
            or ownership["current_broker_reconciliation_performed"] is not False
            or _stamp(ownership["last_saved_reconciliation_at"]) != baseline):
        raise ValueError("Ownership observation is blocked, unready or ambiguously sourced")
    allocations, owned = ownership["active_allocations"], ownership["owned_shares"]
    if not isinstance(allocations, list) or not isinstance(owned, dict) or set(owned) != set(symbols):
        raise ValueError("Ownership allocation coverage is incomplete")
    totals, identities, routes = dict.fromkeys(symbols, 0), set(), set()
    for item in allocations:
        if not isinstance(item, dict) or set(item) != ALLOCATION_FIELDS:
            raise ValueError("Ownership allocation contains unsupported private fields")
        identity, symbol, horizon = _hash(item["allocation_id_sha256"]), item["symbol"], item["horizon"]
        if (symbol not in totals or horizon not in {"1h", "4h", "1d", "1w"} or item["status"] != "ACTIVE"
                or identity in identities or (symbol, horizon) in routes
                or _stamp(item["target_end"]) <= _stamp(item["target_start"])):
            raise ValueError("Ownership allocation identity or targets differ")
        quantity = _number(item["owned_shares"], whole=True)
        if any(_number(item[name], whole=True) != 0 for name in ("reserved_buy_shares", "reserved_sell_shares")):
            raise ValueError("Unresolved ownership reservations prevent planning")
        totals[symbol] += quantity
        identities.add(identity); routes.add((symbol, horizon))
    if any(_number(owned[symbol], whole=True) != totals[symbol] or totals[symbol] > held[symbol] for symbol in symbols):
        raise ValueError("Ownership totals disagree with saved baseline holdings")
    return deepcopy(record)


def validate_observations(config, observations, *, observed_at):
    if not isinstance(observations, (list, tuple)) or len(observations) != 2:
        raise ValueError("Both fresh producer ownership observations are required")
    records = [validate_observation(config, value, observed_at=observed_at) for value in observations]
    if {record["producer"] for record in records} != {"atlas", "scout"}:
        raise ValueError("Both distinct ownership producers are required")
    return records


def _pins(path):
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), *path.parents):
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Ownership ledger paths cannot be links or reparse points")
    if not path.is_file() or Path(str(path) + "-journal").exists():
        raise ValueError("Existing native ownership ledger is unavailable")
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    if wal.exists() and not shm.is_file():
        raise ValueError("Ownership WAL requires its existing shared-memory sidecar")
    return {suffix: file_checksum(Path(str(path) + suffix)) if Path(str(path) + suffix).is_file() else None
            for suffix in ("", "-wal", "-shm")}


def capture_ownership(config, now=None):
    """Read a producer's real ledger; never manufacture missing ownership."""
    config = deepcopy(config)
    actor = config.get("actor")
    if actor not in {"Atlas", "Scout"} or config.get("private_exchange_authorized") is not True:
        raise ValueError("Ownership export requires an explicitly authorized local producer")
    owners = _partitions(config)
    scope = _hash(config.get("account_scope_sha256"))
    repository = Path(__file__).resolve().parents[1]
    paths = {name: _path(config.get(name)) for name in ("workflow_config", "local_profile", "coordination_active")}
    frozen = {path: _read(path, path.parent) for path in paths.values()}
    workflow = nightly_workflow.load_config(paths["workflow_config"])
    profile = _object(frozen[paths["local_profile"]])
    symbols = owners[actor.lower()]
    if (workflow["actor"] != actor or _path(workflow["repository"]) != repository
            or paths["local_profile"] != repository / "scratch/cross-pc/local-profile.json"
            or paths["coordination_active"] != repository / "scratch/cross-pc/active.json"
            or _path(workflow["local_profile"]) != paths["local_profile"]
            or _path(workflow["coordination_active"]) != paths["coordination_active"]
            or profile.get("actor") != actor or profile.get("symbols") != symbols
            or profile.get("machine") != ("pc-original" if actor == "Atlas" else "pc-new")
            or profile.get("contract_version") != "cross-pc-v2" or _path(profile.get("checkout")) != repository):
        raise ValueError("Ownership must use this producer's installed local bindings")
    nightly_workflow.verify_installation(workflow)
    source = nightly_workflow.source_identity(repository)
    exporter_hash = file_checksum(Path(__file__))
    root = _path(workflow["datastore"])
    path = root / LEDGER
    pins = _pins(path)
    started, timer = utc_timestamp(now), monotonic()
    try:
        # SQLite may update read marks in SHM even for mode=ro. Open only this
        # private local copy; the source database/WAL/SHM never enter SQLite.
        with TemporaryDirectory(prefix="ownership-read-", dir=repository / "scratch") as temporary:
            copied_root = Path(temporary)
            copied = copied_root / LEDGER
            copied.parent.mkdir(parents=True)
            for suffix, expected in pins.items():
                if expected is not None:
                    target = Path(str(copied) + suffix)
                    shutil.copyfile(Path(str(path) + suffix), target)
                    if file_checksum(target) != expected:
                        raise ValueError("Native ownership ledger changed during local snapshot copy")
            if _pins(path) != pins:
                raise ValueError("Native ownership ledger changed during observation")
            with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("BEGIN")
                latest = connection.execute("SELECT observed_at,ready,payload FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()
                if latest is None or latest[1] != 1:
                    raise ValueError("No ready saved ownership baseline exists")
                held = json.loads(latest[2])["held_shares"]
                if not isinstance(held, dict) or set(held) != set(symbols):
                    raise ValueError("Saved ownership baseline does not cover the exact producer universe")
                ownership = _ownership(copied_root, symbols, scope, held, started.isoformat())
        if _pins(path) != pins:
            raise ValueError("Native ownership ledger changed during observation")
    except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("Native ownership evidence could not be read coherently") from None
    completed = started + pd.Timedelta(seconds=max(0, monotonic() - timer))
    record = {"producer": actor.lower(), "account_fingerprint": scope, "observed_at": started.isoformat(),
              "symbols": symbols, "held_shares": held, "ownership": ownership}
    record["source_fingerprint"] = _fingerprint(record)
    value = {"schema_version": VERSION, "actor": actor, "held_basis": "SAVED_READY_RECONCILIATION",
             "ledger_observed_at": started.isoformat(), "saved_baseline_at": latest[0], "envelope": record}
    validate_observation(config, value, observed_at=completed)
    for name, original in frozen.items():
        if _read(name, name.parent) != original:
            raise ValueError("Local ownership bindings changed during capture")
    if (nightly_workflow.load_config(paths["workflow_config"]) != workflow
            or nightly_workflow.source_identity(repository) != source or file_checksum(Path(__file__)) != exporter_hash
            or _pins(path) != pins):
        raise ValueError("Ownership source or native ledger changed during capture")
    nightly_workflow.verify_installation(workflow)
    json.dumps(value, allow_nan=False)
    return value
