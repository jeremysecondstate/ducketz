from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from datafetching import FetchResult
from datafetching import orchestrate
from datafetching.loop_a_cycle import read_loop_a_cycle
from datafetching.parquet_store import ParquetStore


def test_loop_a_stage_order_preserves_fetch_and_calculations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fetch(*_args: object, **_kwargs: object) -> tuple[FetchResult, ...]:
        events.append("fetch")
        return (FetchResult("fmp", 2, 0),)

    def stage(name: str):
        def run(_args: list[str]) -> int:
            events.append(name)
            return 0

        return run

    monkeypatch.setattr(orchestrate, "run_symbol_fetch", fetch)
    monkeypatch.setattr(orchestrate, "run_fundamentals", stage("fundamentals"))
    monkeypatch.setattr(orchestrate, "run_technicals", stage("technicals"))
    monkeypatch.setattr(orchestrate, "run_signals", stage("signals"))

    failures = orchestrate.run_cycle(
        ("NVDA",),
        ParquetStore(tmp_path),
        providers=("fmp",),
        requested_profile="continuation",
        include_cme=False,
        run_technical_calculations=True,
        datastore_target=None,
        datastore_path=tmp_path,
        run_fundamental_calculations=True,
        run_signal_calculations=True,
        cycle_started_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )

    assert failures == 0
    assert events == ["fetch", "fundamentals", "technicals", "signals"]


def test_loop_a_fetches_every_configured_lane_without_a_forecast_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, tuple[str, ...], bool, bool]] = []

    def fetch(
        symbol: str,
        _store: ParquetStore,
        **kwargs: object,
    ) -> tuple[FetchResult, ...]:
        providers = tuple(kwargs["providers"])
        observed.append(
            (
                symbol,
                providers,
                bool(kwargs["include_cme"]),
                bool(kwargs["include_fmp_macro"]),
            )
        )
        return tuple(FetchResult(provider, 0, 0) for provider in providers)

    monkeypatch.setattr(orchestrate, "run_symbol_fetch", fetch)
    monkeypatch.setattr(orchestrate, "run_fundamentals", lambda _args: 0)
    monkeypatch.setattr(orchestrate, "run_technicals", lambda _args: 0)
    monkeypatch.setattr(orchestrate, "run_signals", lambda _args: 0)

    failures = orchestrate.run_cycle(
        ("GOOG", "MU"),
        ParquetStore(tmp_path),
        providers=("databento", "fmp", "fred", "schwab", "sec"),
        requested_profile="continuation",
        include_cme=True,
        run_technical_calculations=True,
        datastore_target=None,
        datastore_path=tmp_path,
    )

    assert failures == 0
    assert observed == [
        (
            "GOOG",
            ("databento", "fmp", "fred", "schwab", "sec"),
            True,
            True,
        ),
        ("MU", ("databento", "fmp", "schwab", "sec"), False, False),
    ]


def test_once_publishes_writing_then_complete_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_statuses: list[str] = []

    def run(*_args: object, **_kwargs: object) -> int:
        cycle = read_loop_a_cycle(tmp_path)
        assert cycle is not None
        observed_statuses.append(cycle.status)
        return 0

    monkeypatch.setattr(orchestrate, "run_cycle", run)

    result = orchestrate.main(
        [
            "--datastore",
            str(tmp_path),
            "--symbols",
            "GOOG",
            "--providers",
            "databento",
            "--once",
        ]
    )

    assert result == 0
    assert observed_statuses == ["WRITING"]
    terminal = read_loop_a_cycle(tmp_path)
    assert terminal is not None
    assert terminal.status == "COMPLETE"
    assert terminal.failure_count == 0
    assert terminal.symbols == ("GOOG",)
    assert terminal.providers == ("databento",)


def test_failed_once_cycle_publishes_failed_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrate, "run_cycle", lambda *_args, **_kwargs: 2)

    result = orchestrate.main(
        [
            "--datastore",
            str(tmp_path),
            "--symbols",
            "GOOG",
            "--providers",
            "databento",
            "--once",
        ]
    )

    assert result == 1
    terminal = read_loop_a_cycle(tmp_path)
    assert terminal is not None
    assert terminal.status == "FAILED"
    assert terminal.failure_count == 2


def test_keyboard_interrupt_marks_cycle_failed_and_cleans_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrate, "run_cycle", interrupt)

    result = orchestrate.main(
        [
            "--datastore",
            str(tmp_path),
            "--symbols",
            "GOOG",
            "--providers",
            "databento",
            "--once",
        ]
    )

    assert result == 0
    terminal = read_loop_a_cycle(tmp_path)
    assert terminal is not None
    assert terminal.status == "FAILED"
    assert not (tmp_path / ".ducketz-orchestration.lock").exists()


def test_orchestration_lock_rejects_a_second_process_and_cleans_up(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".ducketz-orchestration.lock"
    with orchestrate.orchestration_lock(lock):
        with pytest.raises(RuntimeError, match="Another Duckets orchestration"):
            with orchestrate.orchestration_lock(lock):
                pass
    assert not lock.exists()


def test_orchestration_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    lock = tmp_path / ".ducketz-orchestration.lock"
    script = (
        "import sys; "
        "from pathlib import Path; "
        "from datafetching.orchestrate import orchestration_lock; "
        "p=Path(sys.argv[1]); "
        "\nwith orchestration_lock(p):\n print('locked', flush=True); input()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeError, match="Another Duckets orchestration"):
            with orchestrate.orchestration_lock(lock):
                pass
    finally:
        if process.stdin is not None:
            process.stdin.write("\n")
            process.stdin.flush()
        process.wait(timeout=10)
    assert process.returncode == 0
    assert not lock.exists()


def test_next_boundary_uses_the_next_interval() -> None:
    assert orchestrate.next_boundary(
        datetime(2026, 7, 29, 10, 14, 30, tzinfo=timezone.utc),
        interval_minutes=15,
    ) == datetime(2026, 7, 29, 10, 15, tzinfo=timezone.utc)
