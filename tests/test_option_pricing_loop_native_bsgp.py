from __future__ import annotations

import inspect
import json
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from datafetching.bar_readiness import publish_bar_readiness
from datafetching.bar_schema import write_normalized_bar_parquet
from ml.artifacts import file_checksum, semantic_metadata_fingerprint
from ml.option_pricing.black_scholes import black_scholes_price
from ml.option_pricing.causal import build_causal_samples, model_feature_frame
from ml.option_pricing.causal import select_strictly_earlier_snapshot
from ml.option_pricing.loop_native_eligibility import (
    LoopNativeEligibilityError,
    LoopNativeEligibilityPolicy,
    build_loop_native_eligibility_report,
    loop_native_eligibility_policy_payload,
    publish_loop_native_eligibility_policy,
    publish_loop_native_eligibility_report,
    read_current_loop_native_eligibility_policy,
    read_current_loop_native_eligibility_report,
    read_loop_native_eligibility_policy,
    verify_loop_native_capture_lineage,
)
from ml.option_pricing.policies import (
    LOOP_NATIVE_CALL_PUTS,
    LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
    LOOP_NATIVE_SYMBOLS,
    LoopNativeModelPolicy,
    SEMANTIC_FEATURE_COLUMNS,
)
from ml.option_pricing.prediction import (
    create_bsgp_shadow_rows,
    create_prediction_rows,
)
from ml.option_pricing.schwab_materialization import (
    OFFLINE_SCHWAB_BOOTSTRAP,
    SCHWAB_MATERIALIZATION_SCHEMA_VERSION,
    SchwabMaterializationError,
    _carry_input_kind,
    _attach_underlying_bar_proofs,
    _publish_materialization,
    _receipt_proven_live_source_samples,
    _route_report,
    _verified_manifest_input_paths,
    collapse_schwab_publications,
    materialize_loop_native_schwab_history,
    read_current_loop_native_schwab_materialization,
    read_loop_native_schwab_materialization,
)
from ml.option_pricing.strategy_shadow import load_strategy_pricing_evidence
from ml.option_pricing.shadow_model import (
    LoopNativeModelError,
    LoopNativeModelLoad,
    load_prior_loop_native_model,
    partition_loop_native_samples,
    predict_loop_native_residuals,
    surface_weights,
    train_loop_native_shadow_generation,
)
from ml.option_pricing.target_outcome import (
    TARGET_OUTCOME_RECEIPT_V3,
    TargetOutcomeError,
    publish_target_outcome,
    read_current_target_outcome,
    read_target_outcome,
)
from ml.option_pricing_runtime import _run_option_pricing_once_impl
from options.publication import publish_option_snapshot, read_option_snapshot


def test_empty_terminal_shadow_publication_advances_verified_pointer(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-11T16:15:00Z")
    publication = publish_target_outcome(
        tmp_path,
        target_snapshot_for=target,
        created_at=target + pd.Timedelta(minutes=1, seconds=45),
        symbols=("NVDA",),
        symbol_outcomes={
            "NVDA": {
                "status": "TARGET_BAR_NOT_READY",
                "reason": "Exact Loop A readiness did not arrive before timeout",
                "target_snapshot_for": target,
            }
        },
        terminal_status="TARGET_BAR_NOT_READY",
        samples=pd.DataFrame(),
        predictions=pd.DataFrame(),
        shadow_predictions=pd.DataFrame(),
        bar_readiness=None,
        clock=lambda: target + pd.Timedelta(minutes=1, seconds=46),
    )

    current = read_current_target_outcome(tmp_path)
    assert current.directory == publication.directory
    assert current.receipt["schema_version"] == TARGET_OUTCOME_RECEIPT_V3
    assert current.predictions().empty
    assert current.shadow_predictions().empty
    assert current.terminal_status == "TARGET_BAR_NOT_READY"


def test_duplicate_schwab_publications_collapse_to_earliest_valid_receipt(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-07-06T14:00:00Z")
    first = _publish_snapshot(
        tmp_path,
        symbol="NVDA",
        target=target,
        available=target + pd.Timedelta(minutes=2),
    )
    retry = _publish_snapshot(
        tmp_path,
        symbol="NVDA",
        target=target,
        available=target + pd.Timedelta(minutes=3),
    )
    assert retry.directory == first.directory
    second = _install_legacy_duplicate(
        tmp_path,
        symbol="NVDA",
        target=target,
        available=target + pd.Timedelta(minutes=3),
    )

    collapsed = collapse_schwab_publications(
        {"NVDA": (second, first)},
        datastore_root=tmp_path,
        cutoff=target + pd.Timedelta(hours=1),
    )

    assert collapsed.selected[("NVDA", target)].directory == first.directory
    assert collapsed.report["selected_snapshot_count"] == 1
    assert collapsed.report["duplicate_publication_count"] == 1
    consulted = collapsed.report["consulted_receipts"]
    assert [row["selection"] for row in consulted] == [
        "SELECTED_EARLIEST_VALID_RECEIPT",
        "DUPLICATE_LINEAGE_DIAGNOSTIC_ONLY",
    ]


def test_conflicting_duplicate_schwab_receipts_fail_closed(tmp_path: Path) -> None:
    target = pd.Timestamp("2026-07-06T14:00:00Z")
    first = _publish_snapshot(
        tmp_path,
        symbol="NVDA",
        target=target,
        available=target + pd.Timedelta(minutes=2),
    )
    second = _install_legacy_duplicate(
        tmp_path,
        symbol="NVDA",
        target=target,
        available=target + pd.Timedelta(minutes=3),
        strike=101.0,
    )

    with pytest.raises(SchwabMaterializationError, match="Conflicting semantic"):
        collapse_schwab_publications(
            {"NVDA": (first, second)},
            datastore_root=tmp_path,
            cutoff=target + pd.Timedelta(hours=1),
        )


def test_later_source_publication_is_diagnostic_only_and_cutoff_is_reported(
    tmp_path: Path,
) -> None:
    source_target = pd.Timestamp("2026-07-06T14:00:00Z")
    prediction_target = source_target + pd.Timedelta(minutes=15)
    first = _publish_snapshot(
        tmp_path,
        symbol="NVDA",
        target=source_target,
        available=source_target + pd.Timedelta(minutes=2),
    )
    duplicate = _install_legacy_duplicate(
        tmp_path,
        symbol="NVDA",
        target=source_target,
        available=source_target + pd.Timedelta(minutes=3),
    )
    after_cutoff = _publish_snapshot(
        tmp_path,
        symbol="NVDA",
        target=source_target + pd.Timedelta(minutes=1),
        available=source_target + pd.Timedelta(hours=2),
    )

    selected = select_strictly_earlier_snapshot(
        (duplicate, first),
        target_snapshot_for=prediction_target,
        prediction_created_at=prediction_target + pd.Timedelta(minutes=1),
    )
    assert selected is not None
    assert selected.directory == first.directory

    collapsed = collapse_schwab_publications(
        {"NVDA": (duplicate, after_cutoff, first)},
        datastore_root=tmp_path,
        cutoff=source_target + pd.Timedelta(hours=1),
    )
    assert collapsed.report["consulted_receipt_count"] == 3
    assert [
        row["selection"] for row in collapsed.report["consulted_receipts"]
    ] == [
        "SELECTED_EARLIEST_VALID_RECEIPT",
        "DUPLICATE_LINEAGE_DIAGNOSTIC_ONLY",
        "EXCLUDED_RECEIPT_NOT_STRICTLY_BEFORE_TRAINER_CUTOFF",
    ]


def test_offline_bootstrap_rows_never_increment_prospective_counts() -> None:
    samples = _training_samples(session_count=3)
    report = _route_report(samples, symbols=LOOP_NATIVE_SYMBOLS)

    assert all(route["prospective_rows"] == 0 for route in report.values())
    assert all(route["prospective_sessions"] == 0 for route in report.values())
    assert all(route["offline_sessions"] == 3 for route in report.values())


def test_target_snapshot_iv_and_later_receipts_cannot_enter_features() -> None:
    source = _source_contracts()
    target = source.copy()
    target["bid"] += 0.25
    target["ask"] += 0.25
    target["implied_volatility"] = 99.0
    target["quote_timestamp"] = pd.Timestamp("2026-01-06T16:02:00Z")
    samples = build_causal_samples(
        source,
        target_contracts=target,
        target_underlying_price=100.0,
        source_snapshot_for="2026-01-05T16:00:00Z",
        source_available_at="2026-01-05T16:01:00Z",
        target_snapshot_for="2026-01-06T16:00:00Z",
        source_provider="schwab",
        prediction_mode="OFFLINE",
        observed_available_at="2026-01-06T16:03:00Z",
        allow_source_chain_carry_fallback=False,
    )
    original = model_feature_frame(samples)
    samples["target_implied_volatility"] = 0.0001
    samples["later_receipt_secret"] = "LEAK"

    assert tuple(original.columns) == SEMANTIC_FEATURE_COLUMNS
    pd.testing.assert_frame_equal(original, model_feature_frame(samples))
    assert samples["lagged_implied_volatility"].max() < 1.0


def test_underlying_feature_uses_immutable_loop_a_readiness(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-01-09T16:00:00Z")
    bars = (
        tmp_path
        / "stocks"
        / "NVDA"
        / "bars"
        / "1m"
        / "databento"
        / "normalized"
        / "bars.parquet"
    )
    bars.parent.mkdir(parents=True)
    write_normalized_bar_parquet(
        pd.DataFrame(
            {
                "timestamp": [target - pd.Timedelta(minutes=1)],
                "open": [99.0],
                "high": [101.0],
                "low": [98.0],
                "close": [100.0],
                "volume": [1_000.0],
            }
        ),
        bars,
    )
    readiness = publish_bar_readiness(
        tmp_path,
        target_snapshot_for=target,
        symbols=("NVDA",),
        loop_a_generation="loop-a-test",
        as_of=target + pd.Timedelta(seconds=4),
        clock=lambda: target + pd.Timedelta(seconds=5),
    )
    frame = _live_samples()

    proven = _attach_underlying_bar_proofs(
        frame,
        input_paths=readiness.evidence_files,
        root=tmp_path.resolve(),
    )

    assert proven["_underlying_readiness_ready_at"].eq(readiness.ready_at).all()
    assert proven["_underlying_readiness_path"].eq(
        readiness.readiness_path.relative_to(tmp_path).as_posix()
    ).all()
    assert proven["_underlying_bar_timestamp"].eq(
        target - pd.Timedelta(minutes=1)
    ).all()
    assert proven["underlying_price"].eq(100.0).all()


def test_same_cycle_matured_outcome_cannot_train_current_model() -> None:
    samples = _training_samples(session_count=3)
    cutoff = pd.Timestamp(samples["observed_available_at"].max())
    with pytest.raises(LoopNativeModelError, match="strictly predate"):
        partition_loop_native_samples(
            samples,
            trainer_cutoff=cutoff,
            policy=_small_model_policy(),
        )


def test_model_publication_must_follow_immutable_materialization(
    tmp_path: Path,
) -> None:
    materialization = _materialization(tmp_path)
    materialization_published = pd.Timestamp(
        materialization.receipt["published_at"]
    )
    with pytest.raises(LoopNativeModelError, match="strictly follow"):
        train_loop_native_shadow_generation(
            tmp_path,
            materialization=materialization,
            trainer_cutoff="2026-01-09T00:00:00Z",
            published_at=materialization_published,
            policy=_small_model_policy(),
        )
    assert not (
        tmp_path / "ml" / "option-pricing-loop-native-models" / "latest.json"
    ).exists()


def test_offline_assessment_replay_uses_only_causal_bsgp_crossfit(
    tmp_path: Path,
) -> None:
    generation, _policy = _model_generation(tmp_path)

    catalog = load_strategy_pricing_evidence(
        tmp_path,
        available_not_after=generation.published_at + pd.Timedelta(seconds=1),
        include_offline_replay=True,
    )
    replay = catalog.predictions.loc[
        catalog.predictions["evidence_lane"].eq(OFFLINE_SCHWAB_BOOTSTRAP)
    ].copy()
    sessions = (
        pd.to_datetime(replay["target_snapshot_for"], utc=True)
        .dt.tz_convert("America/New_York")
        .dt.strftime("%Y-%m-%d")
    )
    assessment_sessions = set(
        generation.manifest["chronological_session_partitions"]["assessment"]
    )
    assessment = replay.loc[sessions.isin(assessment_sessions)]
    earlier = replay.loc[~sessions.isin(assessment_sessions)]

    assert not assessment.empty
    assert assessment["pricing_source"].eq("BSGP").all()
    assert assessment["pricing_evidence_status"].eq(
        "OFFLINE_CAUSAL_CROSSFIT_BSGP"
    ).all()
    assert earlier["pricing_source"].eq("BLACK_SCHOLES").all()
    assert pd.to_datetime(assessment["prediction_created_at"], utc=True).lt(
        pd.to_datetime(assessment["target_snapshot_for"], utc=True)
        + pd.Timedelta(minutes=1)
    ).all()
    np.testing.assert_allclose(
        assessment["bsgp_shadow_fair_value_raw"].to_numpy(dtype=float),
        assessment["black_scholes_price"].to_numpy(dtype=float)
        + assessment["underlying_price"].to_numpy(dtype=float)
        * assessment["bsgp_shadow_normalized_residual"].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-10,
    )
    print(
        json.dumps(
            {
                "assessment_bsgp_rows": len(assessment),
                "earlier_black_scholes_rows": len(earlier),
                "fixture": "immutable-schwab-offline-causal-crossfit",
                "replay_rows": len(replay),
            },
            sort_keys=True,
        )
    )


def test_empty_materialization_is_a_valid_empty_offline_replay(
    tmp_path: Path,
) -> None:
    materialization = materialize_loop_native_schwab_history(
        tmp_path,
        trainer_cutoff="2026-01-09T00:00:00Z",
        published_at="2026-01-09T00:00:01Z",
    )
    sample_path = materialization.directory / "causal-residual-samples.parquet"
    assert pd.read_parquet(sample_path).columns.tolist() == ["id"]

    catalog = load_strategy_pricing_evidence(
        tmp_path,
        available_not_after="2026-01-09T00:00:02Z",
        include_offline_replay=True,
    )

    assert catalog.predictions.empty
    assert not any(error.startswith("offline_replay:") for error in catalog.errors)


def test_target_time_volatility_clock_cannot_train() -> None:
    samples = _training_samples(session_count=3)
    samples["volatility_source_at"] = samples["observed_quote_timestamp"]
    with pytest.raises(LoopNativeModelError, match="target-time or later"):
        partition_loop_native_samples(
            samples,
            trainer_cutoff="2026-01-20T00:00:00Z",
            policy=_small_model_policy(),
        )


def test_late_underlying_readiness_cannot_train() -> None:
    samples = _training_samples(session_count=3)
    samples["underlying_readiness_ready_at"] = samples[
        "offline_emulated_prediction_at"
    ]
    with pytest.raises(LoopNativeModelError, match="target-time or later"):
        partition_loop_native_samples(
            samples,
            trainer_cutoff="2026-01-20T00:00:00Z",
            policy=_small_model_policy(),
        )


def test_surface_weighting_and_duplicate_collapse_are_invariant() -> None:
    samples = _training_samples(session_count=3)
    duplicated = pd.concat((samples, samples.iloc[[0]]), ignore_index=True)
    partitions = partition_loop_native_samples(
        duplicated,
        trainer_cutoff="2026-01-20T00:00:00Z",
        policy=_small_model_policy(),
    )
    assert partitions.duplicate_rows_collapsed == 1
    weights = surface_weights(partitions.train)
    totals = (
        partitions.train.assign(_weight=weights)
        .groupby(["symbol", "target_snapshot_for", "call_put"])["_weight"]
        .sum()
    )
    assert np.allclose(totals, 1.0)


def test_chronological_sessions_are_disjoint_and_lockbox_values_are_not_features() -> None:
    samples = _training_samples(session_count=5)
    samples["lockbox_secret"] = np.arange(len(samples))
    partitions = partition_loop_native_samples(
        samples,
        trainer_cutoff="2026-01-20T00:00:00Z",
        policy=_small_model_policy(),
    )
    assert max(partitions.train_sessions) < min(partitions.calibration_sessions)
    assert max(partitions.calibration_sessions) < min(partitions.assessment_sessions)
    assert "lockbox_secret" not in model_feature_frame(partitions.train).columns


def test_missing_source_carry_is_excluded_and_never_labeled_fred() -> None:
    source = _source_contracts().copy()
    source["dividend_yield"] = np.nan
    samples = build_causal_samples(
        source,
        target_contracts=None,
        target_underlying_price=100.0,
        source_snapshot_for="2026-01-05T16:00:00Z",
        source_available_at="2026-01-05T16:01:00Z",
        target_snapshot_for="2026-01-06T16:00:00Z",
        source_provider="schwab",
        prediction_mode="LIVE",
        allow_source_chain_carry_fallback=False,
    )
    assert samples["sample_status"].eq("DIVIDEND_UNAVAILABLE").all()
    assert (
        _carry_input_kind(
            samples,
            source_contracts=source,
            source_available_at=pd.Timestamp("2026-01-05T16:01:00Z"),
        )
        == "CAUSAL_CARRY_UNAVAILABLE"
    )


def test_current_revised_rate_receipt_cannot_cover_earlier_bootstrap() -> None:
    source = _source_contracts().copy()
    source["interest_rate"] = np.nan
    rates = pd.DataFrame(
        {
            "available_at": [pd.Timestamp("2026-08-10T00:00:00Z")],
            "risk_free_rate": [0.04],
        }
    )
    samples = build_causal_samples(
        source,
        target_contracts=None,
        target_underlying_price=100.0,
        source_snapshot_for="2026-01-05T16:00:00Z",
        source_available_at="2026-01-05T16:01:00Z",
        target_snapshot_for="2026-01-06T16:00:00Z",
        source_provider="schwab",
        prediction_mode="OFFLINE",
        rate_observations=rates,
        allow_source_chain_carry_fallback=False,
    )
    assert samples["sample_status"].eq("RATE_UNAVAILABLE").all()
    assert samples["risk_free_rate"].isna().all()


def test_unverifiable_legacy_pricing_input_is_excluded_and_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ml.option_pricing.schwab_materialization as materializer

    run = tmp_path / "ml" / "option-pricing-runs" / "legacy"
    run.mkdir(parents=True)
    samples_path = run / "pricing-samples.parquet"
    _live_samples().to_parquet(samples_path, index=False)
    mutable_input = tmp_path / "causal-input.json"
    mutable_input.write_text('{"state":"later"}\n', encoding="utf-8")
    manifest = {
        "output_files": {
            samples_path.name: {
                "size": samples_path.stat().st_size,
                "checksum_sha256": file_checksum(samples_path),
            }
        },
        "input_files": [
            {
                "path": mutable_input.relative_to(tmp_path).as_posix(),
                "size": mutable_input.stat().st_size,
                "checksum_sha256": "0" * 64,
                "status": "present",
            }
        ],
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(materializer, "authoritative_target_outcomes", lambda _root: ())
    monkeypatch.setattr(
        materializer,
        "authoritative_option_pricing_runs",
        lambda _root: {run.resolve(): pd.Timestamp("2026-01-09T00:00:00Z")},
    )

    selected, files, rejections = _receipt_proven_live_source_samples(
        tmp_path,
        trainer_cutoff=pd.Timestamp("2026-01-10T00:00:00Z"),
    )

    assert selected == {}
    assert files == ()
    assert rejections == {
        "SOURCE_PRICING_GENERATION_INPUT_FILE_UNVERIFIED": 1
    }


def test_legacy_pricing_input_path_escape_still_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SchwabMaterializationError, match="escapes the datastore"):
        _verified_manifest_input_paths(
            {
                "input_files": [
                    {
                        "path": "../escaped.json",
                        "size": 0,
                        "checksum_sha256": "0" * 64,
                        "status": "present",
                    }
                ]
            },
            root=tmp_path.resolve(),
        )


def test_model_published_at_or_after_prediction_is_rejected(tmp_path: Path) -> None:
    generation, policy = _model_generation(tmp_path)
    assert generation.published_at == pd.Timestamp("2026-01-09T00:00:02Z")

    same_time = load_prior_loop_native_model(
        tmp_path,
        prediction_created_at=generation.published_at,
    )
    earlier = load_prior_loop_native_model(
        tmp_path,
        prediction_created_at=generation.published_at - pd.Timedelta(microseconds=1),
    )
    later = load_prior_loop_native_model(
        tmp_path,
        prediction_created_at=generation.published_at + pd.Timedelta(seconds=1),
    )

    assert same_time.generation is None
    assert earlier.generation is None
    assert later.generation is not None
    assert later.status == "MODEL_VERIFIED"
    assert policy.minimum_calibrated_sessions == 1


def test_model_expiry_and_failed_verification_fall_back_to_black_scholes(
    tmp_path: Path,
) -> None:
    generation, _policy = _model_generation(tmp_path)
    stale = load_prior_loop_native_model(
        tmp_path,
        prediction_created_at=generation.expires_at,
    )
    assert stale.generation is None
    assert stale.status == "BASELINE_FALLBACK_STALE_MODEL"

    model_path = generation.directory / "pooled-call-put-model.joblib"
    model_path.write_bytes(model_path.read_bytes() + b"tamper")
    failed = load_prior_loop_native_model(
        tmp_path,
        prediction_created_at=generation.published_at + pd.Timedelta(minutes=1),
    )
    assert failed.generation is None
    assert failed.status == "BASELINE_FALLBACK_NO_MODEL"
    assert "verification failed" in failed.reason.lower()


def test_out_of_support_and_missing_input_shrink_to_baseline(tmp_path: Path) -> None:
    generation, policy = _model_generation(tmp_path)
    rows = _live_samples().iloc[:2].copy()
    rows.loc[rows.index[0], "strike"] = 1_000_000.0
    diagnostics = predict_loop_native_residuals(generation, rows, policy=policy)
    assert diagnostics.iloc[0]["status"] == "BASELINE_FALLBACK_OUT_OF_SUPPORT"
    assert diagnostics.iloc[0]["normalized_residual"] == 0.0
    assert diagnostics.iloc[0]["predictive_standard_deviation_normalized"] >= (
        policy.black_scholes_fallback_standard_deviation_normalized
    )

    missing = rows.iloc[[1]].copy()
    missing["lagged_implied_volatility"] = np.nan
    diagnostics = predict_loop_native_residuals(generation, missing, policy=policy)
    assert diagnostics.iloc[0]["status"] == "BASELINE_FALLBACK_INPUT_UNAVAILABLE"
    assert diagnostics.iloc[0]["normalized_residual"] == 0.0
    assert diagnostics.iloc[0]["predictive_standard_deviation_normalized"] == (
        policy.black_scholes_fallback_standard_deviation_normalized
    )

    sparse_policy = replace(policy, minimum_route_support_sessions=10)
    sparse = predict_loop_native_residuals(
        generation,
        _live_samples().iloc[[0]],
        policy=sparse_policy,
    ).iloc[0]
    assert 0.0 < sparse["shrinkage"] < 1.0
    assert sparse["predictive_standard_deviation_normalized"] >= (
        sparse_policy.black_scholes_fallback_standard_deviation_normalized
        * (1.0 - sparse["shrinkage"])
    )


def test_mixed_support_surface_falls_back_to_constrained_baseline(
    tmp_path: Path,
) -> None:
    generation, policy = _model_generation(tmp_path)
    samples = _live_samples()
    first_call = samples.index[samples["call_put"].eq("CALL")][0]
    samples.loc[first_call, "lagged_implied_volatility"] = 5.0
    created = pd.Timestamp("2026-01-09T16:01:00Z")
    baseline = create_prediction_rows(
        samples,
        prediction_created_at=created,
        prediction_available_at=created,
        models={},
    )
    shadow = create_bsgp_shadow_rows(
        samples,
        baseline,
        prediction_created_at=created,
        prediction_available_at=created,
        model_load=LoopNativeModelLoad(generation, "MODEL_VERIFIED", ""),
        model_policy=policy,
    )
    calls = shadow.loc[shadow["call_put"].eq("CALL")]
    assert calls["bsgp_shadow_status"].eq(
        "BASELINE_FALLBACK_OUT_OF_SUPPORT"
    ).all()
    assert calls["bsgp_shadow_fair_value_raw"].equals(
        calls["black_scholes_price"]
    )
    assert calls["bsgp_shadow_fair_value_constrained"].equals(
        calls["baseline_constrained_fair_value"]
    )
    assert calls["bsgp_shadow_normalized_residual"].eq(0.0).all()
    assert shadow.loc[shadow["call_put"].eq("PUT"), "bsgp_shadow_status"].eq(
        "BSGP_SHADOW_READY"
    ).all()


def test_valid_earlier_model_publishes_separate_shadow_before_options_receipt(
    tmp_path: Path,
) -> None:
    generation, policy = _model_generation(tmp_path)
    samples = _live_samples()
    created = pd.Timestamp("2026-01-09T16:01:00Z")
    baseline = create_prediction_rows(
        samples,
        prediction_created_at=created,
        prediction_available_at=created,
        models={},
    )
    before = baseline.copy(deep=True)
    shadow = create_bsgp_shadow_rows(
        samples,
        baseline,
        prediction_created_at=created,
        prediction_available_at=created,
        model_load=LoopNativeModelLoad(generation, "MODEL_VERIFIED", ""),
        model_policy=policy,
    )
    publication = publish_target_outcome(
        tmp_path,
        target_snapshot_for="2026-01-09T16:00:00Z",
        created_at=created,
        symbols=("NVDA",),
        symbol_outcomes={"NVDA": {"status": "READY", "reason": ""}},
        terminal_status="PREDICTIONS_PUBLISHED",
        samples=samples,
        predictions=baseline,
        shadow_predictions=shadow,
        bar_readiness=None,
        clock=lambda: pd.Timestamp("2026-01-09T16:01:05Z"),
    )
    fake_options_receipt = pd.Timestamp("2026-01-09T16:01:06Z")

    pd.testing.assert_frame_equal(baseline, before)
    assert generation.published_at < created < publication.published_at
    assert publication.published_at < fake_options_receipt
    assert publication.shadow_predictions_path is not None
    observed = publication.shadow_predictions()
    assert len(observed) == len(baseline)
    assert observed["bsgp_shadow_status"].eq("BSGP_SHADOW_READY").all()
    assert observed["black_scholes_price"].equals(
        publication.predictions(include_proof=False)["black_scholes_price"]
    )
    assert observed["automated_action_allowed"].eq(False).all()
    assert np.allclose(
        observed["bsgp_shadow_fair_value_raw"],
        observed["black_scholes_price"]
        + observed["underlying_price"]
        * observed["bsgp_shadow_normalized_residual"],
        rtol=0.0,
        atol=1e-12,
    )
    policy_artifact = publish_loop_native_eligibility_policy(
        tmp_path,
        published_at="2026-01-09T15:59:00Z",
    )
    lineage = verify_loop_native_capture_lineage(
        policy_artifact=policy_artifact,
        target_publication=publication,
        materialization=read_current_loop_native_schwab_materialization(
            tmp_path,
            load_samples=False,
        ),
        model_load=LoopNativeModelLoad(generation, "MODEL_VERIFIED", ""),
    )
    assert lineage["status"] == "PASS"


def test_black_scholes_baseline_is_identical_with_or_without_shadow_model(
    tmp_path: Path,
) -> None:
    generation, policy = _model_generation(tmp_path)
    samples = _live_samples()
    created = pd.Timestamp("2026-01-09T16:01:00Z")
    baseline = create_prediction_rows(
        samples,
        prediction_created_at=created,
        prediction_available_at=created,
        models={},
    )
    before_bytes = baseline.to_json(date_format="iso", orient="split")
    no_model = create_bsgp_shadow_rows(
        samples,
        baseline,
        prediction_created_at=created,
        prediction_available_at=created,
        model_load=LoopNativeModelLoad(
            None, "BASELINE_FALLBACK_NO_MODEL", "missing"
        ),
    )
    with_model = create_bsgp_shadow_rows(
        samples,
        baseline,
        prediction_created_at=created,
        prediction_available_at=created,
        model_load=LoopNativeModelLoad(generation, "MODEL_VERIFIED", ""),
        model_policy=policy,
    )

    assert baseline.to_json(date_format="iso", orient="split") == before_bytes
    assert no_model["black_scholes_price"].equals(baseline["black_scholes_price"])
    assert with_model["black_scholes_price"].equals(
        baseline["black_scholes_price"]
    )
    assert no_model["bsgp_shadow_fair_value_constrained"].equals(
        baseline["constrained_fair_value"]
    )


def test_loop_native_policy_uses_twelve_routes_and_requires_bounded_opra(
    tmp_path: Path,
) -> None:
    payload = loop_native_eligibility_policy_payload()
    routes = payload["required_universe"]["routes"]
    assert len(routes) == 12
    assert {row["symbol"] for row in routes} == set(LOOP_NATIVE_SYMBOLS)
    assert {row["call_put"] for row in routes} == set(LOOP_NATIVE_CALL_PUTS)
    assert payload["historical_evidence"]["paid_opra_prerequisite"] is True
    assert payload["historical_evidence"]["offline_increments_prospective_counts"] is False
    assert payload["automated_action_allowed"] is False

    artifact = publish_loop_native_eligibility_policy(
        tmp_path,
        published_at="2026-01-09T00:00:00Z",
    )
    report = build_loop_native_eligibility_report(
        policy_artifact=artifact,
        materialization_report={"routes": {}},
        model_manifest=None,
        operational_report=None,
        strategy_report=None,
        generated_at="2026-01-09T00:00:01Z",
        capture_lineage_verified=True,
    )
    assert len(report["routes"]) == 12
    assert report["gates"]["1"]["status"] == "PASS"
    assert all(
        report["gates"][str(number)]["status"] == "NOT_PROVEN"
        for number in range(2, 11)
    )
    assert report["production_authorized"] is False


def test_new_runtime_and_training_paths_have_no_opra_range_call() -> None:
    import ml.option_pricing.schwab_materialization as materializer
    import ml.option_pricing.shadow_model as shadow_model
    import ml.option_pricing_loop_native_worker as worker

    assert "get_range" not in inspect.getsource(materializer)
    assert "get_range" not in inspect.getsource(shadow_model)
    worker_source = inspect.getsource(worker)
    assert "get_range" not in worker_source
    assert 'materialization.receipt.get("published_at")' in worker_source
    assert "published_at=materialization_published" not in worker_source
    runtime_source = inspect.getsource(_run_option_pricing_once_impl)
    fast_position = runtime_source.index("_publish_fast_target_outcome(")
    worker_position = runtime_source.index("launch_loop_native_worker(")
    assert fast_position < worker_position


def test_materialization_pointer_and_selected_receipt_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    materialization = _materialization(tmp_path)
    sample_path = materialization.directory / "causal-residual-samples.parquet"
    sample_path.write_bytes(sample_path.read_bytes() + b"tamper")
    with pytest.raises(SchwabMaterializationError, match="checksum mismatch"):
        read_loop_native_schwab_materialization(
            materialization.directory,
            datastore_root=tmp_path,
        )


def test_model_and_materialization_pointer_path_or_schema_drift_fail_closed(
    tmp_path: Path,
) -> None:
    generation, _policy = _model_generation(tmp_path)
    model_pointer = (
        tmp_path / "ml" / "option-pricing-loop-native-models" / "latest.json"
    )
    payload = json.loads(model_pointer.read_text(encoding="utf-8"))
    payload["current"]["run_path"] = "../../escaped-model"
    model_pointer.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_prior_loop_native_model(
        tmp_path,
        prediction_created_at=generation.published_at + pd.Timedelta(seconds=1),
    )
    assert loaded.generation is None
    assert "escapes" in loaded.reason.lower()

    materialization_pointer = (
        tmp_path
        / "ml"
        / "option-pricing-loop-native-materialization-latest"
        / "run.json"
    )
    materialization_payload = json.loads(
        materialization_pointer.read_text(encoding="utf-8")
    )
    materialization_payload["schema_version"] = "schema-drift"
    materialization_pointer.write_text(
        json.dumps(materialization_payload), encoding="utf-8"
    )
    with pytest.raises(SchwabMaterializationError, match="malformed"):
        read_current_loop_native_schwab_materialization(tmp_path)


def test_loop_native_policy_and_report_pointers_verify_and_fail_closed(
    tmp_path: Path,
) -> None:
    policy = publish_loop_native_eligibility_policy(
        tmp_path,
        published_at="2026-01-09T00:00:00Z",
    )
    report = build_loop_native_eligibility_report(
        policy_artifact=policy,
        materialization_report=None,
        model_manifest=None,
        operational_report=None,
        strategy_report=None,
        generated_at="2026-01-09T00:00:01Z",
        capture_lineage_verified=False,
    )
    published = publish_loop_native_eligibility_report(
        tmp_path,
        report=report,
        published_at="2026-01-09T00:00:02Z",
    )
    assert read_current_loop_native_eligibility_policy(tmp_path).policy_hash == (
        policy.policy_hash
    )
    assert read_current_loop_native_eligibility_report(tmp_path).policy_hash == (
        published.policy_hash
    )

    policy_pointer = (
        tmp_path
        / "ml"
        / "option-pricing-loop-native-eligibility-policy-latest"
        / "run.json"
    )
    pointer_payload = json.loads(policy_pointer.read_text(encoding="utf-8"))
    pointer_payload["current"]["receipt_checksum_sha256"] = "0" * 64
    policy_pointer.write_text(json.dumps(pointer_payload), encoding="utf-8")
    with pytest.raises(LoopNativeEligibilityError, match="pointer verification"):
        read_current_loop_native_eligibility_policy(tmp_path)

    report_pointer = (
        tmp_path
        / "ml"
        / "option-pricing-loop-native-eligibility-latest"
        / "run.json"
    )
    pointer_payload = json.loads(report_pointer.read_text(encoding="utf-8"))
    pointer_payload["current"]["run_path"] = "../../escaped-report"
    report_pointer.write_text(json.dumps(pointer_payload), encoding="utf-8")
    with pytest.raises(LoopNativeEligibilityError, match="escapes"):
        read_current_loop_native_eligibility_report(tmp_path)


def test_shadow_target_input_inventory_tamper_fails_closed(tmp_path: Path) -> None:
    generation, model_policy = _model_generation(tmp_path)
    samples = _live_samples()
    created = pd.Timestamp("2026-01-09T16:01:00Z")
    baseline = create_prediction_rows(
        samples,
        prediction_created_at=created,
        prediction_available_at=created,
        models={},
    )
    shadow = create_bsgp_shadow_rows(
        samples,
        baseline,
        prediction_created_at=created,
        prediction_available_at=created,
        model_load=LoopNativeModelLoad(generation, "MODEL_VERIFIED", ""),
        model_policy=model_policy,
    )
    proof = tmp_path / "causal-input-proof.json"
    proof.write_text('{"verified":true}\n', encoding="utf-8")
    publish_target_outcome(
        tmp_path,
        target_snapshot_for="2026-01-09T16:00:00Z",
        created_at=created,
        symbols=("NVDA",),
        symbol_outcomes={"NVDA": {"status": "READY", "reason": ""}},
        terminal_status="PREDICTIONS_PUBLISHED",
        samples=samples,
        predictions=baseline,
        shadow_predictions=shadow,
        bar_readiness=None,
        input_files=(proof,),
        clock=lambda: pd.Timestamp("2026-01-09T16:01:05Z"),
    )
    proof.write_text('{"verified":false}\n', encoding="utf-8")
    with pytest.raises(TargetOutcomeError, match="input failed verification"):
        read_target_outcome(
            tmp_path,
            target_snapshot_for="2026-01-09T16:00:00Z",
        )


def test_expired_shadow_generation_cannot_enter_target_authority(
    tmp_path: Path,
) -> None:
    generation, model_policy = _model_generation(tmp_path)
    samples = _live_samples()
    created = generation.expires_at
    baseline = create_prediction_rows(
        samples,
        prediction_created_at=created,
        prediction_available_at=created,
        models={},
    )
    shadow = create_bsgp_shadow_rows(
        samples,
        baseline,
        prediction_created_at=created,
        prediction_available_at=created,
        model_load=LoopNativeModelLoad(generation, "MODEL_VERIFIED", ""),
        model_policy=model_policy,
    )

    with pytest.raises(TargetOutcomeError, match="unexpired"):
        publish_target_outcome(
            tmp_path,
            target_snapshot_for="2026-01-09T16:00:00Z",
            created_at=created,
            symbols=("NVDA",),
            symbol_outcomes={"NVDA": {"status": "READY", "reason": ""}},
            terminal_status="PREDICTIONS_PUBLISHED",
            samples=samples,
            predictions=baseline,
            shadow_predictions=shadow,
            bar_readiness=None,
            clock=lambda: created + pd.Timedelta(seconds=1),
        )
    assert not (
        tmp_path / "ml" / "option-pricing-target-latest" / "run.json"
    ).exists()


def test_policy_and_shadow_sidecar_tamper_fail_closed(tmp_path: Path) -> None:
    policy = publish_loop_native_eligibility_policy(
        tmp_path,
        published_at="2026-01-09T00:00:00Z",
    )
    policy_path = policy.directory / "policy.json"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            '"paid_opra_prerequisite": true',
            '"paid_opra_prerequisite": false',
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoopNativeEligibilityError, match="verification"):
        read_loop_native_eligibility_policy(
            policy.directory,
            datastore_root=tmp_path,
        )

    generation, model_policy = _model_generation(tmp_path)
    samples = _live_samples()
    created = pd.Timestamp("2026-01-09T16:01:00Z")
    baseline = create_prediction_rows(
        samples,
        prediction_created_at=created,
        prediction_available_at=created,
        models={},
    )
    shadow = create_bsgp_shadow_rows(
        samples,
        baseline,
        prediction_created_at=created,
        prediction_available_at=created,
        model_load=LoopNativeModelLoad(generation, "MODEL_VERIFIED", ""),
        model_policy=model_policy,
    )
    publication = publish_target_outcome(
        tmp_path,
        target_snapshot_for="2026-01-09T16:00:00Z",
        created_at=created,
        symbols=("NVDA",),
        symbol_outcomes={"NVDA": {"status": "READY", "reason": ""}},
        terminal_status="PREDICTIONS_PUBLISHED",
        samples=samples,
        predictions=baseline,
        shadow_predictions=shadow,
        bar_readiness=None,
        clock=lambda: pd.Timestamp("2026-01-09T16:01:05Z"),
    )
    publication.shadow_predictions_path.write_bytes(
        publication.shadow_predictions_path.read_bytes() + b"tamper"
    )
    with pytest.raises(TargetOutcomeError, match="verification failed"):
        read_target_outcome(
            tmp_path,
            target_snapshot_for="2026-01-09T16:00:00Z",
        )


def _publish_snapshot(
    root: Path,
    *,
    symbol: str,
    target: pd.Timestamp,
    available: pd.Timestamp,
    strike: float = 100.0,
):
    key = {
        "symbol": symbol,
        "snapshot_for": target,
        "available_at": available,
    }
    raw = pd.DataFrame([{**key, "status": "OK"}])
    contracts = pd.DataFrame(
        [
            {
                **key,
                "contract_symbol": f"{symbol}_TEST_C100",
                "expiration_date": target + pd.Timedelta(days=30),
                "call_put": "CALL",
                "strike": strike,
                "multiplier": 100.0,
                "mini": False,
                "non_standard": False,
            }
        ]
    )
    features = pd.DataFrame([{**key, "contract_count": 1}])
    return publish_option_snapshot(
        root,
        symbol=symbol,
        raw=raw,
        contracts=contracts,
        features=features,
        receipt_published_at=available,
    )


def _install_legacy_duplicate(
    root: Path,
    *,
    symbol: str,
    target: pd.Timestamp,
    available: pd.Timestamp,
    strike: float = 100.0,
):
    """Install an immutable pre-natural-key duplicate fixture read-only code can consume."""

    source_root = root / f"legacy-source-{available.value}-{strike:g}"
    source = _publish_snapshot(
        source_root,
        symbol=symbol,
        target=target,
        available=available,
        strike=strike,
    )
    destination = (
        root
        / "stocks"
        / symbol
        / "options"
        / "snapshots"
        / "schwab"
        / f"legacy-{target.value}-{available.value}-{strike:g}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source.directory, destination)
    receipt_path = destination / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["run_path"] = destination.relative_to(root).as_posix()
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return read_option_snapshot(destination)


def _source_contracts() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    source_time = pd.Timestamp("2026-01-05T16:00:00Z")
    expiration = pd.Timestamp("2026-02-04T16:00:00Z")
    for call_put in LOOP_NATIVE_CALL_PUTS:
        for strike in (90.0, 100.0, 110.0):
            theoretical = black_scholes_price(
                100.0,
                strike,
                0.04,
                0.30,
                30.0 / 365.0,
                0.01,
                call_put,
            )
            rows.append(
                {
                    "symbol": "NVDA",
                    "contract_symbol": f"NVDA_{call_put}_{int(strike)}",
                    "call_put": call_put,
                    "expiration_date": expiration,
                    "strike": strike,
                    "underlying_price": 100.0,
                    "bid": max(theoretical - 0.10, 0.01),
                    "ask": theoretical + 0.10,
                    "multiplier": 100.0,
                    "mini": False,
                    "non_standard": False,
                    "interest_rate": 0.04,
                    "dividend_yield": 0.01,
                    "implied_volatility": 0.30,
                    "quote_timestamp": source_time - pd.Timedelta(seconds=5),
                    "quote_staleness_seconds": 5.0,
                }
            )
    return pd.DataFrame(rows)


def _training_samples(*, session_count: int) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    sessions = pd.bdate_range("2026-01-05", periods=session_count, tz="UTC")
    for day_index, day in enumerate(sessions):
        target = day + pd.Timedelta(hours=16)
        for symbol_index, symbol in enumerate(LOOP_NATIVE_SYMBOLS):
            underlying = 100.0 + symbol_index
            for call_put in LOOP_NATIVE_CALL_PUTS:
                for strike_index, strike_offset in enumerate((-10.0, 0.0, 10.0)):
                    strike = underlying + strike_offset
                    years = 30.0 / 365.0
                    black_scholes = black_scholes_price(
                        underlying,
                        strike,
                        0.04,
                        0.30,
                        years,
                        0.01,
                        call_put,
                    )
                    residual = (
                        0.0004 * (strike_index - 1)
                        + 0.00005 * day_index
                        + 0.00001 * symbol_index
                    )
                    rows.append(
                        {
                            "symbol": symbol,
                            "source_provider": "schwab",
                            "prediction_mode": "OFFLINE",
                            "call_put": call_put,
                            "contract_symbol": (
                                f"{symbol}_{day_index}_{call_put}_{strike_index}"
                            ),
                            "expiration_date": target + pd.Timedelta(days=30),
                            "target_snapshot_for": target,
                            "source_snapshot_for": target - pd.Timedelta(minutes=15),
                            "source_available_at": target - pd.Timedelta(minutes=14),
                            "source_quote_timestamp": target - pd.Timedelta(minutes=15),
                            "rate_source_at": target - pd.Timedelta(minutes=14),
                            "volatility_source_at": target
                            - pd.Timedelta(minutes=15),
                            "dividend_source_at": target - pd.Timedelta(minutes=14),
                            "source_quote_staleness_seconds": 0.0,
                            "observed_quote_timestamp": target + pd.Timedelta(minutes=1),
                            "observed_available_at": target + pd.Timedelta(minutes=2),
                            "offline_emulated_prediction_at": target
                            + pd.Timedelta(seconds=30),
                            "prediction_created_at": pd.NaT,
                            "prediction_available_at": pd.NaT,
                            "underlying_readiness_ready_at": target
                            + pd.Timedelta(seconds=5),
                            "underlying_readiness_path": (
                                f"loop-a/bar-readiness/{target.value}/readiness.json"
                            ),
                            "underlying_readiness_receipt_path": (
                                f"loop-a/bar-readiness/{target.value}/receipt.json"
                            ),
                            "underlying_price": underlying,
                            "strike": strike,
                            "multiplier": 100.0,
                            "risk_free_rate": 0.04,
                            "lagged_implied_volatility": 0.30,
                            "target_years_to_expiration": years,
                            "dividend_yield": 0.01,
                            "observed_bid": black_scholes
                            + residual * underlying
                            - 0.05,
                            "observed_ask": black_scholes
                            + residual * underlying
                            + 0.05,
                            "observed_mid": black_scholes + residual * underlying,
                            "bid_ask_spread": 0.10,
                            "black_scholes_price": black_scholes,
                            "normalized_residual": residual,
                            "dollar_residual": residual * underlying,
                            "sample_status": "AVAILABLE",
                            "exclusion_reason": "",
                            "evidence_lane": OFFLINE_SCHWAB_BOOTSTRAP,
                            "prospective_eligible": False,
                        }
                    )
    return pd.DataFrame(rows)


def _small_model_policy() -> LoopNativeModelPolicy:
    return LoopNativeModelPolicy(
        component_count=12,
        gamma_grid=(0.3,),
        random_state=17,
        maximum_training_rows=10_000,
        minimum_fit_sessions=1,
        minimum_calibration_sessions=1,
        minimum_assessment_sessions=1,
        minimum_calibrated_sessions=1,
        minimum_route_support_sessions=1,
        maximum_age_hours=72,
    )


def _materialization(root: Path):
    samples = _training_samples(session_count=3)
    report = {
        "schema_version": SCHWAB_MATERIALIZATION_SCHEMA_VERSION,
        "policy_version": LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
        "routes": _route_report(samples, symbols=LOOP_NATIVE_SYMBOLS),
        "paid_opra_used": False,
        "external_provider_requests": 0,
        "automated_action_allowed": False,
    }
    manifest_base = {
        "schema_version": SCHWAB_MATERIALIZATION_SCHEMA_VERSION,
        "policy_version": LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
        "trainer_cutoff": "2026-01-09T00:00:00+00:00",
        "scope": {
            "symbols": list(LOOP_NATIVE_SYMBOLS),
            "call_puts": list(LOOP_NATIVE_CALL_PUTS),
            "routes": [
                {"symbol": symbol, "call_put": call_put}
                for symbol in LOOP_NATIVE_SYMBOLS
                for call_put in LOOP_NATIVE_CALL_PUTS
            ],
        },
        "selected_input_receipts": [],
        "input_files": [],
        "causality_validation": {
            "status": "PASS",
            "underlying_rule": (
                "immutable-loop-a-readiness-strictly-before-prediction"
            ),
            "target_snapshot_allowed_as_feature": False,
            "target_time_iv_allowed_as_feature": False,
            "current_revised_rate_history_used": False,
        },
        "consulted_receipt_count": 0,
        "duplicate_publication_count": 0,
        "contract_policy": {},
        "offline_emulation_delay_seconds": 60,
        "sample_columns": list(samples.columns),
        "sample_rows": len(samples),
        "report_hash_sha256": semantic_metadata_fingerprint(report),
        "automated_action_allowed": False,
    }
    return _publish_materialization(
        root,
        samples=samples,
        report=report,
        manifest_base=manifest_base,
        source_files=(),
        published_at="2026-01-09T00:00:01Z",
    )


def _model_generation(root: Path):
    materialization = _materialization(root)
    policy = _small_model_policy()
    generation = train_loop_native_shadow_generation(
        root,
        materialization=materialization,
        trainer_cutoff="2026-01-09T00:00:00Z",
        published_at="2026-01-09T00:00:02Z",
        policy=policy,
    )
    return generation, policy


def _live_samples() -> pd.DataFrame:
    source = _source_contracts().copy()
    source["expiration_date"] = pd.Timestamp("2026-02-08T16:00:00Z")
    source["quote_timestamp"] = pd.Timestamp("2026-01-08T15:59:55Z")
    samples = build_causal_samples(
        source,
        target_contracts=None,
        target_underlying_price=100.0,
        source_snapshot_for="2026-01-08T16:00:00Z",
        source_available_at="2026-01-08T16:01:00Z",
        target_snapshot_for="2026-01-09T16:00:00Z",
        source_provider="schwab",
        prediction_mode="LIVE",
        allow_source_chain_carry_fallback=False,
    )
    samples["target_years_to_expiration"] = 30.0 / 365.0
    return samples
