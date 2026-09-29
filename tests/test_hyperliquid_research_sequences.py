"""Research sequence candidates must stay deterministic and holdout-independent."""
from __future__ import annotations

import numpy as np
import pytest

from ml import hyperliquid_research_sequences as sequences


def inputs():
    rng = np.random.default_rng(731)
    values = rng.normal(size=(24, 8, 3)).astype(np.float32)
    labels = np.tile(np.array([0, 1], dtype=np.int8), 12)
    return values, labels, values[:7].copy()


def fit(**kwargs):
    values, labels, calibration = inputs()
    return sequences.fit_sequence_models(values, labels, calibration, epochs=1, threads=1, batch_size=8, **kwargs)


def test_all_candidates_are_compact_cpu_binary_predictors_and_leave_inputs_untouched():
    values, labels, calibration = inputs()
    original_values, original_labels = values.copy(), labels.copy()
    models = sequences.fit_sequence_models(values, labels, calibration, epochs=1, threads=1, batch_size=8)
    assert tuple(models) == sequences.MODEL_NAMES
    for name, result in models.items():
        predictor = result["model"]
        probabilities = predictor.predict_proba(calibration)
        assert probabilities.shape == (7, 2)
        assert np.isfinite(probabilities).all()
        assert (probabilities >= 0).all() and (probabilities <= 1).all()
        np.testing.assert_allclose(probabilities.sum(axis=1), 1.0)
        np.testing.assert_array_equal(probabilities[:, 1], result["raw_calibration_probability"])
        np.testing.assert_array_equal(predictor.classes_, [0, 1])
        assert 0 < result["parameter_count"] < 10_000
        assert result["fit_seconds"] > 0
        assert len(result["training_loss_by_epoch"]) == 1
        assert all(parameter.device.type == "cpu" for parameter in predictor.network.parameters())
        assert result["config"]["architecture"] == name
        assert result["config"]["calibration_used_for_fitting"] is False
    np.testing.assert_array_equal(values, original_values)
    np.testing.assert_array_equal(labels, original_labels)


def test_calibration_features_do_not_change_fit_and_seed_is_reproducible():
    values, labels, calibration = inputs()
    first = sequences.fit_sequence_models(values, labels, calibration, epochs=1, threads=1, batch_size=8)
    second = sequences.fit_sequence_models(values, labels, calibration + 20, epochs=1, threads=1, batch_size=8)
    for name in first:
        np.testing.assert_array_equal(first[name]["model"].predict_proba(values), second[name]["model"].predict_proba(values))


def test_thread_and_rng_state_restored_after_fit_and_prediction():
    import torch

    old_threads = torch.get_num_threads()
    rng_before = torch.random.get_rng_state().clone()
    models = fit()
    assert torch.get_num_threads() == old_threads
    assert torch.equal(torch.random.get_rng_state(), rng_before)
    models["gru"]["model"].predict_proba(inputs()[0])
    assert torch.get_num_threads() == old_threads
    assert torch.equal(torch.random.get_rng_state(), rng_before)


def test_thread_and_rng_state_restored_when_training_raises(monkeypatch):
    import torch

    old_threads = torch.get_num_threads()
    rng_before = torch.random.get_rng_state().clone()

    def fail(*args):
        torch.rand(3)
        raise RuntimeError("deliberate fitting failure")

    monkeypatch.setattr(sequences, "_build_network", fail)
    with pytest.raises(RuntimeError, match="deliberate fitting failure"):
        fit()
    assert torch.get_num_threads() == old_threads
    assert torch.equal(torch.random.get_rng_state(), rng_before)


def test_prediction_batches_empty_input_and_window_shape_validation():
    values, _, _ = inputs()
    predictor = fit()["cnn_gru"]["model"]
    full = predictor.predict_proba(values)
    predictor.batch_size = 5
    np.testing.assert_allclose(predictor.predict_proba(values), full, atol=1e-7)
    assert predictor.predict_proba(values[:0]).shape == (0, 2)
    with pytest.raises(ValueError, match="dimensions"):
        predictor.predict_proba(values[:, :-1])
    with pytest.raises(ValueError, match="finite"):
        predictor.predict_proba(np.full_like(values, np.nan))


@pytest.mark.parametrize("mutation", ["nan_fit", "inf_calibration", "calibration_shape", "flat_fit", "empty_fit", "nonbinary", "one_class", "label_shape", "label_count", "complex"])
def test_invalid_training_inputs_fail_before_training(mutation):
    values, labels, calibration = inputs()
    if mutation == "nan_fit":
        values[0, 0, 0] = np.nan
    elif mutation == "inf_calibration":
        calibration[0, 0, 0] = np.inf
    elif mutation == "calibration_shape":
        calibration = calibration[:, :, :2]
    elif mutation == "flat_fit":
        values = values[:, 0]
    elif mutation == "empty_fit":
        values, labels = values[:0], labels[:0]
    elif mutation == "nonbinary":
        labels[0] = 2
    elif mutation == "one_class":
        labels[:] = 1
    elif mutation == "label_shape":
        labels = labels[:, None]
    elif mutation == "label_count":
        labels = labels[:-1]
    elif mutation == "complex":
        values = values.astype(np.complex64)
    with pytest.raises(ValueError):
        sequences.fit_sequence_models(values, labels, calibration)


@pytest.mark.parametrize("name,value", [("seed", -1), ("seed", True), ("threads", 0), ("threads", 1.5), ("epochs", False), ("batch_size", 0)])
def test_invalid_runtime_parameters_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        sequences.fit_sequence_models(*inputs(), **{name: value})
