"""Optional joint adoption interfaces for the current price-free recipe."""
from copy import deepcopy

import pandas as pd
import pytest

from ml.gameplan_execution_policy import direction_only_rows, is_current_gameplan_recipe
from ml.joint_capital_plan import compose_joint_plan, build_owner_package
from ml.joint_capital_adoption import accept_joint_plan, read_accepted_joint_plan
from app.ui.gameplan_data import load_gameplan
from test_joint_capital_plan import records, DAY, NOW, SCOPE
from test_account_gameplan_producer_prices import producer_case, publish


@pytest.mark.parametrize("change", ["universe", "binding_during_selection"])
def test_current_account_producer_keeps_source_universe_and_final_binding(producer_case, monkeypatch, change):
    import json
    from ml import gameplan_trade_planning as planning
    from ml.account_gameplan.config import CONFIG, digest
    from test_account_gameplan_sources import save
    case = producer_case
    if change == "universe":
        value = json.loads((case.root / CONFIG).read_text())
        value["participants"]["pc-original"] = ["NVDA"]
        value["activation"]["binding_sha256"] = digest({key: val for key, val in value.items() if key != "activation"})
        save(case.root / CONFIG, value)
    else:
        first = publish(case)
        pointer = case.root / "ml/gameplan-trade-plan-latest/run.json"
        before = pointer.read_bytes()
        original = planning._write_json
        def changed(path, value):
            original(path, value)
            if path == pointer:
                config = json.loads((case.root / CONFIG).read_text())
                config["activation"]["status"] = "ACTIVE"
                save(case.root / CONFIG, config)
        monkeypatch.setattr(planning, "_write_json", changed)
    with pytest.raises(ValueError):
        publish(case)
    if change != "universe":
        assert pointer.read_bytes() == before and first.is_dir()


def test_current_joint_recipe_adopts_and_displays_intents_without_any_price_file(tmp_path):
    packages = []
    for owner, symbol in (("scout", "ABCL"), ("atlas", "AAPL")):
        frame = pd.DataFrame(records(symbol, .48))
        frame["direction"] = "NO_EDGE"
        frame["model_status"] = "RESEARCH_NOT_PROMOTED"
        rows, _ = direction_only_rows(frame, action_date=DAY)
        packages.append(build_owner_package(owner_id=owner, run_id="source-" + owner, source_revision=None,
            action_date=DAY, frozen_symbols=[symbol], created_at=NOW,
            source_hashes={key: "a" * 64 for key in ("receipt_sha256", "manifest_sha256", "forecasts_sha256")},
            forecasts=rows.to_dict("records"), direction_only=True))
    universes = {p["owner_id"]: p["frozen_symbols"] for p in packages}
    hashes = {p["owner_id"]: p["package_sha256"] for p in packages}
    plan = compose_joint_plan(packages, action_date=DAY, expected_universes=universes,
        expected_package_sha256=hashes, account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    before = deepcopy(plan)
    run = accept_joint_plan(tmp_path, plan, expected_sha256=plan["plan_sha256"], expected_package_sha256=hashes,
        expected_universes=universes, account_scope_sha256=SCOPE, local_actor="scout", executor_owner="atlas",
        action_date=DAY, accepted_at=NOW)
    assert read_accepted_joint_plan(tmp_path, DAY)[0] == before
    ui = load_gameplan(tmp_path)
    assert ui.run_directory == run and ui.projection_status == "DIRECTION_ONLY"
    assert is_current_gameplan_recipe(plan) and len(ui.forecasts) == 48
    assert ui.actions and all(row.action == "SELL" and row.quantity is None for row in ui.actions)
    assert not list(tmp_path.rglob("planning-price-path.json"))
