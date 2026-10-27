"""Native orphan commits keep fresh transitions and atomic publication semantics."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import types

from filelock import FileLock, Timeout
import pytest

from ml.intraday import market_collection as M

NOW = datetime(2026, 10, 27, 16, 15, 2, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline orphan encoding regression")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def case(tmp_path):
    request = M.SourceRequest("fmp", "stock_quote", "TEST", "fixture", "quote", "quote")
    calls = []
    def adapter(*args):
        calls.append(True)
        return M.CollectionResult([], "EMPTY", "Fixture receipt")
    col = M.QuarterHourMarketCollector(tmp_path, symbols=["TEST"], requests=[request],
        adapters={"fmp": adapter}, clock=lambda: NOW)
    col.enqueue(NOW - timedelta(minutes=15, seconds=2))
    initial = col.state_path.read_bytes()
    state = col._state()
    key, task = next(iter(state["tasks"].items()))
    receipt = col._collect(key, task)
    col.state_path.write_bytes(initial)
    return col, key, task, receipt, calls


def recover(case, expected_calls=1):
    col, key, task, expected, calls = case
    observed = {"atomic_native": [], "canonical_queue": [], "lock": []}
    previous_profile = sys.getprofile()
    def trace(frame, event, arg):
        if event == "call" and frame.f_code is M._NATIVE_ATOMIC.__code__:
            observed["atomic_native"].append(frame.f_locals["native_json"])
            observed["lock"].append(col.lock.is_locked)
        if event == "call" and frame.f_code is M._NATIVE_CANONICAL.__code__:
            value = frame.f_locals.get("value")
            if type(value) is dict and value.get("schema_version") == M.QUEUE_VERSION:
                observed["canonical_queue"].append(True)
        return trace
    sys.setprofile(trace)
    try:
        actual = col._collect(key, task)
        if expected is not None:
            assert actual == expected
    finally:
        sys.setprofile(previous_profile)
    assert calls == [True] * expected_calls
    assert observed["lock"] and all(observed["lock"])
    return observed


def test_native_recovered_transition_uses_exact_bytes_without_normalizing_queue(case):
    col, key, _, receipt, _ = case
    state = col._state()
    state["other_writer"] = {"unicode": "é鸭", "negative_zero": -0.0, "nested": [None, True, 1, 1.25]}
    state["tasks"]["untouched"] = {"owned": "another native worker"}
    M._atomic(col.state_path, state)
    observed = recover(case)
    assert observed["atomic_native"] == [True] and observed["canonical_queue"] == []
    actual = col.state_path.read_bytes()
    decoded = json.loads(actual)
    assert actual == M.canonical(decoded)
    assert decoded["other_writer"] == state["other_writer"]
    assert decoded["tasks"]["untouched"] == state["tasks"]["untouched"]
    assert decoded["tasks"][key]["attempts"] == 1
    assert decoded["tasks"][key]["status"] == receipt["status"]
    assert len(decoded["tasks"][key]["snapshots"]) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_native_json_retains_original_normalization(case, value):
    col = case[0]
    state = col._state()
    state["legacy_nonfinite"] = value
    col.state_path.write_text(json.dumps(state), encoding="utf-8")
    observed = recover(case)
    assert observed["atomic_native"] == [True] and observed["canonical_queue"] == [True]
    assert json.loads(col.state_path.read_bytes())["legacy_nonfinite"] is None


@pytest.mark.parametrize("kind", ["state", "restoring_state", "recovery", "restoring_recovery", "transition", "restoring_transition", "writer", "plain", "canonical", "loads", "dumps", "decoder", "decoder_decode", "decoder_raw_decode", "decoder_scan", "encoder", "encoder_encode", "encoder_iterencode"])
def test_custom_behavior_retains_original_two_argument_publication(case, monkeypatch, kind):
    col = case[0]
    native_state = M.QuarterHourMarketCollector._native_state_reader
    native_recovery = M.QuarterHourMarketCollector._native_recovery_reader
    native_transition = M.QuarterHourMarketCollector._native_transition
    native_atomic, native_plain, native_canonical = M._atomic, M._plain, M.canonical
    if kind in {"state", "restoring_state"}:
        def state(self):
            if kind == "restoring_state":
                self._state = types.MethodType(native_state, self)
            value = native_state(self)
            value["custom"] = {1: "key normalization"}
            return value
        col._state = types.MethodType(state, col)
    elif kind in {"recovery", "restoring_recovery"}:
        def recovery(self, *args):
            if kind == "restoring_recovery":
                monkeypatch.setattr(M.QuarterHourMarketCollector, "_existing_settled_receipt", native_recovery)
            return native_recovery(self, *args)
        monkeypatch.setattr(M.QuarterHourMarketCollector, "_existing_settled_receipt", recovery)
    elif kind in {"transition", "restoring_transition"}:
        def transition(self, *args, **kwargs):
            if kind == "restoring_transition":
                monkeypatch.setattr(M.QuarterHourMarketCollector, "_apply_collection_transition", native_transition)
            return native_transition(self, *args, **kwargs)
        monkeypatch.setattr(M.QuarterHourMarketCollector, "_apply_collection_transition", transition)
    elif kind == "writer":
        def writer(path, value):
            return native_atomic(path, value)
        monkeypatch.setattr(M, "_atomic", writer)
    elif kind == "plain":
        monkeypatch.setattr(M, "_plain", lambda value: native_plain(value))
    elif kind == "canonical":
        monkeypatch.setattr(M, "canonical", lambda value: native_canonical(value))
    elif kind in {"loads", "dumps"}:
        original = getattr(json, kind)
        monkeypatch.setattr(json, kind, lambda *args, **kwargs: original(*args, **kwargs))
    elif kind == "decoder":
        monkeypatch.setattr(json, "_default_decoder", json.JSONDecoder())
    elif kind in {"decoder_decode", "decoder_raw_decode", "decoder_scan"}:
        attribute = {"decoder_decode": "decode", "decoder_raw_decode": "raw_decode", "decoder_scan": "scan_once"}[kind]
        original = getattr(json._default_decoder, attribute)
        monkeypatch.setattr(json._default_decoder, attribute, lambda *args, **kwargs: original(*args, **kwargs))
    elif kind == "encoder":
        class Encoder(json.JSONEncoder):
            pass
        monkeypatch.setattr(json, "JSONEncoder", Encoder)
    else:
        attribute = {"encoder_encode": "encode", "encoder_iterencode": "iterencode"}[kind]
        original = getattr(json.JSONEncoder, attribute)
        monkeypatch.setattr(json.JSONEncoder, attribute, lambda self, *args, **kwargs: original(self, *args, **kwargs))
    observed = recover(case)
    assert observed["atomic_native"] == [False]
    assert observed["canonical_queue"] == [True]
    if "state" in kind:
        assert json.loads(col.state_path.read_bytes())["custom"] == {"1": "key normalization"}


def test_subclass_does_not_enter_native_encoding(case):
    col = case[0]
    class Custom(M.QuarterHourMarketCollector):
        pass
    col.__class__ = Custom
    assert recover(case)["atomic_native"] == [False]


def test_custom_recovered_snapshot_retains_general_normalization(case, monkeypatch):
    native = M.QuarterHourMarketCollector._native_recovery_reader
    def custom(self, *args):
        path, receipt = native(self, *args)
        return path, {**receipt, "window_finalized": {1: NOW}}
    monkeypatch.setattr(M.QuarterHourMarketCollector, "_existing_settled_receipt", custom)
    observed = recover((case[0], case[1], case[2], None, case[4]))
    assert observed["atomic_native"] == [False] and observed["canonical_queue"] == [True]
    saved = json.loads(case[0].state_path.read_bytes())["tasks"][case[1]]
    assert saved["window_finalized"] == {"1": NOW.isoformat()}


def test_native_recovery_preserves_fresh_other_worker_queue_changes(case, monkeypatch):
    from ml.intraday import market_collection_index as index
    col = case[0]
    native = index.index_receipt
    def index_then_other_writer(*args):
        result = native(*args)
        with col.lock:
            state = col._state()
            state["tasks"]["concurrent-native-row"] = {"evidence": "fresh before orphan commit"}
            state["source_blocks"]["other-source"] = {"snapshot": "untouched"}
            M._atomic(col.state_path, state)
        return result
    monkeypatch.setattr(index, "index_receipt", index_then_other_writer)
    # The separate native writer itself uses the default encoder before the
    # orphan commit, and the orphan must then use its own fresh native state.
    observed = recover(case)
    assert observed["atomic_native"] == [False, True]
    assert observed["canonical_queue"] == [True]
    saved = json.loads(col.state_path.read_bytes())
    assert saved["tasks"]["concurrent-native-row"] == {"evidence": "fresh before orphan commit"}
    assert saved["source_blocks"]["other-source"] == {"snapshot": "untouched"}
    assert saved["tasks"][case[1]]["attempts"] == 1


def test_fresh_receipt_commit_keeps_original_encoding(case):
    col = case[0]
    for path in col.root.glob("snapshots/**/*.json"):
        path.unlink()
    observed = recover((col, case[1], case[2], None, case[4]), expected_calls=2)
    assert observed["atomic_native"] == [False] and observed["canonical_queue"] == [True]


def test_orphan_commit_keeps_native_lock_timeout_and_original_queue(case):
    col = case[0]
    original = col.state_path.read_bytes()
    col.lock.timeout = 0.01
    with FileLock(col.lock.lock_file, timeout=0):
        with pytest.raises(Timeout):
            col._collect(case[1], case[2])
    assert col.state_path.read_bytes() == original and case[4] == [True]


def test_native_atomic_retries_only_original_windows_replacement_errors(tmp_path, monkeypatch):
    destination = tmp_path / "queue.json"
    destination.write_bytes(b"original")
    original = Path.replace
    retries, pauses = [], []
    def replace(path, target):
        retries.append(path.read_bytes())
        if len(retries) <= 2:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error
        return original(path, target)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(M.time, "sleep", pauses.append)
    M._atomic(destination, {"new": "é"}, native_json=True)
    assert retries == [M.canonical({"new": "é"})] * 3 and pauses == [0.05, 0.1]
    assert destination.read_bytes() == retries[0] and list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("stage", ["fsync", "replace", "encoding"])
def test_native_atomic_preserves_failed_destination_and_cleans_temporary(tmp_path, monkeypatch, stage):
    destination = tmp_path / "queue.json"
    destination.write_bytes(b"original")
    value = {"new": "\ud800" if stage == "encoding" else 1}
    failure = OSError("fixture atomic error")
    if stage == "fsync":
        monkeypatch.setattr(os, "fsync", lambda *args: (_ for _ in ()).throw(failure))
    elif stage == "replace":
        monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(failure))
    with pytest.raises(UnicodeEncodeError if stage == "encoding" else OSError) as caught:
        M._atomic(destination, value, native_json=True)
    if stage != "encoding":
        assert caught.value is failure
    assert destination.read_bytes() == b"original" and list(tmp_path.glob("*.tmp")) == []


def test_default_atomic_preserves_general_normalization(tmp_path):
    class Scalar:
        def item(self):
            return float("nan")
    value = {1: (Scalar(), NOW)}
    path = tmp_path / "queue.json"
    M._atomic(path, value)
    assert path.read_bytes() == M.canonical(value)


def test_native_atomic_type_failure_falls_back_to_general_normalization(tmp_path):
    class Scalar:
        def item(self):
            return 1.5
    value = {"value": Scalar()}
    path = tmp_path / "queue.json"
    M._atomic(path, value, native_json=True)
    assert path.read_bytes() == M.canonical(value)


def test_native_atomic_exhaustion_preserves_error_when_cleanup_fails(tmp_path, monkeypatch):
    destination = tmp_path / "queue.json"
    destination.write_bytes(b"original")
    failure = PermissionError("fixture publication failure")
    failure.winerror = 33
    attempts, pauses = [], []
    def replace(*args):
        attempts.append(True)
        raise failure
    def unlink(*args, **kwargs):
        raise OSError("fixture cleanup failure")
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(M.time, "sleep", pauses.append)
    with pytest.raises(PermissionError) as caught:
        M._atomic(destination, {"native": "bytes"}, native_json=True)
    assert caught.value is failure and attempts == [True] * 5
    assert pauses == [0.05, 0.1, 0.2, 0.4] and destination.read_bytes() == b"original"
    assert len(list(tmp_path.glob("*.tmp"))) == 1
