from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import ml.runtime_pipeline as runtime_module
from app.ui.rolling_forecast_data import (
    ForecastDataError,
    format_timestamp_local,
    load_forecast_dashboard,
    route_accessible_status_labels,
)
from ml.artifacts import verify_manifest
from ml.current_publication import resolve_current_output
from ml.horizons import (
    HORIZON_ORDER,
    WEEKLY_HORIZON_ORDER,
    PRODUCTION_FEATURE_PROFILE,
    HorizonSpecification,
    horizon_specifications_for_profile,
    is_weekly_horizon,
)
from ml.model_features import resolve_model_feature_set
from ml.rolling_materialization import (
    RollingMaterialization,
    RouteMaterialization,
)
from ml.runtime_pipeline import RuntimeConfig, run_loop_b_once
from tests.test_ml_runtime_pipeline import (
    _CONFIG,
    _FIRST_RUN,
    _SPECIFICATIONS,
    _write_synthetic_loop_a_outputs,
)


def test_runtime_intelligence_evidence_is_rendered_by_dashboard_loader(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    runtime = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )

    assert runtime.status == "COMPLETED"
    assert runtime.route_errors == {}
    dashboard = load_forecast_dashboard(
        runtime.latest_intelligence_path,
        loaded_at=(_FIRST_RUN + timedelta(minutes=1)).to_pydatetime(),
    )

    assert dashboard.operational_statuses == ("OPERATIONALLY_CURRENT",)
    assert dashboard.operational_label == "Operationally Current"
    assert dashboard.freshness_label == "Data Pipeline Is Current"
    assert dashboard.automated_action_allowed is False
    assert dashboard.automation_label == "Automated action is off"
    assert dashboard.published_route_count == 1

    symbol = dashboard.symbols[0]
    assert symbol.symbol == "GOOG"
    route = next(item for item in symbol.routes if item.horizon == "1d")
    assert route.is_missing is False
    assert route.operational_status == "OPERATIONALLY_CURRENT"
    assert route.model_evidence_status == "OFFLINE_EVALUATED_CANDIDATE"
    assert route.live_evidence_status == "NO_COMPLETED_DECISIONS"
    assert route.live_evidence_label == (
        "Awaiting First Completed Forecast (0 of 30)"
    )
    assert route.completed_decision_count == 0
    assert route.minimum_live_decision_count == 30
    assert route.automated_action_allowed is False
    assert route_accessible_status_labels(route) == (
        "Actionability: Current Forecast",
        "Live Evidence: Awaiting First Completed Forecast (0 of 30)",
    )


def test_stale_pricing_is_quarantined_without_blocking_directional_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = pd.Timestamp("2026-02-02T13:00:00Z")
    specifications = {
        "1d": horizon_specifications_for_profile(
            "loop-a-all-bsgp-active-v3",
            horizons=("1d",),
        )["1d"]
    }
    materialization = _synthetic_materialization(
        tmp_path,
        symbols=("NVDA",),
        specifications=specifications,
        created_at=created,
    )
    samples = materialization.samples.copy()
    pricing_columns = [
        column for column in samples if column.startswith("opx__")
    ]
    samples.loc[:, pricing_columns] = float("nan")
    samples["opx__join_status"] = "STALE"
    samples["opx__source_status"] = "VERIFIED"
    samples["opx__source_target_snapshot_for"] = (
        pd.to_datetime(samples["decision_timestamp"], utc=True)
        - pd.Timedelta(days=3)
    )
    stale_materialization = RollingMaterialization(
        samples=samples,
        routes=(
            RouteMaterialization(
                symbol="NVDA",
                horizon="1d",
                status="READY",
                samples=samples.copy(),
                source_files=(),
            ),
        ),
        source_files=(),
        datastore_root=tmp_path,
    )
    monkeypatch.setattr(
        runtime_module,
        "materialize_rolling_samples",
        lambda *_args, **_kwargs: stale_materialization,
    )
    reports: list[str] = []

    result = run_loop_b_once(
        tmp_path,
        symbols=("NVDA",),
        config=RuntimeConfig(
            feature_profile="loop-a-all-bsgp-active-v3",
            minimum_train_clusters=4,
            calibration_clusters=2,
            assessment_clusters=2,
            lockbox_clusters=2,
        ),
        specifications=specifications,
        run_timestamp=created,
        input_available_at=created,
        reporter=reports.append,
    )

    assert result.status == "COMPLETED"
    assert result.route_errors == {}
    assert result.models_trained == 1
    assert any("Option Pricing family quarantined" in line for line in reports)
    manifest = verify_manifest(result.run_directory)
    assert manifest["configuration"]["model_feature_sets"] == {
        "1d": "loop-a-all-v3-1d"
    }
    pricing = manifest["configuration"]["pricing_evidence"]
    assert pricing["downstream_training_eligible"] is False
    assert pricing["model_admission_by_horizon"]["1d"] == {
        "policy_version": "option-pricing-loop-b-family-coverage-freshness-gate-v1",
        "pricing_family_selected": True,
        "pricing_family_admitted": False,
        "requested_feature_set": "loop-a-all-bsgp-active-v3-1d",
        "effective_model_feature_set": "loop-a-all-v3-1d",
        "failed_routes": ["NVDA|1d"],
    }
    model_manifest_path = next(
        (tmp_path / "ml" / "models" / "1d" / "logistic-1d").glob(
            "*/manifest.json"
        )
    )
    model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    assert model_manifest["feature_set_name"] == "loop-a-all-v3-1d"
    assert not any(
        column.startswith("opx__")
        for column in model_manifest["feature_columns"]
    )
    assert "macro__fed_funds_level" in model_manifest["feature_columns"]


def test_ui_canonical_current_path_resolves_authoritative_pointer(
    tmp_path: Path,
) -> None:
    _write_synthetic_loop_a_outputs(tmp_path)
    runtime = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=_CONFIG,
        specifications=_SPECIFICATIONS,
        run_timestamp=_FIRST_RUN,
        input_available_at=_FIRST_RUN,
        reporter=None,
    )
    compatibility_mirror = (
        tmp_path
        / "ml-intelligence"
        / "latest"
        / "rolling-predictions.parquet"
    )
    compatibility_mirror.write_bytes(b"synthetic mixed legacy mirror")

    dashboard = load_forecast_dashboard(
        compatibility_mirror,
        loaded_at=(_FIRST_RUN + timedelta(minutes=1)).to_pydatetime(),
    )

    assert dashboard.source_path == (
        runtime.run_directory / "intelligence.parquet"
    )
    assert dashboard.published_route_count == 1
    assert dashboard.symbols[0].symbol == "GOOG"

    (tmp_path / "ml" / "latest" / "run.json").write_text(
        "{}",
        encoding="utf-8",
    )
    with pytest.raises(ForecastDataError) as caught:
        load_forecast_dashboard(compatibility_mirror)
    assert caught.value.code == "UNREADABLE_FILE"


def test_real_synthetic_loop_b_publishes_all_routes_and_renders_outlook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One hour before the ordinary XNAS winter open. The current synthetic
    # intraday routes therefore exercise prior-session-to-market-open display.
    created = pd.Timestamp("2026-02-02T13:30:00Z")
    symbols = ("GOOG", "MU", "NVDA")
    specifications = horizon_specifications_for_profile(
        PRODUCTION_FEATURE_PROFILE
    )
    materialization = _synthetic_materialization(
        tmp_path,
        symbols=symbols,
        specifications=specifications,
        created_at=created,
    )
    monkeypatch.setattr(
        "ml.runtime_pipeline.materialize_rolling_samples",
        lambda *_args, **_kwargs: materialization,
    )
    evaluation_sample_inputs: list[pd.DataFrame] = []
    original_evaluation_frame = runtime_module._evaluation_frame

    def recording_evaluation_frame(
        predictions: pd.DataFrame,
        samples: pd.DataFrame,
        *,
        evaluated_at: pd.Timestamp,
    ) -> pd.DataFrame:
        evaluation_sample_inputs.append(samples.copy())
        return original_evaluation_frame(
            predictions,
            samples,
            evaluated_at=evaluated_at,
        )

    monkeypatch.setattr(
        runtime_module,
        "_evaluation_frame",
        recording_evaluation_frame,
    )

    runtime = run_loop_b_once(
        tmp_path,
        symbols=symbols,
        config=RuntimeConfig(
            feature_profile=PRODUCTION_FEATURE_PROFILE,
            minimum_train_clusters=4,
            calibration_clusters=2,
            assessment_clusters=2,
            lockbox_clusters=2,
        ),
        specifications=specifications,
        run_timestamp=created,
        input_available_at=created,
        reporter=None,
    )

    assert runtime.status == "COMPLETED"
    assert runtime.route_errors == {}
    assert runtime.models_trained == len(specifications)
    assert runtime.intelligence_rows == len(symbols) * len(specifications) == 27
    intelligence = pd.read_parquet(runtime.latest_intelligence_path)
    persisted_samples = pd.read_parquet(
        runtime.run_directory / "samples.parquet"
    )
    latest_samples = pd.read_parquet(
        tmp_path / "ml" / "latest" / "samples.parquet"
    )
    predictions = pd.read_parquet(
        runtime.run_directory / "predictions.parquet"
    )
    evaluations = pd.read_parquet(
        runtime.run_directory / "evaluations.parquet"
    )
    assert len(intelligence) == 27
    assert intelligence["id"].is_unique
    assert intelligence["id"].str.count(r"\|").eq(2).all()
    assert intelligence["schema_version"].eq("one-id-v2").all()
    assert intelligence["target_definition_version"].notna().all()
    assert intelligence["minimum_live_decision_count"].gt(0).all()
    assert (
        intelligence.loc[
            intelligence["horizon"].eq("4h"),
            "model_name",
        ]
        .eq("logistic-4h")
        .all()
    )
    assert {
        (symbol, horizon)
        for symbol in symbols
        for horizon in specifications
    } == set(
        intelligence.loc[:, ["symbol", "horizon"]].itertuples(
            index=False,
            name=None,
        )
    )
    closed_lockbox_windows: set[tuple[str, pd.Timestamp]] = set()
    completed = materialization.samples.loc[
        materialization.samples["label_status"].eq("COMPLETE")
    ]
    for horizon in specifications:
        starts = pd.Index(
            completed.loc[
                completed["horizon"].eq(horizon),
                "target_window_start",
            ]
            .drop_duplicates()
            .sort_values()
        )
        closed_lockbox_windows.update(
            (horizon, pd.Timestamp(value)) for value in starts[-2:]
        )
    for frame in (
        persisted_samples,
        latest_samples,
        predictions,
        evaluations,
    ):
        observed_windows = set(
            zip(
                frame["horizon"],
                pd.to_datetime(
                    frame["target_window_start"],
                    utc=True,
                ),
                strict=True,
            )
        )
        assert observed_windows.isdisjoint(closed_lockbox_windows)
    assert not persisted_samples["target_cost_adjusted_positive"].eq(7).any()
    assert len(evaluation_sample_inputs) == 1
    assert not evaluation_sample_inputs[0][
        "target_cost_adjusted_positive"
    ].eq(7).any()

    manifest = verify_manifest(runtime.run_directory)
    assert manifest["configuration"]["horizons"] == list(specifications)
    assert manifest["configuration"]["horizon_specifications"]["4h"] == (
        specifications["4h"].as_dict()
    )

    dashboard = load_forecast_dashboard(
        runtime.latest_intelligence_path,
        loaded_at=(created + pd.Timedelta(minutes=1)).to_pydatetime(),
    )
    assert dashboard.published_route_count == 27
    assert tuple(symbol.symbol for symbol in dashboard.symbols) == symbols
    for symbol in dashboard.symbols:
        assert tuple(route.horizon for route in symbol.routes) == HORIZON_ORDER
        one_hour = next(
            route for route in symbol.routes if route.horizon == "1h"
        )
        four_hour = next(
            route for route in symbol.routes if route.horizon == "4h"
        )
        assert one_hour.target_window_start == pd.Timestamp(
            "2026-02-02T14:30:00Z"
        ).to_pydatetime()
        assert four_hour.target_window_start == one_hour.target_window_start
        assert "09:30 EST" in format_timestamp_local(
            one_hour.target_window_start,
            local_timezone=ZoneInfo("America/New_York"),
        )
        assert "06:30 PST" in format_timestamp_local(
            one_hour.target_window_start,
            local_timezone=ZoneInfo("America/Los_Angeles"),
        )
        assert not four_hour.is_missing
        assert four_hour.model_name == "logistic-4h"
        assert four_hour.id.startswith(f"{symbol.symbol}|4h|")
        assert symbol.weekly_outlook is not None
        assert tuple(
            route.horizon for route in symbol.weekly_outlook.routes
        ) == WEEKLY_HORIZON_ORDER
        assert symbol.weekly_outlook.aggregate.target_window_start == (
            pd.Timestamp("2026-02-02T14:30:00Z").to_pydatetime()
        )
        assert symbol.weekly_outlook.aggregate.target_window_end == (
            pd.Timestamp("2026-02-06T21:00:00Z").to_pydatetime()
        )


def test_frozen_weekly_snapshot_reuses_receipt_history_across_loop_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("GOOG",)
    specifications = horizon_specifications_for_profile(
        PRODUCTION_FEATURE_PROFILE
    )
    config = RuntimeConfig(
        feature_profile=PRODUCTION_FEATURE_PROFILE,
        minimum_train_clusters=4,
        calibration_clusters=2,
        assessment_clusters=2,
        lockbox_clusters=2,
        require_all_routes=True,
    )
    first_created = pd.Timestamp("2026-02-02T13:30:00Z")
    current_materialization = _synthetic_materialization(
        tmp_path,
        symbols=symbols,
        specifications=specifications,
        created_at=first_created,
    )
    monkeypatch.setattr(
        runtime_module,
        "materialize_rolling_samples",
        lambda *_args, **_kwargs: current_materialization,
    )

    first = run_loop_b_once(
        tmp_path,
        symbols=symbols,
        config=config,
        specifications=specifications,
        run_timestamp=first_created,
        input_available_at=first_created,
        reporter=None,
    )
    frozen_columns = [
        "id",
        "horizon",
        "decision_timestamp",
        "target_window_start",
        "target_window_end",
        "actionable_until",
        "prediction_created_at",
        "model_name",
        "model_version",
        "raw_probability",
        "calibrated_probability",
    ]

    def weekly_live(run_directory: Path) -> pd.DataFrame:
        frame = pd.read_parquet(run_directory / "predictions.parquet")
        return (
            frame.loc[
                frame["prediction_mode"].eq("LIVE")
                & frame["horizon"].isin(WEEKLY_HORIZON_ORDER),
                frozen_columns,
            ]
            .sort_values("horizon", kind="mergesort")
            .reset_index(drop=True)
        )

    issued = weekly_live(first.run_directory)
    assert len(issued) == 6

    for cycle_created in (
        pd.Timestamp("2026-02-03T20:00:00Z"),
        pd.Timestamp("2026-02-03T20:15:00Z"),
    ):
        refreshed = _synthetic_materialization(
            tmp_path,
            symbols=symbols,
            specifications=specifications,
            created_at=cycle_created,
        )
        refreshed_samples = refreshed.samples.copy()
        matured_d1 = (
            refreshed_samples["horizon"].eq("1w-d1")
            & refreshed_samples["decision_timestamp"].eq(
                pd.Timestamp("2026-01-30T21:05:00Z")
            )
        )
        refreshed_samples.loc[matured_d1, "label_status"] = "COMPLETE"
        refreshed_samples.loc[matured_d1, "label_exclusion_reason"] = None
        refreshed_samples.loc[matured_d1, "target_open"] = 100.0
        refreshed_samples.loc[matured_d1, "target_close"] = 101.0
        refreshed_samples.loc[matured_d1, "forward_raw_return"] = 0.01
        refreshed_samples.loc[
            matured_d1, "forward_cost_adjusted_return"
        ] = 0.009
        refreshed_samples.loc[
            matured_d1, "target_cost_adjusted_positive"
        ] = 1
        current_materialization = RollingMaterialization(
            samples=refreshed_samples,
            routes=refreshed.routes,
            source_files=refreshed.source_files,
            datastore_root=refreshed.datastore_root,
        )

        observed = run_loop_b_once(
            tmp_path,
            symbols=symbols,
            config=config,
            specifications=specifications,
            run_timestamp=cycle_created,
            input_available_at=cycle_created,
            reporter=None,
        )
        pd.testing.assert_frame_equal(
            weekly_live(observed.run_directory),
            issued,
        )
        intelligence = pd.read_parquet(
            observed.run_directory / "intelligence.parquet"
        )
        d1 = intelligence.loc[intelligence["horizon"].eq("1w-d1")]
        assert d1["intelligence_status"].eq("COMPLETED_EVIDENCE").all()
        assert d1["completed_decision_count"].eq(1).all()


def test_weekly_route_failure_preserves_prior_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = pd.Timestamp("2026-02-02T13:30:00Z")
    specifications = horizon_specifications_for_profile(
        PRODUCTION_FEATURE_PROFILE
    )
    config = RuntimeConfig(
        feature_profile=PRODUCTION_FEATURE_PROFILE,
        minimum_train_clusters=4,
        calibration_clusters=2,
        assessment_clusters=2,
        lockbox_clusters=2,
        require_all_routes=True,
    )
    current_materialization = _synthetic_materialization(
        tmp_path,
        symbols=("GOOG",),
        specifications=specifications,
        created_at=created,
    )
    monkeypatch.setattr(
        runtime_module,
        "materialize_rolling_samples",
        lambda *_args, **_kwargs: current_materialization,
    )
    first = run_loop_b_once(
        tmp_path,
        symbols=("GOOG",),
        config=config,
        specifications=specifications,
        run_timestamp=created,
        input_available_at=created,
        reporter=None,
    )
    current_before = (
        tmp_path / "ml" / "latest" / "run.json"
    ).read_bytes()
    broken_routes = tuple(
        RouteMaterialization(
            symbol=route.symbol,
            horizon=route.horizon,
            status=(
                "NO_ELIGIBLE_SOURCE_DATA"
                if route.horizon == "1w-d5"
                else route.status
            ),
            samples=route.samples,
            source_files=route.source_files,
            error=(
                "synthetic weekly component failure"
                if route.horizon == "1w-d5"
                else route.error
            ),
        )
        for route in current_materialization.routes
    )
    current_materialization = RollingMaterialization(
        samples=current_materialization.samples,
        routes=broken_routes,
        source_files=current_materialization.source_files,
        datastore_root=current_materialization.datastore_root,
    )

    with pytest.raises(RuntimeError, match="GOOG/1w-d5"):
        run_loop_b_once(
            tmp_path,
            symbols=("GOOG",),
            config=config,
            specifications=specifications,
            run_timestamp=created + pd.Timedelta(minutes=1),
            input_available_at=created + pd.Timedelta(minutes=1),
            reporter=None,
        )

    assert (tmp_path / "ml" / "latest" / "run.json").read_bytes() == (
        current_before
    )
    assert resolve_current_output(
        tmp_path,
        "predictions.parquet",
    ) == first.run_directory / "predictions.parquet"


def test_four_hour_route_failure_does_not_replace_current_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = pd.Timestamp("2026-02-02T12:00:00Z")
    symbols = ("GOOG", "MU", "NVDA")
    specifications = horizon_specifications_for_profile(
        PRODUCTION_FEATURE_PROFILE
    )
    ready = _synthetic_materialization(
        tmp_path,
        symbols=symbols,
        specifications=specifications,
        created_at=created,
    )
    monkeypatch.setattr(
        "ml.runtime_pipeline.materialize_rolling_samples",
        lambda *_args, **_kwargs: ready,
    )
    config = RuntimeConfig(
        feature_profile=PRODUCTION_FEATURE_PROFILE,
        minimum_train_clusters=4,
        calibration_clusters=2,
        assessment_clusters=2,
        lockbox_clusters=2,
        require_all_routes=True,
    )
    first = run_loop_b_once(
        tmp_path,
        symbols=symbols,
        config=config,
        specifications=specifications,
        run_timestamp=created,
        input_available_at=created,
        reporter=None,
    )
    current_before = first.latest_intelligence_path.read_bytes()

    failed_routes = tuple(
        RouteMaterialization(
            symbol=route.symbol,
            horizon=route.horizon,
            status=(
                "NO_ELIGIBLE_SOURCE_DATA"
                if (route.symbol, route.horizon) == ("NVDA", "4h")
                else route.status
            ),
            samples=(
                pd.DataFrame()
                if (route.symbol, route.horizon) == ("NVDA", "4h")
                else route.samples
            ),
            source_files=route.source_files,
            error=(
                "synthetic missing 4h source"
                if (route.symbol, route.horizon) == ("NVDA", "4h")
                else route.error
            ),
        )
        for route in ready.routes
    )
    failed = RollingMaterialization(
        samples=ready.samples.loc[
            ~(
                ready.samples["symbol"].eq("NVDA")
                & ready.samples["horizon"].eq("4h")
            )
        ].copy(),
        routes=failed_routes,
        source_files=(),
        datastore_root=tmp_path,
    )
    monkeypatch.setattr(
        "ml.runtime_pipeline.materialize_rolling_samples",
        lambda *_args, **_kwargs: failed,
    )

    with pytest.raises(
        RuntimeError,
        match=r"NVDA/4h.*synthetic missing 4h source",
    ):
        run_loop_b_once(
            tmp_path,
            symbols=symbols,
            config=config,
            specifications=specifications,
            run_timestamp=created + pd.Timedelta(minutes=1),
            input_available_at=created + pd.Timedelta(minutes=1),
            reporter=None,
        )

    assert first.latest_intelligence_path.read_bytes() == current_before


def test_four_hour_live_prediction_failure_is_transactional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = pd.Timestamp("2026-02-02T12:00:00Z")
    symbols = ("GOOG", "MU", "NVDA")
    specifications = horizon_specifications_for_profile(
        PRODUCTION_FEATURE_PROFILE
    )
    ready = _synthetic_materialization(
        tmp_path,
        symbols=symbols,
        specifications=specifications,
        created_at=created,
    )
    monkeypatch.setattr(
        runtime_module,
        "materialize_rolling_samples",
        lambda *_args, **_kwargs: ready,
    )
    config = RuntimeConfig(
        feature_profile=PRODUCTION_FEATURE_PROFILE,
        minimum_train_clusters=4,
        calibration_clusters=2,
        assessment_clusters=2,
        lockbox_clusters=2,
        require_all_routes=True,
    )
    first = run_loop_b_once(
        tmp_path,
        symbols=symbols,
        config=config,
        specifications=specifications,
        run_timestamp=created,
        input_available_at=created,
        reporter=None,
    )
    current_before = first.latest_intelligence_path.read_bytes()
    prediction_frame = runtime_module._prediction_frame

    def fail_four_hour_live(
        model: object,
        rows: pd.DataFrame,
        *,
        prediction_created_at: pd.Timestamp,
        mode: str,
    ) -> pd.DataFrame:
        if model.horizon == "4h" and mode == "LIVE":
            raise RuntimeError("synthetic 4h live prediction failure")
        return prediction_frame(
            model,
            rows,
            prediction_created_at=prediction_created_at,
            mode=mode,
        )

    monkeypatch.setattr(runtime_module, "_prediction_frame", fail_four_hour_live)

    with pytest.raises(
        RuntimeError,
        match=r"GOOG/4h.*synthetic 4h live prediction failure",
    ):
        run_loop_b_once(
            tmp_path,
            symbols=symbols,
            config=config,
            specifications=specifications,
            run_timestamp=created + pd.Timedelta(minutes=1),
            input_available_at=created + pd.Timedelta(minutes=1),
            reporter=None,
        )

    assert first.latest_intelligence_path.read_bytes() == current_before


def _synthetic_materialization(
    root: Path,
    *,
    symbols: tuple[str, ...],
    specifications: dict[str, HorizonSpecification],
    created_at: pd.Timestamp,
) -> RollingMaterialization:
    root.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    routes: list[RouteMaterialization] = []
    for horizon, specification in specifications.items():
        feature_set = resolve_model_feature_set(
            specification.feature_set,
            horizon=horizon,
        )
        cluster_count = (
            22
            if horizon == "4h"
            else (13 if horizon == "1h" else 10)
        )
        frequency = "h" if horizon in {"1h", "4h"} else "D"
        starts = pd.date_range(
            "2026-01-05T14:30:00Z",
            periods=cluster_count,
            freq=frequency,
        )
        window = (
            pd.Timedelta(hours=4)
            if horizon == "4h"
            else (
                pd.Timedelta(hours=1)
                if horizon == "1h"
                else pd.Timedelta(hours=6)
            )
        )
        target_specification = json.dumps(
            specification.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        horizon_rows: list[dict[str, object]] = []
        for cluster_index, target_start in enumerate(starts):
            decision = target_start - pd.Timedelta(hours=1)
            for symbol_index, symbol in enumerate(symbols):
                positive = (cluster_index + symbol_index) % 2
                target_close = 101.0 if positive else 99.0
                row: dict[str, object] = {
                    "id": f"{symbol}|{horizon}|{_iso_z(decision)}",
                    "symbol": symbol,
                    "provider": "databento",
                    "timeframe": specification.source_timeframe,
                    "exchange_calendar": "XNAS",
                    "exchange_session": decision.normalize(),
                    "horizon": horizon,
                    "target_definition_version": (
                        specification.target_definition_version
                    ),
                    "target_specification": target_specification,
                    "decision_timestamp": decision,
                    "information_available_at": decision,
                    "target_window_start": target_start,
                    "target_window_end": target_start + window,
                    "actionable_until": target_start,
                    "label_available_at": (
                        target_start
                        + window
                        + specification.processing_delay
                    ),
                    "target_open": 100.0,
                    "target_close": target_close,
                    "forward_raw_return": target_close / 100.0 - 1.0,
                    "forward_cost_adjusted_return": (
                        target_close / 100.0 - 1.0 - 0.001
                    ),
                    "target_cost_adjusted_positive": (
                        7
                        if cluster_index >= cluster_count - 2
                        else positive
                    ),
                    "label_status": "COMPLETE",
                    "label_exclusion_reason": None,
                    "previous_period_direction": (cluster_index + 1) % 2,
                    "assumed_round_trip_cost": 0.001,
                    **_feature_values(
                        feature_set,
                        cluster_index=cluster_index,
                        symbol_index=symbol_index,
                    ),
                }
                horizon_rows.append(row)

        if is_weekly_horizon(horizon):
            live_decision = pd.Timestamp("2026-01-30T21:05:00Z")
            live_exchange_session = pd.Timestamp("2026-01-30T00:00:00Z")
            weekly_windows = {
                "1w": (
                    pd.Timestamp("2026-02-02T14:30:00Z"),
                    pd.Timestamp("2026-02-06T21:00:00Z"),
                ),
                "1w-d1": (
                    pd.Timestamp("2026-02-02T14:30:00Z"),
                    pd.Timestamp("2026-02-02T21:00:00Z"),
                ),
                "1w-d2": (
                    pd.Timestamp("2026-02-03T14:30:00Z"),
                    pd.Timestamp("2026-02-03T21:00:00Z"),
                ),
                "1w-d3": (
                    pd.Timestamp("2026-02-04T14:30:00Z"),
                    pd.Timestamp("2026-02-04T21:00:00Z"),
                ),
                "1w-d4": (
                    pd.Timestamp("2026-02-05T14:30:00Z"),
                    pd.Timestamp("2026-02-05T21:00:00Z"),
                ),
                "1w-d5": (
                    pd.Timestamp("2026-02-06T14:30:00Z"),
                    pd.Timestamp("2026-02-06T21:00:00Z"),
                ),
            }
            live_start, live_end = weekly_windows[horizon]
            live_actionable_until = (
                pd.Timestamp("2026-02-02T21:00:00Z")
                if horizon == "1w"
                else live_end
            )
        else:
            live_start = created_at + pd.Timedelta(hours=1)
            live_end = live_start + window
            live_actionable_until = live_start
            live_decision = created_at - pd.Timedelta(minutes=5)
            live_exchange_session = live_decision.normalize()
        for symbol_index, symbol in enumerate(symbols):
            horizon_rows.append(
                {
                    "id": f"{symbol}|{horizon}|{_iso_z(live_decision)}",
                    "symbol": symbol,
                    "provider": "databento",
                    "timeframe": specification.source_timeframe,
                    "exchange_calendar": "XNAS",
                    "exchange_session": live_exchange_session,
                    "horizon": horizon,
                    "target_definition_version": (
                        specification.target_definition_version
                    ),
                    "target_specification": target_specification,
                    "decision_timestamp": live_decision,
                    "information_available_at": live_decision,
                    "target_window_start": live_start,
                    "target_window_end": live_end,
                    "actionable_until": live_actionable_until,
                    "label_available_at": (
                        live_end + specification.processing_delay
                    ),
                    "target_open": None,
                    "target_close": None,
                    "forward_raw_return": None,
                    "forward_cost_adjusted_return": None,
                    "target_cost_adjusted_positive": pd.NA,
                    "label_status": "INCOMPLETE_LABEL",
                    "label_exclusion_reason": "target_window_not_mature",
                    "previous_period_direction": symbol_index % 2,
                    "assumed_round_trip_cost": 0.001,
                    **_feature_values(
                        feature_set,
                        cluster_index=cluster_count + 1,
                        symbol_index=symbol_index,
                    ),
                }
            )
        horizon_frame = pd.DataFrame(horizon_rows)
        frames.append(horizon_frame)
        for symbol in symbols:
            routes.append(
                RouteMaterialization(
                    symbol=symbol,
                    horizon=horizon,
                    status="READY",
                    samples=horizon_frame.loc[
                        horizon_frame["symbol"].eq(symbol)
                    ].copy(),
                    source_files=(),
                )
            )
    return RollingMaterialization(
        samples=pd.concat(frames, ignore_index=True, sort=False),
        routes=tuple(routes),
        source_files=(),
        datastore_root=root,
    )


def _feature_values(
    feature_set: object,
    *,
    cluster_index: int,
    symbol_index: int,
) -> dict[str, float]:
    return {
        feature.name: (
            float(cluster_index + 1)
            + float(symbol_index) / 10.0
            + float(offset) / 100.0
        )
        for offset, feature in enumerate(feature_set.features, start=1)
    }


def _iso_z(value: pd.Timestamp) -> str:
    return value.isoformat().replace("+00:00", "Z")
