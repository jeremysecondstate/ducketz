"""Offline real renewal adapter coverage for already supported forecast partitions."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import socket
import sqlite3

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER = _ROOT / "ml/intraday/live_session.py"
if not _ADAPTER.is_file():
    pytest.skip("Installed Atlas renewal adapter required for integration", allow_module_level=True)
import ast
if "run_renewal_lane" not in {node.name for node in ast.parse(_ADAPTER.read_text()).body
                              if isinstance(node, ast.FunctionDef)}:
    pytest.skip("Full installed renewal-lane adapter is absent on this public base", allow_module_level=True)
from ml.intraday import live_session
from ml.intraday import local_execution, split_model


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Partitioned storage fixtures prohibit live network")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import dotenv
    symbols = ("AAPL", "AMZN", "GOOG", "MU")
    now = datetime(2026, 10, 28, 11, 56, tzinfo=timezone.utc)
    config = live_session.ResearchConfig(symbol_workers=4)
    session = dict(session_id="original-admitted-partition-session", actor="Atlas", symbols=list(symbols),
                   architecture=split_model.ARCHITECTURE, started_at=now.isoformat(),
                   close_at=(now + timedelta(hours=1)).isoformat(), config=asdict(config),
                   binding_fingerprint="original-private-binding")
    state = SimpleNamespace(root=tmp_path, now=now, stopped=False, waits=0, calls=[])
    def wait_due(**kwargs):
        state.waits += 1
    state.renewal = SimpleNamespace(session=session, state={"original": "cursor"},
                                   stopped=lambda: state.stopped, wait_due=wait_due)
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", symbols))
    monkeypatch.setattr(local_execution, "load_local_binding", lambda root: SimpleNamespace(
        actor="Atlas", symbols=symbols, fingerprint=session["binding_fingerprint"]))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    def model(**kwargs):
        state.calls.append(kwargs)
        return {"status": "FIXTURE_MODEL_PASS", "lane_index": kwargs["lane_index"]}
    monkeypatch.setattr(split_model, "renewal_model_pass", model)
    def run(role, sleep=lambda seconds: None):
        return live_session.run_renewal_lane(role, repository=tmp_path / "repo", datastore=tmp_path,
            profile=tmp_path / "profile.json", watchlist=tmp_path / "watchlist.txt", renewal=state.renewal,
            clock=lambda: state.now, sleep=sleep)
    state.run = run
    return state


@pytest.mark.parametrize("index", range(4))
def test_actual_partition_adapter_reaches_model_pass_with_original_identity(env, index):
    before = json.dumps([env.renewal.session, env.renewal.state], sort_keys=True)
    assert env.run("forecast-" + str(index)) == {"status": "FIXTURE_MODEL_PASS", "lane_index": index}
    assert env.waits == 1 and len(env.calls) == 1
    assert env.calls[0]["lane_index"] == index and env.calls[0]["lane_count"] == 4
    assert env.calls[0]["renewal"] is env.renewal
    assert json.dumps([env.renewal.session, env.renewal.state], sort_keys=True) == before
    assert not (env.root / "intraday/manual-session.json").exists()
    assert not (env.root / "state/trader-renewal").exists()


@pytest.mark.parametrize("role", ["forecast-4", "forecast-99"])
def test_actual_adapter_still_rejects_index_outside_frozen_concurrency(env, role):
    with pytest.raises(live_session.ContractError, match="frozen concurrency"):
        env.run(role)
    assert not env.calls and env.waits == 0


def test_partition_requires_existing_split_atlas_architecture(env):
    env.renewal.session.pop("architecture")
    with pytest.raises(live_session.ContractError, match="Partitioned model lanes"):
        env.run("forecast-0")
    assert not env.calls and env.waits == 0


@pytest.mark.parametrize("control", ["manual-stop", "original-cutoff"])
def test_actual_partition_adapter_keeps_early_stop_and_cutoff(env, monkeypatch, control):
    if control == "manual-stop":
        env.stopped = True
    else:
        env.now = datetime.fromisoformat(env.renewal.session["close_at"])
    monkeypatch.setattr(live_session, "ShadowStore", lambda *args: pytest.fail("Storage setup after stop/cutoff"))
    expected = "STOPPED" if control == "manual-stop" else "COMPLETED"
    assert env.run("forecast-0") == {"status": expected, "role": "forecast-0"}
    assert not env.calls and env.waits == 0


@pytest.mark.parametrize("control", ["recover", "manual-stop", "original-cutoff"])
def test_actual_adapter_native_sqlite_contention_preserves_controls(env, monkeypatch, control):
    path = env.root / "contention.sqlite3"
    holder = sqlite3.connect(path, timeout=0)
    holder.execute("CREATE TABLE preserved(value TEXT)")
    holder.execute("INSERT INTO preserved VALUES('original immutable bytes')")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    original_store = live_session.ShadowStore
    attempts, closed_connections = [], []
    def contended_store(target):
        attempts.append(target)
        connection = sqlite3.connect(path, timeout=0)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS setup_probe(value TEXT)")
        finally:
            connection.close()
            closed_connections.append(connection)
        return original_store(target)
    monkeypatch.setattr(live_session, "ShadowStore", contended_store)
    tick = [0.]
    monkeypatch.setattr(live_session.time, "monotonic", lambda: tick[0])
    def release(seconds):
        holder.rollback()
        tick[0] += seconds
        if control == "manual-stop":
            env.stopped = True
        elif control == "original-cutoff":
            env.now = datetime.fromisoformat(env.renewal.session["close_at"])
    try:
        result = env.run("forecast-3", sleep=release)
        if control == "recover":
            assert result == {"status": "FIXTURE_MODEL_PASS", "lane_index": 3}
            assert len(attempts) == 2 and len(env.calls) == 1 and env.waits == 1
        else:
            assert result == {"status": "STOPPED" if control == "manual-stop" else "COMPLETED", "role": "forecast-3"}
            assert len(attempts) == 1 and not env.calls and env.waits == 0
        for db in closed_connections:
            with pytest.raises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
        assert holder.execute("SELECT * FROM preserved").fetchall() == [("original immutable bytes",)]
    finally:
        holder.rollback()
        holder.close()
