"""Bounded regressions for the observed 429MB repeated candle-processing path."""
import json

import pytest

from ml.intraday import data, live_session
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.scoring_candles import ScoringCandleReader
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


def saved(store, **changes):
    return store.save_forecast(forecast(mode='live', pc='Scout', **changes))


def run(store, *, turns=2, pause=None, clock=lambda: '2026-10-09T00:01Z', stopped=None):
    polls = []
    def wait(seconds):
        polls.append(seconds)
        if pause:
            pause(len(polls))
    live_session.scoring_loop(store, histories={}, provider=None, journal=None, pc='Scout',
        session_id='candle-read-test', clock=clock, pause=wait,
        stopped=stopped or (lambda: len(polls) >= turns))
    return polls


def count_reads(reader, monkeypatch):
    calls = []
    original = reader._read
    def read(identity):
        calls.append(tuple(identity))
        return original(identity)
    monkeypatch.setattr(reader, '_read', read)
    return calls


def test_large_originals_and_proofs_are_never_reprocessed_on_unchanged_poll(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    # Bounded analog of the retained-input 429,606,769-byte episode: large
    # original-only evidence and counters, rather than allocating 429MB in CI.
    for i in range(4):
        store.record_candle(candle(symbol='SYM' + str(i), evidence_json=json.dumps({'large': 'x' * 262144})))
    continuity_calls = []
    original = data.synthetic_continuity_verified
    def verify(value):
        continuity_calls.append(value['symbol'])
        return original(value)
    monkeypatch.setattr(data, 'synthetic_continuity_verified', verify)
    reader = ScoringCandleReader(store)
    reads = count_reads(reader, monkeypatch)
    first = list(reader)
    assert len(reads) == len(continuity_calls) == 4
    assert list(reader) == first
    assert len(reads) == len(continuity_calls) == 4
    assert reader._cache_bytes < 4096
    assert 'large' not in repr(reader._cache) and 'evidence_json' not in repr(reader._cache)
    assert 'evidence_json' in reader.original(first[0])
    assert len(reads) == len(continuity_calls) == 5
    first[0]['candle']['close'] = '999'
    assert list(reader)[0]['candle']['close'] == '100'
    with pytest.raises(ContractError, match='changed before outcome mutation'):
        reader.original(first[0])


@pytest.mark.parametrize('column', ['symbol', 'opened_at', 'available_at'])
def test_changed_index_is_revalidated_before_routing(tmp_path, column):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    key = store.record_candle(candle())
    reader = ScoringCandleReader(store)
    list(reader)
    with store.connect() as db:
        db.execute('UPDATE candles SET ' + column + '=? WHERE id=?', ('changed', key))
    with pytest.raises(ContractError, match='indexed identity'):
        list(reader)


def test_delete_and_rowid_reuse_new_and_same_time_revisions_keep_sql_order(tmp_path):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    first = store.record_candle(candle())
    reader = ScoringCandleReader(store)
    assert list(reader)[0]['candle_id'] == first
    with store.connect() as db:
        rowid = db.execute('SELECT rowid FROM candles').fetchone()[0]
        db.execute('DELETE FROM candles')
    assert list(reader) == [] and reader._cache_bytes == 0
    second = store.record_candle(candle(close='101'))
    third = store.record_candle(candle(close='99'))
    with store.connect() as db:
        assert db.execute('SELECT rowid FROM candles WHERE id=?', (second,)).fetchone()[0] == rowid
    assert [s['candle_id'] for s in reader] == [second, third]
    assert first not in reader._cache


@pytest.mark.parametrize('change', ['payload', 'index', 'delete'])
def test_selected_original_is_checked_again_before_any_outcome_write(tmp_path, change):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    f = saved(store)
    key = store.record_candle(candle())
    reader = ScoringCandleReader(store)
    selection = list(reader)[0]
    with store.connect() as db:
        if change == 'payload':
            value = json.loads(db.execute('SELECT payload FROM candles WHERE id=?', (key,)).fetchone()[0])
            value['close'] = '999'
            db.execute('UPDATE candles SET payload=? WHERE id=?', (encoded(value), key))
        elif change == 'index':
            db.execute("UPDATE candles SET symbol='OTHER' WHERE id=?", (key,))
        else:
            db.execute('DELETE FROM candles WHERE id=?', (key,))
    with pytest.raises(ContractError):
        store.observe(f, as_of='2026-10-09T00:01Z', candle=reader.original(selection))
    assert store.revisions(f['forecast_id']) == []


@pytest.mark.parametrize('limit', ['MAX_BYTES', 'MAX_ENTRIES'])
def test_cache_overflow_retains_correct_processing_without_unbounded_storage(tmp_path, monkeypatch, limit):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    for i in range(3):
        store.record_candle(candle(symbol='SYM' + str(i)))
    monkeypatch.setattr(ScoringCandleReader, limit, 1)
    reader = ScoringCandleReader(store)
    assert len(list(reader)) == len(list(reader)) == 3
    assert len(reader._cache) <= reader.MAX_ENTRIES and reader._cache_bytes <= reader.MAX_BYTES


def test_unchanged_scored_poll_has_no_original_reads_or_duplicate_writes(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    f = saved(store)
    key = store.record_candle(candle())
    writes = []
    observe = store.observe
    def write(*args, **kwargs):
        writes.append(kwargs['candle'])
        return observe(*args, **kwargs)
    monkeypatch.setattr(store, 'observe', write)
    reads = []
    read = ScoringCandleReader._read
    def tracked(self, identity):
        reads.append(identity[1])
        return read(self, identity)
    monkeypatch.setattr(ScoringCandleReader, '_read', tracked)
    run(store, turns=3)
    assert len(writes) == 1 and reads == [key, key]
    assert store.revisions(f['forecast_id'])[0]['candle_id'] == key == digest(writes[0])


def test_same_time_correction_and_later_new_rows_remain_visible(tmp_path):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    f = saved(store)
    first = store.record_candle(candle(close='101'))
    inserted = []
    def pause(turn):
        if turn == 1:
            inserted.append(store.record_candle(candle(close='99')))
        if turn == 2:
            inserted.append(store.record_candle(candle(close='102', high='103', available_at='2026-10-08T18:00:00Z')))
    run(store, turns=3, pause=pause)
    outcomes = store.revisions(f['forecast_id'])
    assert [o['candle_id'] for o in outcomes] == [first, *inserted]
    assert [o['target_not_down'] for o in outcomes] == [True, False, True]


def test_cold_audit_rechecks_original_before_event_and_warm_poll_deduplicates(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    key = store.record_candle(candle(synthetic=True, evidence_json='{}'))
    reads = []
    read = ScoringCandleReader._read
    def tracked(self, identity):
        reads.append(identity[1])
        return read(self, identity)
    monkeypatch.setattr(ScoringCandleReader, '_read', tracked)
    run(store, turns=3)
    with store.connect() as db:
        events = db.execute("SELECT payload FROM events WHERE kind='unverified_synthetic_outcome_requires_audit'").fetchall()
    assert len(events) == 1 and json.loads(events[0][0])['candle_hash'] == key
    assert reads == [key, key]


@pytest.mark.parametrize('audit', [False, True])
def test_corrupt_selected_original_fails_before_audit_or_outcome_write(tmp_path, monkeypatch, audit):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    f = saved(store)
    key = store.record_candle(candle(synthetic=audit, evidence_json='{}'))
    original = ScoringCandleReader.original
    def corrupt(self, selection):
        with store.connect() as db:
            value = json.loads(db.execute('SELECT payload FROM candles WHERE id=?', (key,)).fetchone()[0])
            value['close'] = '999'
            db.execute('UPDATE candles SET payload=? WHERE id=?', (encoded(value), key))
        return original(self, selection)
    monkeypatch.setattr(ScoringCandleReader, 'original', corrupt)
    with pytest.raises(ContractError, match='hash or indexed identity'):
        run(store, turns=1)
    assert store.revisions(f['forecast_id']) == []
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 0


def test_stop_during_original_validation_prevents_outcome_write(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    f = saved(store)
    store.record_candle(candle())
    stopped = []
    original = ScoringCandleReader.original
    def read(self, selection):
        value = original(self, selection)
        stopped.append(True)
        return value
    monkeypatch.setattr(ScoringCandleReader, 'original', read)
    run(store, stopped=lambda: bool(stopped))
    assert stopped and store.revisions(f['forecast_id']) == []


def test_future_availability_and_original_full_candle_dedup_api(tmp_path):
    store = ShadowStore(tmp_path / 'candles.sqlite3')
    f = saved(store)
    original = candle()
    store.record_candle(original)
    now = ['2026-10-08T17:45:03Z']
    def pause(turn):
        if turn == 1:
            assert store.revisions(f['forecast_id']) == []
            now[0] = '2026-10-08T18:00:00Z'
    run(store, pause=pause, clock=lambda: now[0])
    revisions = store.revisions(f['forecast_id'])
    assert len(revisions) == 1
    assert live_session._outcome_already_observed(f, store.observed_candles()[0], revisions, as_of=now[0])
