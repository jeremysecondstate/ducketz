"""Offline admission accounting and idle-process bounds; no production fits."""
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=("tests/test_intraday_split_services.py",),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/runtime.py", "ShadowRunner", "__init__",
                 ("prepared_source", "forecast_preparer", "datastore"))))

from ml.intraday import split_model
from ml.intraday.prediction import STEP, instant
from tests.test_intraday_split_services import _environment, no_network


def scope_events(store):
    with store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='system_1b_scope_accounting' ORDER BY id")]


def test_unready_assigned_scope_has_durable_truthful_deferrals_once_per_transition(tmp_path):
    env = _environment(tmp_path)
    first = env.consume()
    assert first["status"] == "WAITING_FOR_READY"
    evidence = scope_events(env.science_store)
    assert len(evidence) == 1
    assert evidence[0]["reference_at"] == (env.now - timedelta(minutes=16)).isoformat()
    assert evidence[0]["scope_symbols"] == list(env.symbols)
    assert evidence[0]["assigned_symbols"] == list(env.symbols)
    assert set(evidence[0]["symbols"]) == set(env.symbols)
    assert all(value == {"disposition": "deferred", "reason": "no_verified_current_ready_snapshot"}
               for value in evidence[0]["symbols"].values())
    second = env.consume(state=first["state"])
    assert len(scope_events(env.science_store)) == 1
    assert env.outputs == [] and env.science_store.forecasts() == []
    env.now += STEP
    env.consume(state=second["state"])
    assert len(scope_events(env.science_store)) == 2
    assert scope_events(env.science_store)[0] == evidence[0]


def test_bounded_partition_accounts_deferred_symbol_then_publishes_it(tmp_path):
    env = _environment(tmp_path)
    env.service.pass_once()
    first = env.consume(max_symbols=1)
    final = scope_events(env.science_store)[-1]
    published = first["symbols"][0]
    deferred = next(symbol for symbol in env.symbols if symbol != published)
    assert final["symbols"][published]["disposition"] == "published"
    assert final["symbols"][deferred]["reason"] == "bounded_fair_queue"
    second = env.consume(max_symbols=1, state=first["state"])
    assert second["symbols"] == [deferred]
    assert all(value["disposition"] == "published"
               for value in scope_events(env.science_store)[-1]["symbols"].values())


def test_invalid_ready_record_is_durable_and_does_not_hide_other_symbol(tmp_path):
    env = _environment(tmp_path)
    env.service.pass_once()
    from dataclasses import asdict
    from ml.intraday.prediction import digest
    records = env.handoff.ready(env.clock(), configuration_hash=digest(asdict(env.config)))
    bad = records[0]
    env.handoff._path("records", bad["record_sha256"]).write_bytes(b"partial")
    result = env.consume()
    assert len(result["symbols"]) == 1 and bad["symbol"] not in result["symbols"]
    evidence = scope_events(env.science_store)[-1]["symbols"][bad["symbol"]]
    assert evidence == {"disposition": "deferred", "reason": "invalid_ready_record", "error_type": "ContractError"}


def test_model_measurements_separate_snapshot_admission_and_model_work(tmp_path):
    env = _environment(tmp_path)
    env.service.pass_once()
    env.consume()
    with env.model_store.connect() as db:
        evidence = json.loads(db.execute("SELECT payload FROM events WHERE kind='system_1b_model_pass'").fetchone()[0])
    assert evidence["ready_index_seconds"] >= 0
    assert evidence["admission_seconds"] >= sum(evidence["snapshot_validation_seconds_by_symbol"].values())
    assert set(evidence["snapshot_validation_seconds_by_symbol"]) == set(env.symbols)
    assert evidence["model_pass_cpu_seconds"] >= 0 and evidence["admission_cpu_seconds"] >= 0
    assert all(evidence["model_started_queue_wait_seconds_by_symbol"][s] >= evidence["queue_wait_seconds_by_symbol"][s]
               for s in env.symbols)


def test_admission_reuses_verified_hash_without_an_extra_matrix_serialization(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    env.service.pass_once()
    original = split_model.digest
    def digest(value):
        assert not (isinstance(value, dict) and "X" in value), "Redundant wrapper matrix serialization"
        return original(value)
    monkeypatch.setattr(split_model, "digest", digest)
    assert env.consume()["status"] == "PUBLISHED"


def test_expiry_during_snapshot_verification_records_original_target_and_never_fits(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    env.service.pass_once()
    original = env.handoff.load
    reference = env.now - timedelta(minutes=16)
    def delayed(record):
        value = original(record)
        env.now += timedelta(minutes=30)
        return value
    monkeypatch.setattr(env.handoff, "load", delayed)
    result = env.consume(max_symbols=1,
        runner_factory=lambda **kwargs: pytest.fail("Expired handoff started model work"))
    assert result["status"] == "EXPIRED" and env.outputs == []
    evidence = scope_events(env.science_store)[-1]
    assert evidence["reference_at"] == reference.isoformat()
    assert evidence["target_close"] == (reference + 3 * STEP).isoformat()
    assert any(value.get("reason") == "original_target_expired" for value in evidence["symbols"].values())


def busy(code=sqlite3.SQLITE_BUSY):
    error = sqlite3.OperationalError("offline fixture storage failure")
    error.sqlite_errorcode = code
    error.sqlite_errorname = {sqlite3.SQLITE_BUSY: "SQLITE_BUSY", sqlite3.SQLITE_LOCKED: "SQLITE_LOCKED",
                             sqlite3.SQLITE_IOERR: "SQLITE_IOERR"}[code]
    return error


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_ready_index_contention_retains_deferrals_then_recovers_original_inputs(tmp_path, monkeypatch, code):
    env = _environment(tmp_path)
    env.service.pass_once()
    original = env.handoff.ready
    def contended(*args, **kwargs):
        raise busy(code)
    monkeypatch.setattr(env.handoff, "ready", contended)
    result = env.consume()
    assert result["status"] == "WAITING_FOR_READY" and env.outputs == []
    evidence = scope_events(env.science_store)[-1]
    assert all(value["reason"] == "ready_index_storage_contention" and value["sqlite_errorcode"] == code
               for value in evidence["symbols"].values())
    monkeypatch.setattr(env.handoff, "ready", original)
    assert env.consume(state=result["state"])["status"] == "PUBLISHED"


def test_noncontention_ready_index_error_is_not_reclassified_as_wait(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    def corrupt(*args, **kwargs):
        raise busy(sqlite3.SQLITE_IOERR)
    monkeypatch.setattr(env.handoff, "ready", corrupt)
    with pytest.raises(sqlite3.OperationalError):
        env.consume()


class Checkpoint(BaseException):
    pass


def idle_fixture(monkeypatch):
    clock = SimpleNamespace(now=instant("2026-10-08T17:00:00Z"), monotonic=0., calls=0, checkpoints=[], stopped=False)
    session = dict(actor="Atlas", symbols=["AAPL"], close_at=(clock.now + timedelta(hours=1)).isoformat())
    def checkpoint(state, due):
        clock.checkpoints.append((state, due))
        raise Checkpoint()
    renewal = SimpleNamespace(session=session, request={"role": "forecast-0"}, state={},
        stopped=lambda: clock.stopped, checkpoint=checkpoint)
    monkeypatch.setattr(split_model, "ReadySnapshots", lambda *a, **k: object())
    monkeypatch.setattr(split_model, "ShadowStore", lambda *a, **k: object())
    def pause(seconds):
        clock.monotonic += seconds
        clock.now += timedelta(seconds=seconds)
    clock.pause = pause
    return clock, renewal


def test_idle_model_lane_polls_without_empty_process_renewal_and_admits_promptly(tmp_path, monkeypatch):
    clock, renewal = idle_fixture(monkeypatch)
    def model_pass(**kwargs):
        clock.calls += 1
        return {"status": "PUBLISHED" if clock.calls == 8 else "WAITING_FOR_READY", "state": {"poll": clock.calls}}
    monkeypatch.setattr(split_model, "model_pass", model_pass)
    with pytest.raises(Checkpoint):
        split_model.renewal_model_pass(root=tmp_path, store=object(), queue=object(), renewal=renewal,
            config=object(), clock=lambda: clock.now, pause=clock.pause, monotonic=lambda: clock.monotonic)
    assert clock.calls == 8
    assert len(clock.checkpoints) == 1 and clock.checkpoints[0][0] == {"poll": 8}
    assert 7 <= clock.monotonic < 8


def test_idle_model_lane_keeps_bounded_generation_boundary(tmp_path, monkeypatch):
    clock, renewal = idle_fixture(monkeypatch)
    def model_pass(**kwargs):
        clock.calls += 1
        return {"status": "WAITING_FOR_READY", "state": {"poll": clock.calls}}
    monkeypatch.setattr(split_model, "model_pass", model_pass)
    with pytest.raises(Checkpoint):
        split_model.renewal_model_pass(root=tmp_path, store=object(), queue=object(), renewal=renewal,
            config=object(), clock=lambda: clock.now, pause=clock.pause, monotonic=lambda: clock.monotonic)
    assert 15 <= clock.monotonic < 15.11
    assert len(clock.checkpoints) == 1 and clock.calls <= 17


def test_idle_model_lane_manual_stop_and_original_close_are_responsive(tmp_path, monkeypatch):
    clock, renewal = idle_fixture(monkeypatch)
    monkeypatch.setattr(split_model, "model_pass", lambda **kwargs: {"status": "WAITING_FOR_READY", "state": {}})
    pause = clock.pause
    def stop(seconds):
        pause(seconds)
        if clock.monotonic >= .3:
            clock.stopped = True
    result = split_model.renewal_model_pass(root=tmp_path, store=object(), queue=object(), renewal=renewal,
        config=object(), clock=lambda: clock.now, pause=stop, monotonic=lambda: clock.monotonic)
    assert result["status"] == "STOPPED" and clock.monotonic < .41 and not clock.checkpoints
    clock.stopped = False
    renewal.session["close_at"] = (clock.now + timedelta(seconds=.3)).isoformat()
    result = split_model.renewal_model_pass(root=tmp_path, store=object(), queue=object(), renewal=renewal,
        config=object(), clock=lambda: clock.now, pause=pause, monotonic=lambda: clock.monotonic)
    assert result["status"] == "COMPLETED" and not clock.checkpoints


def test_setup_contention_is_bounded_and_persisted_in_original_handoff(tmp_path, monkeypatch):
    clock, renewal = idle_fixture(monkeypatch)
    def contended(*args, **kwargs):
        clock.calls += 1
        raise busy()
    monkeypatch.setattr(split_model, "ReadySnapshots", contended)
    monkeypatch.setattr(split_model, "model_pass", lambda **kwargs: pytest.fail("Setup contention admitted work"))
    with pytest.raises(Checkpoint):
        split_model.renewal_model_pass(root=tmp_path, store=object(), queue=object(), renewal=renewal,
            config=object(), clock=lambda: clock.now, pause=clock.pause, monotonic=lambda: clock.monotonic)
    assert 15 <= clock.monotonic < 15.11 and clock.calls <= 17
    evidence = clock.checkpoints[0][0]["admission_storage_deferral"]
    assert evidence["assigned_symbols"] == ["AAPL"]
    assert evidence["original_close_at"] == renewal.session["close_at"]
    assert evidence["sqlite_errorname"] == "SQLITE_BUSY"


def test_noncontention_setup_error_surfaces_without_renewal(tmp_path, monkeypatch):
    clock, renewal = idle_fixture(monkeypatch)
    def unavailable(*args, **kwargs):
        raise busy(sqlite3.SQLITE_IOERR)
    monkeypatch.setattr(split_model, "ReadySnapshots", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        split_model.renewal_model_pass(root=tmp_path, store=object(), queue=object(), renewal=renewal,
            config=object(), clock=lambda: clock.now, pause=clock.pause, monotonic=lambda: clock.monotonic)
    assert not clock.checkpoints and clock.monotonic == 0
