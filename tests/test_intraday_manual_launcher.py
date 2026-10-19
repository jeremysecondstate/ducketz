import json
from contextlib import contextmanager

import pytest

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.intraday import manual_launcher
from ml.intraday.prediction import instant


@pytest.mark.parametrize("started,wake", [
    ("2026-10-15T00:00:00Z", "2026-10-15T11:00:00Z"),
    ("2026-10-15T00:00:01Z", "2026-10-15T11:00:00Z"),
    ("2026-10-19T04:00:00Z", "2026-10-19T11:00:00Z"),
    ("2026-10-17T02:00:00Z", "2026-10-19T11:00:00Z"),
    ("2026-07-03T00:00:00Z", "2026-07-06T11:00:00Z"),
    ("2026-10-31T03:00:00Z", "2026-11-02T12:00:00Z"),
    ("2026-03-07T04:00:00Z", "2026-03-09T11:00:00Z"),
    ("2026-11-27T18:00:00Z", "2026-11-30T12:00:00Z"),
    ("2026-11-27T10:00:00Z", "2026-11-27T14:30:00Z"),
    ("2026-10-19T10:55:00Z", "2026-10-19T11:00:00Z"),
    ("2026-10-19T18:00:00Z", "2026-10-19T11:00:00Z"),
])
def test_wake_selects_unfinished_session_across_calendar_and_dst(started, wake):
    assert manual_launcher.selected_wake_at(started) == instant(wake)


def test_evening_wait_excludes_duplicate_and_direct_engine_then_delegates_once(tmp_path):
    now = [instant("2026-10-19T04:00:00Z")]
    wake = instant("2026-10-19T11:00:00Z")
    calls, waits = [], []
    argv = ["--live", "--symbol-workers", "2"]
    read = lambda: json.loads((tmp_path / "intraday/manual-launcher.json").read_text())

    def delegate(received):
        assert received == argv and now[0] == wake
        assert read()["status"] == "DELEGATING"
        assert (tmp_path / "locks/intraday-manual-launcher.lock").is_file()
        # Only the engine lock is released; the engine can now acquire it.
        with exclusive_runtime_lock(tmp_path / "locks/intraday-manual-session.lock", process_name="fixture-engine"):
            calls.append("engine")
        now[0] = instant("2026-10-20T01:00:00Z")
        return 0

    def sleep(seconds):
        assert calls == [] and 0 < seconds <= 60
        status = read()
        assert status["status"] == "WAITING"
        assert instant(status["wake_at"]) == wake
        assert status["automatic_stop_at"] == "2026-10-20T00:00:00+00:00"
        before = (tmp_path / "intraday/manual-launcher.json").read_bytes()
        with pytest.raises(RuntimeError, match="Another intraday-manual-launcher"):
            manual_launcher.run_scout(argv, datastore=tmp_path, clock=lambda: now[0], delegate=delegate)
        assert (tmp_path / "intraday/manual-launcher.json").read_bytes() == before
        with pytest.raises(RuntimeError, match="Another fixture-engine"):
            with exclusive_runtime_lock(tmp_path / "locks/intraday-manual-session.lock", process_name="fixture-engine"):
                pytest.fail("direct engine obtained waiting runtime lock")
        waits.append(now[0])
        now[0] = instant("2026-10-19T07:00:00Z") if len(waits) == 1 else wake

    assert manual_launcher.run_scout(argv, datastore=tmp_path, clock=lambda: now[0], sleep=sleep, delegate=delegate) == 0
    assert len(waits) == 2 and calls == ["engine"]
    assert read()["status"] == "FINISHED" and read()["automatic_daily_restart"] is False
    assert not (tmp_path / "locks/intraday-manual-launcher.lock").exists()
    assert not (tmp_path / "locks/intraday-manual-session.lock").exists()


def test_ctrl_c_cancels_evening_invocation_and_releases_locks(tmp_path):
    def stop(seconds):
        raise KeyboardInterrupt()
    result = manual_launcher.run_scout(["--live"], datastore=tmp_path,
        clock=lambda: "2026-10-19T04:00:00Z", sleep=stop,
        delegate=lambda argv: pytest.fail("cancelled invocation started engine"))
    assert result == 0
    status = json.loads((tmp_path / "intraday/manual-launcher.json").read_text())
    assert status["status"] == "STOPPED" and status["stop_reason"] == "manual_stop"
    assert not (tmp_path / "locks/intraday-manual-session.lock").exists()
    assert not (tmp_path / "locks/intraday-manual-launcher.lock").exists()


def test_resume_after_selected_stop_never_rolls_forward_or_delegates(tmp_path):
    now = [instant("2026-10-19T04:00:00Z")]
    result = manual_launcher.run_scout(["--live"], datastore=tmp_path, clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, instant("2026-10-20T12:00:00Z")),
        delegate=lambda argv: pytest.fail("elapsed invocation started engine"))
    assert result == 0
    status = json.loads((tmp_path / "intraday/manual-launcher.json").read_text())
    assert status["stop_reason"] == "selected_session_elapsed"
    assert status["automatic_stop_at"] == "2026-10-20T00:00:00+00:00"


def test_suspend_during_lock_handoff_does_not_start_another_day(tmp_path, monkeypatch):
    now = [instant("2026-10-19T11:00:00Z")]

    @contextmanager
    def lock(path, **options):
        with exclusive_runtime_lock(path, **options):
            yield
        if path.name == "intraday-manual-session.lock":
            now[0] = instant("2026-10-20T12:00:00Z")

    monkeypatch.setattr(manual_launcher, "exclusive_runtime_lock", lock)
    assert manual_launcher.run_scout(["--live"], datastore=tmp_path, clock=lambda: now[0],
        delegate=lambda argv: pytest.fail("handoff started a later day")) == 0
    status = json.loads((tmp_path / "intraday/manual-launcher.json").read_text())
    assert status["stop_reason"] == "selected_session_elapsed"


def test_daytime_delegates_immediately_and_retains_engine_failure(tmp_path):
    calls = []
    result = manual_launcher.run_scout(["--live"], datastore=tmp_path,
        clock=lambda: "2026-10-19T18:00:00Z", sleep=lambda seconds: pytest.fail("daytime launch slept"),
        delegate=lambda argv: calls.append(argv) or 1)
    assert result == 1 and calls == [["--live"]]
    status = json.loads((tmp_path / "intraday/manual-launcher.json").read_text())
    assert status["engine_exit_code"] == 1


@pytest.mark.parametrize("live,pc", [(False, "Scout"), (True, "Atlas")])
def test_diagnostics_and_atlas_delegate_unchanged_without_launcher_files(tmp_path, monkeypatch, live, pc):
    argv = ["--datastore", str(tmp_path), "--publication-offset-seconds", "45"] + (["--live"] if live else [])
    monkeypatch.setattr(manual_launcher, "local_symbols", lambda *args: (pc, ("FIXTURE",)))
    monkeypatch.setattr(manual_launcher, "run_scout", lambda *args, **kwargs: pytest.fail("delegated path waited"))
    calls = []
    assert manual_launcher.main(argv, delegate=lambda received: calls.append(received) or 2) == 2
    assert calls == [argv] and not list(tmp_path.iterdir())


def test_scout_main_validates_scope_and_preserves_engine_arguments(tmp_path, monkeypatch):
    argv = ["--live", "--datastore", str(tmp_path), "--scout-admission", "broker-local", "--lookback-days", "20"]
    monkeypatch.setattr(manual_launcher, "local_symbols", lambda *args: ("Scout", ("FIXTURE",)))
    calls = []
    assert manual_launcher.main(argv, clock=lambda: "2026-10-19T18:00:00Z",
        delegate=lambda received: calls.append(received) or 0) == 0
    assert calls == [argv]


def test_default_delegate_blocks_suspend_before_engine_first_clock_read(tmp_path, monkeypatch):
    from ml.intraday import live_session
    now = [instant("2026-10-19T11:00:00Z")]
    monkeypatch.setattr(manual_launcher, "local_symbols", lambda *args: ("Scout", ("FIXTURE",)))
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Scout", ("FIXTURE",)))
    original = live_session.run_manual

    def suspended_before_start(**options):
        now[0] = instant("2026-10-20T12:00:00Z")
        return original(**options)

    monkeypatch.setattr(live_session, "run_manual", suspended_before_start)
    monkeypatch.setattr(live_session, "ShadowStore", lambda *args: pytest.fail("elapsed startup created engine state"))
    assert manual_launcher.main(["--live", "--datastore", str(tmp_path)], clock=lambda: now[0]) == 0
    status = json.loads((tmp_path / "intraday/manual-launcher.json").read_text())
    assert status["status"] == "STOPPED" and status["stop_reason"] == "selected_session_elapsed"
    assert not (tmp_path / "intraday/manual-session.json").exists()


def test_default_delegate_preserves_actual_clock_and_existing_runtime_arguments(tmp_path, monkeypatch):
    from ml.intraday import live_session
    now = [instant("2026-10-19T11:00:00Z")]
    monkeypatch.setattr(manual_launcher, "local_symbols", lambda *args: ("Scout", ("FIXTURE",)))
    calls = []

    def engine(**options):
        calls.append(options)
        assert options["clock"]() == now[0]
        now[0] = instant("2026-10-20T00:00:01Z")
        # After its valid first read, the unchanged engine receives actual late
        # time so its normal 17:00 shutdown logic, not the startup fence, owns it.
        assert options["clock"]() == now[0]
        return {"status": "STOPPED"}

    monkeypatch.setattr(live_session, "run_manual", engine)
    argv = ["--live", "--repository", str(tmp_path), "--datastore", str(tmp_path),
        "--publication-offset-seconds", "45", "--symbol-workers", "2", "--lookback-days", "20"]
    assert manual_launcher.main(argv, clock=lambda: now[0]) == 0
    assert len(calls) == 1
    config = calls[0]["config"]
    assert (config.publication_offset_seconds, config.symbol_workers, config.lookback_days) == (45, 2, 20)
    assert config.use_supervised_models is True
    assert calls[0]["profile"] == tmp_path / "scratch/cross-pc/local-profile.json"
    assert calls[0]["watchlist"] == tmp_path / "datafetching/watchlist.local.txt"
    assert calls[0]["scout_admission"] == "broker-local"
