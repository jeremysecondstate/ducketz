"""Independent preparation reuses only already-verified causal row projections."""
from copy import deepcopy
import sqlite3
import time

import numpy as np
import pandas as pd
import pytest

from ml.intraday import market_context_cache as module
from ml.intraday.market_context import (augment_dataset, build_market_context,
    market_context_contract)
from ml.intraday.market_context_cache import ContextCache
from ml.intraday.prediction import ContractError
from tests.test_intraday_market_context import CUTOFF, example_dataset, quote, sealed
from tests.test_intraday_market_context_cache import LazyFixture


@pytest.fixture(autouse=True)
def isolated_working_set(monkeypatch):
    cache = module._VerifiedRows()
    monkeypatch.setattr(module, "_VERIFIED_ROWS", cache)
    return cache


def row(cache, snapshots=(), **kwargs):
    return cache.build_row(snapshots, symbol="DOCU", cutoff=CUTOFF, **kwargs)


def test_warm_training_row_avoids_database_json_and_canonical_reconstruction(tmp_path, monkeypatch):
    path = tmp_path / "context.sqlite3"
    snapshot = sealed()
    expected = build_market_context([snapshot], symbol="DOCU", cutoff=CUTOFF)
    with ContextCache(path) as cache:
        first = row(cache, [snapshot])
        assert cache.row_reuse_hits == 0
    # No durable row is necessary once an independently verified immutable
    # working-set entry exists. The public inference path still audits disk.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE contexts SET payload=?", (b"corrupt",))
    with ContextCache(path) as cache:
        warmed = row(cache, [snapshot])
        assert cache.row_reuse_hits == cache.hits == 1
        assert not cache.connection.in_transaction
        inference = cache.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
        assert cache.invalid_entries == 1 and inference == expected
    assert np.array_equal(first.values, warmed.values)
    assert np.array_equal(warmed.values, expected.values)
    assert warmed.snapshot_sha256 == expected.snapshot_sha256 and warmed.has_source
    with pytest.raises(ValueError, match="read-only"):
        warmed.values[0] = 100
    with pytest.raises(ValueError):
        warmed.values.flags.writeable = True


def test_source_revisions_cutoffs_contract_order_and_other_stores_do_not_alias(tmp_path):
    path = tmp_path / "context.sqlite3"
    snapshot = sealed()
    with ContextCache(path) as cache:
        baseline = row(cache, [snapshot])
    revised = sealed(rows=[quote(bid=97)], snapshot_id="revised")
    contract = market_context_contract({"stock_quote": ["fixture:stock_quote"]})
    with ContextCache(path) as cache:
        assert np.array_equal(row(cache, [snapshot]).values, baseline.values)
        changed = row(cache, [revised])
        assert changed.snapshot_sha256 != baseline.snapshot_sha256
        later = cache.build_row([snapshot], symbol="DOCU", cutoff="2026-10-16T14:15:11Z")
        assert later.snapshot_sha256 != baseline.snapshot_sha256
        scoped = row(cache, [snapshot], contract=contract,
                     source_ids=contract["source_ids"])
        assert scoped.snapshot_sha256 != baseline.snapshot_sha256
        assert cache.row_reuse_hits == 1
        contract["feature_names"].reverse()
        with pytest.raises(ContractError, match="contract changed"):
            row(cache, [snapshot], contract=contract, source_ids=contract["source_ids"])
    with ContextCache(tmp_path / "other.sqlite3") as cache:
        row(cache, [snapshot])
        assert cache.row_reuse_hits == 0


def test_no_reuse_can_hide_invalid_or_subsequently_changed_plain_receipt(tmp_path, isolated_working_set):
    path = tmp_path / "context.sqlite3"
    snapshot = sealed()
    with ContextCache(path) as cache:
        row(cache, [snapshot])
    changed = deepcopy(snapshot)
    changed["records"][0]["bid"] = 1
    with ContextCache(path) as cache:
        with pytest.raises(ContractError, match="hash mismatch"):
            row(cache, [changed])
    count = len(isolated_working_set.entries)
    with pytest.raises(ContractError, match="receipt changed"):
        with ContextCache(path) as cache:
            row(cache, [snapshot])
            row(cache, [sealed(snapshot_id="new")])
            snapshot["records"][0]["bid"] = 1
    assert len(isolated_working_set.entries) == count


def test_lazy_witness_is_checked_before_reuse_and_dataset_release(tmp_path, isolated_working_set):
    path = tmp_path / "context.sqlite3"
    lazy = LazyFixture(sealed())
    with ContextCache(path) as cache:
        row(cache, [lazy])
    assert lazy.loads == 1
    before = lazy.stat_checks
    with pytest.raises(ContractError, match="receipt changed"):
        with ContextCache(path) as cache:
            row(cache, [lazy])
            assert cache.row_reuse_hits == 1 and lazy.loads == 1
            lazy.changed = True
    assert lazy.stat_checks == before + 2


@pytest.mark.parametrize("bound", ["entries", "bytes"])
def test_bounded_reuse_and_pending_work_never_change_results(tmp_path, monkeypatch, isolated_working_set, bound):
    monkeypatch.setattr(module, "MAX_REUSE_ENTRIES" if bound == "entries" else "MAX_REUSE_BYTES", 1)
    with ContextCache(tmp_path / "context.sqlite3") as cache:
        for receipt in [sealed(), sealed(snapshot_id="second", rows=[quote(bid=97)])]:
            actual = row(cache, [receipt])
            expected = build_market_context([receipt], symbol="DOCU", cutoff=CUTOFF)
            assert np.array_equal(actual.values, expected.values)
            assert actual.snapshot_sha256 == expected.snapshot_sha256
        assert len(cache._verified_pending) <= module.MAX_REUSE_ENTRIES
        assert cache._verified_pending_bytes <= module.MAX_REUSE_BYTES
    assert len(isolated_working_set.entries) <= module.MAX_REUSE_ENTRIES
    assert isolated_working_set.bytes <= module.MAX_REUSE_BYTES


def test_failed_preparation_does_not_publish_a_working_set_entry(tmp_path, isolated_working_set):
    with pytest.raises(RuntimeError, match="failure"):
        with ContextCache(tmp_path / "context.sqlite3") as cache:
            row(cache, [sealed()])
            raise RuntimeError("fixture preparation failure")
    assert not isolated_working_set.entries


def test_reused_dataset_keeps_labels_probabilities_inputs_and_manifest_exact(tmp_path):
    dataset = example_dataset()
    options = {"training_decision_at": {ref: ref + pd.Timedelta(minutes=15)
        for ref in dataset.reference_at}, "inference_cutoff": CUTOFF}
    receipts = [sealed()]
    expected = augment_dataset(dataset, receipts, **options)
    for _ in range(3):
        actual = augment_dataset(dataset, receipts,
            cache_path=tmp_path / "context.sqlite3", **options)
        assert np.array_equal(actual.X, expected.X)
        assert np.array_equal(actual.inference_X, expected.inference_X)
        assert np.array_equal(actual.y, expected.y)
        assert np.array_equal(actual.reference_at, expected.reference_at)
        assert np.array_equal(actual.label_available_at, expected.label_available_at)
        assert np.array_equal(actual.target_close_at, expected.target_close_at)
        assert actual.feature_names == expected.feature_names
        assert actual.manifest == expected.manifest
        assert actual.inference == expected.inference
        # Prerecorded deterministic probability consumer sees exactly the same
        # vectors and labels; no fitting or changed interpretation is involved.
        assert float(actual.inference_X.sum()) == float(expected.inference_X.sum())


def test_future_invalid_bytes_cannot_change_reused_historical_vector(tmp_path):
    snapshot = sealed()
    future = sealed(snapshot_id="future", receipt_at="2026-10-16T14:16:00Z",
                    available_at="2026-10-16T14:16:00Z")
    with ContextCache(tmp_path / "context.sqlite3") as cache:
        expected = row(cache, [snapshot])
    future["records"][0]["bid"] = "invalid and future"
    with ContextCache(tmp_path / "context.sqlite3") as cache:
        actual = row(cache, [snapshot, future])
        assert actual.snapshot_sha256 == expected.snapshot_sha256
        assert np.array_equal(actual.values, expected.values)
        assert cache.row_reuse_hits == 1


def test_changed_implementation_invalidates_working_set(tmp_path, monkeypatch):
    path = tmp_path / "context.sqlite3"
    with ContextCache(path) as cache:
        expected = row(cache, [sealed()])
    monkeypatch.setattr(module, "_implementation_digest", lambda: "changed-code")
    with ContextCache(path) as cache:
        actual = row(cache, [sealed()])
        assert actual.snapshot_sha256 == expected.snapshot_sha256
        assert cache.row_reuse_hits == 0 and cache.misses == 1


def benchmark_rows(directory, *, rows=512, source_count=162):
    """Offline reproducible sparse-history preparation subphase benchmark.

    This measures durable cached JSON versus process-local row reuse only. It
    does not estimate complete preparation, model time or live order latency.
    Every fixture cell/hash is compared; no provider, broker or model is used.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "benchmark-context.sqlite3"
    selectors = {"stock_quote": [f"fixture:{i}" for i in range(source_count)]}
    contract = market_context_contract(selectors)
    cutoffs = pd.date_range("2026-09-01T14:15Z", periods=rows, freq="15min")
    options = {"symbol": "DOCU", "contract": contract, "source_ids": selectors}
    for strategy in ("build", "build_row"):
        with ContextCache(path) as cache:
            for cutoff in cutoffs:
                getattr(cache, strategy)([], cutoff=cutoff, **options)
    measurements = {}
    matrices = {}
    hashes = {}
    for strategy in ("build", "build_row"):
        begun = time.perf_counter()
        matrix = np.empty((rows, len(contract["feature_names"])), dtype=float)
        digests = []
        with ContextCache(path) as cache:
            for index, cutoff in enumerate(cutoffs):
                value = getattr(cache, strategy)([], cutoff=cutoff, **options)
                matrix[index] = value.values
                digests.append(value.snapshot_sha256)
            assert not cache.connection.in_transaction
        measurements[strategy] = {"seconds": time.perf_counter() - begun,
            "hits": cache.hits, "misses": cache.misses,
            "row_reuse_hits": cache.row_reuse_hits}
        matrices[strategy] = matrix
        hashes[strategy] = digests
    assert np.array_equal(matrices["build"], matrices["build_row"])
    assert hashes["build"] == hashes["build_row"]
    return {"fixture": "sparse_missing_causal_context", "rows": rows,
        "features": len(contract["feature_names"]), "measurements": measurements,
        "speedup": measurements["build"]["seconds"] / measurements["build_row"]["seconds"],
        "exact_values_and_context_hashes_equal": True,
        "scope": "Context lookup and matrix assembly only; no live extrapolation"}


def test_sparse_projection_benchmark_preserves_every_cell_and_context_hash(tmp_path):
    result = benchmark_rows(tmp_path, rows=32)
    assert result["exact_values_and_context_hashes_equal"]
    assert result["measurements"]["build_row"]["row_reuse_hits"] == 32
