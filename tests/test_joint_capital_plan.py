from copy import deepcopy
from dataclasses import replace
import json

import pandas as pd
import pytest

from ml.joint_capital_plan import (build_owner_package, compose_joint_plan,
    content_sha256, publish_joint_plan, load_joint_plan, publish_owner_package, load_owner_package, RANKING_POLICY)
from ml.stock_trader.contracts import StockTraderPolicy
from ml.independent_stock_targets import stock_target_windows, STOCK_TARGET_CONTRACT_VERSION
from ml.nightly_gameplan import EXECUTION_AUTHORITY
from ml.gameplan_price_bands import PLANNING_PRICE_PATH_CONTRACT
from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE, stock_price_dataset


DAY = "2026-09-09"
NOW = "2026-09-09T10:00:00Z"
SCOPE = "a" * 64
POLICY = replace(StockTraderPolicy(), minimum_order_notional=0)


def records(symbol, probability=.6, *, hour=4):
    result = []
    for index, spec in enumerate(stock_target_windows(pd.Timestamp(DAY).date())):
        start = pd.Timestamp(spec["target_window_start"])
        selected = spec["execution_eligible"] and spec["model_group"] == "1h" and start.tz_convert("America/Los_Angeles").hour == hour
        value = probability if selected else .5
        result.append({**spec, "id": f"forecast-{index}", "action_date": DAY, "symbol": symbol,
            "target_window_start": start.isoformat(), "target_window_end": pd.Timestamp(spec["target_window_end"]).isoformat(),
            "calibrated_probability": value, "direction": "BULLISH" if value > .5 else "BEARISH" if value < .5 else "NO_EDGE",
            "model_status": "PROMOTED", "model_family": "fixture", "decision_timestamp": NOW,
            "information_available_at": NOW, "frozen_at": NOW, "broker_orders_enabled": False,
            "target_contract_version": STOCK_TARGET_CONTRACT_VERSION, "execution_authority": EXECUTION_AUTHORITY,
            "target_price_source_contract": CANONICAL_STOCK_PRICE_SOURCE,
            "target_price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
            "action_anchor_local": start.tz_convert("America/Los_Angeles").strftime("%H:%M") if spec["execution_eligible"] else None})
    return sorted(result, key=lambda row: row["calibrated_probability"] == .5)


def prices(symbol):
    previous_close = next(pd.Timestamp(spec["target_window_start"]) for spec in stock_target_windows(pd.Timestamp(DAY).date()) if spec["route"] == "1h@gap")
    return {"contract_version": PLANNING_PRICE_PATH_CONTRACT, "observed_at": (pd.Timestamp(NOW) - pd.Timedelta(minutes=1)).isoformat(),
        "price_source_contract": CANONICAL_STOCK_PRICE_SOURCE, "price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
        "lookback_sessions": 504, "minimum_samples": 2, "working_half_width_bps": 475,
        "points": {f"{symbol}|{DAY}|{hour:02d}:00": {"status": "AVAILABLE", "symbol": symbol,
            "action_date": DAY, "clock_local": f"{hour:02d}:00", "timestamp": pd.Timestamp(f"{DAY} {hour:02d}:00", tz="America/Los_Angeles").isoformat(),
            "planned_price_low": 10, "planned_price_mid": 10.5, "planned_price_high": 11,
            "sample_count": 2, "reference_price": 10.5, "ratio_median": 1,
            "reference_session": previous_close.tz_convert("America/Los_Angeles").date().isoformat(),
            "reference_observed_at": previous_close.isoformat()}
            for hour in range(4, 18)}}


def package(owner="scout", symbol="ABCL", probability=.6, *, hour=4, rows=None, path=None):
    return build_owner_package(owner_id=owner, run_id=f"run-{owner}", source_revision="b" * 40,
        action_date=DAY, frozen_symbols=[symbol], created_at=NOW,
        source_hashes={name: content_sha256([owner, name]) for name in
            ("receipt_sha256", "manifest_sha256", "forecasts_sha256", "price_path_sha256")},
        forecasts=rows or records(symbol, probability, hour=hour), price_path=path or prices(symbol))


def snapshot(cash=20):
    symbols = ("ABCL", "AAPL")
    return {"account_scope_sha256": SCOPE, "observed_at": NOW, "status": "OBSERVED",
        "cash_status": "CASH_ONLY_BOUNDED", "account_equity": 1000, "available_cash": cash,
        "reserved_cash": 0, "gross_exposure": 0, "held_shares": dict.fromkeys(symbols, 0),
        "symbol_exposure": dict.fromkeys(symbols, 0), "stock_market_value_by_symbol": dict.fromkeys(symbols, 0),
        "other_symbol_exposure": dict.fromkeys(symbols, 0), "pending_buy_shares": {}, "pending_sell_shares": {},
        "quotes": {symbol: {"ask": 11} for symbol in symbols},
        "ownership": {"safe_for_planning": True, "active_allocations": [], "blocked_symbols": []}}


def compose(packages=None, state=None, **changes):
    packages = packages if packages is not None else [package(), package("atlas", "AAPL", .7)]
    settings = dict(action_date=DAY, expected_universes={"scout": ["ABCL"], "atlas": ["AAPL"]},
        expected_package_sha256={item["owner_id"]: item["package_sha256"] for item in packages},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas", policy=POLICY)
    settings.update(changes)
    return compose_joint_plan(packages, state if state is not None else snapshot(), **settings)


def test_two_plans_cannot_each_spend_the_same_starting_cash():
    plan = compose()
    assert plan["ledger"]["summary"]["starting_cash"] == 20
    assert plan["ledger"]["summary"]["buy_events"] == 1
    assert plan["ledger"]["events"][0]["symbol"] == "AAPL"
    assert plan["ledger"]["summary"]["ending_cash_low"] == 9
    eligible = [row for row in plan["forecasts"] if row["execution_eligible"] and row["calibrated_probability"] != .5]
    assert {row["owner_id"]: row["direction_based_trade_quantity"] for row in eligible} == {"atlas": 1, "scout": 0}
    assert {row["projected_cash_after_low"] for row in eligible} == {9}
    assert plan["ranking_policy"] == RANKING_POLICY
    assert not plan["live_capital_reserved"] and not plan["live_orders_authorized"]


def test_cash_released_by_other_owners_sale_funds_only_conditional_projection():
    state = snapshot(cash=2)
    state["held_shares"]["ABCL"] = 2
    state["symbol_exposure"]["ABCL"] = state["stock_market_value_by_symbol"]["ABCL"] = 20
    state["gross_exposure"] = 20
    plan = compose([package(probability=.4), package("atlas", "AAPL", .7, hour=5)], state)
    events = plan["ledger"]["events"]
    assert [(event["owner_id"], event["action"], event["quantity"]) for event in events] == [("scout", "SELL", 2), ("atlas", "BUY", 1)]
    assert all(event["fill_basis"] == "CONDITIONAL_PLANNING_FILL" for event in events)
    assert plan["ledger"]["no_fill_baseline"]["cash"] == 2
    assert plan["cash_basis"] == "ONE_ACCOUNT_SNAPSHOT_CONDITIONAL_ON_PROJECTED_FILLS"
    assert any("broker makes cash available" in item for item in plan["limitations"])
    assert plan["sole_executor_owner"] == "atlas"
    assert plan["owner_views"]["scout"]["role"] == "FORECAST_PRODUCER_ONLY"


def test_source_ids_can_overlap_between_owners_but_global_ids_and_views_are_bound():
    inputs = [package(), package("atlas", "AAPL")]
    before = deepcopy(inputs)
    plan = compose(inputs)
    assert inputs == before
    assert len({row["id"] for row in plan["forecasts"]}) == 48
    assert len({row["original_forecast_id"] for row in plan["forecasts"]}) == 24
    by_owner = {owner: {row["original_forecast_id"]: row for row in plan["forecasts"] if row["owner_id"] == owner}
                for owner in ("scout", "atlas")}
    for source in inputs:
        for original in source["forecasts"]:
            projected = by_owner[source["owner_id"]][original["id"]]
            assert all(projected[key] == value for key, value in original.items() if key != "id")
    for owner, view in plan["owner_views"].items():
        assert view["plan_id"] == plan["plan_id"]
        assert len(view["forecast_ids"]) == 24
    assert compose(list(reversed(inputs))) == plan


def test_saved_abstention_remains_neutral_despite_bearish_probability():
    rows = records("ABCL", .49)
    rows[0].update(direction="NO_EDGE", model_status="RESEARCH_NO_TARGET_HISTORY", symbol_fitted_target_rows=0,
                   symbol_route_fitted_target_rows=0)
    plan = compose([package(rows=rows), package("atlas", "AAPL")])
    abstention = next(row for row in plan["forecasts"] if row["owner_id"] == "scout" and row["calibrated_probability"] == .49)
    assert abstention["calibrated_probability"] == .49
    assert abstention["direction"] == "NO_EDGE"
    assert abstention["direction_based_trade_quantity"] == 0
    assert abstention["direction_based_reason"] == "NEUTRAL"


@pytest.mark.parametrize("mutation", ["missing-peer", "duplicate-owner", "overlapping-symbols", "wrong-date", "wrong-universe", "unknown-executor"])
def test_rejects_ambiguous_joint_scope(mutation):
    inputs = [package(), package("atlas", "AAPL")]
    settings = {}
    if mutation == "missing-peer": inputs.pop()
    elif mutation == "duplicate-owner": inputs[1] = deepcopy(inputs[0])
    elif mutation == "overlapping-symbols":
        inputs[1] = package("atlas", "ABCL")
        settings["expected_universes"] = {"scout": ["ABCL"], "atlas": ["ABCL"]}
    elif mutation == "wrong-date": settings["action_date"] = "2026-09-10"
    elif mutation == "wrong-universe": settings["expected_universes"] = {"scout": ["DBX"], "atlas": ["AAPL"]}
    else: settings["executor_owner"] = "third-pc"
    with pytest.raises(ValueError): compose(inputs, **settings)


def test_tampered_or_resigned_package_cannot_replace_independently_selected_bytes():
    inputs = [package(), package("atlas", "AAPL")]
    trusted = {item["owner_id"]: item["package_sha256"] for item in inputs}
    inputs[0]["source_revision"] = "c" * 40
    with pytest.raises(ValueError, match="digest"): compose(inputs, expected_package_sha256=trusted)
    inputs[0]["package_sha256"] = content_sha256({k: v for k, v in inputs[0].items() if k != "package_sha256"})
    with pytest.raises(ValueError, match="independently selected"): compose(inputs, expected_package_sha256=trusted)


@pytest.mark.parametrize("field,value", [("available_cash", float("nan")), ("available_cash", float("inf")),
    ("available_cash", True), ("available_cash", -1), ("account_equity", 0)])
def test_invalid_account_numbers_never_reach_projection(field, value):
    state = snapshot()
    state[field] = value
    with pytest.raises(ValueError): compose(state=state)


@pytest.mark.parametrize("mutation", ["scope", "stale", "future", "missing-holding", "unsafe-ownership", "duplicate-allocation"])
def test_rejects_unusable_authoritative_snapshot(mutation):
    state = snapshot()
    if mutation == "scope": state["account_scope_sha256"] = "d" * 64
    elif mutation == "stale": state["observed_at"] = "2026-09-09T09:00:00Z"
    elif mutation == "future": state["observed_at"] = "2026-09-09T10:00:01Z"
    elif mutation == "missing-holding": del state["held_shares"]["ABCL"]
    elif mutation == "unsafe-ownership": state["ownership"]["safe_for_planning"] = False
    else:
        state["ownership"]["active_allocations"] = [{"symbol": "ABCL", "horizon": "1h", "owned_shares": 0}] * 2
    with pytest.raises(ValueError): compose(state=state)


@pytest.mark.parametrize("mutation", ["duplicate-id", "missing-row", "probability-nan", "probability-bool", "probability-large", "direction", "naive-time"])
def test_invalid_frozen_forecasts_fail_before_a_plan_is_returned(mutation):
    rows = records("ABCL")
    if mutation == "duplicate-id": rows[1]["id"] = rows[0]["id"]
    elif mutation == "missing-row": rows.pop()
    elif mutation == "probability-nan": rows[0]["calibrated_probability"] = float("nan")
    elif mutation == "probability-bool": rows[0]["calibrated_probability"] = True
    elif mutation == "probability-large": rows[0]["calibrated_probability"] = 2
    elif mutation == "direction": rows[0]["direction"] = "BEARISH"
    else: rows[0]["target_window_start"] = "2026-09-09T04:00:00"
    with pytest.raises(ValueError): compose([package(rows=rows), package("atlas", "AAPL")])


def test_optional_missing_forecast_values_normalize_without_altering_required_probability():
    rows = records("ABCL")
    rows[0]["optional_metric"] = float("nan")
    rows[0]["optional_label"] = pd.NA
    plan = compose([package(rows=rows), package("atlas", "AAPL")])
    row = next(row for row in plan["forecasts"] if row["owner_id"] == "scout" and row["calibrated_probability"] == .6)
    assert row["optional_metric"] is None and row["optional_label"] is None
    assert row["calibrated_probability"] == .6
    json.dumps(plan, allow_nan=False)


def test_different_price_capture_times_allowed_but_policy_mismatch_rejected():
    first, second = prices("ABCL"), prices("AAPL")
    first["reference_completion"] = {"symbols": ["ABCL"]}
    second["reference_completion"] = {"symbols": ["AAPL"]}
    second["observed_at"] = "2026-09-09T09:58:00Z"
    compose([package(path=first), package("atlas", "AAPL", path=second)])
    second["lookback_sessions"] = 120
    with pytest.raises(ValueError, match="price policies"): compose([package(path=first), package("atlas", "AAPL", path=second)])


def test_package_future_or_stale_and_future_price_evidence_are_rejected():
    inputs = [package(), package("atlas", "AAPL")]
    with pytest.raises(ValueError, match="stale or future"): compose(inputs, as_of="2026-09-09T09:59:00Z")
    with pytest.raises(ValueError, match="stale or future"): compose(inputs, as_of="2026-09-17T10:00:00Z")
    path = prices("ABCL")
    path["observed_at"] = "2026-09-09T10:00:01Z"
    with pytest.raises(ValueError, match="postdates"): package(path=path)


def test_unsupported_cross_horizon_policy_is_not_silently_omitted():
    source = package()
    source["cross_horizon_fallback_policy"] = {"enabled": True}
    source["package_sha256"] = content_sha256({k: v for k, v in source.items() if k != "package_sha256"})
    with pytest.raises(ValueError, match="Unsupported or premature"): compose([source, package("atlas", "AAPL")])


def test_bounded_json_publication_is_immutable_and_round_trips(tmp_path):
    plan = compose()
    path = publish_joint_plan(tmp_path, plan)
    assert load_joint_plan(tmp_path, path, expected_sha256=plan["plan_sha256"]) == plan
    with pytest.raises(FileExistsError): publish_joint_plan(tmp_path, plan)
    plan["sole_executor_owner"] = "scout"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="contents"): load_joint_plan(tmp_path, path, expected_sha256=plan["plan_sha256"])


def test_reader_rejects_escape_missing_and_oversize_files(tmp_path, monkeypatch):
    plan = compose()
    path = publish_joint_plan(tmp_path, plan)
    with pytest.raises(ValueError, match="escapes"): load_joint_plan(tmp_path / "child", path, expected_sha256=plan["plan_sha256"])
    with pytest.raises(FileNotFoundError): load_joint_plan(tmp_path, tmp_path / "absent.json", expected_sha256=plan["plan_sha256"])
    monkeypatch.setattr("ml.joint_capital_plan.MAXIMUM_JSON_BYTES", 10)
    with pytest.raises(ValueError, match="size bound"): load_joint_plan(tmp_path, path, expected_sha256=plan["plan_sha256"])


@pytest.mark.parametrize("mutation", ["all-context", "wrong-route", "future-freeze", "old-prices", "unknown-price-contract", "missing-completion", "wrong-price-source"])
def test_native_grid_and_causal_price_evidence_are_required(mutation):
    rows, path = records("ABCL"), prices("ABCL")
    if mutation == "all-context":
        for row in rows:
            row.update(execution_eligible=False, target_role="CONTEXT", action_anchor_local=None)
    elif mutation == "wrong-route": rows[0]["route"] = "fabricated"
    elif mutation == "future-freeze": rows[0]["frozen_at"] = "2026-09-09T10:00:01Z"
    elif mutation == "old-prices": path["observed_at"] = "2020-01-01T00:00:00Z"
    elif mutation == "unknown-price-contract": path["contract_version"] = "arbitrary"
    elif mutation == "missing-completion": path["contract_version"] = "conditional-hourly-planning-price-path-v3"
    else: path["price_dataset"] = "OTHER"
    with pytest.raises(ValueError): compose([package(rows=rows, path=path), package("atlas", "AAPL")])


def test_unrecorded_source_revision_and_exact_provenance_are_preserved():
    args = dict(owner_id="scout", run_id="source", source_revision=None, action_date=DAY,
        frozen_symbols=["ABCL"], created_at=NOW, source_hashes=package()["source_hashes"],
        forecasts=records("ABCL"), price_path=prices("ABCL"))
    unknown = build_owner_package(**args)
    assert unknown["source_revision"] is None and unknown["source_revision_status"] == "UNRECORDED"
    reference = {"kind": "SAVED_ARTIFACT_PROVENANCE", "source_revision": "e" * 40,
        "receipt_sha256": args["source_hashes"]["receipt_sha256"], "reference_sha256": "f" * 64}
    known = build_owner_package(**{**args, "source_revision": "e" * 40}, source_reference=reference)
    assert known["source_reference"] == reference
    reference["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="provenance reference"):
        build_owner_package(**{**args, "source_revision": "e" * 40}, source_reference=reference)


def test_owner_package_round_trip_checks_external_binding_and_refuses_overwrite(tmp_path):
    source = package()
    path = publish_owner_package(tmp_path, source)
    assert load_owner_package(tmp_path, path, expected_sha256=source["package_sha256"]) == source
    assert publish_owner_package(tmp_path, source) == path
    with pytest.raises(ValueError, match="independently selected"):
        load_owner_package(tmp_path, path, expected_sha256="f" * 64)


def test_matching_native_fallback_policy_is_applied_once_globally(monkeypatch):
    from ml.stock_trader.cross_horizon_fallback import policy_for_action_date
    monkeypatch.setitem(globals(), "DAY", "2026-10-02")
    monkeypatch.setitem(globals(), "NOW", "2026-10-02T10:00:00Z")
    fallback = policy_for_action_date(DAY)
    inputs = [package(probability=.4), package("atlas", "AAPL", .5)]
    for source in inputs:
        source["cross_horizon_fallback_policy"] = fallback
        source["package_sha256"] = content_sha256({k: v for k, v in source.items() if k != "package_sha256"})
    state = snapshot(cash=2)
    state["held_shares"]["ABCL"] = 48
    state["symbol_exposure"]["ABCL"] = state["stock_market_value_by_symbol"]["ABCL"] = 480
    state["gross_exposure"] = 480
    state["ownership"]["active_allocations"] = [{"symbol": "ABCL", "horizon": "1w", "owned_shares": 48,
        "target_end": "2026-10-09T00:00:00Z", "allocation_id_sha256": "9" * 64}]
    plan = compose(inputs, state)
    assert plan["cross_horizon_fallback_policy"] == fallback
    events = plan["ledger"]["events"]
    assert len(events) == 1 and events[0]["reason"] == "BEARISH_CROSS_HORIZON_FALLBACK"
    assert events[0]["quantity"] == 1
    del state["ownership"]["active_allocations"][0]["allocation_id_sha256"]
    with pytest.raises(ValueError, match="allocation identity"): compose(inputs, state)
    inputs[0]["cross_horizon_fallback_policy"] = None
    inputs[0]["package_sha256"] = content_sha256({k: v for k, v in inputs[0].items() if k != "package_sha256"})
    with pytest.raises(ValueError, match="fallback policies disagree"): compose(inputs)


@pytest.mark.parametrize("mutation", ["zero-samples", "missing-anchor", "future-anchor", "zero-width", "wrong-midpoint", "missing-sample-count"])
def test_available_price_points_require_causal_observed_statistics(mutation):
    path = prices("ABCL")
    point = next(iter(path["points"].values()))
    if mutation == "zero-samples": point["sample_count"] = 0
    elif mutation == "missing-anchor": point["reference_price"] = None
    elif mutation == "future-anchor": point["reference_observed_at"] = "2026-09-09T12:00:00Z"
    elif mutation == "zero-width": path["working_half_width_bps"] = 0
    elif mutation == "wrong-midpoint": point["planned_price_mid"] = 9
    else: del point["sample_count"]
    with pytest.raises(ValueError): package(path=path)


@pytest.mark.parametrize("day,utc_hour", [("2026-10-30", 11), ("2026-11-02", 12)])
def test_all_22_symbols_share_one_capital_plan_across_pacific_dst(monkeypatch, day, utc_hour):
    monkeypatch.setitem(globals(), "DAY", day)
    monkeypatch.setitem(globals(), "NOW", pd.Timestamp(f"{day} 03:00", tz="America/Los_Angeles").tz_convert("UTC").isoformat())
    universes = {"scout": ["DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN"],
        "atlas": ["AAPL", "AMZN", "COST", "CROX", "GOOG", "IONQ", "MU", "NVDA", "PATH", "SNDK", "TWST"]}
    inputs = []
    for owner, symbols in universes.items():
        source_rows = [{**row, "id": f"{symbol}:{row['id']}"} for symbol in symbols for row in records(symbol)]
        price_path = prices(symbols[0])
        price_path["points"] = {key: point for symbol in symbols for key, point in prices(symbol)["points"].items()}
        inputs.append(build_owner_package(owner_id=owner, run_id=f"run-{owner}", source_revision=None,
            action_date=day, frozen_symbols=symbols, created_at=NOW, source_hashes=package()["source_hashes"],
            forecasts=source_rows, price_path=price_path))
    state = snapshot(cash=100000)
    state["account_equity"] = 1000000
    symbols = set(universes["scout"] + universes["atlas"])
    for name in ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure"):
        state[name] = dict.fromkeys(symbols, 0)
    state["quotes"] = {symbol: {"ask": 11} for symbol in symbols}
    options = dict(action_date=day, expected_universes=universes,
        expected_package_sha256={source["owner_id"]: source["package_sha256"] for source in inputs},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas", policy=replace(POLICY, maximum_orders_per_wake=1))
    plan = compose_joint_plan(inputs, state, **options)
    assert len(plan["forecasts"]) == 528
    assert all(len(view["forecast_ids"]) == 264 for view in plan["owner_views"].values())
    assert {row["model_group"] for row in plan["forecasts"]} == {"1h", "4h", "1d", "1w"}
    assert plan["ledger"]["summary"]["starting_cash"] == 100000
    assert plan["ledger"]["summary"]["cash_buffer"] == 5000
    assert plan["ledger"]["summary"]["ending_cash_low"] >= 5000
    assert plan["ledger"]["summary"]["buy_events"] > 1  # Informational projection does not promise a live batch size.
    events = plan["ledger"]["events"]
    assert [event["symbol"] for event in events] == sorted(event["symbol"] for event in events)
    assert all(pd.Timestamp(event["timestamp"]).hour == utc_hour for event in events)
    assert all(event["executor_owner"] == "atlas" for event in events)
    assert compose_joint_plan(list(reversed(inputs)), state, **options) == plan
