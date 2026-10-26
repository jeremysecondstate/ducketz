"""Concurrent resource-scope regression checks; numerical fitting is always fake."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
import threading
import warnings

import numpy as np
import pytest

from ml.intraday import models


@pytest.fixture
def native_limits(monkeypatch):
    state = {"threads": 8, "entered": 0, "restored": 0}
    warning_state = warnings.filters, warnings.showwarning, warnings._showwarnmsg_impl

    @contextmanager
    def limits(*, limits):
        previous = state["threads"]
        state["threads"] = limits
        state["entered"] += 1
        try:
            yield
        finally:
            state["threads"] = previous
            state["restored"] += 1

    monkeypatch.setattr(models, "threadpool_limits", limits)
    yield state
    # Retain pytest's hooks even when exercising the broken pre-fix scope.
    warnings.filters, warnings.showwarning, warnings._showwarnmsg_impl = warning_state
    if hasattr(models, "_FIT_RESOURCE_USERS"):
        assert models._FIT_RESOURCE_USERS == 0
        assert models._FIT_RESOURCE_STACK is None


@pytest.mark.parametrize("first_fails", [False, True])
@pytest.mark.parametrize("exit_first", ["first", "second"])
def test_actual_ensemble_keeps_overlapping_limit_and_warning_ownership(native_limits, monkeypatch, first_fails, exit_first):
    """Exercise the real ensemble scope while replacing only numerical work."""
    first_ready, second_ready = threading.Event(), threading.Event()
    release_first, release_second = threading.Event(), threading.Event()
    original_handler, original_filters = warnings.showwarning, warnings.filters
    forwarded = []
    monkeypatch.setattr(warnings, "showwarning", lambda message, *a, **k: forwarded.append(str(message)))
    expected_handler = warnings.showwarning
    monkeypatch.setattr(models, "_versions", lambda spec: {"offline_fixture": "1"})
    frozen = models.freeze_model_spec(models.ModelSpec("first", family="logistic", parameters={"C": .5}))
    specs = [frozen, models.ModelSpec("second", family="logistic", parameters={"C": .7},
                                    probability_calibration="sigmoid", calibration_fraction=.25)]
    before = [asdict(spec) for spec in specs]

    def fake_fit(spec, *args, **kwargs):
        if spec.name == "first":
            warnings.warn("first-before", UserWarning)
            first_ready.set()
            assert release_first.wait(5)
        else:
            warnings.warn("second-before", UserWarning)
            second_ready.set()
            assert release_second.wait(5)
        if first_fails and spec.name == exit_first:
            raise RuntimeError("fake fit failure")
        warnings.warn(spec.name + "-after", UserWarning)
        return object(), {"fixture_evidence": spec.name}

    monkeypatch.setattr(models, "fit_calibrated_estimator", fake_fit)
    monkeypatch.setattr(models, "probability_not_down", lambda *args: .600123)

    def ensemble(spec):
        return models.fit_ensemble([[0.], [1.]], [0, 1], [[1.]], config=[spec],
            training_cutoff="2026-10-19T14:00:00Z",
            label_available_at=["2026-10-19T13:00:00Z", "2026-10-19T13:15:00Z"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ensemble, specs[0])
        assert first_ready.wait(5)
        second = pool.submit(ensemble, specs[1])
        try:
            assert second_ready.wait(5)
            warnings.warn("unrelated-thread", UserWarning)
            (release_first if exit_first == "first" else release_second).set()
            (first if exit_first == "first" else second).result(timeout=5)
            assert native_limits == {"threads": 1, "entered": 2, "restored": 0}
            assert warnings.showwarning is not expected_handler
        finally:
            release_first.set()
            release_second.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert native_limits == {"threads": 8, "entered": 2, "restored": 2}
    assert warnings.showwarning is expected_handler and warnings.filters is original_filters
    assert forwarded == ["unrelated-thread"]
    assert [asdict(spec) for spec in specs] == before
    outputs = [first_result["outputs"][0], second_result["outputs"][0]]
    for result, output, spec in zip([first_result, second_result], outputs, specs):
        assert output["metadata"]["spec"] == asdict(spec)
        assert output["metadata"]["internal_workers"] == 1
        assert output["metadata"]["target_version"] == models.TARGET_VERSION
        assert output["metadata"]["training_rows"] == 2
        assert output["metadata"]["training_data_sha256"] == outputs[0]["metadata"]["training_data_sha256"]
        if first_fails and spec.name == exit_first:
            assert result["complete"] is False
            assert output["error"] == {"phase": "training", "type": "RuntimeError", "message": "fake fit failure"}
            assert result["probability_not_down"] is None
        else:
            assert result["complete"] is True
            assert output["warnings"] == [spec.name + "-before", spec.name + "-after"]
            assert result["probability_not_down"] == .600123


@pytest.mark.parametrize("first_fails", [False, True])
@pytest.mark.parametrize("exit_first", ["first", "second"])
def test_scope_normal_or_failed_exit_keeps_other_user_limits(native_limits, first_fails, exit_first):
    ready = threading.Barrier(3, timeout=5)
    release_first, release_second = threading.Event(), threading.Event()
    captures = {}

    def scope(name, release):
        with models._fit_resources() as observed:
            captures[name] = observed
            warnings.warn(name + "-before", UserWarning)
            ready.wait()
            assert release.wait(5)
            warnings.warn(name + "-after", UserWarning)
            if name == exit_first and first_fails:
                raise RuntimeError("scope failure")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(scope, "first", release_first)
        second = pool.submit(scope, "second", release_second)
        try:
            ready.wait()
            (release_first if exit_first == "first" else release_second).set()
            exiting = first if exit_first == "first" else second
            if first_fails:
                with pytest.raises(RuntimeError, match="scope failure"):
                    exiting.result(timeout=5)
            else:
                exiting.result(timeout=5)
            assert native_limits == {"threads": 1, "entered": 2, "restored": 0}
        finally:
            release_first.set()
            release_second.set()
        (second if exit_first == "first" else first).result(timeout=5)
    assert native_limits == {"threads": 8, "entered": 2, "restored": 2}
    assert {name: [str(w.message) for w in own] for name, own in captures.items()} == {
        "first": ["first-before", "first-after"], "second": ["second-before", "second-after"]}


def test_nested_scope_keeps_distinct_warning_lists_and_delays_restore(native_limits):
    with models._fit_resources() as outer:
        warnings.warn("outer-before")
        with models._fit_resources() as inner:
            warnings.warn("inner")
        assert native_limits["restored"] == 0
        warnings.warn("outer-after")
    assert [str(w.message) for w in outer] == ["outer-before", "outer-after"]
    assert [str(w.message) for w in inner] == ["inner"]
    assert native_limits == {"threads": 8, "entered": 2, "restored": 2}


@pytest.mark.parametrize("phase", ["native", "warning_filter"])
def test_first_setup_failure_restores_hooks_and_native_scope(native_limits, monkeypatch, phase):
    original_handler, original_filters = warnings.showwarning, warnings.filters

    def failure(*args, **kwargs):
        raise RuntimeError("setup failure")

    with monkeypatch.context() as patch:
        patch.setattr(models if phase == "native" else warnings,
                      "threadpool_limits" if phase == "native" else "simplefilter", failure)
        with pytest.raises(RuntimeError, match="setup failure"):
            with models._fit_resources():
                pytest.fail("failed setup must not enter a fit")
    assert models._FIT_RESOURCE_USERS == 0 and models._FIT_RESOURCE_STACK is None
    assert warnings.showwarning is original_handler and warnings.filters is original_filters
    assert native_limits["threads"] == 8


def test_later_native_setup_failure_preserves_existing_scope(native_limits, monkeypatch):
    original = models.threadpool_limits
    with models._fit_resources() as observed:
        monkeypatch.setattr(models, "threadpool_limits", lambda **kw: (_ for _ in ()).throw(RuntimeError("late setup")))
        with pytest.raises(RuntimeError, match="late setup"):
            with models._fit_resources():
                pytest.fail("failed setup must not enter a fit")
        assert models._FIT_RESOURCE_USERS == 1
        assert native_limits == {"threads": 1, "entered": 1, "restored": 0}
        warnings.warn("existing-scope-survives")
        monkeypatch.setattr(models, "threadpool_limits", original)
    assert [str(w.message) for w in observed] == ["existing-scope-survives"]
    assert native_limits == {"threads": 8, "entered": 1, "restored": 1}


@pytest.mark.parametrize("family", [item["family"] for item in models.registry_status()
                                    if item["status"] == "available"])
def test_existing_frozen_constructor_pipeline_contract_roundtrip_without_fitting(family):
    """Preserve main's constructor and model policy contracts without training."""
    import json
    frozen = models.freeze_model_spec(models.ModelSpec("local", family=family))
    before = asdict(frozen)
    restored = models.configured_models(json.loads(json.dumps([before])))[0]
    assert asdict(restored) == before
    actual = models.build_estimator(restored)
    expected = models.build_estimator(frozen)
    assert [(name, models._resolved_step(step)) for name, step in actual.steps] == [
        (name, models._resolved_step(step)) for name, step in expected.steps]
    assert models.freeze_model_spec(restored) == restored


def test_each_entry_discovers_new_native_controllers_and_unwinds_reverse(monkeypatch):
    state = {"first": 8}
    restored = []

    @contextmanager
    def limits(*, limits):
        observed = dict(state)
        state.update({key: limits for key in state})
        try:
            yield
        finally:
            state.update(observed)
            restored.append(observed)

    monkeypatch.setattr(models, "threadpool_limits", limits)
    with models._fit_resources():
        state["newly_loaded"] = 12
        with models._fit_resources():
            assert state == {"first": 1, "newly_loaded": 1}
        assert state == {"first": 1, "newly_loaded": 1}
        assert restored == []
    assert state == {"first": 8, "newly_loaded": 12}
    assert restored == [{"first": 1, "newly_loaded": 12}, {"first": 8}]


def test_fit_resources_restore_after_baseexception(native_limits):
    with pytest.raises(KeyboardInterrupt):
        with models._fit_resources():
            raise KeyboardInterrupt("offline cancellation fixture")
    assert native_limits == {"threads": 8, "entered": 1, "restored": 1}
