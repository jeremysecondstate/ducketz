"""Compact cache storage retains exact context evidence and bounded publication."""
from dataclasses import fields
from datetime import timedelta
import hashlib
import json
import sqlite3
import zlib

import pytest

from ml.intraday import market_context_cache as module
from ml.intraday.market_context import (
    FEATURE_VERSION, LEGACY_FEATURE_VERSION, PREVIOUS_FEATURE_VERSION, SOURCE_KINDS,
    _json, _owned_context_digest, build_market_context, instant, market_context_contract,
)
from ml.intraday.market_context_cache import CONTEXT_FORMAT, ContextCache
from ml.intraday.prediction import ContractError
from tests.test_intraday_market_context import CUTOFF, sealed
from tests.test_intraday_market_context_cache import LazyFixture


def cache_envelope(path):
    with sqlite3.connect(path) as connection:
        key, payload, digest = connection.execute(
            "SELECT cache_key,payload,payload_sha256 FROM contexts").fetchone()
    assert hashlib.sha256(payload).hexdigest() == digest
    return key, json.loads(zlib.decompress(payload))


def replace_envelope(path, key, envelope):
    payload = zlib.compress(_json(envelope).encode("utf-8"))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE contexts SET payload=?,payload_sha256=? WHERE cache_key=?",
                           (payload, hashlib.sha256(payload).hexdigest(), key))


@pytest.mark.parametrize("version", [LEGACY_FEATURE_VERSION, PREVIOUS_FEATURE_VERSION, FEATURE_VERSION])
def test_compact_and_legacy_named_envelopes_restore_identical_ordered_context(tmp_path, version):
    path = tmp_path / "envelope.sqlite3"
    selectors = {"stock_quote": ["fixture:first", "fixture:second"]}
    contract = market_context_contract(selectors, version=version)
    options = dict(symbol="DOCU", cutoff=CUTOFF, source_ids=selectors, contract=contract)
    expected = build_market_context([], **options)
    with ContextCache(path) as cache:
        assert cache.build([], **options) == expected
    key, envelope = cache_envelope(path)
    assert envelope["context_format"] == CONTEXT_FORMAT
    assert "feature_names" not in envelope["context"]
    with ContextCache(path) as cache:
        actual = cache.build([], **options)
        assert actual == expected and actual.snapshot == expected.snapshot
        assert _owned_context_digest(actual) == expected.snapshot_sha256
        assert (cache.hits, cache.misses, cache.invalid_entries) == (1, 0, 0)
    envelope.pop("context_format")
    envelope["context"]["feature_names"] = list(expected.feature_names)
    replace_envelope(path, key, envelope)
    with ContextCache(path) as cache:
        assert cache.build([], **options) == expected
        assert (cache.hits, cache.misses, cache.invalid_entries) == (1, 0, 0)


@pytest.mark.parametrize("damage", ["unknown_format", "null_format", "missing_format", "injected_names",
                                   "values", "source_ids", "snapshot_hash", "feature_version", "shape"])
def test_compact_context_tampering_recomputes_from_verified_inputs(tmp_path, damage):
    path = tmp_path / "damaged.sqlite3"
    snapshot = sealed()
    with ContextCache(path) as cache:
        expected = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
    key, envelope = cache_envelope(path)
    value = envelope["context"]
    if damage == "unknown_format":
        envelope["context_format"] = "unsupported-format"
    elif damage == "null_format":
        envelope["context_format"] = None
    elif damage == "missing_format":
        envelope.pop("context_format")
    elif damage == "injected_names":
        value["feature_names"] = list(expected.feature_names)
    elif damage == "values":
        value["values"][0] += 1.0
    elif damage == "source_ids":
        value["source_ids"].append(["wrong_kind", ["wrong_source"]])
    elif damage == "snapshot_hash":
        value["snapshot_sha256"] = "0" * 64
    elif damage == "feature_version":
        value["feature_version"] = LEGACY_FEATURE_VERSION
    else:
        value["values"].pop()
    replace_envelope(path, key, envelope)
    with ContextCache(path) as cache:
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (cache.invalid_entries, cache.misses, cache.hits) == (1, 1, 0)
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (cache.invalid_entries, cache.misses, cache.hits) == (1, 1, 1)


def test_repeated_wide_contract_names_no_longer_exhaust_pending_byte_budget(tmp_path, monkeypatch):
    # A bounded analog of the observed 4,846-coordinate, 4,097-call dataset.
    # Real derivation and full hashes remain enabled; no model is fitted.
    selectors = {kind: [f"fixture:{kind}:{index:02d}" for index in range(14)]
                 for kind in SOURCE_KINDS}
    contract = market_context_contract(selectors)
    cutoffs = [instant(CUTOFF) + timedelta(seconds=index) for index in range(64)]
    expected = [build_market_context([], symbol="DOCU", cutoff=cutoff,
                source_ids=selectors, contract=contract) for cutoff in cutoffs]
    def named_payload(context):
        value = {field.name: getattr(context, field.name) for field in fields(context)}
        return zlib.compress(_json({"cache_key": "0" * 64, "context": value}).encode("utf-8"))
    old_total = sum(len(named_payload(context)) for context in expected)
    budget = 16 * len(named_payload(expected[0]))
    assert len(expected[0].feature_names) > 4000 and old_total > budget
    monkeypatch.setattr(module, "MAX_PENDING_BYTES", budget)
    path = tmp_path / "wide.sqlite3"
    with ContextCache(path) as cache:
        for cutoff, context in zip(cutoffs, expected):
            assert cache.build([], symbol="DOCU", cutoff=cutoff, source_ids=selectors, contract=contract) == context
        assert (cache.misses, cache.write_skips) == (64, 0)
        assert len(cache._pending) == 64 and cache._pending_bytes <= budget
        with sqlite3.connect(path) as observer:
            assert observer.execute("SELECT count(*) FROM contexts").fetchone()[0] == 0
    with ContextCache(path) as cache:
        for cutoff, context in zip(cutoffs, expected):
            assert cache.build([], symbol="DOCU", cutoff=cutoff, source_ids=selectors, contract=contract) == context
        assert (cache.hits, cache.misses, cache.invalid_entries) == (64, 0, 0)


def test_compact_vectors_still_respect_entry_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_PENDING_ENTRIES", 3)
    path = tmp_path / "entries.sqlite3"
    with ContextCache(path) as cache:
        for index in range(5):
            cutoff = instant(CUTOFF) + timedelta(seconds=index)
            assert cache.build([], symbol="DOCU", cutoff=cutoff) == build_market_context([], symbol="DOCU", cutoff=cutoff)
        assert len(cache._pending) == 3 and cache.write_skips == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM contexts").fetchone()[0] == 3


@pytest.mark.parametrize("failure", ["receipt_changed", "dataset_exception"])
def test_compact_vectors_are_not_published_before_final_source_validation(tmp_path, failure):
    path = tmp_path / "not-published.sqlite3"
    lazy = LazyFixture(sealed())
    error = ContractError if failure == "receipt_changed" else RuntimeError
    with pytest.raises(error):
        with ContextCache(path) as cache:
            cache.build([lazy], symbol="DOCU", cutoff=CUTOFF)
            with sqlite3.connect(path) as observer:
                assert observer.execute("SELECT count(*) FROM contexts").fetchone()[0] == 0
            if failure == "receipt_changed":
                lazy.changed = True
            else:
                raise RuntimeError("dataset abandoned before validation")
    assert cache._pending == {} and cache._pending_bytes == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM contexts").fetchone()[0] == 0
