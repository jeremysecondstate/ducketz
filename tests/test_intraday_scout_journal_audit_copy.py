"""Audit comparison views retain fresh evidence checks and detached public values."""
from collections import UserDict
from copy import deepcopy
import json
import sqlite3

import pytest

from ml.intraday import scout_journal as module
from ml.intraday.scout_journal import JournalCorruptionError, ScoutJournal
from ml.intraday.scout_protocol import Grant, Request
from ml.stock_trader.horizon_ledger import OrderEvidence


BINDING = {"account_fingerprint": "a" * 64, "session_id": "audit-copy-test",
           "participant_id": "pc-new", "stream": "scout-15m"}


def request(index):
    return Request(request_id=f"request-{index}", account_fingerprint=BINDING["account_fingerprint"],
        session_id=BINDING["session_id"], symbol="DOCU", side="BUY", quantity=2,
        limit_price="10.25", forecast_id="b" * 64, forecast_sha256="b" * 64,
        snapshot_sha256="c" * 64, sizing_sha256="d" * 64,
        reference_at="2026-10-12T18:00:00Z", issued_at="2026-10-12T18:00:01Z",
        target_close_at="2026-10-12T18:15:00Z", valid_until="2026-10-12T18:14:00Z")


@pytest.fixture
def ledger(tmp_path):
    journal = ScoutJournal(tmp_path / "audit.sqlite3", binding=BINDING)
    for index in range(4):
        req = request(index)
        journal.prepare(req)
        if index == 3:
            journal.revoke_unsent(req.request_id, "offline-prepared")
            continue
        permit = Grant(grant_id=f"grant-{index}", request_id=req.request_id,
            request_sha256=req.sha256, account_fingerprint=req.account_fingerprint,
            session_id=req.session_id, authority_epoch=1, quantity=2,
            reservation_id=f"reservation-{index}", issued_at="2026-10-12T18:00:02Z",
            expires_at="2026-10-12T18:13:00Z")
        journal.grant(permit)
        if index == 2:
            journal.revoke_unsent(req.request_id, "offline-granted")
            continue
        token = journal.claim_send(req.request_id)
        journal.outcome(req.request_id, token, "offline-order" if index == 0 else None)
        if index == 1:
            journal.outcome(req.request_id, token, "late-offline-order")
        else:
            journal.observe_order(req.request_id, OrderEvidence("evidence", permit.reservation_id,
                BINDING["account_fingerprint"], "2026-10-12T18:01:00+00:00", "offline-order",
                "CANCELLED", 2, 0, 0, (), "CANCELED"), as_of="2026-10-12T18:10:00+00:00")
    yield journal
    journal.close()


def audit_outcome(ledger, db):
    try:
        return ("return", ledger._audit(db, include_lifecycle=True))
    except Exception as error:
        return ("error", type(error), str(error))


def assert_historical_audit_outcome(ledger, db, monkeypatch):
    current = audit_outcome(ledger, db)
    with monkeypatch.context() as original:
        original.setattr(module, "_audit_event_record", module._event_record)
        assert audit_outcome(ledger, db) == current
    return current


def test_fresh_audit_preserves_all_results_without_recursive_current_record_copies(ledger, monkeypatch):
    with sqlite3.connect(ledger.path) as db:
        before = tuple(db.iterdump())
        historical = assert_historical_audit_outcome(ledger, db, monkeypatch)
        assert historical[0] == "return"
        copied_records = []
        def counted(value, *args, **kwargs):
            if type(value) is dict and set(value) == module._RECORD_KEYS:
                copied_records.append(value)
            return deepcopy(value, *args, **kwargs)
        monkeypatch.setattr(module, "deepcopy", counted)
        assert audit_outcome(ledger, db) == historical
        assert not copied_records
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("table,identity", [("control", "id"), ("events", "sequence"),
    ("records", "request_id"), ("lifecycle_control", "id"),
    ("lifecycle_events", "sequence"), ("lifecycle_records", "request_id")])
def test_next_audit_rejects_each_freshly_changed_stored_hash(ledger, monkeypatch, table, identity):
    with sqlite3.connect(ledger.path) as db:
        assert audit_outcome(ledger, db)[0] == "return"
        key = db.execute(f"SELECT {identity} FROM {table} LIMIT 1").fetchone()[0]
        db.execute(f"UPDATE {table} SET sha256=? WHERE {identity}=?", ("0" * 64, key))
        result = assert_historical_audit_outcome(ledger, db, monkeypatch)
        assert result[0:2] == ("error", JournalCorruptionError)


@pytest.mark.parametrize("field", ["request", "grant", "state", "send_token",
    "broker_order_id", "revoke_reason"])
def test_rehashed_record_tampering_keeps_exact_original_error(ledger, monkeypatch, field):
    with sqlite3.connect(ledger.path) as db:
        rid, raw = db.execute("SELECT request_id,payload FROM records WHERE request_id='request-0'").fetchone()
        record = json.loads(raw)
        if field in {"request", "grant"}:
            record[field]["quantity"] = 999
        else:
            record[field] = "ILLEGAL" if field == "state" else "foreign"
        db.execute("UPDATE records SET payload=?,sha256=? WHERE request_id=?",
            (module._encoded(record), module.canonical_sha256(record), rid))
        result = assert_historical_audit_outcome(ledger, db, monkeypatch)
        assert result[0:2] == ("error", JournalCorruptionError)


def test_helper_override_keeps_one_argument_and_one_call_per_current_record(ledger, monkeypatch):
    original, seen = module._event_record, []
    def override(record):
        seen.append(record)
        return original(record)
    monkeypatch.setattr(module, "_event_record", override)
    with sqlite3.connect(ledger.path) as db:
        count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        ledger._audit(db, include_lifecycle=True)
    assert len(seen) == count


class Opaque:
    pass


class CustomText(str):
    pass


@pytest.mark.parametrize("shape", ["outer_mapping", "nested_mapping", "opaque", "list",
    "nested_dict", "custom_scalar", "bool", "top_key"])
def test_custom_or_nested_inputs_keep_historical_copy_detachment(ledger, shape):
    record = ledger.get("request-0")
    if shape == "outer_mapping":
        record = UserDict(record)
    elif shape == "nested_mapping":
        record["request"] = UserDict(record["request"])
    elif shape == "opaque":
        record["request"]["extra"] = Opaque()
    elif shape == "list":
        record["request"]["extra"] = [1, {"nested": [2]}]
    elif shape == "nested_dict":
        record["request"]["extra"] = {"nested": [2]}
    elif shape == "custom_scalar":
        record["request"]["symbol"] = CustomText("DOCU")
    elif shape == "bool":
        record["request"]["quantity"] = True
    else:
        record[CustomText("state")] = record.pop("state")
    result = module._audit_event_record(record)
    assert type(result) is type(module._event_record(record))
    assert result["request"] is not record["request"]
    assert result["grant"] is not record["grant"]
    if shape == "opaque":
        assert isinstance(result["request"]["extra"], Opaque)
        assert result["request"]["extra"] is not record["request"]["extra"]
    else:
        assert result == module._event_record(record)
    assert "send_token" in record
    assert "send_token_sha256" not in record


def test_historical_helper_keeps_native_nested_values_detached(ledger):
    record = ledger.get("request-0")
    result = module._event_record(record)
    result["request"]["quantity"] = 999
    result["grant"]["quantity"] = 777
    assert record["request"]["quantity"] == record["grant"]["quantity"] == 2


def test_append_uses_historical_helper_and_records_are_detached(tmp_path, monkeypatch):
    journal = ScoutJournal(tmp_path / "append.sqlite3", binding=BINDING)
    original, seen = module._event_record, []
    def original_append(record):
        result = original(record)
        assert result["request"] is not record["request"]
        seen.append(record)
        return result
    monkeypatch.setattr(module, "_event_record", original_append)
    saved = journal.prepare(request(0))
    assert len(seen) == 1
    saved["request"]["quantity"] = 999
    assert journal.get("request-0")["request"]["quantity"] == 2
    journal.close()
