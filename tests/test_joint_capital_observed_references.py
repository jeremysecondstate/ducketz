from copy import deepcopy

import pytest

from ml.joint_capital_plan import content_sha256
from tests.test_joint_capital_source_lineage import compose, mixed_packages
from tests.test_joint_capital_plan import package, snapshot


def with_synthetic_evidence(*, anchor=False, samples=0):
    values = mixed_packages()
    path = deepcopy(values[0]["price_path"])
    for point in path["points"].values():
        point.update(reference_is_synthetic=anchor, synthetic_close_sample_count=samples)
    values[0] = package("atlas", "AAPL", rows=values[0]["forecasts"], path=path)
    return values


@pytest.mark.parametrize("anchor,samples", [(True, 0), (False, 1), (True, 2)])
def test_new_projection_excludes_synthetic_anchor_or_aggregate_history(anchor, samples):
    values = with_synthetic_evidence(anchor=anchor, samples=samples)
    before = deepcopy(values)
    result = compose(values)
    assert values == before
    assert result["source_price_paths"]["atlas"] == values[0]["price_path"]
    assert result["planning_observation_policy"] == "observed-only-joint-planning-projection-v1"
    assert len(result["planning_point_exclusions"]["atlas"]) == 14
    assert result["planning_point_exclusions"]["scout"] == {}
    for key, point in result["price_path"]["points"].items():
        if point["symbol"] == "AAPL":
            assert point["status"] == "UNAVAILABLE_REFERENCE_PRICE"
            assert point["reason"] == "UNVERIFIED_SYNTHETIC_REFERENCE_EXCLUDED"
            assert point["sample_count"] == 0
            assert all(point[name] is None for name in (
                "reference_price", "ratio_median", "planned_price_low", "planned_price_mid",
                "planned_price_high", "historical_price_low", "historical_price_high"))
            evidence = result["planning_point_exclusions"]["atlas"][key]
            assert evidence["source_point_sha256"] == content_sha256(values[0]["price_path"]["points"][key])
            assert evidence["source_reference_is_synthetic"] is anchor
            assert evidence["source_synthetic_close_sample_count"] == samples
        else:
            assert point == values[1]["price_path"]["points"][key]
    for owner in ("atlas", "scout"):
        originals = {row["id"]: row for row in values[0 if owner == "atlas" else 1]["forecasts"]}
        for row in result["forecasts"]:
            if row["owner_id"] == owner:
                original = originals[row["original_forecast_id"]]
                assert all(row[k] == v for k, v in original.items() if k != "id")


def test_saved_balance_projection_cannot_spend_on_excluded_prices():
    from ml.gameplan_cash_ledger import UnavailablePlanningPricePath
    values, basis = with_synthetic_evidence(samples=1), snapshot()
    before = deepcopy((values, basis))
    with pytest.raises(UnavailablePlanningPricePath):
        compose(values, basis)
    assert (values, basis) == before


@pytest.mark.parametrize("anchor,samples", [("false", 0), (0, 0), (False, True), (False, -1), (False, 1.5)])
def test_malformed_synthetic_provenance_fails_closed(anchor, samples):
    with pytest.raises(ValueError, match="synthetic-reference provenance"):
        compose(with_synthetic_evidence(anchor=anchor, samples=samples))


def test_genuine_existing_projection_keeps_compatible_shape():
    values = with_synthetic_evidence()
    result = compose(values)
    assert "planning_observation_policy" not in result
    assert "planning_point_exclusions" not in result
    assert result["price_path"]["points"] == {**values[0]["price_path"]["points"], **values[1]["price_path"]["points"]}


@pytest.mark.parametrize("field,value", [("reference_fill_count", 1), ("reference_completion_status", "AVAILABLE_SYNTHETIC")])
def test_alternate_explicit_synthetic_provenance_also_excludes(field, value):
    values = mixed_packages()
    path = deepcopy(values[0]["price_path"])
    for point in path["points"].values():
        point[field] = value
    values[0] = package("atlas", "AAPL", rows=values[0]["forecasts"], path=path)
    result = compose(values)
    assert len(result["planning_point_exclusions"]["atlas"]) == 14
    assert all(p["planned_price_mid"] is None for p in result["price_path"]["points"].values() if p["symbol"] == "AAPL")
