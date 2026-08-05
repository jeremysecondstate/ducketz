from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

import ml.runtime_pipeline as runtime_module
from datafetching.bar_schema import write_normalized_bar_parquet
from datafetching.cme_history import cme_writer_lock_path
from datafetching.ids import add_readable_id
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import file_checksum, verify_manifest, write_manifest
from ml.current_publication import (
    publication_contract_kind,
    read_current_publication,
    resolve_current_output,
)
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import horizon_specification
from ml.parquet_contracts import (
    CONTROL_PLANE_COLUMN_NAMES,
    NON_PERSISTED_SAMPLE_WORKFLOW_COLUMNS,
    PREDICTION_SCHEMA,
    forbidden_identity_columns,
    frame_with_readable_id,
    write_parquet_with_schema,
)
from ml.model_runtime import ModelPartitionConfig, partition_model_rows
from ml.runtime_pipeline import (
    RuntimeConfig,
    _canonical_live_evaluations,
    _evaluation_frame,
    _monitoring_frame,
    _valid_archived_live_rows,
    run_loop_b_once,
)
from ml.strategy_publication import read_current_strategy_publication
from ml.strategy_runtime import run_strategy_once
from options.publication import option_writer_lock_path


_FIRST_RUN = pd.Timestamp("2024-06-03T12:00:00Z")
_SECOND_RUN = pd.Timestamp("2024-06-03T12:01:00Z")
_MATURED_RUN = pd.Timestamp("2024-06-03T20:10:00Z")
_SPECIFICATIONS = {"1d": horizon_specification("1d")}
_CONFIG = RuntimeConfig(
    provider="databento",
    model_family="logistic",
    calibration_method="platt",
    minimum_train_clusters=30,
    calibration_clusters=10,
    assessment_clusters=10,
    lockbox_clusters=5,
)


def test_loop_b_materializes_trains_predicts_and_persists_readable_ids(
    tmp_path: Path,
) -> None:
    source = _write_synthetic_loop_a_outputs(tmp_path)

    result = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )

    assert result.status == "COMPLETED"
    assert result.route_errors == {}
    assert result.models_trained == 1
    assert result.models_reused == 0
    assert result.sample_rows == len(source["bars"]) - _CONFIG.lockbox_clusters
    assert result.prediction_rows == _CONFIG.assessment_clusters + 1
    assert result.evaluation_rows == result.prediction_rows
    assert result.monitoring_rows >= 20
    assert result.intelligence_rows == 1
    assert result.run_directory.name == "20240603T120000.000000Z"
    run_manifest = verify_manifest(result.run_directory)
    assert run_manifest["target_column"] == (
        "target_cost_adjusted_positive"
    )
    receipt = json.loads(
        (result.run_directory / "publication.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["run_path"] == (
        "ml/runs/20240603T120000.000000Z"
    )
    assert pd.Timestamp(receipt["run_timestamp"]) == _FIRST_RUN
    assert pd.Timestamp(receipt["promoted_at"]) == _FIRST_RUN
    pointer = read_current_publication(tmp_path)
    assert pointer.run_directory == result.run_directory
    assert pointer.pointer["current"]["receipt_checksum_sha256"]
    model_manifest_path = next(
        (
            tmp_path / "ml" / "models" / "1d" / "logistic-1d"
        ).glob("*/manifest.json")
    )
    model_manifest = json.loads(
        model_manifest_path.read_text(encoding="utf-8")
    )
    assert (
        model_manifest["offline_evaluation"]["status"]
        == "OFFLINE_EVALUATED_CANDIDATE"
    )
    assert (
        model_manifest["offline_evaluation"][
            "assessment_used_for_training"
        ]
        is False
    )
    assert (
        model_manifest["offline_evaluation"][
            "assessment_used_for_calibration"
        ]
        is False
    )
    assert model_manifest["offline_evaluation"]["calibrated_model"][
        "log_loss"
    ] >= 0.0
    assert model_manifest["lockbox"]["status"] == "CLOSED_UNTOUCHED_UNSCORED"
    assert model_manifest["lockbox"]["rows"] == _CONFIG.lockbox_clusters
    assert "python_major_minor" in model_manifest["runtime_compatibility"]
    assert "scikit-learn" in model_manifest["runtime_compatibility"]["packages"]

    samples = pd.read_parquet(result.run_directory / "samples.parquet")
    predictions = pd.read_parquet(result.run_directory / "predictions.parquet")
    evaluations = pd.read_parquet(result.run_directory / "evaluations.parquet")
    monitoring = pd.read_parquet(result.run_directory / "monitoring.parquet")
    intelligence = pd.read_parquet(result.run_directory / "intelligence.parquet")

    assert samples["id"].str.startswith("GOOG|1d|").all()
    assert samples["id"].str.endswith("Z").all()
    assert samples["id"].is_unique
    assert set(predictions["prediction_mode"]) == {"BACKTEST", "LIVE"}
    assert predictions["id"].str.count(r"\|").eq(3).all()
    assert predictions["raw_probability"].between(0.0, 1.0).all()
    assert predictions["calibrated_probability"].between(0.0, 1.0).all()
    assert set(evaluations["evaluation_status"]) == {"EVALUATED", "PENDING"}
    assert set(evaluations["id"]) == set(predictions["id"])
    assert evaluations.loc[
        evaluations["evaluation_status"].eq("EVALUATED"), "log_loss"
    ].notna().all()
    completed_prediction = predictions.loc[
        predictions["prediction_mode"].eq("BACKTEST")
    ].head(1)
    wrong_window = completed_prediction.copy()
    wrong_window["target_window_start"] = (
        pd.to_datetime(wrong_window["target_window_start"], utc=True)
        + pd.Timedelta(minutes=1)
    )
    assert (
        _evaluation_frame(
            wrong_window,
            samples,
            evaluated_at=_FIRST_RUN,
        ).loc[0, "evaluation_status"]
        == "TARGET_WINDOW_MISMATCH"
    )
    wrong_cost = completed_prediction.copy()
    wrong_cost["assumed_round_trip_cost"] = 0.25
    assert (
        _evaluation_frame(
            wrong_cost,
            samples,
            evaluated_at=_FIRST_RUN,
        ).loc[0, "evaluation_status"]
        == "CONFIGURATION_MISMATCH"
    )


    wrong_contract = completed_prediction.copy()
    wrong_contract["target_definition_version"] = "retired-target-v0"
    wrong_contract["target_specification"] = (
        '{"target_definition_version":"retired-target-v0"}'
    )
    assert (
        _evaluation_frame(
            wrong_contract,
            samples,
            evaluated_at=_FIRST_RUN,
        ).loc[0, "evaluation_status"]
        == "TARGET_CONTRACT_MISMATCH"
    )
    missing_live_contract = completed_prediction.copy()
    missing_live_contract["prediction_mode"] = "LIVE"
    missing_live_contract["target_definition_version"] = pd.NA
    missing_live_contract["target_specification"] = pd.NA
    missing_contract_evaluation = _evaluation_frame(
        missing_live_contract,
        samples,
        evaluated_at=_FIRST_RUN,
    )
    assert (
        missing_contract_evaluation.loc[0, "evaluation_status"]
        == "TARGET_CONTRACT_MISMATCH"
    )
    assert not _canonical_live_evaluations(
        missing_contract_evaluation
    ).shape[0]
    post_entry = completed_prediction.copy()
    post_entry["prediction_mode"] = "LIVE"
    post_entry["prediction_created_at"] = pd.to_datetime(
        post_entry["target_window_start"],
        utc=True,
    )
    assert (
        _evaluation_frame(
            post_entry,
            samples,
            evaluated_at=_FIRST_RUN,
        ).loc[0, "evaluation_status"]
        == "POST_ENTRY_PREDICTION"
    )
    invalid_status = completed_prediction.copy()
    invalid_status["prediction_status"] = "REJECTED"
    assert (
        _evaluation_frame(
            invalid_status,
            samples,
            evaluated_at=_FIRST_RUN,
        ).loc[0, "evaluation_status"]
        == "INVALID_PREDICTION"
    )
    unavailable_information = completed_prediction.copy()
    unavailable_information["information_available_at"] = (
        pd.to_datetime(
            unavailable_information["prediction_created_at"],
            utc=True,
        )
        + pd.Timedelta(seconds=1)
    )
    assert (
        _evaluation_frame(
            unavailable_information,
            samples,
            evaluated_at=_FIRST_RUN,
        ).loc[0, "evaluation_status"]
        == "INVALID_PREDICTION"
    )
    assert {
        "mean_raw_log_loss",
        "mean_log_loss",
        "mean_raw_brier_score",
        "mean_brier_score",
        "accuracy_at_0_5",
        "calibration_gap",
        "roc_auc",
        "completed_live_forecasts",
    }.issubset(set(monitoring["metric_name"]))
    live_decisions = monitoring.loc[
        monitoring["metric_name"].eq("completed_live_forecasts")
        & monitoring["scope_type"].eq("symbol_horizon")
        & monitoring["scope_value"].eq("GOOG|1d")
    ].iloc[0]
    assert live_decisions["observed_value"] == 0
    assert live_decisions["reference_value"] == 30
    assert live_decisions["status"] == "NO_COMPLETED_DECISIONS"
    assert intelligence.loc[0, "actionability_status"] == "ACTIONABLE"
    assert (
        intelligence.loc[0, "operational_status"]
        == "OPERATIONALLY_CURRENT"
    )
    assert (
        intelligence.loc[0, "model_evidence_status"]
        == "OFFLINE_EVALUATED_CANDIDATE"
    )
    assert (
        intelligence.loc[0, "live_evidence_status"]
        == "NO_COMPLETED_DECISIONS"
    )
    assert intelligence.loc[0, "completed_decision_count"] == 0
    assert intelligence.loc[0, "minimum_live_decision_count"] == 30
    assert intelligence.loc[0, "schema_version"] == "one-id-v2"
    assert not bool(intelligence.loc[0, "automated_action_allowed"])
    for name in (
        "samples.parquet",
        "predictions.parquet",
        "evaluations.parquet",
        "monitoring.parquet",
        "intelligence.parquet",
    ):
        assert (tmp_path / "ml" / "latest" / name).is_file()
    assert result.latest_intelligence_path.is_file()

    for path in _all_parquets(tmp_path):
        _assert_one_readable_id(path)


def test_loop_b_publishes_directional_outputs_before_independent_strategy(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)

    result = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )

    assert result.status == "COMPLETED"
    assert result.strategy_candidate_rows == 0
    assert result.strategy_audit_rows == 0
    assert not (result.run_directory / "strategy-candidates.parquet").exists()
    assert not (result.run_directory / "strategy-audit.parquet").exists()
    assert not (result.run_directory / "strategy-recommendations.parquet").exists()
    manifest = verify_manifest(result.run_directory)
    strategy = manifest["configuration"]["strategy_selection"]
    assert strategy["policy"] == "schwab-spreads-v1"
    assert strategy["account_authorization"] == "SPREADS"
    assert "automated_action_allowed" not in strategy
    assert strategy["real_lockbox_used"] is False
    assert strategy["mode"] == "independent-runtime"
    assert strategy["authority"] == "ml/strategy-latest/run.json"
    assert strategy["research_trace"]["version"] == "nyu-hu-uh-trace-v2"


def test_directional_publication_does_not_wait_for_active_external_writers_and_strategy_is_separate(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    release = threading.Event()
    cme_entered = threading.Event()
    options_entered = threading.Event()

    def hold_writer(path: Path, entered: threading.Event, name: str) -> None:
        with exclusive_runtime_lock(path, process_name=name):
            entered.set()
            release.wait(timeout=30)

    threads = (
        threading.Thread(
            target=hold_writer,
            args=(cme_writer_lock_path(tmp_path), cme_entered, "slow CME provider"),
            daemon=True,
        ),
        threading.Thread(
            target=hold_writer,
            args=(option_writer_lock_path(tmp_path), options_entered, "slow Options provider"),
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()
    assert cme_entered.wait(timeout=5)
    assert options_entered.wait(timeout=5)
    try:
        started = time.perf_counter()
        loop_b = run_loop_b_once(
            tmp_path,
            symbols=("GOOG",),
            config=_CONFIG,
            specifications=_SPECIFICATIONS,
            run_timestamp=_FIRST_RUN,
            input_available_at=_FIRST_RUN,
            reporter=None,
        )
        elapsed = time.perf_counter() - started
        assert elapsed < 20
        assert (loop_b.run_directory / "publication.json").is_file()
        assert not (loop_b.run_directory / "strategy-audit.parquet").exists()
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)

    source_checksums = {
        path.name: file_checksum(path)
        for path in loop_b.run_directory.iterdir()
        if path.is_file()
    }
    strategy = run_strategy_once(
        tmp_path,
        run_timestamp=_FIRST_RUN + pd.Timedelta(minutes=5),
        runtime_clock=lambda: _FIRST_RUN + pd.Timedelta(minutes=6),
        reporter=None,
    )
    publication = read_current_strategy_publication(tmp_path)
    assert publication.run_directory == strategy.run_directory
    assert strategy.source_loop_b_directory == loop_b.run_directory
    assert strategy.audit_rows == 40
    assert strategy.run_directory.parent.name == "strategy-runs"
    assert (strategy.run_directory / "strategy-audit.parquet").is_file()
    assert source_checksums == {
        path.name: file_checksum(path)
        for path in loop_b.run_directory.iterdir()
        if path.is_file()
    }
    manifest = verify_manifest(strategy.run_directory)
    assert manifest["configuration"]["source_loop_b_run"] == (
        loop_b.run_directory.relative_to(tmp_path).as_posix()
    )


def test_loop_b_reuses_model_then_reconciles_matured_live_predictions(
    tmp_path: Path,
) -> None:
    source = _write_synthetic_loop_a_outputs(tmp_path)
    first = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )
    second = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_SECOND_RUN,
        input_available_at=_SECOND_RUN,
        reporter=None,
    )

    assert second.models_trained == 0
    assert second.models_reused == 1
    model_directories = [
        path.parent
        for path in (
            tmp_path / "ml" / "models" / "1d" / "logistic-1d"
        ).glob("*/model.joblib")
    ]
    assert len(model_directories) == 1

    rogue_created_at = pd.Timestamp("2024-06-03T12:02:00Z")
    second_predictions = pd.read_parquet(
        second.run_directory / "predictions.parquet"
    )
    rogue_live = second_predictions.loc[
        second_predictions["prediction_mode"].eq("LIVE")
    ].drop(columns=["id"])
    rogue_live["prediction_created_at"] = rogue_created_at
    rogue_live = frame_with_readable_id(
        rogue_live,
        key_columns=(
            "symbol",
            "horizon",
            "decision_timestamp",
            "prediction_created_at",
        ),
    )
    incomplete_run = (
        tmp_path / "ml" / "runs" / "20240603T120200.000000Z"
    )
    write_parquet_with_schema(
        rogue_live,
        incomplete_run / "predictions.parquet",
        PREDICTION_SCHEMA,
    )
    write_manifest(
        incomplete_run,
        run_timestamp=rogue_created_at,
        input_files=(),
        output_files=("predictions.parquet",),
        configuration={},
        datastore_root=tmp_path,
    )

    completed_bars = _append_target_session(source["bars"])
    write_normalized_bar_parquet(completed_bars, source["bars_path"])
    matured = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_MATURED_RUN,
        input_available_at=_MATURED_RUN,
        reporter=None,
    )

    evaluations = pd.read_parquet(matured.run_directory / "evaluations.parquet")
    prior_live = evaluations.loc[
        evaluations["prediction_mode"].eq("LIVE")
        & evaluations["prediction_created_at"].isin([_FIRST_RUN, _SECOND_RUN])
    ]
    assert len(prior_live) == 2
    assert prior_live["evaluation_status"].eq("EVALUATED").all()
    assert prior_live["observed_target"].eq(1).all()
    assert prior_live["log_loss"].notna().all()
    assert prior_live["id"].is_unique
    assert not evaluations["prediction_created_at"].eq(rogue_created_at).any()
    assert not any(
        column in evaluations.columns
        for column in ("observation_id", "sample_id", "prediction_publication_id")
    )
    intelligence = pd.read_parquet(
        matured.run_directory / "intelligence.parquet"
    )
    monitoring = pd.read_parquet(
        matured.run_directory / "monitoring.parquet"
    )
    assert (
        intelligence.loc[0, "live_evidence_status"]
        == "INSUFFICIENT_LIVE_EVIDENCE"
    )
    assert intelligence.loc[0, "completed_decision_count"] == 1
    assert not bool(intelligence.loc[0, "automated_action_allowed"])
    live_metrics = monitoring.loc[
        monitoring["scope_type"].eq("live_horizon")
        & monitoring["scope_value"].eq("1d")
    ]
    assert {
        "mean_log_loss",
        "mean_brier_score",
        "calibration_gap",
        "roc_auc",
    }.issubset(set(live_metrics["metric_name"]))
    assert live_metrics["evidence_row_count"].eq(1).all()
    completed_metric = monitoring.loc[
        monitoring["metric_name"].eq("completed_live_forecasts")
        & monitoring["scope_type"].eq("symbol_horizon")
        & monitoring["scope_value"].eq("GOOG|1d")
    ].iloc[0]
    assert completed_metric["observed_value"] == 1
    assert intelligence.loc[0, "operational_status"] == "OPERATIONALLY_STALE"

    for path in _all_parquets(tmp_path):
        _assert_one_readable_id(path)


def test_distinct_live_decisions_increase_evidence_but_duplicate_cycles_do_not(
    tmp_path: Path,
) -> None:
    source = _write_synthetic_loop_a_outputs(tmp_path)
    first = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )

    through_june_3 = _append_target_session(source["bars"])
    write_normalized_bar_parquet(through_june_3, source["bars_path"])
    _refresh_synthetic_technicals(tmp_path, through_june_3)
    second_created = pd.Timestamp("2024-06-04T12:00:00Z")
    second = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=second_created,
        input_available_at=second_created,
        reporter=None,
    )
    second_intelligence = pd.read_parquet(
        second.run_directory / "intelligence.parquet"
    )
    assert second_intelligence.loc[0, "completed_decision_count"] == 1

    duplicate = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=second_created + pd.Timedelta(minutes=1),
        input_available_at=second_created + pd.Timedelta(minutes=1),
        reporter=None,
    )
    duplicate_intelligence = pd.read_parquet(
        duplicate.run_directory / "intelligence.parquet"
    )
    assert duplicate_intelligence.loc[0, "completed_decision_count"] == 1

    through_june_4 = _append_regular_session(
        through_june_3,
        session="2024-06-04",
        open_price=112.5,
        close_price=114.0,
    )
    write_normalized_bar_parquet(through_june_4, source["bars_path"])
    _refresh_synthetic_technicals(tmp_path, through_june_4)
    matured = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=pd.Timestamp("2024-06-05T12:00:00Z"),
        input_available_at=pd.Timestamp("2024-06-05T12:00:00Z"),
        reporter=None,
    )

    intelligence = pd.read_parquet(
        matured.run_directory / "intelligence.parquet"
    )
    monitoring = pd.read_parquet(
        matured.run_directory / "monitoring.parquet"
    )
    evaluations = pd.read_parquet(
        matured.run_directory / "evaluations.parquet"
    )
    completed = monitoring.loc[
        monitoring["metric_name"].eq("completed_live_forecasts")
        & monitoring["scope_type"].eq("symbol_horizon")
        & monitoring["scope_value"].eq("GOOG|1d")
    ].iloc[0]
    assert intelligence.loc[0, "completed_decision_count"] == 2
    assert completed["observed_value"] == 2
    canonical_decisions = evaluations.loc[
        evaluations["prediction_mode"].eq("LIVE")
        & evaluations["evaluation_status"].eq("EVALUATED"),
        "decision_timestamp",
    ]
    assert canonical_decisions.nunique() == 2
    assert len(canonical_decisions) == 3
    assert first.run_directory.is_dir()


def test_loop_b_reports_a_failed_symbol_route_when_another_symbol_succeeds(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)

    result = run_loop_b_once(
        tmp_path,
        symbols=("GOOG", "NVDA"),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )

    assert result.status == "COMPLETED_WITH_LIMITATIONS"
    assert "NVDA|1d" in result.route_errors
    assert "Expected one adjusted bar dataset" in result.route_errors["NVDA|1d"]
    predictions = pd.read_parquet(result.run_directory / "predictions.parquet")
    assert set(predictions["symbol"]) == {"GOOG"}


def test_loop_b_can_require_every_requested_symbol_route(tmp_path: Path) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)

    with pytest.raises(
        RuntimeError,
        match=(
            r"Loop B produced no predictions for required routes: NVDA/1d "
            r".*Expected one adjusted bar dataset"
        ),
    ):
        run_loop_b_once(
            tmp_path,
            symbols=("GOOG", "NVDA"),
            config=replace(_CONFIG, require_all_routes=True),
            specifications=_SPECIFICATIONS,
            run_timestamp=_FIRST_RUN,
            input_available_at=_FIRST_RUN,
            reporter=None,
        )


def test_publication_deadline_guard_preserves_current_files(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    first = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )
    current_path = first.latest_intelligence_path
    before = current_path.read_bytes()
    clock_values = iter(
        (
            pd.Timestamp("2024-06-03T12:02:00Z"),
            pd.Timestamp("2024-06-03T12:03:00Z"),
            pd.Timestamp("2024-06-03T14:00:00Z"),
        )
    )

    with pytest.raises(RuntimeError, match="publication deadline passed"):
        run_loop_b_once(
            tmp_path,
            symbols=("GOOG",),
            config=_CONFIG,
            specifications=_SPECIFICATIONS,
            run_timestamp=_SECOND_RUN,
            input_available_at=_SECOND_RUN,
            runtime_clock=lambda: next(clock_values),
            reporter=None,
        )

    failed_run = (
        tmp_path / "ml" / "runs" / "20240603T120100.000000Z"
    )
    assert failed_run.is_dir()
    assert not (failed_run / "manifest.json").exists()
    assert current_path.read_bytes() == before
    latest_pointer = json.loads(
        (tmp_path / "ml" / "latest" / "run.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest_pointer["path"].endswith(first.run_directory.name)

def test_current_output_promotion_failure_rolls_back_every_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    first = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )
    current_paths = (
        *(tmp_path / "ml" / "latest" / name for name in (
            "samples.parquet",
            "predictions.parquet",
            "evaluations.parquet",
            "monitoring.parquet",
            "intelligence.parquet",
            "run.json",
        )),
        first.latest_intelligence_path,
    )
    before = {path: path.read_bytes() for path in current_paths}
    original_replace = runtime_module._replace_staged_current_file
    replacements = 0

    def fail_during_promotion(stage: Path, destination: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == 3:
            raise OSError("synthetic promotion failure")
        original_replace(stage, destination)

    monkeypatch.setattr(
        runtime_module,
        "_replace_staged_current_file",
        fail_during_promotion,
    )
    with pytest.raises(OSError, match="synthetic promotion failure"):
        run_loop_b_once(
            tmp_path,
            symbols=("GOOG",),
            config=_CONFIG,
            specifications=_SPECIFICATIONS,
            run_timestamp=_SECOND_RUN,
            input_available_at=_SECOND_RUN,
            reporter=None,
        )

    assert {path: path.read_bytes() for path in current_paths} == before
    assert runtime_module._verify_publication_receipt(
        first.run_directory,
        verify_manifest(first.run_directory),
    )
    failed_run = tmp_path / "ml" / "runs" / "20240603T120100.000000Z"
    assert (failed_run / "manifest.json").is_file()
    assert not (failed_run / "publication.json").exists()
    assert not runtime_module._verify_publication_receipt(
        failed_run,
        verify_manifest(failed_run),
    )
    assert not list(
        (tmp_path / "ml" / "latest").glob(".*.next")
    )
    assert not list(
        (tmp_path / "ml" / "latest").glob(".*.previous")
    )


def test_authoritative_reader_stays_on_one_generation_during_mirror_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    first = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )
    first_current = resolve_current_output(
        tmp_path,
        "intelligence.parquet",
    )
    original_replace = runtime_module._replace_staged_current_file
    observed_during_mirror_replacements: list[Path] = []

    def observe_authoritative_generation(
        stage: Path,
        destination: Path,
    ) -> None:
        original_replace(stage, destination)
        observed_during_mirror_replacements.append(
            resolve_current_output(
                tmp_path,
                "intelligence.parquet",
            )
        )

    monkeypatch.setattr(
        runtime_module,
        "_replace_staged_current_file",
        observe_authoritative_generation,
    )
    second = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_SECOND_RUN,
        input_available_at=_SECOND_RUN,
        reporter=None,
    )

    assert observed_during_mirror_replacements
    assert set(observed_during_mirror_replacements) == {first_current}
    assert first_current == first.run_directory / "intelligence.parquet"
    assert second.latest_intelligence_path == (
        second.run_directory / "intelligence.parquet"
    )
    assert resolve_current_output(
        tmp_path,
        "intelligence.parquet",
    ) == second.latest_intelligence_path


def test_unselected_valid_receipt_is_not_live_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    first = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )
    original_pointer_replace = (
        runtime_module._replace_authoritative_pointer
    )
    receipt_valid_before_pointer_swap: list[bool] = []

    def fail_pointer_swap(_stage: Path, _destination: Path) -> None:
        pending = (
            tmp_path / "ml" / "runs" / "20240603T120100.000000Z"
        )
        receipt_valid_before_pointer_swap.append(
            runtime_module._verify_publication_receipt(
                pending,
                verify_manifest(pending),
            )
        )
        raise OSError("synthetic pointer failure")

    monkeypatch.setattr(
        runtime_module,
        "_replace_authoritative_pointer",
        fail_pointer_swap,
    )
    with pytest.raises(OSError, match="synthetic pointer failure"):
        run_loop_b_once(
            tmp_path,
            symbols=("GOOG",),
            config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_SECOND_RUN,
        input_available_at=_SECOND_RUN,
        reporter=None,
        )
    failed_run = tmp_path / "ml" / "runs" / "20240603T120100.000000Z"
    assert receipt_valid_before_pointer_swap == [True]
    assert not (failed_run / "publication.json").exists()
    assert (
        read_current_publication(tmp_path).run_directory
        == first.run_directory
    )

    # Simulate a process crash after preparing a fully valid receipt but
    # before the atomic pointer swap. Reachability, not receipt presence, is
    # the publication proof used by evidence reconciliation.
    previous = dict(
        read_current_publication(tmp_path).pointer["current"]
    )
    runtime_module._write_publication_receipt(
        failed_run / "publication.json",
        run_directory=failed_run,
        datastore_root=tmp_path,
        promoted_at=_SECOND_RUN,
        previous_publication=previous,
    )
    assert runtime_module._verify_publication_receipt(
        failed_run,
        verify_manifest(failed_run),
    )
    monkeypatch.setattr(
        runtime_module,
        "_replace_authoritative_pointer",
        original_pointer_replace,
    )

    observed = runtime_module._load_prior_live_predictions(
        tmp_path / "ml" / "runs",
        tmp_path / "ml" / "runs" / "uncreated-current",
        as_of=_SECOND_RUN + pd.Timedelta(minutes=1),
        specifications=_SPECIFICATIONS,
    )
    live_created = set(
        pd.to_datetime(
            observed["prediction_created_at"],
            utc=True,
        )
    )
    assert live_created == {_FIRST_RUN}
    assert _SECOND_RUN not in live_created


def test_malformed_publication_contract_never_fails_open() -> None:
    manifest = {
        "configuration": {
            "publication_contract": "not-an-object",
        }
    }
    assert publication_contract_kind(manifest) == "invalid"
    assert publication_contract_kind({}) == "invalid"
    assert not runtime_module._verify_publication_receipt(
        Path("ml/runs/does-not-matter"),
        manifest,
    )


def test_receipt_rejects_wrong_run_path_and_backwards_chronology(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    result = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )
    manifest = verify_manifest(result.run_directory)
    receipt_path = result.run_directory / "publication.json"
    original = json.loads(receipt_path.read_text(encoding="utf-8"))

    wrong_path = dict(original)
    wrong_path["run_path"] = "ml/runs/a-different-run"
    receipt_path.write_text(
        json.dumps(wrong_path),
        encoding="utf-8",
    )
    assert not runtime_module._verify_publication_receipt(
        result.run_directory,
        manifest,
    )

    backwards = dict(original)
    backwards["promoted_at"] = (
        _FIRST_RUN - pd.Timedelta(microseconds=1)
    ).isoformat()
    receipt_path.write_text(
        json.dumps(backwards),
        encoding="utf-8",
    )
    assert not runtime_module._verify_publication_receipt(
        result.run_directory,
        manifest,
    )


def test_target_boundary_crossing_during_promotion_rolls_back_current(
    tmp_path: Path,
) -> None:
    output_names = (
        "samples.parquet",
        "predictions.parquet",
        "evaluations.parquet",
        "monitoring.parquet",
        "intelligence.parquet",
    )
    run_directory = (
        tmp_path / "ml" / "runs" / "20260730T185959.000000Z"
    )
    prior_run = (
        tmp_path / "ml" / "runs" / "20260730T185800.000000Z"
    )
    latest_root = tmp_path / "ml" / "latest"
    latest_intelligence = (
        tmp_path
        / "ml-intelligence"
        / "latest"
        / "rolling-predictions.parquet"
    )
    run_directory.mkdir(parents=True)
    prior_run.mkdir(parents=True)
    latest_root.mkdir(parents=True)
    latest_intelligence.parent.mkdir(parents=True)
    for name in output_names:
        (run_directory / name).write_bytes(f"new-{name}".encode())
        (prior_run / name).write_bytes(f"old-{name}".encode())
        (latest_root / name).write_bytes(f"old-{name}".encode())
    write_manifest(
        prior_run,
        run_timestamp=pd.Timestamp("2026-07-30T18:58:00Z"),
        input_files=(),
        output_files=output_names,
        configuration={},
        datastore_root=tmp_path,
    )
    write_manifest(
        run_directory,
        run_timestamp=pd.Timestamp("2026-07-30T18:59:59Z"),
        input_files=(),
        output_files=output_names,
        configuration={
            "publication_contract": {
                "version": runtime_module._PUBLICATION_RECEIPT_VERSION,
                "receipt": runtime_module._PUBLICATION_RECEIPT_NAME,
                "required_for_live_evidence": True,
                "authority": "ml/latest/run.json",
            }
        },
        datastore_root=tmp_path,
    )
    (latest_root / "run.json").write_text(
        json.dumps(
            {
                "path": "ml/runs/20260730T185800.000000Z",
                "run_timestamp": "2026-07-30T18:58:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    latest_intelligence.write_bytes(b"old-intelligence")
    current_paths = (
        *(latest_root / name for name in output_names),
        latest_root / "run.json",
        latest_intelligence,
    )
    before = {path: path.read_bytes() for path in current_paths}
    times = iter(
        (
            pd.Timestamp("2026-07-30T18:59:59.900000Z"),
            pd.Timestamp("2026-07-30T19:00:00Z"),
        )
    )

    with pytest.raises(
        RuntimeError,
        match="publication deadline passed during atomic promotion",
    ):
        runtime_module._promote_current_outputs(
            run_directory=run_directory,
            datastore_root=tmp_path,
            output_names=output_names,
            latest_root=latest_root,
            latest_intelligence_path=latest_intelligence,
            clock=lambda: next(times),
            enforce_target_deadline=True,
            target_deadline=pd.Timestamp("2026-07-30T19:00:00Z"),
        )

    assert {path: path.read_bytes() for path in current_paths} == before


def test_live_evidence_is_deduplicated_at_symbol_horizon_decision_grain(
) -> None:
    decision = pd.Timestamp("2026-07-30T18:05:00Z")
    base = {
        "provider": "databento",
        "target_window_start": pd.Timestamp("2026-07-30T19:00:00Z"),
        "target_window_end": pd.Timestamp("2026-07-30T20:00:00Z"),
        "evaluated_at": pd.Timestamp("2026-07-30T20:05:00Z"),
        "model_name": "logistic",
        "model_version": "20260730T180000Z",
        "evaluation_status": "EVALUATED",
        "assumed_round_trip_cost": 0.001,
        "observed_target": 1,
        "observed_forward_raw_return": 0.01,
        "observed_forward_cost_adjusted_return": 0.009,
        "raw_probability": 0.55,
        "calibrated_probability": 0.54,
        "raw_log_loss": 0.60,
        "log_loss": 0.61,
        "raw_brier_score": 0.20,
        "brier_score": 0.21,
        "prediction_correct_0_5": True,
        "target_definition_version": "contract-v2",
        "target_specification": '{"version":"contract-v2"}',
    }
    rows = [
        {
            **base,
            "symbol": "GOOG",
            "horizon": "1h",
            "decision_timestamp": decision,
            "prediction_created_at": pd.Timestamp("2026-07-30T18:10:00Z"),
            "prediction_mode": "LIVE",
        },
        {
            **base,
            "symbol": "GOOG",
            "horizon": "1h",
            "decision_timestamp": decision,
            "prediction_created_at": pd.Timestamp("2026-07-30T18:20:00Z"),
            "prediction_mode": "LIVE",
        },
        {
            **base,
            "symbol": "MU",
            "horizon": "1h",
            "decision_timestamp": decision,
            "prediction_created_at": pd.Timestamp("2026-07-30T18:15:00Z"),
            "prediction_mode": "LIVE",
        },
        {
            **base,
            "symbol": "GOOG",
            "horizon": "4h",
            "decision_timestamp": decision,
            "prediction_created_at": pd.Timestamp("2026-07-30T18:12:00Z"),
            "prediction_mode": "LIVE",
        },
        {
            **base,
            "symbol": "GOOG",
            "horizon": "1h",
            "decision_timestamp": decision + pd.Timedelta(hours=1),
            "prediction_created_at": pd.Timestamp("2026-07-30T18:25:00Z"),
            "prediction_mode": "BACKTEST",
        },
        {
            **base,
            "symbol": "GOOG",
            "horizon": "1h",
            "decision_timestamp": decision + pd.Timedelta(hours=2),
            "prediction_created_at": pd.Timestamp("2026-07-30T18:30:00Z"),
            "prediction_mode": "LIVE",
            "evaluation_status": "PENDING",
        },
    ]
    evaluations = pd.DataFrame(rows)
    predictions = evaluations.loc[:, ["symbol", "horizon"]].drop_duplicates()

    monitoring = _monitoring_frame(
        predictions,
        evaluations,
        models={},
        monitored_at=pd.Timestamp("2026-07-30T20:05:00Z"),
    )

    evidence = monitoring.loc[
        monitoring["metric_name"].eq("completed_live_forecasts")
    ].set_index("scope_value")
    assert evidence.loc["GOOG|1h", "observed_value"] == 1
    assert evidence.loc["MU|1h", "observed_value"] == 1
    assert evidence.loc["GOOG|4h", "observed_value"] == 1
    assert evidence["reference_value"].eq(60).all()


def test_archived_live_row_validation_rejects_invalid_or_future_rows(
) -> None:
    created = pd.Timestamp("2026-07-30T18:20:00Z")
    valid = pd.DataFrame(
        {
            "symbol": ["GOOG"],
            "horizon": ["1h"],
            "decision_timestamp": [pd.Timestamp("2026-07-30T18:05:00Z")],
            "information_available_at": [
                pd.Timestamp("2026-07-30T18:05:00Z")
            ],
            "target_window_start": [
                pd.Timestamp("2026-07-30T19:00:00Z")
            ],
            "target_window_end": [
                pd.Timestamp("2026-07-30T20:00:00Z")
            ],
            "actionable_until": [
                pd.Timestamp("2026-07-30T19:00:00Z")
            ],
            "prediction_created_at": [created],
            "prediction_mode": ["LIVE"],
            "prediction_status": ["CREATED"],
            "assumed_round_trip_cost": [0.001],
            "calibrated_probability": [0.55],
            "target_definition_version": ["contract-v2"],
            "target_specification": ['{"version":"contract-v2"}'],
        }
    )
    rejected = valid.copy()
    rejected["prediction_status"] = "REJECTED"
    future = valid.copy()
    future["prediction_created_at"] = pd.Timestamp(
        "2026-07-31T18:20:00Z"
    )
    unavailable = valid.copy()
    unavailable["information_available_at"] = created + pd.Timedelta(
        seconds=1
    )
    backtest = valid.copy()
    backtest["prediction_mode"] = "BACKTEST"

    observed = _valid_archived_live_rows(
        pd.concat(
            [valid, rejected, future, unavailable, backtest],
            ignore_index=True,
        ),
        as_of=pd.Timestamp("2026-07-30T20:00:00Z"),
        supported_horizons=frozenset({"1h"}),
    )

    assert len(observed) == 1
    assert observed.iloc[0]["prediction_created_at"] == created

    manifest_run = pd.Timestamp("2026-07-30T18:10:00Z")
    promoted_at = pd.Timestamp("2026-07-30T18:30:00Z")
    at_manifest = valid.copy()
    at_manifest["prediction_created_at"] = manifest_run
    at_promoted = valid.copy()
    at_promoted["prediction_created_at"] = promoted_at
    before_manifest = valid.copy()
    before_manifest["prediction_created_at"] = (
        manifest_run - pd.Timedelta(microseconds=1)
    )
    after_promoted = valid.copy()
    after_promoted["prediction_created_at"] = (
        promoted_at + pd.Timedelta(microseconds=1)
    )
    target_at_promotion = valid.copy()
    target_at_promotion["target_window_start"] = promoted_at
    receipt_bounded = _valid_archived_live_rows(
        pd.concat(
            [
                valid,
                at_manifest,
                at_promoted,
                before_manifest,
                after_promoted,
                target_at_promotion,
            ],
            ignore_index=True,
        ),
        as_of=pd.Timestamp("2026-07-30T20:00:00Z"),
        supported_horizons=frozenset({"1h"}),
        manifest_run=manifest_run,
        promoted_at=promoted_at,
    )
    assert set(receipt_bounded["prediction_created_at"]) == {
        manifest_run,
        created,
        promoted_at,
    }
    not_yet_promoted = _valid_archived_live_rows(
        valid,
        as_of=promoted_at - pd.Timedelta(microseconds=1),
        supported_horizons=frozenset({"1h"}),
        manifest_run=manifest_run,
        promoted_at=promoted_at,
    )
    assert not_yet_promoted.empty


def test_model_partition_keeps_the_lockbox_closed_and_unread(
) -> None:
    decisions = pd.date_range(
        "2024-01-01T12:00:00Z",
        periods=12,
        freq="2h",
    )
    targets: list[object] = [index % 2 for index in range(10)]
    targets.extend(["LOCKBOX_TARGET_MUST_NOT_BE_READ"] * 2)
    samples = pd.DataFrame(
        {
            "id": [
                f"GOOG|1d|{value.isoformat().replace('+00:00', 'Z')}"
                for value in decisions
            ],
            "symbol": "GOOG",
            "decision_timestamp": decisions,
            "target_window_start": decisions + pd.Timedelta(hours=1),
            "target_window_end": decisions + pd.Timedelta(minutes=90),
            "label_status": "COMPLETE",
            "target_cost_adjusted_positive": targets,
        }
    )

    partitions = partition_model_rows(
        samples,
        config=ModelPartitionConfig(
            minimum_train_clusters=4,
            calibration_clusters=2,
            assessment_clusters=2,
            lockbox_clusters=2,
        ),
    )

    assert decisions.normalize().nunique() < 10
    assert partitions.lockbox_row_count == 2
    assert partitions.lockbox_cluster_count == 2
    assert partitions.lockbox_start == decisions[-2] + pd.Timedelta(hours=1)
    assert partitions.lockbox_end == decisions[-1] + pd.Timedelta(hours=1)
    returned_ids = set(partitions.train["id"])
    returned_ids.update(partitions.calibration["id"])
    returned_ids.update(partitions.assessment["id"])
    assert not returned_ids.intersection(samples["id"].tail(2))
    assert all(
        "LOCKBOX" not in str(value)
        for frame in (
            partitions.train,
            partitions.calibration,
            partitions.assessment,
        )
        for value in frame["target_cost_adjusted_positive"]
    )


def test_loop_b_rejects_stale_technical_split_adjustment_basis(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    market_regime_path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "technicals"
        / "market-regime"
        / "databento"
        / "1d.parquet"
    )
    market_regime = pd.read_parquet(market_regime_path)
    market_regime["split_event_count"] = 1
    market_regime.to_parquet(market_regime_path, index=False)

    with pytest.raises(
        RuntimeError,
        match=(
            r"Loop B produced no predictions for required routes: GOOG/1d "
            r".*split event count does not match current bars"
        ),
    ):
        run_loop_b_once(
            tmp_path,
            symbols=("GOOG",),
            config=_CONFIG,
            specifications=_SPECIFICATIONS,
            run_timestamp=_FIRST_RUN,
            input_available_at=_FIRST_RUN,
            reporter=None,
        )


def _write_synthetic_loop_a_outputs(root: Path) -> dict[str, object]:
    import exchange_calendars as xcals

    root.mkdir(parents=True, exist_ok=True)
    calendar = xcals.get_calendar(
        "XNAS",
        start="2024-01-01",
        end="2024-06-30",
    )
    sessions = calendar.sessions[
        (calendar.sessions >= pd.Timestamp("2024-01-02"))
        & (calendar.sessions <= pd.Timestamp("2024-05-31"))
    ]
    timestamps = pd.DatetimeIndex(sessions)
    if timestamps.tz is None:
        timestamps = timestamps.tz_localize("UTC")
    else:
        timestamps = timestamps.tz_convert("UTC")
    row_number = np.arange(len(timestamps), dtype=float)
    opens = 100.0 + row_number * 0.1
    gains = np.where(np.arange(len(timestamps)) % 2 == 0, -0.01, 0.01)
    closes = opens * (1.0 + gains)
    bars = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": np.maximum(opens, closes) + 0.5,
            "low": np.minimum(opens, closes) - 0.5,
            "close": closes,
            "volume": 1_000_000.0 + row_number * 1_000.0,
        }
    )
    bars_path = (
        root
        / "stocks"
        / "GOOG"
        / "bars"
        / "1d"
        / "databento"
        / "normalized"
        / "bars.parquet"
    )
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    write_normalized_bar_parquet(bars, bars_path)

    closes_by_session = pd.Series(
        [
            pd.Timestamp(calendar.closes.loc[pd.Timestamp(session)]).tz_convert(
                "UTC"
            )
            for session in sessions
        ]
    )
    _write_technical_family(
        root,
        timestamps=timestamps,
        bar_closes=closes_by_session,
        family="mr",
    )
    _write_technical_family(
        root,
        timestamps=timestamps,
        bar_closes=closes_by_session,
        family="bp",
    )
    return {"bars": bars, "bars_path": bars_path}


def _write_technical_family(
    root: Path,
    *,
    timestamps: pd.DatetimeIndex,
    bar_closes: pd.Series,
    family: str,
) -> None:
    row_number = np.arange(len(timestamps), dtype=float)
    calculation = DEFAULT_FEATURE_REGISTRY.calculation(family)
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set("technical-all")
    frame = pd.DataFrame(
        {
            "symbol": "GOOG",
            "provider": "databento",
            "timeframe": "1d",
            "timestamp": timestamps,
            "bar_end_timestamp": pd.to_datetime(bar_closes, utc=True),
            "bar_complete": True,
            "calculation": calculation.calculation_name,
            "calculation_version": calculation.allowed_versions[0],
            calculation.mode_column: "FULL",
            "price_adjustment_status": "NO_SPLIT_EVENTS_IN_RANGE",
            "split_event_count": 0,
            "generated_at": pd.to_datetime(bar_closes, utc=True)
            + pd.Timedelta(minutes=1),
        }
    )
    for offset, feature in enumerate(
        feature_set.for_family(family),
        start=1,
    ):
        frame[feature.source_column] = (
            float(offset) + np.sin(row_number / (offset + 2.0))
        )
    if family == "mr":
        frame["atr_14"] = 1.0 + row_number / 1_000.0
    frame = add_readable_id(frame, key_columns=("timestamp",))
    path = (
        root
        / "stocks"
        / "GOOG"
        / "technicals"
        / calculation.calculation_name
        / "databento"
        / "1d.parquet"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _append_target_session(frame: pd.DataFrame) -> pd.DataFrame:
    target = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-06-03T00:00:00Z")],
            "open": [111.0],
            "high": [113.0],
            "low": [110.5],
            "close": [112.5],
            "volume": [1_500_000.0],
        }
    )
    return pd.concat([frame, target], ignore_index=True)


def _append_regular_session(
    frame: pd.DataFrame,
    *,
    session: str,
    open_price: float,
    close_price: float,
) -> pd.DataFrame:
    target = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(session, tz="UTC")],
            "open": [open_price],
            "high": [max(open_price, close_price) + 0.5],
            "low": [min(open_price, close_price) - 0.5],
            "close": [close_price],
            "volume": [1_600_000.0],
        }
    )
    return pd.concat([frame, target], ignore_index=True)


def _refresh_synthetic_technicals(
    root: Path,
    bars: pd.DataFrame,
) -> None:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar(
        "XNAS",
        start="2024-01-01",
        end="2024-06-30",
    )
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(bars["timestamp"], utc=True)
    )
    closes = pd.Series(
        [
            pd.Timestamp(
                calendar.closes.loc[
                    timestamp.tz_localize(None).normalize()
                ]
            ).tz_convert("UTC")
            for timestamp in timestamps
        ]
    )
    for family in ("mr", "bp"):
        _write_technical_family(
            root,
            timestamps=timestamps,
            bar_closes=closes,
            family=family,
        )


def _all_parquets(root: Path) -> list[Path]:
    return sorted(root.rglob("*.parquet"))


def _assert_one_readable_id(path: Path) -> None:
    schema = pq.read_schema(path)
    assert schema.names.count("id") == 1, path
    assert schema.names[0] == "id", path
    assert forbidden_identity_columns(schema.names) == [], path
    assert not CONTROL_PLANE_COLUMN_NAMES.intersection(schema.names), path
    assert not NON_PERSISTED_SAMPLE_WORKFLOW_COLUMNS.intersection(
        schema.names
    ), path
    ids = pd.read_parquet(path, columns=["id"])["id"].astype("string")
    assert ids.notna().all(), path
    assert ids.str.strip().ne("").all(), path
    assert ids.is_unique, path
    assert not ids.str.fullmatch(r"[0-9a-f]{32,}", case=False).any(), path
