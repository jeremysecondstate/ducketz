from dataclasses import FrozenInstanceError, replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from ml.stock_trader.horizon_ledger import (
    FillEvidence, HorizonLedger, LedgerError, OrderEvidence, PortfolioEvidence,
)


ACCOUNT = "a" * 64
T0 = "2026-09-08T11:00:00+00:00"
END = "2026-09-08T12:00:00+00:00"


def portfolio(identity="p0", at=T0, held=20, **kwargs):
    return PortfolioEvidence(identity, ACCOUNT, at, {"COST": held}, {"COST": 100},
                             {"COST": 10000}, "b" * 64, **kwargs)


@pytest.fixture
def ledger(tmp_path):
    result = HorizonLedger(tmp_path / "horizons.sqlite3", ACCOUNT)
    assert result.reconcile(portfolio()).ready
    return result


def entry(ledger, **kwargs):
    values = dict(symbol="COST", horizon="1h", forecast_id="forecast-1", target_start=T0,
                  target_end=END, quantity=10, limit_price=100, snapshot_id="p0",
                  idempotency_key="entry-1", batch_id="batch-1", as_of="2026-09-08T11:00:10+00:00")
    values.update(kwargs)
    return ledger.reserve_entry(**values)


def order(reservation, *, identity="order-1", at="2026-09-08T11:00:20+00:00",
          status="FILLED", quantity=None, fills=None, broker_id=None):
    count = reservation.quantity if quantity is None else quantity
    if fills is None:
        fills = (FillEvidence(f"fill-{reservation.reservation_id}", count, 100, at),) if count else ()
    return OrderEvidence(identity, reservation.reservation_id, ACCOUNT, at,
        broker_id or f"broker-{reservation.reservation_id}", status, reservation.quantity, count,
        0 if status in {"FILLED", "CANCELLED", "REJECTED"} else reservation.quantity - count, fills)


def filled_entry(ledger, **kwargs):
    reservation = entry(ledger, **kwargs)
    evidence = order(reservation)
    assert ledger.reconcile(portfolio("p1", "2026-09-08T11:00:21+00:00", 20 + reservation.quantity),
                            order_evidence=(evidence,)).ready
    return reservation


def fresh_exit(ledger, held=30, at=END):
    assert ledger.reconcile(portfolio("exit-snapshot", at, held)).ready


def exit_order(ledger, allocation, **kwargs):
    args = dict(allocation_id=allocation, quantity=10, limit_price=100, snapshot_id="exit-snapshot",
                idempotency_key="exit-1", batch_id="exit-batch", as_of=END)
    args.update(kwargs)
    return ledger.reserve_exit(**args)


def test_reservations_survive_restart_and_are_exact_once(ledger):
    reservation = entry(ledger)
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert entry(reopened) == reservation
    assert len(reopened.snapshot().reservations) == 1
    with pytest.raises(LedgerError, match="IDEMPOTENCY"):
        entry(reopened, quantity=9)
    with pytest.raises(FrozenInstanceError):
        reopened.snapshot().allocations[0].filled_shares = 1


def test_one_active_allocation_and_never_reenter_a_cancelled_forecast(ledger):
    reservation = entry(ledger, horizon="1w", target_end="2026-09-15T00:00:00+00:00")
    with pytest.raises(LedgerError, match="ACTIVE_ALLOCATION"):
        entry(ledger, horizon="1w", forecast_id="next-nightly-week", idempotency_key="entry-2")
    assert ledger.reconcile(portfolio("cancel-p", "2026-09-08T11:00:21+00:00"),
        order_evidence=(order(reservation, status="CANCELLED", quantity=0),)).ready
    assert ledger.snapshot().allocations[0].status == "CLOSED"
    with pytest.raises(LedgerError, match="FORECAST_ALREADY_RESERVED"):
        entry(ledger, horizon="1w", idempotency_key="new-attempt", snapshot_id="cancel-p",
              as_of="2026-09-08T11:00:22+00:00")


def test_manual_buys_accumulate_and_bearish_sale_keeps_each_forecast_identity(ledger):
    first = filled_entry(ledger, allow_accumulation=True)
    assert ledger.reconcile(portfolio("second-start", END, 30)).ready
    second = entry(ledger, allow_accumulation=True, forecast_id="forecast-2", target_start=END,
        target_end="2026-09-08T13:00:00+00:00", snapshot_id="second-start", as_of=END,
        idempotency_key="entry-2", batch_id="batch-2")
    assert second.allocation_id == first.allocation_id
    assert second.forecast_id == "forecast-2" and second.target_start == END
    assert ledger.reconcile(portfolio("second-filled", "2026-09-08T12:00:21+00:00", 40),
        order_evidence=(order(second, identity="second-fill", at="2026-09-08T12:00:20+00:00"),)).ready
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert reopened.snapshot().allocations[0].filled_shares == 20
    assert {r.forecast_id for r in reopened.snapshot().reservations} == {"forecast-1", "forecast-2"}
    with pytest.raises(LedgerError, match="FORECAST_ALREADY_RESERVED"):
        entry(reopened, allow_accumulation=True, forecast_id="forecast-2", target_start=END,
              target_end="2026-09-08T13:00:00+00:00", snapshot_id="second-filled",
              as_of="2026-09-08T12:00:22+00:00", idempotency_key="duplicate-second", batch_id="new-batch")
    sell = reopened.reserve_direction_exit(symbol="COST", horizon="1h", forecast_id="bearish-3",
        target_start=END, target_end="2026-09-08T13:00:00+00:00", quantity=10, limit_price=100,
        snapshot_id="second-filled", as_of="2026-09-08T12:00:22+00:00",
        idempotency_key="sell-3", batch_id="sell-batch")
    assert sell.forecast_id == "bearish-3" and sell.allocation_id == first.allocation_id
    assert reopened.reconcile(portfolio("sold", "2026-09-08T12:00:24+00:00", 30),
        order_evidence=(order(sell, identity="sell-fill", at="2026-09-08T12:00:23+00:00"),)).ready
    assert reopened.snapshot().allocations[0].filled_shares == 10


@pytest.mark.parametrize("horizon,max_shares", [("1h", 10), ("4h", 20), ("1d", 30), ("1w", 40)])
def test_allocation_weights_stay_inside_the_shared_symbol_budget(ledger, horizon, max_shares):
    with pytest.raises(LedgerError, match="WEIGHTED_BUDGET"):
        entry(ledger, horizon=horizon, quantity=max_shares + 1)
    assert entry(ledger, horizon=horizon, quantity=max_shares).quantity == max_shares


def test_accepted_submission_does_not_create_shares_and_unknown_blocks(ledger):
    reserved = entry(ledger)
    accepted = ledger.mark_submission(reserved.reservation_id, status="SUBMITTED",
        broker_order_id="accepted-order", evidence_id="accepted", observed_at="2026-09-08T11:00:11+00:00")
    assert accepted.filled_quantity == 0
    result = ledger.reconcile(portfolio("no-fills", "2026-09-08T11:00:12+00:00"))
    assert not result.ready and any("UNRECONCILED_SUBMITTED" in item for item in result.reasons)
    assert ledger.snapshot().allocations[0].filled_shares == 0


def test_unknown_submission_retains_reserved_inventory_until_broker_evidence(ledger):
    reserved = entry(ledger)
    ledger.mark_submission(reserved.reservation_id, status="UNKNOWN", evidence_id="uncertain",
                           observed_at="2026-09-08T11:00:11+00:00")
    with pytest.raises(LedgerError, match="UNKNOWN_SUBMISSION"):
        entry(ledger, horizon="4h", idempotency_key="same-batch-second-entry", forecast_id="another-horizon")
    with pytest.raises(LedgerError, match="BROKER_RECONCILIATION"):
        ledger.mark_submission(reserved.reservation_id, status="REJECTED", evidence_id="guess",
                               observed_at="2026-09-08T11:00:12+00:00")
    assert not ledger.reconcile(portfolio("unknown", "2026-09-08T11:00:15+00:00")).ready
    assert ledger.pending_reservations()[0].reserved_quantity == 10
    assert ledger.reconcile(portfolio("known-cancel", "2026-09-08T11:00:21+00:00"),
        order_evidence=(order(reserved, status="CANCELLED", quantity=0),)).ready
    assert not ledger.pending_reservations()


def test_partial_fill_repeated_cumulative_evidence_and_terminal_cancel(ledger):
    reserved = entry(ledger)
    partial = order(reserved, status="PARTIAL", quantity=4)
    assert ledger.reconcile(portfolio("partial", "2026-09-08T11:00:21+00:00", 24),
                            order_evidence=(partial,)).ready
    assert ledger.reconcile(portfolio("repeat", "2026-09-08T11:00:24+00:00", 24),
        order_evidence=(replace(partial, evidence_id="repeat-order", observed_at="2026-09-08T11:00:23+00:00"),)).ready
    assert ledger.snapshot().allocations[0].filled_shares == 4
    assert ledger.snapshot().allocations[0].reserved_buy_shares == 6
    cancelled = replace(partial, evidence_id="cancel-order", observed_at="2026-09-08T11:00:25+00:00",
                        status="CANCELLED", remaining_quantity=0)
    assert ledger.reconcile(portfolio("cancelled", "2026-09-08T11:00:26+00:00", 24),
                            order_evidence=(cancelled,)).ready
    assert ledger.snapshot().allocations[0].reserved_buy_shares == 0
    fresh_exit(ledger, held=24)
    assert ledger.due_exits(as_of=END, snapshot_id="exit-snapshot")[0].quantity == 4
    with pytest.raises(LedgerError, match="OWN_UNRESERVED"):
        exit_order(ledger, reserved.allocation_id, quantity=5)


def test_missing_fill_detail_or_regression_is_rejected_atomically(ledger):
    reserved = entry(ledger)
    with pytest.raises(LedgerError, match="COMPLETE_PER_ORDER"):
        ledger.reconcile(portfolio("bad", "2026-09-08T11:00:21+00:00", 30),
                         order_evidence=(order(reserved, fills=()),))
    assert ledger.lookup_reservation(reserved.reservation_id).status == "RESERVED"
    assert ledger.reconcile(portfolio("filled", "2026-09-08T11:00:21+00:00", 30),
                            order_evidence=(order(reserved),)).ready
    with pytest.raises(LedgerError, match="TERMINAL_ORDER_STATUS"):
        ledger.reconcile(portfolio("regressed", "2026-09-08T11:00:24+00:00", 30),
            order_evidence=(order(reserved, identity="regressed-order", at="2026-09-08T11:00:23+00:00",
                                  status="PARTIAL", quantity=9),))
    assert ledger.snapshot().allocations[0].filled_shares == 10


def test_fill_identity_cannot_be_reused_for_another_horizon(ledger):
    first = entry(ledger)
    second = entry(ledger, horizon="4h", forecast_id="4h-forecast", idempotency_key="4h-entry")
    shared = FillEvidence("shared-fill-id", 10, 100, "2026-09-08T11:00:20+00:00")
    with pytest.raises(LedgerError, match="FILL_ID_REUSED"):
        ledger.reconcile(portfolio("conflicting", "2026-09-08T11:00:21+00:00", 40),
            order_evidence=(order(first, fills=(shared,)), order(second, identity="order-2", fills=(shared,))))
    assert all(a.filled_shares == 0 for a in ledger.snapshot().allocations)


def test_exits_only_own_shares_and_reserved_partial_sells_not_manual_shares(ledger):
    bought = filled_entry(ledger)
    fresh_exit(ledger)
    assert ledger.due_exits(as_of=END, snapshot_id="exit-snapshot")[0].quantity == 10
    with pytest.raises(LedgerError, match="OWN_UNRESERVED"):
        exit_order(ledger, bought.allocation_id, quantity=11)
    sell = exit_order(ledger, bought.allocation_id)
    assert exit_order(ledger, bought.allocation_id).reservation_id == sell.reservation_id
    evidence = order(sell, identity="sell-evidence", at="2026-09-08T12:00:10+00:00", status="PARTIAL", quantity=4)
    assert ledger.reconcile(portfolio("partial-sell", "2026-09-08T12:00:11+00:00", 26),
                            order_evidence=(evidence,)).ready
    allocation = ledger.snapshot().allocations[0]
    assert (allocation.filled_shares, allocation.reserved_sell_shares) == (6, 6)
    plan = ledger.due_exits(as_of="2026-09-08T12:00:12+00:00", snapshot_id="partial-sell")[0]
    assert plan.quantity == 0 and not plan.ready


@pytest.mark.parametrize("remaining", [29, 9])
def test_manual_reduction_persistently_blocks_without_reassigning_shares(ledger, remaining):
    filled_entry(ledger)
    result = ledger.reconcile(portfolio("manual", "2026-09-08T11:00:30+00:00", remaining))
    assert not result.ready
    assert ledger.snapshot().allocations[0].filled_shares == 10
    assert ledger.snapshot().persistent_blocks
    restored = ledger.reconcile(portfolio("restored", "2026-09-08T11:00:31+00:00", 30))
    assert not restored.ready


def test_account_mismatch_and_raw_account_number_never_enter_ledger(ledger):
    with pytest.raises(LedgerError, match="FINGERPRINT_MISMATCH"):
        HorizonLedger(ledger.path, "c" * 64)
    with pytest.raises(LedgerError, match="fingerprint"):
        HorizonLedger(ledger.path, "123456789")
    with pytest.raises(LedgerError, match="FINGERPRINT_MISMATCH"):
        ledger.reconcile(replace(portfolio("wrong"), account_fingerprint="c" * 64))
    assert not ledger.snapshot().allocations


def test_stale_portfolio_or_working_order_fails_closed(ledger):
    with pytest.raises(LedgerError, match="STALE_OR_FUTURE"):
        entry(ledger, as_of="2026-09-08T11:01:01+00:00")
    reserved = entry(ledger)
    assert ledger.reconcile(portfolio("working", "2026-09-08T11:00:21+00:00"),
                            order_evidence=(order(reserved, status="WORKING", quantity=0),)).ready
    result = ledger.reconcile(portfolio("old-order", "2026-09-08T11:01:21+00:00"))
    assert not result.ready and any("STALE_WORKING_ORDER" in reason for reason in result.reasons)


def test_closing_lead_applies_only_at_1700_and_preserves_target(ledger):
    end = "2026-09-09T00:00:00+00:00"
    bought = filled_entry(ledger, horizon="1d", target_end=end)
    now = "2026-09-08T23:59:00+00:00"
    fresh_exit(ledger, at=now)
    assert not ledger.due_exits(as_of=now, snapshot_id="exit-snapshot")
    plan = ledger.due_exits(as_of=now, snapshot_id="exit-snapshot", exit_lead_seconds=60)[0]
    assert plan.ready and plan.target_end == end
    with pytest.raises(LedgerError, match="NOT_DUE"):
        exit_order(ledger, bought.allocation_id, as_of=now)
    assert exit_order(ledger, bought.allocation_id, as_of=now, exit_lead_seconds=60).quantity == 10
    assert not HorizonLedger._exit_due(END, "2026-09-08T11:59:00+00:00", 60)


def test_concurrent_duplicate_entry_has_one_durable_reservation(ledger):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: entry(HorizonLedger(ledger.path, ACCOUNT)), range(2)))
    assert results[0].reservation_id == results[1].reservation_id
    assert len(ledger.snapshot().reservations) == 1


def test_cancellation_is_one_attempt_and_does_not_release_a_partial_order(ledger):
    reserved = entry(ledger)
    partial = order(reserved, status="PARTIAL", quantity=4)
    assert ledger.reconcile(portfolio("partial", "2026-09-08T11:00:21+00:00", 24),
                            order_evidence=(partial,)).ready
    with pytest.raises(LedgerError, match="GRACE_NOT_EXPIRED"):
        ledger.reserve_cancellation(reserved.reservation_id, as_of="2026-09-08T11:04:59+00:00", evidence_id="early")
    assert ledger.reserve_cancellation(reserved.reservation_id, as_of="2026-09-08T11:05:00+00:00", evidence_id="cancel")
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert not reopened.reserve_cancellation(reserved.reservation_id, as_of="2026-09-08T11:06:00+00:00", evidence_id="retry")
    state = reopened.lookup_reservation(reserved.reservation_id)
    assert state.cancel_requested_at == "2026-09-08T11:05:00+00:00"
    assert state.status == "PARTIAL" and state.reserved_quantity == 6
    assert any(item.kind == "cancellation-reservation" for item in reopened.history())


def test_unknown_submission_cannot_be_cancelled_by_guess(ledger):
    reserved = entry(ledger)
    with pytest.raises(LedgerError, match="KNOWN_WORKING_ENTRY"):
        ledger.reserve_cancellation(reserved.reservation_id, as_of="2026-09-08T11:05:00+00:00", evidence_id="cancel")


def test_final_exit_closes_only_its_allocation_and_releases_next_forecast(ledger):
    bought = filled_entry(ledger)
    fresh_exit(ledger)
    sell = exit_order(ledger, bought.allocation_id)
    evidence = order(sell, identity="sell-filled", at="2026-09-08T12:00:10+00:00")
    assert ledger.reconcile(portfolio("closed", "2026-09-08T12:00:11+00:00", 20), order_evidence=(evidence,)).ready
    assert ledger.snapshot().allocations[0].status == "CLOSED"
    assert ledger.snapshot().allocations[0].filled_shares == 0
    assert entry(ledger, forecast_id="next-hour", target_start=END, target_end="2026-09-08T13:00:00+00:00",
                 snapshot_id="closed", idempotency_key="next-entry", batch_id="next-batch",
                 as_of="2026-09-08T12:00:12+00:00").quantity == 10


def test_1300_entry_preserves_ten_minute_cancellation_grace(ledger):
    start = "2026-09-08T20:00:00+00:00"
    assert ledger.reconcile(portfolio("1300", start)).ready
    reservation = entry(ledger, target_start=start, target_end="2026-09-08T21:00:00+00:00",
                        snapshot_id="1300", as_of="2026-09-08T20:00:10+00:00")
    ledger.mark_submission(reservation.reservation_id, status="SUBMITTED", broker_order_id="13-order",
                           observed_at="2026-09-08T20:00:11+00:00", evidence_id="13-submitted")
    with pytest.raises(LedgerError, match="GRACE_NOT_EXPIRED"):
        ledger.reserve_cancellation(reservation.reservation_id, as_of="2026-09-08T20:09:59+00:00", evidence_id="too-soon")
    assert ledger.reserve_cancellation(reservation.reservation_id, as_of="2026-09-08T20:10:00+00:00", evidence_id="13-cancel")


def test_buy_fill_between_order_history_and_account_read_waits_for_evidence(ledger):
    reserved = entry(ledger)
    stale_working = order(reserved, status="WORKING", quantity=0)
    result = ledger.reconcile(portfolio("race", "2026-09-08T11:00:21+00:00", 30),
                              order_evidence=(stale_working,))
    assert not result.ready and not ledger.snapshot().persistent_blocks
    assert ledger.snapshot().allocations[0].filled_shares == 0
    current_fill = order(reserved, identity="known-fill", at="2026-09-08T11:00:22+00:00")
    assert ledger.reconcile(portfolio("resolved", "2026-09-08T11:00:23+00:00", 30),
                            order_evidence=(current_fill,)).ready
    assert ledger.snapshot().allocations[0].filled_shares == 10


def test_sell_fill_between_order_history_and_account_read_is_not_manual_reduction(ledger):
    bought = filled_entry(ledger)
    fresh_exit(ledger)
    sell = exit_order(ledger, bought.allocation_id)
    stale_working = order(sell, identity="working-sell", at="2026-09-08T12:00:10+00:00", status="WORKING", quantity=0)
    result = ledger.reconcile(portfolio("race", "2026-09-08T12:00:11+00:00", 20),
                              order_evidence=(stale_working,))
    assert not result.ready and not ledger.snapshot().persistent_blocks
    assert ledger.snapshot().allocations[0].filled_shares == 10
    current_fill = order(sell, identity="sell-fill", at="2026-09-08T12:00:12+00:00")
    assert ledger.reconcile(portfolio("resolved", "2026-09-08T12:00:13+00:00", 20),
                            order_evidence=(current_fill,)).ready
    assert ledger.snapshot().allocations[0].filled_shares == 0


def test_account_lag_after_confirmed_buy_does_not_establish_false_manual_baseline(ledger):
    reserved = entry(ledger)
    result = ledger.reconcile(portfolio("lag", "2026-09-08T11:00:21+00:00", 20),
                              order_evidence=(order(reserved),))
    assert not result.ready and not ledger.snapshot().persistent_blocks
    assert ledger.snapshot().allocations[0].filled_shares == 10
    assert ledger.reconcile(portfolio("caught-up", "2026-09-08T11:00:22+00:00", 30)).ready


def test_account_lag_after_confirmed_sell_waits_without_restoring_owned_shares(ledger):
    bought = filled_entry(ledger)
    fresh_exit(ledger)
    sell = exit_order(ledger, bought.allocation_id)
    result = ledger.reconcile(portfolio("lag", "2026-09-08T12:00:11+00:00", 30),
        order_evidence=(order(sell, identity="sold", at="2026-09-08T12:00:10+00:00"),))
    assert not result.ready and not ledger.snapshot().persistent_blocks
    assert ledger.snapshot().allocations[0].filled_shares == 0
    assert ledger.reconcile(portfolio("caught-up", "2026-09-08T12:00:12+00:00", 20)).ready
