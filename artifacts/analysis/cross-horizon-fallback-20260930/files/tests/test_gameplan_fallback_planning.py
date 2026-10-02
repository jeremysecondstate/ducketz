"""Prospective scenario accounting for frozen cross-horizon fallback sales."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
import json

import pandas as pd
import pytest

from ml.gameplan_cash_ledger import project_direction_trades
from ml.independent_stock_targets import stock_target_windows
from ml.stock_direction_policy import stock_direction
from ml.stock_trader.contracts import StockTraderPolicy
from ml.stock_trader.cross_horizon_fallback import policy_for_action_date, slot_quota


DAY = "2026-10-01"
POLICY = replace(StockTraderPolicy(), minimum_order_notional=0)


def reference(horizon):
    return hashlib.sha256(f"original-{horizon}".encode()).hexdigest()


def forecasts(signals=None, *, all_short_bearish=False, day=DAY):
    signals = signals or {}
    rows = []
    for window in stock_target_windows(date.fromisoformat(day)):
        hour = pd.Timestamp(window["target_window_start"]).tz_convert("America/Los_Angeles").hour
        horizon = window["model_group"]
        probability = signals.get((horizon, hour), .3 if all_short_bearish and horizon != "1w" else .5)
        rows.append({**window, "id": f"{day}:AAPL:{window['route']}", "action_date": day,
            "symbol": "AAPL", "calibrated_probability": probability, "model_status": "PROMOTED",
            "direction": stock_direction(probability), "projected_trade_quantity": 1,
            "trade_price_mid": 10.5})
    return pd.DataFrame(rows)


def state(holdings=None, *, free=0, cash=0):
    holdings = holdings or {"1w": 300}
    total = sum(holdings.values()) + free
    return {"observed_at": "2026-10-01T04:00:00Z", "status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED",
        "account_equity": 100000, "available_cash": cash, "reserved_cash": 0,
        "held_shares": {"AAPL": total}, "symbol_exposure": {"AAPL": total * 10},
        "stock_market_value_by_symbol": {"AAPL": total * 10}, "other_symbol_exposure": {"AAPL": 0},
        "gross_exposure": total * 10, "pending_buy_shares": {}, "pending_sell_shares": {},
        "quotes": {"AAPL": {"ask": 11}},
        "ownership": {"safe_for_planning": True, "blocked_symbols": [], "active_allocations": [
            {"allocation_id_sha256": reference(h), "symbol": "AAPL", "horizon": h,
             "owned_shares": q, "target_end": "2026-10-08T00:00:00Z"} for h, q in holdings.items()]}}


def prices(day=DAY):
    return {"points": {f"AAPL|{day}|{hour:02d}:00": {"status": "AVAILABLE", "symbol": "AAPL",
        "action_date": day, "clock_local": f"{hour:02d}:00", "timestamp": f"{day}T{hour:02d}:00:00-07:00",
        "planned_price_low": 10, "planned_price_mid": 10.5, "planned_price_high": 11}
        for hour in range(4, 18)}}


def run(frame=None, snapshot=None, *, enabled=True, day=DAY):
    return project_direction_trades(forecasts(all_short_bearish=True, day=day) if frame is None else frame,
        state() if snapshot is None else snapshot, prices(day), policy=POLICY, signal_driven=True,
        cross_horizon_fallback_policy=policy_for_action_date(DAY) if enabled else None)


def entry(rows, horizon, hour):
    return rows.loc[rows.execution_eligible & rows.model_group.eq(horizon)
                    & pd.to_datetime(rows.target_window_start, utc=True).dt.tz_convert("America/Los_Angeles").dt.hour.eq(hour)].iloc[0]


def test_weighted_18_slots_conserve_cash_shares_and_combined_half_cap():
    rows, report = run()
    assert len(rows) == 24
    assert report["cross_horizon_fallback"]["symbol_daily_caps"] == {"AAPL": 150}
    assert report["cross_horizon_fallback"]["symbol_used"] == {"AAPL": 150}
    assert len(report["events"]) == 18
    assert report["ending_positions"] == {"AAPL": 150}
    assert [entry(rows, h, 4).direction_based_trade_quantity for h in ("1h", "4h", "1d")] == [-6, -12, -19]
    assert len(report["hourly"]) == 14
    for endpoint, price in (("low", 10), ("base", 10.5), ("high", 11)):
        balance = 0
        for event in report["events"]:
            assert event[f"cash_before_{endpoint}"] == balance
            balance += event["quantity"] * price
            assert event[f"cash_{endpoint}"] == balance
            attribution = event["cross_horizon_fallback"]
            assert attribution["donor_horizon"] == "1w"
            assert attribution["trigger_forecast_id"] == event["forecast_id"]
            assert attribution["donor_used_after"] <= attribution["donor_daily_cap"] == 150
            assert attribution["quantity"] <= attribution["slot_quota"]
        assert report["summary"][f"ending_cash_{endpoint}"] == balance == 150 * price
        for hour in report["hourly"]:
            assert hour[f"cash_{endpoint}"] == sum(e["quantity"] * price for e in report["events"]
                                                    if e["timestamp"] <= hour["timestamp"])
    assert sum(lot["quantity"] for lot in report["ending_allocations"]) == 150
    assert report["no_fill_baseline"]["held_shares"] == {"AAPL": 300}


def test_only_saved_policy_enables_fallback_and_premature_policy_rejected():
    rows, report = run(enabled=False)
    assert not report["events"] and report["ending_positions"] == {"AAPL": 300}
    assert "cross_horizon_fallback_policy" not in report
    assert entry(rows, "1h", 4).direction_based_reason == "NO_AVAILABLE_SHARES_FOR_THIS_HORIZON"
    with pytest.raises(ValueError, match="premature"):
        run(day="2026-09-30")


def test_nearest_single_donor_and_normal_sales_precede_all_fallbacks():
    rows, report = run(forecasts({("1h", 4): .3}), state({"4h": 100, "1d": 100, "1w": 100}))
    assert report["events"][0]["cross_horizon_fallback"]["donor_horizon"] == "4h"
    assert entry(rows, "1h", 4).fallback_donor_allocation_id_sha256 == reference("4h")
    rows, report = run(forecasts({("1h", 4): .3, ("1d", 4): .3}), state({"1d": 100, "1w": 200}))
    assert [event["reason"] for event in report["events"]] == ["BEARISH_SELL", "BEARISH_CROSS_HORIZON_FALLBACK"]
    assert report["events"][0]["quantity"] == 100
    assert report["events"][1]["cross_horizon_fallback"]["donor_horizon"] == "1w"
    assert report["cross_horizon_fallback"]["symbol_used"] == {"AAPL": 6}


@pytest.mark.parametrize("pending", ["buy", "sell", "bullish"])
def test_donor_pending_order_or_same_clock_bullish_is_excluded(pending):
    snapshot = state({"4h": 100, "1d": 100, "1w": 100})
    signals = {("1h", 4): .3}
    if pending == "bullish":
        signals["4h", 4] = .7  # Excluded even before cash sizing decides whether it can buy.
    else:
        snapshot["ownership"]["active_allocations"][0][f"reserved_{pending}_shares"] = 1
        snapshot[f"pending_{pending}_shares"] = {"AAPL": 1}
        snapshot["reserved_cash"] = 11 if pending == "buy" else 0
    rows, report = run(forecasts(signals), snapshot)
    assert entry(rows, "1h", 4).fallback_donor_horizon == "1d"
    assert report["cross_horizon_fallback"]["donors"][reference("4h")]["used"] == 0


@pytest.mark.parametrize("obstruction", ["own_reserved", "own_pending_buy", "external_pending_buy", "unallocated_reserved", "free_consumed", "blocked"])
def test_empty_due_to_reservations_prior_same_clock_use_or_block_never_triggers(obstruction):
    snapshot = state()
    signals = {("1h", 4): .3, ("4h", 4): .3}
    target = "1h"
    if obstruction == "own_reserved":
        snapshot = state({"1h": 1, "1w": 300})
        snapshot["ownership"]["active_allocations"][0]["reserved_sell_shares"] = 1
        snapshot["pending_sell_shares"] = {"AAPL": 1}
    elif obstruction == "own_pending_buy":
        snapshot["ownership"]["active_allocations"].append({"allocation_id_sha256": reference("1h"), "symbol": "AAPL",
            "horizon": "1h", "owned_shares": 0, "reserved_buy_shares": 1})
        snapshot["pending_buy_shares"] = {"AAPL": 1}
        snapshot["reserved_cash"] = 11
    elif obstruction == "unallocated_reserved":
        snapshot = state(free=1)
        snapshot["pending_sell_shares"] = {"AAPL": 1}
    elif obstruction == "external_pending_buy":
        snapshot["pending_buy_shares"] = {"AAPL": 1}
        snapshot["reserved_cash"] = 11
    elif obstruction == "free_consumed":
        snapshot = state(free=1)
        target = "4h"
    else:
        snapshot["ownership"]["blocked_symbols"] = ["AAPL"]
    rows, report = run(forecasts(signals), snapshot)
    assert entry(rows, target, 4).direction_based_trade_quantity == 0
    assert not any(event.get("cross_horizon_fallback", {}).get("trigger_horizon") == target for event in report["events"])


def test_unused_quota_does_not_roll_and_new_buys_do_not_grow_baseline():
    rows, report = run(forecasts({("1h", 16): .3}))
    expected = slot_quota(150, "1h", f"{DAY}T16:00:00-07:00", DAY)
    assert entry(rows, "1h", 16).direction_based_trade_quantity == -expected
    assert report["cross_horizon_fallback"]["symbol_used"] == {"AAPL": expected}
    rows, report = run(forecasts({("1w", 4): .7, ("1h", 5): .3}), state(cash=200))
    assert report["events"][0]["reason"] == "BULLISH_BUY"
    assert report["cross_horizon_fallback"]["symbol_daily_caps"] == {"AAPL": 150}
    assert report["cross_horizon_fallback"]["donors"][reference("1w")]["initial_shares"] == 300


def test_weekly_never_falls_back_and_individual_donor_cap_is_not_combined_cap():
    snapshot = state({"4h": 2, "1d": 100, "1w": 198})
    rows, report = run(forecasts({("1h", 4): .3, ("1h", 5): .3}), snapshot)
    first, second = report["events"]
    assert first["quantity"] == 1  # Do not top up this order from a second donor.
    assert first["cross_horizon_fallback"]["donor_horizon"] == "4h"
    assert second["cross_horizon_fallback"]["donor_horizon"] == "1d"
    rows, report = run(forecasts({("1w", 4): .3}), state({"1d": 300}))
    assert not report["events"] and entry(rows, "1w", 4).direction_based_trade_quantity == 0


def test_new_session_caps_reset_from_remaining_actual_snapshot():
    _, first = run()
    rows, second = run(forecasts(all_short_bearish=True, day="2026-10-02"), state({"1w": 150}), day="2026-10-02")
    assert second["cross_horizon_fallback"]["symbol_daily_caps"] == {"AAPL": 75}
    assert second["ending_positions"] == {"AAPL": 75}


def test_donor_identity_and_entry_slot_ambiguity_fail_closed():
    snapshot = state()
    del snapshot["ownership"]["active_allocations"][0]["allocation_id_sha256"]
    with pytest.raises(ValueError, match="allocation identities"):
        run(snapshot=snapshot)
    frame = forecasts()
    duplicate = frame.iloc[[0]].copy()
    duplicate["id"] = "different-identity-same-slot"
    with pytest.raises(ValueError, match="unique symbol entry slots"):
        run(pd.concat([frame, duplicate], ignore_index=True))


def test_readable_reason_and_ui_preserve_trigger_and_donor():
    from app.ui.gameplan_data import _forecast, _actions, reason_text, GameplanError
    from ml.gameplan_trade_review import _plan_action, _projection_tables
    rows, report = run(forecasts({("1h", 4): .3}))
    records = rows.to_dict("records")
    forecasts_ui = tuple(_forecast(row) for row in records)
    actions = _actions(report, forecasts_ui, DAY)
    assert actions[0].fallback_donor_horizon == "1w"
    assert actions[0].fallback_donor_allocation_id_sha256 == reference("1w")
    assert reason_text(actions[0].reason, actions[0].fallback_donor_horizon) == "Bearish fallback sale from 1w holdings"
    assert _plan_action(entry(rows, "1h", 4)) == "Sell from 1w holdings — 1h bearish fallback"
    main, details = _projection_tables(report, ["AAPL"], state())
    assert "50% daily symbol cap" in "\n".join(main)
    assert "1h bearish fallback from 1w holdings" in "\n".join(details)
    changed = deepcopy(report)
    changed["events"][0]["cross_horizon_fallback"]["donor_horizon"] = "4h"
    with pytest.raises(GameplanError, match="attribution"):
        _actions(changed, forecasts_ui, DAY)


@pytest.mark.parametrize("mutation", ["slot", "symbol_cap", "donor_cap", "used", "donor_shares"])
def test_ui_refuses_semantically_forged_fallback_accounting(mutation):
    from app.ui.gameplan_data import _forecast, _actions, GameplanError
    rows, report = run(forecasts({("1h", 4): .3}))
    forecasts_ui = tuple(_forecast(row) for row in rows.to_dict("records"))
    attribution = report["events"][0]["cross_horizon_fallback"]
    if mutation == "slot":
        attribution["slot_quota"] += 1
    elif mutation == "symbol_cap":
        report["cross_horizon_fallback"]["symbol_daily_caps"]["AAPL"] += 1
    elif mutation == "donor_cap":
        report["cross_horizon_fallback"]["donors"][reference("1w")]["daily_cap"] += 1
    elif mutation == "used":
        attribution["symbol_used_before"] += 1
    else:
        attribution["donor_shares_after"] += 1
    with pytest.raises(GameplanError, match="Fallback"):
        _actions(report, forecasts_ui, DAY)


@pytest.fixture
def saved_fallback_plan(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ml import nightly_gameplan, gameplan_price_bands
    from ml.stock_trader import independent_signals
    from ml.artifacts import write_manifest, file_checksum
    from ml.gameplan_trade_planning import publish_trade_plan
    from test_gameplan_trade_planning import forecast as base_forecast, bands
    source = tmp_path / "ml/nightly-gameplan-runs/frozen"
    source.mkdir(parents=True)
    policy = policy_for_action_date(DAY)
    config = {"preparation_scope": "STOCK_ONLY", "target_contract_version": "independent-stock-targets-v1",
        "target_price_source_contract": "xnas-itch-archive-v1", "symbols": ["AAPL"], "action_date": DAY,
        "cross_horizon_fallback_policy": policy}
    payload = {"action_date": DAY, "cross_horizon_fallback_policy": policy}
    (source / "gameplan.json").write_text(json.dumps(payload))
    (source / "model-reports.json").write_text("{}")
    frame = pd.DataFrame([{**base_forecast(), **row, "decision_timestamp": "2026-10-01T00:05:00Z",
                          "frozen_at": "2026-10-01T04:05:00Z"} for row in forecasts(all_short_bearish=True).to_dict("records")])
    frame.to_parquet(source / "forecasts.parquet", index=False)
    pd.DataFrame({"id": frame.id + ":OPTION", "symbol": "AAPL", "plan_status": "NO_TRADE_STOCK_ONLY",
        "legs_json": None, "candidate_key": None, "strategy_source_run": None}).to_parquet(source / "option-strategy-intents.parquet", index=False)
    manifest = write_manifest(source, run_timestamp="2026-10-01T04:05:00Z", input_files=[],
        output_files=["gameplan.json", "model-reports.json", "forecasts.parquet", "option-strategy-intents.parquet"], configuration=config)
    receipt = {**payload, "manifest_checksum_sha256": file_checksum(source / "manifest.json")}
    (source / "receipt.json").write_text(json.dumps(receipt))
    manifest = json.loads((source / "manifest.json").read_text())
    monkeypatch.setattr(nightly_gameplan, "read_gameplan_run", lambda *a: SimpleNamespace(run_directory=source, manifest=manifest, receipt=receipt))
    monkeypatch.setattr(independent_signals, "_validated_independent_forecasts", lambda value, **kw: value)
    monkeypatch.setattr(independent_signals, "verified_promoted_model_groups", lambda *a: frozenset(("1h", "4h", "1d", "1w")))
    def entry_bands(*args, **kwargs):
        result = bands(frame.to_dict("records"))
        result["reference_completion"] = {"contract_version": "test-observed-only", "references": {}, "synthetic_bars": []}
        return result
    def price_path(*args, **kwargs):
        result = prices()
        result["working_half_width_bps"] = 20
        for point in result["points"].values():
            point.update(reason="Observed fixture", method="Fixture conditional range")
        return result
    monkeypatch.setattr(gameplan_price_bands, "build_entry_price_bands", entry_bands)
    monkeypatch.setattr(gameplan_price_bands, "build_planning_price_path", price_path)
    before = {item.name: file_checksum(item) for item in source.iterdir()}
    snapshot = state()
    original_snapshot = deepcopy(snapshot)
    run_path = publish_trade_plan(tmp_path, gameplan_run=source, snapshot_loader=lambda *a, **kw: snapshot,
        price_loader=lambda *a, **kw: (pd.DataFrame(), (), {}), clock=lambda: pd.Timestamp("2026-10-01T04:10:00Z"))
    assert snapshot == original_snapshot
    assert {item.name: file_checksum(item) for item in source.iterdir()} == before
    return SimpleNamespace(root=tmp_path, source=source, run=run_path, policy=policy)


def test_publication_and_reader_bind_saved_policy_and_donor_evidence(saved_fallback_plan):
    from app.ui.gameplan_data import load_gameplan
    from ml.artifacts import verify_manifest
    c = saved_fallback_plan
    verify_manifest(c.run)
    for name in ("report.json", "receipt.json", "direction-ledger.json"):
        assert json.loads((c.run / name).read_text())["cross_horizon_fallback_policy"] == c.policy
    assert json.loads((c.run / "manifest.json").read_text())["configuration"]["cross_horizon_fallback_policy"] == c.policy
    loaded = load_gameplan(c.root)
    assert len(loaded.forecasts) == 24 and len(loaded.actions) == 18
    assert sum(row.quantity for row in loaded.actions) == 150
    assert all(row.fallback_donor_horizon == "1w" for row in loaded.actions)
    assert "1h bearish fallback from 1w holdings" in (c.run / "Gameplan.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("artifact", ["report.json", "receipt.json", "direction-ledger.json", "manifest.json", "source"])
def test_reader_refuses_mismatched_policy_even_with_valid_output_hashes(saved_fallback_plan, artifact):
    from app.ui.gameplan_data import load_gameplan, GameplanError
    from ml.artifacts import write_manifest, file_checksum
    c = saved_fallback_plan
    if artifact == "source":
        payload = json.loads((c.source / "gameplan.json").read_text())
        payload["cross_horizon_fallback_policy"] = None
        (c.source / "gameplan.json").write_text(json.dumps(payload))
    else:
        path = c.run / artifact
        payload = json.loads(path.read_text())
        (payload["configuration"] if artifact == "manifest.json" else payload)["cross_horizon_fallback_policy"] = None
        path.write_text(json.dumps(payload))
        manifest = json.loads((c.run / "manifest.json").read_text())
        write_manifest(c.run, run_timestamp="2026-10-01T04:10:00Z", input_files=[],
            output_files=list(manifest["output_files"]), configuration=manifest["configuration"])
        receipt = json.loads((c.run / "receipt.json").read_text())
        receipt["manifest_sha256"] = file_checksum(c.run / "manifest.json")
        (c.run / "receipt.json").write_text(json.dumps(receipt))
        pointer_path = c.root / "ml/gameplan-trade-plan-latest/run.json"
        pointer = json.loads(pointer_path.read_text())
        pointer["current"]["receipt_sha256"] = file_checksum(c.run / "receipt.json")
        pointer_path.write_text(json.dumps(pointer))
    with pytest.raises(GameplanError):
        load_gameplan(c.root)
