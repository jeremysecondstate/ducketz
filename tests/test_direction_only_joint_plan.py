"""Offline source-recipe compatibility without prices, providers or live traders."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from ml.gameplan_execution_policy import (current_gameplan_policy_metadata, direction_only_rows,
    is_current_gameplan_recipe, validate_direction_only_plan)
from ml.independent_stock_targets import stock_target_windows, STOCK_TARGET_CONTRACT_VERSION
from ml.joint_capital_plan import (build_owner_package, compose_joint_plan, content_sha256,
    load_owner_package, publish_owner_package, _validate_plan_digest)
from ml.nightly_gameplan import EXECUTION_AUTHORITY
from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_VERSION, stock_direction,
    stock_direction_for_policy, saved_direction_policy_summary)
from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE, stock_price_dataset

DAY, NOW, SCOPE = "2026-10-20", "2026-10-20T10:00:00Z", "a" * 64
PEER_POLICY = "stock-direction-5001-4999-v3"


def forecasts(symbol, probability=.5001, *, version=STOCK_DIRECTION_POLICY_VERSION):
    result = []
    for index, spec in enumerate(stock_target_windows(pd.Timestamp(DAY).date())):
        start = pd.Timestamp(spec["target_window_start"])
        result.append({**spec, "id": f"forecast-{index}", "action_date": DAY, "symbol": symbol,
            "target_window_start": start.isoformat(), "target_window_end": pd.Timestamp(spec["target_window_end"]).isoformat(),
            "calibrated_probability": probability, "raw_probability": probability,
            "direction": stock_direction_for_policy(probability, version),
            "direction_policy_version": version, "direction_up_threshold": .5001, "direction_down_threshold": .4999,
            "model_status": "RESEARCH_NOT_PROMOTED", "model_family": "offline-fixture", "decision_timestamp": NOW,
            "information_available_at": NOW, "frozen_at": NOW, "broker_orders_enabled": False,
            "target_contract_version": STOCK_TARGET_CONTRACT_VERSION, "execution_authority": EXECUTION_AUTHORITY,
            "target_price_source_contract": CANONICAL_STOCK_PRICE_SOURCE,
            "target_price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
            "action_anchor_local": start.tz_convert("America/Los_Angeles").strftime("%H:%M") if spec["execution_eligible"] else None})
    return result


def price_path(symbol):
    prior = next(pd.Timestamp(r["target_window_start"]) for r in stock_target_windows(pd.Timestamp(DAY).date()) if r["route"] == "1h@gap")
    return {"contract_version": "conditional-hourly-planning-price-path-v1", "observed_at": NOW,
        "price_source_contract": CANONICAL_STOCK_PRICE_SOURCE,
        "price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
        "lookback_sessions": 504, "minimum_samples": 2, "working_half_width_bps": 475,
        "points": {f"{symbol}|{DAY}|{hour:02d}:00": {"status": "AVAILABLE", "symbol": symbol,
            "action_date": DAY, "clock_local": f"{hour:02d}:00",
            "timestamp": pd.Timestamp(f"{DAY} {hour:02d}:00", tz="America/Los_Angeles").isoformat(),
            "planned_price_low": 10, "planned_price_mid": 10.5, "planned_price_high": 11,
            "sample_count": 2, "reference_price": 10.5, "ratio_median": 1,
            "reference_session": prior.tz_convert("America/Los_Angeles").date().isoformat(),
            "reference_observed_at": prior.isoformat()} for hour in range(4, 18)}}


def package(owner="scout", symbol="ABCL", probability=.5001, *, current=True, peer=False, **changes):
    rows = forecasts(symbol, probability, version=PEER_POLICY if peer else STOCK_DIRECTION_POLICY_VERSION)
    if current:
        rows = direction_only_rows(pd.DataFrame(rows), action_date=DAY)[0].to_dict("records")
        if peer:
            for row in rows:
                row["direction_policy_version"] = PEER_POLICY
    names = ["receipt_sha256", "manifest_sha256", "forecasts_sha256"]
    if not current:
        names.append("price_path_sha256")
    settings = dict(owner_id=owner, run_id=f"run-{owner}", source_revision=None,
        action_date=DAY, frozen_symbols=[symbol], created_at=NOW,
        source_hashes={name: content_sha256([owner, name]) for name in names}, forecasts=rows,
        direction_only=current)
    if not current:
        settings["price_path"] = price_path(symbol)
    settings.update(changes)
    value = build_owner_package(**settings)
    if peer:
        value["direction_policy_version"] = PEER_POLICY
        value["package_sha256"] = content_sha256({k: v for k, v in value.items() if k != "package_sha256"})
    return value


def compose(inputs=None, snapshot=None):
    inputs = inputs or [package(peer=True), package("atlas", "AAPL")]
    return compose_joint_plan(inputs, snapshot, action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in inputs},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in inputs},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")


@pytest.mark.parametrize("horizon", ["1h", "4h", "1d", "1w"])
@pytest.mark.parametrize("probability,action", [(.4999, "SELL"), (.49995, "HOLD"), (.5, "HOLD"),
    (.50005, "HOLD"), (.5001, "BUY")])
def test_current_directions_ignore_model_gates_and_missing_capacity(horizon, probability, action):
    row = dict(id="fixture", symbol="AAPL", model_group=horizon, execution_eligible=True,
        calibrated_probability=probability, direction="NO_EDGE", model_status="RESEARCH_NO_TARGET_HISTORY",
        symbol_fitted_target_rows=0, symbol_route_fitted_target_rows=0)
    original = deepcopy(row)
    rows, ledger = direction_only_rows(pd.DataFrame([row]), action_date=DAY,
        snapshot={"available_cash": 0, "held_shares": {}, "quotes": {}})
    assert rows.iloc[0].direction_based_action == action
    assert rows.iloc[0].source_saved_direction == "NO_EDGE"
    assert row == original and ledger["events"] == []
    quantity = rows.iloc[0].direction_based_trade_quantity
    assert quantity == 0 if action == "HOLD" else pd.isna(quantity)


def test_peer_policy_alias_preserves_identity_and_historical_unrecorded_status():
    assert stock_direction_for_policy(.50005, PEER_POLICY) == "NO_EDGE"
    assert stock_direction_for_policy(.50005, "stock-direction-50-v2") == "BULLISH"
    assert stock_direction_for_policy(.50005, None) == "BULLISH"
    summary = saved_direction_policy_summary([{"direction_policy_version": PEER_POLICY}, {}])
    assert summary["direction_policy_versions"] == [PEER_POLICY]
    assert summary["unrecorded_direction_policy_forecasts"] == 1
    assert summary["direction_policy_cutoffs"][PEER_POLICY]["boundaries"] == "inclusive"


def test_peer_current_package_roundtrip_preserves_exact_source_and_needs_no_prices(tmp_path):
    value = package(peer=True)
    before = deepcopy(value)
    path = publish_owner_package(tmp_path, value)
    loaded = load_owner_package(tmp_path, path, expected_sha256=value["package_sha256"])
    assert loaded == before == value
    assert "price_path" not in loaded and "price_path_sha256" not in loaded["source_hashes"]
    assert loaded["direction_policy_version"] == PEER_POLICY
    assert loaded["content_sha256"] == {"forecasts": content_sha256(loaded["forecasts"])}


@pytest.mark.parametrize("legacy", [False, True])
def test_current_and_mixed_composition_preserve_probabilities_without_estimated_quantities(legacy):
    inputs = [package(peer=True, probability=.4999), package("atlas", "AAPL", .50005, current=not legacy)]
    originals = deepcopy(inputs)
    state = {"account_scope_sha256": SCOPE, "observed_at": NOW, "available_cash": 0,
        "held_shares": {"ABCL": 0, "AAPL": 0}, "pending_sell_shares": {}, "quotes": {}}
    plan = compose(inputs, state)
    validate_direction_only_plan(plan)
    _validate_plan_digest(plan, plan["plan_sha256"])
    assert inputs == originals and compose(list(reversed(inputs)), state) == plan
    assert plan["ledger"]["status"] == "DIRECTION_ONLY" and plan["ledger"]["events"] == []
    assert plan["account_snapshot_sha256"] == content_sha256(state)
    assert plan["source_cross_horizon_fallback_policies"] == {
        source["owner_id"]: source["cross_horizon_fallback_policy"] for source in originals}
    assert all(set(binding) == {"run_id", "source_revision", "source_revision_status", "source_reference",
        "frozen_symbols", "created_at", "source_hashes", "content_sha256", "package_sha256"}
        for binding in plan["input_bindings"].values())
    for row in plan["forecasts"]:
        original = next(p for p in originals if p["owner_id"] == row["owner_id"])
        source = next(r for r in original["forecasts"] if r["id"] == row["original_forecast_id"])
        assert row["calibrated_probability"] == source["calibrated_probability"]
        assert row["source_saved_direction"] == source.get("source_saved_direction", source["direction"])
        assert row["target_window_start"] == source["target_window_start"]
        assert not any(k.startswith(("projected_", "planning_", "trade_")) for k in row)
        if row["execution_eligible"]:
            assert row["direction_based_action"] == ("SELL" if row["symbol"] == "ABCL" else "HOLD")
            assert row["direction_based_trade_quantity"] == (None if row["symbol"] == "ABCL" else 0)


@pytest.mark.parametrize("mutation", ["hash", "extra-price-hash", "cutoff", "row-recipe", "label", "content"])
def test_current_owner_reader_rejects_malformed_or_tampered_packages(tmp_path, mutation):
    value = package(peer=True)
    if mutation == "hash": value["source_hashes"]["forecasts_sha256"] = "bad"
    elif mutation == "extra-price-hash": value["source_hashes"]["price_path_sha256"] = "b" * 64
    elif mutation == "cutoff": value["direction_up_threshold"] = .55
    elif mutation == "row-recipe": value["forecasts"][0]["execution_quantity_basis"] = "estimated"
    elif mutation == "label": value["forecasts"][0]["direction"] = "BEARISH"
    else: value["content_sha256"]["forecasts"] = "b" * 64
    path = tmp_path / "selected.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_owner_package(tmp_path, path, expected_sha256=value["package_sha256"])


@pytest.mark.parametrize("mutation", ["quantity", "ledger", "policy", "action"])
def test_current_plan_validation_rejects_legacy_execution_caps_and_wrong_directions(mutation):
    value = compose()
    if mutation == "quantity": value["forecasts"][0]["direction_based_trade_quantity"] = 1
    elif mutation == "ledger": value["ledger"]["events"] = [{"quantity": 1}]
    elif mutation == "policy": value["direction_up_threshold"] = .55
    else: value["forecasts"][0]["direction_based_action"] = "HOLD"
    with pytest.raises(ValueError): validate_direction_only_plan(value)


def test_retired_projection_does_not_block_or_change_current_direction():
    value = compose()
    value["forecasts"][0]["projected_trade_budget"] = 1
    before = deepcopy(value)
    validate_direction_only_plan(value)
    assert value == before
    rows, _ = direction_only_rows(pd.DataFrame(value["forecasts"]), action_date=DAY)
    assert "projected_trade_budget" not in rows
    for field in ("calibrated_probability", "direction", "direction_based_action", "direction_based_trade_quantity"):
        assert rows.iloc[0][field] == before["forecasts"][0][field]
