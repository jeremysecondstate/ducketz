"""Offline regressions for byte-bound frozen-snapshot validation witnesses."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import socket
from threading import Barrier

import pytest

from ml.intraday import local_forecast
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.store import ShadowStore
from test_intraday_local_forecast import make_case, read_saved, save_candidate
from test_intraday_scout_local_execution import env


@pytest.fixture(autouse=True)
def isolated_cache_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Snapshot validation tests must remain offline")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with local_forecast._SNAPSHOT_VALIDATIONS_LOCK:
        local_forecast._SNAPSHOT_VALIDATIONS.clear()
    yield
    with local_forecast._SNAPSHOT_VALIDATIONS_LOCK:
        local_forecast._SNAPSHOT_VALIDATIONS.clear()


def counted_validator(monkeypatch):
    original = ShadowStore._snapshot_value
    calls = []
    def validate(key, raw):
        calls.append((key, sha256(raw.encode() if isinstance(raw, str) else raw).digest()))
        return original(key, raw)
    monkeypatch.setattr(ShadowStore, "_snapshot_value", staticmethod(validate))
    return calls


def test_six_saved_reads_validate_snapshot_once_and_still_read_current_rows(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    calls = counted_validator(monkeypatch)
    queries = []
    original_cycle = local_forecast._cycle
    def trace_cycle(db, key):
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        db.set_trace_callback(queries.append)
        return original_cycle(db, key)
    monkeypatch.setattr(local_forecast, "_cycle", trace_cycle)
    before = case.store.path.read_bytes()
    for _ in range(6):
        assert read_saved(case) is None
    assert len(calls) == 1
    assert sum("FROM snapshots s JOIN symbol_inputs" in query for query in queries) == 6
    assert sum("FROM cycles WHERE id=" in query for query in queries) == 6
    assert sum("julianday(reference_at)>=" in query for query in queries) == 6
    assert case.store.path.read_bytes() == before


@pytest.mark.parametrize("raw", ['{}', '{', '[1]', 'null'])
def test_warm_snapshot_tampering_is_revalidated_and_rejected(tmp_path, monkeypatch, raw):
    case = make_case(tmp_path)
    assert read_saved(case) is None
    calls = counted_validator(monkeypatch)
    with case.store.connect() as db:
        db.execute("UPDATE snapshots SET payload=?", (raw,))
    before = case.store.path.read_bytes()
    with pytest.raises(ContractError):
        read_saved(case)
    assert len(calls) == 1
    assert case.store.path.read_bytes() == before


@pytest.mark.parametrize("raw,key", [
    ('{malformed}', sha256(b'{malformed}').hexdigest()),
    ('[]', digest([])),
    ('null', digest(None)),
    ('{"x":NaN}', sha256(b'{"x":NaN}').hexdigest()),
])
def test_first_visit_requires_full_semantic_validation_and_failures_are_not_cached(monkeypatch, raw, key):
    calls = counted_validator(monkeypatch)
    for _ in range(2):
        with pytest.raises((ContractError, ValueError)):
            local_forecast._validate_saved_snapshot(key, raw)
    assert len(calls) == 2
    assert not local_forecast._SNAPSHOT_VALIDATIONS


def test_equivalent_noncanonical_json_is_fully_validated_before_becoming_warm(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    assert read_saved(case) is None
    calls = counted_validator(monkeypatch)
    with case.store.connect() as db:
        raw = db.execute("SELECT payload FROM snapshots").fetchone()[0]
        db.execute("UPDATE snapshots SET payload=?", (json.dumps(json.loads(raw), indent=2),))
    assert read_saved(case) is None
    assert read_saved(case) is None
    assert len(calls) == 1


def test_binary_payloads_always_use_original_full_json_validation(monkeypatch):
    calls = counted_validator(monkeypatch)
    value = {"input": "immutable"}
    key, raw = digest(value), encoded(value).encode("utf-16")
    local_forecast._validate_saved_snapshot(key, raw)
    local_forecast._validate_saved_snapshot(key, raw)
    assert len(calls) == 2
    assert not local_forecast._SNAPSHOT_VALIDATIONS


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16-le"])
def test_valid_binary_json_cannot_warm_invalid_text_with_the_same_byte_fingerprint(monkeypatch, encoding):
    calls = counted_validator(monkeypatch)
    value = {"input": "immutable"}
    key, raw = digest(value), encoded(value).encode(encoding)
    text = raw.decode("utf-8")
    assert raw == text.encode("utf-8")
    local_forecast._validate_saved_snapshot(key, raw)
    for _ in range(2):
        with pytest.raises(ValueError):
            local_forecast._validate_saved_snapshot(key, text)
    assert len(calls) == 3
    assert not local_forecast._SNAPSHOT_VALIDATIONS


@pytest.mark.parametrize("statement", [
    "UPDATE forecasts SET symbol='DBX'",
    "UPDATE cycles SET pc='Atlas'",
    "UPDATE cycles SET payload=json_set(payload,'$.experiment','different-session')",
    "DELETE FROM symbol_inputs",
])
def test_warm_snapshot_does_not_cache_indexed_evidence_or_session(tmp_path, statement):
    case = make_case(tmp_path)
    assert read_saved(case) is None
    with case.store.connect() as db:
        db.execute(statement)
    with pytest.raises(ContractError):
        read_saved(case)


def test_warm_snapshot_does_not_cache_original_mapping(tmp_path):
    case = make_case(tmp_path)
    assert read_saved(case) is None
    value = {"different": "genuine snapshot"}
    with case.store.connect() as db:
        db.execute("INSERT INTO snapshots VALUES(?,?)", (digest(value), encoded(value)))
        db.execute("UPDATE symbol_inputs SET snapshot_id=?", (digest(value),))
    with pytest.raises(ContractError, match="frozen forecast snapshot"):
        read_saved(case)


def test_warm_snapshot_does_not_cache_current_manual_session_or_newer_forecast(tmp_path):
    case = make_case(tmp_path)
    assert read_saved(case) is None
    with pytest.raises(ContractError, match="manual Scout session"):
        read_saved(case, session_id="different-manual-session")
    save_candidate(case)
    with pytest.raises(ContractError, match="supersedes"):
        read_saved(case)


def test_warm_snapshot_still_rejects_a_newly_unsafe_source_path(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    assert read_saved(case) is None
    def unsafe(*args, **kwargs):
        raise ContractError("Forecast evidence cannot be redirected")
    monkeypatch.setattr(local_forecast, "_safe_path", unsafe)
    with pytest.raises(ContractError, match="cannot be redirected"):
        read_saved(case)


def test_cache_bounds_fingerprints_and_evicts_least_recently_used_bytes(monkeypatch):
    calls = counted_validator(monkeypatch)
    rows = [(digest({"row": index}), encoded({"row": index}))
            for index in range(local_forecast._SNAPSHOT_CACHE_LIMIT + 1)]
    for row in rows[:-1]:
        local_forecast._validate_saved_snapshot(*row)
    local_forecast._validate_saved_snapshot(*rows[0])
    local_forecast._validate_saved_snapshot(*rows[-1])
    local_forecast._validate_saved_snapshot(*rows[0])
    assert len(calls) == local_forecast._SNAPSHOT_CACHE_LIMIT + 1
    local_forecast._validate_saved_snapshot(*rows[1])
    assert len(calls) == local_forecast._SNAPSHOT_CACHE_LIMIT + 2
    assert len(local_forecast._SNAPSHOT_VALIDATIONS) == local_forecast._SNAPSHOT_CACHE_LIMIT
    for (key, fingerprint), value in local_forecast._SNAPSHOT_VALIDATIONS.items():
        assert isinstance(key, str) and len(key) == 64
        assert isinstance(fingerprint, bytes) and len(fingerprint) == 32
        # This wrapper is a custom validator: its successful witness must
        # never authorize the native-only fresh-input fast path.
        assert value is False


def test_concurrent_misses_validate_outside_lock_and_publish_only_valid_witnesses(monkeypatch):
    barrier = Barrier(2)
    original = ShadowStore._snapshot_value
    calls = []
    def validate(key, raw):
        calls.append(key)
        barrier.wait(timeout=5)
        return original(key, raw)
    monkeypatch.setattr(ShadowStore, "_snapshot_value", staticmethod(validate))
    value = {"input": "same concurrent snapshot"}
    row = digest(value), encoded(value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(local_forecast._validate_saved_snapshot, *row) for _ in range(2)]
        for future in futures:
            assert future.result(timeout=10) is None
    assert calls == [row[0], row[0]]
    assert len(local_forecast._SNAPSHOT_VALIDATIONS) == 1
    local_forecast._validate_saved_snapshot(*row)
    assert len(calls) == 2


@pytest.mark.parametrize("boundary", ["manual", "stop", "target"])
def test_warm_snapshot_does_not_cache_manual_controls_or_fixed_target(env, boundary):
    assert local_forecast.saved_forecast(env.store.path, env.forecast,
        session_id=env.executor.session_id, session_started=env.executor.session_started_at) is None
    if boundary == "target":
        env.state.now = instant(env.forecast["target_close"])
        assert env.executor.step(env.forecast)["disposition"] == "fixed_target_already_elapsed"
    else:
        if boundary == "manual":
            env.state.manual = False
        else:
            env.state.stop = True
        with pytest.raises(ContractError, match="manual Scout session authority"):
            env.executor.step(env.forecast)
    assert env.broker.posts == []
