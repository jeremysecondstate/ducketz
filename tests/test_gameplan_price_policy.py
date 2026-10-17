"""Offline source migration retains completed-run identity and fails closed."""
import copy
import pytest
from ml.gameplan_price_policy import POLICY_VERSION, resolve_gameplan_price_source


def configuration():
    return {"stock_price_source": "xnas-itch-archive-v1", "stock_price_source_policy": {
        "version": POLICY_VERSION, "effective_from_action_date": "2026-10-20",
        "source_contract": "observed-multi-source-stock-v2"}}


def test_resolved_source_preserves_past_and_switches_on_effective_session():
    config = configuration()
    original = copy.deepcopy(config)
    assert resolve_gameplan_price_source(config, "2026-10-19") == "xnas-itch-archive-v1"
    assert resolve_gameplan_price_source(config, "2026-10-20") == "observed-multi-source-stock-v2"
    assert resolve_gameplan_price_source(config, "2026-10-21") == "observed-multi-source-stock-v2"
    assert resolve_gameplan_price_source(config) == "xnas-itch-archive-v1"
    assert config == original


@pytest.mark.parametrize("policy", [{}, {"version": "anything"}, [], False,
    {**configuration()["stock_price_source_policy"], "source_contract": "invented"},
    {**configuration()["stock_price_source_policy"], "source_contract": []},
    {**configuration()["stock_price_source_policy"], "effective_from_action_date": "20261020"},
    {**configuration()["stock_price_source_policy"], "effective_from_action_date": "2026-02-31"},
    {**configuration()["stock_price_source_policy"], "unknown": True}])
def test_invalid_policy_rejected_even_when_reading_old_session(policy):
    with pytest.raises(ValueError):
        resolve_gameplan_price_source({"stock_price_source_policy": policy}, "2026-10-19")


def test_no_policy_retains_explicit_source_and_historical_default():
    assert resolve_gameplan_price_source({}, "2026-10-19") == "xnas-itch-archive-v1"
    assert resolve_gameplan_price_source({"stock_price_source": "canonical-equity-minute-v1"}) == "canonical-equity-minute-v1"
    with pytest.raises(ValueError):
        resolve_gameplan_price_source({"stock_price_source": "invented"})


@pytest.mark.parametrize("value", ["20261020", "2026-10-20T00:00:00Z", 20261020, "bad"])
def test_action_date_is_explicit_iso_date(value):
    with pytest.raises(ValueError):
        resolve_gameplan_price_source(configuration(), value)
