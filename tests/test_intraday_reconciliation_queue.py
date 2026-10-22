"""Local unknown-order evidence must not starve unrelated own-symbol work."""
from datetime import timedelta
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ml.intraday import live_session
from ml.intraday.prediction import instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


def test_due_preserves_reference_then_symbol_order_instead_of_storage_order(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    queue = live_session.ExecutionQueue(store, "ordering")
    later = {
        "reference_at": "2026-10-08T17:15:00+00:00",
        "target_open": "2026-10-08T17:45:00+00:00",
        "target_close": "2026-10-08T18:00:00+00:00",
        "issued_at": "2026-10-08T17:31:00+00:00",
        "information_available_at": "2026-10-08T17:30:01+00:00",
        "data_through": "2026-10-08T17:30:00+00:00",
    }
    for symbol, changes in (("NVDA", later), ("SNDK", {}), ("AAPL", {})):
        saved = store.save_forecast(forecast(mode="live", symbol=symbol, cycle_id=symbol, **changes))
        queue.enqueue(saved)
    assert [f["symbol"] for f in queue.due("2026-10-08T17:32:00Z")] == ["AAPL", "SNDK", "NVDA"]


def test_due_breaks_equal_reference_symbol_issue_ties_by_forecast_identity(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    queue = live_session.ExecutionQueue(store, "ties")
    saved = [store.save_forecast(forecast(mode="live", cycle_id=cycle)) for cycle in ("tie-a", "tie-b")]
    for value in saved:
        queue.enqueue(value)
    expected = sorted(value["forecast_id"] for value in saved)
    # Arrange a storage order opposite to the required stable identity order.
    with store.connect() as db:
        db.execute("UPDATE forecasts SET rowid=-rowid")
        for index, identity in enumerate(reversed(expected), start=1):
            db.execute("UPDATE forecasts SET rowid=? WHERE id=?", (index, identity))
    assert [f["forecast_id"] for f in queue.due("2026-10-08T17:17:00Z")] == expected


def test_empty_due_does_not_decode_saved_forecasts(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    queue = live_session.ExecutionQueue(store, "empty")
    store.save_forecast(forecast(mode="live"))
    monkeypatch.setattr(store, "forecasts", lambda **kwargs: pytest.fail("No due IDs require forecast payloads"))
    assert queue.due("2026-10-08T17:17:00Z") == []


@pytest.mark.parametrize("mode", ["local_unknown", "explicit_wait", "proven_unsent", "submission_recovery", "exception"])
def test_manual_dispatch_keeps_unknown_local_and_real_broker_wait_global(tmp_path, monkeypatch, mode):
    """Run production orchestration with temporary storage and inert transports.

    The first symbol repeatedly retains an unknown original reservation. The
    later symbol must run in the same pass only when no provider delay exists.
    """
    import dotenv
    import requests
    from app.services import databento_market_data, schwab
    from ml.intraday import data

    now = [instant("2026-10-08T17:17:00Z")]
    started = now[0]
    calls = []
    original = {"reservation_id": "original-unknown", "status": "RESERVED",
                "broker_order_id": None, "quantity": 1, "filled_quantity": 0}
    original_bytes = json.dumps(original, sort_keys=True)
    retained_forecasts = {}
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "12"
    provider_error = requests.HTTPError("offline429", response=response)

    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", ("AAPL", "AMZN")))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: SimpleNamespace(offline_fixture=True))
    class NoFeed:
        history_source = None
        def __init__(self, **kwargs):
            pass
        def run(self, **kwargs):
            pass
    # Historical main and installed local composition have different startup
    # dependencies. Both adapters remain inert at the same execution boundary.
    for name, attributes in (
        ("ml.intraday.runtime_compatibility", {"read_gameplan_runtime_compatibility": lambda *a, **k: {"ready": True}}),
        ("ml.intraday.account_execution", {"execution_failure_evidence": lambda exc: {}}),
        ("ml.intraday.local_execution", {"load_local_binding": lambda root: SimpleNamespace(actor="Atlas", symbols=("AAPL", "AMZN"))}),
        ("ml.intraday.minute_feed", {"QuarterHourMinuteFeed": NoFeed}),
    ):
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)

    class InlineForecastOnly:
        def __init__(self, *, target, name, **kwargs):
            self.target, self.name = target, name
        def start(self):
            if self.name == "intraday-manual-forecast":
                self.target()
        def is_alive(self):
            return False
        def join(self, **kwargs):
            pass

    monkeypatch.setattr(live_session.threading, "Thread", InlineForecastOnly)
    def publish(runner, **kwargs):
        for symbol in ("AAPL", "AMZN"):
            saved = runner.store.save_forecast(forecast(mode="live", symbol=symbol, issued_at=now[0].isoformat()))
            retained_forecasts[symbol] = saved["forecast_id"]
            kwargs["queue"].enqueue(saved)
    monkeypatch.setattr(live_session, "forecasting_loop", publish)

    class Executor:
        def step(self, value):
            symbol = value["symbol"]
            calls.append(("step", symbol, (now[0] - started).total_seconds()))
            if symbol != "AAPL":
                return {"kind": "execution_disposition", "disposition": "no_purchasing_capacity", "evidence": {}}
            evidence = {"reservation_id": original["reservation_id"], "status": "RESERVED",
                        "state": "reconciliation_required", "reason_code": "EXACT_BROKER_ORDER_ID_UNAVAILABLE",
                        "retry_after_seconds": None}
            disposition = "previous_intention_reconciliation"
            if mode == "exception":
                raise provider_error
            if mode != "local_unknown":
                evidence["retry_after_seconds"] = 12
            if mode == "proven_unsent":
                disposition = "not_submitted"
                evidence["proven_unsent_retry_eligible"] = True
            if mode == "submission_recovery":
                disposition = "submission_recovery_required"
            return {"kind": "execution_disposition", "disposition": disposition, "evidence": evidence}
        def recover(self):
            calls.append(("recover", None, (now[0] - started).total_seconds()))

    monkeypatch.setattr(sys.modules["ml.intraday.account_execution"], "IntradayAccountExecutor",
                        lambda **kwargs: Executor(), raising=False)
    def sleep(seconds):
        if (now[0] - started).total_seconds() >= 8:
            raise KeyboardInterrupt()
        now[0] += timedelta(seconds=seconds)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile.json", watchlist=tmp_path / "watchlist.txt",
        config=live_session.ResearchConfig(), clock=lambda: now[0], sleep=sleep)
    assert result["status"] == "STOPPED"
    assert calls[0] == ("step", "AAPL", 0)
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        broker_wait = db.execute("SELECT next_attempt_at FROM intraday_live_retry WHERE scope='broker'").fetchone()
        statuses = dict(db.execute("SELECT symbol,status FROM intraday_live_queue"))
        forecast_ids = dict(db.execute("SELECT symbol,forecast_id FROM intraday_live_queue"))
    assert forecast_ids == retained_forecasts
    assert json.dumps(original, sort_keys=True) == original_bytes
    if mode == "local_unknown":
        assert ("step", "AMZN", 0) in calls
        assert ("recover", None, 0) in calls
        assert broker_wait is None
        assert statuses == {"AAPL": "pending", "AMZN": "no_purchasing_capacity"}
        assert [c[2] for c in calls if c[:2] == ("step", "AAPL")] == [0, 3, 6]
    else:
        assert calls == [("step", "AAPL", 0)]
        assert instant(broker_wait[0]) == started + timedelta(seconds=12)
        assert statuses["AMZN"] == "pending"
        assert statuses["AAPL"] == ("submission_recovery_required" if mode == "submission_recovery" else "pending")


def test_existing_provider_wait_survives_local_defer_and_queue_reopen(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    queue = live_session.ExecutionQueue(store, "retained-wait")
    saved = store.save_forecast(forecast(mode="live"))
    queue.enqueue(saved)
    queue.broker_wait(now="2026-10-08T17:17:00Z", seconds=120)
    queue.defer(saved["forecast_id"], now="2026-10-08T17:17:01Z", seconds=3, broker_wait=False)
    restored = live_session.ExecutionQueue(store, "retained-wait")
    assert restored.due("2026-10-08T17:18:59Z") == []
    assert [f["forecast_id"] for f in restored.due("2026-10-08T17:19:00Z")] == [saved["forecast_id"]]


def test_unknown_original_cancellation_does_not_call_broker_or_change_reservation(monkeypatch):
    # The installed Atlas composition owns this implementation separately from
    # shared main. Exercise it where present without importing another checkout.
    cancellation = pytest.importorskip("ml.intraday.cancellation")
    original = SimpleNamespace(reservation_id="original", status="RESERVED", broker_order_id=None,
                               quantity=1, filled_quantity=0)
    before = vars(original).copy()
    ledger = SimpleNamespace(account_fingerprint="own-account", lookup_reservation=lambda identity: original)
    monkeypatch.setattr(cancellation, "reservation_stream", lambda ledger, identity: "atlas-15m")
    class NoBroker:
        def __getattr__(self, name):
            pytest.fail("Unknown original must not call broker: " + name)
    service = cancellation.CancellationService(broker=NoBroker(), ledger=ledger,
        account_fingerprint="own-account", before_cancel=lambda *a: pytest.fail("cancel"),
        apply_evidence=lambda *a: pytest.fail("mutation"), record_event=lambda *a: pytest.fail("mutation"))
    result = service.step("original", reason="new-forecast", evidence_id="offline", trigger_forecast_id="new")
    assert result.reason_code == "EXACT_BROKER_ORDER_ID_UNAVAILABLE"
    assert result.retry_after_seconds is None
    assert vars(original) == before
