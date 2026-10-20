"""Exact snapshot projection and preprocessing clocks, without fitting or providers."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.intraday import runtime
from ml.intraday.prediction import digest, encoded
from ml.intraday.store import ShadowStore


def legacy_jsonable(value):
    """The established projection defines existing snapshot bytes and errors."""
    if isinstance(value, dict):
        return {str(k): legacy_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray, pd.Index)):
        return [legacy_jsonable(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def projection_cases():
    cases = []
    for dtype in (np.bool_, np.int8, np.uint8, np.int32, np.uint64,
                  np.float16, np.float32, np.float64, np.longdouble):
        array = np.asarray([[0, 1], [1, 0]], dtype=dtype)
        for label, value in (("contiguous", array), ("strided", array[:, ::2]),
                             ("transposed", array.T), ("empty", np.empty((0, 3), dtype=dtype))):
            cases.append(pytest.param(value, id=f"{np.dtype(dtype).name}-{label}"))
    cases.extend([
        pytest.param(np.asarray([0, 2**64 - 1], dtype=np.uint64), id="uint64-max"),
        pytest.param(np.asarray([-0.0, 0.0, 1e-300, 1e300]), id="signed-zero-and-extreme-floats"),
        pytest.param(np.asarray([np.nan, np.inf, -np.inf]), id="nonfinite-fallback"),
        pytest.param(np.asarray(3.5), id="zero-dimensional-error"),
        pytest.param(np.asarray([{"x": np.float32(1.5)},
                                 [np.int64(2), datetime(2026, 10, 20, tzinfo=timezone.utc)]],
                                dtype=object), id="nested-object-fallback"),
        pytest.param(np.asarray([1 + 2j]), id="complex-fallback"),
        pytest.param({"a": np.ones((2, 3)), "b": [float("nan"), np.float32(.25),
                      pd.Timestamp("2026-10-20T22:00:00Z")]}, id="nested-payload"),
    ])
    return cases


@pytest.mark.parametrize("value", projection_cases())
def test_projection_preserves_canonical_bytes_and_existing_errors(value):
    try:
        expected = encoded(legacy_jsonable(value))
    except (TypeError, ValueError) as error:
        with pytest.raises(type(error)) as actual:
            encoded(runtime._jsonable(value))
        assert str(actual.value) == str(error)
    else:
        assert encoded(runtime._jsonable(value)) == expected


def test_numeric_array_subclass_retains_recursive_projection():
    class DistinctToList(np.ndarray):
        def tolist(self):
            pytest.fail("A subclass must retain the established recursive path")

    array = np.arange(12, dtype=np.float64).reshape(3, 4).view(DistinctToList)
    assert encoded(runtime._jsonable(array)) == encoded(legacy_jsonable(array))


def test_dense_snapshot_numeric_projection_preserves_full_payload_hash():
    array = np.arange(64 * 512, dtype=np.float32).reshape(64, 512) / np.float32(7)
    array[:, ::7] = -0.0
    payload = {"X": array, "y": np.arange(64, dtype=np.uint8) % 2,
               "inference_X": array[-1:], "manifest": {"recipe": "unchanged"}}
    old = legacy_jsonable(payload)
    projected = runtime._jsonable(payload)
    assert encoded(projected) == encoded(old)
    assert digest(projected) == digest(old)


REF = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)


def fixture_history():
    return pd.DataFrame([dict(symbol="AAA", opened_at=REF,
        available_at=REF + timedelta(minutes=15), availability_kind="actual",
        interval_seconds=900, open=100, high=101, low=99, close=100,
        volume=100, synthetic=False)])


def fixture_dataset(frame, *, cutoff, reference_at, **kwargs):
    return SimpleNamespace(X=np.asarray([[0.], [1.], [0.], [1.]]),
        y=np.asarray([0, 1, 0, 1]), inference_X=np.asarray([[1.]]), feature_names=("x",),
        label_available_at=pd.date_range(REF - timedelta(hours=2), periods=4, freq="15min"),
        reference_at=pd.date_range(REF - timedelta(hours=3), periods=4, freq="15min"),
        target_close_at=pd.date_range(REF - timedelta(hours=2, minutes=15), periods=4, freq="15min"),
        inference=dict(symbol="AAA", reference_at=reference_at.isoformat(), reference_open=100,
            information_available_at=(REF + timedelta(minutes=15)).isoformat(),
            availability_kind="actual"), manifest={"training_rows": 4})


def fixture_trainer(*args, **kwargs):
    return dict(outputs=[dict(name="fixture", status="success", probability_not_down=.6,
                             version="fixture", training_seconds=0, inference_seconds=0)],
        configured_count=1, successful_count=1, complete=True,
        probability_not_down=.6, training_seconds=0, inference_seconds=0)


@pytest.mark.parametrize("reused", [False, True], ids=["new-inputs", "frozen-inputs"])
def test_phase_clocks_separate_history_and_preserve_total_scope(tmp_path, monkeypatch, reused):
    runner = runtime.ShadowRunner(store=ShadowStore(tmp_path / "shadow.sqlite3"),
        pc="Scout", symbols=("AAA",), config=runtime.ResearchConfig(models=("fixture",)),
        dataset_builder=fixture_dataset, trainer=fixture_trainer,
        clock=lambda: REF + timedelta(minutes=16))
    cutoff = REF + timedelta(minutes=16)
    frame = fixture_history()
    if reused:
        runner._symbol("prime", "AAA", frame, REF, cutoff, "live", None, None, 0)
        _, snapshot = runner.store.inputs("prime", "AAA")
        runner.store.freeze_inputs("target", "AAA", snapshot)

    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    clock = Clock()
    monkeypatch.setattr(runtime.time, "perf_counter", clock)
    original_inputs = runner.store.inputs
    original_freeze = runner.store.freeze_inputs
    original_projection = runtime._jsonable
    original_scope = runtime._dataset_scope
    original_frame = runtime._frame
    built = []
    scope_calls = []
    frame_calls = []

    def inputs(*args):
        clock.advance(2)
        return original_inputs(*args)

    def history():
        assert not reused, "Reused immutable inputs must not load history"
        clock.advance(11)
        return frame

    def builder(*args, **kwargs):
        assert not reused, "Reused immutable inputs must not rebuild a dataset"
        clock.advance(3)
        value = fixture_dataset(*args, **kwargs)
        built.append(value)
        return value

    def projection(value):
        if built and value is built[0].X:
            clock.advance(5)
        return original_projection(value)

    def retained_frame(value):
        frame_calls.append(value)
        if len(frame_calls) == 2:
            clock.advance(7)
        return original_frame(value)

    def freeze(*args):
        clock.advance(11)
        return original_freeze(*args)

    def scope(*args):
        scope_calls.append(args)
        if len(scope_calls) == (1 if reused else 2):
            clock.advance(13)
        return original_scope(*args)

    def trainer(*args, **kwargs):
        clock.advance(17)
        return fixture_trainer(*args, **kwargs)

    monkeypatch.setattr(runner.store, "inputs", inputs)
    monkeypatch.setattr(runner.store, "freeze_inputs", freeze)
    monkeypatch.setattr(runtime, "_jsonable", projection)
    monkeypatch.setattr(runtime, "_frame", retained_frame)
    monkeypatch.setattr(runtime, "_dataset_scope", scope)
    runner.dataset_builder = builder
    runner.trainer = trainer
    result = runner._symbol("target", "AAA", history, REF, cutoff, "live", None, None, 0)
    profile = result["preprocessing_profile"]
    assert profile["version"] == "intraday-preprocessing-phases-v1"
    assert profile["input_source"] == ("frozen_snapshot" if reused else "new_snapshot")
    assert profile["initial_snapshot_read_seconds"] == 2
    assert profile["initial_snapshot_read_in_preprocessing"] is reused
    assert profile["history_load_seconds"] == (0 if reused else 11)
    assert profile["dataset_build_seconds"] == (0 if reused else 3)
    assert profile["snapshot_projection_seconds"] == (0 if reused else 5)
    assert profile["candle_retention_seconds"] == (0 if reused else 7)
    assert profile["snapshot_freeze_seconds"] == (0 if reused else 11)
    assert profile["snapshot_validation_seconds"] == 13
    assert profile["unattributed_preprocessing_seconds"] == 0
    assert result["preprocessing_seconds"] == (15 if reused else 39)
    assert result["symbol_seconds_before_persistence"] == result["preprocessing_seconds"] + 17
    # Diagnostics belong to the forecast, never the immutable model inputs.
    _, saved = original_inputs("target", "AAA")
    assert "preprocessing_profile" not in saved
    assert saved["X"] == [[0.], [1.], [0.], [1.]]
