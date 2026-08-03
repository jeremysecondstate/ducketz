from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pandas as pd
import pytest

from ml.contracts import ALLOWED_HORIZONS, FeatureSet, FeatureSpec
from ml.datasets.families import (
    CME_FRESHNESS,
    ENERGY_FRESHNESS,
    LIFECYCLE_FRESHNESS,
    OPTION_FRESHNESS,
    QUOTE_FRESHNESS,
)
from ml.datasets.point_in_time import backward_asof_by_symbol
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.readiness import evaluate_feature_readiness


_EXISTING_FEATURE_SET_FINGERPRINTS = {
    "technical-all": (
        "551b71a14c72088c548b3a58cada5d380d9e627c9eed01a56131d93c96966e04"
    ),
    "technical-all-v2-1h": (
        "0cc1364e24948ed4227ad1a4764fd69b156545cad5aa11f12409fe38e2c1863f"
    ),
    "technical-all-v2-1d": (
        "789058297650e830ddfe08c8b48edb2f91822c92176bc0dbbf42205d858b6a82"
    ),
    "technical-all-v2-1w": (
        "4c4eb51e915a53fe09a05dde086c751e4c7f79e117b9e8db72f425fd3ec836c4"
    ),
    "loop-a-all-v1-1h": (
        "0b0c01c2ed7bb2cb85acb8618e7d1393e254ec57884d5ecdbbc7ba8b1554af45"
    ),
    "loop-a-all-v1-4h": (
        "b8b6e9c6dd6a7b08c667592845fdcb44d27bc799ef9fa6395679e204c27221de"
    ),
    "loop-a-all-v1-1d": (
        "967d296ea08445f7ac0108dca8f1898e18943a777d83dcb8763e3a200b175abc"
    ),
    "loop-a-all-v1-1w": (
        "ba8f3859d925c4b76e0e0c59254e2110c02fb7c3f1fbdff297e3a0e0395b0d09"
    ),
}

_FOUR_HOUR_FRESHNESS_BY_FAMILY = {
    "mr": "exact-decision",
    "bp": "exact-decision",
    "bar": "exact-decision",
    "opt": "2-hours",
    "quote": "5-minutes",
    "cme": "15-minutes",
    "life": "2-calendar-days",
    "energy": "30-minutes",
}


def test_allowed_feature_contract_horizon_order_includes_4h() -> None:
    assert ALLOWED_HORIZONS == ("1h", "4h", "1d", "1w")
    feature = FeatureSpec(
        name="fixture__value",
        source_family="fixture",
        source_column="value",
        applicable_horizons=("4h",),
        freshness_by_horizon=(("4h", "exact-decision"),),
    )
    feature_set = FeatureSet(
        "fixture-4h",
        (feature,),
        applicable_horizons=("4h",),
    )

    feature_set.ensure_model_eligible(horizon="4h")


@pytest.mark.parametrize(
    ("four_hour_name", "one_hour_name", "expected_count"),
    (
        ("technical-all-4h", "technical-all", 19),
        ("technical-all-v2-4h", "technical-all-v2-1h", 22),
        ("loop-a-all-v1-4h", "loop-a-all-v1-1h", 69),
    ),
)
def test_every_closed_4h_profile_clones_the_ordered_1h_inventory(
    four_hour_name: str,
    one_hour_name: str,
    expected_count: int,
) -> None:
    one_hour = DEFAULT_FEATURE_REGISTRY.feature_set(one_hour_name)
    four_hour = DEFAULT_FEATURE_REGISTRY.feature_set(
        four_hour_name,
        require_active=True,
        horizon="4h",
    )

    assert four_hour.names == one_hour.names
    assert len(four_hour.names) == expected_count
    assert four_hour.applicable_horizons == ("4h",)
    assert all(
        feature.applicable_horizons == ("4h",)
        for feature in four_hour.features
    )
    assert all(
        "4h" not in feature.applicable_horizons
        for feature in one_hour.features
    )


def test_loop_a_4h_has_exact_1h_family_composition() -> None:
    one_hour = DEFAULT_FEATURE_REGISTRY.feature_set("loop-a-all-v1-1h")
    four_hour = DEFAULT_FEATURE_REGISTRY.feature_set("loop-a-all-v1-4h")

    assert len(four_hour.features) == 69
    assert Counter(
        feature.source_family for feature in four_hour.features
    ) == Counter(feature.source_family for feature in one_hour.features)
    assert Counter(
        feature.source_family for feature in four_hour.features
    ) == {
        "opt": 26,
        "mr": 13,
        "bp": 13,
        "cme": 8,
        "life": 5,
        "bar": 2,
        "quote": 1,
        "energy": 1,
    }


def test_4h_clones_encode_hourly_freshness_without_mutating_1h_specs() -> None:
    one_hour = DEFAULT_FEATURE_REGISTRY.feature_set("loop-a-all-v1-1h")
    four_hour = DEFAULT_FEATURE_REGISTRY.feature_set("loop-a-all-v1-4h")
    one_hour_by_name = {
        feature.name: feature for feature in one_hour.features
    }

    for feature in four_hour.features:
        expected_freshness = _FOUR_HOUR_FRESHNESS_BY_FAMILY[
            feature.source_family
        ]
        assert feature.freshness_by_horizon == (
            ("4h", expected_freshness),
        )
        assert feature == replace(
            one_hour_by_name[feature.name],
            applicable_horizons=("4h",),
            freshness_by_horizon=(("4h", expected_freshness),),
        )

    assert {
        feature.source_family: feature.freshness_limit("1h")
        for feature in one_hour.features
    } == {
        "mr": "exact-decision",
        "bp": "exact-decision",
        "bar": "exact-decision",
        "opt": "scheduled-intraday-surface",
        "quote": "5-minutes",
        "cme": "15-minutes",
        "life": "2-calendar-days",
        "energy": "30-minutes",
    }


def test_4h_family_freshness_matches_the_1h_decision_policy() -> None:
    assert OPTION_FRESHNESS["4h"] == pd.Timedelta(hours=2)
    assert QUOTE_FRESHNESS["4h"] == pd.Timedelta(minutes=5)
    assert CME_FRESHNESS["4h"] == pd.Timedelta(minutes=15)
    assert LIFECYCLE_FRESHNESS["4h"] == pd.Timedelta(days=2)
    assert ENERGY_FRESHNESS["4h"] == pd.Timedelta(minutes=30)

    assert OPTION_FRESHNESS["4h"] == OPTION_FRESHNESS["1h"]
    assert QUOTE_FRESHNESS["4h"] == QUOTE_FRESHNESS["1h"]
    assert CME_FRESHNESS["4h"] == CME_FRESHNESS["1h"]
    assert LIFECYCLE_FRESHNESS["4h"] == LIFECYCLE_FRESHNESS["1h"]
    assert ENERGY_FRESHNESS["4h"] == ENERGY_FRESHNESS["1h"]


@pytest.mark.parametrize(
    ("family", "freshness"),
    (
        ("opt", OPTION_FRESHNESS["4h"]),
        ("quote", QUOTE_FRESHNESS["4h"]),
        ("cme", CME_FRESHNESS["4h"]),
        ("life", LIFECYCLE_FRESHNESS["4h"]),
        ("energy", ENERGY_FRESHNESS["4h"]),
    ),
)
def test_4h_family_freshness_cutoff_is_inclusive_then_fails_closed(
    family: str,
    freshness: pd.Timedelta,
) -> None:
    available = pd.Timestamp("2026-07-29T15:00:00Z")
    decisions = pd.DataFrame(
        {
            "symbol": ["GOOG", "GOOG"],
            "decision_timestamp": [
                available + freshness,
                available + freshness + pd.Timedelta(nanoseconds=1),
            ],
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["GOOG"],
            "available_at": [available],
            "value": [1.0],
        }
    )

    joined = backward_asof_by_symbol(
        decisions,
        source,
        family=family,
        value_columns={f"{family}__value": "value"},
        freshness=freshness,
    )

    assert joined.loc[0, f"{family}__value"] == 1.0
    assert joined.loc[0, f"{family}__join_status"] == "JOINED"
    assert pd.isna(joined.loc[1, f"{family}__value"])
    assert joined.loc[1, f"{family}__join_status"] == "STALE"


@pytest.mark.parametrize(
    ("feature_set_name", "expected_fingerprint"),
    tuple(_EXISTING_FEATURE_SET_FINGERPRINTS.items()),
)
def test_feature_set_semantic_fingerprints_match_versioned_snapshots(
    feature_set_name: str,
    expected_fingerprint: str,
) -> None:
    assert (
        DEFAULT_FEATURE_REGISTRY.feature_set(
            feature_set_name
        ).semantic_fingerprint
        == expected_fingerprint
    )


def test_readiness_uses_the_active_horizon_scoped_4h_feature_contract() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["GOOG", "NVDA"],
            "horizon": ["4h", "4h"],
            "decision_timestamp": pd.to_datetime(
                [
                    "2026-07-29T17:05:00Z",
                    "2026-07-29T17:05:00Z",
                ],
                utc=True,
            ),
        }
    )
    joined = decisions.copy()
    joined["quote__relative_bid_ask_spread"] = [0.01, 0.02]
    joined["quote__available_at"] = decisions["decision_timestamp"]
    joined["quote__is_stale"] = False
    source = pd.DataFrame(
        {
            "symbol": ["GOOG", "NVDA"],
            "available_at": decisions["decision_timestamp"],
            "schema_version": ["quote-liquidity-v1"] * 2,
            "calculation_version": ["1.0.0"] * 2,
        }
    )

    report = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="loop-a-all-v1-4h",
        horizon="4h",
        feature_names=("quote__relative_bid_ask_spread",),
        source_frames={"quote": source},
        natural_keys={"quote": ("symbol", "available_at")},
        minimum_eligible_decisions=2,
        minimum_symbol_coverage=1.0,
    )

    assert report.state == "ACTIVE"
    assert report.features[0].eligible_decision_count == 2
    report.ensure_model_ready()
