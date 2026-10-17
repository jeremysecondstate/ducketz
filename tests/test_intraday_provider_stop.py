"""Manual feed stops preserve received evidence and prevent another provider call."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from requests.exceptions import ReadTimeout

from ml.intraday import data
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.prediction import STEP, instant
from test_intraday_minute_feed import setup


REFERENCE = instant("2026-10-14T17:00Z")
CALLS = ("native_specs", "dataset_range", "available_range_for_schema", "fetch_native_bars_range")


class Provider:
    dataset = "EQUS.MINI"

    def __init__(self, *, stop_after=None, interrupt_at=None, error=None):
        self.calls = []
        self.stop_after = stop_after
        self.interrupt_at = interrupt_at
        self.error = error
        self.stop_requested = False
        self.interruption = InterruptedError("manual provider interruption")
        self.spec = SimpleNamespace(schema="ohlcv-1m", frequency="1m")

    def stopped(self):
        return self.stop_requested

    def _called(self, name):
        self.calls.append(name)
        if name == self.stop_after:
            self.stop_requested = True
        if name == self.interrupt_at:
            raise self.interruption

    def native_specs(self):
        self._called("native_specs")
        return (self.spec,)

    def dataset_range(self):
        self._called("dataset_range")
        return {"existing": "metadata"}

    def available_range_for_schema(self, schema, *, dataset_range):
        self._called("available_range_for_schema")
        assert schema == "ohlcv-1m" and dataset_range == {"existing": "metadata"}
        return SimpleNamespace(start=REFERENCE, end=REFERENCE + STEP)

    def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range):
        self._called("fetch_native_bars_range")
        assert spec is self.spec
        assert start == available_range.start == REFERENCE
        assert end == available_range.end == REFERENCE + STEP
        if self.error is not None:
            raise self.error
        bars = tuple(SimpleNamespace(timestamp=REFERENCE + timedelta(minutes=n),
            open=10, high=11, low=9, close=10, volume=100) for n in range(15))
        return {symbol: (bars, None) for symbol in symbols}, available_range


def fetch(provider, mode, *, stopped=None):
    arguments = dict(opened_at=REFERENCE, attempt_id="stop-boundary",
        clock=lambda: REFERENCE + STEP + timedelta(seconds=10),
        stopped=provider.stopped if stopped is None else stopped)
    if mode == "single":
        return {"ABCL": data.fetch_databento_interval_attempt(provider, symbol="ABCL", **arguments)}
    return data.fetch_databento_interval_attempts(provider, symbols=("ABCL", "DBX"), **arguments)


@pytest.mark.parametrize("mode", ["single", "batch"])
@pytest.mark.parametrize("next_call", range(len(CALLS)), ids=CALLS)
def test_stop_prevents_each_next_provider_call(mode, next_call):
    provider = Provider(stop_after=CALLS[next_call - 1] if next_call else None)
    provider.stop_requested = next_call == 0
    with pytest.raises(InterruptedError):
        fetch(provider, mode)
    assert provider.calls == list(CALLS[:next_call])


@pytest.mark.parametrize("mode", ["single", "batch"])
@pytest.mark.parametrize("call", CALLS)
def test_provider_interruption_propagates_without_becoming_error_response(mode, call):
    provider = Provider(interrupt_at=call)
    with pytest.raises(InterruptedError) as caught:
        fetch(provider, mode)
    assert caught.value is provider.interruption
    assert provider.calls == list(CALLS[:CALLS.index(call) + 1])


@pytest.mark.parametrize("mode", ["single", "batch"])
@pytest.mark.parametrize("status", ["success", "temporary_error"])
def test_stop_during_last_call_retains_received_rows_or_provider_error(mode, status):
    provider = Provider(stop_after=CALLS[-1],
        error=ReadTimeout("received provider timeout") if status == "temporary_error" else None)
    responses = fetch(provider, mode)
    assert set(responses) == ({"ABCL"} if mode == "single" else {"ABCL", "DBX"})
    assert provider.stop_requested and provider.calls == list(CALLS)
    for symbol, response in responses.items():
        assert response.symbol == symbol and response.status == status
        assert instant(response.interval_open) == REFERENCE
        assert instant(response.interval_close) == REFERENCE + STEP
        assert instant(response.coverage_open) == REFERENCE
        assert instant(response.coverage_close) == REFERENCE + STEP
        if status == "success":
            assert len(response.rows) == 15
            assert response.rows[0]["opened_at"] == REFERENCE.isoformat()
            assert response.rows[-1]["opened_at"] == (REFERENCE + timedelta(minutes=14)).isoformat()
            # Genuine rows survive a stop, but this bare fake provider has no
            # persisted complete source receipt that could prove an omission.
            assert response.provider_no_trade_evidence is None and response.error is None
        else:
            assert not response.rows and response.error == "ReadTimeout"


def feed_with_provider(tmp_path, monkeypatch, provider):
    original_fetch = data.fetch_databento_interval_attempts
    feed, arguments, clock, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(data, "fetch_databento_interval_attempts", original_fetch)
    feed.provider = arguments["provider"] = provider
    return feed, arguments, clock


def event_kinds(feed):
    with feed.store.connect() as db:
        return [row[0] for row in db.execute(
            "SELECT kind FROM events WHERE cycle_id=? ORDER BY id", (feed.identity,))]


def assert_request_lock_released(feed):
    lock = data._provider_request_lock(feed.journal)
    assert lock.acquire(blocking=False), "manual interruption retained provider request lock"
    lock.release()


def test_stopped_step_does_not_queue_or_fetch(tmp_path, monkeypatch):
    provider = Provider()
    feed, _, _ = feed_with_provider(tmp_path, monkeypatch, provider)
    before = event_kinds(feed)
    with pytest.raises(InterruptedError):
        feed.step(stopped=lambda: True)
    assert not provider.calls and not feed.pending
    assert event_kinds(feed) == before
    assert_request_lock_released(feed)


@pytest.mark.parametrize("stop_after", CALLS[:-1])
def test_run_passes_manual_stop_to_each_provider_boundary_and_retains_pending(
        tmp_path, monkeypatch, stop_after):
    provider = Provider(stop_after=stop_after)
    feed, arguments, _ = feed_with_provider(tmp_path, monkeypatch, provider)
    with pytest.raises(InterruptedError):
        feed.run(stopped=provider.stopped, pause=lambda _: pytest.fail("no pause after manual stop"))
    assert provider.calls == list(CALLS[:CALLS.index(stop_after) + 1])
    assert feed.pending[REFERENCE] == set(feed.symbols)
    assert not feed.outcomes and not feed._request_error_due
    assert "minute_feed_request_error" not in event_kinds(feed)
    assert "minute_feed_batch_saved" not in event_kinds(feed)
    for symbol in feed.symbols:
        assert not feed.journal.responses(**feed._slot(symbol, REFERENCE))
    assert_request_lock_released(feed)
    restored = QuarterHourMinuteFeed(**arguments)
    assert restored.pending[REFERENCE] == set(feed.symbols)
    assert not restored.closed


def test_feed_does_not_convert_provider_interruption_to_a_retry(tmp_path, monkeypatch):
    provider = Provider(interrupt_at=CALLS[-1])
    feed, _, _ = feed_with_provider(tmp_path, monkeypatch, provider)
    with pytest.raises(InterruptedError) as caught:
        feed.step(stopped=provider.stopped)
    assert caught.value is provider.interruption
    assert provider.calls == list(CALLS)
    assert feed.pending[REFERENCE] == set(feed.symbols)
    assert not feed._request_error_due
    assert "minute_feed_request_error" not in event_kinds(feed)
    assert_request_lock_released(feed)


@pytest.mark.parametrize("status", ["success", "temporary_error"])
def test_run_saves_received_response_then_stops_without_pause_or_another_call(
        tmp_path, monkeypatch, status):
    provider = Provider(stop_after=CALLS[-1],
        error=ReadTimeout("received provider timeout") if status == "temporary_error" else None)
    feed, arguments, _ = feed_with_provider(tmp_path, monkeypatch, provider)
    result = feed.run(stopped=provider.stopped,
        pause=lambda _: pytest.fail("no pause after received response with manual stop"))
    assert result["status"] == "manual_stop" and result["pending_windows_retained"]
    assert provider.calls == list(CALLS)
    assert "minute_feed_batch_saved" in event_kinds(feed)
    assert "minute_feed_request_error" not in event_kinds(feed)
    assert_request_lock_released(feed)
    restored = QuarterHourMinuteFeed(**arguments)
    for symbol in feed.symbols:
        attempts = restored._attempts(symbol, REFERENCE)
        assert len(attempts) == 1 and attempts[0].status == status
        if status == "success":
            assert len(attempts[0].rows) == 15
            assert len(restored.store.observed_candles(symbol)) == 1
            assert symbol not in restored.pending[REFERENCE]
        else:
            assert not attempts[0].rows and attempts[0].error == "ReadTimeout"
            assert not restored.store.observed_candles(symbol)
            assert symbol in restored.pending[REFERENCE]
