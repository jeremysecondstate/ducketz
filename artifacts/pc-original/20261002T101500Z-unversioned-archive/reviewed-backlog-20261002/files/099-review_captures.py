"""Offline bounded CME warning capture review; no provider or runtime calls."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import io
import json
import re
import sys

sys.dont_write_bytecode = True
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path("<LOCAL_DATASTORE>")
OUT = Path(__file__).resolve().parent
RUN = ROOT / "ml/overnight-runs/20261001T040644.667536Z"
RUN_START = pd.Timestamp("2026-10-01T04:06:44.667536Z")
FLAG_START = pd.Timestamp("2026-09-30T00:00:00Z")
FLAG_END = pd.Timestamp("2026-10-01T00:00:00Z")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    observed = datetime.now(timezone.utc)
    metadata_path = OUT / "quality-metadata-20261001T041634Z.json"
    metadata = json.loads(metadata_path.read_bytes())
    ranges = next(call["response"] for call in metadata["calls"]
                  if call["endpoint"] == "metadata.get_dataset_range"
                  and call["status"] == "SUCCESS")
    log_path = RUN / "loop_a_close_fetch.log"
    log_data = log_path.read_bytes()
    requests, warnings = [], []
    current_request = None
    for number, raw in enumerate(log_data.decode("utf-8", errors="replace").splitlines(), 1):
        line = re.sub(r"https?://\S+", "[provider documentation URL omitted]", raw)
        if "provider.request" in line and 'name="CME ' in line:
            row = {"line": number, "text": line}
            requests.append(row)
            if "] START " in line:
                current_request = row
        if "reduced quality" in line:
            warnings.append({"line": number, "text": line,
                             "enclosing_logged_request": current_request})
    captures = []
    for scope in ["CME_CONTEXT", "CME_CONTRACTS"]:
        for schema in ["ohlcv-1m", "bbo-1m", "mbp-10"]:
            period = scope.lower() + "_" + schema
            path = ROOT / "pools/cme" / scope / period / "databento/normalized" / (scope + "_" + period + ".parquet")
            row = {"scope": scope, "schema": schema, "path": str(path)}
            if not path.exists():
                row["status"] = "MISSING"
                captures.append(row)
                continue
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("CAPTURE_SIZE_BOUND_EXCEEDED")
            data = path.read_bytes()
            source = io.BytesIO(data)
            if pq.read_metadata(source).num_rows > 300000:
                raise ValueError("CAPTURE_ROW_BOUND_EXCEEDED")
            names = set(pq.read_schema(source).names)
            wanted = ["symbol", "timestamp", "fetched_at", "provider_dataset", "provider_schema", "provider_stype_in",
                      "initial_range_start", "initial_range_end", "effective_range_start", "effective_range_end",
                      "limit", "request_limit_saturated", "latest_window_shrink_count", "empty_window_expansion_count", "cme_schema_status"]
            frame = pq.read_table(source, columns=[name for name in wanted if name in names]).to_pandas()
            fetched = pd.to_datetime(frame["fetched_at"], utc=True)
            current = frame.loc[fetched.ge(RUN_START)].copy()
            row.update(sha256=digest(data), file_rows=len(frame), current_native_rows=len(current),
                       latest_fetched_at=str(fetched.max()),
                       status="CURRENT_NATIVE_CAPTURE" if len(current) else "PENDING_CURRENT_NATIVE_CAPTURE",
                       schema_available_end=ranges["schema"][schema]["end"])
            detail_cols = [name for name in wanted if name in names and name not in {"symbol", "timestamp", "fetched_at"}]
            row["request_metadata"] = current[detail_cols].drop_duplicates().to_dict("records")
            for request in row["request_metadata"]:
                available = pd.Timestamp(row["schema_available_end"])
                request["initial_end_within_metadata_availability"] = pd.Timestamp(request["initial_range_end"]) <= available
                request["effective_end_within_metadata_availability"] = pd.Timestamp(request["effective_range_end"]) <= available
                overlap_start = max(pd.Timestamp(request["effective_range_start"]), FLAG_START)
                overlap_end = min(pd.Timestamp(request["effective_range_end"]), FLAG_END)
                request["overlaps_flagged_day"] = overlap_start < overlap_end
                request["effective_flagged_day_overlap_start"] = str(overlap_start) if overlap_start < overlap_end else None
                request["effective_flagged_day_overlap_end"] = str(overlap_end) if overlap_start < overlap_end else None
            row["symbols"] = []
            for symbol, records in current.groupby("symbol"):
                times = pd.to_datetime(records["timestamp"], utc=True)
                row["symbols"].append({"symbol": symbol, "rows": len(records), "first_market_timestamp": str(times.min()),
                                       "last_market_timestamp": str(times.max()),
                                       "rows_on_flagged_utc_day": int((times.ge(FLAG_START) & times.lt(FLAG_END)).sum())})
            captures.append(row)
    stage = json.loads((RUN / "stage-report.json").read_bytes())
    all_current = all(row["status"] == "CURRENT_NATIVE_CAPTURE" for row in captures)
    result = {
        "schema_version": 1, "reviewed_at_utc": observed.isoformat(),
        "scope": "OFFLINE_READ_ONLY_PROVIDER_WARNING_REVIEW", "native_run_id": RUN.name,
        "native_status": stage.get("status"), "native_stage": stage.get("current_stage"),
        "dataset": "GLBX.MDP3", "flagged_utc_date": "2026-09-30",
        "status": "CAPTURES_REVIEWED_MODEL_TRACE_PENDING" if all_current else "CURRENT_NATIVE_CAPTURES_PENDING",
        "metadata_evidence": {"path": str(metadata_path), "sha256": digest(metadata_path.read_bytes()),
                              "checked_at_utc": metadata["finished_at_utc"]},
        "provider_conditions": metadata["calls"][0]["response"],
        "schema_ranges": {key: ranges["schema"][key] for key in ["ohlcv-1m", "bbo-1m", "mbp-10", "mbo"]},
        "log_snapshot": {"path": str(log_path), "bytes": len(log_data), "sha256": digest(log_data)},
        "warning_occurrences": warnings, "cme_requests": requests, "captures": captures,
        "provider_cause": "UNDISCLOSED: metadata identifies date, condition and modification date, with no cause or affected channel/symbol/schema/intraday interval.",
        "limitations": [
            "September 30 is a completed UTC day at this capture; the degraded flag is not explained by an in-progress UTC day.",
            "Schema publication lag and reduced provider quality are separate observations; neither establishes the cause of the other.",
            "A successful nonempty request proves observed delivery, not complete requested-range coverage or model eligibility.",
            "Record-limit saturation is explicitly retained and prevents claiming full-range completeness.",
            "Dataset-condition metadata does not identify which individual delivered records are defective.",
            "Fresh model artifacts are not yet available; verify actual admitted features and manifest-bound source rows after publication."
        ],
        "provider_calls_this_review": 0, "metadata_calls_reused": 2, "market_data_acquisitions": 0,
        "broker_calls": 0, "orders": 0, "production_writes": 0,
    }
    path = OUT / observed.strftime("capture-review-%Y%m%dT%H%M%SZ.json")
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps({"path": str(path), "sha256": digest(path.read_bytes()), "status": result["status"],
                      "warnings": len(warnings), "captures": [{"scope": r["scope"], "schema": r["schema"], "status": r["status"], "rows": r.get("current_native_rows", 0), "requests": r.get("request_metadata", [])} for r in captures]}, indent=2, default=str))


if __name__ == "__main__":
    main()
