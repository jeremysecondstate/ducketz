from __future__ import annotations

import asyncio
import errno
import json
import socket
import ssl
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
from http.client import IncompleteRead, RemoteDisconnected
from types import SimpleNamespace

import pytest
from requests import Response
from requests import exceptions as requests_errors
from urllib3 import exceptions as urllib3_errors

from app.services import databento_retry as retry


@pytest.fixture
def bento_errors(offline_databento_sdk):
    # The unrelated SDK Live class creates a socket during import on Windows;
    # use the opt-in offline fixture before loading the actual error classes.
    from databento.common import error

    return error


def http_error(status: int, *, headers=None, message="private provider body"):
    response = Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests_errors.HTTPError(message, response=response)


def wrapped_stream_error(bento_errors, underlying: Exception):
    try:
        raise underlying
    except Exception:
        try:
            raise bento_errors.BentoError(
                "Error streaming response: Response ended prematurely"
            ) from None
        except bento_errors.BentoError as exc:
            return exc


def test_default_persists_beyond_old_limit_and_keeps_timing(monkeypatch) -> None:
    attempts = 0
    sleeps, stages, annotations, reports = [], [], [], []

    @contextmanager
    def stage(name, **kwargs):
        stages.append((name, kwargs))
        yield SimpleNamespace(annotate=lambda **fields: annotations.append(fields))

    monkeypatch.setattr(retry, "timed_stage", stage)

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts <= 80:
            raise requests_errors.ReadTimeout("secret response")
        return [1, 2, 3]

    assert retry.call_with_persistent_databento_retry(
        operation, operation_name="history", symbol="LOCAL", schema="ohlcv-1d",
        request_start="start", request_end="end", sleep=sleeps.append,
        reporter=reports.append,
    ) == [1, 2, 3]
    assert attempts == 81
    assert sleeps == [4.0] * 80
    assert [kwargs["attempt"] for _, kwargs in stages] == list(range(1, 82))
    assert all(name == "provider.request" for name, _ in stages)
    assert stages[-1][1]["symbol"] == "LOCAL"
    assert stages[-1][1]["schema"] == "ohlcv-1d"
    assert stages[-1][1]["request_start"] == "start"
    assert stages[-1][1]["request_end"] == "end"
    assert annotations == [{"row_count": 3, "operation": "fetched"}]
    assert "attempt 81" in reports[-1]
    assert all("secret response" not in line for line in reports)


def test_explicit_attempt_budget_reraises_final_exception() -> None:
    errors = [http_error(503) for _ in range(3)]
    attempts, callbacks, sleeps = [], [], []

    def operation():
        exc = errors[len(attempts)]
        attempts.append(exc)
        raise exc

    with pytest.raises(requests_errors.HTTPError) as caught:
        retry.call_with_persistent_databento_retry(
            operation, operation_name="history", max_attempts=3,
            before_retry=lambda exc, attempt: callbacks.append((exc, attempt)),
            sleep=sleeps.append, reporter=None,
        )
    assert caught.value is errors[-1]
    assert attempts == errors
    assert callbacks == [(errors[0], 1), (errors[1], 2)]
    assert sleeps == [4.0, 4.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_actual_http_status_retries_with_sanitized_logs(status) -> None:
    reports = []
    results = [http_error(status, message="sensitive-body auth=secret"), "done"]

    def operation():
        value = results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    assert retry.call_with_persistent_databento_retry(
        operation, operation_name="history", max_attempts=2,
        sleep=lambda seconds: None, reporter=reports.append,
    ) == "done"
    assert f"http_status={status}" in reports[0]
    assert all("sensitive-body" not in line and "auth=secret" not in line for line in reports)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 422, 429, 501])
def test_nonretryable_status_overrides_text_and_retry_after(status) -> None:
    exc = http_error(status, headers={"Retry-After": "7200"}, message="503 read timed out")
    actions = []

    def operation():
        actions.append("request")
        raise exc

    with pytest.raises(requests_errors.HTTPError) as caught:
        retry.call_with_persistent_databento_retry(
            operation, operation_name="history",
            sleep=lambda seconds: actions.append("sleep"),
            before_retry=lambda exc, attempt: actions.append("retry"), reporter=None,
        )
    assert caught.value is exc
    assert actions == ["request"]


def test_status_precedes_even_typed_read_timeout() -> None:
    exc = requests_errors.ReadTimeout("Read timed out")
    exc.response = SimpleNamespace(status_code=401)
    assert not retry.is_retryable_databento_error(exc)


@pytest.mark.parametrize("header, expected", [
    ("7200", 7200.0), ("1", 4.0), ("0", 4.0), ("12.5", 12.5),
    ("-5", 4.0), ("nan", 4.0), ("inf", 4.0), ("not a date", 4.0),
    ("9" * 400, 4.0),
])
def test_retry_after_uses_longer_server_delay_without_cap(header, expected) -> None:
    results = [http_error(503, headers={"Retry-After": header}), "ok"]
    sleeps = []

    def operation():
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    assert retry.call_with_persistent_databento_retry(
        operation, operation_name="history", max_attempts=2,
        sleep=sleeps.append, reporter=None,
    ) == "ok"
    assert sleeps == [expected]


def test_sdk_retry_after_http_date_and_past_date(bento_errors, monkeypatch) -> None:
    now = datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(retry.time, "time", lambda: now)
    future = format_datetime(datetime.fromtimestamp(now + 10800, tz=timezone.utc), usegmt=True)
    exc = bento_errors.BentoServerError(503, headers={"retry-after": future})
    assert retry.is_retryable_databento_error(exc)
    assert retry._retry_after_seconds(exc) == 10800
    past = format_datetime(datetime.fromtimestamp(now - 60, tz=timezone.utc), usegmt=True)
    assert retry._retry_after_seconds(http_error(503, headers={"Retry-After": past})) == 0
    assert not retry.is_retryable_databento_error(
        bento_errors.BentoClientError(429, message="503 Response ended prematurely")
    )


@pytest.mark.parametrize("exc", [
    requests_errors.ReadTimeout("read timeout"),
    urllib3_errors.ReadTimeoutError(None, "/v0/timeseries.get_range", "read timeout"),
    requests_errors.ConnectionError(urllib3_errors.ReadTimeoutError(None, "/", "read timeout")),
    requests_errors.ChunkedEncodingError(urllib3_errors.ProtocolError("Response ended prematurely")),
    requests_errors.ChunkedEncodingError(urllib3_errors.ProtocolError("Connection broken", IncompleteRead(b"x", 4))),
    urllib3_errors.ProtocolError("Connection broken", ConnectionResetError(errno.ECONNRESET, "reset")),
    IncompleteRead(b"x", 4), urllib3_errors.IncompleteRead(1, 4),
    RemoteDisconnected("remote disconnected"), ConnectionResetError(errno.ECONNRESET, "reset"),
])
def test_typed_remote_errors_are_retryable(exc, bento_errors) -> None:
    assert retry.is_retryable_databento_error(exc)
    wrapped = wrapped_stream_error(bento_errors, exc)
    assert wrapped.__suppress_context__ is True
    assert wrapped.__context__ is exc
    assert retry.is_retryable_databento_error(wrapped)


def test_premature_chunk_header_value_error_context_is_retryable(bento_errors) -> None:
    try:
        int(b"", 16)
    except ValueError:
        try:
            raise urllib3_errors.ProtocolError("Response ended prematurely") from None
        except urllib3_errors.ProtocolError as exc:
            wrapped = wrapped_stream_error(bento_errors, requests_errors.ChunkedEncodingError(exc))
    assert retry.is_retryable_databento_error(wrapped)


@pytest.mark.parametrize("underlying", [
    OSError(errno.ENOSPC, "disk full"), ssl.SSLError("TLS configuration"),
    ValueError("invalid parser input"),
])
def test_premature_stream_message_cannot_hide_local_context(underlying, bento_errors) -> None:
    try:
        raise underlying
    except Exception:
        try:
            raise urllib3_errors.ProtocolError("Response ended prematurely") from None
        except urllib3_errors.ProtocolError as exc:
            wrapped = wrapped_stream_error(bento_errors, requests_errors.ChunkedEncodingError(exc))
    assert not retry.is_retryable_databento_error(wrapped)


@pytest.mark.parametrize("exc", [
    ValueError("503 Read timed out"), RuntimeError("503 Service unavailable"),
    RuntimeError("HTTPSConnectionPool(host='hist.databento.com', port=443): Read timed out. (read timeout=100)"),
    PermissionError("Response ended prematurely"), FileNotFoundError("503"),
    OSError(errno.ENOSPC, "No space left on device; 503"),
    socket.gaierror(socket.EAI_NONAME, "Read timed out"), ssl.SSLError("503"),
    requests_errors.SSLError("Read timed out"), requests_errors.ProxyError("503"),
    requests_errors.InvalidURL("503"), requests_errors.InvalidSchema("503"),
    requests_errors.MissingSchema("503"), requests_errors.ConnectTimeout("503"),
    requests_errors.Timeout("503"), requests_errors.ContentDecodingError("503"),
    requests_errors.ConnectionError("read timed out"),
    json.JSONDecodeError("503", "x", 0),
    urllib3_errors.SSLError("503"), urllib3_errors.ProxyError("503", OSError("proxy")),
    urllib3_errors.NewConnectionError(None, "DNS read timed out"),
    urllib3_errors.NameResolutionError("hist.databento.com", None, socket.gaierror("DNS")),
    urllib3_errors.InvalidChunkLength(SimpleNamespace(tell=lambda: 0, length_remaining=1), b"invalid"),
    urllib3_errors.ResponseNotChunked("503"),
    urllib3_errors.ProtocolError("Connection broken", OSError(errno.ENOSPC, "disk")),
    requests_errors.ConnectionError(urllib3_errors.SSLError("503")),
    requests_errors.ChunkedEncodingError(urllib3_errors.DecodeError("503")),
])
def test_local_unknown_and_parser_errors_stop_inside_sdk_stream(exc, bento_errors) -> None:
    assert not retry.is_retryable_databento_error(exc)
    # Misleading wrapper text cannot override the actual writer/decoder error.
    assert not retry.is_retryable_databento_error(wrapped_stream_error(bento_errors, exc))


def test_unknown_exception_does_not_inherit_old_transient_context() -> None:
    try:
        raise requests_errors.ReadTimeout("transient")
    except requests_errors.ReadTimeout:
        try:
            raise ValueError("local validation failure")
        except ValueError as exc:
            assert not retry.is_retryable_databento_error(exc)


@pytest.mark.parametrize("message, expected", [
    ("Error streaming response: Response ended prematurely", True),
    ("Error streaming response: HTTPSConnectionPool(host='hist.databento.com', port=443): Read timed out. (read timeout=100)", True),
    ("503", False), ("gateway timeout", False),
    ("Error streaming response: [Errno 28] No space left on device", False),
    ("HTTPSConnectionPool(host='localhost', port=443): Read timed out. (read timeout=100)", False),
])
def test_sdk_only_fallback_is_exact_and_remote(message, expected, bento_errors) -> None:
    assert retry.is_retryable_databento_error(bento_errors.BentoError(message)) is expected


@pytest.mark.parametrize("kwargs", [
    {"delay_seconds": value} for value in [0, -1, float("nan"), float("inf"), float("-inf"), True, "4", None, 10 ** 1000]
] + [
    {"max_attempts": value} for value in [0, -1, True, 1.5, "3", float("inf"), float("nan")]
])
def test_invalid_arguments_fail_before_operation(kwargs) -> None:
    calls = []
    with pytest.raises(ValueError):
        retry.call_with_persistent_databento_retry(
            lambda: calls.append("called"), operation_name="history", **kwargs,
        )
    assert calls == []


def test_cleanup_callback_precedes_delay_and_next_attempt() -> None:
    events = []
    error = http_error(502)

    def operation():
        events.append("request")
        if events == ["request"]:
            raise error
        return "ok"

    def cleanup(exc, attempt):
        assert exc is error
        assert attempt == 1
        events.append("cleanup")

    assert retry.call_with_persistent_databento_retry(
        operation, operation_name="history", before_retry=cleanup,
        sleep=lambda seconds: events.append(("sleep", seconds)), reporter=None,
    ) == "ok"
    assert events == ["request", "cleanup", ("sleep", 4.0), "request"]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(2), asyncio.CancelledError()])
@pytest.mark.parametrize("location", ["operation", "cleanup", "sleep"])
def test_interruption_propagates_immediately(interruption, location) -> None:
    events = []

    def action(name):
        events.append(name)
        if name == location:
            raise interruption
        if name == "operation":
            raise requests_errors.ReadTimeout("remote")

    with pytest.raises(type(interruption)) as caught:
        retry.call_with_persistent_databento_retry(
            lambda: action("operation"), operation_name="history",
            before_retry=lambda exc, attempt: action("cleanup"),
            sleep=lambda seconds: action("sleep"), reporter=None,
        )
    assert caught.value is interruption
    order = ["operation", "cleanup", "sleep"]
    assert events == order[:order.index(location) + 1]


def test_cleanup_failure_stops_before_sleep_or_repeat() -> None:
    events = []
    cleanup_error = OSError(errno.EACCES, "staging cleanup denied")

    def operation():
        events.append("request")
        raise requests_errors.ReadTimeout("remote")

    def cleanup(exc, attempt):
        events.append("cleanup")
        raise cleanup_error

    with pytest.raises(OSError) as caught:
        retry.call_with_persistent_databento_retry(
            operation, operation_name="history", before_retry=cleanup,
            sleep=lambda seconds: events.append("sleep"), reporter=None,
        )
    assert caught.value is cleanup_error
    assert events == ["request", "cleanup"]
