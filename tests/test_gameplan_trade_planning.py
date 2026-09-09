import pandas as pd
import pytest

from ml.gameplan_trade_planning import AUTHORITY, VERSION, plan_trade_rows, _signal, _plan_working_price_rows
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
            "stock_market_value_by_symbol": {s: 0. for s in symbols}, "other_symbol_exposure": {s: 0. for s in symbols},
            "pending_buy_shares": {}, "pending_sell_shares": {}, "observed_at": "2026-09-09T04:10:00Z",
            "quotes": {s: {"bid": 9.99, "ask": 10., "price_reference": 10., "price_reference_time": "2026-09-09T00:00:00Z"} for s in symbols},
            "ownership": {"safe_for_planning": True, "active_allocations": [], "blocked_symbols": []}}


def bands(rows, *, low=9., high=11.):
    return {"rows": [{"forecast_id": row["id"], "price_band_status": "AVAILABLE",
                      "trade_price_low": low, "trade_price_high": high,
                      "price_reference_observed_at": "2026-09-09T00:00:00Z"} for row in rows]}


def planning_path(rows, *, low=9.98, mid=10., high=10.02):
    points = {}
    for symbol in sorted({row["symbol"] for row in rows}):
        for hour in range(4, 18):
            timestamp = pd.Timestamp(f"2026-09-09T{hour:02d}:00:00", tz="America/Los_Angeles")
            key = f"{symbol}|2026-09-09|{hour:02d}:00"
            points[key] = {"symbol": symbol, "action_date": "2026-09-09", "clock_local": f"{hour:02d}:00",
                           "timestamp": timestamp.tz_convert("UTC").isoformat(), "status": "AVAILABLE",
                           "reason": "Conditional test fill", "planned_price_low": low, "planned_price_mid": mid,
                           "planned_price_high": high, "historical_price_low": 9., "historical_price_high": 11.,
                           "method": "Observed median with conditional fill allowance"}
    return {"points": points, "working_half_width_bps": 20,
            "working_range_semantics": "Conditional fill assumption"}


def test_working_price_capacity_preserves_separate_scheduled_preview_and_history():
    row = forecast()
    original = bands([row])
    result = _plan_working_price_rows(pd.DataFrame([row]), snapshot(), original,
                                     planning_path([row]), policy=StockTraderPolicy()).iloc[0]
    assert result.trade_quantity == result.scheduled_trade_quantity == 6
    assert result.trade_notional_reserved == 60
    assert result.projected_trade_quantity == 14
    assert result.projected_trade_notional == pytest.approx(140.28)
    assert (result.trade_price_low, result.trade_price_mid, result.trade_price_high) == (9.98, 10, 10.02)
    assert (result.historical_price_low, result.historical_price_high) == (9, 11)
    assert (result.scheduled_trade_price_low, result.scheduled_trade_price_high) == (9, 11)
    assert original["rows"][0]["trade_price_high"] == 11


def test_unavailable_working_point_does_not_invent_projected_capacity():
    row = forecast(); path = planning_path([row])
    path["points"]["AAPL|2026-09-09|04:00"]["status"] = "UNAVAILABLE_MINIMUM_SAMPLES"
    result = _plan_working_price_rows(pd.DataFrame([row]), snapshot(), bands([row]), path,
                                     policy=StockTraderPolicy()).iloc[0]
    assert result.trade_quantity == 6
    assert result.projected_trade_quantity is None
    assert result.trade_price_low is None and result.trade_price_high is None
    assert result.projected_quantity_reason == "PRICE_RANGE_UNAVAILABLE"


@pytest.mark.parametrize("field,value", [("symbol", "AMZN"), ("timestamp", "2026-09-09T12:00:00Z"),
                                        ("planned_price_high", 9), ("planned_price_mid", float("nan"))])
def test_working_point_identity_and_economics_are_validated(field, value):
    row = forecast(); path = planning_path([row])
    path["points"]["AAPL|2026-09-09|04:00"][field] = value
    with pytest.raises(ValueError):
        _plan_working_price_rows(pd.DataFrame([row]), snapshot(), bands([row]), path, policy=StockTraderPolicy())


def test_working_prices_do_not_turn_context_into_an_entry():
    row = {**forecast(), "execution_eligible": False, "target_role": "OUTLOOK"}
    result = _plan_working_price_rows(pd.DataFrame([row]), snapshot(), bands([row]), planning_path([row]),
                                     policy=StockTraderPolicy()).iloc[0]
    assert result.trade_price_low is None and result.trade_price_mid is None
    assert result.trade_quantity == 0 and result.projected_trade_quantity is None


def test_scheduled_whole_shares_reserve_current_ask_and_do_not_borrow():
    rows = [forecast("AAPL", probability=.8), forecast("AMZN", probability=.8)]
    state = snapshot(("AAPL", "AMZN"), cash=75)
    result = plan_trade_rows(pd.DataFrame(rows), state, bands(rows))
    assert result.trade_quantity.tolist() == [7, 0]
    assert result.trade_notional_reserved.sum() == 70
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


@pytest.mark.parametrize("probability,promoted", [(.3, True), (.5287, True), (.7, False)])
def test_projected_capacity_is_available_even_without_a_scheduled_entry(probability, promoted):
    row = forecast(probability=probability, promoted=promoted)
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(), bands([row])).iloc[0]
    assert result.projected_trade_quantity == 13
    assert result.projected_trade_budget == 150
    assert result.projected_trade_notional == 143
    assert result.trade_quantity == result.scheduled_trade_quantity == 0
    assert result.projected_quantity_reason == "AFFORDABLE_HORIZON_ALLOCATION"


def test_each_projection_obeys_cash_and_existing_position_capacity():
    rows = [forecast("AAPL", probability=.3), forecast("AMZN", probability=.3)]
    state = snapshot(("AAPL", "AMZN"), cash=75)
    state["symbol_exposure"]["AAPL"] = 1460
    result = plan_trade_rows(pd.DataFrame(rows), state, bands(rows))
    assert result.projected_trade_quantity.tolist() == [3, 6]
    assert result.projected_trade_notional.le(75 * .95).all()
    assert result.trade_notional_reserved.sum() == 0


def test_context_has_no_entry_quantity_and_high_price_does_not_invent_fractional_shares():
    context = {**forecast(), "execution_eligible": False}
    result = plan_trade_rows(pd.DataFrame([context]), snapshot(), bands([context])).iloc[0]
    assert pd.isna(result.projected_trade_quantity)
    row = forecast(probability=.3)
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(), bands([row], high=200)).iloc[0]
    assert result.projected_trade_quantity == 0
    assert result.projected_quantity_reason == "INSUFFICIENT_WHOLE_SHARE_CAPACITY"


def test_new_bullish_boundary_supplies_a_scheduled_entry():
    row = forecast(probability=.54)
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(equity=100000), bands([row])).iloc[0]
    assert result.planning_direction == "BULLISH"
    assert result.scheduled_trade_quantity > 0


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
    assert result.trade_quantity.tolist() == [6, 0]
    assert result.iloc[1].trade_planning_reason == "REQUIRES_PRIOR_EXIT_CONFIRMATION"
    state = snapshot(); state["ownership"]["active_allocations"] = [{"symbol": "AAPL", "horizon": "1h"}]
    assert plan_trade_rows(pd.DataFrame(rows), state, bands(rows)).trade_quantity.sum() == 0


def test_all_symbols_share_six_order_batch_cap():
    symbols = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST")
    rows = [forecast(s) for s in symbols]
    result = plan_trade_rows(pd.DataFrame(rows), snapshot(symbols), bands(rows))
    assert result.trade_quantity.gt(0).sum() == 6
    assert result.trade_planning_reason.eq("COMBINED_BATCH_ORDER_CAP").sum() == 1


def test_missing_estimated_range_does_not_block_quote_based_scheduled_preview():
    row = forecast(); b = bands([row]); b["rows"][0]["price_band_status"] = "UNAVAILABLE_MINIMUM_SAMPLES"
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(), b).iloc[0]
    assert result.trade_quantity == 6 and result.trade_planning_reason == "PROVISIONAL_BUY"
    assert result.projected_trade_quantity is None


@pytest.mark.parametrize("low,high", [(7, 9), (11, 13), (100, 110)])
def test_current_quote_above_or_below_estimated_range_remains_eligible(low, high):
    row = forecast()
    result = plan_trade_rows(pd.DataFrame([row]), snapshot(), bands([row], low=low, high=high)).iloc[0]
    assert result.trade_quantity == 6 and result.trade_planning_reason == "PROVISIONAL_BUY"
    assert result.trade_limit_price_reference == 10
    assert result.trade_notional_reserved == 60


def test_forecast_or_band_identity_tampering_fails():
    row = forecast(); frame = pd.DataFrame([row])
    with pytest.raises(ValueError, match="identities"):
        plan_trade_rows(frame, snapshot(), {"rows": []})
    with pytest.raises(ValueError, match="unique"):
        plan_trade_rows(pd.concat([frame, frame]), snapshot(), bands([row]))
    duplicate = bands([row]); duplicate["rows"] *= 2
    with pytest.raises(ValueError, match="identities"):
        plan_trade_rows(frame, snapshot(), duplicate)


def test_quote_freshness_remains_required_while_ask_above_estimate_changes_size():
    row = forecast(); state = snapshot()
    state["quotes"]["AAPL"]["price_reference_time"] = "2026-09-08T20:00:00Z"
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 0 and result.trade_planning_reason == "QUOTE_REFERENCE_STALE_OR_FUTURE"
    state = snapshot(); state["quotes"]["AAPL"].update(bid=10.99, ask=11.01, price_reference=10.99)
    result = plan_trade_rows(pd.DataFrame([row]), state, bands([row])).iloc[0]
    assert result.trade_quantity == 5 and result.trade_planning_reason == "PROVISIONAL_BUY"
    assert result.trade_limit_price_reference == 11.01
    assert result.trade_notional_reserved == pytest.approx(55.05)


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
    from ml.independent_stock_targets import stock_target_windows
    rows = [{**forecast(), **window, "id": f"fixture:{i}", "action_date": "2026-09-09"}
            for i, window in enumerate(stock_target_windows(pd.Timestamp("2026-09-09").date()))]
    frame = pd.DataFrame(rows)
    frame.to_parquet(source / "forecasts.parquet", index=False)
    pd.DataFrame({"id": frame.id + ":OPTION", "symbol": "AAPL", "plan_status": "NO_TRADE_STOCK_ONLY",
                  "legs_json": None, "candidate_key": None, "strategy_source_run": None}).to_parquet(source / "option-strategy-intents.parquet", index=False)
    monkeypatch.setattr(nightly_gameplan, "read_gameplan_run", lambda *a: SimpleNamespace(run_directory=source,
                        manifest={"configuration": config}, receipt={"action_date": "2026-09-09"}))
    monkeypatch.setattr(independent_signals, "_validated_independent_forecasts", lambda frame, **kw: frame)
    monkeypatch.setattr(independent_signals, "verified_promoted_model_groups", lambda p: frozenset({"1h", "4h", "1d", "1w"}))
    calls = []
    def entry_bands(*a, **kw):
        result = bands(rows)
        calls.append(("bands", kw["observed_at"], result))
        return result
    def price_path(*a, **kw):
        assert kw["entry_bands"] is calls[-1][2]
        calls.append(("path", kw["observed_at"], kw["entry_bands"]))
        return planning_path(rows)
    monkeypatch.setattr(gameplan_price_bands, "build_entry_price_bands", entry_bands)
    monkeypatch.setattr(gameplan_price_bands, "build_planning_price_path", price_path)
    return SimpleNamespace(root=tmp_path, source=source, state=snapshot(),
             prices=lambda *a, **kw: (pd.DataFrame(), (), {}), clock=lambda: pd.Timestamp("2026-09-09T04:10:00Z"), calls=calls)


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
    assert receipt["schema_version"] == VERSION == "cash-aware-gameplan-trade-planning-v3"
    assert receipt["source_receipt_sha256"] == before["receipt.json"]
    pointer = json.loads((c.root / "ml/gameplan-trade-plan-latest/run.json").read_text())
    assert pointer["current"]["receipt_sha256"] == file_checksum(run / "receipt.json")
    assert {p.name: file_checksum(p) for p in c.source.iterdir()} == before
    assert "Trade Quantity" in (run / "Gameplan.md").read_text(encoding="utf-8")
    assert c.calls[0][1] == c.calls[1][1]
    report = json.loads((run / "report.json").read_text())
    assert report["direction_based_projection"] == json.loads((run / "direction-ledger.json").read_text())
    assert report["planning_price_path"]["working_half_width_bps"] == 20
    assert "points" not in report["planning_price_path"]
    rows = pd.read_parquet(run / "trade-plan.parquet")
    assert rows.direction_based_trade_quantity.gt(0).any()
    assert rows.historical_price_high.eq(11).all()
    assert rows.scheduled_trade_price_high.eq(11).all()
    assert rows.loc[rows.execution_eligible, "trade_price_high"].eq(10.02).all()
    # Both new evidence artifacts are verified outputs, not unbound side files.
    for name in ("planning-price-path.json", "direction-ledger.json"):
        path = run / name
        original = path.read_bytes()
        path.write_text("tampered")
        with pytest.raises((ValueError, RuntimeError)):
            verify_manifest(run)
        path.write_bytes(original)
    verify_manifest(run)


def test_direction_ledger_failure_preserves_previous_pointer_without_raw_error(publication_case, monkeypatch):
    import json
    from ml import gameplan_cash_ledger
    from ml.gameplan_trade_planning import publish_trade_plan
    c = publication_case
    pointer = c.root / "ml/gameplan-trade-plan-latest/run.json"
    pointer.parent.mkdir(parents=True); pointer.write_text("previous-valid-pointer")
    def fail(*a, **kw):
        raise ValueError("RAW_PRIVATE_FAILURE_SENTINEL")
    monkeypatch.setattr(gameplan_cash_ledger, "project_direction_trades", fail)
    with pytest.raises(RuntimeError, match="Trade planning failed"):
        publish_trade_plan(c.root, gameplan_run=c.source, snapshot_loader=lambda *a, **kw: c.state,
                           price_loader=c.prices, clock=c.clock)
    assert pointer.read_text() == "previous-valid-pointer"
    run = next((c.root / "ml/gameplan-trade-plan-runs").iterdir())
    report = json.loads((run / "report.json").read_text())
    assert report["status"] == "FAILED"
    assert report["failure_phase"] == "DIRECTION_BASED_CASH_AND_SHARE_PROJECTION"
    assert report["failure_code"] == "TRADE_PLANNING_VALIDATION_FAILED"
    assert "RAW_PRIVATE_FAILURE_SENTINEL" not in (run / "report.json").read_text()
    assert "RAW_PRIVATE_FAILURE_SENTINEL" not in (run / "receipt.json").read_text()


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
