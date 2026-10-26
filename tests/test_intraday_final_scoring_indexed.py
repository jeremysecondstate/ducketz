"""Finalization retains evidence semantics while avoiding repeated model I/O."""
from contextlib import contextmanager
from datetime import timedelta
import json
import re
import socket

import pytest

from ml.intraday.final_scoring import score_saved_session_outcomes
from ml.intraday.prediction import ContractError, STEP, digest, encoded, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


CLOSE = instant('2026-10-15T00:00:00Z')
NOW = CLOSE + timedelta(seconds=5)
SESSION = 'manual-final-indexed-fixture'
SOURCE = 'databento:EQUS.MINI:ohlcv-1m'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Finalization fixtures must not contact services')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def own(store, *, cycle='own', session=SESSION, actor='Atlas', symbol='AAPL',
        reference=None, issued_at=None, source=SOURCE, mode='live', reference_open='100'):
    reference = reference or CLOSE - 3 * STEP
    store.begin_cycle(cycle, dict(pc=actor, mode=mode, experiment=session, symbols=[symbol],
        reference_at=reference.isoformat(), configuration_hash='fixture', started_at=reference.isoformat()))
    return store.save_forecast(forecast(pc=actor, symbol=symbol, cycle_id=cycle, mode=mode,
        reference_at=reference.isoformat(), data_through=(reference + STEP).isoformat(),
        issued_at=(issued_at or reference + STEP + timedelta(seconds=20)).isoformat(),
        information_available_at=(reference + STEP + timedelta(seconds=10)).isoformat(),
        target_open=(reference + 2 * STEP).isoformat(), target_close=(reference + 3 * STEP).isoformat(),
        source_id=source, source_dataset='EQUS.MINI', reference_open=reference_open,
        diagnostics={'retained_scientific_tree': 'large-model-evidence ' * 25000}))


def candle(*, symbol='AAPL', source=SOURCE, close=100.5, available=None, opened=None):
    return dict(symbol=symbol, opened_at=(opened or CLOSE - STEP).isoformat(),
        available_at=(available or CLOSE + timedelta(seconds=4)).isoformat(),
        source_id=source, source_dataset='EQUS.MINI', availability_kind='actual',
        synthetic=False, no_trade=False, open=100., high=102., low=98., close=close,
        volume=100., evidence_json='{}', interval_seconds=900)


def score(store, **changes):
    options = dict(session_id=SESSION, actor='Atlas', symbols=('AAPL',), as_of=NOW)
    options.update(changes)
    return score_saved_session_outcomes(store, **options)


def trace_payload_reads(store, monkeypatch):
    statements, connect = [], store.connect
    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(store, 'connect', traced)
    return statements


def original_reads(statements):
    return [v for v in statements if re.search(r'\bSELECT\b.*\bpayload\b.*\bFROM forecasts\b', v, re.I | re.S)]


def test_repeated_finalization_preserves_counts_without_unchanged_original_reads(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path/'warm.sqlite3')
    original = own(store)
    store.record_candle(candle())
    expected = dict(session_id=SESSION, scored=1, synthetic=0, unavailable=0)
    assert score(store) == expected
    before = store.revisions(original['forecast_id'])
    statements = trace_payload_reads(store, monkeypatch)
    monkeypatch.setattr(store, 'forecast', lambda *a: pytest.fail('Unchanged final target reread its original'))
    assert score(store, as_of=NOW + timedelta(seconds=5)) == expected
    assert original_reads(statements) == []
    assert store.revisions(original['forecast_id']) == before


def test_new_finalizer_process_reuses_attested_reference_open_proof(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path/'durable.sqlite3')
    original = own(store, reference_open=100.0)
    store.record_candle(candle())
    assert score(store)['scored'] == 1
    other = ShadowStore(store.path)
    statements = trace_payload_reads(other, monkeypatch)
    assert score(other)['scored'] == 1
    assert original_reads(statements) == []
    assert other.forecast(original['cycle_id'], 'AAPL') == original


@pytest.mark.parametrize('future', [False, True])
def test_unavailable_or_future_original_corruption_is_not_hidden(tmp_path, future):
    store = ShadowStore(tmp_path/'corrupt-original.sqlite3')
    original = own(store, reference=CLOSE - (2 if future else 3) * STEP)
    score(store)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=json_set(payload,'$.reference_open','999') WHERE id=?",
                   (original['forecast_id'],))
    with pytest.raises(ContractError, match='hash or indexed identity'):
        score(store)


def test_damaged_indexed_pc_does_not_escape_own_cycle_validation(tmp_path):
    store = ShadowStore(tmp_path/'pc.sqlite3')
    original = own(store)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET pc='Scout' WHERE id=?", (original['forecast_id'],))
    with pytest.raises(ContractError, match='hash or indexed identity'):
        score(store)


def test_source_none_keeps_exact_match_and_unavailable_count(tmp_path):
    store = ShadowStore(tmp_path/'none-source.sqlite3')
    original = own(store, source=None)
    store.record_candle(candle())
    assert score(store) == dict(session_id=SESSION, scored=0, synthetic=0, unavailable=1)
    assert store.revisions(original['forecast_id']) == []


def test_corrupt_future_candle_still_rejects_finalization(tmp_path):
    store = ShadowStore(tmp_path/'future-candle.sqlite3')
    own(store)
    key = store.record_candle(candle(available=NOW + timedelta(minutes=1)))
    with store.connect() as db:
        db.execute("UPDATE candles SET payload=json_set(payload,'$.close',99) WHERE id=?", (key,))
    with pytest.raises(ContractError, match='candle evidence hash'):
        score(store)


def test_valid_hash_wrong_derived_outcome_uses_ordinary_recomputation(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path/'derived.sqlite3')
    original = own(store)
    target = candle()
    store.record_candle(target)
    assert score(store)['scored'] == 1
    value = dict(store.revisions(original['forecast_id'])[-1])
    value.pop('revision_id')
    value.pop('observed_at')
    value.update(status='synthetic', target_not_down=False, reference_to_target_return='-0.1')
    key = digest(value)
    forged = dict(value, revision_id=key, observed_at=NOW.isoformat())
    with store.connect() as db:
        db.execute('INSERT INTO outcomes VALUES(?,?,?,?)', (key, original['forecast_id'], NOW.isoformat(), encoded(forged)))
    observed, observe = [], store.observe
    def recorded(*args, **kwargs):
        observed.append(True)
        return observe(*args, **kwargs)
    monkeypatch.setattr(store, 'observe', recorded)
    assert score(store)['scored'] == 1
    assert observed == [True]


def test_finalizer_preserves_latest_only_ancestry_scope(tmp_path):
    store = ShadowStore(tmp_path/'ancestry.sqlite3')
    original = own(store)
    old = store.observe(original, as_of=CLOSE)
    store.record_candle(candle())
    assert score(store)['scored'] == 1
    with store.connect() as db:
        db.execute("UPDATE outcomes SET payload=json_set(payload,'$.status','wrong') WHERE id=?", (old['revision_id'],))
    # The ordinary one-shot observe path reads latest plus any matching
    # revision; a distinct unread older revision is outside this API's scope.
    assert score(store)['scored'] == 1


def test_cached_count_rechecks_original_changed_after_initial_inventory(tmp_path, monkeypatch):
    from ml.intraday import final_scoring
    store = ShadowStore(tmp_path/'between-reads.sqlite3')
    original = own(store)
    store.record_candle(candle())
    assert score(store)['scored'] == 1
    inventory = final_scoring._session_forecast_headers
    def change_after_inventory(*args, **kwargs):
        headers = inventory(*args, **kwargs)
        with store.connect() as db:
            db.execute("UPDATE forecasts SET payload=json_set(payload,'$.reference_open','999') WHERE id=?",
                       (original['forecast_id'],))
        return headers
    monkeypatch.setattr(final_scoring, '_session_forecast_headers', change_after_inventory)
    with pytest.raises(ContractError, match='hash or indexed identity'):
        score(store)


def test_manual_stop_after_cached_recheck_prevents_count(tmp_path, monkeypatch):
    from ml.intraday import final_scoring
    store = ShadowStore(tmp_path/'stop-after-recheck.sqlite3')
    own(store)
    store.record_candle(candle())
    assert score(store)['scored'] == 1
    stopped = []
    cached = final_scoring._cached_outcome
    def stop_after_recheck(*args, **kwargs):
        value = cached(*args, **kwargs)
        stopped.append(True)
        return value
    monkeypatch.setattr(final_scoring, '_cached_outcome', stop_after_recheck)
    assert score(store, stopped=lambda: bool(stopped)) == dict(session_id=SESSION, scored=0, synthetic=0, unavailable=0)


def test_future_issue_and_other_scopes_retain_original_counts(tmp_path):
    store = ShadowStore(tmp_path/'scope.sqlite3')
    own(store, cycle='future', issued_at=NOW + timedelta(seconds=1))
    own(store, cycle='other-session', session='previous')
    own(store, cycle='other-actor', actor='Scout')
    own(store, cycle='other-symbol', symbol='AMZN')
    own(store, cycle='other-mode', mode='historical_replay')
    store.record_candle(candle())
    assert score(store) == dict(session_id=SESSION, scored=0, synthetic=0, unavailable=0)


def test_stop_prevents_original_or_candle_reads(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path/'stopped.sqlite3')
    own(store)
    monkeypatch.setattr(store, '_revision_forecast_headers', lambda *a, **k: pytest.fail('Stopped original inventory read'))
    monkeypatch.setattr(store, 'observed_candles', lambda *a, **k: pytest.fail('Stopped candle read'))
    assert score(store, stopped=lambda: True) == dict(session_id=SESSION, scored=0, synthetic=0, unavailable=0)


def test_revision_markers_remain_mandatory_for_unchanged_scored_originals(tmp_path):
    store = ShadowStore(tmp_path/'markers.sqlite3')
    own(store)
    store.record_candle(candle())
    assert score(store)['scored'] == 1
    with store.connect() as db:
        db.execute('DROP TRIGGER execution_forecast_update')
    with pytest.raises(ContractError, match='[Ee]xecution forecast'):
        score(store)


def test_missing_reference_open_projection_keeps_full_original_fallback(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path/'old-projection.sqlite3')
    own(store)
    store.record_candle(candle())
    assert score(store)['scored'] == 1
    headers = store._revision_forecast_headers
    def old_projection(*args, **kwargs):
        return [{k:v for k,v in row.items() if k != 'reference_open'}
                for row in headers(*args, **kwargs)]
    monkeypatch.setattr(store, '_revision_forecast_headers', old_projection)
    observed, observe = [], store.observe
    def full_fallback(*args, **kwargs):
        observed.append(True)
        return observe(*args, **kwargs)
    monkeypatch.setattr(store, 'observe', full_fallback)
    assert score(store)['scored'] == 1
    assert observed == [True]
