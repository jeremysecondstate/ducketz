"""Offline orchestration checks: fake workers never fetch, train or trade."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from filelock import FileLock, Timeout
import pytest

from ml import hyperliquid_paper_loop as loop


def config(tmp_path, *, strict=True, interval="5m"):
    root = tmp_path / "data"
    markets = tmp_path / "markets.json"
    models = tmp_path / "models.json"
    paper = tmp_path / "paper.json"
    markets.write_text(json.dumps({"version": 1, "symbols": ["BTC"], "interval": interval,
                                   "output_root": str(root)}))
    models.write_text(json.dumps({"version": 1, "markets_config": str(markets),
                                  "horizons_bars": [1], "retrain_before_predict": strict}))
    paper.write_text(json.dumps({"version": 1, "model_config": str(models),
                                 "data_root": str(root), "seed_mode": "manual"}))
    return paper, root


def test_once_runs_data_training_then_paper_and_persists_result(tmp_path):
    path, root = config(tmp_path)
    calls = []

    def factory(path, models, stopped, once):
        assert once and not stopped.is_set()
        def worker(name):
            calls.append(name)
            assert loop.read_status(root)["running"]
            return {"status": "stopped", "name": name}
        return {name: lambda name=name: worker(name) for name in ("data", "models", "paper")}

    result = loop.run(path, once=True, worker_factory=factory)
    assert calls == ["data", "models", "paper"]
    assert result["status"] == "stopped" and result["last_error"] is None
    saved = loop.read_status(root)
    assert not saved["running"]
    assert saved["workers"]["paper"]["result"]["name"] == "paper"


@pytest.mark.parametrize("failure", ["data", "models"])
def test_upstream_failed_once_never_calls_downstream_paper(tmp_path, failure):
    path, _ = config(tmp_path)
    calls = []
    def factory(*args):
        def worker(name):
            calls.append(name)
            return {"status": "failed" if name == failure else "stopped"}
        return {name: lambda name=name: worker(name) for name in ("data", "models", "paper")}
    assert loop.run(path, once=True, worker_factory=factory)["status"] == "failed"
    assert "paper" not in calls


@pytest.mark.parametrize("settings", [{"strict": False}, {"interval": "15m"}])
def test_wrong_recipe_rejected_before_workers(tmp_path, settings):
    path, _ = config(tmp_path, **settings)
    with pytest.raises(ValueError):
        loop.run(path, worker_factory=lambda *_: pytest.fail("started workers"))


def test_existing_worker_lock_blocks_entire_start(tmp_path):
    path, root = config(tmp_path)
    held = root / "_paper" / "_runtime" / ".paper.lock"
    held.parent.mkdir(parents=True)
    with FileLock(str(held), timeout=0):
        with pytest.raises(Timeout):
            loop.run(path, worker_factory=lambda *_: pytest.fail("started workers"))


def test_worker_stop_request_is_preserved(tmp_path):
    path, root = config(tmp_path)
    marker = root / "BTC" / "5m" / "stop.request"
    marker.parent.mkdir(parents=True)
    marker.write_text("requested")
    with pytest.raises(ValueError, match="stop request"):
        loop.run(path, worker_factory=lambda *_: pytest.fail("started workers"))
    assert marker.read_text() == "requested"


def test_continuous_workers_run_concurrently_and_stop_together(tmp_path):
    path, root = config(tmp_path)
    barrier = threading.Barrier(3, timeout=5)
    finished = []
    def factory(path, models, stopped, once):
        assert not once
        def worker(name):
            barrier.wait()
            if name == "paper":
                assert loop.request_stop(root)["status"] == "stop_requested"
            assert stopped.wait(5)
            finished.append(name)
            return {"status": "stopped"}
        return {name: lambda name=name: worker(name) for name in ("data", "models", "paper")}
    result = loop.run(path, worker_factory=factory)
    assert result["status"] == "stopped"
    assert sorted(finished) == ["data", "models", "paper"]
    assert not loop.read_status(root)["running"]
    assert loop.request_stop(root)["status"] == "already_stopped"


def test_worker_exception_is_visible_and_stops_remaining_workers(tmp_path):
    path, _ = config(tmp_path)
    finished = []
    def factory(path, models, stopped, once):
        def fail():
            raise RuntimeError("broken fit worker")
        def wait():
            assert stopped.wait(5)
            finished.append(True)
            return {"status": "stopped"}
        return {"data": wait, "models": fail, "paper": wait}
    result = loop.run(path, worker_factory=factory)
    assert result["status"] == "failed"
    assert "broken fit worker" in result["last_error"]
    assert len(finished) == 2


def test_status_reads_coordinator_actual_filename_without_starting(tmp_path):
    _, root = config(tmp_path)
    path = root / "_coordinator" / "coordinator_status.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "running", "cycle_count": 3}))
    assert loop.read_status(root)["components"]["data"]["cycle_count"] == 3
