"""Synthetic own-Gameplan commitment gaps; no network or live account state."""
from dataclasses import replace
from decimal import Decimal
import json

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, ACCOUNT
from test_account_gameplan_execution import account_env, run, state, native
from test_gameplan_direction_engine import inputs, build, orders
from test_account_gameplan_authority import env, request, snapshot, submitted, evidence, Clock, SOURCES
from ml.account_gameplan.authority import AccountAuthority, AccountSnapshot, AuthorityBinding, AuthorityError, ReservationRequest
from ml.stock_trader.horizon_ledger import OrderEvidence, FillEvidence


def _first_own_order(account_env):
    fixture = account_env
    original = dict(fixture.signals)
    fixture.signals = {key: value for key, value in original.items() if key[0] == "AAPL"}
    assert run(fixture).submitted_orders == 1
    prior, = native(fixture).snapshot().reservations
    fixture.now += pd.Timedelta(seconds=1)
    fixture.signals = {key: value for key, value in original.items() if key[0] == "NVDA"}
    fixture.order_evidence = (OrderEvidence("own-working-before-portfolio", prior.reservation_id, ACCOUNT,
        fixture.now.isoformat(), prior.broker_order_id, "WORKING", prior.quantity, 0, prior.quantity, ()),)
    return fixture, prior


def test_runtime_sizes_from_own_missing_working_commitment_before_native_reserve(account_env):
    fixture, prior = _first_own_order(account_env)
    assert prior.quantity == 15
    fixture.portfolio_transform = lambda p: replace(p, available_cash=2000.)
    result = run(fixture)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == 1
    records = state(fixture)["reservations"]
    current, = [r for r in records if r["request"]["symbol"] == "NVDA"]
    # Cash buffer applies once: 2000*.95 - missing own 15*100 = 400.
    assert current["request"]["quantity"] == 4
    assert current["status"] == "SUBMITTED"
    assert all(r.status != "REJECTED" for r in native(fixture).snapshot().reservations)
    published = json.loads((result.run_directory / "decisions.json").read_text())
    assert published["prediction_handoff"]["account_execution_capacity"]["cash_available"] == "400"
    assert published["decisions"][0]["portfolio"]["available_cash"] == 2000.
    assert fixture.broker.submissions[-1]["orderLegCollection"][0]["quantity"] == 4


def test_runtime_represented_own_order_does_not_deduct_cash_twice(account_env):
    fixture, prior = _first_own_order(account_env)
    pending = ({"status": "CURRENT", "asset_type": "EQUITY", "instruction": "BUY", "symbol": "AAPL",
        "remaining_quantity": prior.quantity, "filled_quantity": 0, "limit_price": 100.,
        "reserved_cash": prior.quantity * 100., "order_id": prior.broker_order_id},)
    fixture.portfolio_transform = lambda p: replace(p, available_cash=2000.,
        pending_buy_shares={"AAPL": prior.quantity}, broker_working_orders=pending, working_order_count=1)
    result = run(fixture)
    assert result.status == "ORDERS_SUBMITTED", result.error
    current, = [r for r in state(fixture)["reservations"] if r["request"]["symbol"] == "NVDA"]
    assert current["request"]["quantity"] == 19


def test_runtime_newly_reconciled_fill_is_not_reserved_again(account_env):
    fixture, prior = _first_own_order(account_env)
    fixture.held["AAPL"] = prior.quantity
    fixture.order_evidence = (replace(fixture.order_evidence[0], evidence_id="own-filled-before-portfolio",
        status="FILLED", cumulative_filled_quantity=prior.quantity, remaining_quantity=0,
        fills=(FillEvidence("own-first-fill", prior.quantity, 100., fixture.now.isoformat()),)),)
    fixture.portfolio_transform = lambda p: replace(p, available_cash=2000.)
    result = run(fixture)
    assert result.status == "ORDERS_SUBMITTED", result.error
    current, = [r for r in state(fixture)["reservations"] if r["request"]["symbol"] == "NVDA"]
    assert current["request"]["quantity"] == 19


def test_preview_does_not_create_authority_or_reserve(account_env):
    fixture = account_env
    result = run(fixture, execute=False)
    assert result.status == "DRY_RUN_INDEPENDENT_STOCK_DECISIONS"
    assert not (fixture.root / "state/gameplan-trader/authority.sqlite3").exists()
    assert not fixture.broker.submissions


def caps(*, cash="100000", gross="100000", symbols=None, shares=None):
    return {"cash_available": cash, "gross_available": gross,
        "symbol_available": symbols or {"AAPL": "100000"}, "eligible_shares": shares or {"AAPL": 0}}


def test_synthetic_current_quote_quantity_fits_conservative_remaining_cash():
    signals, portfolio = inputs(("AAPL",), probability=.7, ask=110., bid=90.)
    signals = {key: value for key, value in signals.items() if key[1] == "1w"}
    portfolio = replace(portfolio, available_cash=2000.)
    original, = orders(build(signals, portfolio))
    assert original.quantity == 19
    # Deliberately synthetic: buffered cash1900 less missing own15*100.
    conservative = caps(cash="400", gross="128500", symbols={"AAPL": "15000"}, shares={"AAPL": 0})
    repaired, = orders(build(signals, portfolio, execution_capacity=conservative))
    assert repaired.quantity == 4
    assert Decimal(repaired.quantity) * Decimal(str(repaired.limit_price)) <= Decimal("400")
    assert repaired.prediction["calibrated_probability"] == original.prediction["calibrated_probability"]
    assert repaired.prediction["target_window_end"] == original.prediction["target_window_end"]


def test_synthetic_authority_accepts_clipped_order_with_missing_own_commitment(tmp_path):
    # All economics are deliberately synthetic. An own MSFT order remains
    # live; a fill unavailable to this snapshot cannot be assumed early.
    clock = Clock()
    store = AccountAuthority(tmp_path / "own-authority.sqlite3",
        AuthorityBinding(ACCOUNT, "Atlas", {"Atlas": ("AAPL",), "Scout": ("MSFT",)}), clock=clock)
    lease = store.acquire_lease(coordinator_id="Atlas", holder_id="offline-synthetic-capacity")
    first = AccountSnapshot("before-msft", ACCOUNT, "synthetic-plan", "2026-10-02", SOURCES,
        clock().isoformat(), "100000", "100000", "95000", "0", "130000",
        {"AAPL": "0", "MSFT": "0"}, {"AAPL": "15000", "MSFT": "15000"},
        {"AAPL": 0, "MSFT": 0}, evidence_fingerprint="d" * 64,
        identity_before=ACCOUNT, identity_after=ACCOUNT, complete=True)
    store.publish_snapshot(lease.token, first)
    prior = store.reserve(lease.token, ReservationRequest("synthetic-msft", first.snapshot_id,
        first.generation, first.action_date, SOURCES, "Scout", "MSFT", "1w", "synthetic-msft-forecast",
        "BUY", 15, "100", "100", "own-msft-native", "own-msft-allocation"))
    def accept(gate):
        gate()
        return "1001"
    store.submit(lease.token, prior.reservation_id, accept)
    clock.tick()
    later = replace(first, snapshot_id="synthetic-aapl-capture", observed_at=clock().isoformat(),
        cash_available="2000", cash_budget="1900")
    store.publish_snapshot(lease.token, later)
    original = ReservationRequest("synthetic-aapl", later.snapshot_id, later.generation, later.action_date,
        SOURCES, "Atlas", "AAPL", "1w", "synthetic-aapl-forecast", "BUY", 19, "100", "110",
        "own-aapl-native", "own-aapl-allocation")
    with pytest.raises(AuthorityError, match="ACCOUNT_RESERVATION_BUDGET_EXHAUSTED"):
        store.reserve(lease.token, original)
    capacity = store.available_capacity(lease.token, later.snapshot_id)
    assert Decimal(capacity["cash_available"]) == Decimal("400")
    signals, portfolio = inputs(("AAPL",), probability=.7, ask=110., bid=90.)
    signals = {key: value for key, value in signals.items() if key[1] == "1w"}
    portfolio = replace(portfolio, available_cash=2000.)
    selected, = orders(build(signals, portfolio, execution_capacity=capacity))
    accepted = store.reserve(lease.token, replace(original, quantity=selected.quantity))
    assert accepted.status == "RESERVED" and accepted.request["quantity"] == 4
    assert len(store.read_state()["reservations"]) == 2


@pytest.mark.parametrize("field,amount,expected", [("cash_available", "299", 2),
    ("gross_available", "399", 3), ("symbol_available", {"AAPL": "499"}, 4)])
def test_pure_builder_preserves_distinct_authority_budgets(field, amount, expected):
    signals, portfolio = inputs()
    signals = {key: value for key, value in signals.items() if key[1] == "1w"}
    capacity = caps()
    capacity[field] = amount
    selected, = orders(build(signals, portfolio, execution_capacity=capacity))
    assert selected.quantity == expected


def test_wide_spread_batch_decrements_gross_at_ask_and_cash_at_limit():
    signals, portfolio = inputs(("AAPL", "NVDA"), ask=110., bid=90.)
    signals = {key: value for key, value in signals.items() if key[1] == "1w"}
    capacity = caps(cash="2000", gross="990", symbols={"AAPL": "100000", "NVDA": "100000"},
        shares={"AAPL": 0, "NVDA": 0})
    decisions = orders(build(signals, portfolio, execution_capacity=capacity))
    assert sum(d.quantity for d in decisions) == 9
    assert all(d.limit_price == 100. for d in decisions)
    assert sum(Decimal(d.quantity) * Decimal("110") for d in decisions) == Decimal("990")


def test_fractional_ask_uses_authority_cent_ceiling_for_exposure():
    signals, portfolio = inputs(ask=110.001, bid=90.001)
    signals = {key: value for key, value in signals.items() if key[1] == "1w"}
    selected, = orders(build(signals, portfolio, execution_capacity=caps(gross="990.01")))
    assert selected.limit_price == 100.01
    assert selected.quantity == 8
    assert Decimal(selected.quantity) * Decimal("110.01") <= Decimal("990.01")


def test_authority_missing_sell_commitment_caps_sales_and_never_funds_buys():
    signals, portfolio = inputs(("AAPL", "NVDA"))
    signals = {key: replace(value, calibrated_probability=.4 if key[0] == "AAPL" else .7)
        for key, value in signals.items() if key[1] == "1w"}
    portfolio = replace(portfolio, held_shares={"AAPL": 5., "NVDA": 0.}, available_cash=2000.)
    capacity = caps(cash="0", shares={"AAPL": 2, "NVDA": 0},
        symbols={"AAPL": "100000", "NVDA": "100000"})
    decisions = build(signals, portfolio, bearish_sell_capacities={("AAPL", "1w"): 5}, execution_capacity=capacity)
    sold, = orders(decisions)
    assert sold.action == "SELL" and sold.quantity == 2
    assert not orders(decisions, "BUY")


def test_authority_available_capacity_uses_exact_remaining_without_side_effects(env):
    store, token, clock = env
    prior = submitted(store, token, request())
    before = store.read_state()["reservations"]
    capacity = store.available_capacity(token, "snapshot-1")
    assert capacity == {"cash_available": "35", "gross_available": "940",
        "symbol_available": {"AAPL": "940", "MSFT": "1000"}, "eligible_shares": {"AAPL": 100, "MSFT": 100}}
    assert store.read_state()["reservations"] == before
    clock.tick()
    store.reconcile(token, evidence(prior, clock))
    clock.tick()
    store.publish_snapshot(token, snapshot(clock, snapshot_id="after-fill", cash_available="40", cash_budget="38",
        gross_exposure="60", gross_budget="940", accounted_fills={prior.broker_order_id: 6},
        symbol_exposure={"AAPL": "60", "MSFT": "0"}, symbol_budgets={"AAPL": "940", "MSFT": "1000"}))
    assert store.available_capacity(token, "after-fill")["cash_available"] == "38"


def test_authority_available_capacity_withholds_missing_sell(env):
    store, token, clock = env
    submitted(store, token, request(side="SELL", quantity=3))
    assert store.available_capacity(token, "snapshot-1")["eligible_shares"]["AAPL"] == 97
    assert store.available_capacity(token, "snapshot-1")["cash_available"] == "95"


@pytest.mark.parametrize("bad", [None, {}, {"AAPL": -1}, {"AAPL": 1.5}])
def test_builder_rejects_incomplete_or_invalid_authority_shares(bad):
    signals, portfolio = inputs()
    capacity = caps()
    capacity["eligible_shares"] = bad
    with pytest.raises(ValueError, match="capacity"):
        build(signals, portfolio, execution_capacity=capacity)
