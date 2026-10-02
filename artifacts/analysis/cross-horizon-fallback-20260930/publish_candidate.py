"""Publish only the user-authorized sealed candidate from its isolated worktree."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parent
TREE = Path(r"C:\Users\7980X\.codex\worktrees\cross-horizon-bearish-fallback\ducketz")
ACTIVE = Path(r"C:\dev\ducketz")
BRANCH = "codex/cross-horizon-bearish-fallback-20260930"
REMOTE = "https://github.com/jeremysecondstate/ducketz.git"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args, cwd=TREE):
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE)


def active_state():
    index = Path(git("rev-parse", "--git-path", "index", cwd=ACTIVE).decode().strip())
    if not index.is_absolute():
        index = ACTIVE / index
    return {"head": git("rev-parse", "HEAD", cwd=ACTIVE).decode().strip(),
            "branch": git("branch", "--show-current", cwd=ACTIVE).decode().strip(),
            "index_sha256": sha(index.read_bytes())}


def verify_candidate():
    manifest_bytes = (PACKAGE / "candidate-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    verified = json.loads((PACKAGE / "combined-verification.json").read_bytes())
    if verified["status"] != "PASS" or verified["source_drift"] or verified["exit_code"]:
        raise RuntimeError("Candidate tests are not clean")
    if sha((PACKAGE / manifest["test_evidence"]["path"]).read_bytes()) != manifest["test_evidence"]["sha256"]:
        raise RuntimeError("Candidate evidence drift")
    for record in manifest["files"]:
        rel = record["path"]
        digest = sha((TREE / rel).read_bytes())
        if digest != record["sha256"] or digest != verified["source_fingerprints"][rel]:
            raise RuntimeError("Candidate file drift: " + rel)
    for rel in manifest["source_dependency_fingerprints"]:
        if sha((TREE / rel).read_bytes()) != verified["source_fingerprints"][rel]:
            raise RuntimeError("Tested dependency drift: " + rel)
    if git("remote", "get-url", "--push", "origin").decode().strip() != REMOTE:
        raise RuntimeError("Unexpected push destination")
    return manifest, sha(manifest_bytes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("commit", "push"))
    args = parser.parse_args()
    manifest, manifest_hash = verify_candidate()
    files = sorted(r["path"] for r in manifest["files"])
    before = active_state()
    if args.operation == "commit":
        if git("rev-parse", "HEAD").decode().strip() != manifest["base_head_sha"]:
            raise RuntimeError("Unexpected isolated HEAD; inspect before retry")
        staged = set(git("diff", "--cached", "--name-only", "-z").decode().strip("\0").split("\0")) - {""}
        if not staged.issubset(files):
            raise RuntimeError("Unrelated staged entries")
        if git("ls-remote", "origin", "refs/heads/" + BRANCH).strip():
            raise RuntimeError("Remote branch already exists")
        git("switch", "-c", BRANCH)
        git("add", "--", *files)
        staged = sorted(set(git("diff", "--cached", "--name-only", "-z").decode().strip("\0").split("\0")) - {""})
        if staged != files:
            raise RuntimeError("Staged file set differs from manifest")
        git("diff", "--cached", "--check")
        for rel in files:
            expected = git("hash-object", "--path=" + rel, rel).decode().strip()
            actual = git("rev-parse", ":" + rel).decode().strip()
            if actual != expected:
                raise RuntimeError("Staged blob differs from reviewed bytes: " + rel)
        verify_candidate()
        git("commit", "--file", str(PACKAGE / "git-commit-message.txt"))
    else:
        if git("branch", "--show-current").decode().strip() != BRANCH:
            raise RuntimeError("Unexpected isolated branch")
        if git("rev-parse", "HEAD^").decode().strip() != manifest["base_head_sha"]:
            raise RuntimeError("Unexpected commit parent")
        committed = sorted(git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").decode().splitlines())
        if committed != files:
            raise RuntimeError("Committed file set differs from manifest")
        for rel in files:
            if git("rev-parse", "HEAD:" + rel).strip() != git("hash-object", "--path=" + rel, rel).strip():
                raise RuntimeError("Commit differs from verified source: " + rel)
        git("push", "--set-upstream", "origin", "HEAD:refs/heads/" + BRANCH)
    after = active_state()
    commit = git("rev-parse", "HEAD").decode().strip()
    remote = git("ls-remote", "origin", "refs/heads/" + BRANCH).decode().strip()
    remote_sha = remote.split()[0] if remote else None
    if args.operation == "push" and remote_sha != commit:
        raise RuntimeError("Remote commit not confirmed")
    record = {"operation": args.operation, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
              "branch": BRANCH, "commit": commit, "parent": git("rev-parse", "HEAD^").decode().strip(),
              "tree": git("rev-parse", "HEAD^{tree}").decode().strip(), "remote_sha": remote_sha,
              "manifest_sha256": manifest_hash, "files": files, "file_count": len(files),
              "active_before": before, "active_after": after, "active_unchanged": before == after,
              "deployment_status": "STAGED_NOT_DEPLOYED", "url": REMOTE[:-4] + "/commit/" + commit}
    (PACKAGE / ("git-" + args.operation + "-receipt.json")).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))
    if before != after:
        raise RuntimeError("Active checkout/index changed concurrently; inspect receipt")


if __name__ == "__main__":
    main()
