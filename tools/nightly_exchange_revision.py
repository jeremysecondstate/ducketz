"""Append an explicitly selected research revision to a completed nightly handoff.

This separate path never rewrites the completed exchange, accepted execution
selection, account evidence, or workflow state. Both source packages remain
immutable. Delivery is a verified file packet in the existing private channel;
Atlas adoption and live execution are not claimed or triggered.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from datetime import date
from pathlib import Path
import re
import stat

from filelock import FileLock

from ml.artifacts import file_checksum, utc_timestamp
from ml.joint_capital_adoption import read_accepted_joint_plan
from ml.joint_capital_plan import content_sha256
from ml.nightly_synthesis import preflight_synthesis, validate_local_profile
from tools import nightly_exchange as exchange


VERSION = "nightly-exchange-research-revision-v1"
PREDECESSOR_FIELDS = {"joint_packet_sha256", "joint_plan_sha256",
                      "atlas_package_sha256", "scout_package_sha256"}
SPEC_FIELDS = {"schema_version", "revision_id", "action_date", "review_session",
               "as_of", "predecessor", "scout"}
FILES = {"joint-plan.json", "revision-receipt.json", "atlas-plan.json",
         "atlas-stats.json", "scout-plan.json", "scout-stats.json"}


def _within(root, path):
    """Keep newly written revision namespaces inside their selected local roots."""
    root, path = Path(root).resolve(), Path(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Revision path escapes its selected root")
    relative = path.absolute().relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Revision namespace contains a symlink or reparse point")
    return path


def _specification(spec, now):
    if (not isinstance(spec, dict) or set(spec) not in
            (SPEC_FIELDS, SPEC_FIELDS | {"saved_account_basis"})
            or spec.get("schema_version") != VERSION):
        raise ValueError("Unsupported explicit research revision specification")
    if not isinstance(spec["revision_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}", spec["revision_id"]):
        raise ValueError("Revision requires a stable safe 8-64 character identity")
    for key in ("action_date", "review_session"):
        if date.fromisoformat(spec[key]).isoformat() != spec[key]:
            raise ValueError("Revision date is not canonical")
    stamp = utc_timestamp(spec["as_of"])
    if stamp > utc_timestamp(now):
        raise ValueError("Revision cannot be future dated")
    if not isinstance(spec["predecessor"], dict) or set(spec["predecessor"]) != PREDECESSOR_FIELDS:
        raise ValueError("Exact predecessor packet, plan and both packages are required")
    for digest in spec["predecessor"].values():
        exchange._digest(digest)
    if not isinstance(spec["scout"], dict) or set(spec["scout"]) != {"plan_package", "stats_package"}:
        raise ValueError("Revision requires exactly Scout plan and Stats selections")


def _predecessor(config, spec):
    """Verify completed local evidence and current shared predecessor selectors."""
    action, review = spec["action_date"], spec["review_session"]
    session = Path(config["state_root"]) / "sessions" / action
    status_path = session / "status.json"
    status = exchange._object(exchange._read(status_path))
    if (status.get("status") != "COMPLETE" or status.get("actor") != "Scout"
            or status.get("action_date") != action or status.get("review_session") != review
            or status.get("delivery_mode") != "TWO_PLAN_GAMEPLAN" or status.get("delivered") is not True):
        raise ValueError("Revision requires the exact completed Scout handoff")
    frozen = {str(status_path): file_checksum(status_path)}
    selected = {}
    for actor, kind in (("atlas", "preparation"), ("scout", "preparation"), ("scout", "joint")):
        local = session / "selections" / f"{actor}-{kind}.json"
        shared = exchange._folder(config, action, actor, kind) / "selection.json"
        if exchange._read(local, 2048) != exchange._read(shared, 2048):
            raise ValueError("Stale predecessor: local and shared selections differ")
        item = exchange._receive(config, action, review, actor, kind, local_only=True)
        selected[(actor, kind)] = item
        frozen.update({str(p): file_checksum(p) for p in (local, shared, *item["files"].values())})
    joint = selected[("scout", "joint")]
    original = exchange._object(exchange._read(joint["files"]["joint-plan.json"]))
    packages = {actor: exchange._object(exchange._read(selected[(actor, "preparation")]["files"]["plan.json"]))
                for actor in ("atlas", "scout")}
    actual = {"joint_packet_sha256": joint["digest"], "joint_plan_sha256": original["plan_sha256"],
              **{f"{a}_package_sha256": packages[a]["package_sha256"] for a in packages}}
    if actual != spec["predecessor"] or status.get("joint_packet_sha256") != joint["digest"]:
        raise ValueError("Stale or conflicting predecessor hashes")
    if joint["inputs"] != {f"{a}_preparation": selected[(a, "preparation")]["digest"] for a in packages}:
        # Historical snapshots may be an additional explicitly frozen input.
        expected = {f"{a}_preparation": selected[(a, "preparation")]["digest"] for a in packages}
        if set(joint["inputs"]) != {*expected, "snapshot"} or any(joint["inputs"].get(k) != v for k, v in expected.items()):
            raise ValueError("Predecessor joint packet binds different preparations")
    native = exchange.workflow.load_config(Path(config["workflow_config"]))
    root = Path(native["datastore"])
    accepted = read_accepted_joint_plan(root, action)
    if (accepted is None or accepted[0]["plan_sha256"] != actual["joint_plan_sha256"]
            or accepted[1]["owner_packages"] != {a: packages[a]["package_sha256"] for a in packages}):
        raise ValueError("Accepted execution predecessor differs from the selected handoff")
    pointer = root / "ml/joint-gameplan-by-date" / action / "run.json"
    frozen.update({str(p): file_checksum(p) for p in (pointer, accepted[2] / "accepted-plan.json", accepted[2] / "joint-plan.json")})
    if original != accepted[0]:
        raise ValueError("Predecessor packet and accepted plan bytes differ")
    return selected, original, frozen


def _prepare(config, spec, now):
    _specification(spec, now)
    if config.get("actor") != "Scout" or config.get("private_exchange_authorized") is not True:
        raise ValueError("Only the authorized Scout producer may deliver this revision")
    selected, predecessor, frozen = _predecessor(config, spec)
    atlas = selected[("atlas", "preparation")]["files"]
    atlas_package = exchange._object(exchange._read(atlas["plan.json"]))
    work = _within(config["state_root"], Path(config["state_root"]) / "sessions" / spec["action_date"] / "research-revisions" / spec["revision_id"])
    synthesis = {"schema_version": "nightly-gameplan-synthesis-v1", "completion_id": spec["revision_id"],
        "datastore_root": str(work / "candidate"), "state_root": str(work / "synthesis"),
        "local_actor": "scout", "executor_owner": "atlas", "action_date": spec["action_date"],
        "review_session": spec["review_session"], "account_scope_sha256": config["account_scope_sha256"],
        "as_of": spec["as_of"], "accepted_at": spec["as_of"], "owners": {
            "atlas": {"symbols": config["owners"]["atlas"], "plan_package": {
                "root": str(atlas["plan.json"].parent), "path": str(atlas["plan.json"]),
                "file_sha256": file_checksum(atlas["plan.json"]), "package_sha256": atlas_package["package_sha256"]},
                "stats_package": {"path": str(atlas["stats.json"]), "file_sha256": file_checksum(atlas["stats.json"])}},
            "scout": {"symbols": config["owners"]["scout"], **spec["scout"]}}}
    if "saved_account_basis" in spec:
        synthesis["saved_account_basis"] = spec["saved_account_basis"]
    validate_local_profile(synthesis, Path(config["local_profile"]))
    prepared = preflight_synthesis(synthesis, now=now)
    plan = prepared["plan"]
    if prepared["package_hashes"]["scout"] == spec["predecessor"]["scout_package_sha256"]:
        raise ValueError("Revision must select a newly prepared Scout package")
    if "saved_account_basis" in spec and plan["account_snapshot_sha256"] != predecessor["account_snapshot_sha256"]:
        raise ValueError("Saved account basis differs from the exact predecessor observation")
    if "saved_account_basis" not in spec and plan.get("account_snapshot_sha256") is not None:
        if plan["account_snapshot_sha256"] != predecessor["account_snapshot_sha256"]:
            raise ValueError("Carried account basis differs from the predecessor observation")
    files = {"joint-plan.json": exchange._bytes(plan)}
    for actor, owner in synthesis["owners"].items():
        for kind in ("plan", "stats"):
            path = Path(owner[f"{kind}_package"]["path"])
            files[f"{actor}-{kind}.json"] = exchange._read(path)
            frozen[str(path)] = file_checksum(path)
    receipt = {"schema_version": VERSION, "revision_id": spec["revision_id"],
        "action_date": spec["action_date"], "review_session": spec["review_session"], "as_of": spec["as_of"],
        "spec_sha256": content_sha256(spec), "predecessor": spec["predecessor"],
        "plan_sha256": plan["plan_sha256"], "owner_packages": prepared["package_hashes"],
        "stats_packages": prepared["stats_hashes"], "status": "PREPARED_RESEARCH_REVISION",
        "cash_basis": plan["cash_basis"], "account_snapshot_sha256": plan["account_snapshot_sha256"],
        "predecessor_account_snapshot_sha256": predecessor["account_snapshot_sha256"],
        "predecessor_account_observed_at": predecessor["account_snapshot_observed_at"],
        "accepted_execution_selection_changed": False, "main_joint_ui_selection_changed": False,
        "orders_placed": 0, "activation_changed": False, "execution_authorized": False,
        "adoption_required": True, "account_capture_performed": False}
    files["revision-receipt.json"] = exchange._bytes(receipt)
    return work, files, frozen, receipt


def deliver_revision(config_path: Path, spec: dict, *, now=None) -> dict:
    """Validate, append and read back a revised research packet; never execute it."""
    config = exchange.load_config(Path(config_path))
    observed = utc_timestamp(now)
    _specification(spec, observed)
    local = Path(config["state_root"])
    shared = _within(config["exchange_root"], exchange._folder(config, spec["action_date"], "scout", "revisions"))
    with ExitStack() as locks:
        for directory in (local, shared):
            directory.mkdir(parents=True, exist_ok=True)
            locks.enter_context(FileLock(str(directory / "research-revision.lock"), timeout=0))
        work, files, frozen, receipt = _prepare(config, spec, observed)
        packet = {"schema_version": VERSION, "sender": "scout", "recipient": "atlas",
            "action_date": spec["action_date"], "review_session": spec["review_session"],
            "revision_id": spec["revision_id"], "account_scope_sha256": config["account_scope_sha256"],
            "owners": config["owners"], "predecessor": spec["predecessor"],
            "spec_sha256": content_sha256(spec), "files": {name: {
                "sha256": exchange._sha(data), "base64": base64.b64encode(data).decode("ascii")}
                for name, data in files.items()}}
        if set(files) != FILES or any(len(v) > exchange.MAX_FILE_BYTES for v in files.values()):
            raise ValueError("Revision files exceed the exact export allowlist or limits")
        packet["content_sha256"] = content_sha256(packet)
        data = exchange._bytes(packet)
        if len(data) > exchange.MAX_PACKET_BYTES:
            raise ValueError("Revision packet exceeds its bounded byte limit")
        destination = _within(config["exchange_root"], shared / spec["revision_id"] / "packet.json")
        claim = {"schema_version": VERSION, "revision_id": spec["revision_id"],
            "spec_sha256": content_sha256(spec), "content_sha256": packet["content_sha256"]}
        claim_path = _within(config["exchange_root"], shared / "predecessor-claims" / (spec["predecessor"]["joint_packet_sha256"] + ".json"))
        # A second revision from the same original predecessor is a conflict,
        # not an implicit replacement of a previously delivered revision.
        if claim_path.exists() and exchange._read(claim_path) != exchange._bytes(claim):
            raise ValueError("Conflicting revision already claims this predecessor")
        for path, digest in frozen.items():
            if file_checksum(Path(path)) != digest:
                raise ValueError("Frozen predecessor or revision source changed during preparation")
        exchange._write(work / "specification.json", exchange._bytes(spec))
        for name, value in files.items():
            exchange._write(work / name, value)
        exchange._write(work / "packet.json", data)
        exchange._write(claim_path, exchange._bytes(claim))
        exchange._write(destination, data)
        if exchange._read(destination, exchange.MAX_PACKET_BYTES) != data:
            raise ValueError("Delivered revision readback differs")
        for path, digest in frozen.items():
            if file_checksum(Path(path)) != digest:
                raise ValueError("Predecessor or source changed before delivery was completed")
        result = {**receipt, "status": "DELIVERED_RESEARCH_REVISION", "delivered": True,
            "delivery_path": str(destination), "packet_sha256": exchange._sha(data),
            "content_sha256": packet["content_sha256"], "bytes": len(data),
            "preserved_input_hashes": frozen}
        exchange._write(work / "delivery.json", exchange._bytes(result))
        exchange._write(destination.parent / "delivery.json", exchange._bytes({
            k: result[k] for k in ("schema_version", "revision_id", "action_date", "status", "delivered",
                                   "packet_sha256", "content_sha256", "bytes", "predecessor",
                                   "accepted_execution_selection_changed", "execution_authorized")}))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    result = deliver_revision(args.config, exchange._object(exchange._read(args.spec)))
    print(exchange._bytes({k: result[k] for k in ("status", "delivery_path", "packet_sha256",
        "content_sha256", "accepted_execution_selection_changed", "execution_authorized")}).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
