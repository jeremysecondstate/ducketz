"""Fresh native receipts retain durable transitions and queue publication controls."""
from datetime import datetime, timedelta, timezone
import json
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
        raise AssertionError("Offline fresh receipt encoding regression")
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
    state = col._state()
    key, task = next(iter(state["tasks"].items()))
    return col, key, task, calls


def collect(case):
    col, key, task, calls = case
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
        snapshot = col._collect(key, task)
    finally:
        sys.setprofile(previous_profile)
    assert calls == [True]
    assert observed["lock"] and all(observed["lock"])
    return observed, snapshot


@pytest.mark.parametrize("status", ["EMPTY", "COMPLETE", "PARTIAL", "ERROR", "RATE_LIMITED", "NOT_AUTHORIZED"])
def test_native_fresh_result_keeps_exact_queue_bytes_and_real_transition(case, status):
    col, key, _, calls = case
    def adapter(*args):
        calls.append(True)
        return M.CollectionResult([], status, "Fixture response")
    col.adapters["fmp"] = adapter
    state = col._state()
    state["other_writer"] = {"unicode": "é鸭", "negative_zero": -0.0, "nested": [None, True, 1, 1.25]}
    state["tasks"]["untouched"] = {"owner": "another native worker"}
    M._atomic(col.state_path, state)
    observed, snapshot = collect(case)
    assert observed["atomic_native"] == [True] and observed["canonical_queue"] == []
    raw = col.state_path.read_bytes()
    saved = json.loads(raw)
    assert raw == M.canonical(saved)
    assert saved["other_writer"] == state["other_writer"]
    assert saved["tasks"]["untouched"] == state["tasks"]["untouched"]
    task = saved["tasks"][key]
    assert task["attempts"] == 1 and task["status"] == snapshot["status"]
    assert task["window_finalized"] is False and len(task["snapshots"]) == 1
    assert M.instant(task["due_at"]) == M.instant(NOW) + timedelta(seconds=30)
    receipt = col.root / task["snapshots"][0]
    assert json.loads(receipt.read_bytes()) == snapshot
    assert snapshot["snapshot_sha256"] == M.snapshot_digest(snapshot)
    if status == "NOT_AUTHORIZED":
        assert saved["source_blocks"][col.requests[0].identity] == {
            "observed_at": M.instant(NOW).isoformat(), "snapshot": task["snapshots"][0]}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_native_queue_preserves_original_normalization(case, value):
    col = case[0]
    state = col._state()
    state["legacy_nonfinite"] = value
    col.state_path.write_text(json.dumps(state), encoding="utf-8")
    observed, _ = collect(case)
    assert observed["atomic_native"] == [True] and observed["canonical_queue"] == [True]
    assert json.loads(col.state_path.read_bytes())["legacy_nonfinite"] is None


@pytest.mark.parametrize("kind", ["state", "restoring_state", "result_writer", "restoring_result_writer", "transition", "restoring_transition", "writer", "plain", "canonical", "loads", "dumps", "decoder", "decoder_decode", "decoder_raw_decode", "decoder_scan", "encoder", "encoder_encode", "encoder_iterencode"])
def test_custom_behavior_retains_original_publication(case, monkeypatch, kind):
    col = case[0]
    native_state = M.QuarterHourMarketCollector._native_state_reader
    native_result = M.QuarterHourMarketCollector._native_result_writer
    native_transition = M.QuarterHourMarketCollector._native_transition
    native_atomic, native_plain, native_canonical = M._atomic, M._plain, M.canonical
    if kind in {"state", "restoring_state"}:
        def state(self):
            if (kind == "restoring_state" and any(
                    active[0] == "receipt_commit" for active in self.queue_lock_timing._active.values())):
                self._state = types.MethodType(native_state, self)
            value = native_state(self)
            value["custom"] = {1: "key normalization"}
            return value
        col._state = types.MethodType(state, col)
    elif kind in {"result_writer", "restoring_result_writer"}:
        def writer(self, *args):
            if kind == "restoring_result_writer":
                monkeypatch.setattr(M.QuarterHourMarketCollector, "_write_collection_result", native_result)
            return native_result(self, *args)
        monkeypatch.setattr(M.QuarterHourMarketCollector, "_write_collection_result", writer)
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
    observed, _ = collect(case)
    assert observed["atomic_native"] == [False] and observed["canonical_queue"] == [True]
    if "state" in kind:
        assert json.loads(col.state_path.read_bytes())["custom"] == {"1": "key normalization"}


def test_subclass_keeps_general_encoding(case):
    class Custom(M.QuarterHourMarketCollector):
        pass
    case[0].__class__ = Custom
    observed, _ = collect(case)
    assert observed["atomic_native"] == [False] and observed["canonical_queue"] == [True]


def test_fresh_custom_status_value_is_not_assumed_to_be_native_json(case):
    col, key, _, calls = case
    class Status(str):
        pass
    def adapter(*args):
        calls.append(True)
        return M.CollectionResult([], Status("EMPTY"), "Custom allowed status")
    col.adapters["fmp"] = adapter
    observed, _ = collect(case)
    assert observed["atomic_native"] == [False] and observed["canonical_queue"] == [True]
    assert json.loads(col.state_path.read_bytes())["tasks"][key]["status"] == "EMPTY"


def test_native_result_uses_fresh_queue_and_preserves_other_writer(case, monkeypatch):
    from ml.intraday import market_collection_index as index
    col = case[0]
    native = index.index_receipt
    def index_then_other_writer(*args):
        result = native(*args)
        with col.lock:
            state = col._state()
            state["tasks"]["concurrent-native-row"] = {"evidence": "fresh before receipt commit"}
            state["source_blocks"]["other-source"] = {"snapshot": "untouched"}
            M._atomic(col.state_path, state)
        return result
    monkeypatch.setattr(index, "index_receipt", index_then_other_writer)
    observed, _ = collect(case)
    assert observed["atomic_native"] == [False, True] and observed["canonical_queue"] == [True]
    saved = json.loads(col.state_path.read_bytes())
    assert saved["tasks"]["concurrent-native-row"] == {"evidence": "fresh before receipt commit"}
    assert saved["source_blocks"]["other-source"] == {"snapshot": "untouched"}


def test_fresh_commit_timeout_preserves_receipt_for_native_recovery(case):
    col, key, task, calls = case
    initial = col.state_path.read_bytes()
    col.lock.timeout = 0.01
    # Let the original source-block read finish; hold the queue only after the
    # native receipt has been flushed and indexed, immediately before commit.
    from ml.intraday import market_collection_index as index
    original_index = index.index_receipt
    held = FileLock(col.lock.lock_file, timeout=0)
    original = index.index_receipt
    def hold_after_index(*args):
        value = original_index(*args)
        held.acquire()
        return value
    index.index_receipt = hold_after_index
    try:
        with pytest.raises(Timeout):
            col._collect(key, task)
    finally:
        held.release()
        index.index_receipt = original
    assert col.state_path.read_bytes() == initial and calls == [True]
    receipts = list(col.root.glob("snapshots/**/*.json"))
    assert len(receipts) == 1
    recovered = col._collect(key, task)
    assert calls == [True] and recovered == json.loads(receipts[0].read_bytes())
    assert col._state()["tasks"][key]["attempts"] == 1


@pytest.mark.parametrize("stage", ["state", "transition", "fsync", "replace"])
def test_body_and_atomic_errors_keep_identity_and_durable_receipt(case, monkeypatch, stage):
    col = case[0]
    initial = col.state_path.read_bytes()
    failure = OSError("Original private failure")
    from ml.intraday import market_collection_index as index
    original_index = index.index_receipt
    def fail(*args, **kwargs):
        raise failure
    def install_error(*args):
        value = original_index(*args)
        if stage == "state":
            col._state = fail
        elif stage == "transition":
            monkeypatch.setattr(M.QuarterHourMarketCollector, "_apply_collection_transition", fail)
        elif stage == "fsync":
            import os
            monkeypatch.setattr(os, "fsync", fail)
        else:
            monkeypatch.setattr(Path, "replace", fail)
        return value
    monkeypatch.setattr(index, "index_receipt", install_error)
    with pytest.raises(OSError) as caught:
        col._collect(case[1], case[2])
    assert caught.value is failure and col.state_path.read_bytes() == initial
    assert len(list(col.root.glob("snapshots/**/*.json"))) == 1
    assert list(col.root.glob("*.tmp")) == []


def test_fragment_checks_reject_nonstring_keys_custom_containers_and_leaves():
    class Number(int):
        pass
    class Values(list):
        pass
    class Mapping(dict):
        pass
    assert M._native_receipt_fragment({"native": [None, True, 1, 1.5, "é"]})
    for value in ({1: "key"}, Values([1]), Mapping(value=1), {"value": Number(1)}, {"time": NOW}, {"tuple": (1,)}):
        assert not M._native_receipt_fragment(value)


def test_custom_transition_keeps_original_mapping_dispatch_without_guard_access(case, monkeypatch):
    native = M.QuarterHourMarketCollector._native_transition
    accesses = []
    class Projection(dict):
        def __getitem__(self, key):
            accesses.append(key)
            raise AssertionError("Optimization inspected custom transition mapping")
    def transition(self, state, *args, **kwargs):
        value = native(self, state, *args, **kwargs)
        state["tasks"] = Projection(state["tasks"])
        return value
    monkeypatch.setattr(M.QuarterHourMarketCollector, "_apply_collection_transition", transition)
    observed, _ = collect(case)
    assert accesses == []
    assert observed["atomic_native"] == [False] and observed["canonical_queue"] == [True]
    assert json.loads(case[0].state_path.read_bytes())["tasks"][case[1]]["attempts"] == 1


def test_custom_status_metaclass_keeps_original_fallback_without_type_comparison(case):
    col, _, _, calls = case
    comparisons = []
    class Meta(type):
        __hash__ = type.__hash__
        def __eq__(self, other):
            if other is str or other is int or other is float or other is bool:
                comparisons.append(other)
                raise AssertionError("Optimization compared a custom type")
            return self is other
    class Status(str, metaclass=Meta):
        pass
    def adapter(*args):
        calls.append(True)
        return M.CollectionResult([], Status("EMPTY"), "Custom allowed status")
    col.adapters["fmp"] = adapter
    observed, _ = collect(case)
    assert comparisons == []
    assert observed["atomic_native"] == [False] and observed["canonical_queue"] == [True]
