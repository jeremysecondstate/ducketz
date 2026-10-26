"""Canonical row validation retains all protocol and journal corruption checks."""
import hashlib
import json
import sqlite3

import pytest

from ml.intraday import scout_journal as journal
from test_intraday_scout_journal import accepted, observe, order_evidence


def original_checked_json(payload, digest):
    """Recorded pre-optimization contract, including exception wrapping."""
    try:
        value = json.loads(payload)
        if journal._encoded(value) != payload or journal.canonical_sha256(value) != digest:
            raise journal.JournalCorruptionError("Stored payload hash or canonical bytes differ")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise journal.JournalCorruptionError("Invalid stored canonical payload") from exc


def exception_chain(error):
    return (type(error), str(error),
            exception_chain(error.__cause__) if error.__cause__ else None)


def outcome(call, payload, digest):
    try:
        return ("accepted", call(payload, digest))
    except Exception as error:
        return ("rejected", exception_chain(error))


@pytest.mark.parametrize("value", [
    {}, {"unicode": "雪☃é", "newline": "a\nb"}, {"n": -(2**63)},
    {"n": 2**63-1}, {"n": -0.0}, {"n": 5e-324}, {"v": True},
    {"v": None}, {"list": [1, False, None, {"x": "ok"}]},
])
def test_canonical_rows_and_changed_hashes_keep_exact_contract(value):
    payload = journal._encoded(value)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert journal._checked_json(payload, digest) == value
    for altered in ("0" * 64, "", None, 1, b"0" * 64):
        assert outcome(journal._checked_json, payload, altered) == outcome(
            original_checked_json, payload, altered)


@pytest.mark.parametrize("payload", [
    "{", "", "null", "true", "1", '"x"', "[]",
    '{"x":1,"x":2}', ' {"x":1}', '{"x":1} ', '{"x":1}\n',
    '{"z":1,"a":2}', '{"x": 1}', '{"x":NaN}', '{"x":Infinity}',
    '{"x":-Infinity}', '{"x":9223372036854775808}',
    '{"x":-9223372036854775809}', '{"x":"\\u96ea"}',
    '{"x":"\\ud800"}', '{"x":1e999}', None, {}, 3,
    b'{"x":1}', bytearray(b'{"x":1}'), b"\xff",
    '{"x":' * 1100 + '{}' + '}' * 1100,
])
def test_malformed_or_noncanonical_rows_keep_exact_exception_chain(payload):
    actual = outcome(journal._checked_json, payload, "0" * 64)
    assert actual == outcome(original_checked_json, payload, "0" * 64)
    assert actual[0] == "rejected"


@pytest.mark.parametrize("size", [1024*1024-1, 1024*1024, 1024*1024+1])
@pytest.mark.parametrize("unicode", [False, True])
def test_one_mib_limit_applies_to_exact_utf8_bytes(size, unicode):
    # Compact {"x":""} occupies eight bytes; fill the exact remainder.
    remaining = size - 8
    text = ("雪" * (remaining // 3) + "a" * (remaining % 3)
            if unicode else "a" * remaining)
    payload = json.dumps({"x": text}, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    assert len(payload.encode("utf-8")) == size
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    actual = outcome(journal._checked_json, payload, digest)
    assert actual == outcome(original_checked_json, payload, digest)
    assert actual[0] == ("accepted" if size <= 1024*1024 else "rejected")


def test_each_stored_row_uses_one_complete_protocol_validation(monkeypatch):
    original = journal.canonical_sha256
    calls = []
    def validated(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(journal, "canonical_sha256", validated)
    payload = '{"nested":[1,{"x":"雪"}]}'
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert journal._checked_json(payload, digest) == {"nested": [1, {"x": "雪"}]}
    assert len(calls) == 1
    # Every subsequent row is still validated; there is no digest/payload cache.
    assert journal._checked_json(payload, digest) == {"nested": [1, {"x": "雪"}]}
    assert len(calls) == 2


@pytest.mark.parametrize("replacement", [None, b"different bytes"])
def test_hash_uses_validated_canonical_string_not_input_subclass(replacement):
    class Payload(str):
        def encode(self, *args, **kwargs):
            if replacement is None:
                raise AssertionError("The untrusted input encoder must not run")
            return replacement
    raw = '{"x":"雪"}'
    payload = Payload(raw)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert journal._checked_json(payload, digest) == {"x": "雪"}
    assert outcome(journal._checked_json, payload, digest) == outcome(
        original_checked_json, payload, digest)


def audit(ledger, checker, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(journal, "_checked_json", checker)
        with ledger._transaction() as db:
            return ledger._audit(db, include_lifecycle=True)


@pytest.mark.parametrize("status,filled", [
    ("WORKING", 0), ("PARTIAL", 1), ("FILLED", 2),
    ("CANCELLED", 1), ("REJECTED", 0),
])
def test_native_full_replay_and_lifecycle_output_match(tmp_path, monkeypatch, status, filled):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence(status=status, filled=filled))
    before = ledger.path.read_bytes()
    expected = audit(ledger, original_checked_json, monkeypatch)
    assert audit(ledger, journal._checked_json, monkeypatch) == expected
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("table", [
    "control", "records", "events", "lifecycle_control",
    "lifecycle_records", "lifecycle_events",
])
def test_native_audit_still_rejects_each_corrupted_payload_chain(tmp_path, monkeypatch, table):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence(status="FILLED", filled=2))
    with sqlite3.connect(ledger.path) as db:
        db.execute(f"UPDATE {table} SET sha256='corrupt' WHERE rowid="
                   f"(SELECT min(rowid) FROM {table})")
        db.commit()
    before = ledger.path.read_bytes()
    results = []
    for checker in (original_checked_json, journal._checked_json):
        try:
            audit(ledger, checker, monkeypatch)
        except journal.JournalCorruptionError as error:
            results.append(exception_chain(error))
        else:
            pytest.fail("Corruption was accepted")
    assert results[0] == results[1]
    assert ledger.path.read_bytes() == before
