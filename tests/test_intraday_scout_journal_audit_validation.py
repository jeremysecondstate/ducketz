"""A complete audit can reuse DTO validation, never stored or replay evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import sys
from threading import Barrier, local
from types import MethodType

import pytest

from ml.intraday import scout_journal as module, scout_protocol as protocol
from ml.intraday.scout_journal import JournalCorruptionError, ScoutJournal
from ml.intraday.scout_protocol import Grant, Request, canonical_sha256


BINDING = {"account_fingerprint": "a" * 64, "session_id": "audit-validation-test",
           "participant_id": "pc-new", "stream": "scout-15m"}


def request(index=0):
    return Request(request_id=f"request-{index}", account_fingerprint=BINDING["account_fingerprint"],
                   session_id=BINDING["session_id"], symbol="DOCU", side="BUY", quantity=2,
                   limit_price="10.25", forecast_id="b" * 64, forecast_sha256="b" * 64,
                   snapshot_sha256="c" * 64, sizing_sha256="d" * 64,
                   reference_at="2026-10-12T18:00:00Z", issued_at="2026-10-12T18:00:01Z",
                   target_close_at="2026-10-12T18:15:00Z", valid_until="2026-10-12T18:14:00Z")


def grant(req, index=0):
    return Grant(grant_id=f"grant-{index}", request_id=req.request_id, request_sha256=req.sha256,
                 account_fingerprint=req.account_fingerprint, session_id=req.session_id,
                 authority_epoch=1, quantity=2, reservation_id=f"reservation-{index}",
                 issued_at="2026-10-12T18:00:02Z", expires_at="2026-10-12T18:13:00Z")


@pytest.fixture
def ledger(tmp_path):
    from ml.stock_trader.horizon_ledger import OrderEvidence
    journal = ScoutJournal(tmp_path / "offline.sqlite3", binding=BINDING)
    for index in range(3):
        req = request(index)
        journal.prepare(req)
        journal.grant(grant(req, index))
        if index == 2:
            journal.revoke_unsent(req.request_id, "offline-unsent")
        else:
            token = journal.claim_send(req.request_id)
            journal.outcome(req.request_id, token, "123" if index == 0 else None)
    journal.observe_order("request-0", OrderEvidence("observation-0", "reservation-0",
        BINDING["account_fingerprint"], "2026-10-12T18:01:00+00:00", "123", "WORKING",
        2, 0, 2, (), "WORKING"), as_of="2026-10-12T18:10:00+00:00")
    yield journal
    journal.close()


class OriginalCalls(ScoutJournal):
    def _validate_record(self, record, *, event=False):
        # The original direct path has no per-audit memo.
        return module._NATIVE_VALIDATE_RECORD(self, record, event=event)


def oracle(ledger):
    value = OriginalCalls.__new__(OriginalCalls)
    value.__dict__.update(ledger.__dict__)
    return value


def chain(error):
    return (type(error), str(error), chain(error.__cause__) if error.__cause__ else None)


def audit(journal, db):
    try:
        return ("return", journal._audit(db, include_lifecycle=True))
    except Exception as error:
        return ("error", chain(error))


def compare(ledger, db):
    before = tuple(db.iterdump())
    actual, expected = audit(ledger, db), audit(oracle(ledger), db)
    assert actual == expected
    assert tuple(db.iterdump()) == before
    return actual


def counts(call):
    codes = {"request": Request.from_payload.__func__.__code__,
             "grant": Grant.from_payload.__func__.__code__,
             "canonical_row": module._checked_json.__code__}
    result = dict.fromkeys(codes, 0)
    prior = sys.getprofile()
    def profile(frame, event, arg):
        if event == "call":
            for name, code in codes.items():
                if frame.f_code is code:
                    result[name] += 1
    try:
        sys.setprofile(profile)
        value = call()
    finally:
        sys.setprofile(prior)
    return value, result


def test_complete_fresh_audit_reuses_only_identical_dto_validation(ledger):
    with sqlite3.connect(ledger.path) as db:
        actual, native_count = counts(lambda: ledger._audit(db, include_lifecycle=True))
        expected, old_count = counts(lambda: oracle(ledger)._audit(db, include_lifecycle=True))
    assert actual == expected
    assert native_count["request"] == native_count["grant"] == 3
    assert old_count["request"] > native_count["request"]
    assert old_count["grant"] > native_count["grant"]
    assert native_count["canonical_row"] == old_count["canonical_row"]
    assert not any("memo" in key for key in vars(ledger))


def rewrite_events(db, change):
    rows = list(db.execute("SELECT sequence,event_id,payload FROM events ORDER BY sequence"))
    prior = None
    for sequence, identity, raw in rows:
        event = json.loads(raw)
        change(event)
        event["prior_sha256"] = prior
        prior = canonical_sha256(event)
        db.execute("UPDATE events SET payload=?,sha256=? WHERE sequence=?",
                   (module._encoded(event), prior, sequence))
    raw = db.execute("SELECT payload FROM control WHERE id=1").fetchone()[0]
    control = json.loads(raw)
    control["last_sha256"] = prior
    db.execute("UPDATE control SET payload=?,sha256=? WHERE id=1",
               (module._encoded(control), canonical_sha256(control)))


@pytest.mark.parametrize("quantity", [True, 2.0, 0, 3])
def test_later_canonical_event_cannot_reuse_different_typed_request(ledger, quantity):
    with sqlite3.connect(ledger.path) as db:
        def change(event):
            if event["sequence"] == 2:
                event["record"]["request"]["quantity"] = quantity
        rewrite_events(db, change)
        assert compare(ledger, db)[0] == "error"


@pytest.mark.parametrize("field", ["request_sha256", "grant_sha256"])
def test_later_event_cannot_reuse_dto_with_another_digest(ledger, field):
    with sqlite3.connect(ledger.path) as db:
        def change(event):
            if event["sequence"] == 3:
                event["record"][field] = "0" * 64
        rewrite_events(db, change)
        assert compare(ledger, db)[0] == "error"


@pytest.mark.parametrize("mutation", ["request", "grant", "token", "state", "binding"])
def test_checks_after_a_memo_hit_still_reject_changed_records(ledger, mutation):
    record = ledger.get("request-0")
    memo = ({}, {})
    module._NATIVE_VALIDATE_RECORD(ledger, record, _validation_memo=memo)
    changed = deepcopy(record)
    if mutation == "request": changed["request"]["symbol"] = "DBX"
    elif mutation == "grant": changed["grant"]["quantity"] = 1
    elif mutation == "token": changed["send_token"] = None
    elif mutation == "state": changed["state"] = "PREPARED"
    else: ledger.binding = dict(ledger.binding, session_id="another-session")
    try:
        actual = outcome(lambda: module._NATIVE_VALIDATE_RECORD(ledger, changed, _validation_memo=memo))
        expected = outcome(lambda: module._NATIVE_VALIDATE_RECORD(ledger, changed))
        assert actual == expected and actual[0] == "error"
    finally:
        ledger.binding = dict(BINDING)


def outcome(call):
    try:
        return ("return", call())
    except Exception as error:
        return ("error", chain(error))


def test_failed_dto_validation_cannot_poison_later_valid_request(ledger):
    record = ledger.get("request-0")
    memo = ({}, {})
    wrong = deepcopy(record)
    wrong["request_sha256"] = "0" * 64
    with pytest.raises(JournalCorruptionError):
        module._NATIVE_VALIDATE_RECORD(ledger, wrong, _validation_memo=memo)
    assert memo == ({}, {})
    module._NATIVE_VALIDATE_RECORD(ledger, record, _validation_memo=memo)
    assert len(memo[0]) == len(memo[1]) == 1
    wrong = deepcopy(record)
    wrong["grant_sha256"] = "0" * 64
    with pytest.raises(JournalCorruptionError):
        module._NATIVE_VALIDATE_RECORD(ledger, wrong, _validation_memo=memo)
    assert len(memo[0]) == len(memo[1]) == 1


def test_grant_memo_binds_the_exact_validated_request(ledger):
    record = ledger.get("request-0")
    memo = ({}, {})
    module._NATIVE_VALIDATE_RECORD(ledger, record, _validation_memo=memo)
    changed = deepcopy(record)
    req = replace(request(), quantity=3)
    changed["request"], changed["request_sha256"] = req.payload(), req.sha256
    actual = outcome(lambda: module._NATIVE_VALIDATE_RECORD(ledger, changed, _validation_memo=memo))
    expected = outcome(lambda: module._NATIVE_VALIDATE_RECORD(ledger, changed))
    assert actual == expected and actual[0] == "error"


def test_next_audit_revalidates_rows_and_has_no_object_memo(ledger):
    with sqlite3.connect(ledger.path) as db:
        first, first_counts = counts(lambda: ledger._audit(db, include_lifecycle=True))
        second, second_counts = counts(lambda: ledger._audit(db, include_lifecycle=True))
        assert first == second and first_counts == second_counts
        db.execute("UPDATE records SET sha256=? WHERE request_id='request-0'", ("0" * 64,))
        assert compare(ledger, db)[0] == "error"
    assert not any("memo" in key for key in vars(ledger))


@pytest.mark.parametrize("table", ["control", "events", "records", "lifecycle_control", "lifecycle_events", "lifecycle_records"])
def test_every_canonical_hash_remains_checked(ledger, table):
    with sqlite3.connect(ledger.path) as db:
        db.execute(f"UPDATE {table} SET sha256=? WHERE rowid=(SELECT MIN(rowid) FROM {table})", ("0" * 64,))
        assert compare(ledger, db)[0] == "error"


@pytest.mark.parametrize("where", ["class", "subclass", "instance"])
def test_custom_validator_keeps_original_signature_and_every_call(ledger, monkeypatch, where):
    seen = []
    def override(self, record, *, event=False):
        seen.append(event)
        return module._NATIVE_VALIDATE_RECORD(self, record, event=event)
    if where == "class": monkeypatch.setattr(ScoutJournal, "_validate_record", override)
    elif where == "instance": monkeypatch.setattr(ledger, "_validate_record", MethodType(override, ledger))
    else:
        class Custom(ScoutJournal):
            _validate_record = override
        ledger.__class__ = Custom
    with sqlite3.connect(ledger.path) as db:
        expected_calls = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] + db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        assert audit(ledger, db)[0] == "return"
    assert len(seen) == expected_calls


def test_validator_replaced_mid_audit_is_observed(ledger, monkeypatch):
    original = module._checked_json
    installed = False
    seen = []
    def override(self, record, *, event=False):
        seen.append(event)
        return module._NATIVE_VALIDATE_RECORD(self, record, event=event)
    def checked(raw, digest):
        nonlocal installed
        value = original(raw, digest)
        if isinstance(value, dict) and value.get("sequence") == 2 and "record" in value and not installed:
            installed = True
            monkeypatch.setattr(ledger, "_validate_record", MethodType(override, ledger))
        return value
    monkeypatch.setattr(module, "_checked_json", checked)
    with sqlite3.connect(ledger.path) as db:
        assert audit(ledger, db)[0] == "return"
    assert installed and seen


@pytest.mark.parametrize("where", ["request", "grant", "protocol_hash"])
def test_overridden_dto_contract_keeps_original_validation_calls(ledger, monkeypatch, where):
    seen = []
    if where in {"request", "grant"}:
        kind = Request if where == "request" else Grant
        original = kind.from_payload.__func__
        def override(cls, payload):
            seen.append(payload)
            return original(cls, payload)
        monkeypatch.setattr(kind, "from_payload", classmethod(override))
    else:
        original = protocol.canonical_sha256
        def override(payload):
            seen.append(payload)
            return original(payload)
        monkeypatch.setattr(protocol, "canonical_sha256", override)
    with sqlite3.connect(ledger.path) as db:
        actual = audit(ledger, db)
        actual_count = len(seen)
        seen.clear()
        expected = audit(oracle(ledger), db)
        assert actual == expected and actual_count == len(seen)


@pytest.mark.parametrize("constant,value", [("STREAM", "another-stream"), ("MAX_INTEGER", 1)])
def test_protocol_constant_changed_between_rows_disables_memo(ledger, monkeypatch, constant, value):
    original = module._checked_json
    def run(journal):
        with monkeypatch.context() as patch:
            def checked(raw, digest):
                decoded = original(raw, digest)
                if isinstance(decoded, dict) and decoded.get("sequence") == 2 and "record" in decoded:
                    patch.setattr(protocol, constant, value)
                return decoded
            patch.setattr(module, "_checked_json", checked)
            with sqlite3.connect(ledger.path) as db:
                return audit(journal, db)
    actual, expected = run(ledger), run(oracle(ledger))
    assert actual == expected and actual[0] == "error"


def test_custom_request_hash_is_never_used_for_a_memo_key(ledger, monkeypatch):
    def forbidden(self):
        raise AssertionError("custom Request hash must retain original uncached path")
    monkeypatch.setattr(Request, "__hash__", forbidden)
    with sqlite3.connect(ledger.path) as db:
        assert compare(ledger, db)[0] == "return"


def test_replaced_dto_support_function_disables_memo(ledger, monkeypatch):
    original = protocol.fields
    seen = []
    def fields(kind):
        seen.append(kind)
        return original(kind)
    monkeypatch.setattr(protocol, "fields", fields)
    with sqlite3.connect(ledger.path) as db:
        actual = audit(ledger, db)
        actual_count = len(seen)
        seen.clear()
        expected = audit(oracle(ledger), db)
        assert actual == expected and actual_count == len(seen)


def test_reentrant_audits_do_not_share_validation_memos(ledger, monkeypatch):
    original = module._checked_json
    inside = False
    nested = []
    def checked(raw, digest):
        nonlocal inside
        value = original(raw, digest)
        if not inside and isinstance(value, dict) and value.get("sequence") == 2 and "record" in value:
            inside = True
            with sqlite3.connect(ledger.path) as db:
                nested.append(audit(ledger, db))
        return value
    monkeypatch.setattr(module, "_checked_json", checked)
    with sqlite3.connect(ledger.path) as db:
        outer = audit(ledger, db)
    assert nested == [outer] and outer[0] == "return"
    assert not any("memo" in key for key in vars(ledger))


def test_concurrent_audits_on_one_object_keep_separate_local_state(ledger, monkeypatch):
    original = module._checked_json
    barrier, state = Barrier(2), local()
    def checked(raw, digest):
        value = original(raw, digest)
        if not getattr(state, "synchronized", False) and isinstance(value, dict) and value.get("sequence") == 2 and "record" in value:
            state.synchronized = True
            barrier.wait(timeout=10)
        return value
    monkeypatch.setattr(module, "_checked_json", checked)
    def run():
        with sqlite3.connect(ledger.path.as_uri() + "?mode=ro", uri=True, timeout=0) as db:
            return audit(ledger, db)
    with ThreadPoolExecutor(max_workers=2) as executor:
        one, two = executor.submit(run), executor.submit(run)
        a, b = one.result(timeout=20), two.result(timeout=20)
    assert a == b and a[0] == "return"
    assert not any("memo" in key for key in vars(ledger))
