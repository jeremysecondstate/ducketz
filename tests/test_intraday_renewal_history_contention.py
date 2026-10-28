"""Offline history admission: native contention cannot become missing science."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ml/intraday/live_session.py"
METADATA = ROOT / "docs/development/patches/intraday-renewal-history-contention.json"
NOW = datetime(2026, 10, 8, 17, 16, tzinfo=timezone.utc)


def history_function_source():
    raw = SOURCE.read_bytes() if SOURCE.is_file() else b""
    tree = ast.parse(raw)
    node = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_renewal_histories"), None)
    if METADATA.is_file():
        # The public common base carries the exact reviewed local patch, not
        # Atlas's private composed application or operating bindings.
        metadata = json.loads(METADATA.read_bytes())
        assert metadata["operation"] == "modify" and metadata["target"] == "ml/intraday/live_session.py"
        for key in ("patch_sha256", "local_before_sha256", "local_after_sha256",
                    "before_function_ast_sha256", "function_ast_sha256"):
            value = metadata[key]
            assert isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
        assert metadata["local_before_sha256"] != metadata["local_after_sha256"]
        package = ROOT / metadata["patch_path"]
        patch_raw = package.read_bytes()
        assert hashlib.sha256(patch_raw).hexdigest() == metadata["patch_sha256"]
        helper_path = ROOT / "tests/intraday_split_package_support.py"
        spec = importlib.util.spec_from_file_location("history_patch_support", helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        entries = helper.parse_patch(patch_raw)
        assert set(entries) == {"ml/intraday/live_session.py"}
        entry = entries["ml/intraday/live_session.py"]
        assert entry["before"] == entry["after"] == "ml/intraday/live_session.py"
        assert len(entry["hunks"]) == 1
        before_tree = ast.parse(b"".join(entry["hunks"][0]["old_lines"]))
        after_tree = ast.parse(b"".join(entry["hunks"][0]["new_lines"]))
        assert len(before_tree.body) == len(after_tree.body) == 1
        before_node, projected = before_tree.body[0], after_tree.body[0]
        assert isinstance(before_node, ast.FunctionDef) and before_node.name == "_renewal_histories"
        assert isinstance(projected, ast.FunctionDef) and projected.name == "_renewal_histories"
        assert hashlib.sha256(ast.dump(before_node, include_attributes=False).encode()).hexdigest() == metadata["before_function_ast_sha256"]
        assert hashlib.sha256(ast.dump(projected, include_attributes=False).encode()).hexdigest() == metadata["function_ast_sha256"]
        # Only the new dependency and the native-contention branch may differ.
        restored = copy.deepcopy(projected)
        data_import = next(n for n in restored.body if isinstance(n, ast.ImportFrom) and n.module == "data")
        assert [n.name for n in data_import.names] == ["AcquisitionPending", "load_local_history"]
        data_import.names = data_import.names[1:]
        handlers = [n for n in ast.walk(restored) if isinstance(n, ast.ExceptHandler)]
        assert len(handlers) == 1 and isinstance(handlers[0].body[0], ast.Assign) and isinstance(handlers[0].body[1], ast.If)
        handlers[0].body = handlers[0].body[2:]
        assert ast.dump(restored, include_attributes=False) == ast.dump(before_node, include_attributes=False)
        if node is not None:
            assert hashlib.sha256(raw).hexdigest() == metadata["local_after_sha256"]
            assert ast.dump(node, include_attributes=False) == ast.dump(projected, include_attributes=False)
        node = projected
    assert node is not None, "The candidate lacks both composed history source and its exact reviewed patch"
    return node


class Pending(ValueError):
    def __init__(self, reason, retry_after_seconds=4):
        self.reason, self.retry_after_seconds = reason, retry_after_seconds
        super().__init__(reason)


def histories(loader):
    package = "verified_history_contention_fixture"
    data, coverage = ModuleType(package + ".data"), ModuleType(package + ".coverage")
    data.load_local_history, data.AcquisitionPending = loader, Pending
    coverage.RECOVERY_POPULATION_CONTRACT = "fixture-recovery"
    namespace = dict(__package__=package, threading=threading, sqlite3=sqlite3,
                     instant=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
    node = history_function_source()
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    events = []
    session = dict(session_id="original-session", symbols=["AAPL", "CROX"], started_at=NOW.isoformat())
    config = SimpleNamespace(source_dataset="EQUS.MINI", population_contract="fixture-recovery")
    with patch.dict(sys.modules, {data.__name__: data, coverage.__name__: coverage}):
        result = namespace["_renewal_histories"](Path("unused-root"),
            SimpleNamespace(event=lambda *args, **kwargs: events.append((args, kwargs))), session, config)
    return result, events, session


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("History contention fixtures prohibit providers and brokers")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


@contextmanager
def native_contention(tmp_path, mode):
    path = tmp_path / "original-native.sqlite3"
    target = path.as_uri() + "?cache=shared" if mode == "locked" else str(path)
    def connect():
        return sqlite3.connect(target, uri=mode == "locked", timeout=0)
    owner = connect()
    owner.execute("CREATE TABLE original(value TEXT)")
    owner.execute("INSERT INTO original VALUES('unchanged original observations')")
    owner.commit()
    owner.execute("BEGIN IMMEDIATE" if mode == "locked" else "BEGIN EXCLUSIVE")
    if mode == "locked":
        owner.execute("UPDATE original SET value=value")
    calls = []
    def read(*args, **kwargs):
        calls.append((args, kwargs))
        db = connect()
        try:
            return db.execute("SELECT value FROM original").fetchone()[0]
        finally:
            db.close()
    try:
        yield read, owner.rollback, calls
    finally:
        owner.rollback()
        owner.close()


@pytest.mark.parametrize("mode", ["busy", "locked"])
@pytest.mark.parametrize("access", ["get", "getitem", "contains", "values", "iter"])
def test_native_contention_retries_same_lazy_history_without_negative_cache(tmp_path, mode, access):
    with native_contention(tmp_path, mode) as (load, release, calls):
        mapping, events, session = histories(load)
        operations = {"get": lambda: mapping.get("AAPL"), "getitem": lambda: mapping["AAPL"],
                      "contains": lambda: "AAPL" in mapping, "values": lambda: mapping.values(),
                      "iter": lambda: iter(mapping)}
        with pytest.raises(Pending) as caught:
            operations[access]()
        cause = caught.value.__cause__
        assert isinstance(cause, sqlite3.OperationalError)
        assert cause.sqlite_errorcode & 255 == (sqlite3.SQLITE_BUSY if mode == "busy" else sqlite3.SQLITE_LOCKED)
        assert caught.value.reason == "local_history_storage_contention"
        assert caught.value.retry_after_seconds == 1
        assert not mapping.missing and not events
        release()
        assert mapping.get("AAPL") == "unchanged original observations"
        assert mapping["AAPL"] == "unchanged original observations" and len(calls) == 2
        assert calls[0] == calls[1]
        assert calls[1][1] == dict(source_dataset="EQUS.MINI", population_contract="fixture-recovery", recovery_cutoff=NOW)
        assert session["started_at"] == NOW.isoformat()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_RECOVERY,
    sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_BUSY_TIMEOUT, sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_LOCKED_SHAREDCACHE])
def test_extended_native_contention_retains_original_exception(code):
    error = sqlite3.OperationalError("fixture native error")
    error.sqlite_errorcode = code
    def load(*args, **kwargs):
        raise error
    mapping, events, _ = histories(load)
    with pytest.raises(Pending) as caught:
        mapping.get("CROX")
    assert caught.value.__cause__ is error and not mapping.missing and not events


@pytest.mark.parametrize("kind", ["missing", "corrupt", "io", "no-code", "bool-code", "wrong-type", "pending"])
def test_actual_missing_and_other_errors_keep_existing_terminal_behavior(kind):
    error = {"missing": FileNotFoundError("actual missing history"), "corrupt": ValueError("actual corruption"),
             "io": sqlite3.OperationalError("database is locked"), "no-code": sqlite3.OperationalError("database is locked"),
             "bool-code": sqlite3.OperationalError("database is locked"), "wrong-type": RuntimeError("database is locked"),
             "pending": Pending("unrelated-prerequisite")}[kind]
    if kind in {"io", "bool-code", "wrong-type"}:
        error.sqlite_errorcode = {"io": sqlite3.SQLITE_IOERR, "bool-code": True, "wrong-type": sqlite3.SQLITE_BUSY}[kind]
    calls = []
    def load(*args, **kwargs):
        calls.append(True)
        raise error
    mapping, events, _ = histories(load)
    assert mapping.get("CROX") is None and mapping.get("CROX") is None
    assert mapping.missing == {"CROX"} and calls == [True]
    assert len(events) == 1 and events[0][0][1] == "history_unavailable"
    assert events[0][0][2] == dict(error_type=type(error).__name__, message=str(error))


@pytest.mark.parametrize("mode", ["busy", "locked"])
def test_actual_data_service_retries_original_cycle_after_native_history_lock(tmp_path, monkeypatch, mode):
    required = ["ml/intraday/split_service.py", "ml/intraday/split_handoff.py", "ml/intraday/store.py"]
    if not all((ROOT / name).is_file() for name in required) or b"def _renewal_histories(" not in SOURCE.read_bytes():
        pytest.skip("Public common base lacks Atlas's composed split data adapters; portable exact-function cases still run")
    import numpy as np
    import pandas as pd
    from ml.intraday import data, live_session
    from ml.intraday.prediction import STEP, digest, instant
    from ml.intraday.runtime import ResearchConfig, ShadowRunner, LiveHistories
    from ml.intraday.split_handoff import ReadySnapshots
    from ml.intraday.split_service import DataService
    from ml.intraday.store import ShadowStore
    state = SimpleNamespace(now=NOW)
    clock = lambda: state.now.isoformat()
    reference = NOW - timedelta(minutes=16)
    config = ResearchConfig(models=("fixture_a", "fixture_b"), symbol_workers=2)
    symbols = ("AAPL", "CROX")
    store, collection = ShadowStore(tmp_path / "prepare.sqlite3"), ShadowStore(tmp_path / "collection.sqlite3")
    handoff = ReadySnapshots(tmp_path / "ready", actor="Atlas", symbols=symbols, clock=clock)
    rows = {symbol: dict(symbol=symbol, opened_at=reference.isoformat(), available_at=(reference + STEP).isoformat(),
        availability_kind="actual", interval_seconds=900, open=100, high=101, low=99, close=100, volume=100,
        synthetic=False, source_dataset="EQUS.MINI", source_id="databento:EQUS.MINI:ohlcv-1m") for symbol in symbols}
    for row in rows.values():
        collection.record_candle(row)
    def builder(frame, *, cutoff, reference_at, **kwargs):
        return SimpleNamespace(X=np.array([[0], [1]]), y=np.array([0, 1]), inference_X=np.array([[1]]),
            feature_names=("original-fixture-feature",), label_available_at=[reference - STEP, reference],
            reference_at=[reference - 2 * STEP, reference - STEP], target_close_at=[reference - STEP, reference],
            inference=dict(symbol=frame.iloc[0].symbol, reference_at=reference_at.isoformat(),
                information_available_at=(reference + STEP).isoformat(), availability_kind="actual", reference_open=100),
            manifest={"training_rows": 2})
    runner = ShadowRunner(store=store, pc="Atlas", symbols=symbols, config=config, clock=clock,
        dataset_builder=builder, trainer=lambda *a, **k: pytest.fail("Data preparation fitted a model"))
    session = dict(session_id="data-original-session", symbols=list(symbols), started_at=NOW.isoformat())
    original_session = dict(session)
    with native_contention(tmp_path, mode) as (read, release, calls):
        def load(root, symbol, **kwargs):
            if symbol == "CROX":
                read(root, symbol, **kwargs)
            frame = pd.DataFrame([rows[symbol]])
            for column in ("opened_at", "available_at"):
                frame[column] = pd.to_datetime(frame[column], utc=True)
            return data.History(frame, {"source_dataset": "EQUS.MINI"})
        monkeypatch.setattr(data, "load_local_history", load)
        mapping = live_session._renewal_histories(tmp_path, collection, session, config)
        def incoming(ref):
            mapping.missing.clear()
            return LiveHistories(histories=mapping, provider=SimpleNamespace(dataset="EQUS.MINI"), store=collection,
                journal=None, reference_at=ref, clock=clock, bounded=True, restore_eagerly=False,
                reuse_saved=True, population_contract="legacy")
        service = DataService(runner=runner, handoff=handoff, incoming=incoming,
            session_id=session["session_id"], clock=clock)
        first = service.pass_once(reference_at=reference)
        cycle_id = first["cycle_id"]
        with store.connect() as db:
            frozen = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()[0])
        error = frozen["failures"]["CROX"]
        assert error["type"] == "AcquisitionPending" and error["retryable"] is True
        assert error["reason"] == "local_history_storage_contention" and error["retry_after_seconds"] == 1
        assert instant(error["retry_at"]) == NOW + timedelta(seconds=1)
        assert first["prepared_symbols"] == ["AAPL"] and not mapping.missing
        assert service.pass_once(reference_at=reference)["status"] == "WAITING_FOR_DATA"
        assert len(calls) == 1
        configuration_hash = digest(asdict(config))
        original_ready = handoff.ready(clock(), configuration_hash=configuration_hash)[0]
        release()
        state.now += timedelta(seconds=1)
        second = service.pass_once(reference_at=reference)
        assert second["cycle_id"] == cycle_id and second["status"] == "prepared"
        assert second["prepared_symbols"] == ["CROX"] and len(calls) == 2
        records = handoff.ready(clock(), configuration_hash=configuration_hash)
        assert {record["symbol"] for record in records} == set(symbols)
        assert next(record for record in records if record["symbol"] == "AAPL") == original_ready
        for record in records:
            cycle, snapshot = handoff.load(record)
            assert cycle["started_at"] == frozen["started_at"] and cycle["training_cutoff"] == frozen["training_cutoff"]
            assert [instant(value) for value in snapshot["reference_at"]] == [reference - 2 * STEP, reference - STEP]
            assert instant(snapshot["inference"]["reference_at"]) == reference
            assert instant(record["reference_at"]) == reference
            assert instant(record["target_open"]) == reference + 2 * STEP
            assert instant(record["target_close"]) == reference + 3 * STEP
            assert instant(record["ready_at"]) < reference + 3 * STEP
            assert record["producer_session"] == "system-1a:atlas:2026-10-08"
        assert session == original_session
        assert runner.store.forecasts() == []
