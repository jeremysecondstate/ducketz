from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from requests.exceptions import ReadTimeout

from datafetching import databento_opra_history as native
from ml.artifacts import file_checksum


def _repair_fixture(tmp_path, monkeypatch, *, fail_validation=False):
    staging = tmp_path / "partition-attempt"
    staging.mkdir()
    definition_path = staging / "point-in-time-definition.dbn.zst"
    parquet_path = staging / "normalized.parquet"
    unresolved = pd.DataFrame({"instrument_id": [123]})

    def validate(store, *, request):
        assert store.valid and not definition_path.exists()
        if fail_validation:
            raise ValueError("synthetic invalid definition metadata")

    monkeypatch.setattr(native, "_validate_dbn_request_metadata", validate)
    monkeypatch.setattr(native, "_definition_symbol_mapping",
                        lambda *a, **k: ({123: "SPY fixture"}, [{"instrument_id": 123}]))
    monkeypatch.setattr(native, "_fill_null_symbols", lambda *a, **k: None)
    monkeypatch.setattr(native, "_normalized_symbol_null_rows", lambda *a: pd.DataFrame())
    store = SimpleNamespace(valid=True, to_df=lambda **k: pd.DataFrame({"instrument_id": [123]}))
    kwargs = dict(parquet_path=parquet_path, definition_path=definition_path,
                  schema="ohlcv-1h", day="2026-01-01", symbols=("SPY.OPT",),
                  unresolved_rows=unresolved)
    return definition_path, store, kwargs


def test_definition_retries_preserve_open_partials_outside_published_staging(tmp_path, monkeypatch):
    destination, store, kwargs = _repair_fixture(tmp_path, monkeypatch)
    requests, handles, pauses = [], [], []
    successful_bytes = b"complete-native-definition-fixture"
    monkeypatch.setattr(native.time, "sleep", pauses.append)

    def get_range(**request):
        requests.append(request)
        path = Path(request["path"])
        assert not path.is_relative_to(destination.parent)
        if len(requests) <= 10:
            handle = path.open("x+b")
            handle.write(f"partial-{len(requests)}".encode())
            handle.flush()
            handles.append(handle)
            raise ReadTimeout("synthetic remote read timeout")
        assert all(not handle.closed for handle in handles)
        path.write_bytes(successful_bytes)
        return store

    try:
        result = native._repair_batch_ohlcv_symbol_mapping(
            SimpleNamespace(timeseries=SimpleNamespace(get_range=get_range)), **kwargs)
        assert len(requests) == 11 and pauses == [4.0] * 10
        paths = [Path(request["path"]) for request in requests]
        assert len(set(paths)) == 11
        assert all({k: v for k, v in request.items() if k != "path"} == result["request"]
                   for request in requests)
        assert destination.read_bytes() == successful_bytes
        assert set(destination.parent.iterdir()) == {destination}
        assert result["source"] == {"path": destination.name, "size_bytes": len(successful_bytes),
                                    "checksum_sha256": file_checksum(destination)}
        assert result["post_repair_null_symbol_count"] == 0
        for attempt, path in enumerate(paths[:-1], start=1):
            assert path.read_bytes() == f"partial-{attempt}".encode()
    finally:
        for handle in handles:
            handle.close()


def test_invalid_definition_never_enters_partition_staging(tmp_path, monkeypatch):
    destination, store, kwargs = _repair_fixture(tmp_path, monkeypatch, fail_validation=True)
    requests = []

    def get_range(**request):
        requests.append(request)
        Path(request["path"]).write_bytes(b"invalid-native-definition")
        return store

    with pytest.raises(ValueError, match="invalid definition metadata"):
        native._repair_batch_ohlcv_symbol_mapping(
            SimpleNamespace(timeseries=SimpleNamespace(get_range=get_range)), **kwargs)
    assert len(requests) == 1 and not destination.exists()
    assert Path(requests[0]["path"]).read_bytes() == b"invalid-native-definition"


def test_existing_definition_destination_is_preserved_without_request(tmp_path, monkeypatch):
    destination, _store, kwargs = _repair_fixture(tmp_path, monkeypatch)
    destination.write_bytes(b"prior-definition")
    client = SimpleNamespace(timeseries=SimpleNamespace(
        get_range=lambda **k: pytest.fail("existing destination reached provider")))
    with pytest.raises(FileExistsError):
        native._repair_batch_ohlcv_symbol_mapping(client, **kwargs)
    assert destination.read_bytes() == b"prior-definition"
