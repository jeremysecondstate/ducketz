"""Cutover fixtures use temporary native ledgers and no broker/network clients."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from ml.account_gameplan import cutover, migration
from ml.stock_trader.contracts import PortfolioState, QuoteState, canonical_sha256
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from test_account_gameplan_migration import ACCOUNT, OBSERVED, source, freeze_sources


NOW = "2026-10-03T01:00:01+00:00"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setup(tmp_path, *, freeze=False, newer_peer=False):
    first, a = source(tmp_path, "pc-original", "COST")
    second, b = source(tmp_path, "pc-new", "MSFT", 4)
    specs, ledgers = [first, second], [a, b]
    if freeze:
        freeze_sources(specs, ledgers)
    if newer_peer:
        assert b.reconcile(PortfolioEvidence("newer-peer", ACCOUNT, "2026-10-02T12:00:00+00:00",
            {"MSFT": 6}, {"MSFT": 100}, {"MSFT": 0}, "c"*64)).ready
        second["pins"] = migration.pin_ledger_files(b.path)
    merged = migration.consolidate_ownership_ledgers(sources=specs, destination_directory=tmp_path/"migration",
        expected_account_fingerprint=ACCOUNT, observed_at=OBSERVED, cutover_action_date="2026-10-05")
    held = {"COST": 5, "MSFT": 6}
    portfolio = PortfolioEvidence("fresh-union", ACCOUNT, NOW, held, {s: 100 for s in held},
                                  {s: 0 for s in held}, "d"*64)
    broker = PortfolioState(NOW, 1000, 100, 900, 0, held, {}, {}, {}, 0,
        {s: QuoteState(s, 99, 100, 100, 100, 1000, NOW) for s in held}, "d"*64, "e"*64, ())
    return dict(candidate_directory=tmp_path/"migration", expected_manifest_sha256=merged["manifest_sha256"],
        expected_participants={"pc-original": ["COST"], "pc-new": ["MSFT"]},
        expected_account_fingerprint=ACCOUNT, portfolio=portfolio,
        expected_portfolio_sha256=canonical_sha256(asdict(portfolio)), broker_snapshot=broker,
        expected_broker_snapshot_sha256=canonical_sha256(asdict(broker)), destination_directory=tmp_path/"ready",
        clock=lambda: datetime.fromisoformat(NOW)), specs


def pin_observations(args, portfolio=None, broker=None):
    if portfolio is not None:
        args["portfolio"] = portfolio
    if broker is not None:
        args["broker_snapshot"] = broker
    args["expected_portfolio_sha256"] = canonical_sha256(asdict(args["portfolio"]))
    args["expected_broker_snapshot_sha256"] = canonical_sha256(asdict(args["broker_snapshot"]))


def reseal_candidate(args):
    path = args["candidate_directory"] / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["database_sha256"] = sha(path.parent / "holdings.sqlite3")
    path.write_bytes(cutover._encoded(manifest))
    args["expected_manifest_sha256"] = sha(path)


def test_fresh_union_checks_both_baselines_preserves_originals_and_activates_nothing(tmp_path):
    args, specs = setup(tmp_path, freeze=True, newer_peer=True)
    before = {p: sha(p) for p in args["candidate_directory"].rglob("*") if p.is_file()}
    original_pins = [migration.pin_ledger_files(s["path"]) for s in specs]
    result = cutover.reconcile_migration_candidate(**args)
    assert result["status"] == "UNION_RECONCILIATION_VERIFIED"
    assert not result["runtime_activation"] and result["orders_placed"] == 0
    report = result["report"]
    assert report["broker_calls"] == 0 and report["only_exact_migration_blocks_released"]
    assert {c["producer"] for c in report["producer_checks"]} == {"pc-original", "pc-new"}
    assert all(c["result"]["ready"] for c in report["producer_checks"])
    assert {p: sha(p) for p in before} == before
    assert [migration.pin_ledger_files(s["path"]) for s in specs] == original_pins
    original = cutover._tables(args["candidate_directory"]/"holdings.sqlite3")
    final = cutover._tables(args["destination_directory"]/"holdings.sqlite3")
    for table in ("allocations", "reservations", "fills", "inventory_assignments", "inventory_assignment_releases"):
        assert original[table] == final[table]
    assert final["blocks"] == [] and len(final["snapshots"]) == len(original["snapshots"]) + 1
    assert final["snapshots"][-1]["ready"] == 1
    assert json.loads(final["snapshots"][-1]["payload"])["symbol_budgets"] == {"COST":"0", "MSFT":"0"}
    assert len(original["fallback_days"]) == len(final["fallback_days"]) == 0
    manifest = json.loads((args["destination_directory"]/"manifest.json").read_text())
    assert all(sha(args["destination_directory"]/p) == pin for p,pin in manifest["output_files"].items())
    complete_broker = json.loads((args["destination_directory"]/"broker-snapshot.json").read_text())
    assert canonical_sha256(complete_broker) == args["expected_broker_snapshot_sha256"]
    assert canonical_sha256(json.loads((args["destination_directory"]/"portfolio-evidence.json").read_text())) == args["expected_portfolio_sha256"]
    assert not (tmp_path/"state").exists() and not (tmp_path/"ml").exists()


def test_older_producer_unexplained_reduction_cannot_hide_behind_newer_peer_snapshot(tmp_path):
    args, _ = setup(tmp_path, newer_peer=True)
    held = {"COST": 4, "MSFT": 6}  # Still covers all owned shares; only older baseline detects reduction.
    pin_observations(args, replace(args["portfolio"], held_shares=held), replace(args["broker_snapshot"], held_shares=held))
    with pytest.raises(ValueError, match="Producer baseline.*pc-original.*UNEXPLAINED"):
        cutover.reconcile_migration_candidate(**args)
    assert not args["destination_directory"].exists()
    assert len(cutover._tables(args["candidate_directory"]/"holdings.sqlite3")["blocks"]) == 2


@pytest.mark.parametrize("reconciler", [cutover.reconcile_migration_candidate, cutover.reconcile_startup_candidate])
@pytest.mark.parametrize("failure", ["stale", "future", "account", "missing_symbol", "extra_symbol",
    "price", "budget", "negative", "pending", "external_pending", "working", "unknown_orders", "portfolio_pin",
    "broker_pin", "source_identity", "observation_time", "source_changed", "archive_changed", "manifest_changed",
    "foreign_block", "missing_block", "unknown_native", "candidate_changed", "destination", "overlap"])
def test_invalid_or_ambiguous_evidence_never_publishes_ready_output(tmp_path, failure, reconciler):
    args, specs = setup(tmp_path)
    portfolio, broker = args["portfolio"], args["broker_snapshot"]
    if failure == "stale": args["clock"] = lambda: datetime.fromisoformat(NOW) + timedelta(seconds=61)
    elif failure == "future": args["clock"] = lambda: datetime.fromisoformat(NOW) - timedelta(seconds=1)
    elif failure == "account": portfolio = replace(portfolio, account_fingerprint="f"*64)
    elif failure == "missing_symbol": portfolio = replace(portfolio, held_shares={"COST":5})
    elif failure == "extra_symbol": portfolio = replace(portfolio, held_shares={**portfolio.held_shares,"AAPL":0})
    elif failure == "price": portfolio = replace(portfolio, prices={"COST":101,"MSFT":100})
    elif failure == "budget": portfolio = replace(portfolio, symbol_budgets={"COST":1,"MSFT":0})
    elif failure == "negative": portfolio = replace(portfolio, held_shares={"COST":-1,"MSFT":6})
    elif failure in {"pending", "external_pending"}: broker = replace(broker, pending_buy_shares={"COST" if failure=="pending" else "TSLA":1})
    elif failure == "working": broker = replace(broker, working_order_count=1)
    elif failure == "unknown_orders": broker = replace(broker, broker_working_orders=None)
    elif failure == "source_identity": broker = replace(broker, source_fingerprint="f"*64)
    elif failure == "observation_time": broker = replace(broker, observed_at=OBSERVED)
    elif failure == "source_changed":
        with sqlite3.connect(specs[0]["path"]) as db: db.execute("INSERT INTO blocks VALUES ('COST','EXTERNAL')")
    elif failure == "archive_changed":
        archive = next((args["candidate_directory"]/"sources").glob("*.sqlite3")); archive.write_bytes(archive.read_bytes()+b"changed")
    elif failure == "manifest_changed":
        path=args["candidate_directory"]/"manifest.json";path.write_bytes(path.read_bytes()+b" ")
    elif failure in {"foreign_block", "missing_block", "unknown_native"}:
        with sqlite3.connect(args["candidate_directory"]/"holdings.sqlite3") as db:
            if failure == "foreign_block": db.execute("UPDATE blocks SET reason='UNEXPLAINED' WHERE symbol='COST'")
            elif failure == "missing_block": db.execute("DELETE FROM blocks WHERE symbol='COST'")
            else: db.execute("UPDATE reservations SET status='UNKNOWN'")
        reseal_candidate(args)
    elif failure == "candidate_changed":
        path=args["candidate_directory"]/"holdings.sqlite3";path.write_bytes(path.read_bytes()+b"changed")
    elif failure == "destination": args["destination_directory"].mkdir()
    elif failure == "overlap": args["expected_participants"]["pc-new"] = ["COST"]
    pin_observations(args, portfolio, broker)
    if failure == "portfolio_pin": args["expected_portfolio_sha256"] = "0"*64
    if failure == "broker_pin": args["expected_broker_snapshot_sha256"] = "0"*64
    with pytest.raises(ValueError): reconciler(**args)
    assert not (args["destination_directory"]/"receipt.json").exists()


@pytest.mark.parametrize("reconciler", [cutover.reconcile_migration_candidate, cutover.reconcile_startup_candidate])
def test_final_freshness_expiry_tombstones_receipt_without_touching_inputs(tmp_path, reconciler):
    args, _ = setup(tmp_path)
    original = sha(args["candidate_directory"]/"holdings.sqlite3")
    args["clock"] = lambda: datetime.fromisoformat(NOW) + timedelta(seconds=61 if args["destination_directory"].exists() else 0)
    with pytest.raises(ValueError, match="stale"):
        reconciler(**args)
    saved = json.loads((args["destination_directory"]/"receipt.json").read_text())
    assert saved["status"] == "FAILED_POST_PUBLICATION_GUARD" and not saved["runtime_activation"]
    assert sha(args["candidate_directory"]/"holdings.sqlite3") == original


@pytest.mark.parametrize("observed_at", [
    "2026-10-05T10:59:59+00:00", "2026-10-05T11:00:00+00:00",
    "2026-10-05T19:00:00+00:00", "2026-10-06T19:00:00+00:00",
])
def test_manual_startup_accepts_fresh_ownership_without_relabeling_original_date(tmp_path, observed_at):
    args, specs = setup(tmp_path, freeze=True, newer_peer=True)
    before = {p: sha(p) for p in args["candidate_directory"].rglob("*") if p.is_file()}
    source_pins = [migration.pin_ledger_files(s["path"]) for s in specs]
    portfolio = replace(args["portfolio"], observed_at=observed_at)
    broker = replace(args["broker_snapshot"], observed_at=observed_at)
    pin_observations(args, portfolio, broker)
    # At the exact freshness boundary even crossing 04:00 cannot expire ownership.
    now = datetime.fromisoformat(observed_at) + timedelta(seconds=60)
    args["clock"] = lambda: now
    result = cutover.reconcile_startup_candidate(**args)
    assert result["status"] == "UNION_RECONCILIATION_VERIFIED"
    assert result["runtime_activation"] is False and result["orders_placed"] == 0
    report = result["report"]
    assert report["cutover_action_date"] == "2026-10-05"
    assert report["reconciliation_mode"] == "MANUAL_STARTUP"
    assert report["reconciled_at"] == now.isoformat()
    assert all(c["result"]["ready"] for c in report["producer_checks"])
    assert {p: sha(p) for p in before} == before
    assert [migration.pin_ledger_files(s["path"]) for s in specs] == source_pins
    original = cutover._tables(args["candidate_directory"] / "holdings.sqlite3")
    final = cutover._tables(args["destination_directory"] / "holdings.sqlite3")
    for table in ("allocations", "reservations", "fills", "inventory_assignments", "inventory_assignment_releases"):
        assert original[table] == final[table]


def test_original_dated_api_keeps_its_contract_for_existing_callers(tmp_path):
    args, _ = setup(tmp_path)
    observed_at = "2026-10-05T11:00:00+00:00"
    pin_observations(args, replace(args["portfolio"], observed_at=observed_at),
                     replace(args["broker_snapshot"], observed_at=observed_at))
    args["clock"] = lambda: datetime.fromisoformat(observed_at)
    with pytest.raises(ValueError, match="opening deadline"):
        cutover.reconcile_migration_candidate(**args)
    assert not args["destination_directory"].exists()


def continuation_args(tmp_path):
    args, _ = setup(tmp_path, freeze=True, newer_peer=True)
    completed = cutover.reconcile_startup_candidate(**args)
    observed_at = "2026-10-05T19:00:00+00:00"
    portfolio = replace(args["portfolio"], snapshot_id="continuation-union", observed_at=observed_at)
    broker = replace(args["broker_snapshot"], observed_at=observed_at)
    return dict(previous_directory=args["destination_directory"], expected_manifest_sha256=completed["manifest_sha256"],
        expected_participants=args["expected_participants"], expected_account_fingerprint=ACCOUNT,
        portfolio=portfolio, expected_portfolio_sha256=canonical_sha256(asdict(portfolio)),
        broker_snapshot=broker, expected_broker_snapshot_sha256=canonical_sha256(asdict(broker)),
        destination_directory=tmp_path / "continued", clock=lambda: datetime.fromisoformat(observed_at))


def test_manual_continuation_preserves_every_previously_committed_identity(tmp_path):
    args = continuation_args(tmp_path)
    original_files = {p: sha(p) for p in args["previous_directory"].rglob("*") if p.is_file()}
    prior = cutover._tables(args["previous_directory"] / "holdings.sqlite3")
    first = cutover.reconcile_startup_continuation(**args)
    current = cutover._tables(args["destination_directory"] / "holdings.sqlite3")
    assert first["report"]["reconciliation_mode"] == "MANUAL_STARTUP_CONTINUATION"
    assert first["report"]["previous_reconciliation_manifest_sha256"] == args["expected_manifest_sha256"]
    assert first["report"]["cutover_action_date"] == "2026-10-05"
    assert not first["runtime_activation"] and first["orders_placed"] == 0
    for table, rows in prior.items():
        if table in {"snapshots", "evidence"}:
            assert current[table][:-1] == rows
        else:
            assert current[table] == rows
    assert {p: sha(p) for p in original_files} == original_files
    # Another interrupted installation can extend the exact chain, never reset it.
    args.update(previous_directory=args["destination_directory"], expected_manifest_sha256=first["manifest_sha256"],
                destination_directory=tmp_path / "continued-again")
    next_observed = "2026-10-05T19:00:01+00:00"
    pin_observations(args, replace(args["portfolio"], snapshot_id="continuation-again", observed_at=next_observed),
                     replace(args["broker_snapshot"], observed_at=next_observed))
    args["clock"] = lambda: datetime.fromisoformat(next_observed)
    second = cutover.reconcile_startup_continuation(**args)
    final = cutover._tables(args["destination_directory"] / "holdings.sqlite3")
    assert final["snapshots"][:-1] == current["snapshots"]
    assert final["evidence"][:-1] == current["evidence"]
    assert second["report"]["previous_reconciliation_manifest_sha256"] == first["manifest_sha256"]


@pytest.mark.parametrize("failure", ["stale", "future", "pending", "unknown_orders", "holdings", "account",
    "source_pin", "receipt", "inventory", "prior_changed", "overlap", "post_publish_stale"])
def test_manual_continuation_blocks_incomplete_or_changed_evidence(tmp_path, failure):
    args = continuation_args(tmp_path)
    before = sha(args["previous_directory"] / "holdings.sqlite3")
    if failure == "stale":
        args["clock"] = lambda: datetime.fromisoformat(args["portfolio"].observed_at) + timedelta(seconds=61)
    elif failure == "future":
        args["clock"] = lambda: datetime.fromisoformat(args["portfolio"].observed_at) - timedelta(seconds=1)
    elif failure == "pending":
        pin_observations(args, broker=replace(args["broker_snapshot"], pending_buy_shares={"TSLA": 1}))
    elif failure == "unknown_orders":
        pin_observations(args, broker=replace(args["broker_snapshot"], broker_working_orders=None))
    elif failure == "holdings":
        held = {"COST": 4, "MSFT": 6}
        pin_observations(args, replace(args["portfolio"], held_shares=held), replace(args["broker_snapshot"], held_shares=held))
    elif failure == "account":
        args["expected_account_fingerprint"] = "f" * 64
    elif failure == "source_pin":
        args["expected_manifest_sha256"] = "0" * 64
    elif failure == "receipt":
        path = args["previous_directory"] / "receipt.json"
        saved = json.loads(path.read_bytes()); saved["status"] = "FAILED_POST_PUBLICATION_GUARD"
        path.write_bytes(cutover._encoded(saved))
    elif failure == "inventory":
        (args["previous_directory"] / "INVALID.json").write_text("{}")
    elif failure == "prior_changed":
        path = args["previous_directory"] / "report.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif failure == "overlap":
        args["expected_participants"]["pc-new"] = ["COST"]
    elif failure == "post_publish_stale":
        args["clock"] = lambda: datetime.fromisoformat(args["portfolio"].observed_at) + timedelta(
            seconds=61 if args["destination_directory"].exists() else 0)
    with pytest.raises(ValueError):
        cutover.reconcile_startup_continuation(**args)
    if failure == "post_publish_stale":
        receipt = json.loads((args["destination_directory"] / "receipt.json").read_bytes())
        assert receipt["status"] == "FAILED_POST_PUBLICATION_GUARD"
    else:
        assert not args["destination_directory"].exists()
    assert sha(args["previous_directory"] / "holdings.sqlite3") == before


def test_manual_continuation_rechecks_prior_inventory_during_native_reconciliation(tmp_path, monkeypatch):
    args = continuation_args(tmp_path)
    native = HorizonLedger.reconcile
    def changed(self, *a, **kw):
        result = native(self, *a, **kw)
        (args["previous_directory"] / "INVALID.json").write_text("{}")
        return result
    monkeypatch.setattr(HorizonLedger, "reconcile", changed)
    with pytest.raises(ValueError, match="inventory changed"):
        cutover.reconcile_startup_continuation(**args)
    assert not args["destination_directory"].exists()


@pytest.mark.parametrize("reconciler", [cutover.reconcile_migration_candidate, cutover.reconcile_startup_candidate,
                                     cutover.reconcile_startup_continuation])
@pytest.mark.parametrize("unexpected", ["INVALID.json", "holdings.sqlite3-wal"])
def test_new_output_inventory_change_after_publication_invalidates_receipt(tmp_path, monkeypatch, reconciler, unexpected):
    if reconciler is cutover.reconcile_startup_continuation:
        args = continuation_args(tmp_path)
        source_directory = args["previous_directory"]
    else:
        args, _ = setup(tmp_path)
        source_directory = args["candidate_directory"]
    before = {p: sha(p) for p in source_directory.rglob("*") if p.is_file()}
    native_rename = Path.rename
    def altered(self, target):
        result = native_rename(self, target)
        if Path(target) == args["destination_directory"]:
            (Path(target) / unexpected).write_bytes(b"unexpected")
        return result
    monkeypatch.setattr(Path, "rename", altered)
    with pytest.raises(ValueError, match="output inventory changed"):
        reconciler(**args)
    receipt = json.loads((args["destination_directory"] / "receipt.json").read_bytes())
    assert receipt["status"] == "FAILED_POST_PUBLICATION_GUARD"
    assert {p: sha(p) for p in before} == before


def test_caller_mutating_observation_during_native_checks_fails_final_pin(tmp_path, monkeypatch):
    args, _ = setup(tmp_path)
    native = HorizonLedger.reconcile
    count = 0
    def observe(self, *a, **kw):
        nonlocal count
        result = native(self, *a, **kw)
        count += 1
        if count == 3: args["portfolio"].held_shares["COST"] = 10
        return result
    monkeypatch.setattr(HorizonLedger, "reconcile", observe)
    with pytest.raises(ValueError, match="content differs"):
        cutover.reconcile_migration_candidate(**args)
    assert not args["destination_directory"].exists()


def test_complete_22_symbol_candidate_is_accepted_by_native_planning_reader(tmp_path):
    args, specs = setup(tmp_path)
    # Prepare a new migration from explicit complete producer baselines.
    partitions = {"pc-original": ["COST","AAPL","AMZN","GOOG","MU","NVDA","SNDK","CROX","PATH","TWST","IONQ"],
                  "pc-new": ["MSFT","DOCU","DBX","SDGR","QBTS","PYPL","GLOB","OUST","ABCL","MRNA","PDYN"]}
    held = {s: (5 if s=="COST" else 6 if s=="MSFT" else 0) for group in partitions.values() for s in group}
    for spec in specs:
        symbols = partitions[spec["producer"]]
        ledger = HorizonLedger(spec["path"], ACCOUNT)
        assert ledger.reconcile(PortfolioEvidence(spec["producer"]+"-complete", ACCOUNT,
            "2026-10-02T12:00:00+00:00", {s:held[s] for s in symbols}, {s:100 for s in symbols},
            {s:0 for s in symbols}, "b"*64)).ready
        spec["symbols"] = symbols
        spec["pins"] = migration.pin_ledger_files(spec["path"])
    merged = migration.consolidate_ownership_ledgers(sources=specs, destination_directory=tmp_path/"all22",
        expected_account_fingerprint=ACCOUNT, observed_at=OBSERVED, cutover_action_date="2026-10-05")
    args.update(candidate_directory=tmp_path/"all22",expected_manifest_sha256=merged["manifest_sha256"],expected_participants=partitions)
    pin_observations(args,replace(args["portfolio"],held_shares=held,prices={s:100 for s in held},symbol_budgets={s:0 for s in held}),
        replace(args["broker_snapshot"],held_shares=held,quotes={s:QuoteState(s,99,100,100,100,1000,NOW) for s in held}))
    cutover.reconcile_migration_candidate(**args)
    from ml.gameplan_trade_snapshot import _ownership
    local = tmp_path/"fixture-runtime"
    path = local/"state/independent-stock-trader/holdings.sqlite3"
    path.parent.mkdir(parents=True)
    shutil.copyfile(args["destination_directory"]/"holdings.sqlite3",path)
    checked = _ownership(local,tuple(sorted(held)),ACCOUNT,held,NOW)
    assert checked["safe_for_planning"] is True and checked["account_matches"] is True
    assert checked["last_saved_reconciliation_ready"] is True


def test_original_baseline_reference_is_verified_not_just_copied(tmp_path):
    args, _ = setup(tmp_path)
    path=args["candidate_directory"]/"manifest.json"
    doc=json.loads(path.read_text());doc["sources"][0]["latest_snapshot_id"]="invented-baseline"
    path.write_bytes(cutover._encoded(doc));args["expected_manifest_sha256"]=sha(path)
    with pytest.raises(ValueError,match="baseline reference"):
        cutover.reconcile_migration_candidate(**args)
    assert not args["destination_directory"].exists()


def test_snapshot_rowid_chronology_cannot_change_with_unchanged_row_contents(tmp_path):
    args, _ = setup(tmp_path,newer_peer=True)
    with sqlite3.connect(args["candidate_directory"]/"holdings.sqlite3") as db:
        rows=db.execute("SELECT * FROM snapshots ORDER BY rowid DESC").fetchall()
        db.execute("DELETE FROM snapshots")
        db.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",rows)
    reseal_candidate(args)
    with pytest.raises(ValueError,match="rows do not exactly match"):
        cutover.reconcile_migration_candidate(**args)
    assert not args["destination_directory"].exists()
