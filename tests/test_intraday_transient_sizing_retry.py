"""A stale quote must not consume a still-valid, unsized own forecast.

Exercise production manual dispatch with temporary storage and inert workers,
provider and broker objects. No transport, model or actual trader is started.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ml.intraday import live_session
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import digest, encoded, instant
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_store import forecast


def unavailable(run, value, executor, reason="quote_stale_or_future"):
    sizing = dict(forecast_id=value["forecast_id"], symbol=value["symbol"],
        side="BUY", status="unavailable", desired_quantity=None, reason=reason,
        calculated_at=run.now.isoformat(), target_open=value["target_open"],
        target_close=value["target_close"],
        calculated_after_target=run.now >= instant(value["target_close"]))
    result = executor.intents.save_sizing(value, SizingResult(digest(sizing), encoded(sizing)))
    assert executor.intents.get(value["forecast_id"]) is None
    assert result["desired_quantity"] is result["adjusted_quantity"] is None
    return result


def sized(run, value, executor):
    payload = dict(forecast_id=value["forecast_id"], symbol=value["symbol"],
        side="BUY", status="sized", desired_quantity=4,
        calculated_at=run.now.isoformat())
    run.sizing = executor.intents.save_sizing(value, SizingResult(digest(payload), encoded(payload)))
    return dict(kind="execution_disposition", disposition="no_purchasing_capacity",
        evidence={"fixture": "valid_size_without_order"})


def manual_dispatch(tmp_path, monkeypatch, step, *, started="2026-10-08T17:32:00Z",
                    stop_after=7, on_tick=None):
    import dotenv
    from app.services import databento_market_data, schwab
    from ml.intraday import account_execution, data, local_execution

    run = SimpleNamespace(now=instant(started), started=instant(started), calls=[],
                          forecasts={}, queue=None, store=None, sizing=None)
    symbols = ("AAPL", "AMZN")
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", symbols))
    monkeypatch.setattr(local_execution, "load_local_binding",
                        lambda *args: SimpleNamespace(actor="Atlas", symbols=symbols))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(data, "AcquisitionJournal", lambda *args: object())
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider",
                        lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: SimpleNamespace(offline_fixture=True))
    monkeypatch.setattr(live_session, "ShadowRunner", lambda **kwargs: SimpleNamespace(**kwargs))

    class ForecastOnlyThread:
        def __init__(self, *, target, name, **kwargs):
            self.target, self.name = target, name

        def start(self):
            if self.name == "intraday-manual-forecast":
                self.target()

        def is_alive(self):
            return False

        def join(self, **kwargs):
            pass

    monkeypatch.setattr(live_session.threading, "Thread", ForecastOnlyThread)

    def publish(runner, **kwargs):
        run.queue, run.store = kwargs["queue"], runner.store
        for symbol in symbols:
            value = runner.store.save_forecast(forecast(mode="live", symbol=symbol,
                issued_at=run.now.isoformat()))
            run.forecasts[symbol] = value
            run.queue.enqueue(value)

    monkeypatch.setattr(live_session, "forecasting_loop", publish)

    class Executor:
        def __init__(self, **kwargs):
            self.intents = IntentionStore(kwargs["store"])
            run.intents = self.intents

        def step(self, value):
            run.calls.append(("step", value["forecast_id"], (run.now-run.started).total_seconds()))
            if value["symbol"] != "AAPL":
                return dict(kind="execution_disposition", disposition="no_purchasing_capacity",
                            evidence={"fixture": "other_symbol_without_order"})
            return step(run, value, self)

        def recover(self):
            run.calls.append(("recover", None, (run.now-run.started).total_seconds()))

    monkeypatch.setattr(account_execution, "IntradayAccountExecutor", Executor)

    def sleep(seconds):
        if (run.now-run.started).total_seconds() >= stop_after:
            raise KeyboardInterrupt()
        run.now += timedelta(seconds=seconds)
        if on_tick is not None:
            on_tick(run)

    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path/"profile.json", watchlist=tmp_path/"watchlist.txt",
        config=live_session.ResearchConfig(), clock=lambda: run.now, sleep=sleep)
    assert result["status"] == "STOPPED" and result["stop_reason"] == "manual_stop"
    run.result = result
    with run.store.connect() as db:
        run.rows = {identity: (status, next_at) for identity, status, next_at in db.execute(
            "SELECT forecast_id,status,next_attempt_at FROM intraday_live_queue")}
        assert db.execute("SELECT COUNT(*) FROM intraday_live_retry WHERE scope='broker'").fetchone()[0] == 0
    for value in run.forecasts.values():
        assert run.store.forecast(value["cycle_id"], value["symbol"]) == value
    return run


def test_stale_quote_retries_original_forecast_without_holding_other_symbol_or_recovery(tmp_path, monkeypatch):
    def step(run, value, executor):
        prior = executor.intents.history(value["forecast_id"])
        if not prior:
            return unavailable(run, value, executor)
        assert len(prior) == 1 and prior[0]["disposition"] == "quote_stale_or_future"
        return sized(run, value, executor)

    run = manual_dispatch(tmp_path, monkeypatch, step)
    original, other = run.forecasts["AAPL"], run.forecasts["AMZN"]
    assert [(kind, at) for kind, identity, at in run.calls if identity == original["forecast_id"]] == [
        ("step", 0), ("step", 3)]
    assert ("step", other["forecast_id"], 0) in run.calls
    assert ("recover", None, 0) in run.calls
    assert run.rows[original["forecast_id"]][0] == "no_purchasing_capacity"
    history = run.intents.history(original["forecast_id"])
    assert [entry["kind"] for entry in history] == ["sizing_unavailable", "sized"]
    assert history[0]["evidence"]["sizing"]["calculated_at"] == run.started.isoformat()
    assert run.intents.get(original["forecast_id"]) == run.sizing
    assert run.sizing["forecast_id"] == original["forecast_id"]
    assert run.sizing["sizing"]["calculated_at"] == (run.started+timedelta(seconds=3)).isoformat()


@pytest.mark.parametrize("reason", ["neutral_probability_no_direction", "symbol_sizing_policy_missing",
    "original_completed_reference_unavailable", "sizing_feature_unavailable: atr14.relative",
    "quote_observation_after_availability", "future_unrecognized_reason"])
def test_other_unavailable_reasons_remain_terminal(tmp_path, monkeypatch, reason):
    run = manual_dispatch(tmp_path, monkeypatch,
                          lambda run, value, executor: unavailable(run, value, executor, reason))
    original = run.forecasts["AAPL"]
    assert [at for kind, identity, at in run.calls if identity == original["forecast_id"]] == [0]
    assert run.rows[original["forecast_id"]][0] == "sizing_unavailable"
    assert run.intents.get(original["forecast_id"]) is None


def test_target_elapsed_during_unavailable_result_is_not_deferred(tmp_path, monkeypatch):
    def step(run, value, executor):
        run.now = instant(value["target_close"])
        return unavailable(run, value, executor)

    run = manual_dispatch(tmp_path, monkeypatch, step, started="2026-10-08T17:44:58Z", stop_after=8)
    original = run.forecasts["AAPL"]
    assert [at for kind, identity, at in run.calls if identity == original["forecast_id"]] == [0]
    assert run.rows[original["forecast_id"]][0] == "sizing_unavailable"
    assert run.rows[original["forecast_id"]][1] == run.started.isoformat()


def test_retry_crossing_target_close_keeps_expiry_disposition_and_no_sizing(tmp_path, monkeypatch):
    def step(run, value, executor):
        if run.now >= instant(value["target_close"]):
            return dict(kind="execution_disposition", disposition="fixed_target_already_elapsed",
                        evidence={"fixture": "existing_executor_expiry_path_without_order"})
        return unavailable(run, value, executor)

    run = manual_dispatch(tmp_path, monkeypatch, step, started="2026-10-08T17:44:58Z")
    original = run.forecasts["AAPL"]
    assert [at for kind, identity, at in run.calls if identity == original["forecast_id"]] == [0, 3]
    assert run.rows[original["forecast_id"]][0] == "fixed_target_already_elapsed"
    assert run.intents.get(original["forecast_id"]) is None
    assert len(run.intents.history(original["forecast_id"])) == 1


def test_newer_forecast_supersedes_stale_quote_retry_without_rewriting_original(tmp_path, monkeypatch):
    newer = []

    def on_tick(run):
        if newer:
            return
        saved = run.store.save_forecast(forecast(mode="live", cycle_id="newer", issued_at=run.now.isoformat(),
            reference_at="2026-10-08T17:15:00Z", target_open="2026-10-08T17:45:00Z",
            target_close="2026-10-08T18:00:00Z", information_available_at="2026-10-08T17:30:01Z",
            data_through="2026-10-08T17:30:00Z"))
        newer.append(saved)
        run.queue.enqueue(saved)

    def step(run, value, executor):
        if newer and value["forecast_id"] == newer[0]["forecast_id"]:
            return sized(run, value, executor)
        return unavailable(run, value, executor)

    run = manual_dispatch(tmp_path, monkeypatch, step, on_tick=on_tick)
    original = run.forecasts["AAPL"]
    assert [at for kind, identity, at in run.calls if identity == original["forecast_id"]] == [0]
    assert [at for kind, identity, at in run.calls if identity == newer[0]["forecast_id"]] == [1]
    assert run.rows[original["forecast_id"]][0] == "pending"
    assert run.rows[newer[0]["forecast_id"]][0] == "no_purchasing_capacity"
    assert run.queue.due(run.now+timedelta(seconds=30)) == []
    assert run.intents.get(original["forecast_id"]) is None


def test_manual_stop_retains_exact_deferred_identity_without_adopting_another_session(tmp_path, monkeypatch):
    run = manual_dispatch(tmp_path, monkeypatch, unavailable, stop_after=0)
    original = run.forecasts["AAPL"]
    restored_store = ShadowStore(tmp_path/"intraday/shadow.sqlite3")
    same = live_session.ExecutionQueue(restored_store, run.result["session_id"])
    assert same.due(run.started+timedelta(seconds=2)) == []
    assert same.due(run.started+timedelta(seconds=3)) == [original]
    assert live_session.ExecutionQueue(restored_store, "other-manual-session").due(
        run.started+timedelta(seconds=3)) == []
    assert run.rows[original["forecast_id"]][0] == "pending"
    assert run.intents.get(original["forecast_id"]) is None
