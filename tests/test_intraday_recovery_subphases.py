"""Offline integration proof for bounded own-journal recovery diagnostics."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import dataset_diagnostics as diagnostics
from ml.intraday import scout_journal as journals
from ml.intraday import scout_local_execution as execution
from ml.intraday.scout_journal import JournalCorruptionError, JournalStateError
from test_intraday_scout_journal import journal
from test_intraday_scout_local_execution import env, forecast, raw_order, record


def groups(profile):
    return {row["group"]: row for row in profile["recovery_subphases"]["groups"]}


def test_many_original_journals_keep_all_subphases_and_existing_phase_slots(env, monkeypatch):
    assert env.executor.step(env.forecast)["disposition"] == "submitted"
    books = env.executor.books
    for number in range(45):
        books.create_session("offline-history-" + str(number)).close()
    original_sessions = books.sessions()
    original_inventory = books.path.read_bytes()
    broker_reads = len(env.broker.reads)
    loader_calls = []
    for name in ("load_initial_gate_clearance", "load_pretransport_clearance", "load_clearance", "load_client_gate_clearance"):
        def load(*args, _name=name, **kwargs):
            loader_calls.append(_name)
            return None
        monkeypatch.setattr(execution, name, load)

    with diagnostics.capture_dataset_build() as profile:
        with diagnostics.dataset_phase("total_step"):
            with diagnostics.dataset_phase("own_journal_recovery"):
                for session in original_sessions:
                    with books.open_session(session) as original:
                        for _ in range(8):
                            original.records()
                own = env.executor.journal
                env.executor._recover_record(own, record(env))
                assert env.executor._commitment_clearance(own, record(env)) is None
            with diagnostics.dataset_phase("capacity"):
                pass
            with diagnostics.dataset_phase("submission"):
                pass

    observed = groups(profile)
    assert set(observed) == {
        "inventory_validation", "journal_constructor", "journal_safe_path", "journal_connect",
        "journal_connection_setup", "journal_begin_immediate", "journal_schema_validation",
        "journal_full_audit", "journal_commit", "journal_close", "journal_records",
        "journal_record_recovery", "clearance_initial_gate", "clearance_pretransport",
        "clearance_commitment", "broker_status",
    }
    assert observed["journal_begin_immediate"]["count"] > 336
    assert observed["journal_constructor"]["count"] == 46
    assert observed["journal_records"]["count"] == 46 * 8
    assert profile["recovery_subphases"]["overflow_count"] == 0
    assert profile["recovery_subphases"]["totals_are_inclusive"] is True
    assert [row["phase"] for row in profile["phases"]] == [
        "own_journal_recovery", "capacity", "submission", "total_step"]
    assert all(row["error_count"] == 0 and 1 <= len(row["slow_samples"]) <= 3
               for row in observed.values())
    assert all(sample["original_journal_id"] is not None
               for sample in observed["journal_constructor"]["slow_samples"])
    assert books.path.read_bytes() == original_inventory and books.sessions() == original_sessions
    assert len(env.broker.reads) == broker_reads + 1
    assert len(env.broker.posts) == 1 and env.broker.deletes == []
    assert loader_calls == ["load_initial_gate_clearance", "load_pretransport_clearance", "load_clearance", "load_client_gate_clearance"]
    serialized = json.dumps(profile)
    assert str(env.root) not in serialized and env.executor.account_fingerprint not in serialized
    assert all(key not in serialized for key in ("send_token", "broker_order_id", "request_id", "payload"))


@pytest.mark.parametrize("winner", [0, 1, 2, 3, None])
def test_clearance_shortcircuit_retains_original_loader_and_clock_count(env, monkeypatch, winner):
    calls, clock_reads = [], []
    answer = {"original": "fixture-proof"}
    names = ("load_initial_gate_clearance", "load_pretransport_clearance", "load_clearance", "load_client_gate_clearance")
    def clock():
        clock_reads.append(True)
        return env.state.now
    monkeypatch.setattr(env.executor, "clock", clock)
    for index, name in enumerate(names):
        def load(books, original, saved, *, as_of, _index=index):
            assert books is env.executor.books and original is env.executor.journal
            assert as_of == env.state.now
            calls.append(_index)
            return answer if _index == winner else None
        monkeypatch.setattr(execution, name, load)
    with diagnostics.capture_dataset_build() as profile:
        actual = env.executor._commitment_clearance(env.executor.journal, {"fixture": "original"})
    expected_count = 4 if winner is None else winner + 1
    assert actual is (None if winner is None else answer)
    assert calls == list(range(expected_count)) and len(clock_reads) == expected_count
    assert list(groups(profile)) == [
        "clearance_initial_gate", "clearance_pretransport", "clearance_commitment"][:min(expected_count, 3)]
    assert env.broker.posts == [] and env.broker.reads == [] and env.broker.deletes == []


@pytest.mark.parametrize("terminal", [False, True])
def test_recovery_only_queries_already_accepted_nonterminal_own_orders(env, terminal):
    env.executor.step(env.forecast)
    own = env.executor.journal
    if terminal:
        env.broker.orders = [raw_order(env, status="CANCELED")]
        env.executor._recover_record(own, record(env))
    before = record(env)
    env.broker.reads.clear()
    with diagnostics.capture_dataset_build() as profile:
        actual = env.executor._recover_record(own, before)
    assert actual == before
    assert len(env.broker.reads) == (0 if terminal else 1)
    assert ("broker_status" in groups(profile)) is not terminal
    assert groups(profile)["journal_record_recovery"]["count"] == 1
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


@pytest.mark.parametrize("post_error", [TimeoutError("lost offline reply"), SystemExit("offline interrupted reply")])
def test_unknown_retains_exact_original_record_and_permanent_one_send_fence(env, post_error):
    env.broker.post_error = post_error
    if isinstance(post_error, Exception):
        assert env.executor.step(env.forecast)["submission_status"] == "UNKNOWN"
    else:
        with pytest.raises(SystemExit) as caught:
            env.executor.step(env.forecast)
        assert caught.value is post_error
    original = record(env)
    reads = len(env.broker.reads)
    with diagnostics.capture_dataset_build() as profile:
        env.executor.recover()
    recovered = record(env)
    assert recovered["state"] == "UNKNOWN"
    assert dict(recovered, state=original["state"]) == original
    assert len(env.broker.reads) == reads and "broker_status" not in groups(profile)
    with diagnostics.capture_dataset_build() as repeated:
        env.executor.recover()
    assert record(env) == recovered and "broker_status" not in groups(repeated)
    with pytest.raises(JournalStateError):
        env.executor.journal.claim_send(original["request"]["request_id"])
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)["disposition"] == "submission_recovery_required"
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_constructor_audit_inherits_expected_original_identity_before_object_is_bound(env):
    books = env.executor.books
    item = books.inspect()["sessions"][env.executor.session_id]
    original = env.executor.journal
    before = hashlib.sha256(original.path.read_bytes()).hexdigest()
    with diagnostics.capture_dataset_build() as profile:
        with books._open_item(item) as reopened:
            assert reopened.journal_id == original.journal_id
    for name in ("journal_constructor", "journal_safe_path", "journal_full_audit", "journal_connect"):
        assert all(row["original_journal_id"] == item["journal_id"]
                   for row in groups(profile)[name]["slow_samples"])
    wrong = dict(item, journal_id="f" * 32)
    with diagnostics.capture_dataset_build() as failed:
        with pytest.raises(JournalCorruptionError, match="expected original identity"):
            books._open_item(wrong)
    assert groups(failed)["journal_constructor"]["error_count"] == 1
    assert all(row["original_journal_id"] == wrong["journal_id"]
               for row in groups(failed)["journal_full_audit"]["slow_samples"])
    assert hashlib.sha256(original.path.read_bytes()).hexdigest() == before


class FakeConnection:
    def __init__(self, ticks, *, rollback_error=None):
        self.ticks, self.rollback_error = ticks, rollback_error
        self.trace = []
        self.in_transaction = False

    def execute(self, sql):
        self.trace.append(sql)
        if sql == "BEGIN IMMEDIATE":
            self.ticks[0] += 5.0
            self.ticks[2] += 2.0
            self.in_transaction = True
        if sql == "PRAGMA synchronous":
            return SimpleNamespace(fetchone=lambda: (3,))
        if sql == "PRAGMA journal_mode":
            return SimpleNamespace(fetchone=lambda: ("delete",))
        if sql.startswith("SELECT type, name, sql"):
            return [("table", name, definition) for name, definition in journals._SCHEMA.items()]
        return None

    def commit(self):
        self.trace.append("commit")
        self.in_transaction = False

    def rollback(self):
        self.trace.append("rollback")
        if self.rollback_error:
            raise self.rollback_error
        self.in_transaction = False

    def close(self):
        self.trace.append("close")


def fake_transaction(tmp_path, monkeypatch, *, rollback_error=None):
    original = journal(tmp_path)
    ticks = [0.0, 0.0, 0.0]
    db = FakeConnection(ticks, rollback_error=rollback_error)
    calls = []
    def connect(path, **kwargs):
        calls.append((path, kwargs))
        return db
    monkeypatch.setattr(journals, "sqlite3", SimpleNamespace(connect=connect, Error=sqlite3.Error))
    monkeypatch.setattr(diagnostics, "_clocks", lambda: tuple(ticks))
    return original, db, ticks, calls


def assert_original_connection_setup(original, db, calls):
    assert calls == [(original.path.as_uri() + "?mode=rw",
                      {"uri": True, "timeout": 30, "isolation_level": None})]
    assert db.trace[:7] == [
        "PRAGMA foreign_keys = ON", "PRAGMA trusted_schema = OFF", "PRAGMA synchronous = EXTRA",
        "PRAGMA synchronous", "PRAGMA journal_mode", "BEGIN IMMEDIATE",
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"]


def test_delayed_begin_measures_only_existing_acquire_and_keeps_setup_commit_close(tmp_path, monkeypatch):
    original, db, ticks, calls = fake_transaction(tmp_path, monkeypatch)
    with diagnostics.capture_dataset_build() as profile:
        with original._transaction() as active:
            assert active is db
            db.trace.append("body")
    assert_original_connection_setup(original, db, calls)
    assert db.trace[7:] == ["body", "commit", "close"]
    phase = groups(profile)["journal_begin_immediate"]
    assert phase["wall_seconds"] == 5.0 and phase["thread_cpu_seconds"] == 0.0
    assert phase["process_cpu_seconds"] == 2.0
    assert phase["slow_samples"][0]["original_journal_id"] == original.journal_id


@pytest.mark.parametrize("body_error", [ValueError("offline original"), KeyboardInterrupt("offline original"),
                                        sqlite3.OperationalError("offline sqlite error")])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_transaction_preserves_original_error_and_preexisting_rollback_failure_semantics(
        tmp_path, monkeypatch, body_error, rollback_fails):
    rollback_error = RuntimeError("offline rollback failed") if rollback_fails else None
    original, db, _, calls = fake_transaction(tmp_path, monkeypatch, rollback_error=rollback_error)
    with diagnostics.capture_dataset_build() as profile:
        with pytest.raises(BaseException) as caught:
            with original._transaction():
                db.trace.append("body")
                raise body_error
    assert_original_connection_setup(original, db, calls)
    assert db.trace[7:] == ["body", "rollback", "close"]
    if rollback_error is not None:
        assert caught.value is rollback_error
    elif isinstance(body_error, sqlite3.Error):
        assert isinstance(caught.value, JournalCorruptionError) and caught.value.__cause__ is body_error
    else:
        assert caught.value is body_error
    assert "journal_commit" not in groups(profile) and groups(profile)["journal_close"]["count"] == 1


@pytest.mark.parametrize("body_error", [ValueError("offline original"), KeyboardInterrupt("offline original")])
@pytest.mark.parametrize("observer_stage", ["entry", "finish"])
def test_close_observer_interrupt_never_masks_pending_original_or_skips_close(
        tmp_path, monkeypatch, body_error, observer_stage):
    original, db, ticks, _ = fake_transaction(tmp_path, monkeypatch)
    diagnostic_error = SystemExit("offline observer interruption")
    def clock():
        if db.trace and db.trace[-1] == ("rollback" if observer_stage == "entry" else "close"):
            raise diagnostic_error
        return tuple(ticks)
    monkeypatch.setattr(diagnostics, "_clocks", clock)
    with diagnostics.capture_dataset_build() as profile:
        with pytest.raises(type(body_error)) as caught:
            with original._transaction():
                raise body_error
    assert caught.value is body_error and db.trace[-2:] == ["rollback", "close"]
    assert "SystemExit" in profile["observer_errors"]


@pytest.mark.parametrize("original_error", [ValueError("offline status failure"), KeyboardInterrupt("offline status interrupted")])
def test_existing_status_failure_preserves_original_identity_and_private_record(env, monkeypatch, original_error):
    env.executor.step(env.forecast)
    original = record(env)
    calls = []
    def status(request_id):
        calls.append(request_id)
        raise original_error
    monkeypatch.setattr(env.executor, "_adapter", lambda *args: SimpleNamespace(status=status))
    with diagnostics.capture_dataset_build() as profile:
        with pytest.raises(type(original_error)) as caught:
            env.executor._recover_record(env.executor.journal, original)
    assert caught.value is original_error and record(env) == original
    assert calls == [original["request"]["request_id"]]
    for name in ("broker_status", "journal_record_recovery"):
        row = groups(profile)[name]
        assert row["count"] == row["error_count"] == 1
        assert row["slow_samples"][0]["error_type"] == type(original_error).__name__
    assert len(env.broker.posts) == 1 and env.broker.deletes == []
