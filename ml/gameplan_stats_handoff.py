"""Sanitized, verified prediction-stat packages for local Atlas/Scout handoff.

This module performs local file operations only. Transport and authorization of
an operating export belong to the caller; package hashes alone confer neither.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from app.ui.gameplan_stats_data import _outcome, load_gameplan_stats, prediction_metrics
from datafetching.symbol_universe import normalize_symbol
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, verify_manifest, write_manifest
from ml.gameplan_actuals_review import RUNS, VERSION, _write_json, completed_session_context
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET, probability_target_contract, probability_target_metadata


PACKAGE_VERSION = "cross-pc-gameplan-stats-v1"
NO_SAVED_FORECASTS = "NO_SAVED_INDEPENDENT_GAMEPLAN"
PRODUCERS = {"Atlas", "Scout"}
ROW_FIELDS = (
    "id", "symbol", "action_date", "model_status", "model_group", "route", "target_role",
    "direction", "actuals_status", "direction_correct", "calibrated_probability",
    "model_observed_target", "model_brier_score", "actual_return", "assumed_round_trip_cost",
    "target_window_start", "target_window_end", "actual_start_observed_at", "actual_end_observed_at",
    "probability_target_contract", "gameplan_variant",
)
REQUIRED_ROW_FIELDS = set(ROW_FIELDS) - {"assumed_round_trip_cost", "actual_start_observed_at", "actual_end_observed_at"}
PACKAGE_FIELDS = {"schema_version", "producer", "action_date", "symbols", "reviewed_at", "outcomes_through",
                  "probability_target_contract", "source_receipt_sha256", "source_manifest_sha256",
                  "source_results_sha256", "source_report_sha256", "coverage_status", "rows"}


def _symbols(values: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(normalize_symbol(value) for value in values)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Stats ownership requires nonempty unique symbols")
    return selected


def _scalar(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def _bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _validate_package(payload: dict) -> pd.DataFrame:
    if set(payload) != PACKAGE_FIELDS or payload.get("schema_version") != PACKAGE_VERSION or payload.get("producer") not in PRODUCERS:
        raise ValueError("Unsupported or unsanitized Stats package")
    symbols = _symbols(payload["symbols"])
    context = completed_session_context(payload["reviewed_at"], payload["action_date"])
    if pd.Timestamp(payload["outcomes_through"]) != pd.Timestamp(context["outcomes_through"]):
        raise ValueError("Stats package outcome cutoff differs from its completed session")
    for key in ("source_receipt_sha256", "source_manifest_sha256", "source_results_sha256", "source_report_sha256"):
        value = payload[key]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("Stats package has invalid source provenance")
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise ValueError("Stats handoff requires an explicit forecast list")
    probability_target_contract(payload)
    coverage = payload["coverage_status"]
    if coverage not in {NO_SAVED_FORECASTS, "SAVED_FORECASTS"} or bool(rows) != (coverage == "SAVED_FORECASTS"):
        raise ValueError("Stats forecast rows disagree with their explicit coverage status")
    if not rows:
        return pd.DataFrame(columns=ROW_FIELDS)
    for row in rows:
        if not isinstance(row, dict) or set(row) - set(ROW_FIELDS) or not REQUIRED_ROW_FIELDS.issubset(row):
            raise ValueError("Stats package contains unsupported prediction fields")
        if (row["symbol"] not in symbols or row["action_date"] != payload["action_date"]
                or not isinstance(row["id"], str) or not row["id"]
                or row["model_status"] not in {"PROMOTED", "RESEARCH_NOT_PROMOTED", "RESEARCH_NO_TARGET_HISTORY"}):
            raise ValueError("Stats package row differs from its frozen ownership or session")
        # Validate every row's frozen target and outcome, including excluded
        # research rows, before a peer package becomes a local UI publication.
        outcome = _outcome(row)
        if outcome.probability_target_contract != payload["probability_target_contract"]:
            raise ValueError("Stats package probability targets disagree")
        if row["gameplan_variant"] != probability_target_metadata(outcome.probability_target_contract)["gameplan_variant"]:
            raise ValueError("Stats package variant differs from its probability target")
        cutoff = pd.Timestamp(context["outcomes_through"])
        if outcome.status == "EVALUATED" and pd.Timestamp(outcome.end) > cutoff:
            raise ValueError("Stats package scores an outcome after its cutoff")
        if any(value is not None and pd.Timestamp(value) > cutoff for value in (outcome.observed_start, outcome.observed_end)):
            raise ValueError("Stats package contains observations after its cutoff")
    frame = pd.DataFrame.from_records(rows, columns=ROW_FIELDS)
    if frame.id.duplicated().any() or frame.duplicated(["symbol", "route"]).any():
        raise ValueError("Stats package has duplicate forecast identities")
    if set(frame.symbol) != set(symbols):
        raise ValueError("Stats package does not cover its declared symbol ownership")
    probability_target_contract(frame)
    return frame


def export_stats_package(root: Path, *, producer: str, symbols: Sequence[str], destination: Path,
                         action_date: str | None = None, review_run: Path | None = None) -> Path:
    """Export prediction outcomes only; never prices, quantities or account state."""
    review = load_gameplan_stats(root, action_date)
    if review_run is not None and review.run_directory != Path(review_run).resolve():
        raise ValueError("Selected Stats publication changed before export")
    run = review.run_directory
    source_hashes = {name: file_checksum(run / name) for name in ("receipt.json", "manifest.json", "forecast-results.parquet", "report.json")}
    original = pd.read_parquet(run / "forecast-results.parquet")
    source_report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    target = review.probability_target_contract
    if not original.empty and probability_target_contract(original) != target:
        raise ValueError("Stats probability target changed before export")
    coverage = "SAVED_FORECASTS"
    if original.empty:
        if source_report.get("coverage_status") != NO_SAVED_FORECASTS or source_report.get("source_gameplan_run"):
            raise ValueError("Empty Stats export requires a verified no-saved-Gameplan baseline")
        coverage = NO_SAVED_FORECASTS
    elif source_report.get("coverage_status") == NO_SAVED_FORECASTS:
        raise ValueError("Stats source coverage disagrees with its saved forecasts")
    frame = original.loc[:, [name for name in ROW_FIELDS if name in original]].copy()
    for key, value in probability_target_metadata(target).items():
        frame[key] = value
    payload = {"schema_version": PACKAGE_VERSION, "producer": producer, "symbols": list(_symbols(symbols)),
               "action_date": review.session, "reviewed_at": review.reviewed_at.isoformat(),
               "outcomes_through": review.outcomes_through.isoformat(), "probability_target_contract": target,
               "source_receipt_sha256": source_hashes["receipt.json"],
               "source_manifest_sha256": source_hashes["manifest.json"],
               "source_results_sha256": source_hashes["forecast-results.parquet"],
               "source_report_sha256": source_hashes["report.json"], "coverage_status": coverage,
               "rows": [{key: _scalar(value) for key, value in row.items()} for row in frame.to_dict("records")]}
    _validate_package(payload)
    verify_manifest(run)
    if any(file_checksum(run / name) != checksum for name, checksum in source_hashes.items()):
        raise ValueError("Stats source changed during export")
    data = _bytes(payload)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if destination.read_bytes() != data:
            raise ValueError("An immutable Stats export already exists with different bytes") from None
    return destination


def read_stats_package(path: Path, *, expected_sha256: str) -> tuple[dict, pd.DataFrame]:
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("Stats handoff bytes differ from the reviewed package hash")
    payload = json.loads(data)
    return payload, _validate_package(payload)


def combined_stats_target(payloads: Sequence[Mapping]) -> str:
    """Empty baselines contribute no scored target to an aggregate."""
    targets = {item["probability_target_contract"] for item in payloads if item["coverage_status"] == "SAVED_FORECASTS"}
    if len(targets) > 1:
        raise ValueError("Combined Stats cannot average different probability targets")
    return next(iter(targets)) if targets else RAW_DIRECTION_TARGET


def combine_stats_frames(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    available = [frame for frame in frames if not frame.empty]
    return pd.concat(available, ignore_index=True) if available else pd.DataFrame(columns=ROW_FIELDS)


def _combined_report(frame: pd.DataFrame, report: dict) -> str:
    outcomes = tuple(_outcome(row) for row in frame.loc[frame.model_status.eq("PROMOTED")].to_dict("records"))
    lines = [f"# Combined Gameplan Stats · {report['action_date']}", "",
             "Verified Atlas and Scout prediction outcomes. Original probabilities and targets are retained. "
             "These statistics measure forecasts, not broker fills or trading profit.", "",
             f"Probability target: `{report['probability_target_contract']}`. All window times are Pacific.", "",
             "| Symbol | Correct / scored | Direction accuracy | Brier | Evaluated | Pending | Awaiting data |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for symbol in ("All", *sorted(frame.symbol.unique())):
        metrics = prediction_metrics(tuple(row for row in outcomes if symbol == "All" or row.symbol == symbol))
        accuracy = "—" if metrics.accuracy is None else f"{metrics.accuracy:.1%}"
        brier = "—" if metrics.brier is None else f"{metrics.brier:.6f}"
        lines.append(f"| {symbol} | {metrics.correct} / {metrics.scored} | {accuracy} | {brier} | {metrics.evaluated} | {metrics.pending} | {metrics.awaiting_data} |")
    for actor, source in report["producers"].items():
        if source["coverage_status"] == NO_SAVED_FORECASTS:
            lines.extend(["", f"{actor}: no saved historical forecasts for {', '.join(source['symbols'])}; no performance score is available."])
    lines.extend(["", "Neutral predictions are included in Brier and excluded from directional accuracy. "
                  "Unpromoted, pending and missing outcomes are excluded from performance.", "",
                  "| Symbol | Route | Window | Call | Saved probability | Actual return | Outcome |",
                  "| --- | --- | --- | --- | --- | --- | --- |"])
    for row in outcomes:
        actual = "—" if row.actual_return is None else f"{row.actual_return:+.4%}"
        window = f"{row.start:%b %d %H:%M} → {row.end:%b %d %H:%M}"
        lines.append(f"| {row.symbol} | {row.route.replace('|', '/')} | {window} | {row.direction} | {row.probability:.4%} | {actual} | {row.state} |")
    return "\n".join(lines) + "\n"


def adopt_combined_stats(root: Path, *, packages: Mapping[str, Path], expected_symbols: Mapping[str, Sequence[str]],
                         expected_sha256: Mapping[str, str], reviewed_at=None) -> Path:
    """Adopt two already-authorized packages into the existing verified Stats UI."""
    if set(packages) != PRODUCERS or set(expected_symbols) != PRODUCERS or set(expected_sha256) != PRODUCERS:
        raise ValueError("Combined Stats requires exactly Atlas and Scout evidence")
    ownership = {actor: _symbols(expected_symbols[actor]) for actor in sorted(PRODUCERS)}
    if set(ownership["Atlas"]) & set(ownership["Scout"]):
        raise ValueError("Stats symbol ownership overlaps")
    verified, frames = {}, []
    for actor in sorted(PRODUCERS):
        payload, frame = read_stats_package(packages[actor], expected_sha256=expected_sha256[actor])
        if payload["producer"] != actor or tuple(payload["symbols"]) != ownership[actor]:
            raise ValueError("Stats package producer or frozen symbol ownership differs")
        verified[actor] = payload
        frames.append(frame)
    if len({item["action_date"] for item in verified.values()}) != 1:
        raise ValueError("Combined Stats must review the same completed session")
    target = combined_stats_target(tuple(verified.values()))
    frame = combine_stats_frames(frames)
    if frame.id.duplicated().any() or frame.duplicated(["symbol", "route"]).any():
        raise ValueError("Combined Stats has duplicate forecast identities")
    root = Path(root).resolve()
    now = utc_timestamp(reviewed_at)
    source = verified["Atlas"]
    if any(utc_timestamp(item["reviewed_at"]) > now for item in verified.values()):
        raise ValueError("Combined review cannot precede its source reviews")
    missing_owners = [actor for actor, payload in verified.items() if payload["coverage_status"] == NO_SAVED_FORECASTS]
    report = {"schema_version": VERSION, "status": "COMPLETE", "review_mode": "combined-atlas-scout",
              "action_date": source["action_date"], "reviewed_at": now.isoformat(),
              "outcomes_through": source["outcomes_through"], **probability_target_metadata(target),
              "coverage_status": NO_SAVED_FORECASTS if len(missing_owners) == 2 else "PARTIAL_SAVED_FORECASTS" if missing_owners else "SAVED_FORECASTS",
              "missing_history_owners": missing_owners,
              "producers": {actor: {"symbols": list(ownership[actor]), "package_sha256": expected_sha256[actor],
                                    "source_receipt_sha256": verified[actor]["source_receipt_sha256"],
                                    "source_report_sha256": verified[actor]["source_report_sha256"],
                                    "coverage_status": verified[actor]["coverage_status"],
                                    "probability_target_contract": verified[actor]["probability_target_contract"]}
                            for actor in sorted(PRODUCERS)},
              "orders_placed": 0, "broker_orders_enabled": False}
    run = create_timestamp_directory(root / RUNS, timestamp=now)
    frame.to_parquet(run / "forecast-results.parquet", index=False)
    _write_json(run / "report.json", report)
    (run / "Gameplan-results.md").write_text(_combined_report(frame, report), encoding="utf-8")
    write_manifest(run, run_timestamp=now, input_files=[],
                   output_files=["forecast-results.parquet", "report.json", "Gameplan-results.md"],
                   configuration={key: report[key] for key in ("schema_version", "action_date", "probability_target_contract", "review_mode", "producers", "coverage_status")})
    verify_manifest(run)
    receipt = {"schema_version": VERSION, "status": "COMPLETE", "action_date": report["action_date"],
               "run_path": run.relative_to(root).as_posix(), "manifest_sha256": file_checksum(run / "manifest.json"),
               "orders_placed": 0, "broker_orders_enabled": False}
    _write_json(run / "receipt.json", receipt)
    pointer = {"schema_version": VERSION, "current": {"run_path": receipt["run_path"], "action_date": report["action_date"],
                                                      "receipt_sha256": file_checksum(run / "receipt.json")}}
    # All package and metric checks finish before either reader pointer changes.
    _write_json(root / "ml/gameplan-actuals-review-latest/run.json", pointer)
    dated = root / "ml/gameplan-actuals-review-by-date" / report["action_date"]
    _write_json(dated / "run.json", pointer)
    temporary = dated / "Gameplan-results.tmp"
    temporary.write_bytes((run / "Gameplan-results.md").read_bytes())
    temporary.replace(dated / "Gameplan-results.md")
    return run
