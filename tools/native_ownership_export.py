"""Explicit private native ownership export; no broker calls or activation.

The caller owns the existing native exclusion locks and reviews the source pins.
Unknown evidence is refused, never silently removed. Raw DB/WAL originals stay
in a separate local backup; only a fresh database of validated logical rows is
eligible for the human-authorized private CODEXSTORE ownership exchange.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import uuid

from ml.account_gameplan import migration
from ml.stock_trader.contracts import canonical_sha256, utc
from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy
from ml.stock_trader.horizon_ledger import HorizonLedger

VERSION = "private-native-ownership-export-v1"
MAX_BYTES = 512 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_JSON_BYTES = 256 * 1024
_HORIZONS = {"1h", "4h", "1d", "1w"}
_PORTFOLIO = set("snapshot_id account_fingerprint observed_at held_shares prices symbol_budgets source_fingerprint".split())
_REQUESTS = {
    "entry": (set("kind symbol horizon forecast start end quantity price snapshot batch".split()), {"allow_accumulation"}),
    "exit": (set("kind allocation quantity price snapshot batch exit_lead_seconds".split()), set()),
    "direction-exit": (set("kind symbol horizon forecast start end quantity price snapshot batch pending_sell_shares".split()), set()),
    "fallback-direction-exit": (set("kind symbol forecast trigger_forecast trigger_horizon start end action_date policy source_fingerprint quantity price snapshot batch donor_allocation_id pending_sell_shares pending_buy_shares excluded_donor_horizons reserved_donor_shares baseline_id fallback_policy_version slot_quota owner_horizon owner_forecast".split()), set()),
}
_EVIDENCE = {
    "submission": set("reservation_id status observed_at broker_order_id".split()),
    "order": set("evidence_id reservation_id account_fingerprint observed_at broker_order_id status order_quantity cumulative_filled_quantity remaining_quantity fills broker_status".split()),
    "cancellation-reservation": set("reservation_id broker_order_id requested_at".split()),
    "gameplan-existing-stock-assignment": set("allocation_id snapshot_id quantity observed_at reason".split()),
    "gameplan-unsold-opening-stock-release": set("assignment_id reservation_id snapshot_id quantity observed_at prior_owned_shares_consumed_first assigned_shares_filled reason".split()),
}
_REASONS = {
    "gameplan-existing-stock-assignment": "User-selected Gameplan directional sale of existing stock",
    "gameplan-unsold-opening-stock-release": "Definitively unsold opening inventory returns to the unallocated pool",
}
_OPTIONAL_SCHEMA = {
    "inventory_assignments": "id TEXT PRIMARY KEY, allocation TEXT NOT NULL REFERENCES allocations(id), snapshot_id TEXT NOT NULL REFERENCES snapshots(id), quantity INTEGER NOT NULL, observed_at TEXT NOT NULL",
    "inventory_assignment_releases": "id TEXT PRIMARY KEY, assignment TEXT NOT NULL REFERENCES inventory_assignments(id), reservation TEXT NOT NULL REFERENCES reservations(id), snapshot_id TEXT NOT NULL REFERENCES snapshots(id), quantity INTEGER NOT NULL, observed_at TEXT NOT NULL",
}


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _object(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_JSON_BYTES:
        raise ValueError("Native JSON exceeds the reviewed bound")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate native evidence key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite native JSON")))


def _private_scan(raw, forbidden_values):
    if forbidden_values is None or not isinstance(forbidden_values, (list, tuple)):
        raise ValueError("Explicit local private-value scan input required")
    if len(forbidden_values) > 4096 or any(not isinstance(value, bytes) or not value or len(value) > 65536 for value in forbidden_values):
        raise ValueError("Invalid bounded local private-value scan input")
    if any(value in raw for value in forbidden_values):
        raise ValueError("Private value detected in native export")


def _scan_tables(tables, forbidden_values):
    """Scan original text and decoded JSON strings, including escaped values."""
    def visit(value):
        if isinstance(value, str):
            _private_scan(value.encode("utf-8"), forbidden_values)
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    visit(tables)
    for rows in tables.values():
        for row in rows:
            for column in {"payload", "request", "policy", "owned", "reasons"} & set(row):
                visit(_object(row[column]))


def _keys(value, expected, optional=()):
    if not isinstance(value, dict) or not expected <= set(value) or set(value) - expected - set(optional):
        raise ValueError("Unsupported private or unknown native evidence fields")


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Native account/source identity must be a SHA-256 fingerprint")


def _identity(value, *, nullable=False):
    if value is None and nullable:
        return
    if (not isinstance(value, str) or (re.fullmatch(r"[A-Za-z0-9_.:+-]{1,256}", value) is None
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}:[A-Z][A-Z0-9.-]{0,14}:(?:1h|4h|1d|1w)@(?:[0-9]{2}:[0-9]{2}|[DT]\+[0-9]+)", value) is None)
            or re.search(r"(?i)(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_|AKIA)", value)):
        raise ValueError("Unsupported private or malformed native identity")


def _number(value):
    migration._quantity(value)
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is None:
        raise ValueError("Invalid native numeric text")


def _time(value):
    if not isinstance(value, str) or migration._allocation_timestamp(value) is None:
        raise ValueError("Invalid native timestamp")


def _mapping(value, keys):
    if not isinstance(value, dict) or not set(value) <= set(keys):
        raise ValueError("Native symbol/horizon map differs")
    for number in value.values():
        _number(number)


def _portfolio(value, account, symbols):
    _keys(value, _PORTFOLIO)
    if value["account_fingerprint"] != account:
        raise ValueError("Native portfolio account differs")
    _hash(value["source_fingerprint"])
    _identity(value["snapshot_id"])
    _time(value["observed_at"])
    for name in ("held_shares", "prices", "symbol_budgets"):
        _mapping(value[name], symbols)


def _baseline(value, account, symbols):
    _keys(value, set("account_fingerprint action_date baseline_id donors observed_at policy snapshot_id source_fingerprint symbols".split()))
    if value["account_fingerprint"] != account:
        raise ValueError("Native fallback account differs")
    validate_fallback_policy(value["policy"], value["action_date"])
    _hash(value["source_fingerprint"])
    _hash(value["baseline_id"])
    _identity(value["snapshot_id"])
    _time(value["observed_at"])
    if not isinstance(value["donors"], dict) or not isinstance(value["symbols"], dict) or not set(value["symbols"]) <= set(symbols):
        raise ValueError("Native fallback ownership differs")
    for identity, donor in value["donors"].items():
        _hash(identity)
        _keys(donor, set("allocation_id symbol horizon owner_forecast_id initial_owned_shares daily_cap".split()))
        if donor["allocation_id"] != identity or donor["symbol"] not in symbols or donor["horizon"] not in _HORIZONS:
            raise ValueError("Native fallback donor differs")
        _identity(donor["owner_forecast_id"])
        _number(donor["initial_owned_shares"])
        _number(donor["daily_cap"])
    for item in value["symbols"].values():
        _keys(item, {"initial_owned_longer_horizon_shares", "daily_cap"})
        for number in item.values():
            _number(number)


def _request(value, symbols):
    if not isinstance(value, dict) or value.get("kind") not in _REQUESTS:
        raise ValueError("Unsupported native request kind")
    required, optional = _REQUESTS[value["kind"]]
    _keys(value, required, optional)
    for key, item in value.items():
        if key == "kind":
            continue
        if key == "symbol":
            if item not in symbols:
                raise ValueError("Native request symbol differs")
        elif key in {"horizon", "trigger_horizon", "owner_horizon"}:
            if item not in _HORIZONS:
                raise ValueError("Native request horizon differs")
        elif key in {"start", "end"}:
            _time(item)
        elif key in {"quantity", "price", "pending_sell_shares", "pending_buy_shares", "slot_quota", "exit_lead_seconds"}:
            _number(item)
        elif key == "allow_accumulation":
            if item is not True:
                raise ValueError("Invalid native accumulation flag")
        elif key == "policy":
            validate_fallback_policy(item, value["action_date"])
        elif key == "excluded_donor_horizons":
            if not isinstance(item, list) or any(h not in _HORIZONS for h in item):
                raise ValueError("Invalid native excluded horizons")
        elif key == "reserved_donor_shares":
            _mapping(item, _HORIZONS)
        else:
            _identity(item)


def _evidence(kind, value, account, symbols):
    if kind == "portfolio":
        return _portfolio(value, account, symbols)
    if kind == "reservation":
        return _request(value, symbols)
    if kind == "fallback-day-baseline":
        return _baseline(value, account, symbols)
    if kind not in _EVIDENCE:
        raise ValueError("Unsupported native evidence kind")
    _keys(value, _EVIDENCE[kind])
    for key, item in value.items():
        if key == "account_fingerprint":
            if item != account:
                raise ValueError("Native order account differs")
        elif key in {"observed_at", "requested_at"}:
            _time(item)
        elif key in {"quantity", "order_quantity", "cumulative_filled_quantity", "remaining_quantity", "assigned_shares_filled"}:
            _number(item)
        elif key == "reason":
            if item != _REASONS[kind]:
                raise ValueError("Unknown native free-text reason")
        elif key == "prior_owned_shares_consumed_first":
            if item is not True:
                raise ValueError("Invalid native release flag")
        elif key == "status":
            if item not in {"RESERVED", "SUBMITTED", "UNKNOWN", "WORKING", "PARTIAL", "FILLED", "CANCELLED", "REJECTED"}:
                raise ValueError("Unsupported native order state")
        elif key == "broker_status":
            if item is not None and (not isinstance(item, str) or re.fullmatch(r"[A-Z_]{1,64}", item) is None):
                raise ValueError("Unsupported native broker status")
        elif key == "fills":
            if not isinstance(item, list) or len(item) > MAX_ROWS:
                raise ValueError("Invalid native fill list")
            for fill in item:
                _keys(fill, {"fill_id", "quantity", "price", "executed_at"})
                _identity(fill["fill_id"])
                _time(fill["executed_at"])
                _number(fill["quantity"])
                _number(fill["price"])
        else:
            _identity(item, nullable=key in {"broker_order_id", "broker_status"})


def _privacy(tables, account, symbols):
    if sum(map(len, tables.values())) > MAX_ROWS:
        raise ValueError("Native ledger exceeds the reviewed row bound")
    for table, rows in tables.items():
        for row in rows:
            for key, value in row.items():
                if key in {"request", "payload", "policy", "reasons", "owned"}:
                    continue
                if key == "account":
                    if value != account:
                        raise ValueError("Native account differs")
                elif key in {"observed_at", "executed_at", "requested_at", "start", "end", "last_evidence_at"}:
                    if value is not None:
                        _time(value)
                elif key in {"quantity", "price", "filled", "ready"}:
                    _number(value)
                else:
                    _identity(value, nullable=key == "broker_order")
            if table == "evidence":
                _evidence(row["kind"], _object(row["payload"]), account, symbols)
            elif table == "reservations":
                _request(_object(row["request"]), symbols)
            elif table == "snapshots":
                _portfolio(_object(row["payload"]), account, symbols)
                reasons = _object(row["reasons"])
                if not isinstance(reasons, list):
                    raise ValueError("Invalid saved reconciliation reasons")
                for reason in reasons:
                    if (not isinstance(reason, str) or re.fullmatch(
                            r"[A-Z][A-Z0-9.-]{0,14}:(?:MISSING_PORTFOLIO_SYMBOL_EVIDENCE|ORDER_AND_ACCOUNT_FILL_STATE_NOT_YET_RECONCILED|UNEXPLAINED_MANUAL_OR_EXTERNAL_SHARE_REDUCTION|BROKER_SHARES_BELOW_HORIZON_INVENTORY|(?:UNRECONCILED_(?:RESERVED|UNKNOWN|SUBMITTED)|STALE_WORKING_ORDER_EVIDENCE):[a-f0-9]{64})", reason) is None):
                        raise ValueError("Unreviewed reconciliation free text")
                _mapping(_object(row["owned"]), symbols)
            elif table == "fallback_days":
                _baseline(_object(row["payload"]), account, symbols)
                validate_fallback_policy(_object(row["policy"]), row["action_date"])


def _group(path):
    result = {}
    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = migration._plain(str(path) + suffix)
        if candidate.exists():
            if not candidate.is_file():
                raise ValueError("Native source is not a regular file")
            total += candidate.stat().st_size
            if total > MAX_BYTES:
                raise ValueError("Native ledger exceeds the reviewed byte bound")
            result[suffix] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        else:
            result[suffix] = None
    return result


def _copy_source(source, directory, pins):
    original = _group(source)
    migration._check_pins(source, pins)
    if original["-journal"] is not None:
        raise ValueError("Rollback journal blocks native export")
    copied = directory / "holdings.sqlite3"
    for suffix, expected in original.items():
        if expected is not None:
            target = Path(str(copied) + suffix)
            with target.open("xb") as destination, Path(str(source) + suffix).open("rb") as handle:
                shutil.copyfileobj(handle, destination)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("Native source changed during private copy")
    if _group(source) != original:
        raise ValueError("Native source changed during private copy")
    return copied, original


def _build(path, tables, account):
    HorizonLedger(path, account)  # This path is a NEW private staging database.
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for name, columns in _OPTIONAL_SCHEMA.items():
            db.execute(f"CREATE TABLE {name} ({columns})")
        db.execute("DELETE FROM metadata")
        # No source SQL/schema is executed. Exact payload text and row order survive.
        order = ["metadata", "allocations", "snapshots", "reservations", "fills", "evidence", "blocks", "cancellations", "fallback_days", "inventory_assignments", "inventory_assignment_releases"]
        for name in order:
            columns = migration._COLUMNS[name].split()
            db.executemany(f"INSERT INTO {name} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           [tuple(row[c] for c in columns) for row in tables[name]])
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Rebuilt native database integrity failed")


def _bounded_rows(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        total = 0
        for name, columns in migration._COLUMNS.items():
            if name not in present:
                continue
            actual = [row[1] for row in db.execute(f"PRAGMA table_info({name})")]
            if actual != columns.split():
                raise ValueError("Native ledger columns differ")
            total += db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            if total > MAX_ROWS:
                raise ValueError("Native ledger exceeds the reviewed row bound")
            for column in set(actual) & {"payload", "request", "policy", "owned", "reasons"}:
                if (db.execute(f"SELECT MAX(length(CAST({column} AS BLOB))) FROM {name}").fetchone()[0] or 0) > MAX_JSON_BYTES:
                    raise ValueError("Native JSON exceeds the reviewed bound")


def export_ownership(*, source, pins, account_fingerprint, producer, symbols,
                     output_directory=None, backup_directory=None, clock=None, forbidden_values=None):
    """Check, or publish exact validated rows while preserving local originals.

    This operation never claims native locks: the caller must retain standard
    writer exclusion through export and record continuity before later cutover.
    Export alone is historical ownership evidence, not fresh reconciliation.
    """
    _hash(account_fingerprint)
    if producer not in {"pc-original", "pc-new"}:
        raise ValueError("Explicit native producer identity required")
    if not Path(source).is_absolute():
        raise ValueError("An absolute native source path is required")
    source = migration._plain(source)
    symbols = tuple(symbols)
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Exact unique producer symbols required")
    observed = (clock or (lambda: datetime.now(timezone.utc)))()
    observed = utc(observed)
    publishing = output_directory is not None
    if publishing != (backup_directory is not None):
        raise ValueError("Export requires a separate explicit local backup directory")
    if publishing:
        _private_scan(b"", forbidden_values)
    stage = None
    if publishing:
        if not Path(output_directory).is_absolute() or not Path(backup_directory).is_absolute():
            raise ValueError("Absolute export and local backup paths required")
        output, backup = migration._plain(output_directory), migration._plain(backup_directory)
        if (output.exists() or backup.exists() or not output.parent.is_dir() or not backup.parent.is_dir()
                or output == backup or output in backup.parents or backup in output.parents
                or source == output or source == backup or output in source.parents or backup in source.parents):
            raise ValueError("New distinct ordinary export and local backup directories required")
        # A mapped/UNC share is not an acceptable raw-original backup location.
        if str(backup).startswith(("\\\\", "//")):
            raise ValueError("Original backups must remain local")
        if os.name == "nt":
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(backup.anchor)) != 3:
                raise ValueError("Original backups require a fixed local drive")
        backup.mkdir()
        private_directory = backup
    else:
        temporary = TemporaryDirectory(prefix="native-ownership-check-")
        private_directory = Path(temporary.name)
    try:
        copied, original = _copy_source(source, private_directory, pins)
        # SQLite may modify SHM read marks. Preserve raw originals and inspect
        # a separate disposable copy, including every existing WAL frame.
        with TemporaryDirectory(prefix="native-ownership-inspect-") as inspection:
            inspected, _ = _copy_source(copied, Path(inspection), pins)
            _bounded_rows(inspected)
            record = migration._read_source({"producer": producer, "path": str(inspected), "symbols": list(symbols), "pins": pins}, account_fingerprint, observed)
        tables = record["tables"]
        _privacy(tables, account_fingerprint, symbols)
        if forbidden_values is not None:
            _scan_tables(tables, forbidden_values)
        summary = {"schema_version": VERSION, "status": "VERIFIED_FOR_PRIVATE_EXPORT",
                   "producer": producer, "symbols": sorted(symbols), "observed_at": observed.isoformat(),
                   "source_pins": pins, "row_counts": {k: len(v) for k, v in tables.items()},
                   "logical_sha256": {k: canonical_sha256(v) for k, v in tables.items()},
                   "native_ids_preserved": True, "account_fingerprint": account_fingerprint,
                   "runtime_activation": False, "broker_calls": 0,
                   "fresh_broker_reconciliation": False}
        if _group(source) != original:
            raise ValueError("Native source changed during validation")
        if not publishing:
            return summary
        stage = output.parent / ("." + output.name + ".building-" + uuid.uuid4().hex)
        stage.mkdir()
        _build(stage / "holdings.sqlite3", tables, account_fingerprint)
        rebuilt_pins = migration.pin_ledger_files(stage / "holdings.sqlite3")
        rebuilt = migration._read_source({"producer": producer, "path": str(stage / "holdings.sqlite3"), "symbols": list(symbols), "pins": rebuilt_pins}, account_fingerprint, observed)
        if rebuilt["tables"] != tables or rebuilt_pins["wal_sha256"] is not None:
            raise ValueError("Native export rows changed during rebuild")
        manifest = {**summary, "status": "EXPORTED", "database": "holdings.sqlite3", "database_sha256": rebuilt_pins["database_sha256"],
                    "privacy_validation": "STRICT_NATIVE_FIELDS_NO_RAW_PAGES_OR_REPLIES",
                    "source_originals_preserved_locally": True}
        raw = _encoded(manifest)
        _private_scan(raw, forbidden_values)
        _private_scan((stage / "holdings.sqlite3").read_bytes(), forbidden_values)
        (stage / "manifest.json").write_bytes(raw)
        (private_directory / "export-receipt.json").write_bytes(_encoded({"schema_version": VERSION, "original_files_sha256": original,
            "export_manifest_sha256": hashlib.sha256(raw).hexdigest()}))
        if _group(source) != original or output.exists():
            raise ValueError("Native source or export destination changed before publication")
        stage.rename(output)
        stage = output
        if _group(source) != original:
            raise ValueError("Native source changed at publication")
        return {**manifest, "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    except BaseException:
        if stage is not None and stage.exists():
            (stage / "INVALID.json").write_bytes(_encoded({"schema_version": VERSION, "status": "INVALID"}))
        raise
    finally:
        if not publishing:
            temporary.cleanup()


def read_export(root, *, expected_producer, expected_symbols,
                expected_account_fingerprint, expected_manifest_sha256, forbidden_values=None):
    """Validate one exact private packet without opening its database in place.

    Byte-identical known-schema reconstruction rejects free pages, raw remnants,
    foreign indexes and other storage not created by the reviewed exporter.
    A SQLite-version storage difference requires review, never blind acceptance.
    """
    _hash(expected_manifest_sha256)
    _hash(expected_account_fingerprint)
    _private_scan(b"", forbidden_values)
    root = migration._plain(root)
    if not root.is_dir() or {p.name for p in root.iterdir()} != {"manifest.json", "holdings.sqlite3"}:
        raise ValueError("Private native export is incomplete or contains unexpected files")
    manifest_path = migration._plain(root / "manifest.json")
    database = migration._plain(root / "holdings.sqlite3")
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("Invalid private native export manifest")
    raw = manifest_path.read_bytes()
    _private_scan(raw, forbidden_values)
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError("Private native export manifest pin differs")
    manifest = _object(raw.decode("utf-8"))
    fields = set("schema_version status producer symbols observed_at source_pins row_counts logical_sha256 native_ids_preserved account_fingerprint runtime_activation broker_calls fresh_broker_reconciliation database database_sha256 privacy_validation source_originals_preserved_locally".split())
    _keys(manifest, fields)
    symbols = list(expected_symbols)
    if (len(symbols) != len(set(symbols)) or not symbols
            or expected_producer not in {"pc-original", "pc-new"}
            or manifest["schema_version"] != VERSION or manifest["status"] != "EXPORTED"
            or manifest["producer"] != expected_producer or manifest["symbols"] != sorted(symbols)
            or manifest["account_fingerprint"] != expected_account_fingerprint
            or manifest["runtime_activation"] is not False or manifest["broker_calls"] != 0
            or manifest["fresh_broker_reconciliation"] is not False
            or manifest["native_ids_preserved"] is not True or manifest["source_originals_preserved_locally"] is not True
            or manifest["database"] != "holdings.sqlite3"
            or manifest["privacy_validation"] != "STRICT_NATIVE_FIELDS_NO_RAW_PAGES_OR_REPLIES"):
        raise ValueError("Private native export binding or authority differs")
    _keys(manifest["source_pins"], {"database_sha256", "wal_sha256"})
    _hash(manifest["source_pins"]["database_sha256"])
    if manifest["source_pins"]["wal_sha256"] is not None:
        _hash(manifest["source_pins"]["wal_sha256"])
    _time(manifest["observed_at"])
    if utc(manifest["observed_at"]) > datetime.now(timezone.utc):
        raise ValueError("Private native export observation is in the future")
    _hash(manifest["database_sha256"])
    original = _group(database)
    _private_scan(database.read_bytes(), forbidden_values)
    if original[""] != manifest["database_sha256"] or any(original[s] is not None for s in ("-wal", "-shm", "-journal")):
        raise ValueError("Private native database pin or sidecars differ")
    pins = {"database_sha256": original[""], "wal_sha256": None}
    with TemporaryDirectory(prefix="native-ownership-receive-") as temporary:
        temporary = Path(temporary)
        copied, _ = _copy_source(database, temporary, pins)
        _bounded_rows(copied)
        record = migration._read_source({"producer": expected_producer, "path": str(copied),
            "symbols": symbols, "pins": pins}, expected_account_fingerprint, utc(manifest["observed_at"]))
        tables = record["tables"]
        _privacy(tables, expected_account_fingerprint, symbols)
        _scan_tables(tables, forbidden_values)
        if ({k: len(v) for k, v in tables.items()} != manifest["row_counts"]
                or {k: canonical_sha256(v) for k, v in tables.items()} != manifest["logical_sha256"]):
            raise ValueError("Private native logical content differs")
        rebuilt = temporary / "known-storage.sqlite3"
        _build(rebuilt, tables, expected_account_fingerprint)
        if hashlib.sha256(rebuilt.read_bytes()).hexdigest() != original[""]:
            raise ValueError("Private native database contains unreviewed storage bytes")
    if ({p.name for p in root.iterdir()} != {"manifest.json", "holdings.sqlite3"}
            or _group(database) != original or manifest_path.read_bytes() != raw):
        raise ValueError("Private native export changed during verification")
    return {"producer": expected_producer, "path": str(database), "symbols": symbols,
            "pins": pins, "observed_at": manifest["observed_at"],
            "manifest_sha256": expected_manifest_sha256, "source_pins": manifest["source_pins"]}


def _installed_private_values(private_env):
    """Use this checkout's verified pinned scanner, returning values only in RAM."""
    import importlib
    import subprocess
    import sys
    import types
    repository = Path(__file__).resolve().parents[1]
    active_path = repository / "scratch/cross-pc/active.json"
    active = _object(active_path.read_text())
    release = migration._plain(active["release_root"])
    result = subprocess.run([sys.executable, "-B", str(release / "tools/cross_pc/cli.py"),
        "verify-installation", "--active", str(active_path)], capture_output=True, timeout=30)
    if result.returncode != 0:
        raise ValueError("Pinned local scanner installation is not verified")
    package = "_native_export_scanner_" + uuid.uuid4().hex
    namespace = types.ModuleType(package)
    namespace.__path__ = [str(release / "tools/cross_pc")]
    sys.modules[package] = namespace
    scanner = importlib.import_module(package + ".artifact_publish")
    # This private-account export also excludes literal account identifiers;
    # the public-source scanner's ordinary credential-only key list is narrower.
    scanner._SENSITIVE_NAME = re.compile(r"KEY|TOKEN|SECRET|PASS|CREDENTIAL|AUTH|PRIVATE|ACCOUNT", re.I)
    env = migration._plain(private_env)
    if env != repository / ".env":
        raise ValueError("Private scan must use this checkout's local environment file")
    return scanner._secret_values(env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--pins-json", required=True)
    parser.add_argument("--account-fingerprint", required=True)
    parser.add_argument("--producer", required=True, choices=("pc-original", "pc-new"))
    parser.add_argument("--symbols", nargs="+", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--export", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--local-backup")
    parser.add_argument("--private-env")
    parser.add_argument("--private-staging-root")
    args = parser.parse_args(argv)
    try:
        if not args.export and (args.output or args.local_backup):
            raise ValueError("Output paths require explicit --export")
        if args.export and (not args.output or not args.local_backup):
            raise ValueError("Export needs both output and local backup paths")
        forbidden_values = None
        if args.export:
            if not args.private_env or not args.private_staging_root:
                raise ValueError("Export requires a local private scan and protected staging root")
            repository = Path(__file__).resolve().parents[1]
            staging = migration._plain(args.private_staging_root)
            if (repository / "scratch").resolve() not in staging.parents:
                raise ValueError("Private staging root must be inside this checkout's ignored scratch directory")
            for value in (args.output, args.local_backup):
                if staging not in migration._plain(value).parents:
                    raise ValueError("Native export output must remain inside its reviewed private staging root")
            forbidden_values = _installed_private_values(args.private_env)
        result = export_ownership(source=args.source, pins=_object(Path(args.pins_json).read_text()),
            account_fingerprint=args.account_fingerprint, producer=args.producer, symbols=args.symbols,
            output_directory=args.output, backup_directory=args.local_backup, forbidden_values=forbidden_values)
        print(json.dumps({key: result[key] for key in ("schema_version", "status", "producer", "runtime_activation", "broker_calls")}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        # Native exception text may contain source/account values. Keep CLI clean.
        print(json.dumps({"status": "BLOCKED", "reason": "NATIVE_EXPORT_VALIDATION_FAILED", "broker_calls": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
