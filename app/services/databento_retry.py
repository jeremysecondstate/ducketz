from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead, RemoteDisconnected
from typing import TypeVar

from requests import exceptions as requests_errors
from urllib3 import exceptions as urllib3_errors

from datafetching.observability import timed_stage

_T = TypeVar("_T")

DATABENTO_RETRY_DELAY_SECONDS = 4.0
DATABENTO_RETRY_MAX_ATTEMPTS: int | None = None
_RETRYABLE_HTTP_STATUSES = {500, 502, 503, 504}
_LEGACY_READ_TIMEOUT = re.compile(
    r"HTTPSConnectionPool\(host='hist\.databento\.com', port=443\): "
    r"Read timed out\. \(read timeout=[0-9]+(?:\.[0-9]+)?\)"
)


def call_with_persistent_databento_retry(
    operation: Callable[[], _T],
    *,
    operation_name: str,
    delay_seconds: float = DATABENTO_RETRY_DELAY_SECONDS,
    max_attempts: int | None = DATABENTO_RETRY_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    reporter: Callable[[str], None] | None = print,
    before_retry: Callable[[Exception, int], None] | None = None,
    symbol: str | None = None,
    schema: str | None = None,
    request_start: object | None = None,
    request_end: object | None = None,
    timing_reporter: Callable[[str], None] | None = None,
) -> _T:
    """Retry known remote failures until success or interruption by default.

    A finite ``max_attempts`` includes the initial request. Requests are separated
    by at least ``delay_seconds``, or a longer server Retry-After. Before each retry,
    ``before_retry`` receives the exception and failed attempt number so a caller
    can prepare a fresh attempt while preserving partial files. Local failures
    and cancellation propagate.
    """
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)):
        raise ValueError("delay_seconds must be a positive finite number")
    try:
        valid_delay = math.isfinite(delay_seconds) and delay_seconds > 0
    except OverflowError:
        valid_delay = False
    if not valid_delay:
        raise ValueError("delay_seconds must be a positive finite number")
    if max_attempts is not None and (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise ValueError("max_attempts must be None or a positive integer")

    attempt = 0
    while True:
        attempt += 1
        try:
            with timed_stage(
                "provider.request",
                symbol=symbol,
                provider="databento",
                schema=schema,
                request_start=request_start,
                request_end=request_end,
                attempt=attempt,
                reporter=timing_reporter,
                extra={"operation_name": operation_name},
            ) as timing:
                result = operation()
                row_count = _result_row_count(result)
                timing.annotate(row_count=row_count, operation="fetched")
            if attempt > 1 and reporter is not None:
                reporter(
                    f"[Databento] {operation_name} succeeded on attempt {attempt}."
                )
            return result
        except Exception as exc:
            if not is_retryable_databento_error(exc) or (
                max_attempts is not None and attempt >= max_attempts
            ):
                raise
            retry_delay = max(delay_seconds, _retry_after_seconds(exc))
            if before_retry is not None:
                before_retry(exc, attempt)
            if reporter is not None:
                attempt_label = str(attempt) if max_attempts is None else f"{attempt}/{max_attempts}"
                reporter(
                    f"[Databento] {operation_name} attempt {attempt_label} "
                    f"failed with a transient {type(exc).__name__} "
                    f"http_status={_http_status(exc)}. Retrying "
                    f"in {retry_delay:.1f}s; press Ctrl+C to stop."
                )
            sleep(retry_delay)


def _result_row_count(result: object) -> int | None:
    """Best-effort provider row count without materializing an iterator."""

    if isinstance(result, tuple) and result:
        payload = result[0]
        if isinstance(payload, dict):
            total = 0
            for value in payload.values():
                if isinstance(value, tuple) and value:
                    value = value[0]
                if not hasattr(value, "__len__"):
                    return None
                total += len(value)
            return total
        if hasattr(payload, "__len__"):
            return len(payload)
    return len(result) if hasattr(result, "__len__") else None


def is_retryable_databento_error(exc: Exception) -> bool:
    """Recognize remote failures without treating arbitrary error text as authority."""
    return _is_retryable_error(exc, seen=set())


def _is_retryable_error(exc: Exception, *, seen: set[int]) -> bool:
    if id(exc) in seen:
        return False
    seen = seen | {id(exc)}
    status = _http_status(exc)
    if status is not None:
        return status in _RETRYABLE_HTTP_STATUSES

    # These subclasses can otherwise resemble a ConnectionError or IncompleteRead.
    if isinstance(exc, (
        requests_errors.SSLError, requests_errors.ProxyError,
        requests_errors.ConnectTimeout, urllib3_errors.SSLError,
        urllib3_errors.ProxyError, urllib3_errors.ConnectTimeoutError,
        urllib3_errors.InvalidChunkLength, urllib3_errors.ResponseNotChunked,
    )):
        return False
    if isinstance(exc, (
        requests_errors.ReadTimeout, urllib3_errors.ReadTimeoutError,
        IncompleteRead, RemoteDisconnected, ConnectionResetError,
    )):
        return True

    if isinstance(exc, (
        requests_errors.ConnectionError, requests_errors.ChunkedEncodingError,
        urllib3_errors.ProtocolError,
    )):
        # urllib3 raises this exact ProtocolError from a benign ValueError when
        # the server closes before the next chunk header; other parser errors stop.
        if type(exc) is urllib3_errors.ProtocolError and exc.args == ("Response ended prematurely",):
            context = exc.__cause__ or exc.__context__
            return context is None or (
                type(context) is ValueError
                and context.args == ("invalid literal for int() with base 16: b''",)
            )
        nested = [arg for arg in exc.args if isinstance(arg, Exception)]
        context = exc.__cause__ or exc.__context__
        if isinstance(context, Exception) and all(context is not item for item in nested):
            nested.append(context)
        return bool(nested) and all(_is_retryable_error(item, seen=seen) for item in nested)

    # Importing the SDK also initializes its live client. Recognize its lightweight
    # error class without importing that unrelated runtime here.
    if any(
        cls.__module__ == "databento.common.error" and cls.__name__ == "BentoError"
        for cls in type(exc).__mro__
    ):
        context = exc.__cause__ or exc.__context__
        if isinstance(context, Exception):
            # SDK stream failures retain context even when raised `from None`.
            # writer.write errors must never fall through to message matching.
            return _is_retryable_error(context, seen=seen)
        message = str(exc)
        prefix = "Error streaming response: "
        if message.startswith(prefix):
            message = message[len(prefix):]
        return message == "Response ended prematurely" or bool(_LEGACY_READ_TIMEOUT.fullmatch(message))
    return False


def _retry_after_seconds(exc: Exception, *, now: float | None = None) -> float:
    """Return a valid Retry-After interval without imposing a client-side cap."""
    response = getattr(exc, "response", None)
    for headers in (getattr(exc, "headers", None), getattr(response, "headers", None)):
        if not isinstance(headers, Mapping):
            continue
        value = next((value for key, value in headers.items() if str(key).lower() == "retry-after"), None)
        if value is None:
            continue
        text = str(value).strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
            seconds = float(text)
            if math.isfinite(seconds):
                return seconds
            continue
        try:
            retry_at = parsedate_to_datetime(text)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = retry_at.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if math.isfinite(seconds):
            return max(0.0, seconds)
    return 0.0


def _http_status(exc: Exception) -> int | None:
    for attribute in ("status_code", "status", "http_status", "http_status_code"):
        parsed = _integer(getattr(exc, attribute, None))
        if parsed is not None:
            return parsed
    response = getattr(exc, "response", None)
    if response is not None:
        return _integer(getattr(response, "status_code", None))
    return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{3}", value):
        return int(value)
    return None
