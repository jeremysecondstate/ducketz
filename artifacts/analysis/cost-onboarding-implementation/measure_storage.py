"""Measure final logical file sizes after the approved COST archive completes."""

from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path


base = Path(__file__).resolve().parent
plan = json.loads((base / "plan.json").read_text(encoding="utf-8"))
progress = json.loads((base / "progress.json").read_text(encoding="utf-8"))
if progress["plan_id"] != plan["plan_id"] or progress["status"] != "HISTORY_FETCHED":
    raise RuntimeError("Wait for the complete approved historical fetch before measuring final storage")
if set(progress["completed_requests"]) != {r["request_id"] for r in plan["requests"]}:
    raise RuntimeError("The historical completion receipt does not cover every planned request")
root = Path(plan["datastore_root"])
symbol = plan["symbol"]
baseline = json.loads((base.parent / "cost-storage-reconciliation-2026-09-06.json").read_text())["inventory"]
started = datetime.now(timezone.utc).isoformat()
categories = defaultdict(int)
owned = defaultdict(int)
stock_details = defaultdict(int)
opra_schemas = defaultdict(int)
total = count = 0
for directory, _, filenames in os.walk(root):
    parts = Path(directory).relative_to(root).parts
    if not parts:
        category = "_root"
    elif parts[0] == "market-data" and len(parts) >= 3 and parts[1] == "databento":
        category = "archive_" + parts[2]
    elif parts[0] == "pools" and len(parts) >= 2:
        category = "pool_" + parts[1]
    else:
        category = parts[0]
    symbol_owned = bool({symbol, symbol + ".OPT"}.intersection(parts))
    for filename in filenames:
        try:
            size = (Path(directory) / filename).stat().st_size
        except FileNotFoundError:
            continue
        total += size
        count += 1
        categories[category] += size
        if symbol_owned:
            owned[category] += size
            if len(parts) >= 3 and parts[:2] == ("stocks", symbol):
                stock_details[parts[2]] += size
            if category == "archive_opra":
                # Canonical and retained batch staging use the same schema names.
                schema = next((p for p in parts if p in {
                    "definition", "ohlcv-1s", "ohlcv-1m", "ohlcv-1h", "ohlcv-1d",
                    "statistics", "status", "tcbbo", "cbbo-1s", "cbbo-1m", "trades",
                }), "metadata")
                opra_schemas[schema] += size

calculated = sum(stock_details[k] for k in ("fundamentals", "technicals", "signals"))
fetched_stocks = owned["stocks"] - calculated
fetched = owned["archive_opra"] + owned["archive_us-equities"] + fetched_stocks + owned["quarantine"]
growth = total - progress["before"]["datastore_bytes"]
ml_growth = categories["ml"] - baseline["categories_bytes"]["ml"]
activation_path = base / "activation.json"
activation = json.loads(activation_path.read_text()) if activation_path.exists() else {}
active = activation.get("plan_id") == plan["plan_id"] and activation.get("status") == "ACTIVE"
report = {
    "plan_id": plan["plan_id"], "symbol": symbol,
    "measurement_started_at": started, "measured_at": datetime.now(timezone.utc).isoformat(),
    "unit": "logical file bytes; 1 GiB = 1,073,741,824 bytes",
    "approved_archive_requests_completed": len(progress["completed_requests"]),
    "activation_complete": active,
    "datastore_before_bytes": progress["before"]["datastore_bytes"],
    "datastore_after_bytes": total, "datastore_growth_bytes": growth, "file_count": count,
    "cost_fetched_data_bytes": fetched,
    "cost_opra_archive_bytes": owned["archive_opra"],
    "cost_equity_archive_bytes": owned["archive_us-equities"],
    "cost_operational_fetched_stock_bytes": fetched_stocks,
    "cost_preserved_secondary_quarantine_bytes": owned["quarantine"],
    "cost_calculated_feature_bytes": calculated,
    "shared_ml_growth_bytes": ml_growth,
    "other_net_growth_bytes": growth - fetched - calculated - ml_growth,
    "cost_path_owned_bytes": sum(owned.values()),
    "categories_bytes": dict(categories), "cost_owned_categories_bytes": dict(owned),
    "cost_stock_detail_bytes": dict(stock_details), "cost_opra_schema_bytes": dict(opra_schemas),
    "attribution": "Shared ML growth includes all seven symbols' new pooled generation; it is not exclusively COST history. Retained native batch staging is included. Quarantine preserves fetched evidence.",
    "phase_note": "Measured after activation." if active else "Activation remains incomplete; later catchup or model stages may add storage.",
}
report["GiB"] = {k.removesuffix("_bytes"): round(v / 1024**3, 6)
                 for k, v in report.items() if k.endswith("_bytes") and isinstance(v, int)}
(base / "storage-measured.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"completed_requests": len(progress["completed_requests"]), "GiB": report["GiB"]}))
