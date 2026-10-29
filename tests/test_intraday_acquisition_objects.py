"""Offline storage migration boundaries; original clocks and responses survive."""

from tests.intraday_shared_storage_support import require_composed_storage
require_composed_storage()
from dataclasses import asdict, replace
import hashlib
import json
import sqlite3

import pytest

from ml.intraday.acquisition_objects import (
    ResponseObjects, REFERENCE_VERSION, object_root, read_journal_record,
)
from ml.intraday.data import AcquisitionJournal, IntervalResponse, _json


def response(attempt="one", received="2026-10-29T14:16:00+00:00"):
    return IntervalResponse(attempt, "TEST", "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI",
        "2026-10-29T14:00:00+00:00", "2026-10-29T14:15:00+00:00", received, received,
        "success", ({"opened_at": "2026-10-29T14:00:00+00:00", "open": 10.0,
                     "high": 11.0, "low": 9.0, "close": 10.5, "volume": 2.0},),
        "2026-10-29T14:00:00+00:00", "2026-10-29T14:15:00+00:00",
        native_response={"schema": {"fields": []}, "data": [{"provider_id": 42}]})


def original(value):
    record = asdict(value)
    if record["failure_diagnostics"] is None:
        record.pop("failure_diagnostics")
    return _json(record)


def test_repeated_content_shares_one_payload_but_retains_attempt_clocks(tmp_path):
    objects = ResponseObjects(tmp_path)
    first, second = response(), response("two", "2026-10-29T14:17:00+00:00")
    refs = [objects.preserve(original(item)) for item in (first, second)]
    assert refs[0] != refs[1]
    assert len(list((tmp_path / "payloads").glob("*.json"))) == 1
    assert len(list((tmp_path / "attempts").glob("*.json"))) == 2
    for item, ref in zip((first, second), refs):
        raw = objects.restore(ref)
        assert raw == original(item).encode()
        assert ref["response_sha256"] == hashlib.sha256(raw).hexdigest()
        assert objects.preserve(raw) == ref
    corrected = replace(second, rows=({**second.rows[0], "close": 10.75},))
    assert objects.restore(objects.preserve(original(corrected))) == original(corrected).encode()
    assert len(list((tmp_path / "payloads").glob("*.json"))) == 2


def test_journals_share_canonical_root_and_no_full_response_in_db_or_receipt(tmp_path, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *args, **kwargs: {"offline": True})
    paths = [tmp_path / "intraday/acquisition.sqlite3", tmp_path / "intraday/system-1a/acquisition.sqlite3"]
    assert object_root(paths[0]) == object_root(paths[1])
    for path in paths:
        journal = AcquisitionJournal(path, reference_writes=True)
        journal.save(response())
        journal.save(response())
        with sqlite3.connect(path) as db:
            row, = db.execute("SELECT payload FROM attempts").fetchall()
            reference = json.loads(row[0])
            assert reference["schema_version"] == REFERENCE_VERSION and "rows" not in reference
            receipt_path, = db.execute("SELECT receipt_path FROM attempt_provenance").fetchone()
        receipt = json.loads(__import__("pathlib").Path(receipt_path).read_text())
        assert "response" not in receipt and receipt["response_ref"] == reference
        assert _json(read_journal_record(path, row[0])) == original(response())
        restored, = journal.responses(symbol="TEST", source_id=response().source_id,
            interval_open=response().interval_open, interval_close=response().interval_close)
        assert original(restored) == original(response())
    assert len(list((object_root(paths[0]) / "payloads").glob("*.json"))) == 1
    assert len(list((object_root(paths[0]) / "attempts").glob("*.json"))) == 1


def test_legacy_original_stays_inline_and_new_records_roundtrip(tmp_path, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *args, **kwargs: {})
    journal = AcquisitionJournal(tmp_path / "journal.sqlite3", reference_writes=True)
    item = response()
    slot = journal.slot(item.symbol, item.source_id, item.interval_open, item.interval_close)
    with sqlite3.connect(journal.path) as db:
        db.execute("INSERT INTO attempts VALUES(?,?,?)", (item.attempt_id, slot, original(item)))
    journal.save(item)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT payload FROM attempts").fetchone()[0] == original(item)
    journal.save(response("two"))
    restored = journal.responses(symbol=item.symbol, source_id=item.source_id,
        interval_open=item.interval_open, interval_close=item.interval_close)
    assert [r.attempt_id for r in restored] == ["one", "two"]
    with pytest.raises(ValueError, match="immutable"):
        journal.save(replace(item, error="changed"))


def test_corruption_missing_objects_and_invalid_references_fail_closed(tmp_path):
    objects = ResponseObjects(tmp_path)
    ref = objects.preserve(original(response()))
    with pytest.raises(ValueError, match="identity"):
        objects.restore({**ref, "object_sha256": "../outside"})
    with pytest.raises(ValueError, match="reference"):
        objects.restore({**ref, "response_bytes": True})
    with pytest.raises(ValueError, match="checksum"):
        objects.restore({**ref, "response_sha256": "0" * 64})
    path = next((tmp_path / "payloads").glob("*.json"))
    path.write_bytes(b"{}")
    with pytest.raises(ValueError, match="checksum"):
        objects.restore(ref)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        objects.restore(ref)


def test_legacy_retry_state_migration_reads_reference_without_changing_attempt(tmp_path, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *args, **kwargs: {})
    path = tmp_path / "journal.sqlite3"
    journal = AcquisitionJournal(path, reference_writes=True)
    item = replace(response(), rows=(), native_response=None, status="temporary_error", retry_after_seconds=20)
    journal.save(item)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE provider_waits")
        before = db.execute("SELECT payload FROM attempts").fetchone()[0]
    restored = AcquisitionJournal(path)
    assert restored.provider_retry_due().isoformat() == "2026-10-29T14:16:20+00:00"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT payload FROM attempts").fetchone()[0] == before


def test_legacy_writer_remains_readable_by_loaded_legacy_processes(tmp_path, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *args, **kwargs: {})
    journal = AcquisitionJournal(tmp_path / "journal.sqlite3", reference_writes=False)
    journal.save(response())
    with sqlite3.connect(journal.path) as db:
        assert json.loads(db.execute("SELECT payload FROM attempts").fetchone()[0])["rows"] == list(response().rows)
    receipt = json.loads(next((tmp_path / "acquisition-receipts").rglob("*.json")).read_bytes())
    assert receipt["response"]["request_ended_at"] == response().request_ended_at
    assert "response_ref" not in receipt
    assert not object_root(journal.path).exists()


def test_default_writer_roundtrips_compact_attempt_and_original_receipt_clock(tmp_path, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *args, **kwargs: {})
    path = tmp_path / "intraday/system-1a/acquisition.sqlite3"
    journal = AcquisitionJournal(path)
    item = response()
    journal.save(item)
    with sqlite3.connect(path) as db:
        encoded = db.execute("SELECT payload FROM attempts").fetchone()[0]
    reference = json.loads(encoded)
    assert reference["schema_version"] == REFERENCE_VERSION and "rows" not in reference
    assert ResponseObjects(object_root(path)).restore(reference) == original(item).encode()
    reopened = AcquisitionJournal(path)
    restored, = reopened.responses(symbol=item.symbol, source_id=item.source_id,
        interval_open=item.interval_open, interval_close=item.interval_close)
    assert original(restored) == original(item)
    assert restored.request_started_at == item.request_started_at
    assert restored.request_ended_at == item.request_ended_at
