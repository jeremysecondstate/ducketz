"""Offline manual-stop races: controls are irreversible for an active session."""
from __future__ import annotations

import json
import ast
import contextlib
import os
from pathlib import Path

import pytest

_source = Path(__file__).resolve().parents[1]
for _module, _writer in (("control.py", "write_activation_intent"),
                          ("gameplan.py", "write_gameplan_stock_activation_intent")):
    _adapter = _source / "ml/stock_trader" / _module
    _definitions = ast.parse(_adapter.read_bytes()).body if _adapter.is_file() else []
    if not any(isinstance(node, ast.FunctionDef) and node.name == _writer
               and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                       and call.func.id == "latch_manual_stop" for call in ast.walk(node))
               for node in _definitions):
        pytest.skip("Installed stop-latching adapters required; apply the reviewed compatibility patch",
                    allow_module_level=True)

from ml import trader_renewal as renewal
from ml.stock_trader.control import write_activation_intent
from ml.stock_trader.gameplan import write_gameplan_stock_activation_intent
from tools import trader_session_host as host


def fixture_session(root, *, active=False, role="initialize", trader="gameplan"):
    root = root.resolve()
    if trader == "gameplan":
        write_activation_intent(root, active=active)
        write_gameplan_stock_activation_intent(root, active=active)
    else:
        renewal.atomic_json(root / "intraday/manual-session.json", {"session_id": "prior", "status": "STOPPED"})
    directory = root / "state/trader-renewal" / trader / "manual-test"
    directory.mkdir(parents=True)
    envelope = {"protocol": renewal.PROTOCOL, "trader": trader, "datastore": str(root),
                "owner_pid": os.getpid(), "owner_birth": renewal.process_birth(os.getpid()),
                "token": "synthetic-session-token", "session": {"session_id": "synthetic-manual-session"},
                "stop_path": str(directory / "stop.json")}
    envelope["initial_controls"] = renewal.control_signatures(envelope)
    renewal.atomic_json(directory / "session.json", envelope)
    renewal.atomic_json(directory.parent / "active.json", dict(envelope, status="RUNNING"))
    request = directory / "request.json"
    renewal.atomic_json(request, {"envelope": str(directory / "session.json"), "token": envelope["token"],
                                "role": role, "generation": 1, "turn": "one",
                                "response": str(directory / "response.json")})
    return renewal.Renewal(request)


@pytest.mark.parametrize("trader", ["gameplan", "intraday"])
def test_initial_snapshot_format_agrees_with_kernel_anchor(tmp_path, trader):
    context = fixture_session(tmp_path, trader=trader)
    assert host.control_signatures(context.envelope) == context.envelope["initial_controls"]
    assert not host.initialization_stop_changed(context.envelope)
    assert not context.stopped()  # Prior inactive state is allowed only on a manual initial start.


def test_worker_dispatch_claim_is_one_shot_and_cannot_be_replayed(tmp_path):
    context = fixture_session(tmp_path)
    context.claim()
    assert not context.stopped()
    with pytest.raises(FileExistsError):
        renewal.Renewal(context.request_path).claim()
    marker = context.request_path.with_suffix(".claim.json")
    value = json.loads(marker.read_text())
    value["birth"] = "previous-process-birth"
    renewal.atomic_json(marker, value)
    assert context.stopped()


@pytest.mark.parametrize("writer", [write_activation_intent, write_gameplan_stock_activation_intent])
def test_official_stop_latches_before_reactivation_even_during_initialization(tmp_path, writer):
    context = fixture_session(tmp_path)
    assert not context.stopped()
    writer(tmp_path, active=False)
    stop_path = Path(context.envelope["stop_path"])
    assert stop_path.is_file()
    original_stop = stop_path.read_bytes()
    write_activation_intent(tmp_path, active=True)
    write_gameplan_stock_activation_intent(tmp_path, active=True)
    assert context.stopped()
    assert stop_path.read_bytes() == original_stop


@pytest.mark.parametrize("relative,key", list(renewal.GAMEPLAN_CONTROLS.items()))
def test_direct_same_value_false_write_during_setup_is_a_new_manual_stop(tmp_path, relative, key):
    context = fixture_session(tmp_path)
    path = tmp_path / relative
    before = path.stat().st_mtime_ns
    path.write_text(key + "=FALSE\n", encoding="utf-8")
    # Explicit timestamps make the same-value edit deterministic on coarse FS clocks.
    os.utime(path, ns=(before + 1_000_000_000, before + 1_000_000_000))
    assert host.initialization_stop_changed(context.envelope)
    assert context.stopped()
    assert Path(context.envelope["stop_path"]).is_file()
    path.write_text(key + "=TRUE\n", encoding="utf-8")
    assert context.stopped()


def test_changed_intraday_stop_during_initialization_is_latched(tmp_path):
    context = fixture_session(tmp_path, trader="intraday")
    renewal.atomic_json(tmp_path / "intraday/manual-session.json",
                       {"session_id": "prior", "status": "STOPPED", "manual_stop": "new edit"})
    assert context.stopped()
    assert Path(context.envelope["stop_path"]).exists()


def test_existing_control_stop_seen_by_worker_is_durable_between_generations(tmp_path):
    context = fixture_session(tmp_path, active=True, role="execution")
    assert not context.stopped()
    (tmp_path / "controls/stock-trader/operator-intent.txt").write_text("CONFIRM_ACTIVE_TRADING=FALSE\n")
    assert context.stopped()
    write_activation_intent(tmp_path, active=True)
    assert renewal.Renewal(context.request_path).stopped()


@pytest.mark.parametrize("damage", ["birth", "unknown_birth", "outside", "ended", "token_missing"])
def test_stale_or_invalid_pointer_never_writes_an_unrelated_stop(tmp_path, damage):
    context = fixture_session(tmp_path)
    pointer = tmp_path / "state/trader-renewal/gameplan/active.json"
    value = json.loads(pointer.read_text())
    if damage == "birth":
        value["owner_birth"] = "different-process-birth"
    elif damage == "unknown_birth":
        value["owner_pid"] = 0
        value["owner_birth"] = None
    elif damage == "outside":
        value["stop_path"] = str(tmp_path / "unrelated/stop.json")
    elif damage == "ended":
        value["status"] = "ENDED"
    else:
        value.pop("token")
    renewal.atomic_json(pointer, value)
    assert not renewal.latch_manual_stop(tmp_path)
    assert not Path(context.envelope["stop_path"]).exists()
    assert not (tmp_path / "unrelated/stop.json").exists()


def test_unknown_owner_birth_is_not_continuation_authority(tmp_path):
    context = fixture_session(tmp_path, active=True, role="execution")
    context.envelope.update(owner_pid=0, owner_birth=None)
    assert context.stopped()


def test_gameplan_control_does_not_stop_independent_intraday_session(tmp_path):
    context = fixture_session(tmp_path, trader="intraday")
    write_activation_intent(tmp_path, active=False)
    write_gameplan_stock_activation_intent(tmp_path, active=False)
    assert not Path(context.envelope["stop_path"]).exists()


def test_anchor_never_admits_execution_after_stop_during_initializer(tmp_path, monkeypatch):
    write_activation_intent(tmp_path, active=False)
    write_gameplan_stock_activation_intent(tmp_path, active=False)
    directory = tmp_path / "state/trader-renewal/gameplan/manual-test"
    invoked = []

    class ExitedChild:
        def poll(self):
            return 0

    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **kw: ExitedChild())

    def phase(name, role):
        if name == "started":
            assert role == "initialize"
            write_gameplan_stock_activation_intent(tmp_path, active=False)
            # Even an already completed initializer cannot undo that stop.
            write_activation_intent(tmp_path, active=True)
            write_gameplan_stock_activation_intent(tmp_path, active=True)

    def command(role, request):
        invoked.append(role)
        return ["offline-never-executed-child"]

    code = host.run_protocol(directory=directory, envelope={
        "trader": "gameplan", "datastore": str(tmp_path), "session": {"session_id": "test"}, "roles": []},
        command=command, phase=phase)
    assert code == 0 and invoked == ["initialize"]
    assert json.loads((directory / "outcome.json").read_text())["disposition"] == "MANUAL_STOP"
    assert json.loads((directory.parent / "active.json").read_text())["status"] == "ENDED"


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded initial activation")
@pytest.mark.parametrize("trader,relative", [
    ("gameplan", "controls/stock-trader/operator-intent.txt"),
    ("gameplan", "controls/gameplan-stock-trader/operator-intent.txt"),
    ("intraday", "intraday/manual-session.json"),
])
@pytest.mark.parametrize("timing", ["normal", "before", "during", "after", "missing_normal", "missing_race"])
def test_initial_activation_never_overwrites_a_successful_raw_stop(tmp_path, monkeypatch, trader, relative, timing):
    context = fixture_session(tmp_path, trader=trader)
    path = tmp_path / relative
    if trader == "gameplan":
        key = renewal.GAMEPLAN_CONTROLS[relative]
        active, inactive = (key + "=TRUE\n").encode(), (key + "=FALSE\n").encode()
    else:
        active = json.dumps({"session_id": context.session_id, "status": "RUNNING"}).encode()
        inactive = json.dumps({"session_id": "prior", "status": "STOPPED", "manual_edit": True}).encode()
    if timing in {"missing_normal", "missing_race"}:
        path.unlink()
        context.envelope["initial_controls"] = host.control_signatures(context.envelope)
    original = host._exclusive_initial_file

    def raw_stop():
        prior = path.stat().st_mtime_ns if path.exists() else 0
        path.write_bytes(inactive)
        os.utime(path, ns=(prior + 1_000_000_000, prior + 1_000_000_000))

    @contextlib.contextmanager
    def guarded(target, *, missing):
        if timing in {"before", "missing_race"}:
            raw_stop()
        with original(target, missing=missing) as stream:
            if timing == "during":
                with pytest.raises(PermissionError):
                    path.write_bytes(inactive)
                replacement = path.with_name("raw-stop-replacement.tmp")
                replacement.write_bytes(inactive)
                with pytest.raises(PermissionError):
                    replacement.replace(path)
            yield stream

    monkeypatch.setattr(host, "_exclusive_initial_file", guarded)
    result = host.guarded_initial_control(context.envelope, relative, active)
    if timing in {"before", "missing_race"}:
        assert result is False and context.stopped()
        assert path.read_bytes() == inactive
    else:
        assert result is True and path.read_bytes() == active
        evidence, = Path(context.envelope["stop_path"]).parent.glob("initial-control-*.json")
        assert "bytes_hex" in json.loads(evidence.read_text())
        if timing == "after":
            raw_stop()
            assert context.stopped()
            assert path.read_bytes() == inactive
        else:
            assert not context.stopped()


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded initial activation")
@pytest.mark.parametrize("writer,relative", [
    (write_activation_intent, "controls/stock-trader/operator-intent.txt"),
    (write_gameplan_stock_activation_intent, "controls/gameplan-stock-trader/operator-intent.txt"),
])
def test_official_stop_latches_even_while_activation_handle_excludes_file_writes(tmp_path, monkeypatch, writer, relative):
    context = fixture_session(tmp_path)
    original = host._exclusive_initial_file

    @contextlib.contextmanager
    def guarded(target, *, missing):
        with original(target, missing=missing) as stream:
            with pytest.raises(PermissionError):
                writer(tmp_path, active=False)
            assert Path(context.envelope["stop_path"]).exists()
            yield stream

    monkeypatch.setattr(host, "_exclusive_initial_file", guarded)
    active = (renewal.GAMEPLAN_CONTROLS[relative] + "=TRUE\n").encode()
    assert not host.guarded_initial_control(context.envelope, relative, active)
    assert context.stopped()


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded initial activation")
def test_intraday_initial_admission_guard_catches_raw_stop_after_precheck(tmp_path, monkeypatch):
    renewal.atomic_json(tmp_path / "intraday/manual-session.json", {"session_id": "prior", "status": "STOPPED"})
    directory = tmp_path / "state/trader-renewal/intraday/manual-test"
    invoked = []
    original = host.guarded_initial_control

    def guarded(envelope, relative, content):
        renewal.atomic_json(tmp_path / relative, {"session_id": "prior", "status": "STOPPED", "new_stop": True})
        return original(envelope, relative, content)

    class ExitedChild:
        def poll(self):
            return 0

    def command(role, request):
        invoked.append(role)
        message = json.loads(request.read_text())
        host.write_json(message["response"], {"protocol": host.PROTOCOL, "token": message["token"],
            "turn": message["turn"], "role": role, "disposition": "COMPLETED",
            "result": {"session": {"session_id": "new-session"}, "roles": ["execution"]}})
        return ["offline-never-executed-child"]

    monkeypatch.setattr(host, "guarded_initial_control", guarded)
    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **kw: ExitedChild())
    code = host.run_protocol(directory=directory, envelope={"trader": "intraday", "datastore": str(tmp_path),
        "session": {"session_id": "original"}, "roles": []}, command=command)
    assert code == 0 and invoked == ["initialize"]
    assert json.loads((directory / "outcome.json").read_text())["disposition"] == "MANUAL_STOP"


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded initial activation")
@pytest.mark.parametrize("raw_stop", [False, True])
def test_actual_gameplan_initializer_uses_guarded_activation(tmp_path, monkeypatch, raw_stop):
    import dotenv
    from app.services import schwab_token_store
    from tools import native_ownership_cutover, trader_worker
    from ml.account_gameplan import config
    context = fixture_session(tmp_path)
    monkeypatch.setenv("DUCKETS_PRODUCTION_WATCHLIST", os.environ.get("DUCKETS_PRODUCTION_WATCHLIST", ""))
    monkeypatch.setattr(schwab_token_store, "TOKEN_CACHE_PATH", schwab_token_store.TOKEN_CACHE_PATH)
    context.envelope.update(repository=str(tmp_path), watchlist=str(tmp_path / "watchlist.local.txt"))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(native_ownership_cutover, "ensure_gameplan_account_ready",
                        lambda *a, **kw: {"ready": True, "status": "ACCOUNT_READY"})
    monkeypatch.setattr(config, "load_account_config", lambda *a: None)
    monkeypatch.setattr(config, "assert_coordinator", lambda *a: None)
    original = host.guarded_initial_control

    def guarded(envelope, relative, content):
        if raw_stop and "gameplan-stock-trader" in relative:
            path = tmp_path / relative
            before = path.stat().st_mtime_ns
            path.write_bytes(b"CONFIRM_GAMEPLAN_STOCK_TRADING=FALSE\n")
            os.utime(path, ns=(before + 1_000_000_000, before + 1_000_000_000))
        return original(envelope, relative, content)

    monkeypatch.setattr(host, "guarded_initial_control", guarded)
    result = trader_worker.run(context)
    if raw_stop:
        assert result == {"status": "STOPPED"} and context.stopped()
    else:
        assert result == {"session": context.session, "roles": ["execution"]}
        assert not context.stopped()
        assert len(list(Path(context.envelope["stop_path"]).parent.glob("initial-control-*.json"))) == 2
