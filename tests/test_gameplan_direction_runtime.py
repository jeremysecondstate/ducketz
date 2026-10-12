"""New policy integration and opening inventory release, using synthetic brokers."""
from dataclasses import replace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, _fixed_signals, _decisions, _owned_allocation, ACCOUNT, NOW, SYMBOLS
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


def test_pending_buy_quote_received_during_capture_uses_verified_readiness(environment, monkeypatch):
    from ml.stock_trader.contracts import StockTraderPolicy, canonical_sha256
    from ml.stock_trader.fixed_horizon_engine import _joint_budgets
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    request_started = env.now
    originals = []
    original_capture = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        value = original_capture(*args, **kwargs)
        value = replace(value, pending_buy_shares={"AMZN": 1.}, working_order_count=1,
            quotes={symbol: replace(quote, observed_at=(request_started+pd.Timedelta(milliseconds=500)).isoformat())
                    if symbol == "AMZN" else quote for symbol, quote in value.quotes.items()},
            broker_working_orders=({"order_id":"other-stream-pending-buy", "instruction":"BUY",
                "asset_type":"EQUITY", "symbol":"AMZN", "remaining_quantity":1,
                "filled_quantity":0, "limit_price":100., "reserved_cash":100., "status":"CURRENT"},))
        originals.append(value)
        env.now += pd.Timedelta(seconds=1)
        return value
    original_verify = env.broker.verify_read_snapshot
    def verify(identity):
        original_verify(identity)
        env.now += pd.Timedelta(milliseconds=250)
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    monkeypatch.setattr(env.broker, "verify_read_snapshot", verify)
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED" and result.submitted_orders == 1, result.error
    original, = originals
    with pytest.raises(ValueError, match="Pending buy exposure"):
        _joint_budgets(original, StockTraderPolicy())  # The actual pre-fix failure.
    recorded = _decisions(result)
    ready = recorded["prediction_handoff"]["portfolio_information_readiness"]
    assert ready["request_started_at"] == ready["original_observed_at"] == request_started.isoformat()
    assert ready["validated_at"] == (request_started+pd.Timedelta(milliseconds=1250)).isoformat()
    assert ready["original_capture_age_seconds"] == 1.25
    assert ready["original_source_fingerprint"] == original.source_fingerprint
    assert ready["quote_timestamps_retained"] is True and ready["quote_timestamp_wait_status"] == "NOT_NEEDED"
    assert original.observed_at == request_started.isoformat()
    assert original.quotes["AMZN"].observed_at == (request_started+pd.Timedelta(milliseconds=500)).isoformat()
    ledger = HorizonLedger(env.root/runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    with ledger._transaction() as db:
        import json
        native = json.loads(db.execute("SELECT payload FROM snapshots ORDER BY rowid LIMIT 1").fetchone()[0])
    assert native["observed_at"] == ready["validated_at"]
    assert native["source_fingerprint"] == canonical_sha256(ready)
    assert env.broker.calls.index("verify_snapshot") < env.broker.calls.index("post")


@pytest.mark.parametrize("damage,error", [("future", "CAPTURE_IN_FUTURE"),
    ("stale", "CAPTURE_TOO_OLD"), ("ages_during_identity", "CAPTURE_TOO_OLD"),
    ("identity_change", "ACCOUNT_CHANGED_DURING_CAPTURE")])
def test_readiness_keeps_original_capture_age_and_identity_guards(environment, monkeypatch, damage, error):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original_capture = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        value = original_capture(*args, **kwargs)
        offset = 1 if damage == "future" else -61 if damage == "stale" else -59 if damage == "ages_during_identity" else 0
        return replace(value, observed_at=(env.now+pd.Timedelta(seconds=offset)).isoformat())
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    if damage == "ages_during_identity":
        original_verify = env.broker.verify_read_snapshot
        def verify(identity):
            original_verify(identity)
            env.now += pd.Timedelta(seconds=2)
        monkeypatch.setattr(env.broker, "verify_read_snapshot", verify)
    if damage == "identity_change":
        checks = []
        def identity():
            checks.append(True)
            return ACCOUNT if len(checks) == 1 else "c"*64
        monkeypatch.setattr(env.broker, "stable_account_fingerprint", identity)
    result = run(env)
    assert result.status == "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE" and error in result.error
    assert env.broker.submissions == []
    assert "portfolio_information_readiness" not in _decisions(result)["prediction_handoff"]
    ledger = HorizonLedger(env.root/runtime.LEDGER_RELATIVE_PATH, ACCOUNT)
    with ledger._transaction() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["future", "stale", "missing", "crossed"])
def test_readiness_never_relabels_invalid_or_unavailable_entry_quotes(environment, monkeypatch, damage):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original_capture = runtime.capture_portfolio_state
    originals = []
    def capture(*args, **kwargs):
        value = original_capture(*args, **kwargs)
        quotes = dict(value.quotes)
        if damage == "missing":
            quotes.pop("AAPL")
        elif damage == "future":
            quotes["AAPL"] = replace(quotes["AAPL"], observed_at=(env.now+pd.Timedelta(seconds=20)).isoformat())
        elif damage == "stale":
            quotes["AAPL"] = replace(quotes["AAPL"], observed_at=(env.now-pd.Timedelta(seconds=61)).isoformat())
        else:
            quotes["AAPL"] = replace(quotes["AAPL"], bid=101., ask=100.)
        value = replace(value, quotes=quotes)
        originals.append(value)
        return value
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
        runtime_clock=lambda:env.now, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY,
        broker_state_retry_max_seconds=0, broker_state_retry_sleep=lambda seconds:pytest.fail("No clock waiver"))
    assert result.submitted_orders == 0 and env.broker.submissions == []
    recorded = _decisions(result)
    decision, = recorded["decisions"]
    assert decision["decision_reason_code"] == ("CURRENT_QUOTE_TOO_OLD" if damage == "stale" else "USABLE_QUOTE_UNAVAILABLE")
    original, = originals
    assert decision["quote"] == ({} if damage == "missing" else vars(original.quotes["AAPL"]))
    assert recorded["prediction_handoff"]["portfolio_information_readiness"]["validated_at"] == env.now.isoformat()


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


@pytest.mark.parametrize("probability,instruction,price", [(.7, "BUY", "124.21"), (.3, "SELL", "124.20")])
def test_current_realtime_response_with_unchanged_bbo_reaches_submission(environment, monkeypatch, probability, instruction, price):
    env = environment
    prepare(env, monkeypatch, probability=probability)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original_capture = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        return replace(result, quotes={symbol: replace(quote, bid=123.66, ask=124.75,
            observed_at=(env.now - pd.Timedelta(minutes=13)).isoformat(),
            received_at=env.now.isoformat(), realtime=True, quote_type="NBBO")
            for symbol, quote in result.quotes.items()})
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == len(env.broker.submissions) == 1
    payload = env.broker.submissions[0]
    assert payload["orderLegCollection"][0]["instruction"] == instruction
    assert float(payload["price"]) == float(price)
    assert run(env).submitted_orders == 0
    assert len(env.broker.submissions) == 1


def test_realtime_response_that_ages_before_post_is_not_submitted(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original_capture = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        return replace(result, quotes={symbol: replace(quote,
            observed_at=(env.now - pd.Timedelta(minutes=13)).isoformat(),
            received_at=(env.now - pd.Timedelta(seconds=59)).isoformat(), realtime=True, quote_type="NBBO")
            for symbol, quote in result.quotes.items()})
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    env.broker.before_post_hook = lambda: setattr(env, "now", env.now + pd.Timedelta(seconds=2))
    result = run(env)
    assert result.status == "SUBMISSION_STOPPED_SAFETY_CHECK"
    assert "CURRENT_QUOTE_TOO_OLD_FOR_SUBMISSION" in result.error
    assert env.broker.submissions == []


def test_midpoint_quantity_agrees_with_native_horizon_valuation(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.signals = {("AAPL", "1h"): env.signals["AAPL", "1h"]}
    original = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        portfolio = original(*args, **kwargs)
        return replace(portfolio, quotes={s:replace(q, bid=99., ask=101.) for s,q in portfolio.quotes.items()})
    monkeypatch.setattr(runtime, "capture_portfolio_state", capture)
    result = run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == 1
    leg = env.broker.submissions[0]["orderLegCollection"][0]
    assert leg["quantity"] == 14  # Fifteen at the ask would exceed the $1,500 horizon capacity.


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


def test_late_opening_uses_same_durable_slot_and_preserves_five_oclock_end(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    env.now = pd.Timestamp('2026-09-08T11:30:00Z')
    env.signals = {('AAPL','1h'):replace(env.signals['AAPL','1h'], actionable_until='2026-09-08T12:00:00Z')}
    def late():
        return runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
            runtime_clock=lambda:env.now, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY,
            late_opening_date='2026-09-08')
    result = late()
    assert result.status == 'ORDERS_SUBMITTED' and result.submitted_orders == 1, result.error
    assert _decisions(result)['prediction_handoff']['late_opening_date'] == '2026-09-08'
    state = HorizonLedger(env.root/runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot()
    assert len(state.allocations) == 1 and pd.Timestamp(state.allocations[0].target_end) == pd.Timestamp('2026-09-08T12:00:00Z')
    env.now += pd.Timedelta(seconds=1)
    assert late().submitted_orders == 0 and len(env.broker.submissions) == 1


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


@pytest.mark.parametrize("horizon", ["1h", "4h", "1d", "1w"])
def test_manual_forecast_boundary_never_submits_an_expiry_sale(environment, monkeypatch, horizon):
    env = environment
    prepare(env, monkeypatch)
    ledger = _owned_allocation(env, horizon=horizon)
    env.now = pd.Timestamp("2026-09-08T12:01:00Z")
    env.signals = {}
    run(env)
    assert env.broker.submissions == []
    assert ledger.snapshot().allocations[0].filled_shares == 10


def test_manual_next_bullish_forecast_adds_to_expired_forecast_bucket(environment, monkeypatch):
    env = environment
    prepare(env, monkeypatch, probability=.7)
    ledger = _owned_allocation(env, symbol="AAPL", manual=0)
    signal = env.signals["AAPL", "1h"]
    env.now = pd.Timestamp("2026-09-08T12:01:00Z")
    env.signals = {("AAPL", "1h"): replace(signal, prediction_id="next-hour-bullish", actionable_until="2026-09-08T12:59:59Z",
        target_window_start="2026-09-08T12:00:00Z", target_window_end="2026-09-08T13:00:00Z")}
    result = run(env)
    assert result.submitted_orders == 1, result.error
    assert env.broker.submissions[0]["orderLegCollection"][0]["instruction"] == "BUY"
    assert len(ledger.snapshot().allocations) == 1
    assert run(env).submitted_orders == 0
    assert len(env.broker.submissions) == 1
    assert env.broker.cancellations == []  # New order uses its own hour, not the bucket's original end.
