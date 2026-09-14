"""Offline COST source comparison; never calls providers or writes production data."""
from pathlib import Path
import json
import hashlib
import pandas as pd

OUT = Path(__file__).parent
FILES = {
    "EQUS.MINI": Path("C:/DATASTORE/stocks/COST/bars/1m/databento/raw/COST_source_100d_1m_ohlcv-1m_1m_raw.parquet"),
    "SCHWAB_CANONICAL": Path("C:/DATASTORE/stocks/COST/bars/1m/schwab/normalized/COST_day_10_minute_1.parquet"),
    "SCHWAB_SEP10_PROBE": Path("C:/dev/ducketz/artifacts/analysis/cost-closing-gap-20260911/schwab-raw.json"),
    "SCHWAB_QUOTE_RAW": Path("C:/DATASTORE/stocks/COST/quotes/schwab/raw/COST_quotes.parquet"),
}
out = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "scope": "Offline feasibility only; close-boundary diagnostics do not authorize source substitution", "inputs": {}, "minute_sources": {}, "quotes": []}
for name, path in FILES.items():
    out["inputs"][name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if name == "SCHWAB_QUOTE_RAW":
        for row in pd.read_parquet(path).to_dict("records"):
            quote = json.loads(row["payload_json"])
            out["quotes"].append({"fetched_at": str(row["fetched_at"]), "quote_time": pd.Timestamp(quote["quoteTime"], unit="ms", tz="UTC").isoformat(), "trade_time": pd.Timestamp(quote["tradeTime"], unit="ms", tz="UTC").isoformat(), "last_price": quote["lastPrice"]})
        continue
    if path.suffix == ".json":
        frame = pd.DataFrame(json.loads(path.read_text())["candles"])
        frame["timestamp"] = pd.to_datetime(frame["datetime"], unit="ms", utc=True)
    else:
        frame = pd.read_parquet(path)
        if "ts_event" in frame:
            assert set(frame.provider_dataset) == {"EQUS.MINI"}
            frame["timestamp"] = frame.ts_event
    frame = frame.sort_values("timestamp")
    complete = frame.timestamp + pd.Timedelta(minutes=1)
    rows = []
    for date in ("2026-09-09", "2026-09-10", "2026-09-11"):
        begin = pd.Timestamp(date, tz="America/Los_Angeles").tz_convert("UTC")
        today = frame[(frame.timestamp >= begin) & (frame.timestamp < begin + pd.Timedelta(days=1))]
        item = {"date": date, "rows": len(today), "first_bar_start": str(today.timestamp.min()), "last_bar_completion": str((today.timestamp + pd.Timedelta(minutes=1)).max()), "close_boundaries": []}
        for hour in (5, 14, 15, 16, 17):
            boundary = begin + pd.Timedelta(hours=hour)
            candidates = frame[(complete <= boundary) & (frame.timestamp >= begin)]
            last = candidates.iloc[-1] if len(candidates) else None
            age = float((boundary - last.timestamp - pd.Timedelta(minutes=1)).total_seconds() / 60) if last is not None else None
            item["close_boundaries"].append({"clock_pacific": f"{hour:02}:00", "last_bar_start_utc": str(last.timestamp) if last is not None else None, "age_minutes": age, "within_five_minutes": age is not None and 0 <= age <= 5})
        rows.append(item)
    out["minute_sources"][name] = rows
(OUT / "source-options-evidence.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(out, indent=2))
