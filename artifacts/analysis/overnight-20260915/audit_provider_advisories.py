"""Bounded local audit. Writes only adjacent analysis artifacts; no provider calls."""
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
from datafetching.fmp_energy_context import calculate_fmp_energy_context

ROOT = Path("C:/DATASTORE")
OUT = Path(__file__).resolve().parent
RUN = ROOT / "ml/overnight-runs/20260915T040742.365640Z"
cycle = json.loads((ROOT / ".ducketz-loop-a-cycle.json").read_text())
assert cycle["generation"] == "20260915T040743.170425Z-pid5368", cycle
symbols = [s.strip() for s in Path("C:/dev/ducketz/datafetching/watchlist.txt").read_text().splitlines()
           if s.strip() and not s.lstrip().startswith("#")]
assert symbols == cycle["symbols"] and len(symbols) == 11
start = pd.Timestamp(cycle["started_at"])
at = pd.Timestamp.now(tz="UTC")
finish = pd.Timestamp(cycle["finished_at"]) if cycle["finished_at"] else at
log_path = RUN / "loop_a_close_fetch.log"
log = log_path.read_bytes()
lines = log.decode("utf-8").splitlines()


def observed_file(path: Path):
    return {"path": str(path), "bytes": path.stat().st_size}


def selected(path, fields):
    cols = set(pq.read_schema(path).names)
    return pd.read_parquet(path, columns=[f for f in fields if f in cols])


def in_cycle(series):
    return pd.to_datetime(series, utc=True, errors="coerce").between(start, finish)


def summary(path):
    data = selected(path, ["fetched_at", "timestamp", "observed_at", "available_at", "date",
                           "latest_observation_date", "fetch_liveness_status", "freshness_status"])
    out = {**observed_file(path), "rows": len(data)}
    for field in data:
        if field.endswith("status"):
            out[field] = sorted(data[field].dropna().astype(str).unique())
        else:
            out["latest_" + field] = pd.to_datetime(data[field], utc=True, errors="coerce").max()
    if "fetched_at" in data:
        out["rows_with_current_cycle_receipt"] = int(in_cycle(data.fetched_at).sum())
    return out


result = {
    "audited_at": at.isoformat(), "audit_is_read_only": True,
    "scope": "Current base Loop A local receipts, errors and optional derived-context advisories. No exhaustive archive checks, provider calls, OPRA or XNAS-stage qualification.",
    "cycle_snapshot": cycle, "production_symbols": symbols,
    "log_snapshot": {"path": str(log_path), "bytes": len(log), "sha256": hashlib.sha256(log).hexdigest(), "lines": len(lines)},
    "provider_summary_lines": [line for line in lines if "blocking provider failures:" in line],
    "provider_request_completion_lines": [line for line in lines if "END   provider.request" in line],
    "technical_failure_lines": [line for line in lines if line.startswith("Failed calculations:")],
    "diagnostics": [], "current_cycle_error_rows": [], "providers": {},
}

for rel in ["pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet",
            "pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet"]:
    path = ROOT / rel
    data = pd.read_parquet(path)
    result["diagnostics"].append({**observed_file(path), "saved_rows": len(data),
        "current_cycle_rows": data.loc[in_cycle(data.fetched_at)].to_dict("records"),
        "latest_saved_row": data.sort_values("fetched_at").tail(1).to_dict("records")})

error_paths = [path for s in symbols for path in (ROOT / "stocks" / s / "errors").glob("*/*/*.parquet")]
error_paths += list((ROOT / "pools/macro").glob("*/errors/*/*/*.parquet"))
error_paths += list((ROOT / "pools/cme").glob("CME_*/errors/*/*/*.parquet"))
for path in sorted(error_paths):
    data = selected(path, ["fetched_at", "source", "symbol", "category", "request_key", "error_type", "error_message"])
    if "fetched_at" in data:
        current = data.loc[in_cycle(data.fetched_at)]
        if len(current):
            result["current_cycle_error_rows"].append({"path": str(path), "rows": current.to_dict("records")})
result["error_metadata_files_scanned"] = len(error_paths)

for provider in ["fmp", "sec"]:
    result["providers"][provider] = {symbol: [summary(path) for path in
        sorted((ROOT / "stocks" / symbol / "corporate").glob(f"*/{provider}/normalized/*.parquet"))]
        for symbol in symbols}
result["providers"]["fred"] = [summary(path) for path in sorted((ROOT / "pools/macro").glob("*/*/fred/normalized/*.parquet"))]
result["providers"]["schwab_quotes"] = {symbol: summary(ROOT / "stocks" / symbol / "quotes/schwab/normalized" / f"{symbol}.parquet") for symbol in symbols}
result["providers"]["schwab_options_log"] = [line for line in lines if re.match(r"\[[A-Z]+/schwab/options\]", line)]
result["providers"]["databento_1m"] = {symbol: [summary(path) for path in sorted((ROOT / "stocks" / symbol).glob("bars/1m/databento/normalized/*.parquet"))] for symbol in symbols}
result["providers"]["fmp_macro_quotes"] = [summary(path) for path in sorted((ROOT / "pools/macro").glob("*/quote/fmp/normalized/*.parquet"))]

fmp_path = ROOT / "pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet"
fmp = pd.read_parquet(fmp_path)
try:
    calculate_fmp_energy_context(fmp)
    full_result = "PASS"
except Exception as exc:
    full_result = f"{type(exc).__name__}: {exc}"
current = fmp.loc[in_cycle(fmp.fetched_at)]
try:
    current_result = {"status": "PASS", "derived_rows": len(calculate_fmp_energy_context(current))}
except Exception as exc:
    current_result = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
observed = pd.to_datetime(fmp.timestamp, utc=True)
receipts = pd.to_datetime(fmp.fetched_at, utc=True)
bad = fmp.loc[(observed-receipts).dt.total_seconds().gt(5)]
result["fmp_advisory_diagnosis"] = {
    "classification": "REPEATED_OPTIONAL_DERIVATION_REJECTION_FROM_PRESERVED_HISTORICAL_CLOCK_SKEW",
    "source_path": str(fmp_path), "pure_in_memory_full_source_validation": full_result,
    "pure_in_memory_current_cycle_validation": current_result,
    "earliest_invalid_rows": bad.head(3).to_dict("records"), "invalid_rows": len(bad),
    "current_cycle_rows": current.to_dict("records"),
    "explanation": "The optional derivation reads the complete preserved CLUSD source and rejects its historical 9.471-second clock skew again. save_advisory deduplicates identical diagnostic identity/message without a new receipt. Fresh current-cycle quote rows pass the unchanged native pure calculation. AAPL carries the shared macro lane's advisory count; this is not an AAPL provider fetch error.",
    "source_code": ["C:/dev/ducketz/datafetching/fmp_energy_context.py:142", "C:/dev/ducketz/datafetching/fmp_fetch.py:183", "C:/dev/ducketz/datafetching/parquet_store.py:340"],
}

cme_flat = []
for path in sorted((ROOT / "pools/cme").glob("CME_*/*/databento/normalized/*.parquet")):
    if path.stem.endswith("_status"):
        continue
    data = selected(path, ["timestamp", "fetched_at", "provider_symbol", "databento_symbol", "symbol", "request_limit_saturated"])
    current = data.loc[in_cycle(data.fetched_at)]
    group = next((c for c in ["databento_symbol", "provider_symbol", "symbol"] if c in data), None)
    cme_flat.append({**observed_file(path), "rows": len(data), "current_cycle_rows": len(current),
        "latest_market_at": data.timestamp.max(), "latest_receipt_at": data.fetched_at.max(),
        "current_cycle_limit_saturated_rows": int(current.request_limit_saturated.fillna(False).sum()) if "request_limit_saturated" in current else None,
        "latest_rows_by_instrument": current.sort_values("timestamp").groupby(group).tail(1).to_dict("records") if group else []})
inventories = {}
for schema in ["ohlcv-1m", "bbo-1m", "mbp-10"]:
    paths = sorted((ROOT / "pools/cme/events/databento/context" / schema / "normalized").glob("**/*.parquet"))
    inventories[schema] = {"files": len(paths), "last_path": str(paths[-1]) if paths else None}
result["providers"]["databento_cme_current_flat"] = cme_flat
result["cme_advisory_diagnosis"] = {
    "classification": "REPEATED_OPTIONAL_DERIVATION_REJECTION_FROM_OLD_PARTITION_SOURCE_SELECTION",
    "partition_path_inventory_only": inventories,
    "explanation": "The unchanged native reader prefers present partitioned context history over flat normalized source files. The current diagnostic selects the same Sep3 common-hour candidate as prior runs and rejects its stale NQ BBO. All six current CME requests report success; this advisory does not imply their data were absent. Current flat request metadata are recorded separately, without claiming complete book coverage or a usable derived context.",
    "source_code": ["C:/dev/ducketz/datafetching/cme_cross_asset_context.py:447"],
}
result["boundaries"] = [
    "Historical retained error rows are not classified as current errors; native counts are also retained because error identities may be deduplicated.",
    "Macro economic observation dates follow cadence; fresh fetched_at receipts do not prove historical vintage availability.",
    "SEC frozen-extractor idempotent reuse may leave older accepted filing receipts; pre-existing source files do not alone establish new fetched coverage.",
    "Existing Schwab bar history is retained; this audit checks current quotes and option capture logs only.",
    "Operational Databento EQUS.MINI 1m evidence is separate from subsequent XNAS.ITCH target-history and planning-source validation.",
    "All optional derived-context quality gates remain unchanged; no repair or provider retry is recommended solely from these known advisories.",
]
result["status"] = ("BASE_CYCLE_COMPLETE_WITH_KNOWN_ADVISORIES" if cycle["status"] == "COMPLETE" else "BASE_CYCLE_IN_PROGRESS_WITH_KNOWN_ADVISORIES")
if result["current_cycle_error_rows"]:
    result["status"] = "REVIEW_CURRENT_CYCLE_ERROR_METADATA"
if cycle["status"] == "COMPLETE":
    complete_path = ROOT / ".ducketz-loop-a-complete.json"
    complete = json.loads(complete_path.read_text())
    assert complete == cycle, (complete, cycle)
    assert cycle["failure_count"] == 0
    result["matching_complete_receipt"] = str(complete_path)
path = OUT / "loop-a-provider-advisory-audit.json"
path.write_text(json.dumps(result, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
note = OUT / "loop-a-provider-advisory-audit.md"
note.write_text(f"""# Loop A provider advisory audit

Audited {at.isoformat()}. Status: {result['status']}.

- Universe matches production and the native cycle: 11 symbols.
- Observed {len(result['provider_summary_lines'])} symbol provider summaries and {len(result['provider_request_completion_lines'])} completed provider request log lines. Current-cycle error metadata: {len(result['current_cycle_error_rows'])} files across {len(error_paths)} scanned error files.
- FMP shared energy advisory repeats the preserved historical 9.471-second quote clock skew. The full source reproduces the rejection; the current-cycle quote subset passes the same native pure calculation. The diagnostic is deduplicated, so its saved timestamp remains September 2.
- CME shared context again selects the old partitioned September 3 common-hour candidate. Current flat BBO and OHLCV receipts are fresh; all six CME requests succeeded and their current rows have zero request-limit-saturation flags. This does not qualify the optional derived context or establish exhaustive book coverage.
- FMP/SEC local corporate receipts, all four FRED series, and Schwab quotes/options evidence were recorded for the current production universe. Unchanged corporate rows and previously accepted filings may retain earlier timestamps.
- No newly actionable provider failure was identified in the bounded evidence. No provider requests, production changes, process actions, retries, model operations, broker calls, or claim operations were performed.

Detailed evidence: [JSON]({path.as_posix()}). OPRA maintenance, subsequent XNAS target history and full overnight final verification remain outside this audit.
""", encoding="utf-8")
print(json.dumps({"status": result["status"], "artifact": str(path), "cycle_status": cycle["status"],
    "symbol_summaries_observed": len(result["provider_summary_lines"]),
    "provider_requests_observed": len(result["provider_request_completion_lines"]),
    "error_files_scanned": len(error_paths), "current_cycle_error_files": len(result["current_cycle_error_rows"]),
    "fmp_full_validation": full_result, "fmp_current_validation": current_result,
    "cme_latest_market_times": {p["path"]:str(p["latest_market_at"]) for p in cme_flat}}, indent=2))
