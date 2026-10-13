"""One nightly Atlas synthesis from the two completed research packages.

Scout publishes its contribution and can display Atlas's finished result. Its
availability, acceptance and trading processes never gate Atlas completion.
Legacy repair/source-transition records are historical, not prerequisites.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from filelock import FileLock, Timeout

from ml.artifacts import file_checksum, utc_timestamp


def _local_preparation(config, native, action, review):
    from tools import nightly_exchange as exchange
    path = Path(native["state_root"]) / "runs" / action / "state.json"
    if not path.exists():
        raise exchange.Pending("LOCAL_PREPARATION")
    state = exchange._object(exchange._read(path))
    output = state.get("steps", {}).get("local_handoff", {}).get("output", {})
    if not output.get("package") or not output.get("stats_package"):
        raise exchange.Pending("LOCAL_PREPARATION")
    files = {"plan.json": Path(output["package"]), "stats.json": Path(output["stats_package"])}
    # Only the actual selected data matters. Finished packages do not become
    # unavailable because source/helper code or a historical claim changed.
    exchange._owners(config, action, review, {config["actor"].lower(): {"inputs": {}, "files": files}})
    return {name: exchange._read(path) for name, path in files.items()}


def _completion(config, native, action, review, session):
    from tools import nightly_exchange as exchange
    from ml.joint_capital_adoption import read_accepted_joint_plan
    path = session / "direct-completion.json"
    if not path.exists():
        return None
    saved = exchange._object(exchange._read(path))
    receipt_path = Path(saved["receipt_path"])
    if file_checksum(receipt_path) != saved["receipt_sha256"]:
        raise ValueError("Saved synthesis receipt changed")
    receipt = exchange._object(exchange._read(receipt_path))
    selected = read_accepted_joint_plan(Path(native["datastore"]), action)
    if (selected is None or selected[0]["plan_sha256"] != receipt["ui_evidence"]["plan_sha256"]
            or receipt["local_actor"] != config["actor"].lower()
            or receipt["action_date"] != action or receipt["review_session"] != review
            or receipt["owner_packages"] != selected[1]["owner_packages"]):
        raise ValueError("Saved synthesis selection changed")
    return receipt


def _remember(session, receipt):
    from tools import nightly_exchange as exchange
    exchange._write(session / "direct-completion.json", exchange._bytes({
        "receipt_path": receipt["receipt_path"], "receipt_sha256": file_checksum(Path(receipt["receipt_path"]))}))


def _save_result(session, result):
    from tools import nightly_exchange as exchange
    status = session / "status.json"
    if status.exists():
        raw = exchange._read(status)
        if exchange._object(raw).get("mode") != "ATLAS_DIRECT_SYNTHESIS":
            exchange._write(session / "legacy-status" / (exchange._sha(raw) + ".json"), raw)
    exchange._write(session / "direct-status.json", exchange._bytes(result), replace=True)
    exchange._write(status, exchange._bytes(result), replace=True)
    return result


def _deliver(config, action, review, session, receipt):
    from tools import nightly_exchange as exchange
    # One-way delivery is useful for Scout display but is never a completion
    # condition on Atlas; the exact intent remains available for a later retry.
    try:
        intent = exchange._object(exchange._read(session / "direct-synthesis-selection.json"))
        files = {"joint-plan.json": Path(receipt["ui_evidence"]["plan_run"]) / "joint-plan.json",
                 "atlas-synthesis-receipt.json": Path(receipt["receipt_path"])}
        for actor, owner in intent["spec"]["owners"].items():
            files[actor + "-plan.json"] = Path(owner["plan_package"]["path"])
            files[actor + "-stats.json"] = Path(owner["stats_package"]["path"])
        exchange._publish(config, action, review, "combined", files, intent["inputs"])
        return "DELIVERED"
    except (OSError, ValueError, KeyError, TypeError):
        return "PENDING_DELIVERY"


def _synthesize(config, native, action, review, session, preparations, clock):
    from tools import nightly_exchange as exchange
    from ml.nightly_synthesis import run_synthesis, validate_local_profile
    selected = session / "direct-synthesis-selection.json"
    inputs = {actor + "_preparation": packet["digest"] for actor, packet in preparations.items()}
    if selected.exists():
        frozen = exchange._object(exchange._read(selected))
        if frozen["inputs"] != inputs:
            raise ValueError("Selected synthesis preparations changed")
        spec = frozen["spec"]
    else:
        from tools.nightly_account_snapshot import capture_snapshot
        snapshot = capture_snapshot(config, now=clock())
        captured_at = clock()
        snapshot_path = session / "direct-account-snapshot.json"
        exchange._write(snapshot_path, exchange._bytes(snapshot), replace=True)
        spec = {"schema_version": "nightly-gameplan-synthesis-v1",
            "completion_id": exchange._completion(action, "atlas", inputs),
            "datastore_root": native["datastore"], "state_root": str(session / "direct-synthesis"),
            "local_actor": "atlas", "executor_owner": "atlas", "action_date": action, "review_session": review,
            "account_scope_sha256": config["account_scope_sha256"], "as_of": captured_at.isoformat(),
            "accepted_at": captured_at.isoformat(), "snapshot": exchange._bind(snapshot_path),
            "owners": exchange._owners(config, action, review, preparations)}
        exchange._write(selected, exchange._bytes({"spec": spec, "inputs": inputs}))
    validate_local_profile(spec, Path(config["local_profile"]))
    receipt = run_synthesis(spec, now=clock())
    _remember(session, receipt)
    return receipt


def _adopt(config, native, action, review, session, preparations, now):
    from tools import nightly_exchange as exchange
    from ml import nightly_synthesis as synthesis
    from ml.joint_capital_plan import load_joint_plan
    from ml.gameplan_stats_handoff import read_stats_package, combined_stats_target, combine_stats_frames
    from ml.nightly_joint_readiness import publish_joint_readiness
    combined = exchange._receive(config, action, review, "atlas", "combined")
    expected = {actor + "_preparation": item["digest"] for actor, item in preparations.items()}
    if combined["inputs"] != expected:
        raise ValueError("Combined plan uses different research preparations")
    producer = exchange._object(exchange._read(combined["files"]["atlas-synthesis-receipt.json"]))
    if (producer.get("local_actor") != "atlas" or producer.get("executor_owner") != "atlas"
            or producer.get("action_date") != action or producer.get("review_session") != review):
        raise ValueError("Combined plan has a different producer or session")
    path = combined["files"]["joint-plan.json"]
    plan = load_joint_plan(path.parent, path, expected_sha256=producer["ui_evidence"]["plan_sha256"])
    owners = exchange._owners(config, action, review, preparations)
    for actor, selected in preparations.items():
        for kind in ("plan", "stats"):
            if exchange._read(combined["files"][actor + "-" + kind + ".json"]) != exchange._read(selected["files"][kind + ".json"]):
                raise ValueError("Combined packet substitutes a research preparation")
    hashes = {actor: owner["plan_package"]["package_sha256"] for actor, owner in owners.items()}
    if hashes != producer["owner_packages"]:
        raise ValueError("Combined plan substitutes an owner package")
    stats_paths = {actor.title(): Path(owners[actor]["stats_package"]["path"]) for actor in sorted(owners)}
    stats_hashes = {actor: file_checksum(path) for actor, path in stats_paths.items()}
    sources, frames = {}, []
    for actor, path in stats_paths.items():
        source, frame = read_stats_package(path, expected_sha256=stats_hashes[actor])
        sources[actor] = source
        frames.append(frame)
    combined_stats_target(tuple(sources.values()))
    prepared = {"plan": plan, "package_hashes": hashes, "universes": config["owners"],
        "stats_paths": stats_paths, "stats_hashes": stats_hashes,
        "stats_symbols": {actor.title(): symbols for actor, symbols in config["owners"].items()},
        "stats_sources": sources, "stats_frame": combine_stats_frames(frames)}
    spec = {"datastore_root": native["datastore"], "state_root": str(session / "direct-adoption"),
        "local_actor": "scout", "executor_owner": "atlas", "action_date": action, "review_session": review,
        "account_scope_sha256": config["account_scope_sha256"], "accepted_at": now.isoformat(),
        "completion_id": exchange._completion(action, "scout", {"combined": combined["digest"]})}
    work = Path(spec["state_root"]) / spec["completion_id"]
    root = Path(native["datastore"])
    synthesis._adopt_plan(root, spec, prepared)
    synthesis._adopt_stats(root, spec, prepared)
    evidence = synthesis._verify_ui(root, spec, prepared)
    receipt = {"schema_version": "nightly-gameplan-adoption-v1", "status": "JOINT_READY_LOCAL",
        "local_actor": "scout", "executor_owner": "atlas", "synthesis_owner": "atlas",
        "action_date": action, "review_session": review, "completion_id": spec["completion_id"],
        "account_scope_sha256": config["account_scope_sha256"], "joint_ready": True, "ui_ready": True,
        "peer_verified": False, "execution_authorized": False, "activation_changed": False, "orders_placed": 0,
        "owner_packages": hashes, "stats_packages": stats_hashes, "ui_evidence": evidence,
        "receipt_path": str(work / "receipt.json")}
    exchange._write(work / "receipt.json", exchange._bytes(receipt))
    publish_joint_readiness(root, spec, receipt)
    _remember(session, receipt)
    return receipt


def run_once(config, *, now=None):
    from tools import nightly_exchange as exchange
    if config.get("private_exchange_authorized") is not True:
        raise ValueError("Private exchange requires direct local authorization")
    now = utc_timestamp(now)
    started = exchange.monotonic()
    clock = lambda: now + pd.Timedelta(seconds=max(0, exchange.monotonic() - started))
    action, review = exchange._context(now)
    native = exchange.workflow.load_config(Path(config["workflow_config"]))
    actor = config["actor"].lower()
    session = Path(config["state_root"]) / "sessions" / action
    session.mkdir(parents=True, exist_ok=True)
    common = {"action_date": action, "review_session": review, "actor": config["actor"],
        "orders_placed": 0, "activation_changed": False, "execution_authorized": False,
        "joint_ready": False, "ui_ready": False, "peer_verified": False,
        "mode": "ATLAS_DIRECT_SYNTHESIS"}
    try:
        with FileLock(str(session / "direct-synthesis.lock"), timeout=0):
            receipt = _completion(config, native, action, review, session)
            if receipt is None:
                own = exchange._publish(config, action, review, "preparation", _local_preparation(config, native, action, review))
                other = "scout" if actor == "atlas" else "atlas"
                try:
                    peer = exchange._receive(config, action, review, other, "preparation")
                    preparations = {actor: own, other: peer}
                    receipt = (_synthesize(config, native, action, review, session, preparations, clock)
                               if actor == "atlas" else _adopt(config, native, action, review, session, preparations, clock()))
                except exchange.Pending as missing:
                    if actor == "atlas":
                        raise
                    return _save_result(session, {**common, "status": "PREPARATION_PUBLISHED", "reason": str(missing)})
            result = {**common, "status": "COMPLETE", "local_status": receipt["status"],
                "joint_ready": True, "ui_ready": True, "receipt_path": receipt["receipt_path"]}
            if actor == "atlas":
                result["peer_output_delivery"] = _deliver(config, action, review, session, receipt)
            return _save_result(session, result)
    except Timeout:
        return {**common, "status": "PENDING", "reason": "SYNTHESIS_BUSY"}
    except exchange.Pending as missing:
        return _save_result(session, {**common, "status": "PENDING", "reason": str(missing)})
