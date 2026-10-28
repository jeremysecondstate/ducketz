import pytest

from ml.gameplan_saved_direction import saved_gameplan_direction
from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION


@pytest.mark.parametrize("field,value", [
    ("direction_up_threshold", .55), ("direction_down_threshold", .48),
])
def test_current_saved_policy_rejects_different_cutoffs(field, value):
    with pytest.raises(ValueError, match="cutoffs disagree"):
        saved_gameplan_direction({"direction_policy_version": STOCK_DIRECTION_POLICY_VERSION,
                                  "direction": "BEARISH", field: value}, .4872)


def test_original_version_two_call_is_preserved_in_current_hold_gap():
    direction, metadata = saved_gameplan_direction({"direction_policy_version": "stock-direction-50-v2",
                                                    "direction": "BULLISH"}, .50005)
    assert direction == "BULLISH" and metadata["direction_policy_version"] == "stock-direction-50-v2"


def test_unknown_historical_policy_retains_saved_call_but_cannot_invent_one():
    assert saved_gameplan_direction({"direction_policy_version": "historical-v1", "direction": "NO_EDGE"}, .7)[0] == "NO_EDGE"
    with pytest.raises(ValueError, match="Cannot infer"):
        saved_gameplan_direction({"direction_policy_version": "historical-v1"}, .7)


@pytest.mark.parametrize("policy", [STOCK_DIRECTION_POLICY_VERSION, "stock-direction-50-01-v3"])
def test_known_current_policy_names_validate_saved_direction_and_preserve_identity(policy):
    with pytest.raises(ValueError, match="direction disagrees"):
        saved_gameplan_direction({"direction_policy_version": policy, "direction": "NO_EDGE"}, .4872)
    direction, metadata = saved_gameplan_direction({"direction_policy_version": policy, "direction": "BEARISH"}, .4872)
    assert direction == "BEARISH" and metadata["direction_policy_version"] == policy
    assert metadata["direction_up_threshold"] == .5001
    assert metadata["direction_down_threshold"] == .4999


@pytest.mark.parametrize("field,value", [("direction_up_threshold", .55), ("direction_down_threshold", .48)])
def test_known_current_policy_alias_rejects_inconsistent_cutoffs(field, value):
    with pytest.raises(ValueError, match="cutoffs disagree"):
        saved_gameplan_direction({"direction_policy_version": "stock-direction-50-01-v3",
                                  "direction": "BEARISH", field: value}, .4872)
