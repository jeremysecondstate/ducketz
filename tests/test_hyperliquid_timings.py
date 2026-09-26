"""Timing reports preserve attempt identity and measured stage boundaries."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from ml import hyperliquid_timings as timings


NOW = "2026-09-26T04:00:00+00:00"
CLOSE = "2026-09-26T03:00:00+00:00"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_events(path, events, suffix=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events) + suffix, encoding="utf-8")


def data_summary(root, coin="BTC", run_id="data-1", **updates):
    value = {
        "coin": coin, "interval": "15m", "run_id": run_id,
        "fetched_at_utc": "2026-09-26T03:00:05+00:00", "last_close_utc": CLOSE,
        "previous_rows": 5000, "rows": 5001, "new_candles": 1, "feature_count": 38,
        "timings_seconds": {"fetch_and_normalize": 0.3, "feature_build": 0.1,
                            "parquet_and_catalog_write": 0.05, "total_before_publish": 0.5},
    }
    value.update(updates)
    write_json(root / coin / "15m" / "runs" / run_id / "summary.json", value)
    return value


def data_event(summary, *, cycle=1, status="published", total=1.0, start="2026-09-26T03:00:05+00:00",
               end="2026-09-26T03:00:06+00:00"):
    return {
        "event": "cycle", "instance_id": "data-worker", "cycle_count": cycle, "at_utc": end,
        "last_result": {"status": status, "run_id": summary["run_id"], "last_close_utc": CLOSE,
                        "new_candles": 1, "rows": 5001},
        "last_cycle_timing": {"started_at_utc": start, "finished_at_utc": end,
                              "total_seconds": total, "queue_wait_seconds": 0.4,
                              "outcome": "success", "result_status": status,
                              "timings_seconds": summary["timings_seconds"]},
    }


def model_report(root, coin="BTC", model_id="model-1"):
    report = {
        "coin": coin, "interval": "15m", "horizon_bars": 4, "data_run_id": "data-1",
        "feature_count": 38, "model_names": ["linear", "tree"], "mature_usable_rows": 4900,
        "eligible": True, "total_seconds": 3.5,
        "model_timings": {
            "linear": {"fit_seconds": 0.2, "calibration_seconds": 0.1, "assessment_seconds": 0.3},
            "tree": {"fit_seconds": 1.2, "calibration_seconds": 0.4, "assessment_seconds": 0.5},
        },
    }
    folder = root / "_models" / coin / "15m" / "h4" / "runs" / model_id
    write_json(folder / "report.json", report)
    write_json(folder / "record.json", {"model_id": model_id, "trained_at_utc": "2026-09-26T03:01:06+00:00",
                                       "source_last_close_utc": CLOSE})
    return report


def model_event(report, *, model_id="model-1", outcome="promoted", start="2026-09-26T03:01:00+00:00"):
    return {
        "event": "training_completed", "instance_id": "model-worker", "at_utc": "2026-09-26T03:01:06+00:00",
        "coin": report["coin"], "interval": "15m", "horizon_bars": 4, "model_id": model_id,
        "source_run_id": report["data_run_id"], "outcome": outcome,
        "report_total_seconds": report["total_seconds"], "model_timings": report["model_timings"],
        "timing": {"submitted_at_utc": start, "completed_at_utc": "2026-09-26T03:01:06+00:00",
                   "worker_seconds": 3.6, "scheduler_observed_seconds": 5.5,
                   "queue_wait_seconds": 0.2, "collection_wait_seconds": 1.7,
                   "publication_seconds": 0.1, "operation_seconds": 3.7, "end_to_end_seconds": 5.6},
    }


def test_published_snapshot_and_duplicate_cycle_join_once_without_losing_work(tmp_path):
    summary = data_summary(tmp_path)
    event = data_event(summary)
    write_events(tmp_path / "BTC/15m/loop_events.jsonl", [event, event, {**event, "event": "stopped"}])
    warnings = []
    rows = timings.collect_data(tmp_path, warnings)
    assert warnings == []
    assert len(rows) == 1
    assert rows[0]["mode"] == "update"
    assert rows[0]["before_publish_seconds"] == 0.5
    assert rows[0]["cycle_seconds"] == 1.0
    assert rows[0]["queue_wait_seconds"] == 0.4
    assert rows[0]["feature_seconds"] == 0.1


def test_unchanged_and_failed_attempts_do_not_reuse_prior_snapshot_duration(tmp_path):
    summary = data_summary(tmp_path)
    published = data_event(summary)
    old_unchanged = {**data_event(summary, cycle=2, status="unchanged")}
    old_unchanged.pop("last_cycle_timing")
    unchanged = data_event(summary, cycle=3, status="unchanged", total=0.4)
    unchanged["last_cycle_timing"]["timings_seconds"] = {
        "fetch_and_normalize": 0.2, "feature_build": 0.0, "parquet_and_catalog_write": 0.0,
        "total_before_return": 0.3,
    }
    failed = {"event": "error", "instance_id": "data-worker", "cycle_count": 4,
              "last_result": published["last_result"],  # Even an old stale result cannot identify this attempt.
              "last_cycle_timing": {"total_seconds": 2.0, "outcome": "failed", "timings_seconds": {},
                                    "started_at_utc": "2026-09-26T03:02:00+00:00",
                                    "finished_at_utc": "2026-09-26T03:02:02+00:00"}}
    write_events(tmp_path / "BTC/15m/loop_events.jsonl", [published, old_unchanged, unchanged, failed])
    rows = timings.collect_data(tmp_path, [])
    assert len(rows) == 3
    by_mode = {row["mode"]: row for row in rows}
    assert by_mode["update"]["feature_seconds"] == 0.1
    assert by_mode["unchanged"]["feature_seconds"] == 0
    assert by_mode["unchanged"].get("before_publish_seconds") is None
    assert by_mode["unchanged"]["unchanged_check_seconds"] == 0.3
    assert by_mode["failed"]["run_id"] is None
    assert by_mode["failed"]["cycle_seconds"] == 2
    assert by_mode["failed"].get("feature_seconds") is None


def test_published_event_without_readable_summary_is_not_an_unchanged_check(tmp_path):
    summary = {"run_id": "data-unreadable", "timings_seconds": {"feature_build": 0.2}}
    write_events(tmp_path / "BTC/15m/loop_events.jsonl", [data_event(summary)])
    rows = timings.collect_data(tmp_path, [])
    assert len(rows) == 1
    assert rows[0]["outcome"] == "published"
    assert rows[0]["mode"] != "unchanged"


def test_batch_requires_every_configured_symbol_and_uses_parallel_wall_span(tmp_path):
    write_json(tmp_path / "_coordinator/coordinator_status.json", {"symbols": ["BTC", "ETH", "HYPE", "ZEC"]})
    for coin in ("BTC", "ETH", "HYPE"):
        summary = data_summary(tmp_path, coin)
        write_events(tmp_path / coin / "15m/loop_events.jsonl", [data_event(summary, total=2,
                     end="2026-09-26T03:00:07+00:00")])
    _, partial = timings.build_report(tmp_path, now=NOW)
    assert partial["complete_batch_spans"] == []
    summary = data_summary(tmp_path, "ZEC")
    write_events(tmp_path / "ZEC/15m/loop_events.jsonl", [data_event(summary, total=2,
                 start="2026-09-26T03:00:06+00:00", end="2026-09-26T03:00:08+00:00")])
    _, complete = timings.build_report(tmp_path, now=NOW)
    assert len(complete["complete_batch_spans"]) == 1
    assert complete["complete_batch_spans"][0]["wall_span_seconds"] == 3  # Not sum of four 2s attempts.


def test_model_report_and_duplicate_success_event_merge_and_sum_stages_once(tmp_path):
    report = model_report(tmp_path)
    event = model_event(report)
    write_events(tmp_path / "_models/_runtime/training_events.jsonl", [event, event])
    rows = timings.collect_models(tmp_path, [])
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_build_seconds"] == 3.5
    assert row["fit_seconds"] == pytest.approx(1.4)
    assert row["calibration_seconds"] == 0.5
    assert row["assessment_seconds"] == 0.8
    assert row["operation_seconds"] == 3.7
    assert row["end_to_end_seconds"] == 5.6
    assert row["source_close_utc"] == CLOSE


def test_duplicate_failed_model_event_counts_once_but_distinct_attempts_remain(tmp_path):
    report = model_report(tmp_path)
    success = model_event(report)
    failed = model_event(report, model_id=None, outcome="training_failed", start="2026-09-26T03:02:00+00:00")
    failed["report_total_seconds"] = None
    failed["model_timings"] = None
    failed["timing"].update(worker_seconds=0.25, publication_seconds=None, operation_seconds=None,
                            completed_at_utc="2026-09-26T03:02:01+00:00")
    second_failure = {**failed, "timing": {**failed["timing"], "submitted_at_utc": "2026-09-26T03:03:00+00:00",
                                          "completed_at_utc": "2026-09-26T03:03:01+00:00"}}
    write_events(tmp_path / "_models/_runtime/training_events.jsonl", [success, failed, failed, second_failure])
    rows = timings.collect_models(tmp_path, [])
    assert len(rows) == 3
    failures = [row for row in rows if row["mode"] == "failed_or_discarded"]
    assert len(failures) == 2
    assert all(row.get("candidate_build_seconds") is None for row in failures)
    assert all(row["worker_seconds"] == 0.25 for row in failures)
    assert all(row["run_id"] is None for row in failures)


def test_partial_model_event_preserves_immutable_report_stage_metrics(tmp_path):
    report = model_report(tmp_path)
    event = model_event(report)
    event.pop("model_timings")
    event.pop("report_total_seconds")
    write_events(tmp_path / "_models/_runtime/training_events.jsonl", [event])
    row, = timings.collect_models(tmp_path, [])
    assert row["candidate_build_seconds"] == 3.5
    assert row["fit_seconds"] == pytest.approx(1.4)


def test_summary_statistics_keep_modes_and_missing_measurements_separate():
    frame = pd.DataFrame([
        {"kind": "data", "coin": "BTC", "interval": "15m", "horizon_bars": None,
         "mode": mode, "feature_seconds": duration, "cycle_seconds": cycle}
        for mode, duration, cycle in [("update", 1.0, None), ("update", 3.0, 5.0),
                                       ("unchanged", 0.0, 0.2), ("failed", None, 10.0)]
    ])
    grouped = {row["mode"]: row for row in timings.summarize(frame)}
    assert grouped["update"]["runs"] == 2
    values = grouped["update"]["metrics"]["feature_seconds"]
    assert values == {"n": 2, "latest": 3.0, "median": 2.0, "p95": 2.9, "max": 3.0}
    assert grouped["update"]["metrics"]["cycle_seconds"]["n"] == 1
    assert "feature_seconds" not in grouped["failed"]["metrics"]
    assert grouped["unchanged"]["metrics"]["feature_seconds"]["median"] == 0


def test_empty_root_and_incomplete_jsonl_are_safe_and_report_read_warnings(tmp_path):
    frame, summary = timings.build_report(tmp_path, now=NOW)
    assert frame.empty
    assert summary["record_count"] == 0
    assert summary["complete_batch_spans"] == []
    assert "no complete cohort" in timings.markdown(summary)
    report = data_summary(tmp_path)
    write_events(tmp_path / "BTC/15m/loop_events.jsonl", [data_event(report)], suffix='[]\n{"event":')
    frame, summary = timings.build_report(tmp_path, now=NOW)
    assert len(frame) == 1
    assert len(summary["warnings"]) == 2
    assert all("incomplete/invalid event" in warning for warning in summary["warnings"])


def test_mixed_iso_timestamp_precision_does_not_drop_valid_records(tmp_path):
    data_summary(tmp_path, "BTC", fetched_at_utc="2026-09-26T03:00:05+00:00")
    data_summary(tmp_path, "ETH", fetched_at_utc="2026-09-26T03:00:05.125000+00:00")
    frame, summary = timings.build_report(tmp_path, now=NOW)
    assert len(frame) == 2
    assert summary["warnings"] == []


def test_backward_wall_clock_cycle_is_excluded_from_batch_span():
    frame = pd.DataFrame([
        {"kind": "data", "coin": coin, "interval": "15m", "horizon_bars": None,
         "source_close_utc": CLOSE, "outcome": "published", "started_at_utc": start,
         "finished_at_utc": end}
        for coin, start, end in [
            ("BTC", "2026-09-26T03:00:05+00:00", "2026-09-26T03:00:06+00:00"),
            ("ETH", "2026-09-26T03:00:05+00:00", "2026-09-26T03:00:04+00:00"),
        ]
    ])
    assert timings.batch_spans(frame, ["BTC", "ETH"]) == []


def test_complete_batch_accepts_mixed_iso_timestamp_precision():
    frame = pd.DataFrame([
        {"kind": "data", "coin": coin, "interval": "15m", "horizon_bars": None,
         "source_close_utc": CLOSE, "outcome": "published", "started_at_utc": start,
         "finished_at_utc": end}
        for coin, start, end in [
            ("BTC", "2026-09-26T03:00:05+00:00", "2026-09-26T03:00:06+00:00"),
            ("ETH", "2026-09-26T03:00:05.125000+00:00", "2026-09-26T03:00:06.250000+00:00"),
        ]
    ])
    spans = timings.batch_spans(frame, ["BTC", "ETH"])
    assert len(spans) == 1
    assert spans[0]["wall_span_seconds"] == 1.25


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), "0.5"])
def test_invalid_stage_values_are_missing_instead_of_zero(value):
    metrics = timings._model_metrics({"total_seconds": value, "model_timings": {
        "first": {"fit_seconds": 1.0}, "second": {"fit_seconds": value}}})
    assert metrics["candidate_build_seconds"] is None
    assert metrics["fit_seconds"] is None
