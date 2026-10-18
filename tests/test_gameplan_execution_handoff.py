"""Private display transfer uses saved JSON only, never a trader's ledger."""
import base64
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from filelock import FileLock
import pytest

from ml import gameplan_execution_handoff as handoff
from ml.gameplan_execution_report import _base, publish_execution_summary, LEDGER_RELATIVE_PATH
from app.ui.gameplan_execution_data import load_gameplan_execution
from tests.test_gameplan_execution_report import ledger, add_order

SESSION = "2026-10-14"
NOW = "2026-10-15T00:00:00+00:00"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def bound(tmp_path):
    exchange = tmp_path / "CODEXSTORE/ducketz-nightly-exchange/v1"
    exchange.mkdir(parents=True)
    result = {"exchange": exchange}
    for actor in ("Atlas", "Scout"):
        checkout = tmp_path / actor / "checkout"
        root = tmp_path / actor / "datastore"
        profile = write(checkout / "scratch/cross-pc/local-profile.json",
                        {"actor": actor, "machine": "pc-original" if actor == "Atlas" else "pc-new", "checkout": str(checkout)})
        workflow = write(checkout / "scratch/nightly-workflow/config.json",
                         {"actor": actor, "datastore": str(root), "repository": str(checkout)})
        (checkout / ".env").write_text("API_TOKEN=private-fixture-secret\n", encoding="utf-8")
        config = write(checkout / "scratch/nightly-workflow/exchange-config.json",
                       {"schema_version": "nightly-exchange-config-v1", "actor": actor,
                        "workflow_config": str(workflow), "local_profile": str(profile),
                        "exchange_root": str(exchange), "account_scope_sha256": "a" * 64,
                        "private_exchange_authorized": True})
        result[actor.lower()] = {"root": root, "checkout": checkout, "config": config}
    return result


def saved(generated=NOW):
    report, _, _ = _base(SESSION, datetime.fromisoformat(generated))
    report.update(state="available", message="Saved execution evidence.", last_broker_observation_at="2026-10-14T23:00:00+00:00",
                  counts={"submitted_attempts": 1, "fully_filled": 0, "partially_filled": 0, "working": 0,
                          "cancelled": 1, "rejected": 0, "unknown": 0, "not_submitted": 0,
                          "unknown_submission": 0, "carryover_attempts": 0},
                  orders=[{"order_ref": "1" * 64, "allocation_ref": "2" * 64, "forecast_ref": "3" * 64,
                           "symbol": "AAPL", "side": "BUY", "requested_quantity": 10, "filled_quantity": 4,
                           "average_fill_price": "123.45", "status": "cancelled", "lifecycle_status": "CANCELLED",
                           "submitted_at": "2026-10-14T16:30:00+00:00", "last_observed_at": "2026-10-14T23:00:00+00:00",
                           "fills": [{"quantity": 4, "price": "123.45", "executed_at": "2026-10-14T16:31:00+00:00"}],
                           "reasons": ["Remaining shares cancelled"], "retry_parent_ref": None, "retry_child_refs": [],
                           "session_scope": "submitted", "submission_state": "submitted"}])
    return report


def summary(bound, actor="atlas", report=None):
    path = bound[actor]["root"] / handoff.SUMMARY / SESSION / "summary.json"
    write(path, saved() if report is None else report)
    return path


def sync(bound, actor, **kwargs):
    return handoff.sync_execution_summary(bound[actor]["root"], SESSION, config_path=bound[actor]["config"], **kwargs)


def folder(bound):
    return bound["exchange"] / handoff.DISPLAY / SESSION / "atlas"


def mutate_packet(bound, change):
    selected = folder(bound) / "selection.json"
    selector = json.loads(selected.read_bytes())
    packet = json.loads((folder(bound) / "packets" / (selector["packet_sha256"] + ".json")).read_bytes())
    change(packet)
    raw = handoff._bytes(packet)
    digest = handoff._sha(raw)
    (folder(bound) / "packets" / (digest + ".json")).write_bytes(raw)
    selected.write_bytes(handoff._bytes({"schema_version": handoff.VERSION, "packet_sha256": digest, "packet_bytes": len(raw)}))


def test_round_trip_preserves_exact_bytes_details_times_and_original(bound, monkeypatch):
    source = summary(bound)
    # Source formatting is not canonicalized during export or import.
    source.write_bytes(json.dumps(saved(), indent=1).replace("\n", "\r\n").encode() + b"\r\n")
    placeholder, _, _ = _base(SESSION, datetime.fromisoformat("2026-10-16T00:00:00+00:00"))
    destination = summary(bound, "scout", placeholder)
    original = destination.read_bytes()
    assert sync(bound, "atlas")["status"] == "PUBLISHED"
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("Display exchange cannot open a ledger"))
    assert sync(bound, "scout")["status"] == "IMPORTED"
    assert destination.read_bytes() == source.read_bytes()
    assert not (bound["scout"]["root"] / LEDGER_RELATIVE_PATH).exists()
    history = bound["scout"]["root"] / handoff.STATE / SESSION / "history"
    assert (history / (handoff._sha(original) + ".json")).read_bytes() == original
    receipt = json.loads((history.parent / "receipt.json").read_bytes())
    assert receipt["generated_at"] == NOW
    assert receipt["last_broker_observation_at"] == saved()["last_broker_observation_at"]
    snapshot = load_gameplan_execution(bound["scout"]["root"], SESSION, now=NOW)
    assert snapshot.counts["cancelled"] == 1
    assert snapshot.orders[0]["fills"] == saved()["orders"][0]["fills"]
    assert snapshot.orders[0]["reasons"] == ["Remaining shares cancelled"]


def test_same_report_is_noop_and_new_version_preserves_history(bound, monkeypatch):
    source = summary(bound)
    assert sync(bound, "atlas")["status"] == "PUBLISHED"
    assert sync(bound, "scout")["status"] == "IMPORTED"
    first = source.read_bytes()
    writer = handoff._write
    monkeypatch.setattr(handoff, "_write", lambda *a, **k: pytest.fail("Unchanged report cannot write data"))
    assert sync(bound, "atlas")["status"] == "UNCHANGED"
    assert sync(bound, "scout")["status"] == "UNCHANGED"
    monkeypatch.setattr(handoff, "_write", writer)
    summary(bound, report=saved("2026-10-15T01:00:00+00:00"))
    assert sync(bound, "atlas")["status"] == "PUBLISHED"
    assert sync(bound, "scout")["status"] == "IMPORTED"
    assert len(list((folder(bound) / "packets").glob("*.json"))) == 2
    assert (bound["scout"]["root"] / handoff.STATE / SESSION / "history" / (handoff._sha(first) + ".json")).read_bytes() == first


@pytest.mark.parametrize("change", [
    lambda p: p.update(producer="Scout"),
    lambda p: p.update(recipient="Atlas"),
    lambda p: p.update(session_date="2026-10-13"),
    lambda p: p.update(account_scope_sha256="b" * 64),
    lambda p: p.update(summary_sha256="0" * 64),
    lambda p: p.update(account_number="raw-account"),
])
def test_packet_identity_digest_and_unknown_fields_rejected(bound, change):
    summary(bound)
    sync(bound, "atlas")
    destination = summary(bound, "scout")
    before = destination.read_bytes()
    mutate_packet(bound, change)
    assert sync(bound, "scout")["status"] == "INVALID"
    assert destination.read_bytes() == before


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(session_date="2026-10-13"),
    lambda r: r["counts"].update(cancelled=2),
    lambda r: r.update(account_number="raw-account"),
    lambda r: r["orders"][0].update(broker_order_id="raw-order"),
    lambda r: r["orders"][0]["fills"][0].update(account="raw-account"),
    lambda r: r["coverage"].update(account="raw-account"),
    lambda r: r["provenance"].update(account="raw-account"),
    lambda r: r["orders"][0].update(reasons=["private-fixture-secret"]),
])
def test_source_schema_and_private_values_are_not_exported(bound, mutate):
    report = saved()
    mutate(report)
    # Keep the source at the selected session even when its payload lies.
    write(bound["atlas"]["root"] / handoff.SUMMARY / SESSION / "summary.json", report)
    assert sync(bound, "atlas")["status"] == "INVALID"
    assert not (folder(bound) / "selection.json").exists()


def test_received_report_unknown_fields_rejected_even_with_matching_digests(bound):
    summary(bound)
    sync(bound, "atlas")
    def corrupt(packet):
        value = json.loads(base64.b64decode(packet["summary_base64"]))
        value["orders"][0]["raw_account"] = "private"
        raw = handoff._bytes(value)
        packet.update(summary_sha256=handoff._sha(raw), summary_base64=base64.b64encode(raw).decode())
    mutate_packet(bound, corrupt)
    assert sync(bound, "scout")["status"] == "INVALID"
    assert not (bound["scout"]["root"] / handoff.SUMMARY / SESSION / "summary.json").exists()


def test_older_packet_and_conflicting_generation_do_not_replace_saved_report(bound):
    summary(bound)
    sync(bound, "atlas")
    selector = (folder(bound) / "selection.json").read_bytes()
    sync(bound, "scout")
    summary(bound, report=saved("2026-10-15T01:00:00+00:00"))
    sync(bound, "atlas")
    sync(bound, "scout")
    destination = bound["scout"]["root"] / handoff.SUMMARY / SESSION / "summary.json"
    newest = destination.read_bytes()
    (folder(bound) / "selection.json").write_bytes(selector)
    assert sync(bound, "scout")["status"] == "STALE_IGNORED"
    assert destination.read_bytes() == newest
    summary(bound, report=saved("2026-10-14T23:30:00+00:00"))
    assert sync(bound, "atlas")["status"] == "STALE_IGNORED"
    changed = saved()
    changed["message"] = "Different report at the same generation"
    summary(bound, report=changed)
    assert sync(bound, "atlas")["status"] == "INVALID"


def test_missing_source_preserves_published_good_report(bound):
    source = summary(bound)
    sync(bound, "atlas")
    original = (folder(bound) / "selection.json").read_bytes()
    missing, _, _ = _base(SESSION, datetime.fromisoformat("2026-10-16T00:00:00+00:00"))
    write(source, missing)
    assert sync(bound, "atlas")["status"] == "NO_SOURCE"
    assert (folder(bound) / "selection.json").read_bytes() == original
    assert sync(bound, "scout")["status"] == "IMPORTED"


def test_missing_binding_and_unbound_datastore_do_not_touch_private_exchange(bound, tmp_path, monkeypatch):
    assert handoff.sync_execution_summary(tmp_path, SESSION, config_path=tmp_path / "absent.json") == {"status": "NOT_CONFIGURED"}
    original = Path.is_dir
    def deny_exchange(path):
        if path == bound["exchange"]:
            pytest.fail("Unbound datastore must not access private exchange")
        return original(path)
    monkeypatch.setattr(Path, "is_dir", deny_exchange)
    assert handoff.sync_execution_summary(tmp_path, SESSION, config_path=bound["atlas"]["config"]) == {"status": "NOT_APPLICABLE"}


def test_receive_only_on_atlas_does_not_publish_or_access_mount(bound, monkeypatch):
    summary(bound)
    original = Path.is_dir
    def deny_exchange(path):
        if path == bound["exchange"]:
            pytest.fail("Atlas UI cannot inspect the peer exchange mount")
        return original(path)
    monkeypatch.setattr(Path, "is_dir", deny_exchange)
    assert sync(bound, "atlas", receive_only=True)["status"] == "NOT_APPLICABLE"
    assert not folder(bound).exists()


def test_disconnection_and_busy_lock_are_nonfatal(bound, monkeypatch):
    summary(bound)
    reader = handoff._read
    def disconnected(path, *args):
        if bound["exchange"] in Path(path).parents:
            raise OSError("PRIVATE_PATH_MUST_NOT_LEAK")
        return reader(path, *args)
    sync(bound, "atlas")
    monkeypatch.setattr(handoff, "_read", disconnected)
    result = sync(bound, "scout")
    assert result["status"] == "UNAVAILABLE" and "PRIVATE_" not in str(result)
    outcome = json.loads((bound["scout"]["root"] / handoff.STATE / SESSION / "status.json").read_bytes())
    assert outcome["status"] == "UNAVAILABLE" and "PRIVATE_" not in str(outcome)
    monkeypatch.setattr(handoff, "_read", reader)
    state = bound["atlas"]["root"] / handoff.STATE / SESSION
    with FileLock(str(state / "sync.lock"), timeout=0):
        assert sync(bound, "atlas")["status"] == "BUSY"


def test_packet_file_corruption_preserves_local_report(bound):
    summary(bound)
    sync(bound, "atlas")
    sync(bound, "scout")
    destination = bound["scout"]["root"] / handoff.SUMMARY / SESSION / "summary.json"
    original = destination.read_bytes()
    selected = json.loads((folder(bound) / "selection.json").read_bytes())
    packet = folder(bound) / "packets" / (selected["packet_sha256"] + ".json")
    packet.write_bytes(packet.read_bytes().replace(b'Atlas', b'Other'))
    assert sync(bound, "scout")["status"] == "INVALID"
    assert destination.read_bytes() == original


def test_source_changing_during_export_is_recorded_for_retry(bound, monkeypatch):
    source = summary(bound)
    scan = handoff._scan
    def newer_source(raw, report, checkout):
        scan(raw, report, checkout)
        write(source, saved("2026-10-15T01:00:00+00:00"))
    monkeypatch.setattr(handoff, "_scan", newer_source)
    assert sync(bound, "atlas")["status"] == "SOURCE_CHANGED"
    assert not (folder(bound) / "selection.json").exists()
    outcome = json.loads((bound["atlas"]["root"] / handoff.STATE / SESSION / "status.json").read_bytes())
    assert outcome["status"] == "SOURCE_CHANGED"
    monkeypatch.setattr(handoff, "_scan", scan)
    assert sync(bound, "atlas")["status"] == "PUBLISHED"


def test_publication_and_scout_refresh_hooks_transfer_real_saved_report(bound, ledger, monkeypatch):
    # Rebind the fixture's Atlas native datastore to the real offline ledger.
    config = json.loads(bound["atlas"]["config"].read_bytes())
    native = json.loads(Path(config["workflow_config"]).read_bytes())
    native["datastore"] = str(ledger)
    write(Path(config["workflow_config"]), native)
    bound["atlas"]["root"] = ledger
    add_order(ledger)
    add_order(ledger, "cancelled", status="CANCELLED", filled=3)
    marker = write(ledger / "ml/completed-gameplan.json", {"state": "COMPLETE", "unchanged": True})
    before = marker.read_bytes()
    monkeypatch.setattr(handoff, "_default_config_path", lambda: bound["atlas"]["config"])
    source = publish_execution_summary(ledger, SESSION, observed_at=NOW)
    assert (folder(bound) / "selection.json").is_file()
    monkeypatch.setattr(handoff, "_default_config_path", lambda: bound["scout"]["config"])
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("Scout display cannot inspect any ledger"))
    snapshot = load_gameplan_execution(bound["scout"]["root"], SESSION, now=NOW)
    assert snapshot.counts["submitted_attempts"] == 2
    assert snapshot.counts["fully_filled"] == 1 and snapshot.counts["cancelled"] == 1
    assert snapshot.report_path.read_bytes() == source.read_bytes()
    assert snapshot.report == json.loads(source.read_bytes())
    assert not (bound["scout"]["root"] / LEDGER_RELATIVE_PATH).exists()
    assert marker.read_bytes() == before
    # A local Scout Stats publication cannot erase the imported display.
    publish_execution_summary(bound["scout"]["root"], SESSION, observed_at="2026-10-16T00:00:00+00:00")
    assert snapshot.report_path.read_bytes() == source.read_bytes()
