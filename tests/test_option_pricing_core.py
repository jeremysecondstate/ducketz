from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from datafetching.decision_time import DecisionClock
from ml.option_pricing.black_scholes import (
    american_option_bounds,
    black_scholes_price,
    implied_volatility,
    target_years_to_expiration,
)
from ml.option_pricing.causal import (
    build_live_prediction_inputs,
    build_causal_samples,
    canonicalize_predictions,
    interpolate_lagged_iv_surface,
    model_feature_frame,
    reconcile_predictions,
)
from ml.option_pricing.constraints import (
    project_surface_values,
    shape_violations,
)
from ml.option_pricing.model import (
    fit_or_reuse_pricing_model,
    partition_pricing_samples,
    route_partitions,
    snapshot_weights,
)
from ml.option_pricing.policies import (
    BSGPModelPolicy,
    PricingPartitionConfig,
    SEMANTIC_FEATURE_COLUMNS,
)
from ml.option_pricing.prediction import create_prediction_rows
from options.publication import CommittedOptionSnapshot


def test_dividend_adjusted_black_scholes_reference_values() -> None:
    call = black_scholes_price(100.0, 100.0, 0.05, 0.20, 1.0, 0.02, "CALL")
    put = black_scholes_price(100.0, 100.0, 0.05, 0.20, 1.0, 0.02, "PUT")

    assert call == pytest.approx(9.227005508154036, rel=1e-12)
    assert put == pytest.approx(6.330080627549918, rel=1e-12)
    assert call - put == pytest.approx(
        100.0 * np.exp(-0.02) - 100.0 * np.exp(-0.05)
    )


def test_black_scholes_limits_invalid_inputs_and_numerical_tails() -> None:
    assert black_scholes_price(110.0, 100.0, 0.03, 0.2, 0.0, 0.01, "CALL") == 10.0
    deterministic = black_scholes_price(100.0, 95.0, 0.04, 0.0, 0.5, 0.01, "CALL")
    assert deterministic == pytest.approx(
        max(100.0 * np.exp(-0.01 * 0.5) - 95.0 * np.exp(-0.04 * 0.5), 0.0)
    )
    deep_tail = black_scholes_price(1_000.0, 1.0, 0.01, 0.05, 30.0, 0.0, "CALL")
    assert np.isfinite(deep_tail)
    assert 0.0 <= deep_tail <= 1_000.0
    with pytest.raises(ValueError, match="finite"):
        black_scholes_price(np.nan, 100.0, 0.01, 0.2, 1.0, 0.0, "CALL")
    with pytest.raises(ValueError, match="positive"):
        black_scholes_price(0.0, 100.0, 0.01, 0.2, 1.0, 0.0, "CALL")


def test_live_target_requires_option_receipt_visible_before_prediction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = pd.Timestamp("2026-07-06T14:00:00Z")
    future_receipt = CommittedOptionSnapshot(
        symbol="NVDA",
        snapshot_for=target,
        available_at=pd.Timestamp("2026-07-06T14:02:00Z"),
        directory=tmp_path / "snapshot",
        raw_path=tmp_path / "raw.parquet",
        contracts_path=tmp_path / "contracts.parquet",
        features_path=tmp_path / "features.parquet",
        receipt_path=tmp_path / "receipt.json",
        receipt={},
    )
    monkeypatch.setattr(
        "ml.option_pricing.causal.latest_completed_bar_clock",
        lambda *_args, **_kwargs: DecisionClock(
            decision_timestamp=target,
            bar_timestamp=target - pd.Timedelta(minutes=1),
            provider="databento",
            timeframe="1m",
            source_file=tmp_path / "bars.parquet",
        ),
    )
    monkeypatch.setattr(
        "ml.option_pricing.causal.committed_option_snapshots",
        lambda *_args, **_kwargs: (future_receipt,),
    )

    batch = build_live_prediction_inputs(
        tmp_path,
        symbol="NVDA",
        prediction_created_at="2026-07-06T14:01:00Z",
    )

    assert batch.status == "SOURCE_SURFACE_UNAVAILABLE"
    assert batch.target_snapshot_for == target


def test_implied_volatility_round_trip_and_impossible_price_rejection() -> None:
    price = black_scholes_price(101.0, 100.0, 0.03, 0.37, 0.4, 0.01, "PUT")
    solved = implied_volatility(price, 101.0, 100.0, 0.03, 0.4, 0.01, "PUT")
    assert solved == pytest.approx(0.37, abs=1e-8)
    with pytest.raises(ValueError, match="outside"):
        implied_volatility(500.0, 101.0, 100.0, 0.03, 0.4, 0.01, "CALL")


def test_expiration_clock_uses_new_york_dst_and_act_365() -> None:
    years = target_years_to_expiration(
        "2026-07-01T16:00:00Z",
        "2026-07-02",
    )
    assert years == pytest.approx(28.0 / (365.0 * 24.0))


def test_american_bounds_and_weighted_call_put_shape_projection() -> None:
    call_lower, call_upper = american_option_bounds(
        100.0, 95.0, 0.04, 0.25, 0.5, 0.01, "CALL"
    )
    put_lower, put_upper = american_option_bounds(
        100.0, 105.0, 0.04, 0.25, 0.5, 0.01, "PUT"
    )
    assert call_lower >= 5.0
    assert call_upper == 100.0
    assert put_lower >= 5.0
    assert put_upper == 105.0

    strikes = np.array([90.0, 100.0, 110.0, 120.0])
    calls = np.array([14.0, 15.0, 4.0, 5.0])
    projection = project_surface_values(
        strikes,
        calls,
        np.zeros(4),
        np.full(4, 100.0),
        "CALL",
        weights=np.array([1.0, 2.0, 1.0, 1.0]),
    )
    assert projection.raw_violations.monotonicity.any()
    assert not projection.constrained_violations.any.any()
    assert np.diff(projection.constrained).max() <= 1e-7
    assert np.diff(np.diff(projection.constrained) / np.diff(strikes)).min() >= -1e-7

    puts = project_surface_values(
        strikes,
        np.array([2.0, 1.0, 14.0, 13.0]),
        np.zeros(4),
        strikes,
        "PUT",
    )
    assert np.diff(puts.constrained).min() >= -1e-7
    assert not shape_violations(
        strikes,
        puts.constrained,
        np.zeros(4),
        strikes,
        "PUT",
    ).any.any()


def test_causal_samples_use_strictly_earlier_surface_and_exclude_target_fields() -> None:
    source = _source_surface()
    target = source.copy()
    target["bid"] = target["bid"] + 0.25
    target["ask"] = target["ask"] + 0.25
    target["implied_volatility"] = 4.99
    target["delta"] = 999.0
    target["quote_timestamp"] = pd.Timestamp("2026-01-03T16:02:00Z")

    samples = build_causal_samples(
        source,
        target_contracts=target,
        target_underlying_price=100.0,
        source_snapshot_for=pd.Timestamp("2026-01-02T16:00:00Z"),
        source_available_at=pd.Timestamp("2026-01-02T16:01:00Z"),
        target_snapshot_for=pd.Timestamp("2026-01-03T16:00:00Z"),
        source_provider="schwab",
        prediction_mode="OFFLINE",
        observed_available_at=pd.Timestamp("2026-01-03T16:03:00Z"),
    )

    assert samples["sample_status"].eq("AVAILABLE").all()
    assert samples["source_snapshot_for"].lt(samples["target_snapshot_for"]).all()
    assert samples["lagged_implied_volatility"].max() < 1.0
    assert tuple(model_feature_frame(samples).columns) == SEMANTIC_FEATURE_COLUMNS
    assert not {"observed_mid", "delta", "implied_volatility"}.intersection(
        model_feature_frame(samples).columns
    )


def test_iv_surface_interpolation_does_not_look_forward_or_extrapolate() -> None:
    source = _source_surface().copy()
    source["_resolved_iv"] = source["implied_volatility"]
    targets = pd.DataFrame(
        {
            "strike": [100.0, 140.0],
            "underlying_price": [100.0, 100.0],
            "expiration_date": [
                pd.Timestamp("2026-02-20T00:00:00Z"),
                pd.Timestamp("2026-02-20T00:00:00Z"),
            ],
        }
    )
    result = interpolate_lagged_iv_surface(
        source,
        targets,
        target_snapshot_for=pd.Timestamp("2026-01-03T16:00:00Z"),
    )
    assert result.iloc[0] == pytest.approx(0.28)
    assert np.isnan(result.iloc[1])


def test_snapshot_partitions_keep_clusters_intact_weight_rows_and_redact_lockbox() -> None:
    config = PricingPartitionConfig(3, 2, 2, 2, 0)
    samples = _partition_samples(cluster_count=12)
    samples["normalized_residual"] = samples["normalized_residual"].astype(object)
    lockbox_starts = sorted(samples["target_snapshot_for"].unique())[-2:]
    samples.loc[
        samples["target_snapshot_for"].isin(lockbox_starts),
        "normalized_residual",
    ] = "CLOSED_TARGET"

    partitions = partition_pricing_samples(samples, config=config)

    assert partitions.train_clusters == 6
    assert partitions.calibration_clusters == 2
    assert partitions.assessment_clusters == 2
    assert partitions.lockbox_clusters == 2
    assert "CLOSED_TARGET" not in set(partitions.train["normalized_residual"])
    assert "CLOSED_TARGET" not in set(partitions.calibration["normalized_residual"])
    assert "CLOSED_TARGET" not in set(partitions.assessment["normalized_residual"])
    for frame in (partitions.train, partitions.calibration, partitions.assessment):
        sums = pd.Series(snapshot_weights(frame), index=frame.index).groupby(
            frame["target_snapshot_for"]
        ).sum()
        assert np.allclose(sums, 1.0)


def test_bsgp_is_deterministic_compares_four_models_and_reuses_only_compatible_artifact(
    tmp_path: Path,
) -> None:
    config = PricingPartitionConfig(20, 8, 8, 4, 0)
    all_partitions = partition_pricing_samples(
        _synthetic_residual_samples(44),
        config=config,
    )
    partitions = route_partitions(
        all_partitions,
        symbol="NVDA",
        call_put="CALL",
        config=config,
    )
    policy = BSGPModelPolicy(component_count=16, gamma_grid=(0.3, 1.0), random_state=19)
    first = fit_or_reuse_pricing_model(
        tmp_path,
        symbol="NVDA",
        call_put="CALL",
        partitions=partitions,
        input_files=(),
        trained_at=pd.Timestamp("2026-07-01T12:00:00Z"),
        model_policy=policy,
        partition_config=config,
    )
    second = fit_or_reuse_pricing_model(
        tmp_path,
        symbol="NVDA",
        call_put="CALL",
        partitions=partitions,
        input_files=(),
        trained_at=pd.Timestamp("2026-07-02T12:00:00Z"),
        model_policy=policy,
        partition_config=config,
    )

    assert first.reused is False
    assert second.reused is True
    assert set(first.offline_evaluation["models"]) == {
        "bsgp",
        "black_scholes",
        "constant_residual",
        "standard_gp",
    }
    assert first.offline_evaluation["models"]["bsgp"]["normalized_rmse"] < (
        first.offline_evaluation["models"]["black_scholes"]["normalized_rmse"]
    )
    first_mean = first.predict_residual(partitions.assessment)[0]
    second_mean = second.predict_residual(partitions.assessment)[0]
    assert np.allclose(first_mean, second_mean)

    incompatible = fit_or_reuse_pricing_model(
        tmp_path,
        symbol="NVDA",
        call_put="CALL",
        partitions=partitions,
        input_files=(),
        trained_at=pd.Timestamp("2026-07-03T12:00:00Z"),
        model_policy=BSGPModelPolicy(
            component_count=12,
            gamma_grid=(0.3, 1.0),
            random_state=19,
        ),
        partition_config=config,
    )
    assert incompatible.reused is False


def test_baseline_prediction_publishes_raw_and_constrained_values_without_uncertainty() -> None:
    samples = build_causal_samples(
        _source_surface().iloc[:5],
        target_contracts=None,
        target_underlying_price=100.0,
        source_snapshot_for=pd.Timestamp("2026-01-02T16:00:00Z"),
        source_available_at=pd.Timestamp("2026-01-02T16:01:00Z"),
        target_snapshot_for=pd.Timestamp("2026-01-03T16:00:00Z"),
        source_provider="schwab",
        prediction_mode="LIVE",
    )
    predictions = create_prediction_rows(
        samples,
        prediction_created_at=pd.Timestamp("2026-01-03T16:01:00Z"),
        prediction_available_at=pd.Timestamp("2026-01-03T16:01:05Z"),
    )

    assert predictions["model_status"].eq("BASELINE_ONLY").all()
    assert predictions["prediction_status"].eq("CREATED").all()
    assert predictions["constrained_fair_value"].notna().all()
    assert predictions["predictive_standard_deviation"].isna().all()
    assert predictions["automated_action_allowed"].eq(False).all()


def test_reconciliation_requires_exact_later_quote_and_canonicalizes_duplicates(
    tmp_path: Path,
) -> None:
    samples = build_causal_samples(
        _source_surface().iloc[:5],
        target_contracts=None,
        target_underlying_price=100.0,
        source_snapshot_for="2026-01-02T16:00:00Z",
        source_available_at="2026-01-02T16:01:00Z",
        target_snapshot_for="2026-01-03T16:00:00Z",
        source_provider="schwab",
        prediction_mode="LIVE",
    )
    prediction = create_prediction_rows(
        samples.iloc[:1],
        prediction_created_at="2026-01-03T16:01:00Z",
        prediction_available_at="2026-01-03T16:01:05Z",
    )
    later = prediction.copy()
    later["prediction_created_at"] = pd.Timestamp("2026-01-03T16:01:10Z")
    later["prediction_available_at"] = pd.Timestamp("2026-01-03T16:01:15Z")
    canonical = canonicalize_predictions(pd.concat([later, prediction], ignore_index=True))
    assert len(canonical) == 1
    assert canonical.iloc[0]["prediction_created_at"] == pd.Timestamp(
        "2026-01-03T16:01:00Z"
    )
    canonical["_pricing_outcome_run_path"] = "ml/option-pricing-target-outcomes/proven"
    canonical["_pricing_outcome_receipt_checksum_sha256"] = "receipt-checksum"
    canonical["_pricing_authority_published_at"] = pd.Timestamp(
        "2026-01-03T16:01:05Z"
    )

    target = _source_surface().iloc[:1].copy()
    target["bid"] = 2.0
    target["ask"] = 2.2
    target["quote_timestamp"] = pd.Timestamp("2026-01-03T16:01:06Z")
    contracts_path = tmp_path / "contracts.parquet"
    target.to_parquet(contracts_path, index=False)
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    request_started_at = "2026-01-03T16:01:05.500000Z"
    barrier = {
        "status": "VERIFIED",
        "target_snapshot_for": "2026-01-03T16:00:00Z",
        "observed_at": "2026-01-03T16:01:05.250000Z",
        "pricing_published_at": "2026-01-03T16:01:05Z",
        "pricing_run_path": "ml/option-pricing-target-outcomes/proven",
        "pricing_receipt_checksum_sha256": "receipt-checksum",
        "prospective_credit_allowed": True,
    }
    snapshot = CommittedOptionSnapshot(
        symbol="NVDA",
        snapshot_for=pd.Timestamp("2026-01-03T16:00:00Z"),
        available_at=pd.Timestamp("2026-01-03T16:01:07Z"),
        directory=tmp_path,
        raw_path=contracts_path,
        contracts_path=contracts_path,
        features_path=contracts_path,
        receipt_path=receipt,
        receipt={
            "request_started_at": request_started_at,
            "pricing_barrier": barrier,
        },
    )
    evaluated = reconcile_predictions(
        canonical,
        snapshots_by_symbol={"NVDA": (snapshot,)},
        evaluated_at="2026-01-03T16:02:00Z",
    )
    assert evaluated.iloc[0]["evaluation_status"] == "COMPLETE"
    assert bool(evaluated.iloc[0]["prospective_eligible"]) is True

    target["non_standard"] = True
    target.to_parquet(contracts_path, index=False)
    nonstandard = reconcile_predictions(
        canonical,
        snapshots_by_symbol={"NVDA": (snapshot,)},
        evaluated_at="2026-01-03T16:02:00Z",
    )
    assert nonstandard.iloc[0]["evaluation_status"] == "TARGET_CONTRACT_MISMATCH"
    assert bool(nonstandard.iloc[0]["prospective_eligible"]) is False
    target["non_standard"] = False
    target.to_parquet(contracts_path, index=False)

    earlier_snapshot = CommittedOptionSnapshot(
        symbol="NVDA",
        snapshot_for=snapshot.snapshot_for,
        available_at=pd.Timestamp("2026-01-03T16:00:59Z"),
        directory=tmp_path / "earlier-natural-receipt",
        raw_path=contracts_path,
        contracts_path=contracts_path,
        features_path=contracts_path,
        receipt_path=receipt,
        receipt={},
    )
    already_observed = reconcile_predictions(
        canonical,
        snapshots_by_symbol={"NVDA": (snapshot, earlier_snapshot)},
        evaluated_at="2026-01-03T16:02:00Z",
    )
    assert (
        already_observed.iloc[0]["evaluation_status"]
        == "TARGET_ALREADY_OBSERVED_BEFORE_PREDICTION"
    )
    assert bool(already_observed.iloc[0]["prospective_eligible"]) is False

    offline_provider = canonical.copy()
    offline_provider["source_provider"] = "databento-opra"
    ineligible = reconcile_predictions(
        offline_provider,
        snapshots_by_symbol={"NVDA": (snapshot,)},
        evaluated_at="2026-01-03T16:02:00Z",
    )
    assert ineligible.iloc[0]["evaluation_status"] == "COMPLETE"
    assert bool(ineligible.iloc[0]["prospective_eligible"]) is False

    target["quote_timestamp"] = pd.Timestamp("2026-01-03T16:01:00Z")
    target.to_parquet(contracts_path, index=False)
    stale = reconcile_predictions(
        canonical,
        snapshots_by_symbol={"NVDA": (snapshot,)},
        evaluated_at="2026-01-03T16:02:00Z",
    )
    assert stale.iloc[0]["evaluation_status"] == "STALE_PRE_PREDICTION_QUOTE"
    assert bool(stale.iloc[0]["prospective_eligible"]) is False

    target["contract_symbol"] = "DIFFERENT-CONTRACT"
    target.to_parquet(contracts_path, index=False)
    missing = reconcile_predictions(
        canonical,
        snapshots_by_symbol={"NVDA": (snapshot,)},
        evaluated_at="2026-01-03T16:02:00Z",
    )
    assert missing.iloc[0]["evaluation_status"] == "MISSING_TARGET_CONTRACT"
    assert bool(missing.iloc[0]["prospective_eligible"]) is False


def _source_surface() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    expirations = (
        pd.Timestamp("2026-02-20T00:00:00Z"),
        pd.Timestamp("2026-03-20T00:00:00Z"),
    )
    for expiration_index, expiration in enumerate(expirations):
        for strike in (90.0, 95.0, 100.0, 105.0, 110.0):
            call_put = "CALL"
            iv = 0.28 + expiration_index * 0.04 + abs(strike - 100.0) * 0.001
            years = target_years_to_expiration(
                pd.Timestamp("2026-01-02T16:00:00Z"), expiration
            )
            mid = black_scholes_price(100.0, strike, 0.04, iv, years, 0.01, call_put)
            rows.append(
                {
                    "symbol": "NVDA",
                    "contract_symbol": f"NVDA-{expiration.date()}-C-{strike:g}",
                    "call_put": call_put,
                    "expiration_date": expiration,
                    "strike": strike,
                    "underlying_price": 100.0,
                    "bid": max(mid - 0.05, 0.01),
                    "ask": mid + 0.05,
                    "multiplier": 100.0,
                    "mini": False,
                    "non_standard": False,
                    "interest_rate": 0.04,
                    "dividend_yield": 0.01,
                    "implied_volatility": iv,
                    "quote_staleness_seconds": 120.0,
                    "quote_timestamp": pd.Timestamp("2026-01-02T15:59:00Z"),
                    "delta": 0.5,
                }
            )
    return pd.DataFrame(rows)


def _partition_samples(cluster_count: int) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(cluster_count):
        target = pd.Timestamp("2025-01-02T16:00:00Z") + pd.Timedelta(days=index)
        row_count = 1 if index % 2 == 0 else 3
        for contract in range(row_count):
            rows.append(
                {
                    "symbol": "NVDA",
                    "call_put": "CALL",
                    "contract_symbol": f"C{index}-{contract}",
                    "target_snapshot_for": target,
                    "source_snapshot_for": target - pd.Timedelta(minutes=15),
                    "observed_available_at": target + pd.Timedelta(minutes=2),
                    "sample_status": "AVAILABLE",
                    "normalized_residual": 0.01,
                    "observed_mid": 2.0,
                    "black_scholes_price": 1.0,
                    "underlying_price": 100.0,
                    "strike": 100.0,
                    "risk_free_rate": 0.04,
                    "lagged_implied_volatility": 0.30,
                    "target_years_to_expiration": 0.2,
                    "dividend_yield": 0.01,
                    "bid_ask_spread": 0.2,
                }
            )
    return pd.DataFrame(rows)


def _synthetic_residual_samples(cluster_count: int) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(cluster_count):
        target = pd.Timestamp("2025-01-02T16:00:00Z") + pd.Timedelta(days=index)
        underlying = 90.0 + index * 0.5
        for strike_ratio in (0.90, 0.95, 1.0, 1.05, 1.10):
            strike = underlying * strike_ratio
            years = 60.0 / 365.0
            bs = black_scholes_price(
                underlying,
                strike,
                0.04,
                0.28,
                years,
                0.01,
                "CALL",
            )
            log_moneyness = np.log(strike_ratio)
            residual = 0.006 + 0.025 * np.exp(-80.0 * log_moneyness**2)
            observed = bs + underlying * residual
            rows.append(
                {
                    "symbol": "NVDA",
                    "source_provider": "fixture",
                    "prediction_mode": "OFFLINE",
                    "call_put": "CALL",
                    "contract_symbol": f"NVDA-{index}-{strike_ratio}",
                    "expiration_date": target + pd.Timedelta(days=60),
                    "target_snapshot_for": target,
                    "source_snapshot_for": target - pd.Timedelta(minutes=15),
                    "source_available_at": target - pd.Timedelta(minutes=14),
                    "observed_available_at": target + pd.Timedelta(minutes=2),
                    "underlying_price": underlying,
                    "strike": strike,
                    "risk_free_rate": 0.04,
                    "lagged_implied_volatility": 0.28,
                    "target_years_to_expiration": years,
                    "dividend_yield": 0.01,
                    "observed_bid": observed - 0.05,
                    "observed_ask": observed + 0.05,
                    "observed_mid": observed,
                    "bid_ask_spread": 0.10,
                    "black_scholes_price": bs,
                    "normalized_residual": residual,
                    "sample_status": "AVAILABLE",
                }
            )
    return pd.DataFrame(rows)
