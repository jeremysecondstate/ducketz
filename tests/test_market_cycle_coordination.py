from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from datafetching.bar_readiness import publish_bar_readiness
from datafetching.bar_schema import write_normalized_bar_parquet
from datafetching.decision_time import (
    CycleTargetState,
    cycle_target_decision,
    latest_eligible_option_target,
)
from datafetching.options_runtime import report_options_result, run_options_cycle
from datafetching.parquet_store import ParquetStore
from ml.option_pricing.target_outcome import read_target_outcome
from ml.option_pricing_runtime import (
    RetryablePricingReadinessError,
    _missed_boundaries,
    _pricing_boundary_is_recoverable,
    _run_pricing_until_ready,
    next_boundary,
    report_pricing_result,
    run_option_pricing_once,
)
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


@pytest.mark.parametrize(
    ("observed", "expected"),
    (
        ("2026-08-10T21:46:00Z", "2026-08-10T20:00:00Z"),
        ("2026-08-11T02:00:00Z", "2026-08-10T20:00:00Z"),
        ("2026-08-15T17:01:00Z", "2026-08-14T20:00:00Z"),
        ("2026-11-27T18:16:00Z", "2026-11-27T18:00:00Z"),
    ),
)
def test_latest_eligible_option_target_binds_discovery_to_real_session(
    observed: str,
    expected: str,
) -> None:
    assert latest_eligible_option_target(observed) == pd.Timestamp(expected)


def test_repeated_closed_pricing_cycles_do_not_grow_targets(
    tmp_path: Path,
) -> None:
    for minute in (46, 1, 16):
        now = pd.Timestamp(f"2026-08-10T2{1 + int(minute < 30)}:{minute:02d}:00Z")
        pricing = run_option_pricing_once(
            tmp_path,
            symbols=("GOOG",),
            run_timestamp=now,
        )
        assert pricing.cycle_mode == "MONITOR_ONLY"

    assert not (tmp_path / "ml" / "option-pricing-target-outcomes").exists()
    assert not list(tmp_path.rglob("*error*.parquet"))


def test_pricing_wait_accepts_readiness_before_deadline(tmp_path: Path) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    _write_bar(tmp_path, symbol="GOOG", target=target)
    simulation = _ReadinessSimulation(
        tmp_path,
        target=target,
        publish_after_seconds=0.5,
        ready_offset_seconds=0.5,
    )

    result = _run_pricing_until_ready(
        tmp_path,
        symbols=("GOOG",),
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=2.0,
        phase_offset_minutes=1,
        runtime_clock=simulation.clock,
        readiness_sleeper=simulation.sleep,
        monotonic_clock=simulation.monotonic,
    )

    assert result.target_state == CycleTargetState.ACTIONABLE_EXACT_TARGET.value
    assert result.target_outcome_status != "TARGET_BAR_NOT_READY"
    assert simulation.published is True
    assert (tmp_path / "ml" / "option-pricing-latest" / "run.json").is_file()


def test_pricing_coordinator_rejects_stale_readiness_pointer(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    stale_target = target - pd.Timedelta(minutes=15)
    _write_bar(tmp_path, symbol="GOOG", target=stale_target)
    publish_bar_readiness(
        tmp_path,
        target_snapshot_for=stale_target,
        symbols=("GOOG",),
        loop_a_generation="stale-loop-a",
        as_of=stale_target + pd.Timedelta(seconds=20),
        clock=lambda: stale_target + pd.Timedelta(seconds=21),
    )
    pricing_pointer = tmp_path / "ml" / "option-pricing-latest" / "run.json"
    pricing_pointer.parent.mkdir(parents=True)
    unchanged_pointer = b'{"sentinel":"prior-pricing-authority"}\n'
    pricing_pointer.write_bytes(unchanged_pointer)

    result = _run_pricing_until_ready(
        tmp_path,
        symbols=("GOOG",),
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=0,
        phase_offset_minutes=1,
        runtime_clock=lambda: target + pd.Timedelta(minutes=1),
        readiness_sleeper=lambda _seconds: None,
        monotonic_clock=lambda: 0.0,
    )

    readiness_pointer = json.loads(
        (tmp_path / "loop-a" / "bar-readiness-latest" / "run.json").read_text(
            encoding="utf-8"
        )
    )
    assert readiness_pointer["current"]["target_snapshot_for"] == stale_target.isoformat()
    assert result.target_state == CycleTargetState.READINESS_DEADLINE_MISSED.value
    assert result.target_snapshot_for == target
    assert result.target_outcome_directory is None
    assert pricing_pointer.read_bytes() == unchanged_pointer
    assert not (tmp_path / "ml" / "option-pricing-target-latest" / "run.json").exists()


def test_pricing_readiness_deadline_expires_once_without_log_spam(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    simulation = _ReadinessSimulation(
        tmp_path,
        target=target,
        publish_after_seconds=10.0,
        ready_offset_seconds=10.0,
    )

    result = _run_pricing_until_ready(
        tmp_path,
        symbols=("GOOG",),
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=2.0,
        phase_offset_minutes=1,
        runtime_clock=simulation.clock,
        readiness_sleeper=simulation.sleep,
        monotonic_clock=simulation.monotonic,
    )
    output: list[str] = []
    report_pricing_result(result, reporter=output.append)

    assert simulation.elapsed == pytest.approx(2.0)
    assert simulation.published is False
    assert result.target_state == CycleTargetState.READINESS_DEADLINE_MISSED.value
    assert result.target_outcome_status == "SKIPPED_READINESS_DEADLINE"
    assert len(output) == 1
    assert "Pricing target skipped" in output[0]
    assert "pricing_authority=UNCHANGED" in output[0]
    assert "options_capture=INDEPENDENT" in output[0]


def test_readiness_deadline_skip_advances_to_next_target(tmp_path: Path) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    scheduled_boundary = (target + pd.Timedelta(minutes=1)).to_pydatetime()

    result = _run_pricing_until_ready(
        tmp_path,
        symbols=("GOOG",),
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=0,
        phase_offset_minutes=1,
        runtime_clock=lambda: target + pd.Timedelta(minutes=1),
        readiness_sleeper=lambda _seconds: None,
        monotonic_clock=lambda: 0.0,
    )
    following_boundary = next_boundary(
        (target + pd.Timedelta(minutes=1, seconds=1)).to_pydatetime(),
        interval_minutes=15,
        phase_offset_minutes=1,
    )

    assert result.next_eligible_cycle == target + pd.Timedelta(minutes=16)
    assert following_boundary == result.next_eligible_cycle.to_pydatetime()
    assert _missed_boundaries(
        scheduled_boundary,
        following_boundary,
        interval_minutes=15,
    ) == ()


def test_late_readiness_remains_retryable_without_empty_terminal(
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

    with pytest.raises(RetryablePricingReadinessError):
        run_option_pricing_once(
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
    assert not (tmp_path / "ml" / "option-pricing-target-latest" / "run.json").exists()

    second = run_option_pricing_once(
        tmp_path,
        symbols=("GOOG",),
        run_timestamp=target + pd.Timedelta(minutes=1, seconds=2),
        runtime_clock=lambda: target + pd.Timedelta(minutes=1, seconds=3),
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=0,
    )

    authoritative = read_target_outcome(tmp_path, target_snapshot_for=target)
    target_directories = tuple(
        path
        for path in (tmp_path / "ml" / "option-pricing-target-outcomes").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    assert simulation.published is True
    assert second.target_outcome_directory == authoritative.directory
    assert second.target_outcome_status != "TARGET_BAR_NOT_READY"
    assert len(target_directories) == 1


@pytest.mark.parametrize(
    ("delay_minutes", "recoverable"),
    ((13, True), (19, True), (22, False)),
)
def test_pricing_scheduler_preserves_only_still_causal_delayed_boundaries(
    delay_minutes: int,
    recoverable: bool,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    scheduled_phase = target + pd.Timedelta(minutes=1)

    assert _pricing_boundary_is_recoverable(
        scheduled_phase.to_pydatetime(),
        observed_at=target + pd.Timedelta(minutes=delay_minutes),
    ) is recoverable


@pytest.mark.parametrize("delay_minutes", (13, 19))
def test_pricing_accepts_delayed_exact_readiness_inside_causal_window(
    tmp_path: Path,
    delay_minutes: int,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    ready_at = target + pd.Timedelta(minutes=delay_minutes)
    _write_bar(tmp_path, symbol="GOOG", target=target)
    publish_bar_readiness(
        tmp_path,
        target_snapshot_for=target,
        symbols=("GOOG",),
        loop_a_generation=f"delayed-{delay_minutes}",
        as_of=ready_at,
        clock=lambda: ready_at,
    )

    result = run_option_pricing_once(
        tmp_path,
        symbols=("GOOG",),
        run_timestamp=ready_at,
        runtime_clock=lambda: ready_at + pd.Timedelta(seconds=1),
        target_snapshot_for=target,
        bar_readiness_mode="required",
        bar_readiness_timeout_seconds=0,
    )

    assert result.target_snapshot_for == target
    assert result.target_state == CycleTargetState.ACTIONABLE_EXACT_TARGET.value
    assert result.target_published_at is not None
    assert result.target_published_at >= ready_at


def test_pricing_never_promotes_readiness_observed_after_causal_window(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    _write_bar(tmp_path, symbol="GOOG", target=target)
    simulation = _ReadinessSimulation(
        tmp_path,
        target=target,
        publish_after_seconds=1.5,
        ready_offset_seconds=1.0,
    )
    simulation.wall = target + pd.Timedelta(minutes=19, seconds=59)

    with pytest.raises(ValueError, match="outside the 1,200-second"):
        run_option_pricing_once(
            tmp_path,
            symbols=("GOOG",),
            run_timestamp=simulation.wall,
            runtime_clock=simulation.clock,
            target_snapshot_for=target,
            bar_readiness_mode="required",
            bar_readiness_timeout_seconds=2,
            readiness_sleeper=simulation.sleep,
            monotonic_clock=simulation.monotonic,
        )

    assert not (tmp_path / "ml" / "option-pricing-target-latest" / "run.json").exists()


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
    assert "health_scope=LAST_ACTIONABLE_GENERATION" in joined


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
