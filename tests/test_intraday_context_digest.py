"""Owned cache-hit hashing preserves bytes, public isolation and rejection guards."""
from copy import deepcopy
import hashlib
import json
import sqlite3
import zlib

import pytest

from ml.intraday import market_context as context_module
from ml.intraday import market_context_cache as cache_module
from ml.intraday.market_context import (
    FEATURE_POLICY, FEATURE_VERSION, LEGACY_FEATURE_VERSION, PREVIOUS_FEATURE_VERSION,
    STATE_POLICY, MarketContext, _hash, _json, _owned_context_digest,
)
from ml.intraday.market_context_cache import ContextCache
from tests.test_intraday_market_context import CUTOFF, sealed


def existing_snapshot(context):
    """Independent pre-change public format, including nested detachment."""
    return {"symbol": context.symbol, "cutoff": context.cutoff,
        "feature_version": context.feature_version,
        "feature_policy": STATE_POLICY if context.feature_version == FEATURE_VERSION else FEATURE_POLICY,
        "feature_names": list(context.feature_names), "features": list(context.values),
        "source_ids": {key: list(values) for key, values in context.source_ids},
        "sources": deepcopy(list(context.source_evidence)),
        "missing_encoding": "zero_placeholder_plus_per_value_missing_indicator"}


@pytest.mark.parametrize("version", [LEGACY_FEATURE_VERSION, PREVIOUS_FEATURE_VERSION, FEATURE_VERSION])
@pytest.mark.parametrize("population", ["empty", "normal", "large_nested"])
def test_owned_digest_keeps_exact_existing_snapshot_bytes(version, population):
    count = 4096 if population == "large_nested" else 3
    evidence = () if population == "empty" else tuple({
        "source_id": f"fixture:{index}", "records_used": index,
        "nested": {"unicode": "receipt café", "values": [None, True, {"observed": [1, 2.5, "x"]}]},
    } for index in range(93 if population == "large_nested" else 1))
    context = MarketContext("DOCU", CUTOFF, tuple(f"feature_{index}" for index in range(count)),
        tuple(float(index % 7) for index in range(count)), evidence, "",
        (("stock_quote", ("fixture:stock_quote",)),), version)
    expected = _json(existing_snapshot(context)).encode("utf-8")
    assert _json(context.snapshot).encode("utf-8") == expected
    assert _owned_context_digest(context) == hashlib.sha256(expected).hexdigest()


def test_public_snapshot_remains_detached_and_owned_digest_rechecks_changes():
    context = MarketContext("DOCU", CUTOFF, ("one",), (1.0,),
        ({"nested": {"receipts": ["first"]}},), "", (("stock_quote", ("fixture:quote",)),))
    before = _owned_context_digest(context)
    detached = context.snapshot
    detached["sources"][0]["nested"]["receipts"].append("caller edit")
    detached["features"][0] = 20
    detached["feature_names"][0] = "changed"
    detached["source_ids"]["stock_quote"].append("other")
    assert context.source_evidence[0]["nested"]["receipts"] == ["first"]
    assert _owned_context_digest(context) == before
    context.source_evidence[0]["nested"]["receipts"].append("owned change")
    assert _owned_context_digest(context) == _hash(context.snapshot)
    assert _owned_context_digest(context) != before


def test_only_immediate_owned_digest_avoids_the_defensive_evidence_copy(monkeypatch):
    context = MarketContext("DOCU", CUTOFF, ("one",), (1.0,), ({"nested": [1]},), "", ())
    expected = _hash(context.snapshot)
    def no_copy(*args):
        raise AssertionError("defensive copy requested")
    monkeypatch.setattr(context_module, "deepcopy", no_copy)
    assert _owned_context_digest(context) == expected
    with pytest.raises(AssertionError, match="defensive copy"):
        context.snapshot


def test_every_warm_hit_hashes_fresh_owned_evidence_and_returned_aliases_cannot_poison_it(tmp_path, monkeypatch):
    snapshot = sealed()
    original = cache_module._owned_context_digest
    validated = []
    def record(context):
        validated.append(context)
        return original(context)
    monkeypatch.setattr(cache_module, "_owned_context_digest", record)
    with ContextCache(tmp_path / "context.sqlite3") as cache:
        expected = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
        first = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
        first.source_evidence[0]["nested_private_edit"] = {"values": [1, 2, 3]}
        assert _hash(first.snapshot) != first.snapshot_sha256
        second = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
        assert second == expected
        assert second.source_evidence[0] is not first.source_evidence[0]
        assert (cache.hits, cache.misses, cache.invalid_entries) == (2, 1, 0)
    assert len(validated) == 2 and validated[0] is not validated[1]


@pytest.mark.parametrize("damage", ["nested_evidence", "values", "shape", "feature_order", "source_ids", "key", "compressed_hash"])
def test_owned_hit_hash_preserves_warm_payload_rejection_and_verified_recomputation(tmp_path, damage):
    path = tmp_path / "context.sqlite3"
    snapshot = sealed()
    with ContextCache(path) as cache:
        expected = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
    with sqlite3.connect(path) as connection:
        key, payload = connection.execute("SELECT cache_key,payload FROM contexts").fetchone()
        envelope = json.loads(zlib.decompress(payload))
        value = envelope["context"]
        if damage == "nested_evidence":
            value["source_evidence"][0]["nested_tampering"] = {"values": [1, 2, 3]}
        elif damage == "values":
            value["values"][0] += 1.0
        elif damage == "shape":
            value["values"].pop()
        elif damage == "feature_order":
            # Exercise the retained legacy named-envelope order guard. New
            # compact envelopes restore names from the exact frozen contract.
            envelope.pop("context_format", None)
            value["feature_names"] = list(reversed(expected.feature_names))
        elif damage == "source_ids":
            value["source_ids"].append(["wrong_kind", ["unowned_source"]])
        elif damage == "key":
            envelope["cache_key"] = "different_inputs"
        payload = zlib.compress(_json(envelope).encode("utf-8"))
        digest = "wrong" if damage == "compressed_hash" else hashlib.sha256(payload).hexdigest()
        connection.execute("UPDATE contexts SET payload=?,payload_sha256=? WHERE cache_key=?", (payload, digest, key))
    with ContextCache(path) as cache:
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (cache.hits, cache.misses, cache.invalid_entries) == (0, 1, 1)
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (cache.hits, cache.misses, cache.invalid_entries) == (1, 1, 1)
