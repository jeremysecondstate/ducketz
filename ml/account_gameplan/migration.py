"""Offline native-ledger consolidation into a new, persistently blocked candidate.

No active path is inferred or replaced. Both original DB/WAL byte pins remain
authoritative. Activation and reconciliation against BOTH producer baselines
are separate reviewed work; this module provides no activation operation.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from ml.stock_trader.contracts import canonical_sha256, utc
from ml.stock_trader.horizon_ledger import HorizonLedger, LEDGER_VERSION, _identity
from ml.stock_trader.state import validated_symbols
from ml.account_gameplan.snapshot import _allocation_timestamp, _quantity


_COLUMNS = {
    "metadata": "key value", "allocations": "id account symbol horizon forecast start end status",
    "reservations": "id allocation side quantity price filled status broker_order idempotency_key batch request last_evidence_at",
    "fills": "id reservation quantity price executed_at", "evidence": "id kind payload",
    "snapshots": "id observed_at payload ready reasons owned", "blocks": "symbol reason",
    "cancellations": "reservation requested_at",
    "fallback_days": "account action_date baseline_id snapshot_id source_fingerprint policy payload",
    "inventory_assignments": "id allocation snapshot_id quantity observed_at",
    "inventory_assignment_releases": "id assignment reservation snapshot_id quantity observed_at",
}
_OPTIONAL = {"fallback_days", "inventory_assignments", "inventory_assignment_releases"}
_BLOCK = "ACCOUNT_GAMEPLAN_MIGRATION_REQUIRES_REVIEWED_ACTIVATION_AND_UNION_RECONCILIATION"
_OPEN = {"RESERVED", "SUBMITTED", "UNKNOWN", "WORKING", "PARTIAL"}


def _plain(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Ledger paths cannot contain symlinks or reparse points")
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def pin_ledger_files(path) -> dict:
    """Read-only byte pins; callers review these before requesting consolidation."""
    path = _plain(path)
    if not path.is_file():
        raise ValueError("Explicit source ledger file is missing")
    wal = _plain(str(path) + "-wal")
    if wal.exists() and not _plain(str(path) + "-shm").is_file():
        raise ValueError("Existing WAL requires its existing shared-memory sidecar")
    if _plain(str(path) + "-journal").exists():
        raise ValueError("Source has a rollback journal; no recovery is authorized")
    return {"database_sha256": _sha(path), "wal_sha256": _sha(wal)}


def _check_pins(path, expected):
    if (not isinstance(expected, dict) or set(expected) != {"database_sha256", "wal_sha256"}
            or re.fullmatch(r"[a-f0-9]{64}", str(expected["database_sha256"])) is None
            or (expected["wal_sha256"] is not None and re.fullmatch(r"[a-f0-9]{64}", str(expected["wal_sha256"])) is None)
            or pin_ledger_files(path) != expected):
        raise ValueError("Source ledger DB/WAL pins changed or are invalid")


def _read_source(source, account, observed):
    if not isinstance(source, dict) or set(source) != {"producer", "path", "symbols", "pins"}:
        raise ValueError("Each source needs explicit producer, path, symbols and reviewed pins")
    symbols = validated_symbols(source["symbols"])
    path = _plain(source["path"])
    _check_pins(path, source["pins"])
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        schema = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        names = {row["name"] for row in schema if row["type"] == "table"}
        if (names - _COLUMNS.keys() or set(_COLUMNS) - _OPTIONAL - names
                or any(row["type"] in {"view", "trigger"} for row in schema)):
            raise ValueError("Unsupported native source ledger schema")
        tables = {}
        for table in _COLUMNS:
            if table not in names:
                tables[table] = []
                continue
            if [row["name"] for row in db.execute(f"PRAGMA table_info({table})")] != _COLUMNS[table].split():
                raise ValueError("Native ledger columns differ: " + table)
            tables[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Source SQLite integrity or references are invalid")
        metadata = {row["key"]: row["value"] for row in tables["metadata"]}
        if len(tables["metadata"]) != 2 or metadata != {"account": account, "version": LEDGER_VERSION}:
            raise ValueError("Source ledger account, version or metadata is unsupported")
        if tables["blocks"] or any(row["status"] in _OPEN for row in tables["reservations"]):
            raise ValueError("Source has blocks or pending/unknown reservations")
        state = HorizonLedger._snapshot(db)  # Query-only native accounting; no constructor on sources.
        _audit_rows(tables, state, account, symbols, observed)
        _check_pins(path, source["pins"])
    _check_pins(path, source["pins"])
    return {"producer": source["producer"], "path": str(path), "symbols": list(symbols),
            "pins": dict(source["pins"]), "tables": tables, "state": state}


def _audit_rows(tables, state, account, symbols, observed):
    for allocation in state.allocations:
        if (allocation.account_fingerprint != account or allocation.symbol not in symbols
                or allocation.horizon not in {"1h", "4h", "1d", "1w"}
                or allocation.status not in {"ACTIVE", "CLOSED"} or allocation.filled_shares < 0
                or allocation.reserved_buy_shares or allocation.reserved_sell_shares
                or (allocation.status == "CLOSED" and allocation.filled_shares)):
            raise ValueError("Native ownership allocation is inconsistent")
        start, end = _allocation_timestamp(allocation.target_start), _allocation_timestamp(allocation.target_end)
        if start is None or end is None or utc(end) <= utc(start):
            raise ValueError("Native allocation has invalid timestamps")
    fills = {}
    for row in tables["fills"]:
        fills[row["reservation"]] = fills.get(row["reservation"], 0) + _quantity(row["quantity"], whole=True)
        if not _quantity(row["quantity"], whole=True) or not _quantity(row["price"]):
            raise ValueError("Invalid native fill quantity or price")
        if _allocation_timestamp(row["executed_at"]) is None or utc(row["executed_at"]) > observed:
            raise ValueError("Invalid or future fill time")
    for row in tables["reservations"]:
        quantity, filled = _quantity(row["quantity"], whole=True), _quantity(row["filled"], whole=True)
        if (row["side"] not in {"BUY", "SELL"} or row["status"] not in {"FILLED", "CANCELLED", "REJECTED"}
                or not quantity or filled > quantity or not _quantity(row["price"])
                or fills.get(row["id"], 0) != filled or (row["status"] == "FILLED" and filled != quantity)
                or (row["status"] == "REJECTED" and filled)):
            raise ValueError("Native reservation or fills are inconsistent")
        if not isinstance(json.loads(row["request"]), dict):
            raise ValueError("Invalid native order request")
    assignments = {row["id"]: row for row in tables["inventory_assignments"]}
    released = {}
    for row in tables["inventory_assignment_releases"]:
        if not _quantity(row["quantity"], whole=True):
            raise ValueError("Invalid native inventory release")
        released[row["assignment"]] = released.get(row["assignment"], 0) + _quantity(row["quantity"], whole=True)
    if any(not _quantity(row["quantity"], whole=True) for row in assignments.values()) or any(
            identity not in assignments or count > _quantity(assignments[identity]["quantity"], whole=True)
            for identity, count in released.items()):
        raise ValueError("Native inventory assignment/release is inconsistent")
    for row in [*assignments.values(), *tables["inventory_assignment_releases"]]:
        if _allocation_timestamp(row["observed_at"]) is None or utc(row["observed_at"]) > observed:
            raise ValueError("Invalid native inventory assignment/release time")
    snapshots = tables["snapshots"]
    if not snapshots:
        raise ValueError("Source needs a saved native reconciliation")
    for row in snapshots:
        stamp = _allocation_timestamp(row["observed_at"])
        payload, owned, reasons = json.loads(row["payload"]), json.loads(row["owned"]), json.loads(row["reasons"])
        if (stamp is None or utc(stamp) > observed or not isinstance(payload, dict)
                or payload.get("account_fingerprint") != account or payload.get("snapshot_id") != row["id"]
                or payload.get("observed_at") != row["observed_at"] or row["ready"] not in {0, 1}
                or not isinstance(reasons, list) or (row["ready"] and reasons)
                or not isinstance(owned, dict) or not set(owned).issubset(symbols)
                or not isinstance(payload.get("held_shares"), dict)
                or not set(payload["held_shares"]).issubset(symbols)):
            raise ValueError("Saved native snapshot identity or symbol coverage is invalid")
        for count in payload["held_shares"].values():
            _quantity(count)
        for symbol, count in owned.items():
            if row["ready"] and _quantity(count, whole=True) > _quantity(payload["held_shares"].get(symbol)):
                raise ValueError("Saved snapshot owned shares exceed observed holdings")
            _quantity(count, whole=True)
    latest = max(snapshots, key=lambda row: row["observed_at"])
    held = json.loads(latest["payload"])["held_shares"]
    if latest["ready"] != 1 or set(held) != set(symbols):
        raise ValueError("Latest source reconciliation is not ready or complete")
    if any(sum(a.filled_shares for a in state.allocations if a.symbol == symbol) > _quantity(held[symbol])
           for symbol in symbols):
        raise ValueError("Current native ownership exceeds its saved broker holdings")
    for row in tables["evidence"]:
        if not isinstance(json.loads(row["payload"]), dict):
            raise ValueError("Native audit evidence must remain structured")
    for row in tables["fallback_days"]:
        payload = json.loads(row["payload"])
        if (row["account"] != account or not isinstance(payload, dict)
                or not isinstance(json.loads(row["policy"]), dict)
                or payload.get("account_fingerprint") != account
                or any(payload.get(key) != row[key] for key in ("action_date", "baseline_id", "snapshot_id", "source_fingerprint"))
                or payload.get("policy") != json.loads(row["policy"])
                or _identity(["fallback-day", {k:v for k,v in payload.items() if k != "baseline_id"}]) != row["baseline_id"]
                or not set(payload.get("symbols", {})).issubset(symbols)):
            raise ValueError("Invalid fallback baseline binding")


def _archive_source(record, directory):
    """Self-contained SQLite snapshot includes WAL rows, with every original ID."""
    source = Path(record["path"])
    _check_pins(source, record["pins"])
    target = directory / (hashlib.sha256(record["producer"].encode()).hexdigest() + ".sqlite3")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as original:
        original.execute("PRAGMA query_only=ON")
        original.execute("PRAGMA trusted_schema=OFF")
        original.execute("BEGIN")
        original.execute("SELECT COUNT(*) FROM metadata").fetchone()  # Pin the read transaction.
        with closing(sqlite3.connect(target)) as archive:
            original.backup(archive)
            archive.row_factory = sqlite3.Row
            if archive.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Original source archive integrity failed")
            present = {row[0] for row in archive.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, expected in record["tables"].items():
                actual = [dict(row) for row in archive.execute(f"SELECT * FROM {table}")] if table in present else []
                if sorted(map(canonical_sha256, actual)) != sorted(map(canonical_sha256, expected)):
                    raise ValueError("Source archive differs from reviewed original rows")
        _check_pins(source, record["pins"])
    _check_pins(source, record["pins"])
    return {"producer": record["producer"], "path": "sources/" + target.name,
            "sha256": _sha(target), "original_pins": record["pins"],
            "snapshot_policy": "SQLITE_READ_TRANSACTION_BACKUP_INCLUDING_WAL",
            "original_rows_verified": True}


def consolidate_ownership_ledgers(*, sources, destination_directory, expected_account_fingerprint,
                                  observed_at, cutover_action_date=None) -> dict:
    """Create a blocked candidate only; original paths and every native ID survive.

    ``sources`` has exactly two mappings: producer/path/symbols/pins. Obtain and
    review DB/WAL pins separately with ``pin_ledger_files``. A successful result
    still requires explicit activation and fresh union reconciliation against
    each producer's saved baseline, not merely the merged latest snapshot.
    With an explicit cutover date strictly after every saved fallback day,
    conflicting historical baselines remain in complete original DB archives;
    they are never relabeled or merged into the active native daily budget.
    """
    account = expected_account_fingerprint
    if re.fullmatch(r"[a-f0-9]{64}", str(account)) is None:
        raise ValueError("Expected account fingerprint is required")
    stamp = _allocation_timestamp(observed_at)
    if stamp is None or not isinstance(sources, (list, tuple)) or len(sources) != 2:
        raise ValueError("Exactly two explicit sources and an aware observation time are required")
    destination = _plain(destination_directory)
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Destination must be a new directory under an existing parent")
    records = [_read_source(source, account, utc(stamp)) for source in sources]
    if (any(not isinstance(r["producer"], str) or not r["producer"] for r in records)
            or len({r["producer"] for r in records}) != 2 or len({r["path"].casefold() for r in records}) != 2
            or set(records[0]["symbols"]) & set(records[1]["symbols"])):
        raise ValueError("Sources must have distinct producers, paths and disjoint symbol ownership")
    if cutover_action_date is not None:
        if not isinstance(cutover_action_date, str) or date.fromisoformat(cutover_action_date).isoformat() != cutover_action_date:
            raise ValueError("Cutover action date must be an exact ISO date")
        if any(date.fromisoformat(row["action_date"]).isoformat() != row["action_date"]
               or row["action_date"] >= cutover_action_date
               for record in records for row in record["tables"]["fallback_days"]):
            raise ValueError("Cutover date must follow every source fallback baseline date")
    symbols = tuple(sorted(s for record in records for s in record["symbols"]))
    merged = {table: [] for table in _COLUMNS}
    historical_collisions = []
    for table in _COLUMNS:
        if table in {"metadata", "blocks"}:
            continue
        keys = set()
        for record in records:
            for row in record["tables"][table]:
                key = (row["account"], row["action_date"]) if table == "fallback_days" else row[_COLUMNS[table].split()[0]]
                if key in keys:
                    if table == "fallback_days" and cutover_action_date is not None:
                        originals = [{"producer": item["producer"], "baseline_id": old["baseline_id"],
                                      "source_fingerprint": old["source_fingerprint"]}
                            for item in records for old in item["tables"][table]
                            if (old["account"], old["action_date"]) == key]
                        if len(originals) != 2 or len({item["producer"] for item in originals}) != 2:
                            raise ValueError("Duplicate fallback key within a native source")
                        historical_collisions.append({"action_date": row["action_date"],
                            "preservation": "EXACT_ORIGINAL_SOURCE_DATABASE_ARCHIVES",
                            "originals": originals})
                        merged[table] = [old for old in merged[table] if (old["account"], old["action_date"]) != key]
                        continue
                    raise ValueError("Native ID or daily-baseline collision in " + table)
                keys.add(key)
                merged[table].append(row)
    # Cross-source rowids cannot retain their old global meaning. Preserve each
    # original snapshot and chronological order; blocks prohibit using a partial
    # latest producer snapshot as a ready combined account baseline.
    merged["snapshots"].sort(key=lambda row: row["observed_at"])
    for record in records:
        _check_pins(Path(record["path"]), record["pins"])
    stage = destination.parent / ("." + destination.name + ".building-" + uuid.uuid4().hex)
    stage.mkdir()
    database = stage / "holdings.sqlite3"
    try:
        (stage / "sources").mkdir()
        archives = [_archive_source(record, stage / "sources") for record in records]
        HorizonLedger(database, account)  # Only this new candidate, never either original.
        with closing(sqlite3.connect(database)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TABLE inventory_assignments (id TEXT PRIMARY KEY, allocation TEXT NOT NULL REFERENCES allocations(id), snapshot_id TEXT NOT NULL REFERENCES snapshots(id), quantity INTEGER NOT NULL, observed_at TEXT NOT NULL)")
            db.execute("CREATE TABLE inventory_assignment_releases (id TEXT PRIMARY KEY, assignment TEXT NOT NULL REFERENCES inventory_assignments(id), reservation TEXT NOT NULL REFERENCES reservations(id), snapshot_id TEXT NOT NULL REFERENCES snapshots(id), quantity INTEGER NOT NULL, observed_at TEXT NOT NULL)")
            for table in ("allocations", "snapshots", "reservations", "fills", "evidence", "cancellations",
                          "fallback_days", "inventory_assignments", "inventory_assignment_releases"):
                columns = _COLUMNS[table].split()
                db.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                               [[row[c] for c in columns] for row in merged[table]])
            db.executemany("INSERT INTO blocks VALUES (?,?)", [(symbol, _BLOCK) for symbol in symbols])
            db.execute("INSERT INTO metadata VALUES ('account_gameplan_migration', 'REQUIRES_REVIEWED_ACTIVATION')")
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Candidate database integrity failed")
            state = HorizonLedger._snapshot(db)
            if {a.allocation_id: asdict(a) for a in state.allocations} != {
                    a.allocation_id: asdict(a) for record in records for a in record["state"].allocations}:
                raise ValueError("Merged native ownership differs from originals")
            if {r.reservation_id: asdict(r) for r in state.reservations} != {
                    r.reservation_id: asdict(r) for record in records for r in record["state"].reservations}:
                raise ValueError("Merged native reservations differ from originals")
            for table, expected in merged.items():
                if table in {"metadata", "blocks"}:
                    continue
                actual = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
                if sorted(map(canonical_sha256, actual)) != sorted(map(canonical_sha256, expected)):
                    raise ValueError("Native rows changed during merge: " + table)
            if dict(state.persistent_blocks) != dict.fromkeys(symbols, _BLOCK):
                raise ValueError("Candidate activation blocks are missing")
            actual_counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in _COLUMNS}
        for record in records:
            _check_pins(Path(record["path"]), record["pins"])
        manifest = {"schema_version": "account-gameplan-ledger-candidate-v1",
            "status": "REQUIRES_REVIEWED_ACTIVATION", "observed_at": stamp,
            "created_at": datetime.now(timezone.utc).isoformat(), "account_fingerprint": account,
            "symbols": list(symbols), "database": "holdings.sqlite3", "database_sha256": _sha(database),
            "cutover_action_date": cutover_action_date, "source_archives": archives,
            "historical_fallback_collisions": historical_collisions,
            "sources": [{key: record[key] for key in ("producer", "path", "symbols", "pins")} | {
                "row_counts": {table: len(rows) for table, rows in record["tables"].items()},
                "latest_snapshot_id": max(record["tables"]["snapshots"], key=lambda row: row["observed_at"])["id"]}
                for record in records], "row_counts": actual_counts,
            "native_accounting_audit": "PASS", "original_native_ids_preserved": True,
            "runtime_activation": False, "broker_calls": 0,
            "activation_requirements": ["Explicit reviewed activation approval", "Both original producers stopped and ownership transition verified",
                "Fresh union broker reconciliation against BOTH saved producer baselines", "Resolve migration blocks only through a reviewed native transition",
                "Execution on or after the reviewed cutover date; never replay historical fallback days",
                "Freeze a new union fallback baseline only through the native first-ready snapshot for that future session"],
            "limitations": ["No pending orders or source blocks supported", "Conflicting native IDs are rejected",
                "Past conflicting fallback days require explicit later cutover and remain only in original source archives",
                "Current/future fallback baseline conflicts are rejected; no historical baseline IDs are rewritten",
                "Original snapshots retained; no synthetic merged ready snapshot", "No deployment or active-path replacement"]}
        with (stage / "manifest.json").open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for record in records:
            _check_pins(Path(record["path"]), record["pins"])
        if destination.exists():
            raise ValueError("Destination appeared during candidate preparation")
        stage.rename(destination)
        return {**manifest, "destination_directory": str(destination),
                "manifest_sha256": _sha(destination / "manifest.json")}
    except BaseException:
        # Preserve this isolated failed build for inspection; never modify an
        # original or promote an unverified candidate to the requested path.
        raise


__all__ = ["pin_ledger_files", "consolidate_ownership_ledgers"]
