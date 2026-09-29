"""Compact CPU sequence candidates for offline Hyperliquid research only.

Callers own chronological windows, labels, and fitting-only preprocessing. This
module never reads datasets, writes artifacts, publishes models, or receives
assessment outcomes. It uses a fixed epoch budget rather than selecting epochs
on the calibration block. PyTorch is imported only when training/predicting.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading
from time import perf_counter
from typing import Any

import numpy as np


_TORCH_RUNTIME_LOCK = threading.RLock()
MODEL_NAMES = ("cnn", "gru", "cnn_gru")
CHANNELS = 16
HIDDEN_SIZE = 16
LEARNING_RATE = 0.001


def _positive_integer(name: str, value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def _features(values: np.ndarray, *, name: str, shape: tuple[int, int] | None = None,
              allow_empty: bool = False) -> np.ndarray:
    if not isinstance(values, np.ndarray) or values.ndim != 3:
        raise ValueError(f"{name} must be a numpy array with shape [rows, timesteps, features].")
    if values.shape[1] < 1 or values.shape[2] < 1 or (not allow_empty and values.shape[0] < 1):
        raise ValueError(f"{name} needs nonempty row, timestep and feature dimensions.")
    if shape is not None and values.shape[1:] != shape:
        raise ValueError(f"{name} timestep/feature dimensions must match fitting data {shape}.")
    if values.dtype.kind not in "fiu":
        raise ValueError(f"{name} must contain numeric real values.")
    with np.errstate(over="ignore", invalid="ignore"):
        converted = np.ascontiguousarray(values, dtype=np.float32)
    if not np.isfinite(converted).all():
        raise ValueError(f"{name} must be finite and representable as float32.")
    # torch.from_numpy cannot safely expose a read-only NumPy buffer. The
    # ordinary writable, contiguous float32 path retains the caller's cache.
    return converted if converted.flags.writeable else converted.copy()


@contextmanager
def _torch_runtime(threads: int, *, seed: int | None = None):
    import torch

    # Intra-op thread count and CPU RNG are process-global. Serialize our own
    # use and always restore the caller's state, including exceptional exits.
    # Do not set inter-op threads: PyTorch does not allow resetting that pool.
    with _TORCH_RUNTIME_LOCK:
        previous_threads = torch.get_num_threads()
        try:
            torch.set_num_threads(threads)
            if seed is None:
                yield torch
            else:
                with torch.random.fork_rng(devices=[]):
                    torch.random.default_generator.manual_seed(seed)
                    yield torch
        finally:
            torch.set_num_threads(previous_threads)


def _build_network(torch: Any, name: str, features: int):
    nn = torch.nn
    layers = {}
    if name in ("cnn", "cnn_gru"):
        conv_layers = [nn.ConstantPad1d((2, 0), 0), nn.Conv1d(features, CHANNELS, 3), nn.ReLU()]
        if name == "cnn":
            conv_layers.extend([
                nn.ConstantPad1d((4, 0), 0),
                nn.Conv1d(CHANNELS, CHANNELS, 3, dilation=2), nn.ReLU(),
            ])
        layers["conv"] = nn.Sequential(*conv_layers)
    if name in ("gru", "cnn_gru"):
        layers["gru"] = nn.GRU(features if name == "gru" else CHANNELS, HIDDEN_SIZE,
                               batch_first=True)
    layers["head"] = nn.Linear(CHANNELS if name == "cnn" else HIDDEN_SIZE, 1)
    return nn.ModuleDict(layers).cpu().float()


def _logits(network: Any, name: str, values: Any):
    if name in ("cnn", "cnn_gru"):
        values = network["conv"](values.transpose(1, 2)).transpose(1, 2)
    if name in ("gru", "cnn_gru"):
        _, hidden = network["gru"](values)
        representation = hidden[-1]
    else:
        representation = values.mean(dim=1)
    return network["head"](representation).squeeze(-1)


@dataclass
class SequencePredictor:
    """CPU-only binary estimator; input windows must match its training schema."""

    network: Any
    architecture: str
    window_shape: tuple[int, int]
    threads: int = 2
    batch_size: int = 256

    @property
    def classes_(self) -> np.ndarray:
        return np.array([0, 1], dtype=np.int64)

    def predict_proba(self, values: np.ndarray) -> np.ndarray:
        array = _features(values, name="prediction input", shape=self.window_shape, allow_empty=True)
        if len(array) == 0:
            return np.empty((0, 2), dtype=np.float64)
        with _torch_runtime(self.threads) as torch:
            self.network.eval()
            probabilities = []
            with torch.inference_mode():
                for start in range(0, len(array), self.batch_size):
                    batch = torch.from_numpy(array[start:start + self.batch_size])
                    logits = _logits(self.network, self.architecture, batch)
                    if not torch.isfinite(logits).all().item():
                        raise RuntimeError(f"{self.architecture} produced nonfinite prediction logits.")
                    probabilities.append(torch.sigmoid(logits).cpu().numpy())
        positive = np.concatenate(probabilities).astype(np.float64)
        return np.column_stack((1.0 - positive, positive))


def fit_sequence_models(
    x_fit: np.ndarray,
    y_fit: np.ndarray,
    x_calibration: np.ndarray,
    *,
    seed: int = 42,
    threads: int = 2,
    epochs: int = 8,
    batch_size: int = 128,
) -> dict[str, dict[str, Any]]:
    """Fit CNN, GRU and CNN+GRU independently, returning raw calibration scores.

    Input shape is [rows, past timesteps, features]. Labels must be a 1-D binary
    array containing both classes. Calibration features are predicted only;
    they never affect updates or epoch selection. Training is sequential with
    fixed order, seed and budget; no accelerator, worker pool or checkpoint IO.
    """
    for name, value in (("threads", threads), ("epochs", epochs), ("batch_size", batch_size)):
        _positive_integer(name, value)
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be a uint32 integer.")
    fit = _features(x_fit, name="x_fit")
    calibration = _features(x_calibration, name="x_calibration", shape=fit.shape[1:])
    if not isinstance(y_fit, np.ndarray) or y_fit.ndim != 1 or len(y_fit) != len(fit):
        raise ValueError("y_fit must be a 1-D numpy array with one label per fitting row.")
    if y_fit.dtype.kind not in "fiub" or not np.isfinite(y_fit).all() or set(np.unique(y_fit)) != {0, 1}:
        raise ValueError("y_fit must contain both binary classes 0 and 1 only.")
    labels = np.ascontiguousarray(y_fit, dtype=np.float32)
    if not labels.flags.writeable:
        labels = labels.copy()
    outputs = {}
    for name in MODEL_NAMES:
        started = perf_counter()
        with _torch_runtime(threads, seed=seed) as torch:
            network = _build_network(torch, name, fit.shape[2])
            optimizer = torch.optim.Adam(network.parameters(), lr=LEARNING_RATE)
            criterion = torch.nn.BCEWithLogitsLoss()
            tensor_features, tensor_labels = torch.from_numpy(fit), torch.from_numpy(labels)
            history = []
            network.train()
            for _ in range(epochs):
                total_loss = 0.0
                for start in range(0, len(fit), batch_size):
                    stop = min(start + batch_size, len(fit))
                    optimizer.zero_grad(set_to_none=True)
                    loss = criterion(_logits(network, name, tensor_features[start:stop]), tensor_labels[start:stop])
                    if not torch.isfinite(loss).item():
                        raise RuntimeError(f"{name} produced nonfinite training loss.")
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(network.parameters(), 1.0, error_if_nonfinite=True)
                    optimizer.step()
                    total_loss += float(loss.detach()) * (stop - start)
                history.append(total_loss / len(fit))
            network.eval()
            if not all(torch.isfinite(value).all().item() for value in network.parameters()):
                raise RuntimeError(f"{name} produced nonfinite model weights.")
            parameter_count = sum(value.numel() for value in network.parameters())
        fit_seconds = perf_counter() - started
        predictor = SequencePredictor(network, name, fit.shape[1:], threads=threads)
        calibration_started = perf_counter()
        raw_calibration = predictor.predict_proba(calibration)[:, 1]
        outputs[name] = {
            "model": predictor,
            "fit_seconds": fit_seconds,
            "parameter_count": parameter_count,
            "raw_calibration_probability": raw_calibration,
            "calibration_predict_seconds": perf_counter() - calibration_started,
            "training_loss_by_epoch": history,
            "config": {
                "architecture": name, "window_shape": list(fit.shape[1:]),
                "seed": seed, "threads": threads, "epochs": epochs, "batch_size": batch_size,
                "prediction_batch_size": predictor.batch_size, "learning_rate": LEARNING_RATE,
                "channels": CHANNELS if name != "gru" else None,
                "hidden_size": HIDDEN_SIZE if name != "cnn" else None,
                "device": "cpu", "dtype": "float32", "loss": "BCEWithLogitsLoss",
                "gradient_clip_norm": 1.0, "shuffle": False,
                "calibration_used_for_fitting": False,
            },
        }
    return outputs
