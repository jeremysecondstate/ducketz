"""Bounded, zero-cost native XNAS history backfill for existing stock features.

Historical request anchors retain the normal 100-day bootstrap contract. An
additional current-time entitlement check prevents an old anchor from extending
the actual subscription window. This command does not place orders or publish
models, and never reads or downloads options history.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import shutil

from datafetching import databento_cold_start as cold
from datafetching.cme_runtime import load_repository_environment
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp
from ml.stock_target_history import latest_completed_through
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS


BACKFILL_VERSION = "bounded-stock-target-backfill-v1"
REQUESTED_START = date(2025, 3, 4)
REQUESTED_THROUGH = date(2026, 5, 28)  # Exclusive; existing history starts here.
DATASET = "XNAS.ITCH"
SCHEMA = "ohlcv-1m"


def _baseline_manifest(root: Path, through: date) -> dict:
    window = cold.schema_window(cold.PLAN_DATASET_US_EQUITIES, SCHEMA)
    if window != {"unit": "days", "value": 100}:
        raise ValueError("Stock backfill requires the unchanged native 100-day bootstrap policy")
    start = cold._window_start(through, window)
    requests = []
    for symbol in sorted(STOCK_TRADER_SYMBOLS):
        identity = {"dataset": DATASET, "standard_plan_dataset": cold.PLAN_DATASET_US_EQUITIES,
                    "schema": SCHEMA, "symbol_scope": [symbol], "stype_in": "raw_symbol",
                    "start": start.isoformat(), "end": through.isoformat(),
                    "storage_contract": "isolated-cold-start", "window": window,
                    "fetch_mode": "initial-baseline", "baseline_start": start.isoformat(),
                    "previous_completed_through": None}
        path = cold._entry_storage_path(root, dataset=DATASET, market=cold.MARKET_US_EQUITIES,
            schema=SCHEMA, symbol=symbol, start=start, end=through, contract="isolated-cold-start")
        request = {"request_id": cold._checksum(identity)[:24], **identity,
                   "storage_path": str(path), "status": "PENDING"}
        cold._validate_execution_request_identity(root, request)
        requests.append(request)
    body = {"schema_version": cold.MANIFEST_VERSION, "coordinator_version": cold.COORDINATOR_VERSION,
            "history_profile": cold.HISTORY_PROFILE, "as_of": through.isoformat(),
            "datastore_root": str(root), "entitlement_authority": cold.STANDARD_PLAN_AUTHORITY,
            "requests": requests, "derived_views": []}
    digest = cold._checksum(body)
    manifest = {"manifest_id": digest[:24], **body, "semantic_checksum_sha256": digest}
    cold._validate_manifest_included_scope(manifest)
    cold._validate_manifest_checksum(manifest)
    return manifest


def build_backfill_manifests(root: Path, *, start=REQUESTED_START, through=REQUESTED_THROUGH) -> tuple[dict, ...]:
    """Build only the authorized feature-era interval, ignoring live cursors."""
    root = Path(root).resolve()
    if start < REQUESTED_START or through > REQUESTED_THROUGH or start >= through:
        raise ValueError("Backfill must stay within the bounded 2025-03-04 to 2026-05-28 request")
    if through > latest_completed_through():
        raise ValueError("Backfill cannot include an unfinished action session")
    actual_today = utc_timestamp().date()
    included_start = cold._window_start(actual_today,
        cold.standard_plan_history_policy(cold.PLAN_DATASET_US_EQUITIES, SCHEMA))
    manifests = []
    end = through
    while end > start:
        manifest = _baseline_manifest(root, end)
        native_start = date.fromisoformat(manifest["requests"][0]["start"])
        if native_start < included_start:
            raise ValueError("Historical request anchor would exceed the actual current eight-year entitlement")
        manifests.append(manifest)
        end = native_start
    return tuple(reversed(manifests))


def _prepare_or_verify_plan(root: Path, *, start: date, through: date, resume_run: Path | None) -> tuple[Path, dict, tuple[dict, ...]]:
    manifests = build_backfill_manifests(root, start=start, through=through)
    if resume_run is not None:
        run = Path(resume_run).resolve()
        if run.parent != (root / "ml/stock-target-backfill-runs").resolve():
            raise ValueError("Backfill resume run escapes its expected directory")
        plan = cold._read_json_object(run / "plan.json", label="stock backfill plan")
        body = {key: value for key, value in plan.items() if key != "plan_checksum_sha256"}
        if (plan.get("plan_checksum_sha256") != cold._checksum(body)
                or plan.get("schema_version") != BACKFILL_VERSION
                or plan.get("datastore_root") != str(root)
                or plan.get("requested_start") != start.isoformat()
                or plan.get("requested_through") != through.isoformat()
                or plan.get("symbols") != list(STOCK_TRADER_SYMBOLS)
                or len(plan.get("chunks", [])) != len(manifests)):
            raise ValueError("Backfill resume plan identity differs")
        for index, (record, expected) in enumerate(zip(plan["chunks"], manifests)):
            name = f"chunks/{index:03d}/manifest.json"
            path = run / name
            if record.get("manifest_path") != name or record.get("manifest_sha256") != file_checksum(path):
                raise ValueError("Backfill immutable chunk manifest changed")
            observed = cold._read_json_object(path, label="stock backfill chunk")
            if observed != expected:
                raise ValueError("Backfill chunk differs from its exact native scope")
        return run, plan, manifests
    run = create_timestamp_directory(root / "ml/stock-target-backfill-runs")
    records = []
    for index, manifest in enumerate(manifests):
        relative = f"chunks/{index:03d}/manifest.json"
        path = run / relative
        cold._write_json_exclusive(path, manifest)
        records.append({"manifest_path": relative, "manifest_sha256": file_checksum(path),
                        "manifest_id": manifest["manifest_id"], "as_of": manifest["as_of"]})
    body = {"schema_version": BACKFILL_VERSION, "datastore_root": str(root),
            "requested_start": start.isoformat(), "requested_through": through.isoformat(),
            "native_start": manifests[0]["requests"][0]["start"], "symbols": list(STOCK_TRADER_SYMBOLS),
            "dataset": DATASET, "schema": SCHEMA, "chunks": records,
            "maximum_cost_usd": 0.0, "orders_placed": 0}
    plan = {**body, "plan_checksum_sha256": cold._checksum(body)}
    cold._write_json_exclusive(run / "plan.json", plan)
    return run, plan, manifests


def run_stock_target_backfill(root: Path, *, client=None, start=REQUESTED_START, through=REQUESTED_THROUGH,
                              execute=False, plan_only=False, resume_run: Path | None = None, reporter=print) -> Path:
    """Prepare all exact costs/capacity before fetching any native chunk.

    Each invocation retains a separate attempt receipt. The plan/chunk manifests
    remain immutable; shared per-chunk native progress supports crash recovery.
    Resume repeats current entitlement, range, cost and total capacity checks.
    """
    root = Path(root).resolve()
    if execute and plan_only:
        raise ValueError("Plan-only backfill cannot execute")
    with exclusive_runtime_lock(root / ".ducketz-databento-cold-start.lock", process_name="Stock target backfill"):
        run, plan, manifests = _prepare_or_verify_plan(root, start=start, through=through, resume_run=resume_run)
        attempt = create_timestamp_directory(run / "attempts")
        cold._write_json_atomic(run / "latest-attempt.json", {"attempt_path": attempt.relative_to(run).as_posix()})
        receipt = {"schema_version": BACKFILL_VERSION, "plan_sha256": file_checksum(run / "plan.json"),
                   "status": "PLANNED", "orders_placed": 0, "chunks": len(manifests),
                   "requests": sum(len(manifest["requests"]) for manifest in manifests), "completed_chunks": []}
        try:
            if not plan_only:
                if client is None:
                    raise ValueError("Backfill preflight requires an explicit provider client")
                catalog = cold.discover_dataset_catalog(client, dataset=DATASET, required_schemas=(SCHEMA,))
                bounds = catalog[SCHEMA]
                costs = []
                for manifest in manifests:
                    for request in manifest["requests"]:
                        if request["start"] < bounds["start"] or request["end"] > bounds["end"]:
                            raise ValueError("Provider range does not cover every exact backfill request")
                        cost = float(client.metadata.get_cost(**cold._request_kwargs(request)))
                        if not math.isfinite(cost) or cost < 0:
                            raise ValueError("Provider returned an invalid backfill cost")
                        costs.append({"request_id": request["request_id"], "estimated_cost_usd": cost})
                cold._write_json_atomic(attempt / "cost-preflight.json", {"dataset_range": catalog,
                    "requests": costs, "total_cost_usd": sum(row["estimated_cost_usd"] for row in costs),
                    "maximum_cost_usd": 0.0, "checked_at": utc_timestamp().isoformat()})
                if any(row["estimated_cost_usd"] != 0 for row in costs):
                    raise ValueError("Stock target backfill requires all requests to have zero cost")
                preflights = []
                for index, manifest in enumerate(manifests):
                    preflight = cold.preflight_manifest(client, datastore_root=root, manifest=manifest)
                    cold._write_json_atomic(attempt / f"chunk-{index:03d}-preflight.json", preflight)
                    if not preflight["capacity_pass"]:
                        raise ValueError("A backfill chunk failed native capacity preflight")
                    preflights.append(preflight)
                    if reporter:
                        reporter(f"BACKFILL PREFLIGHT {index + 1}/{len(manifests)}: {preflight['total_estimated_download_size_bytes']} bytes")
                total_bytes = sum(record["total_estimated_download_size_bytes"] for record in preflights)
                required = cold.required_free_bytes(total_bytes)
                free = shutil.disk_usage(root.anchor).free
                summary = {"total_estimated_download_size_bytes": total_bytes,
                           "total_record_count": sum(record["total_record_count"] for record in preflights),
                           "required_free_bytes": required, "available_free_bytes": free,
                           "capacity_pass": free >= required, "estimated_cost_usd": 0.0,
                           "all_requests_preflighted_before_execution": True}
                cold._write_json_atomic(attempt / "preflight-summary.json", summary)
                if not summary["capacity_pass"]:
                    raise ValueError("Aggregate backfill capacity preflight failed")
                receipt.update(status="PREFLIGHTED", **summary)
                if execute:
                    for index, (manifest, preflight) in enumerate(zip(manifests, preflights)):
                        # Recheck actual access and remaining capacity after long downloads.
                        build_backfill_manifests(root, start=start, through=through)
                        remaining = sum(record["total_estimated_download_size_bytes"] for record in preflights[index:])
                        if shutil.disk_usage(root.anchor).free < cold.required_free_bytes(remaining):
                            raise ValueError("Remaining aggregate backfill capacity is insufficient")
                        counts = cold.execute_manifest(client, datastore_root=root, manifest=manifest,
                            preflight=preflight, progress_path=run / f"chunks/{index:03d}/progress.json", reporter=reporter)
                        if counts.get("failed", 0) or counts.get("no_data", 0):
                            raise RuntimeError("Backfill chunk did not publish complete native stock history")
                        for request in manifest["requests"]:
                            if not cold._entry_is_verified(root, request):
                                raise RuntimeError("Backfill native partition did not verify after execution")
                        receipt["completed_chunks"].append({"index": index, "counts": counts})
                        cold._write_json_atomic(attempt / "progress.json", receipt)
                    receipt["status"] = "COMPLETE"
        except BaseException as exc:
            receipt.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            receipt["finished_at"] = utc_timestamp().isoformat()
            cold._write_json_atomic(attempt / "receipt.json", receipt)
        return run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--datastore", type=Path)
    location.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc")
    parser.add_argument("--start", type=date.fromisoformat, default=REQUESTED_START)
    parser.add_argument("--through", type=date.fromisoformat, default=REQUESTED_THROUGH)
    parser.add_argument("--resume-run", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=None if args.datastore else args.datastore_target)
    client = None
    if not args.plan_only:
        load_repository_environment()
        import databento as db
        api_key = os.environ.get("DATABENTO_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("DATABENTO_API_KEY is required for native preflight")
        client = db.Historical(api_key)
    run = run_stock_target_backfill(root, client=client, start=args.start, through=args.through,
        execute=args.execute, plan_only=args.plan_only, resume_run=args.resume_run)
    print(json.dumps({"run_path": str(run), "latest_attempt": json.loads((run / "latest-attempt.json").read_text()),
                      "orders_placed": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
