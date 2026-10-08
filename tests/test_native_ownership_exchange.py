"""Private transport regressions: synthetic ledgers only, no broker calls."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import os

import pytest
from filelock import FileLock, Timeout

from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from ml.account_gameplan.migration import pin_ledger_files
from tools import native_ownership_exchange as exchange
from tools.native_ownership_export import export_ownership, read_export

NOW = datetime.fromisoformat("2026-10-08T10:00:00+00:00")
ACCOUNT = "a" * 64


@pytest.fixture(autouse=True)
def synthetic_binding_inputs(monkeypatch):
    # Tests never read the checkout's private profiles, credentials or workflow.
    monkeypatch.setattr(exchange, "_binding_inputs", lambda config: {
        "profile": Path(config["local_profile"]), "active": Path(config["coordination_active"])})


def account(actor="Atlas"):
    return SimpleNamespace(machine_id=exchange.ACTORS[actor], fingerprint="b" * 64,
        account_fingerprint=ACCOUNT, activation={"status": "PREPARING"},
        participants={"pc-original": ("AAA",), "pc-new": ("BBB",)})


def configuration(tmp_path, actor="Atlas"):
    root = tmp_path / actor
    (root / "state/account-gameplan").mkdir(parents=True)
    (root / "state/account-gameplan/config.json").write_text("{}")
    (root / "profile.json").write_text("{}")
    (root / "active.json").write_text("{}")
    return {"schema_version": exchange.VERSION, "actor": actor,
        "datastore_root": str(root), "local_profile": str(root / "profile.json"),
        "coordination_active": str(root / "active.json"),
        "exchange_root": str(tmp_path / "CODEXSTORE/ducketz-nightly-exchange/v1"),
        "state_root": str(root / "private-state"), "operation_id": "test-cutover-20261008",
        "cutover_action_date": "2026-10-08", "private_native_ledger_exchange_authorized": True}


def ledger(config):
    actor = config["actor"]
    symbol = account(actor).participants[exchange.ACTORS[actor]][0]
    root = Path(config["datastore_root"])
    db = HorizonLedger(root / "state/independent-stock-trader/holdings.sqlite3", ACCOUNT)
    state = PortfolioEvidence(actor + "-snapshot", ACCOUNT, "2026-10-07T23:00:00+00:00",
                              {symbol: 2}, {symbol: 100}, {symbol: 0}, "c" * 64)
    assert db.reconcile(state).ready
    return db


def packet(tmp_path, config):
    db = ledger(config)
    output = tmp_path / (config["actor"] + "-export")
    backup = tmp_path / (config["actor"] + "-backup")
    export_ownership(source=db.path, pins=pin_ledger_files(db.path), account_fingerprint=ACCOUNT,
        producer=exchange.ACTORS[config["actor"]], symbols=account(config["actor"]).participants[exchange.ACTORS[config["actor"]]],
        output_directory=output, backup_directory=backup, clock=lambda: NOW, forbidden_values=[])
    return db, output


def verified(config, output):
    return read_export(output, expected_producer=exchange.ACTORS[config["actor"]],
        expected_symbols=account(config["actor"]).participants[exchange.ACTORS[config["actor"]]],
        expected_account_fingerprint=ACCOUNT, forbidden_values=[],
        expected_manifest_sha256=exchange._sha((output / "manifest.json").read_bytes()))


def publish(config, output):
    return exchange._publish(config, account(), output, verified_source=verified(config, output), forbidden_values=[])


def test_receive_partial_arrival_is_pending_and_modified_bytes_fail(tmp_path):
    cfg = configuration(tmp_path, "Scout")
    _, output = packet(tmp_path, cfg)
    selected, digest = publish(cfg, output)
    root = exchange._folder(cfg, "Scout") / "packets" / digest
    target = root / "holdings.sqlite3"
    raw = target.read_bytes()
    target.write_bytes(raw[:30])
    with pytest.raises(exchange.Pending, match="INCOMPLETE"):
        exchange._receive(cfg, "Scout", account(), tmp_path / "receiver")
    target.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(ValueError, match="changed"):
        exchange._receive(cfg, "Scout", account(), tmp_path / "receiver")
    target.write_bytes(raw)
    record, received = exchange._receive(cfg, "Scout", account(), tmp_path / "receiver")
    assert received == digest and record["producer"] == "pc-new"
    assert pin_ledger_files(Path(record["path"])) == record["pins"]


@pytest.mark.parametrize("field,value", [("actor", "Atlas"), ("operation_id", "different-operation"),
    ("cutover_action_date", "2026-10-09"), ("symbols", ["AAA"]), ("account_fingerprint", "d" * 64)])
def test_received_identity_cannot_change(tmp_path, field, value):
    cfg = configuration(tmp_path, "Scout")
    _, output = packet(tmp_path, cfg)
    selected, _ = publish(cfg, output)
    selected[field] = value
    (exchange._folder(cfg, "Scout") / "selection.json").write_bytes(exchange._bytes(selected))
    with pytest.raises(ValueError, match="identity differs"):
        exchange._receive(cfg, "Scout", account(), tmp_path / "receiver")


def test_one_wake_exports_once_and_stages_union_after_peer_arrival(tmp_path, monkeypatch):
    atlas = configuration(tmp_path)
    scout = configuration(tmp_path, "Scout")
    atlas_db = ledger(atlas)
    monkeypatch.setattr(exchange, "load_account_config", lambda root: account())
    monkeypatch.setattr(exchange, "_private_values", lambda config: [b"secret-should-not-appear"])
    source_bytes = atlas_db.path.read_bytes()
    first = exchange.run(atlas, clock=lambda: NOW)
    assert first["status"] == "PENDING" and first["reason"] == "NATIVE_SCOUT_LEDGER_EXPORT"
    selector = Path(atlas["state_root"]) / atlas["operation_id"] / "atlas-selection.json"
    assert not exchange._folder(atlas, "Atlas").exists()
    original = selector.read_bytes()
    _, peer_packet = packet(tmp_path, scout)
    publish(scout, peer_packet)
    completed = exchange.run(atlas, clock=lambda: NOW)
    assert completed["status"] == "CUTOVER_RECONCILIATION_REQUIRED"
    assert completed["activation_changed"] is False and completed["broker_calls"] == 0
    assert Path(completed["migration_candidate"]).is_dir()
    retried = exchange.run(atlas, clock=lambda: NOW)
    assert retried == completed and selector.read_bytes() == original
    assert atlas_db.path.read_bytes() == source_bytes


def test_missing_authorization_never_exports(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    cfg["private_native_ledger_exchange_authorized"] = False
    monkeypatch.setattr(exchange, "load_account_config", lambda root: account())
    with pytest.raises(ValueError, match="not locally authorized"):
        exchange.run(cfg, clock=lambda: NOW)
    assert not Path(cfg["exchange_root"]).exists()


def test_changed_source_after_first_export_is_not_republished(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    db = ledger(cfg)
    monkeypatch.setattr(exchange, "load_account_config", lambda root: account())
    monkeypatch.setattr(exchange, "_private_values", lambda config: [])
    assert exchange.run(cfg, clock=lambda: NOW)["status"] == "PENDING"
    selector = Path(cfg["state_root"]) / cfg["operation_id"] / "atlas-selection.json"
    original = selector.read_bytes()
    db.reconcile(PortfolioEvidence("later", ACCOUNT, "2026-10-07T23:01:00+00:00",
                                   {"AAA": 2}, {"AAA": 100}, {"AAA": 0}, "c" * 64))
    with pytest.raises(ValueError, match="changed after export"):
        exchange.run(cfg, clock=lambda: NOW)
    assert selector.read_bytes() == original


def test_changed_profile_keeps_original_operation_binding(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    ledger(cfg)
    monkeypatch.setattr(exchange, "load_account_config", lambda root: account())
    monkeypatch.setattr(exchange, "_private_values", lambda config: [])
    exchange.run(cfg, clock=lambda: NOW)
    Path(cfg["local_profile"]).write_text('{"changed":true}')
    with pytest.raises(ValueError, match="Frozen native accounting"):
        exchange.run(cfg, clock=lambda: NOW)


def test_atlas_native_database_is_never_published(tmp_path):
    cfg = configuration(tmp_path)
    _, output = packet(tmp_path, cfg)
    with pytest.raises(ValueError, match="stays local"):
        publish(cfg, output)
    assert not Path(cfg["exchange_root"]).exists()


@pytest.mark.parametrize("change", ["bytes", "private_value", "invalid_marker"])
def test_outgoing_transport_uses_only_exact_validated_scanned_bytes(tmp_path, change):
    cfg = configuration(tmp_path, "Scout")
    _, output = packet(tmp_path, cfg)
    accepted = verified(cfg, output)
    forbidden = []
    if change == "bytes":
        with (output / "holdings.sqlite3").open("ab") as handle:
            handle.write(b"UNREVIEWED_BYTES")
    elif change == "private_value":
        forbidden = [b"Scout-snapshot"]
    else:
        (output / "INVALID.json").write_text("{}")
    with pytest.raises(ValueError):
        exchange._publish(cfg, account(), output, verified_source=accepted, forbidden_values=forbidden)
    assert not Path(cfg["exchange_root"]).exists()


@pytest.mark.parametrize("change", ["invalid_marker", "selection", "late_invalid"])
def test_receive_never_drops_remote_invalidation_or_changed_selection(tmp_path, monkeypatch, change):
    cfg = configuration(tmp_path, "Scout")
    _, output = packet(tmp_path, cfg)
    _, digest = publish(cfg, output)
    folder = exchange._folder(cfg, "Scout")
    remote = folder / "packets" / digest
    if change == "invalid_marker":
        (remote / "INVALID.json").write_text("{}")
    else:
        original = exchange.read_export
        def changes(*args, **kwargs):
            value = original(*args, **kwargs)
            if change == "late_invalid":
                (remote / "INVALID.json").write_text("{}")
            else:
                selected = folder / "selection.json"
                selected.write_bytes(selected.read_bytes() + b" ")
            return value
        monkeypatch.setattr(exchange, "read_export", changes)
    local = tmp_path / "receiver"
    with pytest.raises(ValueError):
        exchange._receive(cfg, "Scout", account(), local)
    assert not (local / "scout-selection.json").exists()


def test_immutable_write_does_not_clobber_a_concurrent_creator(tmp_path, monkeypatch):
    target = tmp_path / "selection.json"
    name = "rename" if os.name == "nt" else "link"
    original = getattr(exchange.os, name)
    def race(source, destination):
        Path(destination).write_bytes(b"OTHER_OWNER")
        return original(source, destination)
    monkeypatch.setattr(exchange.os, name, race)
    with pytest.raises(ValueError, match="Frozen"):
        exchange._write(target, b"THIS_OWNER")
    assert target.read_bytes() == b"OTHER_OWNER"


def test_busy_operation_never_replaces_the_owners_durable_status(tmp_path):
    cfg = configuration(tmp_path)
    local = Path(cfg["state_root"]) / cfg["operation_id"]
    local.mkdir(parents=True)
    status = local / "status.json"
    original = b'{"status":"OWNED_RESULT"}'
    status.write_bytes(original)
    with FileLock(str(local / "operation.lock"), timeout=0):
        result = exchange.run(cfg, clock=lambda: NOW)
    assert result["reason"] == "NATIVE_ACCOUNTING_OPERATION_LOCKED"
    assert status.read_bytes() == original


def test_status_and_failure_receipts_are_written_while_operation_is_owned(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    ledger(cfg)
    monkeypatch.setattr(exchange, "load_account_config", lambda root: account())
    monkeypatch.setattr(exchange, "_private_values", lambda config: [])
    local = Path(cfg["state_root"]) / cfg["operation_id"]
    original = exchange._write
    writes = []
    def checked(path, *args, **kwargs):
        if Path(path).name == "status.json" or Path(path).parent.name == "failures":
            with pytest.raises(Timeout):
                FileLock(str(local / "operation.lock"), timeout=0).acquire()
            writes.append(Path(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(exchange, "_write", checked)
    exchange.run(cfg, clock=lambda: NOW)
    Path(cfg["local_profile"]).write_text('{"different":true}')
    with pytest.raises(ValueError, match="Frozen"):
        exchange.run(cfg, clock=lambda: NOW)
    assert json.loads((local / "status.json").read_text())["status"] == "FAILED"
    assert len(list((local / "failures").glob("*.json"))) == 1
    assert len(writes) == 3


def test_mid_run_binding_change_blocks_before_any_selection(tmp_path, monkeypatch):
    cfg = configuration(tmp_path, "Scout")
    ledger(cfg)
    monkeypatch.setattr(exchange, "load_account_config", lambda root: account("Scout"))
    def changed(config):
        Path(config["local_profile"]).write_text('{"changed":true}')
        return []
    monkeypatch.setattr(exchange, "_private_values", changed)
    with pytest.raises(ValueError, match="bindings changed"):
        exchange.run(cfg, clock=lambda: NOW)
    assert not Path(cfg["exchange_root"]).exists()


def test_native_database_and_json_have_separate_bounded_limits(tmp_path, monkeypatch):
    cfg = configuration(tmp_path, "Scout")
    _, output = packet(tmp_path, cfg)
    size = (output / "holdings.sqlite3").stat().st_size
    assert size > (output / "manifest.json").stat().st_size
    monkeypatch.setattr(exchange, "MAX_BYTES", size)
    publish(cfg, output)
    exchange._receive(cfg, "Scout", account(), tmp_path / "receiver")
    monkeypatch.setattr(exchange, "MAX_JSON_BYTES", 8)
    with pytest.raises(ValueError, match="pin"):
        exchange._receive(cfg, "Scout", account(), tmp_path / "second-receiver")


@pytest.mark.parametrize("changed", [None, "exchange_root", "datastore_root"])
def test_configuration_uses_exact_existing_reviewed_roots(tmp_path, monkeypatch, changed):
    cfg = configuration(tmp_path)
    repository = tmp_path / "repository"
    coordination = repository / "scratch/cross-pc"
    coordination.mkdir(parents=True)
    cfg.update(local_profile=str(coordination / "local-profile.json"),
               coordination_active=str(coordination / "active.json"),
               state_root=str(repository / "scratch/native-ownership-exchange"))
    native = account()
    native.participants = {"pc-original": tuple("A" + str(n) for n in range(11)),
                           "pc-new": tuple("B" + str(n) for n in range(11))}
    Path(cfg["local_profile"]).write_text(json.dumps({"actor": "Atlas", "machine": "pc-original",
        "checkout": str(repository), "symbols": native.participants["pc-original"]}))
    Path(cfg["coordination_active"]).write_text("{}")
    monkeypatch.setattr(exchange, "_repository", lambda: repository)
    monkeypatch.setattr(exchange, "_verify_installation", lambda _: None)
    monkeypatch.setattr(exchange, "load_account_config", lambda _: native)
    reviewed = repository / "scratch/nightly-workflow"
    reviewed.mkdir()
    common = {"actor": "Atlas", "local_profile": cfg["local_profile"], "coordination_active": cfg["coordination_active"]}
    (reviewed / "config.json").write_text(json.dumps({**common, "repository": str(repository), "datastore": cfg["datastore_root"]}))
    (reviewed / "exchange-config.json").write_text(json.dumps({**common,
        "workflow_config": str(reviewed / "config.json"), "exchange_root": cfg["exchange_root"],
        "private_exchange_authorized": True, "account_scope_sha256": ACCOUNT,
        "owners": {actor.lower(): list(native.participants[machine]) for actor, machine in exchange.ACTORS.items()}}))
    if changed == "exchange_root":
        # Folder names alone must not authorize a public checkout destination.
        cfg[changed] = str(repository / "artifacts/CODEXSTORE/ducketz-nightly-exchange/v1")
    elif changed:
        cfg[changed] = str(tmp_path / "unreviewed-datastore")
    path = reviewed / "native-exchange.json"
    path.write_text(json.dumps(cfg))
    if changed:
        with pytest.raises(ValueError, match="existing reviewed nightly bindings"):
            exchange.load_config(path)
    else:
        assert exchange.load_config(path) == cfg
