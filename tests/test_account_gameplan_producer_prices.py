# Canonical publisher tests use direction-live-quote-v1. Pure historical
# price arithmetic and immutable historical reader fixtures remain tested.
# Retired estimated-publisher cases have these current-contract replacements:
# test_source_price_preparation_has_no_account_snapshot_and_exports_verified_bundle:
#   test_gameplan_direction_recipe.py::test_price_free_producer_handoff_composition_and_ui_keep_real_probabilities
# test_preparing_ui_explains_shared_projection_without_fake_holds_or_cash:
#   test_gameplan_direction_recipe_optional.py::test_current_joint_recipe_adopts_and_displays_intents_without_any_price_file
# test_native_actuals_retains_original_price_estimates_from_producer_only_output:
#   test_gameplan_direction_recipe.py::test_stats_accepts_verified_current_derivation_without_relabelling_scientific_history
# test_source_only_price_failure_preserves_previous_pointer:
#   test_gameplan_direction_recipe.py::test_producer_does_not_load_price_history_or_capture_scout_account

from copy import deepcopy
import json

import pandas as pd
import pytest

from app.ui.gameplan_data import load_gameplan
from ml.account_gameplan.config import CONFIG, load_account_config
from ml.account_gameplan.sources import export_source_bundle
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import _saved_trade_plan, _match_trade_rows
from ml.gameplan_trade_planning import publish_trade_plan
from test_account_gameplan_sources import DAY, native_case, save
from test_account_gameplan_config import write_config


@pytest.fixture
def producer_case(tmp_path, monkeypatch):
    from ml import gameplan_price_bands, gameplan_trade_snapshot
    from ml.stock_trader import independent_signals
    case = native_case(tmp_path, monkeypatch, producer="pc-original", symbol="AAPL")
    write_config(case.root)
    forecasts = pd.read_parquet(case.source / "forecasts.parquet")
    pd.DataFrame({"id": forecasts.id + ":OPTION", "symbol": "AAPL", "plan_status": "NO_TRADE_STOCK_ONLY",
        "legs_json": None, "candidate_key": None, "strategy_source_run": None}).to_parquet(case.source / "option-strategy-intents.parquet", index=False)
    monkeypatch.setattr(independent_signals, "verified_promoted_model_groups", lambda publication: frozenset())
    monkeypatch.setattr(gameplan_trade_snapshot, "capture_trade_planning_snapshot", lambda *a, **k: pytest.fail("producer must not read broker or ownership"))
    source_path = json.loads((case.trade / "planning-price-path.json").read_text())
    def bands(*args, **kwargs):
        assert kwargs["allow_sparse_session_references"] is True and kwargs["allow_reference_forward_fill"] is True
        assert kwargs["lookback_sessions"] == 504
        return {"rows": [{"forecast_id": row.id, "price_band_status": "AVAILABLE", "trade_price_low": 9., "trade_price_high": 11.}
                         for row in forecasts.itertuples()], "reference_completion": {
            "contract_version": "bounded-planning-reference-completion-v1", "references": {"AAPL": {
                "symbol": "AAPL", "status": "AVAILABLE_SYNTHETIC", "observed_at": "2026-10-02T16:50:00-07:00",
                "effective_at": "2026-10-02T17:00:00-07:00", "gap_minutes": 10}}, "synthetic_bars": []}}
    def price_path(*args, **kwargs):
        result = deepcopy(source_path)
        result["observed_at"] = kwargs["observed_at"].isoformat()
        return result
    monkeypatch.setattr(gameplan_price_bands, "build_entry_price_bands", bands)
    monkeypatch.setattr(gameplan_price_bands, "build_planning_price_path", price_path)
    return case


def publish(case, **changes):
    kwargs = dict(gameplan_run=case.source, clock=lambda: pd.Timestamp("2026-10-05T06:00:00Z"),
        price_loader=lambda *a, **k: (pd.DataFrame(), (), {}), account_producer_only=True,
        expected_account_config=load_account_config(case.root).fingerprint)
    kwargs.update(changes)
    return publish_trade_plan(case.root, **kwargs)








@pytest.mark.parametrize("failure", ["binding", "snapshot", "refresh", "deadline", "universe"])
def test_producer_mode_cannot_bypass_binding_or_change_source_scope(producer_case, failure):
    case = producer_case
    kwargs = {}
    if failure == "binding": kwargs["expected_account_config"] = "0" * 64
    elif failure == "snapshot": kwargs["snapshot_loader"] = lambda *a, **k: pytest.fail("not called")
    elif failure == "refresh": kwargs["refresh_plan"] = case.trade
    elif failure == "deadline": kwargs["deadline"] = "2026-10-05T12:00:00Z"
    else:
        value = json.loads((case.root / CONFIG).read_text())
        value["participants"]["pc-original"] = ["NVDA"]
        from ml.account_gameplan.config import digest
        value["activation"]["binding_sha256"] = digest({key: val for key, val in value.items() if key != "activation"})
        save(case.root / CONFIG, value)
    with pytest.raises(ValueError):
        publish(case, **kwargs)




@pytest.mark.parametrize("failure", ["deadline", "config", "foreign_before", "foreign_after"])
def test_source_only_pointer_commit_guard_preserves_prior_or_independent_bytes(producer_case, monkeypatch, failure):
    from ml import gameplan_trade_planning as planning
    case = producer_case
    first = publish(case)
    pointer = case.root / "ml/gameplan-trade-plan-latest/run.json"
    previous = pointer.read_bytes()
    foreign = b'{"current":{"run_path":"independently-published"}}'
    now = [pd.Timestamp("2026-10-05T06:00:00Z")]
    original = planning._write_json
    def write_then_change(path, value):
        result = original(path, value)
        if failure == "foreign_before" and path.name == "receipt.json" and value.get("status") == "COMPLETE":
            pointer.write_bytes(foreign)
        if path == pointer:
            if failure == "deadline":
                now[0] = pd.Timestamp("2026-10-05T11:00:00Z")
            elif failure == "config":
                current = json.loads((case.root / CONFIG).read_text())
                current["activation"]["status"] = "ACTIVE"
                save(case.root / CONFIG, current)
            elif failure == "foreign_after":
                pointer.write_bytes(foreign)
        return result
    monkeypatch.setattr(planning, "_write_json", write_then_change)
    with pytest.raises(ValueError, match="changed"):
        publish(case, clock=lambda: now[0])
    assert pointer.read_bytes() == (foreign if failure.startswith("foreign") else previous)
    assert verify_manifest(first)
    failed = [json.loads(path.read_text()) for path in pointer.parent.parent.glob("gameplan-trade-plan-runs/*/receipt.json")]
    assert any(receipt["status"] == "FAILED" for receipt in failed)
