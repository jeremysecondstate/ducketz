"""Offline readback of each candidate native DBN and diagnostic receipt."""
import hashlib
import json
from pathlib import Path
import databento as db
import pandas as pd

root = Path(__file__).parent / "source-probe"
report = json.loads((root / "report.json").read_text())
assert report["status"] == "COMPLETE"
results = []
for result in report["windows"]:
    directory = root / result["window"]
    receipt = json.loads((directory / "receipt.json").read_text())
    preflight = json.loads((directory / "preflight.json").read_text())
    assert preflight["estimated_cost_usd"] == 0 and preflight["capacity_pass"] and preflight["range_pass"]
    for name, field in (("provider.dbn", "raw_sha256"), ("observations.parquet", "observations_sha256"), ("native-metadata.json", "native_metadata_sha256"), ("preflight.json", "preflight_sha256")):
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == receipt[field]
    store = db.DBNStore.from_file(directory / "provider.dbn")
    try:
        assert str(store.metadata.dataset) == "XNAS.BASIC" and str(store.metadata.schema) == "ohlcv-1m"
        assert store.metadata.start == pd.Timestamp(receipt["request"]["start"]).value
        assert store.metadata.end == pd.Timestamp(receipt["request"]["end"]).value
        assert not store.metadata.partial and not store.metadata.not_found
        frame = store.to_df(schema="ohlcv-1m", map_symbols=True, price_type="float").reset_index().sort_values("ts_event")
    finally:
        store.reader.close()
    saved = pd.read_parquet(directory / "observations.parquet")
    pd.testing.assert_frame_equal(frame, saved)
    assert len(frame) == receipt["rows"] == preflight["estimated_record_count"]
    assert set(frame.publisher_id) == {93} and set(frame.symbol) == {"COST"}
    row = {"window": directory.name, "rows": len(frame), "native_zero_volume_rows": int(frame.volume.eq(0).sum()), "raw_reproduces_saved": True, "hashes_verified": True}
    if directory.name == "sep11-afterhours":
        row["first_open_observations"] = []
        for hour in (21, 22, 23):
            clock = pd.Timestamp(f"2026-09-11T{hour:02}:00:00Z")
            candidate = frame[frame.ts_event >= clock].iloc[0]
            distance = (candidate.ts_event - clock).total_seconds()/60
            row["first_open_observations"].append({"clock_utc": str(clock), "bar_start": str(candidate.ts_event), "open": float(candidate.open), "distance_minutes": distance, "within_five_minutes": bool(0 <= distance <= 5)})
    results.append(row)
verification = {"verified_at": pd.Timestamp.now(tz="UTC").isoformat(), "status": "VERIFIED", "windows": results, "production_writes": False}
(root / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
print(json.dumps(verification, indent=2))
