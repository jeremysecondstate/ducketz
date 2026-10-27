"""Fresh canonical replay need not clone its immutable nested JSON values."""
from copy import deepcopy
import json
import sqlite3

import pytest

from ml.intraday import scout_journal as module
from ml.intraday.scout_journal import JournalCorruptionError, ScoutJournal
from ml.intraday.scout_protocol import Grant, Request


BINDING = {"account_fingerprint": "a" * 64, "session_id": "replay-copy-test",
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
    journal = ScoutJournal(tmp_path / "replay.sqlite3", binding=BINDING)
    for index in range(4):
        req = request(index)
        journal.prepare(req)
        if index == 3:
            journal.revoke_unsent(req.request_id, "offline-prepared")
            continue
        permit = Grant(grant_id=f"grant-{index}", request_id=req.request_id, request_sha256=req.sha256,
            account_fingerprint=req.account_fingerprint, session_id=req.session_id,
            authority_epoch=1, quantity=2, reservation_id=f"reservation-{index}",
            issued_at="2026-10-12T18:00:02Z", expires_at="2026-10-12T18:13:00Z")
        journal.grant(permit)
        if index == 2:
            journal.revoke_unsent(req.request_id, "offline-granted")
        else:
            token = journal.claim_send(req.request_id)
            journal.outcome(req.request_id, token, "offline-order" if index == 0 else None)
            if index == 1:
                journal.outcome(req.request_id, token, "late-offline-order")
    yield journal
    journal.close()


def historical_replay(previous, record, kind):
    """The original independent deep-copy oracle, including its exact errors."""
    if kind == "PREPARED":
        if previous is not None or record["state"] != "PREPARED":
            raise JournalCorruptionError("Request identity was reused")
        return
    allowed = {"GRANTED": {"PREPARED"}, "SENDING": {"GRANTED"},
               "UNKNOWN": {"SENDING"}, "ACCEPTED": {"SENDING", "UNKNOWN"},
               "NOT_SENT": {"PREPARED", "GRANTED"}}
    if kind not in allowed or previous is None or previous["state"] not in allowed[kind]:
        raise JournalCorruptionError("Invalid transition history")
    expected = deepcopy(previous)
    expected["state"] = kind
    changed = {"GRANTED": ("grant", "grant_sha256"), "SENDING": ("send_token_sha256",),
               "UNKNOWN": (), "ACCEPTED": ("broker_order_id",), "NOT_SENT": ("revoke_reason",)}
    for key in changed[kind]:
        expected[key] = record[key]
    if expected != record:
        raise JournalCorruptionError("Transition changed immutable record fields")


def outcome(function, previous, record, kind):
    try:
        return ("return", function(previous, record, kind))
    except Exception as error:
        return ("error", type(error), str(error))


def transitions(ledger):
    with sqlite3.connect(ledger.path) as db:
        events = [json.loads(raw) for (raw,) in db.execute("SELECT payload FROM events ORDER BY sequence")]
    previous = {}
    for event in events:
        record = event["record"]
        yield previous.get(event["request_id"]), record, event["kind"]
        previous[event["request_id"]] = record


def assert_json_domain(value):
    # DTO validation constructs separate objects; durable replay inputs retain
    # exactly the built-in containers/scalars produced by json.loads.
    if type(value) is dict:
        assert all(type(key) is str for key in value)
        for item in value.values(): assert_json_domain(item)
    elif type(value) is list:
        for item in value: assert_json_domain(item)
    else:
        assert type(value) in (str, int, float, bool, type(None))


def test_all_native_transitions_keep_json_domain_and_nested_inputs(ledger):
    kinds = set()
    for previous, record, kind in transitions(ledger):
        kinds.add(kind)
        assert_json_domain(record)
        originals = deepcopy((previous, record))
        prior_nested = tuple(id(previous[key]) for key in ("request", "grant")) if previous else None
        current_nested = tuple(id(record[key]) for key in ("request", "grant"))
        assert outcome(ScoutJournal._replay, previous, record, kind) == ("return", None)
        assert (previous, record) == originals
        assert tuple(id(record[key]) for key in ("request", "grant")) == current_nested
        if previous:
            assert tuple(id(previous[key]) for key in ("request", "grant")) == prior_nested
    assert kinds == {"PREPARED", "GRANTED", "SENDING", "UNKNOWN", "ACCEPTED", "NOT_SENT"}


@pytest.mark.parametrize("mutation", ["request_quantity", "request_symbol", "request_binding",
    "request_hash", "grant_quantity", "grant_reservation", "grant_hash", "token", "broker_id",
    "reason", "extra_nested", "removed_nested", "previous_state", "unknown_kind", "missing_previous"])
def test_native_and_nested_corruption_matches_original_replay(ledger, mutation):
    checked = 0
    for previous, record, kind in transitions(ledger):
        previous, record = deepcopy((previous, record))
        if mutation == "request_quantity": record["request"]["quantity"] = 999
        elif mutation == "request_symbol": record["request"]["symbol"] = "DBX"
        elif mutation == "request_binding": record["request"]["session_id"] = "foreign"
        elif mutation == "request_hash": record["request_sha256"] = "0" * 64
        elif mutation in {"grant_quantity", "grant_reservation"}:
            if record["grant"] is None: continue
            record["grant"]["quantity" if mutation == "grant_quantity" else "reservation_id"] = 999
        elif mutation == "grant_hash": record["grant_sha256"] = "0" * 64
        elif mutation == "token": record["send_token_sha256"] = "0" * 64
        elif mutation == "broker_id": record["broker_order_id"] = "foreign"
        elif mutation == "reason": record["revoke_reason"] = "foreign"
        elif mutation == "extra_nested": record["request"]["unknown"] = {"a": [1, False, None]}
        elif mutation == "removed_nested": del record["request"]["symbol"]
        elif mutation == "previous_state":
            if previous is None: continue
            previous["state"] = "ILLEGAL"
        elif mutation == "unknown_kind": kind = "ILLEGAL"
        elif mutation == "missing_previous": previous = None
        saved = deepcopy((previous, record))
        assert outcome(ScoutJournal._replay, previous, record, kind) == outcome(historical_replay, previous, record, kind)
        assert (previous, record) == saved
        checked += 1
    assert checked


def test_fresh_audit_has_same_result_without_deepcopying_replay_records(ledger, monkeypatch):
    with sqlite3.connect(ledger.path) as db:
        before = tuple(db.iterdump())
        native = ledger._audit(db, include_lifecycle=True)
        copied_records = []
        def counted(value, *args, **kwargs):
            if type(value) is dict and "send_token_sha256" in value and "request" in value:
                copied_records.append(value)
            return deepcopy(value, *args, **kwargs)
        monkeypatch.setattr(module, "deepcopy", counted)
        assert ledger._audit(db, include_lifecycle=True) == native
        assert not copied_records
        assert tuple(db.iterdump()) == before
        # The next audit must still reject freshly changed durable row bytes.
        db.execute("UPDATE events SET sha256=? WHERE sequence=2", ("0" * 64,))
        with pytest.raises(JournalCorruptionError): ledger._audit(db, include_lifecycle=True)


def test_custom_replay_override_keeps_signature_and_is_called_for_every_event(ledger, monkeypatch):
    seen = []
    original = ScoutJournal._replay
    def override(previous, record, kind):
        seen.append(kind)
        return original(previous, record, kind)
    monkeypatch.setattr(ledger, "_replay", override)
    with sqlite3.connect(ledger.path) as db:
        expected = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        ledger._audit(db, include_lifecycle=True)
    assert len(seen) == expected
