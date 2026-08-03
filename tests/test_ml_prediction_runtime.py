from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from datafetching.loop_a_cycle import (
    begin_loop_a_cycle,
    finish_loop_a_cycle,
)
from ml import prediction_runtime


def _publish_complete_loop_a_cycle(root: Path) -> None:
    writing = begin_loop_a_cycle(
        root,
        symbols=("GOOG",),
        providers=("databento",),
        now="2026-07-30T15:00:00Z",
    )
    finish_loop_a_cycle(
        root,
        writing,
        failure_count=0,
        now="2026-07-30T15:01:00Z",
    )


def _result(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        status="SUCCESS",
        sample_rows=10,
        prediction_rows=4,
        evaluation_rows=2,
        models_trained=0,
        models_reused=4,
        run_directory=root / "ml" / "runs" / "run",
        latest_intelligence_path=root / "ml" / "latest" / "intelligence.parquet",
    )


def test_next_boundary_aligns_hourly_runtime() -> None:
    assert prediction_runtime.next_boundary(
        datetime(2026, 7, 29, 10, 14, 30, tzinfo=timezone.utc),
        interval_minutes=60,
        phase_offset_minutes=5,
    ) == datetime(2026, 7, 29, 11, 5, tzinfo=timezone.utc)


def test_next_boundary_accepts_zero_phase_without_coordination() -> None:
    assert prediction_runtime.next_boundary(
        datetime(2026, 7, 29, 10, 14, 30, tzinfo=timezone.utc),
        interval_minutes=15,
        phase_offset_minutes=0,
    ) == datetime(2026, 7, 29, 10, 15, tzinfo=timezone.utc)


def test_next_boundary_rejects_an_invalid_phase() -> None:
    with pytest.raises(ValueError, match="0 <= phase"):
        prediction_runtime.next_boundary(
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            interval_minutes=15,
            phase_offset_minutes=15,
        )


def test_runtime_lock_rejects_a_second_process_and_cleans_up(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".ducketz-ml-prediction-runtime.lock"
    with prediction_runtime.runtime_lock(lock):
        with pytest.raises(RuntimeError, match="Another Duckets Loop B"):
            with prediction_runtime.runtime_lock(lock):
                pass
    assert not lock.exists()


def test_once_reads_only_a_complete_loop_a_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_complete_loop_a_cycle(tmp_path)
    observed: dict[str, object] = {}
    lock_acquired = False

    class ControlledDateTime:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            del cls, tz
            return datetime(
                2026,
                7,
                30,
                15,
                2 if lock_acquired else 0,
                tzinfo=timezone.utc,
            )

    @contextmanager
    def completed_cycle_lock(*_args: object, **_kwargs: object):
        nonlocal lock_acquired
        lock_acquired = True
        try:
            yield
        finally:
            lock_acquired = False

    def run(root: Path, **kwargs: object) -> SimpleNamespace:
        observed["root"] = root
        observed.update(kwargs)
        return _result(root)

    monkeypatch.setattr(prediction_runtime, "run_loop_b_once", run)
    monkeypatch.setattr(prediction_runtime, "datastore_cycle_lock", completed_cycle_lock)
    monkeypatch.setattr(prediction_runtime, "datetime", ControlledDateTime)

    result = prediction_runtime.main(
        [
            "--datastore",
            str(tmp_path),
            "--symbols",
            "GOOG",
            "--provider",
            "databento",
            "--horizons",
            "1h",
            "4h",
            "1d",
            "1w",
            "--once",
        ]
    )

    assert result == 0
    assert observed["root"] == tmp_path
    assert observed["symbols"] == ("GOOG",)
    assert observed["run_timestamp"] == datetime(
        2026, 7, 30, 15, 2, tzinfo=timezone.utc
    )
    assert observed["input_available_at"] == datetime(
        2026, 7, 30, 15, 1, tzinfo=timezone.utc
    )
    assert tuple(observed["specifications"]) == (
        "1h",
        "4h",
        "1d",
        "1w",
        "1w-d1",
        "1w-d2",
        "1w-d3",
        "1w-d4",
        "1w-d5",
    )
    assert "required_live_decision_timestamp" not in observed
    assert "coordination_context" not in observed


def test_once_without_a_complete_loop_a_cycle_fails_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prediction_runtime,
        "run_loop_b_once",
        lambda *_args, **_kwargs: pytest.fail("incomplete inputs must not be read"),
    )

    result = prediction_runtime.main(
        [
            "--datastore",
            str(tmp_path),
            "--symbols",
            "GOOG",
            "--once",
        ]
    )

    assert result == 1


def test_recurring_loop_can_reuse_the_same_complete_input_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_complete_loop_a_cycle(tmp_path)
    calls = 0

    def run(root: Path, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return _result(root)

    monkeypatch.setattr(prediction_runtime, "run_loop_b_once", run)
    monkeypatch.setattr(prediction_runtime, "next_boundary", lambda now, **_: now)
    monkeypatch.setattr(prediction_runtime.time, "sleep", lambda _seconds: None)

    result = prediction_runtime.main(
        [
            "--datastore",
            str(tmp_path),
            "--symbols",
            "GOOG",
            "--interval-minutes",
            "15",
            "--phase-offset-minutes",
            "0",
        ]
    )

    assert result == 0
    assert calls == 3
    assert not (tmp_path / ".ducketz-ml-prediction-runtime.lock").exists()


def test_failed_once_cycle_returns_nonzero_and_removes_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_complete_loop_a_cycle(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("model failed")

    monkeypatch.setattr(prediction_runtime, "run_loop_b_once", fail)
    result = prediction_runtime.main(
        ["--datastore", str(tmp_path), "--symbols", "GOOG", "--once"]
    )

    assert result == 1
    assert not (tmp_path / ".ducketz-ml-prediction-runtime.lock").exists()
