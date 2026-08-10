from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from datafetching.bar_readiness import publish_bar_readiness
from datafetching.bar_schema import write_normalized_bar_parquet
from datafetching.decision_time import (
    CycleTargetState,
    cycle_target_decision,
)
from datafetching.options_runtime import report_options_result, run_options_cycle
from datafetching.parquet_store import ParquetStore
from ml.option_pricing.target_outcome import read_target_outcome
from ml.option_pricing_runtime import report_pricing_result, run_option_pricing_once
from options.publication import publish_option_snapshot


@pytest.mark.parametrize(
    ("observed", "next_target"),
    (
        ("2026-08-10T21:46:00Z", "2026-08-11T13:45:00Z"),
        ("2026-08-11T02:00:00Z", "2026-08-11T13:45:00Z"),
        ("2026-08-15T17:01:00Z", "2026-08-17T13:45:00Z"),
        ("2026-07-03T17:01:00Z", "2026-07-06T13:45:00Z"),
        ("2026-03-09T12:00:00Z", "2026-03-09T13:45:00Z"),
        ("2026-11-27T18:16:00Z", "2026-11-30T14:45:00Z"),
        ("2026-12-25T17:01:00Z", "2026-12-28T14:45:00Z"),
    ),
)
def test_calendar_closed_cycles_are_monitor_only(
    observed: str,
    next_target: str,
) -> None:
    decision = cycle_target_decision(observed)
    assert decision.cycle_mode == "MONITOR_ONLY"
    assert decision.target_state is CycleTargetState.MARKET_CLOSED_IDLE
    assert decision.target_snapshot_for is None
    assert decision.next_eligible_target == pd.Timestamp(next_target)


def test_first_completed_target_and_early_close_are_calendar_owned() -> None:
    before_first = cycle_target_decision("2026-08-11T13:44:59Z")
    first = cycle_target_decision("2026-08-11T13:46:00Z")
    early_close = cycle_target_decision("2026-11-27T18:01:00Z")

    assert before_first.target_state is CycleTargetState.MARKET_CLOSED_IDLE
    assert before_first.next_eligible_target == pd.Timestamp("2026-08-11T13:45:00Z")
    assert first.target_snapshot_for == pd.Timestamp("2026-08-11T13:45:00Z")
    assert early_close.target_snapshot_for == pd.Timestamp("2026-11-27T18:00:00Z")
    assert early_close.session_close == pd.Timestamp("2026-11-27T18:00:00Z")


def test_repeated_closed_cycles_do_not_grow_targets_or_call_schwab(
    tmp_path: Path,
) -> None:
    calls = 0
    output: list[str] = []

    class Session:
        def get_option_chain_snapshot(self, *_args: object, **_kwargs: object):
            nonlocal calls
            calls += 1
            return {"unexpected": True}

    for minute in (46, 1, 16):
        now = pd.Timestamp(f"2026-08-10T2{1 + int(minute < 30)}:{minute:02d}:00Z")
        pricing = run_option_pricing_once(
            tmp_path,
            symbols=("GOOG",),
            run_timestamp=now,
        )
        assert pricing.cycle_mode == "MONITOR_ONLY"
        options = run_options_cycle(
            ParquetStore(tmp_path),
            symbols=("GOOG",),
            session=Session(),  # type: ignore[arg-type]
            clock=lambda now=now: now.to_pydatetime(),
            reporter=output.append,
        )
        assert options.target_snapshot_for is None
        assert options.schwab_called is False

    assert calls == 0
    assert not (tmp_path / "ml" / "option-pricing-target-outcomes").exists()
    assert not list(tmp_path.rglob("*error*.parquet"))
    assert any("cycle_mode=MONITOR_ONLY" in line for line in output)
    assert any("schwab_called=false" in line for line in output)


def test_pricing_wait_accepts_readiness_before_deadline(tmp_path: Path) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    _write_bar(tmp_path, symbol="GOOG", target=target)
    simulation = _ReadinessSimulation(
        tmp_path,
        target=target,
        publish_after_seconds=0.5,
        ready_offset_seconds=0.5,
    )

    result = run_option_pricing_once(
        tmp_path,
        symbols=("GOOG",),
        run_timestamp=target + pd.Timedelta(minutes=1),
        runtime_clock=simulation.clock,
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=2.0,
        readiness_sleeper=simulation.sleep,
        monotonic_clock=simulation.monotonic,
    )

    assert result.target_state == CycleTargetState.ACTIONABLE_EXACT_TARGET.value
    assert result.target_outcome_status != "TARGET_BAR_NOT_READY"
    assert simulation.published is True


def test_late_readiness_publishes_one_immutable_noncreditable_terminal(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    _write_bar(tmp_path, symbol="GOOG", target=target)
    simulation = _ReadinessSimulation(
        tmp_path,
        target=target,
        publish_after_seconds=0.5,
        ready_offset_seconds=1.0,
    )

    first = run_option_pricing_once(
        tmp_path,
        symbols=("GOOG",),
        run_timestamp=target + pd.Timedelta(minutes=1),
        runtime_clock=simulation.clock,
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=0.5,
        readiness_sleeper=simulation.sleep,
        monotonic_clock=simulation.monotonic,
    )
    authoritative = read_target_outcome(tmp_path, target_snapshot_for=target)
    target_directories = tuple(
        path
        for path in (tmp_path / "ml" / "option-pricing-target-outcomes").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )

    second = run_option_pricing_once(
        tmp_path,
        symbols=("GOOG",),
        run_timestamp=target + pd.Timedelta(minutes=1, seconds=2),
        runtime_clock=lambda: target + pd.Timedelta(minutes=1, seconds=3),
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=0,
    )

    assert first.target_state == CycleTargetState.READINESS_DEADLINE_MISSED.value
    assert first.target_outcome_status == "TARGET_BAR_NOT_READY"
    assert authoritative.predictions().empty
    assert second.target_outcome_directory == authoritative.directory
    assert len(target_directories) == 1


def test_options_restart_for_observed_target_is_request_free(tmp_path: Path) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    common = {
        "symbol": "GOOG",
        "snapshot_for": target,
        "available_at": target + pd.Timedelta(minutes=2),
    }
    publish_option_snapshot(
        tmp_path,
        symbol="GOOG",
        raw=pd.DataFrame([{**common, "payload": "fixture"}]),
        contracts=pd.DataFrame([{**common, "contract_symbol": "GOOG-C", "bid": 1.0, "ask": 1.1}]),
        features=pd.DataFrame([{**common, "quality": 1.0}]),
    )

    class Session:
        @staticmethod
        def get_option_chain_snapshot(*_args: object, **_kwargs: object):
            raise AssertionError("Schwab must not be called for an observed target")

    result = run_options_cycle(
        ParquetStore(tmp_path),
        symbols=("GOOG",),
        session=Session(),  # type: ignore[arg-type]
        clock=lambda: (target + pd.Timedelta(minutes=2, seconds=1)).to_pydatetime(),
        target_snapshot_for=target,
        reporter=None,
    )
    assert result.target_state == CycleTargetState.TARGET_ALREADY_OBSERVED.value
    assert result.schwab_called is False
    assert result.skipped == 1
    output: list[str] = []
    report_options_result(result, reporter=output.append)
    assert "pricing_barrier_verification=ALREADY_RECORDED" in output[0]
    assert "pricing_terminal_outcome=NONE" in output[0]
    assert "schwab_called=false" in output[0]


def test_pricing_console_separates_target_deltas_from_carried_inventory(
    tmp_path: Path,
) -> None:
    result = run_option_pricing_once(
        tmp_path,
        symbols=("GOOG",),
        run_timestamp="2026-08-10T21:46:00Z",
    )
    output: list[str] = []
    report_pricing_result(result, reporter=output.append)
    joined = "\n".join(output)
    assert "cycle_mode=MONITOR_ONLY" in joined
    assert "target_state=MARKET_CLOSED_IDLE" in joined
    assert "current_target_predictions=0" in joined
    assert "new_prospective_predictions=0" in joined
    assert "cumulative_predictions=0" in joined
    assert "automated_action_allowed=false" in joined


class _ReadinessSimulation:
    def __init__(
        self,
        root: Path,
        *,
        target: pd.Timestamp,
        publish_after_seconds: float,
        ready_offset_seconds: float,
    ) -> None:
        self.root = root
        self.target = target
        self.wall = target + pd.Timedelta(minutes=1)
        self.elapsed = 0.0
        self.publish_after_seconds = publish_after_seconds
        self.ready_offset_seconds = ready_offset_seconds
        self.published = False

    def clock(self) -> pd.Timestamp:
        return self.wall

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds
        self.wall += pd.Timedelta(seconds=seconds)
        if not self.published and self.elapsed >= self.publish_after_seconds:
            ready_at = self.target + pd.Timedelta(
                minutes=1,
                seconds=self.ready_offset_seconds,
            )
            publish_bar_readiness(
                self.root,
                target_snapshot_for=self.target,
                symbols=("GOOG",),
                loop_a_generation="simulated-loop-a",
                as_of=ready_at,
                clock=lambda: ready_at,
            )
            self.published = True


def _write_bar(root: Path, *, symbol: str, target: pd.Timestamp) -> None:
    directory = (
        root / "stocks" / symbol / "bars" / "1m" / "databento" / "normalized"
    )
    directory.mkdir(parents=True, exist_ok=True)
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
