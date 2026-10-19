"""Offline regression for the previously class-only primary acquisition error."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import data


OPEN = datetime(2026, 10, 19, 11, 45, tzinfo=timezone.utc)
CLOSE = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 19, 12, 2, 13, tzinfo=timezone.utc)


class BentoServerError(Exception):
    status_code = 504
    headers = {"Retry-After": "7"}


class Provider:
    dataset = "EQUS.MINI"
    api_key = "private-provider-key"

    def __init__(self, failure=None, stage="native_bars"):
        self.failure, self.stage = failure, stage

    def fail_at(self, stage):
        if self.failure is not None and self.stage == stage:
            raise self.failure

    def native_specs(self):
        self.fail_at("native_specs")
        return [SimpleNamespace(schema="ohlcv-1m", frequency="1m")]

    def dataset_range(self):
        self.fail_at("dataset_range")
        return {}

    def available_range_for_schema(self, schema, **kwargs):
        self.fail_at("schema_range")
        return SimpleNamespace(start=OPEN, end=CLOSE)

    def fetch_native_bars_range(self, symbols, spec, **kwargs):
        self.fail_at("native_bars")
        return {symbol: ([], None) for symbol in symbols}, SimpleNamespace(start=OPEN, end=CLOSE)


def acquire(provider, batch=False):
    kwargs = dict(opened_at=OPEN, attempt_id="regression", clock=lambda: NOW)
    if batch:
        return data.fetch_databento_interval_attempts(provider, symbols=["AAPL", "AMZN"], **kwargs)
    return {"AAPL": data.fetch_databento_interval_attempt(provider, symbol="AAPL", **kwargs)}


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("stage", ["native_specs", "dataset_range", "schema_range", "native_bars"])
def test_retains_provider_cause_and_stage_without_changing_retry(batch, stage):
    error = BentoServerError("upstream gateway timed out after 60 seconds")
    error.__cause__ = TimeoutError("upstream read timed out")
    for response in acquire(Provider(error, stage), batch).values():
        assert response.status == "temporary_error"
        assert response.error == "BentoServerError"
        assert response.retry_after_seconds == 7
        assert response.rows == ()
        diagnostics = response.failure_diagnostics
        assert diagnostics["stage"] == stage
        assert diagnostics["chain"][0]["http_status"] == 504
        assert diagnostics["chain"][0]["cause"] == "upstream gateway timed out after 60 seconds"
        assert diagnostics["chain"][1]["error_type"] == "TimeoutError"
        assert diagnostics["chain"][1]["cause"] == "upstream read timed out"


@pytest.mark.parametrize("batch", [False, True])
def test_diagnostics_redact_credentials_and_never_read_response_body(batch):
    class Response:
        status_code = 504
        headers = {"Retry-After": "7", "Set-Cookie": "private-cookie"}

        @property
        def text(self):
            raise AssertionError("Arbitrary response bodies must not be read")

    error = BentoServerError(
        "gateway timeout private-provider-key https://provider.invalid/path?token=query-secret "
        "Authorization: Basic basic-secret Bearer bearer-secret password=password-secret")
    error.response = Response()
    error.__context__ = ValueError("api_key=nested-secret")
    for response in acquire(Provider(error), batch).values():
        encoded = json.dumps(response.failure_diagnostics)
        assert "gateway timeout" in encoded
        for secret in ("private-provider-key", "query-secret", "basic-secret", "bearer-secret",
                       "password-secret", "nested-secret", "private-cookie", "provider.invalid"):
            assert secret not in encoded


@pytest.mark.parametrize("batch", [False, True])
def test_chain_and_message_are_bounded_and_cycles_terminate(batch):
    root = BentoServerError("failure " + "x" * 4000)
    current = root
    for index in range(6):
        child = ValueError(str(index))
        current.__cause__ = child
        current = child
    current.__cause__ = root
    diagnostics = acquire(Provider(root), batch)["AAPL"].failure_diagnostics
    assert len(diagnostics["chain"]) == 4
    assert diagnostics["chain_truncated"] is True
    assert all(len(item["cause"]) <= 1200 for item in diagnostics["chain"])


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("status,expected", [(429, "temporary_error"), (403, "permanent_error")])
def test_classification_and_retry_after_remain_unchanged(batch, status, expected):
    error = BentoServerError("provider rejection")
    error.status_code = status
    response = acquire(Provider(error), batch)["AAPL"]
    assert (response.status, response.error, response.retry_after_seconds) == (expected, "BentoServerError", 7)
    assert response.failure_diagnostics["chain"][0]["http_status"] == status


@pytest.mark.parametrize("batch", [False, True])
def test_success_and_control_errors_remain_unchanged(batch):
    response = acquire(Provider(), batch)["AAPL"]
    assert response.status == "success"
    assert response.failure_diagnostics is None
    for error in (InterruptedError("manual stop"), data.ContractError("invalid provider scope")):
        with pytest.raises(type(error), match=str(error)):
            acquire(Provider(error), batch)


def test_journal_preserves_old_bytes_and_roundtrips_new_diagnostics(tmp_path, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *args, **kwargs: {"offline_test": True})
    journal = data.AcquisitionJournal(tmp_path / "acquisition.sqlite3")
    legacy = acquire(Provider())["AAPL"]
    legacy_record = asdict(legacy)
    legacy_record.pop("failure_diagnostics", None)
    legacy_bytes = data._json(legacy_record)
    slot = journal.slot(legacy.symbol, legacy.source_id, legacy.interval_open, legacy.interval_close)
    with sqlite3.connect(journal.path) as db:
        db.execute("INSERT INTO attempts VALUES(?,?,?)", (legacy.attempt_id, slot, legacy_bytes))
    loaded = journal.responses(symbol=legacy.symbol, source_id=legacy.source_id,
                               interval_open=legacy.interval_open, interval_close=legacy.interval_close)[0]
    assert loaded.failure_diagnostics is None
    journal.save(loaded)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT payload FROM attempts WHERE id=?", (legacy.attempt_id,)).fetchone()[0] == legacy_bytes
    failed = replace(acquire(Provider(BentoServerError("gateway timeout")))["AAPL"], attempt_id="failed")
    journal.save(failed)
    restored = journal.responses(symbol=failed.symbol, source_id=failed.source_id,
                                 interval_open=failed.interval_open, interval_close=failed.interval_close)[-1]
    assert restored.failure_diagnostics == failed.failure_diagnostics
    with pytest.raises(data.ContractError, match="immutable"):
        journal.save(replace(restored, failure_diagnostics={"stage": "different"}))
