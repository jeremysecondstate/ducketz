"""Exact cached causal vectors are interchangeable with fresh derivation."""
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
import sqlite3
import hashlib
import json
import zlib
import numpy as np
import pandas as pd

import pytest

from ml.intraday.market_context import augment_dataset, build_market_context
from ml.intraday.market_context_cache import ContextCache
from ml.intraday.prediction import ContractError
from tests.test_intraday_market_context import CUTOFF, example_dataset, quote, sealed


def test_exact_cache_roundtrip_and_later_receipt_contract_cutoff_separation(tmp_path):
    path = tmp_path / "cache.sqlite3"
    snapshot = sealed()
    expected = build_market_context([snapshot], symbol="DOCU", cutoff=CUTOFF)
    with ContextCache(path) as cache:
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (cache.hits, cache.misses) == (1, 1)
    with ContextCache(path) as cache:
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        revised = sealed(rows=[quote(bid=97)], snapshot_id="revised")
        assert cache.build([revised], symbol="DOCU", cutoff=CUTOFF) != expected
        cache.build([snapshot], symbol="DOCU", cutoff="2026-10-16T14:15:11Z")
        cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF, source_ids={"stock_quote": ["fixture:stock_quote"]})
        assert (cache.hits, cache.misses) == (1, 3)


def test_cache_hit_never_accepts_changed_plain_receipt_bytes(tmp_path):
    snapshot = sealed()
    with ContextCache(tmp_path / "cache.sqlite3") as cache:
        cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
        changed = deepcopy(snapshot)
        changed["records"][0]["bid"] = 1
        with pytest.raises(ContractError, match="hash mismatch"):
            cache.build([changed], symbol="DOCU", cutoff=CUTOFF)


def test_future_receipts_cannot_change_cached_historical_result(tmp_path):
    snapshot = sealed()
    future = sealed(receipt_at="2026-10-16T14:16:00Z", available_at="2026-10-16T14:16:00Z")
    future["records"][0]["bid"] = 1
    with ContextCache(tmp_path / "cache.sqlite3") as cache:
        expected = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
        assert cache.build([snapshot, future], symbol="DOCU", cutoff=CUTOFF) == expected
        assert cache.hits == 1


def test_corrupted_derived_cache_is_recomputed_from_verified_receipt(tmp_path):
    path = tmp_path / "cache.sqlite3"
    snapshot = sealed()
    with ContextCache(path) as cache:
        expected = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE contexts SET payload=?", (b"damaged",))
    with ContextCache(path) as cache:
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (cache.invalid_entries, cache.misses) == (1, 1)


@pytest.mark.parametrize("envelope", [[], None])
def test_checksummed_non_object_cache_envelope_recomputes(envelope, tmp_path):
    path = tmp_path / "cache.sqlite3"
    snapshot = sealed()
    with ContextCache(path) as cache:
        expected = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
    payload = zlib.compress(json.dumps(envelope).encode("utf-8"))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE contexts SET payload=?,payload_sha256=?",
                           (payload, hashlib.sha256(payload).hexdigest()))
    with ContextCache(path) as cache:
        assert cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (cache.invalid_entries, cache.misses) == (1, 1)


def test_valid_derived_payload_transplant_cannot_cross_receipt_identity(tmp_path):
    path = tmp_path / "cache.sqlite3"
    first, second = sealed(), sealed(rows=[quote(bid=97)], snapshot_id="second")
    with ContextCache(path) as cache:
        one = cache.build([first], symbol="DOCU", cutoff=CUTOFF)
        two = cache.build([second], symbol="DOCU", cutoff=CUTOFF)
    assert one != two
    with sqlite3.connect(path) as connection:
        entries = connection.execute("SELECT cache_key,payload,payload_sha256 FROM contexts ORDER BY rowid").fetchall()
        connection.execute("UPDATE contexts SET payload=?,payload_sha256=? WHERE cache_key=?",
                           (entries[1][1], entries[1][2], entries[0][0]))
    with ContextCache(path) as cache:
        assert cache.build([first], symbol="DOCU", cutoff=CUTOFF) == one
        assert (cache.invalid_entries, cache.misses) == (1, 1)


class LazyFixture(Mapping):
    def __init__(self, value):
        self.value = value
        self.header = {k: v for k, v in value.items() if k != "records"}
        self.loads = self.stat_checks = 0
        self.changed = False

    def __getitem__(self, key): return self.header[key]
    def __iter__(self): return iter(self.header)
    def __len__(self): return len(self.header)

    def verify_unchanged(self):
        self.stat_checks += 1
        if self.changed:
            raise ContractError("Immutable receipt changed")

    def load_verified(self):
        self.verify_unchanged()
        self.loads += 1
        return self.value


def test_lazy_cache_hit_checks_identity_without_decoding_raw_history(tmp_path):
    lazy = LazyFixture(sealed())
    with ContextCache(tmp_path / "cache.sqlite3") as cache:
        first = cache.build([lazy], symbol="DOCU", cutoff=CUTOFF)
        assert lazy.loads == 1
        assert cache.build([lazy], symbol="DOCU", cutoff=CUTOFF) == first
        assert lazy.loads == 1 and lazy.stat_checks == 2
    assert lazy.stat_checks == 3  # final dataset-boundary check
    with pytest.raises(ContractError, match="receipt changed"):
        with ContextCache(tmp_path / "cache.sqlite3") as cache:
            cache.build([lazy], symbol="DOCU", cutoff=CUTOFF)
            lazy.changed = True
            cache.build([lazy], symbol="DOCU", cutoff=CUTOFF)


def test_lazy_miss_rejects_payload_index_identity_mismatch(tmp_path):
    lazy = LazyFixture(sealed())
    lazy.header["snapshot_id"] = "other-receipt"
    with ContextCache(tmp_path / "cache.sqlite3") as cache:
        with pytest.raises(ContractError, match="indexed identity"):
            cache.build([lazy], symbol="DOCU", cutoff=CUTOFF)


def test_cached_dataset_preserves_exact_vectors_labels_manifests_and_causality(tmp_path):
    original = example_dataset()
    kwargs = {"training_decision_at": {ref: ref+pd.Timedelta(minutes=15) for ref in original.reference_at},
              "inference_cutoff": CUTOFF}
    expected = augment_dataset(original, [sealed()], **kwargs)
    lazy = LazyFixture(sealed())
    for _ in range(2):
        actual = augment_dataset(original, [lazy], cache_path=tmp_path / "DOCU.sqlite3", **kwargs)
        assert np.array_equal(actual.X, expected.X)
        assert np.array_equal(actual.inference_X, expected.inference_X)
        assert np.array_equal(actual.y, expected.y)
        assert actual.manifest == expected.manifest
        assert actual.inference == expected.inference
    assert lazy.loads == 1


def test_real_index_to_cache_reads_payload_once_and_detects_later_file_change(tmp_path, monkeypatch):
    from ml.intraday.market_collection import canonical, load_snapshots
    from ml.intraday.market_collection_index import initialize_receipt_index, index_receipt
    root = tmp_path / "market-data"
    initialize_receipt_index(root)
    snapshot = sealed()
    path = root / "snapshots/2026-10-16/receipt-1/receipt-1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical(snapshot))
    index_receipt(root, path, snapshot)
    reads = []
    original = Path.read_text
    def read(target, *args, **kwargs):
        if target == path:
            reads.append(target)
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    options = dict(symbol="DOCU", cutoff=CUTOFF, since="2026-10-16T00:00Z",
                   decision_cutoffs=[CUTOFF], source_ids={"stock_quote": ["fixture:stock_quote"]}, lazy=True)
    for _ in range(2):
        refs = load_snapshots(root, **options)
        with ContextCache(tmp_path / "DOCU.sqlite3") as cache:
            actual = cache.build(refs, symbol="DOCU", cutoff=CUTOFF)
            assert actual.features["ctx_stock_quote_relative_spread"] == pytest.approx(.02)
    assert len(reads) == 1
    path.write_bytes(path.read_bytes()+b" ")
    with ContextCache(tmp_path / "DOCU.sqlite3") as cache:
        with pytest.raises(ValueError, match="file changed"):
            cache.build(refs, symbol="DOCU", cutoff=CUTOFF)


def test_contract_validated_once_per_identity_but_nested_mutation_is_rejected(tmp_path, monkeypatch):
    from ml.intraday import market_context_cache as module
    from ml.intraday.market_context import market_context_contract
    contract = market_context_contract()
    original, calls = module._resolved_contract, []
    def resolve(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(module, '_resolved_contract', resolve)
    with ContextCache(tmp_path/'cache.sqlite3') as cache:
        first = cache.build([], symbol='DOCU', cutoff=CUTOFF, contract=contract)
        assert cache.build([], symbol='DOCU', cutoff=CUTOFF, contract=contract) == first
        assert calls == [1]
        contract['feature_names'][0] = 'silently_changed_feature'
        with pytest.raises(ContractError, match='contract changed'):
            cache.build([], symbol='DOCU', cutoff=CUTOFF, contract=contract)


def test_contract_memo_cannot_hide_changed_selectors(tmp_path):
    from ml.intraday.market_context import market_context_contract
    selectors = {'stock_quote':['fixture:first']}
    contract = market_context_contract(selectors)
    with ContextCache(tmp_path/'cache.sqlite3') as cache:
        cache.build([], symbol='DOCU', cutoff=CUTOFF, source_ids=selectors, contract=contract)
        selectors['stock_quote'][0] = 'fixture:second'
        with pytest.raises(ContractError, match='selectors differ'):
            cache.build([], symbol='DOCU', cutoff=CUTOFF, source_ids=selectors, contract=contract)
