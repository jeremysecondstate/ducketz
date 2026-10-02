"""Read-only fingerprints around the authorized source installation."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

root = Path(r"C:\dev\ducketz")
data = Path(r"C:\DATASTORE")
out = Path(__file__).resolve().parent
phase = sys.argv[1]
assert phase in {"before", "after"}
destination = out / ("production-preservation-" + phase + ".json")
assert not destination.exists(), "Preserve existing observation"
def digest(path):
    return hashlib.file_digest(path.open("rb"), "sha256").hexdigest() if path.is_file() else None
def git(*args):
    return subprocess.check_output(["git", *args], cwd=root).decode().strip()
paths = ["controls/stock-trader/operator-intent.txt", "controls/gameplan-stock-trader/operator-intent.txt",
         "state/independent-stock-trader/holdings.sqlite3", "state/independent-stock-trader/holdings.sqlite3-wal",
         "state/independent-stock-trader/holdings.sqlite3-shm", "state/independent-stock-trader/session-status.json",
         "ml/nightly-gameplan-latest/run.json", "ml/gameplan-trade-plan-latest/run.json",
         "ml/gameplan-actuals-review-latest/run.json"]
published = data / "ml/nightly-gameplan-runs/20260930T061501.586401Z"
paths.extend(str(p.relative_to(data)).replace("\\", "/") for p in published.rglob("*") if p.is_file())
manifest = json.loads((out / "candidate-manifest.json").read_text())
owned = {r["path"] for r in manifest["files"]}
dirty = {p for p in git("diff", "HEAD", "--name-only", "-z").split("\0") if p} - owned
index = Path(git("rev-parse", "--git-path", "index"))
if not index.is_absolute():
    index = root / index
report = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "phase": phase,
          "git": {"head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"), "index_sha256": digest(index)},
          "protected_datastore_files": {p: digest(data / p) for p in sorted(paths)},
          "unrelated_dirty_sources": {p: digest(root / p) for p in sorted(dirty)}}
if phase == "after":
    before = json.loads((out / "production-preservation-before.json").read_text())
    report["changed_sections"] = [key for key in ("git", "protected_datastore_files", "unrelated_dirty_sources") if report[key] != before[key]]
    report["status"] = "PASS" if not report["changed_sections"] else "CHANGED_REQUIRES_INSPECTION"
destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"path": str(destination), "phase": phase, "status": report.get("status", "RECORDED"),
                  "protected_file_count": len(report["protected_datastore_files"]),
                  "unrelated_dirty_file_count": len(report["unrelated_dirty_sources"])}))
