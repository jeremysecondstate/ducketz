"""Verify completed bootstrap and unchanged production authority before handoff."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from datafetching.symbol_onboarding import load_plan
from datafetching.symbol_universe import REPOSITORY_WATCHLIST, read_symbols
from ml.nightly_gameplan import read_current_gameplan
from ml.stock_trader.model import load_current_enrichment_model

base = Path(__file__).resolve().parent
plan = load_plan(base / "plan.json")
root = Path(plan["datastore_root"])
progress = json.loads((base / "progress.json").read_text())
assert progress["plan_id"] == plan["plan_id"]
assert progress["status"] == "HISTORY_FETCHED"
assert set(progress["completed_requests"]) == {row["request_id"] for row in plan["requests"]}
registry = json.loads((root / "state/symbol-onboarding/COST.json").read_text())
assert registry["plan_id"] == plan["plan_id"]
assert Path(registry["plan_path"]).resolve() == (base / "plan.json").resolve()
assert not Path(registry["activation_path"]).exists()
assert read_symbols(REPOSITORY_WATCHLIST) == tuple(plan["previous_symbols"])
assert read_symbols(base / "candidate-watchlist.txt") == tuple(plan["candidate_symbols"])

publication = read_current_gameplan(root)
assert publication.manifest["configuration"]["symbols"] == plan["previous_symbols"]
model = load_current_enrichment_model(root)
assert "symbol_COST" not in model.feature_names
before_pointers = json.loads((base / "publication-pointers-before.json").read_text())["pointers"]
preserved = []
for entry in before_pointers:
    path = Path(entry["path"])
    if path.parent.name not in {"nightly-gameplan-latest", "stock-trader-model-latest"}:
        continue
    with path.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == entry["checksum_sha256"]
    preserved.append(str(path))
assert len(preserved) == 2

before = json.loads((base / "automations-before.json").read_text(encoding="utf-8-sig"))
after = {item["id"]: item for item in json.loads((base / "automations-after.json").read_text(encoding="utf-8-sig"))}
settings = ("status", "rrule", "model", "reasoning_effort", "notification_policy", "execution_environment", "target", "cwds")
automations = []
for original in before:
    current = tomllib.loads((Path.home() / ".codex/automations" / original["id"] / "automation.toml").read_text(encoding="utf-8"))
    for key in settings:
        assert current.get(key) == original.get(key), (current["id"], key)
    assert current["prompt"] == after[current["id"]]["prompt"], current["id"]
    assert current["name"] == after[current["id"]]["name"], current["id"]
    automations.append({"id": current["id"], "name": current["name"], "status": current["status"], "settings_preserved": True})
assert len(automations) == 10
watch = next(row for row in automations if row["id"] == "loops-stock-trader-daily-adaptation")
assert watch["status"] == "ACTIVE"

output = {"checked_at": datetime.now(timezone.utc).isoformat(), "plan_id": plan["plan_id"],
    "state": "AWAITING_PROVIDER_REQUIRED_SESSION", "approved_archive_requests_completed": len(plan["requests"]),
    "production_symbols": list(read_symbols(REPOSITORY_WATCHLIST)), "candidate_symbols": plan["candidate_symbols"],
    "production_gameplan": str(publication.run_directory), "production_stock_model_features": len(model.feature_names),
    "preserved_production_pointers": preserved, "registered_continuation": registry,
    "automations": automations, "activation_complete": False}
(base / "handoff-verification.json").write_text(json.dumps(output, indent=2) + "\n")
print(json.dumps({key: value for key, value in output.items() if key not in {"automations", "registered_continuation"}}))
print(json.dumps({"verified_automations": len(automations), "active": sum(row["status"] == "ACTIVE" for row in automations),
                  "paused": sum(row["status"] == "PAUSED" for row in automations), "health_watch": watch["status"]}))
