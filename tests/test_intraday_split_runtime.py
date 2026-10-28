"""Offline parity and progress checks for the data/model process boundary."""
from datetime import timedelta
import json
import threading

import numpy as np
import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=(
    "ml/intraday/prediction.py", "ml/intraday/store.py", "tests/test_intraday_runtime.py"),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/runtime.py", "ShadowRunner", "__init__",
                 ("prepared_source", "forecast_preparer", "datastore"))))

from ml.intraday.prediction import digest
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.store import ShadowStore
from tests.test_intraday_runtime import REF, dataset, history, trained


def make_runner(path, **options):
    defaults = dict(store=ShadowStore(path / "shadow.sqlite3"), pc="Atlas",
        symbols=("AAPL", "IONQ"), config=ResearchConfig(models=("fixture_a", "fixture_b"), symbol_workers=2),
        dataset_builder=dataset, trainer=trained, clock=lambda: REF + timedelta(minutes=16))
    defaults.update(options)
    return ShadowRunner(**defaults)


def test_preparation_publishes_ready_symbol_while_other_symbol_waits_without_training(tmp_path):
    release, published = threading.Event(), threading.Event()
    received = {}

    class Histories:
        def get(self, symbol):
            if symbol == "IONQ":
                assert release.wait(10)
            return history(symbol)

    runner = make_runner(tmp_path, trainer=lambda *a, **k: pytest.fail("1A trained a model"))

    def prepared(cycle_id, symbol, cycle, frozen):
        received[symbol] = (cycle_id, cycle, frozen)
        if symbol == "AAPL":
            published.set()

    results = []
    thread = threading.Thread(target=lambda: results.append(runner.prepare_cycle(
        Histories(), reference_at=REF, experiment="data-service", on_prepared=prepared)))
    thread.start()
    try:
        assert published.wait(10)
        assert thread.is_alive()
        assert set(received) == {"AAPL"}
        assert not runner.store.forecasts(pc="Atlas")
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert results[0]["status"] == "prepared"
    assert set(received) == {"AAPL", "IONQ"}
    assert all(frozen[0] == digest(frozen[1]) for _, _, frozen in received.values())


def test_split_consumer_preserves_exact_science_and_deadlines_without_history_or_selection_reads(tmp_path):
    original_calls, split_calls = [], []

    def recorder(target):
        def train(*args, **kwargs):
            target.append((tuple(np.asarray(a).tolist() for a in args), {
                key: kwargs[key] for key in ("config", "target_version", "training_cutoff",
                    "label_available_at", "feature_names", "reference_at", "target_close_at")}))
            return trained(*args, **kwargs)
        return train

    local_config = ResearchConfig(symbol_workers=2)
    original = make_runner(tmp_path / "original", symbols=("AAPL",), config=local_config,
        trainer=recorder(original_calls))
    direct = original.run_cycle({"AAPL": history("AAPL")}, reference_at=REF, mode="live", experiment="manual")
    data = make_runner(tmp_path / "producer", symbols=("AAPL",), config=local_config,
        trainer=lambda *a, **k: pytest.fail("preparation trained"))
    prepared = {}
    data.prepare_cycle({"AAPL": history("AAPL")}, reference_at=REF, experiment="data-service",
        on_prepared=lambda cid, symbol, cycle, frozen: prepared.update({symbol: frozen}))

    class NoHistories:
        def get(self, symbol):
            pytest.fail("model worker read acquisition history")

    consumer = make_runner(tmp_path / "consumer", symbols=("AAPL",), config=local_config,
        trainer=recorder(split_calls),
        prepared_source=lambda cid, symbol: prepared[symbol],
        dataset_builder=lambda *a, **k: pytest.fail("model worker rebuilt features"),
        clock=lambda: REF + timedelta(minutes=17), selected_config_path=tmp_path / "invalid-active.json")
    consumer.selected_config_path.write_text("invalid selection must never be read by 1B")
    split = consumer.run_cycle(NoHistories(), reference_at=REF, mode="live", experiment="manual",
        acquisition={"data_handoff": "original producer snapshot"})
    assert original_calls == split_calls
    left, right = direct["forecasts"]["AAPL"], split["forecasts"]["AAPL"]
    for key in ("reference_at", "reference_open", "target_open", "target_close", "training_cutoff",
                "target_version", "probability_not_down", "features", "dataset_scope"):
        assert right[key] == left[key]
    assert right["snapshot_hash"] == prepared["AAPL"][0]
    assert consumer.store.inputs(split["cycle_id"], "AAPL") == prepared["AAPL"]


def test_preparation_retry_reuses_original_cutoff_and_frozen_bytes(tmp_path):
    runner = make_runner(tmp_path, symbols=("AAPL",))
    received = []
    first = runner.prepare_cycle({"AAPL": history("AAPL")}, reference_at=REF,
        experiment="data", on_prepared=lambda *args: received.append(args))
    runner.clock = lambda: REF + timedelta(minutes=25)
    runner.dataset_builder = lambda *a, **k: pytest.fail("immutable preparation rebuilt")
    second = runner.prepare_cycle({}, reference_at=REF, experiment="data",
        on_prepared=lambda *args: received.append(args))
    assert first["cycle_id"] == second["cycle_id"]
    assert received[0][3] == received[1][3]
    assert received[1][3][1]["cutoff"] == (REF + timedelta(minutes=16)).isoformat()
    with runner.store.connect() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 0


def test_consumer_rejects_changed_snapshot_before_training(tmp_path):
    data = make_runner(tmp_path / "data", symbols=("AAPL",))
    prepared = []
    data.prepare_cycle({"AAPL": history("AAPL")}, reference_at=REF,
        on_prepared=lambda *args: prepared.append(args[3]))
    snapshot_id, snapshot = prepared[0]
    changed = json.loads(json.dumps(snapshot))
    changed["y"][0] = 1
    consumer = make_runner(tmp_path / "model", symbols=("AAPL",),
        prepared_source=lambda cid, symbol: (snapshot_id, changed),
        trainer=lambda *a, **k: pytest.fail("corrupt snapshot trained"))
    result = consumer.run_cycle({}, reference_at=REF, mode="live")
    assert not result["forecasts"]
    assert result["failures"]["AAPL"]["type"] == "ContractError"
    assert "immutable identity" in result["failures"]["AAPL"]["message"]
    assert consumer.store.inputs(result["cycle_id"], "AAPL") is None


def test_data_service_uses_original_datastore_market_inputs_and_selected_recipe(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr("ml.intraday.market_inputs.MarketInputStore", lambda root: captured.append(root))
    source_root = tmp_path / "original"
    runner = make_runner(tmp_path / "system-1a", datastore=source_root,
        config=ResearchConfig(models=("fixture",), market_inputs_enabled=True))
    assert runner.selected_config_path == source_root / "intraday/model-supervision/active.json"
    assert captured == [source_root]
    make_runner(tmp_path / "model", datastore=source_root,
        config=ResearchConfig(models=("fixture",), market_inputs_enabled=True), prepared_source=lambda *a: None)
    assert captured == [source_root]


def test_preparation_separate_database_preserves_original_own_model_policy_binding(tmp_path, monkeypatch):
    roots = []
    monkeypatch.setattr("ml.intraday.model_policy.load_active_policy",
        lambda root, **kwargs: roots.append(root))
    datastore = tmp_path / "original-local-datastore"
    data = make_runner(tmp_path / "system-1a", datastore=datastore,
        config=ResearchConfig(models=("fixture_a",), use_supervised_models=True))
    result = data.prepare_cycle({symbol: history(symbol) for symbol in data.symbols}, reference_at=REF)
    assert result["status"] == "prepared"
    assert roots == [datastore / "intraday/supervision"] * 2


def test_execution_preparation_time_is_measured_only_for_opted_in_model_worker(tmp_path):
    original = make_runner(tmp_path / "original", symbols=("AAPL",))
    legacy = original.run_cycle({"AAPL": history("AAPL")}, reference_at=REF, mode="live")
    assert "execution_preparation_seconds" not in legacy["forecasts"]["AAPL"]
    opted_in = make_runner(tmp_path / "split", symbols=("AAPL",),
                          forecast_preparer=lambda payload, snapshot, fitted: dict(payload))
    split = opted_in.run_cycle({"AAPL": history("AAPL")}, reference_at=REF, mode="live")
    value = split["forecasts"]["AAPL"]
    assert value["execution_preparation_seconds"] >= 0
    assert value["symbol_seconds_before_persistence"] >= value["execution_preparation_seconds"]
