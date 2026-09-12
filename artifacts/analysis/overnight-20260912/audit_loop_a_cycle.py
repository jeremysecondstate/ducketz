"""Bounded local-only audit; writes evidence solely beside this script."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")
import pandas as pd
import pyarrow.parquet as pq
from datafetching.loop_a_cycle import require_complete_loop_a_cycle, read_latest_complete_loop_a_cycle
from datafetching.cme_history import cme_normalized_event_paths
from datafetching.cme_cross_asset_context import (
    _read_persisted_source, _prepare_events, _prepare_ohlcv, _complete_common_ohlcv_windows,
)

ROOT = Path("C:/DATASTORE")
OUT = Path(__file__).resolve().parent
GENERATION = "20260912T040658.065355Z-pid68344"
LOG = ROOT / "ml/overnight-runs/20260912T040657.173506Z/loop_a_close_fetch.log"
cycle = require_complete_loop_a_cycle(ROOT)
assert cycle.generation == GENERATION and cycle.failure_count == 0
assert read_latest_complete_loop_a_cycle(ROOT) == cycle
start, finish = pd.Timestamp(cycle.started_at), pd.Timestamp(cycle.finished_at)


def evidence(path):
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def selected(path, fields):
    columns = set(pq.read_schema(path).names)
    return pd.read_parquet(path, columns=[key for key in fields if key in columns])


def in_cycle(series):
    return pd.to_datetime(series, utc=True, errors="coerce").between(start, finish)


def table_summary(path):
    fields = ["fetched_at", "timestamp", "observed_at", "available_at", "date", "acceptedDate",
              "filingDate", "latest_observation_date", "fetch_liveness_status", "fetch_liveness_age_days",
              "fetch_liveness_limit_days", "freshness_status", "cadence", "quote_time", "trade_time"]
    data = selected(path, fields)
    result = {**evidence(path), "rows": len(data)}
    for field in data:
        if field in {"fetch_liveness_status", "freshness_status", "cadence"}:
            result[field] = sorted(data[field].dropna().astype(str).unique())
        elif field.startswith("fetch_liveness_"):
            result[field] = data[field].iloc[-1] if len(data) else None
        else:
            values = pd.to_datetime(data[field], utc=True, errors="coerce")
            result["latest_" + field] = values.max()
    if "fetched_at" in data:
        result["rows_with_receipt_in_cycle"] = int(in_cycle(data.fetched_at).sum())
    return result


log_bytes = LOG.read_bytes()
marker = ("Loop A datastore cycle " + GENERATION + ": COMPLETE").encode()
marker_end = log_bytes.index(marker) + len(marker)
prefix = log_bytes[:marker_end]
lines = prefix.decode("utf-8").splitlines()
result = {
    "audited_at": pd.Timestamp.now(tz="UTC").isoformat(), "audit_is_read_only": True,
    "scope": "Completed base Loop A datastore cycle only; later OPRA maintenance and XNAS stage not assessed",
    "cycle": cycle.as_dict(),
    "native_receipts": [evidence(ROOT / name) for name in (".ducketz-loop-a-cycle.json", ".ducketz-loop-a-complete.json")],
    "cycle_log_prefix": {"path": str(LOG), "bytes_through_cycle_completion": len(prefix),
                         "sha256": hashlib.sha256(prefix).hexdigest(), "completion_line": len(lines)},
    "providers": {}, "diagnostics": [], "current_cycle_error_rows": [],
}

for provider in ("fmp", "sec"):
    symbols = {}
    for symbol in cycle.symbols:
        paths = sorted((ROOT / "stocks" / symbol / "corporate").glob(f"*/{provider}/normalized/*.parquet"))
        symbols[symbol] = [table_summary(path) for path in paths]
    result["providers"][provider] = {"by_symbol": symbols}

macro_paths = sorted((ROOT / "pools/macro").glob("*/*/fred/normalized/*.parquet"))
result["providers"]["fred"] = {"shared_series": [table_summary(path) for path in macro_paths],
    "interpretation": "Current-revised macro rows were freshly fetched. Series dates follow monthly/quarterly cadence; current receipts do not themselves prove historical vintage availability."}
result["providers"]["fmp"]["macro_quotes"] = [table_summary(path) for path in sorted((ROOT / "pools/macro").glob("*/quote/fmp/normalized/*.parquet"))]
result["providers"]["fmp"]["derived_energy_context"] = table_summary(ROOT / "pools/macro/features/energy-context/fmp/quote.parquet")

schwab = {}
for symbol in cycle.symbols:
    stock = ROOT / "stocks" / symbol
    quote = stock / f"quotes/schwab/normalized/{symbol}.parquet"
    snapshots = [line for line in lines if line.startswith(f"[{symbol}/schwab/options]")]
    bars = [table_summary(path) for path in sorted(stock.glob("bars/*/schwab/normalized/*.parquet"))]
    schwab[symbol] = {"quote": table_summary(quote), "bar_scopes": bars, "options_publication_log": snapshots}
result["providers"]["schwab"] = schwab

databento = {"provider_request_completions": [line for line in lines if "END   provider.request" in line],
             "equity_1m": {}, "cme_current_flat": {}}
for symbol in cycle.symbols:
    databento["equity_1m"][symbol] = [table_summary(path) for path in sorted((ROOT / "stocks" / symbol).glob("bars/1m/databento/normalized/*.parquet"))]
for path in sorted((ROOT / "pools/cme").glob("CME_*/*/databento/normalized/*.parquet")):
    if "_status" in path.name:
        continue
    data = selected(path, ["timestamp", "fetched_at", "symbol", "provider_symbol", "databento_symbol", "request_limit_saturated", "bid_px_00", "ask_px_00"])
    symbol_col = "databento_symbol" if "databento_symbol" in data else "symbol"
    newest = data.sort_values("timestamp").groupby(symbol_col).tail(1)
    databento["cme_current_flat"][str(path)] = {
        **evidence(path), "rows": len(data), "latest_market_at": data.timestamp.max(),
        "latest_receipt_at": data.fetched_at.max(), "by_instrument_latest_rows": newest.to_dict("records"),
        "cycle_rows": int(in_cycle(data.fetched_at).sum()),
        "cycle_limit_saturated_rows": int(data.loc[in_cycle(data.fetched_at), "request_limit_saturated"].fillna(False).sum()) if "request_limit_saturated" in data else None,
    }
result["providers"]["databento"] = databento

diagnostic_paths = [
    ROOT / "pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet",
    ROOT / "pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet",
]
for path in diagnostic_paths:
    data = pd.read_parquet(path)
    current = data.loc[in_cycle(data.fetched_at)]
    result["diagnostics"].append({**evidence(path), "cycle_rows": current.to_dict("records"),
        "latest_saved_row": data.sort_values("fetched_at").tail(1).to_dict("records"),
        "has_current_cycle_timestamp": bool(len(current))})

# Only error metadata are read; do not classify old retained errors as new.
error_paths = []
for symbol in cycle.symbols:
    error_paths.extend((ROOT / "stocks" / symbol / "errors").glob("*/*/*.parquet"))
error_paths.extend((ROOT / "pools/macro").glob("*/errors/*/*/*.parquet"))
error_paths.extend((ROOT / "pools/cme").glob("CME_*/errors/*/*/*.parquet"))
for path in sorted(error_paths):
    data = selected(path, ["fetched_at", "source", "symbol", "category", "request_key", "error_type"])
    if "fetched_at" in data:
        current = data.loc[in_cycle(data.fetched_at)]
        if len(current):
            result["current_cycle_error_rows"].append({"path": str(path), "rows": current.to_dict("records")})
result["error_metadata_files_scanned"] = len(error_paths)

# Native reader source selection is read-only. Avoid reading the 14GB MBP
# history: its path/date inventory is sufficient to establish precedence.
bbo = _prepare_events(_read_persisted_source(ROOT, "cme_context_bbo-1m").combined, label="BBO")
ohlcv = _prepare_ohlcv(_read_persisted_source(ROOT, "cme_context_ohlcv-1m").combined)
windows = _complete_common_ohlcv_windows(ohlcv)
window_start = max(windows)
window_end = window_start + pd.Timedelta(hours=1)
nq = bbo.loc[bbo._root.eq("NQ") & bbo.timestamp.ge(window_start) & bbo.timestamp.lt(window_end)].sort_values("timestamp").tail(1)
inventories = {}
for schema in ("ohlcv-1m", "bbo-1m", "mbp-10"):
    paths = cme_normalized_event_paths(ROOT, group_key="context", schema=schema)
    inventories[schema] = {"files": len(paths), "bytes": sum(path.stat().st_size for path in paths),
                           "last_path": str(paths[-1])}
result["cme_derived_context_diagnosis"] = {
    "selected_partitioned_sources": inventories,
    "selected_bbo_latest_market_by_root": bbo.groupby("_root").timestamp.max().to_dict(),
    "latest_complete_common_ohlcv_window_start": window_start,
    "latest_complete_common_ohlcv_window_end": window_end,
    "exact_nq_row_in_advisory_window": nq[[key for key in ("timestamp", "ts_event", "ts_recv", "fetched_at", "provider_symbol", "bid_px_00", "ask_px_00") if key in nq]].to_dict("records"),
    "exact_nq_partition_path": str(ROOT / "pools/cme/events/databento/context/bbo-1m/normalized/date=2026-09-03/hour=20/events.parquet"),
    "classification": "LOCAL_SOURCE_SELECTION_MISMATCH_PLUS_CALCULATION_TIME_FRESHNESS_GATE",
    "explanation": "The native derived-context reader prefers existing partitioned event history, ending Sep3/Sep4, over the freshly updated flat normalized outputs. The eight-day NQ age describes its selected Sep3 common-hour candidate, not current provider-download availability. Even Sep11 latest market observations are more than seven hours old at overnight calculation and do not satisfy the unchanged 15-minute calculation-time gate. Both current MBP requests are explicitly limit-saturated at 5000 rows and cannot prove complete book coverage. No repair or qualification is asserted.",
    "source_code": {"path": "C:/dev/ducketz/datafetching/cme_cross_asset_context.py", "source_selection_line": 447, "staleness_gate_line": 877},
}
result["expected_skips_and_boundaries"] = {
    "option_market_cycle": next(line for line in lines if line.startswith("Loop A target decision:")),
    "technical_failures": [line for line in lines if line.startswith("Failed calculations:")],
    "technical_not_ready": [line for line in lines if line.startswith("Not-ready calculations skipped:")],
    "sec_existing_acceptances": "SEC fetch skips filings already processed by the current frozen extractor; unchanged texts/events are expected idempotent reuse, not a failed request.",
    "schwab_price_history": "The scheduled orchestrator's run_cycle default include_schwab_price_history=False intentionally skips new Schwab bar requests; existing bar files are retained inputs, not refreshed completed-session coverage. Fresh Schwab quote/options capture remains enabled.",
    "fmp_sec_discovery": "FMP corporate fetch intentionally skips sec_filings_search_symbol because the separate SEC lane owns filing discovery.",
    "old_cme_status_rows": "Retained NO CURRENT ROWS diagnostics dated Sep6 are historical and do not describe tonight's successful six CME requests.",
    "opra_and_xnas": "NOT_ASSESSED_PENDING_NATIVE_STAGES",
}
result["status"] = "BASE_CYCLE_COMPLETE_WITH_LOCAL_ADVISORIES" if not result["current_cycle_error_rows"] else "BASE_CYCLE_COMPLETE_REVIEW_ERROR_METADATA"

json_path = OUT / "loop-a-provider-audit.json"
json_path.write_text(json.dumps(result, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"status": result["status"], "output": str(json_path),
    "fmp_scopes_by_symbol": {symbol: len(rows) for symbol, rows in result["providers"]["fmp"]["by_symbol"].items()},
    "fred_series": len(macro_paths), "error_files_scanned": len(error_paths),
    "current_cycle_error_files": len(result["current_cycle_error_rows"]),
    "cme_latest_common_window": str(window_end)}, indent=2))
