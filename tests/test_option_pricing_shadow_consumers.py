from __future__ import annotations

import json
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ml.rolling_materialization as rolling_materialization
import ml.runtime_pipeline as loop_b_runtime
from app.services.schwab_strategy_orders import (
    SchwabPositionContext,
    build_strategy_order_draft,
)
from ml.artifacts import file_checksum, write_manifest
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import DEFAULT_FEATURE_PROFILE, horizon_specifications_for_profile
from ml.model_features import model_matrix_for_feature_set
from ml.option_pricing.consumers import (
    LEGACY_NORMALIZATION_POLICY,
    LEGACY_PRICING_POLICY_VERSION,
    PricingEvidenceContractError,
    read_verified_compact_pricing_features,
)
from ml.option_pricing.publication import (
    LEGACY_OPTION_PRICING_PUBLICATION_VERSION,
    OPTION_PRICING_PUBLICATION_VERSION,
    OptionPricingPublicationError,
    pricing_pointer_path,
    publish_option_pricing_run,
)
from ml.option_pricing.policies import (
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
)
from ml.option_pricing.reporting import SURFACE_VERSION
from ml.option_pricing.reporting import build_pricing_surfaces
from ml.option_pricing.strategy_shadow import (
    StrategyPricingEvidenceCatalog,
    attach_strategy_pricing_evidence,
    attach_strategy_pricing_shadow,
)
from ml.parquet_contracts import (
    LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
    LEGACY_OPTION_PRICING_EVALUATION_SCHEMA,
    LEGACY_OPTION_PRICING_PREDICTION_SCHEMA,
    LEGACY_OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    empty_frame,
    frame_with_readable_id,
    write_parquet_with_schema,
)
from ml.rolling_materialization import (
    RollingMaterialization,
    RouteMaterialization,
    _join_symbol_values,
)
from ml.runtime_pipeline import (
    RuntimeConfig,
    _pricing_evidence_manifest,
    _pricing_family_gate,
    run_loop_b_once,
)
from ml.models.registry import ModelSpec, build_estimator
from ml.strategy_selection.contracts import StrategySelectionPolicy
from ml.strategy_selection.market_state import MarketState, score_market_state_prior


def _publish_pricing(
    root: Path,
    *,
    legacy: bool = False,
    published_at: str = "2026-07-06T14:01:01Z",
    row_available_at: str = "2026-07-06T14:01:01Z",
    symbol: str = "NVDA",
) -> Path:
    run = root / "ml" / "option-pricing-runs" / "20260706T140100.000000Z"
    run.mkdir(parents=True)
    prediction = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "source_provider": "schwab",
                "prediction_mode": "LIVE",
                "call_put": "CALL",
                "contract_symbol": f"{symbol}  260821C00100000",
                "expiration_date": pd.Timestamp("2026-08-21T00:00:00Z"),
                "target_snapshot_for": pd.Timestamp("2026-07-06T14:00:00Z"),
                "source_snapshot_for": pd.Timestamp("2026-07-06T13:45:00Z"),
                "source_available_at": pd.Timestamp("2026-07-06T13:46:00Z"),
                "prediction_created_at": pd.Timestamp("2026-07-06T14:01:00Z"),
                "prediction_available_at": pd.Timestamp("2026-07-06T14:01:01Z"),
                "source_quote_staleness_seconds": 60.0,
                "model_name": "bsgp",
                "model_version": "fixture",
                "model_status": "MODEL_FIT",
                "underlying_price": 100.0,
                "strike": 100.0,
                "multiplier": 100.0,
                "risk_free_rate": 0.04,
                "lagged_implied_volatility": 0.30,
                "target_years_to_expiration": 46 / 365,
                "dividend_yield": 0.01,
                "black_scholes_price": 2.0,
                "predicted_normalized_residual": 0.003,
                "raw_fair_value": 2.3,
                "point_lower_bound": 0.0,
                "point_upper_bound": 100.0,
                "predictive_standard_deviation": 0.10,
                "raw_interval_80_lower": 2.15,
                "raw_interval_80_upper": 2.45,
                "raw_interval_95_lower": 2.05,
                "raw_interval_95_upper": 2.55,
                "constrained_fair_value": 2.3,
                "constrained_interval_80_lower": 2.15,
                "constrained_interval_80_upper": 2.45,
                "constrained_interval_95_lower": 2.05,
                "constrained_interval_95_upper": 2.55,
                "raw_bound_violation": False,
                "raw_monotonicity_violation": False,
                "raw_convexity_violation": False,
                "constrained_bound_violation": False,
                "constrained_monotonicity_violation": False,
                "constrained_convexity_violation": False,
                "projection_correction": 0.0,
                "projection_status": "COMPLETE",
                "prediction_status": "CREATED",
                "pricing_policy_version": (
                    LEGACY_PRICING_POLICY_VERSION
                    if legacy
                    else OPTION_PRICING_POLICY_VERSION
                ),
                "timing_policy_version": (
                    "pre-quote-quarter-hour-v1"
                    if legacy
                    else OPTION_PRICING_TIMING_POLICY_VERSION
                ),
                "schema_version": OPTION_PRICING_SCHEMA_VERSION,
                "automated_action_allowed": False,
            }
        ]
    )
    prediction = frame_with_readable_id(
        prediction,
        key_columns=("symbol", "target_snapshot_for", "contract_symbol", "prediction_created_at"),
    )
    surface = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "target_snapshot_for": pd.Timestamp("2026-07-06T14:00:00Z"),
                "available_at": pd.Timestamp(row_available_at),
                "first_available_at": pd.Timestamp(row_available_at),
                "call_put": "CALL",
                "expiration_bucket": "31-60d",
                "moneyness_bucket": "near-the-money",
                "source_provider": "schwab",
                "prediction_mode": "LIVE",
                "contract_count": 1,
                "causal_coverage": 1.0,
                "median_normalized_residual": 0.003,
                "median_predictive_standard_deviation": 0.10,
                "median_model_edge_in_half_spreads": 2.0,
                "positive_edge_fraction": 1.0,
                "negative_edge_fraction": 0.0,
                "raw_arbitrage_violation_rate": 0.0,
                "constrained_arbitrage_violation_rate": 0.0,
                "interval_80_coverage": 0.8,
                "interval_95_coverage": 0.95,
                "median_bid_ask_spread": 0.2,
                "median_relative_bid_ask_spread": 0.1,
                "median_quote_staleness_seconds": 5.0,
                "surface_quality_pass": True,
                "surface_status": "AVAILABLE",
                "pricing_policy_version": (
                    LEGACY_PRICING_POLICY_VERSION
                    if legacy
                    else OPTION_PRICING_POLICY_VERSION
                ),
                "schema_version": (
                    "option-pricing-compact-surface-v1" if legacy else SURFACE_VERSION
                ),
                "automated_action_allowed": False,
            }
        ]
    )
    surface = frame_with_readable_id(
        surface,
        key_columns=("symbol", "target_snapshot_for", "call_put", "expiration_bucket", "moneyness_bucket"),
    )
    surface_schema = (
        LEGACY_OPTION_PRICING_SURFACE_SCHEMA
        if legacy
        else OPTION_PRICING_SURFACE_SCHEMA
    )
    if legacy:
        surface = surface.drop(columns="first_available_at")
    sample_schema = (
        LEGACY_OPTION_PRICING_SAMPLE_SCHEMA
        if legacy
        else OPTION_PRICING_SAMPLE_SCHEMA
    )
    prediction_schema = (
        LEGACY_OPTION_PRICING_PREDICTION_SCHEMA
        if legacy
        else OPTION_PRICING_PREDICTION_SCHEMA
    )
    evaluation_schema = (
        LEGACY_OPTION_PRICING_EVALUATION_SCHEMA
        if legacy
        else OPTION_PRICING_EVALUATION_SCHEMA
    )
    outputs = {
        "pricing-samples.parquet": (empty_frame(sample_schema), sample_schema),
        "pricing-predictions.parquet": (prediction, prediction_schema),
        "pricing-evaluations.parquet": (empty_frame(evaluation_schema), evaluation_schema),
        "pricing-surfaces.parquet": (surface, surface_schema),
        "pricing-monitoring.parquet": (empty_frame(OPTION_PRICING_MONITORING_SCHEMA), OPTION_PRICING_MONITORING_SCHEMA),
    }
    for name, (frame, schema) in outputs.items():
        write_parquet_with_schema(frame, run / name, schema)
    report = "option-pricing-model-reports.json"
    (run / report).write_text("{}\n", encoding="utf-8")
    write_manifest(
        run,
        run_timestamp="2026-07-06T14:01:00Z",
        input_files=(),
        output_files=(*outputs, report),
        configuration={
            "publication_contract": {
                "version": (
                    LEGACY_OPTION_PRICING_PUBLICATION_VERSION
                    if legacy
                    else OPTION_PRICING_PUBLICATION_VERSION
                ),
                "authority": "ml/option-pricing-latest/run.json",
                "schema_validation": True,
                "automated_action_allowed": False,
            }
        },
        datastore_root=root,
    )
    if legacy:
        receipt = {
            "schema_version": LEGACY_OPTION_PRICING_PUBLICATION_VERSION,
            "run_path": run.relative_to(root).as_posix(),
            "run_timestamp": "2026-07-06T14:01:00+00:00",
            "published_at": pd.Timestamp(published_at).isoformat(),
            "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
            "previous_publication": None,
        }
        (run / "publication.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pointer = pricing_pointer_path(root)
        pointer.parent.mkdir(parents=True)
        pointer.write_text(
            json.dumps(
                {
                    "schema_version": "option-pricing-pointer-v1",
                    "current": {
                        "run_path": receipt["run_path"],
                        "run_timestamp": receipt["run_timestamp"],
                        "published_at": receipt["published_at"],
                        "manifest_checksum_sha256": receipt[
                            "manifest_checksum_sha256"
                        ],
                        "receipt_checksum_sha256": file_checksum(
                            run / "publication.json"
                        ),
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        publish_option_pricing_run(
            root,
            run_directory=run,
            published_at=published_at,
        )
    return run


def _reseal_pricing_surface(
    root: Path,
    run: Path,
    surface: pd.DataFrame,
    *,
    surface_schema=OPTION_PRICING_SURFACE_SCHEMA,
    publication_version: str = OPTION_PRICING_PUBLICATION_VERSION,
) -> None:
    write_parquet_with_schema(
        surface,
        run / "pricing-surfaces.parquet",
        surface_schema,
    )
    output_names = (
        "pricing-samples.parquet",
        "pricing-predictions.parquet",
        "pricing-evaluations.parquet",
        "pricing-surfaces.parquet",
        "pricing-monitoring.parquet",
        "option-pricing-model-reports.json",
    )
    write_manifest(
        run,
        run_timestamp="2026-07-06T14:01:00Z",
        input_files=(),
        output_files=output_names,
        configuration={
            "publication_contract": {
                "version": publication_version,
                "authority": "ml/option-pricing-latest/run.json",
                "schema_validation": True,
                "automated_action_allowed": False,
            }
        },
        datastore_root=root,
    )
    receipt = {
        "schema_version": publication_version,
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": "2026-07-06T14:01:00+00:00",
        "published_at": "2026-07-06T14:01:01+00:00",
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "previous_publication": None,
    }
    (run / "publication.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pricing_pointer_path(root).write_text(
        json.dumps(
            {
                "schema_version": "option-pricing-pointer-v2",
                "current": {
                    "run_path": receipt["run_path"],
                    "run_timestamp": receipt["run_timestamp"],
                    "published_at": receipt["published_at"],
                    "manifest_checksum_sha256": receipt[
                        "manifest_checksum_sha256"
                    ],
                    "receipt_checksum_sha256": file_checksum(
                        run / "publication.json"
                    ),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _candidate() -> pd.DataFrame:
    leg = {
        "asset": "OPTION",
        "contract_symbol": "NVDA  260821C00100000",
        "side": "LONG",
        "quantity": 1,
        "option_type": "CALL",
        "expiration_role": "FRONT",
        "expiration_date": "2026-08-21T00:00:00+00:00",
        "strike": 100.0,
        "bid": 1.9,
        "ask": 2.1,
        "multiplier": 100.0,
        "target_snapshot_for": "2026-07-06T14:00:00+00:00",
        "quote_timestamp": "2026-07-06T14:02:00+00:00",
    }
    return pd.DataFrame(
        [
            {
                "id": "NVDA | 1h | 2026-07-06T14:00Z | long_call",
                "symbol": "NVDA",
                "horizon": "1h",
                "decision_timestamp": pd.Timestamp("2026-07-06T14:00:00Z"),
                "candidate_key": "long_call|w1|front=2026-08-21|back=none",
                "candidate_rank": 1,
                "decision_score": 0.7,
                "expected_net_profit": 5.0,
                "strategy_name": "long_call",
                "strategy_display_name": "Long Call",
                "stock_requirement": "NONE",
                "legs_json": json.dumps([leg], sort_keys=True, separators=(",", ":")),
            }
        ]
    )


def test_directional_shadow_profile_is_explicit_and_default_is_unchanged() -> None:
    assert DEFAULT_FEATURE_PROFILE == "loop-a-all-v1"
    specs = horizon_specifications_for_profile("loop-a-all-bsgp-shadow-v1")
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        specs["1h"].feature_set,
        require_active=True,
        horizon="1h",
    )
    assert any(name.startswith("opx__") for name in feature_set.names)
    active_specs = horizon_specifications_for_profile(
        "loop-a-all-bsgp-active-v3"
    )
    active_features = DEFAULT_FEATURE_REGISTRY.feature_set(
        active_specs["1h"].feature_set,
        require_active=True,
        horizon="1h",
    )
    assert any(name.startswith("opx__") for name in active_features.names)
    default = horizon_specifications_for_profile()["1h"]
    assert not any(
        name.startswith("opx__")
        for name in DEFAULT_FEATURE_REGISTRY.feature_set(default.feature_set).names
    )


def test_verified_compact_surface_respects_publication_cutoff(tmp_path: Path) -> None:
    run = _publish_pricing(tmp_path)
    frame, sources = read_verified_compact_pricing_features(
        tmp_path,
        available_not_after="2026-07-06T14:01:01Z",
    )
    assert frame.iloc[0]["median_model_edge_in_half_spreads"] == pytest.approx(2.0)
    assert sources
    with pytest.raises(FileNotFoundError, match="causal cutoff"):
        read_verified_compact_pricing_features(
            tmp_path,
            available_not_after="2026-07-06T14:01:00Z",
        )
    with (run / "pricing-surfaces.parquet").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(RuntimeError):
        read_verified_compact_pricing_features(
            tmp_path,
            available_not_after="2026-07-06T14:03:00Z",
        )


def test_verified_v1_surface_is_normalized_in_memory_without_mutation(
    tmp_path: Path,
) -> None:
    run = _publish_pricing(
        tmp_path,
        legacy=True,
        row_available_at="2026-07-06T14:01:01Z",
        published_at="2026-07-06T14:05:00Z",
    )
    immutable = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            run / "pricing-surfaces.parquet",
            run / "manifest.json",
            run / "publication.json",
            pricing_pointer_path(tmp_path),
        )
    }

    frame, sources = read_verified_compact_pricing_features(
        tmp_path,
        available_not_after="2026-07-06T14:06:00Z",
    )

    canonical = pd.Timestamp("2026-07-06T14:05:00Z")
    assert frame["available_at"].eq(canonical).all()
    assert frame["first_available_at"].eq(canonical).all()
    assert frame["_pricing_original_available_at"].eq(
        pd.Timestamp("2026-07-06T14:01:01Z")
    ).all()
    expected_provenance = {
        "source_publication_version": "option-pricing-publication-v1",
        "source_surface_version": "option-pricing-compact-surface-v1",
        "source_policy_version": LEGACY_PRICING_POLICY_VERSION,
        "normalization_policy": LEGACY_NORMALIZATION_POLICY,
        "legacy_normalized": True,
        "authority_published_at": canonical.isoformat(),
        "authority_run_path": run.relative_to(tmp_path).as_posix(),
    }
    assert {
        key: frame.attrs["pricing_evidence"][key]
        for key in expected_provenance
    } == expected_provenance
    assert frame.attrs["pricing_evidence"]["history_policy"] == (
        "append-only-newest-causal-generation-v1"
    )
    assert sources[0] == run / "pricing-surfaces.parquet"
    assert immutable == {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in immutable
    }


def test_v1_normalization_does_not_renew_the_market_target(tmp_path: Path) -> None:
    _publish_pricing(
        tmp_path,
        legacy=True,
        row_available_at="2026-07-06T14:01:01Z",
        published_at="2026-07-06T14:10:00Z",
    )
    source, _paths = read_verified_compact_pricing_features(
        tmp_path,
        available_not_after="2026-07-06T16:00:01Z",
    )
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "decision_timestamp": [pd.Timestamp("2026-07-06T16:00:01Z")],
        }
    )

    joined = _join_symbol_values(
        decisions,
        source,
        family="opx",
        value_columns={"opx__causal_coverage": "causal_coverage"},
        freshness=pd.Timedelta(hours=2),
        event_column="target_snapshot_for",
        quality_columns=("surface_quality_pass",),
        require_populated_values=False,
    )

    assert pd.isna(joined.loc[0, "opx__causal_coverage"])
    assert joined.loc[0, "opx__join_status"] == "STALE"


def test_v1_canonical_availability_uses_later_verified_row_clock(
    tmp_path: Path,
) -> None:
    _publish_pricing(
        tmp_path,
        legacy=True,
        row_available_at="2026-07-06T14:05:00Z",
        published_at="2026-07-06T14:02:00Z",
    )

    frame, _sources = read_verified_compact_pricing_features(
        tmp_path,
        available_not_after="2026-07-06T14:06:00Z",
    )

    assert frame["available_at"].eq(pd.Timestamp("2026-07-06T14:05:00Z")).all()
    assert frame["first_available_at"].eq(
        pd.Timestamp("2026-07-06T14:05:00Z")
    ).all()


def test_v1_surface_with_nonlegacy_policy_is_rejected(tmp_path: Path) -> None:
    run = _publish_pricing(tmp_path, legacy=True)
    surface = pd.read_parquet(run / "pricing-surfaces.parquet")
    surface["pricing_policy_version"] = OPTION_PRICING_POLICY_VERSION
    _reseal_pricing_surface(
        tmp_path,
        run,
        surface,
        surface_schema=LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
        publication_version=LEGACY_OPTION_PRICING_PUBLICATION_VERSION,
    )

    with pytest.raises(PricingEvidenceContractError, match="policy version"):
        read_verified_compact_pricing_features(
            tmp_path,
            available_not_after="2026-07-06T14:03:00Z",
        )


def test_verified_v2_surface_missing_first_availability_still_fails_closed(
    tmp_path: Path,
) -> None:
    run = _publish_pricing(tmp_path)
    surface = pd.read_parquet(run / "pricing-surfaces.parquet").drop(
        columns="first_available_at"
    )
    surface = surface.loc[:, LEGACY_OPTION_PRICING_SURFACE_SCHEMA.names]
    _reseal_pricing_surface(
        tmp_path,
        run,
        surface,
        surface_schema=LEGACY_OPTION_PRICING_SURFACE_SCHEMA,
    )

    with pytest.raises(OptionPricingPublicationError, match="Parquet contract"):
        read_verified_compact_pricing_features(
            tmp_path,
            available_not_after="2026-07-06T14:03:00Z",
        )


@pytest.mark.parametrize("mutation", ("mixed", "unknown", "malformed", "automation"))
def test_verified_surface_rejects_incompatible_or_unsafe_rows(
    tmp_path: Path,
    mutation: str,
) -> None:
    run = _publish_pricing(tmp_path)
    surface = pd.read_parquet(run / "pricing-surfaces.parquet")
    if mutation == "mixed":
        second = surface.drop(columns="id").copy()
        second["call_put"] = "PUT"
        second["schema_version"] = "option-pricing-compact-surface-v1"
        surface = frame_with_readable_id(
            pd.concat([surface.drop(columns="id"), second], ignore_index=True),
            key_columns=(
                "symbol",
                "target_snapshot_for",
                "call_put",
                "expiration_bucket",
                "moneyness_bucket",
            ),
        )
    elif mutation == "unknown":
        surface["schema_version"] = "option-pricing-compact-surface-v999"
    elif mutation == "malformed":
        surface["target_snapshot_for"] = pd.NaT
    else:
        surface["automated_action_allowed"] = True
    _reseal_pricing_surface(tmp_path, run, surface)

    with pytest.raises(PricingEvidenceContractError):
        read_verified_compact_pricing_features(
            tmp_path,
            available_not_after="2026-07-06T14:03:00Z",
        )


def test_unknown_publication_contract_fails_before_surface_consumption(
    tmp_path: Path,
) -> None:
    run = _publish_pricing(tmp_path)
    surface = pd.read_parquet(run / "pricing-surfaces.parquet")
    _reseal_pricing_surface(
        tmp_path,
        run,
        surface,
        publication_version="option-pricing-publication-v999",
    )

    with pytest.raises(OptionPricingPublicationError, match="publication contract"):
        read_verified_compact_pricing_features(
            tmp_path,
            available_not_after="2026-07-06T14:03:00Z",
        )


def test_active_v2_model_contract_predicts_with_missing_pricing_family() -> None:
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        "loop-a-all-bsgp-active-v2-1h",
        require_active=True,
        horizon="1h",
    )
    rows = pd.DataFrame(
        {
            name: (
                [np.nan] * 8
                if name.startswith("opx__")
                else np.linspace(1.0, 2.0, 8)
            )
            for name in feature_set.names
        }
    )
    estimator = build_estimator(
        ModelSpec(
            model_name="active-v2-missing-pricing-fixture",
            family="logistic",
            feature_set=feature_set.name,
        ),
        feature_set,
    )

    estimator.fit(rows, pd.Series([0, 1, 0, 1, 0, 1, 0, 1]))
    probabilities = estimator.predict_proba(rows)

    assert probabilities.shape == (8, 2)
    assert np.isfinite(probabilities).all()


def test_pricing_family_gate_requires_coverage_freshness_and_mature_intervals() -> None:
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        "loop-a-all-bsgp-active-v3-1d",
        require_active=True,
        horizon="1d",
    )
    opx_columns = tuple(
        name for name in feature_set.names if name.startswith("opx__")
    )
    targets = pd.date_range("2026-06-01T17:00:00Z", periods=20, freq="D")
    frame = pd.DataFrame(
        {
            "symbol": "NVDA",
            "horizon": "1d",
            "decision_timestamp": targets + pd.Timedelta(minutes=2),
            "opx__source_target_snapshot_for": targets,
            "opx__join_status": "JOINED",
            **{column: 0.0 for column in opx_columns},
        }
    )

    passed = _pricing_family_gate(frame, feature_columns=opx_columns)
    assert passed["downstream_training_eligible"] is True

    immature = frame.copy()
    immature["opx__interval_80_coverage"] = np.nan
    immature["opx__interval_95_coverage"] = np.nan
    assert _pricing_family_gate(
        immature, feature_columns=opx_columns
    )["downstream_training_eligible"] is False

    stale = frame.copy()
    stale["opx__join_status"] = "STALE"
    assert _pricing_family_gate(
        stale, feature_columns=opx_columns
    )["downstream_training_eligible"] is False


def test_active_v2_loop_b_feature_materialization_uses_verified_v1_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _publish_pricing(
        tmp_path,
        legacy=True,
        symbol="NVDA",
        published_at="2026-07-06T14:05:00Z",
        row_available_at="2026-07-06T14:01:01Z",
    )
    specification = horizon_specifications_for_profile(
        "loop-a-all-bsgp-active-v3",
        horizons=("1d",),
    )["1d"]
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        specification.feature_set,
        require_active=True,
        horizon="1d",
    )
    decision_at = pd.Timestamp("2026-07-06T14:06:00Z")
    row: dict[str, object] = {
        "id": "NVDA|1d|2026-07-06T14:06:00Z",
        "symbol": "NVDA",
        "provider": "databento",
        "horizon": "1d",
        "decision_timestamp": decision_at,
        "information_available_at": decision_at,
    }
    for feature in feature_set.features:
        if feature.source_family in {"mr", "bp"}:
            row[feature.name] = 1.0
    decisions = pd.DataFrame([row])
    dummy_path = tmp_path / "fixtures" / "source.parquet"
    real_join = rolling_materialization._join_symbol_values

    def source_paths(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
        return (dummy_path,)

    def read_sources(*_args: object, **_kwargs: object) -> pd.DataFrame:
        return pd.DataFrame({"fixture": [1.0]})

    def fill_values(
        frame: pd.DataFrame,
        _source: pd.DataFrame,
        *,
        value_columns: dict[str, str],
        **_kwargs: object,
    ) -> pd.DataFrame:
        output = frame.copy()
        for name in value_columns:
            output[name] = 1.0
        return output

    def join_values(
        frame: pd.DataFrame,
        source: pd.DataFrame,
        *,
        family: str,
        value_columns: dict[str, str],
        **kwargs: object,
    ) -> pd.DataFrame:
        if family == "opx":
            return real_join(
                frame,
                source,
                family=family,
                value_columns=value_columns,
                **kwargs,
            )
        return fill_values(frame, source, value_columns=value_columns)

    monkeypatch.setattr(
        rolling_materialization,
        "_partitioned_stock_paths",
        source_paths,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_stock_glob_paths",
        source_paths,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_fred_source_paths",
        lambda _root: (dummy_path,),
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_cme_normalized_source_paths",
        lambda _root: (dummy_path, dummy_path, dummy_path),
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_read_required_sources",
        read_sources,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "read_committed_option_surfaces",
        lambda *_args, **_kwargs: (pd.DataFrame(), ()),
    )
    monkeypatch.setattr(
        rolling_materialization,
        "committed_option_snapshots",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_bar_shape_features",
        fill_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_weekly_context_features",
        fill_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_sec_event_features",
        fill_values,
    )
    monkeypatch.setattr(rolling_materialization, "_join_symbol_values", join_values)
    monkeypatch.setattr(rolling_materialization, "_join_shared_values", fill_values)
    monkeypatch.setattr(
        rolling_materialization,
        "read_verified_macro_evidence",
        lambda _root: rolling_materialization.VerifiedMacroEvidence(
            release_context=pd.DataFrame({"fixture": [1.0]}),
            vintages=pd.DataFrame({"fixture": [1.0]}),
            source_files=(dummy_path,),
            readiness=None,  # type: ignore[arg-type]
        ),
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_macro_features",
        fill_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_derive_current_cme_context",
        lambda *_sources: pd.DataFrame({"fixture": [1.0]}),
    )

    assembled, sources = rolling_materialization._attach_loop_a_features(
        tmp_path,
        decisions,
        symbols=("NVDA",),
        horizon="1d",
        source_timeframe=specification.source_timeframe,
        provider="databento",
        feature_set_name=feature_set.name,
        parquet_cache={},
        derived_cache={},
        input_available_at=decision_at,
    )
    matrix = model_matrix_for_feature_set(assembled, feature_set)
    route = RouteMaterialization(
        symbol="NVDA",
        horizon="1d",
        status="READY",
        samples=assembled,
        source_files=sources,
    )
    audit = _pricing_evidence_manifest(
        RollingMaterialization(
            samples=assembled,
            routes=(route,),
            source_files=sources,
            datastore_root=tmp_path,
        ),
        feature_columns=feature_set.names,
    )

    assert matrix.shape == (1, len(feature_set.names))
    assert assembled.loc[0, "opx__join_status"] == "JOINED"
    assert bool(assembled.loc[0, "opx__legacy_normalized"])
    assert assembled.loc[0, "opx__source_surface_version"] == (
        "option-pricing-compact-surface-v1"
    )
    assert run / "pricing-surfaces.parquet" in sources
    route_audit = audit["routes"]["NVDA|1d"]
    assert route_audit["route_status"] == "READY"
    assert route_audit["all_pricing_values_missing_rows"] == 0
    assert route_audit["join_status_counts"] == {"JOINED": 1}

    missing_root = tmp_path / "missing-pricing"
    missing_root.mkdir()
    without_pricing, _missing_sources = (
        rolling_materialization._attach_loop_a_features(
            missing_root,
            decisions,
            symbols=("NVDA",),
            horizon="1d",
            source_timeframe=specification.source_timeframe,
            provider="databento",
            feature_set_name=feature_set.name,
            parquet_cache={},
            derived_cache={},
            input_available_at=decision_at,
        )
    )
    opx_columns = [name for name in feature_set.names if name.startswith("opx__")]
    assert without_pricing.loc[:, opx_columns].isna().all().all()
    assert without_pricing.loc[0, "opx__join_status"] == "NO_PRIOR_PUBLICATION"
    assert without_pricing.loc[0, "opx__source_status"] == "UNAVAILABLE"


def test_fatal_pricing_contract_violation_cannot_partially_promote_loop_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = tmp_path / "ml" / "latest" / "run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b"existing-authority\n")

    def fail_materialization(*_args: object, **_kwargs: object):
        raise PricingEvidenceContractError("tampered Pricing authority")

    monkeypatch.setattr(
        loop_b_runtime,
        "materialize_rolling_samples",
        fail_materialization,
    )

    with pytest.raises(PricingEvidenceContractError, match="tampered"):
        run_loop_b_once(
            tmp_path,
            symbols=("NVDA",),
            config=RuntimeConfig(feature_profile="loop-a-all-bsgp-active-v3"),
            run_timestamp="2026-07-06T14:06:00Z",
            input_available_at="2026-07-06T14:06:00Z",
            reporter=None,
        )

    assert pointer.read_bytes() == b"existing-authority\n"
    assert not (tmp_path / "ml" / "runs").exists()


def test_strategy_off_and_shadow_do_not_change_rank_legs_or_order_draft(tmp_path: Path) -> None:
    _publish_pricing(tmp_path)
    candidate = _candidate()
    position = SchwabPositionContext("NVDA", None, 0.0, 0.0, 0, 10_000.0)
    before = build_strategy_order_draft(candidate.iloc[0].to_dict(), position=position)
    off = attach_strategy_pricing_shadow(
        candidate,
        datastore_root=tmp_path,
        pricing_mode="off",
        available_not_after="2026-07-06T14:03:00Z",
        per_contract_fee=0.65,
    ).candidates
    shadow = attach_strategy_pricing_shadow(
        candidate,
        datastore_root=tmp_path,
        pricing_mode="shadow",
        available_not_after="2026-07-06T14:03:00Z",
        per_contract_fee=0.65,
    ).candidates
    for output in (off, shadow):
        assert output.iloc[0]["candidate_rank"] == candidate.iloc[0]["candidate_rank"]
        assert output.iloc[0]["decision_score"] == candidate.iloc[0]["decision_score"]
        assert output.iloc[0]["legs_json"] == candidate.iloc[0]["legs_json"]
        assert build_strategy_order_draft(output.iloc[0].to_dict(), position=position) == before
    assert off.iloc[0]["pricing_status"] == "Unavailable"
    assert shadow.iloc[0]["pricing_status"] == "Active"
    assert shadow.iloc[0]["pricing_candidate_edge"] == pytest.approx(20.0)
    assert shadow.iloc[0]["pricing_conservative_edge"] == pytest.approx(-5.0)
    assert shadow.iloc[0]["pricing_edge_to_friction"] == pytest.approx(20.0 / 21.30)
    assert shadow.iloc[0]["pricing_uncertainty"] == pytest.approx(10.0)
    assert shadow.iloc[0]["pricing_source"] == "BSGP"


def test_strategy_missing_or_tampered_pricing_falls_back_without_rank_change(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    missing = attach_strategy_pricing_shadow(
        candidate,
        datastore_root=tmp_path,
        pricing_mode="shadow",
        available_not_after="2026-07-06T14:03:00Z",
        per_contract_fee=0.65,
    ).candidates
    assert missing.iloc[0]["pricing_status"] == "Delayed"
    assert missing.iloc[0]["candidate_rank"] == 1

    run = _publish_pricing(tmp_path)
    with (run / "pricing-predictions.parquet").open("ab") as handle:
        handle.write(b"tamper")
    tampered = attach_strategy_pricing_shadow(
        candidate,
        datastore_root=tmp_path,
        pricing_mode="shadow",
        available_not_after="2026-07-06T14:03:00Z",
        per_contract_fee=0.65,
    ).candidates
    assert tampered.iloc[0]["pricing_status"] == "Delayed"
    assert tampered.iloc[0]["candidate_rank"] == 1


def test_exact_contract_matching_and_stale_fallback_are_isolated() -> None:
    candidate = _candidate()
    exact = _canonical_prediction(
        contract_symbol="NVDA  260821C00100000",
        fair_value=2.30,
    )
    matched = attach_strategy_pricing_evidence(
        candidate,
        catalog=StrategyPricingEvidenceCatalog(pd.DataFrame([exact]), ()),
        pricing_mode="active",
        per_contract_fee=0.65,
        allow_offline_replay=False,
    ).candidates.iloc[0]
    assert matched["pricing_status"] == "Active"
    assert matched["pricing_leg_coverage"] == 1.0

    semantic_only = dict(exact)
    semantic_only["contract_symbol"] = "DIFFERENT-CONTRACT"
    rejected = attach_strategy_pricing_evidence(
        candidate,
        catalog=StrategyPricingEvidenceCatalog(
            pd.DataFrame([semantic_only]), ()
        ),
        pricing_mode="active",
        per_contract_fee=0.65,
        allow_offline_replay=False,
    ).candidates.iloc[0]
    assert rejected["pricing_status"] == "Delayed"
    assert rejected["pricing_leg_coverage"] == 0.0
    assert pd.isna(rejected["pricing_candidate_edge"])

    stale = dict(exact)
    stale["input_staleness_seconds"] = 1_201.0
    rejected_stale = attach_strategy_pricing_evidence(
        candidate,
        catalog=StrategyPricingEvidenceCatalog(pd.DataFrame([stale]), ()),
        pricing_mode="active",
        per_contract_fee=0.65,
        allow_offline_replay=False,
    ).candidates.iloc[0]
    assert rejected_stale["pricing_status"] == "Unavailable"
    assert rejected_stale["pricing_source"] == "UNAVAILABLE"
    assert pd.isna(rejected_stale["pricing_candidate_edge"])

    missing_age = dict(exact)
    missing_age["input_staleness_seconds"] = float("nan")
    rejected_missing_age = attach_strategy_pricing_evidence(
        candidate,
        catalog=StrategyPricingEvidenceCatalog(pd.DataFrame([missing_age]), ()),
        pricing_mode="active",
        per_contract_fee=0.65,
        allow_offline_replay=False,
    ).candidates.iloc[0]
    assert rejected_missing_age["pricing_status"] == "Unavailable"
    assert rejected_missing_age["pricing_source"] == "UNAVAILABLE"

    offline_bsgp = dict(exact)
    offline_bsgp["evidence_lane"] = "OFFLINE_SCHWAB_BOOTSTRAP"
    rejected_offline = attach_strategy_pricing_evidence(
        candidate,
        catalog=StrategyPricingEvidenceCatalog(pd.DataFrame([offline_bsgp]), ()),
        pricing_mode="active",
        per_contract_fee=0.65,
        allow_offline_replay=False,
    ).candidates.iloc[0]
    assert rejected_offline["pricing_status"] == "Delayed"
    assert rejected_offline["pricing_source"] == "UNAVAILABLE"


def test_long_short_multileg_edge_and_conservative_joint_uncertainty() -> None:
    long_leg = json.loads(_candidate().iloc[0]["legs_json"])[0]
    long_leg["quantity"] = 2
    short_leg = {
        **long_leg,
        "contract_symbol": "NVDA  260821C00105000",
        "side": "SHORT",
        "quantity": 1,
        "strike": 105.0,
        "bid": 2.0,
        "ask": 2.2,
    }
    stock_leg = {
        "asset": "STOCK",
        "contract_symbol": "NVDA",
        "side": "LONG",
        "quantity": 100,
        "bid": 99.9,
        "ask": 100.1,
        "multiplier": 1.0,
    }
    candidate = _candidate()
    candidate.loc[0, "legs_json"] = json.dumps(
        [long_leg, short_leg, stock_leg], sort_keys=True, separators=(",", ":")
    )
    predictions = pd.DataFrame(
        [
            _canonical_prediction(
                contract_symbol=str(long_leg["contract_symbol"]),
                fair_value=3.0,
                lower=2.5,
                upper=3.5,
                standard_deviation=0.2,
            ),
            _canonical_prediction(
                contract_symbol=str(short_leg["contract_symbol"]),
                strike=105.0,
                fair_value=1.5,
                lower=1.0,
                upper=2.0,
                standard_deviation=0.3,
            ),
        ]
    )
    row = attach_strategy_pricing_evidence(
        candidate,
        catalog=StrategyPricingEvidenceCatalog(predictions, ()),
        pricing_mode="active",
        per_contract_fee=0.65,
        allow_offline_replay=False,
    ).candidates.iloc[0]

    # Long: 2*100*(3.0-2.1)=180. Short: 1*100*(2.0-1.5)=50.
    assert row["pricing_candidate_edge"] == pytest.approx(230.0)
    # Conservative long is 80; conservative short is zero.
    assert row["pricing_conservative_edge"] == pytest.approx(80.0)
    # Conservative L1 interval aggregation: 2*100*.2 + 1*100*.3.
    assert row["pricing_uncertainty"] == pytest.approx(70.0)
    assert row["pricing_leg_coverage"] == 1.0


def test_pricing_evidence_changes_profit_probability_and_candidate_rank() -> None:
    first = _candidate().iloc[0].to_dict()
    second = _candidate().iloc[0].to_dict()
    first["candidate_key"] = "candidate-a"
    second["candidate_key"] = "candidate-b"
    first_leg = json.loads(str(first["legs_json"]))[0]
    second_leg = dict(first_leg)
    second_leg["contract_symbol"] = "NVDA  260821C00105000"
    second_leg["strike"] = 105.0
    second["legs_json"] = json.dumps([second_leg])
    candidates = pd.DataFrame([first, second])
    for column, value in {
        "underlying_price": 100.0,
        "net_delta": 0.0,
        "net_gamma": 0.0,
        "net_theta": 0.0,
        "max_loss": 1_000.0,
        "max_profit": 1_000.0,
        "capital_required": 1_000.0,
    }.items():
        candidates[column] = value

    def score(fair_a: float, fair_b: float) -> pd.DataFrame:
        catalog = StrategyPricingEvidenceCatalog(
            pd.DataFrame(
                [
                    _canonical_prediction(
                        contract_symbol=str(first_leg["contract_symbol"]),
                        fair_value=fair_a,
                        lower=fair_a - 0.2,
                        upper=fair_a + 0.2,
                    ),
                    _canonical_prediction(
                        contract_symbol=str(second_leg["contract_symbol"]),
                        strike=105.0,
                        fair_value=fair_b,
                        lower=fair_b - 0.2,
                        upper=fair_b + 0.2,
                    ),
                ]
            ),
            (),
        )
        enriched = attach_strategy_pricing_evidence(
            candidates,
            catalog=catalog,
            pricing_mode="active",
            per_contract_fee=0.65,
            allow_offline_replay=False,
        ).candidates
        return score_market_state_prior(
            enriched,
            state=MarketState(0.5, 0.01, 0.2, 1.0, None, None, 1.0),
            policy=StrategySelectionPolicy(
                minimum_train_decisions=1,
                calibration_decisions=1,
                assessment_decisions=1,
            ),
        )

    a_favored = score(3.0, 1.5)
    b_favored = score(1.5, 3.0)
    rank_a_first = dict(
        zip(a_favored["candidate_key"], a_favored["candidate_rank"])
    )
    rank_b_first = dict(
        zip(b_favored["candidate_key"], b_favored["candidate_rank"])
    )
    score_a_first = dict(
        zip(a_favored["candidate_key"], a_favored["decision_score"])
    )
    score_b_first = dict(
        zip(b_favored["candidate_key"], b_favored["decision_score"])
    )
    assert rank_a_first == {"candidate-a": 1, "candidate-b": 2}
    assert rank_b_first == {"candidate-b": 1, "candidate-a": 2}
    assert score_a_first["candidate-a"] > score_b_first["candidate-a"]
    assert score_b_first["candidate-b"] > score_a_first["candidate-b"]
    assert a_favored["decision_score"].between(0.0, 1.0).all()
    print(
        json.dumps(
            {
                "a_favored_ranks": rank_a_first,
                "a_favored_scores": score_a_first,
                "b_favored_ranks": rank_b_first,
                "b_favored_scores": score_b_first,
                "fixture": "controlled-pricing-evidence-rerank",
            },
            sort_keys=True,
        )
    )


def test_republishing_surface_preserves_market_first_availability() -> None:
    prediction = pd.DataFrame([_publishable_prediction_row()])
    surface = build_pricing_surfaces(
        prediction,
        pd.DataFrame(),
        available_at="2026-07-06T18:00:00Z",
    )
    expected = pd.Timestamp("2026-07-06T14:01:01Z")
    assert surface["available_at"].eq(expected).all()
    assert surface["first_available_at"].eq(expected).all()


def _canonical_prediction(
    *,
    contract_symbol: str,
    fair_value: float,
    strike: float = 100.0,
    lower: float | None = None,
    upper: float | None = None,
    standard_deviation: float = 0.10,
) -> dict[str, object]:
    return {
        "symbol": "NVDA",
        "target_snapshot_for": pd.Timestamp("2026-07-06T14:00:00Z"),
        "source_snapshot_for": pd.Timestamp("2026-07-06T13:45:00Z"),
        "expiration_date": pd.Timestamp("2026-08-21T00:00:00Z"),
        "call_put": "CALL",
        "contract_symbol": contract_symbol,
        "strike": strike,
        "multiplier": 100.0,
        "prediction_created_at": pd.Timestamp("2026-07-06T14:01:00Z"),
        "prediction_available_at": pd.Timestamp("2026-07-06T14:01:01Z"),
        "model_published_at": pd.Timestamp("2026-07-06T13:50:00Z"),
        "underlying_price": 100.0,
        "fair_value": fair_value,
        "fair_value_95_lower": fair_value - 0.25 if lower is None else lower,
        "fair_value_95_upper": fair_value + 0.25 if upper is None else upper,
        "predictive_standard_deviation": standard_deviation,
        "residual_shrinkage": 0.8,
        "pricing_source": "BSGP",
        "input_staleness_seconds": 900.0,
        "evidence_lane": "LIVE",
    }


def _publishable_prediction_row() -> dict[str, object]:
    row = _canonical_prediction(
        contract_symbol="NVDA  260821C00100000", fair_value=2.30
    )
    return {
        **row,
        "source_provider": "schwab",
        "prediction_mode": "LIVE",
        "target_years_to_expiration": 46 / 365,
        "prediction_status": "CREATED",
        "raw_bound_violation": False,
        "raw_monotonicity_violation": False,
        "raw_convexity_violation": False,
        "constrained_bound_violation": False,
        "constrained_monotonicity_violation": False,
        "constrained_convexity_violation": False,
        "automated_action_allowed": False,
    }
