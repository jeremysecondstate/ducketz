"""Offline evidence checks for the standalone local forecast helpers."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import ctypes
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from ml.intraday import local_forecast
from ml.intraday.prediction import ContractError, TARGET_VERSION, digest, encoded, instant
from ml.intraday.store import ShadowStore
from ml.intraday.submission_time import SubmissionGate, submission_gate


def rehashed(forecast, **changes):
    value = {key: item for key, item in forecast.items() if key != "forecast_id"}
    value.update(changes)
    value["forecast_id"] = digest(value)
    return value


def make_case(tmp_path, *, reference="2026-10-08T17:00:00Z"):
    ref = instant(reference)
    store = ShadowStore(tmp_path / "forecast.sqlite3")
    session = "manual-scout-local-forecast-test"
    store.begin_cycle("original", dict(pc="Scout", mode="live", experiment=session,
        started_at=(ref + timedelta(minutes=15)).isoformat(), reference_at=ref.isoformat(),
        symbols=["DOCU"], configuration_hash="c" * 64))
    snapshot, _ = store.freeze_inputs("original", "DOCU", {"input": "immutable"})
    forecast = store.save_forecast(dict(pc="Scout", symbol="DOCU", cycle_id="original",
        reference_at=ref.isoformat(), reference_open="100",
        target_open=(ref + timedelta(minutes=30)).isoformat(),
        target_close=(ref + timedelta(minutes=45)).isoformat(),
        issued_at=(ref + timedelta(minutes=16, seconds=2)).isoformat(),
        information_available_at=(ref + timedelta(minutes=15, seconds=3)).isoformat(),
        data_through=(ref + timedelta(minutes=15)).isoformat(), target_version=TARGET_VERSION,
        snapshot_hash=snapshot, mode="live", availability_basis="observed",
        probability_not_down=.72, configured_count=2, successful_count=2,
        ensemble_status="full", models=[dict(name=name, status="success",
            probability_not_down=.72) for name in ("a", "b")]))
    return SimpleNamespace(store=store, forecast=forecast, session=session,
        started=ref + timedelta(seconds=1), now=ref + timedelta(minutes=16, seconds=10))


def read_saved(case, **changes):
    arguments = dict(session_id=case.session, session_started=case.started)
    arguments.update(changes)
    return local_forecast.saved_forecast(case.store.path, case.forecast, **arguments)


def classify(case, forecast=None, **changes):
    arguments = dict(now=case.now, session_started_at=case.started, local_symbols={"DOCU"})
    arguments.update(changes)
    return local_forecast.forecast_disposition(forecast or case.forecast, **arguments)


def save_candidate(case, *, pc="Scout", mode="live", session=None, symbol="DOCU",
                   reference_offset=0, newer_reference=False, complete=True):
    original = case.forecast
    reference = instant(original["reference_at"]) + timedelta(minutes=15 if newer_reference else 0)
    issued = max(instant(original["issued_at"]) + timedelta(seconds=2),
                 reference + timedelta(minutes=16))
    offset = timezone(timedelta(hours=reference_offset))
    reference_text = reference.astimezone(offset).isoformat()
    cycle = "candidate"
    case.store.begin_cycle(cycle, dict(pc=pc, mode=mode,
        experiment=session or case.session, started_at=issued.isoformat(),
        reference_at=reference_text, symbols=[symbol], configuration_hash="c" * 64))
    snapshot, _ = case.store.freeze_inputs(cycle, symbol, {"input": "candidate"})
    value = {key: item for key, item in original.items() if key != "forecast_id"}
    value.update(pc=pc, mode=mode, symbol=symbol, cycle_id=cycle,
        reference_at=reference_text, issued_at=issued.astimezone(offset).isoformat(),
        target_open=(reference + timedelta(minutes=30)).isoformat(),
        target_close=(reference + timedelta(minutes=45)).isoformat(),
        data_through=(reference + timedelta(minutes=15)).isoformat(),
        information_available_at=(reference + timedelta(minutes=15)).isoformat(),
        snapshot_hash=snapshot)
    if not complete:
        value.update(successful_count=1, ensemble_status="incomplete",
            models=[dict(name="a", status="success", probability_not_down=.72),
                    dict(name="b", status="failed")])
    return case.store.save_forecast(value)


@pytest.mark.parametrize("session,cutoff", [
    (date(2026, 10, 8), "2026-10-08T23:45:00Z"),
    (date(2026, 11, 5), "2026-11-06T00:45:00Z"),
])
def test_submission_cutoff_is_exact_in_pacific_on_both_sides_of_dst(session, cutoff):
    edge = instant(cutoff)
    before = submission_gate(as_of=edge - timedelta(microseconds=1), eligible_session=session)
    at = submission_gate(as_of=edge, eligible_session=session)
    after = submission_gate(as_of=edge + timedelta(microseconds=1), eligible_session=session)
    assert isinstance(before, SubmissionGate)
    assert before.state == "before_submission_cutoff"
    assert at.state == after.state == "submission_cutoff"
    assert instant(before.cutoff_at) == edge
    assert before.eligible_session == session.isoformat()
    assert instant(before.checked_at) == edge - timedelta(microseconds=1)
    with pytest.raises(FrozenInstanceError):
        before.state = "submission_cutoff"


def test_submission_date_uses_pacific_calendar_and_requires_an_explicit_date():
    # UTC has rolled into Friday; the Pacific session is still Thursday.
    assert submission_gate(as_of="2026-11-06T00:00:00Z",
                           eligible_session=date(2026, 11, 5)).state == "before_submission_cutoff"
    assert submission_gate(as_of="2026-11-06T00:00:00Z",
                           eligible_session=date(2026, 11, 6)).state == "wrong_session_date"
    for supplied in ("2026-11-05", None, datetime(2026, 11, 5, tzinfo=timezone.utc)):
        with pytest.raises(ContractError):
            submission_gate(as_of="2026-11-06T00:00:00Z", eligible_session=supplied)


def test_disposition_preserves_original_forecast_and_only_classifies_eligibility(tmp_path):
    case = make_case(tmp_path)
    before = encoded(case.forecast)
    assert classify(case) == "eligible_for_account_checks"
    assert encoded(case.forecast) == before


@pytest.mark.parametrize("changes,expected", [
    ({"pc": "Atlas"}, "outside_local_scout_scope"),
    ({"symbol": "DBX"}, "outside_local_scout_scope"),
    ({"mode": "shadow"}, "not_a_current_live_forecast"),
    ({"availability_basis": "historical"}, "not_a_current_live_forecast"),
    ({"ensemble_status": "incomplete"}, "incomplete_ensemble"),
    ({"successful_count": 1}, "incomplete_ensemble"),
])
def test_disposition_retains_local_live_and_complete_ensemble_boundaries(tmp_path, changes, expected):
    case = make_case(tmp_path)
    assert classify(case, rehashed(case.forecast, **changes)) == expected


def test_disposition_rejects_forged_hash_wrong_target_and_future_issue(tmp_path):
    case = make_case(tmp_path)
    for forecast in ({**case.forecast, "symbol": "DBX"},
                     rehashed(case.forecast, target_version="different-target")):
        with pytest.raises(ContractError, match="Immutable exact-target"):
            classify(case, forecast)
    with pytest.raises(ContractError, match="future"):
        classify(case, now=instant(case.forecast["issued_at"]) - timedelta(microseconds=1))


def test_disposition_retains_manual_start_target_and_cutoff_boundaries(tmp_path):
    case = make_case(tmp_path)
    assert classify(case, session_started_at=case.now) == "forecast_precedes_manual_start"
    assert classify(case, now=instant(case.forecast["target_close"])) == "fixed_target_already_elapsed"
    assert classify(case, now=case.now + timedelta(days=1)) == "wrong_session_date"
    late = rehashed(case.forecast, reference_at="2026-10-08T23:15:00Z",
        issued_at="2026-10-08T23:31:00Z", target_close="2026-10-09T00:00:00Z")
    assert classify(case, late, now="2026-10-08T23:45:00Z") == "submission_cutoff"


def test_saved_reader_uses_exact_immutable_evidence_without_constructing_or_writing_store(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    before = case.store.path.read_bytes()
    original_cycle = local_forecast._cycle
    query_only = []

    def audited_cycle(db, cycle_id):
        query_only.append(db.execute("PRAGMA query_only").fetchone()[0])
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM forecasts")
        return original_cycle(db, cycle_id)

    monkeypatch.setattr(local_forecast, "_cycle", audited_cycle)
    monkeypatch.setattr(ShadowStore, "__init__", lambda *a, **kw: pytest.fail("Reader constructed a store"))
    assert read_saved(case) is None
    assert query_only == [1]
    assert case.store.path.read_bytes() == before


@pytest.mark.parametrize("reference_offset,newer_reference", [(-7, False), (9, False), (-7, True), (9, True)])
def test_saved_newer_current_session_forecast_supersedes_across_timestamp_offsets(tmp_path, reference_offset, newer_reference):
    case = make_case(tmp_path)
    save_candidate(case, reference_offset=reference_offset, newer_reference=newer_reference)
    with pytest.raises(ContractError, match="supersedes"):
        read_saved(case)


@pytest.mark.parametrize("changes", [
    {"pc": "Atlas"}, {"mode": "shadow"}, {"session": "another-manual-session"},
    {"symbol": "DBX"}, {"complete": False},
])
def test_foreign_or_incomplete_candidates_do_not_supersede_current_local_forecast(tmp_path, changes):
    case = make_case(tmp_path)
    save_candidate(case, **changes)
    assert read_saved(case) is None


@pytest.mark.parametrize("changes", [
    {"session_id": "another-manual-session"},
    {"session_started": instant("2026-10-08T17:15:01Z")},
])
def test_saved_reader_rejects_another_manual_session_or_cycle_before_start(tmp_path, changes):
    case = make_case(tmp_path)
    with pytest.raises(ContractError, match="manual Scout session"):
        read_saved(case, **changes)


@pytest.mark.parametrize("statement", [
    "UPDATE forecasts SET symbol='DBX'",
    "UPDATE forecasts SET payload='{}'",
    "UPDATE forecasts SET payload='{'",
    "DELETE FROM forecasts",
    "UPDATE cycles SET pc='Atlas'",
    "UPDATE cycles SET payload='[]'",
    "UPDATE cycles SET payload='{'",
    "UPDATE cycles SET started_at='2026-10-08T17:15:01Z'",
    "DELETE FROM cycles",
    "UPDATE snapshots SET payload='{}'",
    "UPDATE snapshots SET payload='{'",
    "DELETE FROM symbol_inputs",
])
def test_corrupted_or_missing_indexed_evidence_fails_without_repairing_bytes(tmp_path, statement):
    case = make_case(tmp_path)
    with case.store.connect() as db:
        db.execute(statement)
    before = case.store.path.read_bytes()
    with pytest.raises(ContractError):
        read_saved(case)
    assert case.store.path.read_bytes() == before


def test_rehashed_unsaved_forecast_cannot_replace_persisted_evidence(tmp_path):
    case = make_case(tmp_path)
    case.forecast = rehashed(case.forecast, probability_not_down=.71)
    with pytest.raises(ContractError, match="not saved"):
        read_saved(case)


@pytest.mark.parametrize("contents", [None, b"not a sqlite database", b""])
def test_missing_or_malformed_database_is_not_created_repaired_or_exposed(tmp_path, contents):
    case = make_case(tmp_path / "source")
    target = tmp_path / "private-forecast-evidence.sqlite3"
    if contents is not None:
        target.write_bytes(contents)
    with pytest.raises(ContractError) as error:
        local_forecast.saved_forecast(target, case.forecast,
            session_id=case.session, session_started=case.started)
    assert str(target) not in str(error.value)
    assert target.exists() == (contents is not None)
    if contents is not None:
        assert target.read_bytes() == contents


def test_exclusive_database_lock_fails_immediately_with_read_only_zero_timeout(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    real_connect = sqlite3.connect
    seen = []

    def audited_connect(*args, **kwargs):
        seen.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(local_forecast.sqlite3, "connect", audited_connect)
    with real_connect(case.store.path) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(ContractError):
            read_saved(case)
    assert len(seen) == 1
    assert seen[0][0][0].endswith("?mode=ro")
    assert seen[0][1] == dict(uri=True, timeout=0)


@pytest.mark.parametrize("forecast", [{}, None, {"forecast_id": []}])
def test_malformed_forecast_inputs_raise_contract_errors_without_exposing_paths(tmp_path, forecast):
    case = make_case(tmp_path)
    with pytest.raises(ContractError) as error:
        local_forecast.saved_forecast(case.store.path, forecast,
            session_id=case.session, session_started=case.started)
    assert str(case.store.path) not in str(error.value)


def test_out_of_range_session_timestamp_fails_without_mutating_saved_evidence(tmp_path):
    case = make_case(tmp_path)
    before = case.store.path.read_bytes()
    with pytest.raises(ContractError) as error:
        read_saved(case, session_started="0001-01-01T00:00:00+14:00")
    assert str(case.store.path) not in str(error.value)
    assert case.store.path.read_bytes() == before


@pytest.mark.parametrize("path", ["relative.sqlite3", "//server/share/private.sqlite3", "\\\\server\\share\\private.sqlite3"])
def test_unsafe_evidence_paths_are_rejected_before_sqlite_connect(tmp_path, monkeypatch, path):
    case = make_case(tmp_path)
    monkeypatch.setattr(local_forecast.sqlite3, "connect", lambda *a, **kw: pytest.fail("Unsafe path opened"))
    with pytest.raises(ContractError):
        local_forecast.saved_forecast(path, case.forecast,
            session_id=case.session, session_started=case.started)


@pytest.mark.parametrize("relative", ["..", "../escape.sqlite3"])
def test_parent_traversal_is_rejected_even_under_an_absolute_root(tmp_path, relative):
    with pytest.raises(ContractError):
        local_forecast._safe_path(tmp_path / relative)


@pytest.mark.parametrize("blocked,kind", [
    ("file", "symlink"), ("parent", "symlink"), ("-journal", "symlink"),
    ("-wal", "reparse"), ("-shm", "reparse"),
    ("file", "hardlink"), ("file", "directory"),
])
def test_redirected_ancestors_sidecars_and_nonstandalone_files_fail_without_symlink_privileges(tmp_path, monkeypatch, blocked, kind):
    target = tmp_path / "forecast.sqlite3"
    obstacle = target if blocked == "file" else target.parent if blocked == "parent" else Path(str(target) + blocked)
    real_lstat = Path.lstat
    info = SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else
        stat.S_IFDIR if kind == "directory" else stat.S_IFREG,
        st_nlink=2 if kind == "hardlink" else 1,
        st_file_attributes=0x400 if kind == "reparse" else 0)

    def observed_lstat(path, *args, **kwargs):
        return info if path == obstacle else real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", observed_lstat)
    with pytest.raises(ContractError):
        local_forecast._safe_path(target)


def test_windows_drive_and_alternate_stream_guards(tmp_path, monkeypatch):
    if os.name == "nt":
        with pytest.raises(ContractError):
            local_forecast._safe_path(tmp_path / "forecast.sqlite3:hidden")
        observed = []
        def network_drive(anchor):
            observed.append(anchor)
            return 4
        monkeypatch.setattr(ctypes.windll.kernel32, "GetDriveTypeW", network_drive)
        with pytest.raises(ContractError):
            local_forecast._safe_path(tmp_path / "forecast.sqlite3")
        assert observed == [str(tmp_path.anchor)]
