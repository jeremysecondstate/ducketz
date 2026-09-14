from __future__ import annotations

import json

import pytest

from app.ui.gameplan_data import GameplanError, load_gameplan, plan_sessions
from gameplan_fixture import plan_payload, unavailable_payload, write_plan


def test_recorded_midpoint_is_joined_by_forecast_without_changing_saved_planning_price(tmp_path):
    run=write_plan(tmp_path)
    before=(run/'trade-plan.parquet').read_bytes()
    plan=load_gameplan(tmp_path)
    forecast=next(row for row in plan.forecasts if row.eligible and row.price is not None)
    execution=tmp_path/'ml/stock-trader-decision-runs/20260914T140101.000000Z'
    execution.mkdir(parents=True)
    (execution/'decisions.json').write_text(json.dumps({'decisions':[{
        'prediction':{'prediction_id':forecast.forecast_id,'position_purpose':'ENTRY'},
        'action':'BUY','quantity':15,'limit_price':127.23,
        'quote':{'bid':127.20,'ask':127.24,'observed_at':'2026-09-14T13:48:00Z',
                 'received_at':'2026-09-14T14:01:00Z','realtime':True}}]}))
    loaded=load_gameplan(tmp_path)
    actual=loaded.execution_quote(forecast.forecast_id)
    assert actual.midpoint == pytest.approx(127.22)
    assert actual.observed_at.hour == 7 and actual.observed_at.minute == 1
    assert loaded.forecast(forecast.forecast_id).price == forecast.price
    assert (run/'trade-plan.parquet').read_bytes() == before
    assert loaded.execution_quote(forecast.forecast_id, is_exit=True) is None
    exit_run=tmp_path/'ml/stock-trader-decision-runs/20260914T150001.000000Z'
    exit_run.mkdir(parents=True)
    (exit_run/'decisions.json').write_text(json.dumps({'decisions':[{
        'prediction':{'prediction_id':'generated-exit-id','parent_forecast_id':forecast.forecast_id,'position_purpose':'EXIT'},
        'action':'SELL','quantity':15,'limit_price':128.,
        'quote':{'bid':128.,'ask':128.02,'observed_at':'2026-09-14T15:00:00Z','received_at':'2026-09-14T15:00:01Z'}}]}))
    loaded=load_gameplan(tmp_path)
    assert loaded.execution_quote(forecast.forecast_id).midpoint == pytest.approx(127.22)
    assert loaded.execution_quote(forecast.forecast_id, is_exit=True).midpoint == pytest.approx(128.01)


def test_quantities_come_from_ledger_and_holds_and_context_remain_forecasts(tmp_path):
    write_plan(tmp_path)
    plan = load_gameplan(tmp_path)
    assert len(plan.forecasts) == 7
    assert [(e.action, e.quantity) for e in plan.actions if e.source == "ledger"] == [("BUY", 17), ("BUY", 2), ("SELL", 17)]
    assert len(plan.rows("1h")) == 2
    assert not plan.trades("1h")
    assert sum(row.action == "CONTEXT" for row in plan.forecasts) == 2
    assert sum(row.eligible for row in plan.forecasts) == 5
    assert all(e.quantity != 9999 for e in plan.actions)


def test_later_expiries_keep_dates_reserved_shares_and_missing_prices(tmp_path):
    write_plan(tmp_path)
    plan = load_gameplan(tmp_path)
    later = plan.trades("4h", "GOOG")[0]
    assert later.when.isoformat() == "2026-09-15T07:00:00-07:00"
    assert later.quantity == 5 and later.reserved == 2
    assert later.price is None and later.source == "remaining_allocation"
    assert later.action == "EXPIRY" and later.reason == "LATER_EXPIRY"
    weekly = plan.trades("1w")
    assert weekly[-1].when.isoformat() == "2026-09-18T17:00:00-07:00"
    assert weekly[-1].price is None
    assert plan.forecast(later.forecast_id) is None  # An earlier owned allocation.


def test_unresolved_expiry_is_not_rescheduled_to_an_invented_clock(tmp_path):
    rows, ledger = plan_payload()
    ledger["ending_allocations"][1]["end"] = "2026-09-14T05:00:00-07:00"
    write_plan(tmp_path, rows=rows, ledger=ledger)
    action = next(e for e in load_gameplan(tmp_path).actions if e.symbol == "GOOG")
    assert action.reason == "UNRESOLVED_EXPIRY" and action.when.hour == 5
    assert action.reserved == 2


def test_saved_session_selection_prefers_latest_pointer_and_excludes_legacy_failures(tmp_path):
    write_plan(tmp_path, session="2026-09-11", latest=False)
    write_plan(tmp_path, session="2026-09-10", latest=False, version="legacy-v3")
    write_plan(tmp_path, session="2026-09-15", latest=False, status="FAILED")
    write_plan(tmp_path)
    assert plan_sessions(tmp_path) == ("2026-09-14", "2026-09-11")
    assert load_gameplan(tmp_path).session == "2026-09-14"
    assert load_gameplan(tmp_path, "2026-09-11").session == "2026-09-11"
    with pytest.raises(GameplanError, match="No completed"):
        load_gameplan(tmp_path, "2026-09-10")


@pytest.mark.parametrize("name", ["receipt.json", "manifest.json", "direction-ledger.json", "trade-plan.parquet", "Gameplan.md"])
def test_changed_publication_or_output_is_rejected(tmp_path, name):
    run = write_plan(tmp_path)
    with (run/name).open("ab") as f:
        f.write(b"changed")
    with pytest.raises(GameplanError):
        load_gameplan(tmp_path)


@pytest.mark.parametrize("mutation", ["quantity", "missing_trade", "duplicate_event", "context_trade", "unzonetime", "session", "duplicate_forecast", "zero_price", "probability", "lot_identity"])
def test_integrity_valid_but_semantically_inconsistent_data_is_rejected(tmp_path, mutation):
    rows, ledger = plan_payload()
    if mutation == "quantity": ledger["events"][0]["quantity"] = 9999
    if mutation == "missing_trade": ledger["events"].pop(0)
    if mutation == "duplicate_event": ledger["events"].append(ledger["events"][0].copy())
    if mutation == "context_trade": ledger["events"][0]["forecast_id"] = rows[-1]["id"]
    if mutation == "unzonetime": ledger["events"][0]["timestamp"] = "2026-09-14T04:00:00"
    if mutation == "session": rows[0]["action_date"] = "2026-09-15"
    if mutation == "duplicate_forecast": rows.append(rows[0].copy())
    if mutation == "zero_price": ledger["events"][0]["price_base"] = 0
    if mutation == "probability": rows[0]["calibrated_probability"] = 1.5
    if mutation == "lot_identity": ledger["ending_allocations"][0]["symbol"] = "AAPL"
    write_plan(tmp_path, rows=rows, ledger=ledger)
    with pytest.raises(GameplanError):
        load_gameplan(tmp_path)


def test_no_trade_plan_is_valid_and_missing_plan_is_explained(tmp_path):
    with pytest.raises(GameplanError, match="No saved Gameplan"):
        load_gameplan(tmp_path)
    rows, ledger = plan_payload()
    for row in rows:
        if row["execution_eligible"]:
            row.update(direction_based_action="HOLD", direction_based_trade_quantity=0, direction_based_reason="NEUTRAL")
    ledger.update(events=[], ending_allocations=[], summary=dict(trade_events=0, buy_events=0, sell_events=0))
    write_plan(tmp_path, rows=rows, ledger=ledger)
    assert not load_gameplan(tmp_path).actions


def test_pointer_cannot_escape_runs_directory(tmp_path):
    write_plan(tmp_path)
    path = tmp_path/"ml/gameplan-trade-plan-latest/run.json"
    pointer = json.loads(path.read_text())
    pointer["current"]["run_path"] = "../unrelated"
    path.write_text(json.dumps(pointer))
    with pytest.raises(GameplanError, match="outside"):
        load_gameplan(tmp_path)


def test_unavailable_projection_preserves_verified_forecasts_without_inventing_actions(tmp_path):
    rows, ledger, report = unavailable_payload()
    run = write_plan(tmp_path, rows=rows, ledger=ledger, report_updates=report)
    before = {path: path.read_bytes() for path in run.iterdir()}
    plan = load_gameplan(tmp_path)
    assert not plan.projection_available
    assert not plan.actions
    assert len(plan.forecasts) == len(rows)
    by_id = {row["id"]: row for row in rows}
    for row in plan.forecasts:
        assert row.probability == by_id[row.forecast_id]["calibrated_probability"]
        assert row.direction == by_id[row.forecast_id]["direction"]
        assert row.quantity is None
        assert row.action == ("UNAVAILABLE" if row.eligible else "CONTEXT")
    assert "AAPL" in plan.projection_note
    assert {path: path.read_bytes() for path in run.iterdir()} == before


def test_carried_planning_closes_are_disclosed_with_original_age(tmp_path):
    write_plan(tmp_path, report_updates={"reference_completion": {"references": {
        "AAPL|2026-09-14": {"status": "AVAILABLE_SYNTHETIC", "symbol": "AAPL", "gap_minutes": 143,
                              "observed_at": "2026-09-11T14:37:00-07:00", "effective_at": "2026-09-11T17:00:00-07:00"}}}})
    plan = load_gameplan(tmp_path)
    assert plan.projection_available
    assert "AAPL (143 min)" in plan.planning_note


@pytest.mark.parametrize("damage", ["symbol", "age", "time"])
def test_carried_close_notice_rejects_inconsistent_provenance(tmp_path, damage):
    ref = {"status": "AVAILABLE_SYNTHETIC", "symbol": "AAPL", "gap_minutes": 143,
           "observed_at": "2026-09-11T14:37:00-07:00", "effective_at": "2026-09-11T17:00:00-07:00"}
    if damage == "symbol": ref["symbol"] = "UNKNOWN"
    if damage == "age": ref["gap_minutes"] = 1
    if damage == "time": ref["observed_at"] = ref["effective_at"]
    write_plan(tmp_path, report_updates={"reference_completion": {"references": {"anchor": ref}}})
    with pytest.raises(GameplanError, match="carried planning close"):
        load_gameplan(tmp_path)


@pytest.mark.parametrize("mutation", ["unmarked", "report_status", "embedded_report", "events", "summary",
                                    "holdings", "cash", "allocations", "actions", "missing_points",
                                    "unknown_symbol", "missing_reason", "probability", "clock", "live"])
def test_unavailable_projection_does_not_bypass_publication_contracts(tmp_path, mutation):
    rows, ledger, report = unavailable_payload()
    if mutation == "unmarked": ledger["status"] = "COMPLETE"; report.clear()
    if mutation == "report_status": report["direction_projection_status"] = "COMPLETE"
    if mutation == "embedded_report": report["direction_based_projection"] = {}
    if mutation == "events": ledger["events"] = [{"action": "BUY"}]
    if mutation == "summary": ledger["summary"] = {"ending_cash_base": 100}
    if mutation == "holdings": ledger["ending_positions"] = {"AAPL": 17}
    if mutation == "cash": ledger["ending_cash_base"] = 0
    if mutation == "allocations": ledger["ending_allocations"] = [{"quantity": 17}]
    if mutation == "actions": rows[0]["direction_based_action"] = "BUY"
    if mutation == "missing_points": ledger["unavailable_points"] = []
    if mutation == "unknown_symbol": ledger["unavailable_points"][0]["symbol"] = "UNKNOWN"
    if mutation == "missing_reason": ledger["reason"] = ""
    if mutation == "probability": rows[0]["calibrated_probability"] = 1.5
    if mutation == "clock": rows[0]["target_window_start"] = rows[0]["target_window_start"].tz_localize(None)
    if mutation == "live": ledger["broker_orders_enabled"] = True
    write_plan(tmp_path, rows=rows, ledger=ledger, report_updates=report)
    with pytest.raises(GameplanError):
        load_gameplan(tmp_path)
