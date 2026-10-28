"""Verified context reuse, including same-size tampering and invalidation."""
import concurrent.futures
import hashlib
import json
import os

import pytest

from ml.intraday import historical_market_inputs as module
from tests.test_intraday_historical_market_inputs import T, archive, build, native


def artifact(tmp_path):
    archive(tmp_path, [native()])
    return build(tmp_path)


def test_reuses_verified_reconstruction_with_identical_join_and_no_shared_mutation(tmp_path, monkeypatch):
    ref = artifact(tmp_path)
    calls = []
    original = module._features
    def tracked(row):
        calls.append(1)
        return original(row)
    monkeypatch.setattr(module, "_features", tracked)
    one = module.HistoricalMarketInputs(tmp_path, ref["path"])
    expected = one.join([T], cutoff=T)
    two = module.HistoricalMarketInputs(tmp_path, ref["path"])
    assert len(calls) == 1
    assert two.join([T], cutoff=T) == expected
    expected["rows"][0][0] = 999
    expected["provenance"][0]["sources"].clear()
    assert two.join([T], cutoff=T) != expected
    with pytest.raises(TypeError):
        one._lookup[next(iter(one._lookup))]["features"]["spread_relative"] = 999


def test_concurrent_symbols_validate_exact_artifact_once(tmp_path, monkeypatch):
    ref = artifact(tmp_path)
    calls = []
    original = module._features
    def tracked(row):
        calls.append(1)
        return original(row)
    monkeypatch.setattr(module, "_features", tracked)
    def load(_):
        return module.HistoricalMarketInputs(tmp_path, ref["path"]).join([T], cutoff=T)
    with concurrent.futures.ThreadPoolExecutor(max_workers=11) as pool:
        outputs = list(pool.map(load, range(11)))
    assert len(calls) == 1
    assert all(value == outputs[0] for value in outputs)


def test_cache_hit_reads_all_source_bytes_even_when_size_and_mtime_unchanged(tmp_path):
    ref = artifact(tmp_path)
    path = module.Path(ref["path"])
    module.HistoricalMarketInputs(tmp_path, path)
    before = path.stat()
    raw = path.read_bytes()
    changed = raw.replace(b'"instrument_id":99', b'"instrument_id":98', 1)
    assert changed != raw and len(changed) == len(raw)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        module.HistoricalMarketInputs(tmp_path, path)
    path.write_bytes(raw)
    assert module.HistoricalMarketInputs(tmp_path, path).join([T], cutoff=T)["rows"]


def test_content_address_changed_source_revalidates_semantics(tmp_path):
    ref = artifact(tmp_path)
    path = module.Path(ref["path"])
    module.HistoricalMarketInputs(tmp_path, path)
    value = json.loads(path.read_bytes())
    value["rows"][0]["features"]["spread_relative"] = 99
    # Even an object renamed to its new whole-file hash cannot reuse the old
    # successful semantic validation or hide its broken original row hash.
    raw = module._bytes(value)
    changed = path.with_name(hashlib.sha256(raw).hexdigest()+".json")
    changed.write_bytes(raw)
    with pytest.raises(ValueError, match="selected-row hash mismatch"):
        module.HistoricalMarketInputs(tmp_path, changed)


def test_validator_contract_change_invalidates_prior_semantic_proof(tmp_path, monkeypatch):
    ref = artifact(tmp_path)
    module.HistoricalMarketInputs(tmp_path, ref["path"])
    monkeypatch.setattr(module, "_features", lambda row: {"changed_contract":True})
    with pytest.raises(ValueError, match="feature differs"):
        module.HistoricalMarketInputs(tmp_path, ref["path"])


def test_cache_is_bounded_and_originals_are_retained(tmp_path):
    module._READERS.clear()
    references = []
    for index in range(module._READER_LIMIT+2):
        root = tmp_path / str(index)
        references.append((root, artifact(root)))
        module.HistoricalMarketInputs(root, references[-1][1]["path"])
    assert len(module._READERS) == module._READER_LIMIT
    assert all(module.Path(ref["path"]).is_file() for _, ref in references)
