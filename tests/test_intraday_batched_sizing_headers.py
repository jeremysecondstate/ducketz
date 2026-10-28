"""Cold recovery inventories reuse one optional authenticated cache snapshot."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
import json
import socket
import sqlite3

import pytest

from ml.intraday import execution_intentions as module
from ml.intraday.execution_intentions import IntentionStore, _RecoveryTimings
from ml.intraday.sizing_header_cache import DurableSizingHeaders, _SCHEMA
from ml.intraday.store import ShadowStore
from tests.test_intraday_durable_sizing_headers import cache_path, decoded, projection_rows
from tests.test_intraday_recovery_batch_preflight import fixture, run, tracked
from tests.test_intraday_durable_sizing_headers import observation


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Batched sizing-header regressions prohibit network calls")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def original_rows(store):
    with store.connect() as db:
        return (db.execute("SELECT * FROM forecasts ORDER BY rowid").fetchall(),
                db.execute("SELECT * FROM intraday_intentions ORDER BY rowid").fetchall(),
                db.execute("SELECT * FROM recovery_sizing_revisions ORDER BY rowid").fetchall())


def cache_reads(monkeypatch):
    """Count actual heavy cache work, leaving MAC and source validation intact."""
    counts = Counter()
    for name in ("load", "load_many", "_connect", "_key_material"):
        original = getattr(DurableSizingHeaders, name)
        def observed(self, *args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(DurableSizingHeaders, name, observed)
    context, protect = DurableSizingHeaders._context, DurableSizingHeaders._protect_secret
    def read_context(db):
        counts["context"] += 1
        return context(db)
    def read_secret(raw, *, decrypt=False):
        counts["decrypt" if decrypt else "protect"] += 1
        return protect(raw, decrypt=decrypt)
    monkeypatch.setattr(DurableSizingHeaders, "_context", staticmethod(read_context))
    monkeypatch.setattr(DurableSizingHeaders, "_protect_secret", staticmethod(read_secret))
    return counts


def assert_unknown_retained(result, expected):
    assert result == expected
    for rows in result.values():
        assert rows[0]["submitted_quantity"] in (None, 0)
        assert rows[0]["evidence"]["commitment_retained"] is True
        assert rows[0]["evidence"]["complete_broker_order_evidence"] is False


def test_422_fresh_headers_use_one_authenticated_cache_snapshot(tmp_path, monkeypatch):
    source, _, _, groups, expected = fixture(tmp_path, 422)
    store = ShadowStore(source.path)
    cold = IntentionStore(store)
    before, before_projection = original_rows(store), projection_rows(store)
    counts = cache_reads(monkeypatch)
    decoded_rows = decoded(cold, monkeypatch)
    calls, statements = tracked(store, monkeypatch)
    timings = _RecoveryTimings()
    result = run(cold, groups, timings=timings)
    assert_unknown_retained(result, expected)
    assert len(calls) == 1
    assert statements.count("BEGIN") == 1
    assert not any(q.startswith(("INSERT ", "UPDATE ", "DELETE ", "BEGIN IMMEDIATE")) for q in statements)
    assert counts["load_many"] == counts["_connect"] == counts["_key_material"] == counts["context"] == counts["decrypt"] == 1
    assert counts["load"] == 0
    assert decoded_rows == []
    assert timings.workload["sizing_cache_hits"] == 422
    assert timings.workload.get("sizing_payload_reads", 0) == 0
    assert original_rows(store) == before
    assert projection_rows(store) == before_projection


def test_mixed_warm_missing_and_bad_mac_headers_keep_original_fallback(tmp_path, monkeypatch):
    source, originals, _, groups, expected = fixture(tmp_path, 5)
    store = ShadowStore(source.path)
    cold = IntentionStore(store)
    keys = list(groups)
    assert cold.record_recovery_group(keys[0], actor="Atlas", observations=groups[keys[0]]) == expected[keys[0]]
    with sqlite3.connect(cache_path(store.path)) as db:
        version = db.execute("PRAGMA schema_version").fetchone()[0]
        records = {json.loads(raw)["signature"][0]: key for key, raw in db.execute("SELECT cache_key,payload FROM sizing_header_cache_rows")}
        db.execute("DROP TRIGGER sizing_header_cache_rows_delete")
        db.execute("DROP TRIGGER sizing_header_cache_rows_update")
        db.execute("DELETE FROM sizing_header_cache_rows WHERE cache_key=?", (records[keys[1]],))
        db.execute("UPDATE sizing_header_cache_rows SET record_mac=? WHERE cache_key=?", (bytes(32), records[keys[2]]))
        db.execute(_SCHEMA["sizing_header_cache_rows_delete"])
        db.execute(_SCHEMA["sizing_header_cache_rows_update"])
        db.execute("PRAGMA schema_version=" + str(version))
    before, before_projection = original_rows(store), projection_rows(store)
    seen = decoded(cold, monkeypatch)
    counts = cache_reads(monkeypatch)
    assert_unknown_retained(run(cold, groups), expected)
    assert set(seen) == {keys[1], keys[2]} and len(seen) == 2
    assert counts["load_many"] == 1 and counts["load"] == 0
    assert original_rows(store) == before and projection_rows(store) == before_projection


@pytest.mark.parametrize("availability", ["exhausted_budget", "partial"])
def test_optional_budget_or_partial_inventory_never_retries_single_cache_loads(tmp_path, monkeypatch, availability):
    source, _, _, groups, expected = fixture(tmp_path, 5)
    store = ShadowStore(source.path)
    cold = IntentionStore(store)
    before, before_projection = original_rows(store), projection_rows(store)
    if availability == "exhausted_budget":
        init = DurableSizingHeaders.__init__
        def exhausted(self, *args, **kwargs):
            init(self, *args, **kwargs)
            self._remaining = 0.
        monkeypatch.setattr(DurableSizingHeaders, "__init__", exhausted)
        expected_decodes = len(groups)
    else:
        load_many = DurableSizingHeaders.load_many
        def partial(self, identity, signatures):
            found = load_many(self, identity, signatures)
            return dict(list(found.items())[:1])
        monkeypatch.setattr(DurableSizingHeaders, "load_many", partial)
        expected_decodes = len(groups) - 1
    counts = cache_reads(monkeypatch)
    seen = decoded(cold, monkeypatch)
    assert_unknown_retained(run(cold, groups), expected)
    assert len(seen) == expected_decodes
    assert counts["load_many"] == 1 and counts["load"] == 0
    if availability == "exhausted_budget":
        assert counts["_connect"] == counts["_key_material"] == 0
    assert original_rows(store) == before and projection_rows(store) == before_projection


def test_uncertain_opened_file_disables_batch_attestation(tmp_path, monkeypatch):
    source, _, _, groups, expected = fixture(tmp_path, 5)
    store = ShadowStore(source.path)
    cold = IntentionStore(store)
    before, before_projection = original_rows(store), projection_rows(store)
    connect = store.connect
    @contextmanager
    def uncertain():
        with connect() as db:
            store._connection_source_identities[id(db)] = None
            yield db
    monkeypatch.setattr(store, "connect", uncertain)
    counts = cache_reads(monkeypatch)
    seen = decoded(cold, monkeypatch)
    assert_unknown_retained(run(cold, groups), expected)
    assert len(seen) == len(groups)
    assert counts["load"] == counts["load_many"] == counts["_connect"] == 0
    assert original_rows(store) == before and projection_rows(store) == before_projection


def test_inventory_survives_scalar_lru_eviction_without_changing_old_unknowns(tmp_path, monkeypatch):
    source, originals, prior, groups, expected = fixture(tmp_path, 7)
    key = originals[0][0]["forecast_id"]
    groups[key] = (observation(replace(originals[0][1], status="UNKNOWN")),)
    expected[key] = prior.record_recovery_group(key, actor="Atlas", observations=groups[key])
    assert expected[key][0]["submitted_quantity"] is None
    store = ShadowStore(source.path)
    cold = IntentionStore(store)
    before, before_projection = original_rows(store), projection_rows(store)
    # Exercise the same boundary as >4096 originals without manufacturing
    # thousands of forecasts merely to cross a scalar-cache capacity limit.
    monkeypatch.setattr(module, "_RECOVERY_SIZING_CACHE_LIMIT", 2)
    counts = cache_reads(monkeypatch)
    assert_unknown_retained(run(cold, groups), expected)
    assert len(cold._recovery_row_cache) <= 2
    assert_unknown_retained(run(cold, groups), expected)
    assert len(cold._recovery_row_cache) <= 2
    assert counts["load_many"] == 2 and counts["load"] == 0
    assert original_rows(store) == before and projection_rows(store) == before_projection
