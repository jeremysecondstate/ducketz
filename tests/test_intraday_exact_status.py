"""Offline exact-ID order observations without a ledger or trading authority."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from ml.intraday import exact_status
from ml.intraday.prediction import ContractError
from ml.stock_trader.horizon_broker import ORDER_HISTORY_LIMIT, OrderHistoryError


ACCOUNT = "a" * 64
OBSERVED = "2026-10-12T17:15:05+00:00"


def reservation(**changes):
    return SimpleNamespace(**(dict(reservation_id="local-reservation:1", broker_order_id="123",
        symbol="DOCU", side="BUY", quantity=3) | changes))


def raw_order(*, identity="123", status="WORKING", filled=0):
    return dict(orderId=identity, status=status, quantity=3, filledQuantity=filled,
        remainingQuantity=0 if status in {"CANCELED", "REJECTED", "FILLED"} else 3-filled,
        orderLegCollection=[dict(legId=1, instruction="BUY", quantity=3,
            instrument=dict(assetType="EQUITY", symbol="DOCU"))],
        orderActivityCollection=([dict(activityType="EXECUTION", executionLegs=[
            dict(legId=1, executionId="fill:1", quantity=filled, price=42.4,
                 time="2026-10-12T17:15:03.028841+0000")])] if filled else []))


class History:
    """The injected object exposes only the two read-only history operations."""
    def __init__(self, rows, identities=(ACCOUNT, ACCOUNT)):
        self.rows, self.calls, self.identities = rows, [], iter(identities)

    def stable_account_fingerprint(self):
        return next(self.identities)

    def get_orders(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows


def read(broker, tracked=None, *, clock=lambda: datetime.fromisoformat(OBSERVED)):
    return exact_status.read_exact_status(broker, tracked or reservation(),
        account_fingerprint=ACCOUNT, clock=clock)


def test_single_exact_id_is_selected_without_mutating_the_tracked_order():
    tracked = reservation()
    original = vars(tracked).copy()
    rows = [raw_order(identity="999"), raw_order()]
    before = deepcopy(rows)
    broker = History(rows)
    evidence = read(broker, tracked)
    assert (evidence.reservation_id, evidence.broker_order_id, evidence.order_quantity) == (
        "local-reservation:1", "123", 3)
    assert (evidence.status, evidence.remaining_quantity, evidence.fills) == ("WORKING", 3, ())
    assert evidence.account_fingerprint == ACCOUNT and evidence.observed_at == OBSERVED
    assert vars(tracked) == original and rows == before
    assert len(broker.calls) == 1
    request = broker.calls[0]
    assert request["max_results"] == ORDER_HISTORY_LIMIT
    assert request["to_entered_time"] == datetime.fromisoformat(OBSERVED)
    assert request["to_entered_time"] - request["from_entered_time"] == timedelta(days=14)


def test_partial_terminal_order_retains_complete_fills_description_and_wire_provenance():
    row = raw_order(status="CANCELED", filled=2)
    row["statusDescription"] = "Synthetic cancellation detail"
    evidence = read(History([row]))
    assert (evidence.status, evidence.broker_status, evidence.cumulative_filled_quantity,
            evidence.remaining_quantity) == ("CANCELLED", "CANCELED", 2, 0)
    fill, = evidence.fills
    assert fill.fill_id.startswith("schwab-fill:")
    assert (fill.quantity, fill.price, fill.executed_at) == (2, "42.4", "2026-10-12T17:15:03.028841+00:00")
    assert evidence.broker_status_description == row["statusDescription"]
    assert evidence.fill_time_provenance == {"version": "schwab-execution-time-v1", "fills": {
        fill.fill_id: {"raw_timestamp": "2026-10-12T17:15:03.028841+0000", "source_field": "execution_leg.time"}}}


@pytest.mark.parametrize("identity,reason,reads", [
    (("b" * 64, ACCOUNT), "CHANGED_BEFORE", 0),
    ((ACCOUNT, "b" * 64), "CHANGED_DURING", 1),
])
def test_broker_identity_must_match_before_and_after_history(identity, reason, reads):
    broker = History([raw_order()], identities=identity)
    with pytest.raises(OrderHistoryError, match=reason):
        read(broker)
    assert len(broker.calls) == reads


@pytest.mark.parametrize("rows,reason", [
    ([], "MISSING_FROM_BOUNDED_HISTORY"),
    ([raw_order(identity="999")], "MISSING_FROM_BOUNDED_HISTORY"),
    ({"orders": [raw_order()]}, "COMPLETE_LIST"),
    ([None], "INVALID_ROWS"),
    ([dict(raw_order(), orderId="")], "ROW_MISSING_ID"),
    ([dict(raw_order(), childOrderStrategies={})], "CHILDREN_ARE_INVALID"),
    ([raw_order(), raw_order()], "DUPLICATE_ORDER_IDS"),
    ([raw_order()] * ORDER_HISTORY_LIMIT, "MAY_BE_TRUNCATED"),
])
def test_missing_malformed_duplicate_and_truncated_history_fails_closed(rows, reason):
    with pytest.raises(OrderHistoryError, match=reason):
        read(History(rows))


@pytest.mark.parametrize("identity", [None, ""])
def test_unknown_submission_without_exact_id_never_queries_history(identity):
    broker = History([raw_order()])
    with pytest.raises(OrderHistoryError, match="WITHOUT_EXACT_BROKER_ORDER_ID"):
        read(broker, reservation(broker_order_id=identity))
    assert broker.calls == []


@pytest.mark.parametrize("damage,reason", [
    ("symbol", "SYMBOL_OR_SIDE"), ("side", "SYMBOL_OR_SIDE"),
    ("quantity", "QUANTITY_MISMATCH"), ("missing_fills", "COMPLETE_PER_ORDER_FILL"),
    ("future_fill", "POSTDATES_CAPTURE_BOUNDARY"), ("unknown_status", "UNCERTAIN_BROKER_ORDER_STATUS"),
])
def test_exact_identity_alone_cannot_make_incomplete_or_mismatched_evidence(damage, reason):
    row = raw_order(status="FILLED", filled=3)
    if damage == "symbol": row["orderLegCollection"][0]["instrument"]["symbol"] = "OTHER"
    elif damage == "side": row["orderLegCollection"][0]["instruction"] = "SELL"
    elif damage == "quantity": row["quantity"] = 4
    elif damage == "missing_fills": row["orderActivityCollection"] = []
    elif damage == "future_fill": row["orderActivityCollection"][0]["executionLegs"][0]["time"] = "2026-10-12T17:15:06Z"
    elif damage == "unknown_status": row["status"] = "REPLACED"
    with pytest.raises(OrderHistoryError, match=reason):
        read(History([row]))


def test_completed_observation_clock_admits_a_fill_received_during_history_io():
    row = raw_order(status="FILLED", filled=3)
    row["orderActivityCollection"][0]["executionLegs"][0]["time"] = "2026-10-12T17:15:06Z"
    ticks = iter((OBSERVED, "2026-10-12T17:15:07+00:00"))
    broker = History([row])
    evidence = read(broker, clock=lambda: next(ticks))
    assert broker.calls[0]["to_entered_time"] == datetime.fromisoformat(OBSERVED)
    assert evidence.observed_at == "2026-10-12T17:15:07+00:00"
    assert evidence.fills[0].executed_at == "2026-10-12T17:15:06+00:00"


def test_backwards_observation_clock_cannot_create_status_evidence():
    ticks = iter((OBSERVED, "2026-10-12T17:15:04+00:00"))
    with pytest.raises(OrderHistoryError, match="CLOCK_MOVED_BACKWARDS"):
        read(History([raw_order()]), clock=lambda: next(ticks))


@pytest.mark.parametrize("count", [0, 2])
def test_wrapper_requires_exactly_one_capture_result(monkeypatch, count):
    monkeypatch.setattr(exact_status, "capture_order_evidence", lambda *args, **kwargs: (object(),) * count)
    with pytest.raises(ContractError, match="Exact tracked status evidence required"):
        read(object())


def test_wrapper_supplies_only_the_selected_reservation_and_observation_clock(monkeypatch):
    tracked, broker, sentinel = reservation(), object(), object()
    clock = lambda: OBSERVED
    def capture(actual_broker, scope, **options):
        assert actual_broker is broker
        assert scope.pending_reservations() == (tracked,)
        assert scope.account_fingerprint == ACCOUNT
        assert options == dict(account_fingerprint=ACCOUNT, as_of=OBSERVED, observation_clock=clock)
        return (sentinel,)
    monkeypatch.setattr(exact_status, "capture_order_evidence", capture)
    assert exact_status.read_exact_status(broker, tracked, account_fingerprint=ACCOUNT, clock=clock) is sentinel
