"""Multi-market lifecycle tests with local data and controlled worker threads."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from filelock import FileLock, Timeout
import pytest

from ml import hyperliquid_coordinator as manager


def write_config(path: Path, *, symbols=("BTC", "ETH"), **settings) -> Path:
    value = {
        "version": 1,
        "symbols": list(symbols),
        "output_root": str(path.parent / "data"),
        "interval": "15m",
        "max_parallel_updates": 2,
    }
    value.update(settings)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_status_reader_retries_transient_windows_replace_conflict(tmp_path, monkeypatch):
    directory = tmp_path / "_coordinator"
    directory.mkdir()
    path = directory / "coordinator_status.json"
    path.write_text(json.dumps({"status": "stopped", "pid": 123}), encoding="utf-8")
    real_read = Path.read_text
    attempts = []
    sleeps = []

    def read(candidate, *args, **kwargs):
        if candidate == path:
            attempts.append(candidate)
            if len(attempts) < 3:
                raise PermissionError("atomic replacement briefly denies a reader")
        return real_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(manager.time, "sleep", sleeps.append)
    status = manager.read_status(tmp_path)
    assert len(attempts) == 3
    assert sleeps == [0.01, 0.02]
    assert status["status"] == "stopped"
    assert status["pid"] == 123
    assert status["coordinator_running"] is False


def test_status_reader_does_not_hide_persistent_access_failure(tmp_path, monkeypatch):
    directory = tmp_path / "_coordinator"
    directory.mkdir()
    path = directory / "coordinator_status.json"
    path.write_text('{"status":"stopped"}', encoding="utf-8")
    sleeps = []

    def read(*args, **kwargs):
        raise PermissionError("access remains denied")

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(manager.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError, match="remains denied"):
        manager.read_status(tmp_path)
    assert sleeps == [0.01, 0.02]


def eventually(predicate, *, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(0.005)
    pytest.fail("The controlled worker did not reach the expected state.")


def market_done(coordinator, key):
    item = coordinator.tick()["markets"].get(key)
    return item and not item["thread_alive"]


def result_for(kwargs):
    close_ms = kwargs["as_of_ms"] // 900_000 * 900_000
    return {
        "status": "published", "rows": 5000, "new_candles": 1,
        "last_close_utc": datetime.fromtimestamp(close_ms / 1000, timezone.utc).isoformat(),
    }


def one_refresh(config, *, max_cycles, stop_event, pipeline_runner):
    result = pipeline_runner(
        coin=config.coin, interval=config.interval, output_root=config.output_root,
        info_url=config.info_url, as_of_ms=1000, refresh_history=False,
    )
    return {"status": "stopped", "reason": "max_cycles", "last_result": result}


class HoldingLoops:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, config, *, max_cycles, stop_event, pipeline_runner):
        with self.lock:
            self.calls.append((config, stop_event, max_cycles))
        if not stop_event.wait(5):
            raise RuntimeError("Test worker was not stopped by its coordinator.")
        return {"status": "stopped", "reason": "stop_requested"}

    def for_coin(self, coin):
        with self.lock:
            return [entry for entry in self.calls if entry[0].coin == coin]


def test_one_shared_runner_receives_each_symbols_own_dataset_configuration(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC", "ETH", "HYPE", "ZEC"))
    loops = HoldingLoops()
    coordinator = manager.Coordinator(path, loop_runner=loops)
    try:
        coordinator.tick()
        eventually(lambda: len(loops.calls) == 4)
        assert {entry[0].coin for entry in loops.calls} == {"BTC", "ETH", "HYPE", "ZEC"}
        assert {entry[0].dataset_dir for entry in loops.calls} == {
            tmp_path / "data" / coin / "15m" for coin in ("BTC", "ETH", "HYPE", "ZEC")
        }
        assert all(entry[0].interval == "15m" for entry in loops.calls)
        assert len({id(entry[1]) for entry in loops.calls}) == 4
    finally:
        coordinator.shutdown()
    assert all(entry[1].is_set() for entry in loops.calls)


def test_bounded_run_onboards_each_market_once_and_passes_cycle_limit(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC", "ETH", "HYPE", "ZEC"))
    calls = []
    limits = []
    lock = threading.Lock()

    def pipeline(**kwargs):
        with lock:
            calls.append(kwargs)
        return result_for(kwargs)

    def worker(config, **kwargs):
        with lock:
            limits.append(kwargs["max_cycles"])
        return one_refresh(config, **kwargs)

    coordinator = manager.Coordinator(path, loop_runner=worker, pipeline_runner=pipeline)
    state = coordinator.run(max_cycles=1, poll_seconds=0.005, max_polls=500)
    assert state["status"] == "stopped"
    assert sorted(call["coin"] for call in calls) == ["BTC", "ETH", "HYPE", "ZEC"]
    assert limits == [1] * 4
    assert all(not item["thread_alive"] for item in state["markets"].values())


def test_failed_market_does_not_prevent_other_markets_from_finishing(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC", "ETH", "HYPE"))
    successful = []

    def pipeline(**kwargs):
        if kwargs["coin"] == "ETH":
            raise RuntimeError("ETH data temporarily unavailable")
        successful.append(kwargs["coin"])
        return result_for(kwargs)

    coordinator = manager.Coordinator(path, loop_runner=one_refresh, pipeline_runner=pipeline)
    try:
        coordinator.tick()
        eventually(lambda: all(market_done(coordinator, f"{coin}/15m") for coin in ("BTC", "ETH", "HYPE")))
        state = coordinator.tick()
        assert sorted(successful) == ["BTC", "HYPE"]
        assert "ETH data temporarily unavailable" in str(state["markets"]["ETH/15m"]["last_error"])
        assert not state["markets"]["BTC/15m"]["last_error"]
        assert not state["markets"]["HYPE/15m"]["last_error"]
    finally:
        coordinator.shutdown()


def test_real_loops_report_one_fetch_failure_without_blocking_other_markets(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC", "ETH", "HYPE"))
    calls = []

    def pipeline(**kwargs):
        calls.append(kwargs["coin"])
        if kwargs["coin"] == "ETH":
            raise RuntimeError("ETH public endpoint temporarily unavailable")
        return result_for(kwargs)

    coordinator = manager.Coordinator(path, pipeline_runner=pipeline)
    state = coordinator.run(max_cycles=1, poll_seconds=0.005, max_polls=500)
    assert sorted(calls) == ["BTC", "ETH", "HYPE"]
    assert state["status"] == "failed"
    assert state["reason"] == "worker_failure"
    eth = state["markets"]["ETH/15m"]
    assert eth["failures"] == 1
    assert eth["successful_cycles"] == 0
    assert eth["last_error"]["type"] == "RuntimeError"
    assert eth["last_cycle_timing"]["outcome"] == "failed"
    for coin in ("BTC", "HYPE"):
        completed = state["markets"][f"{coin}/15m"]
        assert completed["failures"] == 0
        assert completed["successful_cycles"] == 1
        assert completed["last_result"]["rows"] == 5000
        assert completed["last_cycle_timing"]["outcome"] == "success"
        assert completed["last_cycle_timing"]["queue_wait_seconds"] >= 0


def test_queue_duration_is_separate_from_pipeline_work(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC",))
    ticks = {"value": 10.0}
    result = {"status": "unchanged", "timings_seconds": {"feature_build": 0.0}}

    class Gate:
        def acquire(self, timeout):
            ticks["value"] += 1.25
            return True

        def release(self):
            pass

    def pipeline(**kwargs):
        ticks["value"] += 2.5
        return result

    coordinator = manager.Coordinator(path, pipeline_runner=pipeline, monotonic=lambda: ticks["value"])
    coordinator._gate = Gate()
    worker = manager.MarketWorker(config=next(iter(coordinator.config.loop_configs().values())))
    actual = coordinator._limited_pipeline(worker)
    assert actual["queue_wait_seconds"] == 1.25
    assert ticks["value"] == 13.75
    assert "queue_wait_seconds" not in result


def test_bounded_run_reports_market_stopped_before_first_update_as_incomplete(tmp_path):
    path = write_config(tmp_path / "markets.json")
    marker = tmp_path / "data" / "BTC" / "15m" / "stop.request"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"requested_at_utc": "test"}', encoding="utf-8")
    calls = []

    def pipeline(**kwargs):
        calls.append(kwargs["coin"])
        return result_for(kwargs)

    coordinator = manager.Coordinator(path, pipeline_runner=pipeline)
    state = coordinator.run(max_cycles=1, poll_seconds=0.005, max_polls=500)
    assert state["status"] == "failed"
    assert state["reason"] == "worker_failure"
    assert calls == ["ETH"]
    btc = state["markets"]["BTC/15m"]
    assert btc["status"] == "stopped"
    assert btc["cycle_count"] == 0
    assert btc["last_result"] is None
    eth = state["markets"]["ETH/15m"]
    assert eth["successful_cycles"] == 1
    assert eth["failures"] == 0
    assert eth["last_result"]["rows"] == 5000
    assert not marker.exists()


def test_parallel_update_limit_applies_across_all_symbol_workers(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC", "ETH", "HYPE", "ZEC"))
    release = threading.Event()
    lock = threading.Lock()
    calls = []
    active = 0
    peak = 0

    def pipeline(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(kwargs["coin"])
        try:
            if not release.wait(4):
                raise RuntimeError("Controlled fetch was not released.")
            return result_for(kwargs)
        finally:
            with lock:
                active -= 1

    coordinator = manager.Coordinator(path, loop_runner=one_refresh, pipeline_runner=pipeline)
    try:
        coordinator.tick()

        def two_fetching_two_queued():
            statuses = [item["status"] for item in coordinator.tick()["markets"].values()]
            return statuses.count("updating") == 2 and statuses.count("queued") == 2

        eventually(two_fetching_two_queued)
        assert len(calls) == 2
        release.set()
        eventually(lambda: all(market_done(coordinator, f"{coin}/15m") for coin in ("BTC", "ETH", "HYPE", "ZEC")))
        assert sorted(calls) == ["BTC", "ETH", "HYPE", "ZEC"]
        assert peak == 2
    finally:
        release.set()
        coordinator.shutdown()


def test_queued_update_uses_time_when_slot_becomes_available(tmp_path):
    path = write_config(tmp_path / "markets.json", max_parallel_updates=1)
    btc_fetching = threading.Event()
    release_btc = threading.Event()
    now = [1_800_000_001.0]
    observed = {}

    def worker(config, **kwargs):
        if config.coin == "ETH" and not btc_fetching.wait(4):
            raise RuntimeError("BTC did not acquire the first update slot.")
        return one_refresh(config, **kwargs)

    def pipeline(**kwargs):
        observed[kwargs["coin"]] = kwargs["as_of_ms"]
        if kwargs["coin"] == "BTC":
            btc_fetching.set()
            if not release_btc.wait(4):
                raise RuntimeError("BTC update was not released.")
        return result_for(kwargs)

    coordinator = manager.Coordinator(
        path, loop_runner=worker, pipeline_runner=pipeline, clock=lambda: now[0]
    )
    try:
        coordinator.tick()
        assert btc_fetching.wait(4)
        eventually(lambda: coordinator.tick()["markets"]["ETH/15m"]["status"] == "queued")
        now[0] += 2 * 900
        release_btc.set()
        eventually(lambda: market_done(coordinator, "ETH/15m"))
        assert observed == {"BTC": 1_800_000_001_000, "ETH": 1_800_001_801_000}
    finally:
        release_btc.set()
        coordinator.shutdown()


@pytest.mark.parametrize("use_real_loop", [True, False])
def test_shutdown_cancels_queued_fetches_but_allows_active_fetch_to_finish(tmp_path, use_real_loop):
    path = write_config(tmp_path / "markets.json", max_parallel_updates=1)
    btc_fetching = threading.Event()
    release_btc = threading.Event()
    eth_stopped = threading.Event()
    stopped = threading.Event()
    calls = []
    outcomes = []

    def worker(config, **kwargs):
        if config.coin == "ETH" and not btc_fetching.wait(4):
            raise RuntimeError("BTC did not acquire the first slot.")
        try:
            return (manager.run_loop if use_real_loop else one_refresh)(config, **kwargs)
        finally:
            if config.coin == "ETH":
                eth_stopped.set()

    def pipeline(**kwargs):
        calls.append(kwargs["coin"])
        if kwargs["coin"] == "BTC":
            btc_fetching.set()
            if not release_btc.wait(4):
                raise RuntimeError("BTC update was not released.")
        return result_for(kwargs)

    coordinator = manager.Coordinator(path, loop_runner=worker, pipeline_runner=pipeline)
    shutdown_thread = None
    try:
        coordinator.tick()
        eventually(lambda: coordinator.tick()["markets"]["ETH/15m"]["status"] == "queued")

        def shutdown():
            outcomes.append(coordinator.shutdown())
            stopped.set()

        shutdown_thread = threading.Thread(target=shutdown)
        shutdown_thread.start()
        assert eth_stopped.wait(4)
        assert calls == ["BTC"]
        assert not stopped.is_set()
        release_btc.set()
        assert stopped.wait(4)
        shutdown_thread.join(1)
        assert outcomes[0]["status"] == "stopped"
        assert calls == ["BTC"]
        eth = outcomes[0]["markets"]["ETH/15m"]
        assert eth["status"] == "stopped"
        assert eth["last_error"] is None
        if use_real_loop:
            assert eth["failures"] == 0
    finally:
        release_btc.set()
        if shutdown_thread is not None:
            shutdown_thread.join(4)
        coordinator.shutdown()


def test_individual_market_stop_is_respected_without_automatic_relaunch(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC",))
    calls = []

    def pipeline(**kwargs):
        calls.append(kwargs["coin"])
        marker = kwargs["output_root"] / kwargs["coin"] / kwargs["interval"] / "stop.request"
        marker.write_text('{"requested_at_utc": "test"}', encoding="utf-8")
        return result_for(kwargs)

    coordinator = manager.Coordinator(path, pipeline_runner=pipeline)
    try:
        coordinator.tick()
        eventually(lambda: market_done(coordinator, "BTC/15m"))
        for _ in range(3):
            state = coordinator.tick()
            assert state["markets"]["BTC/15m"]["status"] == "stopped"
        assert calls == ["BTC"]
        assert state["markets"]["BTC/15m"]["failures"] == 0
        assert not (tmp_path / "data" / "BTC" / "15m" / "stop.request").exists()
    finally:
        coordinator.shutdown()


def test_symbol_addition_and_removal_preserve_unchanged_worker_and_saved_history(tmp_path):
    path = write_config(tmp_path / "markets.json")
    old_data = tmp_path / "data" / "ETH" / "15m" / "existing-history.parquet"
    old_data.parent.mkdir(parents=True)
    old_data.write_bytes(b"existing saved history")
    loops = HoldingLoops()
    coordinator = manager.Coordinator(path, loop_runner=loops)
    try:
        coordinator.tick()
        eventually(lambda: len(loops.calls) == 2)
        btc_event = loops.for_coin("BTC")[0][1]
        eth_event = loops.for_coin("ETH")[0][1]
        write_config(path, symbols=("BTC", "HYPE"))
        assert coordinator.reload()
        coordinator.tick()
        eventually(lambda: len(loops.for_coin("HYPE")) == 1)
        assert eth_event.is_set()
        assert not btc_event.is_set()
        assert len(loops.for_coin("BTC")) == 1
        assert old_data.read_bytes() == b"existing saved history"
        assert set(coordinator.config.symbols) == {"BTC", "HYPE"}
    finally:
        coordinator.shutdown()


def test_invalid_hot_reload_keeps_active_workers_and_recovers_after_edit(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC",))
    original_bytes = path.read_bytes()
    loops = HoldingLoops()
    coordinator = manager.Coordinator(path, loop_runner=loops)
    try:
        coordinator.tick()
        eventually(lambda: len(loops.calls) == 1)
        original_event = loops.calls[0][1]
        path.write_text('{"symbols": [', encoding="utf-8")
        assert not coordinator.reload()
        invalid_state = coordinator.tick()
        assert invalid_state["config_error"]
        assert coordinator.config.symbols == ("BTC",)
        assert not original_event.is_set()
        assert len(loops.calls) == 1

        path.write_bytes(original_bytes)
        assert not coordinator.reload()
        assert not coordinator.tick()["config_error"]
        assert len(loops.calls) == 1
        assert not original_event.is_set()

        write_config(path, symbols=("BTC", "ETH"))
        assert coordinator.reload()
        eventually(lambda: coordinator.tick() and len(loops.for_coin("ETH")) == 1)
        assert not coordinator.tick()["config_error"]
        assert len(loops.for_coin("BTC")) == 1
        assert not original_event.is_set()
    finally:
        coordinator.shutdown()


@pytest.mark.parametrize("changed", [{"interval": "1h"}, {"max_parallel_updates": 1}])
def test_operating_settings_change_requires_restart_without_disrupting_workers(tmp_path, changed):
    path = write_config(tmp_path / "markets.json", symbols=("BTC",))
    loops = HoldingLoops()
    coordinator = manager.Coordinator(path, loop_runner=loops)
    try:
        coordinator.tick()
        eventually(lambda: len(loops.calls) == 1)
        write_config(path, symbols=("BTC",), **changed)
        assert not coordinator.reload()
        state = coordinator.tick()
        assert "restart" in str(state["config_error"]).lower()
        assert not loops.calls[0][1].is_set()
        assert coordinator.config.interval == "15m"
        assert coordinator.config.max_parallel_updates == 2
        assert len(loops.calls) == 1
    finally:
        coordinator.shutdown()


def test_removal_then_rapid_readdition_waits_for_old_worker_to_finish(tmp_path):
    path = write_config(tmp_path / "markets.json")
    loops = HoldingLoops()
    release_first_eth = threading.Event()
    first_eth_stopping = threading.Event()
    eth_calls = []

    def worker(config, **kwargs):
        if config.coin == "ETH":
            eth_calls.append(kwargs["stop_event"])
            if len(eth_calls) == 1:
                if not kwargs["stop_event"].wait(4):
                    raise RuntimeError("ETH removal never signaled the old worker.")
                first_eth_stopping.set()
                if not release_first_eth.wait(4):
                    raise RuntimeError("Old ETH worker was not released.")
                return {"status": "stopped", "reason": "stop_requested"}
        return loops(config, **kwargs)

    coordinator = manager.Coordinator(path, loop_runner=worker)
    try:
        coordinator.tick()
        eventually(lambda: len(eth_calls) == 1 and len(loops.for_coin("BTC")) == 1)
        write_config(path, symbols=("BTC",))
        coordinator.tick()
        assert first_eth_stopping.wait(4)
        write_config(path, symbols=("BTC", "ETH"))
        coordinator.tick()
        assert len(eth_calls) == 1
        release_first_eth.set()
        eventually(lambda: coordinator.tick() and len(eth_calls) == 2)
        assert eth_calls[0] is not eth_calls[1]
        assert len(loops.for_coin("BTC")) == 1
    finally:
        release_first_eth.set()
        coordinator.shutdown()


def test_market_owned_by_an_external_worker_is_not_stopped_or_overwritten(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC",))
    dataset = tmp_path / "data" / "BTC" / "15m"
    dataset.mkdir(parents=True)
    external_status = dataset / "loop_status.json"
    external_status.write_text('{"status": "waiting", "external": true}', encoding="utf-8")

    def forbidden(**kwargs):
        pytest.fail("The externally owned market must not run a competing fetch.")

    coordinator = manager.Coordinator(path, pipeline_runner=forbidden)
    with FileLock(str(dataset / ".loop.lock"), timeout=0):
        try:
            coordinator.tick()
            eventually(lambda: coordinator.tick()["markets"]["BTC/15m"]["status"] == "external_worker")
        finally:
            coordinator.shutdown()
        assert not (dataset / "stop.request").exists()
        assert json.loads(external_status.read_text())["external"] is True


def test_second_coordinator_cannot_start_workers_in_the_same_output_root(tmp_path):
    path = write_config(tmp_path / "markets.json")
    control = tmp_path / "data" / "_coordinator"
    control.mkdir(parents=True)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        return {"status": "stopped", "reason": "max_cycles"}

    coordinator = manager.Coordinator(path, loop_runner=forbidden)
    with FileLock(str(control / ".coordinator.lock"), timeout=0):
        with pytest.raises(Timeout):
            coordinator.run(max_cycles=1, max_polls=1)
    assert calls == []


def test_stopping_idle_coordinator_cleans_stale_request(tmp_path):
    output = tmp_path / "data"
    control = output / "_coordinator"
    control.mkdir(parents=True)
    marker = control / "stop.request"
    marker.write_text("stale", encoding="utf-8")
    state = manager.request_stop(output)
    assert state["status"] == "already_stopped"
    assert not marker.exists()


def test_stop_request_is_consumed_and_stops_only_managed_workers(tmp_path):
    path = write_config(tmp_path / "markets.json")
    loops = HoldingLoops()
    coordinator = manager.Coordinator(path, loop_runner=loops)
    finished = threading.Event()
    outcomes = []
    errors = []

    def run():
        try:
            outcomes.append(coordinator.run(poll_seconds=0.005, max_polls=1000))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        eventually(lambda: len(loops.calls) == 2)
        assert manager.read_status(coordinator.config.output_root)["coordinator_running"] is True
        state = manager.request_stop(coordinator.config.output_root)
        assert state["status"] == "stop_requested"
        assert finished.wait(4)
        assert errors == []
        assert outcomes[0]["status"] == "stopped"
        assert all(entry[1].is_set() for entry in loops.calls)
        assert not (coordinator.config.control_dir / "stop.request").exists()
        assert manager.read_status(coordinator.config.output_root)["coordinator_running"] is False
    finally:
        manager.request_stop(coordinator.config.output_root)
        thread.join(4)


def test_stale_persisted_status_does_not_claim_coordinator_is_running(tmp_path):
    path = write_config(tmp_path / "markets.json", symbols=("BTC",))
    coordinator = manager.Coordinator(
        path, loop_runner=one_refresh, pipeline_runner=lambda **kwargs: result_for(kwargs)
    )
    # Use a real bounded run to obtain the public status file and shape.
    coordinator.run(max_cycles=1, poll_seconds=0.005, max_polls=500)
    status_path = coordinator.config.control_dir / "coordinator_status.json"
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    saved["status"] = "running"
    status_path.write_text(json.dumps(saved), encoding="utf-8")
    state = manager.read_status(coordinator.config.output_root)
    assert state["coordinator_running"] is False
    assert state["status"] == "not_running"
