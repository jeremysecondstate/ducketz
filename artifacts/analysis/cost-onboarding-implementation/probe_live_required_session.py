"""Try the existing bounded Live quote route for COST's missing session."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import databento
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_opra_replay import capture_replay
from datafetching.opra_replay_fallback import replay_session_bounds

base = Path(__file__).resolve().parent
observed_at = datetime.now(timezone.utc)
directory = base / ("live-replay-review-" + observed_at.strftime("%Y%m%dT%H%M%SZ"))
directory.mkdir()
load_repository_environment()
client = databento.Historical(os.environ["DATABENTO_API_KEY"])
available = client.metadata.get_dataset_range(dataset="OPRA.PILLAR")
with (base / "provider-range-checks.jsonl").open("a") as handle:
    handle.write(json.dumps({"observed_at": observed_at.isoformat(), "dataset": "OPRA.PILLAR", "range": available}) + "\n")
start, end = replay_session_bounds("2026-09-04", "cbbo-1m")
result = {"checked_at": observed_at.isoformat(), "schema": "cbbo-1m", "symbol": "COST.OPT",
          "start": start.isoformat(), "end": end.isoformat(), "historical_exclusive_end": available["end"],
          "canonical_publication_attempted": False, "production_unchanged": True}
print(json.dumps({"probe": result, "evidence_directory": str(directory)}), flush=True)
if all(available["schema"][schema]["end"][:10] >= "2026-09-05" for schema in ("definition", "ohlcv-1h", "cbbo-1m")):
    result["status"] = "HISTORICAL_SESSION_AVAILABLE"
else:
    try:
        delivery = capture_replay(os.environ["DATABENTO_API_KEY"], "cbbo-1m", ["COST.OPT"], start, end,
                                  directory / "quotes.dbn", max_bytes=192 * 1024 * 1024, timeout_seconds=45)
        (directory / "delivery.json").write_text(json.dumps(delivery, indent=2) + "\n")
        result["status"] = "VERIFIED_LIVE_CAPTURE"
        result["delivery_path"] = str(directory / "delivery.json")
    except Exception as exc:
        result["status"] = "LIVE_CAPTURE_UNAVAILABLE"
        result["exception_type"] = type(exc).__name__
        result["error"] = str(exc)
result["finished_at"] = datetime.now(timezone.utc).isoformat()
(directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
(base / "live-required-session-check.json").write_text(json.dumps({**result, "evidence_directory": str(directory)}, indent=2) + "\n")
print(json.dumps(result), flush=True)
