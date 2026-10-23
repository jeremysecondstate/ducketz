"""Local carry-policy adapters, using only fixture data and mocked model fits."""
from copy import deepcopy
from dataclasses import replace
import json
import sys
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from ml.intraday import live_model_recipe as recipes
from ml.intraday.data import FEATURE_NAMES, IntervalResponse, SessionBounds
from ml.intraday.forward_fill import policy_contract, feature_contract, make_reference
from ml.intraday.native_interval import RECOVERY_CONTRACT
from ml.intraday.prediction import ContractError
from ml.intraday.runtime import ResearchConfig
from ml.intraday.training_timing import HISTORICAL_RESEARCH
from test_intraday_market_recipe import candidate, frame, frozen_recipe, save


def migrated(original=None):
    return recipes.migrate_forward_fill_recipe(original or candidate(), pc="Scout", symbols=("TEST",),
        installed_at="2026-05-12T23:00:00Z", authority_reference="Local fixture carry authorization")


def rehash(value):
    value.pop("sha256", None)
    value["sha256"] = recipes._sha(value)
    return value


def adapters(tmp_path, value):
    save(tmp_path, value)
    return recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
        pc="Scout", symbols=("TEST",))


def fit_arguments(config, dataset):
    return dict(config=config.models, dataset_metadata=dataset.manifest,
        reference_at=dataset.reference_at, target_close_at=dataset.target_close_at,
        label_available_at=dataset.label_available_at,
        training_cutoff=dataset.manifest["training_cutoff"], feature_names=dataset.feature_names)


def fake_fit(calls):
    def fitted(X, y, inference_X, **kwargs):
        calls.append((X.copy(), y.copy(), inference_X.copy(), kwargs))
        return {"configured_count": 2, "successful_count": 2, "complete": True,
            "consensus_status": "full", "probability_not_down": .5,
            "outputs": [{"name": name, "status": "success"}
                        for name in ("logistic_strong", "forest_shallow")]}
    return fitted


def test_migration_preserves_local_tuning_support_context_timing_and_original():
    original = candidate()
    original["historical_recovery_contract"] = RECOVERY_CONTRACT
    original = recipes.migrate_training_timing_recipe(rehash(original), pc="Scout", symbols=("TEST",),
        installed_at="2026-05-12T22:00:00Z", authority_reference="Fixture research timing",
        mode=HISTORICAL_RESEARCH)
    original_copy = deepcopy(original)
    new = migrated(original)
    assert original == original_copy
    for field in ("models", "training_support", "dataset_scope", "market_context_contract",
                  "market_collection_configuration_sha256", "training_timing_contract",
                  "historical_recovery_contract", "feature_contract", "synthetic_policy"):
        assert new[field] == original[field]
    assert new["reference_fill_policy"] == policy_contract()
    assert new["symbol_feature_contracts"] == {"TEST": feature_contract()}
    assert new["migrated_from_sha256"] == original["sha256"]
    assert new["sha256"] != original["sha256"]
    with pytest.raises(ContractError):
        recipes._validate_recipe(new, pc="Atlas", symbols=("TEST",))
    with pytest.raises(ContractError):
        recipes._validate_recipe(new, pc="Scout", symbols=("OTHER",))


def test_opt_in_requires_all_local_symbols_already_native():
    with pytest.raises(ContractError, match="native feature contract"):
        migrated(frozen_recipe())
    old = recipes.make_manual_recipe(pc="Scout", symbols=("TEST", "OTHER"),
        installed_at="2026-05-01T00:00:00Z", authority_reference="Local fixture")
    frozen = recipes.migrate_manual_recipe(old, pc="Scout", symbols=("TEST", "OTHER"), config=ResearchConfig())
    partial = recipes.migrate_native_interval_recipe(frozen, pc="Scout", symbols=("TEST", "OTHER"),
        native_symbols=("TEST",), installed_at="2026-05-02T00:00:00Z", authority_reference="Local native fixture")
    with pytest.raises(ContractError, match="every local symbol"):
        recipes.migrate_forward_fill_recipe(partial, pc="Scout", symbols=("TEST", "OTHER"),
            installed_at="2026-05-03T00:00:00Z", authority_reference="Local carry fixture")


@pytest.mark.parametrize("damage", ["policy", "contract", "missing_contract", "missing_policy", "support", "null_policy"])
def test_rehashed_invalid_contract_never_weakens_recipe(damage):
    value = migrated()
    if damage == "policy":
        value["reference_fill_policy"]["labels"] = "include_derived"
    elif damage == "contract":
        value["symbol_feature_contracts"]["TEST"] = recipes._native_feature_contract()
    elif damage == "missing_contract":
        value["symbol_feature_contracts"] = {}
    elif damage == "missing_policy":
        value.pop("reference_fill_policy")
    elif damage == "null_policy":
        value["reference_fill_policy"] = None
    else:
        value["training_support"]["minimum_rows"] = 1
    with pytest.raises(ContractError):
        recipes._validate_recipe(rehash(value), pc="Scout", symbols=("TEST",))


def test_observed_inputs_keep_training_population_and_models(tmp_path, monkeypatch):
    old = candidate()
    old_config, old_adapters = adapters(tmp_path / "old", old)
    new_config, new_adapters = adapters(tmp_path / "new", migrated(old))
    assert old_adapters["dataset_builder"].reference_fill_policy is None
    assert new_adapters["dataset_builder"].reference_fill_policy == policy_contract()
    assert new_config.models == old_config.models
    data = frame()
    reference = pd.Timestamp(data.opened_at.iloc[-1])
    kwargs = dict(cutoff=reference + pd.Timedelta(minutes=16), reference_at=reference,
        lookback_days=None, synthetic_policy="exclude")
    before = old_adapters["dataset_builder"](data, **kwargs)
    after = new_adapters["dataset_builder"](data, **kwargs)
    np.testing.assert_array_equal(before.X, after.X)
    np.testing.assert_array_equal(before.y, after.y)
    np.testing.assert_array_equal(before.inference_X, after.inference_X)
    assert before.manifest["manual_recipe_training_support"] == after.manifest["manual_recipe_training_support"]
    assert after.manifest["reference_price_basis"] == "observed_native"
    calls = []
    monkeypatch.setattr(recipes, "fit_ensemble", fake_fit(calls))
    new_adapters["trainer"](after.X, after.y, after.inference_X, **fit_arguments(new_config, after))
    assert len(calls) == 1
    changed = after.inference_X.copy()
    changed[0, FEATURE_NAMES.index("synthetic_input_fraction20")] = 1 / 20
    with pytest.raises(ContractError, match="feature matrices"):
        new_adapters["trainer"](after.X, after.y, changed, **fit_arguments(new_config, after))
    assert len(calls) == 1


def test_old_recipe_cannot_admit_new_evidence_or_change_mid_session(tmp_path, monkeypatch):
    original = candidate()
    config, runtime = adapters(tmp_path, original)
    save(tmp_path, migrated(original))
    assert runtime["dataset_builder"].reference_fill_policy is None
    data = frame()
    reference = pd.Timestamp(data.opened_at.iloc[-1])
    dataset = runtime["dataset_builder"](data, cutoff=reference + pd.Timedelta(minutes=16),
        reference_at=reference, lookback_days=None, synthetic_policy="exclude")
    calls = []
    monkeypatch.setattr(recipes, "fit_ensemble", fake_fit(calls))
    metadata = {**dataset.manifest, "reference_fill_policy": policy_contract()}
    with pytest.raises(ContractError, match="explicit local recipe"):
        runtime["trainer"](dataset.X, dataset.y, dataset.inference_X,
            **{**fit_arguments(config, dataset), "dataset_metadata": metadata})
    assert not calls


def carried_inputs():
    data = frame()
    reference = pd.Timestamp(data.opened_at.iloc[-1]) - pd.Timedelta(minutes=30)
    genuine = data.loc[pd.to_datetime(data.opened_at, utc=True).lt(reference)].copy()
    prior = genuine.iloc[-1]
    session = SessionBounds(prior.session_id, prior.session_open, prior.session_close)
    closed = reference + pd.Timedelta(minutes=15)
    first = IntervalResponse("empty-one", "TEST", "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI",
        reference.isoformat(), closed.isoformat(), (closed + pd.Timedelta(seconds=1)).isoformat(),
        (closed + pd.Timedelta(seconds=2)).isoformat(), "success", (),
        reference.isoformat(), closed.isoformat())
    second = replace(first, attempt_id="empty-two",
        request_started_at=(closed + pd.Timedelta(seconds=6)).isoformat(),
        request_ended_at=(closed + pd.Timedelta(seconds=7)).isoformat())
    derived = make_reference(genuine, (first, second), session=session)
    projected = pd.concat([genuine, pd.DataFrame([derived])], ignore_index=True)
    return genuine, projected, reference, closed + pd.Timedelta(seconds=8)


def test_carried_reference_uses_existing_genuine_training_and_purged_support(tmp_path, monkeypatch):
    original = candidate()
    _, old = adapters(tmp_path / "old", original)
    config, runtime = adapters(tmp_path / "new", migrated(original))
    genuine, projected, reference, cutoff = carried_inputs()
    genuine_before = genuine.copy(deep=True)
    before = old["dataset_builder"](genuine, cutoff=cutoff,
        reference_at=pd.Timestamp(genuine.opened_at.iloc[-1]), lookback_days=None, synthetic_policy="exclude")
    after = runtime["dataset_builder"](projected, cutoff=cutoff, reference_at=reference,
        lookback_days=None, synthetic_policy="exclude")
    pd.testing.assert_frame_equal(genuine, genuine_before)
    np.testing.assert_array_equal(before.X, after.X)
    np.testing.assert_array_equal(before.y, after.y)
    assert before.manifest["manual_recipe_training_support"] == after.manifest["manual_recipe_training_support"]
    assert after.manifest["synthetic_labels_included"] == 0
    assert after.manifest["reference_price_basis"] == "forward_filled_last_genuine_close"
    assert after.inference_X[0, FEATURE_NAMES.index("synthetic_input_fraction20")] == 1 / 20
    assert after.inference["reference_open"] == float(genuine.close.iloc[-1])
    calls = []
    monkeypatch.setattr(recipes, "fit_ensemble", fake_fit(calls))
    runtime["trainer"](after.X, after.y, after.inference_X, **fit_arguments(config, after))
    assert len(calls) == 1
    with pytest.raises(ContractError):
        old["dataset_builder"](projected, cutoff=cutoff, reference_at=reference,
            lookback_days=None, synthetic_policy="exclude")


@pytest.mark.parametrize("damage", ["training", "inference", "missing_proof", "tampered_proof", "policy"])
def test_carried_fit_rejects_unsupported_inputs_before_model_call(tmp_path, monkeypatch, damage):
    config, runtime = adapters(tmp_path, migrated())
    _, projected, reference, cutoff = carried_inputs()
    dataset = runtime["dataset_builder"](projected, cutoff=cutoff, reference_at=reference,
        lookback_days=None, synthetic_policy="exclude")
    features, inference, metadata = dataset.X.copy(), dataset.inference_X.copy(), deepcopy(dataset.manifest)
    fraction = FEATURE_NAMES.index("synthetic_input_fraction20")
    if damage == "training":
        features[0, fraction] = 1 / 20
    elif damage == "inference":
        inference[0, fraction] = 0
    elif damage == "missing_proof":
        metadata["reference_fill_reference"] = None
    elif damage == "tampered_proof":
        metadata["reference_fill_reference"]["volume"] = 1
    else:
        metadata["reference_fill_policy"] = {"version": "unreviewed"}
    calls = []
    monkeypatch.setattr(recipes, "fit_ensemble", fake_fit(calls))
    with pytest.raises(ContractError):
        runtime["trainer"](features, dataset.y, inference,
            **{**fit_arguments(config, dataset), "dataset_metadata": metadata})
    assert not calls


def test_explicit_live_recipe_is_separate_and_does_not_change_frozen_default(tmp_path):
    original = candidate()
    save(tmp_path, original)
    default = tmp_path / "intraday/scout/manual-model-recipe.json"
    original_bytes = default.read_bytes()
    selected = tmp_path / "live-only-recipe.json"
    selected.write_text(json.dumps(migrated(original)), encoding="utf-8")
    _, live = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
        pc="Scout", symbols=("TEST",), recipe_path=selected)
    _, study = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
        pc="Scout", symbols=("TEST",))
    assert live["dataset_builder"].reference_fill_policy == policy_contract()
    assert study["dataset_builder"].reference_fill_policy is None
    assert default.read_bytes() == original_bytes
    with pytest.raises(ContractError, match="Explicit manual model recipe is missing"):
        recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
            pc="Scout", symbols=("TEST",), recipe_path=tmp_path / "missing-live-recipe.json")
    selected.write_text("{}", encoding="utf-8")
    with pytest.raises(ContractError, match="checksum"):
        recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
            pc="Scout", symbols=("TEST",), recipe_path=selected)
    assert default.read_bytes() == original_bytes


@pytest.mark.parametrize("entrypoint", ["manual_launcher", "live_session"])
def test_explicit_recipe_cli_preserves_original_repository_and_data(tmp_path, monkeypatch, entrypoint):
    from ml.intraday import live_session, manual_launcher
    root, data = tmp_path / "repository", tmp_path / "datastore"
    recipe_path = tmp_path / "isolated-runtime" / "live-recipe.json"
    captured = []
    def engine(**options):
        captured.append(options)
        return {"status": "STOPPED"}
    monkeypatch.setattr(live_session, "run_manual", engine)
    monkeypatch.setattr(manual_launcher, "local_symbols", lambda *args: ("Scout", ("TEST",)))
    argv = ["--live", "--repository", str(root), "--datastore", str(data),
        "--model-recipe", str(recipe_path)]
    result = (manual_launcher.main(argv, clock=lambda: "2026-10-19T12:00:00Z")
              if entrypoint == "manual_launcher" else live_session.main(argv))
    assert result == 0 and len(captured) == 1
    assert captured[0]["model_recipe"] == recipe_path
    assert captured[0]["repository"] == root and captured[0]["datastore"] == data
    assert captured[0]["profile"] == root / "scratch/cross-pc/local-profile.json"
    assert captured[0]["watchlist"] == root / "datafetching/watchlist.local.txt"


def test_explicit_missing_recipe_stops_before_operating_clients(tmp_path, monkeypatch):
    from ml.intraday import live_session, data
    save(tmp_path, candidate())
    def forbidden(*args, **kwargs):
        pytest.fail("Explicit missing recipe must fail before provider or broker work")
    for name, attribute in (("app.services.schwab", "SchwabSession"),
                            ("app.services.databento_market_data", "DatabentoMarketDataProvider")):
        module = ModuleType(name)
        setattr(module, attribute, forbidden)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(data, "AcquisitionJournal", forbidden)
    monkeypatch.setattr(data, "load_local_history", forbidden)
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Scout", ("TEST",)))
    with pytest.raises(ContractError, match="Explicit manual model recipe is missing"):
        live_session.run_manual(repository=tmp_path, datastore=tmp_path,
            profile=tmp_path / "profile.json", watchlist=tmp_path / "watchlist.txt",
            config=ResearchConfig(), model_recipe=tmp_path / "missing.json",
            clock=lambda: "2026-10-19T12:00:00Z")
