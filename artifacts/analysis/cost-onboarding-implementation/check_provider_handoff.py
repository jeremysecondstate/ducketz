"""Read-only final availability and retained provider-quality reconciliation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import databento
from datafetching.cme_runtime import load_repository_environment

base = Path(__file__).resolve().parent
load_repository_environment()
client = databento.Historical(os.environ["DATABENTO_API_KEY"])
provider_range = client.metadata.get_dataset_range(dataset="OPRA.PILLAR")
observed_at = datetime.now(timezone.utc).isoformat()
with (base / "provider-range-checks.jsonl").open("a") as handle:
    handle.write(json.dumps({"observed_at": observed_at, "dataset": "OPRA.PILLAR", "range": provider_range}) + "\n")

requests = [(schema, day) for schema, days in {
    "ohlcv-1h": ["2024-06-03", "2025-10-22"],
    "ohlcv-1d": ["2021-06-18", "2024-06-03", "2025-10-22"],
}.items() for day in days]

def get_count(request):
    schema, day = request
    records = client.metadata.get_record_count(dataset="OPRA.PILLAR", schema=schema,
        symbols=["COST.OPT"], stype_in="parent", start=day,
        end=str(date.fromisoformat(day) + timedelta(days=1)))
    return {"schema": schema, "date": day, "provider_records": records}

with ThreadPoolExecutor(max_workers=4) as pool:
    rows = list(pool.map(get_count, requests))
audit = json.loads((base / "archive-verification.json").read_text())
reconciliation = []
for schema in ("ohlcv-1h", "ohlcv-1d"):
    entry = next(item for item in audit["requests"] if item["dataset"] == "OPRA.PILLAR" and item["schema"] == schema)
    ignored = sum(row["provider_records"] for row in rows if row["schema"] == schema)
    reconciliation.append({"schema": schema, "ignored_provider_records": ignored,
        "broad_count_difference": -entry["record_difference_after_duplicates"],
        "matches": ignored == -entry["record_difference_after_duplicates"]})
result = {"observed_at": observed_at, "excluded_date_counts": rows, "reconciliation": reconciliation}
(base / "provider-quality-reconciliation.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({"observed_at": observed_at, "opra_exclusive_end": provider_range["end"],
    "production_schema_ends": {name: provider_range["schema"][name]["end"] for name in ("definition", "ohlcv-1h", "cbbo-1m")},
    "reconciliation": reconciliation}))
