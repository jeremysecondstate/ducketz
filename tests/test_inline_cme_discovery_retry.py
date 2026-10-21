"""Inline CME discovery recovers remote errors and retains local failures."""
from __future__ import annotations

import pandas as pd
import pytest

from datafetching import databento_fetch
from datafetching.parquet_store import ParquetStore


class GatewayTimeout(Exception):
    status_code = 504


def test_inline_cme_discovery_retries_gateway_then_continues(tmp_path, monkeypatch):
    attempts = []
    delays = []
    retry_calls = []
    materialized = []

    class Provider:
        def specs(self):
            attempts.append("discover")
            if len(attempts) == 1:
                raise GatewayTimeout("The remote gateway timed out.")
            return ()

    retry = databento_fetch.call_with_persistent_databento_retry

    def retry_without_wait(operation, **kwargs):
        retry_calls.append(kwargs)
        return retry(operation, sleep=delays.append, **kwargs)

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", Provider)
    monkeypatch.setattr(databento_fetch, "call_with_persistent_databento_retry", retry_without_wait)
    monkeypatch.setattr(databento_fetch, "materialize_cme_cross_asset_context",
                        lambda root: materialized.append(root))
    store = ParquetStore(tmp_path)

    assert databento_fetch._fetch_cme_unlocked(store) == (0, 0, 0)
    assert attempts == ["discover", "discover"]
    assert len(delays) == 1 and delays[0] > 0
    assert len(retry_calls) == 1
    assert retry_calls[0]["operation_name"] == "CME endpoint discovery"
    assert materialized == [tmp_path]
    assert not list(tmp_path.rglob("errors/**/*.parquet"))


def test_inline_cme_discovery_permanent_error_keeps_original_evidence(tmp_path, monkeypatch):
    attempts = []

    class Provider:
        def specs(self):
            attempts.append("discover")
            raise ValueError("Invalid CME fixture configuration")

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", Provider)
    monkeypatch.setattr(databento_fetch, "materialize_cme_cross_asset_context",
                        lambda root: pytest.fail("Failed discovery must not materialize context"))

    assert databento_fetch._fetch_cme_unlocked(ParquetStore(tmp_path)) == (0, 1, 0)
    assert attempts == ["discover"]
    errors = list(tmp_path.rglob("errors/**/*.parquet"))
    assert len(errors) == 1
    frame = pd.read_parquet(errors[0])
    assert frame["symbol"].tolist() == ["CME_CONTEXT"]
    assert frame["source"].tolist() == ["databento"]
    assert frame["request_key"].tolist() == ["cme_context"]
    assert frame["error_type"].tolist() == ["ValueError"]
    assert frame["error_message"].tolist() == ["Invalid CME fixture configuration"]


def test_inline_cme_discovery_manual_interruption_propagates(tmp_path, monkeypatch):
    attempts = []

    class Provider:
        def specs(self):
            attempts.append("discover")
            raise KeyboardInterrupt("Manual fixture interruption")

    monkeypatch.setattr(databento_fetch, "DatabentoCmeContextProvider", Provider)
    monkeypatch.setattr(databento_fetch, "materialize_cme_cross_asset_context",
                        lambda root: pytest.fail("Interrupted discovery must not materialize context"))

    with pytest.raises(KeyboardInterrupt, match="Manual fixture interruption"):
        databento_fetch._fetch_cme_unlocked(ParquetStore(tmp_path))
    assert attempts == ["discover"]
    assert not list(tmp_path.rglob("errors/**/*.parquet"))
