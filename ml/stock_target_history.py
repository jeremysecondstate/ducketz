"""Maintain verified, included XNAS minute history for independent stock targets."""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, timedelta
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_cold_start import (
    COORDINATOR_VERSION, MANIFEST_VERSION, PLAN_DATASET_US_EQUITIES, STANDARD_PLAN_AUTHORITY,
    _checksum, _entry_storage_path, _overlap_days, _read_request_cursor, _request_kwargs,
    _validate_execution_request_identity, _validate_manifest_checksum, _validate_manifest_included_scope,
    _verify_generic_partition, _window_start, _write_json_atomic, discover_dataset_catalog,
    execute_manifest, preflight_manifest, schema_window,
)
from datafetching.databento_storage import HISTORY_PROFILE, MARKET_US_EQUITIES, dataset_root
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS


def latest_completed_through(as_of=None) -> date:
    local = utc_timestamp(as_of).tz_convert("America/Los_Angeles")
    calendar = xcals.get_calendar("XNYS")
    session = calendar.date_to_session(local.date(), direction="previous")
    if session.date() == local.date() and local.hour < 17:
        session = calendar.previous_session(session)
    return session.date() + timedelta(days=1)


def _verified_target_cursor(root: Path, symbol: str) -> dict | None:
    """A cursor advances upkeep only with its exact complete native evidence."""
    cursor = _read_request_cursor(root, market=MARKET_US_EQUITIES, dataset="XNAS.ITCH",
                                  schema="ohlcv-1m", symbol=symbol)
    if cursor is None:
        return None
    if cursor.get("status") not in {"PUBLISHED", "VERIFIED_EXISTING"}:
        raise ValueError(f"{symbol} target history cursor has no published native data")
    directory = Path(str(cursor.get("storage_path", ""))).resolve()
    source_root = dataset_root(root, market=MARKET_US_EQUITIES, dataset="XNAS.ITCH").resolve()
    if not directory.is_relative_to(source_root):
        raise ValueError(f"{symbol} target history cursor escapes its source archive")
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        request = manifest["request"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{symbol} target history cursor has no verified archive partition") from exc
    if (not isinstance(request, dict) or request.get("dataset") != "XNAS.ITCH"
            or request.get("schema") != "ohlcv-1m" or request.get("symbol_scope") != [symbol]
            or request.get("stype_in") != "raw_symbol"):
        raise ValueError(f"{symbol} target history cursor has a different source identity")
    _validate_execution_request_identity(root, request)
    if (Path(str(request["storage_path"])).resolve() != directory
            or any(cursor.get(key) != request.get(key) for key in ("request_id", "start", "end", "fetch_mode"))
            or cursor.get("completed_through") != request.get("end")):
        raise ValueError(f"{symbol} target history cursor differs from its verified request")
    for name in ("raw", "normalized"):
        payload = manifest.get(name, {})
        path = (directory / str(payload.get("path", ""))).resolve()
        if not path.is_relative_to(directory):
            raise ValueError(f"{symbol} target history payload escapes its archive partition")
    _verify_generic_partition(directory, request)
    return dict(cursor)


def build_target_history_manifest(root: Path, *, through: date, symbols=STOCK_TRADER_SYMBOLS) -> dict:
    """Restrict native bootstrap/overlap requests to seven-symbol XNAS OHLCV1m."""
    root = Path(root).resolve()
    universe = tuple(symbols)
    if universe != tuple(STOCK_TRADER_SYMBOLS):
        raise ValueError("Target history requires the complete production stock universe")
    requests = []
    window = schema_window(PLAN_DATASET_US_EQUITIES, "ohlcv-1m")
    baseline_start = _window_start(through, window)
    for symbol in sorted(universe):
        cursor = _verified_target_cursor(root, symbol)
        completed = date.fromisoformat(cursor["completed_through"]) if cursor is not None else None
        if completed is not None and completed >= through:
            continue
        start = max(baseline_start, completed - timedelta(days=_overlap_days("ohlcv-1m"))) if completed else baseline_start
        identity = {"dataset": "XNAS.ITCH", "standard_plan_dataset": PLAN_DATASET_US_EQUITIES,
                    "schema": "ohlcv-1m", "symbol_scope": [symbol], "stype_in": "raw_symbol",
                    "start": start.isoformat(), "end": through.isoformat(),
                    "storage_contract": "isolated-cold-start", "window": window,
                    "fetch_mode": "overlap-fill" if completed else "initial-baseline",
                    "baseline_start": baseline_start.isoformat(),
                    "previous_completed_through": completed.isoformat() if completed else None}
        path = _entry_storage_path(root, dataset="XNAS.ITCH", market=MARKET_US_EQUITIES,
                                   schema="ohlcv-1m", symbol=symbol, start=start, end=through,
                                   contract="isolated-cold-start")
        request = {"request_id": _checksum(identity)[:24], **identity,
                   "storage_path": str(path), "status": "PENDING"}
        _validate_execution_request_identity(root, request)
        requests.append(request)
    body = {"schema_version": MANIFEST_VERSION, "coordinator_version": COORDINATOR_VERSION,
            "history_profile": HISTORY_PROFILE, "as_of": through.isoformat(), "datastore_root": str(root),
            "entitlement_authority": STANDARD_PLAN_AUTHORITY, "requests": requests, "derived_views": []}
    _validate_manifest_included_scope(body)
    digest = _checksum(body)
    result = {"manifest_id": digest[:24], **body, "semantic_checksum_sha256": digest}
    _validate_manifest_checksum(result)
    return result


LIVE_REPLAY_MAX_BYTES = 64 * 1024 * 1024
LIVE_REPLAY_TIMEOUT_SECONDS = 300


def _action_session_end(session: date) -> pd.Timestamp:
    return (pd.Timestamp(session).tz_localize("America/Los_Angeles")
            + pd.Timedelta(hours=17)).tz_convert("UTC")


def _historical_request_through(through: date) -> date:
    """Native exclusive UTC date-end covering the requested action sessions.

    ``through`` remains the day after the final requested exchange session.
    During Pacific Standard Time its 17:00 action close is 01:00 UTC on that
    date. The native date-based archive therefore needs the following midnight
    to include the tail. Provider range checks still govern acquisition; this
    does not assert that the rounded-up native interval is already available.
    """
    calendar = xcals.get_calendar("XNYS")
    final_session = calendar.date_to_session(through - timedelta(days=1), direction="previous")
    return max(through, _action_session_end(final_session.date()).ceil("D").date())


def _missing_sessions(completed_through: date, through: date) -> tuple[str, ...]:
    if completed_through > through:
        return ()
    calendar = xcals.get_calendar("XNYS")
    historical_end = pd.Timestamp(completed_through, tz="UTC")
    # A date cursor is an exclusive UTC endpoint, not an action-session claim.
    # In Pacific Standard Time the previous date's 17:00 close is 01:00 UTC,
    # so even a same-date cursor can leave its final action hour uncovered.
    return tuple(value.date().isoformat() for value in calendar.sessions_in_range(
        pd.Timestamp(completed_through - timedelta(days=1)), pd.Timestamp(through - timedelta(days=1)))
        if _action_session_end(value.date()) > historical_end)


def _needs_session_coverage(root: Path, through: date) -> bool:
    return any((cursor := _verified_target_cursor(root, symbol)) is None or
               _missing_sessions(date.fromisoformat(cursor["completed_through"]), through)
               for symbol in STOCK_TRADER_SYMBOLS)


def _historical_acquisition(root: Path, *, client, manifest: dict, catalog: dict,
                            run: Path, execute: bool, reporter, prefix: str = "") -> dict:
    """Keep the existing exact-cost and native publication checks on both paths."""
    bounds = catalog["ohlcv-1m"]
    costs = []
    for request in manifest["requests"]:
        if request["start"] < bounds["start"] or request["end"] > bounds["end"]:
            raise ValueError("Provider range does not cover the exact stock history request")
        cost = float(client.metadata.get_cost(**_request_kwargs(request)))
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("Provider returned an invalid stock history cost")
        costs.append({"request_id": request["request_id"], "estimated_cost_usd": cost})
    _write_json_atomic(run / f"{prefix}cost-preflight.json", {"dataset_range": catalog, "requests": costs,
        "total_cost_usd": sum(row["estimated_cost_usd"] for row in costs),
        "maximum_cost_usd": 0.0, "generated_at": utc_timestamp().isoformat()})
    if any(row["estimated_cost_usd"] != 0 for row in costs):
        raise ValueError("Stock target history requires included zero-cost requests")
    preflight = preflight_manifest(client, datastore_root=root, manifest=manifest)
    _write_json_atomic(run / f"{prefix}preflight.json", preflight)
    if not preflight["capacity_pass"]:
        raise ValueError("Stock target history capacity preflight failed")
    counts = execute_manifest(client, datastore_root=root, manifest=manifest, preflight=preflight,
        reporter=reporter, progress_path=run / f"{prefix}progress.json") if execute else {}
    if execute and counts.get("failed", 0):
        raise RuntimeError("Native stock target history acquisition failed; inspect durable progress")
    if execute:
        for request in manifest["requests"]:
            symbol = request["symbol_scope"][0]
            cursor = _verified_target_cursor(root, symbol)
            if cursor is None or date.fromisoformat(cursor["completed_through"]) < date.fromisoformat(request["end"]):
                raise RuntimeError(f"{symbol} target history did not publish verified completion")
    return counts


def _live_fallback(root: Path, *, client, manifest: dict, catalog: dict, through: date,
                   run: Path, execute: bool, reporter, api_key: str | None) -> Path:
    """Fill completed recent sessions without mislabeling Live as Historical."""
    from datafetching.xnas_replay_archive import (
        partition_directory, publish_session, session_bounds, verify_partition,
    )

    cursors = {symbol: _verified_target_cursor(root, symbol) for symbol in STOCK_TRADER_SYMBOLS}
    if any(cursor is None for cursor in cursors.values()):
        raise ValueError("Live target fallback requires a verified Historical baseline for every symbol")
    # Catch up genuinely older sessions through Historical first. The replay
    # window must never be shortened to jump over missing exchange dates.
    available_through = min(_historical_request_through(through),
                            date.fromisoformat(catalog["ohlcv-1m"]["end"]))
    available_end = pd.Timestamp(available_through, tz="UTC")
    # A provider date-end can close older sessions while leaving the newest
    # winter session's 00:00--01:00 UTC tail missing. Only actual action closes
    # establish useful prefix coverage; never reinterpret this date as a new
    # requested action horizon or advance past the provider's available end.
    needs_prefix = any(
        _action_session_end(date.fromisoformat(session)) <= available_end
        for cursor in cursors.values()
        for session in _missing_sessions(date.fromisoformat(cursor["completed_through"]), through)
    )
    if needs_prefix:
        prefix_manifest = build_target_history_manifest(root, through=available_through)
        if prefix_manifest["requests"]:
            _write_json_atomic(run / "historical-prefix-manifest.json", prefix_manifest)
            _historical_acquisition(root, client=client, manifest=prefix_manifest, catalog=catalog,
                                   run=run, execute=execute, reporter=reporter, prefix="historical-prefix-")
            if not execute:
                _write_json_atomic(run / "receipt.json", {"status": "PREFLIGHTED_HISTORICAL_PREFIX",
                    "requires_live_session_preflight_after_prefix": True, "orders_placed": 0,
                    "manifest_sha256": file_checksum(run / "manifest.json")})
                return run
            cursors = {symbol: _verified_target_cursor(root, symbol) for symbol in STOCK_TRADER_SYMBOLS}

    planned, verified = [], []
    for symbol, cursor in cursors.items():
        for session in _missing_sessions(date.fromisoformat(cursor["completed_through"]), through):
            destination = partition_directory(root, symbol=symbol, session=session)
            if destination.exists():
                verified.append(verify_partition(destination, root=root))
                continue
            start, end = session_bounds(session)
            if utc_timestamp() - start > pd.Timedelta(hours=24):
                raise ValueError(f"Missing {symbol} session {session} is outside Live replay retention; Historical catch-up required")
            request = {"dataset": "XNAS.ITCH", "schema": "ohlcv-1m", "symbols": [symbol],
                       "stype_in": "raw_symbol", "start": start.isoformat(), "end": end.isoformat()}
            cost = float(client.metadata.get_cost(**request))
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("Provider returned an invalid exact stock session cost")
            planned.append({"symbol": symbol, "session": session, "request": request,
                            "historical_estimated_cost_usd": cost})
    preflight = {"schema_version": "xnas-stock-live-fallback-preflight-v1",
        "reason": "HISTORICAL_SESSION_COVERAGE_INCOMPLETE", "historical_available_range": catalog,
        "source_dataset": "XNAS.ITCH", "required_completed_through": through.isoformat(),
        "requests": planned, "reused_sessions": [{"symbol": item["symbol"], "session": item["session"]} for item in verified],
        "maximum_bytes_per_session": LIVE_REPLAY_MAX_BYTES, "timeout_seconds": LIVE_REPLAY_TIMEOUT_SECONDS,
        "historical_cost_is_not_live_entitlement": True, "generated_at": utc_timestamp().isoformat()}
    _write_json_atomic(run / "live-fallback-preflight.json", preflight)
    if any(item["historical_estimated_cost_usd"] != 0 for item in planned):
        raise ValueError("Stock target Live fallback requires exact zero-cost session preflights")
    if planned:
        _live_subscription_preflight(client, preflight, run)
    if execute and planned and not api_key:
        raise ValueError("DATABENTO_API_KEY is required for the Live target fallback")
    progress = {"status": "RUNNING" if execute else "PREFLIGHTED", "completed": [], "orders_placed": 0}
    if execute:
        for item in planned:
            if reporter:
                reporter(f"XNAS_LIVE_FALLBACK_START symbol={item['symbol']}; session={item['session']}")
            try:
                verified.append(publish_session(root, symbol=item["symbol"], session=item["session"],
                    api_key=api_key, max_bytes=LIVE_REPLAY_MAX_BYTES, timeout_seconds=LIVE_REPLAY_TIMEOUT_SECONDS))
            except Exception as exc:
                # Preserve provider evidence in failed staging; sanitize operator logs.
                reason = str(exc).replace(api_key, "[REDACTED]") if api_key else str(exc)
                progress.update(status="FAILED", failed_symbol=item["symbol"], failed_session=item["session"],
                                error_type=type(exc).__name__, error=reason)
                _write_json_atomic(run / "live-fallback-progress.json", progress)
                raise RuntimeError(f"XNAS Live fallback failed for {item['symbol']}/{item['session']}: {reason}") from None
            progress["completed"].append({"symbol": item["symbol"], "session": item["session"]})
            _write_json_atomic(run / "live-fallback-progress.json", progress)
    if execute:
        # Re-read all receipts after acquisition, including scopes reused on a
        # restart. Native Historical cursors remain unchanged and truthful.
        verified = []
        for symbol, cursor in cursors.items():
            for session in _missing_sessions(date.fromisoformat(cursor["completed_through"]), through):
                verified.append(verify_partition(partition_directory(root, symbol=symbol, session=session), root=root))
        progress["status"] = "COMPLETE"
        _write_json_atomic(run / "live-fallback-progress.json", progress)
    _write_json_atomic(run / "receipt.json", {"status": "COMPLETE" if execute else "PREFLIGHTED",
        "completed_through": through.isoformat(), "coverage_basis": "HISTORICAL_PLUS_VERIFIED_XNAS_ACTION_SESSIONS",
        "historical_cursor_ends": {symbol: cursor["completed_through"] for symbol, cursor in cursors.items()},
        "replay_manifests": [{"path": str(item["manifest_path"]), "sha256": file_checksum(item["manifest_path"])} for item in verified],
        "manifest_sha256": file_checksum(run / "manifest.json"),
        "live_preflight_sha256": file_checksum(run / "live-fallback-preflight.json"),
        "estimated_cost_usd": 0.0, "orders_placed": 0, "completed_at": utc_timestamp().isoformat()})
    return run


def _live_subscription_preflight(client, preflight: dict, run: Path) -> None:
    """Bind replay to the deployed flat plan, never interpret tariffs as quotes.

    The deployment's existing Standard subscription is the authority for
    included Live acquisition. The gateway must also accept this exact XNAS
    subscription: generic US Equities access alone does not prove XNAS access.
    This path cannot purchase or activate a plan/license. Historical get_cost's
    deprecated mode argument is deliberately not used as a Live price quote.
    """
    prices = client.metadata.list_unit_prices(dataset="XNAS.ITCH")
    preflight["generic_unit_prices_informational_only"] = prices
    preflight["live_cost_basis"] = {"basis": "EXISTING_FLAT_SUBSCRIPTION",
        "plan_authority": STANDARD_PLAN_AUTHORITY, "incremental_acquisition_cost_usd": 0.0,
        "exact_live_interval_quote": False, "requires_native_access_acceptance": True,
        "policy_source": "https://databento.com/pricing",
        "account_scope": "DEPLOYED_STANDARD_SUBSCRIPTION_NOT_GENERIC_METERED_ACCOUNTS"}
    preflight["live_entitlement_check"] = "NATIVE_SUBSCRIPTION_ACK_REQUIRED_NO_LICENSE_PURCHASE"
    _write_json_atomic(run / "live-fallback-preflight.json", preflight)


def maintain_target_history(root: Path, *, client, through: date | None = None,
                            execute: bool = False, reporter=print, api_key: str | None = None) -> Path:
    """Cost-preflight exact requests before native download; never request paid data."""
    root = Path(root).resolve()
    through = through or latest_completed_through()
    if through > latest_completed_through():
        raise ValueError("Target history cannot request an incomplete exchange session")
    run = create_timestamp_directory(root / "ml/stock-target-history-runs")
    with exclusive_runtime_lock(root / ".ducketz-databento-cold-start.lock",
                                process_name="Independent stock target history"):
        manifest = build_target_history_manifest(root, through=_historical_request_through(through))
        _write_json_atomic(run / "manifest.json", manifest)
        if not manifest["requests"] and not _needs_session_coverage(root, through):
            _write_json_atomic(run / "receipt.json", {"status": "CURRENT", "completed_through": through.isoformat(),
                "manifest_sha256": file_checksum(run / "manifest.json"), "orders_placed": 0})
            return run
        catalog = discover_dataset_catalog(client, dataset="XNAS.ITCH", required_schemas=("ohlcv-1m",))
        bounds = catalog["ohlcv-1m"]
        if any(request["start"] < bounds["start"] for request in manifest["requests"]):
            raise ValueError("Provider range does not cover the exact stock history request")
        if not manifest["requests"] or any(request["end"] > bounds["end"] for request in manifest["requests"]):
            return _live_fallback(root, client=client, manifest=manifest, catalog=catalog,
                through=through, run=run, execute=execute, reporter=reporter, api_key=api_key)
        counts = _historical_acquisition(root, client=client, manifest=manifest, catalog=catalog,
                                        run=run, execute=execute, reporter=reporter)
        if execute and _needs_session_coverage(root, through):
            return _live_fallback(root, client=client, manifest=manifest, catalog=catalog,
                through=through, run=run, execute=execute, reporter=reporter, api_key=api_key)
        _write_json_atomic(run / "receipt.json", {
            "status": "COMPLETE" if execute else "PREFLIGHTED", "completed_through": through.isoformat(),
            "counts": counts, "manifest_sha256": file_checksum(run / "manifest.json"),
            "preflight_sha256": file_checksum(run / "preflight.json"),
            "cost_preflight_sha256": file_checksum(run / "cost-preflight.json"),
            "estimated_cost_usd": 0.0, "orders_placed": 0, "completed_at": utc_timestamp().isoformat(),
        })
        return run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    data = parser.add_mutually_exclusive_group()
    data.add_argument("--datastore", type=Path)
    data.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc")
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=None if args.datastore else args.datastore_target)
    load_repository_environment()
    import databento as db
    api_key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DATABENTO_API_KEY is required")
    run = maintain_target_history(root, client=db.Historical(api_key), through=args.through,
                                  execute=args.execute, api_key=api_key)
    print(f"Stock target history receipt: {run / 'receipt.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
