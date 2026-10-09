"""Count-weighted accuracy for two explicitly pinned original publications."""
from __future__ import annotations

import math
import io
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import _match_trade_rows
from ml.gameplan_probability_target import observed_probability_target
from ml.account_gameplan.sources import SourceBundle, _json, _digest, _safe_outputs, _output_matches, _sha, combined_forecast_id

VERSION = "account-gameplan-accuracy-v1"
_STATUSES = {"EVALUATED", "PENDING_MATURITY", "MATURE_AWAITING_DATA"}


def read_source_accuracy(run: Path, *, bundle: SourceBundle, expected_manifest_sha256: str) -> dict:
    """Verify one chosen native actuals result; never search a latest pointer."""
    run = Path(run).resolve()
    expected = _digest(expected_manifest_sha256)
    manifest_raw = (run / "manifest.json").read_bytes()
    if _sha(manifest_raw) != expected:
        raise ValueError("Accuracy manifest differs from the explicit expected hash")
    manifest = _json(manifest_raw)
    _safe_outputs(run, manifest)
    verify_manifest(run)
    if not {"report.json", "forecast-results.parquet"}.issubset(manifest["output_files"]):
        raise ValueError("Accuracy result omits bound report or forecast outcomes")
    receipt_raw = (run / "receipt.json").read_bytes()
    receipt = _json(receipt_raw)
    report_raw = (run / "report.json").read_bytes()
    rows_raw = (run / "forecast-results.parquet").read_bytes()
    _output_matches(manifest, "report.json", report_raw)
    _output_matches(manifest, "forecast-results.parquet", rows_raw)
    report = _json(report_raw)
    metadata = bundle.metadata
    if (receipt.get("schema_version") != "gameplan-actuals-review-v1" or receipt.get("status") != "COMPLETE"
            or receipt.get("manifest_sha256") != expected or receipt.get("action_date") != metadata["action_date"]
            or receipt.get("orders_placed") != 0 or receipt.get("broker_orders_enabled") is not False
            or report.get("schema_version") != "gameplan-actuals-review-v1" or report.get("status") != "COMPLETE"
            or report.get("action_date") != metadata["action_date"]
            or report.get("source_gameplan_run") != metadata["source_gameplan_run"]
            or report.get("target_price_source_contract") != metadata["target_price_source_contract"]
            or manifest.get("configuration", {}).get("action_date") != metadata["action_date"]):
        raise ValueError("Accuracy result is not bound to the selected frozen source")
    source_receipt_path = metadata["source_gameplan_run"].replace("\\", "/") + "/receipt.json"
    matching = [item for item in manifest.get("input_files", [])
                if str(item.get("path", "")).replace("\\", "/") == source_receipt_path]
    if (len(matching) != 1 or matching[0].get("status") != "present"
            or matching[0].get("checksum_sha256") != metadata["source_receipt_sha256"]):
        raise ValueError("Accuracy input receipt does not match the source bundle")
    rows = pd.read_parquet(io.BytesIO(rows_raw))
    _match_trade_rows(bundle.forecasts, rows)
    result = {"producer_id": metadata["producer_id"], "source_receipt_sha256": metadata["source_receipt_sha256"],
        "source_bundle_manifest_sha256": bundle.manifest_sha256, "action_date": metadata["action_date"],
        "result_manifest_sha256": expected, "result_receipt_sha256": _sha(receipt_raw),
        "rows": rows}
    _validate_rows(bundle, result)
    if file_checksum(run / "manifest.json") != expected:
        raise ValueError("Accuracy manifest changed while reading")
    return result


def _missing(value) -> bool:
    return value is None or bool(pd.isna(value))


def _validate_rows(bundle: SourceBundle, result: Mapping) -> pd.DataFrame:
    metadata = bundle.metadata
    for key in ("producer_id", "source_receipt_sha256", "action_date"):
        if result.get(key) != metadata[key]:
            raise ValueError("Accuracy producer/source/date identity differs")
    if result.get("source_bundle_manifest_sha256") != bundle.manifest_sha256:
        raise ValueError("Accuracy source bundle binding differs")
    _digest(result.get("result_manifest_sha256"))
    rows = result.get("rows")
    if not isinstance(rows, pd.DataFrame):
        raise ValueError("Explicit source forecast outcomes are required")
    _match_trade_rows(bundle.forecasts, rows)
    if not {"actuals_status", "actual_return", "direction_correct"}.issubset(rows):
        raise ValueError("Accuracy results lack outcome and coverage fields")
    normalized = []
    for row in rows.to_dict("records"):
        status, direction = row["actuals_status"], row["direction"]
        if status not in _STATUSES or direction not in {"BULLISH", "BEARISH", "NO_EDGE"}:
            raise ValueError("Unsupported outcome coverage or direction")
        correct, brier = None, None
        if status == "EVALUATED":
            change = row["actual_return"]
            if type(change) not in (int, float) or not math.isfinite(change):
                raise ValueError("Evaluated accuracy needs an observed finite return")
            correct = (change > 0 if direction == "BULLISH" else change < 0) if direction != "NO_EDGE" else None
            cost = row.get("assumed_round_trip_cost", .001)
            cost = .001 if _missing(cost) else cost
            target = observed_probability_target(change, cost, metadata["probability_target_contract"])
            brier = (float(row["calibrated_probability"]) - target) ** 2
            if "model_brier_score" in row and not _missing(row["model_brier_score"]) and not math.isclose(float(row["model_brier_score"]), brier, abs_tol=1e-12, rel_tol=0):
                raise ValueError("Saved model score differs from its frozen probability target")
        elif not _missing(row["actual_return"]):
            raise ValueError("Pending or missing outcomes cannot carry an observed return")
        stored = row["direction_correct"]
        if ((correct is None and not _missing(stored)) or (correct is not None
                and (type(stored) is not bool or stored != correct))):
            raise ValueError("Saved directional outcome differs from the original forecast")
        normalized.append({"id": combined_forecast_id(metadata["producer_id"], metadata["source_receipt_sha256"], row["id"]),
            "producer_id": metadata["producer_id"], "source_forecast_id": row["id"], "symbol": row["symbol"],
            "model_group": row["model_group"], "model_status": row["model_status"], "status": status, "direction": direction,
            "correct": correct, "brier_score": brier, "probability_target_contract": metadata["probability_target_contract"]})
    return pd.DataFrame(normalized)


def _counts(rows: pd.DataFrame) -> dict:
    evaluated = rows.status.eq("EVALUATED")
    directional = evaluated & rows.direction.ne("NO_EDGE")
    count = int(directional.sum())
    correct = int(rows.loc[directional, "correct"].sum())
    return {"forecasts": len(rows), "evaluated": int(evaluated.sum()), "directional_evaluated": count,
        "correct": correct, "incorrect": count - correct, "direction_accuracy": correct / count if count else None,
        "neutral_total": int(rows.direction.eq("NO_EDGE").sum()),
        "neutral_evaluated": int((evaluated & rows.direction.eq("NO_EDGE")).sum()),
        "pending_maturity": int(rows.status.eq("PENDING_MATURITY").sum()),
        "mature_awaiting_data": int(rows.status.eq("MATURE_AWAITING_DATA").sum())}


def aggregate_accuracy(sources: Sequence[SourceBundle], results: Sequence[Mapping]) -> dict:
    """Combine counts, preserving both sources and all excluded coverage."""
    if len(sources) != 2 or len(results) != 2:
        raise ValueError("Account accuracy requires exactly two explicit source results")
    by_producer = {source.metadata["producer_id"]: source for source in sources}
    result_map = {result.get("producer_id"): result for result in results}
    if len(by_producer) != 2 or len(result_map) != 2 or set(by_producer) != set(result_map):
        raise ValueError("Duplicate, missing or unexpected accuracy producer")
    if len({source.metadata["action_date"] for source in sources}) != 1:
        raise ValueError("Account accuracy sources have different action dates")
    frames = [_validate_rows(by_producer[producer], result_map[producer]) for producer in sorted(by_producer)]
    combined = pd.concat(frames, ignore_index=True)
    if combined.id.duplicated().any():
        raise ValueError("Combined accuracy identities are duplicated")
    contracts = {}
    for contract, rows in combined.groupby("probability_target_contract", sort=True):
        assessed = rows.loc[rows.status.eq("EVALUATED"), "brier_score"]
        contracts[contract] = {**_counts(rows), "model_brier_sum": float(assessed.sum()),
            "model_brier_mean": float(assessed.sum()) / len(assessed) if len(assessed) else None}
    return {"schema_version": VERSION, "action_date": sources[0].metadata["action_date"], "status": "COMPLETE",
        "totals": _counts(combined), "by_producer": {producer: _counts(combined.loc[combined.producer_id.eq(producer)]) for producer in sorted(by_producer)},
        "promoted_totals": _counts(combined.loc[combined.model_status.eq("PROMOTED")]),
        "by_model_status": {status: _counts(rows) for status, rows in combined.groupby("model_status", sort=True)},
        "by_probability_target": contracts,
        "sources": [{key: result_map[producer][key] for key in ("producer_id", "source_receipt_sha256", "source_bundle_manifest_sha256", "result_manifest_sha256")}
                    for producer in sorted(by_producer)],
        "forecast_identities": combined.id.tolist(), "orders_placed": 0,
        "semantics": "Totals retain every frozen row; promoted_totals matches the native promoted-call view. Accuracy is correct divided by evaluated bullish/bearish calls across both explicit originals. Neutral, pending, missing and research outcomes remain separate. Probability scores remain grouped by their frozen target. No percentage averaging, source selection, retraining or execution authority."}
