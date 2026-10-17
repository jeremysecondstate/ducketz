"""Offline causal fixtures: no providers, production training, orders or tasks."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import numpy as np
import pytest

from ml.intraday.full_model_selection import (
    SelectionConfig, _fit, _fit_partitions, default_candidates, probability_metrics, select_models,
)
from ml.intraday.models import (
    ModelSpec, UnsupportedModelError, configured_models, fit_calibrated_estimator, registry_status,
)
from ml.intraday.prediction import ContractError, TARGET_VERSION, instant


def fixture_dataset(rows=1000, seed=27, signal=True):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(rows, 5))
    y = (X[:, 0] > 0).astype(np.uint8) if signal else rng.integers(0, 2, size=rows, dtype=np.uint8)
    if signal:
        flip = rng.random(rows) < .08
        y[flip] = 1-y[flip]
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    references = tuple(start+timedelta(minutes=15*i) for i in range(rows))
    closes = tuple(ref+timedelta(minutes=45) for ref in references)
    available = tuple(end+timedelta(seconds=5) for end in closes)
    return SimpleNamespace(X=X, y=y, reference_at=references, target_close_at=closes,
                           label_available_at=available, manifest={"target_version": TARGET_VERSION,
                           "training_cutoff": (available[-1]+timedelta(days=1)).isoformat(),
                           "feature_version": "offline-fixture", "source_history": {"kind": "offline"}})


def candidates():
    return (ModelSpec("logistic_fixture", family="logistic", parameters={"C": .1}),)


def test_signal_is_selected_with_causal_purges_disjoint_calibration_and_portable_specs():
    dataset = fixture_dataset()
    before_X, before_y = dataset.X.copy(), dataset.y.copy()
    result = select_models(dataset, candidates=candidates())
    assert result.report["status"] == "selected"
    assert result.selected_models[0].probability_calibration == "sigmoid"
    assert configured_models(json.loads(json.dumps(result.report["selected_models"]))) == result.selected_models
    assert result.report["holdout"]["ensemble"]["brier"] < result.report["holdout"]["baseline"]["brier"]
    for fold in result.report["folds"]:
        train, calibration, validation = (fold[key] for key in ("training", "calibration", "validation"))
        assert instant(train["latest_target_close"]) <= instant(calibration["first_reference"])
        assert instant(train["latest_label_available"]) <= instant(calibration["first_reference"])
        assert instant(calibration["latest_target_close"]) <= instant(validation["first_reference"])
        assert instant(calibration["latest_label_available"]) <= instant(validation["first_reference"])
    assert instant(result.report["search"]["latest_label_available"]) <= instant(result.holdout_start)
    np.testing.assert_array_equal(dataset.X, before_X)
    np.testing.assert_array_equal(dataset.y, before_y)


def test_holdout_labels_and_features_cannot_change_ranking_or_selected_membership():
    first = fixture_dataset()
    initial = select_models(first, candidates=candidates())
    assert initial.selected_models
    second = fixture_dataset()
    heldout = np.asarray([ref >= instant(initial.holdout_start) for ref in second.reference_at])
    second.y[heldout] = 1-second.y[heldout]
    second.X[heldout, 0] *= .2
    changed = select_models(second, candidates=candidates(), previous_holdout_start=initial.holdout_start)
    assert changed.selected_models == initial.selected_models
    assert changed.report["search_sha256"] == initial.report["search_sha256"]
    assert changed.report["candidates"][0]["summary"] == initial.report["candidates"][0]["summary"]
    assert changed.report["holdout"]["ensemble"]["brier"] > initial.report["holdout"]["ensemble"]["brier"]+.1
    assert changed.report["status"] == "selected"
    assert changed.report["holdout"]["purpose"] == "report_only_no_selection_or_activation_gate"


def test_frozen_holdout_never_slides_inspected_rows_into_new_search():
    first = fixture_dataset(1000)
    initial = select_models(first, candidates=candidates())
    expanded = fixture_dataset(1080)
    # Preserve every original input byte, only append genuinely later records.
    expanded.X[:1000], expanded.y[:1000] = first.X, first.y
    next_wake = select_models(expanded, candidates=candidates(), previous_holdout_start=initial.holdout_start)
    assert next_wake.holdout_start == initial.holdout_start
    assert next_wake.report["search_sha256"] == initial.report["search_sha256"]
    assert next_wake.selected_models == initial.selected_models
    assert next_wake.report["holdout_rows"] == initial.report["holdout_rows"]+80


def test_late_receipt_rows_are_purged_even_after_target_has_closed():
    dataset = fixture_dataset()
    clean = select_models(dataset, candidates=candidates())
    first_calibration = instant(clean.report["folds"][0]["calibration"]["first_reference"])
    location = next(i for i, ref in enumerate(dataset.reference_at) if ref == first_calibration)-10
    availability = list(dataset.label_available_at)
    availability[location] = first_calibration+timedelta(minutes=1)
    dataset.label_available_at = tuple(availability)
    delayed = select_models(dataset, candidates=candidates())
    assert delayed.report["folds"][0]["training"]["rows"] == clean.report["folds"][0]["training"]["rows"]-1
    assert instant(delayed.report["folds"][0]["training"]["latest_label_available"]) <= first_calibration


def test_real_unregularized_tree_overfit_is_rejected_and_logged():
    dataset = fixture_dataset(signal=False)
    tree = ModelSpec("memorizing_tree", family="random_forest", parameters={
        "n_estimators": 1, "max_depth": None, "min_samples_leaf": 1, "bootstrap": False})
    result = select_models(dataset, candidates=(tree,))
    assert not result.selected_models
    record = result.report["candidates"][0]
    assert record["status"] == "rejected"
    assert record["selection_reason"] == "validation_rejected" and not record["selected"]
    assert "training_validation_brier_gap" in record["rejection_reasons"]
    assert all(fold["raw_training"]["brier"] == 0 for fold in record["folds"])
    assert record["summary"]["maximum_brier_generalization_gap"] > .2


def test_no_feature_signal_does_not_force_baseline_or_stale_model_substitution():
    dataset = fixture_dataset(signal=False)
    dataset.X[:] = 0
    result = select_models(dataset, candidates=candidates())
    assert result.report["status"] == "no_supported_candidate"
    assert result.selected_models == ()
    assert result.report["candidates"][0]["status"] == "rejected"
    assert result.report["holdout"]["models"] == []


def test_candidate_failures_remain_explicit_while_other_candidates_continue():
    bad = ModelSpec("unimplemented_sequence", family="transformer")
    result = select_models(fixture_dataset(), candidates=(bad, *candidates()))
    assert result.selected_models
    failed = result.report["candidates"][0]
    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "UnsupportedModelError"
    assert failed["catalog_status"] == failed["selection_reason"] == "adapter_not_implemented"
    assert not failed["selected"]
    assert len(result.report["candidates"]) == 2
    json.dumps(result.report, allow_nan=False)


def test_multiple_hyperparameters_compete_but_same_family_is_not_counted_as_independent_votes():
    specs = (ModelSpec("logistic_a", family="logistic", parameters={"C": .05}),
             ModelSpec("logistic_b", family="logistic", parameters={"C": .3}))
    result = select_models(fixture_dataset(), candidates=specs)
    assert len(result.report["candidates"]) == 2
    assert len(result.selected_models) == 1


@pytest.mark.parametrize("training_max_rows", [None, 180])
def test_selection_scores_the_same_capped_calibrated_fit_as_live(training_max_rows):
    dataset = fixture_dataset()
    spec = replace(candidates()[0], training_max_rows=training_max_rows)
    result = select_models(dataset, candidates=(spec,), config=replace(SelectionConfig(),
        max_brier_gap=1., max_fold_brier_std=1., max_fold_baseline_regression=1.))
    assert result.selected_models
    boundary = instant(result.holdout_start)
    search = np.array([i for i, reference in enumerate(dataset.reference_at)
                       if reference < boundary and dataset.target_close_at[i] <= boundary
                       and dataset.label_available_at[i] <= boundary])
    holdout = np.array([i for i, reference in enumerate(dataset.reference_at) if reference >= boundary])
    model, evidence = fit_calibrated_estimator(result.selected_models[0], dataset.X[search], dataset.y[search],
        reference_at=[dataset.reference_at[i] for i in search],
        target_close_at=[dataset.target_close_at[i] for i in search],
        label_available_at=[dataset.label_available_at[i] for i in search], training_cutoff=boundary)
    expected = probability_metrics(dataset.y[holdout], model.predict_proba(dataset.X[holdout])[:, 1])
    assert result.report["holdout"]["models"][0]["metrics"] == expected
    assert evidence["calibration"]["base_latest_label_available_at"] <= evidence["calibration"]["first_calibration_reference"]
    scored = result.report["holdout"]["models"][0]
    assert scored["training_partition"]["rows"] == evidence["calibration"]["base_rows"]
    assert scored["calibration_partition"]["rows"] == evidence["calibration"]["calibration_rows"]
    if training_max_rows:
        assert scored["training_partition"]["rows"] + scored["calibration_partition"]["rows"] <= training_max_rows


def test_too_small_candidate_history_cap_is_reported_without_expanding_live_history():
    limited = replace(candidates()[0], name="too_little_history", training_max_rows=40)
    result = select_models(fixture_dataset(), candidates=(limited, *candidates()))
    assert result.selected_models == (replace(candidates()[0], probability_calibration="sigmoid"),)
    record = result.report["candidates"][0]
    assert record["status"] == "failed"
    assert record["error"]["type"] == "ContractError"
    assert "Insufficient chronological calibration history" in record["error"]["message"]


@pytest.mark.parametrize("training_max_rows", [None, 180])
def test_chronological_calibration_fits_preprocessing_only_on_base_fit_rows(training_max_rows):
    dataset = fixture_dataset(300)
    pool = np.arange(263)
    train, calibration = _fit_partitions(pool, dataset.reference_at, dataset.target_close_at,
                                         dataset.label_available_at, SelectionConfig(), training_max_rows)
    dataset.X[calibration] += 1000
    spec = replace(candidates()[0], probability_calibration="sigmoid", training_max_rows=training_max_rows)
    model, _ = _fit(spec, dataset.X, dataset.y, train, calibration, pool, dataset.reference_at,
                    dataset.target_close_at, dataset.label_available_at, dataset.manifest["training_cutoff"])
    np.testing.assert_allclose(model.base.named_steps["scale"].mean_, dataset.X[train].mean(axis=0))
    p = model.predict_proba(dataset.X[263:])
    assert p.shape == (37, 2)
    assert np.isfinite(p).all()
    assert ((p >= 0) & (p <= 1)).all()
    np.testing.assert_allclose(p.sum(axis=1), 1)


def test_insufficient_history_is_honest_no_selection_and_not_an_exception():
    result = select_models(fixture_dataset(100), candidates=candidates())
    assert result.report["status"] == "insufficient_history"
    assert result.selected_models == ()
    assert "Insufficient" in result.report["reason"]
    assert result.report["candidate_coverage"]["attempted_variant_count"] == 0
    assert result.report["candidates"][0]["selection_reason"] == "insufficient_history"


def test_trimmed_history_cannot_move_a_retained_holdout_boundary():
    dataset = fixture_dataset(1000)
    old_boundary = dataset.reference_at[100].isoformat()
    result = select_models(dataset, candidates=candidates(), config=replace(SelectionConfig(), max_rows=500),
                           previous_holdout_start=old_boundary)
    assert result.holdout_start == old_boundary
    assert result.report["status"] == "insufficient_history"
    assert "Retained holdout boundary" in result.report["reason"]


def test_single_class_calibration_records_failure_without_a_constant_classifier():
    dataset = fixture_dataset()
    result = select_models(dataset, candidates=candidates())
    first = result.report["folds"][0]["calibration"]
    start, end = instant(first["first_reference"]), instant(first["last_reference"])
    indices = [i for i, ref in enumerate(dataset.reference_at) if start <= ref <= end]
    dataset.y[indices] = 0
    failed = select_models(dataset, candidates=candidates())
    assert not failed.selected_models
    record = failed.report["candidates"][0]
    assert record["status"] == "failed"
    assert "both target classes" in record["error"]["message"]


def test_overconfident_forecast_metrics_are_not_reported_as_accuracy():
    metrics = probability_metrics([0, 1, 0, 1], [1., 1., 1., 1.])
    assert metrics["mean_probability"] == 1
    assert metrics["balanced_accuracy"] == .5
    assert metrics["brier"] == .5
    assert metrics["saturated_probability_fraction"] == 1


@pytest.mark.parametrize("change", ["future_receipt", "wrong_target", "out_of_order", "infinite_feature", "wrong_target_version"])
def test_malformed_causal_inputs_are_rejected(change):
    dataset = fixture_dataset()
    if change == "future_receipt":
        dataset.label_available_at = (*dataset.label_available_at[:-1], instant(dataset.manifest["training_cutoff"])+timedelta(seconds=1))
    elif change == "wrong_target":
        dataset.target_close_at = (dataset.target_close_at[0]+timedelta(minutes=15), *dataset.target_close_at[1:])
    elif change == "out_of_order":
        dataset.reference_at = (dataset.reference_at[1], dataset.reference_at[0], *dataset.reference_at[2:])
    elif change == "infinite_feature":
        dataset.X[0, 0] = np.inf
    else:
        dataset.manifest["target_version"] = "other-label"
    with pytest.raises(ContractError):
        select_models(dataset, candidates=candidates())


@pytest.mark.parametrize("config", [replace(SelectionConfig(), folds=1), replace(SelectionConfig(), max_rows=True),
                                   replace(SelectionConfig(), max_brier_gap=np.nan)])
def test_invalid_selection_parameters_are_rejected(config):
    with pytest.raises(ContractError):
        select_models(fixture_dataset(), candidates=candidates(), config=config)


def test_default_grid_has_real_regularized_architectures_and_json_portability():
    specs = default_candidates()
    assert len(specs) == 21
    families = {spec.family for spec in specs}
    implemented = {item["family"] for item in registry_status() if item["status"] != "adapter_not_implemented"}
    assert len(families) == 16 and families == implemented
    assert SelectionConfig().max_selected == len(families)
    architectures = {tuple(spec.parameters["hidden_layer_sizes"]) for spec in specs if spec.family == "mlp"}
    assert len(architectures) == 3
    assert all(spec.probability_calibration == "sigmoid" for spec in specs)
    encoded = json.loads(json.dumps([asdict(spec) for spec in specs], allow_nan=False))
    assert len(configured_models(encoded)) == len(specs)


class _CatalogFixtureModel:
    """Deterministic signal predictions; no estimators or real training occur."""
    classes_ = np.array([0, 1])

    def __init__(self):
        self.base = self

    def predict_proba(self, X):
        probability = np.where(X[:, 0] > 0, .85, .15)
        return np.column_stack((1-probability, probability))


def _mock_catalog_fits(monkeypatch, *, unavailable=()):
    from ml.intraday import full_model_selection as model_selection
    catalog = registry_status()
    for item in catalog:
        if item["status"] != "adapter_not_implemented":
            item["status"] = "dependency_unavailable" if item["family"] in unavailable else "available"
    monkeypatch.setattr(model_selection, "registry_status", lambda: [dict(item) for item in catalog])
    attempts = []

    def fit(spec, *args):
        attempts.append(spec.name)
        if spec.family in unavailable:
            raise UnsupportedModelError(f"{spec.family} fixture dependency is unavailable")
        return _CatalogFixtureModel(), []

    monkeypatch.setattr(model_selection, "_fit", fit)
    return attempts


def test_full_catalog_attempts_omitted_families_and_records_unavailable_dependency(monkeypatch):
    attempts = _mock_catalog_fits(monkeypatch, unavailable={"catboost"})
    result = select_models(fixture_dataset())
    specs = default_candidates()
    assert {spec.name for spec in specs} <= set(attempts)
    assert len(result.selected_models) == 15  # All remaining genuine families qualify, including more than four.
    assert len({spec.family for spec in result.selected_models}) == 15
    coverage = result.report["candidate_coverage"]
    assert coverage["configured_variant_count"] == coverage["attempted_variant_count"] == 21
    assert coverage["implemented_family_count"] == coverage["configured_family_count"] == 16
    assert coverage["attempted_family_count"] == 16
    assert coverage["available_family_count"] == 15
    assert coverage["unconfigured_implemented_families"] == []
    assert coverage["dependency_unavailable_families"] == ["catboost"]
    records = {item["spec"]["family"]: item for item in result.report["candidates"]}
    assert records["catboost"]["status"] == "failed"
    assert records["catboost"]["selection_reason"] == "dependency_unavailable"
    assert records["catboost"]["error"]["type"] == "UnsupportedModelError"
    catalog = {item["family"]: item for item in result.report["model_catalog"]}
    assert catalog["catboost"]["configured"]
    assert len(coverage["adapter_not_implemented_families"]) == 14
    for family in coverage["adapter_not_implemented_families"]:
        assert catalog[family]["status"] == catalog[family]["nonselection_reason"] == "adapter_not_implemented"
        assert not catalog[family]["configured"] and catalog[family]["candidate_names"] == []
    assert result.report["holdout"]["purpose"] == "report_only_no_selection_or_activation_gate"
    json.dumps(result.report, allow_nan=False)


def test_duplicate_families_and_explicit_cap_have_separate_nonselection_reasons(monkeypatch):
    _mock_catalog_fits(monkeypatch)
    specs = (ModelSpec("a_logistic", family="logistic"), ModelSpec("b_logistic", family="logistic"),
             ModelSpec("c_nb", family="gaussian_nb"), ModelSpec("d_lda", family="lda"))
    result = select_models(fixture_dataset(), candidates=specs,
                           config=replace(SelectionConfig(), max_selected=2))
    records = {item["spec"]["name"]: item for item in result.report["candidates"]}
    assert [spec.name for spec in result.selected_models] == ["a_logistic", "c_nb"]
    assert records["a_logistic"]["selection_reason"] == "selected_validation_rank"
    assert records["c_nb"]["selected"] is True
    assert records["b_logistic"]["selection_reason"] == "duplicate_family"
    assert records["d_lda"]["selection_reason"] == "configured_cap"
    assert not records["b_logistic"]["selected"] and not records["d_lda"]["selected"]
    coverage = result.report["candidate_coverage"]
    assert coverage["configured_variant_count"] == coverage["attempted_variant_count"] == 4
    assert coverage["configured_family_count"] == coverage["attempted_family_count"] == 3
    assert "catboost" in coverage["unconfigured_implemented_families"]


def _live_selection_fixture(tmp_path, symbols):
    """Minimal local runtime fixture; trainer below never fits a real model."""
    import pandas as pd
    from ml.intraday.selected_config import VERSION
    reference = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)
    entry = {"models": [asdict(ModelSpec("selected_nb", family="gaussian_nb",
             parameters={"var_smoothing": .01}, probability_calibration="sigmoid"))],
             "selection_id": "night-a", "selected_at": (reference-timedelta(hours=1)).isoformat(),
             "feature_names": ["x"], "max_training_rows": 3}
    document = {"schema_version": VERSION, "actor": "Atlas", "source_dataset": "EQUS.MINI",
                "target_version": TARGET_VERSION, "symbols": {s: dict(entry) for s in symbols}}
    path = tmp_path/"model-supervision/active.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    histories = {symbol: pd.DataFrame([{"symbol": symbol, "opened_at": reference,
                "available_at": reference+timedelta(minutes=16), "availability_kind": "actual",
                "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100, "synthetic": False}])
                 for symbol in symbols}
    return reference, path, document, histories


def _live_dataset(frame, *, cutoff, reference_at, **kwargs):
    return SimpleNamespace(X=np.array([[0], [1], [0], [1]]), y=np.array([0, 1, 0, 1]),
        inference_X=np.array([[1]]), feature_names=("x",),
        label_available_at=[reference_at-timedelta(hours=1)]*4,
        reference_at=[reference_at-timedelta(hours=2)+i*timedelta(minutes=15) for i in range(4)],
        target_close_at=[reference_at-timedelta(hours=1, minutes=15)+i*timedelta(minutes=15) for i in range(4)],
        inference={"symbol": str(frame.symbol.iloc[0]), "reference_at": reference_at.isoformat(),
            "reference_open": 100, "information_available_at": (reference_at+timedelta(minutes=15)).isoformat(),
            "availability_kind": "actual"}, manifest={})


def _live_training(*args, config, **kwargs):
    return {"outputs": [{"name": config[0]["name"], "status": "success", "probability_not_down": .6}],
            "configured_count": 1, "successful_count": 1, "complete": True,
            "probability_not_down": .6, "training_seconds": 0., "inference_seconds": 0.}


def test_partial_recovery_keeps_original_selection_after_supervisor_publishes_new_config(tmp_path):
    from ml.intraday.data import AcquisitionPending
    from ml.intraday.runtime import ShadowRunner
    from ml.intraday.store import ShadowStore
    symbols = ("AAPL", "IONQ")
    reference, path, document, histories = _live_selection_fixture(tmp_path, symbols)
    missing, seen = {"IONQ"}, []
    class Incoming:
        def get(self, symbol):
            if symbol in missing:
                raise AcquisitionPending("offline_fixture_unavailable", 4)
            return histories[symbol]
    def train(*args, config, **kwargs):
        seen.append(config[0]["parameters"]["var_smoothing"])
        assert len(args[0]) == 3
        return _live_training(*args, config=config, **kwargs)
    now = [reference+timedelta(minutes=16)]
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    runner = ShadowRunner(store=store, pc="Atlas", symbols=symbols, trainer=train,
                          dataset_builder=_live_dataset, clock=lambda: now[0])
    first = runner.run_cycle(Incoming(), reference_at=reference, mode="live", experiment="manual")
    assert first["pending_recovery_symbols"] == ["IONQ"]
    original = json.dumps(first["forecasts"]["AAPL"], sort_keys=True)
    for entry in document["symbols"].values():
        entry["selection_id"] = "night-b"
        entry["models"][0]["parameters"]["var_smoothing"] = .1
        entry["max_training_rows"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")
    missing.clear()
    now[0] += timedelta(minutes=5)
    recovered = runner.run_cycle(Incoming(), reference_at=reference, mode="live", experiment="manual",
                                 retry_symbols=["IONQ"])
    assert recovered["cycle_id"] == first["cycle_id"]
    assert recovered["status"] == "completed"
    assert seen == [.01, .01]
    assert json.dumps(recovered["forecasts"]["AAPL"], sort_keys=True) == original
    assert recovered["forecasts"]["IONQ"]["model_selection_id"] == "night-a"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM cycles").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 2


def test_concurrent_callers_bind_first_selected_configuration_to_one_logical_cycle(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from ml.intraday import selected_config
    from ml.intraday.data import AcquisitionPending
    from ml.intraday.runtime import ShadowRunner
    from ml.intraday.store import ShadowStore
    reference, _, _, _ = _live_selection_fixture(tmp_path, ("AAPL",))
    observed_bindings = []
    class BindingStore(ShadowStore):
        def begin_cycle(self, cycle_id, payload):
            saved = super().begin_cycle(cycle_id, payload)
            observed_bindings.append(saved["configuration"]["selected_models_by_symbol"]["AAPL"]["selection_id"])
            return saved
    store = BindingStore(tmp_path/"shadow.sqlite3")
    thread_scope, barrier = local(), Barrier(2)
    entry = {"models": [asdict(ModelSpec("selected_nb", family="gaussian_nb", probability_calibration="sigmoid"))],
             "selected_at": (reference-timedelta(hours=1)).isoformat(), "feature_names": ["x"],
             "lookback_days": None, "synthetic_policy": "include", "max_training_rows": 3}
    def selection_view(*args, **kwargs):
        view = {"AAPL": {**entry, "selection_id": thread_scope.name}}
        barrier.wait(timeout=10)
        return view
    monkeypatch.setattr(selected_config, "load_selected_config", selection_view)
    class Missing:
        def get(self, symbol):
            raise AcquisitionPending("offline_concurrent_binding_fixture", 4)
    def invoke(name):
        thread_scope.name = name
        runner = ShadowRunner(store=store, pc="Atlas", symbols=("AAPL",), trainer=_live_training,
                              dataset_builder=_live_dataset, clock=lambda: reference+timedelta(minutes=16))
        return runner.run_cycle(Missing(), reference_at=reference, mode="live", experiment="manual")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ("night-a", "night-b")))
    assert results[0]["cycle_id"] == results[1]["cycle_id"]
    frozen = set(observed_bindings)
    assert len(observed_bindings) == 2
    assert len(frozen) == 1
    with store.connect() as db:
        count, payload = db.execute("SELECT count(*), payload FROM cycles").fetchone()
    assert count == 1
    assert json.loads(payload)["configuration"]["selected_models_by_symbol"]["AAPL"]["selection_id"] in frozen
