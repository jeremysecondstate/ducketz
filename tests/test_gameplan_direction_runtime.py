"""New policy integration and opening inventory release, using synthetic brokers."""
from dataclasses import replace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, _fixed_signals, _decisions, ACCOUNT, NOW, SYMBOLS
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence, OrderEvidence, FillEvidence
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


def prepare(env, monkeypatch, *, probability=.3):
    env.signals = {key: replace(signal, calibrated_probability=probability) for key, signal in _fixed_signals().items()}
    env.held = {symbol: 1. for symbol in SYMBOLS}
    original_capture = runtime.capture_portfolio_state
    def capture(broker, *, literal_cash_only=False, use_actual_quote_timestamps=False, **kwargs):
        assert literal_cash_only is True
        assert use_actual_quote_timestamps is True
        return original_capture(broker, **kwargs)
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    monkeypatch.setattr(runtime, "load_current_enrichment_model", lambda root: pytest.fail("The opt-in Gameplan strategy does not load learned sizing"))


def run(env, *, execute=True):
    return runtime.run_independent_stock_trader_once(env.root, execute=execute, session=env.broker,
        runtime_clock=lambda: env.now, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY)


def test_quote_that_ages_out_before_submission_is_not_disguised_by_fresh_account_capture(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original_capture = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        return replace(result, quotes={symbol: replace(quote, observed_at=(env.now - pd.Timedelta(seconds=59)).isoformat())
                                      for symbol, quote in result.quotes.items()})
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    env.broker.before_post_hook = lambda: setattr(env, "now", env.now + pd.Timedelta(seconds=2))
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK"
    assert "CURRENT_QUOTE_TOO_OLD_FOR_SUBMISSION" in result.error
    assert env.broker.submissions == []


def test_seven_opening_bearish_sales_use_all_held_stocks_once_and_create_no_fabricated_buys(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch)
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == len(env.broker.submissions) == 7
    assert all(payload["orderLegCollection"][0]["instruction"] == "SELL" for payload in env.broker.submissions)
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    state = ledger.snapshot()
    assert len(state.allocations) == len(state.reservations) == 7
    assert all(order.side == "SELL" and order.quantity == 1 and order.filled_quantity == 0 for order in state.reservations)
    assert all(allocation.filled_shares == 1 and allocation.reserved_sell_shares == 1 for allocation in state.allocations)
    assert sum(row["quantity"] for row in _decisions(result)["decisions"]) == 7
    assert _decisions(result)["prediction_handoff"]["planning_ranges_have_execution_authority"] is False


def test_current_market_outside_gameplan_estimates_still_prices_and_sizes_live_decisions(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.5471)
    env.signals = {("SNDK", "1h"): replace(env.signals["SNDK", "1h"], enrichment_feature_values={
        "planned_price_low": 1745.95, "planned_price_high": 1752.96,
        "projected_cash_after_low": 0., "projected_cash_after_high": 1.})}
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == 1
    decision = next(row for row in _decisions(result)["decisions"] if row["quantity"])
    assert decision["action"] == "BUY" and decision["quantity"] == 15 and decision["limit_price"] == 100.


def test_no_execution_request_only_publishes_decisions_without_assigning_stock(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch)
    result = run(env, execute=False)
    assert result.status == "DRY_RUN_INDEPENDENT_STOCK_DECISIONS"
    assert result.selected_orders == 7 and result.submitted_orders == 0
    assert env.broker.submissions == []
    state = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot()
    assert state.allocations == state.reservations == ()


def test_final_gate_rejection_returns_unsold_manual_stock_to_free_inventory(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch)
    env.broker.before_post_hook = lambda: setattr(env, "active", False)
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK"
    assert env.broker.submissions == []
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    state = ledger.snapshot()
    assert len(state.reservations) == 1 and state.reservations[0].status == "REJECTED"
    assert state.allocations[0].filled_shares == 0 and state.allocations[0].status == "CLOSED"
    assert any(record.kind == "gameplan-unsold-opening-stock-release" for record in ledger.history())


def test_unknown_direction_submission_keeps_stock_reserved_and_is_not_retried(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch)
    env.broker.unknown_submission = True
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_AFTER_ERROR" and len(env.broker.submissions) == 1
    ledger = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    state = ledger.snapshot()
    assert state.reservations[0].status == "UNKNOWN" and state.reservations[0].reserved_quantity == 1
    assert state.allocations[0].filled_shares == 1 and state.allocations[0].status == "ACTIVE"
    env.now += pd.Timedelta(seconds=1)
    assert run(env).submitted_orders == 0 and len(env.broker.submissions) == 1
    assert not any(record.kind == "gameplan-unsold-opening-stock-release" for record in ledger.history())


def p(identity, at, held):
    return PortfolioEvidence(identity, ACCOUNT, at, {"AAPL": held}, {"AAPL": 100.}, {"AAPL": 15000.}, identity)


def sale(ledger, *, quantity=2, snapshot="start", key="direction-sale", at="2026-09-08T11:00:01Z"):
    return ledger.reserve_direction_exit(symbol="AAPL", horizon="1h", forecast_id="bearish-forecast",
        target_start="2026-09-08T11:00:00Z", target_end="2026-09-08T12:00:00Z", quantity=quantity,
        limit_price=100., snapshot_id=snapshot, idempotency_key=key, batch_id=key, as_of=at)


def terminal(order, *, filled=0, status="CANCELLED", identity="terminal", at="2026-09-08T11:00:05Z"):
    return OrderEvidence(identity, order.reservation_id, ACCOUNT, at, "12345", status, order.quantity, filled,
        0 if status in {"CANCELLED", "REJECTED", "FILLED"} else order.quantity-filled,
        (FillEvidence("sale-fill", filled, 100., "2026-09-08T11:00:04Z"),) if filled else ())


@pytest.mark.parametrize("filled", [0, 1, 2])
def test_terminal_manual_sale_releases_only_unsold_stock_and_reconciles_repeatedly(tmp_path, filled):
    ledger = HorizonLedger(tmp_path / "ledger.sqlite3", ACCOUNT)
    assert ledger.reconcile(p("start", "2026-09-08T11:00:00Z", 2)).ready
    order = sale(ledger)
    outcome = terminal(order, filled=filled, status="FILLED" if filled == 2 else "CANCELLED")
    assert ledger.reconcile(p("after", "2026-09-08T11:00:06Z", 2-filled), order_evidence=(outcome,)).ready
    state = ledger.snapshot()
    assert state.allocations[0].filled_shares == 0 and state.allocations[0].status == "CLOSED"
    assert ledger.reconcile(p("repeat", "2026-09-08T11:00:08Z", 2-filled),
        order_evidence=(replace(outcome, evidence_id="terminal-repeat", observed_at="2026-09-08T11:00:07Z"),)).ready
    assert ledger.reconcile(p("quiet", "2026-09-08T11:00:09Z", 2-filled)).ready
    assert ledger.snapshot().persistent_blocks == ()
    releases = [record for record in ledger.history() if record.kind == "gameplan-unsold-opening-stock-release"]
    assert len(releases) == (0 if filled == 2 else 1)


def test_assignment_release_after_ready_working_snapshot_does_not_look_like_external_reduction(tmp_path):
    ledger = HorizonLedger(tmp_path / "ledger.sqlite3", ACCOUNT)
    assert ledger.reconcile(p("start", "2026-09-08T11:00:00Z", 2)).ready
    order = sale(ledger)
    working = terminal(order, status="WORKING")
    assert ledger.reconcile(p("working", "2026-09-08T11:00:06Z", 2), order_evidence=(working,)).ready
    cancel = terminal(order, filled=1, at="2026-09-08T11:00:07Z", identity="cancel")
    assert ledger.reconcile(p("cancelled", "2026-09-08T11:00:08Z", 1), order_evidence=(cancel,)).ready
    assert ledger.reconcile(p("quiet", "2026-09-08T11:00:09Z", 1)).ready
    assert ledger.snapshot().persistent_blocks == ()


def test_release_retains_last_ready_baseline_through_unready_broker_capture(tmp_path):
    ledger = HorizonLedger(tmp_path / "ledger.sqlite3", ACCOUNT)
    assert ledger.reconcile(p("start", "2026-09-08T11:00:00Z", 2)).ready
    order = sale(ledger)
    outcome = terminal(order, filled=1)
    # The fill arrives before the account position catches up. Releasing the
    # unsold share must not turn that inconsistent capture into a new baseline.
    assert not ledger.reconcile(p("lag", "2026-09-08T11:00:06Z", 2), order_evidence=(outcome,)).ready
    assert ledger.reconcile(p("caught-up", "2026-09-08T11:00:08Z", 1),
        order_evidence=(replace(outcome, evidence_id="repeat-cancel", observed_at="2026-09-08T11:00:07Z"),)).ready
    assert ledger.snapshot().persistent_blocks == ()


@pytest.mark.parametrize("sold,owned_remaining", [(0, 3), (1, 2), (3, 0), (4, 0)])
def test_prior_purchased_inventory_is_consumed_first_and_never_released_as_manual_stock(tmp_path, sold, owned_remaining):
    ledger = HorizonLedger(tmp_path / "ledger.sqlite3", ACCOUNT)
    assert ledger.reconcile(p("start", "2026-09-08T11:00:00Z", 2)).ready
    buy = ledger.reserve_entry(symbol="AAPL", horizon="1h", forecast_id="earlier-buy",
        target_start="2026-09-08T11:00:00Z", target_end="2026-09-08T12:00:00Z", quantity=3,
        limit_price=100., snapshot_id="start", idempotency_key="purchase", batch_id="purchase", as_of="2026-09-08T11:00:01Z")
    buy_fill = OrderEvidence("buy-filled", buy.reservation_id, ACCOUNT, "2026-09-08T11:00:02Z", "11111", "FILLED", 3, 3, 0,
        (FillEvidence("buy-fill", 3, 100., "2026-09-08T11:00:02Z"),))
    assert ledger.reconcile(p("purchased", "2026-09-08T11:00:03Z", 5), order_evidence=(buy_fill,)).ready
    order = sale(ledger, quantity=5, snapshot="purchased", at="2026-09-08T11:00:04Z")
    assert ledger.reconcile(p("cancelled", "2026-09-08T11:00:06Z", 5-sold), order_evidence=(terminal(order, filled=sold),)).ready
    state = ledger.snapshot()
    assert state.allocations[0].filled_shares == owned_remaining
    assert state.allocations[0].status == ("ACTIVE" if owned_remaining else "CLOSED")
    assert ledger.reconcile(p("quiet", "2026-09-08T11:00:07Z", 5-sold)).ready
    assert state.persistent_blocks == ()
