"""Seal reviewed source bytes and a bounded local import dependency inventory."""
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

WORKTREE = Path(r"C:\Users\7980X\.codex\worktrees\cross-horizon-bearish-fallback\ducketz")
TARGET = Path(r"C:\dev\ducketz")
OUT = Path(__file__).resolve().parent
BASE = "0407388559d54612603380793fc26a98937697fc"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=WORKTREE, stderr=subprocess.PIPE)


def normalized(data):
    return data.replace(b"\r\n", b"\n")


def main():
    verification = json.loads((OUT / "combined-verification.json").read_text())
    if verification["status"] != "PASS":
        raise RuntimeError("Combined tests have not passed")
    changed = set(git("diff", "HEAD", "--name-only", "-z").decode().strip("\0").split("\0"))
    changed.update(git("ls-files", "--others", "--exclude-standard", "-z").decode().strip("\0").split("\0"))
    changed.discard("")
    # Review evidence stays beside the candidate; it is never deployed as source.
    changed = {p for p in changed if not p.startswith("artifacts/analysis/cross-horizon-bearish-fallback/")}
    allowed = {"app", "ml", "tests", "docs"}
    if any(p.split("/")[0] not in allowed for p in changed):
        raise RuntimeError("Unexpected candidate file scope")
    records = []
    for relative in sorted(changed):
        path = WORKTREE / relative
        final = path.read_bytes()
        if verification["source_fingerprints"].get(relative) != sha(final):
            raise RuntimeError(f"File changed after tests: {relative}")
        old = TARGET / relative
        try:
            original = git("show", f"{BASE}:{relative}")
        except subprocess.CalledProcessError:
            original = None
        if original is None:
            if old.exists():
                raise RuntimeError(f"New candidate path already exists in active checkout: {relative}")
            base_hash = None
        else:
            if not old.is_file() or normalized(old.read_bytes()) != normalized(original):
                raise RuntimeError(f"Concurrent edit in candidate target: {relative}")
            base_hash = sha(old.read_bytes())
            backup = OUT / "base" / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(old.read_bytes())
        payload = OUT / "files" / relative
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(final)
        records.append({"path": relative, "base_sha256": base_hash, "sha256": sha(final)})
    # Parse import statements only. Never import application or broker code.
    all_paths = set(git("ls-files", "-z").decode().strip("\0").split("\0")) | changed
    modules = {}
    for p in all_paths:
        if p.endswith(".py"):
            name = p[:-3].replace("/", ".")
            if name.endswith(".__init__"):
                name = name[:-9]
            modules[name] = p
    roots = [p for p in changed if p.endswith(".py") and not p.startswith("tests/")]
    visited, queue = set(), list(roots)
    while queue:
        relative = queue.pop()
        if relative in visited:
            continue
        visited.add(relative)
        current_module = relative[:-3].replace("/", ".")
        package = current_module.rsplit(".", 1)[0]
        if relative.endswith("/__init__.py"):
            package = current_module[:-9]
        tree = ast.parse((WORKTREE / relative).read_text(encoding="utf-8-sig"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    parent = package.split(".")
                    prefix = ".".join(parent[:len(parent) - node.level + 1] + ([prefix] if prefix else []))
                names.append(prefix)
                names.extend(prefix + "." + alias.name for alias in node.names)
        for name in names:
            for size in range(1, len(name.split(".")) + 1):
                module = ".".join(name.split(".")[:size])
                if module in modules:
                    queue.append(modules[module])
    # Explicit configuration read through filesystem helpers is not an import.
    dependencies = visited - changed
    dependencies.add("datafetching/watchlist.txt")
    fingerprints = {}
    drift = []
    for relative in sorted(dependencies):
        tested = (WORKTREE / relative).read_bytes()
        if verification["source_fingerprints"].get(relative) != sha(tested):
            raise RuntimeError(f"Dependency changed after tests: {relative}")
        active = (TARGET / relative).read_bytes()
        if normalized(active) != normalized(tested):
            drift.append(relative)
        fingerprints[relative] = sha(active)
    if drift:
        raise RuntimeError(f"Active dependency differs from tested checkout: {drift}")
    helper_evidence = json.loads((OUT / "apply-helper-verification.json").read_text())
    helper_hash = sha((OUT / "apply_candidate.py").read_bytes())
    if helper_evidence.get("status") != "PASS" or helper_evidence.get("helper_sha256") != helper_hash:
        raise RuntimeError("Deployment helper differs from passing verification")
    for path_key, hash_key in (("test_path", "test_sha256"), ("junit_path", "junit_sha256")):
        if sha((OUT / helper_evidence[path_key]).read_bytes()) != helper_evidence[hash_key]:
            raise RuntimeError("Deployment helper test evidence changed")
    audit = json.loads((OUT / "apply-helper-audit.json").read_text(encoding="utf-8"))
    if audit.get("status") != "PASS" or audit.get("bindings", {}).get("apply_candidate.py") != helper_hash:
        raise RuntimeError("Deployment helper lacks matching independent audit")
    for name, expected in audit["bindings"].items():
        if sha((OUT / name).read_bytes()) != expected:
            raise RuntimeError("Independent audit evidence changed: " + name)
    manifest = {"schema_version": 1, "package_id": "cross-horizon-fallback-20260930",
        "status": "VERIFIED_STAGED_NOT_DEPLOYED", "effective_action_date": "2026-10-01",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "base_head_sha": BASE,
        "source_checkout": str(WORKTREE), "target_checkout": str(TARGET),
        "files": records, "source_dependency_fingerprints": fingerprints,
        "apply_helper_sha256": helper_hash,
        "apply_helper_test_evidence": {"path": "apply-helper-verification.json", "sha256": sha((OUT / "apply-helper-verification.json").read_bytes())},
        "apply_helper_audit": {"path": "apply-helper-audit.json", "sha256": sha((OUT / "apply-helper-audit.json").read_bytes())},
        "test_evidence": {"path": "combined-verification.json", "sha256": sha((OUT / "combined-verification.json").read_bytes()),
            "log_path": "combined-tests.log", "log_sha256": sha((OUT / "combined-tests.log").read_bytes())},
        "authorization": "September 30 user approval: 50% combined daily fallback cap, weighted small trims, next-session execution; preserve active trader.",
        "deployment_condition": "After September 30 session is terminal and absent, under native supervision/session/cycle/overnight ownership, before October 1 04:00 Pacific.",
        "runtime_actions_performed": False}
    destination = OUT / "candidate-manifest.json"
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": manifest["status"], "files": len(records), "dependencies": len(fingerprints),
        "manifest": str(destination), "sha256": sha(destination.read_bytes())}, indent=2))


if __name__ == "__main__":
    main()
