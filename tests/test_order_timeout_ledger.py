"""Durable timeout/retry accounting using only synthetic local SQLite ledgers."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ml.stock_trader.horizon_ledger import (
    FillEvidence, HorizonLedger, LedgerError, OrderEvidence, PortfolioEvidence,
)


ACCOUNT = "a" * 64
PLAN = "b" * 64
START = "2026-10-12T11:00:00+00:00"
END = "2026-10-13T00:00:00+00:00"
SUBMITTED = "2026-10-12T11:02:00+00:00"
TIMEOUT = "2026-10-12T11:07:00+00:00"


def portfolio(identity, at, held=20):
    return PortfolioEvidence(identity, ACCOUNT, at, {"COST": held}, {"COST": 100},
                             {"COST": 10000}, "c" * 64)


def reserve(ledger, side, *, attempt="initial", snapshot="initial", quantity=10,
            at="2026-10-12T11:00:10+00:00", horizon="1h", plan=PLAN):
    options = dict(symbol="COST", horizon=horizon,
                   forecast_id=f"catchup:{plan}:COST:{horizon}:{attempt}",
                   target_start=START, target_end=END, quantity=quantity, limit_price=100,
                   snapshot_id=snapshot, idempotency_key=attempt, batch_id="batch", as_of=at)
    if side == "SELL":
        return ledger.reserve_direction_exit(**options)
    return ledger.reserve_entry(**options, allow_accumulation=True)


def evidence(order, status, *, filled=0, at="2026-10-12T11:05:59+00:00", suffix="working"):
    fills = ((FillEvidence(f"fill:{order.reservation_id}", filled, 100,
                           "2026-10-12T11:03:00+00:00"),) if filled else ())
    return OrderEvidence(f"{order.reservation_id}:{suffix}", order.reservation_id, ACCOUNT,
                         at, f"broker:{order.reservation_id}", status, order.quantity, filled,
                         0 if status in {"CANCELLED", "FILLED", "REJECTED"} else order.quantity-filled,
                         fills)


def working(tmp_path, side="BUY", *, filled=0):
    ledger = HorizonLedger(tmp_path / "holdings.sqlite3", ACCOUNT)
    assert ledger.reconcile(portfolio("initial", START)).ready
    parent = reserve(ledger, side)
    ledger.mark_submission(parent.reservation_id, status="SUBMITTED", observed_at=SUBMITTED,
                           evidence_id="submitted", broker_order_id=f"broker:{parent.reservation_id}")
    held = 20 + filled * (1 if side == "BUY" else -1)
    assert ledger.reconcile(portfolio("working", "2026-10-12T11:06:00+00:00", held),
                            order_evidence=(evidence(parent, "PARTIAL" if filled else "WORKING", filled=filled),)).ready
    return ledger, ledger.lookup_reservation(parent.reservation_id)


def cancelled(tmp_path, side="BUY", *, filled=0):
    ledger, parent = working(tmp_path, side, filled=filled)
    assert ledger.reserve_order_timeout(parent.reservation_id, as_of=TIMEOUT, evidence_id="timeout")
    held = 20 + filled * (1 if side == "BUY" else -1)
    assert ledger.reconcile(portfolio("cancelled", "2026-10-12T11:07:03+00:00", held),
        order_evidence=(evidence(parent, "CANCELLED", filled=filled,
                                at="2026-10-12T11:07:02+00:00", suffix="cancelled"),)).ready
    return ledger, ledger.lookup_reservation(parent.reservation_id)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_timeout_uses_first_submission_and_keeps_partial_reservation_after_request(tmp_path, side):
    ledger, parent = working(tmp_path, side, filled=2)
    assert parent.submitted_at == SUBMITTED
    assert parent.last_evidence_at != parent.submitted_at
    with pytest.raises(LedgerError, match="ORDER_TIMEOUT_NOT_DUE"):
        ledger.reserve_order_timeout(parent.reservation_id, as_of="2026-10-12T11:06:59+00:00", evidence_id="early")
    assert ledger.reserve_order_timeout(parent.reservation_id, as_of=TIMEOUT, evidence_id="timeout")
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    current = reopened.lookup_reservation(parent.reservation_id)
    assert current.submitted_at == SUBMITTED
    assert current.timeout_requested_at == current.cancel_requested_at == TIMEOUT
    assert current.status == "PARTIAL" and current.filled_quantity == 2 and current.reserved_quantity == 8
    assert not reopened.reserve_order_timeout(parent.reservation_id, as_of="2026-10-12T11:10:00Z", evidence_id="again")
    # A lost/failed cancellation response cannot free shares or cash locally.
    assert reopened.pending_reservations()[0].reserved_quantity == 8


def test_legacy_submission_audit_backfill_preserves_first_unknown_time_and_original_evidence(tmp_path):
    ledger = HorizonLedger(tmp_path / "old.sqlite3", ACCOUNT)
    ledger.reconcile(portfolio("initial", START))
    parent = reserve(ledger, "BUY")
    ledger.mark_submission(parent.reservation_id, status="UNKNOWN", observed_at=SUBMITTED, evidence_id="unknown")
    ledger.mark_submission(parent.reservation_id, status="SUBMITTED", observed_at="2026-10-12T11:02:30Z",
                           evidence_id="accepted", broker_order_id="broker")
    before = ledger.history()
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TABLE order_submission_times")
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert reopened.lookup_reservation(parent.reservation_id).submitted_at == SUBMITTED
    assert reopened.history() == before
    assert reopened.lookup_reservation(parent.reservation_id).submitted_at == SUBMITTED


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_confirmed_partial_cancel_allows_one_durable_same_intention_retry(tmp_path, side):
    ledger, parent = cancelled(tmp_path, side, filled=2)
    assert parent.filled_quantity == 2 and parent.reserved_quantity == 0
    child = reserve(ledger, side, attempt="retry", snapshot="cancelled", quantity=8,
                    at="2026-10-12T11:07:04Z")
    bound = ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity,
                                     as_of="2026-10-12T11:07:04Z", evidence_id="link")
    assert bound.retry_parent_id == parent.reservation_id
    assert bound.retry_requested_quantity == 8
    assert bound.submitted_at is None and bound.timeout_requested_at is None
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert reopened.lookup_reservation(child.reservation_id).retry_parent_id == parent.reservation_id
    assert reopened.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity,
                                      as_of="2026-10-12T11:07:05Z", evidence_id="repeat") == bound
    second = reserve(reopened, side, attempt="extra", snapshot="cancelled", quantity=1,
                     at="2026-10-12T11:07:05Z")
    with pytest.raises(LedgerError, match="TIMEOUT_RETRY_ALREADY_RESERVED"):
        reopened.bind_timeout_retry(second.reservation_id, parent.reservation_id, requested_quantity=second.quantity,
                                    as_of="2026-10-12T11:07:05Z", evidence_id="extra-link")


def test_retry_can_recover_proven_prepost_rejection_but_unknown_consumes_attempt(tmp_path):
    ledger, parent = cancelled(tmp_path)
    first = reserve(ledger, "BUY", attempt="prepost", snapshot="cancelled", quantity=5,
                    at="2026-10-12T11:07:04Z")
    ledger.bind_timeout_retry(first.reservation_id, parent.reservation_id, requested_quantity=first.quantity,
                              as_of="2026-10-12T11:07:04Z", evidence_id="first-link")
    rejected = ledger.mark_submission(first.reservation_id, status="REJECTED", observed_at="2026-10-12T11:07:05Z",
                                     evidence_id="prepost-stop")
    assert rejected.submitted_at is None and rejected.broker_order_id is None
    second = reserve(ledger, "BUY", attempt="retry", snapshot="cancelled", quantity=5,
                     at="2026-10-12T11:07:06Z")
    ledger.bind_timeout_retry(second.reservation_id, parent.reservation_id, requested_quantity=second.quantity,
                              as_of="2026-10-12T11:07:06Z", evidence_id="second-link")
    third = reserve(ledger, "BUY", attempt="third", snapshot="cancelled", quantity=1,
                    at="2026-10-12T11:07:06Z")
    ledger.mark_submission(second.reservation_id, status="UNKNOWN", observed_at="2026-10-12T11:07:07Z",
                           evidence_id="uncertain")
    with pytest.raises(LedgerError, match="TIMEOUT_RETRY_ALREADY_RESERVED"):
        ledger.bind_timeout_retry(third.reservation_id, parent.reservation_id, requested_quantity=third.quantity,
                                  as_of="2026-10-12T11:07:08Z", evidence_id="third-link")
    with pytest.raises(LedgerError, match="UNKNOWN_SUBMISSION_REQUIRES"):
        ledger.mark_submission(second.reservation_id, status="REJECTED", observed_at="2026-10-12T11:07:08Z",
                               evidence_id="cannot-guess")


@pytest.mark.parametrize("change,reason", [
    ({"quantity": 9}, "EXCEEDS_CONFIRMED_REMAINDER"),
    ({"side": "SELL"}, "INTENTION_MISMATCH"),
    ({"horizon": "4h"}, "INTENTION_MISMATCH"),
    ({"plan": "d" * 64}, "PLAN_FAMILY_MISMATCH"),
])
def test_invalid_retry_identity_or_quantity_cannot_link(tmp_path, change, reason):
    ledger, parent = cancelled(tmp_path, filled=2)
    options = dict(side="BUY", attempt="child", snapshot="cancelled", quantity=8,
                   at="2026-10-12T11:07:04Z")
    options.update(change)
    child = reserve(ledger, **options)
    with pytest.raises(LedgerError, match=reason):
        ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity,
                                  as_of="2026-10-12T11:07:04Z", evidence_id="invalid")
    assert ledger.lookup_reservation(child.reservation_id).retry_parent_id is None


def test_unconfirmed_cancel_and_missing_submission_time_fail_closed(tmp_path):
    ledger, parent = working(tmp_path)
    ledger.reserve_order_timeout(parent.reservation_id, as_of=TIMEOUT, evidence_id="timeout")
    # An exact child fixture can exist, but a working parent's cancel request
    # alone grants no permission to bind it for submission.
    child = reserve(ledger, "BUY", attempt="child", snapshot="working", quantity=1,
                    at="2026-10-12T11:06:01Z")
    with pytest.raises(LedgerError, match="CANCELLED_INITIAL_ORDER"):
        ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity, as_of=TIMEOUT, evidence_id="bad")
    with pytest.raises(LedgerError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        HorizonLedger(ledger.path, "f" * 64)
    with sqlite3.connect(ledger.path) as db:
        db.execute("DELETE FROM cancellations")
        db.execute("DELETE FROM order_timeouts")
        db.execute("DELETE FROM order_submission_times")
        db.execute("DELETE FROM evidence WHERE kind='submission'")
    with pytest.raises(LedgerError, match="TIMEOUT_SUBMISSION_TIME_UNAVAILABLE"):
        ledger.reserve_order_timeout(parent.reservation_id, as_of=TIMEOUT, evidence_id="missing")


def test_retry_cannot_spawn_another_retry_after_its_own_timeout(tmp_path):
    ledger, parent = cancelled(tmp_path)
    child = reserve(ledger, "BUY", attempt="retry", snapshot="cancelled", quantity=5,
                    at="2026-10-12T11:07:04Z")
    ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity,
                              as_of="2026-10-12T11:07:04Z", evidence_id="link")
    ledger.mark_submission(child.reservation_id, status="SUBMITTED", observed_at="2026-10-12T11:07:05Z",
                           evidence_id="retry-submitted", broker_order_id=f"broker:{child.reservation_id}")
    ledger.reconcile(portfolio("retry-working", "2026-10-12T11:12:05Z"),
        order_evidence=(evidence(child, "WORKING", at="2026-10-12T11:12:04Z"),))
    ledger.reserve_order_timeout(child.reservation_id, as_of="2026-10-12T11:12:05Z", evidence_id="retry-timeout")
    ledger.reconcile(portfolio("retry-cancelled", "2026-10-12T11:12:07Z"),
        order_evidence=(evidence(child, "CANCELLED", at="2026-10-12T11:12:06Z", suffix="cancelled"),))
    third = reserve(ledger, "BUY", attempt="third", snapshot="retry-cancelled", quantity=5,
                    at="2026-10-12T11:12:08Z")
    with pytest.raises(LedgerError, match="CANCELLED_INITIAL_ORDER"):
        ledger.bind_timeout_retry(third.reservation_id, child.reservation_id, requested_quantity=third.quantity,
                                  as_of="2026-10-12T11:12:08Z", evidence_id="no-chain")


def test_competing_retry_bindings_have_one_durable_winner(tmp_path):
    ledger, parent = cancelled(tmp_path)
    children = [reserve(ledger, "BUY", attempt=f"child-{n}", snapshot="cancelled", quantity=1,
                        at="2026-10-12T11:07:04Z") for n in range(2)]

    def bind(child):
        try:
            return ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity,
                as_of="2026-10-12T11:07:04Z", evidence_id=f"link:{child.reservation_id}").retry_parent_id
        except LedgerError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(bind, children))
    assert sorted(outcomes) == sorted([parent.reservation_id, "TIMEOUT_RETRY_ALREADY_RESERVED"])
    restarted = HorizonLedger(ledger.path, ACCOUNT)
    assert sum(r.retry_parent_id == parent.reservation_id for r in restarted.snapshot().reservations) == 1


def test_timeout_and_retry_require_the_ledger_account_even_for_corrupt_mixed_rows(tmp_path):
    ledger, parent = working(tmp_path)
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE allocations SET account=? WHERE id=?", ("f" * 64, parent.allocation_id))
    with pytest.raises(LedgerError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        ledger.reserve_order_timeout(parent.reservation_id, as_of=TIMEOUT, evidence_id="wrong-account")
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM order_timeouts").fetchone()[0] == 0
    ledger, parent = cancelled(tmp_path / "retry")
    child = reserve(ledger, "BUY", attempt="retry", snapshot="cancelled", quantity=5,
                    at="2026-10-12T11:07:04Z")
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE allocations SET account=? WHERE id=?", ("f" * 64, child.allocation_id))
    with pytest.raises(LedgerError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity,
                                  as_of="2026-10-12T11:07:04Z", evidence_id="wrong-account")


def test_terminal_manual_cancel_does_not_grant_timeout_retry(tmp_path):
    ledger, parent = working(tmp_path)
    ledger.reconcile(portfolio("cancelled", "2026-10-12T11:07:03Z"),
        order_evidence=(evidence(parent, "CANCELLED", at="2026-10-12T11:07:02Z", suffix="manual"),))
    child = reserve(ledger, "BUY", attempt="retry", snapshot="cancelled", quantity=5,
                    at="2026-10-12T11:07:04Z")
    with pytest.raises(LedgerError, match="CANCELLED_INITIAL_ORDER"):
        ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id, requested_quantity=child.quantity,
                                  as_of="2026-10-12T11:07:04Z", evidence_id="no-timeout")


@pytest.mark.parametrize("requested", [5, 8])
def test_retry_preserves_economic_requested_quantity_before_execution_sizing(tmp_path, requested):
    ledger, parent = cancelled(tmp_path, filled=2)
    child = reserve(ledger, "BUY", attempt="retry", snapshot="cancelled", quantity=5,
                    at="2026-10-12T11:07:04Z")
    bound = ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id,
        requested_quantity=requested, as_of="2026-10-12T11:07:04Z", evidence_id="link")
    assert bound.quantity == 5 and parent.quantity - parent.filled_quantity == 8
    assert bound.retry_requested_quantity == requested
    restarted = HorizonLedger(ledger.path, ACCOUNT)
    assert restarted.lookup_reservation(child.reservation_id).retry_requested_quantity == requested
    with pytest.raises(LedgerError, match="REQUESTED_QUANTITY_CHANGED"):
        restarted.bind_timeout_retry(child.reservation_id, parent.reservation_id,
            requested_quantity=6, as_of="2026-10-12T11:07:05Z", evidence_id="changed")
    assert restarted.lookup_reservation(child.reservation_id).retry_requested_quantity == requested


@pytest.mark.parametrize("requested,reason", [
    (0, "REQUESTED_QUANTITY_INVALID"), (-1, "REQUESTED_QUANTITY_INVALID"),
    (True, "REQUESTED_QUANTITY_INVALID"), (5.0, "REQUESTED_QUANTITY_INVALID"),
    (5.5, "REQUESTED_QUANTITY_INVALID"), ("5", "REQUESTED_QUANTITY_INVALID"),
    (4, "EXCEEDS_CONFIRMED_REMAINDER"), (9, "EXCEEDS_CONFIRMED_REMAINDER"),
])
def test_retry_requested_quantity_must_cover_child_inside_confirmed_parent_remainder(tmp_path, requested, reason):
    ledger, parent = cancelled(tmp_path, filled=2)
    child = reserve(ledger, "BUY", attempt="retry", snapshot="cancelled", quantity=5,
                    at="2026-10-12T11:07:04Z")
    with pytest.raises(LedgerError, match=reason):
        ledger.bind_timeout_retry(child.reservation_id, parent.reservation_id,
            requested_quantity=requested, as_of="2026-10-12T11:07:04Z", evidence_id="invalid")
    current = ledger.lookup_reservation(child.reservation_id)
    assert current.retry_parent_id is None and current.retry_requested_quantity is None
