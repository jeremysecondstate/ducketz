"""Read-only verification of a locally selected completed joint publication.

Only the local synthesis/handoff publisher selects a completion receipt. This
selection is evidence of display readiness, never transport or order authority.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pandas as pd

from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import file_checksum
from ml.gameplan_actuals_review import _write_json
from ml.gameplan_stats_handoff import read_stats_package
from ml.joint_capital_adoption import read_accepted_joint_plan
from ml.joint_capital_handoff import _object, _read
from ml.joint_capital_plan import load_owner_package


VERSION = "nightly-joint-readiness-selection-v1"
FIELDS = {"schema_version", "local_actor", "action_date", "review_session",
          "completion_id", "receipt_path", "receipt_sha256"}
COMPLETIONS = {"scout": ("nightly-gameplan-synthesis-v1", "JOINT_READY_LOCAL"),
               "atlas": ("nightly-gameplan-handoff-v1", "HANDOFF_VERIFIED_LOCAL")}


def _selection(root: Path, action_date: str) -> Path:
    if date.fromisoformat(action_date).isoformat() != action_date:
        raise ValueError("Joint readiness requires an exact action date")
    return root / "ml/nightly-joint-readiness-by-date" / action_date / "run.json"


def _absolute(value) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute() or value.startswith(("\\\\", "//")):
        raise ValueError("Joint readiness requires explicit local absolute paths")
    return Path(value).resolve()


def _receipt_identity(receipt: dict, *, actor: str, action_date: str, review_session: str) -> None:
    expected = COMPLETIONS.get(actor)
    evidence = receipt.get("ui_evidence")
    if (not {"completion_id", "receipt_path", "account_scope_sha256"}.issubset(receipt)
            or not isinstance(receipt.get("owner_packages"), dict)
            or set(receipt["owner_packages"]) != {"atlas", "scout"}
            or not isinstance(receipt.get("stats_packages"), dict)
            or set(receipt["stats_packages"]) != {"Atlas", "Scout"}
            or not isinstance(evidence, dict)
            or not {"plan_run", "stats_run", "files", "plan_sha256", "stats_receipt_sha256"}.issubset(evidence)
            or not isinstance(evidence["files"], dict)):
        raise ValueError("Joint readiness receipt evidence fields differ from the completed contract")
    if (expected is None or receipt.get("schema_version") != expected[0]
            or receipt.get("status") != expected[1] or receipt.get("local_actor") != actor
            or receipt.get("executor_owner") != "atlas" or receipt.get("action_date") != action_date
            or receipt.get("review_session") != review_session or receipt.get("ui_ready") is not True
            or receipt.get("joint_ready") is not True or receipt.get("execution_authorized") is not False
            or receipt.get("activation_changed") is not False or receipt.get("orders_placed") != 0):
        raise ValueError("Joint readiness receipt identity, session or authority differs")


def publish_joint_readiness(root: Path, spec: dict, receipt: dict) -> None:
    """Pin only this local publisher's exact completed receipt; retry is immutable."""
    root = Path(root).resolve()
    if root != _absolute(spec["datastore_root"]):
        raise ValueError("Joint readiness datastore differs from its local specification")
    _receipt_identity(receipt, actor=spec["local_actor"], action_date=spec["action_date"],
                      review_session=spec["review_session"])
    expected_path = _absolute(spec["state_root"]) / spec["completion_id"] / "receipt.json"
    receipt_path = _absolute(receipt["receipt_path"])
    if (receipt_path != expected_path or receipt["completion_id"] != spec["completion_id"]
            or _object(_read(receipt_path, receipt_path.parent)) != receipt):
        raise ValueError("Joint readiness must select this publisher's saved completion receipt")
    pin = {"schema_version": VERSION, "local_actor": spec["local_actor"],
           "action_date": spec["action_date"], "review_session": spec["review_session"],
           "completion_id": spec["completion_id"], "receipt_path": str(receipt_path),
           "receipt_sha256": file_checksum(receipt_path)}
    path = _selection(root, spec["action_date"])
    with exclusive_runtime_lock(root / "locks/nightly-joint-readiness.lock", process_name="nightly-joint-readiness"):
        if path.exists():
            if _object(_read(path, path.parent)) != pin:
                raise ValueError("This session already selects a different joint readiness receipt")
            return
        _write_json(path, pin)


def _verify_local_contribution(root: Path, state: dict, receipt: dict, plan: dict):
    actor = receipt["local_actor"]
    exports = state["steps"]["local_handoff"]["output"]
    display = state["steps"]["verify_display"]["output"]
    package_path, stats_path = _absolute(exports["package"]), _absolute(exports["stats_package"])
    for path in (package_path, stats_path):
        if exports.get("files", {}).get(str(path)) != file_checksum(path):
            raise ValueError("Local prepared handoff export changed")
    package = load_owner_package(package_path.parent, package_path,
                                  expected_sha256=receipt["owner_packages"][actor])
    source = _absolute(display["source_gameplan_run"])
    trade = _absolute(display["plan_run"])
    if (source.parent != root / "ml/nightly-gameplan-runs"
            or trade.parent != root / "ml/gameplan-trade-plan-runs"
            or package["owner_id"] != actor or package["action_date"] != state["action_date"]
            or package["frozen_symbols"] != state["symbols"] or package["run_id"] != source.name):
        raise ValueError("Joint readiness owner package differs from local preparation")
    expected_source = {"receipt_sha256": file_checksum(source / "receipt.json"),
                       "manifest_sha256": file_checksum(source / "manifest.json"),
                       "forecasts_sha256": file_checksum(source / "forecasts.parquet"),
                       "price_path_sha256": file_checksum(trade / "planning-price-path.json")}
    source_fields = ("run_id", "source_revision", "source_revision_status", "source_reference",
                     "frozen_symbols", "created_at", "source_hashes", "content_sha256", "package_sha256")
    if (package["source_hashes"] != expected_source
            or plan["input_bindings"].get(actor) != {key: package[key] for key in source_fields}):
        raise ValueError("Joint readiness substitutes this worker's frozen native source")
    stats, frame = read_stats_package(stats_path, expected_sha256=receipt["stats_packages"][actor.title()])
    original = _absolute(display["stats_run"])
    if (original.parent != root / "ml/gameplan-actuals-review-runs"
            or stats["producer"] != actor.title() or stats["action_date"] != state["source_session"]
            or stats["symbols"] != state["symbols"]
            or any(stats[field] != file_checksum(original / name) for field, name in (
                ("source_receipt_sha256", "receipt.json"), ("source_manifest_sha256", "manifest.json"),
                ("source_results_sha256", "forecast-results.parquet"), ("source_report_sha256", "report.json")))):
        raise ValueError("Joint readiness substitutes this worker's reviewed Stats contribution")
    return stats, frame


def verify_joint_readiness(config: dict, state: dict) -> dict | None:
    """Verify one session's local selection without discovering or writing inputs."""
    root = Path(config["datastore"]).resolve()
    selected_path = _selection(root, state["action_date"])
    if not selected_path.exists():
        return None
    pin = _object(_read(selected_path, selected_path.parent))
    actor = config["actor"].lower()
    if (set(pin) != FIELDS or pin["schema_version"] != VERSION or pin["local_actor"] != actor
            or pin["action_date"] != state["action_date"] or pin["review_session"] != state["source_session"]):
        raise ValueError("Joint readiness selection differs from this local worker session")
    receipt_path = _absolute(pin["receipt_path"])
    if file_checksum(receipt_path) != pin["receipt_sha256"]:
        raise ValueError("Selected joint readiness receipt changed")
    receipt = _object(_read(receipt_path, receipt_path.parent))
    _receipt_identity(receipt, actor=actor, action_date=state["action_date"], review_session=state["source_session"])
    if (receipt.get("completion_id") != pin["completion_id"]
            or _absolute(receipt.get("receipt_path")) != receipt_path):
        raise ValueError("Joint readiness completion identity differs from its selection")
    accepted = read_accepted_joint_plan(root, state["action_date"])
    if accepted is None:
        raise ValueError("Joint readiness has no accepted combined plan")
    plan, binding, plan_run = accepted
    evidence = receipt["ui_evidence"]
    if (binding["local_actor"] != actor or binding["executor_owner"] != "atlas"
            or binding["account_scope_sha256"] != receipt["account_scope_sha256"]
            or binding["owner_packages"] != receipt["owner_packages"]
            or plan["plan_sha256"] != evidence["plan_sha256"]
            or _absolute(evidence["plan_run"]) != plan_run):
        raise ValueError("Accepted combined plan differs from the completed local receipt")
    if actor == "atlas":
        # Scout's readiness delta does not need Atlas's account subsystem.
        from ml.account_gameplan.config import load_account_config
        account = load_account_config(root)
        if (account is None or account.machine_id != "pc-original" or account.coordinator_id != "pc-original"
                or account.account_fingerprint != binding["account_scope_sha256"]
                or set(account.participants["pc-original"]) != set(binding["owner_universes"]["atlas"])
                or set(account.participants["pc-new"]) != set(binding["owner_universes"]["scout"])):
            raise ValueError("Joint readiness differs from Atlas's local account binding")
    ui_plan, stats = load_gameplan(root), load_gameplan_stats(root)
    dated_stats = load_gameplan_stats(root, state["source_session"])
    stats_run = _absolute(evidence["stats_run"])
    if (ui_plan.run_directory != plan_run or ui_plan.session != state["action_date"]
            or set(ui_plan.symbols) != set(binding["execution_symbols"])
            or stats.run_directory != stats_run or dated_stats.run_directory != stats_run
            or stats.session != state["source_session"]
            or file_checksum(stats_run / "receipt.json") != evidence["stats_receipt_sha256"]):
        raise ValueError("Default combined UI differs from its selected completion receipt")
    paths = (plan_run / "accepted-plan.json", plan_run / "joint-plan.json", stats_run / "receipt.json",
             stats_run / "manifest.json", stats_run / "forecast-results.parquet")
    expected_files = {str(path): file_checksum(path) for path in paths}
    if evidence.get("files") != expected_files:
        raise ValueError("Completed combined UI artifact bytes changed")
    report = json.loads((stats_run / "report.json").read_text(encoding="utf-8"))
    producers = report.get("producers", {})
    if (report.get("review_mode") != "combined-atlas-scout" or set(producers) != {"Atlas", "Scout"}
            or set(receipt["stats_packages"]) != set(producers)
            or any(producers[title].get("package_sha256") != receipt["stats_packages"][title]
                   or producers[title].get("symbols") != binding["owner_universes"][title.lower()]
                   for title in producers)):
        raise ValueError("Combined Stats source bindings differ from the selected receipt")
    local_stats, local_frame = _verify_local_contribution(root, state, receipt, plan)
    if any(producers[actor.title()].get(field) != local_stats[field] for field in (
            "symbols", "coverage_status", "probability_target_contract", "source_receipt_sha256", "source_report_sha256")):
        raise ValueError("Combined Stats metadata substitutes the local reviewed source")
    combined_frame = pd.read_parquet(stats_run / "forecast-results.parquet")
    local_rows = combined_frame.loc[combined_frame.symbol.isin(state["symbols"])]
    try:
        pd.testing.assert_frame_equal(local_rows.sort_values("id").sort_index(axis=1).reset_index(drop=True),
            local_frame.sort_values("id").sort_index(axis=1).reset_index(drop=True), check_dtype=False, check_exact=True)
    except AssertionError as exc:
        raise ValueError("Combined Stats rows differ from the local reviewed contribution") from exc
    if file_checksum(receipt_path) != pin["receipt_sha256"] or _object(_read(selected_path, selected_path.parent)) != pin:
        raise ValueError("Joint readiness selection changed during verification")
    return {"status": receipt["status"], "joint_ready": True, "ui_ready": True,
            "peer_verified": False, "execution_authorized": False, "orders_placed": 0,
            "completion_id": receipt["completion_id"], "receipt_path": str(receipt_path),
            "receipt_sha256": pin["receipt_sha256"], "selection_path": str(selected_path),
            "ui_evidence": evidence}
