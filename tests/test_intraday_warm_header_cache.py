"""Warm original proofs avoid optional disk I/O while retaining source guards."""
from contextlib import contextmanager
import json
import socket
import sqlite3

import pytest

from ml.intraday.forecast_header_cache import DurableForecastHeaders
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError
from ml.intraday.store import ShadowStore, _EXECUTION_FORECAST_SCHEMA
from tests.test_intraday_execution_forecast_projection import put, spy
from tests.test_intraday_recovery_grouping import group, observation
from tests.test_intraday_recovery_sizing_projection import prepared


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Warm-header fixtures must not contact brokers or providers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def inventory(store):
    return store.execution_forecast_headers(pc="Atlas", symbol="AAPL")


def warm(tmp_path):
    store = ShadowStore(tmp_path / "source.sqlite3")
    originals = [put(store, "first"), put(store, "second", offset=15)]
    assert len(inventory(store)) == 2
    return store, originals


def durable_spy(monkeypatch):
    calls = []
    original = DurableForecastHeaders.load_many
    def tracked(self, identity, signatures):
        signatures = tuple(signatures)
        calls.append(signatures)
        return original(self, identity, signatures)
    monkeypatch.setattr(DurableForecastHeaders, "load_many", tracked)
    return calls


def test_warm_exact_headers_do_not_read_optional_disk_cache(tmp_path, monkeypatch):
    store, originals = warm(tmp_path)
    calls = durable_spy(monkeypatch)
    decoded = spy(store, monkeypatch)
    first = inventory(store)
    assert inventory(store) == first
    assert calls == [], "Exact in-process proofs redundantly read the optional durable cache"
    assert decoded == []
    assert [f["forecast_id"] for f in first] == [f["forecast_id"] for f in originals]


def test_warm_recovery_both_passes_do_not_read_optional_disk_cache(tmp_path, monkeypatch):
    store, original, intents, order = prepared(tmp_path)
    first = group(intents, original, observation(order))
    calls = durable_spy(monkeypatch)
    assert group(intents, original, observation(order)) == first
    assert calls == [], "Both protected recovery passes already own exact validated scalar proofs"


def test_mixed_inventory_reads_only_missing_exact_signatures(tmp_path, monkeypatch):
    store, originals = warm(tmp_path)
    missing = put(store, "third", offset=30)
    calls = durable_spy(monkeypatch)
    decoded = spy(store, monkeypatch)
    assert len(inventory(store)) == 3
    assert len(calls) == 1
    assert [s[0] for s in calls[0]] == [missing["forecast_id"]]
    assert decoded == [missing["forecast_id"]]
    calls.clear()
    assert len(inventory(store)) == 3 and calls == []


def test_fresh_process_equivalent_instance_reuses_all_durable_headers(tmp_path, monkeypatch):
    store, originals = warm(tmp_path)
    cold = ShadowStore(store.path)
    calls = durable_spy(monkeypatch)
    decoded = spy(cold, monkeypatch)
    assert len(inventory(cold)) == 2
    assert len(calls) == 1 and len(calls[0]) == 2
    assert decoded == []


def test_inventory_larger_than_bound_keeps_scan_owned_warm_proofs(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "bounded.sqlite3")
    # One fixture transaction avoids thousands of unrelated durability commits.
    connect = store.connect
    with connect() as db:
        @contextmanager
        def same_fixture_connection():
            yield db
        with monkeypatch.context() as fixture:
            fixture.setattr(store, "connect", same_fixture_connection)
            for index in range(4100):
                put(store, "bounded-" + str(index), offset=15 * index)
    # Disable optional I/O only while first validating the synthetic originals.
    # The real method is restored for the mixed warm/cold repeat below.
    original_load, original_save = DurableForecastHeaders.load_many, DurableForecastHeaders.save
    monkeypatch.setattr(DurableForecastHeaders, "load_many", lambda *args, **kwargs: {})
    monkeypatch.setattr(DurableForecastHeaders, "save", lambda *args, **kwargs: None)
    expected = inventory(store)
    assert len(store._execution_forecast_cache) == 4096
    monkeypatch.setattr(DurableForecastHeaders, "load_many", original_load)
    monkeypatch.setattr(DurableForecastHeaders, "save", original_save)
    calls = durable_spy(monkeypatch)
    decoded = spy(store, monkeypatch)
    assert inventory(store) == expected
    assert len(calls) == 1 and len(calls[0]) == 4
    assert decoded == [row["forecast_id"] for row in expected[:4]]
    assert len(store._execution_forecast_cache) == 4096


def test_uncertain_opened_file_identity_discards_warm_source_proof(tmp_path, monkeypatch):
    store, originals = warm(tmp_path)
    payload_reads = []
    original_projection = store._forecast_projection
    def checked(row):
        payload_reads.append(row[0])
        return original_projection(row)
    monkeypatch.setattr(store, "_forecast_projection", checked)
    calls = durable_spy(monkeypatch)
    with store._execution_forecast_lock, store.connect() as db:
        db.execute("BEGIN")
        store._connection_source_identities[id(db)] = None
        headers = store._revision_forecast_headers(db, ["f.pc=?", "f.symbol=?"], ["Atlas", "AAPL"])
    assert len(headers) == 2 and len(payload_reads) == 2
    assert calls == []  # Neither an uncertain durable lookup nor stale warm proof.


def test_same_bytes_updated_revision_still_revalidates_original(tmp_path, monkeypatch):
    store, originals = warm(tmp_path)
    changed = originals[1]["forecast_id"]
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=payload WHERE id=?", (changed,))
    calls = durable_spy(monkeypatch)
    payload_reads = []
    original_projection = store._forecast_projection
    def checked(row):
        payload_reads.append(row[0])
        return original_projection(row)
    monkeypatch.setattr(store, "_forecast_projection", checked)
    assert len(inventory(store)) == 2
    assert len(calls) == 1 and [s[0] for s in calls[0]] == [changed]
    # Identical raw bytes may reuse their existing full semantic proof after
    # this mandatory original payload reread/hash under the changed revision.
    assert payload_reads == [changed]


@pytest.mark.parametrize("damage", ["payload", "actor", "symbol", "cycle", "reference", "issue"])
def test_warm_source_tampering_cannot_reuse_a_validated_header(tmp_path, damage):
    store, originals = warm(tmp_path)
    key = originals[0]["forecast_id"]
    with store.connect() as db:
        if damage == "payload":
            raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (key,)).fetchone()[0]
            changed = raw.replace('"probability_not_down":0.72', '"probability_not_down":0.73')
            assert changed != raw and len(changed) == len(raw)
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (changed, key))
        else:
            column = {"actor":"pc", "symbol":"symbol", "cycle":"cycle_id",
                      "reference":"reference_at", "issue":"issued_at"}[damage]
            db.execute("UPDATE forecasts SET " + column + "='changed' WHERE id=?", (key,))
    # Query the exact original, so changed selection fields cannot hide damage.
    with pytest.raises(ContractError):
        with store._execution_forecast_lock, store.connect() as db:
            db.execute("BEGIN")
            store._revision_forecast_headers(db, ["f.id=?"], [key])


@pytest.mark.parametrize("damage", ["trigger", "revision", "generation"])
def test_warm_proof_never_skips_source_schema_or_revision_validation(tmp_path, damage):
    store, originals = warm(tmp_path)
    with store.connect() as db:
        if damage == "trigger":
            db.execute("DROP TRIGGER execution_forecast_update")
        elif damage == "revision":
            db.execute("DELETE FROM execution_forecast_revisions WHERE forecast_id=?", (originals[0]["forecast_id"],))
        else:
            # Deliberately restore the exact protected schema/cookie after an
            # out-of-band regression; the validated highwater must still hold.
            cookie = db.execute("PRAGMA schema_version").fetchone()[0]
            db.execute("DROP TRIGGER execution_generation_update")
            db.execute("UPDATE execution_forecast_generation SET generation=0")
            db.execute(_EXECUTION_FORECAST_SCHEMA["execution_generation_update"])
            db.execute("PRAGMA schema_version=" + str(cookie))
    with pytest.raises(ContractError):
        inventory(store)


def test_optional_cache_damage_does_not_invalidate_exact_source_proof(tmp_path, monkeypatch):
    store, originals = warm(tmp_path)
    expected = inventory(store)
    DurableForecastHeaders(store.path).path.write_bytes(b"unusable optional cache")
    calls = durable_spy(monkeypatch)
    assert inventory(store) == expected and calls == []
    # A new row still misses and retains the ordinary full original validator.
    added = put(store, "new-after-cache-damage", offset=30)
    decoded = spy(store, monkeypatch)
    assert len(inventory(store)) == 3 and decoded == [added["forecast_id"]]


def test_returned_header_mutation_does_not_poison_warm_proof(tmp_path, monkeypatch):
    store, originals = warm(tmp_path)
    first = inventory(store)
    first[0]["mode"] = "shadow"
    calls = durable_spy(monkeypatch)
    assert all(row["mode"] == "live" for row in inventory(store))
    assert calls == []


@pytest.mark.parametrize("damage", ["payload", "actor", "trigger", "missing"])
def test_warm_recovery_rechecks_committed_change_between_read_and_writer(tmp_path, monkeypatch, damage):
    store, original, intents, order = prepared(tmp_path)
    group(intents, original, observation(order))
    connect = store.connect
    calls = []
    @contextmanager
    def interleaved():
        calls.append(True)
        with connect() as db:
            yield db
        if len(calls) == 1:
            with sqlite3.connect(store.path, timeout=0) as other:
                if damage == "payload":
                    other.execute("UPDATE forecasts SET payload=json_set(payload,'$.probability_not_down',0.73)")
                elif damage == "actor":
                    other.execute("UPDATE forecasts SET pc='Scout'")
                elif damage == "trigger":
                    other.execute("DROP TRIGGER execution_forecast_update")
                else:
                    other.execute("DELETE FROM forecasts")
    monkeypatch.setattr(store, "connect", interleaved)
    with pytest.raises(ContractError):
        group(intents, original, observation(order))
    assert len(calls) == 2
