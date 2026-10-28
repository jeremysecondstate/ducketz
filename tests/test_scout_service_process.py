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
def test_real_roles_rotate_model_and_trading_twice_without_duplicate_effects(tmp_path):
    release = tmp_path / "release"
    source = release / "source"
    source.mkdir(parents=True)
    fake_service = '''from pathlib import Path
import json
import time

def identity(lifecycle):
    return dict(**lifecycle.header(), started_at=lifecycle.started_at,
        wake_at=lifecycle.wake_at, deadline=lifecycle.deadline)

def run_model_service(*, repository, datastore, release, lifecycle):
    lifecycle.ready()
    root = Path(datastore)
    # Prerecorded model output only. No training or production imports.
    (root / ("model-generation-" + str(lifecycle.generation))).write_text(json.dumps(identity(lifecycle)))
    if lifecycle.generation <= 2:
        (root / "ready-forecast").write_text("same-immutable-fixture-id")
        lifecycle.checkpoint(dict(schema="scout-independent-model-checkpoint-v1",
            boundary_kind="full_symbol_attempt_with_durable_deferrals", owned_workers_drained=True,
            boundary_cycle_id="fixture-cycle-" + str(lifecycle.generation), symbols=release["symbols"],
            started_at=lifecycle.started_at, wake_at=lifecycle.wake_at, deadline=lifecycle.deadline))
        return dict(status="PLANNED_RESTART")
    while not lifecycle.stop_reason():
        time.sleep(0.01)
    return dict(status="STOPPED", stop_reason=lifecycle.stop_reason())

def run_trading_service(*, repository, datastore, release, lifecycle):
    previous = lifecycle.previous_checkpoint
    if lifecycle.generation > 1:
        assert previous["schema"] == "scout-independent-trading-checkpoint-v1"
        assert previous["session_id"] == lifecycle.session_id
        assert previous["generation"] == lifecycle.generation - 1
        assert previous["fixture_order_identity"] == "same-immutable-fixture-id"
        for name in ("started_at", "wake_at", "deadline"):
            assert previous[name] == getattr(lifecycle, name)
    else:
        assert previous is None
    lifecycle.ready()
    root = Path(datastore)
    checked_effect = False
    while not lifecycle.stop_reason():
        with (root / "recovery-ticks").open("a") as stream:
            stream.write(str(lifecycle.generation) + "\\n")
        if (root / "ready-forecast").exists() and not checked_effect:
            # Each generation visits the same synthetic broker's exclusive
            # fixture identity. Existing identity cannot create another effect.
            try:
                with (root / "fake-broker-effect").open("x") as stream:
                    stream.write("same-immutable-fixture-id")
                outcome = "created"
            except FileExistsError:
                assert (root / "fake-broker-effect").read_text() == "same-immutable-fixture-id"
                outcome = "existing"
            checked_effect = True
            (root / ("trading-generation-" + str(lifecycle.generation))).write_text(
                json.dumps(dict(**identity(lifecycle), effect=outcome)))
        rotation = lifecycle.trading_rotation_request()
        if rotation is not None:
            assert checked_effect
            lifecycle.phase("draining")
            lifecycle.checkpoint(dict(schema="scout-independent-trading-checkpoint-v1",
                rotation_request=rotation, owned_workers_drained=True,
                pending_order_identity_preserved=True, boundary_kind="model_full_symbol_attempt",
                symbols=release["symbols"], started_at=lifecycle.started_at,
                wake_at=lifecycle.wake_at, deadline=lifecycle.deadline,
                fixture_order_identity="same-immutable-fixture-id"))
            return dict(status="PLANNED_RESTART")
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
        "trading_rotation_version": services.TRADING_ROTATION_VERSION,
        "files": {name: releases.sha256(raw) for name, raw in files.items()}, "original_repository": str(tmp_path),
        "recipe_path": str(recipe), "recipe_file_sha256": releases.sha256(recipe.read_bytes())}
    releases.atomic_json(release / "manifest.json", manifest)
    selector = tmp_path / "selector"
    releases.select_release(selector, release, releases.sha256((release / "manifest.json").read_bytes()),
                            protocol=services.PROTOCOL)
    datastore = tmp_path / "private"
    datastore.mkdir()
    now = datetime.now(timezone.utc)
    owner = services.ServiceController(selector=selector, repository=tmp_path, datastore=datastore,
        wake_at=now, deadline=now + timedelta(seconds=30), select=services.selected_release,
        startup_timeout=8, drain_timeout=3)
    evidence = []
    observer_errors = []
    observer_stop = threading.Event()
    def stop_after_two_replacements():
        import time
        until = time.monotonic() + 25
        try:
            while not observer_stop.wait(.01) and time.monotonic() < until:
                if ((datastore / "model-generation-3").exists()
                        and (datastore / "trading-generation-3").exists()):
                    evidence.append((datastore / "recovery-ticks").read_text())
                    owner.request_stop()
                    return
        except BaseException as exc:
            observer_errors.append(exc)
    observer = threading.Thread(target=stop_after_two_replacements, daemon=True)
    observer.start()
    try:
        result = owner.run(lock_factory=lambda *_: nullcontext())
    finally:
        # Even a controller assertion/exception must retire this thread before
        # later tests monkeypatch process-global clocks and sleep functions.
        observer_stop.set()
        observer.join(timeout=2)
    assert not observer.is_alive() and not observer_errors
    assert result == 0
    assert evidence and len(evidence[0].splitlines()) > 1
    assert (datastore / "fake-broker-effect").read_text() == "same-immutable-fixture-id"
    assert owner.state["roles"]["trading"]["generation"] == 3
    assert owner.state["roles"]["model"]["generation"] == 3
    assert owner.state["trading_completed_iteration"] == 2
    assert [item["iteration_sequence"] for item in owner.state["completed_iterations"]] == [1, 2]
    assert set(evidence[0].splitlines()) == {"1", "2", "3"}
    for role in ("model", "trading"):
        identities = []
        for generation in (1, 2, 3):
            receipt = json.loads((datastore / (role + "-generation-" + str(generation))).read_text())
            identities.append((receipt["worker_pid"], receipt["worker_create_time"]))
            assert receipt["generation"] == generation and receipt["session_id"] == owner.run_id
            for name in ("started_at", "wake_at", "deadline"):
                assert receipt[name] == owner.state[name]
            if role == "trading":
                assert receipt["effect"] == ("created" if generation == 1 else "existing")
            if generation < 3:
                root = owner.root / role / str(generation)
                assert json.loads((root / "exit.json").read_text())["exit_code"] == services.PLANNED_EXIT
                checkpoint = json.loads((root / "checkpoint.json").read_text())
                assert checkpoint["generation"] == generation and checkpoint["role"] == role
                if role == "trading":
                    request = json.loads((root / "rotation-request.json").read_text())
                    assert checkpoint["rotation_request"] == request
                    assert request["iteration_sequence"] == generation
                    assert request["model_generation"] == generation
        assert len(set(identities)) == 3
    assert owner.state["stop_reason"] == "manual_stop"
    assert owner.workers == {}
