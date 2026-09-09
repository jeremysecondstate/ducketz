"""Count the native September 3 delivery independently of normalization."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

import databento
import pyarrow.parquet as pq

base = Path(__file__).resolve().parent
partition = Path(r"C:\DATASTORE\market-data\databento\opra\OPRA.PILLAR\cbbo-1m\COST.OPT\dates\2026-09-03\segments\full-day")
store = databento.DBNStore.from_file(partition / "provider.dbn.zst")
counts = Counter(type(record).__name__ for record in store)
result = {
    "observed_at": datetime.now(timezone.utc).isoformat(),
    "native_record_types": dict(counts),
    "native_record_count": sum(counts.values()),
    "normalized_record_count": pq.read_metadata(partition / "normalized.parquet").num_rows,
    "native_metadata": str(store.metadata),
}
(base / "quote-native-count.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({key: value for key, value in result.items() if key != "native_metadata"}))
