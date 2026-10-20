"""Synthetic current-package regressions; no accounts, providers or models."""
from copy import deepcopy
import json
import socket

import pandas as pd
import pytest

from ml.gameplan_execution_policy import (current_gameplan_policy_metadata, direction_only_rows,
    is_current_gameplan_recipe, validate_direction_only_plan)
from ml.joint_capital_plan import (build_owner_package, compose_joint_plan, content_sha256,
    load_owner_package, publish_owner_package, _compose_legacy_joint_plan, direction_plan_from_saved_plan)
from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_VERSION,
    SCOUT_STOCK_DIRECTION_POLICY_VERSION, stock_direction_for_policy)
from test_joint_capital_plan import records, package as historical_package, snapshot, DAY, NOW, SCOPE
from test_joint_capital_handoff import saved, write, inventory, digest, export


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Current-package regressions must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.mark.parametrize("version", [STOCK_DIRECTION_POLICY_VERSION, SCOUT_STOCK_DIRECTION_POLICY_VERSION])
@pytest.mark.parametrize("probability,expected", [(.4999, "BEARISH"), (.5001, "BULLISH"),
    (.49990001, "NO_EDGE"), (.50009999, "NO_EDGE"), (.5, "NO_EDGE")])
def test_both_recorded_current_ids_have_identical_exact_inclusive_cutoffs(version, probability, expected):
    assert stock_direction_for_policy(probability, version) == expected


def test_unknown_saved_policy_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match="Unsupported saved stock direction policy"):
        stock_direction_for_policy(.7, "unreviewed-policy")


def test_recipe_reader_supports_dataframe_rows_and_rejects_nonrecords():
    metadata = current_gameplan_policy_metadata(DAY)
    assert is_current_gameplan_recipe(pd.Series(metadata))
    assert not is_current_gameplan_recipe(None)
    assert not is_current_gameplan_recipe("direction-live-quote-v1")
    assert not is_current_gameplan_recipe(pd.DataFrame([metadata]))


@pytest.mark.parametrize("probability,action", [(.5001, "BUY"), (.4999, "SELL"), (.5, "HOLD")])
def test_current_derivation_uses_probability_without_model_or_confidence_gate(probability, action):
    science = pd.DataFrame([dict(id="science", symbol="TEST", model_group="1h", execution_eligible=True,
        calibrated_probability=probability, raw_probability=.4170123456789012, direction="NO_EDGE",
        model_status="RESEARCH_NO_TARGET_HISTORY", symbol_fitted_target_rows=0,
        probability_target_contract="saved-target", target_window_start="2026-10-20T11:00Z")])
    original = science.copy(deep=True)
    rows, ledger = direction_only_rows(science, action_date="2026-10-20")
    pd.testing.assert_frame_equal(science, original)
    row = rows.iloc[0]
    assert row.direction_based_action == action
    assert row.calibrated_probability == probability
    assert row.raw_probability == original.iloc[0].raw_probability
    assert row.probability_target_contract == "saved-target"
    assert row.source_saved_direction == "NO_EDGE"
    assert row.source_saved_direction_policy_version is None
    assert row.direction_policy_version == STOCK_DIRECTION_POLICY_VERSION
    assert ledger["status"] == "DIRECTION_ONLY" and ledger["events"] == []
    assert row.direction_based_trade_quantity == 0 if action == "HOLD" else pd.isna(row.direction_based_trade_quantity)


def current_package(owner="scout", symbol="ABCL", *, scout_id=False):
    science = pd.DataFrame(records(symbol, .48))
    science["direction"] = "NO_EDGE"
    science["model_status"] = "RESEARCH_NOT_PROMOTED"
    rows, _ = direction_only_rows(science, action_date=DAY)
    result = build_owner_package(owner_id=owner, run_id="current-" + owner, source_revision=None,
        action_date=DAY, frozen_symbols=[symbol], created_at=NOW,
        source_hashes={key: "a" * 64 for key in ("forecasts_sha256", "manifest_sha256", "receipt_sha256")},
        forecasts=rows.to_dict("records"), direction_only=True)
    if scout_id:
        result["direction_policy_version"] = SCOUT_STOCK_DIRECTION_POLICY_VERSION
        for row in result["forecasts"]:
            row["direction_policy_version"] = SCOUT_STOCK_DIRECTION_POLICY_VERSION
        reseal(result)
    return result


def reseal(package):
    package["content_sha256"] = {"forecasts": content_sha256(package["forecasts"])}
    package["package_sha256"] = content_sha256({key: value for key, value in package.items() if key != "package_sha256"})


def test_no_price_scout_package_loads_without_rewriting_original_bytes(tmp_path):
    source = current_package(scout_id=True)
    path = publish_owner_package(tmp_path, source)
    before = path.read_bytes()
    result = load_owner_package(tmp_path, path, expected_sha256=source["package_sha256"])
    assert result == source and path.read_bytes() == before
    assert "price_path" not in result and "price_path_sha256" not in result["source_hashes"]
    assert result["direction_policy_version"] == SCOUT_STOCK_DIRECTION_POLICY_VERSION


@pytest.mark.parametrize("field,value", [("direction_up_threshold", .51), ("direction_down_threshold", .49),
    ("direction_policy_version", "unknown"), ("direction_up_threshold", True)])
def test_current_package_rejects_resealed_policy_drift(tmp_path, field, value):
    source = current_package(scout_id=True)
    source["forecasts"][0][field] = value
    reseal(source)
    with pytest.raises(ValueError):
        publish_owner_package(tmp_path, source)


def test_current_package_rejects_resealed_probability_direction_mismatch(tmp_path):
    source = current_package(scout_id=True)
    source["forecasts"][0]["calibrated_probability"] = .7
    reseal(source)
    with pytest.raises(ValueError, match="direction disagrees"):
        publish_owner_package(tmp_path, source)


def test_current_composition_reuses_predictions_and_actual_observed_context_without_estimates(monkeypatch):
    from ml import joint_capital_plan
    def denied(*args, **kwargs):
        pytest.fail("Current composition cannot project planning prices or quantities")
    monkeypatch.setattr(joint_capital_plan, "project_direction_trades", denied)
    sources = [current_package(scout_id=True), historical_package("atlas", "AAPL", .7)]
    originals = deepcopy(sources)
    observed = snapshot(cash=123.45)
    observed["held_shares"]["ABCL"] = 12
    observed["pending_sell_shares"]["ABCL"] = 2
    plan = compose_joint_plan(sources, observed, action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in sources},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in sources},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    assert sources == originals
    assert is_current_gameplan_recipe(plan) and plan["direction_policy_version"] == STOCK_DIRECTION_POLICY_VERSION
    assert plan["account_snapshot_sha256"] == content_sha256(observed)
    assert plan["ledger"]["observed_account_context"]["available_cash"] == 123.45
    assert plan["ledger"]["events"] == [] and "price_path" not in plan and "source_price_paths" not in plan
    for row in plan["forecasts"]:
        original = next(value for value in originals if value["owner_id"] == row["owner_id"])
        original_row = next(value for value in original["forecasts"] if value["id"] == row["original_forecast_id"])
        assert row["calibrated_probability"] == original_row["calibrated_probability"]
        assert row["target_contract_version"] == original_row["target_contract_version"]
        assert row["source_saved_direction"] == original_row.get("source_saved_direction", original_row["direction"])
        assert not any(key.startswith(("planning_", "projected_", "historical_price_")) for key in row)
    scout_rows = [row for row in plan["forecasts"] if row["owner_id"] == "scout"]
    assert all(row["observed_available_account_shares"] == 10 for row in scout_rows)


def current_handoff(saved):
    case = saved
    science = case.frame.assign(calibrated_probability=.48, direction="NO_EDGE", model_status="RESEARCH_NOT_PROMOTED")
    science.to_parquet(case.game / "forecasts.parquet", index=False)
    case.gm["output_files"]["forecasts.parquet"] = inventory(case.game / "forecasts.parquet")
    write(case.game / "manifest.json", case.gm)
    case.gr["manifest_checksum_sha256"] = digest(case.game / "manifest.json")
    write(case.game / "receipt.json", case.gr)
    derived, _ = direction_only_rows(science, action_date=DAY)
    derived.to_parquet(case.trade / "trade-plan.parquet", index=False)
    case.tm["configuration"].update(current_gameplan_policy_metadata(DAY), source_receipt_sha256=digest(case.game / "receipt.json"))
    case.tr.update(case.tm["configuration"])
    case.tm["output_files"] = {"trade-plan.parquet": inventory(case.trade / "trade-plan.parquet")}
    (case.trade / "planning-price-path.json").unlink()
    write(case.trade / "manifest.json", case.tm)
    case.tr["manifest_sha256"] = digest(case.trade / "manifest.json")
    write(case.trade / "receipt.json", case.tr)
    return case


def test_current_handoff_requires_no_price_artifact_and_preserves_science(saved, monkeypatch):
    from ml import joint_capital_handoff
    case = current_handoff(saved)
    originals = {path: path.read_bytes() for path in (case.game / "forecasts.parquet", case.game / "receipt.json", case.game / "manifest.json")}
    read = joint_capital_handoff._read
    def checked(path, *args, **kwargs):
        assert path.name not in {"planning-price-path.json", "price-bands.json", "account-snapshot.json", "model.joblib"}
        return read(path, *args, **kwargs)
    monkeypatch.setattr(joint_capital_handoff, "_read", checked)
    result = json.loads(export(case, archival=False).read_text())
    assert "price_path" not in result and len(result["source_hashes"]) == 3
    assert all(row["calibrated_probability"] == .48 and row["source_saved_direction"] == "NO_EDGE"
        and row["direction"] == "BEARISH" for row in result["forecasts"])
    assert all(path.read_bytes() == value for path, value in originals.items())


def test_current_handoff_rejects_even_subdecimal_probability_mutation(saved):
    case = current_handoff(saved)
    path = case.trade / "trade-plan.parquet"
    frame = pd.read_parquet(path)
    frame.loc[0, "calibrated_probability"] += 1e-12
    frame.to_parquet(path, index=False)
    case.tm["output_files"]["trade-plan.parquet"] = inventory(path)
    write(case.trade / "manifest.json", case.tm)
    case.tr["manifest_sha256"] = digest(case.trade / "manifest.json")
    write(case.trade / "receipt.json", case.tr)
    with pytest.raises(ValueError, match="derived directions differ"):
        export(case, archival=False)


@pytest.mark.parametrize("archived_prices", [None, {"points": {}},
    {"observed_at": "2099-01-01T00:00Z", "points": {"unusable": {"planned_price_mid": -5}}}])
def test_current_read_and_compose_never_require_archived_price_semantics(tmp_path, archived_prices):
    legacy = historical_package("atlas", "AAPL", .7)
    if archived_prices is None:
        del legacy["price_path"]
        del legacy["source_hashes"]["price_path_sha256"]
    else:
        legacy["price_path"] = archived_prices
        legacy["content_sha256"]["price_path"] = content_sha256(archived_prices)
    legacy["package_sha256"] = content_sha256({key: value for key, value in legacy.items() if key != "package_sha256"})
    path = tmp_path / "selected.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()
    selected = load_owner_package(tmp_path, path, expected_sha256=legacy["package_sha256"])
    assert selected == legacy and path.read_bytes() == before
    sources = [current_package(), selected]
    settings = dict(action_date=DAY, expected_universes={p["owner_id"]: p["frozen_symbols"] for p in sources},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in sources},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    current = compose_joint_plan(sources, snapshot(), **settings)
    assert is_current_gameplan_recipe(current) and current["ledger"]["events"] == []
    assert any(row["direction_based_action"] == "BUY" for row in current["forecasts"])
    with pytest.raises(ValueError):
        load_owner_package(tmp_path, path, expected_sha256=legacy["package_sha256"], validate_legacy_prices=True)
    with pytest.raises(ValueError):
        _compose_legacy_joint_plan(sources, snapshot(), **settings)


def test_current_legacy_forecast_read_still_checks_original_content_digest(tmp_path):
    legacy = historical_package("atlas", "AAPL", .7)
    legacy["forecasts"][0]["calibrated_probability"] = .71
    legacy["package_sha256"] = content_sha256({key: value for key, value in legacy.items() if key != "package_sha256"})
    path = tmp_path / "mutated.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(ValueError, match="content hashes"):
        load_owner_package(tmp_path, path, expected_sha256=legacy["package_sha256"])


@pytest.mark.parametrize("retired_price", [None, {"points": {}},
    {"points": {"unusable": {"planned_price_mid": -5}}, "observed_at": "2099-01-01"}])
def test_new_package_defaults_ignore_retired_estimates_and_preserve_original_predictions(retired_price):
    source = records("ABCL", .48)
    for row in source:
        row["direction"] = "NO_EDGE"
        row["model_status"] = "RESEARCH_NOT_PROMOTED"
    original = deepcopy(source)
    hashes = {key: "a" * 64 for key in ("receipt_sha256", "manifest_sha256", "forecasts_sha256")}
    baseline = build_owner_package(owner_id="scout", run_id="saved-science", source_revision=None,
        action_date=DAY, frozen_symbols=["ABCL"], created_at=NOW, source_hashes=hashes, forecasts=source)
    result = build_owner_package(owner_id="scout", run_id="saved-science", source_revision=None,
        action_date=DAY, frozen_symbols=["ABCL"], created_at=NOW,
        source_hashes={**hashes, "price_path_sha256": "retired-not-an-operating-input"},
        forecasts=source, price_path=retired_price)
    assert result == baseline and source == original
    assert is_current_gameplan_recipe(result)
    assert "price_path" not in result and result["source_hashes"] == hashes
    assert all(row["source_saved_direction"] == "NO_EDGE" for row in result["forecasts"])


@pytest.mark.parametrize("retired_bytes", [None, b"invalid retired JSON", b'{"planned_price_mid":-10}'])
def test_default_export_of_old_science_ignores_retired_file_availability_and_values(saved, monkeypatch, retired_bytes):
    from ml import joint_capital_handoff
    case = saved
    retired = case.trade / "planning-price-path.json"
    if retired_bytes is None:
        retired.unlink()
    else:
        retired.write_bytes(retired_bytes)
    originals = {path: path.read_bytes() for path in (case.game / "forecasts.parquet", case.game / "receipt.json", case.game / "manifest.json")}
    read = joint_capital_handoff._read
    def checked(path, *args, **kwargs):
        assert path.name != "planning-price-path.json"
        return read(path, *args, **kwargs)
    monkeypatch.setattr(joint_capital_handoff, "_read", checked)
    path = joint_capital_handoff.export_owner_package(case.root, gameplan_run=case.game,
        trade_plan_run=case.trade, owner_id="scout", output_root=case.out, created_at="2026-09-09T10:30Z")
    result = json.loads(path.read_text())
    assert is_current_gameplan_recipe(result) and "price_path" not in result
    assert result["source_hashes"] == {"forecasts_sha256": digest(case.game / "forecasts.parquet"),
        "manifest_sha256": digest(case.game / "manifest.json"), "receipt_sha256": digest(case.game / "receipt.json")}
    assert all(path.read_bytes() == before for path, before in originals.items())


def test_current_revision_keeps_original_real_account_observation_without_replacement():
    sources = [current_package(), current_package("atlas", "AAPL")]
    original = compose_joint_plan(sources, snapshot(cash=123.45), action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in sources},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in sources},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    before = deepcopy(original)
    revised = direction_plan_from_saved_plan(original, as_of=NOW)
    assert original == before
    assert revised["source_plan_sha256"] == original["plan_sha256"]
    assert revised["account_snapshot_sha256"] == original["account_snapshot_sha256"]
    assert revised["account_snapshot_observed_at"] == original["account_snapshot_observed_at"]
    assert revised["ledger"]["observed_account_context"] == original["ledger"]["observed_account_context"]


@pytest.mark.parametrize("field,value", [("direction_based_action", "HOLD"),
    ("direction_based_trade_quantity", 1), ("direction_up_threshold", .6)])
def test_public_direction_plan_validator_preserves_concurrent_instruction_checks(field, value):
    sources = [current_package(), current_package("atlas", "AAPL")]
    plan = compose_joint_plan(sources, snapshot(), action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in sources},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in sources},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    validate_direction_only_plan(plan)
    row = next(row for row in plan["forecasts"] if row["direction_based_action"] == "SELL")
    row[field] = value
    with pytest.raises(ValueError, match="directional instruction"):
        validate_direction_only_plan(plan)


def test_direction_validator_ignores_retired_estimates_and_new_revision_strips_them():
    sources = [current_package(), current_package("atlas", "AAPL")]
    plan = compose_joint_plan(sources, snapshot(), action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in sources},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in sources},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    for row in plan["forecasts"]:
        row.update(planning_price_mid="unusable", projected_cash_after_low=-1,
            trade_price_low=None, scheduled_trade_price={"retired": True})
    validate_direction_only_plan(plan)
    rows, _ = direction_only_rows(pd.DataFrame(plan["forecasts"]), action_date=DAY)
    assert not any(name.startswith(("planning_", "projected_", "trade_", "scheduled_trade_")) for name in rows)


@pytest.mark.parametrize("current", [False, True])
def test_operational_packet_read_ignores_retired_price_fields_and_hash_values(tmp_path, current):
    packet = current_package("atlas", "AAPL") if current else historical_package("atlas", "AAPL", .7)
    packet["price_path"] = {"points": "unusable", "planned_price_mid": -3}
    packet["source_hashes"]["price_path_sha256"] = "retired malformed audit"
    packet["content_sha256"]["price_path"] = {"retired": "not an operating input"}
    packet["package_sha256"] = content_sha256({key: value for key, value in packet.items() if key != "package_sha256"})
    path = tmp_path / "retired-metadata.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    before = path.read_bytes()
    selected = load_owner_package(tmp_path, path, expected_sha256=packet["package_sha256"])
    assert selected == packet and path.read_bytes() == before
    assert publish_owner_package(tmp_path, packet).is_file()
    sources = [current_package(), selected]
    result = compose_joint_plan(sources, snapshot(), action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in sources},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in sources},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    assert result["ledger"]["events"] == [] and is_current_gameplan_recipe(result)


def test_current_handoff_preserves_reviewed_native_policy_alias(saved):
    from ml.joint_capital_handoff import export_owner_package
    case = current_handoff(saved)
    derived_path = case.trade / "trade-plan.parquet"
    derived = pd.read_parquet(derived_path)
    derived["direction_policy_version"] = SCOUT_STOCK_DIRECTION_POLICY_VERSION
    derived.to_parquet(derived_path, index=False)
    case.tm["configuration"]["direction_policy_version"] = SCOUT_STOCK_DIRECTION_POLICY_VERSION
    case.tr["direction_policy_version"] = SCOUT_STOCK_DIRECTION_POLICY_VERSION
    case.tm["output_files"]["trade-plan.parquet"] = inventory(derived_path)
    write(case.trade / "manifest.json", case.tm)
    case.tr["manifest_sha256"] = digest(case.trade / "manifest.json")
    write(case.trade / "receipt.json", case.tr)
    result = json.loads(export_owner_package(case.root, gameplan_run=case.game, trade_plan_run=case.trade,
        owner_id="scout", output_root=case.out, created_at="2026-09-09T10:30Z").read_text())
    assert all(row["direction_policy_version"] == SCOUT_STOCK_DIRECTION_POLICY_VERSION for row in result["forecasts"])
    assert result["direction_policy_version"] == STOCK_DIRECTION_POLICY_VERSION


def test_current_composition_retains_different_historical_fallback_policies_without_gate(monkeypatch):
    import test_joint_capital_plan as historical
    from ml.stock_trader.cross_horizon_fallback import policy_for_action_date
    day, now = "2026-10-20", "2026-10-20T10:00:00Z"
    monkeypatch.setitem(globals(), "DAY", day)
    monkeypatch.setitem(globals(), "NOW", now)
    monkeypatch.setattr(historical, "DAY", day)
    monkeypatch.setattr(historical, "NOW", now)
    sources = [current_package(), current_package("atlas", "AAPL")]
    sources[0]["cross_horizon_fallback_policy"] = policy_for_action_date(day)
    reseal(sources[0])
    plan = compose_joint_plan(sources, snapshot(), action_date=day,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in sources},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in sources},
        account_scope_sha256=SCOPE, as_of=now, executor_owner="atlas")
    assert plan["cross_horizon_fallback_policy"] is None
    assert plan["source_cross_horizon_fallback_policies"]["scout"] == sources[0]["cross_horizon_fallback_policy"]
    assert plan["source_cross_horizon_fallback_policies"]["atlas"] is None
    validate_direction_only_plan(plan)
