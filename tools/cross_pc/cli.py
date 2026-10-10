"""Explicit one-shot coordination commands. No internal polling loop."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.cross_pc import artifact_publish, core, drift, drive_signal, incoming, installation, notices, requests


def _communication_arguments(sub):
    for name in ("prepare", "plan", "begin-write", "bind-created", "reconcile", "inspect"):
        command = sub.add_parser("drive-" + name)
        command.add_argument("--binding", help="Private binding file; defaults to the corresponding profile binding")
        if name == "prepare":
            command.add_argument("--spec", required=True)
            command.add_argument("--reviewed", action="store_true")
        if name in {"begin-write", "bind-created", "reconcile", "inspect"}:
            command.add_argument("--metadata", required=name != "begin-write")
        if name in {"begin-write", "reconcile", "inspect"}:
            command.add_argument("--raw", required=name != "begin-write")
        if name == "inspect":
            command.add_argument("--known-digests")
            command.add_argument("--previous")
    for name in ("register", "track"):
        command = sub.add_parser("request-" + name)
        command.add_argument("--notice-id", "--id", dest="notice_id", required=True)
        command.add_argument("--digest", required=True)
        command.add_argument("--spec", required=True)
        if name == "track":
            command.add_argument("--reviewed", action="store_true")
    for name in ("plan", "status"):
        sub.add_parser("request-" + name)
    command = sub.add_parser("request-prepare-reply", aliases=["request-prepare"])
    command.add_argument("--id", required=True)
    command.add_argument("--spec", required=True)
    command.add_argument("--reviewed", action="store_true")
    for name in ("enqueue-reply", "sync-reply"):
        command = sub.add_parser("request-" + name, aliases=["request-" + name.removesuffix("-reply")])
        command.add_argument("--id", required=True)
    command = sub.add_parser("incoming-enqueue")
    command.add_argument("--spec", required=True)
    command.add_argument("--reviewed", action="store_true")
    sub.add_parser("incoming-process")


def _local_bytes(path, limit):
    """Bounded caller-selected local evidence; never dereference peer paths."""
    candidate = Path(path)
    if not candidate.is_absolute() or str(candidate).startswith(("\\\\", "//")):
        raise ValueError("communication evidence requires an absolute local file")
    candidate = installation._regular_file(candidate)
    if candidate.stat().st_size > limit:
        raise ValueError("communication evidence exceeds byte limit")
    with candidate.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("communication evidence exceeds byte limit")
    return raw


def _local_json(path, limit=128 * 1024):
    return core.parse(_local_bytes(path, limit).decode("utf-8-sig"))


def _drive_binding(profile, path, *, peer=False):
    binding = drive_signal.validate_binding(_local_json(path))
    if binding["repository"] != profile["repository"]:
        raise ValueError("Drive binding repository differs from local profile")
    if peer:
        if (binding["actor"] == profile["actor"] or binding["machine"] == profile["machine"]
                or binding["recipient"] != profile["machine"] or not binding.get("file_id")):
            raise ValueError("Drive inspection requires a pinned peer signal addressed to this PC")
    elif (binding["actor"], binding["machine"]) != (profile["actor"], profile["machine"]):
        raise ValueError("Drive writer must match the local profile actor and machine")
    return binding


def _drive_root(profile):
    base = Path(profile["state_path"]).parent
    root = Path(profile.get("drive_signal_root", base / "drive-signal"))
    if (not base.is_absolute() or not root.is_absolute()
            or str(base).startswith(("\\\\", "//")) or str(root).startswith(("\\\\", "//"))):
        raise ValueError("Drive signal state requires an absolute private local root")
    base, root = installation._safe_location(base), installation._safe_location(root)
    if root == base or not root.is_relative_to(base):
        raise ValueError("Drive signal state must remain inside the private receipt directory")
    return root


def _drive_command(args, profile):
    if core.notification_policy(profile) == "github_only":
        # Retired signal evidence is historical, not a current delivery gate.
        # Do not even open its bindings/state: they may contain an interrupted
        # write that must remain intact, without fabricating a Drive receipt.
        return {"status": "DISABLED_BY_POLICY", "action": args.action,
                "coordination_notification_policy": "github_only",
                "drive_operation_performed": False, "drive_delivery": "not_attempted",
                "historical_state_preserved": True}
    peer = args.action == "drive-inspect"
    binding_path = args.binding or profile.get("drive_peer_binding" if peer else "drive_own_binding")
    if not binding_path:
        raise ValueError("configure the private Drive binding or supply --binding")
    binding = _drive_binding(profile, binding_path, peer=peer)
    if args.action == "drive-inspect":
        metadata = _local_json(args.metadata)
        drive_signal.validate_metadata(metadata, binding)
        raw = _local_bytes(args.raw, drive_signal.MAX_BYTES)
        drive_signal.validate_metadata(metadata, binding, raw=raw)
        return drive_signal.inspect_signal(raw, binding,
            known_digests=_local_json(args.known_digests) if args.known_digests else None,
            previous=_local_json(args.previous) if args.previous else None)
    root = _drive_root(profile)
    if args.action == "drive-prepare":
        spec = _local_json(args.spec, drive_signal.MAX_BYTES)
        if not isinstance(spec, dict) or set(spec) != {"git_head", "notices"}:
            raise ValueError("Drive prepare spec must contain only git_head and notices")
        return drive_signal.prepare(root, binding, **spec, reviewed=args.reviewed)
    if args.action == "drive-plan":
        return drive_signal.plan(root, binding)
    if args.action == "drive-begin-write":
        if bool(args.metadata) != bool(args.raw):
            raise ValueError("Drive update requires metadata and original raw bytes together")
        metadata = _local_json(args.metadata) if args.metadata else None
        if metadata is not None:
            drive_signal.validate_metadata(metadata, binding)
        raw = _local_bytes(args.raw, drive_signal.MAX_BYTES) if args.raw else None
        return drive_signal.begin_write(root, binding, metadata=metadata, raw=raw)
    metadata = _local_json(args.metadata)
    drive_signal.validate_metadata(metadata, binding)
    if args.action == "drive-bind-created":
        return drive_signal.bind_created(root, binding, metadata)
    return drive_signal.reconcile(root, binding, metadata, _local_bytes(args.raw, drive_signal.MAX_BYTES))


def _request_command(args, profile):
    if args.action == "request-register":
        return requests.register(profile, args.notice_id, args.digest, _local_json(args.spec, 32 * 1024))
    if args.action == "request-track":
        return requests.track_outgoing(profile, args.notice_id, args.digest,
            _local_json(args.spec, 32 * 1024), reviewed=args.reviewed)
    if args.action == "request-prepare-reply":
        return requests.prepare_reply(profile, args.id, _local_json(args.spec), reviewed=args.reviewed)
    if args.action == "request-enqueue-reply":
        return requests.enqueue_reply(profile, args.id)
    if args.action == "request-sync-reply":
        return requests.sync_reply(profile, args.id)
    if args.action == "request-plan":
        return requests.plan(profile)
    return requests.read(profile)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("plan")
    sub.add_parser("observe")
    command = sub.add_parser("queue")
    command.add_argument("--source", required=True)
    command.add_argument("--spec", required=True)
    for name in ("prepare-candidate", "verify-candidate"):
        command = sub.add_parser(name)
        command.add_argument("--id", required=True)
        command.add_argument("--worktree", required=True)
        command.add_argument("--reviewed", action="store_true")
    command = sub.add_parser("publish-source")
    command.add_argument("--id", required=True)
    command = sub.add_parser("artifact-snapshot")
    command.add_argument("--source", required=True)
    command.add_argument("--spec", required=True)
    command = sub.add_parser("artifact-publish")
    command.add_argument("--id", required=True)
    command.add_argument("--worktree", required=True)
    command = sub.add_parser("notice")
    command.add_argument("--spec", required=True)
    command.add_argument("--reviewed", action="store_true")
    command = sub.add_parser("deliver")
    command.add_argument("--id", required=True)
    sub.add_parser("inbox-scan")
    command = sub.add_parser("inbox-read")
    command.add_argument("--id", required=True)
    command = sub.add_parser("inbox-mark")
    command.add_argument("--id", required=True)
    command.add_argument("--digest", required=True)
    command.add_argument("--disposition", required=True)
    command = sub.add_parser("drift")
    command.add_argument("--reference", required=True)
    command = sub.add_parser("run")
    for value in ("purpose", "outcome", "summary", "source-version"):
        command.add_argument("--" + value, required=True)
    command.add_argument("--blocker")
    command = sub.add_parser("install")
    command.add_argument("--repository", required=True)
    command.add_argument("--commit", required=True)
    command.add_argument("--destination", required=True)
    command = sub.add_parser("verify-installation")
    command.add_argument("--active", required=True)
    command = sub.add_parser("rollback")
    command.add_argument("--destination", required=True)
    _communication_arguments(sub)
    args = parser.parse_args(argv)
    args.action = {"request-prepare": "request-prepare-reply", "request-enqueue": "request-enqueue-reply",
                   "request-sync": "request-sync-reply"}.get(args.action, args.action)
    if args.action == "install":
        result = installation.install(args.repository, args.commit, args.destination)
    elif args.action == "verify-installation":
        active = core.load(args.active)
        result = installation.verify(active["release_root"], active["manifest_sha256"])
    elif args.action == "rollback":
        result = installation.rollback(args.destination)
    else:
        if not args.profile:
            parser.error("--profile is required for coordination commands")
        profile = core.validate_profile(core.load(args.profile))
        if args.action == "plan":
            result = core.plan(profile)
        elif args.action == "queue":
            record = core.queue(profile, args.source, core.load(args.spec))
            result = {"id": record["id"], "stage": "queued", "fingerprinted_files": len(record["fingerprints"])}
        elif args.action == "prepare-candidate":
            result = core.prepare_candidate(profile, args.id, args.worktree)
        elif args.action == "verify-candidate":
            result = core.verify_candidate(profile, args.id, args.worktree, args.reviewed)
        elif args.action == "publish-source":
            result = core.publish_source(profile, args.id)
        elif args.action == "artifact-snapshot":
            receipt = artifact_publish.snapshot(profile, args.source, core.load(args.spec))
            result = {"id": receipt["id"], "stage": receipt["stage"], "files": len(receipt["files"])}
        elif args.action == "artifact-publish":
            receipt = artifact_publish.publish(profile, args.id, args.worktree)
            result = {"id": receipt["id"], "stage": receipt["stage"],
                      "commit_sha": receipt.get("commit_sha"), "remote_sha": receipt.get("remote_sha")}
        elif args.action == "notice":
            result = notices.enqueue(profile, core.load(args.spec), reviewed=args.reviewed)
        elif args.action == "deliver":
            result = notices.deliver(profile, args.id)
        elif args.action == "inbox-scan":
            result = notices.inbox_scan(profile)
        elif args.action == "inbox-read":
            result = notices.inbox_read(profile, args.id)
        elif args.action == "inbox-mark":
            result = notices.inbox_mark(profile, args.id, args.digest, args.disposition)
        elif args.action == "observe":
            result = notices.observe(profile)
        elif args.action == "drift":
            result = drift.inspect(profile, args.reference)
        elif args.action == "run":
            result = core.record_run(profile, args.purpose, args.outcome, args.summary, args.source_version, args.blocker)
        elif args.action.startswith("drive-"):
            result = _drive_command(args, profile)
        elif args.action.startswith("request-"):
            result = _request_command(args, profile)
        elif args.action == "incoming-enqueue":
            result = incoming.enqueue(profile, _local_json(args.spec, incoming.MAX_STATE_BYTES), reviewed=args.reviewed)
        elif args.action == "incoming-process":
            # Local-file remotes are an internal fixture seam, never a CLI option
            # or a field that an incoming notice/profile can enable.
            result = incoming.process_one(profile)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
