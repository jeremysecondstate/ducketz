"""Offline injected cancellation transport, exact status and stream fences."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from ml.intraday.cancellation import CancellationService, gameplan_allocation, gameplan_reservation, retry_delay
from ml.intraday.prediction import ContractError
from ml.stock_trader.horizon_ledger import FillEvidence, HorizonLedger, OrderEvidence, PortfolioEvidence, ReservationState
from ml.stock_trader import independent_runtime

NOW = "2026-10-12T17:20:00+00:00"
ACCOUNT = "a"*64


def reservation(**changes):
    value = ReservationState("r1", "a1", "AAPL", "15m", "old-forecast", "BUY", 4, "100", 0,
                             "WORKING", "123", "idempotent-1", "batch-1", None)
    return replace(value, **changes)


class Ledger:
    account_fingerprint = ACCOUNT
    def __init__(self, *, stream="atlas-15m", value=None):
        self.current = value or reservation()
        self.stream = stream
        self.markers = []
        self.applied = []
    def reservation_stream(self, _): return self.stream
    def allocation_stream(self, _): return self.stream
    def lookup_reservation(self, _): return self.current
    def reserve_stream_cancellation(self, key, **kwargs):
        if self.current.cancel_requested_at:
            return False
        self.markers.append((key, kwargs))
        self.current = replace(self.current, cancel_requested_at=kwargs["as_of"])
        return True
    def apply(self, value):
        self.applied.append(value)
        self.current = replace(self.current, status=value.status, filled_quantity=value.cumulative_filled_quantity,
                               last_evidence_at=value.observed_at)


class Broker:
    def __init__(self):
        self.account = ACCOUNT
        self.deletes = []
        self.attempts = 0
        self.before_transport = lambda: None
        self.send_error = None
        self.orders = []
        self.history_calls = 0
    def stable_account_fingerprint(self): return self.account
    def prepare_order_submission(self): return SimpleNamespace(identity_fingerprint=self.account)
    def cancel_prepared_order(self, key, context, *, before_delete):
        self.before_transport()
        before_delete()
        self.attempts += 1
        if self.send_error is not None:
            raise self.send_error
        self.deletes.append(key)
    def get_orders(self, **_):
        self.history_calls += 1
        return self.orders


def evidence(*, status="WORKING", filled=0, observed=NOW, broker_status=None, **changes):
    fills = (FillEvidence("fill-1", filled, "100", "2026-10-12T17:19:00+00:00"),) if filled else ()
    value = OrderEvidence("evidence-"+status+str(filled), "r1", ACCOUNT, observed, "123", status, 4, filled,
                          0 if status in {"CANCELLED", "FILLED", "REJECTED"} else 4-filled,
                          fills, broker_status or status)
    return replace(value, **changes)


def service(*, ledger=None, broker=None, observations=None, apply=None, before=None, stopped=None):
    ledger, broker = ledger or Ledger(), broker or Broker()
    events, sleeps, gates = [], [], []
    sequence = iter(observations or [evidence()])
    def observe(_):
        value = next(sequence)
        if isinstance(value, Exception):
            raise value
        return value
    def gate(context, current):
        gates.append((context, current))
        if before:
            before(context, current)
    result = CancellationService(broker=broker, ledger=ledger, account_fingerprint=ACCOUNT,
        before_cancel=gate, apply_evidence=apply or ledger.apply, record_event=lambda kind,payload: events.append((kind,payload)),
        clock=lambda: NOW, sleep=sleeps.append, stopped=stopped or (lambda: False), observe=observe)
    return result, ledger, broker, events, sleeps, gates


def step(value, **changes):
    return value.step("r1", reason="NEW_FORECAST_REPLACES_UNFILLED", evidence_id="cancel-new-forecast", **changes)


def test_one_step_acknowledgment_preserves_commitment_and_does_not_sleep():
    adapter, ledger, broker, events, sleeps, gates = service()
    result = step(adapter, trigger_forecast_id="new-forecast")
    assert result.state == "pending" and not result.complete_terminal_evidence
    assert result.retry_after_seconds == 3 and ledger.current.reserved_quantity == 4
    assert broker.deletes == ["123"] and len(ledger.markers) == 1 and len(gates) == 2
    assert sleeps == [] and ledger.markers[0][1]["stream"] == "atlas-15m"
    assert events[-1][1]["acknowledgment_is_terminal"] is False


def test_persisted_ack_is_observe_only_until_complete_terminal_with_partial_fills():
    adapter, ledger, broker, _, sleeps, _ = service(observations=[evidence(), evidence(status="PARTIAL",filled=1), evidence(status="CANCELLED",filled=2)])
    assert step(adapter).state == "pending"
    assert step(adapter).state == "pending" and ledger.current.reserved_quantity == 3
    result = step(adapter)
    assert result.state == "terminal" and result.complete_terminal_evidence
    assert ledger.current.filled_quantity == 2 and ledger.current.reserved_quantity == 0
    assert len(ledger.applied[-1].fills) == 1 and ledger.applied[-1].fills[0].quantity == 2
    assert broker.deletes == ["123"] and sleeps == []


def test_pending_cancel_does_not_resend_even_with_explicit_failed_send_retry():
    ledger = Ledger(value=reservation(cancel_requested_at=NOW))
    adapter, _, broker, _, sleeps, _ = service(ledger=ledger, observations=[evidence(broker_status="PENDING_CANCEL")])
    result = step(adapter, retry_send=True)
    assert result.state == "pending" and result.reason_code == "BROKER_CANCELLATION_PENDING"
    assert broker.deletes == [] and sleeps == []


def test_unknown_without_exact_broker_id_retains_identity_without_shape_matching():
    ledger = Ledger(value=reservation(status="UNKNOWN", broker_order_id=None))
    adapter, _, broker, _, _, _ = service(ledger=ledger)
    result = step(adapter)
    assert result.state == "reconciliation_required" and result.reason_code == "EXACT_BROKER_ORDER_ID_UNAVAILABLE"
    assert ledger.current.idempotency_key == "idempotent-1" and ledger.current.reserved_quantity == 4
    assert broker.deletes == [] and ledger.applied == []


@pytest.mark.parametrize("stream", ["gameplan", "scout-15m", "unknown"])
def test_automatic_cancellation_cannot_touch_gameplan_or_other_stream(stream):
    adapter, ledger, broker, _, _, _ = service(ledger=Ledger(stream=stream))
    with pytest.raises(ContractError, match="outside"):
        step(adapter)
    assert ledger.markers == [] and broker.deletes == []


def test_legacy_rows_remain_gameplan_and_explicit_intraday_is_fenced():
    legacy = SimpleNamespace()
    assert gameplan_reservation(legacy, reservation()) and gameplan_allocation(legacy, "a1")
    intraday = Ledger()
    assert not gameplan_reservation(intraday, reservation()) and not gameplan_allocation(intraday, "a1")






def test_final_transport_gate_refuses_changed_account_and_does_not_release_marker():
    broker = Broker()
    broker.before_transport = lambda: setattr(broker,"account","different-account")
    adapter, ledger, _, _, sleeps, _ = service(broker=broker)
    result = step(adapter)
    assert result.state == "reconciliation_required" and not result.complete_terminal_evidence
    assert broker.deletes == [] and ledger.current.cancel_requested_at == NOW
    assert ledger.current.reserved_quantity == 4 and sleeps == []


def test_temporary_send_step_preserves_provider_wait_and_only_explicit_retry_resends():
    broker = Broker()
    error = RuntimeError("private provider text")
    error.response = SimpleNamespace(status_code=503, headers={"Retry-After":"17"})
    broker.send_error = error
    adapter, ledger, _, events, sleeps, _ = service(broker=broker, observations=[evidence(),evidence(),evidence()])
    first = step(adapter)
    assert first.state == "pending" and first.retry_after_seconds == 17
    broker.send_error = None
    assert step(adapter).state == "pending" and broker.attempts == 1
    assert step(adapter,retry_send=True).state == "pending" and broker.attempts == 2
    assert ledger.current.reserved_quantity == 4 and len(ledger.markers) == 1 and sleeps == []
    assert "private provider text" not in json.dumps(events)


def test_http_date_and_minimum_retry_wait_are_respected():
    exc = RuntimeError()
    exc.response = SimpleNamespace(headers={"Retry-After":"Mon, 12 Oct 2026 17:20:30 GMT"})
    assert retry_delay(exc,NOW) == 30
    exc.retry_after = 1
    assert retry_delay(exc,NOW) == 3


def test_unbounded_internal_recovery_retries_until_explicit_stop_not_count_cap():
    adapter, ledger, broker, events, sleeps, _ = service(observations=[TimeoutError() for _ in range(7)])
    adapter.stopped = lambda: len(sleeps) == 7
    result = adapter.request("r1",reason="new_forecast",evidence_id="cancel")
    assert result.state == "stopped" and len(sleeps) == 7
    assert len(events) == 7 and ledger.current.reserved_quantity == 4 and broker.deletes == []


@pytest.mark.parametrize("bad", [
    {"broker_order_id":"999"}, {"reservation_id":"other"}, {"account_fingerprint":"other"},
    {"fills":()}, {"remaining_quantity":1}, {"order_quantity":5},
    {"observed_at":"2026-10-12T17:21:00+00:00"},
])
def test_incomplete_or_mismatched_terminal_evidence_never_releases_commitment(bad):
    ledger = Ledger(value=reservation(cancel_requested_at=NOW))
    adapter, _, broker, _, _, _ = service(ledger=ledger,observations=[evidence(status="CANCELLED",filled=1,**bad)])
    result = step(adapter)
    assert result.state == "reconciliation_required" and not result.complete_terminal_evidence
    assert ledger.current.reserved_quantity == 4 and ledger.applied == [] and broker.deletes == []


def test_reconciliation_callback_must_persist_native_evidence_before_terminal_success():
    adapter, ledger, broker, _, _, _ = service(observations=[evidence(status="CANCELLED")],apply=lambda _: None)
    result = step(adapter)
    assert result.state == "reconciliation_required" and not result.complete_terminal_evidence
    assert ledger.current.status == "WORKING" and broker.deletes == []


def test_existing_strict_schwab_history_normalizer_is_reused_without_new_broker_stack():
    adapter, ledger, broker, _, _, _ = service()
    adapter.observe = None
    broker.orders = [{"orderId":"123","status":"CANCELED","quantity":4,"filledQuantity":1,"remainingQuantity":0,
        "orderLegCollection":[{"legId":1,"instruction":"BUY","quantity":4,"instrument":{"assetType":"EQUITY","symbol":"AAPL"}}],
        "orderActivityCollection":[{"activityType":"EXECUTION","executionLegs":[{"legId":1,"executionId":"fill-1",
            "quantity":1,"price":100,"time":"2026-10-12T17:19:00+00:00"}]}]}]
    result = step(adapter)
    assert result.state == "terminal" and result.complete_terminal_evidence
    assert broker.history_calls == 1 and broker.deletes == [] and ledger.current.filled_quantity == 1
    assert ledger.applied[0].fills[0].fill_id.startswith("schwab-fill:")


def test_real_native_marker_restart_and_complete_fill_reconciliation(tmp_path):
    ledger = HorizonLedger(tmp_path/"native.sqlite3",ACCOUNT)
    initial = PortfolioEvidence("initial",ACCOUNT,"2026-10-12T17:19:00+00:00",{"AAPL":0},{"AAPL":100},{"AAPL":0},"b"*64)
    assert ledger.reconcile(initial).ready
    reserved = ledger.reserve_intraday(symbol="AAPL",forecast_id="c"*64,side="BUY",quantity=4,
        limit_price=100,snapshot_id="initial",idempotency_key="native-intent",batch_id="native-batch",
        as_of="2026-10-12T17:19:01+00:00",reference_at="2026-10-12T17:00:00+00:00",
        target_close_at="2026-10-12T17:45:00+00:00",forecast_sha256="c"*64,
        forecast_snapshot_sha256="d"*64,local_symbols=["AAPL"])
    ledger.mark_submission(reserved.reservation_id,status="SUBMITTED",observed_at="2026-10-12T17:19:02+00:00",
                           evidence_id="submitted",broker_order_id="123")
    broker = Broker()
    events, sleeps = [],[]
    clock = {"now":NOW}
    def observed(current):
        filled = 2 if clock["now"] != NOW else 0
        status = "CANCELLED" if filled else "WORKING"
        fills = (FillEvidence("native-fill",2,100,"2026-10-12T17:20:00+00:00"),) if filled else ()
        return OrderEvidence("native-status-"+status,current.reservation_id,ACCOUNT,
            "2026-10-12T17:20:01+00:00" if filled else "2026-10-12T17:19:59+00:00",
            "123",status,4,filled,0 if filled else 4,fills,status)
    def apply(value):
        ledger.reconcile(PortfolioEvidence("snapshot-"+value.status,ACCOUNT,clock["now"],
            {"AAPL":value.cumulative_filled_quantity},{"AAPL":100},{"AAPL":0},"e"*64),order_evidence=(value,))
    adapter = CancellationService(broker=broker,ledger=ledger,account_fingerprint=ACCOUNT,
        before_cancel=lambda *_:None,apply_evidence=apply,record_event=lambda *args:events.append(args),
        clock=lambda:clock["now"],sleep=sleeps.append,observe=observed)
    first = adapter.step(reserved.reservation_id,reason="new-forecast",evidence_id="cancel-native",trigger_forecast_id="new-forecast")
    assert first.state == "pending" and broker.deletes == ["123"]
    # Restart reads the same committed marker and avoids another DELETE.
    ledger = HorizonLedger(ledger.path,ACCOUNT)
    adapter.ledger = ledger
    assert ledger.lookup_reservation(reserved.reservation_id).cancel_requested_at == NOW
    clock["now"] = "2026-10-12T17:20:02+00:00"
    completed = adapter.step(reserved.reservation_id,reason="new-forecast",evidence_id="cancel-native")
    assert completed.complete_terminal_evidence and completed.status == "CANCELLED"
    assert ledger.lookup_reservation(reserved.reservation_id).filled_quantity == 2
    assert ledger.snapshot().allocations[0].filled_shares == 2
    assert broker.deletes == ["123"] and sleeps == []
