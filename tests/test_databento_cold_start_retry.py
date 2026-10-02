from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from requests.exceptions import HTTPError, ReadTimeout

from datafetching import databento_cold_start as cold


def _http_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return HTTPError("fixture response", response=SimpleNamespace(status_code=status, headers=headers))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_metadata_recovers_beyond_old_attempt_and_wait_limits(status):
    calls, sleeps = [], []
    def request():
        calls.append(1)
        if len(calls) <= 80:
            raise _http_error(status, 9 if len(calls) == 1 else None)
        return 17
    assert cold._metadata_call(request, _retry_sleeper=sleeps.append) == 17
    assert len(calls) == 81 and sleeps == [9.0] + [4.0] * 79


@pytest.mark.parametrize("error", [
    *[_http_error(status) for status in (400, 401, 403, 404, 408, 429, 501)],
    RuntimeError("504 local metadata parser failed"), PermissionError("local storage denied"),
])
def test_metadata_permanent_errors_stop_without_sleep(error):
    calls, sleeps = [], []
    def request():
        calls.append(1)
        raise error
    with pytest.raises(cold.ColdStartError) as caught:
        cold._metadata_call(request, _retry_sleeper=sleeps.append)
    assert caught.value.__cause__ is error
    assert calls == [1] and not sleeps


def _batch(payload=b"verified DBN"):
    return SimpleNamespace(
        _gateway="https://hist.databento.com", _base_url="https://hist.databento.com/v0/batch",
        _key="fixture-key",
        list_files=lambda _job: [{"filename": "fixture.dbn.zst", "size": len(payload),
                                 "hash": "sha256:" + hashlib.sha256(payload).hexdigest()}],
    )


class _Response:
    def __init__(self, *, status=200, chunks=(b"verified DBN",)):
        self.status_code = status
        self.chunks = chunks
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise _http_error(self.status_code)
    def iter_content(self, **kwargs):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def test_batch_polls_and_metadata_retry_beyond_old_limits():
    calls, sleeps = [], []
    def details(job):
        calls.append(job)
        if len(calls) <= 80:
            raise ReadTimeout("fixture")
        return {"state": "done"}
    assert cold._wait_for_batch_fallback(SimpleNamespace(get_job_details=details),
        job_id="JOB", reporter=None, sleeper=sleeps.append) == {"state": "done"}
    assert len(calls) == 81 and sleeps == [4.0] * 80


def test_batch_file_metadata_and_download_persist_with_all_partials(tmp_path, monkeypatch):
    batch = _batch()
    native_files = batch.list_files
    metadata_calls, download_calls, sleeps = [], [], []
    def files(job):
        metadata_calls.append(job)
        if len(metadata_calls) <= 80:
            raise _http_error(503)
        return native_files(job)
    batch.list_files = files
    def get(url, **kwargs):
        download_calls.append(url)
        assert url == "https://hist.databento.com/v0/batch/download/JOB/fixture.dbn.zst"
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (100, 100)
        assert kwargs["auth"].username == "fixture-key"
        if len(download_calls) <= 80:
            return _Response(chunks=(b"partial", ReadTimeout("fixture")))
        return _Response()
    monkeypatch.setattr(cold.requests, "get", get)
    source, _ = cold._download_batch_fallback_file(batch, staging_base=tmp_path,
        job_id="JOB", sleeper=sleeps.append, reporter=None)
    assert source.read_bytes() == b"verified DBN"
    assert len(metadata_calls) == len(download_calls) == 81
    assert sleeps == [4.0] * 160
    attempts = sorted((tmp_path / "batch-downloads").iterdir())
    assert len(attempts) == 81
    assert all((path / "fixture.dbn.zst").read_bytes() == b"partial" for path in attempts[:-1])


@pytest.mark.parametrize("status", [301, 307, 401, 403, 429])
def test_batch_redirects_auth_and_rate_limits_stop_once(tmp_path, monkeypatch, status):
    calls, sleeps = [], []
    def get(*args, **kwargs):
        calls.append(1)
        return _Response(status=status)
    monkeypatch.setattr(cold.requests, "get", get)
    with pytest.raises((HTTPError, cold.ColdStartInfrastructureError)):
        cold._download_batch_fallback_file(_batch(), staging_base=tmp_path,
            job_id="JOB", sleeper=sleeps.append, reporter=None)
    assert calls == [1] and not sleeps


@pytest.mark.parametrize("field,value", [
    ("filename", "../unsafe.dbn.zst"), ("filename", "alternate:stream.dbn.zst"),
    ("size", 0), ("size", -1), ("size", True), ("size", 1.5), ("size", "12"),
    ("size", float("nan")), ("size", float("inf")),
    ("hash", "sha256:bad"), ("hash", "md5:abc"),
])
def test_batch_malformed_metadata_stops_before_download(tmp_path, monkeypatch, field, value):
    batch = _batch()
    record = batch.list_files("JOB")[0]
    record[field] = value
    batch.list_files = lambda _job: [record]
    monkeypatch.setattr(cold.requests, "get", lambda *a, **kw: pytest.fail("invalid metadata downloaded"))
    with pytest.raises(cold.ColdStartInfrastructureError):
        cold._download_batch_fallback_file(batch, staging_base=tmp_path, job_id="JOB",
            sleeper=lambda _delay: pytest.fail("invalid metadata retried"), reporter=None)


@pytest.mark.parametrize("attribute,value", [
    ("_gateway", "https://example.com"),
    ("_base_url", "https://hist.databento.com.example.com/v0/batch"),
    ("_base_url", "https://hist.databento.com/v0/batch?redirect=example.com"),
    ("_base_url", "https://user@hist.databento.com/v0/batch"),
])
def test_batch_authentication_never_leaves_fixed_origin(tmp_path, monkeypatch, attribute, value):
    batch = _batch()
    setattr(batch, attribute, value)
    monkeypatch.setattr(cold.requests, "get", lambda *a, **kw: pytest.fail("unsafe endpoint called"))
    with pytest.raises(cold.ColdStartInfrastructureError, match="Unsafe"):
        cold._download_batch_file_once(batch, job_id="JOB", filename="fixture.dbn.zst",
                                      target=tmp_path / "file")


def test_batch_local_storage_failure_with_network_context_is_not_retried(tmp_path, monkeypatch):
    original_open = Path.open
    opens, sleeps = [], []
    def denied(path, *args, **kwargs):
        if path.name == "fixture.dbn.zst":
            opens.append(path)
            try:
                raise ReadTimeout("earlier remote failure")
            except ReadTimeout:
                raise PermissionError("local disk denied")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(cold.requests, "get", lambda *a, **kw: pytest.fail("local failure sent request"))
    with pytest.raises(PermissionError):
        cold._download_batch_fallback_file(_batch(), staging_base=tmp_path, job_id="JOB",
            sleeper=sleeps.append, reporter=None)
    assert len(opens) == 1 and not sleeps


def test_batch_checksum_failure_is_not_downloaded_again(tmp_path, monkeypatch):
    calls, sleeps = [], []
    def get(*a, **kw):
        calls.append(1)
        return _Response(chunks=(b"corrupt DBN!",))
    monkeypatch.setattr(cold.requests, "get", get)
    with pytest.raises(cold.ColdStartInfrastructureError, match="checksum"):
        cold._download_batch_fallback_file(_batch(), staging_base=tmp_path, job_id="JOB",
            sleeper=sleeps.append, reporter=None)
    assert calls == [1] and not sleeps


@pytest.mark.parametrize("failure", [ReadTimeout("lost response"), KeyboardInterrupt()])
def test_ambiguous_submission_is_preserved_and_never_resubmitted(tmp_path, failure):
    calls = []
    request = {"request_id": "fixture", "dataset": "XNAS.ITCH", "schema": "ohlcv-1m",
               "symbol_scope": ["AAPL"], "stype_in": "raw_symbol", "start": "2026-09-01", "end": "2026-09-02"}
    def submit(**kwargs):
        calls.append(kwargs)
        raise failure
    batch = SimpleNamespace(submit_job=submit)
    with pytest.raises((cold.ColdStartInfrastructureError, KeyboardInterrupt)):
        cold._load_or_submit_batch_fallback(batch, staging_base=tmp_path, request=request, reporter=None)
    intent = (tmp_path / "batch-submission-intent.json").read_bytes()
    for _ in range(2):
        with pytest.raises(cold.ColdStartInfrastructureError, match="unconfirmed"):
            cold._load_or_submit_batch_fallback(batch, staging_base=tmp_path, request=request, reporter=None)
    assert len(calls) == 1 and (tmp_path / "batch-submission-intent.json").read_bytes() == intent


def test_attempt_allocation_has_no_old_9999_limit(tmp_path, monkeypatch):
    original = Path.exists
    def exists(path):
        if path.parent == tmp_path and path.name.startswith("attempt-"):
            return int(path.name.removeprefix("attempt-")) <= 9999
        return original(path)
    monkeypatch.setattr(Path, "exists", exists)
    assert cold._next_attempt_directory(tmp_path).name == "attempt-10000"
