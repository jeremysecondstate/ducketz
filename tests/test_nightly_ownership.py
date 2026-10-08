"""Read real temporary native ledgers; never acquire production account data."""
import builtins
from contextlib import closing
from copy import deepcopy
import gc
import json
from pathlib import Path
import runpy
import sqlite3

import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml import nightly_workflow
from tools import nightly_ownership as module
from tests.test_gameplan_trade_snapshot import ledger

NOW, SCOPE = "2026-10-08T10:00:00Z", "a" * 64
ATLAS = ["AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ"]
SCOUT = ["DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN"]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def setup_owner(tmp_path, actor):
    repository, root = tmp_path / (actor + "-checkout"), tmp_path / (actor + "-data")
    symbols = ATLAS if actor == "Atlas" else SCOUT
    watchlist = repository / "datafetching/watchlist.local.txt"
    watchlist.parent.mkdir(parents=True)
    watchlist.write_text("\n".join(symbols) + "\n")
    profile = repository / "scratch/cross-pc/local-profile.json"
    write(profile, {"actor": actor, "machine": "pc-original" if actor == "Atlas" else "pc-new",
        "contract_version": "cross-pc-v2", "checkout": str(repository),
        "symbol_profile_path": str(watchlist), "symbols": symbols})
    release = repository / "scratch/cross-pc/releases/fixture"
    write(release / "coordination/contract.json", {"contract_version": "cross-pc-v2"})
    write(release / "installation.json", {"commit": "c" * 40, "files": {
        "coordination/contract.json": file_checksum(release / "coordination/contract.json")}})
    active = repository / "scratch/cross-pc/active.json"
    write(active, {"release_root": str(release), "commit": "c" * 40,
                   "manifest_sha256": file_checksum(release / "installation.json")})
    native = repository / "scratch/nightly-workflow/config.json"
    write(native, {"schema_version": nightly_workflow.VERSION, "actor": actor,
        "repository": str(repository), "datastore": str(root), "state_root": str(repository / "scratch/nightly-workflow"),
        "local_profile": str(profile), "coordination_active": str(active), "peer_communication_enabled": False,
        "reviewer": {"model": "fixture", "reasoning_effort": "high"}})
    probe = repository / "tools/nightly_ownership.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("# isolated exporter identity\n")
    config = {"actor": actor, "private_exchange_authorized": True, "account_scope_sha256": SCOPE,
        "owners": {"atlas": list(ATLAS), "scout": list(SCOUT)}, "workflow_config": str(native),
        "local_profile": str(profile), "coordination_active": str(active)}
    path = ledger(root)
    gc.collect()  # The legacy synthetic fixture leaves its connection to GC.
    held = dict.fromkeys(symbols, 0); held[symbols[0]] = 2
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE allocations SET id=?,symbol=?", (actor + "-SECRET_NATIVE_ID", symbols[0]))
        db.execute("UPDATE reservations SET allocation=?", (actor + "-SECRET_NATIVE_ID",))
        db.execute("UPDATE snapshots SET payload=?,owned=?", (json.dumps({"held_shares": held}), json.dumps({symbols[0]: 1})))
    return config, root, probe


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKETS_PRODUCTION_WATCHLIST", raising=False)
    config, root, probe = setup_owner(tmp_path, "Scout")
    monkeypatch.setattr(module, "__file__", str(probe))
    monkeypatch.setattr(module.nightly_workflow, "source_identity", lambda _: {"commit": "f" * 40, "source_sha256": "e" * 64})
    return config, root


def test_real_scout_ledger_preserves_original_baseline_and_private_bytes(configured, tmp_path):
    config, root = configured
    before = files(tmp_path)
    value = module.capture_ownership(config, now=NOW)
    assert files(tmp_path) == before
    assert value["ledger_observed_at"] == pd.Timestamp(NOW).isoformat()
    assert value["saved_baseline_at"] == "2026-09-09T04:00:00Z"
    assert value["held_basis"] == "SAVED_READY_RECONCILIATION"
    record = module.validate_observation(config, value, observed_at=NOW, actor="scout")
    assert record["held_shares"]["DOCU"] == 2 and record["ownership"]["owned_shares"]["DOCU"] == 1
    assert set(record["ownership"]["owned_shares"]) == set(SCOUT)
    assert len(record["ownership"]["active_allocations"]) == 1
    assert record["ownership"]["current_broker_reconciliation_performed"] is False
    assert "SECRET" not in json.dumps(value) and str(root) not in json.dumps(value)


@pytest.mark.parametrize("damage", ["missing", "missing_symbol", "unready", "account", "pending", "block", "bad_held", "bad_time"])
def test_missing_or_unsafe_native_state_cannot_become_empty_ownership(configured, tmp_path, damage):
    config, root = configured
    path = root / module.LEDGER
    if damage == "missing": path.unlink()
    else:
        with closing(sqlite3.connect(path)) as db, db:
            if damage == "missing_symbol": db.execute("UPDATE snapshots SET payload=?", (json.dumps({"held_shares": {"DOCU": 2}}),))
            elif damage == "unready": db.execute("UPDATE snapshots SET ready=0")
            elif damage == "account": db.execute("UPDATE metadata SET value=? WHERE key='account'", ("b" * 64,))
            elif damage == "pending": db.execute("UPDATE reservations SET status='UNKNOWN'")
            elif damage == "block": db.execute("INSERT INTO blocks VALUES ('DOCU','SECRET')")
            elif damage == "bad_held": db.execute("UPDATE snapshots SET payload=?", (json.dumps({"held_shares": dict.fromkeys(SCOUT, None)}),))
            else: db.execute("UPDATE snapshots SET observed_at='2030-01-01T00:00:00Z'")
    before = files(tmp_path)
    with pytest.raises(ValueError): module.capture_ownership(config, now=NOW)
    assert files(tmp_path) == before


@pytest.mark.parametrize("damage", ["permission", "actor", "scope", "profile", "overlap"])
def test_export_binding_failures_precede_ledger_reads(configured, monkeypatch, damage):
    config, _ = configured
    if damage == "permission": config["private_exchange_authorized"] = False
    elif damage == "actor": config["actor"] = "Atlas"
    elif damage == "scope": config["account_scope_sha256"] = "not-a-hash"
    elif damage == "profile":
        path = Path(config["local_profile"]); value = json.loads(path.read_text()); value["machine"] = "pc-original"; write(path, value)
    else: config["owners"]["atlas"][0] = "DOCU"
    monkeypatch.setattr(module, "_pins", lambda _: pytest.fail("Unbound ledger read"))
    with pytest.raises(ValueError): module.capture_ownership(config, now=NOW)


def test_current_wal_is_observed_without_changing_original_database_or_sidecars(configured, tmp_path):
    config, root = configured
    db = sqlite3.connect(root / module.LEDGER)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE reservations SET quantity=2,filled=2"); db.commit()
        before = files(tmp_path)
        # The increased native fill exceeds this unchanged saved expectation;
        # it cannot be hidden by reading only the database without its WAL.
        with pytest.raises(ValueError): module.capture_ownership(config, now=NOW)
        assert files(tmp_path) == before
        db.execute("UPDATE reservations SET quantity=1,filled=1"); db.commit()
        before = files(tmp_path)
        assert module.capture_ownership(config, now=NOW)["envelope"]["ownership"]["safe_for_planning"]
        assert files(tmp_path) == before
    finally: db.close()


@pytest.mark.parametrize("mutation", ["database", "profile", "source"])
def test_concurrent_state_change_is_rejected(configured, monkeypatch, mutation):
    config, root = configured
    original = module._ownership
    def changed(*args):
        result = original(*args)
        if mutation == "database":
            with closing(sqlite3.connect(root / module.LEDGER)) as db, db: db.execute("UPDATE reservations SET price='101'")
        elif mutation == "profile":
            path = Path(config["local_profile"]); path.write_bytes(path.read_bytes() + b" ")
        else: monkeypatch.setattr(module.nightly_workflow, "source_identity", lambda _: {"changed": True})
        return result
    monkeypatch.setattr(module, "_ownership", changed)
    with pytest.raises(ValueError, match="changed"): module.capture_ownership(config, now=NOW)


@pytest.mark.parametrize("damage", ["stale", "future", "private_field", "baseline", "fingerprint", "duplicate", "scope"])
def test_transport_observations_preserve_exact_fresh_provenance(configured, damage):
    config, _ = configured
    value = module.capture_ownership(config, now=NOW)
    if damage == "stale": stamp = pd.Timestamp(NOW) + pd.Timedelta(seconds=61)
    else: stamp = NOW
    if damage == "future": value["ledger_observed_at"] = "2030-01-01T00:00:00Z"
    elif damage == "private_field":
        value["envelope"]["ownership"]["raw_database"] = "SECRET"
        value["envelope"]["source_fingerprint"] = module._fingerprint(value["envelope"])
    elif damage == "baseline": value["saved_baseline_at"] = NOW
    elif damage == "fingerprint": value["envelope"]["source_fingerprint"] = "b" * 64
    elif damage == "scope": value["envelope"]["account_fingerprint"] = "b" * 64
    elif damage == "duplicate":
        with pytest.raises(ValueError): module.validate_observations(config, [value, value], observed_at=NOW)
        return
    with pytest.raises(ValueError): module.validate_observation(config, value, observed_at=stamp)


def test_scout_capture_does_not_import_atlas_or_call_broker(configured, monkeypatch):
    original = builtins.__import__
    def checked(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("ml.account_gameplan") or (name == "ml.stock_trader.state" and "validated_symbols" in fromlist):
            pytest.fail("Atlas-only import")
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", checked)
    monkeypatch.setattr("ml.gameplan_trade_snapshot.SchwabSession", lambda: pytest.fail("Broker read"))
    assert module.capture_ownership(configured[0], now=NOW)["actor"] == "Scout"
