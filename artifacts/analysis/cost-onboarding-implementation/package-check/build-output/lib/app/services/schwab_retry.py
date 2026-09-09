from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TypeVar

import requests
from filelock import Timeout as FileLockTimeout

from datafetching.observability import timed_stage

_T = TypeVar("_T")

SCHWAB_RETRY_DELAY_SECONDS = 5.0
SCHWAB_RETRY_MAX_ATTEMPTS = 60
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_RETRYABLE_MESSAGE_MARKERS = (
    "connection aborted",
    "connection reset",
    "gateway timed out",
    "gateway timeout",
    "internal server error",
    "remote disconnected",
    "service unavailable",
    "temporarily unavailable",
    "timed out",
    "too many requests",
)


def call_with_persistent_schwab_retry(
    operation: Callable[[], _T],
    *,
    operation_name: str,
    delay_seconds: float = SCHWAB_RETRY_DELAY_SECONDS,
    max_attempts: int = SCHWAB_RETRY_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    reporter: Callable[[str], None] | None = print,
    symbol: str | None = None,
    schema: str | None = None,
    timing_reporter: Callable[[str], None] | None = None,
) -> _T:
    """Retry a transient Schwab market-data request at a fixed interval.

    The default policy makes up to 60 attempts, five seconds apart: just under
    five minutes of persistence for one request. The final provider exception is
    then raised so the existing error Parquet is preserved and the next
    orchestration cycle can try again. Authentication, malformed-request, and
    validation failures are raised immediately. ``KeyboardInterrupt`` is
    intentionally not caught, so Ctrl+C always stops the process.
    """

    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            with timed_stage(
                "provider.request",
                symbol=symbol,
                provider="schwab",
                schema=schema,
                attempt=attempt,
                reporter=timing_reporter,
                extra={"operation_name": operation_name},
            ) as timing:
                result = operation()
                row_count = len(result) if hasattr(result, "__len__") else None
                timing.annotate(row_count=row_count, operation="fetched")
            if attempt > 1 and reporter is not None:
                reporter(f"[Schwab] {operation_name} succeeded on attempt {attempt}.")
            return result
        except Exception as exc:
            if not is_retryable_schwab_error(exc) or attempt >= max_attempts:
                raise
            if reporter is not None:
                reporter(
                    f"[Schwab] {operation_name} attempt {attempt}/{max_attempts} "
                    f"failed with a transient {type(exc).__name__}: {exc}. Retrying "
                    f"in {delay_seconds:.1f}s; press Ctrl+C to stop."
                )
            sleep(delay_seconds)

    raise AssertionError("Schwab retry loop exited unexpectedly")


def is_retryable_schwab_error(exc: Exception) -> bool:
    retry_override = getattr(exc, "schwab_retry_safe", None)
    if retry_override is False:
        return False
    if retry_override is True:
        return True
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, FileLockTimeout):
        return True

    status = _http_status(exc)
    if status in _RETRYABLE_HTTP_STATUSES:
        return True

    message = str(exc).strip().lower()
    if any(marker in message for marker in _RETRYABLE_MESSAGE_MARKERS):
        return True
    status_pattern = "|".join(str(code) for code in sorted(_RETRYABLE_HTTP_STATUSES))
    return bool(
        re.search(
            rf"(?:^|\bhttp(?:\s+status)?\s*[:=]?\s*|\bstatus(?:\s+code)?\s*[:=]?\s*)"
            rf"(?:{status_pattern})\b",
            message,
        )
    )


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
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
