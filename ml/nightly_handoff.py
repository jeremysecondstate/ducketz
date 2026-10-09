"""Adopt an explicitly selected Scout synthesis on Atlas without reallocating capital.

All inputs are already local, reviewed, immutable selections. Hashes establish
consistency, not sender authentication or execution authority. Transport and
the human's manual trader start remain separate operations.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.config import CONFIG, load_account_config
from ml.artifacts import utc_timestamp
from ml.gameplan_actuals_review import _write_json, completed_session_context
from ml.gameplan_stats_handoff import read_stats_package, combined_stats_target, combine_stats_frames
from ml.joint_capital_adoption import _verify, read_accepted_joint_plan
from ml.joint_capital_handoff import _object, _read
from ml.joint_capital_plan import content_sha256, load_joint_plan, load_owner_package
from ml.nightly_synthesis import (ACTORS, _path, _digest, _stamp, _selected_file,
    _adopt_plan, _adopt_stats, _matching_stats, _verify_ui)


VERSION = "nightly-gameplan-handoff-v1"
FIELDS = {"schema_version", "completion_id", "datastore_root", "state_root", "local_actor",
          "executor_owner", "action_date", "review_session", "account_scope_sha256", "accepted_at",
          "local_profile", "account_config", "scout_receipt", "joint_plan", "owners"}


def _validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != FIELDS or spec["schema_version"] != VERSION:
        raise ValueError("Unsupported Atlas handoff specification")
    if not isinstance(spec["completion_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}", spec["completion_id"]):
        raise ValueError("A stable 8–64 character handoff completion ID is required")
    if spec["local_actor"] != "atlas" or spec["executor_owner"] != "atlas":
        raise ValueError("Handoff receipt adoption belongs to Atlas")
    for key in ("datastore_root", "state_root"):
        _path(spec[key])
    _digest(spec["account_scope_sha256"])
    _stamp(spec["accepted_at"], "accepted_at")
    if not isinstance(spec["owners"], dict) or set(spec["owners"]) != set(ACTORS):
        raise ValueError("Exactly Atlas and Scout source selections are required")
    for owner in spec["owners"].values():
        if not isinstance(owner, dict) or set(owner) != {"symbols", "plan_package", "stats_package"}:
            raise ValueError("Each owner requires symbols, plan package and Stats package")


def _local_binding(spec, local_profile=None):
    profile = _object(_selected_file(spec["local_profile"]))
    selected = _path(spec["local_profile"]["path"])
    repository = Path(__file__).resolve().parents[1]
    if (selected.resolve() != repository / "scratch/cross-pc/local-profile.json"
            or _path(profile.get("checkout")).resolve() != repository
            or (local_profile is not None and _path(str(local_profile)).resolve() != selected.resolve())):
        raise ValueError("Handoff requires this checkout's installed local profile")
    if (profile.get("contract_version") != "cross-pc-v2" or profile.get("actor") != "Atlas"
            or profile.get("machine") != "pc-original"
            or profile.get("symbols") != spec["owners"]["atlas"]["symbols"]):
        raise ValueError("Handoff identity or research symbols differ from the Atlas profile")
    account_path = _path(spec["account_config"]["path"])
    if account_path.resolve() != (_path(spec["datastore_root"]) / CONFIG).resolve():
        raise ValueError("Handoff must bind the existing local account configuration")
    _selected_file(spec["account_config"])
    config = load_account_config(_path(spec["datastore_root"]))
    if (config is None or config.machine_id != "pc-original" or config.coordinator_id != "pc-original"
            or config.account_fingerprint != spec["account_scope_sha256"]
            or set(config.participants["pc-original"]) != set(spec["owners"]["atlas"]["symbols"])
            or set(config.participants["pc-new"]) != set(spec["owners"]["scout"]["symbols"])):
        raise ValueError("Local account identity or producer universes differ from the handoff")
    return config


def preflight_handoff(spec, *, local_profile=None, now=None):
    """Verify local selections and Scout's receipt without writing any output."""
    _validate_spec(spec)
    config = _local_binding(spec, local_profile)
    accepted = _stamp(spec["accepted_at"], "accepted_at")
    if accepted > utc_timestamp(now):
        raise ValueError("Selected acceptance time is in the future")
    receipt = _object(_selected_file(spec["scout_receipt"]))
    if (receipt.get("schema_version") != "nightly-gameplan-synthesis-v1"
            or receipt.get("status") != "JOINT_READY_LOCAL" or receipt.get("local_actor") != "scout"
            or receipt.get("executor_owner") != "atlas" or receipt.get("ui_ready") is not True
            or receipt.get("joint_ready") is not True or receipt.get("orders_placed") != 0
            or receipt.get("activation_changed") is not False):
        raise ValueError("Selected receipt does not verify Scout's completed synthesis")
    for key in ("action_date", "review_session", "account_scope_sha256"):
        if receipt.get(key) != spec[key]:
            raise ValueError("Scout receipt session or account binding differs")
    binding = spec["joint_plan"]
    _selected_file(binding, extra=("root", "plan_sha256"))
    plan = load_joint_plan(_path(binding["root"]), _path(binding["path"]),
                           expected_sha256=_digest(binding["plan_sha256"]))
    if accepted < _stamp(plan["as_of"], "synthesis time"):
        raise ValueError("Acceptance cannot precede Scout synthesis")
    if completed_session_context(plan["as_of"], spec["review_session"])["successor_action_date"] != spec["action_date"]:
        raise ValueError("Stats review session must precede the selected next exchange session")
    evidence = receipt.get("ui_evidence", {})
    # The remote receipt's path is a provenance key, never a path to execute or
    # fetch. Select its exact original plan file digest from the saved map.
    expected_path = str(evidence.get("plan_run", "")).replace("\\", "/").rstrip("/") + "/joint-plan.json"
    original_files = {str(path).replace("\\", "/"): value for path, value in evidence.get("files", {}).items()}
    if (evidence.get("plan_sha256") != plan["plan_sha256"]
            or original_files.get(expected_path) != binding["file_sha256"]
            or receipt.get("account_snapshot_sha256") != plan.get("account_snapshot_sha256")):
        raise ValueError("Scout receipt does not bind the selected frozen plan bytes")
    universes, hashes, stats_paths, stats_hashes, stats_symbols, sources, frames = {}, {}, {}, {}, {}, {}, []
    for actor, title in ACTORS.items():
        owner = spec["owners"][actor]
        selected = owner["plan_package"]
        _selected_file(selected, extra=("root", "package_sha256"))
        package = load_owner_package(_path(selected["root"]), _path(selected["path"]),
                                     expected_sha256=_digest(selected["package_sha256"]))
        source = plan.get("input_bindings", {}).get(actor, {})
        source_fields = ("run_id", "source_revision", "source_revision_status", "source_reference",
                         "frozen_symbols", "created_at", "source_hashes", "content_sha256", "package_sha256")
        if (package["owner_id"] != actor or package["action_date"] != spec["action_date"]
                or package["frozen_symbols"] != owner["symbols"]
                or source != {key: package[key] for key in source_fields}):
            raise ValueError("Selected owner package differs from Scout's frozen source binding")
        universes[actor], hashes[actor] = owner["symbols"], package["package_sha256"]
        stats_binding = owner["stats_package"]
        _selected_file(stats_binding)
        stats_paths[title], stats_hashes[title], stats_symbols[title] = (
            _path(stats_binding["path"]), stats_binding["file_sha256"], owner["symbols"])
        payload, frame = read_stats_package(stats_paths[title], expected_sha256=stats_hashes[title])
        if (payload["producer"] != title or payload["action_date"] != spec["review_session"]
                or payload["symbols"] != owner["symbols"]
                or _stamp(payload["reviewed_at"], "Stats review") > _stamp(plan["as_of"], "synthesis time")):
            raise ValueError("Selected Stats producer, session, universe or review time differs")
        sources[title] = payload
        frames.append(frame)
    if receipt.get("owner_packages") != hashes or receipt.get("stats_packages") != stats_hashes:
        raise ValueError("Scout receipt source package hashes differ")
    combined_stats_target(tuple(sources.values()))
    combined = combine_stats_frames(frames)
    if combined.id.duplicated().any() or combined.duplicated(["symbol", "route"]).any():
        raise ValueError("Combined Stats has duplicate forecast identities")
    _verify(plan, {"schema_version": "accepted-joint-gameplan-v1", "plan_sha256": plan["plan_sha256"],
        "action_date": spec["action_date"], "account_scope_sha256": spec["account_scope_sha256"],
        "local_actor": "atlas", "executor_owner": "atlas", "owner_packages": hashes,
        "owner_universes": universes, "execution_symbols": list(config.symbols)})
    # Recheck the exact original selections before returning verified data.
    for key in ("local_profile", "account_config", "scout_receipt"):
        _selected_file(spec[key])
    _selected_file(binding, extra=("root", "plan_sha256"))
    for owner in spec["owners"].values():
        _selected_file(owner["plan_package"], extra=("root", "package_sha256"))
        _selected_file(owner["stats_package"])
    return {"plan": plan, "universes": universes, "package_hashes": hashes, "stats_paths": stats_paths,
        "stats_hashes": stats_hashes, "stats_symbols": stats_symbols, "stats_frame": combined,
        "stats_sources": sources, "account_binding_sha256": config.fingerprint}


def run_handoff(spec, *, local_profile=None, now=None):
    """Resume exact receipt-bound Atlas adoption; never synthesize or activate."""
    prepared = preflight_handoff(spec, local_profile=local_profile, now=now)
    state_root, root = _path(spec["state_root"]), _path(spec["datastore_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    spec_hash = content_sha256(spec)
    work = state_root / spec["completion_id"]
    with exclusive_runtime_lock(state_root / "handoff.lock", process_name="nightly-handoff"):
        work.mkdir(parents=True, exist_ok=True)
        record = work / "state.json"
        state = _object(record.read_bytes()) if record.exists() else {
            "schema_version": VERSION, "completion_id": spec["completion_id"], "spec_sha256": spec_hash,
            "status": "PREPARING", "steps": {}, "orders_placed": 0, "activation_changed": False}
        if state.get("spec_sha256") != spec_hash:
            raise ValueError("Handoff retry changed its exact local specification")
        _write_json(record, state)
        try:
            if state["status"] == "HANDOFF_VERIFIED_LOCAL":
                evidence = _verify_ui(root, spec, prepared)
                receipt = _object((work / "receipt.json").read_bytes())
                if receipt["ui_evidence"] != evidence or receipt["spec_sha256"] != spec_hash:
                    raise ValueError("Completed handoff evidence changed")
                return receipt
            candidate = work / "candidate"
            candidate.mkdir(exist_ok=True)
            _adopt_plan(candidate, spec, prepared)
            _adopt_stats(candidate, spec, prepared)
            state["steps"]["candidate_verified"] = _verify_ui(candidate, spec, prepared)
            state["status"] = "CANDIDATE_VERIFIED"
            _write_json(record, state)
            prepared = preflight_handoff(spec, local_profile=local_profile, now=now)
            selected = read_accepted_joint_plan(root, spec["action_date"])
            if selected and selected[0]["plan_sha256"] != prepared["plan"]["plan_sha256"]:
                raise ValueError("This session already has a different accepted combined plan")
            _matching_stats(root, spec, prepared)
            state["steps"]["plan_adopted"] = str(_adopt_plan(root, spec, prepared))
            state["status"] = "PARTIAL_ADOPTION"
            _write_json(record, state)
            state["steps"]["stats_adopted"] = str(_adopt_stats(root, spec, prepared))
            _write_json(record, state)
            evidence = _verify_ui(root, spec, prepared)
            _local_binding(spec, local_profile)
            receipt = {"schema_version": VERSION, "completion_id": spec["completion_id"], "spec_sha256": spec_hash,
                "status": "HANDOFF_VERIFIED_LOCAL", "ui_ready": True, "joint_ready": True,
                "execution_authorized": False, "activation_changed": False, "orders_placed": 0,
                "local_actor": "atlas", "executor_owner": "atlas", "action_date": spec["action_date"],
                "review_session": spec["review_session"], "account_scope_sha256": spec["account_scope_sha256"],
                "scout_receipt_sha256": spec["scout_receipt"]["file_sha256"],
                "account_config_sha256": spec["account_config"]["file_sha256"],
                "owner_packages": prepared["package_hashes"], "stats_packages": prepared["stats_hashes"],
                "ui_evidence": evidence, "receipt_path": str(work / "receipt.json")}
            _write_json(work / "receipt.json", receipt)
            state["status"] = "HANDOFF_VERIFIED_LOCAL"
            state.pop("error", None)
            _write_json(record, state)
            return receipt
        except Exception as exc:
            partial = "plan_adopted" in state["steps"]
            if not partial:
                try:
                    adopted = read_accepted_joint_plan(root, spec["action_date"])
                    partial = (adopted is not None and adopted[1]["local_actor"] == "atlas"
                               and adopted[0]["plan_sha256"] == prepared["plan"]["plan_sha256"])
                except (OSError, ValueError, KeyError):
                    pass
            state["status"] = "PARTIAL_ADOPTION" if partial else "FAILED"
            state["error"] = f"{type(exc).__name__}: {exc}"
            _write_json(record, state)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--local-profile", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    spec_path = args.spec.resolve()
    spec = _object(_read(spec_path, spec_path.parent))
    if args.validate_only:
        prepared = preflight_handoff(spec, local_profile=args.local_profile)
        result = {"status": "INPUTS_VERIFIED", "plan_sha256": prepared["plan"]["plan_sha256"],
                  "ui_ready": False, "joint_ready": False, "orders_placed": 0}
    else:
        result = run_handoff(spec, local_profile=args.local_profile)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
