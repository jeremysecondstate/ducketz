import pandas as pd
import pytest

from ml.gameplan_trade_planning import AUTHORITY, plan_trade_rows, _signal
from ml.stock_trader.contracts import ActivationIntent, PortfolioState, QuoteState, StockTraderPolicy
from ml.stock_trader.fixed_horizon_budget import size_fixed_horizon_budget


def forecast(symbol="AAPL", *, hour=4, horizon="1h", probability=.7, promoted=True):
    start = pd.Timestamp("2026-09-09T11:00:00Z") + pd.Timedelta(hours=hour - 4)
    return {"id": f"{symbol}:{horizon}:{hour}", "symbol": symbol, "model_group": horizon,
            "route": f"{horizon}@{hour:02d}:00", "execution_eligible": True, "target_role": "EXECUTION",
            "target_window_start": start, "target_window_end": start + pd.Timedelta(hours=1 if horizon == "1h" else 4),
            "decision_timestamp": "2026-09-09T00:05:00Z", "frozen_at": "2026-09-09T04:05:00Z",
            "calibrated_probability": probability, "raw_probability": probability,
            "model_status": "PROMOTED" if promoted else "RESEARCH_NOT_PROMOTED",
            "model_family": "actual-family", "model_artifact": "model.joblib",
            "target_contract_version": "independent-stock-targets-v1", "target_price_source_contract": "xnas-itch-archive-v1",
            "symbol_route_fitted_target_rows": 40}


def snapshot(symbols=("AAPL",), *, cash=1000., equity=10000.):
    return {"status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED", "available_cash": cash,
            "reserved_cash": 0.,
            "broker_available_cash": 100000., "account_equity": equity, "gross_exposure": 0.,
            "symbol_exposure": {s: 0. for s in symbols}, "held_shares": {s: 0. for s in symbols},
            "pending_buy_shares": {}, "pending_sell_shares": {}, "observed_at": "2026-09-09T04:10:00Z",
            "quotes": {s: {"bid": 9.99, "ask": 10., "price_reference": 10., "price_reference_time": "2026-09-09T00:00:00Z"} for s in symbols},
            "ownership": {"safe_for_planning": True, "active_allocations": [], "blocked_symbols": []}}


def bands(rows, *, low=9., high=11.):
    return {"rows": [{"forecast_id": row["id"], "price_band_status": "AVAILABLE",
                      "trade_price_low": low, "trade_price_high": high,
                      "price_reference_observed_at": "2026-09-09T00:00:00Z"} for row in rows]}


def test_whole_shares_reserve_upper_range_and_do_not_borrow():
    rows = [forecast("AAPL", probability=.8), forecast("AMZN", probability=.8)]
    state = snapshot(("AAPL", "AMZN"), cash=75)
    result = plan_trade_rows(pd.DataFrame(rows), state, bands(rows))
    assert result.trade_quantity.tolist() == [6, 0]
    assert result.trade_notional_reserved.sum() == 66
    assert result.trade_notional_reserved.sum() <= 75 * .95
    assert result.trade_planning_authority.eq(AUTHORITY).all()
    assert "order_payload" not in result and "protective_price" not in result


def test_zero_price_uncertainty_matches_existing_fixed_budget_arithmetic():
    row = forecast()
    quote = QuoteState("AAPL", 9.99, 10., 10., 10., 1., "2026-09-09T11:00:00Z")
    portfolio = PortfolioState(quote.observed_at, 10000., 1000., 0., 0., {"AAPL": 0}, {"AAPL": 0}, {}, {}, 0, {"AAPL": quote}, "test")
    actual = size_fixed_horizon_budget(_signal(row), portfolio, quote,
        ActivationIntent(True, "ACTIVE", "test only", "", "test"), forecast_promoted=True,
        ledger_ready=True, has_active_allocation=False, decided_at=quote.observed_at)
    proposed = plan_trade_rows(pd.DataFrame([row]), snapshot(), bands([row], low=10, high=10))
    assert proposed.iloc[0].trade_quantity == actual.quantity == 6


@pytest.mark.parametrize("change,reason", [
    ({"calibrated_probability": .539999}, "NO_BULLISH_ENTRY_SIGNAL"),
    ({"model_status": "RESEARCH_NOT_PROMOTED"}, "FORECAST_NOT_PROMOTED"),
    ({"execution_eligible": False}, "NON_ENTRY_CONTEXT"),
    ({"symbol_route_fitted_target_rows": 0}, "NO_EXACT_ROUTE_FITTED_HISTORY"),
])
def test_forecast_and_context_gates_remain_zero(change, reason):
    row = {**forecast(), **change}
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(), bands([row])).iloc[0]
    assert result.trade_quantity == 0 and result.trade_planning_reason == reason


def test_holdings_pending_buys_and_no_credit_for_pending_sells():
    row = forecast()
    state = snapshot()
    state.update(symbol_exposure={"AAPL": 1490}, held_shares={"AAPL": 149}, pending_buy_shares={"AAPL": 1}, pending_sell_shares={"AAPL": 149})
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 0
    assert result.current_held_shares == 149 and result.current_symbol_investment == 1490


@pytest.mark.parametrize("cash", [None, float("nan"), -1])
def test_unavailable_or_invalid_account_is_not_invented(cash):
    row = forecast(); state = snapshot(cash=cash)
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 0
    assert result.trade_planning_reason == "ACCOUNT_OR_OWNERSHIP_EVIDENCE_UNAVAILABLE"


def test_existing_or_planned_horizon_does_not_assume_exit_fill():
    rows = [forecast(hour=4), forecast(hour=5)]
    result = plan_trade_rows(pd.DataFrame(rows), snapshot(), bands(rows))
    assert result.trade_quantity.tolist() == [5, 0]
    assert result.iloc[1].trade_planning_reason == "REQUIRES_PRIOR_EXIT_CONFIRMATION"
    state = snapshot(); state["ownership"]["active_allocations"] = [{"symbol": "AAPL", "horizon": "1h"}]
    assert plan_trade_rows(pd.DataFrame(rows), state, bands(rows)).trade_quantity.sum() == 0


def test_all_symbols_share_six_order_batch_cap():
    symbols = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST")
    rows = [forecast(s) for s in symbols]
    result = plan_trade_rows(pd.DataFrame(rows), snapshot(symbols), bands(rows))
    assert result.trade_quantity.gt(0).sum() == 6
    assert result.trade_planning_reason.eq("COMBINED_BATCH_ORDER_CAP").sum() == 1


def test_missing_range_and_quote_outside_band_are_explicit():
    row = forecast(); b = bands([row]); b["rows"][0]["price_band_status"] = "UNAVAILABLE_MINIMUM_SAMPLES"
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(), b).iloc[0]
    assert result.trade_quantity == 0 and result.trade_planning_reason == "ENTRY_PRICE_RANGE_UNAVAILABLE"
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(), bands([row], low=7, high=9)).iloc[0]
    assert result.trade_quantity == 0 and result.trade_planning_reason == "REFERENCE_OUTSIDE_HISTORICAL_RANGE"


def test_forecast_or_band_identity_tampering_fails():
    row = forecast(); frame = pd.DataFrame([row])
    with pytest.raises(ValueError, match="identities"):
        plan_trade_rows(frame, snapshot(), {"rows": []})
    with pytest.raises(ValueError, match="unique"):
        plan_trade_rows(pd.concat([frame, frame]), snapshot(), bands([row]))
    duplicate = bands([row]); duplicate["rows"] *= 2
    with pytest.raises(ValueError, match="identities"):
        plan_trade_rows(frame, snapshot(), duplicate)


def test_actual_quote_time_and_ask_outside_range_cannot_authorize_quantity():
    row = forecast(); state = snapshot()
    state["quotes"]["AAPL"]["price_reference_time"] = "2026-09-08T20:00:00Z"
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 0 and result.trade_planning_reason == "QUOTE_REFERENCE_STALE_OR_FUTURE"
    state = snapshot(); state["quotes"]["AAPL"].update(bid=10.99, ask=11.01, price_reference=10.99)
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 0 and result.trade_planning_reason == "REFERENCE_OUTSIDE_HISTORICAL_RANGE"


def test_nonuniverse_working_buys_consume_gross_headroom():
    row = forecast(); state = snapshot()
    state.update(gross_exposure=12900, reserved_cash=100)
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 0
    state = snapshot(); state["symbol_exposure"] = {}
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 0 and result.trade_planning_reason == "ACCOUNT_OR_OWNERSHIP_EVIDENCE_UNAVAILABLE"


@pytest.fixture
def publication_case(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from ml import nightly_gameplan
    from ml.stock_trader import independent_signals
    from ml import gameplan_price_bands
    source = tmp_path / "ml/nightly-gameplan-runs/frozen"
    source.mkdir(parents=True)
    config = {"preparation_scope": "STOCK_ONLY", "target_contract_version": "independent-stock-targets-v1",
              "target_price_source_contract": "xnas-itch-archive-v1", "symbols": ["AAPL"]}
    report = {"1h": {"promotion_gate": {"status": "PROMOTED", "checks": {}}, "partitions": {},
                     "assessment": {}, "training_base_rate_assessment": {}}}
    for name, payload in (("receipt.json", {"action_date": "2026-09-09"}), ("manifest.json", {"configuration": config}),
                          ("model-reports.json", report)):
        (source / name).write_text(json.dumps(payload))
    # Match the source's 24-row option table contract; grid semantics are
    # validated separately by the native frozen-forecast reader.
    rows = [{**forecast(), "id": f"fixture:{i}"} for i in range(24)]
    frame = pd.DataFrame(rows)
    frame.to_parquet(source / "forecasts.parquet", index=False)
    pd.DataFrame({"id": frame.id + ":OPTION", "symbol": "AAPL", "plan_status": "NO_TRADE_STOCK_ONLY",
                  "legs_json": None, "candidate_key": None, "strategy_source_run": None}).to_parquet(source / "option-strategy-intents.parquet", index=False)
    monkeypatch.setattr(nightly_gameplan, "read_gameplan_run", lambda *a: SimpleNamespace(run_directory=source,
                        manifest={"configuration": config}, receipt={"action_date": "2026-09-09"}))
    monkeypatch.setattr(independent_signals, "_validated_independent_forecasts", lambda frame, **kw: frame)
    monkeypatch.setattr(independent_signals, "verified_promoted_model_groups", lambda p: frozenset({"1h"}))
    monkeypatch.setattr(gameplan_price_bands, "build_entry_price_bands", lambda *a, **kw: bands(rows))
    return SimpleNamespace(root=tmp_path, source=source, state=snapshot(),
             prices=lambda *a, **kw: (pd.DataFrame(), (), {}), clock=lambda: pd.Timestamp("2026-09-09T04:10:00Z"))


def test_publication_binds_source_and_outputs_without_changing_gameplan(publication_case):
    import json
    from ml.artifacts import file_checksum, verify_manifest
    from ml.gameplan_trade_planning import publish_trade_plan
    c = publication_case
    before = {p.name: file_checksum(p) for p in c.source.iterdir()}
    run = publish_trade_plan(c.root, gameplan_run=c.source, snapshot_loader=lambda *a, **kw: c.state,
                             price_loader=c.prices, clock=c.clock)
    verify_manifest(run)
    receipt = json.loads((run / "receipt.json").read_text())
    assert receipt["orders_placed"] == 0 and receipt["execution_authority"] == AUTHORITY
    assert receipt["source_receipt_sha256"] == before["receipt.json"]
    pointer = json.loads((c.root / "ml/gameplan-trade-plan-latest/run.json").read_text())
    assert pointer["current"]["receipt_sha256"] == file_checksum(run / "receipt.json")
    assert {p.name: file_checksum(p) for p in c.source.iterdir()} == before
    assert "Trade Quantity" in (run / "Gameplan.md").read_text(encoding="utf-8")


def test_missing_account_retains_previous_review_and_audits_failure(publication_case):
    import json
    from ml.gameplan_trade_planning import publish_trade_plan
    c = publication_case
    pointer = c.root / "ml/gameplan-trade-plan-latest/run.json"
    pointer.parent.mkdir(parents=True); pointer.write_text("previous-valid-pointer")
    state = {**c.state, "status": "UNAVAILABLE", "available_cash": None}
    with pytest.raises(RuntimeError, match="Trade planning failed"):
        publish_trade_plan(c.root, gameplan_run=c.source, snapshot_loader=lambda *a, **kw: state,
                           price_loader=c.prices, clock=c.clock)
    assert pointer.read_text() == "previous-valid-pointer"
    receipt_path = next((c.root / "ml/gameplan-trade-plan-runs").glob("*/receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "FAILED" and receipt["failure_code"] == "ACCOUNT_SNAPSHOT_UNAVAILABLE"
    assert receipt["orders_placed"] == 0


def test_publication_after_original_deadline_and_source_swap_fail_closed(publication_case):
    from ml.gameplan_trade_planning import publish_trade_plan
    c = publication_case
    def no_read(*a, **kw):
        pytest.fail("A late publication must not read account state")
    with pytest.raises(ValueError, match="deadline has passed"):
        publish_trade_plan(c.root, gameplan_run=c.source, snapshot_loader=no_read,
                           clock=lambda: pd.Timestamp("2026-09-09T11:00:00Z"))
    def changed(*a, **kw):
        (c.source / "receipt.json").write_text("changed source")
        return c.state
    with pytest.raises(RuntimeError, match="Trade planning failed"):
        publish_trade_plan(c.root, gameplan_run=c.source, snapshot_loader=changed, price_loader=c.prices, clock=c.clock)
    assert not (c.root / "ml/gameplan-trade-plan-latest/run.json").exists()
