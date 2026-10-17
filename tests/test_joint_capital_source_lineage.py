from copy import deepcopy

import pytest

from ml.joint_capital_adoption import accept_joint_plan, read_accepted_joint_plan
from ml.joint_capital_plan import compose_joint_plan
from ml.stock_target_prices import stock_price_dataset
from tests.test_joint_capital_plan import package, prices, records, snapshot, DAY, NOW, SCOPE, POLICY


def mixed_packages():
    source = "observed-multi-source-stock-v2"
    rows, path = records("ABCL"), prices("ABCL")
    for row in rows:
        row.update(target_price_source_contract=source, target_price_dataset=stock_price_dataset(source),
                   source_selection_contract="xnas-archive-observed-technicals-v2")
    path.update(price_source_contract=source, price_dataset=stock_price_dataset(source))
    return [package("atlas", "AAPL"), package("scout", "ABCL", rows=rows, path=path)]


def compose(values, basis=None):
    return compose_joint_plan(values, basis, action_date=DAY,
        expected_universes={"atlas": ["AAPL"], "scout": ["ABCL"]},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in values},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas", policy=POLICY)


@pytest.mark.parametrize("with_basis", [False, True])
def test_mixed_validated_sources_preserve_each_owner_and_policy(with_basis):
    values = mixed_packages()
    before = deepcopy(values)
    result = compose(values, snapshot() if with_basis else None)
    assert values == before
    assert result["price_lineage_contract"] == "owner-scoped-planning-price-lineage-v1"
    assert result["owner_price_lineage"]["scout"]["price_source_contract"] == "observed-multi-source-stock-v2"
    assert result["owner_price_lineage"]["atlas"]["price_source_contract"] == values[0]["price_path"]["price_source_contract"]
    assert result["source_price_paths"] == {p["owner_id"]: p["price_path"] for p in values}
    assert len(result["forecasts"]) == 48
    assert all(row.get("source_selection_contract") == "xnas-archive-observed-technicals-v2"
               for row in result["forecasts"] if row["owner_id"] == "scout")
    assert all("source_selection_contract" not in row
               for row in result["forecasts"] if row["owner_id"] == "atlas")
    assert not result["live_orders_authorized"]
    if not with_basis:
        assert "price_source_contract" not in result["price_path"]
        assert "price_dataset" not in result["price_path"]
        assert result["price_path"]["working_half_width_bps"] == values[0]["price_path"]["working_half_width_bps"]


def test_mixed_output_is_readable_by_existing_verified_adoption(tmp_path):
    values = mixed_packages()
    result = compose(values)
    run = accept_joint_plan(tmp_path, result, expected_sha256=result["plan_sha256"],
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in values},
        expected_universes={"atlas": ["AAPL"], "scout": ["ABCL"]}, account_scope_sha256=SCOPE,
        local_actor="scout", executor_owner="atlas", action_date=DAY, accepted_at=NOW)
    assert read_accepted_joint_plan(tmp_path, DAY)[2] == run


@pytest.mark.parametrize("key,value", [("lookback_sessions", 503), ("minimum_samples", 3), ("method", "different method")])
def test_mixed_sources_never_relax_planning_policy_equality(key, value):
    values = mixed_packages()
    altered = deepcopy(values[1]["price_path"])
    altered[key] = value
    if key == "minimum_samples":
        for point in altered["points"].values():
            point["sample_count"] = 3
    values[1] = package("scout", "ABCL", rows=values[1]["forecasts"], path=altered)
    with pytest.raises(ValueError, match="policies disagree"):
        compose(values)


@pytest.mark.parametrize("damage", ["dataset", "source", "forecast_source"])
def test_mismatched_source_dataset_or_forecast_is_rejected(damage):
    values = mixed_packages()
    path, rows = deepcopy(values[1]["price_path"]), deepcopy(values[1]["forecasts"])
    if damage == "dataset":
        path["price_dataset"] = "UNRELATED.DATASET"
    elif damage == "source":
        path["price_source_contract"] = "invented-source-v1"
    else:
        rows[0]["target_price_dataset"] = "UNRELATED.DATASET"
    with pytest.raises((ValueError, RuntimeError)):
        package("scout", "ABCL", rows=rows, path=path)


def test_equal_source_output_keeps_existing_shape_for_old_readers():
    result = compose([package("atlas", "AAPL"), package("scout", "ABCL")])
    assert "price_lineage_contract" not in result
    assert "owner_price_lineage" not in result
    assert "price_source_contract" in result["price_path"]
