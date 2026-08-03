from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.calibration import IdentityCalibrator
from ml.artifacts import write_manifest
from ml.contracts import FeatureSet, FeatureSpec
from ml.horizons import (
    WEEKLY_HORIZON_ORDER,
    HorizonSpecification,
    horizon_specifications_for_profile,
)
from ml.model_runtime import RuntimeModel
from ml.parquet_contracts import PREDICTION_SCHEMA, write_parquet_with_schema
from ml.runtime_pipeline import (
    VerifiedWeeklyPredictionRun,
    _compatible_prospective_live_predictions,
    _evaluation_frame,
    _load_prior_live_predictions,
    _valid_archived_live_rows,
    _validate_weekly_specification_set,
    _weekly_live_predictions,
    _weekly_prediction_evidence_status,
)


_SYMBOL = "GOOG"
_COST = 0.001
_FRIDAY_DECISION = pd.Timestamp("2026-07-31T20:05:00Z")
_ISSUED_AT = pd.Timestamp("2026-07-31T20:06:00Z")
_D1_OPEN = pd.Timestamp("2026-08-03T13:30:00Z")
_FIRST_TARGET_SESSIONS = (
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
)
_SPECIFICATIONS = horizon_specifications_for_profile(
    "loop-a-all-v1",
    horizons=("1w",),
)
_FROZEN_COLUMNS = (
    "horizon",
    "decision_timestamp",
    "information_available_at",
    "target_window_start",
    "target_window_end",
    "actionable_until",
    "prediction_created_at",
    "model_name",
    "model_version",
    "raw_probability",
    "calibrated_probability",
)


def test_friday_issuance_creates_exact_august_three_through_seven_bundle(
    tmp_path: Path,
) -> None:
    samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )
    issued, fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.51, version="original"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=_ISSUED_AT,
    )

    assert tuple(issued["horizon"]) == WEEKLY_HORIZON_ORDER
    assert len(issued) == len(fresh) == 6
    assert issued["prediction_created_at"].nunique() == 1
    assert issued["prediction_created_at"].iloc[0] == _ISSUED_AT
    assert issued["prediction_created_at"].lt(_D1_OPEN).all()
    assert issued["decision_timestamp"].eq(_FRIDAY_DECISION).all()

    ordered = issued.set_index("horizon")
    expected_starts = pd.to_datetime(
        [f"{session}T13:30:00Z" for session in _FIRST_TARGET_SESSIONS],
        utc=True,
    )
    expected_ends = pd.to_datetime(
        [f"{session}T20:00:00Z" for session in _FIRST_TARGET_SESSIONS],
        utc=True,
    )
    assert issued.loc[issued["horizon"].eq("1w"), "actionable_until"].eq(
        expected_ends[0]
    ).all()
    assert ordered.loc["1w", "target_window_start"] == expected_starts[0]
    assert ordered.loc["1w", "target_window_end"] == expected_ends[-1]
    for lead in range(1, 6):
        row = ordered.loc[f"1w-d{lead}"]
        assert row["target_window_start"] == expected_starts[lead - 1]
        assert row["target_window_end"] == expected_ends[lead - 1]
        assert row["actionable_until"] == expected_ends[lead - 1]


def test_same_decision_is_reused_but_newer_decision_shortens_the_outlook(
    tmp_path: Path,
) -> None:
    original_samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )
    issued, _fresh = _weekly_live_predictions(
        original_samples,
        models=_models(tmp_path, probability_base=0.41, version="original"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=_ISSUED_AT,
    )
    verified = _verified_run(tmp_path, issued, suffix="origin", minutes=1)
    monday_samples = _sample_bundle(
        decision_session="2026-08-03",
        decision_timestamp=pd.Timestamp("2026-08-03T20:05:00Z"),
        target_sessions=(
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-10",
        ),
    )
    later_samples = pd.concat([original_samples, monday_samples], ignore_index=True)
    changed_models = _models(
        tmp_path,
        probability_base=0.81,
        version="daily-refresh",
    )

    first_reuse, first_fresh = _weekly_live_predictions(
        original_samples,
        models=changed_models,
        verified_runs=(verified,),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-08-03T15:00:00Z"),
    )
    refreshed, refreshed_fresh = _weekly_live_predictions(
        later_samples,
        models=_models(tmp_path, probability_base=0.11, version="next-cycle"),
        verified_runs=(verified,),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-08-04T15:00:00Z"),
    )

    assert first_fresh.empty
    pd.testing.assert_frame_equal(
        issued.loc[:, _FROZEN_COLUMNS].reset_index(drop=True),
        first_reuse.loc[:, _FROZEN_COLUMNS].reset_index(drop=True),
    )
    assert _frozen_bytes(issued) == _frozen_bytes(first_reuse)
    assert not first_reuse["model_version"].str.contains("daily-refresh").any()
    assert tuple(refreshed["horizon"]) == WEEKLY_HORIZON_ORDER[:5]
    assert tuple(refreshed_fresh["horizon"]) == WEEKLY_HORIZON_ORDER[:5]
    assert refreshed["decision_timestamp"].eq(
        pd.Timestamp("2026-08-03T20:05:00Z")
    ).all()
    assert refreshed["model_version"].str.contains("next-cycle").all()


@pytest.mark.parametrize(
    "attempted_at",
    (_D1_OPEN, _D1_OPEN + pd.Timedelta(hours=3)),
)
def test_late_monday_issuance_keeps_each_component_open_until_its_close(
    tmp_path: Path,
    attempted_at: pd.Timestamp,
) -> None:
    samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )

    issued, fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.5, version="monday-remainder"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=attempted_at,
    )

    assert tuple(issued["horizon"]) == WEEKLY_HORIZON_ORDER
    assert len(issued) == len(fresh) == 6
    assert issued["prediction_created_at"].lt(issued["actionable_until"]).all()


@pytest.mark.parametrize(
    (
        "decision_session",
        "decision_timestamp",
        "target_sessions",
        "issued_at",
        "expected",
    ),
    (
        (
            "2026-08-03",
            pd.Timestamp("2026-08-03T20:05:00Z"),
            ("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"),
            pd.Timestamp("2026-08-03T20:06:00Z"),
            ("1w", "1w-d1", "1w-d2", "1w-d3", "1w-d4"),
        ),
        (
            "2026-08-04",
            pd.Timestamp("2026-08-04T20:05:00Z"),
            ("2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11"),
            pd.Timestamp("2026-08-04T20:06:00Z"),
            ("1w", "1w-d1", "1w-d2", "1w-d3"),
        ),
        (
            "2026-08-05",
            pd.Timestamp("2026-08-05T20:05:00Z"),
            ("2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12"),
            pd.Timestamp("2026-08-05T20:06:00Z"),
            ("1w", "1w-d1", "1w-d2"),
        ),
        (
            "2026-08-06",
            pd.Timestamp("2026-08-06T20:05:00Z"),
            ("2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"),
            pd.Timestamp("2026-08-06T20:06:00Z"),
            ("1w", "1w-d1"),
        ),
    ),
)
def test_latest_completed_session_issues_only_the_remaining_exchange_week(
    tmp_path: Path,
    decision_session: str,
    decision_timestamp: pd.Timestamp,
    target_sessions: tuple[str, ...],
    issued_at: pd.Timestamp,
    expected: tuple[str, ...],
) -> None:
    samples = _sample_bundle(
        decision_session=decision_session,
        decision_timestamp=decision_timestamp,
        target_sessions=target_sessions,
    )

    issued, fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.5, version=decision_session),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=issued_at,
    )

    assert tuple(issued["horizon"]) == expected
    assert tuple(fresh["horizon"]) == expected
    assert issued["target_window_end"].dt.dayofweek.eq(4).any()


def test_dynamic_weekly_deadlines_survive_verified_run_compatibility_checks(
    tmp_path: Path,
) -> None:
    samples = _sample_bundle(
        decision_session="2026-08-03",
        decision_timestamp=pd.Timestamp("2026-08-03T20:05:00Z"),
        target_sessions=(
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-10",
        ),
    )
    issued_at = pd.Timestamp("2026-08-03T20:06:00Z")
    issued, _fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.5, version="archive"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=issued_at,
    )

    valid = _valid_archived_live_rows(
        issued,
        as_of=pd.Timestamp("2026-08-04T15:00:00Z"),
        supported_horizons=frozenset(WEEKLY_HORIZON_ORDER),
        manifest_run=issued_at,
        promoted_at=issued_at + pd.Timedelta(minutes=1),
    )
    compatible = pd.concat(
        [
            _compatible_prospective_live_predictions(
                valid,
                specification=_SPECIFICATIONS[horizon],
                assumed_round_trip_cost=_COST,
            )
            for horizon in WEEKLY_HORIZON_ORDER
        ],
        ignore_index=True,
    )
    expected = WEEKLY_HORIZON_ORDER[:5]

    assert tuple(
        horizon for horizon in WEEKLY_HORIZON_ORDER if horizon in set(valid["horizon"])
    ) == expected
    assert tuple(
        horizon
        for horizon in WEEKLY_HORIZON_ORDER
        if horizon in set(compatible["horizon"])
    ) == expected


def test_thursday_midday_aggregate_and_daily_routes_remain_evaluable(
    tmp_path: Path,
) -> None:
    samples = _sample_bundle(
        decision_session="2026-08-05",
        decision_timestamp=pd.Timestamp("2026-08-05T20:05:00Z"),
        target_sessions=(
            "2026-08-06",
            "2026-08-07",
            "2026-08-10",
            "2026-08-11",
            "2026-08-12",
        ),
    )
    issued, _fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.5, version="midday"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-08-06T15:00:00Z"),
    )
    completed = samples.copy()
    completed["label_status"] = "COMPLETE"
    completed["target_cost_adjusted_positive"] = 1
    completed["forward_raw_return"] = 0.02
    completed["forward_cost_adjusted_return"] = 0.019

    evaluations = _evaluation_frame(
        issued,
        completed,
        evaluated_at=pd.Timestamp("2026-08-07T20:06:00Z"),
    )

    assert tuple(issued["horizon"]) == ("1w", "1w-d1", "1w-d2")
    assert evaluations["evaluation_status"].eq("EVALUATED").all()


def test_partial_weekly_contract_and_partial_candidate_fail_all_or_nothing(
    tmp_path: Path,
) -> None:
    partial_specifications = dict(_SPECIFICATIONS)
    partial_specifications.pop("1w-d5")
    with pytest.raises(ValueError, match="requires all six route specifications"):
        _validate_weekly_specification_set(partial_specifications)

    partial_samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    ).loc[lambda frame: ~frame["horizon"].eq("1w-d5")]
    with pytest.raises(RuntimeError, match="no verified weekly outlook"):
        _weekly_live_predictions(
            partial_samples,
            models=_models(tmp_path, probability_base=0.5, version="partial"),
            verified_runs=(),
            specifications=_SPECIFICATIONS,
            symbols=(_SYMBOL,),
            assumed_round_trip_cost=_COST,
            prediction_created_at=_ISSUED_AT,
        )


def test_duplicate_verified_copies_select_one_bundle_without_multiplying_rows(
    tmp_path: Path,
) -> None:
    samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )
    issued, _fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.57, version="origin"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=_ISSUED_AT,
    )
    verified_copies = (
        _verified_run(tmp_path, issued, suffix="first-copy", minutes=1),
        _verified_run(tmp_path, issued.copy(), suffix="second-copy", minutes=2),
    )

    reused, fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.9, version="ignored"),
        verified_runs=verified_copies,
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-08-04T15:00:00Z"),
    )

    assert len(reused) == 6
    assert reused["id"].is_unique
    assert fresh.empty
    assert _frozen_bytes(reused) == _frozen_bytes(issued)


def test_active_original_survives_independent_maturity_and_evidence_updates(
    tmp_path: Path,
) -> None:
    samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )
    issued, _fresh = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.48, version="origin"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=_ISSUED_AT,
    )
    verified = _verified_run(tmp_path, issued, suffix="origin", minutes=1)
    matured_status = {
        "1w": "INCOMPLETE_LABEL",
        "1w-d1": "COMPLETE",
        "1w-d2": "COMPLETE",
        "1w-d3": "COMPLETE",
        "1w-d4": "INCOMPLETE_LABEL",
        "1w-d5": "INCOMPLETE_LABEL",
    }
    updated_samples = samples.copy()
    updated_samples["label_status"] = updated_samples["horizon"].map(
        matured_status
    )

    still_active, fresh = _weekly_live_predictions(
        updated_samples,
        models=_models(tmp_path, probability_base=0.7, version="refit"),
        verified_runs=(verified,),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-08-06T20:06:00Z"),
    )
    assert fresh.empty
    assert _frozen_bytes(still_active) == _frozen_bytes(issued)

    evaluations = _evaluation_status_rows(
        issued,
        {
            "1w": "PENDING",
            "1w-d1": "EVALUATED",
            "1w-d2": "EVALUATED",
            "1w-d3": "EVALUATED",
            "1w-d4": "PENDING",
            "1w-d5": "PENDING",
        },
    )
    evidence = {
        str(row.horizon): _weekly_prediction_evidence_status(
            still_active.loc[still_active["horizon"].eq(row.horizon)].iloc[0],
            evaluations,
        )
        for row in still_active.itertuples(index=False)
    }
    assert evidence == {
        "1w": "PENDING_EVIDENCE",
        "1w-d1": "COMPLETED_EVIDENCE",
        "1w-d2": "COMPLETED_EVIDENCE",
        "1w-d3": "COMPLETED_EVIDENCE",
        "1w-d4": "PENDING_EVIDENCE",
        "1w-d5": "PENDING_EVIDENCE",
    }


def test_nonfinal_session_issues_remaining_week_and_corrupt_window_fails_closed(
    tmp_path: Path,
) -> None:
    monday = _sample_bundle(
        decision_session="2026-08-03",
        decision_timestamp=pd.Timestamp("2026-08-03T20:05:00Z"),
        target_sessions=(
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-10",
        ),
    )
    issued, _fresh = _weekly_live_predictions(
        monday,
        models=_models(tmp_path, probability_base=0.5, version="monday"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-08-03T20:06:00Z"),
    )
    assert tuple(issued["horizon"]) == WEEKLY_HORIZON_ORDER[:5]

    malformed = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )
    malformed.loc[
        malformed["horizon"].eq("1w-d1"), "target_window_end"
    ] = pd.Timestamp("2026-08-10T20:00:00Z")
    malformed.loc[
        malformed["horizon"].eq("1w-d1"), "actionable_until"
    ] = pd.Timestamp("2026-08-10T20:00:00Z")
    with pytest.raises(RuntimeError, match="1w-d1 target window"):
        _weekly_live_predictions(
            malformed,
            models=_models(tmp_path, probability_base=0.5, version="bad-window"),
            verified_runs=(),
            specifications=_SPECIFICATIONS,
            symbols=(_SYMBOL,),
            assumed_round_trip_cost=_COST,
            prediction_created_at=_ISSUED_AT,
        )


def test_conflicting_verified_issuances_fail_closed(tmp_path: Path) -> None:
    samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )
    first, _ = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.42, version="first"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=_ISSUED_AT,
    )
    second, _ = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.72, version="second"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=_ISSUED_AT + pd.Timedelta(minutes=1),
    )
    verified = (
        VerifiedWeeklyPredictionRun(
            run_directory=tmp_path / "first",
            promoted_at=_ISSUED_AT + pd.Timedelta(minutes=2),
            predictions=first,
        ),
        VerifiedWeeklyPredictionRun(
            run_directory=tmp_path / "second",
            promoted_at=_ISSUED_AT + pd.Timedelta(minutes=3),
            predictions=second,
        ),
    )

    with pytest.raises(RuntimeError, match="conflicting verified weekly"):
        _weekly_live_predictions(
            samples,
            models=_models(tmp_path, probability_base=0.9, version="ignored"),
            verified_runs=verified,
            specifications=_SPECIFICATIONS,
            symbols=(_SYMBOL,),
            assumed_round_trip_cost=_COST,
            prediction_created_at=pd.Timestamp("2026-08-04T15:00:00Z"),
        )


def test_legacy_weekly_rows_are_excluded_from_live_evidence_history(
    tmp_path: Path,
) -> None:
    samples = _sample_bundle(
        decision_session="2026-07-31",
        decision_timestamp=_FRIDAY_DECISION,
        target_sessions=_FIRST_TARGET_SESSIONS,
    )
    issued, _ = _weekly_live_predictions(
        samples,
        models=_models(tmp_path, probability_base=0.53, version="legacy"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=_ISSUED_AT,
    )
    run = tmp_path / "ml" / "runs" / "20260731T200600.000000Z"
    run.mkdir(parents=True)
    write_parquet_with_schema(
        issued,
        run / "predictions.parquet",
        PREDICTION_SCHEMA,
    )
    output_names = (
        "samples.parquet",
        "predictions.parquet",
        "evaluations.parquet",
        "monitoring.parquet",
        "intelligence.parquet",
    )
    for name in output_names:
        path = run / name
        if not path.exists():
            path.write_bytes(b"synthetic legacy output")
    write_manifest(
        run,
        run_timestamp=_ISSUED_AT,
        input_files=(),
        output_files=output_names,
        configuration={
            "horizon_specifications": {
                horizon: specification.as_dict()
                for horizon, specification in _SPECIFICATIONS.items()
            }
        },
        datastore_root=tmp_path,
    )

    observed = _load_prior_live_predictions(
        tmp_path / "ml" / "runs",
        tmp_path / "ml" / "runs" / "uncreated-current",
        as_of=pd.Timestamp("2026-08-04T15:00:00Z"),
        specifications=_SPECIFICATIONS,
    )
    assert observed.empty


def test_new_final_session_decision_replaces_a_holiday_week_snapshot(
    tmp_path: Path,
) -> None:
    original_samples = _sample_bundle(
        decision_session="2026-09-04",
        decision_timestamp=pd.Timestamp("2026-09-04T20:05:00Z"),
        target_sessions=(
            "2026-09-08",
            "2026-09-09",
            "2026-09-10",
            "2026-09-11",
            "2026-09-14",
        ),
    )
    issued, _ = _weekly_live_predictions(
        original_samples,
        models=_models(tmp_path, probability_base=0.44, version="holiday-origin"),
        verified_runs=(),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-09-04T20:06:00Z"),
    )
    next_friday_samples = _sample_bundle(
        decision_session="2026-09-11",
        decision_timestamp=pd.Timestamp("2026-09-11T20:05:00Z"),
        target_sessions=(
            "2026-09-14",
            "2026-09-15",
            "2026-09-16",
            "2026-09-17",
            "2026-09-18",
        ),
    )
    verified = VerifiedWeeklyPredictionRun(
        run_directory=tmp_path / "holiday-origin",
        promoted_at=pd.Timestamp("2026-09-04T20:07:00Z"),
        predictions=issued,
    )

    reused, fresh = _weekly_live_predictions(
        pd.concat(
            [original_samples, next_friday_samples],
            ignore_index=True,
        ),
        models=_models(tmp_path, probability_base=0.84, version="overlap"),
        verified_runs=(verified,),
        specifications=_SPECIFICATIONS,
        symbols=(_SYMBOL,),
        assumed_round_trip_cost=_COST,
        prediction_created_at=pd.Timestamp("2026-09-11T20:06:00Z"),
    )

    assert len(reused) == len(fresh) == 6
    assert reused["decision_timestamp"].eq(
        pd.Timestamp("2026-09-11T20:05:00Z")
    ).all()
    assert reused.loc[
        reused["horizon"].eq("1w"), "target_window_end"
    ].iloc[0] == pd.Timestamp("2026-09-18T20:00:00Z")
    assert _frozen_bytes(reused) != _frozen_bytes(issued)


class _ConstantEstimator:
    def __init__(self, probability: float) -> None:
        self.probability = probability

    def predict_proba(self, rows: pd.DataFrame) -> np.ndarray:
        positive = np.full(len(rows), self.probability, dtype=float)
        return np.column_stack((1.0 - positive, positive))


def _feature_set() -> FeatureSet:
    return FeatureSet(
        name="synthetic-weekly-runtime-v1",
        features=(
            FeatureSpec(
                name="mr__synthetic_weekly_signal",
                source_family="mr",
                source_column="synthetic_weekly_signal",
            ),
        ),
        applicable_horizons=("1w",),
    )


def _models(
    tmp_path: Path,
    *,
    probability_base: float,
    version: str,
) -> dict[str, RuntimeModel]:
    feature_set = _feature_set()
    return {
        horizon: RuntimeModel(
            model_name=f"logistic-{horizon}",
            horizon=horizon,
            feature_set=feature_set,
            estimator=_ConstantEstimator(probability_base + index * 0.01),
            calibrator=IdentityCalibrator(),
            calibration_method="none",
            artifact_directory=tmp_path / f"{version}-{horizon}",
            offline_evaluation={},
            reused=True,
        )
        for index, horizon in enumerate(WEEKLY_HORIZON_ORDER)
    }


def _sample_bundle(
    *,
    decision_session: str,
    decision_timestamp: pd.Timestamp,
    target_sessions: tuple[str, str, str, str, str],
) -> pd.DataFrame:
    starts = [pd.Timestamp(f"{session}T13:30:00Z") for session in target_sessions]
    ends = [pd.Timestamp(f"{session}T20:00:00Z") for session in target_sessions]
    first_week = (
        starts[0].normalize()
        - pd.Timedelta(days=int(starts[0].weekday()))
    )
    remaining_week_ends = [
        end
        for start, end in zip(starts, ends, strict=True)
        if (
            start.normalize()
            - pd.Timedelta(days=int(start.weekday()))
        )
        == first_week
    ]
    rows: list[dict[str, object]] = []
    for horizon in WEEKLY_HORIZON_ORDER:
        specification = _SPECIFICATIONS[horizon]
        if horizon == "1w":
            target_start = starts[0]
            target_end = remaining_week_ends[-1]
        else:
            lead = int(horizon[-1])
            target_start = starts[lead - 1]
            target_end = ends[lead - 1]
        rows.append(
            {
                "symbol": _SYMBOL,
                "provider": "databento",
                "exchange_calendar": "XNAS",
                "exchange_session": pd.Timestamp(decision_session),
                "horizon": horizon,
                "decision_timestamp": decision_timestamp,
                "information_available_at": decision_timestamp,
                "target_window_start": target_start,
                "target_window_end": target_end,
                "actionable_until": (
                    ends[0] if horizon == "1w" else ends[int(horizon[-1]) - 1]
                ),
                "target_definition_version": (
                    specification.target_definition_version
                ),
                "target_specification": _target_specification(specification),
                "assumed_round_trip_cost": _COST,
                "label_status": "INCOMPLETE_LABEL",
                "mr__synthetic_weekly_signal": float(len(rows) + 1),
            }
        )
    return pd.DataFrame(rows)


def _target_specification(specification: HorizonSpecification) -> str:
    return json.dumps(
        specification.as_dict(),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _verified_run(
    tmp_path: Path,
    predictions: pd.DataFrame,
    *,
    suffix: str,
    minutes: int,
) -> VerifiedWeeklyPredictionRun:
    return VerifiedWeeklyPredictionRun(
        run_directory=tmp_path / suffix,
        promoted_at=_ISSUED_AT + pd.Timedelta(minutes=minutes),
        predictions=predictions.copy(),
    )


def _frozen_bytes(predictions: pd.DataFrame) -> bytes:
    return predictions.loc[:, _FROZEN_COLUMNS].to_csv(
        index=False,
        date_format="%Y-%m-%dT%H:%M:%S.%fZ",
    ).encode("utf-8")


def _evaluation_status_rows(
    predictions: pd.DataFrame,
    status_by_horizon: dict[str, str],
) -> pd.DataFrame:
    rows = predictions.loc[
        :,
        [
            "symbol",
            "horizon",
            "decision_timestamp",
            "prediction_created_at",
            "prediction_mode",
        ],
    ].copy()
    rows["evaluated_at"] = pd.Timestamp("2026-08-06T20:06:00Z")
    rows["evaluation_status"] = rows["horizon"].map(status_by_horizon)
    return rows
