"""Explicit one-shot coordination commands. No internal polling loop."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.cross_pc import artifact_publish, core, drift, installation, notices


def main():
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
    args = parser.parse_args()
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
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
