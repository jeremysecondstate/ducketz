"""Native export tests use temporary native databases, never operating paths."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from ml.account_gameplan import migration
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence, OrderEvidence, FillEvidence
from ml.stock_trader.cross_horizon_fallback import policy_for_action_date
from tools import native_ownership_export as exporter


ACCOUNT = "a" * 64
START = "2026-10-02T11:00:00+00:00"
READY = "2026-10-02T11:00:21+00:00"
NOW = datetime.fromisoformat("2026-10-03T01:00:00+00:00")


def source(tmp_path):
    ledger = HorizonLedger(tmp_path / "source.sqlite3", ACCOUNT)
    def portfolio(identity, at, held):
        return PortfolioEvidence(identity, ACCOUNT, at, {"COST": held}, {"COST": 100}, {"COST": 10000}, "b" * 64)
    assert ledger.reconcile(portfolio("empty", START, 2)).ready
    reservation = ledger.reserve_entry(symbol="COST", horizon="1d", forecast_id="forecast",
        target_start=START, target_end="2026-10-09T00:00:00+00:00", quantity=3, limit_price=100,
        snapshot_id="empty", idempotency_key="entry", batch_id="batch", as_of="2026-10-02T11:00:10+00:00")
    evidence = OrderEvidence("fill-evidence", reservation.reservation_id, ACCOUNT, "2026-10-02T11:00:20+00:00",
        "broker-order", "FILLED", 3, 3, 0, (FillEvidence("fill", 3, 100, "2026-10-02T11:00:20+00:00"),))
    assert ledger.reconcile(portfolio("ready", READY, 5), order_evidence=(evidence,)).ready
    return ledger


def arguments(ledger):
    return {"source": ledger.path, "pins": migration.pin_ledger_files(ledger.path),
            "account_fingerprint": ACCOUNT, "producer": "pc-original", "symbols": ["COST"], "clock": lambda: NOW, "forbidden_values": ()}


def test_check_is_default_read_only_and_export_preserves_all_logical_rows(tmp_path):
    ledger = source(tmp_path)
    args = arguments(ledger)
    before = exporter._group(ledger.path)
    checked = exporter.export_ownership(**args)
    assert checked["status"] == "VERIFIED_FOR_PRIVATE_EXPORT"
    assert set(tmp_path.iterdir()) == {ledger.path}
    result = exporter.export_ownership(**args, output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
    assert result["status"] == "EXPORTED" and result["native_ids_preserved"]
    assert exporter._group(ledger.path) == before
    assert (tmp_path / "backup/holdings.sqlite3").read_bytes() == ledger.path.read_bytes()
    packet = tmp_path / "packet"
    assert set(p.name for p in packet.iterdir()) == {"holdings.sqlite3", "manifest.json"}
    assert str(ledger.path) not in (packet / "manifest.json").read_text()
    record = migration._read_source({"producer": "pc-original", "path": str(packet / "holdings.sqlite3"),
        "symbols": ["COST"], "pins": migration.pin_ledger_files(packet / "holdings.sqlite3")}, ACCOUNT, NOW)
    assert {k: len(v) for k, v in record["tables"].items()} == result["row_counts"]
    assert record["state"] == ledger.snapshot()
    received = exporter.read_export(packet, expected_producer="pc-original", expected_symbols=["COST"],
        expected_account_fingerprint=ACCOUNT, forbidden_values=(), expected_manifest_sha256=result["manifest_sha256"])
    assert received["path"] == str(packet / "holdings.sqlite3")
    assert received["source_pins"] == args["pins"]


@pytest.mark.parametrize("payload_kind", ["extra_request", "raw_reply", "unknown_kind", "raw_account", "credential_identity", "unknown_reason", "duplicate_key"])
def test_unknown_or_private_native_fields_refuse_without_publishing(tmp_path, payload_kind):
    ledger = source(tmp_path)
    with sqlite3.connect(ledger.path) as db:
        if payload_kind == "extra_request":
            row = db.execute("SELECT id,request FROM reservations").fetchone()
            value = json.loads(row[1]); value["account_number"] = "private-number"
            db.execute("UPDATE reservations SET request=? WHERE id=?", (json.dumps(value), row[0]))
        elif payload_kind == "raw_reply":
            row = db.execute("SELECT id,payload FROM evidence WHERE kind='order'").fetchone()
            value = json.loads(row[1]); value["raw_response"] = {"accountNumber": "private-number"}
            db.execute("UPDATE evidence SET payload=? WHERE id=?", (json.dumps(value), row[0]))
        elif payload_kind == "unknown_kind":
            db.execute("INSERT INTO evidence VALUES ('unknown','raw-broker-response','{}')")
        elif payload_kind == "raw_account":
            db.execute("UPDATE metadata SET value='private-number' WHERE key='account'")
        elif payload_kind == "credential_identity":
            db.execute("UPDATE evidence SET id='sk-proj-private' WHERE kind='order'")
        elif payload_kind == "unknown_reason":
            db.execute("UPDATE snapshots SET ready=0,reasons='[\"Private account number 1234\"]' WHERE id='empty'")
        else:
            db.execute("INSERT INTO evidence VALUES ('unknown','submission','{\"status\":\"FILLED\",\"status\":\"REJECTED\"}')")
    with pytest.raises((ValueError, sqlite3.Error)):
        exporter.export_ownership(**arguments(ledger), output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
    assert not (tmp_path / "packet").exists()


def test_fallback_and_inventory_assignment_evidence_round_trip(tmp_path):
    ledger = source(tmp_path)
    ledger.freeze_fallback_day(action_date="2026-10-02", policy=policy_for_action_date("2026-10-02"),
        source_fingerprint="c" * 64, snapshot_id="ready", as_of="2026-10-02T11:00:22+00:00")
    order = ledger.reserve_direction_exit(symbol="COST", horizon="4h", forecast_id="sale",
        target_start=START, target_end="2026-10-02T15:00:00+00:00", quantity=1, limit_price=100,
        snapshot_id="ready", idempotency_key="sale", batch_id="sale", as_of="2026-10-02T11:00:22+00:00")
    ledger.mark_submission(order.reservation_id, status="REJECTED", observed_at="2026-10-02T11:00:23+00:00", evidence_id="rejected")
    result = exporter.export_ownership(**arguments(ledger), output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
    assert result["row_counts"]["inventory_assignments"] == result["row_counts"]["inventory_assignment_releases"] == 1
    assert result["row_counts"]["fallback_days"] == 1


def test_source_changes_during_build_invalidate_publication(tmp_path, monkeypatch):
    ledger = source(tmp_path)
    build = exporter._build
    def changed(*args):
        build(*args)
        with sqlite3.connect(ledger.path) as db:
            db.execute("INSERT INTO evidence VALUES ('new','submission','{}')")
    monkeypatch.setattr(exporter, "_build", changed)
    with pytest.raises(ValueError, match="changed"):
        exporter.export_ownership(**arguments(ledger), output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
    assert not (tmp_path / "packet").exists()
    assert list(tmp_path.glob(".packet.building-*/INVALID.json"))


def test_raw_deleted_pages_not_exported(tmp_path):
    ledger = source(tmp_path)
    secret = "PRIVATE_REMNANT_DO_NOT_TRANSPORT_" * 100
    with sqlite3.connect(ledger.path) as db:
        db.execute("PRAGMA secure_delete=OFF")
        db.execute("CREATE TABLE old_private_data (payload TEXT)")
        db.execute("INSERT INTO old_private_data VALUES (?)", (secret,))
        db.commit()
        db.execute("DROP TABLE old_private_data")
    assert secret.encode() in ledger.path.read_bytes()
    exporter.export_ownership(**arguments(ledger), output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
    assert secret.encode() not in (tmp_path / "packet/holdings.sqlite3").read_bytes()
    assert secret.encode() in (tmp_path / "backup/holdings.sqlite3").read_bytes()


def test_pins_paths_and_size_fail_closed(tmp_path, monkeypatch):
    ledger = source(tmp_path)
    args = arguments(ledger)
    args["pins"] = {"database_sha256": "0" * 64, "wal_sha256": None}
    with pytest.raises(ValueError, match="pins"):
        exporter.export_ownership(**args)
    with pytest.raises(ValueError, match="separate"):
        exporter.export_ownership(**arguments(ledger), output_directory=tmp_path / "packet")
    with pytest.raises(ValueError, match="distinct"):
        exporter.export_ownership(**arguments(ledger), output_directory=tmp_path / "packet", backup_directory=tmp_path / "packet")
    monkeypatch.setattr(exporter, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="byte bound"):
        exporter.export_ownership(**arguments(ledger))


def test_wal_source_keeps_exact_original_group_and_exports_committed_rows(tmp_path):
    ledger = source(tmp_path)
    db = sqlite3.connect(ledger.path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("UPDATE snapshots SET observed_at=observed_at")
        db.commit()
        before = exporter._group(ledger.path)
        assert before["-wal"] is not None
        result = exporter.export_ownership(**arguments(ledger), output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
        assert exporter._group(ledger.path) == before
        assert exporter._group(tmp_path / "backup/holdings.sqlite3") == before
        exporter.read_export(tmp_path / "packet", expected_producer="pc-original", expected_symbols=["COST"],
            expected_account_fingerprint=ACCOUNT, forbidden_values=(), expected_manifest_sha256=result["manifest_sha256"])
    finally:
        db.close()


@pytest.mark.parametrize("mutation", ["manifest", "binding", "extra_file", "raw_storage"])
def test_receiver_rejects_changed_or_unreviewed_packets(tmp_path, mutation):
    ledger = source(tmp_path)
    packet = tmp_path / "packet"
    result = exporter.export_ownership(**arguments(ledger), output_directory=packet, backup_directory=tmp_path / "backup")
    expected = result["manifest_sha256"]
    if mutation == "manifest":
        (packet / "manifest.json").write_text("{}")
    elif mutation == "extra_file":
        (packet / "INVALID.json").write_text("{}")
    elif mutation == "raw_storage":
        with sqlite3.connect(packet / "holdings.sqlite3") as db:
            db.execute("CREATE TABLE deleted_private (value TEXT)")
            db.execute("INSERT INTO deleted_private VALUES ('PRIVATE_REMNANT')")
            db.commit()
            db.execute("DROP TABLE deleted_private")
        manifest = json.loads((packet / "manifest.json").read_text())
        manifest["database_sha256"] = hashlib.sha256((packet / "holdings.sqlite3").read_bytes()).hexdigest()
        raw = exporter._encoded(manifest); (packet / "manifest.json").write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        exporter.read_export(packet, expected_producer="pc-new" if mutation == "binding" else "pc-original",
            expected_symbols=["COST"], expected_account_fingerprint=ACCOUNT, forbidden_values=(), expected_manifest_sha256=expected)


def test_receiver_detects_invalidation_during_verification(tmp_path, monkeypatch):
    ledger = source(tmp_path)
    packet = tmp_path / "packet"
    result = exporter.export_ownership(**arguments(ledger), output_directory=packet, backup_directory=tmp_path / "backup")
    build = exporter._build
    def invalidate(*args):
        build(*args)
        (packet / "INVALID.json").write_text("{}")
    monkeypatch.setattr(exporter, "_build", invalidate)
    with pytest.raises(ValueError, match="changed"):
        exporter.read_export(packet, expected_producer="pc-original", expected_symbols=["COST"],
            expected_account_fingerprint=ACCOUNT, forbidden_values=(), expected_manifest_sha256=result["manifest_sha256"])


def test_native_forecast_route_identity_is_allowed_but_email_is_not():
    exporter._identity("2026-10-02:COST:1h@04:00")
    exporter._identity("2026-10-02:MU:1w@T+5")
    exporter._identity("2026-10-02:MU:1w@D+5")
    with pytest.raises(ValueError):
        exporter._identity("private@example.com")


@pytest.mark.parametrize("escaped", [False, True])
def test_exact_private_values_cannot_hide_in_native_identity_or_json(tmp_path, escaped):
    ledger = source(tmp_path)
    value = "PRIVATELOCALVALUE"
    with sqlite3.connect(ledger.path) as db:
        row = db.execute("SELECT id,request FROM reservations").fetchone()
        request = json.loads(row[1]); request["forecast"] = value
        raw = json.dumps(request)
        if escaped:
            raw = raw.replace(value, "".join("\\u%04x" % ord(c) for c in value))
        db.execute("UPDATE reservations SET request=? WHERE id=?", (raw, row[0]))
    args = arguments(ledger); args["forbidden_values"] = (value.encode(),)
    with pytest.raises(ValueError, match="Private value"):
        exporter.export_ownership(**args, output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
    assert not (tmp_path / "packet").exists()


def test_private_scan_input_is_required_for_publication_and_receive(tmp_path):
    ledger = source(tmp_path)
    args = arguments(ledger); args.pop("forbidden_values")
    with pytest.raises(ValueError, match="private-value scan"):
        exporter.export_ownership(**args, output_directory=tmp_path / "packet", backup_directory=tmp_path / "backup")
