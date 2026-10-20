"""Current packages reach both displays without inventing capacity or fills."""
from copy import deepcopy

import pandas as pd
import pytest

from app.ui.gameplan_data import load_gameplan, GameplanError, _forecast
from ml.gameplan_execution_policy import direction_only_rows
from ml.joint_capital_adoption import accept_joint_plan, read_accepted_joint_plan
from ml.joint_capital_plan import build_owner_package, compose_joint_plan, content_sha256
from test_joint_capital_plan import DAY, NOW, SCOPE, package


def current_plan():
    legacy = package("atlas", "AAPL")
    peer = package("scout", "ABCL", .4)
    rows, _ = direction_only_rows(pd.DataFrame(peer["forecasts"]), action_date=DAY)
    current = build_owner_package(owner_id="scout", run_id=peer["run_id"], source_revision=None,
        action_date=DAY, frozen_symbols=["ABCL"], created_at=NOW,
        source_hashes={key: value for key, value in peer["source_hashes"].items() if key != "price_path_sha256"},
        forecasts=rows.to_dict("records"), direction_only=True)
    return compose_joint_plan([legacy, current], {"account_scope_sha256": SCOPE, "observed_at": NOW},
        action_date=DAY, expected_universes={"atlas": ["AAPL"], "scout": ["ABCL"]},
        expected_package_sha256={"atlas": legacy["package_sha256"], "scout": current["package_sha256"]},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")


def adopt(root, plan, actor):
    return accept_joint_plan(root, plan, expected_sha256=plan["plan_sha256"],
        expected_package_sha256={key: source["package_sha256"] for key, source in plan["input_bindings"].items()},
        expected_universes={key: source["frozen_symbols"] for key, source in plan["input_bindings"].items()},
        account_scope_sha256=SCOPE, local_actor=actor, executor_owner="atlas", action_date=DAY, accepted_at=NOW)


@pytest.mark.parametrize("actor", ["atlas", "scout"])
def test_mixed_current_plan_adopts_and_displays_same_intents(tmp_path, actor):
    plan = current_plan()
    original = deepcopy(plan)
    run = adopt(tmp_path, plan, actor)
    ui = load_gameplan(tmp_path)
    assert ui.run_directory == run
    assert ui.projection_status == "DIRECTION_ONLY" and ui.projection_available
    assert set(ui.symbols) == {"AAPL", "ABCL"}
    assert {row.action for row in ui.actions} == {"BUY", "SELL"}
    assert all(row.quantity is None and row.price is None for row in ui.actions)
    assert read_accepted_joint_plan(tmp_path, DAY)[0] == original == plan
    assert not (tmp_path / "controls").exists()


@pytest.mark.parametrize("field,value", [("direction_based_trade_quantity", 1),
    ("direction_based_action", "HOLD"), ("direction_up_threshold", .6)])
def test_current_forecast_rejects_estimated_capacity_and_suppressed_direction(field, value):
    row = next(row for row in current_plan()["forecasts"] if row["direction_based_action"] == "BUY")
    row[field] = value
    with pytest.raises(GameplanError):
        _forecast(row)


@pytest.mark.parametrize("retired_price", [10, "retired-not-a-number", -1])
def test_current_forecast_does_not_validate_or_use_retired_estimated_prices(retired_price):
    row = next(row for row in current_plan()["forecasts"] if row["direction_based_action"] == "BUY")
    row["trade_price_mid"] = retired_price
    current = _forecast(row)
    assert current.action == "BUY" and current.price is None and current.quantity is None
    assert current.probability == row["calibrated_probability"]


def test_even_resealed_current_plan_cannot_install_fabricated_fills(tmp_path):
    plan = current_plan()
    plan["ledger"]["events"] = [{"action": "BUY", "quantity": 1}]
    plan.pop("plan_sha256")
    plan.pop("plan_id")
    plan.pop("owner_views")
    # A matching digest does not turn a projected fill into a current recipe.
    plan["plan_id"] = "joint-capital:" + content_sha256(plan)
    plan["owner_views"] = {}
    plan["plan_sha256"] = content_sha256(plan)
    with pytest.raises(ValueError, match="projected fills"):
        adopt(tmp_path, plan, "atlas")
