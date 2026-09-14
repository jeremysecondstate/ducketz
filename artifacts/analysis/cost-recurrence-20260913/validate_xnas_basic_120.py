"""Isolated seven-symbol 120-session coverage comparison; no model/production writes."""
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path
import databento as db
import exchange_calendars as xcals
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path.cwd()))
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_xnas_replay import _external_failure
from ml.stock_target_prices import load_stock_target_prices, XNAS_STOCK_PRICE_SOURCE
from ml.independent_stock_targets import stock_target_windows, _target_observations, STOCK_TARGET_BOUNDARY_TOLERANCE

OUT = Path(__file__).parent / "source-probe/validation-120"
OUT.mkdir(parents=True, exist_ok=True)
SYMBOLS = tuple(line.strip() for line in Path("datafetching/watchlist.txt").read_text().splitlines() if line.strip() and not line.lstrip().startswith("#"))
assert len(SYMBOLS) == 7 and len(set(SYMBOLS)) == 7
CAL = xcals.get_calendar("XNYS", start="2025-01-01", end="2026-10-01")
SESSIONS = CAL.sessions[CAL.sessions <= pd.Timestamp("2026-09-11")][-120:]
PREVIOUS = CAL.previous_session(SESSIONS[0])
START = pd.Timestamp(PREVIOUS.date(), tz="America/Los_Angeles").tz_convert("UTC")
END = pd.Timestamp("2026-09-11 17:00", tz="America/Los_Angeles").tz_convert("UTC")
MAX_ONE = 128 * 1024**2
MAX_TOTAL = 1024**3

def now(): return pd.Timestamp.now(tz="UTC").isoformat()
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def save(path, value): path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")
def progress(message): print(message, flush=True)

report = {"started_at": now(), "status": "PREFLIGHT", "source": "XNAS.BASIC", "baseline": "XNAS.ITCH", "symbols": SYMBOLS, "sessions": [s.date().isoformat() for s in SESSIONS], "prior_session": PREVIOUS.date().isoformat(), "request_start": START.isoformat(), "request_end": END.isoformat(), "production_writes": False, "orders": 0, "model_training": False, "preflights": [], "acquisitions": []}
save(OUT / "run.json", report)

try:
    load_repository_environment()
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key: raise ValueError("Existing Databento credential unavailable")
    client = db.Historical(key)
    schemas = client.metadata.list_schemas(dataset="XNAS.BASIC")
    ranges = client.metadata.get_dataset_range(dataset="XNAS.BASIC")
    save(OUT / "dataset-metadata.json", {"observed_at": now(), "dataset": "XNAS.BASIC", "schemas": schemas, "range": ranges})
    available = ranges.get("schema", {}).get("ohlcv-1m", ranges)
    if "ohlcv-1m" not in schemas or pd.Timestamp(available["start"]) > START or pd.Timestamp(available["end"]) < END:
        raise ValueError("Candidate schema or exact 120-session range unavailable")
    for symbol in SYMBOLS:
        directory = OUT / symbol
        directory.mkdir(exist_ok=True)
        request = {"dataset": "XNAS.BASIC", "schema": "ohlcv-1m", "symbols": [symbol], "stype_in": "raw_symbol", "start": START.isoformat(), "end": END.isoformat()}
        cost = float(client.metadata.get_cost(**request))
        size = int(client.metadata.get_billable_size(**request))
        count = int(client.metadata.get_record_count(**request))
        preflight = {"observed_at": now(), "request": request, "estimated_cost_usd": cost, "estimated_billable_bytes": size, "estimated_record_count": count, "range_pass": True, "max_bytes": MAX_ONE, "capacity_pass": bool(0 <= size <= MAX_ONE)}
        save(directory / "preflight.json", preflight)
        report["preflights"].append(preflight)
        save(OUT / "run.json", report)
        progress(json.dumps({"stage": "PREFLIGHT", "symbol": symbol, "cost": cost, "bytes": size, "records": count}))
        if not math.isfinite(cost) or cost != 0 or not preflight["capacity_pass"]:
            raise ValueError(f"{symbol} exact zero-dollar or per-symbol capacity preflight failed")
    total = sum(p["estimated_billable_bytes"] for p in report["preflights"])
    free = shutil.disk_usage(OUT).free
    if total > MAX_TOTAL or free <= 5*1024**3 + total*4:
        raise ValueError("Total 1GB or free-space capacity preflight failed")
    report["total_preflight"] = {"estimated_cost_usd": sum(p["estimated_cost_usd"] for p in report["preflights"]), "estimated_billable_bytes": total, "max_bytes": MAX_TOTAL, "available_bytes": free, "capacity_pass": True}
    report["status"] = "ACQUIRING"
    save(OUT / "run.json", report)
    progress(json.dumps({"stage": "ALL_PREFLIGHTS_PASSED", **report["total_preflight"]}))
    candidate_frames = []
    for preflight in report["preflights"]:
        request = preflight["request"]
        symbol = request["symbols"][0]
        directory = OUT / symbol
        raw = directory / "provider.dbn"
        if raw.exists(): raise ValueError(f"{symbol} diagnostic output already exists; refusing overwrite")
        store = client.timeseries.get_range(**request, path=raw)
        try:
            meta = store.metadata
            metadata = {k: getattr(meta, k, None) for k in ("dataset", "schema", "start", "end", "symbols", "stype_in", "stype_out", "partial", "not_found", "mappings", "version")}
            save(directory / "native-metadata.json", metadata)
            if str(meta.dataset) != "XNAS.BASIC" or str(meta.schema) != "ohlcv-1m" or int(meta.start) != START.value or int(meta.end) != END.value or meta.partial or meta.not_found or tuple(meta.symbols) != (symbol,):
                raise ValueError(f"{symbol} native source/range/symbol metadata verification failed")
            frame = store.to_df(schema="ohlcv-1m", map_symbols=True, price_type="float").reset_index()
        finally: store.reader.close()
        stamps = pd.to_datetime(frame.ts_event, utc=True)
        vals = frame[["open", "high", "low", "close", "volume"]].to_numpy(float)
        if (len(frame) != preflight["estimated_record_count"] or set(frame.symbol.astype(str)) != {symbol} or set(map(int, frame.publisher_id)) != {93}
                or not (stamps.ge(START) & stamps.lt(END) & stamps.eq(stamps.dt.floor("min"))).all()
                or not np.isfinite(vals).all() or (vals[:, :4] <= 0).any() or (vals[:, 4] < 0).any()
                or frame.duplicated(["symbol", "ts_event"]).any()
                or frame.high.lt(frame[["open", "low", "close"]].max(axis=1)).any()
                or frame.low.gt(frame[["open", "high", "close"]].min(axis=1)).any()):
            raise ValueError(f"{symbol} native row validity or count verification failed")
        frame = frame.sort_values("ts_event").reset_index(drop=True)
        normalized = directory / "observations.parquet"
        frame.to_parquet(normalized, index=False, compression="zstd")
        receipt = {"completed_at": now(), "dataset": "XNAS.BASIC", "symbol": symbol, "rows": len(frame), "zero_volume_rows": int(frame.volume.eq(0).sum()), "request": request,
            "payloads": [{"path": name, "bytes": (directory/name).stat().st_size, "sha256": digest(directory/name)} for name in ("provider.dbn", "observations.parquet", "preflight.json", "native-metadata.json")]}
        save(directory / "manifest.json", receipt)
        # Read back the saved native delivery independently before comparison.
        reread = db.DBNStore.from_file(raw)
        try: reproduced = reread.to_df(schema="ohlcv-1m", map_symbols=True, price_type="float").reset_index().sort_values("ts_event").reset_index(drop=True)
        finally: reread.reader.close()
        pd.testing.assert_frame_equal(reproduced, pd.read_parquet(normalized))
        report["acquisitions"].append({"symbol": symbol, "rows": len(frame), "zero_volume_rows": receipt["zero_volume_rows"], "manifest_sha256": digest(directory / "manifest.json"), "raw_readback_verified": True})
        save(OUT / "run.json", report)
        candidate_frames.append(frame.rename(columns={"ts_event": "timestamp"}))
        progress(json.dumps({"stage": "ACQUIRED_VERIFIED", **report["acquisitions"][-1]}))
    report["status"] = "COMPARING"
    save(OUT / "run.json", report)
    baseline, _, inventory = load_stock_target_prices(Path("C:/DATASTORE"), symbols=SYMBOLS, source_contract=XNAS_STOCK_PRICE_SOURCE)
    baseline = baseline[baseline.timestamp.ge(START) & baseline.timestamp.lt(END)].copy()
    save(OUT / "baseline-inventory.json", inventory)
    candidate = pd.concat(candidate_frames, ignore_index=True)
    window_rows = []
    for session in SESSIONS:
        for symbol in SYMBOLS:
            for window in stock_target_windows(session.date(), calendar=CAL):
                if window["execution_eligible"]:
                    window_rows.append({"symbol": symbol, "action_date": session.date().isoformat(), **window})
    windows = pd.DataFrame(window_rows)
    assert len(windows) == 120*7*19
    boundary_rows, availability_rows = [], []
    for dataset, data in (("XNAS.ITCH", baseline), ("XNAS.BASIC", candidate)):
        by_symbol = {symbol: frame.sort_values("timestamp").reset_index(drop=True) for symbol, frame in data.groupby("symbol")}
        observed = _target_observations(windows, by_symbol=by_symbol)
        aligned = (np.isfinite(observed.entry_price) & np.isfinite(observed.exit_price) & observed.entry_price.gt(0) & observed.exit_price.gt(0)
            & (observed.observed_open_timestamp-windows.target_window_start).abs().le(STOCK_TARGET_BOUNDARY_TOLERANCE)
            & (observed.observed_close_timestamp-windows.target_window_end).abs().le(STOCK_TARGET_BOUNDARY_TOLERANCE)
            & observed.observed_open_timestamp.lt(observed.observed_close_timestamp))
        for i, window in windows.iterrows():
            mature = window.target_window_end <= END
            availability_rows.append({"dataset": dataset, "symbol": window.symbol, "action_date": window.action_date, "route": window.route, "model_group": window.model_group,
                "target_start": window.target_window_start, "target_end": window.target_window_end, "mature": mature, "available": bool(mature and aligned.loc[i]),
                "observed_open": observed.loc[i,"observed_open_timestamp"], "observed_close": observed.loc[i,"observed_close_timestamp"]})
        for symbol, bars in by_symbol.items():
            times = pd.DatetimeIndex(bars.timestamp)
            for session in SESSIONS:
                local = pd.Timestamp(session.date(), tz="America/Los_Angeles")
                regular_open, regular_close = CAL.session_open(session), CAL.session_close(session)
                for close in (False, True):
                    for hour in (range(5,18) if close else range(4,17)):
                        clock = (local + pd.Timedelta(hours=hour)).tz_convert("UTC")
                        pos = times.searchsorted(clock-pd.Timedelta(minutes=1), side="right")-1 if close else times.searchsorted(clock, side="left")
                        actual = times[pos] + (pd.Timedelta(minutes=1) if close else pd.Timedelta(0)) if 0 <= pos < len(times) else pd.NaT
                        distance = ((clock-actual) if close else (actual-clock)).total_seconds()/60 if pd.notna(actual) else None
                        regular = regular_open < clock <= regular_close if close else regular_open <= clock < regular_close
                        period = "regular" if regular else "premarket" if clock < regular_open else "afterhours"
                        boundary_rows.append({"dataset": dataset, "symbol": symbol, "session": session.date().isoformat(), "clock_pacific": f"{hour:02}:00", "kind": "close" if close else "open", "period": period, "observed_at": actual, "distance_minutes": distance, "available": distance is not None and 0 <= distance <= 5})
    boundary = pd.DataFrame(boundary_rows)
    availability = pd.DataFrame(availability_rows)
    boundary.to_parquet(OUT / "boundary-results.parquet", index=False)
    availability.to_parquet(OUT / "entry-window-results.parquet", index=False)
    rates = boundary.groupby(["dataset", "symbol", "period"]).available.agg(["size", "sum"]).reset_index().rename(columns={"size": "boundaries", "sum": "available"})
    rates["missing"] = rates.boundaries-rates.available
    rates["missing_rate"] = rates.missing/rates.boundaries
    routes = availability[availability.mature].groupby(["dataset", "symbol", "route"]).available.agg(["size", "sum"]).reset_index().rename(columns={"size": "mature_windows", "sum": "available"})
    routes["missing"] = routes.mature_windows-routes.available
    routes["missing_rate"] = routes.missing/routes.mature_windows
    summary = {"completed_at": now(), "status": "COMPLETE", "sessions": len(SESSIONS), "first_session": SESSIONS[0].date().isoformat(), "last_session": SESSIONS[-1].date().isoformat(), "prior_session": PREVIOUS.date().isoformat(),
        "boundary_policy": "Native directional open-at-or-after and completed-close-at-or-before, maximum five minutes; no fill", "maturity_cutoff": END.isoformat(),
        "future_windows_excluded_per_symbol": int((~availability[availability.dataset.eq('XNAS.BASIC') & availability.symbol.eq('COST')].mature).sum()),
        "caveat": "Coverage-only qualification; no feature/sample-partition/model assessment performed; native zero-volume bars preserved and counted separately", "boundary_rates": rates.to_dict("records"), "entry_routes": routes.to_dict("records"), "native_zero_volume_counts": report["acquisitions"]}
    save(OUT / "comparison.json", summary)
    report["status"] = "COMPLETE"
    report["comparison_sha256"] = digest(OUT / "comparison.json")
    report["output_manifests"] = [{"path": name,"sha256":digest(OUT/name)} for name in ("boundary-results.parquet", "entry-window-results.parquet", "baseline-inventory.json")]
    progress(json.dumps({"stage": "COMPLETE", "first_session": summary["first_session"], "last_session": summary["last_session"], "boundary_rates": rates.to_dict("records")}))
except Exception as exc:
    report["status"] = "BLOCKED"
    report["error"] = _external_failure(exc)
    progress(json.dumps({"stage": "BLOCKED", "error": report["error"]}))
report["finished_at"] = now()
save(OUT / "run.json", report)
