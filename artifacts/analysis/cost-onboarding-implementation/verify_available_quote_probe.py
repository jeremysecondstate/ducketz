"""Count saved native probe records independently of the Live callback."""
from collections import Counter
import json
from pathlib import Path

import databento

base = Path(__file__).resolve().parent
result = json.loads((base / "live-all-available-check.json").read_text())
native = Path(result["evidence_directory"]) / "quotes.dbn"
records = Counter(type(record).__name__ for record in databento.DBNStore.from_file(native))
output = {"native_record_types": dict(records), "native_market_quote_records": records.get("CBBOMsg", 0),
          "callback_market_quote_records": result["data_records"],
          "replay_complete": result["replay_complete"], "errors": result["errors"],
          "subscription_acknowledgements": [row for row in result["control_messages"] if row["code"] == 1],
          "completion_messages": [row for row in result["control_messages"] if row["code"] == 3]}
assert output["native_market_quote_records"] == output["callback_market_quote_records"]
(base / "live-all-available-native-verification.json").write_text(json.dumps(output, indent=2) + "\n")
print(json.dumps(output))
