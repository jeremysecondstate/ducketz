"""Evaluate saved forecast-only publications without changing Gameplan history."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, verify_manifest, write_manifest
from ml.directional_forecasts import RUN_ROOT, read_directional_forecast_run

VERSION = "directional-forecast-evaluations-v1"
EVALUATION_ROOT = Path("ml/directional-forecast-evaluation-runs")
POINTER = Path("ml/directional-forecast-evaluation-latest/run.json")


def evaluate_saved_directional_forecasts(root: Path, *, observed_groups, evaluated_at, input_files=()):
    from ml.gameplan_evaluation import _counts, _write_json, evaluate_forecasts

    root = Path(root).resolve()
    receipts = sorted((root / RUN_ROOT).glob("*/receipt.json"))
    if not receipts:
        return None
    with exclusive_runtime_lock(root / ".ducketz-directional-forecast-evaluation.lock",
                                process_name="Directional forecast evaluation"):
        frames, files = [], []
        for receipt in receipts:
            publication = read_directional_forecast_run(root, receipt.parent)
            frame = pd.read_parquet(receipt.parent / "forecasts.parquet")
            frame["source_gameplan_run"] = publication.run_directory.relative_to(root).as_posix()
            frames.append(frame)
            files.extend((receipt, receipt.parent / "manifest.json", receipt.parent / "forecasts.parquet"))
        previous = None
        pointer = root / POINTER
        if pointer.is_file():
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            run = (root / payload["current"]["run_path"]).resolve()
            if payload.get("schema_version") != VERSION or run.parent != root / EVALUATION_ROOT:
                raise RuntimeError("Directional evaluation pointer is invalid")
            manifest = verify_manifest(run)
            receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
            if (payload["current"]["receipt_checksum_sha256"] != file_checksum(run / "receipt.json")
                or receipt.get("schema_version") != VERSION
                or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")
                or manifest.get("configuration", {}).get("schema_version") != VERSION):
                raise RuntimeError("Directional evaluation receipt and manifest disagree")
            previous = pd.read_parquet(run / "evaluations.parquet")
            files.extend((run / "receipt.json", run / "evaluations.parquet"))
        now = utc_timestamp(evaluated_at)
        frame = evaluate_forecasts(pd.concat(frames, ignore_index=True), observed_groups=observed_groups,
                                   evaluated_at=now, previous=previous)
        summary = {"schema_version": VERSION, "evaluated_at": now.isoformat(), **_counts(frame),
                   "scope": "Read-only stock-direction forecasts with non-options features; no trade or options P/L",
                   "broker_orders_enabled": False, "orders_placed": 0}
        run = create_timestamp_directory(root / EVALUATION_ROOT)
        frame.to_parquet(run / "evaluations.parquet", index=False)
        _write_json(run / "summary.json", summary)
        write_manifest(run, run_timestamp=now, input_files=(*files, *input_files),
                       output_files=("evaluations.parquet", "summary.json"),
                       configuration={"schema_version": VERSION}, datastore_root=root)
        _write_json(run / "receipt.json", {"schema_version": VERSION,
            "manifest_checksum_sha256": file_checksum(run / "manifest.json")})
        _write_json(pointer, {"schema_version": VERSION, "current": {
            "run_path": run.relative_to(root).as_posix(),
            "receipt_checksum_sha256": file_checksum(run / "receipt.json")}})
        return run
