"""Reconcile an explicitly pinned migration into a new, non-active candidate.

No broker calls, live paths, configuration changes or order methods belong here.
The caller obtains coherent native evidence under the ordinary ownership locks.
Both producer baselines are checked by the unchanged native reconciliation code.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from ml.account_gameplan import migration
from ml.account_gameplan.snapshot import _quantity
from ml.stock_trader.contracts import PortfolioState, canonical_sha256
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from ml.stock_trader.state import validated_symbols


VERSION = "account-gameplan-union-reconciliation-v1"


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("An exact SHA-256 pin is required")
    return value


def _aware(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("An offset-aware observation time is required")
    return result.astimezone(timezone.utc)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate evidence key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _pinned(path, expected, *, database=False):
    path = migration._plain(path)
    if database and any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Frozen candidate/archive must be a self-contained database")
    raw = path.read_bytes()
    if _sha(raw) != _hash(expected):
        raise ValueError("Pinned migration input changed")
    return raw


def _tables(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        schema = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        names = {row["name"] for row in schema if row["type"] == "table"}
        if names != set(migration._COLUMNS) or any(row["type"] in {"view", "trigger"} for row in schema):
            raise ValueError("Migration candidate schema differs")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Migration database integrity failed")
        result = {}
        for table, columns in migration._COLUMNS.items():
            if [row["name"] for row in db.execute(f"PRAGMA table_info({table})")] != columns.split():
                raise ValueError("Migration database columns differ")
            result[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        return result


def _same_rows(first, second):
    return sorted(map(canonical_sha256, first)) == sorted(map(canonical_sha256, second))


def _fresh(portfolio, clock, cutover_date, *, require_before_opening=True):
    now, observed = _aware(clock()), _aware(portfolio.observed_at)
    if not 0 <= (now - observed).total_seconds() <= 60:
        raise ValueError("Union portfolio is future/stale")
    if require_before_opening:
        opening = datetime.combine(date.fromisoformat(cutover_date), datetime.min.time(),
                                   ZoneInfo("America/Los_Angeles")).replace(hour=4).astimezone(timezone.utc)
        if now >= opening:
            raise ValueError("The cutover opening deadline passed")
    return now


def _portfolio(portfolio, broker, symbols, account, expected_portfolio_sha256, expected_broker_snapshot_sha256):
    if not isinstance(portfolio, PortfolioEvidence) or not isinstance(broker, PortfolioState):
        raise ValueError("Explicit native portfolio and broker snapshot evidence are required")
    if (canonical_sha256(asdict(portfolio)) != _hash(expected_portfolio_sha256)
            or canonical_sha256(asdict(broker)) != _hash(expected_broker_snapshot_sha256)):
        raise ValueError("Native observation content differs from its explicit pin")
    if (portfolio.account_fingerprint != account or not isinstance(portfolio.snapshot_id, str) or not portfolio.snapshot_id.strip()
            or portfolio.source_fingerprint != broker.source_fingerprint
            or _aware(portfolio.observed_at) != _aware(broker.observed_at)
            or not broker.broker_identity_fingerprint):
        raise ValueError("Native observation account, source or clock differs")
    _hash(portfolio.source_fingerprint)
    for values in (portfolio.held_shares, portfolio.prices, portfolio.symbol_budgets,
                   broker.held_shares, broker.quotes):
        if set(values) != symbols:
            raise ValueError("Fresh native observation must cover the exact union")
    if (type(broker.working_order_count) is not int or broker.working_order_count != 0
            or broker.broker_working_orders != ()
            or any(_quantity(value) != 0 for values in (broker.pending_buy_shares, broker.pending_sell_shares)
                   for value in values.values())):
        raise ValueError("Pending or unknown broker orders prevent cutover")
    for symbol in symbols:
        if (_quantity(portfolio.held_shares[symbol]) != _quantity(broker.held_shares[symbol])
                or _quantity(portfolio.prices[symbol]) <= 0
                or _quantity(portfolio.prices[symbol]) != _quantity(broker.quotes[symbol].ask)
                or _quantity(portfolio.symbol_budgets[symbol]) != 0):
            raise ValueError("Portfolio must preserve observed holdings/prices and zero execution budgets")


def _preserved_native_rows(before, after):
    for table in before:
        if table not in {"allocations", "snapshots", "evidence", "blocks"} and not _same_rows(before[table], after[table]):
            raise ValueError("Native accounting rows changed during transition: " + table)
    for table in ("allocations", "snapshots", "evidence"):
        old = {row["id"]: row for row in before[table]}
        new = {row["id"]: row for row in after[table]}
        for identity, row in old.items():
            changed = new.get(identity)
            if changed != row and not (table == "allocations" and row["status"] == "ACTIVE"
                    and changed == {**row, "status": "CLOSED"}):
                raise ValueError("Original native identity/evidence changed: " + table)
        allowed = 0 if table == "allocations" else 1
        if len(new) - len(old) != allowed:
            raise ValueError("Unexpected new native rows: " + table)
    if after["blocks"]:
        raise ValueError("Union transition has persistent blocks")


def _reconcile_migration_candidate(*, candidate_directory, expected_manifest_sha256,
        expected_participants, expected_account_fingerprint, portfolio, expected_portfolio_sha256,
        broker_snapshot, expected_broker_snapshot_sha256, destination_directory, clock=None,
        require_before_opening):
    """Publish verified union readiness in a NEW directory; activate nothing.

    The caller pins a fresh native PortfolioState captured with order identities,
    checks the stable account before/after capture, and supplies its zero-budget
    PortfolioEvidence. The complete observed source is retained, never refreshed
    or fabricated here. All original files and the blocked candidate stay intact.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    candidate, destination = migration._plain(candidate_directory), migration._plain(destination_directory)
    if (not candidate.is_dir() or destination.exists() or not destination.parent.is_dir()
            or candidate == destination or candidate in destination.parents or destination in candidate.parents):
        raise ValueError("A separate new destination under an existing parent is required")
    account = _hash(expected_account_fingerprint)
    if not isinstance(expected_participants, dict) or set(expected_participants) != {"pc-original", "pc-new"}:
        raise ValueError("Both explicitly reviewed producer partitions are required")
    if any(not isinstance(values, (list, tuple)) or not values or any(not isinstance(v, str) for v in values)
           or len(set(values)) != len(values) for values in expected_participants.values()):
        raise ValueError("Producer partitions must contain explicit unique symbols")
    partitions = {producer: sorted(validated_symbols(symbols)) for producer, symbols in expected_participants.items()}
    symbols = set(partitions["pc-original"]) | set(partitions["pc-new"])
    if len(symbols) != sum(map(len, partitions.values())):
        raise ValueError("Producer symbol partitions overlap")
    manifest_raw = _pinned(candidate / "manifest.json", expected_manifest_sha256)
    manifest = _json(manifest_raw)
    if (manifest.get("schema_version") != "account-gameplan-ledger-candidate-v1"
            or manifest.get("status") != "REQUIRES_REVIEWED_ACTIVATION"
            or manifest.get("account_fingerprint") != account or manifest.get("symbols") != sorted(symbols)
            or manifest.get("database") != "holdings.sqlite3" or manifest.get("runtime_activation") is not False
            or manifest.get("native_accounting_audit") != "PASS" or manifest.get("original_native_ids_preserved") is not True):
        raise ValueError("Manifest is not the reviewed blocked union migration")
    cutover = manifest.get("cutover_action_date")
    if not isinstance(cutover, str) or date.fromisoformat(cutover).isoformat() != cutover:
        raise ValueError("An explicit exact migration cutover action date is required")
    _portfolio(portfolio, broker_snapshot, symbols, account, expected_portfolio_sha256, expected_broker_snapshot_sha256)
    def fresh():
        return _fresh(portfolio, clock, cutover, require_before_opening=require_before_opening)
    reconciled_at = fresh()
    database_raw = _pinned(candidate / "holdings.sqlite3", manifest["database_sha256"], database=True)
    sources, archives = manifest.get("sources"), manifest.get("source_archives")
    if (not isinstance(sources, list) or not isinstance(archives, list) or len(sources) != 2 or len(archives) != 2
            or {s.get("producer") for s in sources} != set(partitions)
            or {s.get("producer") for s in archives} != set(partitions)):
        raise ValueError("Both original source bindings and archives are required")
    archive_bytes, originals = {}, {}
    for source in sources:
        producer = source["producer"]
        archive = next(item for item in archives if item["producer"] == producer)
        name = "sources/" + hashlib.sha256(producer.encode()).hexdigest() + ".sqlite3"
        if (sorted(source["symbols"]) != partitions[producer] or len(source["symbols"]) != len(partitions[producer]) or archive.get("path") != name
                or archive.get("original_pins") != source["pins"] or archive.get("original_rows_verified") is not True
                or archive.get("snapshot_policy") != "SQLITE_READ_TRANSACTION_BACKUP_INCLUDING_WAL"):
            raise ValueError("Archived source ownership or pin differs")
        migration._check_pins(Path(source["path"]), source["pins"])
        archive_bytes[producer] = _pinned(candidate / name, archive["sha256"], database=True)
        originals[producer] = source
    # Staging is deliberately retained on failure; it has no runtime pointer.
    stage = destination.parent / ("." + destination.name + ".building-" + uuid.uuid4().hex)
    stage.mkdir()
    (stage / "original-migration-manifest.json").write_bytes(manifest_raw)
    (stage / "original-blocked-holdings.sqlite3").write_bytes(database_raw)
    (stage / "original-sources").mkdir()
    specs = []
    for producer, raw in archive_bytes.items():
        path = stage / "original-sources" / (producer + ".sqlite3")
        path.write_bytes(raw)
        specs.append({"producer": producer, "path": str(path), "symbols": partitions[producer],
                      "pins": migration.pin_ledger_files(path)})
    rebuilt = migration.consolidate_ownership_ledgers(sources=specs, destination_directory=stage / "reconstructed",
        expected_account_fingerprint=account, observed_at=portfolio.observed_at, cutover_action_date=cutover)
    for source in rebuilt["sources"]:
        original = originals[source["producer"]]
        if (source["row_counts"] != original.get("row_counts")
                or source["latest_snapshot_id"] != original.get("latest_snapshot_id")):
            raise ValueError("Original source row counts or latest baseline reference differs")
    before = _tables(stage / "original-blocked-holdings.sqlite3")
    expected = _tables(stage / "reconstructed/holdings.sqlite3")
    if (any(not _same_rows(before[table], expected[table]) for table in before)
            or before["snapshots"] != expected["snapshots"]
            or rebuilt["historical_fallback_collisions"] != manifest.get("historical_fallback_collisions")
            or {table: len(rows) for table, rows in before.items()} != manifest.get("row_counts")):
        raise ValueError("Blocked migration rows do not exactly match both archived originals")
    if ({row["symbol"]: row["reason"] for row in before["blocks"]} != dict.fromkeys(symbols, migration._BLOCK)
            or any(row["status"] in migration._OPEN for row in before["reservations"])):
        raise ValueError("Only exact migration blocks and terminal native reservations are permitted")
    checks = []
    (stage / "producer-checks").mkdir()
    for producer, raw in archive_bytes.items():
        fresh()
        path = stage / "producer-checks" / (producer + ".sqlite3")
        path.write_bytes(raw)
        evidence = replace(portfolio, snapshot_id=canonical_sha256([portfolio.snapshot_id, producer]),
            held_shares={s: portfolio.held_shares[s] for s in partitions[producer]},
            prices={s: portfolio.prices[s] for s in partitions[producer]},
            symbol_budgets={s: 0 for s in partitions[producer]})
        result = HorizonLedger(path, account).reconcile(evidence)
        checks.append({"producer": producer, "source_archive_sha256": _sha(raw),
            "prior_snapshot_id": originals[producer]["latest_snapshot_id"],
            "observed_portfolio": asdict(evidence), "result": asdict(result)})
        if not result.ready:
            (stage / "failed-producer-checks.json").write_bytes(_encoded(checks))
            raise ValueError("Producer baseline reconciliation failed: " + producer + ":" + ",".join(result.reasons))
    database = stage / "holdings.sqlite3"
    database.write_bytes(database_raw)
    # This reviewed transition releases only its own exact migration markers,
    # on a private staged copy, after BOTH unchanged native checks succeeded.
    with closing(sqlite3.connect(database)) as db:
        db.execute("BEGIN IMMEDIATE")
        rows = dict(db.execute("SELECT symbol,reason FROM blocks"))
        if rows != dict.fromkeys(symbols, migration._BLOCK):
            raise ValueError("Migration blocks changed before native transition")
        db.execute("DELETE FROM blocks WHERE reason=?", (migration._BLOCK,))
        db.commit()
    fresh()
    result = HorizonLedger(database, account).reconcile(portfolio)
    if not result.ready:
        raise ValueError("Union native reconciliation did not become ready: " + ",".join(result.reasons))
    after = _tables(database)
    _preserved_native_rows(before, after)
    order_proof = {key: asdict(broker_snapshot)[key] for key in ("observed_at", "source_fingerprint",
        "broker_identity_fingerprint", "working_order_count", "broker_working_orders", "pending_buy_shares", "pending_sell_shares")}
    report = {"schema_version": VERSION, "status": "UNION_RECONCILIATION_VERIFIED",
        "cutover_action_date": cutover, "account_fingerprint": account, "participants": partitions,
        "migration_manifest_sha256": expected_manifest_sha256, "portfolio_sha256": expected_portfolio_sha256,
        "broker_snapshot_sha256": expected_broker_snapshot_sha256, "producer_checks": checks,
        "union_result": asdict(result), "database_sha256": _sha(database.read_bytes()),
        "only_exact_migration_blocks_released": True, "original_native_ids_preserved": True,
        "runtime_activation": False, "orders_placed": 0, "broker_calls": 0,
        "activation_remaining": ["Peer execution fence and reviewed installed-source evidence", "Reviewed live-ledger installation and local cutover receipt"]}
    if not require_before_opening:
        report.update(reconciliation_mode="MANUAL_STARTUP", reconciled_at=reconciled_at.isoformat(),
                      activation_remaining=["Native union installation and local readiness receipt"])
    (stage / "portfolio-evidence.json").write_bytes(_encoded(asdict(portfolio)))
    (stage / "broker-snapshot.json").write_bytes(_encoded(asdict(broker_snapshot)))
    (stage / "broker-order-evidence.json").write_bytes(_encoded(order_proof))
    (stage / "report.json").write_bytes(_encoded(report))
    outputs = {p.relative_to(stage).as_posix(): _sha(p.read_bytes()) for p in stage.rglob("*") if p.is_file()}
    final_manifest = {"schema_version": VERSION, "migration_manifest_sha256": expected_manifest_sha256,
                      "output_files": outputs}
    (stage / "manifest.json").write_bytes(_encoded(final_manifest))
    receipt = {"schema_version": VERSION, "status": "UNION_RECONCILIATION_VERIFIED",
               "manifest_sha256": _sha((stage / "manifest.json").read_bytes()), "orders_placed": 0,
               "runtime_activation": False}
    (stage / "receipt.json").write_bytes(_encoded(receipt))
    def final_guard(location):
        fresh()
        _pinned(candidate / "manifest.json", expected_manifest_sha256)
        _pinned(candidate / "holdings.sqlite3", manifest["database_sha256"], database=True)
        for source, archive in zip(sorted(sources, key=lambda v:v["producer"]), sorted(archives, key=lambda v:v["producer"])):
            migration._check_pins(Path(source["path"]), source["pins"])
            _pinned(candidate / archive["path"], archive["sha256"], database=True)
        _portfolio(portfolio, broker_snapshot, symbols, account, expected_portfolio_sha256, expected_broker_snapshot_sha256)
        for relative, pin in outputs.items():
            _pinned(location / relative, pin)
        _pinned(location / "manifest.json", receipt["manifest_sha256"])
        _pinned(location / "receipt.json", _sha(_encoded(receipt)))
        if {p.relative_to(location).as_posix() for p in location.rglob("*") if p.is_file()} != set(outputs) | {"manifest.json", "receipt.json"}:
            raise ValueError("Reconciliation output inventory changed")
    final_guard(stage)
    if destination.exists():
        raise ValueError("Destination appeared before publication")
    stage.rename(destination)
    try:
        final_guard(destination)
    except BaseException:
        (destination / "receipt.json").write_bytes(_encoded({**receipt, "status": "FAILED_POST_PUBLICATION_GUARD"}))
        raise
    return {**receipt, "destination_directory": str(destination), "report": report}


def reconcile_migration_candidate(*, candidate_directory, expected_manifest_sha256,
        expected_participants, expected_account_fingerprint, portfolio, expected_portfolio_sha256,
        broker_snapshot, expected_broker_snapshot_sha256, destination_directory, clock=None):
    """Preserve the original dated reconciliation contract for existing callers."""
    return _reconcile_migration_candidate(candidate_directory=candidate_directory,
        expected_manifest_sha256=expected_manifest_sha256, expected_participants=expected_participants,
        expected_account_fingerprint=expected_account_fingerprint, portfolio=portfolio,
        expected_portfolio_sha256=expected_portfolio_sha256, broker_snapshot=broker_snapshot,
        expected_broker_snapshot_sha256=expected_broker_snapshot_sha256,
        destination_directory=destination_directory, clock=clock, require_before_opening=True)


def reconcile_startup_candidate(*, candidate_directory, expected_manifest_sha256,
        expected_participants, expected_account_fingerprint, portfolio, expected_portfolio_sha256,
        broker_snapshot, expected_broker_snapshot_sha256, destination_directory, clock=None):
    """Verify fresh native ownership for manual startup at any time of day.

    The saved migration date and all accounting checks remain unchanged. The
    caller controls session eligibility; 04:00 readiness is a target, not an
    expiry of otherwise fresh ownership evidence. This function starts nothing.
    """
    return _reconcile_migration_candidate(candidate_directory=candidate_directory,
        expected_manifest_sha256=expected_manifest_sha256, expected_participants=expected_participants,
        expected_account_fingerprint=expected_account_fingerprint, portfolio=portfolio,
        expected_portfolio_sha256=expected_portfolio_sha256, broker_snapshot=broker_snapshot,
        expected_broker_snapshot_sha256=expected_broker_snapshot_sha256,
        destination_directory=destination_directory, clock=clock, require_before_opening=False)


def reconcile_startup_continuation(*, previous_directory, expected_manifest_sha256,
        expected_participants, expected_account_fingerprint, portfolio, expected_portfolio_sha256,
        broker_snapshot, expected_broker_snapshot_sha256, destination_directory, clock=None):
    """Extend an already verified union with one fresh native reconciliation.

    The caller must prove, under native locks, that live rows equal this prior
    union and use those exact rows as the installation transaction's preimage.
    Starting from the previous union preserves its new accounting identities;
    rebuilding the original migration would discard that committed history.
    This function only creates a new private candidate and starts nothing.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    previous, destination = migration._plain(previous_directory), migration._plain(destination_directory)
    if (not previous.is_dir() or destination.exists() or not destination.parent.is_dir()
            or previous == destination or previous in destination.parents or destination in previous.parents):
        raise ValueError("A separate new destination under an existing parent is required")
    account = _hash(expected_account_fingerprint)
    if (not isinstance(expected_participants, dict) or set(expected_participants) != {"pc-original", "pc-new"}
            or any(not isinstance(values, (list, tuple)) or not values
                   or any(not isinstance(v, str) for v in values) or len(set(values)) != len(values)
                   for values in expected_participants.values())):
        raise ValueError("Both explicit unique producer partitions are required")
    partitions = {producer: sorted(validated_symbols(values)) for producer, values in expected_participants.items()}
    symbols = set(partitions["pc-original"]) | set(partitions["pc-new"])
    if len(symbols) != sum(map(len, partitions.values())):
        raise ValueError("Producer symbol partitions overlap")
    manifest_raw = _pinned(previous / "manifest.json", expected_manifest_sha256)
    manifest = _json(manifest_raw)
    outputs = manifest.get("output_files")
    if (manifest.get("schema_version") != VERSION or not isinstance(outputs, dict)
            or not {"holdings.sqlite3", "report.json", "portfolio-evidence.json", "broker-snapshot.json", "broker-order-evidence.json"} <= outputs.keys()):
        raise ValueError("Previous reconciliation manifest is incomplete")
    prior_pins = {}
    for relative, checksum in outputs.items():
        path = Path(relative)
        if (not isinstance(relative, str) or path.is_absolute() or path.drive or ":" in relative or ".." in path.parts
                or not path.parts or path.as_posix() != relative or path.parts[0] in {"manifest.json", "receipt.json"}):
            raise ValueError("Previous reconciliation contains an invalid output path")
        selected = previous / path
        _pinned(selected, checksum)
        prior_pins[relative] = checksum
    if {p.relative_to(previous).as_posix() for p in previous.rglob("*") if p.is_file()} != set(outputs) | {"manifest.json", "receipt.json"}:
        raise ValueError("Previous reconciliation output inventory changed")
    receipt_raw = migration._plain(previous / "receipt.json").read_bytes()
    receipt = _json(receipt_raw)
    if receipt != {"schema_version": VERSION, "status": "UNION_RECONCILIATION_VERIFIED",
                   "manifest_sha256": expected_manifest_sha256, "orders_placed": 0, "runtime_activation": False}:
        raise ValueError("Previous reconciliation receipt is not complete")
    report = _json(_pinned(previous / "report.json", outputs["report.json"]))
    if (report.get("schema_version") != VERSION or report.get("status") != "UNION_RECONCILIATION_VERIFIED"
            or report.get("account_fingerprint") != account or report.get("participants") != partitions
            or report.get("database_sha256") != outputs["holdings.sqlite3"]
            or report.get("migration_manifest_sha256") != manifest.get("migration_manifest_sha256")
            or report.get("original_native_ids_preserved") is not True or report.get("runtime_activation") is not False
            or report.get("orders_placed") != 0 or report.get("broker_calls") != 0
            or report.get("union_result", {}).get("ready") is not True):
        raise ValueError("Previous reconciliation account or result differs")
    cutover = report.get("cutover_action_date")
    if not isinstance(cutover, str) or date.fromisoformat(cutover).isoformat() != cutover:
        raise ValueError("Previous reconciliation omitted its original migration date")
    _portfolio(portfolio, broker_snapshot, symbols, account, expected_portfolio_sha256, expected_broker_snapshot_sha256)
    def fresh():
        return _fresh(portfolio, clock, cutover, require_before_opening=False)
    reconciled_at = fresh()
    database_raw = _pinned(previous / "holdings.sqlite3", outputs["holdings.sqlite3"], database=True)
    before = _tables(previous / "holdings.sqlite3")
    if {row["key"]: row["value"] for row in before["metadata"]} != {
            "account": account, "version": migration.LEDGER_VERSION,
            "account_gameplan_migration": "REQUIRES_REVIEWED_ACTIVATION"}:
        raise ValueError("Previous native union account or metadata differs")
    with closing(sqlite3.connect((previous / "holdings.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        migration._audit_rows(before, HorizonLedger._snapshot(db), account, sorted(symbols), _aware(portfolio.observed_at))
    if (before["blocks"] or any(row["status"] in migration._OPEN for row in before["reservations"])
            or not before["snapshots"] or before["snapshots"][-1]["ready"] != 1):
        raise ValueError("Previous native union is not a complete unblocked baseline")
    stage = destination.parent / ("." + destination.name + ".building-" + uuid.uuid4().hex)
    stage.mkdir()
    for name, raw in (("previous-manifest.json", manifest_raw), ("previous-receipt.json", receipt_raw),
                      ("previous-report.json", _pinned(previous / "report.json", outputs["report.json"])),
                      ("previous-holdings.sqlite3", database_raw)):
        (stage / name).write_bytes(raw)
    database = stage / "holdings.sqlite3"
    database.write_bytes(database_raw)
    fresh()
    result = HorizonLedger(database, account).reconcile(portfolio)
    if not result.ready:
        raise ValueError("Union native continuation did not become ready: " + ",".join(result.reasons))
    after = _tables(database)
    _preserved_native_rows(before, after)
    if after["snapshots"][:-1] != before["snapshots"]:
        raise ValueError("Previous native snapshot chronology changed")
    order_proof = {key: asdict(broker_snapshot)[key] for key in ("observed_at", "source_fingerprint",
        "broker_identity_fingerprint", "working_order_count", "broker_working_orders", "pending_buy_shares", "pending_sell_shares")}
    report = {**report, "reconciliation_mode": "MANUAL_STARTUP_CONTINUATION", "reconciled_at": reconciled_at.isoformat(),
              "previous_reconciliation_manifest_sha256": expected_manifest_sha256,
              "portfolio_sha256": expected_portfolio_sha256, "broker_snapshot_sha256": expected_broker_snapshot_sha256,
              "union_result": asdict(result), "database_sha256": _sha(database.read_bytes()),
              "activation_remaining": ["Native union installation and local readiness receipt"]}
    for name, value in (("portfolio-evidence.json", asdict(portfolio)), ("broker-snapshot.json", asdict(broker_snapshot)),
                        ("broker-order-evidence.json", order_proof), ("report.json", report)):
        (stage / name).write_bytes(_encoded(value))
    new_outputs = {p.relative_to(stage).as_posix(): _sha(p.read_bytes()) for p in stage.rglob("*") if p.is_file()}
    new_manifest = {"schema_version": VERSION, "migration_manifest_sha256": manifest["migration_manifest_sha256"],
                    "output_files": new_outputs}
    (stage / "manifest.json").write_bytes(_encoded(new_manifest))
    new_receipt = {**receipt, "manifest_sha256": _sha((stage / "manifest.json").read_bytes())}
    (stage / "receipt.json").write_bytes(_encoded(new_receipt))
    def final_guard(location):
        fresh()
        _pinned(previous / "manifest.json", expected_manifest_sha256)
        _pinned(previous / "receipt.json", _sha(receipt_raw))
        if {p.relative_to(previous).as_posix() for p in previous.rglob("*") if p.is_file()} != set(outputs) | {"manifest.json", "receipt.json"}:
            raise ValueError("Previous reconciliation output inventory changed")
        for relative, checksum in prior_pins.items():
            _pinned(previous / relative, checksum)
        _portfolio(portfolio, broker_snapshot, symbols, account, expected_portfolio_sha256, expected_broker_snapshot_sha256)
        for relative, checksum in new_outputs.items():
            _pinned(location / relative, checksum)
        _pinned(location / "manifest.json", new_receipt["manifest_sha256"])
        _pinned(location / "receipt.json", _sha(_encoded(new_receipt)))
        if {p.relative_to(location).as_posix() for p in location.rglob("*") if p.is_file()} != set(new_outputs) | {"manifest.json", "receipt.json"}:
            raise ValueError("Reconciliation output inventory changed")
    final_guard(stage)
    if destination.exists():
        raise ValueError("Destination appeared before publication")
    stage.rename(destination)
    try:
        final_guard(destination)
    except BaseException:
        (destination / "receipt.json").write_bytes(_encoded({**new_receipt, "status": "FAILED_POST_PUBLICATION_GUARD"}))
        raise
    return {**new_receipt, "destination_directory": str(destination), "report": report}


__all__ = ["reconcile_migration_candidate", "reconcile_startup_candidate", "reconcile_startup_continuation"]
