"""Existing recovery snapshots and new appends keep distinct source boundaries.

Every value, key, database and native order in these fixtures is synthetic.
The same cases exercise the local complete implementation and common subclass.
"""
from contextlib import contextmanager
from dataclasses import asdict
import socket
import sqlite3

import pytest

from ml.intraday import intentions as base_intentions
from ml.intraday.forecast_header_cache import DurableForecastHeaders
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.sizing_header_cache import DurableSizingHeaders
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from ml.stock_trader.horizon_ledger import ReservationState
from tests.test_intraday_store import forecast

# Local installations retain the complete original implementation. Common
# source keeps the equivalent recovery API in an explicit execution subclass.
if hasattr(base_intentions.IntentionStore, "record_recovery_group"):
    IntentionStore = base_intentions.IntentionStore
else:
    from ml.intraday.execution_intentions import IntentionStore


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Recovery-boundary fixtures prohibit provider/broker calls")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    # Temporary fixture keys retain real authentication without touching the
    # current Windows account's secret-protection service or private stores.
    for helper in (DurableForecastHeaders, DurableSizingHeaders):
        monkeypatch.setattr(helper, "_protect_secret",
                            staticmethod(lambda raw, **kwargs: raw))


def prepared(tmp_path):
    store = ShadowStore(tmp_path / "synthetic-original.sqlite3")
    original = store.save_forecast(forecast())
    intents = IntentionStore(store)
    sizing = dict(forecast_id=original["forecast_id"], symbol="AAPL", side="BUY",
        status="sized", desired_quantity=4, calculated_at="2026-10-08T17:16:03Z",
        research={"models": [{"history": list(range(100))}]})
    intents.save_sizing(original, SizingResult(digest(sizing), encoded(sizing)))
    order = ReservationState("synthetic-order", "synthetic-allocation", "AAPL", "15m",
        original["forecast_id"], "BUY", 1, "50", 0, "UNKNOWN", None,
        "synthetic-key", "synthetic-batch", None)
    return store, original, intents, order


def observation(order, *, key=None, at="2026-10-08T17:17:00Z"):
    kind = "execution_disposition"
    disposition = "unresolved_submission_retained"
    evidence = dict(native_reservation_id=order.reservation_id, status=order.status,
                    commitment_retained=True, complete_broker_order_evidence=False)
    return dict(event_key=key or digest([kind, disposition, evidence, asdict(order), None]),
        recorded_at=at, disposition=disposition, evidence=evidence, kind=kind, order=order)


def project(intents, original, item):
    return intents.record_recovery_group(original["forecast_id"], actor="Atlas",
                                         observations=(item,))


def projection_rows(store):
    with store.connect() as db:
        return (db.execute("SELECT * FROM intraday_intention_events ORDER BY rowid").fetchall(),
                db.execute("SELECT * FROM intents ORDER BY rowid").fetchall())


@pytest.mark.parametrize("damage", ["payload", "actor", "trigger", "missing"])
@pytest.mark.parametrize("case", ["new_event", "noop"])
def test_distinct_recovery_boundary(tmp_path, monkeypatch, damage, case):
    store, original, intents, order = prepared(tmp_path)
    prior = observation(order)
    expected = project(intents, original, prior)
    before = projection_rows(store)
    assert expected[0]["evidence"]["commitment_retained"] is True
    assert expected[0]["evidence"]["complete_broker_order_evidence"] is False
    assert expected[0]["submitted_quantity"] is None
    pending = observation(order, key="synthetic-new-event", at="2026-10-08T17:18:00Z")
    connect = store.connect
    calls = []

    @contextmanager
    def interleaved():
        calls.append(True)
        index = len(calls)
        with connect() as db:
            yield db
        # Commit after the coherent read snapshot ends and before a possible
        # append writer opens; never mutate through the read handle itself.
        if index == 1:
            with sqlite3.connect(store.path, timeout=0) as other:
                if damage == "payload":
                    other.execute("UPDATE forecasts SET payload=json_set(payload,'$.probability_not_down',0.73)")
                elif damage == "actor":
                    other.execute("UPDATE forecasts SET pc='Scout'")
                elif damage == "trigger":
                    other.execute("DROP TRIGGER execution_forecast_update")
                else:
                    other.execute("DELETE FROM forecasts")

    monkeypatch.setattr(store, "connect", interleaved)
    if case == "new_event":
        with pytest.raises(ContractError):
            project(intents, original, pending)
        assert len(calls) == 2
    else:
        # An exact existing projection belongs to its coherent prior snapshot.
        assert project(intents, original, prior) == expected
        assert len(calls) == 1
    monkeypatch.setattr(store, "connect", connect)
    assert projection_rows(store) == before
    # The next read must see every committed corruption even when no append
    # is requested. A failed writer must also leave no partial projection.
    with pytest.raises(ContractError):
        project(intents, original, prior)
    assert projection_rows(store) == before
