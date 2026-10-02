"""Reviewed, fast-forward-only main integration of a pushed source record.

This module never uses the running application checkout as a Git workspace.
Preparation, verification, and publication are separate so the complete
integration diff can be reviewed before any main push. Only the local caller's
sealed cross-pc-v2 completion record is executable authority; remote branches
and peer notices are evidence to validate, not instructions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

if __package__ in {None, ""}:
    # Installed task prompts invoke this exact immutable file from arbitrary cwd.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.cross_pc import core
else:
    from . import core


SHA = re.compile(r"[0-9a-f]{40}\Z")
RECORD_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")


def _worktree(profile, value):
    root = Path(value).resolve()
    if root == Path(profile["checkout"]).resolve() or not (root / ".git").is_file():
        raise ValueError("main integration requires an isolated managed worktree")
    if Path(core.git_text(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("candidate worktree identity mismatch")
    origin = core.git_text(root, "remote", "get-url", "origin")
    if origin != profile.get("remote"):
        raise ValueError("candidate origin differs from the local profile")
    allowed = {"https://github.com/jeremysecondstate/ducketz.git",
               "git@github.com:jeremysecondstate/ducketz.git"}
    if origin not in allowed and profile.get("test_remote") is not True:
        raise ValueError("unexpected source repository")
    return root


def _source(profile, record_id, root):
    if not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id):
        raise ValueError("invalid completion record ID")
    core.validate_profile(profile)
    receipts = core.state(profile)
    entry = receipts["records"][record_id]
    record = core.validate_record(profile, record_id, receipts, current=False)
    if record.get("scope") != "shared":
        raise ValueError("main integration requires a sealed shared scope; review symbol-specific or unclassified work separately")
    summary = record.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 240 or "\n" in summary or "\r" in summary:
        raise ValueError("invalid sealed commit summary")
    mixed = {name.casefold() for name in profile.get("local_config_fields", {})}
    if mixed & {item["path"].casefold() for item in record["files"]}:
        raise ValueError("mixed configuration needs field-level peer overlay review before main integration")
    branch = profile["branch_prefix"] + record_id
    source_sha = entry.get("commit_sha")
    if (entry.get("source_stage") != "pushed" or entry.get("branch") != branch
            or entry.get("remote_sha") != source_sha or not isinstance(source_sha, str)
            or not SHA.fullmatch(source_sha)):
        raise ValueError("source record lacks a verified own-branch push")
    core.verify_test_evidence(entry.get("isolated_tests"), record["fingerprints"])
    if entry.get("record_sha256") != core.digest((Path(profile["ready_root"]) / (record_id + ".json")).read_bytes()):
        raise ValueError("sealed source receipt mismatch")
    return receipts, entry, record, branch, source_sha


def _remote(root, branch):
    output = core.git_text(root, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
    lines = output.splitlines()
    if len(lines) != 1:
        raise ValueError("remote branch missing or ambiguous: " + branch)
    fields = lines[0].split("\t")
    if len(fields) != 2 or not SHA.fullmatch(fields[0]) or fields[1] != "refs/heads/" + branch:
        raise ValueError("remote branch identity mismatch")
    return fields[0]


def _fetch(root, record_id, branch, expected, label):
    ref = "refs/cross-pc/main-integration/" + record_id + "/" + label
    core.git(root, "fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "origin", "refs/heads/" + branch + ":" + ref)
    if core.git_text(root, "rev-parse", "--verify", ref) != expected:
        raise ValueError("fetched " + label + " differs from remote SHA")


def _paths(root, old, new):
    raw = core.git(root, "diff", "--no-renames", "--name-only", "-z", old, new).stdout
    return {name.decode("utf-8") for name in raw.split(b"\0") if name}


def _changed(root):
    tracked = set(core.git_text(root, "diff", "--name-only", "HEAD").splitlines())
    untracked = set(core.git_text(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return tracked | untracked


def _candidate_bytes(root, record):
    owned = {item["path"] for item in record["files"]}
    if _changed(root) != owned or core.git_text(root, "diff", "--cached", "--name-only"):
        raise ValueError("integration candidate has unrelated or missing changes")
    if core.fingerprints(root, record["fingerprints"]) != record["fingerprints"]:
        raise ValueError("integration candidate or dependencies differ from reviewed bytes")


def _public_line(value):
    if (not isinstance(value, str) or not value or len(value) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or re.search(r"[A-Za-z]:[/\\]", value)):
        raise ValueError("private or malformed text in public integration metadata")
    return value


def _base_checks(root, record, source_sha, main_sha):
    core.verify_commit(root, record, source_sha)
    if core.git(root, "merge-base", "--is-ancestor", record["base_commit"], main_sha, check=False).returncode:
        raise ValueError("origin/main does not descend from the reviewed source base")
    protected = {path.casefold() for path in record["fingerprints"]}
    changed_main = _paths(root, record["base_commit"], main_sha)
    if protected & {path.casefold() for path in changed_main}:
        raise ValueError("main changed an owned path or reviewed dependency")


def _verify_commit(root, record, commit, parent):
    if core.git_text(root, "rev-parse", commit + "^") != parent:
        raise ValueError("integration parent differs from reviewed main")
    changed = _paths(root, parent, commit)
    if changed != {item["path"] for item in record["files"]}:
        raise ValueError("integration commit includes unrelated or missing paths")
    for item in record["files"]:
        blob = core.git(root, "show", commit + ":" + item["path"], check=False)
        if item["operation"] == "delete":
            if blob.returncode == 0:
                raise ValueError("integration deletion missing")
        elif blob.returncode or core.digest(blob.stdout) != record["fingerprints"][item["path"]]:
            raise ValueError("integration bytes differ from sealed source")


def _receipt_path(profile, record_id, main_sha):
    if not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id):
        raise ValueError("invalid completion record ID")
    if not SHA.fullmatch(main_sha):
        raise ValueError("invalid main SHA")
    return Path(profile["evidence_root"]) / "main-integration" / record_id / (main_sha + ".json")


def _load_receipt(profile, record_id, main_sha):
    path = _receipt_path(profile, record_id, main_sha)
    receipt = core.load(path)
    if (receipt.get("contract_version") != "cross-pc-v2" or receipt.get("record_id") != record_id
            or receipt.get("main_before") != main_sha or receipt.get("actor") != profile["actor"]):
        raise ValueError("main integration receipt identity mismatch")
    return path, receipt


def _existing_candidate(profile, record_id, root, entry, record, source_sha):
    head = core.git_text(root, "rev-parse", "HEAD")
    pending_path = _receipt_path(profile, record_id, head)
    if pending_path.exists():
        _, pending = _load_receipt(profile, record_id, head)
        if pending.get("stage") == "committing":
            _validate_pending(pending, root, entry, record, source_sha)
            _pending_bytes(root, record)
            return pending
    parent = core.git(root, "rev-parse", head + "^", check=False)
    if parent.returncode:
        return None
    main_before = parent.stdout.decode("ascii").strip()
    path = _receipt_path(profile, record_id, main_before)
    if not path.exists():
        return None
    _, receipt = _load_receipt(profile, record_id, main_before)
    if (receipt.get("record_sha256") != entry["record_sha256"] or receipt.get("source_sha") != source_sha
            or receipt.get("candidate_sha") not in {None, head} or receipt.get("worktree") != str(root)):
        raise ValueError("existing integration candidate receipt mismatch")
    if receipt.get("stage") == "committing":
        _validate_pending(receipt, root, entry, record, source_sha)
        return _finish_commit(profile, record_id, root, record, receipt)
    if receipt.get("candidate_sha") != head or receipt.get("stage") not in {"prepared", "push_pending", "main_integrated"}:
        raise ValueError("existing integration candidate stage mismatch")
    core.verify_test_evidence(receipt.get("integration_tests"), record["fingerprints"])
    _verify_commit(root, record, head, main_before)
    if (core.git_text(root, "status", "--porcelain", "--untracked-files=all")
            or core.fingerprints(root, record["fingerprints"]) != record["fingerprints"]):
        raise ValueError("existing integration candidate is dirty")
    return receipt


def _validate_pending(receipt, root, entry, record, source_sha):
    if (receipt.get("record_sha256") != entry["record_sha256"] or receipt.get("source_sha") != source_sha
            or receipt.get("worktree") != str(root) or receipt.get("candidate_sha") is not None
            or not isinstance(receipt.get("commit_message"), str)
            or core.digest(receipt["commit_message"].encode("utf-8")) != receipt.get("commit_message_sha256")):
        raise ValueError("pending integration commit receipt mismatch")
    core.verify_test_evidence(receipt.get("integration_tests"), record["fingerprints"])


def _pending_bytes(root, record):
    owned = {item["path"] for item in record["files"]}
    staged = set(core.git_text(root, "diff", "--cached", "--name-only").splitlines())
    if _changed(root) != owned or staged - owned:
        raise ValueError("pending integration commit contains unrelated or missing changes")
    if core.fingerprints(root, record["fingerprints"]) != record["fingerprints"]:
        raise ValueError("pending integration bytes or dependencies changed after tests")
    for name in staged:
        blob = core.git(root, "show", ":" + name, check=False)
        expected = record["fingerprints"][name]
        if ((expected is None and blob.returncode == 0)
                or (expected is not None and (blob.returncode or core.digest(blob.stdout) != expected))):
            raise ValueError("pending integration index changed after review")


def _scan_publication(profile, record, message):
    values = core.source_private_values(profile)
    for item in record["files"]:
        if item["operation"] != "delete":
            raw = core.blob_path(profile, record["fingerprints"][item["path"]]).read_bytes()
            core.reject_source_private_values(raw, values)
    core.reject_source_private_values(message.encode("utf-8"), values)


def _finish_commit(profile, record_id, root, record, receipt):
    """Resume one frozen commit, including a crash after Git created it."""
    main_sha = receipt["main_before"]
    head = core.git_text(root, "rev-parse", "HEAD")
    if head == main_sha:
        _pending_bytes(root, record)
        _scan_publication(profile, record, receipt["commit_message"])
        for item in record["files"]:
            if item["operation"] == "delete":
                core.git(root, "update-index", "--force-remove", "--", item["path"])
            else:
                raw = core.blob_path(profile, record["fingerprints"][item["path"]]).read_bytes()
                oid = core.git(root, "hash-object", "-w", "--stdin", "--no-filters", data=raw).stdout.decode().strip()
                prior = core.git_text(root, "ls-tree", receipt["source_sha"], "--", item["path"])
                mode = prior.split()[0] if prior else "100644"
                core.git(root, "update-index", "--add", "--cacheinfo", mode + "," + oid + "," + item["path"])
        _pending_bytes(root, record)
        core.git(root, "-c", "user.name=" + profile["actor"], "-c", "user.email=" + profile["actor"].lower()
                 + "@cross-pc.invalid", "commit", "-F", "-", data=receipt["commit_message"].encode("utf-8"))
        head = core.git_text(root, "rev-parse", "HEAD")
    _verify_commit(root, record, head, main_sha)
    if core.git_text(root, "log", "-1", "--format=%B") != receipt["commit_message"].strip():
        raise ValueError("unknown integration commit after interruption")
    if (core.git_text(root, "status", "--porcelain", "--untracked-files=all")
            or core.fingerprints(root, record["fingerprints"]) != record["fingerprints"]):
        raise ValueError("candidate changed after integration commit")
    receipt.update(candidate_sha=head, stage="prepared", verified_at_utc=core.now())
    path = _receipt_path(profile, record_id, main_sha)
    core.atomic(path, receipt)
    receipts = core.state(profile)
    receipts["records"][record_id].update(integration_stage="validated", integration_receipt=str(path), integration_sha=head)
    core.atomic(profile["state_path"], receipts)
    return receipt


def prepare(profile, record_id, worktree):
    """Materialize exact reviewed bytes on current main; return a diff for review."""
    root = _worktree(profile, worktree)
    with core.exclusive(profile["git_lock"]):
        _, entry, record, branch, source_sha = _source(profile, record_id, root)
        remote_source = _remote(root, branch)
        if remote_source != source_sha:
            raise ValueError("remote source branch differs from pushed receipt")
        existing = _existing_candidate(profile, record_id, root, entry, record, source_sha)
        if existing:
            return {"record_id": record_id, "source_sha": source_sha,
                    "main_before": existing["main_before"], "candidate_sha": existing["candidate_sha"],
                    "paths": sorted(item["path"] for item in record["files"]),
                    "review_required": False, "stage": existing["stage"]}
        main_sha = _remote(root, "main")
        _fetch(root, record_id, branch, source_sha, "source")
        _fetch(root, record_id, "main", main_sha, "main")
        _base_checks(root, record, source_sha, main_sha)
        if core.git_text(root, "status", "--porcelain", "--untracked-files=all"):
            if core.git_text(root, "rev-parse", "HEAD") != main_sha:
                raise ValueError("candidate worktree contains preexisting changes")
            _candidate_bytes(root, record)
        else:
            core.git(root, "switch", "--detach", main_sha)
            for item in record["files"]:
                target = core.safe_path(root, item["path"])
                if item["operation"] == "delete":
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(core.blob_path(profile, record["fingerprints"][item["path"]]).read_bytes())
            _candidate_bytes(root, record)
        return {"record_id": record_id, "source_sha": source_sha, "main_before": main_sha,
                "paths": sorted(item["path"] for item in record["files"]),
                "dependencies": sorted(set(record["fingerprints"]) - {item["path"] for item in record["files"]}),
                "review_required": True, "worktree": str(root)}


def verify(profile, record_id, worktree, *, reviewed=False):
    """Run fresh candidate checks, commit exact bytes, and seal a local receipt."""
    if reviewed is not True:
        raise ValueError("explicit whole-diff integration review required")
    root = _worktree(profile, worktree)
    with core.exclusive(profile["git_lock"]):
        receipts, entry, record, branch, source_sha = _source(profile, record_id, root)
        if _remote(root, branch) != source_sha:
            raise ValueError("remote source branch changed")
        existing = _existing_candidate(profile, record_id, root, entry, record, source_sha)
        if existing:
            if existing["stage"] == "committing":
                return _finish_commit(profile, record_id, root, record, existing)
            return existing
        main_sha = core.git_text(root, "rev-parse", "HEAD")
        if not SHA.fullmatch(main_sha) or _remote(root, "main") != main_sha:
            raise ValueError("main advanced; prepare a new integration candidate")
        _fetch(root, record_id, branch, source_sha, "source")
        _base_checks(root, record, source_sha, main_sha)
        _candidate_bytes(root, record)
        details = record.get("change_details")
        if not isinstance(details, list) or not details:
            details = [record["summary"]]
        if len(details) > 12:
            raise ValueError("invalid sealed change details")
        details = [_public_line(detail) for detail in details]
        limitations = record.get("limitations", [])
        if not isinstance(limitations, list) or len(limitations) > 12:
            raise ValueError("invalid sealed limitations")
        limitations = [_public_line(item) for item in limitations]
        runtime = _public_line(record["runtime_implications"])
        producer = _public_line(record["producer"])
        tests = core.run_checks(root, [test["argv"] for test in record["tests"]], record["fingerprints"],
                                Path(profile["evidence_root"]) / "main-integration" / record_id / main_sha,
                                original_root=record["source_root"])
        _candidate_bytes(root, record)
        body = (profile["actor"] + ": Integrate " + record["summary"] + "\n\n"
                + "Producer: " + producer + " (" + profile["machine"] + ")\nScope: shared\n"
                + "Changes:\n" + "\n".join("- " + detail for detail in details) + "\n"
                + "Files:\n" + "\n".join("- " + item["operation"] + " " + item["path"] for item in record["files"]) + "\n"
                + "Fresh offline checks:\n" + "\n".join("- PASS " + test["started_at_utc"] + " to "
                + test["completed_at_utc"] + "; output SHA-256 " + test["output_sha256"] for test in tests) + "\n"
                + ("Limitations:\n" + "\n".join("- " + item for item in limitations) + "\n" if limitations else "")
                + "Runtime: " + runtime + "\n"
                + "Source-Commit: " + source_sha + "\nCompletion-Record: " + record_id + "\n"
                + "Main-Base: " + main_sha + "\n")
        _scan_publication(profile, record, body)
        path = _receipt_path(profile, record_id, main_sha)
        receipt = {"contract_version": "cross-pc-v2", "actor": profile["actor"], "record_id": record_id,
                   "record_sha256": entry["record_sha256"], "source_branch": branch, "source_sha": source_sha,
                   "main_before": main_sha, "candidate_sha": None, "integration_tests": tests,
                   "stage": "committing", "reviewed_at_utc": core.now(), "worktree": str(root),
                   "commit_message": body, "commit_message_sha256": core.digest(body.encode("utf-8"))}
        if path.exists():
            raise ValueError("an integration transaction already owns this main base; preserve its receipt")
        # Persist passing evidence and the exact message before changing the index.
        core.atomic(path, receipt)
        entry.update(integration_stage="committing", integration_receipt=str(path))
        core.atomic(profile["state_path"], receipts)
        return _finish_commit(profile, record_id, root, record, receipt)


def publish(profile, record_id, worktree, *, approved=False):
    """Push only a verified descendant of unchanged main; read back remote SHA."""
    if approved is not True:
        raise ValueError("explicit main integration approval required")
    root = _worktree(profile, worktree)
    with core.exclusive(profile["git_lock"]):
        receipts, entry, record, branch, source_sha = _source(profile, record_id, root)
        candidate_sha = core.git_text(root, "rev-parse", "HEAD")
        main_before = core.git_text(root, "rev-parse", candidate_sha + "^")
        path, receipt = _load_receipt(profile, record_id, main_before)
        if (receipt.get("record_sha256") != entry["record_sha256"] or receipt.get("source_branch") != branch
                or receipt.get("source_sha") != source_sha or receipt.get("candidate_sha") != candidate_sha
                or receipt.get("worktree") != str(root)
                or receipt.get("stage") not in {"prepared", "push_pending", "main_integrated"}):
            raise ValueError("integration receipt differs from sealed source or worktree")
        core.verify_test_evidence(receipt.get("integration_tests"), record["fingerprints"])
        _verify_commit(root, record, candidate_sha, main_before)
        if (core.git_text(root, "status", "--porcelain", "--untracked-files=all")
                or core.fingerprints(root, record["fingerprints"]) != record["fingerprints"]):
            raise ValueError("integration worktree changed after tests")
        if _remote(root, branch) != source_sha:
            raise ValueError("remote source branch changed after verification")
        current = _remote(root, "main")
        if current != candidate_sha:
            if current != main_before:
                _fetch(root, record_id, "main", current, "main")
                if core.git(root, "merge-base", "--is-ancestor", candidate_sha, current, check=False).returncode:
                    raise ValueError("main advanced outside the verified candidate; reprepare and retest")
            else:
                if receipt["stage"] != "prepared":
                    raise ValueError("previous main push is not present on remote; preserve its receipt for review before retrying")
                _scan_publication(profile, record, core.git_text(root, "log", "-1", "--format=%B"))
                receipt.update(stage="push_pending", push_started_at_utc=core.now())
                core.atomic(path, receipt)
                entry.update(integration_stage="push_pending", integration_receipt=str(path))
                core.atomic(profile["state_path"], receipts)
                # An interrupted invocation reconciles this frozen SHA before any
                # further write. It never blindly repeats an uncertain main push.
                outcome = core.git(root, "push", "origin", candidate_sha + ":refs/heads/main", check=False)
                receipt["push_exit_code"] = outcome.returncode
                core.atomic(path, receipt)
                current = _remote(root, "main")
                if current != candidate_sha:
                    _fetch(root, record_id, "main", current, "main")
                    if core.git(root, "merge-base", "--is-ancestor", candidate_sha, current, check=False).returncode:
                        raise RuntimeError("main push outcome unverified or advanced; pending receipt retained")
        receipt.update(stage="main_integrated", integrated_sha=candidate_sha,
                       verified_remote_sha=current, published_at_utc=receipt.get("published_at_utc") or core.now())
        core.atomic(path, receipt)
        entry.update(integration_stage="main_integrated", integrated_sha=candidate_sha,
                     integration_remote_sha=current, integration_receipt=str(path))
        core.atomic(profile["state_path"], receipts)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "publish"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args(argv)
    profile = core.load(args.profile)
    if args.action == "prepare":
        result = prepare(profile, args.id, args.worktree)
    elif args.action == "verify":
        result = verify(profile, args.id, args.worktree, reviewed=args.reviewed)
    else:
        result = publish(profile, args.id, args.worktree, approved=args.approved)
    visible = {key: result[key] for key in ("record_id", "source_sha", "main_before", "candidate_sha",
                                            "paths", "dependencies", "review_required", "stage",
                                            "integrated_sha", "verified_remote_sha") if key in result}
    if "integration_tests" in result:
        visible["passing_integration_checks"] = len(result["integration_tests"])
    print(json.dumps(visible, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
