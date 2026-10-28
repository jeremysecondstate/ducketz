"""Bounded 1A scheduling with prerecorded histories and no production fitting."""
from datetime import timedelta
import json
import threading
import time

import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=("tests/test_intraday_split_services.py",),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/runtime.py", "ShadowRunner", "__init__", ("datastore",))))

from ml.intraday.prediction import STEP, digest, instant
from ml.intraday.runtime import ShadowRunner
from ml.intraday.split_service import DataService, PreparationScheduler
from ml.intraday.store import ShadowStore
from tests.test_intraday_split_services import _environment, no_network


def scheduler_for(env, tmp_path, *, builder=None):
    services = {}
    def service(symbol):
        runner = ShadowRunner(store=ShadowStore(tmp_path / (symbol + ".sqlite3")),
            pc="Atlas", symbols=env.symbols, config=env.config, clock=env.clock,
            dataset_builder=builder or env.runner.dataset_builder,
            trainer=lambda *a, **k: pytest.fail("Data preparation trained a model"))
        services[symbol] = DataService(runner=runner, handoff=env.handoff,
            incoming=env.service.incoming, session_id="fixture-data", clock=env.clock,
            assigned_symbols=(symbol,))
        return services[symbol]
    scheduler = PreparationScheduler(runner=env.runner, handoff=env.handoff,
        service_factory=service, session_id="fixture-data", clock=env.clock)
    return scheduler, services


def wait_for(predicate):
    until = time.monotonic() + 15
    while not predicate():
        assert time.monotonic() < until, "Offline preparation fixture timed out"
        time.sleep(.01)


def test_fast_symbol_prepares_next_quarter_while_slow_previous_quarter_remains_busy(tmp_path):
    env = _environment(tmp_path)
    reference = env.now - timedelta(minutes=16)
    entered, release = threading.Event(), threading.Event()
    def build(frame, **kwargs):
        if frame.iloc[0]["symbol"] == "AAPL" and kwargs["reference_at"] == reference:
            entered.set()
            assert release.wait(15)
        return env.runner.dataset_builder(frame, **kwargs)
    scheduler, services = scheduler_for(env, tmp_path, builder=build)
    try:
        first = scheduler.pass_once()
        assert entered.wait(10)
        wait_for(lambda: scheduler.running["IONQ"][1].done())
        env.now += STEP
        second = scheduler.pass_once()
        assert len(scheduler.running) == 2
        wait_for(lambda: scheduler.running["IONQ"][1].done())
        assert not scheduler.running["AAPL"][1].done()
        scheduler.poll()
        from dataclasses import asdict
        ready = env.handoff.ready(env.clock(), configuration_hash=digest(asdict(env.config)))
        latest = next(row for row in ready if row["symbol"] == "IONQ")
        assert latest["reference_at"] == (reference + STEP).isoformat()
        assert latest["target_close"] == (reference + 4 * STEP).isoformat()
        assert first["cycle_id"] != second["cycle_id"]
        assert services["IONQ"].runner.symbols == env.symbols
        assert services["IONQ"].runner.config == env.config
    finally:
        release.set()
        scheduler.close()
    # The old symbol keeps its original reference; no wall-clock rollover.
    with services["AAPL"].runner.store.connect() as db:
        original = json.loads(db.execute("SELECT payload FROM cycles").fetchone()[0])
    assert original["reference_at"] == reference.isoformat()
    assert all(service.runner.store.forecasts() == [] for service in services.values())


def test_policy_envelope_freezes_once_with_empty_explicit_subset_and_keeps_existing_progress(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    scheduler, services = scheduler_for(env, tmp_path)
    calls = []
    original = env.runner.prepare_cycle
    def freeze(*args, **kwargs):
        calls.append(kwargs["retry_symbols"])
        return original(*args, **kwargs)
    monkeypatch.setattr(env.runner, "prepare_cycle", freeze)
    try:
        first = scheduler.pass_once()
        wait_for(lambda: all(future.done() for _, future in scheduler.running.values()))
        scheduler.poll()
        assert calls == [[]]
        # The common store owns the original cycle/policy, never matrices.
        with env.runner.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM symbol_inputs").fetchone()[0] == 0
            common = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (first["cycle_id"],)).fetchone()[0])
        for service in services.values():
            with service.runner.store.connect() as db:
                lane = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (first["cycle_id"],)).fetchone()[0])
            for field in ("configuration", "configuration_hash", "training_cutoff", "supervised_model_policy", "symbols"):
                assert lane[field] == common[field]
        env.runner.store.update_cycle(first["cycle_id"], {"review_marker": "retained-original-progress"})
        scheduler.pass_once()
        with env.runner.store.connect() as db:
            after = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (first["cycle_id"],)).fetchone()[0])
        assert calls == [[]] and after["review_marker"] == "retained-original-progress"
    finally:
        scheduler.close()


def test_bounded_allocation_rotates_past_unavailable_symbol_without_queued_backlog(tmp_path):
    from dataclasses import replace
    from ml.intraday.data import AcquisitionPending
    env = _environment(tmp_path)
    env.config = env.runner.config = replace(env.config, symbol_workers=1)
    def build(frame, **kwargs):
        if frame.iloc[0]["symbol"] == "AAPL":
            raise AcquisitionPending("fixture_unavailable", retry_after_seconds=120)
        return env.runner.dataset_builder(frame, **kwargs)
    scheduler, _ = scheduler_for(env, tmp_path, builder=build)
    try:
        scheduler.pass_once()
        assert list(scheduler.running) == ["AAPL"]
        wait_for(lambda: scheduler.running["AAPL"][1].done())
        scheduler.pass_once()
        assert list(scheduler.running) == ["IONQ"]
        wait_for(lambda: scheduler.running["IONQ"][1].done())
        scheduler.poll()
        from dataclasses import asdict
        ready = env.handoff.ready(env.clock(), configuration_hash=digest(asdict(env.config)))
        assert [row["symbol"] for row in ready] == ["IONQ"]
    finally:
        scheduler.close()


def test_unpublished_original_snapshot_survives_preparation_lane_adoption(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    reference = env.now - timedelta(minutes=16)
    experiment = "system-1a:atlas:" + reference.astimezone(__import__("ml.intraday.prediction", fromlist=["PACIFIC"]).PACIFIC).date().isoformat()
    before = env.runner.prepare_cycle(env.service.incoming(reference), reference_at=reference, experiment=experiment)
    originals = {symbol: env.runner.store.inputs(before["cycle_id"], symbol) for symbol in env.symbols}
    scheduler, services = scheduler_for(env, tmp_path,
        builder=lambda *a, **k: pytest.fail("Adoption reconstructed an already frozen original"))
    try:
        scheduler.pass_once()
        wait_for(lambda: all(future.done() for _, future in scheduler.running.values()))
        result = scheduler.poll()
        assert len(result) == 2 and all(value["status"] == "prepared" for value in result)
        for symbol, service in services.items():
            assert service.runner.store.inputs(before["cycle_id"], symbol) == originals[symbol]
    finally:
        scheduler.close()
