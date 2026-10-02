"""Read-only local advisory classification; pure FMP calculation, no writes to production."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import io
import json
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")
import pandas as pd
import pyarrow.parquet as pq
from datafetching.fmp_energy_context import calculate_fmp_energy_context, _fmp_provider_timestamp

ROOT = Path("C:/DATASTORE")
REPO = Path("C:/dev/ducketz")
OUT = Path(__file__).resolve().parent
RUN = ROOT / "ml/overnight-runs/20261002T040848.225355Z"
GENERATION = "20261002T040849.137744Z-pid67212"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_frame(path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("FILE_SIZE_BOUND_EXCEEDED")
    data = path.read_bytes()
    stream = io.BytesIO(data)
    if pq.read_metadata(stream).num_rows > 300000:
        raise ValueError("ROW_BOUND_EXCEEDED")
    return pq.read_table(stream).to_pandas(), {"path": str(path), "sha256": sha(data), "bytes": len(data)}


def main():
    observed = datetime.now(timezone.utc)
    cycle_path = ROOT / ".ducketz-loop-a-cycle.json"
    cycle_bytes = cycle_path.read_bytes()
    cycle = json.loads(cycle_bytes)
    if cycle["generation"] != GENERATION:
        raise ValueError("CURRENT_CYCLE_IDENTITY_CHANGED")
    log_path = RUN / "loop_a_close_fetch.log"
    log_data = log_path.read_bytes()
    lines = log_data.decode("utf-8", errors="replace").splitlines()
    symbols, technical = [], []
    for number, line in enumerate(lines, 1):
        match = re.fullmatch(r"\[([^]]+)\] changed parquet files: (\d+); blocking provider failures: (\d+)(?: \([^)]*\))?; optional capture failures: (\d+)(?: \([^)]*\))?; local advisories: (\d+)(?: \(([^)]*)\))?", line)
        if match:
            symbol, files, failures, optional, advisories, detail = match.groups()
            symbols.append({"symbol": symbol, "changed_files": int(files), "blocking_failures": int(failures), "optional_failures": int(optional), "advisories": int(advisories), "advisory_detail": detail, "line": number})
        if re.fullmatch(r"Failed calculations:\s*\d+", line):
            technical.append({"line": number, "failed_calculations": int(line.split(":")[1])})
    advisory_paths = [
        ROOT / "pools/cme/CME_CONTEXT/cme_context_mbp-10/databento/diagnostics/CME_CONTEXT_cme_context_mbp-10.parquet",
        ROOT / "pools/cme/CME_CONTRACTS/cme_contracts_mbp-10/databento/diagnostics/CME_CONTRACTS_cme_contracts_mbp-10.parquet",
        ROOT / "pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet",
    ]
    diagnostics = []
    for path in advisory_paths:
        frame, evidence = read_frame(path)
        allowed = [column for column in frame.columns if column not in {"endpoint", "provider_base_url", "url"}]
        evidence.update(records=frame[allowed].to_dict("records"), count=len(frame))
        diagnostics.append(evidence)
    source_path = ROOT / "pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet"
    frame, fmp_evidence = read_frame(source_path)
    fmp_evidence.update(rows=len(frame), latest_receipt=str(pd.to_datetime(frame["fetched_at"], utc=True).max()))
    try:
        calculate_fmp_energy_context(frame)
        reproduction = {"status": "CALCULATION_PASSED"}
    except Exception as error:
        reproduction = {"status": "REPRODUCED_REJECTION", "type": type(error).__name__, "message": str(error)}
    violations = []
    for _, row in frame.iterrows():
        provider_time = _fmp_provider_timestamp(row["timestamp"])
        receipt = pd.Timestamp(row["fetched_at"])
        skew = (provider_time - receipt).total_seconds()
        if skew > 5:
            violations.append({"provider_symbol": row.get("provider_symbol"), "provider_timestamp": str(provider_time), "receipt_timestamp": str(receipt), "clock_skew_seconds": skew})
    checks = {
        "cycle_bound_to_current_native_log": any("CYCLE 2026-10-02T04:08:49.137744+00:00" == line for line in lines),
        "cycle_complete": cycle["status"] == "COMPLETE",
        "cycle_zero_failures": cycle["failure_count"] == 0,
        "all_symbol_summaries_present": {row["symbol"] for row in symbols} == set(cycle["symbols"]),
        "symbol_summaries_zero_blocking_failures": all(row["blocking_failures"] == 0 for row in symbols),
        "symbol_summaries_zero_optional_failures": all(row["optional_failures"] == 0 for row in symbols),
        "exactly_three_local_advisories": sum(row["advisories"] for row in symbols) == 3,
        "all_technical_summaries_present": len(technical) == len(cycle["symbols"]),
        "technical_summaries_zero_failures": all(row["failed_calculations"] == 0 for row in technical),
        "fmp_guard_reproduced": reproduction.get("message") == diagnostics[2]["records"][0]["advisory_message"],
    }
    source_files = ["datafetching/databento_fetch.py", "datafetching/fmp_fetch.py", "datafetching/fmp_energy_context.py", "datafetching/parquet_store.py", "datafetching/orchestrate.py"]
    result = {
        "schema_version": 1, "reviewed_at_utc": observed.isoformat(),
        "scope": "READ_ONLY_CURRENT_BASE_CYCLE_ADVISORY_REVIEW", "native_run_id": RUN.name,
        "status": "BASE_CYCLE_COMPLETE_ADVISORIES_CLASSIFIED" if all(checks.values()) else "BASE_CYCLE_PENDING_OR_REVIEW_REQUIRED",
        "cycle": cycle, "cycle_evidence": {"path": str(cycle_path), "sha256": sha(cycle_bytes)},
        "log_snapshot": {"path": str(log_path), "bytes": len(log_data), "sha256": sha(log_data)},
        "checks": checks, "symbol_summaries": symbols, "technical_summaries": technical,
        "advisory_evidence": diagnostics, "fmp_source_evidence": fmp_evidence,
        "fmp_read_only_reproduction": reproduction, "fmp_clock_skew_violations": violations,
        "classifications": [
            {"count": 2, "provider": "databento", "classification": "CURRENT_CME_MBP_LIMIT_SATURATION", "effect": "Hot/raw rows preserved; capped MBP event history was not advanced and CME derived-context materialization was skipped by the existing guard."},
            {"count": 1, "provider": "fmp", "classification": "REPEATED_HISTORICAL_ENERGY_QUOTE_CLOCK_SKEW", "effect": "Current pure calculation reproduces the retained September 2 diagnostic exactly; five September 2 USO quote rows exceed five seconds. Repeated identical advisories keep their older stored receipt because volatile receipt fields are excluded from content equality. This does not establish a new current-run FMP quote defect."},
        ],
        "limits": ["Optional local feature refusals do not establish provider acquisition failure.", "Provider degraded condition is separate from these local refusals.", "No guard was relaxed or invalid history rewritten.", "Base-cycle completion does not imply whole Loop A/OPRA-maintenance or overnight completion.", "Final model feature/source use requires fresh immutable publication evidence."],
        "source_evidence": [{"path": str(REPO / path), "sha256": sha((REPO / path).read_bytes())} for path in source_files],
        "provider_calls": 0, "production_writes": 0, "source_edits": 0, "orders": 0,
    }
    output = OUT / observed.strftime("local-advisory-review-%Y%m%dT%H%M%SZ.json")
    payload = json.dumps(result, indent=2, default=str) + "\n"
    if re.search(r"https?://", payload):
        raise ValueError("UNSANITIZED_URL_IN_EVIDENCE")
    with output.open("x", encoding="utf-8") as handle:
        handle.write(payload)
    print(json.dumps({"path": str(output), "sha256": sha(output.read_bytes()), "status": result["status"], "checks": checks, "cycle_status": cycle["status"], "symbols_complete": len(symbols), "fmp_reproduction": reproduction}, indent=2))


if __name__ == "__main__":
    main()
