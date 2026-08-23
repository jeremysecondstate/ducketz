from __future__ import annotations

from app.services.databento_retry import call_with_persistent_databento_retry


def test_premature_stream_end_is_retried() -> None:
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Error streaming response: Response ended prematurely")
        return "ok"

    result = call_with_persistent_databento_retry(
        operation,
        operation_name="bounded equities tail",
        delay_seconds=0,
        max_attempts=2,
        sleep=lambda _seconds: None,
        reporter=None,
        timing_reporter=None,
    )

    assert result == "ok"
    assert attempts == 2


def test_provider_read_timeout_is_retried() -> None:
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(
                "HTTPSConnectionPool(host='hist.databento.com', port=443): "
                "Read timed out. (read timeout=100)"
            )
        return "ok"

    result = call_with_persistent_databento_retry(
        operation,
        operation_name="CME endpoint discovery",
        delay_seconds=0,
        max_attempts=2,
        sleep=lambda _seconds: None,
        reporter=None,
        timing_reporter=None,
    )

    assert result == "ok"
    assert attempts == 2
