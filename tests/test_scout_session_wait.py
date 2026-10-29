"""Offline proof of the existing 1A session gate; no provider or models run."""
from contextlib import nullcontext
from datetime import datetime, timedelta
import json
import socket
from types import SimpleNamespace

import dotenv
import pytest

from datafetching import runtime_lock
from ml.intraday import data, live_session, runtime, scout_services


class InitializationReached(Exception):
    pass


@pytest.fixture
def waiting_service(tmp_path, monkeypatch):
    wake = datetime.fromisoformat("2026-10-29T11:00:00+00:00")
    started = wake - timedelta(seconds=1)
    elapsed = [0]
    initialized = []
    pauses = []
    status_path = tmp_path / "intraday/services/data-status.json"

    def clock():
        return started + timedelta(microseconds=elapsed[0])

    def forbidden(*args, **kwargs):
        raise AssertionError("Session gate fixture reached history, provider, models, or network")

    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(data, "load_local_history", forbidden)
    monkeypatch.setattr(live_session, "prepare_minute_provider", forbidden)
    monkeypatch.setattr(live_session, "prepare_market_collection", forbidden)
    monkeypatch.setattr(runtime, "ShadowRunner", forbidden)
    monkeypatch.setattr(runtime_lock, "exclusive_runtime_lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(scout_services, "ReadySpool", lambda *args, **kwargs: object())
    monkeypatch.setattr(scout_services, "ServiceStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(scout_services, "_configuration", lambda *args, **kwargs:
        ("Scout", ("AAPL",), SimpleNamespace(publication_offset_seconds=30), {}, "fixture-recipe", None))
    monkeypatch.setattr(live_session, "manual_wake_at", lambda _: wake)
    monkeypatch.setattr(scout_services, "time", SimpleNamespace(monotonic=lambda: elapsed[0] / 1_000_000))

    def first_initialization(*args, **kwargs):
        initialized.append(clock())
        raise InitializationReached()

    monkeypatch.setattr(dotenv, "load_dotenv", first_initialization)

    def sleep(seconds):
        assert clock() < wake
        assert initialized == []
        saved = json.loads(status_path.read_text())
        assert saved["status"] == "STARTING"
        assert datetime.fromisoformat(saved["wake_at"]) == wake
        pauses.append(clock())
        elapsed[0] += max(1, round(seconds * 1_000_000))

    def run(*, stopped=lambda: False):
        return scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
            release={"release_root": "fixture-release", "manifest_sha256": "fixture-manifest"},
            clock=clock, sleep=sleep, stopped=stopped)

    return SimpleNamespace(run=run, wake=wake, clock=clock, initialized=initialized,
                           pauses=pauses, status_path=status_path)


def test_1a_waits_until_four_before_first_operating_initialization(waiting_service):
    case = waiting_service
    with pytest.raises(InitializationReached):
        case.run()
    assert case.pauses and all(observed < case.wake for observed in case.pauses)
    assert case.initialized == [case.wake]
    assert case.clock() == case.wake


def test_1a_stop_before_session_never_initializes_environment_or_provider(waiting_service):
    case = waiting_service
    result = case.run(stopped=lambda: True)
    assert result["status"] == "STOPPED"
    assert case.initialized == [] and case.pauses == []
    assert case.clock() < case.wake
    assert json.loads(case.status_path.read_text())["status"] == "STOPPED"
