"""Offline, local recipe migration and production-adapter feature contract checks."""
from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from ml.intraday import live_model_recipe as recipes
from ml.intraday.data import IntervalResponse, SessionBounds, FEATURE_NAMES
from ml.intraday.native_interval import resolve_native_interval, CONTRACT
from ml.intraday.prediction import ContractError
from ml.intraday.runtime import ResearchConfig


def frozen_recipe():
    old = recipes.make_manual_recipe(pc="Scout", symbols=("TEST",),
        installed_at="2026-05-01T00:00:00Z", authority_reference="Local fixture authority")
    return recipes.migrate_manual_recipe(old, pc="Scout", symbols=("TEST",), config=ResearchConfig())


def candidate():
    return recipes.migrate_market_context_recipe(frozen_recipe(), pc="Scout", symbols=("TEST",),
        collection_configuration_sha256="a" * 64,
        source_ids={"stock_quote": "schwab:quote"}, installed_at="2026-05-02T00:00:00Z",
        authority_reference="Local actual observations and causal market context")


def save(tmp_path, value):
    path = tmp_path / "intraday/scout/manual-model-recipe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def frame(*, late_reference=False):
    candles = []
    for day in pd.bdate_range("2026-05-04", periods=6):
        first = day.tz_localize("UTC") + pd.Timedelta(hours=13, minutes=30)
        session = SessionBounds(str(day.date()), first.isoformat(),
                                (first + pd.Timedelta(hours=10)).isoformat())
        for quarter in range(38):
            opened = first + pd.Timedelta(minutes=15 * quarter)
            closed = opened + pd.Timedelta(minutes=15)
            price = 100 + (quarter % 7) / 10
            rows = ({"opened_at": (opened + pd.Timedelta(minutes=2)).isoformat(),
                     "open": price, "high": price + 1, "low": price - 1,
                     "close": price + .1, "volume": 100 + quarter},)
            one = IntervalResponse(f"{opened}-1", "TEST", "databento:EQUS.MINI:ohlcv-1m",
                "EQUS.MINI", opened.isoformat(), closed.isoformat(),
                (closed + pd.Timedelta(seconds=1)).isoformat(),
                (closed + pd.Timedelta(seconds=2)).isoformat(), "success", rows,
                opened.isoformat(), closed.isoformat())
            two = replace(one, attempt_id=f"{opened}-2",
                request_started_at=(closed + pd.Timedelta(seconds=6)).isoformat(),
                request_ended_at=(closed + pd.Timedelta(seconds=7)).isoformat())
            if late_reference and day == pd.Timestamp("2026-05-06") and quarter == 10:
                one = replace(one, request_started_at=(closed + pd.Timedelta(minutes=16)).isoformat(),
                              request_ended_at=(closed + pd.Timedelta(minutes=16, seconds=1)).isoformat())
                two = replace(two, request_started_at=(closed + pd.Timedelta(minutes=16, seconds=5)).isoformat(),
                              request_ended_at=(closed + pd.Timedelta(minutes=16, seconds=6)).isoformat())
            candles.append(resolve_native_interval([one, two], session=session)["candle"])
    return pd.DataFrame(candles)


def test_local_migration_preserves_models_support_and_rejects_peer_scope():
    old, new = frozen_recipe(), candidate()
    assert old["models"] == new["models"]
    assert old["training_support"] == new["training_support"]
    assert old["dataset_scope"] == new["dataset_scope"]
    assert new["migrated_from_sha256"] == old["sha256"]
    assert new["symbol_feature_contracts"]["TEST"]["native_interval_contract"] == CONTRACT
    with pytest.raises(ContractError):
        recipes._validate_recipe(new, pc="Atlas", symbols=("TEST",))


def test_production_adapter_uses_frozen_expanded_features_without_backfilling(tmp_path, monkeypatch):
    new = candidate()
    save(tmp_path, new)
    config, adapters = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
        pc="Scout", symbols=("TEST",))
    data = frame()
    reference = pd.Timestamp(data.opened_at.iloc[-1])
    dataset = adapters["dataset_builder"](data, cutoff=reference + pd.Timedelta(minutes=16),
        reference_at=reference, lookback_days=None, synthetic_policy="exclude")
    assert dataset.X.shape[1] > len(FEATURE_NAMES)
    assert tuple(dataset.feature_names[:len(FEATURE_NAMES)]) == FEATURE_NAMES
    assert dataset.manifest["market_context"]["contract"] == new["market_context_contract"]
    assert dataset.manifest["native_interval_contract"] == CONTRACT
    assert np.isfinite(dataset.X).all()
    assert (dataset.X[:, FEATURE_NAMES.index("synthetic_input_fraction20")] == 0).all()
    seen = []
    def fitted(X, y, inference_X, **kwargs):
        seen.append((X.shape, kwargs["feature_names"]))
        return {"configured_count": 2, "successful_count": 2, "complete": True,
                "consensus_status": "full", "probability_not_down": .5,
                "outputs": [{"name": name, "status": "success"}
                            for name in ("logistic_strong", "forest_shallow")]}
    monkeypatch.setattr(recipes, "fit_ensemble", fitted)
    kwargs = dict(config=config.models, dataset_metadata=dataset.manifest,
        reference_at=dataset.reference_at, target_close_at=dataset.target_close_at,
        label_available_at=dataset.label_available_at, training_cutoff=reference + pd.Timedelta(minutes=16),
        feature_names=dataset.feature_names)
    adapters["trainer"](dataset.X, dataset.y, dataset.inference_X, **kwargs)
    assert seen[0][0] == dataset.X.shape
    bad = deepcopy(dataset.manifest)
    bad["market_context"]["contract"]["source_ids"] = {}
    with pytest.raises(ContractError):
        adapters["trainer"](dataset.X, dataset.y, dataset.inference_X,
            **{**kwargs, "dataset_metadata": bad})


def test_cannot_rehash_a_recipe_to_weaken_gates_or_feature_order():
    for change in (lambda v: v["training_support"].update(minimum_rows=1),
                   lambda v: v["market_context_contract"]["feature_names"].reverse(),
                   lambda v: v.update(symbol_feature_contracts={})):
        value = candidate()
        change(value)
        value.pop("sha256")
        value["sha256"] = recipes._sha(value)
        with pytest.raises((ContractError, ValueError)):
            recipes._validate_recipe(value, pc="Scout", symbols=("TEST",))


def test_late_reference_excludes_its_row_without_disabling_future_fits(tmp_path):
    save(tmp_path, candidate())
    _, adapters = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
        pc="Scout", symbols=("TEST",))
    data = frame(late_reference=True)
    reference = pd.Timestamp(data.opened_at.iloc[-1])
    dataset = adapters["dataset_builder"](data, cutoff=reference + pd.Timedelta(minutes=16),
        reference_at=reference, lookback_days=None, synthetic_policy="exclude")
    assert dataset.manifest["context_decision_rows_excluded"] == 1
    assert pd.Timestamp("2026-05-06T16:00:00Z") not in dataset.reference_at
    assert len(dataset.y) >= 160


def test_opt_in_selects_live_primary_without_historical_provider(tmp_path, monkeypatch):
    from ml.intraday import live_session, minute_persistence
    from ml.intraday.live_minute_provider import LiveMinuteReplayProvider
    import app.services.databento_market_data as historical
    def forbidden():
        raise AssertionError("Historical constructor called for a live-context recipe")
    monkeypatch.setattr(historical, "DatabentoMarketDataProvider", forbidden)
    monkeypatch.setattr(minute_persistence, "PersistedMinuteProvider", lambda p, **kwargs: p)
    builder = lambda: None
    builder.market_context_contract = candidate()["market_context_contract"]
    provider = live_session.prepare_minute_provider({"dataset_builder": builder},
        datastore=tmp_path, pc="Scout", symbols=("TEST",), source_dataset="EQUS.MINI")
    assert isinstance(provider, LiveMinuteReplayProvider)
    with pytest.raises(ContractError):
        live_session.prepare_minute_provider({"dataset_builder": builder}, datastore=tmp_path,
            pc="Scout", symbols=("TEST",), source_dataset="OTHER")


def test_collection_latches_private_configuration_and_source_contract(tmp_path, monkeypatch):
    from ml.intraday import live_session, market_collection as collection
    from ml.intraday.market_context import market_context_contract
    from dataclasses import asdict
    request = collection.SourceRequest("schwab", "stock_quote", "TEST", "marketdata-v1", "quote", "quotes")
    config = {"requests": [asdict(request)], "configuration_sha256": "b" * 64}
    builder = lambda: None
    builder.market_context_contract = market_context_contract({"stock_quote": [request.source_id]})
    builder.market_collection_configuration_sha256 = "b" * 64
    monkeypatch.setattr(collection, "load_local_collection_config", lambda *args: config)
    sentinel = object()
    monkeypatch.setattr(collection, "collector_from_config", lambda *args, **kwargs: sentinel)
    arguments = dict(datastore=tmp_path, pc="Scout", symbols=("TEST",))
    assert live_session.prepare_market_collection({}, **arguments) is None
    assert live_session.prepare_market_collection({"dataset_builder": builder}, **arguments) is sentinel
    config["configuration_sha256"] = "c" * 64
    with pytest.raises(ContractError):
        live_session.prepare_market_collection({"dataset_builder": builder}, **arguments)
