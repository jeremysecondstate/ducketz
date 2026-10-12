"""Offline merge tests create native ledgers only in pytest temporary folders."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import sqlite3

import pytest

from ml.account_gameplan import migration
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence, OrderEvidence, FillEvidence, LedgerError
from ml.stock_trader.cross_horizon_fallback import policy_for_action_date


ACCOUNT = "a" * 64
START = "2026-10-02T11:00:00+00:00"
FILLED = "2026-10-02T11:00:20+00:00"
READY = "2026-10-02T11:00:21+00:00"
OBSERVED = "2026-10-03T01:00:00+00:00"


def source(tmp_path, producer, symbol, quantity=3):
    ledger = HorizonLedger(tmp_path / (producer + ".sqlite3"), ACCOUNT)
    def portfolio(name, at, held):
        return PortfolioEvidence(producer + name, ACCOUNT, at, {symbol: held}, {symbol: 100},
                                 {symbol: 10000}, "b" * 64)
    assert ledger.reconcile(portfolio("-empty", START, 2)).ready
    reservation = ledger.reserve_entry(symbol=symbol, horizon="1d", forecast_id=producer+"-forecast",
        target_start=START, target_end="2026-10-09T00:00:00+00:00", quantity=quantity, limit_price=100,
        snapshot_id=producer+"-empty", idempotency_key=producer+"-entry", batch_id=producer+"-batch",
        as_of="2026-10-02T11:00:10+00:00")
    order = OrderEvidence(producer+"-fill-evidence", reservation.reservation_id, ACCOUNT, FILLED,
        producer+"-broker", "FILLED", quantity, quantity, 0,
        (FillEvidence(producer+"-fill", quantity, 100, FILLED),))
    assert ledger.reconcile(portfolio("-ready", READY, quantity+2), order_evidence=(order,)).ready
    return {"producer": producer, "path": str(ledger.path), "symbols": [symbol],
            "pins": migration.pin_ledger_files(ledger.path)}, ledger


def setup(tmp_path):
    a, first = source(tmp_path, "Atlas", "COST")
    b, second = source(tmp_path, "Scout", "MSFT", 4)
    return [a,b], [first, second]


def run(tmp_path, sources, **kwargs):
    parameters = dict(sources=sources, destination_directory=tmp_path/"candidate",
                      expected_account_fingerprint=ACCOUNT, observed_at=OBSERVED)
    parameters.update(kwargs)
    return migration.consolidate_ownership_ledgers(**parameters)


def mutate(source, sql, args=()):
    with sqlite3.connect(source["path"]) as db:
        db.execute(sql, args)
    source["pins"] = migration.pin_ledger_files(source["path"])


def test_two_native_ledgers_preserve_every_original_row_and_stay_blocked(tmp_path):
    sources, originals = setup(tmp_path)
    before = deepcopy([s["pins"] for s in sources])
    states = [ledger.snapshot() for ledger in originals]
    result = run(tmp_path, sources)
    assert result["status"] == "REQUIRES_REVIEWED_ACTIVATION" and not result["runtime_activation"]
    assert result["native_accounting_audit"] == "PASS" and result["broker_calls"] == 0
    assert result["symbols"] == ["COST", "MSFT"]
    assert result["row_counts"]["snapshots"] == 4 and result["row_counts"]["blocks"] == 2
    path = tmp_path/"candidate/holdings.sqlite3"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["database_sha256"]
    assert hashlib.sha256((path.parent/"manifest.json").read_bytes()).hexdigest() == result["manifest_sha256"]
    merged = HorizonLedger(path, ACCOUNT).snapshot()
    assert {a.allocation_id:asdict(a) for a in merged.allocations} == {a.allocation_id:asdict(a) for s in states for a in s.allocations}
    assert {r.reservation_id:asdict(r) for r in merged.reservations} == {r.reservation_id:asdict(r) for s in states for r in s.reservations}
    assert {s for s,_ in merged.persistent_blocks} == {"COST", "MSFT"}
    for spec, pins in zip(sources, before): assert migration.pin_ledger_files(spec["path"]) == pins
    with sqlite3.connect(path) as db:
        latest = db.execute("SELECT id FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()[0]
    with pytest.raises(LedgerError, match="PERSISTENT_INVENTORY"):
        HorizonLedger(path, ACCOUNT).reserve_entry(symbol="MSFT", horizon="1h", forecast_id="must-not-trade",
            target_start=START,target_end="2026-10-02T12:00:00+00:00",quantity=1,limit_price=100,
            snapshot_id=latest,idempotency_key="must-not-trade",batch_id="none",as_of="2026-10-02T11:00:22+00:00")


@pytest.mark.parametrize("failure", ["account", "symbols", "overlap", "producer", "pins", "block", "unknown", "working",
    "reserved", "submitted", "partial", "bad_fill", "snapshot_identity", "snapshot_holdings", "snapshot_future", "latest_unready",
    "duplicate_evidence", "duplicate_broker_order", "extra_table", "extra_column"])
def test_inconsistent_or_ambiguous_sources_fail_without_publishing_destination(tmp_path, failure):
    sources, _ = setup(tmp_path)
    if failure == "account": mutate(sources[0], "UPDATE metadata SET value=? WHERE key='account'", ("b"*64,))
    elif failure == "symbols": sources[0]["symbols"] = ["AAPL"]
    elif failure == "overlap": sources[1]["symbols"] = ["COST"]
    elif failure == "producer": sources[1]["producer"] = "Atlas"
    elif failure == "pins": sources[0]["pins"]["database_sha256"] = "0"*64
    elif failure == "block": mutate(sources[0], "INSERT INTO blocks VALUES ('COST','UNEXPLAINED')")
    elif failure in {"unknown", "working", "reserved", "submitted", "partial"}:
        mutate(sources[0], "UPDATE reservations SET status=?", (failure.upper(),))
    elif failure == "bad_fill": mutate(sources[0], "UPDATE fills SET quantity=2")
    elif failure.startswith("snapshot_"):
        with sqlite3.connect(sources[0]["path"]) as db:
            raw = db.execute("SELECT payload FROM snapshots WHERE id='Atlas-ready'").fetchone()[0]
        value = json.loads(raw)
        if failure == "snapshot_identity": value["account_fingerprint"] = "b"*64
        elif failure == "snapshot_holdings": value["held_shares"]["COST"] = 0
        elif failure == "snapshot_future": value["observed_at"] = "2099-01-01T00:00:00Z"
        mutate(sources[0], "UPDATE snapshots SET payload=? WHERE id='Atlas-ready'", (json.dumps(value),))
    elif failure == "latest_unready": mutate(sources[0], "UPDATE snapshots SET ready=0 WHERE id='Atlas-ready'")
    elif failure == "duplicate_evidence": mutate(sources[1], "UPDATE evidence SET id='Atlas-fill-evidence' WHERE id='Scout-fill-evidence'")
    elif failure == "duplicate_broker_order": mutate(sources[1], "UPDATE reservations SET broker_order='Atlas-broker'")
    elif failure == "extra_table": mutate(sources[0], "CREATE TABLE unrelated_private_state (value TEXT)")
    elif failure == "extra_column": mutate(sources[0], "ALTER TABLE allocations ADD COLUMN new_owner TEXT")
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        run(tmp_path, sources)
    assert not (tmp_path/"candidate").exists()


def test_optional_assignment_and_release_audit_rows_are_preserved(tmp_path):
    sources, ledgers = setup(tmp_path)
    order = ledgers[0].reserve_direction_exit(symbol="COST", horizon="4h", forecast_id="manual-stock-sale",
        target_start=START, target_end="2026-10-02T15:00:00+00:00", quantity=1, limit_price=100,
        snapshot_id="Atlas-ready", idempotency_key="assigned-sale", batch_id="assigned-sale",
        as_of="2026-10-02T11:00:22+00:00")
    ledgers[0].mark_submission(order.reservation_id,status="REJECTED",observed_at="2026-10-02T11:00:23+00:00",
                              evidence_id="rejected-assigned-sale")
    sources[0]["pins"] = migration.pin_ledger_files(sources[0]["path"])
    result = run(tmp_path,sources)
    assert result["row_counts"]["inventory_assignments"] == 1
    assert result["row_counts"]["inventory_assignment_releases"] == 1
    assert result["row_counts"]["reservations"] == 3


def test_conflicting_daily_fallback_baselines_cannot_be_combined_or_reset(tmp_path):
    sources, ledgers = setup(tmp_path)
    for item, ledger in zip(sources,ledgers):
        ledger.freeze_fallback_day(action_date="2026-10-02",policy=policy_for_action_date("2026-10-02"),
            source_fingerprint="c"*64,snapshot_id=item["producer"]+"-ready",as_of="2026-10-02T11:00:22+00:00")
        item["pins"] = migration.pin_ledger_files(item["path"])
    with pytest.raises(ValueError,match="daily-baseline collision"):
        run(tmp_path,sources)
    assert not (tmp_path/"candidate").exists()


def freeze_sources(sources, ledgers):
    for item, ledger in zip(sources, ledgers):
        ledger.freeze_fallback_day(action_date="2026-10-02", policy=policy_for_action_date("2026-10-02"),
            source_fingerprint=hashlib.sha256(item["producer"].encode()).hexdigest(),
            snapshot_id=item["producer"]+"-ready", as_of="2026-10-02T11:00:22+00:00")
        item["pins"] = migration.pin_ledger_files(item["path"])


def test_explicit_future_cutover_preserves_conflicting_past_days_in_exact_source_archives(tmp_path):
    sources, ledgers = setup(tmp_path)
    freeze_sources(sources, ledgers)
    result = run(tmp_path, sources, cutover_action_date="2026-10-05")
    assert result["status"] == "REQUIRES_REVIEWED_ACTIVATION"
    assert result["cutover_action_date"] == "2026-10-05"
    collisions = result["historical_fallback_collisions"]
    assert len(collisions) == 1 and collisions[0]["action_date"] == "2026-10-02"
    assert {row["producer"] for row in collisions[0]["originals"]} == {"Atlas", "Scout"}
    assert len({row["baseline_id"] for row in collisions[0]["originals"]}) == 2
    with sqlite3.connect(tmp_path/"candidate/holdings.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM fallback_days").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 2
    for reference in result["source_archives"]:
        archive_path = tmp_path/"candidate"/reference["path"]
        assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == reference["sha256"]
        original = next(item for item in sources if item["producer"] == reference["producer"])
        assert reference["original_pins"] == original["pins"]
        assert reference["original_rows_verified"] is True
        with sqlite3.connect(original["path"]) as before, sqlite3.connect(archive_path) as after:
            assert before.execute("SELECT * FROM fallback_days").fetchall() == after.execute("SELECT * FROM fallback_days").fetchall()
            for table in ("allocations", "reservations", "fills", "evidence", "snapshots"):
                assert before.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == after.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        assert migration.pin_ledger_files(original["path"]) == original["pins"]


@pytest.mark.parametrize("cutover", ["2026-10-01", "2026-10-02", "20261005", "not-a-date", 20261005])
def test_past_collision_exception_cannot_reset_current_or_future_daily_budget(tmp_path, cutover):
    sources, ledgers = setup(tmp_path)
    freeze_sources(sources, ledgers)
    with pytest.raises(ValueError):
        run(tmp_path, sources, cutover_action_date=cutover)
    assert not (tmp_path/"candidate").exists()


def test_nonconflicting_past_daily_baseline_remains_native_and_archived(tmp_path):
    sources, ledgers = setup(tmp_path)
    freeze_sources(sources[:1], ledgers[:1])
    result = run(tmp_path, sources, cutover_action_date="2026-10-05")
    assert result["historical_fallback_collisions"] == []
    assert result["row_counts"]["fallback_days"] == 1
    with sqlite3.connect(tmp_path/"candidate/holdings.sqlite3") as db:
        assert db.execute("SELECT action_date FROM fallback_days").fetchone()[0] == "2026-10-02"


def test_source_write_during_build_is_detected_and_candidate_never_published(tmp_path, monkeypatch):
    sources, _ = setup(tmp_path)
    original = migration.HorizonLedger
    def create(path, account):
        result = original(path, account)
        with sqlite3.connect(sources[0]["path"]) as db:
            db.execute("INSERT INTO evidence VALUES ('concurrent','test','{}')")
        return result
    monkeypatch.setattr(migration,"HorizonLedger",type("Factory",(),{"__new__":staticmethod(lambda cls,*a: create(*a)),
        "_snapshot":original._snapshot}))
    with pytest.raises(ValueError,match="pins changed"):
        run(tmp_path,sources)
    assert not (tmp_path/"candidate").exists()


def test_existing_destination_is_untouched(tmp_path):
    sources, _ = setup(tmp_path)
    target = tmp_path/"candidate"
    target.mkdir()
    (target/"keep.txt").write_text("original")
    with pytest.raises(ValueError,match="new directory"):
        run(tmp_path,sources)
    assert (target/"keep.txt").read_text()=="original" and len(list(target.iterdir()))==1


def test_existing_wal_snapshot_is_read_without_checkpoint_or_source_write(tmp_path):
    sources, _ = setup(tmp_path)
    writer=sqlite3.connect(sources[0]["path"])
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]=='wal'
        initial_count = writer.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        writer.execute("INSERT INTO evidence VALUES ('wal-only','test','{}')")
        writer.commit()
        sources[0]["pins"]=migration.pin_ledger_files(sources[0]["path"])
        assert sources[0]["pins"]["wal_sha256"] is not None
        result=run(tmp_path,sources)
        assert result["sources"][0]["row_counts"]["evidence"]==initial_count+1
        assert migration.pin_ledger_files(sources[0]["path"])==sources[0]["pins"]
        with sqlite3.connect(tmp_path/"candidate/holdings.sqlite3") as merged:
            assert merged.execute("SELECT kind FROM evidence WHERE id='wal-only'").fetchone()[0]=='test'
    finally:
        writer.close()


def test_wal_without_existing_shm_is_refused_before_open(tmp_path):
    sources,_=setup(tmp_path)
    with open(sources[0]["path"]+'-wal','xb') as handle: handle.write(b'incomplete')
    with pytest.raises(ValueError,match="shared-memory"):
        migration.pin_ledger_files(sources[0]["path"])
