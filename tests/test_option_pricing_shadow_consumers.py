from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.services.schwab_strategy_orders import (
    SchwabPositionContext,
    build_strategy_order_draft,
)
from ml.artifacts import write_manifest
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import DEFAULT_FEATURE_PROFILE, horizon_specifications_for_profile
from ml.option_pricing.consumers import read_verified_compact_pricing_features
from ml.option_pricing.publication import (
    OPTION_PRICING_PUBLICATION_VERSION,
    publish_option_pricing_run,
)
from ml.option_pricing.strategy_shadow import attach_strategy_pricing_shadow
from ml.parquet_contracts import (
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    empty_frame,
    frame_with_readable_id,
    write_parquet_with_schema,
)


def _publish_pricing(root: Path) -> Path:
    run = root / "ml" / "option-pricing-runs" / "20260706T140100.000000Z"
    run.mkdir(parents=True)
    prediction = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "source_provider": "schwab",
                "prediction_mode": "LIVE",
                "call_put": "CALL",
                "contract_symbol": "NVDA  260821C00100000",
                "expiration_date": pd.Timestamp("2026-08-21T00:00:00Z"),
                "target_snapshot_for": pd.Timestamp("2026-07-06T14:00:00Z"),
                "source_snapshot_for": pd.Timestamp("2026-07-06T13:45:00Z"),
                "source_available_at": pd.Timestamp("2026-07-06T13:46:00Z"),
                "prediction_created_at": pd.Timestamp("2026-07-06T14:01:00Z"),
                "prediction_available_at": pd.Timestamp("2026-07-06T14:01:01Z"),
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
                "pricing_policy_version": "black-scholes-rbf-residual-v1",
                "timing_policy_version": "pre-quote-quarter-hour-v1",
                "schema_version": "option-pricing-shadow-v1",
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
                "symbol": "NVDA",
                "target_snapshot_for": pd.Timestamp("2026-07-06T14:00:00Z"),
                "available_at": pd.Timestamp("2026-07-06T14:01:01Z"),
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
                "pricing_policy_version": "black-scholes-rbf-residual-v1",
                "schema_version": "option-pricing-compact-surface-v1",
                "automated_action_allowed": False,
            }
        ]
    )
    surface = frame_with_readable_id(
        surface,
        key_columns=("symbol", "target_snapshot_for", "call_put", "expiration_bucket", "moneyness_bucket"),
    )
    outputs = {
        "pricing-samples.parquet": (empty_frame(OPTION_PRICING_SAMPLE_SCHEMA), OPTION_PRICING_SAMPLE_SCHEMA),
        "pricing-predictions.parquet": (prediction, OPTION_PRICING_PREDICTION_SCHEMA),
        "pricing-evaluations.parquet": (empty_frame(OPTION_PRICING_EVALUATION_SCHEMA), OPTION_PRICING_EVALUATION_SCHEMA),
        "pricing-surfaces.parquet": (surface, OPTION_PRICING_SURFACE_SCHEMA),
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
                "version": OPTION_PRICING_PUBLICATION_VERSION,
                "authority": "ml/option-pricing-latest/run.json",
                "schema_validation": True,
                "automated_action_allowed": False,
            }
        },
        datastore_root=root,
    )
    publish_option_pricing_run(
        root,
        run_directory=run,
        published_at="2026-07-06T14:01:01Z",
    )
    return run


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
    assert off.iloc[0]["pricing_status"] == "OFF"
    assert shadow.iloc[0]["pricing_status"] == "COVERED"
    assert shadow.iloc[0]["pricing_candidate_edge"] == pytest.approx(20.0)
    assert shadow.iloc[0]["pricing_edge_to_friction"] == pytest.approx(20.0 / 20.65)
    assert shadow.iloc[0]["pricing_uncertainty"] == pytest.approx(25.0)


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
    assert missing.iloc[0]["pricing_status"] == "EVIDENCE_UNAVAILABLE"
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
    assert tampered.iloc[0]["pricing_status"] == "EVIDENCE_UNAVAILABLE"
    assert tampered.iloc[0]["candidate_rank"] == 1
