"""Plan and resume one symbol's bootstrap using the existing verified writers.

The reference symbol supplies retained archive windows. Shared CME and macro
history is reused. A separate candidate watchlist lets the ordinary overnight
pipeline train the expanded universe before its production list is activated.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_cold_start import (
    OPRA_SCHEMAS, US_EQUITIES_SCHEMAS, ColdStartRequest,
    _download_generic_entry, _entry_storage_path, _execute_opra_entry,
    _request_kwargs, schema_window,
)
from datafetching.main import run_symbol_fetch
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.symbol_universe import REPOSITORY_WATCHLIST, WATCHLIST_ENV, normalize_symbol, read_symbols

VERSION = "symbol-onboarding-v1"
PRODUCTION_OPRA_SCHEMAS = ("definition", "ohlcv-1h", "cbbo-1m")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _checksum(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inventory(root: Path, symbol: str) -> dict:
    """Logical file sizes, including native, normalized, and retained staging."""
    total = symbol_bytes = count = 0
    for directory, _, filenames in os.walk(root):
        owned = bool({symbol, symbol + ".OPT"}.intersection(Path(directory).relative_to(root).parts))
        for filename in filenames:
            try:
                size = (Path(directory) / filename).stat().st_size
            except FileNotFoundError:
                continue  # An unrelated atomic publication may replace its temporary file.
            total += size
            symbol_bytes += size if owned else 0
            count += 1
    return {"observed_at": _now(), "datastore_bytes": total, "symbol_path_bytes": symbol_bytes,
            "files": count, "free_bytes": shutil.disk_usage(root).free}


def _request(root: Path, symbol: str, dataset: str, schema: str, start: str, end: str) -> dict:
    opra = dataset == "OPRA.PILLAR"
    scope = symbol + ".OPT" if opra else symbol
    role = "OPRA" if opra else "US_EQUITIES"
    contract = "canonical-opra" if opra else "isolated-cold-start"
    destination = _entry_storage_path(root, dataset=dataset,
        market="opra" if opra else "us-equities", schema=schema, symbol=scope,
        start=date.fromisoformat(start), end=date.fromisoformat(end), contract=contract)
    identity = {"dataset": dataset, "schema": schema, "symbol": scope, "start": start, "end": end}
    return asdict(ColdStartRequest(request_id=_checksum(identity)[:24], dataset=dataset,
        standard_plan_dataset=role, schema=schema, symbol_scope=(scope,),
        stype_in="parent" if opra else "raw_symbol", start=start, end=end,
        storage_path=str(destination), storage_contract=contract,
        window=schema_window(role, schema), baseline_start=start))


def reference_requests(root: Path, symbol: str, reference: str, catalog: dict) -> list[dict]:
    base = root / "market-data" / "databento"
    requests = []
    ordered = (*PRODUCTION_OPRA_SCHEMAS, *(s for s in OPRA_SCHEMAS if s not in PRODUCTION_OPRA_SCHEMAS))
    for schema in ordered:
        dates = base / "opra" / "OPRA.PILLAR" / schema / (reference + ".OPT") / "dates"
        retained = sorted(p.name for p in dates.iterdir() if p.is_dir() and any(p.glob("segments/*/receipt.json")))
        if not retained:
            raise ValueError(f"No published reference history: {dates}")
        start = retained[0]
        end = (date.fromisoformat(retained[-1]) + timedelta(days=1)).isoformat()
        if schema in PRODUCTION_OPRA_SCHEMAS:
            available = str(catalog.get("schema", {}).get(schema, catalog)["end"])[:10]
            end = available
        if start >= end:
            raise ValueError(f"No available reference window for {schema}: {start} to {end}")
        requests.append(_request(root, symbol, "OPRA.PILLAR", schema, start, end))
    for schema in US_EQUITIES_SCHEMAS:
        windows = base / "us-equities" / "XNAS.ITCH" / schema / reference / "windows"
        manifests = sorted(windows.glob("*/manifest.json"))
        if not manifests:
            raise ValueError(f"No published reference archive: {windows}")
        for manifest in manifests:
            if not (manifest.parent / "receipt.json").is_file():
                raise ValueError(f"Unreceipted reference archive: {manifest}")
            prior = json.loads(manifest.read_text(encoding="utf-8"))["request"]
            requests.append(_request(root, symbol, str(prior["dataset"]), schema, str(prior["start"]), str(prior["end"])))
    return requests


def enforce_budget(estimates: list[dict], *, max_billable_bytes: int, max_cost_usd: float, free_bytes: int) -> dict:
    billable = sum(int(e["estimated_download_size_bytes"]) for e in estimates)
    cost = sum(float(e["cost_usd"]) for e in estimates)
    required = billable * 2 + 5 * 1024**3
    if any(int(e["record_count"]) <= 0 for e in estimates):
        raise ValueError("A requested history family has no provider records")
    if billable > max_billable_bytes or cost > max_cost_usd or required > free_bytes:
        raise ValueError(f"Bootstrap budget exceeded: billable_bytes={billable}, cost_usd={cost}, required_free_bytes={required}")
    return {"billable_uncompressed_bytes": billable, "estimated_cost_usd": cost,
            "required_free_bytes": required, "max_billable_bytes": max_billable_bytes, "max_cost_usd": max_cost_usd}


def build_plan(root: Path, client: object, *, symbol: str, reference: str,
               max_billable_bytes: int = 20_000_000_000, max_cost_usd: float = 1.0) -> dict:
    symbol, reference = normalize_symbol(symbol), normalize_symbol(reference)
    current = read_symbols()
    if symbol in current or reference not in current:
        raise ValueError("Candidate must be new and its reference must already be active")
    catalog = client.metadata.get_dataset_range(dataset="OPRA.PILLAR")
    requests = reference_requests(root, symbol, reference, catalog)

    def estimate(request: dict) -> dict:
        arguments = _request_kwargs(request)
        return {"request_id": request["request_id"],
                "estimated_download_size_bytes": client.metadata.get_billable_size(**arguments),
                "record_count": client.metadata.get_record_count(**arguments),
                "cost_usd": client.metadata.get_cost(**arguments)}

    with ThreadPoolExecutor(max_workers=4) as executor:
        estimates = list(executor.map(estimate, requests))
    budget = enforce_budget(estimates, max_billable_bytes=max_billable_bytes,
        max_cost_usd=max_cost_usd, free_bytes=shutil.disk_usage(root).free)
    payload = {"schema_version": VERSION, "created_at": _now(), "datastore_root": str(root),
               "symbol": symbol, "reference": reference, "previous_symbols": list(current),
               "candidate_symbols": [*current, symbol], "requests": requests, "estimates": estimates,
               "budget": budget, "shared_history": "Reuse existing CME and macro datasets",
               "size_units": "Provider billing bytes are uncompressed DBN, not final stored bytes"}
    return {**payload, "plan_id": _checksum(payload)}


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    payload = {k: v for k, v in plan.items() if k != "plan_id"}
    if plan.get("schema_version") != VERSION or plan.get("plan_id") != _checksum(payload):
        raise ValueError("Onboarding plan checksum or version is invalid")
    root = Path(plan["datastore_root"]).resolve()
    for request in plan["requests"]:
        expected = _request(root, plan["symbol"], request["dataset"], request["schema"], request["start"], request["end"])
        if json.loads(json.dumps(expected)) != request:
            raise ValueError("Onboarding request does not match its canonical destination and scope")
    return plan


def register_onboarding(path: Path, plan: dict) -> Path:
    """Let the existing Health Watch discover an explicitly started bootstrap."""
    destination = Path(plan["datastore_root"]) / "state/symbol-onboarding" / f"{plan['symbol']}.json"
    _write(destination, {"schema_version": VERSION, "plan_id": plan["plan_id"], "symbol": plan["symbol"],
        "plan_path": str(path.resolve()), "progress_path": str((path.parent / "progress.json").resolve()),
        "activation_path": str((path.parent / "activation.json").resolve()), "registered_at": _now()})
    return destination


def queue_history(path: Path, client: object) -> dict:
    """Prepare independent provider batch jobs; the fetcher remains the writer.

    This can run while a different schema's job is processing. It never writes
    canonical partitions, history cursors, or production publication pointers.
    """
    from datafetching.databento_opra_history import (
        OPRA_BATCH_MIN_DAYS, SyncScope,
        _batch_job_states, _load_or_submit_batch_job, _partition_plan,
    )
    plan = load_plan(path)
    root = Path(plan["datastore_root"])
    enforce_budget(plan["estimates"], max_billable_bytes=plan["budget"]["max_billable_bytes"],
        max_cost_usd=plan["budget"]["max_cost_usd"], free_bytes=shutil.disk_usage(root).free)
    jobs = []
    with exclusive_runtime_lock(path.parent / "queue.lock", process_name="Duckets onboarding batch preparation"):
        for request in plan["requests"]:
            schema = request["schema"]
            if request["dataset"] != "OPRA.PILLAR":
                continue
            parents = request["symbol_scope"]
            existing = _batch_job_states(root, schema=schema, symbols=parents)
            if existing:
                jobs.extend({"schema": schema, "job_id": state["job_id"], "status": "REUSED"} for _, state in existing)
                continue
            if any(Path(request["storage_path"]).glob("dates/*/segments/*/receipt.json")):
                continue  # Let the normal writer calculate only the remaining gaps.
            entitlement = {"entitlements": {schema: {"entitled_start": request["start"], "entitled_end": request["end"]}}}
            parts = _partition_plan(client, entitlement=entitlement, scope=SyncScope(
                schemas=(schema,), symbols=tuple(parents), start=request["start"], end=request["end"]))
            days = tuple(day for _, day in parts)
            if len(days) < OPRA_BATCH_MIN_DAYS:
                continue
            _, state = _load_or_submit_batch_job(client, datastore_root=root, schema=schema,
                symbols=parents, start=days[0], end=(date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat(),
                planned_dates=days, reporter=print)
            jobs.append({"schema": schema, "job_id": state["job_id"], "status": "QUEUED"})
            _write(path.parent / "batch-preparation.json", {"plan_id": plan["plan_id"], "jobs": jobs})
    receipt = {"plan_id": plan["plan_id"], "prepared_at": _now(), "jobs": jobs}
    _write(path.parent / "batch-preparation.json", receipt)
    return receipt


def fetch_plan(path: Path, client: object) -> dict:
    plan = load_plan(path)
    register_onboarding(path, plan)
    root = Path(plan["datastore_root"])
    progress_path = path.parent / "progress.json"
    with exclusive_runtime_lock(path.parent / "fetch.lock", process_name="Duckets symbol onboarding"):
        progress = json.loads(progress_path.read_text()) if progress_path.exists() else {
            "plan_id": plan["plan_id"], "before": inventory(root, plan["symbol"]), "completed_requests": [], "providers": {}}
        if progress["plan_id"] != plan["plan_id"]:
            raise ValueError("Progress belongs to a different onboarding plan")
        enforce_budget(plan["estimates"], max_billable_bytes=plan["budget"]["max_billable_bytes"],
            max_cost_usd=plan["budget"]["max_cost_usd"], free_bytes=shutil.disk_usage(root).free)
        candidate = path.parent / "candidate-watchlist.txt"
        candidate.write_text("\n".join(plan["candidate_symbols"]) + "\n", encoding="utf-8")
        progress["status"] = "FETCHING"
        _write(progress_path, progress)
        try:
            for provider in ("databento", "fmp", "schwab", "sec"):
                previous = progress["providers"].get(provider, {})
                if previous and previous.get("error_files") == 0:
                    continue
                result, = run_symbol_fetch(plan["symbol"], ParquetStore(root), providers=(provider,),
                    include_cme=False, include_fmp_macro=False, include_options=True, include_schwab_price_history=True)
                progress["providers"][provider] = asdict(result)
                _write(progress_path, progress)
                if result.error_files:
                    raise RuntimeError(f"{provider} bootstrap has {result.error_files} hard failures")
            for request, estimate in zip(plan["requests"], plan["estimates"], strict=True):
                print(f"ONBOARDING {request['dataset']}/{request['schema']} {request['start']} to {request['end']}", flush=True)
                # Reusing a scope still invokes the writer's checksum verification.
                if request["dataset"] == "OPRA.PILLAR":
                    _execute_opra_entry(client, datastore_root=root, request=request, manifest_id=plan["plan_id"],
                        reporter=print, preflight_estimate=estimate, available_free_bytes=shutil.disk_usage(root).free)
                else:
                    _download_generic_entry(client, datastore_root=root, request=request, reporter=print)
                if request["request_id"] not in progress["completed_requests"]:
                    progress["completed_requests"].append(request["request_id"])
                _write(progress_path, progress)
            progress["status"] = "HISTORY_FETCHED"
            progress.pop("error", None)
        except Exception as exc:
            progress.update(status="FAILED", error=str(exc))
            raise
        finally:
            progress["updated_at"] = _now()
            progress["after"] = inventory(root, plan["symbol"])
            progress["datastore_growth_bytes"] = progress["after"]["datastore_bytes"] - progress["before"]["datastore_bytes"]
            progress["symbol_path_growth_bytes"] = progress["after"]["symbol_path_bytes"] - progress["before"]["symbol_path_bytes"]
            _write(progress_path, progress)
    return progress


def complete_operational_history(plan: dict, client: object, output: Path) -> dict:
    """Extend native equity bars to the reference's retained first session."""
    import pandas as pd
    from app.services.databento_market_data import DatabentoMarketDataProvider
    from datafetching.continuation import normalized_bar_path
    from datafetching.databento_fetch import _persist_native_results

    root = Path(plan["datastore_root"])
    provider = DatabentoMarketDataProvider()
    catalog = provider.dataset_range()
    results = []
    for spec in provider.native_specs():
        key = f"{spec.key}_{spec.schema}_{spec.frequency}"
        reference_path = normalized_bar_path(root, source="databento", symbol=plan["reference"],
            timeframe=spec.frequency, request_key=key)
        reference = pd.to_datetime(pd.read_parquet(reference_path, columns=["timestamp"])["timestamp"], utc=True)
        available = provider.available_range_for_schema(spec.schema, dataset_range=catalog)
        start = max(pd.Timestamp(available.start), reference.min().floor("D"))
        target_path = normalized_bar_path(root, source="databento", symbol=plan["symbol"],
            timeframe=spec.frequency, request_key=key)
        current = pd.to_datetime(pd.read_parquet(target_path, columns=["timestamp"])["timestamp"], utc=True)
        item = {"dataset": provider.dataset, "schema": spec.schema, "reference_path": str(reference_path),
                "requested_start": start.isoformat(), "requested_end": available.end.isoformat()}
        if not current.empty and current.min().floor("D") <= start:
            results.append({**item, "status": "ALREADY_COVERED", "rows": len(current)})
            continue
        arguments = {"dataset": provider.dataset, "schema": spec.schema, "symbols": [plan["symbol"]],
                     "stype_in": "raw_symbol", "start": start.isoformat(), "end": available.end.isoformat()}
        estimate = {"estimated_download_size_bytes": client.metadata.get_billable_size(**arguments),
                    "record_count": client.metadata.get_record_count(**arguments), "cost_usd": client.metadata.get_cost(**arguments)}
        enforce_budget([*plan["estimates"], estimate], max_billable_bytes=plan["budget"]["max_billable_bytes"],
            max_cost_usd=plan["budget"]["max_cost_usd"], free_bytes=shutil.disk_usage(root).free)
        request_spec = replace(spec, lookback=(pd.Timestamp(available.end) - start).to_pytimedelta())
        bars, raw, selected = provider.fetch_native_bars(plan["symbol"], request_spec, available_range=available)
        persisted = _persist_native_results(plan["symbol"], ParquetStore(root), provider=provider,
            profile="symbol-onboarding-reference-history", observed_at=datetime.now(timezone.utc),
            native_results=[(spec, bars, raw, selected, None)])
        if persisted.error_files:
            raise RuntimeError(f"Operational {spec.schema} parity backfill failed")
        results.append({**item, "status": "BACKFILLED", "rows": len(bars), "preflight": estimate})
        _write(output, {"plan_id": plan["plan_id"], "status": "IN_PROGRESS", "results": results})
    receipt = {"plan_id": plan["plan_id"], "status": "COMPLETE", "results": results, "completed_at": _now()}
    _write(output, receipt)
    return receipt


def screen_secondary_history(plan: dict, output: Path) -> dict:
    """Preserve unusable Schwab equity series outside the technicals input tree.

    Negative adjusted provider history is evidence, but cannot be interpreted
    as executable equity OHLC or fed into return/log-price calculations.
    """
    import numpy as np
    import pandas as pd
    from ml.artifacts import file_checksum

    root = Path(plan["datastore_root"]).resolve()
    stock_root = (root / "stocks" / plan["symbol"] / "bars").resolve()
    quarantine = (root / "quarantine/symbol-onboarding" / plan["symbol"] / plan["plan_id"]).resolve()
    previous = json.loads(output.read_text()) if output.exists() else {"plan_id": plan["plan_id"], "excluded_series": []}
    if previous["plan_id"] != plan["plan_id"]:
        raise ValueError("Secondary-history receipt belongs to another plan")
    excluded = list(previous["excluded_series"])
    for source in sorted(stock_root.glob("*/schwab/normalized/*.parquet")):
        frame = pd.read_parquet(source)
        prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        invalid = (~np.isfinite(prices) | prices.le(0)).any(axis=1)
        if not invalid.any():
            continue
        destination = (quarantine / source.relative_to(stock_root)).resolve()
        if not source.resolve().is_relative_to(stock_root) or not destination.is_relative_to(root / "quarantine"):
            raise ValueError("Secondary-history quarantine escaped its declared datastore paths")
        digest = file_checksum(source)
        if destination.exists():
            raise ValueError("Quarantine destination already exists; reconcile duplicate provider evidence")
        record = {"source_path": str(source), "quarantine_path": str(destination), "checksum_sha256": digest,
                  "rows": len(frame), "invalid_price_rows": int(invalid.sum()),
                  "reason": "NONPOSITIVE_OR_NONFINITE_SECONDARY_EQUITY_PRICES",
                  "samples": json.loads(frame.loc[invalid].head(8).to_json(orient="records", date_format="iso"))}
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        if file_checksum(destination) != digest:
            raise RuntimeError("Quarantined secondary history checksum changed")
        excluded.append(record)
        _write(output, {"plan_id": plan["plan_id"], "excluded_series": excluded, "status": "IN_PROGRESS"})
    receipt = {"plan_id": plan["plan_id"], "screened_at": _now(), "status": "COMPLETE",
               "excluded_series": excluded, "raw_provider_deliveries_preserved": True}
    _write(output, receipt)
    return receipt


def validate_candidate(plan: dict) -> dict:
    import pandas as pd
    from ml.nightly_gameplan import read_current_gameplan
    from ml.stock_trader.model import load_current_enrichment_model

    expected = tuple(plan["candidate_symbols"])
    if read_symbols() != expected:
        raise ValueError("Validation requires DUCKETS_PRODUCTION_WATCHLIST to select this plan's candidate watchlist")
    root = Path(plan["datastore_root"])
    publication = read_current_gameplan(root)
    if tuple(publication.manifest["configuration"]["symbols"]) != expected:
        raise ValueError("Current Gameplan does not contain the complete candidate universe")
    for filename in ("forecasts.parquet", "option-strategy-intents.parquet"):
        frame = pd.read_parquet(publication.run_directory / filename)
        counts = frame.groupby("symbol").size().to_dict()
        if counts != {symbol: 24 for symbol in expected} or frame.duplicated(["symbol", "route"]).any():
            raise ValueError(f"Incomplete or duplicate candidate routes in {filename}: {counts}")
    model = load_current_enrichment_model(root)
    if f"symbol_{plan['symbol']}" not in model.feature_names:
        raise ValueError("Stock trader model does not include the candidate")
    return {"plan_id": plan["plan_id"], "validated_at": _now(), "symbols": list(expected),
            "gameplan_run": str(publication.run_directory), "action_date": publication.receipt["action_date"],
            "forecasts": 24 * len(expected), "option_intents": 24 * len(expected),
            "stock_model_fingerprint": model.model_fingerprint, "orders_submitted": 0}


def run_loops(path: Path, client: object) -> dict:
    plan = load_plan(path)
    progress = json.loads((path.parent / "progress.json").read_text())
    if progress.get("plan_id") != plan["plan_id"] or progress.get("status") != "HISTORY_FETCHED":
        raise ValueError("Complete the history fetch before running the candidate Loops stack")
    screen_secondary_history(plan, path.parent / "secondary-history-quality.json")
    complete_operational_history(plan, client, path.parent / "operational-history.json")
    environment = dict(os.environ)
    environment[WATCHLIST_ENV] = str((path.parent / "candidate-watchlist.txt").resolve())
    root = plan["datastore_root"]
    for module, arguments in (
        ("ml.overnight_runtime", ["--datastore", root, "--once"]),
        ("ml.stock_trader.training", ["--root-dir", root]),
    ):
        subprocess.run([sys.executable, "-u", "-m", module, *arguments], env=environment,
            cwd=REPOSITORY_WATCHLIST.parent.parent, check=True)
    # A fresh process loads every universe-dependent contract from the candidate.
    subprocess.run([sys.executable, "-m", "datafetching.symbol_onboarding", "validate", "--plan", str(path.resolve())],
        env=environment, cwd=REPOSITORY_WATCHLIST.parent.parent, check=True)
    return json.loads((path.parent / "validation.json").read_text())


def activate_plan(path: Path) -> dict:
    plan = load_plan(path)
    validation = validate_candidate(plan)
    progress = json.loads((path.parent / "progress.json").read_text())
    operational = json.loads((path.parent / "operational-history.json").read_text())
    secondary = json.loads((path.parent / "secondary-history-quality.json").read_text())
    if progress.get("plan_id") != plan["plan_id"] or progress.get("status") != "HISTORY_FETCHED":
        raise ValueError("History receipt is not complete")
    if operational.get("plan_id") != plan["plan_id"] or operational.get("status") != "COMPLETE":
        raise ValueError("Operational history receipt is not complete")
    if secondary.get("plan_id") != plan["plan_id"] or secondary.get("status") != "COMPLETE":
        raise ValueError("Secondary history quality receipt is not complete")
    with exclusive_runtime_lock(REPOSITORY_WATCHLIST.with_suffix(".activation.lock"), process_name="Duckets universe activation"):
        current = read_symbols(REPOSITORY_WATCHLIST)
        if current not in (tuple(plan["previous_symbols"]), tuple(plan["candidate_symbols"])):
            raise ValueError("Production watchlist changed independently; reconcile before activation")
        if current != tuple(plan["candidate_symbols"]):
            temporary = REPOSITORY_WATCHLIST.with_suffix(".txt.tmp")
            previous_text = REPOSITORY_WATCHLIST.read_text(encoding="utf-8")
            temporary.write_text(previous_text.rstrip() + "\n" + plan["symbol"] + "\n", encoding="utf-8")
            temporary.replace(REPOSITORY_WATCHLIST)
        receipt = {**validation, "status": "ACTIVE", "activated_at": _now(),
                   "after": inventory(Path(plan["datastore_root"]), plan["symbol"])}
        _write(path.parent / "activation.json", receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("symbol")
    plan_parser.add_argument("--reference", default="AAPL")
    group = plan_parser.add_mutually_exclusive_group()
    group.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS))
    group.add_argument("--datastore", type=Path)
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--max-billable-bytes", type=int, default=20_000_000_000)
    plan_parser.add_argument("--max-cost-usd", type=float, default=1.0)
    for command in ("queue-history", "fetch", "run-loops", "validate", "activate"):
        phase = sub.add_parser(command)
        phase.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    load_repository_environment()
    if args.command == "validate":
        receipt = validate_candidate(load_plan(args.plan))
        _write(args.plan.parent / "validation.json", receipt)
        print(json.dumps(receipt))
        return 0
    if args.command == "activate":
        print(json.dumps(activate_plan(args.plan)))
        return 0
    import databento as db
    client = db.Historical(os.environ["DATABENTO_API_KEY"])
    if args.command == "plan":
        if args.output.exists():
            parser.error("Plan already exists; reuse it for fetch or choose a new output directory")
        root = resolve_datastore_dir(root_dir=args.datastore, target=args.datastore_target)
        plan = build_plan(root, client, symbol=args.symbol, reference=args.reference,
            max_billable_bytes=args.max_billable_bytes, max_cost_usd=args.max_cost_usd)
        _write(args.output, plan)
        print(json.dumps({"plan": str(args.output), "plan_id": plan["plan_id"], "budget": plan["budget"]}))
    elif args.command == "queue-history":
        print(json.dumps(queue_history(args.plan, client)))
    elif args.command == "fetch":
        print(json.dumps(fetch_plan(args.plan, client)))
    else:
        print(json.dumps(run_loops(args.plan, client)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
