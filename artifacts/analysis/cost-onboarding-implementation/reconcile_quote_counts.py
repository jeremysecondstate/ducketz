"""Read-only provider/count comparison for the matched minute-quote window."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta, datetime, timezone
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
plan = json.loads((base / "plan.json").read_text())
request = next(r for r in plan["requests"] if r["schema"] == "cbbo-1m")
start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
conditions = client.metadata.get_dataset_condition(dataset="OPRA.PILLAR", start_date=str(start), end_date=str(end))
conditions = {str(r["date"]): r["condition"] for r in conditions}
local = {}
for path in Path(request["storage_path"]).glob("dates/*/segments/*/manifest.json"):
    m = json.loads(path.read_text())
    local[m["partition_date"]] = local.get(m["partition_date"], 0) + m["normalized"]["row_count"]

def compare(day):
    records = client.metadata.get_record_count(dataset="OPRA.PILLAR", schema="cbbo-1m", symbols=["COST.OPT"],
        stype_in="parent", start=str(day), end=str(day + timedelta(days=1)))
    return {"day": str(day), "condition": conditions.get(str(day)), "provider_records": records,
            "normalized_records": local.get(str(day), 0)}

with ThreadPoolExecutor(max_workers=4) as pool:
    rows = list(pool.map(compare, [start + timedelta(days=i) for i in range((end - start).days)]))
result = {"observed_at": datetime.now(timezone.utc).isoformat(), "rows": rows,
          "provider_daily_sum": sum(r["provider_records"] for r in rows),
          "normalized_sum": sum(local.values()),
          "differences": [r for r in rows if r["provider_records"] != r["normalized_records"]]}
(base / "quote-count-reconciliation.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({k: v for k, v in result.items() if k != "rows"}))
