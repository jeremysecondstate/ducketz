"""Timeout retries consume original quantities, independently of prediction IDs."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ml.account_gameplan.authority import AuthorityError
from ml.account_gameplan.execution import AccountExecutionContext
from ml.stock_trader.catchup import catchup_signals


FIRST = "2026-09-08T11:06:00Z"
LATER = "2026-09-08T12:01:00Z"


def plan(side="BUY", *, future_side=None, quantity=10):
    events = [{"forecast_id": "first", "symbol": "AAPL", "horizon": "1h", "action": side,
               "quantity": quantity, "timestamp": "2026-09-08T11:00:00Z", "sequence": 1}]
    if future_side:
        events.append({"forecast_id": "future", "symbol": "AAPL", "horizon": "1h",
                       "action": future_side, "quantity": 3, "timestamp": "2026-09-08T12:00:00Z",
                       "sequence": 2})
    return {"action_date": "2026-09-08", "as_of": "2026-09-08T09:00:00Z",
            "plan_sha256": "a" * 64, "ledger": {"events": events}}


def signals(source, reservations=(), *, as_of=FIRST, **kwargs):
    return catchup_signals(source, as_of=as_of, source_fingerprint="synthetic-source",
                           reservations=reservations, **kwargs)


def reservation(signal, identity, *, quantity=None, filled=0, status="CANCELLED",
                timeout=True, parent=None, broker_id="synthetic-broker-order", submitted_at=FIRST):
    quantity = signal.planned_quantity if quantity is None else quantity
    return SimpleNamespace(
        reservation_id=identity, forecast_id=signal.prediction_id, symbol="AAPL", horizon="1h",
        quantity=quantity, filled_quantity=filled, status=status, side=signal.suggested_action,
        reserved_quantity=quantity - filled if status in {"RESERVED", "SUBMITTED", "UNKNOWN", "WORKING", "PARTIAL"} else 0,
        timeout_requested_at=FIRST if timeout else None, retry_parent_id=parent,
        retry_requested_quantity=signal.timeout_retry_quantity if parent else None,
        submitted_at=submitted_at, broker_order_id=broker_id, last_evidence_at=FIRST,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_confirmed_timeout_offers_one_partial_remainder_with_durable_parent(side):
    source = plan(side)
    first = signals(source)["AAPL", "1h"]
    original = reservation(first, "original", filled=2)
    retry = signals(source, [original])["AAPL", "1h"]
    assert retry.planned_quantity == 8 and retry.suggested_action == side
    assert retry.timeout_retry_parent_id == "original"
    assert retry.timeout_retry_quantity == 8
    assert asdict(retry)["timeout_retry_parent_id"] == "original"
    assert signals(source, [original])["AAPL", "1h"] == retry
    assert original.filled_quantity == 2  # Reconciliation is a pure read.


@pytest.mark.parametrize("status", ["RESERVED", "SUBMITTED", "WORKING", "PARTIAL", "UNKNOWN"])
def test_timeout_request_does_not_release_order_before_cancel_confirmation(status):
    source = plan()
    first = signals(source)["AAPL", "1h"]
    pending = reservation(first, "original", filled=2, status=status)
    assert signals(source, [pending]) == {}


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("child_timeout", [False, True])
def test_final_cancel_abandons_partial_retry_and_unsent_remainder(side, child_timeout):
    source = plan(side)
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    retry = signals(source, [original])["AAPL", "1h"]
    child = reservation(retry, "retry", quantity=3, filled=1, parent="original", timeout=child_timeout)
    abandoned = []
    assert signals(source, [original, child], abandoned=abandoned) == {}
    assert [(row["reservation_id"], row["abandoned_quantity"]) for row in abandoned] == [("original", 5), ("retry", 2)]
    sign = 1 if side == "BUY" else -1
    assert sum(row["signed_abandoned_quantity"] for row in abandoned) == sign * 7
    assert signals(source, [child, original]) == {}  # Order and restart do not reset the cap.


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("future_side", ["BUY", "SELL"])
def test_future_instruction_does_not_resurrect_abandoned_old_delta(side, future_side):
    source = plan(side, future_side=future_side)
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    retry = signals(source, [original])["AAPL", "1h"]
    child = reservation(retry, "retry", filled=1, parent="original")
    assert signals(source, [original, child]) == {}
    future = signals(source, [original, child], as_of=LATER)["AAPL", "1h"]
    assert future.planned_quantity == 3
    assert future.suggested_action == future_side
    assert future.timeout_retry_parent_id is None
    assert future.prediction_id != retry.prediction_id


@pytest.mark.parametrize("status", ["RESERVED", "SUBMITTED", "WORKING", "PARTIAL", "UNKNOWN"])
def test_live_or_unknown_child_consumes_retry_and_keeps_all_remaining_reserved(status):
    source = plan()
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    retry = signals(source, [original])["AAPL", "1h"]
    child = reservation(retry, "retry", quantity=3, filled=1, parent="original", status=status, timeout=False)
    abandoned = []
    assert signals(source, [original, child], abandoned=abandoned) == {}
    assert len(abandoned) == 1 and abandoned[0]["abandoned_quantity"] == 5


def test_proven_pre_post_rejection_keeps_same_parent_allowance_across_new_prediction_ids():
    source = plan()
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    attempts = [original]
    prediction_ids = set()
    for index in range(3):
        retry = signals(source, attempts)["AAPL", "1h"]
        prediction_ids.add(retry.prediction_id)
        assert retry.planned_quantity == 8 and retry.timeout_retry_parent_id == "original"
        attempts.append(reservation(retry, f"safety-{index}", quantity=3, status="REJECTED",
                                    parent="original", broker_id=None, submitted_at=None, timeout=False))
    assert len(prediction_ids) == 3
    retry = signals(source, attempts)["AAPL", "1h"]
    attempts.append(reservation(retry, "actual-retry", filled=0, parent="original"))
    assert signals(source, attempts) == {}


@pytest.mark.parametrize("broker_id,reason", [
    ("confirmed-rejection", "CONFIRMED_BROKER_REJECTION_REQUIRES_REVIEW"),
    (None, "TIMEOUT_RETRY_OUTCOME_REQUIRES_REVIEW"),
])
def test_attempted_rejected_child_does_not_reopen_allowance(broker_id, reason):
    source = plan()
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    retry = signals(source, [original])["AAPL", "1h"]
    child = reservation(retry, "retry", parent="original", status="REJECTED", broker_id=broker_id, timeout=False)
    blocked = []
    assert signals(source, [original, child], blocked=blocked) == {}
    assert blocked[0]["reason_code"] == reason


def test_split_original_orders_each_have_one_retry_in_deterministic_order():
    source = plan(quantity=12)
    first = signals(source)["AAPL", "1h"]
    a = reservation(first, "a", quantity=5, filled=1)
    b = reservation(first, "b", quantity=7, filled=2)
    retry_a = signals(source, [b, a])["AAPL", "1h"]
    assert retry_a.timeout_retry_parent_id == "a" and retry_a.planned_quantity == 4
    child_a = reservation(retry_a, "retry-a", parent="a", status="FILLED", filled=4, timeout=False)
    retry_b = signals(source, [child_a, b, a])["AAPL", "1h"]
    assert retry_b.timeout_retry_parent_id == "b" and retry_b.planned_quantity == 5
    child_b = reservation(retry_b, "retry-b", parent="b", filled=1)
    assert signals(source, [child_b, child_a, b, a]) == {}


@pytest.mark.parametrize("side,opposite", [("BUY", "SELL"), ("SELL", "BUY")])
@pytest.mark.parametrize("status", ["WORKING", "FILLED", "CANCELLED"])
@pytest.mark.parametrize("capacity", [3, 5])
def test_opposite_instruction_before_retry_is_not_also_abandoned(side, opposite, status, capacity):
    source = plan(side, future_side=opposite)
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    retry = signals(source, [original], as_of=LATER)["AAPL", "1h"]
    assert retry.planned_quantity == 5 and retry.timeout_retry_parent_id == "original"
    assert retry.timeout_retry_quantity == 5
    child = reservation(retry, "retry", quantity=capacity, parent="original", status=status,
                        filled=capacity if status == "FILLED" else 1)
    abandoned = []
    assert signals(source, [original, child], as_of=LATER, abandoned=abandoned) == {}
    # Economic netting already accounted for the opposite three-share event.
    # Capacity alone can abandon two unsent shares; terminal cancellation can
    # additionally abandon just the actual child's unfilled quantity.
    expected = 5 - capacity + (capacity - 1 if status == "CANCELLED" else 0)
    assert sum(row["abandoned_quantity"] for row in abandoned) == expected
    assert signals(source, [child, original], as_of=LATER) == {}


@pytest.mark.parametrize("requested", [None, True, 0, 4, 9, 5.0, "5"])
def test_missing_or_invalid_retry_requested_quantity_fails_closed(requested):
    source = plan(future_side="SELL")
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    retry = signals(source, [original], as_of=LATER)["AAPL", "1h"]
    child = reservation(retry, "retry", parent="original", status="WORKING", filled=1)
    child.retry_requested_quantity = requested
    with pytest.raises(ValueError, match="valid frozen requested quantity"):
        signals(source, [original, child], as_of=LATER)


def test_ordinary_cancel_and_filled_orders_keep_legacy_netting():
    source = plan()
    first = signals(source)["AAPL", "1h"]
    ordinary = reservation(first, "ordinary", filled=2, timeout=False)
    residual = signals(source, [ordinary])["AAPL", "1h"]
    assert residual.planned_quantity == 8 and residual.timeout_retry_parent_id is None
    assert residual.timeout_retry_quantity is None
    complete = reservation(residual, "completed", filled=8, status="FILLED", timeout=False)
    assert signals(source, [ordinary, complete]) == {}


def test_account_reconstruction_excludes_only_new_child_and_verifies_parent_metadata():
    source = plan()
    original = reservation(signals(source)["AAPL", "1h"], "original", filled=2)
    retry = signals(source, [original])["AAPL", "1h"]
    native = reservation(retry, "new-native", quantity=3, parent="original", status="RESERVED", timeout=False,
                         broker_id=None, submitted_at=None)
    context = SimpleNamespace(
        plan=SimpleNamespace(report={"accepted_joint_plan": True, "joint_composition": source},
                             path=SimpleNamespace(name="synthetic-source")),
        native_ledger=SimpleNamespace(snapshot=lambda: SimpleNamespace(reservations=(original, native))),
        config=SimpleNamespace(participants={"pc-original": ("AAPL",)}),
    )
    decision = SimpleNamespace(symbol="AAPL", prediction=asdict(retry), action="BUY", quantity=3, decided_at=FIRST)
    assert AccountExecutionContext._catchup_participant(context, decision, native) == "pc-original"
    decision.prediction["timeout_retry_quantity"] = 3
    with pytest.raises(AuthorityError, match="DECISION_DIFFERS_FROM_RECONCILED_ACCOUNT_INTENTION"):
        AccountExecutionContext._catchup_participant(context, decision, native)
    decision.prediction["timeout_retry_quantity"] = retry.timeout_retry_quantity
    decision.prediction["timeout_retry_parent_id"] = "different-parent"
    with pytest.raises(AuthorityError, match="DECISION_DIFFERS_FROM_RECONCILED_ACCOUNT_INTENTION"):
        AccountExecutionContext._catchup_participant(context, decision, native)
