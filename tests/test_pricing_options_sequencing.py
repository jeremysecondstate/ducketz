from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from datafetching.bar_readiness import (
    BarReadinessError,
    publish_bar_readiness,
    read_bar_readiness,
)
from datafetching.decision_time import (
    DecisionClock,
    completed_bar_clock_for_target,
)
from datafetching.options_runtime import run_options_cycle
from datafetching import FetchResult, orchestrate
from datafetching.parquet_store import ParquetStore
from datafetching.pricing_barrier import wait_for_pricing_barrier
from datafetching.bar_schema import write_normalized_bar_parquet
from ml.option_pricing.black_scholes import black_scholes_price
from ml.option_pricing.causal import (
    build_causal_samples,
    build_live_prediction_inputs,
    reconcile_predictions,
)
from ml.option_pricing.prediction import create_prediction_rows
from ml.option_pricing.publication import receipt_proven_prediction_rows
from ml.option_pricing.publication import (
    OPTION_PRICING_PUBLICATION_VERSION,
    read_current_option_pricing_publication,
)
from ml.option_pricing.target_outcome import (
    TargetOutcomeError,
    publish_target_outcome,
    read_current_target_outcome,
    read_target_outcome,
)
from ml.option_pricing_runtime import (
    _missed_boundaries,
    _publish_missed_target_outcome,
    run_option_pricing_once,
)
from ml.artifacts import file_checksum, write_manifest
from ml.parquet_contracts import (
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    empty_frame,
    write_parquet_with_schema,
)
from options import OptionSnapshotOutput
from options.publication import publish_option_snapshot, read_option_snapshot


def test_loop_a_readiness_is_atomic_all_symbol_and_exact_target(tmp_path: Path) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    for symbol, close in (("GOOG", 201.0), ("NVDA", 181.0)):
        _write_bar(tmp_path, symbol=symbol, target=target, close=close)

    readiness = publish_bar_readiness(
        tmp_path,
        target_snapshot_for=target,
        symbols=("GOOG", "NVDA"),
        loop_a_generation="loop-a-1",
        as_of=target + pd.Timedelta(seconds=20),
        clock=lambda: target + pd.Timedelta(seconds=21),
    )

    verified = read_bar_readiness(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("NVDA", "GOOG"),
    )
    assert verified.ready_at == target + pd.Timedelta(seconds=21)
    assert verified.close("GOOG") == 201.0
    assert verified.close("NVDA") == 181.0
    assert verified.receipt_checksum_sha256 == readiness.receipt_checksum_sha256

    stale_target = target + pd.Timedelta(minutes=15)
    with pytest.raises(FileNotFoundError, match="Exact completed"):
        completed_bar_clock_for_target(
            tmp_path,
            symbol="GOOG",
            target_snapshot_for=stale_target,
            as_of=stale_target + pd.Timedelta(minutes=1),
        )
    with pytest.raises(BarReadinessError, match="No verified"):
        read_bar_readiness(tmp_path, target_snapshot_for=stale_target)


def test_target_outcome_is_invisible_until_verified_pointer_and_restart_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    orphan = tmp_path / "ml" / "option-pricing-target-outcomes" / ".staging"
    orphan.mkdir(parents=True)
    (orphan / "outcome.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(TargetOutcomeError, match="pointer"):
        read_current_target_outcome(tmp_path)
    assert wait_for_pricing_barrier(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("GOOG",),
        timeout_seconds=0,
        clock=lambda: target + pd.Timedelta(minutes=1),
    ).status == "MISSING"

    first = _publish_prediction(tmp_path, symbol="GOOG", target=target)
    assert read_current_target_outcome(tmp_path).directory == first.directory

    from ml.option_pricing import target_outcome as target_module

    real_atomic = target_module._write_json_atomic

    def interrupt_pointer(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated pointer interruption")

    monkeypatch.setattr(target_module, "_write_json_atomic", interrupt_pointer)
    with pytest.raises(OSError, match="pointer interruption"):
        _publish_prediction(
            tmp_path,
            symbol="GOOG",
            target=target + pd.Timedelta(minutes=15),
        )
    monkeypatch.setattr(target_module, "_write_json_atomic", real_atomic)
    assert read_current_target_outcome(tmp_path).directory == first.directory

    recovered = _publish_prediction(
        tmp_path,
        symbol="GOOG",
        target=target + pd.Timedelta(minutes=15),
        created_offset_seconds=62,
    )
    assert read_current_target_outcome(tmp_path).directory == recovered.directory
    assert read_target_outcome(tmp_path, target_snapshot_for=target).directory == first.directory


def test_target_outcome_verification_failure_never_advances_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ml.option_pricing import target_outcome as target_module

    monkeypatch.setattr(
        target_module,
        "verify_parquet_schema",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated incomplete verification")
        ),
    )
    with pytest.raises(RuntimeError, match="incomplete verification"):
        _publish_prediction(
            tmp_path,
            symbol="GOOG",
            target=pd.Timestamp("2026-08-10T17:00:00Z"),
        )
    with pytest.raises(TargetOutcomeError, match="pointer"):
        read_current_target_outcome(tmp_path)


def test_options_barrier_and_real_authority_order_control_prospective_credit(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    publication = _publish_prediction(tmp_path, symbol="GOOG", target=target)
    barrier = wait_for_pricing_barrier(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("GOOG",),
        timeout_seconds=0,
        clock=lambda: publication.published_at + pd.Timedelta(milliseconds=250),
    )
    request = publication.published_at + pd.Timedelta(milliseconds=500)
    available = publication.published_at + pd.Timedelta(seconds=8.75)
    snapshot = _publish_target_snapshot(
        tmp_path,
        publication=publication,
        barrier_metadata=barrier.as_receipt_metadata(request_started_at=request),
        request_started_at=request,
        available_at=available,
    )
    prediction = publication.predictions()
    evaluated = reconcile_predictions(
        prediction,
        snapshots_by_symbol={"GOOG": (snapshot,)},
        evaluated_at=available + pd.Timedelta(seconds=1),
    )
    assert evaluated["evaluation_status"].eq("COMPLETE").all()
    assert evaluated["prospective_eligible"].eq(True).all()
    assert snapshot.available_at - publication.published_at == pd.Timedelta(
        seconds=8.75
    )
    assert snapshot.receipt["pricing_barrier"]["pricing_run_path"] == (
        publication.receipt["run_path"]
    )
    assert snapshot.receipt["pricing_barrier"][
        "pricing_receipt_checksum_sha256"
    ] == publication.receipt_checksum_sha256


def test_late_pricing_cannot_gain_credit_from_an_earlier_embedded_timestamp(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    missing = wait_for_pricing_barrier(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("GOOG",),
        timeout_seconds=0,
        clock=lambda: target + pd.Timedelta(minutes=1, seconds=2),
    )
    request = target + pd.Timedelta(minutes=1, seconds=2)
    publication = _publish_prediction(
        tmp_path,
        symbol="GOOG",
        target=target,
        created_offset_seconds=61,
        published_offset_seconds=64,
    )
    snapshot = _publish_target_snapshot(
        tmp_path,
        publication=publication,
        barrier_metadata=missing.as_receipt_metadata(request_started_at=request),
        request_started_at=request,
        available_at=target + pd.Timedelta(minutes=1, seconds=10),
    )
    prediction = publication.predictions()
    prediction["prediction_available_at"] = target + pd.Timedelta(seconds=61.5)
    evaluated = reconcile_predictions(
        prediction,
        snapshots_by_symbol={"GOOG": (snapshot,)},
        evaluated_at=target + pd.Timedelta(minutes=2),
    )
    assert evaluated["evaluation_status"].eq("COMPLETE").all()
    assert evaluated["prospective_eligible"].eq(False).all()


def test_pricing_published_after_target_receipt_is_never_prospective(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    request = target + pd.Timedelta(minutes=1, seconds=2)
    missing = wait_for_pricing_barrier(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("GOOG",),
        timeout_seconds=0,
        clock=lambda: request,
    )
    publication = _publish_prediction(
        tmp_path,
        symbol="GOOG",
        target=target,
        created_offset_seconds=61,
        published_offset_seconds=80,
    )
    snapshot = _publish_target_snapshot(
        tmp_path,
        publication=publication,
        barrier_metadata=missing.as_receipt_metadata(request_started_at=request),
        request_started_at=request,
        available_at=target + pd.Timedelta(seconds=70),
    )
    prediction = publication.predictions()
    prediction["prediction_available_at"] = target + pd.Timedelta(seconds=61.5)
    evaluated = reconcile_predictions(
        prediction,
        snapshots_by_symbol={"GOOG": (snapshot,)},
        evaluated_at=target + pd.Timedelta(minutes=2),
    )
    assert evaluated["evaluation_status"].eq("PENDING_TARGET_RECEIPT").all()
    assert evaluated["prospective_eligible"].eq(False).all()


def test_causal_exclusions_remain_distinct_and_successful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    clock = DecisionClock(
        decision_timestamp=target,
        bar_timestamp=target - pd.Timedelta(minutes=1),
        provider="databento",
        timeframe="1m",
        source_file=tmp_path / "bar.parquet",
    )
    pd.DataFrame(
        {"timestamp": [clock.bar_timestamp], "close": [200.0]}
    ).to_parquet(clock.source_file, index=False)
    source = _source_surface("GOOG", target=target)
    source_snapshot = _committed_stub(
        tmp_path / "source",
        symbol="GOOG",
        target=target - pd.Timedelta(minutes=15),
        available=target - pd.Timedelta(minutes=14),
        contracts=source,
    )
    observed = _committed_stub(
        tmp_path / "observed",
        symbol="GOOG",
        target=target,
        available=target + pd.Timedelta(seconds=10),
        contracts=source,
    )
    monkeypatch.setattr(
        "ml.option_pricing.causal.completed_bar_clock_for_target",
        lambda *_args, **_kwargs: clock,
    )
    monkeypatch.setattr(
        "ml.option_pricing.causal.committed_option_snapshots",
        lambda *_args, **_kwargs: (source_snapshot, observed),
    )
    already = build_live_prediction_inputs(
        tmp_path,
        symbol="GOOG",
        prediction_created_at=target + pd.Timedelta(seconds=20),
        target_snapshot_for=target,
    )
    assert already.status == "TARGET_ALREADY_OBSERVED"

    stale_source = _source_surface("GOOG", target=target)
    stale_source["quote_staleness_seconds"] = 10_000.0
    excluded_snapshot = _committed_stub(
        tmp_path / "excluded",
        symbol="GOOG",
        target=target - pd.Timedelta(minutes=15),
        available=target - pd.Timedelta(minutes=14),
        contracts=stale_source,
    )
    monkeypatch.setattr(
        "ml.option_pricing.causal.committed_option_snapshots",
        lambda *_args, **_kwargs: (excluded_snapshot,),
    )
    excluded = build_live_prediction_inputs(
        tmp_path,
        symbol="GOOG",
        prediction_created_at=target + pd.Timedelta(seconds=20),
        target_snapshot_for=target,
    )
    assert excluded.status == "NO_ELIGIBLE_CONTRACTS"
    assert not excluded.samples.empty
    assert not excluded.samples["sample_status"].eq("AVAILABLE").any()


def test_live_pricing_skips_newer_stale_surface_without_relaxing_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    clock = DecisionClock(
        decision_timestamp=target,
        bar_timestamp=target - pd.Timedelta(minutes=1),
        provider="databento",
        timeframe="1m",
        source_file=tmp_path / "bar.parquet",
    )
    pd.DataFrame(
        {"timestamp": [clock.bar_timestamp], "close": [200.0]}
    ).to_parquet(clock.source_file, index=False)
    valid = _source_surface("GOOG", target=target)
    older = _committed_stub(
        tmp_path / "older-valid",
        symbol="GOOG",
        target=target - pd.Timedelta(minutes=30),
        available=target - pd.Timedelta(minutes=29),
        contracts=valid,
    )
    stale = valid.copy()
    stale["quote_staleness_seconds"] = 10_000.0
    newer = _committed_stub(
        tmp_path / "newer-stale",
        symbol="GOOG",
        target=target - pd.Timedelta(minutes=15),
        available=target - pd.Timedelta(minutes=14),
        contracts=stale,
    )
    monkeypatch.setattr(
        "ml.option_pricing.causal.completed_bar_clock_for_target",
        lambda *_args, **_kwargs: clock,
    )
    monkeypatch.setattr(
        "ml.option_pricing.causal.committed_option_snapshots",
        lambda *_args, **_kwargs: (older, newer),
    )

    batch = build_live_prediction_inputs(
        tmp_path,
        symbol="GOOG",
        prediction_created_at=target + pd.Timedelta(seconds=20),
        target_snapshot_for=target,
    )

    assert batch.status == "READY"
    assert batch.samples["sample_status"].eq("AVAILABLE").any()
    assert batch.samples["source_snapshot_for"].eq(older.snapshot_for).all()
    assert "Skipped 1 newer causal receipt" in batch.reason
    assert newer.contracts_path in batch.source_files
    assert older.contracts_path in batch.source_files


def test_options_timeout_preserves_capture_and_one_target_across_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    captured: list[dict[str, object]] = []

    monkeypatch.setattr(
        "datafetching.options_runtime.completed_bar_clock_for_target",
        lambda *_args, **_kwargs: DecisionClock(
            decision_timestamp=target,
            bar_timestamp=target - pd.Timedelta(minutes=1),
            provider="databento",
            timeframe="1m",
            source_file=tmp_path / "bars.parquet",
        ),
    )

    def persist(*_args: object, **kwargs: object) -> OptionSnapshotOutput:
        captured.append(dict(kwargs))
        return OptionSnapshotOutput(
            tmp_path / "contracts.parquet",
            tmp_path / "features.parquet",
            tmp_path / "raw.parquet",
            1,
            tmp_path / "receipt.json",
            tmp_path / "snapshot",
        )

    monkeypatch.setattr(
        "datafetching.options_runtime.persist_schwab_option_snapshot", persist
    )

    class Session:
        @staticmethod
        def get_option_chain_snapshot(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {"status": "ok"}

    class Clock:
        def __init__(self) -> None:
            self.value = target + pd.Timedelta(minutes=14, seconds=59)

        def __call__(self) -> datetime:
            current = self.value
            self.value += pd.Timedelta(seconds=2)
            return current.to_pydatetime()

    result = run_options_cycle(
        ParquetStore(tmp_path),
        symbols=("GOOG", "NVDA"),
        session=Session(),  # type: ignore[arg-type]
        clock=Clock(),
        target_snapshot_for=target,
        pricing_barrier_timeout_seconds=0.001,
        barrier_sleeper=lambda _seconds: None,
        bar_readiness_mode="exact",
        reporter=None,
    )
    assert result.published == 2
    assert result.pricing_barrier_status == "TIMED_OUT"
    assert {item["clock"].decision_timestamp for item in captured} == {target}
    assert {
        item["pricing_barrier"]["status"] for item in captured
    } == {"TIMED_OUT"}
    assert not any(
        item["pricing_barrier"]["prospective_credit_allowed"] for item in captured
    )


def test_verified_pricing_failure_is_a_noncreditable_options_barrier(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    failure = publish_target_outcome(
        tmp_path,
        target_snapshot_for=target,
        created_at=target + pd.Timedelta(minutes=1),
        symbols=("GOOG",),
        symbol_outcomes={
            "GOOG": {
                "status": "PRICING_FAILED",
                "reason": "deterministic fixture failure",
                "target_snapshot_for": target,
            }
        },
        terminal_status="PRICING_FAILED",
        samples=pd.DataFrame(),
        predictions=pd.DataFrame(),
        bar_readiness=None,
        clock=lambda: target + pd.Timedelta(minutes=1, seconds=1),
    )
    barrier = wait_for_pricing_barrier(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("GOOG",),
        timeout_seconds=0,
        clock=lambda: target + pd.Timedelta(minutes=1, seconds=2),
    )
    metadata = barrier.as_receipt_metadata(
        request_started_at=target + pd.Timedelta(minutes=1, seconds=3)
    )
    assert barrier.status == "VERIFIED"
    assert barrier.terminal_status == "PRICING_FAILED"
    assert metadata["authority_before_request"] is True
    assert metadata["prospective_credit_allowed"] is False
    assert failure.predictions().empty


def test_empty_mixed_terminal_cannot_claim_prospective_credit(tmp_path: Path) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    publication = publish_target_outcome(
        tmp_path,
        target_snapshot_for=target,
        created_at=target + pd.Timedelta(minutes=1),
        symbols=("GOOG", "NVDA"),
        symbol_outcomes={
            "GOOG": {
                "status": "TARGET_ALREADY_OBSERVED",
                "reason": "fixture",
                "target_snapshot_for": target,
            },
            "NVDA": {
                "status": "PRICING_FAILED",
                "reason": "fixture",
                "target_snapshot_for": target,
            },
        },
        terminal_status="MIXED_TERMINAL",
        samples=pd.DataFrame(),
        predictions=pd.DataFrame(),
        bar_readiness=None,
        clock=lambda: target + pd.Timedelta(minutes=1, seconds=1),
    )
    barrier = wait_for_pricing_barrier(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("GOOG", "NVDA"),
        timeout_seconds=0,
        clock=lambda: target + pd.Timedelta(minutes=1, seconds=2),
    )
    metadata = barrier.as_receipt_metadata(
        request_started_at=target + pd.Timedelta(minutes=1, seconds=3)
    )
    assert publication.predictions().empty
    assert barrier.status == "VERIFIED"
    assert barrier.pricing_prediction_rows == 0
    assert metadata["prospective_credit_allowed"] is False


def test_consecutive_target_cycles_accumulate_prospective_evaluations(
    tmp_path: Path,
) -> None:
    targets = (
        pd.Timestamp("2026-08-10T17:00:00Z"),
        pd.Timestamp("2026-08-10T17:15:00Z"),
    )
    snapshots = []
    for target in targets:
        publication = _publish_prediction(tmp_path, symbol="GOOG", target=target)
        barrier = wait_for_pricing_barrier(
            tmp_path,
            target_snapshot_for=target,
            required_symbols=("GOOG",),
            timeout_seconds=0,
            clock=lambda publication=publication: publication.published_at
            + pd.Timedelta(milliseconds=100),
        )
        request = publication.published_at + pd.Timedelta(milliseconds=200)
        snapshots.append(
            _publish_target_snapshot(
                tmp_path,
                publication=publication,
                barrier_metadata=barrier.as_receipt_metadata(
                    request_started_at=request
                ),
                request_started_at=request,
                available_at=publication.published_at + pd.Timedelta(seconds=9),
            )
        )
    predictions = receipt_proven_prediction_rows(tmp_path)
    evaluations = reconcile_predictions(
        predictions,
        snapshots_by_symbol={"GOOG": tuple(snapshots)},
        evaluated_at=targets[-1] + pd.Timedelta(minutes=2),
    )
    assert set(pd.to_datetime(evaluations["target_snapshot_for"], utc=True)) == set(
        targets
    )
    assert evaluations["evaluation_status"].eq("COMPLETE").all()
    assert evaluations["prospective_eligible"].eq(True).all()


def test_missed_pricing_boundaries_publish_explicit_terminal_outcomes(
    tmp_path: Path,
) -> None:
    previous = datetime(2026, 8, 10, 17, 1, tzinfo=timezone.utc)
    next_scheduled = datetime(2026, 8, 10, 17, 46, tzinfo=timezone.utc)
    missed = _missed_boundaries(
        previous,
        next_scheduled,
        interval_minutes=15,
    )
    assert missed == (
        datetime(2026, 8, 10, 17, 16, tzinfo=timezone.utc),
        datetime(2026, 8, 10, 17, 31, tzinfo=timezone.utc),
    )
    publications = [
        _publish_missed_target_outcome(
            tmp_path,
            symbols=("GOOG", "NVDA"),
            target_snapshot_for=pd.Timestamp(boundary).floor("15min"),
            detected_at=pd.Timestamp("2026-08-10T17:40:00Z")
            + pd.Timedelta(seconds=index),
        )
        for index, boundary in enumerate(missed)
    ]
    assert [item.terminal_status for item in publications] == [
        "PRICING_TIMED_OUT",
        "PRICING_TIMED_OUT",
    ]
    assert all(item.predictions().empty for item in publications)
    assert read_target_outcome(
        tmp_path,
        target_snapshot_for="2026-08-10T17:15:00Z",
    ).symbol_outcomes["GOOG"]["status"] == "PRICING_TIMED_OUT"


def test_runtime_event_clocks_are_distinct_and_complete_cycle_is_bounded(
    tmp_path: Path,
) -> None:
    class IncrementingClock:
        def __init__(self) -> None:
            self.value = pd.Timestamp("2026-08-10T17:01:01Z")

        def __call__(self) -> pd.Timestamp:
            current = self.value
            self.value += pd.Timedelta(seconds=1)
            return current

    started = time.perf_counter()
    result = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA",),
        run_timestamp="2026-08-10T17:01:00Z",
        target_snapshot_for="2026-08-10T17:00:00Z",
        runtime_clock=IncrementingClock(),
        bar_readiness_mode="exact",
    )
    elapsed = time.perf_counter() - started
    target_receipt = json.loads(
        (result.target_outcome_directory / "receipt.json").read_text(encoding="utf-8")
    )
    generation_receipt = json.loads(
        (result.run_directory / "publication.json").read_text(encoding="utf-8")
    )
    generation_report = json.loads(
        (result.run_directory / "option-pricing-model-reports.json").read_text(
            encoding="utf-8"
        )
    )
    eligibility_report = json.loads(
        (result.eligibility_report_directory / "eligibility-report.json").read_text(
            encoding="utf-8"
        )
    )
    eligibility_receipt = json.loads(
        (result.eligibility_report_directory / "receipt.json").read_text(
            encoding="utf-8"
        )
    )
    health = json.loads(result.health_path.read_text(encoding="utf-8"))
    clocks = [
        pd.Timestamp(target_receipt["published_at"]),
        pd.Timestamp(generation_report["cycle"]["immutable_files_completed_at"]),
        pd.Timestamp(generation_receipt["published_at"]),
        pd.Timestamp(eligibility_report["generated_at"]),
        pd.Timestamp(eligibility_receipt["published_at"]),
        pd.Timestamp(health["checked_at"]),
    ]
    assert clocks == sorted(clocks)
    assert len(set(clocks)) == len(clocks)
    assert health["elapsed_seconds"] < health["runtime_limits"]["maximum_cycle_seconds"]
    assert elapsed < 30.0
    assert set(health["stage_timings"]) == {
        "preflight_seconds",
        "target_authority_seconds",
        "research_and_generation_prepare_seconds",
        "generation_publication_and_lineage_seconds",
        "post_publication_tail_seconds",
    }
    assert not (tmp_path / "ml" / "latest" / "run.json").exists()
    assert not (tmp_path / "ml" / "strategy-latest" / "run.json").exists()
    assert not (tmp_path / "pools" / "cme").exists()


def test_representative_open_market_inventory_meets_runtime_budget(
    tmp_path: Path,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")
    symbols = (
        "AAPL",
        "AMD",
        "AMZN",
        "GOOG",
        "META",
        "MSFT",
        "NVDA",
        "SNDK",
        "TSLA",
        "TSM",
    )
    source_snapshot_for = target - pd.Timedelta(minutes=15)
    source_available_at = target - pd.Timedelta(minutes=14)
    for symbol in symbols:
        _write_bar(tmp_path, symbol=symbol, target=target, close=200.0)
        common = {
            "symbol": symbol,
            "snapshot_for": source_snapshot_for,
            "available_at": source_available_at,
        }
        contracts = _representative_source_surface(symbol, target=target).assign(
            **common
        )
        publish_option_snapshot(
            tmp_path,
            symbol=symbol,
            raw=pd.DataFrame([{**common, "payload": "representative-fixture"}]),
            contracts=contracts,
            features=pd.DataFrame([{**common, "quality": 1.0}]),
            receipt_published_at=source_available_at,
        )

    started = time.perf_counter()
    result = run_option_pricing_once(
        tmp_path,
        symbols=symbols,
        run_timestamp=target + pd.Timedelta(minutes=1),
        target_snapshot_for=target,
        runtime_clock=lambda: target + pd.Timedelta(minutes=1, seconds=1),
        bar_readiness_mode="exact",
    )
    elapsed = time.perf_counter() - started

    health = json.loads(result.health_path.read_text(encoding="utf-8"))
    assert result.sample_rows == 14_000
    assert result.prediction_rows == 14_000
    assert result.target_outcome_status == "PREDICTIONS_PUBLISHED"
    assert result.stage_timings["target_authority_seconds"] < 45.0
    assert health["elapsed_seconds"] < health["runtime_limits"]["maximum_cycle_seconds"]
    assert elapsed < 120.0


def test_cross_loop_target_ordering_from_loop_a_through_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pd.Timestamp("2026-08-10T17:00:00Z")

    def fetch(
        symbols: tuple[str, ...],
        _store: ParquetStore,
        **_kwargs: object,
    ) -> dict[str, tuple[FetchResult, ...]]:
        for symbol in symbols:
            _write_bar(tmp_path, symbol=symbol, target=target, close=200.0)
        return {symbol: (FetchResult("databento", 1, 0),) for symbol in symbols}

    monkeypatch.setattr(orchestrate, "run_symbols_fetch", fetch)
    assert orchestrate.run_cycle(
        ("GOOG", "NVDA"),
        ParquetStore(tmp_path),
        providers=("databento",),
        requested_profile="continuation",
        include_cme=False,
        include_options=False,
        run_technical_calculations=False,
        run_fundamental_calculations=False,
        run_signal_calculations=False,
        datastore_target=None,
        datastore_path=tmp_path,
        cycle_started_at=target + pd.Timedelta(seconds=20),
        loop_a_generation="loop-a-integrated",
        bar_readiness_clock=lambda: target + pd.Timedelta(seconds=21),
    ) == 0
    readiness = read_bar_readiness(
        tmp_path,
        target_snapshot_for=target,
        required_symbols=("GOOG", "NVDA"),
    )
    assert readiness.loop_a_generation == "loop-a-integrated"

    _publish_source_snapshot(tmp_path, symbol="GOOG", target=target)
    pricing = run_option_pricing_once(
        tmp_path,
        symbols=("GOOG",),
        run_timestamp=target + pd.Timedelta(minutes=1),
        target_snapshot_for=target,
        runtime_clock=lambda: target + pd.Timedelta(minutes=1, seconds=1),
        bar_readiness_mode="required",
    )
    assert pricing.target_outcome_status == "PREDICTIONS_PUBLISHED"
    target_publication = read_target_outcome(
        tmp_path,
        target_snapshot_for=target,
    )

    published_snapshots = []

    def persist(*_args: object, **kwargs: object) -> OptionSnapshotOutput:
        snapshot = _publish_target_snapshot(
            tmp_path,
            publication=target_publication,
            barrier_metadata=dict(kwargs["pricing_barrier"]),
            request_started_at=pd.Timestamp(kwargs["quote_cutoff_at"]),
            available_at=pd.Timestamp(kwargs["fetched_at"]),
        )
        published_snapshots.append(snapshot)
        return OptionSnapshotOutput(
            snapshot.contracts_path,
            snapshot.features_path,
            snapshot.raw_path,
            len(pd.read_parquet(snapshot.contracts_path)),
            snapshot.receipt_path,
            snapshot.directory,
        )

    monkeypatch.setattr(
        "datafetching.options_runtime.persist_schwab_option_snapshot", persist
    )

    class Session:
        @staticmethod
        def get_option_chain_snapshot(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {"status": "ok"}

    class OptionsClock:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> datetime:
            values = (
                target + pd.Timedelta(minutes=1, seconds=2),
                target + pd.Timedelta(minutes=1, seconds=2),
                target + pd.Timedelta(minutes=1, seconds=2, milliseconds=50),
                target + pd.Timedelta(minutes=1, seconds=2, milliseconds=100),
                target + pd.Timedelta(minutes=1, seconds=10),
            )
            value = values[min(self.calls, len(values) - 1)]
            self.calls += 1
            return value.to_pydatetime()

    options = run_options_cycle(
        ParquetStore(tmp_path),
        symbols=("GOOG",),
        session=Session(),  # type: ignore[arg-type]
        clock=OptionsClock(),
        target_snapshot_for=target,
        pricing_barrier_timeout_seconds=0,
        reporter=None,
    )
    assert options.published == 1
    assert options.pricing_barrier_status == "VERIFIED"
    proven = receipt_proven_prediction_rows(tmp_path)
    evaluated = reconcile_predictions(
        proven,
        snapshots_by_symbol={"GOOG": tuple(published_snapshots)},
        evaluated_at=target + pd.Timedelta(minutes=2),
    )
    assert evaluated["evaluation_status"].eq("COMPLETE").all()
    assert evaluated["prospective_eligible"].eq(True).all()


def test_pricing_chain_verification_is_bounded_at_representative_depth(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "ml" / "option-pricing-runs"
    runs.mkdir(parents=True)
    previous = None
    base = pd.Timestamp("2026-01-02T14:01:00Z")
    for index in range(120):
        timestamp = base + pd.Timedelta(minutes=15 * index)
        run = runs / f"historical-{index:03d}"
        run.mkdir()
        manifest = {
            "run_timestamp": timestamp.isoformat(),
            "output_files": {},
            "configuration": {},
        }
        (run / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        receipt = {
            "schema_version": OPTION_PRICING_PUBLICATION_VERSION,
            "run_path": run.relative_to(tmp_path).as_posix(),
            "run_timestamp": timestamp.isoformat(),
            "published_at": (timestamp + pd.Timedelta(seconds=1)).isoformat(),
            "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
            "previous_publication": previous,
        }
        (run / "publication.json").write_text(
            json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
        )
        previous = {
            "run_path": receipt["run_path"],
            "run_timestamp": receipt["run_timestamp"],
            "published_at": receipt["published_at"],
            "manifest_checksum_sha256": receipt["manifest_checksum_sha256"],
            "receipt_checksum_sha256": file_checksum(run / "publication.json"),
        }

    current_time = base + pd.Timedelta(minutes=15 * 120)
    current = runs / "current"
    current.mkdir()
    outputs = {
        "pricing-samples.parquet": OPTION_PRICING_SAMPLE_SCHEMA,
        "pricing-predictions.parquet": OPTION_PRICING_PREDICTION_SCHEMA,
        "pricing-evaluations.parquet": OPTION_PRICING_EVALUATION_SCHEMA,
        "pricing-surfaces.parquet": OPTION_PRICING_SURFACE_SCHEMA,
        "pricing-monitoring.parquet": OPTION_PRICING_MONITORING_SCHEMA,
    }
    for name, schema in outputs.items():
        write_parquet_with_schema(empty_frame(schema), current / name, schema)
    report_name = "option-pricing-model-reports.json"
    (current / report_name).write_text(
        json.dumps({"automated_action_allowed": False}) + "\n", encoding="utf-8"
    )
    write_manifest(
        current,
        run_timestamp=current_time,
        input_files=(),
        output_files=(*outputs, report_name),
        configuration={
            "publication_contract": {
                "version": OPTION_PRICING_PUBLICATION_VERSION,
                "authority": "ml/option-pricing-latest/run.json",
                "schema_validation": True,
                "automated_action_allowed": False,
            }
        },
        datastore_root=tmp_path,
    )
    receipt = {
        "schema_version": OPTION_PRICING_PUBLICATION_VERSION,
        "run_path": current.relative_to(tmp_path).as_posix(),
        "run_timestamp": current_time.isoformat(),
        "published_at": (current_time + pd.Timedelta(seconds=1)).isoformat(),
        "manifest_checksum_sha256": file_checksum(current / "manifest.json"),
        "previous_publication": previous,
    }
    (current / "publication.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    pointer = {
        "schema_version": "option-pricing-pointer-v1",
        "current": {
            "run_path": receipt["run_path"],
            "run_timestamp": receipt["run_timestamp"],
            "published_at": receipt["published_at"],
            "manifest_checksum_sha256": receipt["manifest_checksum_sha256"],
            "receipt_checksum_sha256": file_checksum(current / "publication.json"),
        },
    }
    pointer_path = tmp_path / "ml" / "option-pricing-latest" / "run.json"
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text(json.dumps(pointer) + "\n", encoding="utf-8")

    started = time.perf_counter()
    publication = read_current_option_pricing_publication(tmp_path)
    elapsed = time.perf_counter() - started
    assert publication.run_directory == current.resolve()
    assert elapsed < 10.0


def _write_bar(root: Path, *, symbol: str, target: pd.Timestamp, close: float) -> None:
    directory = (
        root
        / "stocks"
        / symbol
        / "bars"
        / "1m"
        / "databento"
        / "normalized"
    )
    directory.mkdir(parents=True, exist_ok=True)
    write_normalized_bar_parquet(
        pd.DataFrame(
            {
                "timestamp": [target - pd.Timedelta(minutes=1)],
                "open": [close - 1.0],
                "high": [close + 1.0],
                "low": [close - 2.0],
                "close": [close],
                "volume": [1000.0],
            }
        ),
        directory / "bars.parquet",
    )


def _publish_prediction(
    root: Path,
    *,
    symbol: str,
    target: pd.Timestamp,
    created_offset_seconds: float = 60.0,
    published_offset_seconds: float = 61.0,
):
    samples = build_causal_samples(
        _source_surface(symbol, target=target),
        target_contracts=None,
        target_underlying_price=200.0,
        source_snapshot_for=target - pd.Timedelta(minutes=15),
        source_available_at=target - pd.Timedelta(minutes=14),
        target_snapshot_for=target,
        source_provider="schwab",
        prediction_mode="LIVE",
    )
    created = target + pd.Timedelta(seconds=created_offset_seconds)
    predictions = create_prediction_rows(
        samples,
        prediction_created_at=created,
        prediction_available_at=created,
    )
    return publish_target_outcome(
        root,
        target_snapshot_for=target,
        created_at=created,
        symbols=(symbol,),
        symbol_outcomes={
            symbol: {
                "status": "READY",
                "reason": "",
                "target_snapshot_for": target,
            }
        },
        terminal_status="PREDICTIONS_PUBLISHED",
        samples=samples,
        predictions=predictions,
        bar_readiness=None,
        clock=lambda: target + pd.Timedelta(seconds=published_offset_seconds),
    )


def _publish_target_snapshot(
    root: Path,
    *,
    publication: object,
    barrier_metadata: dict[str, object],
    request_started_at: pd.Timestamp,
    available_at: pd.Timestamp,
):
    predictions = publication.predictions()
    prediction = predictions.iloc[0]
    target = publication.target_snapshot_for
    common = {
        "symbol": str(prediction["symbol"]),
        "snapshot_for": target,
        "available_at": available_at,
    }
    raw = pd.DataFrame([{**common, "payload": "fixture"}])
    contracts = pd.DataFrame(
        [
            {
                **common,
                "contract_symbol": row["contract_symbol"],
                "call_put": row["call_put"],
                "expiration_date": row["expiration_date"],
                "strike": row["strike"],
                "multiplier": row["multiplier"],
                "bid": 9.9,
                "ask": 10.1,
                "quote_timestamp": publication.published_at
                + pd.Timedelta(seconds=1),
            }
            for _, row in predictions.iterrows()
        ]
    )
    features = pd.DataFrame([{**common, "quality": 1.0}])
    return publish_option_snapshot(
        root,
        symbol=str(prediction["symbol"]),
        raw=raw,
        contracts=contracts,
        features=features,
        request_started_at=request_started_at,
        pricing_barrier=barrier_metadata,
        receipt_published_at=available_at,
    )


def _publish_source_snapshot(
    root: Path,
    *,
    symbol: str,
    target: pd.Timestamp,
):
    snapshot_for = target - pd.Timedelta(minutes=15)
    available_at = target - pd.Timedelta(minutes=14)
    common = {
        "symbol": symbol,
        "snapshot_for": snapshot_for,
        "available_at": available_at,
    }
    contracts = _source_surface(symbol, target=target).assign(**common)
    return publish_option_snapshot(
        root,
        symbol=symbol,
        raw=pd.DataFrame([{**common, "payload": "source"}]),
        contracts=contracts,
        features=pd.DataFrame([{**common, "quality": 1.0}]),
        receipt_published_at=available_at,
    )


def _source_surface(symbol: str, *, target: pd.Timestamp) -> pd.DataFrame:
    expiration = target + pd.Timedelta(days=60)
    rows = []
    for strike in (180.0, 190.0, 200.0, 210.0, 220.0):
        years = 60.0 / 365.0
        price = black_scholes_price(200.0, strike, 0.04, 0.28, years, 0.01, "CALL")
        rows.append(
            {
                "symbol": symbol,
                "contract_symbol": f"{symbol}-{target.value}-C-{strike:g}",
                "call_put": "CALL",
                "expiration_date": expiration,
                "strike": strike,
                "underlying_price": 200.0,
                "bid": max(price - 0.05, 0.01),
                "ask": price + 0.05,
                "multiplier": 100.0,
                "mini": False,
                "non_standard": False,
                "interest_rate": 0.04,
                "dividend_yield": 0.01,
                "implied_volatility": 0.28,
                "quote_staleness_seconds": 60.0,
                "quote_timestamp": target - pd.Timedelta(minutes=16),
            }
        )
    return pd.DataFrame(rows)


def _representative_source_surface(
    symbol: str,
    *,
    target: pd.Timestamp,
) -> pd.DataFrame:
    rows = []
    for day in (15, 30, 45, 60, 75, 90, 105):
        expiration = target + pd.Timedelta(days=day)
        for call_put in ("CALL", "PUT"):
            for index in range(100):
                strike = 160.0 + index * 0.8
                rows.append(
                    {
                        "symbol": symbol,
                        "contract_symbol": (
                            f"{symbol}-{target.value}-{day}-{call_put[0]}-{index}"
                        ),
                        "call_put": call_put,
                        "expiration_date": expiration,
                        "strike": strike,
                        "underlying_price": 200.0,
                        "bid": 9.95,
                        "ask": 10.05,
                        "multiplier": 100.0,
                        "mini": False,
                        "non_standard": False,
                        "interest_rate": 0.04,
                        "dividend_yield": 0.01,
                        "implied_volatility": 0.28,
                        "quote_staleness_seconds": 60.0,
                        "quote_timestamp": target - pd.Timedelta(minutes=16),
                    }
                )
    return pd.DataFrame(rows)


def _committed_stub(
    directory: Path,
    *,
    symbol: str,
    target: pd.Timestamp,
    available: pd.Timestamp,
    contracts: pd.DataFrame,
):
    from options.publication import CommittedOptionSnapshot

    directory.mkdir(parents=True)
    contracts_path = directory / "contracts.parquet"
    contracts.to_parquet(contracts_path, index=False)
    receipt = directory / "receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    return CommittedOptionSnapshot(
        symbol=symbol,
        snapshot_for=target,
        available_at=available,
        directory=directory,
        raw_path=contracts_path,
        contracts_path=contracts_path,
        features_path=contracts_path,
        receipt_path=receipt,
        receipt={},
    )
