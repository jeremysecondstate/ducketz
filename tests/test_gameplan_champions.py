import json
from datetime import date

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from ml.artifacts import file_checksum, write_manifest
from ml.calibration import IdentityCalibrator
from ml.gameplan_champions import latest_promoted_champion, retain_champion
from ml.gameplan_source_selection import GAMEPLAN_SOURCE_SELECTION_VERSION
from ml.nightly_gameplan import GAMEPLAN_VERSION, _publish_gameplan
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS


DAY = date(2026, 9, 8)
SOURCE = "xnas-itch-archive-v1"
CONTRACT = "independent-stock-targets-v1"


def publish(root, stamp="20260908T080000.000000Z", *, status="PROMOTED", config_changes=None):
    run = root / "ml/nightly-gameplan-runs" / stamp
    model_path = run / "models/1d/model.joblib"
    model_path.parent.mkdir(parents=True)
    matrix = pd.DataFrame({"x": [-2., -1., 1., 2.], "symbol": "COST", "route": "1d@D+1"})
    estimator = Pipeline([("x", ColumnTransformer([("x", "passthrough", ["x"])])),
                          ("model", LogisticRegression())]).fit(matrix, [0, 0, 1, 1])
    trained_at = pd.Timestamp(stamp)
    joblib.dump({"schema_version": GAMEPLAN_VERSION, "group": "1d", "feature_columns": ["x"],
                 "categorical_columns": ["symbol", "route"], "selected_family": "fixture-logistic",
                 "trained_at": trained_at.isoformat(), "estimator": estimator,
                 "calibrator": IdentityCalibrator()}, model_path)
    report = {"schema_version": GAMEPLAN_VERSION, "group": "1d", "target_contract_version": CONTRACT,
              "target_price_source_contract": SOURCE, "target_price_dataset": "XNAS.ITCH",
              "selected_family": "fixture-logistic", "selected_neural_weight": None,
              "calibration_method": "none", "calibration_diagnostics": {"information_available": True, "status": "DIRECTIONAL_INFORMATION_AVAILABLE"},
              "features": {"admitted": ["x"]}, "partition_decision_clusters": {"assessment": 10},
              "assessment": {"brier_score": .1, "log_loss": .3, "expected_calibration_error_10_bin": .1},
              "training_base_rate_assessment": {"brier_score": .25, "log_loss": .69},
              "promotion_gate": {"status": status, "checks": {
                  "calibration_retains_directional_information": True,
                  "assessment_has_at_least_10_decision_clusters": True,
                  "brier_beats_training_base_rate": True, "log_loss_beats_training_base_rate": True,
                  "expected_calibration_error_at_most_0_15": True}},
              "target_support_by_symbol": {"COST": {"admitted_rows": 80, "fitted_rows": 50,
                  "assessment_rows": 10, "admitted_rows_by_route": {"1d@D+1": 80},
                  "fitted_rows_by_route": {"1d@D+1": 50}, "assessment_rows_by_route": {"1d@D+1": 10}}},
              "model_file": {"path": "models/1d/model.joblib", "size": model_path.stat().st_size,
                             "checksum_sha256": file_checksum(model_path)}}
    (run / "model-reports.json").write_text(json.dumps({"1d": report}))
    matrix.assign(target=[0, 0, 1, 1]).to_parquet(run / "training-cohort-1d.parquet", index=False)
    config = {"schema_version": GAMEPLAN_VERSION, "target_contract_version": CONTRACT,
              "target_price_source_contract": SOURCE, "target_price_dataset": "XNAS.ITCH",
              "action_date": str(DAY), "symbols": list(STOCK_TRADER_SYMBOLS), **(config_changes or {})}
    write_manifest(run, run_timestamp=trained_at, input_files=(),
                   output_files=("models/1d/model.joblib", "model-reports.json", "training-cohort-1d.parquet"),
                   configuration=config)
    _publish_gameplan(root, run=run, action_date=DAY, published_at=trained_at,
                      source_loop_b="fixture", source_strategy=None)
    return run


def find(root):
    return latest_promoted_champion(root, group="1d", action_date=DAY, symbols=STOCK_TRADER_SYMBOLS,
                                    price_source=SOURCE, before=pd.Timestamp("2026-09-08T10:00Z"))


def test_failed_challenger_cannot_evict_latest_compatible_promoted_champion(tmp_path):
    publish(tmp_path, "20260908T070000.000000Z")
    qualified = publish(tmp_path)
    publish(tmp_path, "20260908T090000.000000Z", status="RESEARCH_NOT_PROMOTED")
    assert find(tmp_path)["run"] == qualified


def test_prior_session_selector_cannot_retain_legacy_selector_champion(tmp_path):
    legacy = publish(tmp_path)
    # The same action date, symbols and native target-price feed are not enough:
    # the model was fitted using a different historical feature-selection rule.
    assert latest_promoted_champion(
        tmp_path, group="1d", action_date=DAY, symbols=STOCK_TRADER_SYMBOLS,
        price_source=SOURCE, before=pd.Timestamp("2026-09-08T10:00Z"),
        source_selection_contract=GAMEPLAN_SOURCE_SELECTION_VERSION,
    ) is None
    assert latest_promoted_champion(
        tmp_path, group="1d", action_date=DAY, symbols=STOCK_TRADER_SYMBOLS,
        price_source=SOURCE, before=pd.Timestamp("2026-09-08T10:00Z"),
        source_selection_contract=None,
    )["run"] == legacy


def test_raw_direction_publication_cannot_retain_cost_adjusted_champion(tmp_path):
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    publish(tmp_path)
    assert latest_promoted_champion(
        tmp_path, group="1d", action_date=DAY, symbols=STOCK_TRADER_SYMBOLS,
        price_source=SOURCE, before=pd.Timestamp("2026-09-08T10:00Z"),
        probability_target=RAW_DIRECTION_TARGET,
    ) is None
    current = pd.DataFrame({"probability_target_contract": [RAW_DIRECTION_TARGET]})
    with pytest.raises(RuntimeError, match="another probability target contract"):
        retain_champion({}, champion=find(tmp_path), current=current, run=tmp_path / "unused",
                        group="1d", frozen_at="2026-09-08T09:30Z")


def test_retained_prediction_rejects_current_features_from_new_selector(tmp_path):
    source_run = publish(tmp_path)
    champion = find(tmp_path)
    source_hash = file_checksum(source_run / "models/1d/model.joblib")
    current = pd.DataFrame({
        "x": [1.], "symbol": ["COST"], "route": ["1d@D+1"],
        "decision_timestamp": ["2026-09-08T07:00Z"],
        "information_available_at": ["2026-09-08T07:00Z"],
        "source_selection_contract": [GAMEPLAN_SOURCE_SELECTION_VERSION],
    })
    destination = tmp_path / "unused"
    with pytest.raises(RuntimeError, match="another source selection contract"):
        retain_champion({}, champion=champion, current=current, run=destination,
                        group="1d", frozen_at="2026-09-08T09:30Z")
    assert not destination.exists()
    assert file_checksum(source_run / "models/1d/model.joblib") == source_hash


@pytest.mark.parametrize("change", [
    {"target_price_source_contract": "canonical-equity-minute-v1"},
    {"symbols": ["COST"]}, {"schema_version": "unknown"},
    {"target_contract_version": "legacy"}, {"action_date": "2026-09-09"},
])
def test_incompatible_publications_are_not_champions(tmp_path, change):
    publish(tmp_path, config_changes=change)
    assert find(tmp_path) is None


def test_tampered_bound_model_fails_closed(tmp_path):
    run = publish(tmp_path)
    with (run / "models/1d/model.joblib").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(RuntimeError, match="mismatch"):
        find(tmp_path)


def test_retained_model_recomputes_current_features_and_keeps_actual_evidence(tmp_path):
    source_run = publish(tmp_path)
    champion = find(tmp_path)
    run = tmp_path / "ml/nightly-gameplan-runs/20260908T093000.000000Z"
    (run / "models/1d").mkdir(parents=True)
    challenger_model = run / "models/1d/model.joblib"
    challenger_model.write_bytes(b"untouched challenger")
    current = pd.DataFrame({"symbol": ["COST", "COST"], "route": ["1d@D+1", "1d@D+2"], "x": [2., -2.],
        "decision_timestamp": pd.Timestamp("2026-09-08T07:00Z"),
        "information_available_at": pd.Timestamp("2026-09-08T07:00Z")})
    frames = current.assign(raw_probability=.42, calibrated_probability=.42,
                            model_status="RESEARCH_NOT_PROMOTED")
    failed = {"forecasts": frames, "report": {"promotion_gate": {"status": "RESEARCH_NOT_PROMOTED"}}}
    retained, outputs = retain_champion(failed, champion=champion, current=current, run=run, group="1d",
                                        frozen_at="2026-09-08T09:30Z")
    expected = champion["payload"]["estimator"].predict_proba(current)[:, 1]
    np.testing.assert_allclose(retained["forecasts"].calibrated_probability, expected)
    assert retained["forecasts"].model_status.tolist() == ["PROMOTED", "RESEARCH_NO_TARGET_HISTORY"]
    assert retained["forecasts"].decision_timestamp.equals(current.decision_timestamp)
    assert retained["report"]["assessment"] == champion["report"]["assessment"]
    assert retained["report"]["deployment"]["source_run"].endswith(source_run.name)
    assert (run / "models/1d/champion-training-cohort.parquet").read_bytes() == (source_run / "training-cohort-1d.parquet").read_bytes()
    assert challenger_model.read_bytes() == b"untouched challenger"
    assert json.loads((run / "models/1d/challenger-report.json").read_text()) == failed["report"]
    assert set(outputs) == {"models/1d/champion.joblib", "models/1d/champion-training-cohort.parquet",
                            "models/1d/champion-source-reports.json", "models/1d/challenger-report.json"}
    # A newly completed native publication binds both artifacts and preserves
    # the champion's actual cohort even when a later retention reads this run.
    pd.DataFrame({"x": [999.], "target": [0]}).to_parquet(run / "training-cohort-1d.parquet", index=False)
    (run / "model-reports.json").write_text(json.dumps({"1d": retained["report"]}))
    config = json.loads((source_run / "manifest.json").read_text())["configuration"]
    write_manifest(run, run_timestamp=pd.Timestamp("2026-09-08T09:30Z"), input_files=champion["files"],
        output_files=(*outputs, "models/1d/model.joblib", "model-reports.json", "training-cohort-1d.parquet"),
        configuration=config)
    _publish_gameplan(tmp_path, run=run, action_date=DAY, published_at=pd.Timestamp("2026-09-08T09:30Z"),
                      source_loop_b="fixture", source_strategy=None)
    subsequent = find(tmp_path)
    assert subsequent["run"] == run
    assert subsequent["cohort_path"] == run / "models/1d/champion-training-cohort.parquet"
    assert subsequent["cohort_path"].read_bytes() == champion["cohort_path"].read_bytes()


def test_retained_prediction_rejects_future_current_inputs(tmp_path):
    publish(tmp_path)
    current = pd.DataFrame({"x": [1.], "symbol": ["COST"], "route": ["1d@D+1"],
                           "decision_timestamp": ["2026-09-08T12:00Z"], "information_available_at": ["2026-09-08T12:00Z"]})
    with pytest.raises(RuntimeError, match="future information"):
        retain_champion({}, champion=find(tmp_path), current=current, run=tmp_path / "unused", group="1d",
                         frozen_at="2026-09-08T09:30Z")
