"""Bounded, zero-dollar diagnostic only; no archive cursor or production writes."""
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path

import databento as db
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path.cwd()))
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_xnas_replay import _external_failure

OUT = Path(__file__).parent / "source-probe"
OUT.mkdir(exist_ok=True)
DATASET = "XNAS.BASIC"
SCHEMA = "ohlcv-1m"
WINDOWS = [
    ("sep11-premarket", "2026-09-11T11:30:00Z", "2026-09-11T12:15:00Z", ["2026-09-11T12:00:00Z"]),
    ("sep11-afterhours", "2026-09-11T20:30:00Z", "2026-09-12T00:00:00Z", ["2026-09-11T21:00:00Z", "2026-09-11T22:00:00Z", "2026-09-11T23:00:00Z", "2026-09-12T00:00:00Z"]),
    ("sep10-prior-close", "2026-09-10T23:30:00Z", "2026-09-11T00:00:00Z", ["2026-09-11T00:00:00Z"]),
]

def now():
    return pd.Timestamp.now(tz="UTC").isoformat()

def save(path, payload):
    path.write_text(json.dumps(payload, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")

def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

report = {"started_at": now(), "dataset": DATASET, "schema": SCHEMA, "symbol": "COST", "diagnostic_only": True, "production_writes": False, "orders": 0, "windows": []}
try:
    load_repository_environment()
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        raise ValueError("Existing Databento credential unavailable")
    client = db.Historical(key)
    schemas = list(client.metadata.list_schemas(dataset=DATASET))
    coverage = client.metadata.get_dataset_range(dataset=DATASET)
    save(OUT / "dataset-metadata.json", {"observed_at": now(), "dataset": DATASET, "schemas": schemas, "range": coverage})
    if SCHEMA not in schemas:
        raise ValueError("Candidate minute schema unavailable")
    availability = coverage.get("schema", {}).get(SCHEMA, coverage)
    available_start = pd.Timestamp(availability.get("start", coverage.get("start")))
    available_end = pd.Timestamp(availability.get("end", coverage.get("end")))
    if available_start.tzinfo is None:
        available_start = available_start.tz_localize("UTC")
    if available_end.tzinfo is None:
        available_end = available_end.tz_localize("UTC")
    preflights = []
    for name, start, end, clocks in WINDOWS:
        directory = OUT / name
        directory.mkdir(exist_ok=True)
        request = {"dataset": DATASET, "schema": SCHEMA, "symbols": ["COST"], "stype_in": "raw_symbol", "start": start, "end": end}
        range_pass = available_start <= pd.Timestamp(start) < pd.Timestamp(end) <= available_end
        if not range_pass:
            raise ValueError("Exact candidate range not yet available")
        cost = float(client.metadata.get_cost(**request))
        size = int(client.metadata.get_billable_size(**request))
        count = int(client.metadata.get_record_count(**request))
        free = shutil.disk_usage(OUT).free
        preflight = {"observed_at": now(), "request": request, "range_pass": bool(range_pass), "estimated_cost_usd": cost, "estimated_billable_bytes": size, "estimated_record_count": count, "available_bytes": free, "max_bytes": 64 * 1024**2, "capacity_pass": bool(0 <= size <= 64 * 1024**2 and free > 5 * 1024**3 + size * 4)}
        save(directory / "preflight.json", preflight)
        if not math.isfinite(cost) or cost != 0 or not preflight["capacity_pass"]:
            raise ValueError("Exact zero-dollar or capacity preflight failed")
        preflights.append((directory, request, clocks, preflight))
    # All exact windows must pass before any data is acquired.
    for directory, request, clocks, preflight in preflights:
        path = directory / "provider.dbn"
        if path.exists():
            raise ValueError("Diagnostic raw destination already exists; refusing overwrite")
        native = client.timeseries.get_range(**request, path=path)
        try:
            meta = native.metadata
            metadata = {field: getattr(meta, field, None) for field in ("dataset", "schema", "start", "end", "symbols", "stype_in", "stype_out", "partial", "not_found", "mappings", "version")}
            save(directory / "native-metadata.json", metadata)
            if str(meta.dataset) != DATASET or str(meta.schema) != SCHEMA or int(meta.start) != pd.Timestamp(request["start"]).value or int(meta.end) != pd.Timestamp(request["end"]).value or meta.partial or meta.not_found or tuple(meta.symbols) != ("COST",):
                raise ValueError("Native dataset, range or symbol metadata differs from exact request")
            frame = native.to_df(schema=SCHEMA, map_symbols=True, price_type="float").reset_index()
        finally:
            native.reader.close()
        if len(frame):
            if set(frame.symbol.astype(str)) != {"COST"}:
                raise ValueError("Unexpected or unmapped symbol in candidate observations")
            stamps = pd.to_datetime(frame.ts_event, utc=True)
            if not (stamps.ge(pd.Timestamp(request["start"])) & stamps.lt(pd.Timestamp(request["end"])) & stamps.eq(stamps.dt.floor("min"))).all():
                raise ValueError("Candidate timestamps outside exact minute request")
            vals = frame[["open", "high", "low", "close", "volume"]].to_numpy(float)
            if not np.isfinite(vals).all() or (vals[:, :4] <= 0).any() or (vals[:, 4] < 0).any():
                raise ValueError("Invalid candidate OHLCV values")
            if frame.high.lt(frame[["open", "low", "close"]].max(axis=1)).any() or frame.low.gt(frame[["open", "high", "close"]].min(axis=1)).any():
                raise ValueError("Inconsistent candidate OHLC range")
            frame = frame.sort_values("ts_event")
            if frame.duplicated(["ts_event", "publisher_id", "symbol"]).any():
                raise ValueError("Duplicate candidate publisher/minute records")
        frame.to_parquet(directory / "observations.parquet", index=False)
        points = []
        for clock in clocks:
            if len(frame):
                eligible = frame[frame.ts_event + pd.Timedelta(minutes=1) <= pd.Timestamp(clock)]
            else:
                eligible = frame
            if len(eligible):
                row = eligible.iloc[-1]
                age = (pd.Timestamp(clock) - row.ts_event - pd.Timedelta(minutes=1)).total_seconds() / 60
                point = {"boundary_utc": clock, "bar_start": str(row.ts_event), "bar_completion": str(row.ts_event + pd.Timedelta(minutes=1)), "close": float(row.close), "age_minutes": age, "within_five_minutes": bool(0 <= age <= 5), "publisher_id": int(row.publisher_id)}
            else:
                point = {"boundary_utc": clock, "within_five_minutes": False, "reason": "NO_EARLIER_OBSERVATION_IN_DIAGNOSTIC_WINDOW"}
            points.append(point)
        result = {"window": directory.name, "status": "COMPLETE" if len(frame) else "COMPLETE_EMPTY", "rows": len(frame), "publisher_ids": sorted(map(int, frame.publisher_id.unique())) if len(frame) else [], "raw_sha256": checksum(path), "observations_sha256": checksum(directory / "observations.parquet"), "points": points}
        save(directory / "receipt.json", {**result, "completed_at": now(), "request": request, "preflight_sha256": checksum(directory / "preflight.json"), "native_metadata_sha256": checksum(directory / "native-metadata.json")})
        report["windows"].append(result)
        save(OUT / "report.json", report)
    report["status"] = "COMPLETE"
except Exception as exc:
    report["status"] = "BLOCKED"
    report["error"] = _external_failure(exc)
report["completed_at"] = now()
save(OUT / "report.json", report)
print(json.dumps(report, indent=2))
