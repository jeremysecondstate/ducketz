"""Local immutable completion queue, native locks and bounded publication stages.

No function executes an incoming notice. Test commands come only from a reviewed
local producer specification. All application publication uses a caller-supplied
managed worktree, never the active application checkout.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from . import VERSION


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def load(path):
    return parse(Path(path).read_text(encoding="utf-8-sig"))


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(encoded(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def exclusive(path):
    """Same non-blocking native byte-range lock used by the legacy couriers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def relative(value):
    if not isinstance(value, str) or not value or value.startswith("/") or any(c in value for c in "\\:\x00"):
        raise ValueError("unsafe repository-relative path")
    for part in value.split("/"):
        if part in {"", ".", ".."} or part.endswith((".", " ")) or any(ord(c) < 32 for c in part):
            raise ValueError("unsafe repository-relative path")
        if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise ValueError("reserved path")
    return value


def shareable(value):
    relative(value)
    parts = value.lower().split("/")
    if any(p in {".git", ".codex", ".venv", "scratch", "data", "datastore", "credentials", "secrets"} for p in parts):
        raise ValueError("local/private path cannot be queued")
    filename_parts = parts[-1].split(".")
    if "local" in filename_parts[1:] and "example" not in filename_parts[1:]:
        raise ValueError("machine-local settings cannot be queued")
    if parts[-1] == ".env" or parts[-1].endswith((".db", ".sqlite", ".sqlite3", ".parquet", ".pkl", ".joblib", ".pem", ".key")) or (parts[-1].endswith(".lock") and not parts[-1].startswith("requirements")):
        raise ValueError("private/generated payload cannot be queued")
    return value


def safe_path(root, name):
    root = Path(root).resolve()
    target = root / relative(name)
    cursor = root
    for part in name.split("/"):
        cursor /= part
        if cursor.exists() or cursor.is_symlink():
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise ValueError("symlink/reparse path forbidden")
    if not target.resolve().is_relative_to(root):
        raise ValueError("path escapes root")
    return target


def git(root, *args, data=None, check=True):
    disabled_hooks = str(Path(tempfile.gettempdir()) / ("cross-pc-no-hooks-" + uuid.uuid4().hex))
    result = subprocess.run(["git", "-c", "core.hooksPath=" + disabled_hooks, "-c", "commit.gpgsign=false", *args], cwd=root, input=data, capture_output=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError("git " + args[0] + " failed: " + result.stderr.decode("utf-8", "replace")[:1500])
    return result


def git_text(root, *args):
    return git(root, *args).stdout.decode("utf-8").strip()


def fingerprints(root, paths):
    return {p: digest(safe_path(root, p).read_bytes()) if safe_path(root, p).is_file() else None for p in paths}


def validate_profile(profile):
    if profile.get("contract_version") != VERSION or profile.get("repository") != "jeremysecondstate/ducketz":
        raise ValueError("profile contract/repository mismatch")
    actor = profile.get("actor")
    if actor not in {"Atlas", "Scout"} or profile.get("machine") != {"Atlas": "pc-original", "Scout": "pc-new"}[actor]:
        raise ValueError("profile identity mismatch")
    if profile.get("branch_prefix") != "codex/" + actor.lower() + "/":
        raise ValueError("profile branch ownership mismatch")
    if not isinstance(profile.get("symbols"), list) or not isinstance(profile.get("authorized_producers"), list):
        raise ValueError("profile symbols/producers missing")
    return profile


def new_state(profile):
    return {"contract_version": VERSION, "actor": profile["actor"], "records": {}, "path_frontier": {},
            "source_cursor": None, "delivery_cursor": None, "observations": {}, "notices": {}, "runs": {}}


def state(profile):
    path = Path(profile["state_path"])
    value = load(path) if path.exists() else new_state(profile)
    if value.get("contract_version") != VERSION or value.get("actor") != profile["actor"]:
        raise ValueError("receipt identity mismatch; preserve existing file")
    for key in ("records", "path_frontier", "observations", "notices", "runs"):
        if not isinstance(value.get(key), dict):
            raise ValueError("malformed receipts; preserve existing file")
    return value


def blob_path(profile, sha):
    if not re.fullmatch("[0-9a-f]{64}", sha):
        raise ValueError("invalid snapshot hash")
    return Path(profile["snapshot_root"]) / sha


def store_blob(profile, raw):
    sha = digest(raw)
    path = blob_path(profile, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("immutable snapshot collision")
    return sha


def run_checks(root, commands, expected, evidence_root, original_root=None):
    """Run explicit local argv only, with pre/post byte binding; no shell."""
    evidence_root = Path(evidence_root) / uuid.uuid4().hex
    evidence_root.mkdir(parents=True, exist_ok=True)
    if not commands:
        raise ValueError("meaningful verification commands required")
    evidence = []
    for index, argv in enumerate(commands):
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("test command must be explicit argv")
        validate_check(argv)
        arguments = argv[1:]
        if arguments[0] == "-B":
            arguments = arguments[1:]
        if arguments[0] != "-m" and expected.get(arguments[0]) is None:
            raise ValueError("direct test script must be in dependency fingerprints")
        if fingerprints(root, expected) != expected:
            raise ValueError("source/dependency mutation before verification")
        started = now()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        result_path = evidence_root / (str(index) + "-imports.json")
        specification = evidence_root / (str(index) + "-runner.json")
        harness = Path(__file__).with_name("test_runner.py")
        atomic(specification, {"root": str(Path(root).resolve()), "original_root": str(Path(original_root or root).resolve()),
                               "fingerprints": expected, "argv": argv, "result_path": str(result_path)})
        result = subprocess.run([argv[0], "-B", str(harness), str(specification)], cwd=root, capture_output=True, timeout=600, env=environment)
        output = result.stdout + b"\n" + result.stderr
        log = evidence_root / (str(index) + ".log")
        log.write_bytes(output)
        item = {"argv": argv, "started_at_utc": started, "completed_at_utc": now(), "exit_code": result.returncode,
                "output_sha256": digest(output), "evidence_path": str(log), "tested_fingerprints": expected,
                "result": output.decode("utf-8", "replace")[-1500:], "verifier_sha256": digest(harness.read_bytes()),
                "import_evidence_path": str(result_path), "import_evidence_sha256": digest(result_path.read_bytes()) if result_path.exists() else None}
        evidence.append(item)
        if result.returncode or not result_path.exists() or load(result_path)["violations"]:
            atomic(evidence_root / "failed.json", evidence)
            raise ValueError("verification failed; evidence retained at " + str(log))
        if fingerprints(root, expected) != expected:
            raise ValueError("source/dependency mutation during verification")
    return evidence


def validate_check(argv):
    """Restrict replay to Python test modules or repository-relative test scripts.

    This is a local review contract, not a sandbox: approved tests must themselves
    stay offline. No shell, inline code, external script path or cwd override.
    """
    executable = Path(argv[0]).name.lower()
    if executable not in {"python", "python.exe", "python3", "python3.exe"}:
        raise ValueError("verification requires an installed Python test runner")
    args = argv[1:]
    if args and args[0] == "-B":
        args = args[1:]
    if not args or args[0] == "-c":
        raise ValueError("inline or missing verification script")
    if args[0] == "-m":
        if len(args) < 2 or args[1] not in {"pytest", "unittest", "compileall"}:
            raise ValueError("unsupported offline verification module")
        targets = args[2:]
    else:
        if not args[0].startswith("tests/") or not args[0].endswith(".py"):
            raise ValueError("test script must be repository-relative")
        targets = args
    for arg in targets:
        value = arg.split("=", 1)[-1]
        if re.search(r"[A-Za-z]:[/\\]", value) or "\\" in value or value.startswith("/") or ".." in value.split("/") or arg.startswith(("--rootdir", "--confcutdir", "--pyargs")):
            raise ValueError("verification arguments must stay in candidate repository")


def verify_test_evidence(tests, expected):
    if not tests:
        raise ValueError("missing test evidence")
    for test in tests:
        validate_check(test["argv"])
        if test["exit_code"] != 0 or test["tested_fingerprints"] != expected or digest(Path(test["evidence_path"]).read_bytes()) != test["output_sha256"]:
            raise ValueError("test evidence mismatch")
        if digest(Path(test["import_evidence_path"]).read_bytes()) != test["import_evidence_sha256"] or load(test["import_evidence_path"])["violations"]:
            raise ValueError("test import evidence mismatch")


def queue(profile, source, spec):
    """Capture reviewed exact bytes and real passing tests as a sealed local record."""
    validate_profile(profile)
    if spec.get("producer") not in profile["authorized_producers"] or spec.get("reviewed") is not True:
        raise ValueError("producer must be locally authorized and review complete")
    if not spec.get("summary") or not isinstance(spec.get("limitations"), list) or not spec.get("runtime_implications"):
        raise ValueError("summary, limitations and runtime implications required")
    base = spec["base_commit"]
    if not re.fullmatch("[0-9a-f]{40}", base) or git_text(source, "rev-parse", "HEAD") != base:
        raise ValueError("queue source must be at the reviewed base")
    files = spec["files"]
    if not files or len({f["path"].casefold() for f in files}) != len(files):
        raise ValueError("empty/duplicate owned file set")
    for item in files:
        shareable(item["path"])
        if item.get("operation") not in {"add", "modify", "delete"} or item.get("owned") is not True:
            raise ValueError("explicit owned operation required")
        exists_base = git(source, "cat-file", "-e", base + ":" + item["path"], check=False).returncode == 0
        if exists_base != (item["operation"] != "add"):
            raise ValueError("operation disagrees with base tree")
        if (safe_path(source, item["path"]).exists()) == (item["operation"] == "delete"):
            raise ValueError("operation disagrees with current bytes")
    paths = sorted(set([f["path"] for f in files] + spec.get("dependencies", [])))
    for name in paths:
        shareable(name)
    expected = fingerprints(source, paths)
    deleted = {item["path"] for item in files if item["operation"] == "delete"}
    if any(sha is None and name not in deleted for name, sha in expected.items()):
        raise ValueError("missing dependency; incomplete publication record")
    record_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    for name, sha in expected.items():
        if sha is not None and store_blob(profile, safe_path(source, name).read_bytes()) != sha:
            raise ValueError("source mutated while snapshotting")
    tests = run_checks(source, spec["checks"], expected, Path(profile["evidence_root"]) / record_id)
    record = {"schema_version": 2, "contract_version": VERSION, "id": record_id, "actor": profile["actor"],
              "producer": spec["producer"], "base_commit": base, "source_root": str(Path(source).resolve()),
              "completed_at_utc": now(), "summary": spec["summary"], "files": files, "fingerprints": expected,
              "tests": tests, "limitations": spec["limitations"], "runtime_implications": spec["runtime_implications"],
              "supersedes": spec.get("supersedes", []), "ready": True, "reviewed": True}
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        for old in receipts["records"].values():
            if old.get("source_stage") in {"pushed", "superseded"}:
                continue
            old_record = load(Path(profile["ready_root"]) / (old["id"] + ".json"))
            overlap = set(f["path"].casefold() for f in files) & set(f["path"].casefold() for f in old_record["files"])
            resolution = profile.get("ownership_resolutions", {}).get(old_record["id"])
            resolved = resolution == {"from": old_record["producer"], "to": spec["producer"], "approved": True}
            if overlap and old_record["producer"] != spec["producer"] and not resolved:
                raise ValueError("overlapping producer ownership: " + ", ".join(sorted(overlap)))
            if old_record["id"] in record["supersedes"] and (not overlap or old.get("source_stage") in {"committed", "committing"}):
                raise ValueError("supersession cannot discard committed or unrelated work")
        if any(item not in receipts["records"] for item in record["supersedes"]):
            raise ValueError("unknown superseded record")
        path = Path(profile["ready_root"]) / (record_id + ".json")
        atomic(path, record)
        receipts["records"][record_id] = {"id": record_id, "record_sha256": digest(path.read_bytes()),
                                           "source_stage": "queued", "delivery_stage": "not_prepared"}
        for replaced in record["supersedes"]:
            if replaced in receipts["records"] and receipts["records"][replaced]["source_stage"] != "pushed":
                receipts["records"][replaced]["source_stage"] = "superseded"
                receipts["records"][replaced]["superseded_by"] = record_id
        atomic(profile["state_path"], receipts)
    return record


def validate_record(profile, record_id, receipts=None, current=True):
    receipts = state(profile) if receipts is None else receipts
    entry = receipts["records"][record_id]
    path = Path(profile["ready_root"]) / (record_id + ".json")
    if digest(path.read_bytes()) != entry["record_sha256"]:
        raise ValueError("sealed completion record changed")
    record = load(path)
    if record.get("contract_version") != VERSION or record.get("actor") != profile["actor"] or record.get("producer") not in profile["authorized_producers"]:
        raise ValueError("record identity/authority mismatch")
    if record.get("ready") is not True or record.get("reviewed") is not True or not record.get("tests"):
        raise ValueError("incomplete record")
    verify_test_evidence(record["tests"], record["fingerprints"])
    for name, sha in record["fingerprints"].items():
        shareable(name)
        if sha is not None and digest(blob_path(profile, sha).read_bytes()) != sha:
            raise ValueError("immutable snapshot changed")
    if current and fingerprints(record["source_root"], record["fingerprints"]) != record["fingerprints"]:
        raise ValueError("source/dependency changed after tests; fresh review required")
    for item in record["files"]:
        frontier = receipts["path_frontier"].get(item["path"].casefold())
        if frontier and frontier["id"] != record_id and frontier["completed_at_utc"] >= record["completed_at_utc"]:
            raise ValueError("superseded by newer accepted revision")
    return record


def choose_fair(keys, cursor):
    keys = sorted(keys)
    return next((key for key in keys if cursor is None or key > cursor), keys[0] if keys else None)


def plan(profile):
    """One source candidate and one independent delivery candidate per wake."""
    validate_profile(profile)
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        # A crash after sealing ready JSON but before its atomic receipt must not
        # strand completed work. Reconcile only intact locally authorized records.
        ready_paths = sorted(Path(profile["ready_root"]).glob("*.json"))
        if len(ready_paths) > 1000:
            raise ValueError("ready inventory exceeds bounded limit; preserve for review")
        for path in ready_paths:
            if path.stem in receipts["records"]:
                continue
            record = load(path)
            if record.get("id") != path.stem:
                raise ValueError("orphan record identity mismatch")
            entry = {"id": path.stem, "record_sha256": digest(path.read_bytes()), "source_stage": "queued", "delivery_stage": "not_prepared", "recovered_orphan": True}
            receipts["records"][path.stem] = entry
            validate_record(profile, path.stem, receipts, current=False)
        source = choose_fair([key for key, value in receipts["records"].items()
                              if value["source_stage"] not in {"pushed", "superseded"}], receipts["source_cursor"])
        delivery = choose_fair([key for key, value in receipts["notices"].items()
                                if value.get("stage") != "published"], receipts["delivery_cursor"])
        result = {"contract_version": VERSION, "source": source, "delivery": delivery, "limits": {"source": 1, "observations": 10}}
        if source:
            receipts["source_cursor"] = source
            try:
                entry = receipts["records"][source]
                record = validate_record(profile, source, receipts, current=entry["source_stage"] not in {"committing", "committed"})
                result["source_record"] = {k: record[k] for k in ("base_commit", "summary", "files", "runtime_implications")}
            except (ValueError, OSError, KeyError) as error:
                result["source_blocker"] = str(error)
                receipts["records"][source]["blocker"] = str(error)
        if delivery:
            receipts["delivery_cursor"] = delivery
        atomic(profile["state_path"], receipts)
        return result


def prepare_candidate(profile, record_id, worktree):
    """Materialize only owned snapshot bytes into a clean managed worktree."""
    worktree = Path(worktree).resolve()
    if worktree == Path(profile["checkout"]).resolve() or not (worktree / ".git").is_file():
        raise ValueError("must use an isolated managed Git worktree")
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        record = validate_record(profile, record_id, receipts)
        entry = receipts["records"][record_id]
        branch = profile["branch_prefix"] + record_id
        recovering = entry.get("source_stage") in {"preparing", "prepared"}
        if recovering and (entry.get("worktree") != str(worktree) or entry.get("branch") != branch):
            raise ValueError("record already owns another candidate")
        if git_text(worktree, "rev-parse", "HEAD") != record["base_commit"]:
            raise ValueError("candidate must be at exact reviewed base")
        if not recovering and git_text(worktree, "status", "--porcelain"):
            raise ValueError("candidate must be clean at exact reviewed base")
        owned = {f["path"] for f in record["files"]}
        if recovering:
            changed = set(git_text(worktree, "diff", "--name-only").splitlines()) | set(git_text(worktree, "ls-files", "--others", "--exclude-standard").splitlines())
            if changed - owned or git_text(worktree, "diff", "--cached", "--name-only"):
                raise ValueError("interrupted candidate contains unrelated changes")
            for name in changed:
                actual = fingerprints(worktree, [name])[name]
                if actual != record["fingerprints"][name]:
                    raise ValueError("interrupted candidate bytes changed; preserve for review")
        dependencies = {p: sha for p, sha in record["fingerprints"].items() if p not in owned}
        if fingerprints(worktree, dependencies) != dependencies:
            raise ValueError("isolated dependency closure differs; include/review missing owned dependencies")
        entry.update(source_stage="preparing", worktree=str(worktree), branch=branch)
        atomic(profile["state_path"], receipts)
        if git_text(worktree, "branch", "--show-current") != branch:
            git(worktree, "switch", "-c", branch)
        for item in record["files"]:
            path = safe_path(worktree, item["path"])
            if item["operation"] == "delete":
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob_path(profile, record["fingerprints"][item["path"]]).read_bytes())
        receipts["records"][record_id].update(source_stage="prepared", worktree=str(worktree), branch=branch)
        atomic(profile["state_path"], receipts)
        return {"worktree": str(worktree), "branch": branch, "review_required": True,
                "next": "Review entire diff/dependencies; verify-candidate records fresh isolated checks."}


def verify_candidate(profile, record_id, worktree, reviewed=False):
    if reviewed is not True:
        raise ValueError("explicit full candidate review required")
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        record = validate_record(profile, record_id, receipts)
        entry = receipts["records"][record_id]
        if Path(entry.get("worktree", "")).resolve() != Path(worktree).resolve():
            raise ValueError("candidate path mismatch")
        if git_text(worktree, "rev-parse", "HEAD") != record["base_commit"] or git_text(worktree, "branch", "--show-current") != entry["branch"]:
            raise ValueError("candidate Git identity changed")
        entry.update(source_stage="prepared")
        atomic(profile["state_path"], receipts)
        tests = run_checks(worktree, [t["argv"] for t in record["tests"]], record["fingerprints"],
                           Path(profile["evidence_root"]) / record_id / "isolated", original_root=record["source_root"])
        entry.update(source_stage="validated", isolated_tests=tests, reviewed_at_utc=now())
        atomic(profile["state_path"], receipts)
        return entry


def verify_commit(root, record, commit):
    if git_text(root, "rev-parse", commit + "^") != record["base_commit"]:
        raise ValueError("committed parent differs from reviewed base")
    changed = set(git_text(root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines())
    if changed != {f["path"] for f in record["files"]}:
        raise ValueError("committed file set differs from reviewed snapshot")
    for item in record["files"]:
        blob = git(root, "show", commit + ":" + item["path"], check=False)
        if item["operation"] == "delete":
            if blob.returncode == 0:
                raise ValueError("committed deletion missing")
        elif blob.returncode or digest(blob.stdout) != record["fingerprints"][item["path"]]:
            raise ValueError("committed bytes differ from reviewed snapshot")


def publish_source(profile, record_id):
    """Normal own-branch push; durable commit stage before network, exact SHA readback."""
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        entry = receipts["records"][record_id]
        record = validate_record(profile, record_id, receipts, current=entry["source_stage"] not in {"committing", "committed", "pushed"})
        root = entry["worktree"]
        branch = profile["branch_prefix"] + record_id
        if entry.get("branch") != branch or git_text(root, "branch", "--show-current") != branch:
            raise ValueError("branch ownership mismatch")
        if entry["source_stage"] not in {"validated", "committing", "committed", "pushed"}:
            raise ValueError("isolated validation required")
        verify_test_evidence(entry.get("isolated_tests"), record["fingerprints"])
        origin = git_text(root, "remote", "get-url", "origin")
        if origin not in {"https://github.com/jeremysecondstate/ducketz.git", "git@github.com:jeremysecondstate/ducketz.git"} and not profile.get("test_remote"):
            raise ValueError("unexpected origin")
        head = git_text(root, "rev-parse", "HEAD")
        if entry["source_stage"] == "committing" and head != record["base_commit"]:
            verify_commit(root, record, head)
            if ("Completion-Record: " + record_id) not in git_text(root, "log", "-1", "--format=%B"):
                raise ValueError("unknown commit after interruption")
            entry.update(source_stage="committed", commit_sha=head, committed_at_utc=now(), recovered_commit=True)
            atomic(profile["state_path"], receipts)
        if entry["source_stage"] in {"validated", "committing"}:
            if fingerprints(root, record["fingerprints"]) != record["fingerprints"]:
                raise ValueError("candidate changed after isolated checks")
            if git_text(root, "rev-parse", "HEAD") != record["base_commit"]:
                raise ValueError("candidate base changed")
            staged = set(git_text(root, "diff", "--cached", "--name-only").splitlines())
            if entry["source_stage"] == "validated" and staged:
                raise ValueError("candidate index must start empty")
            changed = set(git_text(root, "diff", "HEAD", "--name-only").splitlines()) | set(git_text(root, "ls-files", "--others", "--exclude-standard").splitlines())
            if changed != {item["path"] for item in record["files"]}:
                raise ValueError("candidate contains unrelated or missing changes")
            if staged - {item["path"] for item in record["files"]}:
                raise ValueError("interrupted index contains unrelated staged work")
            entry.update(source_stage="committing")
            atomic(profile["state_path"], receipts)
            for item in record["files"]:
                if item["operation"] == "delete":
                    git(root, "update-index", "--force-remove", "--", item["path"])
                else:
                    raw = blob_path(profile, record["fingerprints"][item["path"]]).read_bytes()
                    blob = git(root, "hash-object", "-w", "--stdin", "--no-filters", data=raw).stdout.decode().strip()
                    # Preserve executable mode where present. No filters may alter tested bytes.
                    prior = git_text(root, "ls-tree", record["base_commit"], "--", item["path"])
                    mode = prior.split()[0] if prior else "100644"
                    git(root, "update-index", "--add", "--cacheinfo", mode + "," + blob + "," + item["path"])
            body = (profile["actor"] + ": " + record["summary"] + "\n\n"
                    + "Reviewed immutable completion " + record_id + ".\n"
                    + "Validation: " + str(len(entry["isolated_tests"])) + " passing offline check commands; exact evidence retained locally.\n"
                    + "Runtime: " + record["runtime_implications"] + "\n"
                    + "Codex-Author: " + profile["actor"] + "\nCompletion-Record: " + record_id + "\n")
            git(root, "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", "commit", "-F", "-", data=body.encode())
            verify_commit(root, record, git_text(root, "rev-parse", "HEAD"))
            entry.update(source_stage="committed", commit_sha=git_text(root, "rev-parse", "HEAD"), committed_at_utc=now())
            atomic(profile["state_path"], receipts)
        sha = entry["commit_sha"]
        if git_text(root, "rev-parse", "HEAD") != sha:
            raise ValueError("committed candidate HEAD changed")
        verify_commit(root, record, sha)
        remote = git_text(root, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
        if remote and remote.split()[0] != sha:
            raise ValueError("remote branch has different bytes; no force push")
        if not remote:
            outcome = git(root, "push", "origin", "HEAD:refs/heads/" + branch, check=False)
            remote = git_text(root, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
            if not remote or remote.split()[0] != sha:
                raise RuntimeError("push outcome unverified; committed stage retained (exit " + str(outcome.returncode) + ")")
        entry.update(source_stage="pushed", remote_sha=sha, pushed_at_utc=now(), integration_stage="pending_review", peer_source_stage="available_unverified", installation_stage="not_installed", runtime_stage="not_deployed")
        for item in record["files"]:
            receipts["path_frontier"][item["path"].casefold()] = {"id": record_id, "completed_at_utc": record["completed_at_utc"], "commit_sha": sha}
        atomic(profile["state_path"], receipts)
        return entry


def record_run(profile, purpose, outcome, summary, source_version, blocker=None):
    if outcome not in {"completed", "failed", "blocked", "unchanged"}:
        raise ValueError("invalid run outcome")
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        value = {"outcome": outcome, "summary": summary, "source_version": source_version, "blocker": blocker}
        fingerprint = digest(encoded(value))
        prior = receipts["runs"].get(purpose, {})
        report = outcome != "unchanged" and prior.get("fingerprint") != fingerprint
        receipts["runs"][purpose] = {**value, "at_utc": now(), "fingerprint": fingerprint}
        atomic(profile["state_path"], receipts)
        return {"meaningful_change": report, **receipts["runs"][purpose]}
