"""Real retained receipts must authorize both durable and in-memory cache hits."""
import os

import pytest

from ml.intraday import market_collection
from ml.intraday.market_collection import canonical, load_snapshots
from ml.intraday.market_collection_index import index_receipt, initialize_receipt_index
from ml.intraday.market_context_cache import ContextCache
from tests.test_intraday_market_context import CUTOFF, sealed


def receipt_case(tmp_path):
    root = tmp_path / "receipts"
    initialize_receipt_index(root)
    value = sealed()
    path = root / "snapshots" / "2026-10-16" / "receipt-1" / "receipt-1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical(value))
    index_receipt(root, path, value)
    def refs():
        return load_snapshots(root, symbol="DOCU", cutoff=CUTOFF,
                              source_ids=["fixture:stock_quote"], lazy=True)
    return path, refs


@pytest.mark.parametrize("method", ["build", "build_row"])
@pytest.mark.parametrize("new_ref", [False, True])
def test_warm_context_rejects_same_size_receipt_corruption_with_restored_mtime(tmp_path, method, new_ref):
    path, refs = receipt_case(tmp_path)
    original = refs()
    cache_path = tmp_path / "context.sqlite3"
    with ContextCache(cache_path) as cache:
        getattr(cache, method)(original, symbol="DOCU", cutoff=CUTOFF)
    with ContextCache(cache_path) as cache:
        getattr(cache, method)(original, symbol="DOCU", cutoff=CUTOFF)
        assert cache.hits == 1
    saved = path.stat()
    raw = path.read_bytes()
    changed = raw.replace(b'"bid":99.0', b'"bid":19.0')
    assert changed != raw and len(changed) == len(raw)
    path.write_bytes(changed)
    os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    assert path.stat().st_size == saved.st_size and path.stat().st_mtime_ns == saved.st_mtime_ns
    with pytest.raises(ValueError, match="[Ii]nvalid immutable market snapshot"):
        with ContextCache(cache_path) as cache:
            getattr(cache, method)(refs() if new_ref else original, symbol="DOCU", cutoff=CUTOFF)


def test_receipt_validation_hook_change_requires_full_content_validation(tmp_path, monkeypatch):
    path, refs = receipt_case(tmp_path)
    ref = refs()[0]
    ref.verify_unchanged()
    def changed_validator(value):
        raise ValueError("fixture changed content validator")
    monkeypatch.setattr(market_collection, "snapshot_digest", changed_validator)
    with pytest.raises(ValueError, match="changed content validator"):
        ref.verify_unchanged()
