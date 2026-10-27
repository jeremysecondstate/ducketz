"""Offline subprocess fixture. Never use operating DATASTORE or network clients."""
from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    # Both the complete input directory and the simulated time are mandatory.
    root, role, now = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
    if not (root / "OFFLINE-RENEWAL-FIXTURE").is_file():
        raise RuntimeError("Explicit isolated fixture marker required")
    def no_network(*args, **kwargs):
        raise AssertionError("Network is forbidden in the renewal fixture")
    socket.socket.connect = no_network
    socket.create_connection = no_network

    import pytest
    from ml.intraday import live_session, runtime, local_execution
    from ml.intraday.prediction import instant
    from ml.intraday.store import ShadowStore
    from ml.trader_renewal import PlannedRenewal, atomic_json
    from app.services import schwab, databento_market_data
    from types import SimpleNamespace
    import dotenv

    monkeypatch = pytest.MonkeyPatch()
    clock = [instant(now)]
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    session_path, cursor_path = root / "session.json", root / f"{role}-cursor.json"
    session = json.loads(session_path.read_text())
    previous = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}

    class Renewal:
        state = previous.get("state", {})
        def __init__(self):
            self.session = session
        def stopped(self):
            return (root / "manual-stop").exists()
        def wait_due(self, **kwargs):
            if previous.get("next_poll_at"):
                clock[0] = max(clock[0], instant(previous["next_poll_at"]))
        def checkpoint(self, state, next_poll_at):
            atomic_json(cursor_path, {"state": state, "next_poll_at": next_poll_at})
            raise PlannedRenewal()
    renewal = Renewal()
    if role == "forecast":
        from test_intraday_runtime import dataset, trained, history
        from ml.intraday import data
        monkeypatch.setattr(data, "load_local_history", lambda _, symbol, **kwargs: history(symbol))
        monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider",
                            lambda: SimpleNamespace(dataset="EQUS.MINI"))
        monkeypatch.setattr(schwab, "SchwabSession", lambda: no_network())
        monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs:
                            {symbol: history(symbol) for symbol in session["symbols"]})
        def train(*args, **kwargs):
            ShadowStore(root / "intraday/shadow.sqlite3").event(session["session_id"],
                "offline_fixture_training", {"pid": os.getpid(), "run_id": kwargs["run_id"]})
            return trained(*args, **kwargs)
        original_runner = runtime.ShadowRunner
        monkeypatch.setattr(live_session, "ShadowRunner", lambda **kwargs:
            original_runner(**kwargs, trainer=train, dataset_builder=dataset))
        binding = local_execution.load_local_binding(root)
    elif role == "execution":
        from test_intraday_process_renewal import env
        from ml.intraday import account_execution
        import pandas as pd
        fixture = env.__wrapped__(root, monkeypatch)
        fixture.store = ShadowStore(root / "intraday/shadow.sqlite3")
        fixture.now = pd.Timestamp(clock[0])
        fixture.session_started_at = instant(session["started_at"])
        broker_path = root / "fixture-broker.json"
        saved = json.loads(broker_path.read_text()) if broker_path.exists() else {}
        fixture.orders = saved.get("orders", {})
        fixture.cash = saved.get("cash", fixture.cash)
        fixture.held = saved.get("held", fixture.held)
        fixture.broker.submissions = saved.get("submissions", [])
        fixture.broker.unknown_submission = bool(session.get("fixture_unknown"))
        clock = None  # The fake broker advances its own observation clock.
        monkeypatch.setattr(schwab, "SchwabSession", lambda: fixture.broker)
        monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", lambda: no_network())
        binding = fixture.config
        monkeypatch.setattr(local_execution, "load_local_binding", lambda *a: binding)
        if not saved:
            value = fixture.forecast()
            fixture.store.update_cycle(value["cycle_id"], {"experiment": session["session_id"]})
            live_session.ExecutionQueue(fixture.store, session["session_id"]).enqueue(value)
        def execution_wait(**kwargs):
            if previous.get("next_poll_at"):
                fixture.now = max(fixture.now, pd.Timestamp(previous["next_poll_at"]))
        renewal.wait_due = execution_wait
    else:
        raise RuntimeError("Unsupported offline fixture role")
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: (session["actor"], tuple(session["symbols"])))
    if binding.fingerprint != session["binding_fingerprint"]:
        raise RuntimeError("Fixture session/binding mismatch")
    try:
        result = live_session.run_renewal_lane(role, repository=root, datastore=root,
            profile=root / "profile.json", watchlist=root / "watchlist.local.txt", renewal=renewal,
            clock=(lambda: clock[0]) if role == "forecast" else fixture.clock,
            sleep=lambda seconds: no_network())
    except PlannedRenewal:
        result = {"status": "PLANNED_RENEWAL"}
    finally:
        if role == "execution":
            atomic_json(broker_path, {"orders": fixture.orders, "cash": fixture.cash, "held": fixture.held,
                                    "submissions": fixture.broker.submissions, "now": fixture.clock()})
    store = ShadowStore(root / "intraday/shadow.sqlite3")
    with store.connect() as db:
        counts = {name: db.execute("SELECT count(*) FROM " + name).fetchone()[0]
                  for name in ("cycles", "forecasts")}
        training = db.execute("SELECT count(*) FROM events WHERE kind='offline_fixture_training'").fetchone()[0]
    atomic_json(root / "turn-result.json", {"pid": os.getpid(), "role": role, "session_id": session["session_id"],
        "started_at": session["started_at"], "close_at": session["close_at"], "training_calls": training,
        "counts": counts, "result": result})


if __name__ == "__main__":
    main()
