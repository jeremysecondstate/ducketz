"""Offline current-authority regressions; temporary ledgers and synthetic quotes."""
from dataclasses import replace

import pytest

from ml.stock_trader.cross_horizon_fallback import autonomous_horizon, policy_for_action_date
from ml.stock_trader.horizon_ledger import HorizonLedger, LedgerError
from test_cross_horizon_fallback_ledger import prepare, portfolio, evidence, ACCOUNT
from test_gameplan_fallback_planning import forecasts, state, prices, entry, POLICY
from ml.gameplan_cash_ledger import project_direction_trades
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


@pytest.mark.parametrize("horizon", ["1h", "4h"])
@pytest.mark.parametrize("saved_fallback", [True, False])
def test_abcl_48_72_percent_sells_all_503_even_with_same_clock_bullish_owners(horizon, saved_fallback):
    frame = forecasts({(horizon, 4): .4872, ("1d", 4): .9, ("1w", 4): .9}, day=DAY)
    snapshot = state({"1h": 20, "4h": 200, "1d": 100, "1w": 176}, free=7)
    snapshot["observed_at"] = DAY+"T10:00:00Z"
    rows, report = project_direction_trades(frame, snapshot, prices(DAY), policy=POLICY, signal_driven=True,
        cross_horizon_fallback_policy=policy_for_action_date("2026-10-01") if saved_fallback else None)
    row = entry(rows, horizon, 4)
    assert row.direction_based_action == "SELL" and row.direction_based_trade_quantity == -503
    assert row.direction_based_reason == "BEARISH_ACCOUNT_WIDE_SELL"
    sale = report["events"][0]
    assert sale["quantity"] == 503 and sale["shares_after"] == 0
    assert report["autonomous_horizons"] == ["1h", "4h"]
    assert "cross_horizon_fallback" not in sale


def test_daily_retains_same_clock_bullish_weekly_donor_protection():
    frame = forecasts({("1d", 4): .4872, ("1w", 4): .9}, day=DAY)
    snapshot = state({"1w": 503})
    snapshot["observed_at"] = DAY+"T10:00:00Z"
    rows, _ = project_direction_trades(frame, snapshot, prices(DAY), policy=POLICY, signal_driven=True,
        cross_horizon_fallback_policy=policy_for_action_date(DAY))
    assert entry(rows, "1d", 4).direction_based_action == "HOLD"
    assert entry(rows, "1d", 4).direction_based_reason == "FALLBACK_NO_ELIGIBLE_LONGER_DONOR"


def test_current_engine_sell_uses_real_quote_and_unrestricted_capacity():
    signals, portfolio_state = inputs(symbols=("ABCL",), probability=.4872, ask=11.94, bid=11.92)
    signal = replace(signals["ABCL", "1h"], target_window_start=START, target_window_end=END, actionable_until=END)
    portfolio_state = replace(portfolio_state, observed_at=NOW, held_shares={"ABCL":503},
        quotes={"ABCL":replace(portfolio_state.quotes["ABCL"], observed_at=NOW)})
    decision = build({("ABCL", "1h"):signal}, portfolio_state, decided_at=NOW,
        bearish_sell_capacities={("ABCL", "1h"):503})[0]
    assert decision.action == "SELL" and decision.quantity == 418  # Existing single-order cap uses current ask.
    assert decision.limit_price == 11.92
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


@pytest.mark.parametrize("horizon", ["1h", "4h"])
@pytest.mark.parametrize("stream", ["atlas-15m", "scout-15m"])
def test_autonomous_projection_can_sell_actual_intraday_lots_but_reserves_pending_sells(horizon, stream):
    frame = forecasts({(horizon, 4): .4872}, day=DAY)
    snapshot = state({"15m":503})
    snapshot["observed_at"] = DAY+"T10:00:00Z"
    snapshot["pending_sell_shares"] = {"AAPL":17}
    intraday = snapshot["ownership"]["active_allocations"][0]
    intraday.update(stream=stream, reserved_sell_shares=17)
    rows, report = project_direction_trades(frame, snapshot, prices(DAY), policy=POLICY, signal_driven=True,
        cross_horizon_fallback_policy=policy_for_action_date(DAY))
    assert entry(rows, horizon, 4).direction_based_trade_quantity == -486
    assert report["ending_positions"] == {"AAPL":17}
    remaining = report["ending_allocations"]
    assert len(remaining) == 1 and remaining[0]["horizon"] == "15m"
    assert remaining[0]["stream"] == stream and remaining[0]["reserved"] == 17
    assert report["cross_horizon_fallback"]["symbol_daily_caps"] == {"AAPL":0}


def test_daily_and_weekly_projection_keep_intraday_lots_owned_and_do_not_borrow_them():
    frame = forecasts({("1d", 4): .4872, ("1w", 4): .4872}, day=DAY)
    snapshot = state({"15m":503})
    snapshot["observed_at"] = DAY+"T10:00:00Z"
    snapshot["ownership"]["active_allocations"][0]["stream"] = "scout-15m"
    rows, report = project_direction_trades(frame, snapshot, prices(DAY), policy=POLICY, signal_driven=True,
        cross_horizon_fallback_policy=policy_for_action_date(DAY))
    assert not report["events"] and report["ending_positions"] == {"AAPL":503}
    assert entry(rows, "1d", 4).direction_based_action == "HOLD"
    assert entry(rows, "1w", 4).direction_based_action == "HOLD"
    assert report["ending_allocations"][0]["horizon"] == "15m"


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
