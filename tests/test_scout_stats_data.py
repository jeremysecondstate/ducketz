"""Offline scalar forecast and original-journal fixtures; no runtime imports."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.ui.scout_stats_data import (
    TARGET_VERSION, execution_summary, load_scout_stats, prediction_summary,
    scout_stats_sessions,
)

NOW = datetime(2026, 10, 24, 18, tzinfo=timezone.utc)
REFERENCE = "2026-10-23T17:00:00+00:00"
OPEN = "2026-10-23T17:30:00+00:00"
CLOSE = "2026-10-23T17:45:00+00:00"
ISSUED = "2026-10-23T17:20:00+00:00"


def encode(value, *, ascii=False):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=ascii, allow_nan=False)


def digest(value, *, ascii=False):
    return hashlib.sha256(encode(value, ascii=ascii).encode()).hexdigest()


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


def prediction_store(root):
    path = root / "intraday/shadow.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with database(path) as db:
        db.executescript("CREATE TABLE forecasts(id TEXT PRIMARY KEY,pc TEXT,symbol TEXT,cycle_id TEXT,reference_at TEXT,issued_at TEXT,payload TEXT);"
                         "CREATE TABLE outcomes(id TEXT PRIMARY KEY,forecast_id TEXT,observed_at TEXT,payload TEXT);")
        # Deliberately unusable writer/input tables are outside this reader's scope.
        db.execute("CREATE TABLE snapshots(payload BLOB)")
        db.execute("INSERT INTO snapshots VALUES(?)", (b'not a model or readable input',))
    return path


def add_forecast(path, key="forecast-1", *, pc="Scout", mode="live", issued=ISSUED, reference=REFERENCE,
                 probability=.8, target=TARGET_VERSION):
    value = dict(forecast_id=key, pc=pc, symbol="TEST", mode=mode, target_version=target,
                 reference_at=reference, issued_at=issued, target_open=OPEN, target_close=CLOSE,
                 probability_not_down=probability, models=[{"private": "not returned"}])
    with database(path) as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (key, pc, "TEST", key, reference, issued, encode(value)))
    return key


def add_outcome(path, fid, *, status="scored", label=True, observed="2026-10-23T18:00:00+00:00", reason=None):
    evidence = dict(forecast_id=fid, status=status, target_not_down=label, target_close_price="101",
                    unavailable_reason=reason)
    key = digest(evidence, ascii=True)
    value = dict(evidence, revision_id=key, observed_at=observed)
    with database(path) as db:
        db.execute("INSERT INTO outcomes VALUES(?,?,?,?)", (key, fid, observed, encode(value, ascii=True)))


def journal(root, *, requests=(), session="manual-scout-15m:fixture"):
    scout = root / "intraday/scout"
    path = scout / "journals" / (digest({"session_id": session}) + ".sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    binding = dict(account_fingerprint="a" * 64, session_id=session, participant_id="pc-new", stream="scout-15m")
    item = dict(filename=path.name, binding=binding, journal_id="b" * 32)
    invpath = scout / "inventory.json"
    inventory = (json.loads(invpath.read_text())["inventory"] if invpath.exists() else
                 dict(version="scout-session-books-v1", account_fingerprint="a" * 64,
                      participant_id="pc-new", stream="scout-15m", sessions={}))
    inventory["sessions"][session] = item
    invpath.write_text(encode(dict(inventory=inventory, sha256=digest(inventory))), encoding="utf-8")
    with database(path) as db:
        db.executescript("CREATE TABLE control(id INTEGER PRIMARY KEY,payload TEXT,sha256 TEXT);"
                         "CREATE TABLE records(request_id TEXT PRIMARY KEY,payload TEXT,sha256 TEXT);"
                         "CREATE TABLE lifecycle_records(request_id TEXT PRIMARY KEY,payload TEXT,sha256 TEXT);")
        control = dict(binding=binding, journal_id=item["journal_id"])
        db.execute("INSERT INTO control VALUES(1,?,?)", (encode(control), digest(control)))
        for spec in requests:
            rid = spec["request_id"]
            request = dict(binding, request_id=rid, forecast_id=spec.get("forecast_id", "forecast-1"),
                           symbol="TEST", side="BUY", reference_at=spec.get("reference_at", REFERENCE),
                           issued_at=ISSUED, quantity=10)
            grant = dict(quantity=10, reservation_id="reservation:" + rid)
            record = dict(request=request, request_sha256=digest(request), grant=grant,
                          grant_sha256=digest(grant), state=spec.get("state", "ACCEPTED"),
                          broker_order_id="broker:" + rid, send_token="private-never-return")
            db.execute("INSERT INTO records VALUES(?,?,?)", (rid, encode(record), digest(record)))
            if "status" in spec:
                filled = spec.get("filled_quantity", 10)
                fills = spec.get("fills", [dict(fill_id="fill:" + rid, price="100", quantity=filled,
                                                 executed_at="2026-10-23T17:21:00+00:00")] if filled else [])
                evidence = dict(account_fingerprint=binding["account_fingerprint"], broker_order_id=record["broker_order_id"],
                                reservation_id=grant["reservation_id"], order_quantity=10, cumulative_filled_quantity=filled,
                                status=spec["status"], fills=fills, observed_at="2026-10-23T18:00:00+00:00")
                lifecycle = dict(evidence=evidence, cancellation=None)
                db.execute("INSERT INTO lifecycle_records VALUES(?,?,?)", (rid, encode(lifecycle), digest(lifecycle)))
    return path


def test_prediction_only_uses_latest_outcome_and_returns_no_private_inputs(tmp_path):
    path = prediction_store(tmp_path)
    fid = add_forecast(path)
    add_outcome(path, fid, status="missing", label=None)
    add_outcome(path, fid, reason="observé")
    add_forecast(path, "atlas", pc="Atlas")
    add_forecast(path, "shadow", mode="shadow")
    add_forecast(path, "replay", mode="historical_replay")
    before = path.read_bytes()
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    summary = prediction_summary(snapshot.forecasts)
    assert snapshot.prediction_status == "read" and snapshot.execution_status == "missing"
    assert summary["issued"] == summary["scored"] == summary["correct"] == 1
    assert summary["accuracy"] == 1 and summary["brier"] == pytest.approx(.04)
    assert "models" not in snapshot.forecasts[0] and "snapshot" not in repr(snapshot.forecasts)
    assert snapshot.orders == () and snapshot.warnings
    assert path.read_bytes() == before
    assert scout_stats_sessions(tmp_path) == ("2026-10-23",)


def test_scoring_preserves_missing_and_excluded_populations(tmp_path):
    path = prediction_store(tmp_path)
    cases = (("observed", ISSUED, .8, "scored"), ("missing", ISSUED, .8, None),
             ("unscorable", ISSUED, .8, "unscorable"), ("synthetic", ISSUED, .8, "synthetic"),
             ("during", OPEN, .8, "scored"), ("after", CLOSE, .8, "scored"),
             ("unknown", "bad", .8, "scored"), ("bad-probability", ISSUED, 1.1, "scored"))
    for key, issued, probability, status in cases:
        add_forecast(path, key, issued=issued, probability=probability)
        if status:
            add_outcome(path, key, status=status)
    add_forecast(path, "unsupported", target="different-target")
    result = prediction_summary(load_scout_stats(tmp_path, "2026-10-23", now=NOW).forecasts)
    assert result == dict(issued=9, eligible=5, scored=1, correct=1, pending=0, missing=1, unscorable=1,
                         synthetic=1, during_target=1, after_target=1, timing_unknown=1,
                         invalid_probability=1, unsupported_target=1, accuracy=1, brier=pytest.approx(.04))


def test_pending_matures_to_missing_without_price_fetch(tmp_path):
    path = prediction_store(tmp_path)
    fid = add_forecast(path)
    add_outcome(path, fid, status="pending", label=None)
    early = datetime(2026, 10, 23, 17, 40, tzinfo=timezone.utc)
    assert prediction_summary(load_scout_stats(tmp_path, "2026-10-23", now=early).forecasts)["pending"] == 1
    assert prediction_summary(load_scout_stats(tmp_path, "2026-10-23", now=NOW).forecasts)["missing"] == 1


def test_order_only_counts_original_attempts_cumulative_fills_and_unknowns(tmp_path):
    journal(tmp_path, requests=[dict(request_id="filled", status="FILLED"),
                               dict(request_id="partial", status="PARTIAL", filled_quantity=4),
                               dict(request_id="cancelled", status="CANCELLED", filled_quantity=2),
                               dict(request_id="unknown", state="UNKNOWN"),
                               dict(request_id="accepted", state="ACCEPTED"),
                               dict(request_id="not-sent", state="NOT_SENT")])
    assert scout_stats_sessions(tmp_path) == ("2026-10-23",)
    snapshot = load_scout_stats(tmp_path, now=NOW)
    result = execution_summary(snapshot.orders)
    assert snapshot.session == "2026-10-23" and snapshot.prediction_status == "missing"
    assert snapshot.execution_status == "read" and result["attempts"] == 6
    assert result["submitted"] == 4 and result["filled"] == 1 and result["partial"] == 1
    assert result["cancelled"] == 1 and result["unknown"] == 2 and result["not_sent"] == 1
    assert result["filled_quantity"] == 16 and result["fill_records"] == 3
    assert result["missing_fill_evidence"] == 2 and result["realized_pnl"] is None
    assert all("account_fingerprint" not in row and "send_token" not in row for row in snapshot.orders)


def test_order_dedup_and_reference_session_are_explicit(tmp_path):
    journal(tmp_path, requests=[dict(request_id="one", status="FILLED", reference_at="2026-10-24T00:00:00+00:00")])
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert execution_summary(snapshot.orders + snapshot.orders)["filled_quantity"] == 10
    assert scout_stats_sessions(tmp_path) == ("2026-10-23",)
    assert load_scout_stats(tmp_path, "2026-10-24", now=NOW).orders == ()


def test_missing_or_corrupt_journal_does_not_hide_valid_predictions_or_orders(tmp_path):
    path = prediction_store(tmp_path)
    fid = add_forecast(path)
    add_outcome(path, fid)
    journal(tmp_path, requests=[dict(request_id="good", status="FILLED")], session="good")
    bad = journal(tmp_path, requests=[dict(request_id="bad", status="FILLED")], session="bad")
    bad.unlink()
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.execution_status == "partial"
    assert prediction_summary(snapshot.forecasts)["scored"] == 1
    assert execution_summary(snapshot.orders)["filled"] == 1
    assert any("journal is missing" in warning for warning in snapshot.warnings)


def test_tampered_latest_outcome_is_missing_instead_of_using_older_success(tmp_path):
    path = prediction_store(tmp_path)
    fid = add_forecast(path)
    add_outcome(path, fid)
    with database(path) as db:
        db.execute("INSERT INTO outcomes VALUES(?,?,?,?)", ("tampered", fid, "2026-10-24T00:00:00+00:00", '{}'))
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.prediction_status == "partial"
    assert prediction_summary(snapshot.forecasts)["missing"] == 1
    assert prediction_summary(snapshot.forecasts)["scored"] == 0


def test_tampered_lifecycle_is_excluded_and_warned(tmp_path):
    path = journal(tmp_path, requests=[dict(request_id="bad", status="FILLED"), dict(request_id="good", status="FILLED")])
    with database(path) as db:
        db.execute("UPDATE lifecycle_records SET sha256='changed' WHERE request_id='bad'")
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.execution_status == "partial" and len(snapshot.orders) == 2
    assert execution_summary(snapshot.orders)["unknown"] == 1
    assert any("result remains unknown" in warning for warning in snapshot.warnings)


def test_all_registered_journals_unreadable_means_unavailable_not_zero(tmp_path):
    path = journal(tmp_path, requests=[dict(request_id="lost", status="FILLED")])
    path.unlink()
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.execution_status == "unreadable" and snapshot.orders == ()


def test_missing_sources_are_unavailable_but_existing_empty_sources_are_zero(tmp_path):
    missing = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert missing.status == "missing" and missing.prediction_status == missing.execution_status == "missing"
    prediction_store(tmp_path)
    journal(tmp_path)
    empty = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert empty.status == "read" and empty.forecasts == empty.orders == ()
    assert prediction_summary(empty.forecasts)["scored"] == execution_summary(empty.orders)["attempts"] == 0


def test_unreadable_prediction_store_does_not_create_or_reset_it(tmp_path):
    path = tmp_path / "intraday/shadow.sqlite3"
    path.parent.mkdir()
    path.write_bytes(b"broken sqlite")
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.prediction_status == "unreadable" and path.read_bytes() == b"broken sqlite"
    assert scout_stats_sessions(tmp_path) == ()


def test_invalid_inventory_is_not_replaced_and_unknown_schema_is_unavailable(tmp_path):
    path = journal(tmp_path)
    invpath = path.parent.parent / "inventory.json"
    raw = invpath.read_text().replace('"sha256":"', '"sha256":"changed')
    invpath.write_text(raw, encoding="utf-8")
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.execution_status == "unreadable" and snapshot.orders == ()
    assert invpath.read_text() == raw


@pytest.mark.parametrize("shape", ["inventory-list", "inventory-version", "binding-list", "request-list", "evidence-list", "fills-dict", "fill-string"])
def test_malformed_json_shapes_do_not_hide_prediction_or_other_orders(tmp_path, shape):
    prediction_path = prediction_store(tmp_path)
    fid = add_forecast(prediction_path)
    add_outcome(prediction_path, fid)
    path = journal(tmp_path, requests=[dict(request_id="bad", status="FILLED"), dict(request_id="good", status="FILLED")])
    if shape in {"inventory-list", "inventory-version", "binding-list"}:
        invpath = path.parent.parent / "inventory.json"
        inventory = json.loads(invpath.read_text())["inventory"]
        if shape == "inventory-list":
            inventory = []
        elif shape == "inventory-version":
            inventory["version"] = "unknown-future-format"
        else:
            next(iter(inventory["sessions"].values()))["binding"] = []
        invpath.write_text(encode(dict(inventory=inventory, sha256=digest(inventory))), encoding="utf-8")
    else:
        table = "records" if shape == "request-list" else "lifecycle_records"
        with database(path) as db:
            value = json.loads(db.execute("SELECT payload FROM " + table + " WHERE request_id='bad'").fetchone()[0])
            if shape == "request-list":
                value["request"] = []
            elif shape == "evidence-list":
                value["evidence"] = ["invalid"]
            elif shape == "fills-dict":
                value["evidence"]["fills"] = {"invalid": "shape"}
            else:
                value["evidence"]["fills"] = ["invalid"]
            db.execute("UPDATE " + table + " SET payload=?,sha256=? WHERE request_id='bad'", (encode(value), digest(value)))
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.prediction_status == "read" and prediction_summary(snapshot.forecasts)["scored"] == 1
    assert snapshot.warnings
    if shape in {"inventory-list", "inventory-version", "binding-list"}:
        assert snapshot.execution_status == "unreadable"
    else:
        result = execution_summary(snapshot.orders)
        assert result["filled"] == 1 and snapshot.execution_status == "partial"
        assert result["unknown"] == (0 if shape == "request-list" else 1)


@pytest.mark.parametrize("field,value", [("reference_at", None), ("reference_at", "invalid"),
                                        ("issued_at", None), ("issued_at", "2026-10-23T17:20:00")])
def test_invalid_request_timestamps_are_warned_without_hiding_other_evidence(tmp_path, field, value):
    prediction_path = prediction_store(tmp_path)
    fid = add_forecast(prediction_path)
    add_outcome(prediction_path, fid)
    path = journal(tmp_path, requests=[dict(request_id="bad", status="FILLED"), dict(request_id="good", status="FILLED")])
    with database(path) as db:
        record = json.loads(db.execute("SELECT payload FROM records WHERE request_id='bad'").fetchone()[0])
        record["request"][field] = value
        record["request_sha256"] = digest(record["request"])
        db.execute("UPDATE records SET payload=?,sha256=? WHERE request_id='bad'", (encode(record), digest(record)))
    snapshot = load_scout_stats(tmp_path, "2026-10-23", now=NOW)
    assert snapshot.prediction_status == "read" and prediction_summary(snapshot.forecasts)["scored"] == 1
    assert snapshot.execution_status == "partial" and execution_summary(snapshot.orders)["filled"] == 1
    assert len(snapshot.orders) == 1 and any("record failed validation" in warning for warning in snapshot.warnings)
