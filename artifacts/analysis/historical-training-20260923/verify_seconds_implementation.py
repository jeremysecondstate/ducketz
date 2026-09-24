"""Read-only production-fixture validation of the archive consistency loader."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from ml.gameplan_archive_seconds import verify_second_minute_overlap

symbols = tuple(s.strip().upper() for s in (REPO / "datafetching/watchlist.txt").read_text().splitlines()
                if s.strip() and not s.strip().startswith("#"))
report, files = verify_second_minute_overlap(Path("C:/DATASTORE"), symbols=symbols,
                                            available_at=datetime.now(timezone.utc))
output = {**report, "source_files": [str(p) for p in files]}
target = Path(__file__).with_name("seconds-implementation-verification.json")
target.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n", encoding="utf-8")
print(json.dumps({"status": report["status"], "partitions": report["native_archive_partitions_verified"],
                  "source_files": len(files), "output": str(target),
                  "overlap_minutes": sum(r["overlap_minutes"] for r in report["by_symbol"].values()),
                  "only_seconds_minutes_unused": sum(r["only_seconds_minutes_unused"] for r in report["by_symbol"].values()),
                  "undefined_second_rows": sum(r["undefined_second_rows"] for r in report["by_symbol"].values())}, indent=2))
