"""Pending eligibility reads indexed presence without loading fitted models."""
from contextlib import contextmanager
import json
import sqlite3

import pytest

from ml.intraday import live_session
from ml.intraday.prediction import ContractError
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


def cycle(store, identity, *, symbols, session="manual", status="running",
          reference="2026-10-12T17:00:00Z", recovery="2026-10-12T17:16:00Z", failures=None):
    store.begin_cycle(identity, {"pc": "Atlas", "symbols": symbols,
        "reference_at": reference, "mode": "live", "experiment": session,
        "configuration_hash": "fixture", "started_at": "2026-10-12T17:15:30Z",
        "status": status, "last_recovery_attempt_at": recovery, "failures": failures or {}})


@contextmanager
def deny_forecast_payload(store, monkeypatch):
    original = store.connect
    denied = []

    @contextmanager
    def connect():
        with original() as db:
            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_READ and table == "forecasts" and column == "payload":
                    denied.append((table, column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", connect)
        yield denied


def test_pending_selector_never_reads_models_and_preserves_due_scope_and_order(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "pending.sqlite3")
    due = {"retryable": True, "retry_at": "2026-10-12T17:17:00Z"}
    future = {"retryable": True, "retry_at": "2026-10-12T19:00:00Z"}
    permanent = {"retryable": False}
    cycle(store, "first", symbols=["CROX", "AAPL", "TWST", "MU", "GOOG", "NVDA"],
        failures={"CROX": future, "TWST": due, "MU": future, "GOOG": permanent})
    original = store.save_forecast(forecast(cycle_id="first"))
    store.event("first", "symbol_failure", due, symbol="CROX")
    store.event("first", "symbol_failure", permanent, symbol="TWST")
    cycle(store, "second", symbols=["PATH"], status="partial_failure",
        reference="2026-10-12T16:45:00Z", recovery="2026-10-12T17:18:00Z", failures={"PATH": due})
    cycle(store, "foreign", symbols=["SNDK"], session="another-manual-start")
    cycle(store, "completed", symbols=["COST"], status="completed")
    # A large malformed model tree remains present, never mistaken for missing
    # or decoded merely to choose the next scheduling operation.
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", ("invalid-model:" + "x" * 1_000_000,
                                                               original["forecast_id"]))
    with deny_forecast_payload(store, monkeypatch) as denied:
        pending = live_session._pending_cycle_passes(store, "manual", "2026-10-12T18:00:00Z")
        assert [(identity, symbols) for identity, _, symbols in pending] == [
            ("first", ["CROX", "NVDA"]), ("second", ["PATH"])]
        assert denied == []
        assert store.forecast_symbols("missing-cycle") == set()


def test_forecast_presence_sees_newly_saved_outputs_without_cache(tmp_path):
    store = ShadowStore(tmp_path / "presence.sqlite3")
    cycle(store, "cycle", symbols=["AAPL", "CROX"])
    assert store.forecast_symbols("cycle") == set()
    first = store.save_forecast(forecast())
    assert store.forecast_symbols("cycle") == {"AAPL"}
    assert live_session._pending_cycle_passes(store, "manual", "2026-10-12T18:00:00Z")[0][2] == ["CROX"]
    store.save_forecast(forecast(symbol="CROX"))
    assert store.forecast_symbols("cycle") == {"AAPL", "CROX"}
    assert live_session._pending_cycle_passes(store, "manual", "2026-10-12T18:00:00Z") == []
    assert store.forecast("cycle", "AAPL") == first
    assert store.forecast_symbols("other-cycle") == set()


def test_presence_does_not_replace_forecast_integrity_at_consumption(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "integrity.sqlite3")
    original = store.save_forecast(forecast())
    with deny_forecast_payload(store, monkeypatch) as denied:
        assert store.forecast_symbols("cycle") == {"AAPL"}
        with pytest.raises(sqlite3.DatabaseError, match="payload"):
            store.forecast("cycle", "AAPL")
        assert denied == [("forecasts", "payload")]
    changed = dict(original, reference_open="999")
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?",
                   (json.dumps(changed), original["forecast_id"]))
    assert store.forecast_symbols("cycle") == {"AAPL"}
    with pytest.raises(ContractError, match="forecast evidence hash or indexed identity"):
        store.forecast("cycle", "AAPL")
