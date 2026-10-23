"""Offline status publication; private files and injected replacement only."""
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from ml.intraday import live_session


def denied(code):
    error = PermissionError(13, "fixture replacement denied")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32])
def test_transient_windows_replace_reuses_same_paths_and_bytes(tmp_path, monkeypatch, code):
    target, temporary = tmp_path / "status.json", tmp_path / "status.tmp"
    target.write_bytes(b"original")
    frozen = b'{"status":"STOPPED","manual_start":true}\n'
    temporary.write_bytes(frozen)
    replacement, calls, pauses = os.replace, [], []
    original_error = denied(code)

    def replace(source, destination):
        calls.append((source, destination))
        assert temporary.read_bytes() == frozen and target.read_bytes() == b"original"
        if len(calls) == 1:
            raise original_error
        replacement(source, destination)

    def pause(seconds):
        pauses.append(seconds)
        assert temporary.read_bytes() == frozen and target.read_bytes() == b"original"

    monkeypatch.setattr(live_session, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(live_session, "time", SimpleNamespace(sleep=pause))
    live_session._replace_session_status(temporary, target)
    assert calls == [(temporary, target)] * 2 and pauses == [0.02]
    assert target.read_bytes() == frozen and not temporary.exists()


@pytest.mark.parametrize("code", [5, 32])
def test_permanent_windows_denial_is_bounded_and_preserves_evidence(tmp_path, monkeypatch, code):
    source, target = tmp_path / "status.tmp", tmp_path / "status.json"
    source.write_bytes(b"pending")
    target.write_bytes(b"original")
    failures, pauses = [], []

    def replace(*paths):
        failures.append(denied(code))
        raise failures[-1]

    monkeypatch.setattr(live_session, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(live_session, "time", SimpleNamespace(sleep=pauses.append))
    with pytest.raises(PermissionError) as caught:
        live_session._replace_session_status(source, target)
    assert caught.value is failures[-1] and len(failures) == 4
    assert pauses == [0.02, 0.04, 0.06] and sum(pauses) == 0.12
    assert source.read_bytes() == b"pending" and target.read_bytes() == b"original"


@pytest.mark.parametrize("platform,code", [("posix", 5), ("posix", 32), ("nt", None),
                                           ("nt", 2), ("nt", 13), ("nt", 87)])
def test_other_permission_errors_are_immediate(monkeypatch, platform, code):
    original, calls, pauses = denied(code), [], []

    def replace(*paths):
        calls.append(paths)
        raise original

    monkeypatch.setattr(live_session, "os", SimpleNamespace(name=platform, replace=replace))
    monkeypatch.setattr(live_session, "time", SimpleNamespace(sleep=pauses.append))
    with pytest.raises(PermissionError) as caught:
        live_session._replace_session_status("pending", "status")
    assert caught.value is original and len(calls) == 1 and pauses == []


@pytest.mark.parametrize("original", [FileNotFoundError("fixture"), OSError("fixture"), KeyboardInterrupt()])
def test_unrelated_errors_and_manual_interrupt_propagate(monkeypatch, original):
    calls, pauses = [], []

    def replace(*paths):
        calls.append(paths)
        raise original

    monkeypatch.setattr(live_session, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(live_session, "time", SimpleNamespace(sleep=pauses.append))
    with pytest.raises(type(original)) as caught:
        live_session._replace_session_status("pending", "status")
    assert caught.value is original and len(calls) == 1 and pauses == []


def test_manual_interrupt_during_retry_is_not_swallowed(monkeypatch):
    calls = []
    interrupted = KeyboardInterrupt()

    def replace(*paths):
        calls.append(paths)
        raise denied(5)

    def pause(seconds):
        raise interrupted

    monkeypatch.setattr(live_session, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(live_session, "time", SimpleNamespace(sleep=pause))
    with pytest.raises(KeyboardInterrupt) as caught:
        live_session._replace_session_status("pending", "status")
    assert caught.value is interrupted and len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows destination sharing semantics")
def test_real_held_reader_retries_after_reader_closes(tmp_path, monkeypatch):
    target, temporary = tmp_path / "status.json", tmp_path / "status.tmp"
    target.write_bytes(b"original")
    temporary.write_bytes(b"replacement")
    held = target.open("rb")
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        assert held.read() == b"original" and temporary.read_bytes() == b"replacement"
        held.close()

    monkeypatch.setattr(live_session, "time", SimpleNamespace(sleep=pause))
    try:
        live_session._replace_session_status(temporary, target)
    finally:
        held.close()
    assert pauses == [0.02] and target.read_bytes() == b"replacement"
    assert not temporary.exists()


@pytest.mark.parametrize("state", ["STARTING", "RUNNING", "CLOSING", "STOPPED", "FAILED"])
def test_actual_status_callsite_serializes_once_and_preserves_manual_state(tmp_path, monkeypatch, state):
    source = Path(live_session.__file__).read_text(encoding="utf8")
    outer = next(v for v in ast.parse(source).body if isinstance(v, ast.FunctionDef) and v.name == "run_manual")
    status = next(v for v in outer.body if isinstance(v, ast.FunctionDef) and v.name == "status")
    module = ast.Module(body=[status], type_ignores=[])
    target = tmp_path / "manual-session.json"
    target.write_bytes(b"previous")
    now = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
    clocks, serializations, replacements, pauses = [], [], [], []
    replacement = os.replace

    def clock():
        clocks.append(now)
        return now

    def encoded(value):
        serializations.append(value)
        return json.dumps(value, sort_keys=True)

    def replace(temporary, destination):
        replacements.append((temporary, destination, temporary.read_bytes()))
        if len(replacements) == 1:
            raise denied(5)
        replacement(temporary, destination)

    monkeypatch.setattr(live_session, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(live_session, "time", SimpleNamespace(sleep=pauses.append))
    scope = dict(VERSION=live_session.VERSION, session_id="fixture-manual-session", os=os, started=now,
                 clock=clock, instant=lambda value: value, close_at=now,
                 final_collection_complete=SimpleNamespace(is_set=lambda: False), status_path=target,
                 uuid=uuid, encoded=encoded, _replace_session_status=live_session._replace_session_status)
    exec(compile(module, str(Path(live_session.__file__)), "exec"), scope)
    scope["status"](state, stop_reason="fixture-manual-control", broker_orders_automatically_cancelled=False)
    assert len(clocks) == len(serializations) == 1
    assert len(replacements) == 2 and replacements[0] == replacements[1] and pauses == [0.02]
    value = json.loads(target.read_bytes())
    assert value["status"] == state and value["manual_start"] is True
    assert value["session_id"] == "fixture-manual-session" and value["stop_reason"] == "fixture-manual-control"
    assert value["broker_orders_automatically_cancelled"] is False
    assert target.read_bytes() == replacements[0][2]
    assert not replacements[0][0].exists()
