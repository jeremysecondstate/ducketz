import copy
import json

import joblib
import numpy as np
import pandas as pd
import pytest

from gameplan_stats_fixture import write_review
from ml.gameplan_model_feedback import (
    REVIEW_VERSION, feedback_review_schema, load_feedback_review, prepare_feedback,
    save_feedback_review, validate_candidates,
)
from ml.nightly_gameplan import _fit_group_model, _feedback_estimator


NOW = "2026-09-12T06:00Z"


def proposal(run, candidates=None):
    diagnostic = json.loads((run / "diagnostics.json").read_text())
    groups = {group: {"decision": "KEEP_CURRENT", "rationale": "No justified change from limited evidence.", "candidates": []}
              for group in ("1h", "4h", "1d", "1w")}
    if candidates:
        groups["1h"].update(decision="EVALUATE_CANDIDATES", candidates=candidates,
                             rationale="Compare stronger regularization on chronological development data.")
    return {"schema_version": REVIEW_VERSION, "stats_receipt_sha256": diagnostic["stats"]["receipt_sha256"],
            "reviewed_by": "fixture reviewer", "groups": groups}


def test_feedback_preserves_ui_metrics_and_requires_explicit_review(tmp_path):
    stats = write_review(tmp_path)
    original = (stats / "forecast-results.parquet").read_bytes()
    run = prepare_feedback(tmp_path, review_run=stats, now=NOW)
    diagnosis = json.loads((run / "diagnostics.json").read_text())
    assert diagnosis["status"] == "AWAITING_MODEL_REVIEW"
    assert diagnosis["groups"]["1h"]["evaluated"] == 4
    assert diagnosis["groups"]["1h"]["scored"] == 3
    assert diagnosis["groups"]["1h"]["brier"] == pytest.approx((.04 + .64 + .25 + .01) / 4)
    assert diagnosis["groups"]["1h"]["pending"] == 1
    assert diagnosis["groups"]["1h"]["awaiting_data"] == 1
    assert not (run / "reviewed-proposal.json").exists()
    path = save_feedback_review(tmp_path, run, proposal(run), now=NOW)
    saved = load_feedback_review(tmp_path, path, as_of=NOW)
    assert saved["status"] == "REVIEWED"
    assert save_feedback_review(tmp_path, run, proposal(run), now=NOW) == path
    assert (stats / "forecast-results.parquet").read_bytes() == original


def test_stats_republication_code_change_and_future_reviews_require_new_review(tmp_path, monkeypatch):
    stats = write_review(tmp_path)
    run = prepare_feedback(tmp_path, now=NOW)
    path = save_feedback_review(tmp_path, run, proposal(run), now=NOW)
    with pytest.raises(ValueError, match="before training"):
        load_feedback_review(tmp_path, path, as_of="2026-09-12T05:59Z")
    with pytest.raises(ValueError, match="probability targets"):
        load_feedback_review(tmp_path, path, probability_target="raw-price-direction-v1")
    with monkeypatch.context() as patch:
        patch.setattr("ml.gameplan_model_feedback._code_binding", lambda: {"changed": "new"})
        with pytest.raises(ValueError, match="implementation changed"):
            load_feedback_review(tmp_path, path)
    write_review(tmp_path, version="02")
    with pytest.raises(ValueError, match="implementation changed"):
        load_feedback_review(tmp_path, path)
    with pytest.raises(ValueError, match="exact newest"):
        prepare_feedback(tmp_path, review_run=stats, now=NOW)


@pytest.mark.parametrize("candidate", [
    {"family": "tree", "parameters": {"max_iter": True}},
    {"family": "tree", "parameters": {"max_iter": 9999}},
    {"family": "tree", "parameters": {"promotion_threshold": 0}},
    {"family": "tree", "parameters": {"learning_rate": float("nan")}},
    {"family": "neural", "parameters": {"hidden_layer_sizes": [256, 256, 256]}},
    {"family": "neural", "parameters": {"hidden_layer_sizes": [8.0]}},
    {"family": "unknown", "parameters": {"C": 1}},
])
def test_unbounded_or_gate_altering_candidates_rejected(candidate):
    with pytest.raises(ValueError):
        validate_candidates([candidate])


def test_schema_and_review_binding_are_strict(tmp_path):
    write_review(tmp_path)
    run = prepare_feedback(tmp_path, now=NOW)
    clean = proposal(run)
    for change in ({"stats_receipt_sha256": "0" * 64}, {"extra": True}, {"groups": {}}, {"reviewed_by": " "}):
        with pytest.raises(ValueError):
            save_feedback_review(tmp_path, run, {**clean, **change}, now=NOW)
    changed = copy.deepcopy(clean)
    changed["groups"]["1h"]["decision"] = "EVALUATE_CANDIDATES"
    with pytest.raises(ValueError, match="disagree"):
        save_feedback_review(tmp_path, run, changed, now=NOW)
    assert feedback_review_schema()["additionalProperties"] is False


def test_verified_no_history_baseline_requires_explicit_target_and_keep_current(tmp_path):
    from ml.gameplan_actuals_review import publish_completed_session_review
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    stats = publish_completed_session_review(tmp_path, action_date="2026-09-11",
        clock=lambda: pd.Timestamp(NOW), price_loader=lambda *args, **kwargs: pytest.fail("No forecast to fetch"))
    original = (stats / "forecast-results.parquet").read_bytes()
    with pytest.raises(ValueError, match="explicit intended"):
        prepare_feedback(tmp_path, now=NOW)
    run = prepare_feedback(tmp_path, now=NOW, probability_target=RAW_DIRECTION_TARGET)
    diagnosis = json.loads((run / "diagnostics.json").read_text())
    assert diagnosis["stats"]["baseline_no_saved_predictions"] is True
    assert diagnosis["stats"]["probability_target_contract"] is None
    assert diagnosis["intended_probability_target_contract"] == RAW_DIRECTION_TARGET
    assert all(metrics["evaluated"] == 0 and metrics["brier"] is None for metrics in diagnosis["groups"].values())
    with pytest.raises(ValueError, match="no completed observations"):
        save_feedback_review(tmp_path, run, proposal(run, [{"family": "logistic", "parameters": {"C": .1}}]), now=NOW)
    saved = save_feedback_review(tmp_path, run, proposal(run), now=NOW)
    loaded = load_feedback_review(tmp_path, saved, probability_target=RAW_DIRECTION_TARGET, as_of=NOW)
    assert loaded["intended_probability_target_contract"] == RAW_DIRECTION_TARGET
    assert (stats / "forecast-results.parquet").read_bytes() == original


def test_real_legacy_forecasts_cannot_be_relabelled_as_no_history(tmp_path):
    from ml.artifacts import file_checksum, write_manifest
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    run = write_review(tmp_path)
    with pytest.raises(ValueError, match="probability targets differ"):
        prepare_feedback(tmp_path, now=NOW, probability_target=RAW_DIRECTION_TARGET)
    report = json.loads((run / "report.json").read_text())
    report["coverage_status"] = "NO_SAVED_INDEPENDENT_GAMEPLAN"
    (run / "report.json").write_text(json.dumps(report))
    manifest = json.loads((run / "manifest.json").read_text())
    write_manifest(run, run_timestamp=NOW, input_files=[], output_files=list(manifest["output_files"]),
                   configuration=manifest["configuration"])
    receipt = json.loads((run / "receipt.json").read_text())
    receipt["manifest_sha256"] = file_checksum(run / "manifest.json")
    (run / "receipt.json").write_text(json.dumps(receipt))
    pointer_path = tmp_path / "ml/gameplan-actuals-review-latest/run.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["current"]["receipt_sha256"] = file_checksum(run / "receipt.json")
    pointer_path.write_text(json.dumps(pointer))
    with pytest.raises(ValueError, match="no saved source or forecast rows"):
        prepare_feedback(tmp_path, now=NOW, probability_target=RAW_DIRECTION_TARGET)


def test_neural_architecture_override_reaches_estimator_without_fitting():
    estimator = _feedback_estimator({"family": "neural", "parameters": {
        "hidden_layer_sizes": [32, 16], "alpha": .1}}, ("x",), ("symbol", "route"))
    assert estimator.named_steps["classifier"].hidden_layer_sizes == (32, 16)
    assert estimator.named_steps["classifier"].alpha == .1


class FixtureEstimator:
    """No real fitting: C=10 has known fixture signal; other candidates are flat."""
    def __init__(self):
        self.parameters = {}

    def get_params(self, **kwargs):
        return self.parameters

    def set_params(self, **kwargs):
        self.parameters.update(kwargs)
        return self

    def fit(self, features, target, **kwargs):
        return self

    def predict_proba(self, features):
        p = (np.where(features["mr__x"].to_numpy() > .5, .9, .1)
             if self.parameters.get("classifier__C") == 10 else np.full(len(features), .5))
        return np.column_stack([1 - p, p])


def fixture_rows():
    starts = pd.date_range("2026-01-01", periods=120, freq="D", tz="UTC")
    targets = np.arange(120) % 2
    return pd.DataFrame({"decision_timestamp": starts, "information_available_at": starts,
        "target_window_start": starts + pd.Timedelta(hours=1), "target_window_end": starts + pd.Timedelta(hours=2),
        "target": targets, "symbol": "AAPL", "model_group": "1h", "route": "1h@04:00",
        "forecast_anchor_local": "04:00", "target_semantics": "independent_test", "trading_hours": 1.,
        "target_contract_version": "independent-stock-targets-v1", "mr__x": targets.astype(float)})


def test_reviewed_candidate_is_fitted_selected_and_frozen_without_assessment_selection(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.nightly_gameplan._estimator", lambda *args: FixtureEstimator())
    samples = fixture_rows()
    results = []
    for reverse in (False, True):
        altered = samples.copy()
        if reverse:
            altered.loc[altered.index[-15:], "target"] = 1 - altered.loc[altered.index[-15:], "target"]
        result = _fit_group_model(altered, current=samples.tail(2), feature_columns=("mr__x",), group="1h",
            model_directory=tmp_path / str(reverse) / "models/1h", trained_at=pd.Timestamp(NOW),
            feedback_candidates=[{"family": "logistic", "parameters": {"C": 10}}],
            feedback_context={"stats_receipt": "fixture"})
        results.append(result)
    first, changed = [item["report"] for item in results]
    assert first["selected_family"] == changed["selected_family"] == "feedback-logistic-1"
    assert first["selection_metrics"] == changed["selection_metrics"]
    assert first["promotion_gate"]["status"] == "PROMOTED"
    assert changed["promotion_gate"]["status"] == "RESEARCH_NOT_PROMOTED"
    assert first["model_feedback"]["candidate_improved_development"] is True
    assert first["model_feedback"]["assessment_used_for_selection"] is False
    model = joblib.load(tmp_path / "False/models/1h/model.joblib")
    assert model["estimator"].parameters["classifier__C"] == 10
    assert model["model_feedback"] == first["model_feedback"]


def test_non_improving_candidate_cannot_replace_defaults_on_tie(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.nightly_gameplan._estimator", lambda *args: FixtureEstimator())
    samples = fixture_rows()
    result = _fit_group_model(samples, current=samples.tail(2), feature_columns=("mr__x",), group="1h",
        model_directory=tmp_path / "models/1h", trained_at=pd.Timestamp(NOW),
        feedback_candidates=[{"family": "logistic", "parameters": {"C": .1}}])
    assert result["report"]["selected_family"] == "hist-gradient"
    assert result["report"]["model_feedback"]["candidate_improved_development"] is False


@pytest.mark.parametrize("candidate_c", [.1, 10])
def test_regressing_or_equal_candidate_keeps_accepted_specification(tmp_path, monkeypatch, candidate_c):
    monkeypatch.setattr("ml.nightly_gameplan._estimator", lambda *args: FixtureEstimator())
    samples = fixture_rows()
    incumbent = {"family": "logistic", "parameters": {"C": 10}}
    result = _fit_group_model(samples, current=samples.tail(2), feature_columns=("mr__x",), group="1h",
        model_directory=tmp_path / "models/1h", trained_at=pd.Timestamp(NOW),
        feedback_candidates=[{"family": "logistic", "parameters": {"C": candidate_c}}],
        incumbent_specification=incumbent)
    assert result["report"]["selected_family"] == "accepted-logistic"
    assert result["report"]["model_feedback"]["selected_specification"] == incumbent
    assert result["report"]["model_feedback"]["candidate_improved_development"] is False
    assert result["report"]["promotion_gate"]["status"] == "PROMOTED"


def test_frozen_feedback_survives_display_replacement_but_training_stays_latest(tmp_path):
    from app.ui.gameplan_stats_data import load_gameplan_stats
    from ml.artifacts import file_checksum
    stats = write_review(tmp_path)
    run = prepare_feedback(tmp_path, now=NOW)
    path = save_feedback_review(tmp_path, run, proposal(run), now=NOW)
    expected = load_feedback_review(tmp_path, path)
    write_review(tmp_path, version="02")
    assert load_feedback_review(tmp_path, path, require_latest_stats=False) == expected
    original = load_gameplan_stats(tmp_path, run_directory=stats,
                                  expected_receipt_sha256=file_checksum(stats / "receipt.json"))
    assert original.run_directory == stats
    with pytest.raises(ValueError, match="implementation changed"):
        load_feedback_review(tmp_path, path)
    with pytest.raises(ValueError, match="receipt"):
        load_gameplan_stats(tmp_path, run_directory=stats, expected_receipt_sha256="0" * 64)
    with pytest.raises(ValueError, match="saved run"):
        load_gameplan_stats(tmp_path, run_directory=tmp_path,
                            expected_receipt_sha256=file_checksum(stats / "receipt.json"))
    with pytest.raises(ValueError, match="exact receipt hash"):
        load_gameplan_stats(tmp_path, run_directory=stats)
    (stats / "forecast-results.parquet").write_bytes(b"changed original evidence")
    with pytest.raises((ValueError, RuntimeError), match="verify|checksum|changed|mismatch"):
        load_feedback_review(tmp_path, path, require_latest_stats=False)
