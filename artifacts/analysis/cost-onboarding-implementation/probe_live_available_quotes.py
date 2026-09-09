"""Inspect the gateway's start=0 replay without publishing any data."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import databento as db
import pandas as pd
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_opra_replay import _CappedStream

base = Path(__file__).resolve().parent
observed = datetime.now(timezone.utc)
directory = base / ("live-all-available-review-" + observed.strftime("%Y%m%dT%H%M%SZ"))
directory.mkdir()
load_repository_environment()
client = db.Live(key=os.environ["DATABENTO_API_KEY"], compression=db.Compression.ZSTD,
                 reconnect_policy="none", slow_reader_behavior="warn")
stream = _CappedStream(directory / "quotes.dbn", 192 * 1024 * 1024)
result = {"checked_at": observed.isoformat(), "schema": "cbbo-1m", "symbols": ["COST.OPT"],
          "start": 0, "data_records": 0, "first_ts_recv": None, "last_ts_recv": None,
          "control_messages": [], "errors": [], "replay_complete": False,
          "canonical_publication_attempted": False}

def exception(exc):
    result["errors"].append(f"{type(exc).__name__}: {exc}")
    client.stop()

def record(item):
    if isinstance(item, db.ErrorMsg):
        result["errors"].append(str(item.err))
        client.stop()
    elif isinstance(item, db.SystemMsg):
        result["control_messages"].append({"code": int(item.code), "message": str(item.msg)})
        if int(item.code) == 3:
            result["replay_complete"] = True
            client.stop()
    elif int(item.rtype) == 193:
        timestamp = int(item.ts_recv)
        result["data_records"] += 1
        result["first_ts_recv"] = timestamp if result["first_ts_recv"] is None else min(result["first_ts_recv"], timestamp)
        result["last_ts_recv"] = timestamp if result["last_ts_recv"] is None else max(result["last_ts_recv"], timestamp)

client.add_callback(record, exception_callback=exception)
client.add_stream(stream, exception_callback=exception)
try:
    client.subscribe(dataset="OPRA.PILLAR", schema="cbbo-1m", symbols=["COST.OPT"], stype_in="parent", start=0)
    client.start()
    client.block_for_close(timeout=35)
except Exception as exc:
    result["errors"].append(f"{type(exc).__name__}: {exc}")
finally:
    client.terminate()
    if client.is_connected():
        client.block_for_close(timeout=5)
    stream.close()
for name in ("first_ts_recv", "last_ts_recv"):
    if result[name] is not None:
        result[name] = pd.Timestamp(result[name], unit="ns", tz="UTC").isoformat()
result["finished_at"] = datetime.now(timezone.utc).isoformat()
result["raw_bytes"] = (directory / "quotes.dbn").stat().st_size
result["evidence_directory"] = str(directory)
(directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
(base / "live-all-available-check.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({**{key: value for key, value in result.items() if key != "control_messages"},
                  "control_message_count": len(result["control_messages"])}), flush=True)
