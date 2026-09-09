"""Verify one provider delivery again without replacing canonical history."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
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
request = dict(dataset="OPRA.PILLAR", schema="cbbo-1m", symbols=["COST.OPT"], stype_in="parent",
               start="2026-09-03", end="2026-09-04")
quote = client.metadata.get_cost(**request)
if quote != 0:
    raise RuntimeError(f"Read-only verification delivery was not quoted free: {quote}")
target = base / "cbbo-1m-2026-09-03-verification.dbn.zst"
store = (databento.DBNStore.from_file(target) if target.exists()
         else client.timeseries.get_range(**request, path=target))
counts = Counter(type(record).__name__ for record in store)
with target.open("rb") as handle:
    digest = hashlib.file_digest(handle, "sha256").hexdigest()
original = Path(r"C:\DATASTORE\market-data\databento\opra\OPRA.PILLAR\cbbo-1m\COST.OPT\dates\2026-09-03\segments\full-day\manifest.json")
manifest = json.loads(original.read_text())
result = {"observed_at": datetime.now(timezone.utc).isoformat(), "request": request,
          "quote_usd": quote, "native_record_types": dict(counts), "record_count": sum(counts.values()),
          "size_bytes": target.stat().st_size, "checksum_sha256": digest,
          "matches_original_checksum": digest == manifest["raw"]["checksum_sha256"],
          "matches_original_count": sum(counts.values()) == manifest["normalized"]["row_count"]}
(base / "quote-redelivery-verification.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
