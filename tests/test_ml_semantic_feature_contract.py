from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from ml.artifacts import input_inventory
from ml.contracts import FeatureSet, FeatureSpec, MLContractError
from ml.datasets.technical import TechnicalDatasetConfig
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import (
    DEFAULT_HORIZON_SPECIFICATIONS,
    horizon_specifications_for_profile,
)
from ml.model_features import model_matrix_for_feature_set
from ml.preprocessing import (
    LOG1P_CAPPED_TRAINING_QUANTILES,
    PREPROCESSING_POLICY_VERSION,
    TRAINING_CLIP_LOWER_QUANTILE,
    TRAINING_CLIP_UPPER_QUANTILE,
    QuantileClipper,
    SemanticFeatureTransformer,
    preprocessing_policy,
)
from ml.rolling_materialization import RollingMaterialization, RouteMaterialization
from ml.runtime_pipeline import RuntimeConfig, _horizon_source_files


EXPECTED_TECHNICAL_ALL = (
    "mr__trend_atr",
    "mr__momentum_risk_adjusted",
    "mr__range_position",
    "mr__volume_score",
    "mr__volatility_ratio",
    "bp__compression_score",
    "bp__range_contraction_score",
    "bp__direction_score",
    "bp__upside_pressure_score",
    "bp__downside_pressure_score",
    "bp__breakout_magnitude_atr",
    "bp__volume_participation_score",
    "mr__technical_score",
    "mr__regime_strength",
    "bp__breakout_readiness_score",
    "bp__breakout_strength_score",
    "bp__setup_quality",
    "mr__confidence_score",
    "bp__confidence_score",
)


def test_current_default_and_phase1_v2_order_are_exact_and_deterministic() -> None:
    assert DEFAULT_HORIZON_SPECIFICATIONS["1h"].feature_set == "technical-all"
    assert (
        DEFAULT_HORIZON_SPECIFICATIONS["4h"].feature_set
        == "technical-all-4h"
    )
    assert DEFAULT_HORIZON_SPECIFICATIONS["1d"].feature_set == "technical-all"
    assert DEFAULT_HORIZON_SPECIFICATIONS["1w"].feature_set == "technical-all"
    assert DEFAULT_FEATURE_REGISTRY.feature_set("technical-all").names == (
        EXPECTED_TECHNICAL_ALL
    )
    assert DEFAULT_FEATURE_REGISTRY.feature_set(
        "technical-all-4h"
    ).names == EXPECTED_TECHNICAL_ALL

    selected = horizon_specifications_for_profile("technical-all-v2")
    assert DEFAULT_FEATURE_REGISTRY.feature_set(
        selected["1h"].feature_set
    ).names == (
        *EXPECTED_TECHNICAL_ALL,
        "mr__atr_percent",
        "mr__technical_score_change_5",
        "bp__bars_since_state_change",
    )
    assert DEFAULT_FEATURE_REGISTRY.feature_set(
        selected["4h"].feature_set
    ).names == (
        *EXPECTED_TECHNICAL_ALL,
        "mr__atr_percent",
        "mr__technical_score_change_5",
        "bp__bars_since_state_change",
    )
    assert DEFAULT_FEATURE_REGISTRY.feature_set(
        selected["1d"].feature_set
    ).names == (
        *EXPECTED_TECHNICAL_ALL,
        "mr__atr_percent",
        "mr__technical_score_change_5",
        "bp__readiness_change_5",
    )
    assert DEFAULT_FEATURE_REGISTRY.feature_set(
        selected["1w"].feature_set
    ).names == (
        *EXPECTED_TECHNICAL_ALL,
        "mr__atr_percent",
        "mr__technical_score_change_5",
        "mr__bars_since_regime_change",
    )


def test_quarantined_families_have_no_cli_or_runtime_profile_override() -> None:
    with pytest.raises(ValueError, match="Unknown feature profile"):
        horizon_specifications_for_profile("option-candidate-v1")
    with pytest.raises(ValueError, match="Unsupported feature_profile"):
        RuntimeConfig(feature_profile="fundamental-candidate-v1")
    with pytest.raises(MLContractError, match="not active"):
        DEFAULT_FEATURE_REGISTRY.feature_set(
            "cme-candidate-v1",
            require_active=True,
            horizon="1h",
        )


def test_obsolete_v1_subset_feature_sets_are_removed() -> None:
    retired = (
        "technical-raw",
        "technical-aggregates",
        "technical-raw-plus-aggregates",
        "technical-raw-plus-confidence",
    )
    for name in retired:
        with pytest.raises(MLContractError, match="Unknown feature set"):
            DEFAULT_FEATURE_REGISTRY.feature_set(name)
    assert TechnicalDatasetConfig().feature_set == "technical-all"


def test_production_v3_uses_verified_alfred_only_for_daily_and_weekly() -> None:
    selected = horizon_specifications_for_profile(
        "loop-a-all-bsgp-active-v3"
    )
    assert selected["1h"].feature_set == "loop-a-all-bsgp-active-v2-1h"
    assert selected["4h"].feature_set == "loop-a-all-bsgp-active-v2-4h"
    assert selected["1d"].feature_set == "loop-a-all-bsgp-active-v3-1d"
    assert selected["1w"].feature_set == "loop-a-all-bsgp-active-v3-1w"

    macro_names = {
        "macro__fed_funds_level",
        "macro__cpi_yoy",
        "macro__unemployment_change",
        "macro__gdp_yoy",
    }
    for horizon in ("1h", "4h"):
        feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
            selected[horizon].feature_set
        )
        assert macro_names.isdisjoint(feature_set.names)
    for horizon in ("1d", "1w"):
        feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
            selected[horizon].feature_set,
            require_active=True,
            horizon=horizon,
        )
        assert macro_names.issubset(feature_set.names)
        for feature in feature_set.for_family("macro"):
            assert feature.provider_policy == "fred-alfred-api-v1-immutable-vintages"
            assert feature.required_calculation_versions == ("2.0.0",)
            assert feature.required_schema_versions == (
                "macro-alfred-release-context-v2",
            )
            assert feature.readiness_policy_version == (
                "fred-alfred-readiness-receipt-v1"
            )

    assert (
        DEFAULT_FEATURE_REGISTRY.feature_set(
            "loop-a-all-bsgp-active-v3-1d"
        ).semantic_fingerprint
        != DEFAULT_FEATURE_REGISTRY.feature_set(
            "loop-a-all-bsgp-active-v2-1d"
        ).semantic_fingerprint
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("source_column", "different_value"),
        ("dtype", "float32"),
        ("applicable_horizons", ("1d",)),
        ("provider_policy", "different-provider-policy"),
        ("source_timeframe", "different-timeframe"),
        ("source_grain", "different-grain"),
        ("required_calculation_versions", ("99.0.0",)),
        ("required_schema_versions", ("schema-v99",)),
        ("availability_rule", "different-availability"),
        ("availability_rule_version", "availability-v99"),
        ("processing_delay_seconds", 301),
        ("freshness_by_horizon", (("1h", "one-second"),)),
        ("missing_policy", "different-missing-policy"),
        ("transform_version", "log1p-nonnegative-v1"),
        ("coverage_policy", "different-coverage-policy"),
        ("readiness_policy_version", "readiness-v99"),
        ("audit_classification", "DIFFERENT_AUDIT_CLASSIFICATION"),
        ("recommended_preprocessing", "different-preprocessing"),
        ("is_aggregate_score", True),
        ("is_confidence_field", True),
    ),
)
def test_every_feature_semantic_change_changes_reuse_fingerprint(
    field: str,
    replacement: object,
) -> None:
    original = DEFAULT_FEATURE_REGISTRY.feature_set("technical-all-v2-1h")
    changed_first = replace(original.features[0], **{field: replacement})
    changed = replace(
        original,
        features=(changed_first, *original.features[1:]),
    )
    assert changed.semantic_fingerprint != original.semantic_fingerprint


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("name", "renamed-active-set"),
        ("version", "99.0.0"),
        ("applicable_horizons", ("1h", "1d")),
    ),
)
def test_feature_set_semantic_change_changes_reuse_fingerprint(
    field: str,
    replacement: object,
) -> None:
    original = DEFAULT_FEATURE_REGISTRY.feature_set("technical-all-v2-1h")
    changed = replace(original, **{field: replacement})
    assert changed.semantic_fingerprint != original.semantic_fingerprint


@pytest.mark.parametrize(
    "unsafe_source",
    (
        "id",
        "available_at",
        "provider",
        "source_file_path",
        "freshness_age_seconds",
        "quality_status",
        "future_return",
        "target_price",
        "prediction_probability",
        "evaluation_status",
    ),
)
def test_metadata_and_post_decision_fields_cannot_enter_model_matrix(
    unsafe_source: str,
) -> None:
    spec = FeatureSpec(
        name="test__value",
        source_family="test",
        source_column=unsafe_source,
    )
    feature_set = FeatureSet("unsafe-test", (spec,))
    with pytest.raises(MLContractError, match="cannot enter"):
        model_matrix_for_feature_set(
            pd.DataFrame({"test__value": [1.0]}),
            feature_set,
        )


def test_training_fitted_clipping_does_not_fit_on_future_values() -> None:
    train = np.arange(1.0, 101.0).reshape(-1, 1)
    validation = np.array([[1_000_000.0]])
    clipper = QuantileClipper()

    assert clipper.lower_quantile == 0.0025
    assert clipper.upper_quantile == 0.9975

    clipper.fit(train)
    upper_before = clipper.upper_bounds_.copy()

    transformed = clipper.transform(validation)

    np.testing.assert_array_equal(clipper.upper_bounds_, upper_before)
    assert transformed[0, 0] == pytest.approx(float(upper_before[0]))


def test_quantile_preprocessing_policy_is_explicit_and_family_aware() -> None:
    assert TRAINING_CLIP_LOWER_QUANTILE == 0.0025
    assert TRAINING_CLIP_UPPER_QUANTILE == 0.9975
    assert PREPROCESSING_POLICY_VERSION == "training-quantiles-0.25-99.75-v1"
    assert preprocessing_policy("logistic") == {
        "version": PREPROCESSING_POLICY_VERSION,
        "fit_partition": "training",
        "numeric_quantile_clipping": {
            "lower_quantile": 0.0025,
            "upper_quantile": 0.9975,
        },
        "semantic_log1p_training_cap_quantiles": {
            "log1p-capped-training-v1": 0.995,
            "log1p-capped-training-v2": 0.9975,
        },
    }
    assert preprocessing_policy("lightgbm")["numeric_quantile_clipping"] is None


@pytest.mark.parametrize(
    ("transform_version", "expected_quantile"),
    tuple(LOG1P_CAPPED_TRAINING_QUANTILES.items()),
)
def test_ratio_cap_is_fitted_only_on_training_rows(
    transform_version: str,
    expected_quantile: float,
) -> None:
    train = pd.DataFrame({"opt__ratio": np.arange(1.0, 101.0)})
    validation = pd.DataFrame({"opt__ratio": [1_000_000.0]})
    transformer = SemanticFeatureTransformer(
        {"opt__ratio": transform_version}
    ).fit(train)
    cap_before = transformer.training_caps_["opt__ratio"]

    transformed = transformer.transform(validation)

    assert cap_before == pytest.approx(
        float(train["opt__ratio"].quantile(expected_quantile))
    )
    assert transformer.training_caps_["opt__ratio"] == cap_before
    assert transformed.iloc[0, 0] == pytest.approx(np.log1p(cap_before))


def test_source_changes_invalidate_only_the_affected_horizon(
    tmp_path,
) -> None:
    hourly = tmp_path / "hourly.parquet"
    daily = tmp_path / "daily.parquet"
    hourly.write_bytes(b"hourly-v1")
    daily.write_bytes(b"daily-v1")
    materialization = RollingMaterialization(
        samples=pd.DataFrame(),
        routes=(
            RouteMaterialization(
                symbol="NVDA",
                horizon="1h",
                status="READY",
                samples=pd.DataFrame(),
                source_files=(hourly,),
            ),
            RouteMaterialization(
                symbol="NVDA",
                horizon="1d",
                status="READY",
                samples=pd.DataFrame(),
                source_files=(daily,),
            ),
        ),
        source_files=(hourly, daily),
        datastore_root=tmp_path,
    )
    hourly_before = input_inventory(
        _horizon_source_files(materialization, "1h"),
        relative_to=tmp_path,
    )
    daily_before = input_inventory(
        _horizon_source_files(materialization, "1d"),
        relative_to=tmp_path,
    )

    hourly.write_bytes(b"hourly-v2")

    hourly_after = input_inventory(
        _horizon_source_files(materialization, "1h"),
        relative_to=tmp_path,
    )
    daily_after = input_inventory(
        _horizon_source_files(materialization, "1d"),
        relative_to=tmp_path,
    )
    assert hourly_after != hourly_before
    assert daily_after == daily_before
