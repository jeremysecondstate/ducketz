"""Offline current-authority regressions; temporary ledgers and synthetic quotes."""
from dataclasses import replace

import pytest

from ml.stock_trader.cross_horizon_fallback import autonomous_horizon, policy_for_action_date
from ml.stock_trader.horizon_ledger import HorizonLedger, LedgerError
from test_cross_horizon_fallback_ledger import prepare, portfolio, evidence, ACCOUNT
from test_gameplan_direction_engine import inputs, build

DAY = "2026-10-19"
START = DAY + "T11:00:00+00:00"
END = DAY + "T12:00:00+00:00"
NOW = DAY + "T11:00:10+00:00"


def opening(tmp_path, *, donors=None, free=7, own=20):
    ledger = prepare(tmp_path, donors=donors or {"4h": 200, "1d": 100, "1w": 176},
                     free=free, own=own, freeze=False)
    held = sum(a.filled_shares for a in ledger.snapshot().allocations) + free
    assert ledger.reconcile(portfolio("current-opening", START, held)).ready
    return ledger, held


def sell(ledger, quantity, *, horizon="1h", forecast="current-bearish", pending=0, key="sell", batch="current"):
    return ledger.reserve_direction_exit(symbol="COST", horizon=horizon, forecast_id=forecast,
        target_start=START, target_end=END, quantity=quantity, limit_price=100,
        snapshot_id="current-opening", idempotency_key=key, batch_id=batch,
        as_of=NOW, pending_sell_shares=pending)


def test_authority_is_exactly_hourly_and_four_hour_from_current_effective_date():
    for horizon in ("1h", "4h"):
        assert not autonomous_horizon(horizon, "2026-10-18")
        assert autonomous_horizon(horizon, DAY)
    assert not any(autonomous_horizon(h, DAY) for h in ("1d", "1w", "15m"))


@pytest.mark.parametrize("horizon", ["1h", "4h"])
def test_sell_debits_own_all_donors_and_unallocated_without_quota(tmp_path, horizon):
    ledger, held = opening(tmp_path)
    order = sell(ledger, held, horizon=horizon)
    assert order.quantity == 503 and order.fallback_policy_version is None
    assert order.horizon == horizon
    planned = ledger.direction_sell_attribution(order.reservation_id)
    assert sum(row["quantity"] for row in planned["slices"]) == 503
    assert len(planned["slices"]) >= 4
    original = {a.allocation_id: a.forecast_id for a in ledger.snapshot().allocations}
    assert ledger.reconcile(portfolio("fully-sold", DAY+"T11:00:21+00:00", 0),
        order_evidence=(evidence(order, at=DAY+"T11:00:20+00:00"),)).ready
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert all(a.filled_shares == 0 and a.reserved_sell_shares == 0 for a in reopened.snapshot().allocations)
    assert all(a.forecast_id == original[a.allocation_id] for a in reopened.snapshot().allocations)
    actual = reopened.direction_sell_attribution(order.reservation_id)
    assert sum(row["quantity"] for row in actual["fills"]) == 503
    assert sell(reopened, held, horizon=horizon) == reopened.lookup_reservation(order.reservation_id)


def test_partial_fill_cancel_releases_unsold_owner_slices_and_unallocated_shares(tmp_path):
    ledger, held = opening(tmp_path)
    order = sell(ledger, held)
    partial = evidence(order, at=DAY+"T11:00:20+00:00", filled=33, status="PARTIAL")
    assert ledger.reconcile(portfolio("partial-current", DAY+"T11:00:21+00:00", held-33),
        order_evidence=(partial,)).ready
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == 470
    terminal = replace(partial, evidence_id="current-cancel", observed_at=DAY+"T11:00:23+00:00",
                       status="CANCELLED", remaining_quantity=0)
    assert ledger.reconcile(portfolio("cancel-current", DAY+"T11:00:24+00:00", held-33),
        order_evidence=(terminal,)).ready
    allocations = ledger.snapshot().allocations
    assert sum(a.reserved_sell_shares for a in allocations) == 0
    assert sum(a.filled_shares for a in allocations) == 463  # Seven unsold opening shares are free again.
    assert sum(row["quantity"] for row in ledger.direction_sell_attribution(order.reservation_id)["fills"]) == 33


def test_account_capacity_and_atomic_reservations_prevent_double_selling(tmp_path):
    ledger, held = opening(tmp_path)
    first = sell(ledger, 350)
    with pytest.raises(LedgerError, match="EXCEEDS_AVAILABLE"):
        sell(ledger, 154, horizon="4h", forecast="second", key="second")
    second = sell(ledger, 153, horizon="4h", forecast="second", key="second")
    assert first.reserved_quantity + second.reserved_quantity == held
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == held


def test_external_pending_sale_reduces_capacity_without_protecting_all_donor_shares(tmp_path):
    ledger, held = opening(tmp_path)
    with pytest.raises(LedgerError, match="EXCEEDS_AVAILABLE"):
        sell(ledger, held, pending=17)
    assert sell(ledger, held-17, pending=17).quantity == 486


def test_pending_own_buy_does_not_veto_sale_of_existing_actual_stock(tmp_path):
    ledger, held = opening(tmp_path, own=0)
    ledger.reserve_entry(symbol="COST", horizon="1h", forecast_id="pending-purchase",
        target_start=START, target_end=END, quantity=1, limit_price=100,
        snapshot_id="current-opening", idempotency_key="pending-purchase", batch_id="current", as_of=NOW)
    assert sell(ledger, held).quantity == held


@pytest.mark.parametrize("horizon", ["1d", "1w"])
def test_daily_weekly_cannot_sell_other_owners_current_stock(tmp_path, horizon):
    ledger, held = opening(tmp_path)
    with pytest.raises(LedgerError, match="EXCEEDS_AVAILABLE"):
        sell(ledger, held, horizon=horizon)






def test_current_engine_sell_uses_real_quote_and_unrestricted_capacity():
    signals, portfolio_state = inputs(symbols=("ABCL",), probability=.48, ask=20.1, bid=19.9)
    signal = replace(signals["ABCL", "1h"], target_window_start=START, target_window_end=END, actionable_until=END)
    portfolio_state = replace(portfolio_state, observed_at=NOW, held_shares={"ABCL":503},
        quotes={"ABCL":replace(portfolio_state.quotes["ABCL"], observed_at=NOW)})
    decision = build({("ABCL", "1h"):signal}, portfolio_state, decided_at=NOW,
        bearish_sell_capacities={("ABCL", "1h"):503})[0]
    assert decision.action == "SELL" and decision.quantity == 248  # Existing single-order cap uses current ask.
    assert decision.limit_price == 20.0
    assert "ownership_policy_version" in decision.prediction


def test_daily_fallback_keeps_cap_but_only_borrows_actual_stock_left_after_autonomous_drafts(tmp_path):
    ledger, _ = opening(tmp_path, donors={"1w":503}, free=0, own=0)
    ledger.freeze_fallback_day(action_date=DAY, policy=policy_for_action_date(DAY),
        source_fingerprint="f"*64, snapshot_id="current-opening", as_of=NOW)
    planned = ledger.fallback_direction_plan(symbol="COST", horizon="1d", forecast_id="daily-after-hourly",
        target_start=START, target_end=END, action_date=DAY, policy=policy_for_action_date(DAY),
        source_fingerprint="f"*64, snapshot_id="current-opening", as_of=NOW,
        reserved_inventory_shares={"1w":480})
    assert planned["eligible"] and planned["maximum_quantity"] == 23
    assert planned["donor_daily_cap"] == 251 and planned["donor_used"] == 0






def test_autonomous_actual_fill_debits_existing_intraday_owner_without_rewriting_identity(tmp_path):
    import sqlite3
    ledger, held = opening(tmp_path, donors={"1w":503}, free=0, own=0)
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE allocations SET horizon='15m' WHERE horizon='1w'")
    original = ledger.snapshot().allocations[0]
    order = sell(ledger, held)
    slices = ledger.direction_sell_attribution(order.reservation_id)["slices"]
    assert slices == [{"allocation":original.allocation_id, "quantity":503, "ordinal":0}]
    assert ledger.reconcile(portfolio("intraday-sold", DAY+"T11:00:21+00:00", 0),
        order_evidence=(evidence(order, at=DAY+"T11:00:20+00:00"),)).ready
    owner = next(a for a in ledger.snapshot().allocations if a.allocation_id == original.allocation_id)
    assert owner.horizon == "15m" and owner.forecast_id == original.forecast_id and owner.filled_shares == 0


def test_gameplan_and_intraday_sell_slices_coexist_without_double_counting(tmp_path):
    from test_intraday_native_ledger import (
        legacy_filled_lots, reserve as reserve_intraday, portfolio as observed, evidence as order_evidence,
    )
    ledger = HorizonLedger(tmp_path / "mixed-slices.sqlite3", ACCOUNT)
    legacy_filled_lots(ledger)
    intraday = reserve_intraday(ledger, "scout-partial", side="SELL", quantity=8, stream="scout-15m")
    first_fill = order_evidence(intraday, "intraday-partial", filled=3, status="PARTIAL")
    assert ledger.reconcile(observed("intraday-partial", "2026-10-12T17:15:05+00:00", held=9),
                            order_evidence=(first_fill,)).ready
    current = replace(first_fill, evidence_id="intraday-current", observed_at=START)
    assert ledger.reconcile(observed("current-mixed", START, held=9), order_evidence=(current,)).ready
    original_owners = {a.allocation_id: a.forecast_id for a in ledger.snapshot().allocations}
    sale = dict(symbol="AAPL", horizon="1h", forecast_id="current-gameplan", target_start=START,
        target_end=END, limit_price=10, snapshot_id="current-mixed", batch_id="current-mixed", as_of=NOW,
        pending_sell_shares=5)
    with pytest.raises(LedgerError, match="EXCEEDS_AVAILABLE"):
        ledger.reserve_direction_exit(**sale, quantity=5, idempotency_key="too-many")
    gameplan = ledger.reserve_direction_exit(**sale, quantity=4, idempotency_key="gameplan-current")
    assert sum(a.reserved_sell_shares for a in ledger.snapshot().allocations) == 9
    gp_fill = order_evidence(gameplan, "gameplan-partial", filled=2, status="PARTIAL", at=DAY+"T11:00:20+00:00")
    current = replace(current, evidence_id="intraday-still-working", observed_at=DAY+"T11:00:20+00:00")
    assert ledger.reconcile(observed("mixed-filled", DAY+"T11:00:21+00:00", held=7),
                            order_evidence=(current, gp_fill)).ready
    reopened = HorizonLedger(ledger.path, ACCOUNT)
    assert sum(a.filled_shares for a in reopened.snapshot().allocations) == 7
    assert sum(a.reserved_sell_shares for a in reopened.snapshot().allocations) == 7
    assert sum(row["quantity"] for row in reopened.intraday_sell_attribution(intraday.reservation_id)["fills"]) == 3
    assert sum(row["quantity"] for row in reopened.direction_sell_attribution(gameplan.reservation_id)["fills"]) == 2
    terminal = replace(current, evidence_id="intraday-cancelled", status="CANCELLED", remaining_quantity=0,
                       observed_at=DAY+"T11:00:23+00:00")
    gp_current = replace(gp_fill, evidence_id="gameplan-still-working", observed_at=DAY+"T11:00:23+00:00")
    assert reopened.reconcile(observed("intraday-released", DAY+"T11:00:24+00:00", held=7),
                              order_evidence=(terminal, gp_current)).ready
    state = reopened.snapshot()
    assert sum(a.reserved_sell_shares for a in state.allocations) == 2
    assert all(a.forecast_id == original_owners[a.allocation_id] for a in state.allocations if a.allocation_id in original_owners)
    assert reopened.reservation_stream(intraday.reservation_id) == "scout-15m"
    assert reopened.reservation_stream(gameplan.reservation_id) == "gameplan"
