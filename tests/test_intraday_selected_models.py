"""Offline checks for calibrated fresh fits and local selection consumption."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest

from ml.intraday.models import ModelSpec, fit_ensemble, purged_chronological_split
from ml.intraday.prediction import ContractError, TARGET_VERSION
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.selected_config import VERSION, load_selected_config
from ml.intraday.store import ShadowStore

REF = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)


def write_config(path, *, selection_id="night-a", actor="Atlas", source="EQUS.MINI"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": VERSION, "actor": actor,
        "source_dataset": source, "target_version": TARGET_VERSION,
        "symbols": {"AAPL": {"models": [asdict(ModelSpec("nb_smooth", family="gaussian_nb",
            parameters={"var_smoothing": .01}, probability_calibration="sigmoid"))],
            "selection_id": selection_id, "selected_at": (REF-timedelta(hours=1)).isoformat(),
            "feature_names": ["x"], "max_training_rows": 3}}}))


def test_selected_calibration_fits_preprocessing_only_before_purged_boundary(tmp_path):
    rng = np.random.default_rng(123)
    X = rng.normal(size=(120, 2)); y = np.arange(120) % 2
    references = [REF + i*timedelta(minutes=15) for i in range(120)]
    targets = [v+timedelta(minutes=45) for v in references]
    train, cal = purged_chronological_split(references, targets)
    X[cal, 0] += 200
    result = fit_ensemble(X, y, [[10., 0.]], config=[ModelSpec("smooth", family="gaussian_nb",
        probability_calibration="sigmoid")], training_cutoff=targets[-1],
        label_available_at=targets, reference_at=references, target_close_at=targets,
        artifact_dir=tmp_path, run_id="calibration-check")
    assert result["complete"], result
    output = result["outputs"][0]
    fitted = joblib.load(output["artifact_path"])["estimator"]
    assert fitted.base.named_steps["scale"].mean_[0] == pytest.approx(X[train, 0].mean())
    assert output["metadata"]["calibration"]["base_latest_target"] <= output["metadata"]["calibration"]["first_calibration_reference"]
    assert 0 < result["probability_not_down"] < 1


def test_calibrated_knn_caps_neighbors_to_base_fit_not_all_rows():
    X = np.arange(80).reshape(-1, 1); y = np.arange(80) % 2
    refs = [REF+i*timedelta(minutes=15) for i in range(80)]
    targets = [v+timedelta(minutes=45) for v in refs]
    result = fit_ensemble(X, y, [[81]], config=[ModelSpec("knn_cal", family="knn",
        parameters={"n_neighbors": 500}, probability_calibration="sigmoid")],
        training_cutoff=targets[-1], label_available_at=targets, reference_at=refs, target_close_at=targets)
    assert result["complete"], result
    metadata = result["outputs"][0]["metadata"]
    assert metadata["effective_parameters"]["n_neighbors"] == metadata["calibration"]["base_rows"]


@pytest.mark.parametrize("change", ["actor", "source", "future", "target", "calibration"])
def test_selection_rejects_foreign_future_or_incompatible_evidence(tmp_path, change):
    path = tmp_path/"active.json"; write_config(path)
    doc = json.loads(path.read_text())
    if change == "actor": doc["actor"] = "Scout"
    if change == "source": doc["source_dataset"] = "XNAS.ITCH"
    if change == "future": doc["symbols"]["AAPL"]["selected_at"] = (REF+timedelta(days=1)).isoformat()
    if change == "target": doc["target_version"] = "another-target"
    if change == "calibration": doc["symbols"]["AAPL"]["models"][0]["probability_calibration"] = None
    path.write_text(json.dumps(doc))
    with pytest.raises(ContractError):
        load_selected_config(path, pc="Atlas", symbols=["AAPL"], source_dataset="EQUS.MINI", as_of=REF)


def test_default_live_cycles_freeze_selected_specs_and_refit_each_reference(tmp_path):
    path = tmp_path/"model-supervision/active.json"; write_config(path)
    seen = []
    def dataset(frame, *, cutoff, reference_at, **kwargs):
        return SimpleNamespace(X=np.array([[0], [1], [0], [1]]), y=np.array([0, 1, 0, 1]),
            inference_X=np.array([[1]]), feature_names=("x",),
            label_available_at=[reference_at-timedelta(hours=1)]*4,
            reference_at=[reference_at-timedelta(hours=2)+i*timedelta(minutes=15) for i in range(4)],
            target_close_at=[reference_at-timedelta(hours=1,minutes=15)+i*timedelta(minutes=15) for i in range(4)],
            inference={"symbol": "AAPL", "reference_at": reference_at.isoformat(), "reference_open": 100,
                "information_available_at": (reference_at+timedelta(minutes=15)).isoformat(),
                "availability_kind": "actual"}, manifest={})
    def train(*args, config, **kwargs):
        seen.append(config)
        assert len(args[0]) == 3 and len(kwargs["label_available_at"]) == 3
        return {"outputs": [{"name": config[0]["name"], "status": "success", "probability_not_down": .6}],
            "configured_count": 1, "successful_count": 1, "complete": True,
            "probability_not_down": .6, "training_seconds": 0., "inference_seconds": 0.}
    now = [REF+timedelta(minutes=16)]
    runner = ShadowRunner(store=ShadowStore(tmp_path/"shadow.sqlite3"), pc="Atlas", symbols=["AAPL"],
        trainer=train, dataset_builder=dataset, clock=lambda: now[0])
    history = pd.DataFrame([dict(symbol="AAPL", opened_at=REF, available_at=now[0],
        availability_kind="actual", open=100, high=101, low=99, close=100, volume=100, synthetic=False)])
    first = runner.run_cycle({"AAPL": history}, reference_at=REF, mode="live")
    assert first["forecasts"]["AAPL"]["model_selection_id"] == "night-a"
    assert seen[0][0]["probability_calibration"] == "sigmoid"
    assert runner.run_cycle({"AAPL": history}, reference_at=REF, mode="live")["recovered_existing"]
    assert len(seen) == 1
    write_config(path, selection_id="night-b")
    same = runner.run_cycle({"AAPL": history}, reference_at=REF, mode="live")
    assert same["cycle_id"] == first["cycle_id"] and same["recovered_existing"]
    assert same["forecasts"]["AAPL"]["model_selection_id"] == "night-a"
    assert len(seen) == 1
    now[0] += timedelta(minutes=15)
    second = runner.run_cycle({"AAPL": history}, reference_at=REF+timedelta(minutes=15), mode="live")
    assert len(seen) == 2 and second["forecasts"]["AAPL"]["model_selection_id"] == "night-b"
    assert first["cycle_id"] != second["cycle_id"]


def test_absent_selection_retains_defaults_without_creating_file(tmp_path):
    path = tmp_path/"absent.json"
    assert load_selected_config(path, pc="Atlas", symbols=["AAPL"], source_dataset="EQUS.MINI", as_of=REF) == {}
    assert not path.exists()


def test_malformed_symbol_keeps_other_selected_models(tmp_path):
    path = tmp_path/"active.json"; write_config(path)
    doc = json.loads(path.read_text()); doc["symbols"]["IONQ"] = {"models": []}
    path.write_text(json.dumps(doc)); errors = []
    result = load_selected_config(path, pc="Atlas", symbols=["AAPL", "IONQ"], source_dataset="EQUS.MINI",
        as_of=REF, on_error=lambda symbol, error: errors.append((symbol, type(error).__name__)))
    assert set(result) == {"AAPL"} and errors == [("IONQ", "ContractError")]


def test_selected_configuration_respects_explicit_earlier_training_cutoff(tmp_path):
    path = tmp_path/"active.json"; write_config(path)
    with pytest.raises(ContractError, match="future"):
        load_selected_config(path, pc="Atlas", symbols=["AAPL"], source_dataset="EQUS.MINI",
            as_of=REF-timedelta(hours=2))


@pytest.fixture
def composed_runner(tmp_path, monkeypatch):
    """Own-store fixture with both configuration lanes; never fits a real model."""
    from ml.intraday import data
    monkeypatch.setattr(data, "FEATURE_NAMES", ("x",))
    seen = []
    def dataset(frame, *, cutoff, reference_at, **scope):
        return SimpleNamespace(X=np.array([[0], [1], [0], [1]]), y=np.array([0, 1, 0, 1]),
            inference_X=np.array([[1]]), feature_names=("x",),
            label_available_at=[reference_at-timedelta(hours=1)]*4,
            reference_at=[reference_at-timedelta(hours=2)+i*timedelta(minutes=15) for i in range(4)],
            target_close_at=[reference_at-timedelta(hours=1, minutes=15)+i*timedelta(minutes=15) for i in range(4)],
            inference={"symbol": "AAPL", "reference_at": reference_at.isoformat(), "reference_open": 100,
                "information_available_at": (reference_at+timedelta(minutes=15)).isoformat(),
                "availability_kind": "actual"}, manifest=dict(scope))
    def train(X, y, inference_X, *, config, **kwargs):
        names = [value["name"] if isinstance(value, dict) else getattr(value, "name", value) for value in config]
        seen.append({"names": names, "rows": len(X), "metadata": kwargs["dataset_metadata"]})
        return {"outputs": [{"name": name, "status": "success", "probability_not_down": .6} for name in names],
            "configured_count": len(names), "successful_count": len(names), "complete": True,
            "probability_not_down": .6, "training_seconds": 0., "inference_seconds": 0.}
    def create(**kwargs):
        runner = ShadowRunner(store=ShadowStore(tmp_path/"shadow.sqlite3"), pc="Atlas", symbols=["AAPL"],
            trainer=train, dataset_builder=dataset, clock=lambda: REF+timedelta(minutes=16), **kwargs)
        history = pd.DataFrame([dict(symbol="AAPL", opened_at=REF, available_at=REF+timedelta(minutes=15),
            availability_kind="actual", open=100, high=101, low=99, close=100, volume=100, synthetic=False)])
        return runner, {"AAPL": history}, seen
    return create


@pytest.mark.parametrize("mode", ["live", "shadow"])
def test_legacy_defaults_consume_own_manifest_without_native_policy_flag(tmp_path, monkeypatch, composed_runner, mode):
    from ml.intraday import model_policy
    write_config(tmp_path/"model-supervision/active.json")
    monkeypatch.setattr(model_policy, "load_active_policy", lambda *a, **k: pytest.fail("unrequested native policy"))
    runner, history, seen = composed_runner()
    assert runner.config.use_supervised_models is False
    result = runner.run_cycle(history, reference_at=REF, mode=mode)
    assert result["forecasts"]["AAPL"]["model_selection_id"] == "night-a"
    assert seen[0]["names"] == ["nb_smooth"] and seen[0]["rows"] == 3


def test_explicit_manifest_wins_and_newer_other_policy_is_reported_without_wait(tmp_path, monkeypatch, composed_runner):
    from ml.intraday import model_policy
    default = tmp_path/"model-supervision/active.json"
    explicit = tmp_path/"selected-explicit.json"
    write_config(default, selection_id="default-choice")
    write_config(explicit, selection_id="explicit-choice")
    other = tmp_path/"supervision/active/Atlas-AAPL.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"installed_at": REF.isoformat()}))
    monkeypatch.setattr(model_policy, "load_active_policy", lambda *a, **k: pytest.fail("winning manifest was replaced"))
    runner, history, _ = composed_runner(selected_config_path=explicit, config=ResearchConfig(use_supervised_models=True))
    result = runner.run_cycle(history, reference_at=REF, mode="live")
    assert result["forecasts"]["AAPL"]["model_selection_id"] == "explicit-choice"
    assert other.exists() and json.loads(default.read_text())["symbols"]["AAPL"]["selection_id"] == "default-choice"
    with runner.store.connect() as db:
        saved = json.loads(db.execute("SELECT payload FROM events WHERE kind='selected_configuration_precedence'").fetchone()[0])
    assert saved["other_policy_newer"] is True and saved["other_policy_preserved"] is True


@pytest.mark.parametrize("malformed", [False, True])
def test_missing_or_malformed_own_manifest_preserves_explicit_native_policy(tmp_path, monkeypatch, composed_runner, malformed):
    from ml.intraday import model_policy
    if malformed:
        path = tmp_path/"model-supervision/active.json"
        path.parent.mkdir(parents=True)
        path.write_text("{malformed")
    selected = ModelSpec("native-policy", family="gaussian_nb", probability_calibration="sigmoid")
    monkeypatch.setattr(model_policy, "load_active_policy", lambda *a, **k: dict(models=(selected,),
        dataset_scope=dict(synthetic_policy="exclude", lookback_days=30), sha256="native-policy-id"))
    runner, history, seen = composed_runner(config=ResearchConfig(use_supervised_models=True))
    result = runner.run_cycle(history, reference_at=REF, mode="live")
    saved = result["forecasts"]["AAPL"]
    assert seen[0]["names"] == ["native-policy"] and seen[0]["rows"] == 4
    assert saved["model_selection_id"] is None
    assert saved["model_selection"]["active_policy_sha256"] == "native-policy-id"
    assert saved["dataset_scope"] == dict(synthetic_policy="exclude", lookback_days=30)


def test_explicit_models_bypass_automatic_manifest_but_native_flag_retains_existing_override(tmp_path, monkeypatch, composed_runner):
    from ml.intraday import model_policy
    write_config(tmp_path/"model-supervision/active.json")
    selected = ModelSpec("native-policy", family="gaussian_nb", probability_calibration="sigmoid")
    loads = []
    def load(*args, **kwargs):
        loads.append(kwargs["symbol"])
        return dict(models=(selected,), dataset_scope=dict(synthetic_policy="exclude", lookback_days=None), sha256="native")
    monkeypatch.setattr(model_policy, "load_active_policy", load)
    runner, history, seen = composed_runner(config=ResearchConfig(models=("explicit-model",)))
    first = runner.run_cycle(history, reference_at=REF, mode="live")
    assert first["forecasts"]["AAPL"]["model_selection"] is None
    assert seen[0]["names"] == ["explicit-model"] and loads == []
    runner.config = ResearchConfig(models=("explicit-model",), use_supervised_models=True)
    second = runner.run_cycle(history, reference_at=REF, mode="live")
    assert second["forecasts"]["AAPL"]["model_selection_id"] is None
    assert seen[-1]["names"] == ["native-policy"] and loads == ["AAPL"]
