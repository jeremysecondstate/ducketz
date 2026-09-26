"""Deterministic lifecycle checks for the public candle refresh worker."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from filelock import FileLock, Timeout
import pytest

from ml import hyperliquid_data_loop as loop


STEP_MS = 900_000
STEP = STEP_MS // 1000
BASE = 1_800_000_000  # A precise 15-minute UTC boundary.


class FakeClock:
    def __init__(self, now: float):
        self.now = now
        self.waits = []

    def __call__(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        assert seconds > 0, "The worker must not busy-spin."
        self.waits.append(seconds)
        self.now += seconds
        return False


def config(tmp_path: Path, **kwargs):
    return loop.LoopConfig(output_root=tmp_path, **kwargs)


def fresh_result(as_of_ms: int, *, behind: int = 0):
    boundary = as_of_ms // STEP_MS * STEP_MS - behind * STEP_MS
    return {
        "status": "published",
        "run_id": "example-run",
        "rows": 5000,
        "new_candles": 1,
        "last_close_utc": datetime.fromtimestamp(boundary / 1000, timezone.utc).isoformat(),
        "gap_count": 0,
        "missing_candle_count": 0,
    }


@pytest.mark.parametrize(
    ("offset", "expected_wake", "expected_ready_close"),
    [
        (0, 5, -STEP),
        (2, 5, -STEP),
        (4.999, 5, -STEP),
        (5, STEP + 5, 0),
        (200, STEP + 5, 0),
        (STEP + 2, STEP + 5, 0),
        (STEP + 5, 2 * STEP + 5, STEP),
    ],
)
def test_boundaries_include_publication_delay_without_expectation_of_future_candles(
    offset, expected_wake, expected_ready_close
):
    now = BASE + offset
    wake = loop.next_wake_time(now, STEP_MS, 5)
    expected_close = loop.expected_close_ms(int(now * 1000), STEP_MS, 5)
    assert wake == pytest.approx(BASE + expected_wake)
    assert wake > now
    assert expected_close == (BASE + expected_ready_close) * 1000
    assert expected_close <= (now - 5) * 1000


def test_startup_catches_up_immediately_then_aligns_with_next_closed_candle(tmp_path):
    settings = config(tmp_path)
    clock = FakeClock(BASE + 103)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return fresh_result(kwargs["as_of_ms"])

    state = loop.run_loop(
        settings, max_cycles=2, clock=clock, wait=clock.wait, pipeline_runner=runner
    )

    assert [call["as_of_ms"] for call in calls] == [
        (BASE + 103) * 1000, (BASE + STEP + 5) * 1000
    ]
    assert calls[0]["coin"] == "BTC"
    assert calls[0]["interval"] == "15m"
    assert calls[0]["output_root"] == tmp_path
    assert sum(clock.waits) == STEP + 5 - 103
    assert state["cycle_count"] == 2
    assert state["successful_cycles"] == 2
    assert state["failures"] == 0
    assert state["status"] == "stopped"
    dataset = tmp_path / "BTC" / "15m"
    persisted = json.loads((dataset / "loop_status.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "stopped"
    events = [json.loads(line) for line in (dataset / "loop_events.jsonl").read_text().splitlines()]
    assert events


def test_first_seconds_after_close_wait_for_delayed_publication_without_lag_retry(tmp_path):
    clock = FakeClock(BASE + 2)
    call_times = []

    def runner(**kwargs):
        call_times.append(clock())
        # At startup the previously published bar is still acceptable during
        # the five-second exchange publication grace period.
        return fresh_result(kwargs["as_of_ms"], behind=1 if len(call_times) == 1 else 0)

    loop.run_loop(config(tmp_path), max_cycles=2, clock=clock, wait=clock.wait, pipeline_runner=runner)
    assert call_times == [BASE + 2, BASE + 5]


def test_missing_latest_exchange_candle_retries_then_resumes_regular_schedule(tmp_path):
    clock = FakeClock(BASE + 5)
    call_times = []

    def runner(**kwargs):
        call_times.append(clock())
        return fresh_result(kwargs["as_of_ms"], behind=1 if len(call_times) < 3 else 0)

    loop.run_loop(config(tmp_path), max_cycles=4, clock=clock, wait=clock.wait, pipeline_runner=runner)
    assert call_times == [BASE + 5, BASE + 20, BASE + 50, BASE + STEP + 5]


def test_failed_fetch_does_not_end_loop_and_next_attempt_uses_current_time(tmp_path):
    clock = FakeClock(BASE + 5)
    call_times = []

    def runner(**kwargs):
        call_times.append(kwargs["as_of_ms"])
        if len(call_times) == 1:
            raise RuntimeError("temporary public endpoint outage")
        return fresh_result(kwargs["as_of_ms"])

    state = loop.run_loop(
        config(tmp_path), max_cycles=3, clock=clock, wait=clock.wait, pipeline_runner=runner
    )
    assert call_times == [(BASE + 5) * 1000, (BASE + 20) * 1000, (BASE + STEP + 5) * 1000]
    assert state["cycle_count"] == 3
    assert state["failures"] == 1
    assert state["successful_cycles"] == 2


def test_repeated_failures_have_capped_backoff_and_count_toward_max_cycles(tmp_path):
    clock = FakeClock(BASE + 5)
    call_times = []

    def runner(**kwargs):
        call_times.append(clock())
        raise RuntimeError("public endpoint still unavailable")

    state = loop.run_loop(
        config(tmp_path, retry_seconds=15, max_retry_seconds=30),
        max_cycles=4, clock=clock, wait=clock.wait, pipeline_runner=runner,
    )
    assert call_times == [BASE + 5, BASE + 20, BASE + 50, BASE + 80]
    assert state["cycle_count"] == 4
    assert state["failures"] == 4
    assert state["successful_cycles"] == 0
    assert state["status"] == "stopped"


def test_restart_after_downtime_catches_up_at_current_time_in_one_refresh(tmp_path):
    clock = FakeClock(BASE + 7 * STEP + 205)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return fresh_result(kwargs["as_of_ms"])

    loop.run_loop(config(tmp_path), max_cycles=1, clock=clock, wait=clock.wait, pipeline_runner=runner)
    assert len(calls) == 1
    assert calls[0]["as_of_ms"] == int(clock() * 1000)
    assert clock.waits == []


def test_stop_event_already_set_does_not_fetch(tmp_path):
    event = threading.Event()
    event.set()

    def forbidden(**kwargs):
        pytest.fail("A stopped worker must not begin another refresh.")

    state = loop.run_loop(config(tmp_path), stop_event=event, pipeline_runner=forbidden)
    assert state["status"] == "stopped"
    assert state["cycle_count"] == 0


def test_stop_requested_while_waiting_exits_without_next_fetch(tmp_path):
    clock = FakeClock(BASE + 5)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return fresh_result(kwargs["as_of_ms"])

    state = loop.run_loop(
        config(tmp_path), clock=clock, wait=lambda seconds: True, pipeline_runner=runner
    )
    assert len(calls) == 1
    assert state["status"] == "stopped"


def test_stop_marker_finishes_current_refresh_and_releases_singleton_lock(tmp_path):
    settings = config(tmp_path)
    clock = FakeClock(BASE + 5)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        loop.request_stop(settings)
        return fresh_result(kwargs["as_of_ms"])

    state = loop.run_loop(settings, clock=clock, wait=clock.wait, pipeline_runner=runner)
    assert len(calls) == 1
    assert state["status"] == "stopped"
    directory = tmp_path / "BTC" / "15m"
    assert not (directory / "stop.request").exists()
    with FileLock(str(directory / ".loop.lock"), timeout=0):
        pass


def test_second_worker_cannot_start_for_same_symbol_and_interval(tmp_path):
    directory = tmp_path / "BTC" / "15m"
    directory.mkdir(parents=True)

    def forbidden(**kwargs):
        pytest.fail("A competing worker must never start a refresh.")

    with FileLock(str(directory / ".loop.lock"), timeout=0):
        with pytest.raises(Timeout):
            loop.run_loop(config(tmp_path), max_cycles=1, pipeline_runner=forbidden)


@pytest.mark.parametrize("field", ["close_delay_seconds", "retry_seconds", "max_retry_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_timing_cannot_create_an_unschedulable_worker(tmp_path, field, value):
    with pytest.raises(ValueError, match="finite"):
        config(tmp_path, **{field: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"close_delay_seconds": -1},
        {"close_delay_seconds": STEP},
        {"close_delay_seconds": STEP + 1},
        {"retry_seconds": 0},
        {"retry_seconds": -1},
        {"retry_seconds": 15, "max_retry_seconds": 14},
        {"max_retry_seconds": 0},
        {"repair_every_cycles": -1},
        {"repair_every_cycles": 1.5},
    ],
)
def test_invalid_timing_and_repair_settings_are_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        config(tmp_path, **kwargs)


def test_zero_publication_delay_is_supported(tmp_path):
    settings = config(tmp_path, close_delay_seconds=0)
    assert loop.expected_close_ms(BASE * 1000, STEP_MS, settings.close_delay_seconds) == BASE * 1000
    assert loop.next_wake_time(BASE, STEP_MS, settings.close_delay_seconds) == BASE + STEP


def test_refresh_crossing_a_candle_boundary_catches_up_before_next_interval(tmp_path):
    clock = FakeClock(BASE + STEP - 1)
    call_times = []

    def runner(**kwargs):
        call_times.append(kwargs["as_of_ms"])
        if len(call_times) == 1:
            clock.now += 10  # A newly completed candle appeared during the request.
        return fresh_result(kwargs["as_of_ms"])

    state = loop.run_loop(
        config(tmp_path), max_cycles=2, clock=clock, wait=clock.wait, pipeline_runner=runner
    )
    assert call_times == [(BASE + STEP - 1) * 1000, (BASE + STEP + 24) * 1000]
    assert clock.waits == [15]
    assert state["last_result"]["lag_intervals_after_delay"] == 0


@pytest.mark.parametrize(
    ("repair_every_cycles", "expected_flags"),
    [
        (3, [False, False, True, False, False, True, False]),
        (0, [False, False, False, False, False, False, False]),
    ],
)
def test_periodic_history_reconciliation_respects_cadence_and_disable_option(
    tmp_path, repair_every_cycles, expected_flags
):
    clock = FakeClock(BASE + 5)
    flags = []

    def runner(**kwargs):
        flags.append(kwargs["refresh_history"])
        return fresh_result(kwargs["as_of_ms"])

    loop.run_loop(
        config(tmp_path, repair_every_cycles=repair_every_cycles),
        max_cycles=len(expected_flags), clock=clock, wait=clock.wait, pipeline_runner=runner,
    )
    assert flags == expected_flags


def test_stop_for_idle_worker_leaves_no_marker_and_cleans_stale_request(tmp_path):
    settings = config(tmp_path)
    marker = settings.dataset_dir / "stop.request"

    first = loop.request_stop(settings)
    assert first["status"] == "already_stopped"
    assert not marker.exists()

    marker.write_text('{"requested_at_utc": "stale"}', encoding="utf-8")
    second = loop.request_stop(settings)
    assert second["status"] == "already_stopped"
    assert not marker.exists()


def test_updating_status_is_persisted_before_starting_refresh(tmp_path):
    settings = config(tmp_path)
    clock = FakeClock(BASE + 5)
    observed = []

    def runner(**kwargs):
        persisted = json.loads((settings.dataset_dir / "loop_status.json").read_text(encoding="utf-8"))
        observed.append(persisted["status"])
        assert persisted["status"] == "updating"
        assert persisted["cycle_count"] == len(observed)
        assert persisted["next_wake_utc"] is None
        return fresh_result(kwargs["as_of_ms"])

    state = loop.run_loop(
        settings, max_cycles=2, clock=clock, wait=clock.wait, pipeline_runner=runner
    )
    # Assertions inside the runner are caught as failed cycles by the worker;
    # checking successful_cycles prevents those failures from hiding this bug.
    assert state["successful_cycles"] == 2
    assert state["failures"] == 0
    assert observed == ["updating", "updating"]


def test_atomic_status_write_retries_transient_windows_reader_lock(tmp_path, monkeypatch):
    path = tmp_path / "loop_status.json"
    old = {"status": "waiting", "cycle_count": 1}
    new = {"status": "stopped", "cycle_count": 2}
    path.write_text(json.dumps(old), encoding="utf-8")
    real_replace = loop.os.replace
    attempts = []
    sleeps = []

    def replace(source, destination):
        attempts.append((source, destination))
        assert json.loads(path.read_text(encoding="utf-8")) == old
        assert json.loads(Path(source).read_text(encoding="utf-8")) == new
        if len(attempts) <= 2:
            raise PermissionError("A Windows reader temporarily holds the status file.")
        return real_replace(source, destination)

    monkeypatch.setattr(loop.os, "replace", replace)
    monkeypatch.setattr(loop.time, "sleep", sleeps.append)
    loop._atomic_json(path, new)

    assert len(attempts) == 3
    assert sleeps == pytest.approx([0.01, 0.02])
    assert json.loads(path.read_text(encoding="utf-8")) == new
    assert set(tmp_path.iterdir()) == {path}


def test_atomic_status_write_exhausted_retries_preserve_old_json_and_remove_temporary(tmp_path, monkeypatch):
    path = tmp_path / "loop_status.json"
    old = '{"status": "waiting", "cycle_count": 1}\n'
    path.write_text(old, encoding="utf-8")
    previous_bytes = path.read_bytes()
    attempts = []
    sleeps = []

    def replace(source, destination):
        attempts.append((source, destination))
        assert Path(source).exists()
        assert path.read_bytes() == previous_bytes
        raise PermissionError("The status file remains locked.")

    monkeypatch.setattr(loop.os, "replace", replace)
    monkeypatch.setattr(loop.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError, match="remains locked"):
        loop._atomic_json(path, {"status": "stopped", "cycle_count": 2})

    assert len(attempts) == 6
    assert sleeps == pytest.approx([0.01, 0.02, 0.04, 0.08, 0.16])
    assert path.read_bytes() == previous_bytes
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "waiting"
    assert set(tmp_path.iterdir()) == {path}


def test_cycle_duration_uses_monotonic_time_despite_wall_clock_adjustment(tmp_path):
    settings = config(tmp_path)
    clock = FakeClock(BASE + 50)
    counter = FakeClock(100)
    stages = {"fetch_and_normalize": 0.7, "feature_build": 0.2, "total_before_publish": 1.1}

    def runner(**kwargs):
        clock.now -= 10  # An operating-system clock correction is not negative work.
        counter.now += 2.25
        return {**fresh_result(kwargs["as_of_ms"]), "timings_seconds": stages,
                "queue_wait_seconds": 0.5}

    state = loop.run_loop(
        settings, max_cycles=1, clock=clock, monotonic=counter,
        wait=clock.wait, pipeline_runner=runner,
    )
    timing = state["last_cycle_timing"]
    assert timing["total_seconds"] == 2.25
    assert timing["finished_at_utc"] < timing["started_at_utc"]
    assert timing["queue_wait_seconds"] == 0.5
    assert timing["timings_seconds"] == stages
    assert timing["outcome"] == "success"
    persisted = json.loads((settings.dataset_dir / "loop_status.json").read_text())
    assert persisted["last_cycle_timing"] == timing


def test_published_unchanged_and_failed_cycles_keep_separate_timings(tmp_path):
    settings = config(tmp_path)
    clock = FakeClock(BASE + 5)
    counter = FakeClock(100)
    attempts = []

    def runner(**kwargs):
        attempts.append(kwargs)
        counter.now += len(attempts)
        if len(attempts) == 3:
            raise RuntimeError("temporary endpoint outage")
        unchanged = len(attempts) == 2
        return {**fresh_result(kwargs["as_of_ms"]),
                "status": "unchanged" if unchanged else "published",
                "timings_seconds": {"feature_build": 0.0 if unchanged else 0.25,
                                    "fetch_and_normalize": 0.1 * len(attempts)}}

    state = loop.run_loop(
        settings, max_cycles=3, clock=clock, monotonic=counter,
        wait=clock.wait, pipeline_runner=runner,
    )
    events = [json.loads(line) for line in (settings.dataset_dir / "loop_events.jsonl").read_text().splitlines()]
    attempts = [event for event in events if event["event"] in {"cycle", "error"}]
    assert [event["last_cycle_timing"]["total_seconds"] for event in attempts] == [1, 2, 3]
    assert [event["last_cycle_timing"]["result_status"] for event in attempts] == ["published", "unchanged", None]
    assert attempts[1]["last_cycle_timing"]["timings_seconds"]["feature_build"] == 0
    assert attempts[1]["last_cycle_timing"]["timings_seconds"]["fetch_and_normalize"] == 0.2
    assert attempts[2]["last_cycle_timing"]["outcome"] == "failed"
    assert attempts[2]["last_cycle_timing"]["timings_seconds"] == {}
    assert "run_id" not in attempts[2]["last_cycle_timing"]
    assert attempts[2]["last_result"] is None
    assert state["last_result"] is None
    assert state["successful_cycles"] == 2
    assert state["failures"] == 1


def test_timing_status_write_failure_does_not_prevent_refresh(tmp_path, monkeypatch):
    clock = FakeClock(BASE + 5)

    def cannot_write(*args):
        raise OSError("status directory is temporarily unavailable")

    monkeypatch.setattr(loop, "_atomic_json", cannot_write)
    state = loop.run_loop(
        config(tmp_path), max_cycles=1, clock=clock, wait=clock.wait,
        pipeline_runner=lambda **kwargs: fresh_result(kwargs["as_of_ms"]),
    )
    assert state["successful_cycles"] == 1
    assert state["last_cycle_timing"]["outcome"] == "success"
