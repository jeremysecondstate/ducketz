from __future__ import annotations

import pytest
import requests

from app.services.schwab_retry import (
    SCHWAB_RETRY_DELAY_SECONDS,
    call_with_persistent_schwab_retry,
    is_retryable_schwab_error,
)


class FakeHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_retryable_500_waits_five_seconds_until_success() -> None:
    calls = 0
    sleeps: list[float] = []
    messages: list[str] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise FakeHttpError(500, "Internal Server Error")
        return "fetched"

    result = call_with_persistent_schwab_retry(
        operation,
        operation_name="NVDA pricehistory year_5_monthly_1",
        sleep=sleeps.append,
        reporter=messages.append,
    )

    assert result == "fetched"
    assert calls == 4
    assert sleeps == [
        SCHWAB_RETRY_DELAY_SECONDS,
        SCHWAB_RETRY_DELAY_SECONDS,
        SCHWAB_RETRY_DELAY_SECONDS,
    ]
    assert len(messages) == 4
    assert all("Retrying in 5.0s" in message for message in messages[:3])
    assert messages[-1].endswith("succeeded on attempt 4.")


def test_retryable_failure_is_raised_after_attempt_budget() -> None:
    calls = 0
    sleeps: list[float] = []

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise FakeHttpError(500, "Internal Server Error")

    with pytest.raises(FakeHttpError, match="Internal Server Error"):
        call_with_persistent_schwab_retry(
            operation,
            operation_name="NVDA pricehistory year_5_monthly_1",
            max_attempts=3,
            sleep=sleeps.append,
            reporter=None,
        )

    assert calls == 3
    assert sleeps == [
        SCHWAB_RETRY_DELAY_SECONDS,
        SCHWAB_RETRY_DELAY_SECONDS,
    ]


def test_retryable_statuses_and_transport_failures_are_recognized() -> None:
    for status in (408, 425, 429, 500, 502, 503, 504):
        assert is_retryable_schwab_error(FakeHttpError(status, "server failure"))
    assert is_retryable_schwab_error(RuntimeError("500 Internal Server Error"))
    assert is_retryable_schwab_error(RuntimeError("HTTP status 503"))
    assert is_retryable_schwab_error(requests.Timeout("request timed out"))
    assert is_retryable_schwab_error(requests.ConnectionError("connection reset"))
    ambiguous_refresh = requests.Timeout("token refresh timed out")
    ambiguous_refresh.schwab_retry_safe = False
    assert not is_retryable_schwab_error(ambiguous_refresh)
    assert not is_retryable_schwab_error(
        ValueError("Validation rejected a 5000-share quantity")
    )


def test_non_retryable_failure_is_not_repeated() -> None:
    calls = 0
    sleeps: list[float] = []

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise FakeHttpError(401, "Access token is invalid")

    with pytest.raises(FakeHttpError, match="Access token"):
        call_with_persistent_schwab_retry(
            operation,
            operation_name="authentication check",
            sleep=sleeps.append,
            reporter=None,
        )

    assert calls == 1
    assert sleeps == []


def test_keyboard_interrupt_stops_retry_loop_immediately() -> None:
    with pytest.raises(KeyboardInterrupt):
        call_with_persistent_schwab_retry(
            lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
            operation_name="interruptible request",
            sleep=lambda _: None,
            reporter=None,
        )
