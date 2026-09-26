"""Inspect persisted Hyperliquid work timings without fetching, fitting or trading.

Run ``python -m ml.hyperliquid_timings --export`` for a Markdown/JSON summary
and a flat Parquet under the datastore's _timings directory. Source journals
continue recording independently; these exports are point-in-time views.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import pandas as pd

from ml.hyperliquid_data_loop import _atomic_json
from ml.hyperliquid_data_pipeline import DEFAULT_OUTPUT_ROOT
from ml.hyperliquid_model_artifacts import _atomic_parquet


def _read(path: Path, warnings: list[str]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value
    except (OSError, ValueError) as exc:
        warnings.append(f"Skipped {path}: {type(exc).__name__}")
        return {}


def _events(path: Path, warnings: list[str]):
    if not path.exists():
        return
    try:
        with path.open(encoding="utf-8") as stream:
            for index, line in enumerate(stream, 1):
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("Expected an event object")
                    yield value
                except ValueError:
                    # A running writer may have appended only part of its last line.
                    warnings.append(f"Skipped incomplete/invalid event {path}:{index}")
    except OSError as exc:
        warnings.append(f"Skipped {path}: {type(exc).__name__}")


def _seconds(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _mode(summary):
    if summary.get("benchmark"):
        return "benchmark"
    if summary.get("previous_rows") == 0:
        return "onboarding"
    if summary.get("new_candles", 0) > 1:
        return "catchup"
    if summary.get("new_candles", 0) == 1:
        return "update"
    return "rebuild_or_correction"


def _data_metrics(timing):
    return {
        "fetch_seconds": _seconds(timing.get("fetch_and_normalize")),
        "feature_seconds": _seconds(timing.get("feature_build")),
        "parquet_write_seconds": _seconds(timing.get("parquet_and_catalog_write")),
        "before_publish_seconds": _seconds(timing.get("total_before_publish")),
        "unchanged_check_seconds": _seconds(timing.get("total_before_return")),
    }


def collect_data(root: Path, warnings: list[str]) -> list[dict]:
    records = {}
    for path in sorted(root.glob("*/*/runs/*/summary.json")):
        item = _read(path, warnings)
        if not item or not item.get("run_id"):
            continue
        key = (item.get("coin"), item.get("interval"), item["run_id"])
        records[key] = {
            "kind": "data", "coin": key[0], "interval": key[1], "run_id": key[2],
            "at_utc": item.get("fetched_at_utc"), "source_close_utc": item.get("last_close_utc"),
            "outcome": "published", "mode": _mode(item), "rows": item.get("rows"),
            "feature_count": item.get("feature_count"), "new_candles": item.get("new_candles"),
            **_data_metrics(item.get("timings_seconds", {})),
        }
    for path in sorted(root.glob("*/*/loop_events.jsonl")):
        coin, interval = path.parent.parent.name, path.parent.name
        seen = set()
        for event in _events(path, warnings):
            if event.get("event") not in {"cycle", "error"}:
                continue
            event_key = (event.get("instance_id"), event.get("cycle_count"))
            if event_key in seen:
                continue
            seen.add(event_key)
            last = event.get("last_result") or {}
            timing = event.get("last_cycle_timing") or {}
            succeeded = event["event"] == "cycle"
            published = succeeded and last.get("status") == "published"
            key = (coin, interval, last.get("run_id")) if published else (coin, interval, *event_key)
            row = records.get(key)
            # Old unchanged/error events contain no usable duration. Do not
            # reuse timings from their previous snapshot or invent zero work.
            if row is None and not timing:
                continue
            if row is None:
                row = {"kind": "data", "coin": coin, "interval": interval,
                       "run_id": last.get("run_id") if succeeded else None,
                       "mode": (_mode(last) if published else "unchanged") if succeeded else "failed",
                       "outcome": last.get("status") if succeeded else "failed",
                       "source_close_utc": last.get("last_close_utc") if succeeded else None,
                       "rows": last.get("rows") if succeeded else None,
                       "new_candles": last.get("new_candles") if succeeded else None}
                records[key] = row
            if published and last.get("history_reconciliation") and row["mode"] not in {"onboarding", "benchmark"}:
                row["mode"] = "reconciliation"
            if timing:
                row.update({k: v for k, v in _data_metrics(timing.get("timings_seconds", {})).items() if v is not None})
                row.update({"at_utc": timing.get("finished_at_utc") or event.get("at_utc"),
                            "started_at_utc": timing.get("started_at_utc"),
                            "finished_at_utc": timing.get("finished_at_utc"),
                            "cycle_seconds": _seconds(timing.get("total_seconds")),
                            "queue_wait_seconds": _seconds(timing.get("queue_wait_seconds"))})
    return list(records.values())


def _model_metrics(report):
    stages = report.get("model_timings", {})
    result = {"candidate_build_seconds": _seconds(report.get("total_seconds"))}
    for stage in ("fit", "calibration", "assessment"):
        values = [_seconds(value.get(f"{stage}_seconds")) for value in stages.values()]
        result[f"{stage}_seconds"] = sum(values) if values and all(v is not None for v in values) else None
    return result


def collect_models(root: Path, warnings: list[str]) -> list[dict]:
    records = {}
    for path in sorted((root / "_models").glob("*/*/h*/runs/*/report.json")):
        report = _read(path, warnings)
        record_path = path.with_name("record.json")
        if not record_path.exists():
            continue  # Publication may still be in progress.
        record = _read(record_path, warnings)
        if not report or not record:
            continue
        model_id = record.get("model_id", path.parent.name)
        key = (report.get("coin"), report.get("interval"), report.get("horizon_bars"), model_id)
        records[key] = {
            "kind": "model", "coin": key[0], "interval": key[1], "horizon_bars": key[2],
            "run_id": model_id, "data_run_id": report.get("data_run_id"),
            "at_utc": record.get("trained_at_utc"), "source_close_utc": record.get("source_last_close_utc"),
            "mode": "candidate", "outcome": "eligible" if report.get("eligible") else "research_only",
            "feature_count": report.get("feature_count"), "model_count": len(report.get("model_names", [])),
            "rows": report.get("mature_usable_rows"), **_model_metrics(report),
        }
    path = root / "_models" / "_runtime" / "training_events.jsonl"
    seen = set()
    for event in _events(path, warnings):
        timing = event.get("timing") or {}
        if not timing:
            continue
        identity = (event.get("instance_id"), timing.get("submitted_at_utc"),
                    event.get("source_run_id"), event.get("coin"), event.get("interval"), event.get("horizon_bars"))
        if identity in seen:
            continue
        seen.add(identity)
        key = (event.get("coin"), event.get("interval"), event.get("horizon_bars"),
               event.get("model_id") or identity)
        row = records.setdefault(key, {"kind": "model", "coin": key[0], "interval": key[1],
                                      "horizon_bars": key[2], "run_id": event.get("model_id"),
                                      "data_run_id": event.get("source_run_id"), "mode": "candidate"})
        row.update({"at_utc": event.get("at_utc"), "outcome": event.get("outcome"),
                    "started_at_utc": timing.get("submitted_at_utc"),
                    "finished_at_utc": timing.get("completed_at_utc")})
        metrics = _model_metrics({"total_seconds": event.get("report_total_seconds"),
                                  "model_timings": event.get("model_timings") or {}})
        row.update({k: v for k, v in metrics.items() if v is not None})
        for name in ("worker_seconds", "scheduler_observed_seconds", "queue_wait_seconds",
                     "collection_wait_seconds", "publication_seconds", "operation_seconds", "end_to_end_seconds"):
            row[name] = _seconds(timing.get(name))
        if row["outcome"] not in {"promoted", "research_only", "eligible"}:
            row["mode"] = "failed_or_discarded"
    return list(records.values())


def summarize(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    groups = []
    for (kind, coin, interval, horizon, mode), part in frame.groupby(
        ["kind", "coin", "interval", "horizon_bars", "mode"], dropna=False, sort=True
    ):
        metrics = {}
        for column in frame.columns:
            if not column.endswith("_seconds"):
                continue
            values = pd.to_numeric(part[column], errors="coerce").dropna()
            if values.empty:
                continue
            metrics[column] = {"n": len(values), "latest": float(values.iloc[-1]),
                               "median": float(values.median()), "p95": float(values.quantile(.95)),
                               "max": float(values.max())}
        groups.append({"kind": kind, "coin": coin, "interval": interval,
                       "horizon_bars": None if pd.isna(horizon) else int(horizon),
                       "mode": mode, "runs": len(part), "metrics": metrics})
    return groups


def batch_spans(frame: pd.DataFrame, symbols: list[str]) -> list[dict]:
    """Elapsed spans of complete observed cohorts, never sums of parallel work."""
    if not symbols or frame.empty or "started_at_utc" not in frame:
        return []
    records = []
    successful = frame[frame["outcome"].isin(["published", "promoted", "research_only", "eligible"])]
    for (kind, interval, horizon, close), part in successful.groupby(
        ["kind", "interval", "horizon_bars", "source_close_utc"], dropna=False
    ):
        if pd.isna(close):
            continue
        part = part[part["coin"].isin(symbols)].drop_duplicates("coin", keep="last")
        if set(part["coin"]) != set(symbols):
            continue
        starts = pd.to_datetime(part["started_at_utc"], utc=True, errors="coerce", format="ISO8601")
        ends = pd.to_datetime(part["finished_at_utc"], utc=True, errors="coerce", format="ISO8601")
        if starts.isna().any() or ends.isna().any() or (ends < starts).any():
            continue
        span = (ends.max() - starts.min()).total_seconds()
        records.append({"kind": kind, "interval": interval,
                        "horizon_bars": None if pd.isna(horizon) else int(horizon),
                        "source_close_utc": close, "symbols": symbols,
                        "started_at_utc": starts.min().isoformat(), "finished_at_utc": ends.max().isoformat(),
                        "wall_span_seconds": span})
    return records


def build_report(output_root=DEFAULT_OUTPUT_ROOT, *, hours=24.0, now=None):
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be finite and positive")
    root = Path(output_root).resolve()
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    warnings = []
    rows = collect_data(root, warnings) + collect_models(root, warnings)
    frame = pd.DataFrame(rows)
    for name in ("kind", "coin", "interval", "horizon_bars", "mode", "outcome", "source_close_utc", "at_utc"):
        if name not in frame:
            frame[name] = None
    dates = pd.to_datetime(frame["at_utc"], utc=True, errors="coerce", format="ISO8601")
    missing = int(dates.isna().sum())
    if missing:
        warnings.append(f"Omitted {missing} timing records with no valid observation timestamp")
    frame = frame.loc[(dates >= now - pd.Timedelta(hours=hours)) & (dates <= now)].copy()
    frame["at_utc"] = dates.loc[frame.index].map(lambda x: x.isoformat())
    frame = frame.sort_values("at_utc", kind="stable").reset_index(drop=True)
    status_path = root / "_coordinator" / "coordinator_status.json"
    status = _read(status_path, warnings) if status_path.exists() else {}
    symbols = status.get("symbols") or sorted(frame["coin"].dropna().unique().tolist())
    summary = {"version": 1, "generated_at_utc": now.isoformat(), "lookback_hours": hours,
               "source_root": str(root), "record_count": len(frame), "groups": summarize(frame),
               "complete_batch_spans": batch_spans(frame, symbols), "warnings": warnings,
               "notes": [
                   "Data before_publish excludes final summary/pointer publication; cycle_seconds includes publication and coordinator queue wait.",
                   "Candidate build includes preparation, fit, calibration, assessment and qualification; operation_seconds adds publication to actual worker time.",
                   "Model end_to_end includes executor queue and collection polling; idle time between scheduled jobs is not compute time.",
                   "Per-model assessment_seconds covers assessment prediction; final metric computation and eligibility are inside candidate_build_seconds.",
                   "Historical records without full cycle/publication measurements remain missing, not zero. Failed, unchanged, onboarding and benchmark runs are separate.",
                   "Batch spans use UTC timestamps for latest successful run per symbol at the same source close, only for complete cohorts; clock changes can affect spans.",
                   "Exports are point-in-time views; journals and immutable reports keep accumulating. p95 is an empirical percentile, not a latency guarantee.",
                   "The old approximately 25-minute stages were a different workload; these measurements are not a controlled speedup or quality comparison.",
               ]}
    return frame, summary


def markdown(summary):
    lines = ["# Hyperliquid operation timings", "", f"Generated: {summary['generated_at_utc']} · Last {summary['lookback_hours']:g} hours", "",
             "All durations are seconds. Data rows below use pre-publication pipeline time; model rows use complete candidate build time. Full cycle and publication metrics are retained in JSON/Parquet.", "",
             "| Operation | Symbol | Mode | Samples | Latest | Median | p95 | Max |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for group in summary["groups"]:
        metric = "before_publish_seconds" if group["kind"] == "data" else "candidate_build_seconds"
        if group["mode"] == "unchanged":
            metric = "unchanged_check_seconds"
        if group["mode"] in {"failed", "failed_or_discarded"}:
            metric = "cycle_seconds" if group["kind"] == "data" else "worker_seconds"
        values = group["metrics"].get(metric)
        if not values:
            continue
        market = group["coin"] + "/" + group["interval"]
        if group["horizon_bars"] is not None:
            market += f"/h{group['horizon_bars']}"
        lines.append(f"| {group['kind']} | {market} | {group['mode']} | {values['n']} | " +
                     " | ".join(f"{values[k]:.3f}" for k in ("latest", "median", "p95", "max")) + " |")
    lines.extend(["", "## Complete observed cohorts", ""])
    for kind in ("data", "model"):
        batches = [b for b in summary["complete_batch_spans"] if b["kind"] == kind]
        if batches:
            last = max(batches, key=lambda b: b["finished_at_utc"])
            lines.append(f"- Latest {kind} span for {', '.join(last['symbols'])}: **{last['wall_span_seconds']:.3f}s**, source close {last['source_close_utc']}. Includes scheduling/queue waits.")
        else:
            lines.append(f"- {kind.title()}: no complete cohort with start/end timings in this window yet.")
    lines.extend(["", "## Measurement boundaries", "", *[f"- {note}" for note in summary["notes"]]])
    if summary["warnings"]:
        lines.extend(["", "## Read warnings", "", *[f"- {w}" for w in summary["warnings"]]])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--export", action="store_true", help="Save timings.parquet, summary.json and report.md in _timings.")
    args = parser.parse_args(argv)
    frame, summary = build_report(args.output_root, hours=args.hours)
    content = markdown(summary)
    if args.export:
        directory = args.output_root / "_timings"
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_parquet(directory / "timings.parquet", frame)
        _atomic_json(directory / "summary.json", summary)
        (directory / "report.md").write_text(content, encoding="utf-8")
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
