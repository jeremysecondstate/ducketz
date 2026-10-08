"""Read-only verification of the intended session's local nightly completion.

No worker, review, provider, synthesis, transport or trader is launched here.
An old successful run is not readiness for a new action date.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp
from ml import nightly_workflow as workflow


def _joint_verification_pending(config: dict, state: dict, display: dict) -> bool:
    """Recognize a valid local adoption awaiting its completion receipt.

    Local completion remains evidence after adoption. Only the two known
    combined publication kinds may replace its displayed artifacts; unrelated
    generations, sessions or producer sources are still verification failures.
    """
    from app.ui.gameplan_data import load_gameplan
    from app.ui.gameplan_stats_data import load_gameplan_stats
    from ml.joint_capital_adoption import read_accepted_joint_plan

    root = Path(config["datastore"]).resolve()
    plan, stats = load_gameplan(root), load_gameplan_stats(root)
    if plan.session != state["action_date"] or stats.session != state["source_session"]:
        raise ValueError("Default UI selection differs from the saved nightly sessions")
    original_plan, original_stats = Path(display["plan_run"]).resolve(), Path(display["stats_run"]).resolve()
    changed_plan, changed_stats = plan.run_directory.resolve() != original_plan, stats.run_directory.resolve() != original_stats
    if changed_plan:
        accepted = read_accepted_joint_plan(root, state["action_date"])
        actor = config["actor"].lower()
        source = Path(display["source_gameplan_run"]).resolve()
        if (accepted is None or accepted[2] != plan.run_directory.resolve()
                or accepted[1]["local_actor"] != actor
                or accepted[1]["executor_owner"] != "atlas"
                or set(accepted[1]["owner_universes"].get(actor, ())) != set(state["symbols"])):
            raise ValueError("Default UI plan is not this machine's accepted combined publication")
        local_source = accepted[0]["input_bindings"][actor]
        if (local_source["run_id"] != source.name
                or local_source["source_hashes"]["receipt_sha256"] != file_checksum(source / "receipt.json")):
            raise ValueError("Default combined plan differs from the completed local source")
    if changed_stats:
        report = json.loads((stats.run_directory / "report.json").read_text(encoding="utf-8"))
        producers = report.get("producers", {})
        local_stats = producers.get(config["actor"], {})
        if (report.get("review_mode") != "combined-atlas-scout"
                or set(producers) != {"Atlas", "Scout"}
                or set(local_stats.get("symbols", ())) != set(state["symbols"])
                or local_stats.get("source_receipt_sha256") != file_checksum(original_stats / "receipt.json")
                or local_stats.get("source_report_sha256") != file_checksum(original_stats / "report.json")):
            raise ValueError("Default UI Stats differ from the completed local source")
    return changed_plan or changed_stats


def readiness(config: dict, *, now=None) -> dict:
    workflow.verify_installation(config)
    observed = utc_timestamp(now)
    local = observed.tz_convert("America/Los_Angeles")
    label = pd.Timestamp(local.date())
    calendar = xcals.get_calendar("XNYS", start=label - pd.Timedelta(days=10),
                                 end=label + pd.Timedelta(days=30))
    result = {"actor": config["actor"], "observed_at": observed.isoformat(),
              "local_ready": False, "joint_ready": False, "execution_authorized": False,
              "peer_communication_enabled": False, "orders_placed": 0}
    if local.hour < 17 and not calendar.is_session(label):
        return {**result, "status": "NOOP_NON_SESSION_DATE"}
    if local.hour >= 17:
        from ml.overnight_runtime import next_action_deadline
        day = next_action_deadline(observed).tz_convert("America/Los_Angeles").date().isoformat()
    else:
        day = local.date().isoformat()
    result["action_date"] = day
    state = workflow.status(config)
    result["worker_status"] = state["status"]
    if state.get("action_date") != day:
        return {**result, "status": "NOT_READY", "reason": "No worker result for the intended action date"}
    if state.get("actor") != config["actor"] or state.get("schema_version") != workflow.VERSION:
        raise ValueError("Saved nightly identity differs from this machine")
    workflow._verify_configuration_binding(config, state)
    workflow._verify_symbol_binding(config, state)
    if state["source_identity"] != workflow.source_identity(Path(config["repository"])):
        raise ValueError("Application source differs from the saved nightly run")
    for step in workflow.STEPS:
        entry = state.get("steps", {}).get(step, {})
        if entry.get("status") != "COMPLETE":
            return {**result, "status": "NOT_READY", "step": step,
                    "reason": entry.get("error") or state.get("error") or "Stage incomplete"}
        workflow._verify_outputs(entry["output"])
    if state["status"] != "LOCAL_COMPLETE_PEER_SETUP_PENDING":
        return {**result, "status": "NOT_READY", "reason": state.get("error") or "Worker has not completed"}
    display = workflow._verify_local_preparation(config, state)
    if display != state["steps"]["verify_display"]["output"]:
        raise ValueError("Frozen local preparation differs from its saved display verification")
    handoff = state["steps"]["local_handoff"]["output"]
    if handoff.get("delivery") != "HELD_FOR_SEPARATE_PC_SETUP":
        raise ValueError("Local rollout handoff hold differs")
    result = {**result, "local_ready": True,
            "source_session": state["source_session"], "run_id": state["run_id"],
            "completed_at": state["completed_at"], "source_identity": state["source_identity"]}
    from ml.nightly_joint_readiness import verify_joint_readiness
    try:
        joint = verify_joint_readiness(config, state)
        if joint is not None:
            return {**result, **joint}
        if _joint_verification_pending(config, state, display):
            return {**result, "status": "JOINT_VERIFICATION_PENDING", "ui_ready": False,
                    "reason": "Combined publication is selected; its local completion receipt is not verified"}
    except (ValueError, OSError) as exc:
        return {**result, "status": "JOINT_VERIFICATION_FAILED", "ui_ready": False,
                "reason": "Frozen local preparation passed; current joint publication did not verify",
                "error": f"{type(exc).__name__}: {exc}"}
    return {**result, "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = readiness(workflow.load_config(args.config))
        code = 0 if result["status"] in {"NOOP_NON_SESSION_DATE", "LOCAL_COMPLETE_PEER_SETUP_PENDING",
                                         "JOINT_READY_LOCAL", "HANDOFF_VERIFIED_LOCAL"} else 1
    except Exception as exc:
        result, code = {"status": "VERIFICATION_FAILED", "error": f"{type(exc).__name__}: {exc}",
                        "local_ready": False, "joint_ready": False, "orders_placed": 0}, 1
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
