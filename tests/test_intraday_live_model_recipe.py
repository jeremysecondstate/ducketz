"""Offline evidence for the explicitly selected next-manual-session recipe."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from ml.intraday import live_model_recipe as recipes
from ml.intraday.data import FEATURE_NAMES
from ml.intraday.prediction import ContractError, STEP
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.store import ShadowStore


def recipe(pc="Scout", symbols=("ABCL", "DBX")):
    return recipes.make_manual_recipe(pc=pc, symbols=symbols,
        installed_at="2026-10-14T22:00:00Z",
        authority_reference="Jeremy requested live installation and a later manual restart")


def save(root, value):
    path = root / "intraday" / value["pc"].lower() / "manual-model-recipe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def rehash(value):
    value["sha256"] = hashlib.sha256(json.dumps(
        {key: item for key, item in value.items() if key != "sha256"},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return value


def candles(*, days=6, per_day=40):
    rows = []
    for day in range(days):
        first = pd.Timestamp("2026-05-11T13:30:00Z") + pd.Timedelta(days=day)
        for number in range(per_day):
            opened = first + number * STEP
            index = day * per_day + number
            price = 100 + (index % 7) / 10
            rows.append(dict(symbol="ABCL", opened_at=opened, available_at=opened + STEP,
                availability_kind="actual", open=price, high=price + 1, low=price - 1,
                close=price + .1, volume=100 + index, source_id="fixture:EQUS.MINI",
                source_dataset="EQUS.MINI", session_id=str(first.date()),
                session_open=first.isoformat(), session_close=(first + pd.Timedelta(hours=16)).isoformat(),
                synthetic=False, no_trade=False, evidence_json="{}"))
    return pd.DataFrame(rows)


def selected(tmp_path):
    value = recipe()
    path = save(tmp_path, value)
    original = ResearchConfig(use_supervised_models=True, symbol_workers=2)
    config, runtime = recipes.prepare_manual_models(original, datastore=tmp_path,
        pc="Scout", symbols=("ABCL", "DBX"))
    return value, path, original, config, runtime


def dataset_for(runtime, frame=None):
    frame = candles() if frame is None else frame
    reference = frame.opened_at.iloc[-1]
    return runtime["dataset_builder"](frame, cutoff=reference + STEP + pd.Timedelta(seconds=3),
        reference_at=reference, lookback_days=None, synthetic_policy="exclude")


def fit(runtime, config, dataset, **extra):
    return runtime["trainer"](dataset.X, dataset.y, dataset.inference_X, config=config.models,
        target_version=dataset.manifest["target_version"],
        training_cutoff=dataset.manifest["training_cutoff"],
        label_available_at=dataset.label_available_at, reference_at=dataset.reference_at,
        target_close_at=dataset.target_close_at, feature_names=dataset.feature_names,
        dataset_metadata=dataset.manifest, **extra)


def test_absent_recipe_preserves_config_and_creates_nothing(tmp_path):
    config = ResearchConfig(use_supervised_models=True)
    result, adapters = recipes.prepare_manual_models(config, datastore=tmp_path,
        pc="Scout", symbols=("ABCL",))
    assert result is config and adapters == {}
    assert list(tmp_path.iterdir()) == []


def test_recipe_is_fixed_candidate_and_configuration_retains_other_controls(tmp_path):
    value, _, original, config, adapters = selected(tmp_path)
    assert original.use_supervised_models is True
    assert config.use_supervised_models is False and config.synthetic_policy == "exclude"
    assert config.symbol_workers == original.symbol_workers
    assert config.publication_offset_seconds == original.publication_offset_seconds
    assert value["selection"] == "authorized_human_live_experiment"
    assert value["models"] == list(config.models)
    assert [v["name"] for v in value["models"]] == ["logistic_strong", "forest_shallow"]
    assert value["models"][0]["parameters"] == {"C": .05}
    assert value["models"][1]["parameters"] == {
        "n_estimators": 64, "max_depth": 3, "min_samples_leaf": 16}
    assert all(v["probability_calibration"] == "sigmoid"
               and v["calibration_fraction"] == .25 and v["training_max_rows"] == 4096
               for v in value["models"])
    assert set(adapters) == {"dataset_builder", "trainer"}


@pytest.mark.parametrize("edit", [
    lambda v: v.update(sha256="0" * 64),
    lambda v: v.update(pc="Atlas"),
    lambda v: v.update(symbols=["DBX", "ABCL"]),
    lambda v: v.update(symbols=["ABCL"]),
    lambda v: v.update(selection="promoted"),
    lambda v: v.update(synthetic_policy="include"),
    lambda v: v.update(feature_version="legacy"),
    lambda v: v["models"][0]["parameters"].update(C=1),
    lambda v: v.update(unexpected_field="unsafe"),
    lambda v: v["training_support"].update(minimum_rows=True),
])
def test_changed_scope_checksum_or_fixed_settings_rejected(tmp_path, edit):
    value = recipe()
    edit(value)
    if value["sha256"] != "0" * 64:
        rehash(value)
    # Save the payload at Scout's own path even if its internal PC is wrong.
    path = save(tmp_path, recipe())
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError):
        recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
            pc="Scout", symbols=("ABCL", "DBX"))


def test_invalid_duplicate_and_noncanonical_json_rejected(tmp_path):
    path = save(tmp_path, recipe())
    for raw in ('{"sha256":1,"sha256":2}', '[1,2]', '{"installed_at":NaN}', '{'):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(ContractError):
            recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
                pc="Scout", symbols=("ABCL", "DBX"))


def test_pc_recipe_is_local_and_other_pc_file_is_not_read(tmp_path):
    save(tmp_path, recipe(pc="Atlas"))
    original = ResearchConfig()
    config, adapters = recipes.prepare_manual_models(original, datastore=tmp_path,
        pc="Scout", symbols=("ABCL", "DBX"))
    assert config is original and adapters == {}
    assert not (tmp_path / "intraday/scout").exists()


def test_startup_latches_recipe_and_later_file_edits_do_not_change_adapters(tmp_path):
    value, path, _, _, runtime = selected(tmp_path)
    path.write_text("invalid later edit", encoding="utf-8")
    dataset = dataset_for(runtime)
    assert dataset.manifest["manual_model_recipe"] == value
    dataset.manifest["manual_model_recipe"]["symbols"].append("OTHER")
    assert dataset_for(runtime).manifest["manual_model_recipe"] == value
    with pytest.raises(ContractError):
        recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
            pc="Scout", symbols=("ABCL", "DBX"))


def test_synthetic_candles_removed_before_features_without_altering_originals(tmp_path):
    value, _, _, _, runtime = selected(tmp_path)
    frame = candles()
    frame.loc[30:35, "synthetic"] = True
    original = frame.copy(deep=True)
    dataset = dataset_for(runtime, frame)
    pd.testing.assert_frame_equal(frame, original)
    assert dataset.manifest["manual_model_recipe"]["sha256"] == value["sha256"]
    assert dataset.manifest["synthetic_candles_excluded"] == 6
    assert dataset.manifest["synthetic_labels_included"] == 0
    assert (dataset.X[:, FEATURE_NAMES.index("synthetic_input_fraction20")] == 0).all()
    assert dataset.inference_X[0, FEATURE_NAMES.index("synthetic_input_fraction20")] == 0
    assert all(not c["synthetic"] for c in dataset.inference["snapshot"]["candles"])
    assert all(t == r + 3 * STEP for r, t in zip(dataset.reference_at, dataset.target_close_at))
    synthetic_times = set(frame.loc[frame.synthetic, "opened_at"])
    assert not (set(dataset.reference_at) & synthetic_times)
    assert not ({v - STEP for v in dataset.target_close_at} & synthetic_times)


def test_exact_synthetic_reference_and_including_synthetic_policy_fail_without_shift(tmp_path):
    *_, runtime = selected(tmp_path)
    frame = candles()
    frame.loc[len(frame) - 1, "synthetic"] = True
    with pytest.raises(ContractError, match="Exact reference candle is synthetic"):
        dataset_for(runtime, frame)
    with pytest.raises(ContractError, match="cannot admit synthetic"):
        runtime["dataset_builder"](frame, reference_at=frame.opened_at.iloc[-1],
            cutoff=frame.available_at.iloc[-1], synthetic_policy="include")


def test_other_symbol_cannot_enter_own_pc_recipe(tmp_path):
    *_, runtime = selected(tmp_path)
    frame = candles()
    frame["symbol"] = "OTHER"
    with pytest.raises(ContractError, match="outside this PC"):
        dataset_for(runtime, frame)


def support(dataset, *, y=None, refs=None, avail=None):
    return recipes._training_support(dataset.y if y is None else y,
        dataset.reference_at if refs is None else refs, dataset.target_close_at,
        dataset.label_available_at if avail is None else avail,
        cutoff=dataset.manifest["training_cutoff"])


def test_calibration_requires_real_purged_partition_support(tmp_path):
    *_, runtime = selected(tmp_path)
    dataset = dataset_for(runtime)
    evidence = support(dataset)
    assert evidence["training_dates"] >= 5 and evidence["training_rows"] >= 160
    assert evidence["purged_rows"] > 0
    assert evidence["base"]["rows"] >= 32 and evidence["calibration"]["rows"] >= 32
    assert min(evidence["base"]["class_counts"].values()) >= 5
    assert min(evidence["calibration"]["class_counts"].values()) >= 5
    labels = dataset.y.copy()
    labels[int(len(labels) * .75):] = 1
    with pytest.raises(ContractError, match="calibration partition"):
        support(dataset, y=labels)
    labels = np.ones(len(dataset.y), dtype=np.uint8)
    labels[-20:] = 0
    with pytest.raises(ContractError, match="base partition"):
        support(dataset, y=labels)
    with pytest.raises(ContractError, match="matching real training rows"):
        support(dataset, y=dataset.y[:159])
    with pytest.raises(ContractError, match="each target class"):
        support(dataset, y=np.ones(len(dataset.y), dtype=np.uint8))
    with pytest.raises(ContractError, match="not yet available"):
        support(dataset, avail=[pd.Timestamp(dataset.manifest["training_cutoff"]) + STEP] * len(dataset.y))


def test_too_few_training_dates_has_no_default_model_fallback(tmp_path):
    *_, runtime = selected(tmp_path)
    with pytest.raises(ContractError, match="actual training dates"):
        dataset_for(runtime, candles(days=4, per_day=60))


def test_newest_training_window_is_applied_before_support_checks(tmp_path, monkeypatch):
    *_, runtime = selected(tmp_path)
    source = dataset_for(runtime)
    # Synthetic-free, exact, chronological fixture large enough to exercise the
    # real retained row window without a costly multi-thousand-candle feature fit.
    refs = pd.date_range("2025-01-01T00:00Z", periods=4200, freq="15min")
    expanded = replace(source, X=np.resize(source.X, (4200, len(FEATURE_NAMES))),
        y=np.arange(4200) % 2, training_reference_at=refs,
        target_close_at=refs + 3 * STEP, label_available_at=refs + 3 * STEP,
        manifest={**source.manifest, "training_rows": 4200})
    monkeypatch.setattr(recipes, "build_observed_dataset", lambda *a, **kw: expanded)
    dataset = runtime["dataset_builder"](None, cutoff=refs[-1] + 4 * STEP,
        reference_at=source.inference["reference_at"], synthetic_policy="exclude")
    assert len(dataset.y) == 4096 and dataset.reference_at[0] == refs[-4096]
    assert dataset.manifest["input_training_rows"] == 4200
    assert dataset.manifest["training_window"]["excluded_rows"] == 104


def test_two_members_are_genuinely_fitted_with_purged_sigmoid_calibration(tmp_path):
    value, _, _, config, runtime = selected(tmp_path)
    dataset = dataset_for(runtime)
    result = fit(runtime, config, dataset, artifact_dir=tmp_path / "models", run_id="real-fit")
    assert result["complete"] and result["successful_count"] == result["configured_count"] == 2
    assert result["consensus_status"] == "full"
    assert 0 <= result["probability_not_down"] <= 1
    for out in result["outputs"]:
        assert out["status"] == "success" and out["error"] is None
        assert Path(out["artifact_path"]).is_file()
        metadata = out["metadata"]
        assert metadata["calibration"]["method"] == "purged_chronological_sigmoid"
        assert metadata["calibration"]["calibration_rows"] >= 32
        assert metadata["calibration"]["purged_rows"] > 0
        assert metadata["dataset_metadata"]["manual_model_recipe"] == value
        assert metadata["training_window"]["max_rows"] == 4096


def test_changed_specs_or_old_synthetic_snapshot_never_call_fitter(tmp_path, monkeypatch):
    *_, config, runtime = selected(tmp_path)
    dataset = dataset_for(runtime)
    monkeypatch.setattr(recipes, "fit_ensemble", lambda *a, **kw: pytest.fail("unsafe fit"))
    with pytest.raises(ContractError, match="different model specifications"):
        fit(runtime, replace(config, models=("logistic",)), dataset)
    for key, value in (("manual_model_recipe", None), ("feature_version", "old"),
                       ("synthetic_labels_included", 1), ("synthetic_policy", "include")):
        changed = replace(dataset, manifest={**dataset.manifest, key: value})
        with pytest.raises(ContractError, match="frozen observed-only dataset evidence"):
            fit(runtime, config, changed)
    features = dataset.X.copy()
    features[0, FEATURE_NAMES.index("synthetic_input_fraction20")] = .05
    with pytest.raises(ContractError, match="observed-only feature matrices"):
        fit(runtime, config, replace(dataset, X=features))


@pytest.mark.parametrize("successes", [0, 1])
def test_partial_or_failed_fit_preserves_errors_and_never_returns_survivor_mean(tmp_path, monkeypatch, successes):
    *_, config, runtime = selected(tmp_path)
    dataset = dataset_for(runtime)
    result = {"configured_count": 2, "successful_count": successes,
        "complete": False, "consensus_status": "incomplete" if successes else "failed",
        "probability_not_down": .9 if successes else None,
        "outputs": [dict(name=name, status="success" if index < successes else "failed",
            probability_not_down=.9 if index < successes else None,
            error=None if index < successes else {"phase": "training", "type": "ActualFitError",
                "message": "actual calibrator fitting failure"},
            artifact_path="retained-real-artifact.joblib" if index < successes else None)
            for index, name in enumerate(("logistic_strong", "forest_shallow"))]}
    monkeypatch.setattr(recipes, "fit_ensemble", lambda *a, **kw: result)
    with pytest.raises(recipes.ManualRecipeFitError, match="no partial forecast or fallback") as caught:
        fit(runtime, config, dataset, artifact_dir=tmp_path / "models", run_id="failed-fit")
    assert caught.value.result is result
    assert "actual calibrator fitting failure" in str(caught.value)
    evidence = Path(caught.value.evidence_path)
    assert json.loads(evidence.read_text(encoding="utf-8")) == result


def test_real_fit_failure_retains_original_model_errors_without_fallback(tmp_path, monkeypatch):
    from ml.intraday import models
    *_, config, runtime = selected(tmp_path)
    dataset = dataset_for(runtime)
    original = models.build_estimator
    def one_fails(spec):
        if spec.name == "forest_shallow":
            raise RuntimeError("deliberate fixture forest failure")
        return original(spec)
    monkeypatch.setattr(models, "build_estimator", one_fails)
    with pytest.raises(recipes.ManualRecipeFitError) as caught:
        fit(runtime, config, dataset, artifact_dir=tmp_path / "models", run_id="one-real-failure")
    result = caught.value.result
    assert result["successful_count"] == 1
    assert result["outputs"][1]["error"]["message"] == "deliberate fixture forest failure"
    assert Path(result["outputs"][0]["artifact_path"]).is_file()
    assert "deliberate fixture forest failure" in str(caught.value)


def test_runner_uses_observed_recipe_metadata_and_two_models_in_frozen_forecast(tmp_path):
    value, _, _, config, runtime = selected(tmp_path)
    frame = candles()
    reference = frame.opened_at.iloc[-1]
    cutoff = reference + STEP + pd.Timedelta(seconds=3)
    store = ShadowStore(tmp_path / "forecast.sqlite3")
    runner = ShadowRunner(store=store, pc="Scout", symbols=("ABCL",), config=config,
        clock=lambda: cutoff.to_pydatetime(), artifact_root=tmp_path / "models", **runtime)
    result = runner.run_cycle({"ABCL": frame}, reference_at=reference, cutoff=cutoff,
        mode="shadow", experiment="manual-recipe-test")
    assert result["forecasts"]["ABCL"]["successful_count"] == 2
    assert result["forecasts"]["ABCL"]["ensemble_status"] == "full"
    assert result["forecasts"]["ABCL"]["dataset_manifest"]["manual_model_recipe"] == value
    assert result["forecasts"]["ABCL"]["synthetic_policy"] == "exclude"


def test_manual_entrypoint_latches_recipe_before_operating_client_side_effects(tmp_path, monkeypatch):
    from ml.intraday import data, live_session
    value = recipe()
    path = save(tmp_path, value)
    before = set(tmp_path.rglob("*"))
    def forbidden(*args, **kwargs):
        pytest.fail("Manual startup performed a provider, process, or session side effect before recipe selection")
    for name, attribute in (("app.services.schwab", "SchwabSession"),
                            ("app.services.databento_market_data", "DatabentoMarketDataProvider")):
        fake = ModuleType(name)
        setattr(fake, attribute, forbidden)
        monkeypatch.setitem(sys.modules, name, fake)
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Scout", ("ABCL", "DBX")))
    monkeypatch.setattr(data, "AcquisitionJournal", forbidden)
    monkeypatch.setattr(data, "load_local_history", forbidden)
    actual = recipes.prepare_manual_models
    captured = {}
    class StartupSelected(Exception):
        pass
    def stop_after_selection(config, **kwargs):
        captured["config"], captured["adapters"] = actual(config, **kwargs)
        captured["kwargs"] = kwargs
        raise StartupSelected
    monkeypatch.setattr(recipes, "prepare_manual_models", stop_after_selection)
    with pytest.raises(StartupSelected):
        live_session.run_manual(repository=tmp_path, datastore=tmp_path,
            profile=tmp_path / "unused-profile", watchlist=tmp_path / "unused-watchlist",
            config=ResearchConfig(use_supervised_models=True), clock=lambda: "2026-10-15T18:00:00Z")
    assert captured["kwargs"] == {"datastore": tmp_path, "pc": "Scout", "symbols": ("ABCL", "DBX")}
    assert captured["config"].models == tuple(value["models"])
    assert captured["config"].use_supervised_models is False
    assert set(captured["adapters"]) == {"trainer", "dataset_builder"}
    # Local session evidence can exist while waiting. Operating clients still
    # cannot open until the current recipe has been selected after wake.
    assert before <= set(tmp_path.rglob("*"))
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"
    assert json.loads(path.read_text(encoding="utf-8")) == value


def migrated_recipe(**kwargs):
    return recipes.migrate_manual_recipe(recipe(), pc="Scout", symbols=("ABCL", "DBX"),
                                         config=ResearchConfig(), **kwargs)


def test_real_legacy_json_shape_and_cycle_configuration_remain_unchanged(tmp_path):
    value = recipe()
    # Exact pre-runtime-contract model dictionaries, rather than new asdict output.
    value["models"] = [
        {"name": "logistic_strong", "family": "logistic", "random_state": 20261012,
         "parameters": {"C": .05}, "calibration_fraction": .25,
         "probability_calibration": "sigmoid", "training_max_rows": 4096},
        {"name": "forest_shallow", "family": "random_forest", "random_state": 20261012,
         "parameters": {"n_estimators": 64, "max_depth": 3, "min_samples_leaf": 16},
         "calibration_fraction": .25, "probability_calibration": "sigmoid", "training_max_rows": 4096},
    ]
    rehash(value)
    path = save(tmp_path, value)
    original = path.read_bytes()
    config, runtime = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
        pc="Scout", symbols=("ABCL", "DBX"))
    assert config.models == tuple(value["models"])
    assert all("runtime_contract" not in model for model in config.models)
    assert dataset_for(runtime).manifest["manual_model_recipe"] == value
    assert path.read_bytes() == original


def test_v2_migration_has_identical_effective_fits_without_writing_or_selecting_new_models(tmp_path):
    original = recipe()
    migrated = migrated_recipe()
    evidence = recipes.manual_recipe_migration_evidence(original, migrated, pc="Scout",
        symbols=("ABCL", "DBX"), config=ResearchConfig())
    assert all(evidence[key] for key in ("effective_models_identical", "training_support_identical",
                                       "feature_contract_identical", "dataset_scope_identical"))
    assert evidence["training_runs"] == evidence["files_written"] == evidence["provider_calls"] == 0
    assert list(tmp_path.iterdir()) == []
    outputs = []
    for value in (original, migrated):
        save(tmp_path, value)
        config, runtime = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
            pc="Scout", symbols=("ABCL", "DBX"))
        outputs.append(fit(runtime, config, dataset_for(runtime)))
    assert all(out["complete"] for out in outputs)
    assert [out["probability_not_down"] for out in outputs[0]["outputs"]] == pytest.approx(
        [out["probability_not_down"] for out in outputs[1]["outputs"]], abs=1e-12)


def test_v2_saved_selection_is_independent_of_shared_recipe_and_model_defaults(tmp_path, monkeypatch):
    from ml.intraday import models
    value = migrated_recipe()
    path = save(tmp_path, value)
    original = path.read_bytes()
    monkeypatch.setattr(recipes, "_specs", lambda: pytest.fail("Peer candidate defaults were consulted"))
    monkeypatch.setitem(models.DEFAULT_PARAMETERS, "logistic", {"C": 500, "solver": "liblinear"})
    config, runtime = recipes.prepare_manual_models(ResearchConfig(lookback_days=1, source_dataset="OTHER"),
        datastore=tmp_path, pc="Scout", symbols=("ABCL", "DBX"))
    assert config.models == tuple(value["models"])
    assert config.lookback_days is None and config.source_dataset == "EQUS.MINI"
    path.write_text("a later local installation cannot mutate this session")
    dataset = dataset_for(runtime)
    assert dataset.manifest["manual_model_recipe"] == value
    assert models.build_estimator(models.configured_models(config.models)[0]).named_steps["model"].solver == "lbfgs"
    with pytest.raises(ContractError, match="history scope"):
        runtime["dataset_builder"](candles(), cutoff=dataset.manifest["training_cutoff"],
            reference_at=dataset.reference_at[-1], lookback_days=1)
    assert original == json.dumps(value).encode()


@pytest.mark.parametrize("damage", ["pc", "symbols", "feature", "source", "gate", "unfrozen"])
def test_v2_cannot_cross_local_scope_or_weaken_evidence(tmp_path, damage):
    value = migrated_recipe()
    if damage == "pc":
        value["pc"] = "Atlas"
    elif damage == "symbols":
        value["symbols"] = ["ABCL"]
    elif damage == "feature":
        value["feature_contract"]["base_feature_version"] = "another-population"
    elif damage == "source":
        value["dataset_scope"]["source_dataset"] = "OTHER"
    elif damage == "gate":
        value["training_support"]["minimum_rows"] = 159
    else:
        value["models"][0]["runtime_contract"] = None
    path = save(tmp_path, migrated_recipe())
    path.write_text(json.dumps(rehash(value)))
    with pytest.raises(ContractError):
        recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path, pc="Scout", symbols=("ABCL", "DBX"))


def test_v2_feature_pipeline_version_change_is_explicit_not_a_silent_retune(tmp_path, monkeypatch):
    save(tmp_path, migrated_recipe())
    monkeypatch.setattr(recipes, "BASE_FEATURE_VERSION", "a-new-feature-algorithm")
    with pytest.raises(ContractError, match="feature contract"):
        recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path, pc="Scout", symbols=("ABCL", "DBX"))
