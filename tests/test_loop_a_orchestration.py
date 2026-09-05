from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from datafetching import FetchResult
from datafetching import orchestrate
from app.models.market_data import MarketBar
from app.services.databento_market_data import DatabentoAvailableRange
from datafetching.loop_a_cycle import read_loop_a_cycle
from datafetching.bar_readiness import (
    BarReadinessError,
    publish_frozen_bar_readiness,
    read_bar_readiness,
)
from datafetching.bar_schema import write_normalized_bar_parquet
from datafetching.parquet_store import ParquetStore
from datafetching.readiness_lane import (
    LoopAReadinessLane,
    materialize_exact_target_readiness,
)
import pandas as pd


def test_loop_a_stage_order_preserves_fetch_and_calculations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fetch(*_args: object, **_kwargs: object) -> tuple[FetchResult, ...]:
        events.append("fetch")
        return (FetchResult("fmp", 2, 0, advisory_files=3),)

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


def test_loop_a_batches_the_watchlist_across_every_configured_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[tuple[str, ...], tuple[str, ...], bool, bool]] = []

    def fetch(
        symbols: tuple[str, ...],
        _store: ParquetStore,
        **kwargs: object,
    ) -> dict[str, tuple[FetchResult, ...]]:
        providers = tuple(kwargs["providers"])
        observed.append(
            (
                symbols,
                providers,
                bool(kwargs["include_cme"]),
                bool(kwargs["include_fmp_macro"]),
            )
        )
        return {
            symbol: tuple(
                FetchResult(provider, 0, 0)
                for provider in providers
                if index == 0 or provider != "fred"
            )
            for index, symbol in enumerate(symbols)
        }

    monkeypatch.setattr(orchestrate, "run_symbols_fetch", fetch)
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
            ("GOOG", "MU"),
            ("databento", "fmp", "fred", "schwab", "sec"),
            True,
            True,
        )
    ]


def test_schwab_quote_capture_failure_does_not_block_directional_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fetch(
        symbols: tuple[str, ...],
        _store: ParquetStore,
        **_kwargs: object,
    ) -> dict[str, tuple[FetchResult, ...]]:
        return {
            symbol: (
                FetchResult("databento", 1, 0),
                FetchResult("schwab", 0, 1),
            )
            for symbol in symbols
        }

    monkeypatch.setattr(orchestrate, "run_symbols_fetch", fetch)

    failures = orchestrate.run_cycle(
        ("GOOG", "NVDA"),
        ParquetStore(tmp_path),
        providers=("databento", "schwab"),
        requested_profile="continuation",
        include_cme=False,
        include_options=False,
        run_technical_calculations=False,
        run_fundamental_calculations=False,
        run_signal_calculations=False,
        datastore_target=None,
        datastore_path=tmp_path,
    )

    assert failures == 0
    output = capsys.readouterr().out
    assert "blocking provider failures: 0" in output
    assert "optional capture failures: 1 (schwab=1)" in output


def test_authoritative_price_failure_still_blocks_directional_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orchestrate,
        "run_symbols_fetch",
        lambda symbols, _store, **_kwargs: {
            symbol: (
                FetchResult("databento", 0, 1),
                FetchResult("schwab", 0, 1),
            )
            for symbol in symbols
        },
    )

    failures = orchestrate.run_cycle(
        ("GOOG", "NVDA"),
        ParquetStore(tmp_path),
        providers=("databento", "schwab"),
        requested_profile="continuation",
        include_cme=False,
        include_options=False,
        run_technical_calculations=False,
        run_fundamental_calculations=False,
        run_signal_calculations=False,
        datastore_target=None,
        datastore_path=tmp_path,
    )

    assert failures == 2


def test_explicit_inline_schwab_capture_failure_remains_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orchestrate,
        "run_symbols_fetch",
        lambda symbols, _store, **_kwargs: {
            symbol: (FetchResult("schwab", 0, 1),)
            for symbol in symbols
        },
    )

    failures = orchestrate.run_cycle(
        ("GOOG", "NVDA"),
        ParquetStore(tmp_path),
        providers=("schwab",),
        requested_profile="continuation",
        include_cme=False,
        include_options=True,
        include_schwab_price_history=False,
        run_technical_calculations=False,
        run_fundamental_calculations=False,
        run_signal_calculations=False,
        datastore_target=None,
        datastore_path=tmp_path,
    )

    assert failures == 2


def test_databento_readiness_precedes_unrelated_provider_and_calculation_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    events: list[str] = []

    def fetch(
        symbols: tuple[str, ...],
        _store: ParquetStore,
        **kwargs: object,
    ) -> dict[str, tuple[FetchResult, ...]]:
        callback = kwargs["provider_completed"]
        for symbol in symbols:
            directory = (
                tmp_path
                / "stocks"
                / symbol
                / "bars"
                / "1m"
                / "databento"
                / "normalized"
            )
            directory.mkdir(parents=True)
            write_normalized_bar_parquet(
                pd.DataFrame(
                    {
                        "timestamp": [target - pd.Timedelta(minutes=1)],
                        "open": [199.0],
                        "high": [201.0],
                        "low": [198.0],
                        "close": [200.0],
                        "volume": [1000.0],
                    }
                ),
                directory / "bars.parquet",
            )
        events.append("databento-complete")
        callback(
            "databento",
            {symbol: FetchResult("databento", 1, 0) for symbol in symbols},
        )
        read_bar_readiness(
            tmp_path,
            target_snapshot_for=target,
            required_symbols=symbols,
        )
        events.append("fmp-start")
        return {
            symbol: (
                FetchResult("databento", 1, 0),
                FetchResult("fmp", 0, 0),
            )
            for symbol in symbols
        }

    monkeypatch.setattr(orchestrate, "run_symbols_fetch", fetch)
    monkeypatch.setattr(
        orchestrate,
        "run_fundamentals",
        lambda _args: events.append("fundamentals") or 0,
    )
    monkeypatch.setattr(orchestrate, "run_technicals", lambda _args: 0)
    monkeypatch.setattr(orchestrate, "run_signals", lambda _args: 0)

    assert orchestrate.run_cycle(
        ("GOOG", "NVDA"),
        ParquetStore(tmp_path),
        providers=("databento", "fmp"),
        requested_profile="continuation",
        include_cme=False,
        include_options=False,
        run_technical_calculations=False,
        run_fundamental_calculations=True,
        run_signal_calculations=False,
        datastore_target=None,
        datastore_path=tmp_path,
        cycle_started_at=target + pd.Timedelta(seconds=20),
        loop_a_generation="early-readiness",
        bar_readiness_clock=lambda: target + pd.Timedelta(seconds=21),
    ) == 0
    assert events[:2] == ["databento-complete", "fmp-start"]
    assert events.index("fmp-start") < events.index("fundamentals")


def test_independent_readiness_lane_freezes_exact_bars_after_provider_delay(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    state = {"seconds": 0.0, "metadata_calls": 0}

    class Provider:
        dataset = "EQUS.MINI"

        @staticmethod
        def native_specs():
            return (SimpleNamespace(schema="ohlcv-1m", frequency="1m"),)

        @staticmethod
        def dataset_range():
            return {}

        @staticmethod
        def available_range_for_schema(_schema, *, dataset_range):
            del dataset_range
            state["metadata_calls"] += 1
            end = target if state["metadata_calls"] > 1 else target - pd.Timedelta(minutes=1)
            return DatabentoAvailableRange(
                schema="ohlcv-1m",
                start=(target - pd.Timedelta(days=1)).to_pydatetime(),
                end=end.to_pydatetime(),
            )

        @staticmethod
        def fetch_native_bars_range(symbols, _spec, *, start, end, available_range):
            assert pd.Timestamp(start) == target - pd.Timedelta(minutes=1)
            assert pd.Timestamp(end) == target
            bars = {
                symbol: (
                    [
                        MarketBar(
                            symbol=symbol,
                            source="databento",
                            timeframe="1m",
                            timestamp=(target - pd.Timedelta(minutes=1)).to_pydatetime(),
                            open=199.0,
                            high=201.0,
                            low=198.0,
                            close=200.0,
                            volume=1000.0,
                        )
                    ],
                    pd.DataFrame(),
                )
                for symbol in symbols
            }
            return bars, DatabentoAvailableRange(
                schema="ohlcv-1m",
                start=pd.Timestamp(start).to_pydatetime(),
                end=pd.Timestamp(end).to_pydatetime(),
            )

    def clock() -> datetime:
        return (target + pd.Timedelta(seconds=1 + state["seconds"])).to_pydatetime()

    def sleeper(seconds: float) -> None:
        state["seconds"] += seconds

    readiness = materialize_exact_target_readiness(
        tmp_path,
        symbols=("GOOG", "NVDA"),
        target_snapshot_for=target,
        deadline_seconds=420,
        poll_seconds=10,
        provider=Provider(),
        clock=clock,
        sleeper=sleeper,
        monotonic_clock=lambda: state["seconds"],
    )

    assert readiness.close("GOOG") == 200.0
    assert readiness.coordination["first_delayed_stage"] == (
        "DATABENTO_PROVIDER_AVAILABILITY"
    )
    assert readiness.coordination["deadline_at"] == (
        target + pd.Timedelta(seconds=420)
    ).isoformat()
    frozen_source = readiness.decision_clock("GOOG").source_file
    assert frozen_source.parent == readiness.directory / "bars"
    assert frozen_source in readiness.evidence_files

    frame = pd.read_parquet(frozen_source)
    frame.loc[0, "close"] = 999.0
    frame.to_parquet(frozen_source, index=False)
    with pytest.raises(BarReadinessError, match="source checksum"):
        read_bar_readiness(
            tmp_path,
            target_snapshot_for=target,
            required_symbols=("GOOG", "NVDA"),
        )


def test_frozen_readiness_rejects_partial_and_wrong_target_bars(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")

    def payload(timestamp: pd.Timestamp) -> dict[str, object]:
        return {
            "timestamp": timestamp,
            "open": 199.0,
            "high": 201.0,
            "low": 198.0,
            "close": 200.0,
            "volume": 1000.0,
            "provider": "databento",
            "dataset": "EQUS.MINI",
            "schema": "ohlcv-1m",
            "timeframe": "1m",
        }

    with pytest.raises(BarReadinessError, match="all-symbol scope"):
        publish_frozen_bar_readiness(
            tmp_path,
            target_snapshot_for=target,
            symbols=("GOOG", "NVDA"),
            loop_a_generation="partial",
            exact_bars={"GOOG": payload(target - pd.Timedelta(minutes=1))},
            coordination={},
            clock=lambda: target + pd.Timedelta(seconds=2),
        )
    with pytest.raises(BarReadinessError, match="exact completed target bar"):
        publish_frozen_bar_readiness(
            tmp_path,
            target_snapshot_for=target,
            symbols=("GOOG",),
            loop_a_generation="wrong-target",
            exact_bars={"GOOG": payload(target)},
            coordination={},
            clock=lambda: target + pd.Timedelta(seconds=2),
        )
    assert not (tmp_path / "loop-a" / "bar-readiness" / str(target.value)).exists()


def test_readiness_lane_runs_while_heavy_datastore_lock_is_occupied(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    published = threading.Event()

    def materializer(*_args, **_kwargs):
        published.set()
        return SimpleNamespace(
            ready_at=target + pd.Timedelta(seconds=2),
            symbols=("GOOG", "NVDA"),
        )

    lane = LoopAReadinessLane(
        datastore_root=tmp_path,
        symbols=("GOOG", "NVDA"),
        clock=lambda: (target + pd.Timedelta(seconds=1)).to_pydatetime(),
        materializer=materializer,
        reporter=lambda _message: None,
    )

    # This lock represents a long Loop B/Loop A heavyweight generation. The
    # exact-bar lane has no dependency on it.
    from datafetching.loop_a_cycle import datastore_cycle_lock

    with datastore_cycle_lock(tmp_path):
        worker = threading.Thread(target=lane.inspect_once)
        worker.start()
        assert published.wait(timeout=2.0)
        worker.join(timeout=2.0)
        assert not worker.is_alive()


def test_readiness_lane_process_reload_never_publishes_an_expired_target(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")

    def forbidden_materializer(*_args, **_kwargs):
        raise AssertionError("expired target must not call the provider")

    lane = LoopAReadinessLane(
        datastore_root=tmp_path,
        symbols=("GOOG",),
        deadline_seconds=420,
        clock=lambda: (target + pd.Timedelta(minutes=8)).to_pydatetime(),
        materializer=forbidden_materializer,
        reporter=lambda _message: None,
    )

    result = lane.inspect_once()

    assert result is not None
    assert result["status"] == "READINESS_DEADLINE_MISSED"
    assert result["target_snapshot_for"] == target.isoformat()
    assert result["first_delayed_stage"] == "SCHEDULER_WAKE"
    assert not (tmp_path / "loop-a" / "bar-readiness" / str(target.value)).exists()


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


def test_next_boundary_preserves_cycle_cadence_after_an_overrun() -> None:
    cycle_started_at = datetime(2026, 7, 29, 10, 0, 20, tzinfo=timezone.utc)
    cycle_completed_at = datetime(2026, 7, 29, 10, 15, 5, tzinfo=timezone.utc)

    next_run = orchestrate.next_boundary(
        cycle_started_at,
        interval_minutes=15,
    )

    assert next_run == datetime(2026, 7, 29, 10, 15, tzinfo=timezone.utc)
    assert next_run < cycle_completed_at


def test_opra_history_maintenance_is_due_once_after_daily_boundary() -> None:
    before = datetime(2026, 9, 3, 20, 59, tzinfo=timezone.utc)
    after = datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc)

    assert not orchestrate.opra_history_maintenance_due(
        before,
        last_attempt=None,
        utc_hour=21,
    )
    assert orchestrate.opra_history_maintenance_due(
        after,
        last_attempt=None,
        utc_hour=21,
    )
    assert not orchestrate.opra_history_maintenance_due(
        after,
        last_attempt=after.date(),
        utc_hour=21,
    )


def test_loop_a_opra_history_command_owns_strategy_training_schemas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(orchestrate.subprocess, "run", run)
    result = orchestrate.run_opra_history_maintenance_once(
        ParquetStore(tmp_path),
        symbols=("AAPL", "NVDA"),
        max_estimated_download_bytes=123,
        max_estimated_cost_usd=0.5,
        max_incremental_catchup_days=7,
    )

    assert len(calls) == 2
    command, history_kwargs = calls[0]
    assert result == 0
    assert isinstance(command, list)
    assert command[:4] == [
        sys.executable,
        "-u",
        "-m",
        "datafetching.options_history",
    ]
    schema_index = command.index("--schemas")
    assert command[schema_index + 1 : schema_index + 4] == [
        "ohlcv-1h",
        "cbbo-1m",
        "definition",
    ]
    assert "--incremental-only" in command
    assert command[command.index("--max-incremental-catchup-days") + 1] == "7"
    assert history_kwargs["check"] is False
    catalog_command, catalog_kwargs = calls[1]
    assert catalog_command[:4] == [
        sys.executable,
        "-u",
        "-m",
        "datafetching.datastore_hygiene",
    ]
    assert "--confirm-cleanup" not in catalog_command
    assert catalog_command[catalog_command.index("--symbols") + 1 :] == [
        "AAPL",
        "NVDA",
    ]
    assert catalog_kwargs["check"] is False


def test_loop_a_does_not_refresh_catalog_after_failed_opra_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(orchestrate.subprocess, "run", run)
    result = orchestrate.run_opra_history_maintenance_once(
        ParquetStore(tmp_path), symbols=("AAPL",)
    )

    assert result == 7
    assert len(calls) == 1
    assert "datafetching.options_history" in calls[0]
