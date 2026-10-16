"""Legacy exact-candle callers honor manual stops without losing received data."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from requests.exceptions import ReadTimeout

from ml.intraday import data, live_session, runtime
from ml.intraday.prediction import STEP, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_data import candles
from tests.test_intraday_provider_stop import CALLS, REFERENCE, Provider
from tests.test_intraday_store import forecast


NOW = REFERENCE + STEP + timedelta(seconds=30)
SOURCE = "databento:EQUS.MINI:ohlcv-1m"


def recovery_arguments(tmp_path):
    journal = data.AcquisitionJournal(tmp_path / "acquisition.sqlite3")
    session = next(item for item in data.equity_sessions(REFERENCE, REFERENCE + STEP)
                   if item.contains(REFERENCE))
    return dict(symbol="AAPL", opened_at=REFERENCE, journal=journal, session=session,
                previous=None, clock=lambda: NOW)


def attempts(journal):
    return journal.responses(symbol="AAPL", source_id=SOURCE,
        interval_open=REFERENCE.isoformat(), interval_close=(REFERENCE + STEP).isoformat())


def assert_request_lock_released(journal):
    lock = data._provider_request_lock(journal)
    assert lock.acquire(blocking=False), "manual interruption retained provider request lock"
    lock.release()


def live_history(tmp_path, provider, *, bounded=True):
    arguments = recovery_arguments(tmp_path)
    frame = candles(1, start=(REFERENCE - STEP).isoformat())
    frame["source_id"] = SOURCE
    frame["interval_seconds"] = 900
    frame["session_id"] = arguments["session"].session_id
    history = data.History(frame, {"source_dataset": provider.dataset})
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    incoming = runtime.LiveHistories(histories={"AAPL": history}, provider=provider,
        store=store, journal=arguments["journal"], reference_at=REFERENCE,
        clock=arguments["clock"], bounded=bounded, stopped=provider.stopped,
        sleep=lambda _: pytest.fail("no retry pause after a manual stop"))
    incoming.bind_cycle("legacy-stop")
    return incoming, store, arguments


@pytest.mark.parametrize("next_call", range(len(CALLS)), ids=CALLS)
def test_recovery_step_passes_stop_to_every_provider_boundary(tmp_path, next_call):
    provider = Provider(stop_after=CALLS[next_call - 1] if next_call else None)
    provider.stop_requested = next_call == 0
    arguments = recovery_arguments(tmp_path)
    with pytest.raises(InterruptedError):
        data.recover_databento_candle_step(provider, stopped=provider.stopped, **arguments)
    assert provider.calls == list(CALLS[:next_call])
    assert not attempts(arguments["journal"])
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("call", CALLS)
def test_recovery_step_propagates_provider_interruption_without_retry_record(tmp_path, call):
    provider = Provider(interrupt_at=call)
    arguments = recovery_arguments(tmp_path)
    with pytest.raises(InterruptedError) as caught:
        data.recover_databento_candle_step(provider, stopped=provider.stopped, **arguments)
    assert caught.value is provider.interruption
    assert provider.calls == list(CALLS[:CALLS.index(call) + 1])
    assert not attempts(arguments["journal"])
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("error", [False, True], ids=["received_rows", "received_timeout"])
def test_recovery_step_saves_final_response_even_when_that_call_requests_stop(tmp_path, error):
    provider = Provider(stop_after=CALLS[-1], error=ReadTimeout("received timeout") if error else None)
    arguments = recovery_arguments(tmp_path)
    result = data.recover_databento_candle_step(provider, stopped=provider.stopped, **arguments)
    saved = attempts(arguments["journal"])
    assert provider.stopped() and provider.calls == list(CALLS)
    assert len(saved) == 1
    assert saved[0].status == ("temporary_error" if error else "success")
    assert len(saved[0].rows) == (0 if error else 15)
    assert result["status"] == ("retry" if error else "observed")
    assert (result["candle"] is None) == error
    assert_request_lock_released(arguments["journal"])


def wrapped_recovery(kind, provider, arguments):
    if kind == "fetch_15m":
        arguments = {**arguments, "reference": arguments["opened_at"]}
        del arguments["opened_at"]
        function = data.fetch_15m
    else:
        function = data.recover_databento_candle
    return function(provider, stopped=provider.stopped,
        sleep=lambda _: pytest.fail("no retry pause after a manual stop"), **arguments)


@pytest.mark.parametrize("kind", ["recover", "fetch_15m"])
def test_recovery_wrappers_stop_before_the_next_provider_request(tmp_path, kind):
    provider = Provider(stop_after="dataset_range")
    arguments = recovery_arguments(tmp_path)
    with pytest.raises(InterruptedError):
        wrapped_recovery(kind, provider, arguments)
    assert provider.calls == list(CALLS[:2])
    assert not attempts(arguments["journal"])
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("kind", ["recover", "fetch_15m"])
def test_recovery_wrappers_save_error_and_stop_before_retry_sleep(tmp_path, kind):
    provider = Provider(stop_after=CALLS[-1], error=ReadTimeout("received timeout"))
    arguments = recovery_arguments(tmp_path)
    with pytest.raises(InterruptedError):
        wrapped_recovery(kind, provider, arguments)
    saved = attempts(arguments["journal"])
    assert provider.calls == list(CALLS)
    assert len(saved) == 1 and saved[0].status == "temporary_error"
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("kind", ["recover", "fetch_15m"])
def test_recovery_wrappers_return_received_success_after_stop(tmp_path, kind):
    provider = Provider(stop_after=CALLS[-1])
    arguments = recovery_arguments(tmp_path)
    result = wrapped_recovery(kind, provider, arguments)
    assert provider.calls == list(CALLS) and len(attempts(arguments["journal"])) == 1
    if kind == "fetch_15m":
        assert len(result) == 1 and instant(result.iloc[0].opened_at) == REFERENCE
    else:
        assert result["status"] == "observed"
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("next_call", range(len(CALLS)), ids=CALLS)
def test_live_history_forwards_stop_without_saving_interrupted_attempt(tmp_path, bounded, next_call):
    provider = Provider(stop_after=CALLS[next_call - 1] if next_call else None)
    incoming, store, arguments = live_history(tmp_path, provider, bounded=bounded)
    provider.stop_requested = next_call == 0
    with pytest.raises(InterruptedError):
        incoming.get("AAPL")
    assert provider.calls == list(CALLS[:next_call])
    assert not attempts(arguments["journal"]) and not store.observed_candles()
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("bounded", [False, True])
def test_live_history_persists_received_candle_before_honoring_next_stop(tmp_path, bounded):
    provider = Provider(stop_after=CALLS[-1])
    incoming, store, arguments = live_history(tmp_path, provider, bounded=bounded)
    result = incoming.get("AAPL")
    assert provider.stopped() and provider.calls == list(CALLS)
    saved = store.observed_candles("AAPL")
    assert len(saved) == 1 and instant(saved[0]["opened_at"]) == REFERENCE
    assert len(result.frame) == 2 and instant(result.frame.iloc[-1].opened_at) == REFERENCE
    assert len(attempts(arguments["journal"])) == 1
    with pytest.raises(InterruptedError):
        incoming.get("AAPL")
    assert provider.calls == list(CALLS) and store.observed_candles("AAPL") == saved
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("bounded", [False, True])
def test_live_history_retains_received_error_and_reports_stop_before_retry(tmp_path, bounded):
    provider = Provider(stop_after=CALLS[-1], error=ReadTimeout("received timeout"))
    incoming, store, arguments = live_history(tmp_path, provider, bounded=bounded)
    with pytest.raises(InterruptedError):
        incoming.get("AAPL")
    saved = attempts(arguments["journal"])
    assert len(saved) == 1 and saved[0].status == "temporary_error"
    assert provider.calls == list(CALLS) and not store.observed_candles()
    assert_request_lock_released(arguments["journal"])


def test_live_history_stops_before_backfilling_another_predecessor(tmp_path, monkeypatch):
    provider = Provider()
    incoming, store, arguments = live_history(tmp_path, provider)
    # Remove the adjacent prior row so the existing covered-omission evidence
    # would normally walk backwards to its missing same-session prerequisite.
    incoming.histories["AAPL"] = data.History(incoming.histories["AAPL"].frame.iloc[:0],
                                              {"source_dataset": provider.dataset})
    reads = []
    def saved_attempts(**slot):
        reads.append(slot)
        provider.stop_requested = True
        return [object()]
    monkeypatch.setattr(arguments["journal"], "responses", saved_attempts)
    monkeypatch.setattr(data, "resolve_minute_candle", lambda *args, **kwargs:
        {"status": "missing", "reason": "no_prior_known_price"})
    with pytest.raises(InterruptedError):
        incoming.get("AAPL")
    assert len(reads) == 1 and not provider.calls and not store.observed_candles()
    assert_request_lock_released(arguments["journal"])


@pytest.mark.parametrize("consumer", ["forecast", "scoring"])
def test_legacy_live_session_fallback_passes_its_stop_to_history_acquisition(tmp_path, consumer):
    provider = Provider(stop_after="native_specs")
    incoming, store, arguments = live_history(tmp_path, provider)
    common = dict(histories=incoming.histories, provider=provider, journal=arguments["journal"],
                  stopped=provider.stopped, clock=lambda: NOW,
                  pause=lambda _: pytest.fail("caller paused after manual stop"))
    if consumer == "forecast":
        def run_cycle(history, **kwargs):
            history.get("AAPL")
        runner = SimpleNamespace(store=store, config=live_session.ResearchConfig(), run_cycle=run_cycle)
        queue = live_session.ExecutionQueue(store, "legacy-stop", pc="Scout")
        with pytest.raises(InterruptedError):
            live_session.forecasting_loop(runner, queue=queue, **common)
    else:
        saved = store.save_forecast(forecast(pc="Scout", mode="live",
            reference_at=(REFERENCE - 2 * STEP).isoformat(), target_open=REFERENCE.isoformat(),
            target_close=(REFERENCE + STEP).isoformat(),
            issued_at=(REFERENCE - STEP + timedelta(seconds=30)).isoformat(),
            information_available_at=(REFERENCE - STEP).isoformat(),
            data_through=(REFERENCE - STEP).isoformat()))
        live_session.scoring_loop(store, session_id="legacy-stop", pc="Scout", **common)
        assert not store.revisions(saved["forecast_id"])
        with store.connect() as connection:
            assert not connection.execute("SELECT 1 FROM events WHERE kind='target_scoring_retry'").fetchall()
    assert provider.calls == ["native_specs"]
    assert not attempts(arguments["journal"]) and not store.observed_candles()
    assert_request_lock_released(arguments["journal"])
