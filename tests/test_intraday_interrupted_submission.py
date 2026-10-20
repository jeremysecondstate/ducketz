"""Interrupted fake POSTs retain diagnostics and conservative own commitments."""
import json
import sqlite3

import pytest

from test_intraday_account_execution import env, account, native, advance
from ml.stock_trader.horizon_broker import OrderHistoryError


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_interrupted_post_preserves_cause_stop_and_unknown_commitment(env, failure_type):
    forecast = env.forecast()
    original = failure_type("private-interruption-fixture-key-and-account")
    posts = []

    def interrupted(payload, context, *, before_post):
        before_post()
        posts.append(payload)
        raise original

    env.broker.submit_prepared_order = interrupted
    with pytest.raises(failure_type) as caught:
        env.executor.step(forecast)
    assert caught.value is original
    assert len(posts) == 1
    own = account(env).read_state()["reservations"][0]
    retained = native(env).snapshot().reservations[0]
    assert own["status"] == "UNKNOWN" and own["broker_order_id"] is None
    assert retained.status == "RESERVED" and retained.broker_order_id is None
    assert account(env).submission_receipt(own["reservation_id"]) is None

    with sqlite3.connect(env.store.path) as db:
        rows = db.execute("SELECT cycle_id,symbol,payload FROM events WHERE kind='submission_interrupted'").fetchall()
    assert len(rows) == 1
    cycle, symbol, raw = rows[0]
    proof = json.loads(raw)
    assert cycle == forecast["cycle_id"] and symbol == forecast["symbol"]
    assert proof["forecast_id"] == forecast["forecast_id"]
    assert proof["account_reservation_id"] == own["reservation_id"]
    assert proof["native_reservation_id"] == retained.reservation_id
    assert proof["acceptance_requires_exact_evidence"] is True
    detail = proof["diagnostics"]
    assert detail["submission_stage"] == "broker_submission"
    assert detail["root_cause"]["error_type"] == failure_type.__name__
    assert any(frame["function"] == "interrupted" for location in detail["exception_locations"] for frame in location["frames"])
    assert "private-interruption" not in raw
    advance(env)
    assert env.executor.step(forecast)["disposition"] == "recover_existing_intention"
    assert account(env).read_state()["reservations"][0]["status"] == "UNKNOWN"
    assert native(env).snapshot().reservations[0].status == "RESERVED"
    assert len(posts) == 1 and env.broker.cancellations == []


def test_diagnostic_write_failure_cannot_consume_manual_stop(env, monkeypatch):
    forecast = env.forecast()
    original = KeyboardInterrupt()

    def interrupted(payload, context, *, before_post):
        before_post()
        raise original

    def unavailable(*args, **kwargs):
        raise OSError("fixture diagnostic sink unavailable")

    env.broker.submit_prepared_order = interrupted
    monkeypatch.setattr(env.store, "event", unavailable)
    with pytest.raises(KeyboardInterrupt) as caught:
        env.executor.step(forecast)
    assert caught.value is original
    assert account(env).read_state()["reservations"][0]["status"] == "UNKNOWN"
    assert native(env).snapshot().reservations[0].status == "RESERVED"
