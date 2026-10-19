"""Offline resource-policy and dispatch checks; all fitting is replaced by fakes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
import threading
import warnings

import numpy as np
import pytest

from ml.intraday import models
from ml.intraday.prediction import ContractError
from ml.intraday.resource_policy import VERSION, manual_symbol_workers


@pytest.fixture
def native_limits(monkeypatch):
    state = {"threads": 8, "entered": 0, "restored": 0}

    @contextmanager
    def limits(*, limits):
        before = state["threads"]
        state["threads"] = limits
        state["entered"] += 1
        try:
            yield
        finally:
            state["threads"] = before
            state["restored"] += 1

    monkeypatch.setattr(models, "threadpool_limits", limits)
    yield state
    assert models._FIT_RESOURCE_USERS == 0
    assert models._FIT_RESOURCE_STACK is None


@pytest.mark.parametrize("first_fails", [False, True])
def test_overlapping_fit_scopes_keep_limits_and_thread_warning_identity(native_limits, monkeypatch, first_fails):
    ready = threading.Barrier(3, timeout=5)
    release_first, release_second = threading.Event(), threading.Event()
    captured, forwarded = {}, []
    original_filters = warnings.filters
    monkeypatch.setattr(warnings, "showwarning", lambda message, *args, **kwargs: forwarded.append(str(message)))
    original_handler = warnings.showwarning

    def fit(name, release):
        with models._fit_resources() as own:
            captured[name] = own
            warnings.warn(name + "-before", UserWarning)
            ready.wait()
            assert release.wait(5)
            warnings.warn(name + "-after", UserWarning)
            if name == "first" and first_fails:
                raise RuntimeError("fake fit failure")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fit, "first", release_first)
        second = pool.submit(fit, "second", release_second)
        try:
            ready.wait()
            warnings.warn("unrelated-thread", UserWarning)
            release_first.set()
            if first_fails:
                with pytest.raises(RuntimeError, match="fake fit failure"):
                    first.result(timeout=5)
            else:
                first.result(timeout=5)
            assert native_limits == {"threads": 1, "entered": 2, "restored": 0}
            assert warnings.showwarning is not original_handler
        finally:
            release_first.set()
            release_second.set()
        second.result(timeout=5)
    assert native_limits == {"threads": 8, "entered": 2, "restored": 2}
    assert warnings.showwarning is original_handler and warnings.filters is original_filters
    assert forwarded == ["unrelated-thread"]
    assert {name: [str(w.message) for w in items] for name, items in captured.items()} == {
        "first": ["first-before", "first-after"], "second": ["second-before", "second-after"]}


def test_nested_fit_scopes_keep_separate_warning_records(native_limits):
    with models._fit_resources() as outer:
        warnings.warn("outer-before")
        with models._fit_resources() as inner:
            warnings.warn("inner")
        assert native_limits["restored"] == 0
        warnings.warn("outer-after")
    assert [str(item.message) for item in outer] == ["outer-before", "outer-after"]
    assert [str(item.message) for item in inner] == ["inner"]
    assert native_limits["threads"] == 8


def test_fit_scope_setup_failure_does_not_leave_global_hooks(monkeypatch):
    original_handler, original_filters = warnings.showwarning, warnings.filters

    def failed_limits(**kwargs):
        raise RuntimeError("fake native setup failure")

    monkeypatch.setattr(models, "threadpool_limits", failed_limits)
    with pytest.raises(RuntimeError, match="fake native setup failure"):
        with models._fit_resources():
            pytest.fail("failed setup must not enter fit")
    assert models._FIT_RESOURCE_USERS == 0 and models._FIT_RESOURCE_STACK is None
    assert warnings.showwarning is original_handler and warnings.filters is original_filters


def test_fake_ensemble_overlaps_and_preserves_each_model_warning(native_limits, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *a, **k: {"offline_fixture": True})
    monkeypatch.setattr(models, "_versions", lambda spec: {"fixture": "1"})
    ready = threading.Barrier(2, timeout=5)
    specs = [models.ModelSpec(name, family="logistic", parameters={"C": .5}) for name in ("first", "second")]

    def fit(spec, *args, **kwargs):
        warnings.warn("warning-" + spec.name)
        ready.wait()
        return object(), {"effective_parameters": dict(spec.parameters)}

    monkeypatch.setattr(models, "fit_calibrated_estimator", fit)
    monkeypatch.setattr(models, "probability_not_down", lambda *args: .6)

    def ensemble(spec):
        return models.fit_ensemble(np.array([[0.], [1.]]), np.array([0, 1]), np.array([[1.]]),
            config=[spec], training_cutoff="2026-10-19T14:00:00Z",
            label_available_at=["2026-10-19T13:00:00Z", "2026-10-19T13:15:00Z"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ensemble, specs))
    for result, spec in zip(results, specs):
        assert result["complete"] is True
        assert result["configured_count"] == result["successful_count"] == 1
        assert result["outputs"][0]["warnings"] == ["warning-" + spec.name]
        assert result["outputs"][0]["metadata"]["spec"]["parameters"] == {"C": .5}
        assert result["outputs"][0]["metadata"]["full_retrain"] is True


def write_policy(root, **changes):
    path = root / "intraday/manual-resource-policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": VERSION, "actor": "Atlas", "symbol_workers": 11, **changes}))
    return path


def test_private_policy_is_read_only_and_does_not_change_other_pc_defaults(tmp_path):
    atlas, unrelated = tmp_path / "atlas", tmp_path / "unrelated"
    path = write_policy(atlas)
    before = path.read_bytes()
    assert manual_symbol_workers(atlas, actor="Atlas") == 11
    assert manual_symbol_workers(unrelated, actor="Scout") == 1
    assert manual_symbol_workers(atlas, actor="Atlas", explicit=3) == 3
    assert path.read_bytes() == before and not unrelated.exists()
    with pytest.raises(ContractError, match="match this 15m actor"):
        manual_symbol_workers(atlas, actor="Scout")


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "11", None])
def test_local_policy_rejects_invalid_worker_counts(tmp_path, value):
    write_policy(tmp_path, symbol_workers=value)
    with pytest.raises(ContractError, match="positive symbol worker"):
        manual_symbol_workers(tmp_path, actor="Atlas")


def test_explicit_workers_do_not_depend_on_optional_policy(tmp_path):
    path = write_policy(tmp_path)
    path.write_text("invalid optional policy")
    assert manual_symbol_workers(tmp_path, actor="Atlas", explicit=2) == 2
    with pytest.raises(ContractError, match="Invalid local manual resource policy"):
        manual_symbol_workers(tmp_path, actor="Atlas")


@pytest.mark.parametrize("explicit,expected", [(None, 11), (2, 2)])
def test_manual_cli_resolves_local_policy_and_explicit_override(tmp_path, monkeypatch, explicit, expected):
    from ml.intraday import live_session
    write_policy(tmp_path)
    calls = []
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(live_session, "run_manual", lambda **kw: calls.append(kw) or {"status": "STOPPED"})
    args = ["--live", "--datastore", str(tmp_path)]
    if explicit is not None:
        args += ["--symbol-workers", str(explicit)]
    assert live_session.main(args) == 0
    assert len(calls) == 1 and calls[0]["config"].symbol_workers == expected


def test_policy_never_implies_manual_start(tmp_path, monkeypatch):
    from ml.intraday import live_session, readiness
    write_policy(tmp_path)
    monkeypatch.setattr(live_session, "run_manual", lambda **kw: pytest.fail("implicit live start"))
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: pytest.fail("live policy resolved in inspection"))
    monkeypatch.setattr(readiness, "inspect_readiness", lambda **kw: {"manual_live_start_ready": True})
    assert live_session.main(["--datastore", str(tmp_path)]) == 0


def test_eleven_symbols_dispatch_without_serial_fit_queue(tmp_path, monkeypatch):
    import ml.market_provenance
    from ml.intraday.runtime import ResearchConfig, ShadowRunner
    from ml.intraday.store import ShadowStore
    from tests.test_intraday_runtime import REF, dataset, history, trained
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *a, **k: {"offline_fixture": True})
    barrier = threading.Barrier(11, timeout=10)
    symbols = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ")
    published = []

    def train(*args, **kwargs):
        assert tuple(kwargs["config"]) == ("fixture_a", "fixture_b")
        barrier.wait()
        return trained(*args, **kwargs)

    runner = ShadowRunner(store=ShadowStore(tmp_path / "shadow.sqlite3"), pc="Atlas", symbols=symbols,
        config=ResearchConfig(models=("fixture_a", "fixture_b"), symbol_workers=11),
        dataset_builder=dataset, trainer=train, clock=lambda: REF + timedelta(minutes=16))
    result = runner.run_cycle({symbol: history(symbol) for symbol in symbols}, reference_at=REF,
        mode="live", on_published=published.append)
    assert result["status"] == "completed" and not result["failures"]
    assert set(result["forecasts"]) == set(symbols) and len(published) == 11
    assert len({forecast["forecast_id"] for forecast in published}) == 11
    for forecast in published:
        assert forecast["configured_count"] == forecast["successful_count"] == 2
        assert forecast["reference_at"] == REF.isoformat()
        assert forecast["target_close"] == (REF + timedelta(minutes=45)).isoformat()


def test_manual_stop_prevents_queued_symbol_fits(tmp_path, monkeypatch):
    import ml.market_provenance
    from ml.intraday.runtime import ResearchConfig, ShadowRunner
    from ml.intraday.store import ShadowStore
    from tests.test_intraday_runtime import REF, dataset, history, trained
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *a, **k: {"offline_fixture": True})
    stopped, calls = threading.Event(), []

    def train(*args, **kwargs):
        calls.append(kwargs["config"])
        stopped.set()
        return trained(*args, **kwargs)

    symbols = ("AAPL", "AMZN", "GOOG")
    runner = ShadowRunner(store=ShadowStore(tmp_path / "shadow.sqlite3"), pc="Atlas", symbols=symbols,
        config=ResearchConfig(models=("fixture_a", "fixture_b"), symbol_workers=1),
        dataset_builder=dataset, trainer=train, clock=lambda: REF + timedelta(minutes=16), stopped=stopped.is_set)
    result = runner.run_cycle({symbol: history(symbol) for symbol in symbols}, reference_at=REF, mode="live")
    assert len(calls) == 1
    assert all("Manual stop" in value["message"] for value in result["failures"].values())
    assert len(result["forecasts"]) <= 1 and len(result["failures"]) >= 2
