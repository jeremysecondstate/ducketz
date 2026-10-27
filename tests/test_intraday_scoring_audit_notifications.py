"""Diagnostic dedup across workers preserves cold original/scientific checks."""
import json

import pytest

from ml.intraday import live_session
from ml.intraday.prediction import ContractError, encoded
from ml.intraday.scoring_candles import ScoringCandleReader
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast

KIND = "unverified_synthetic_outcome_requires_audit"


def run(store, session="manual-audit", *, turns=2, stopped=None):
    polls = []
    live_session.scoring_loop(store, histories={}, provider=None, journal=None,
        pc="Scout", session_id=session, clock=lambda: "2026-10-09T00:01Z",
        pause=lambda seconds: polls.append(seconds),
        stopped=stopped or (lambda: len(polls) >= turns))


def notices(store):
    with store.connect() as db:
        return db.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (KIND,)).fetchall()


def originals(store):
    with store.connect() as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in ("candles", "forecasts", "outcomes", "intents", "snapshots", "symbol_inputs")}


def test_replaced_worker_keeps_notice_once_and_revalidates_original_each_time(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    key = store.record_candle(candle(synthetic=True, evidence_json="{}"))
    store.save_forecast(forecast())
    before = originals(store)
    checks, scans = [], []
    original = ScoringCandleReader.original
    seed = store.scoring_audit_notices
    def checked(self, selection):
        checks.append(selection["candle_id"])
        return original(self, selection)
    def seeded(session):
        scans.append(session)
        return seed(session)
    monkeypatch.setattr(ScoringCandleReader, "original", checked)
    monkeypatch.setattr(store, "scoring_audit_notices", seeded)
    run(store, turns=3)
    first = notices(store)
    run(store, turns=3)
    assert notices(store) == first and len(first) == 1
    assert checks == [key, key] and scans == ["manual-audit", "manual-audit"]
    assert originals(store) == before


def test_new_logical_session_new_hash_and_new_symbol_keep_their_notices(tmp_path):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    first = store.record_candle(candle(synthetic=True, evidence_json="{}"))
    run(store)
    run(store, session="another-manual-run")
    changed = store.record_candle(candle(synthetic=True, evidence_json="{}", close="99"))
    other = store.record_candle(candle(symbol="OTHER", synthetic=True, evidence_json="{}"))
    run(store)
    assert store.scoring_audit_notices("manual-audit") == {
        ("AAPL", first), ("AAPL", changed), ("OTHER", other)}
    assert store.scoring_audit_notices("another-manual-run") == {("AAPL", first)}
    assert len(notices(store)) == 4


def test_existing_duplicate_notices_are_retained_byte_for_byte(tmp_path):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    key = store.record_candle(candle(synthetic=True, evidence_json="{}"))
    payload = {"candle_hash": key, "immutable_original_retained": True}
    for _ in range(3):
        store.event("manual-audit", KIND, payload, symbol="AAPL")
    before = notices(store)
    run(store)
    assert notices(store) == before and len(before) == 3


@pytest.mark.parametrize("raw,symbol", [
    ("invalid-json", "AAPL"),
    ("[]", "AAPL"),
    ("null", "AAPL"),
    ("{}", "AAPL"),
    (json.dumps({"candle_hash": "a" * 64, "immutable_original_retained": True, "extra": 1}), "AAPL"),
    (encoded({"candle_hash": "a" * 64, "immutable_original_retained": False}), "AAPL"),
    (encoded({"candle_hash": "a" * 64, "immutable_original_retained": 1}), "AAPL"),
    (encoded({"candle_hash": "A" * 64, "immutable_original_retained": True}), "AAPL"),
    (encoded({"candle_hash": "g" * 64, "immutable_original_retained": True}), "AAPL"),
    (encoded({"candle_hash": "a" * 63, "immutable_original_retained": True}), "AAPL"),
    (encoded({"candle_hash": 1, "immutable_original_retained": True}), "AAPL"),
    (json.dumps({"candle_hash": "a" * 64, "immutable_original_retained": True}, indent=2), "AAPL"),
    ('{"candle_hash":"' + "a" * 64 + '","immutable_original_retained":false,"immutable_original_retained":true}', "AAPL"),
    (encoded({"candle_hash": "a" * 64, "immutable_original_retained": True}), ""),
    (encoded({"candle_hash": "a" * 64, "immutable_original_retained": True}), " "),
    (encoded({"candle_hash": "a" * 64, "immutable_original_retained": True}), None),
])
def test_malformed_historical_notices_cannot_seed_notification_set(tmp_path, raw, symbol):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    with store.connect() as db:
        db.execute("INSERT INTO events(cycle_id,symbol,recorded_at,kind,payload) VALUES(?,?,?,?,?)",
            ("manual-audit", symbol, "2026-10-08T18:00:00Z", KIND, raw))
    before = notices(store)
    assert store.scoring_audit_notices("manual-audit") == set()
    assert notices(store) == before


@pytest.mark.parametrize("case", ["foreign-session", "foreign-kind", "foreign-symbol", "retained-number"])
def test_unrelated_or_false_notice_does_not_suppress_exact_notice(tmp_path, case):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    key = store.record_candle(candle(synthetic=True, evidence_json="{}"))
    store.event("other" if case == "foreign-session" else "manual-audit",
        "other" if case == "foreign-kind" else KIND,
        {"candle_hash": key, "immutable_original_retained": 1 if case == "retained-number" else True},
        symbol="OTHER" if case == "foreign-symbol" else "AAPL")
    with store.connect() as db:
        before = db.execute("SELECT * FROM events ORDER BY id").fetchall()
    run(store)
    with store.connect() as db:
        after = db.execute("SELECT * FROM events ORDER BY id").fetchall()
    assert after[:-1] == before
    assert json.loads(after[-1][-1]) == {"candle_hash": key, "immutable_original_retained": True}
    assert after[-1][1:3] == ("manual-audit", "AAPL")


def test_replaced_worker_rejects_original_corruption_despite_prior_notice(tmp_path):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    key = store.record_candle(candle(synthetic=True, evidence_json="{}"))
    run(store)
    previous = notices(store)
    with store.connect() as db:
        changed = json.loads(db.execute("SELECT payload FROM candles WHERE id=?", (key,)).fetchone()[0])
        changed["close"] = "999"
        db.execute("UPDATE candles SET payload=? WHERE id=?", (encoded(changed), key))
    before = originals(store)
    with pytest.raises(ContractError, match="hash or indexed identity"):
        run(store)
    assert notices(store) == previous and originals(store) == before


def test_stop_during_revalidation_does_not_write_notice(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    store.record_candle(candle(synthetic=True, evidence_json="{}"))
    stopped = []
    original = ScoringCandleReader.original
    def checked(self, selection):
        value = original(self, selection)
        stopped.append(True)
        return value
    monkeypatch.setattr(ScoringCandleReader, "original", checked)
    run(store, stopped=lambda: bool(stopped))
    assert notices(store) == []


def test_legacy_store_without_notice_reader_retains_previous_behavior(tmp_path):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    store.record_candle(candle(synthetic=True, evidence_json="{}"))
    class LegacyStore:
        def __getattr__(self, name):
            if name == "scoring_audit_notices":
                raise AttributeError(name)
            return getattr(store, name)
    run(LegacyStore())
    run(LegacyStore())
    assert len(notices(store)) == 2
