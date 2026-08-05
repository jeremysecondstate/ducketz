from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

_T = TypeVar("_T")

DATABENTO_RETRY_DELAY_SECONDS = 4.0
DATABENTO_RETRY_MAX_ATTEMPTS = 75
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
_RETRYABLE_MESSAGE_MARKERS = (
    "gateway timed out",
    "gateway timeout",
    "temporarily unavailable",
    "service unavailable",
    "too many requests",
)


def call_with_persistent_databento_retry(
    operation: Callable[[], _T],
    *,
    operation_name: str,
    delay_seconds: float = DATABENTO_RETRY_DELAY_SECONDS,
    max_attempts: int = DATABENTO_RETRY_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    reporter: Callable[[str], None] | None = print,
) -> _T:
    """Retry a transient Databento request at a fixed interval.

    The default policy makes up to 75 attempts, four seconds apart: just under five
    minutes of persistence for one request. The final provider exception is then
    raised so the existing error Parquet is preserved and the next orchestration
    cycle can try again. Authentication, entitlement, malformed-request, and
    validation failures are raised immediately. ``KeyboardInterrupt`` is intentionally
    not caught, so Ctrl+C always stops the process.
    """
    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 4A: DATABENTO RETRY")

    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 4B: DATABENTO RETRY")
    for attempt in range(1, max_attempts + 1):
        try:
            result = operation()
            if attempt > 1 and reporter is not None:
                reporter(
                    f"[Databento] {operation_name} succeeded on attempt {attempt}."
                )
            return result
        except Exception as exc:
            if not is_retryable_databento_error(exc) or attempt >= max_attempts:
                raise
            if reporter is not None:
                reporter(
                    f"[Databento] {operation_name} attempt {attempt}/{max_attempts} "
                    f"failed with a transient {type(exc).__name__}: {exc}. Retrying "
                    f"in {delay_seconds:.1f}s; press Ctrl+C to stop."
                )
            sleep(delay_seconds)

    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 4C: DATABENTO RETRY")
    raise AssertionError("Databento retry loop exited unexpectedly")


def is_retryable_databento_error(exc: Exception) -> bool:
    status = _http_status(exc)
    if status in _RETRYABLE_HTTP_STATUSES:
        return True
    message = str(exc).strip().lower()
    if any(marker in message for marker in _RETRYABLE_MESSAGE_MARKERS):
        return True
    return any(str(status_code) in message for status_code in _RETRYABLE_HTTP_STATUSES)


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
