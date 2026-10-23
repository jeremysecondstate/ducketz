"""Truncated safe reads recapture complete snapshots; ambiguous writes stay fatal."""
from http.client import IncompleteRead

import pytest
import requests
from urllib3.exceptions import ProtocolError

from app.services.schwab_retry import (
    call_with_persistent_schwab_retry,
    is_retryable_schwab_error,
)
from ml.stock_trader.runtime import (
    _BrokerStateCaptureFailure,
    _capture_portfolio_state_with_retry,
)


def truncated():
    error = requests.exceptions.ChunkedEncodingError(
        ProtocolError("Connection broken", IncompleteRead(b"0123456789", 1)))
    error.stock_trader_operation = "open_orders"
    return error


class Capture:
    def __init__(self, failures, *, failed_component="open_orders", on_sleep=None):
        self.failures = list(failures)
        self.failed_component = failed_component
        self.on_sleep = on_sleep
        self.elapsed = 0.
        self.calls = 0
        self.reads = []
        self.sleeps = []
        self.portfolio = object()

    def snapshot(self, timestamp):
        self.calls += 1
        for component in ("account", "order_history", "open_orders", "quotes"):
            self.reads.append((self.calls, component))
            if self.failures and component == self.failed_component:
                raise self.failures.pop(0)
        return self.portfolio

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        if self.on_sleep:
            self.on_sleep(self)

    def run(self, *, budget=120., attempts=60):
        return _capture_portfolio_state_with_retry(object(), observed_at="2026-10-22T11:45:44Z",
            parallel=True, retry_delay_seconds=3., maximum_retry_seconds=budget,
            maximum_attempts=attempts, sleep=self.sleep, monotonic=lambda: self.elapsed,
            capture_snapshot=self.snapshot)


@pytest.mark.parametrize("component", ["account", "order_history", "open_orders", "quotes"])
def test_truncated_read_recaptures_complete_snapshot(component):
    capture = Capture([truncated()], failed_component=component)
    portfolio, completed_at, metadata = capture.run()
    assert portfolio is capture.portfolio
    assert capture.calls == 2 and capture.sleeps == [3.]
    assert [name for attempt, name in capture.reads if attempt == 2] == [
        "account", "order_history", "open_orders", "quotes"]
    assert str(completed_at) == "2026-10-22 11:45:47+00:00"
    assert metadata["status"] == "CURRENT_AFTER_RETRY"
    assert metadata["attempts"] == 2 and metadata["transient_failures"] == 1
    assert metadata["last_error_type"] == "ChunkedEncodingError"
    assert metadata["last_error_operation"] == "open_orders"


@pytest.mark.parametrize("budget,attempts,expected_calls", [(120.,3,3),(5.,60,2),(0.,60,1)])
def test_truncated_reads_preserve_attempt_and_time_budgets(budget, attempts, expected_calls):
    errors = [truncated() for _ in range(60)]
    capture = Capture(errors)
    with pytest.raises(_BrokerStateCaptureFailure) as caught:
        capture.run(budget=budget, attempts=attempts)
    assert capture.calls == expected_calls
    assert capture.sleeps == [3.] * (expected_calls - 1)
    assert caught.value.cause is errors[expected_calls - 1]
    assert caught.value.metadata["attempts"] == expected_calls
    assert caught.value.metadata["transient_failures"] == expected_calls


@pytest.mark.parametrize("reason", ["TRADER_INACTIVE_DURING_BROKER_CAPTURE", "BROKER_READ_EXECUTION_WINDOW_CLOSED"])
def test_retry_preserves_callback_manual_stop_and_signal_expiry(reason):
    stop = ValueError(reason)
    capture = Capture([truncated()], on_sleep=lambda current: current.failures.append(stop))
    with pytest.raises(_BrokerStateCaptureFailure) as caught:
        capture.run()
    assert caught.value.cause is stop
    assert capture.calls == 2 and capture.sleeps == [3.]
    assert caught.value.metadata["transient_failures"] == 1


def test_explicit_ambiguous_write_override_stays_fatal():
    error = truncated()
    error.schwab_retry_safe = False
    capture = Capture([error])
    with pytest.raises(_BrokerStateCaptureFailure) as caught:
        capture.run()
    assert caught.value.cause is error
    assert capture.calls == 1 and capture.sleeps == []
    assert caught.value.metadata["transient_failures"] == 0


@pytest.mark.parametrize("error", [ValueError("invalid account identity"),
    requests.exceptions.ContentDecodingError("malformed content"),
    requests.HTTPError("unauthorized", response=type("Response", (), {"status_code":401})())])
def test_nontransient_validation_and_authentication_failures_remain_fatal(error):
    capture = Capture([error])
    with pytest.raises(_BrokerStateCaptureFailure) as caught:
        capture.run()
    assert caught.value.cause is error
    assert capture.calls == 1 and capture.sleeps == []
    assert caught.value.metadata["transient_failures"] == 0


def test_keyboard_interrupt_still_propagates():
    capture = Capture([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        capture.run()
    assert capture.calls == 1 and capture.sleeps == []


def test_shared_request_classifier_and_retry_scope_are_unchanged():
    error = truncated()
    assert is_retryable_schwab_error(error) is False
    calls = []
    sleeps = []
    def request():
        calls.append(True)
        raise error
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        call_with_persistent_schwab_retry(request, operation_name="unchanged scope",
            sleep=sleeps.append, reporter=None)
    assert calls == [True] and sleeps == []
