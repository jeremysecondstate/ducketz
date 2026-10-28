"""Real Windows contained role processes, entirely synthetic release inputs."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading

import pytest

from tools import scout_release as releases
from tools import scout_service_controller as services


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment")
def test_real_roles_rotate_model_keep_trading_and_stop_without_duplicate_effects(tmp_path):
    release = tmp_path / "release"
    source = release / "source"
    source.mkdir(parents=True)
    fake_service = '''from pathlib import Path
import time

def run_model_service(*, repository, datastore, release, lifecycle):
    lifecycle.ready()
    root = Path(datastore)
    # Prerecorded model output only. No training or production imports.
    (root / ("model-generation-" + str(lifecycle.generation))).write_text("fixture")
    if lifecycle.generation == 1:
        (root / "ready-forecast").write_text("same-immutable-fixture-id")
        lifecycle.checkpoint(dict(schema="scout-independent-model-checkpoint-v1",
            boundary_kind="full_symbol_attempt_with_durable_deferrals", owned_workers_drained=True,
            boundary_cycle_id="fixture-cycle", symbols=release["symbols"],
            started_at=lifecycle.started_at, wake_at=lifecycle.wake_at, deadline=lifecycle.deadline))
        return dict(status="PLANNED_RESTART")
    while not lifecycle.stop_reason():
        time.sleep(0.01)
    return dict(status="STOPPED", stop_reason=lifecycle.stop_reason())

def run_trading_service(*, repository, datastore, release, lifecycle):
    lifecycle.ready()
    root = Path(datastore)
    while not lifecycle.stop_reason():
        with (root / "recovery-ticks").open("a") as stream:
            stream.write("tick\\n")
        if (root / "ready-forecast").exists() and not (root / "fake-broker-effect").exists():
            # Synthetic broker with exclusive fixture-id fence, never real IO.
            with (root / "fake-broker-effect").open("x") as stream:
                stream.write("same-immutable-fixture-id")
        time.sleep(0.01)
    return dict(status="STOPPED", stop_reason=lifecycle.stop_reason())
'''
    files = {"app/__init__.py": b"", "app/services/__init__.py": b"",
        "app/services/schwab_token_store.py": b"# offline fixture; no broker\n",
        "ml/__init__.py": b"", "ml/intraday/__init__.py": b"",
        "ml/intraday/live_session.py": b"# offline fixture\n", "ml/intraday/manual_launcher.py": b"",
        "ml/intraday/live_model_recipe.py": b"", "ml/intraday/scout_services.py": fake_service.encode()}
    for name, raw in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    recipe = release / "recipe.json"
    recipe.write_text(json.dumps({"pc": "Scout", "symbols": ["SYM" + str(i) for i in range(11)]}), encoding="utf-8")
    manifest = {"source_root": str(source), "lifecycle_protocol": services.PROTOCOL,
        "files": {name: releases.sha256(raw) for name, raw in files.items()}, "original_repository": str(tmp_path),
        "recipe_path": str(recipe), "recipe_file_sha256": releases.sha256(recipe.read_bytes())}
    releases.atomic_json(release / "manifest.json", manifest)
    selected = releases.validate_release(release, releases.sha256((release / "manifest.json").read_bytes()), protocol=services.PROTOCOL)
    datastore = tmp_path / "private"
    datastore.mkdir()
    now = datetime.now(timezone.utc)
    owner = services.ServiceController(selector=tmp_path / "selector", repository=tmp_path, datastore=datastore,
        wake_at=now, deadline=now + timedelta(seconds=12), select=lambda _: selected,
        startup_timeout=8, drain_timeout=3)
    evidence = []
    def stop_after_second_generation():
        import time
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if (datastore / "model-generation-2").exists() and (datastore / "fake-broker-effect").exists():
                evidence.append((datastore / "recovery-ticks").read_text())
                owner.request_stop()
                return
            time.sleep(0.01)
    observer = threading.Thread(target=stop_after_second_generation, daemon=True)
    observer.start()
    assert owner.run(lock_factory=lambda *_: nullcontext()) == 0
    observer.join(timeout=1)
    assert evidence and len(evidence[0].splitlines()) > 1
    assert (datastore / "fake-broker-effect").read_text() == "same-immutable-fixture-id"
    assert owner.state["roles"]["trading"]["generation"] == 1
    assert owner.state["roles"]["model"]["generation"] == 2
    assert owner.state["stop_reason"] == "manual_stop"
    assert owner.workers == {}
