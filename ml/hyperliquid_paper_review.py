"""Guarded Paper experiment rollover, with immutable archive and opening evidence.

Stages are explicit: begin, archive, prepare, accept, complete. The caller stops
and starts the native runtimes and supplies its broader health evidence. No
stage signs orders, starts Powder, deletes history, or edits model settings.
Failures retain maintenance ownership and evidence for operator recovery.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
from uuid import uuid4

from filelock import FileLock
import psutil

OWNER = "paper-self-improvement"
ACCOUNTS = {"alex", "jeremy", "clearpond"}
NAMESPACES = ("_paper", "_models")
EXCLUDED_SEED_PREFIX = "2026-09-26T10:35:59"
BASELINE_NAME = "paper-current-accepted.json"
RUNTIMES = {
    "paper": ("_paper/_runtime", ".paper.lock", "ml.hyperliquid_paper_runtime", "hyperliquid-paper.json"),
    "models": ("_models/_runtime", ".runtime.lock", "ml.hyperliquid_model_runtime", "hyperliquid-models.json"),
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value, *, exclusive=False):
    path = Path(path)
    encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
        return
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def safe_path(path):
    """Reject symlinks/junctions before resolve(), including existing ancestors."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"Symlink path is not allowed: {item}")
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Reparse path is not allowed: {item}")
    return path.resolve()


def child(parent, name):
    parent = safe_path(parent)
    path = safe_path(parent / name)
    if path.parent != parent:
        raise ValueError(f"Expected an exact direct child: {path}")
    return path


def inventory(root, namespaces=NAMESPACES):
    root = safe_path(root)
    rows = []
    for name in namespaces:
        directory = child(root, name)
        if not directory.is_dir():
            raise ValueError(f"Missing archive namespace: {directory}")
        for path in sorted(directory.rglob("*")):
            safe_path(path)
            if path.is_file():
                rows.append({"relative_path": path.relative_to(root).as_posix(),
                             "bytes": path.stat().st_size, "sha256": digest(path)})
    return rows


def runtime_processes():
    """Fail closed when a Python identity cannot be read; include launchers."""
    found, uncertain = [], []
    modules = {row[2] for row in RUNTIMES.values()} | {"ml.hyperliquid_powder_runtime"}
    for process in psutil.process_iter(["pid", "name", "cmdline", "create_time", "ppid"]):
        try:
            info = process.info
            command = info.get("cmdline")
            if not command:
                if "python" in (info.get("name") or "").lower():
                    uncertain.append(process.pid)
                continue
            matched = modules.intersection(command)
            if matched:
                found.append({**info, "module": sorted(matched)[0]})
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            uncertain.append(process.pid)
    if uncertain:
        raise ValueError(f"Unverified process identities: {uncertain}")
    return found


def probe_lock(path):
    """Probe an existing lock without creating/truncating an archived file."""
    path = safe_path(path)
    if not path.exists():
        return
    with path.open("r+b") as stream:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def ledger_read(path, *, immutable=False):
    path = safe_path(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Paper SQLite integrity check failed")
        yield connection
    finally:
        connection.close()


def seed_info(paper, *, stopped=False):
    wal = paper / "ledger.sqlite3-wal"
    # An idle checkpointed database can be inspected without SQLite creating
    # empty WAL/SHM companions. Existing WAL content always remains visible.
    immutable = stopped and (not wal.exists() or wal.stat().st_size == 0)
    with ledger_read(paper / "ledger.sqlite3", immutable=immutable) as connection:
        seed_text = connection.execute("SELECT details_json FROM seed WHERE singleton=1").fetchone()[0]
        opening_text = connection.execute("SELECT result_json FROM cycles WHERE cycle_id='opening'").fetchone()[0]
        positions = [dict(row) for row in connection.execute("SELECT * FROM initial_positions ORDER BY account,coin,kind")]
        counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("fills", "decisions", "transfers", "funding")}
        latest = connection.execute("SELECT timestamp_utc FROM cycles ORDER BY timestamp_utc DESC LIMIT 1").fetchone()[0]
    seed = json.loads(seed_text)
    if seed["timestamp_utc"].startswith(EXCLUDED_SEED_PREFIX):
        raise ValueError("The permanently excluded predecessor is not a review/recovery target")
    hashes = {
        "seed_sha256": hashlib.sha256(seed_text.encode()).hexdigest(),
        "opening_cycle_sha256": hashlib.sha256(opening_text.encode()).hexdigest(),
        "initial_positions_sha256": hashlib.sha256(json.dumps(positions, sort_keys=True).encode()).hexdigest(),
        "opening_snapshot_sha256": digest(paper / "opening_snapshot.json"),
    }
    if read_json(paper / "opening_snapshot.json") != seed:
        raise ValueError("Opening export differs from authoritative seed")
    return {"seed": seed, "opening": json.loads(opening_text), "counts": counts,
            "latest_committed_at_utc": latest, "immutable_opening_hashes": hashes}


def assert_not_excluded(root, experiment):
    if experiment.get("seed_at_utc", "").startswith(EXCLUDED_SEED_PREFIX):
        raise ValueError("Permanently excluded predecessor")
    path = root / "_operations/excluded-paper-runs.json"
    if path.exists():
        for row in read_json(path)["excluded_runs"]:
            if (row.get("experiment_id") == experiment.get("experiment_id")
                    or row.get("seed_at_utc") == experiment.get("seed_at_utc")):
                raise ValueError("Experiment is listed in the current exclusion registry")


def assert_stopped(root, namespaces=NAMESPACES):
    processes = runtime_processes()
    if processes:
        raise ValueError(f"Paper/model runtime processes still exist: {[p['pid'] for p in processes]}")
    states = {}
    for role, (relative, lock, _, _) in RUNTIMES.items():
        if relative.split("/")[0] not in namespaces:
            continue
        control = safe_path(root / relative)
        status = read_json(control / "status.json")
        if status.get("status") != "stopped" or (control / "stop.request").exists():
            raise ValueError(f"{role} has not reached a clean stopped state")
        probe_lock(control / lock)
        states[role] = {"status": status["status"], "saved_pid": status.get("pid")}
    return {"checked_at_utc": utc(), "states": states, "runtime_processes": []}


def snapshot_provenance(project, destination):
    destination.mkdir()
    sources = [*sorted((project / "configs").glob("hyperliquid*.json")),
               *sorted((project / "ml").glob("hyperliquid*.py"))]
    for name in ("OPERATIONS_WATCH.md", "PAPER_IMPROVEMENT.md"):
        path = project / "docs/hyperliquid-system-analysis" / name
        if path.exists():
            sources.append(path)
    rows = []
    for source in sources:
        safe_path(source)
        relative = source.relative_to(project)
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
        rows.append({"relative_path": relative.as_posix(), "sha256": digest(output)})
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, text=True, capture_output=True, check=False)
    result = {"files": rows, "git_head": git.stdout.strip() if git.returncode == 0 else None,
              "snapshotted_at_utc": utc()}
    write_json(destination / "manifest.json", result, exclusive=True)
    return result


class ReviewCycle:
    def __init__(self, root, project, cycle_id):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", cycle_id):
            raise ValueError("cycle-id must be a simple unique identifier, without path separators")
        self.root, self.project = safe_path(root), safe_path(project)
        if not self.root.is_dir() or not self.project.is_dir():
            raise ValueError("Existing project and datastore directories are required")
        self.operations = child(self.root, "_operations")
        self.operations.mkdir(exist_ok=True)
        evidence_root = child(self.operations, "paper-improvement")
        evidence_root.mkdir(exist_ok=True)
        self.directory = child(evidence_root, cycle_id)
        self.archive_parent = child(self.root, "_paper_archives")
        self.archive_parent.mkdir(exist_ok=True)
        self.archive_path = child(self.archive_parent, cycle_id)
        self.cycle_id = cycle_id
        self.guard = child(self.operations, "paper-maintenance.json")
        self.operation_path = self.directory / "operation.json"

    def lock(self):
        return FileLock(str(child(self.operations, ".paper-review.lock")), timeout=0)

    def operation(self, *phases):
        value = read_json(self.operation_path)
        guard = read_json(self.guard)
        if (value.get("owner") != OWNER or value.get("cycle_id") != self.cycle_id
                or guard.get("status") != "in_progress" or guard.get("owner") != OWNER
                or guard.get("cycle_id") != self.cycle_id
                or safe_path(guard["evidence_directory"]) != self.directory):
            raise ValueError("Matching in-progress maintenance ownership is required")
        if phases and value["phase"] not in phases:
            raise ValueError(f"Stage requires {phases}; found {value['phase']}")
        assert_not_excluded(self.root, value["previous_experiment"])
        return value

    def phase(self, operation, phase, **fields):
        operation.update(phase=phase, updated_at_utc=utc(), **fields)
        write_json(self.operation_path, operation)
        guard = read_json(self.guard)
        guard.update(phase=phase)
        write_json(self.guard, guard)
        return operation

    def begin(self):
        with self.lock():
            if self.guard.exists() and read_json(self.guard).get("status") == "in_progress":
                raise ValueError("Existing maintenance must be resolved by its owner")
            if self.operation_path.exists() or self.archive_path.exists():
                raise ValueError("Duplicate cycle-id; preserve existing evidence and choose a new review")
            if self.directory.exists() and any((self.directory / name).exists() for name in
                                               ("before", "preservation-before.json", "opening-verification.json")):
                raise ValueError("Unclaimed lifecycle evidence already exists; investigate before begin")
            if any(row["module"] == "ml.hyperliquid_powder_runtime" for row in runtime_processes()):
                raise ValueError("Powder is active; shared model changes require separate operator review")
            previous = read_json(self.root / "_paper/experiment.json")
            assert_not_excluded(self.root, previous)
            if not previous.get("analysis_eligible") or previous["seed_at_utc"].startswith(EXCLUDED_SEED_PREFIX):
                raise ValueError("Current experiment is not eligible for review")
            self.directory.mkdir(exist_ok=True)
            if self.guard.exists():
                shutil.copy2(self.guard, self.directory / "prior-maintenance.json")
            baseline = self.operations / BASELINE_NAME
            if baseline.exists():
                shutil.copy2(baseline, self.directory / "prior-accepted.json")
            provenance = snapshot_provenance(self.project, self.directory / "before")
            operation = {"owner": OWNER, "cycle_id": self.cycle_id, "phase": "begun", "created_at_utc": utc(),
                         "root": str(self.root), "project": str(self.project),
                         "evidence_directory": str(self.directory), "archive_directory": str(self.archive_path),
                         "previous_experiment": previous, "before_provenance": provenance,
                         "source_snapshot_note": "Snapshot records files on disk at begin; a running process may have imported earlier source. Bind retained incumbent source to the review/change record when applicable."}
            write_json(self.operation_path, operation, exclusive=True)
            write_json(self.guard, {"status": "in_progress", "owner": OWNER, "cycle_id": self.cycle_id,
                                   "requested_at_utc": utc(), "phase": "begun",
                                   "evidence_directory": str(self.directory), "archive": str(self.archive_path),
                                   "reason": "User-authorized Paper self-improvement and fresh account mirror",
                                   "expected_paper_state": "maintenance; no watchdog recovery or reseeding",
                                   "expected_upstream_state": "data continues; models operator-controlled",
                                   "powder": "inactive; no real account mutation"})
            return operation

    def bootstrap(self):
        """Claim a human-authorized first session, never repair missing history.

        No predecessor exists to archive. Existing operating namespaces or
        lifecycle receipts require the ordinary review/recovery workflow.
        """
        with self.lock():
            if self.guard.exists() or (self.operations / BASELINE_NAME).exists():
                raise ValueError("Existing lifecycle state forbids first-session bootstrap")
            if any(child(self.root, name).exists() for name in NAMESPACES):
                raise ValueError("Existing Paper/model namespace forbids first-session bootstrap")
            if (self.operations / "paper-improvement-cadence.json").exists():
                raise ValueError("Existing cadence forbids first-session bootstrap")
            if any(self.directory.parent.iterdir()) or any(self.archive_parent.iterdir()):
                raise ValueError("Existing experiment history forbids first-session bootstrap")
            if runtime_processes():
                raise ValueError("Paper/model/Powder runtime exists before first-session bootstrap")
            self.directory.mkdir()
            provenance = snapshot_provenance(self.project, self.directory / "before")
            operation = {"owner": OWNER, "cycle_id": self.cycle_id, "phase": "archived",
                         "created_at_utc": utc(), "root": str(self.root), "project": str(self.project),
                         "evidence_directory": str(self.directory), "archive_directory": None,
                         "previous_experiment": {}, "before_provenance": provenance,
                         "first_session": True,
                         "archive_note": "No predecessor exists; no archive was created."}
            write_json(self.operation_path, operation, exclusive=True)
            write_json(self.guard, {"status": "in_progress", "owner": OWNER, "cycle_id": self.cycle_id,
                                   "requested_at_utc": utc(), "phase": "archived",
                                   "evidence_directory": str(self.directory), "archive": None,
                                   "reason": "Human-authorized first local simulated Paper session",
                                   "expected_paper_state": "maintenance; no watchdog recovery or reseeding",
                                   "expected_upstream_state": "data continues; models operator-controlled",
                                   "powder": "inactive; no real account mutation"})
            return operation

    def archive(self):
        with self.lock():
            operation = self.operation("begun", "archiving", "archived")
            manifest_path = self.directory / "preservation-before.json"
            if operation["phase"] == "archived":
                expected = read_json(manifest_path)["files"]
                if inventory(self.archive_path) != expected:
                    raise ValueError("Previously verified archive changed")
                return read_json(self.directory / "preservation-verified.json")
            if operation["phase"] == "begun":
                assert_stopped(self.root)
                seed = seed_info(self.root / "_paper", stopped=True)["seed"]
                if seed["timestamp_utc"] != operation["previous_experiment"]["seed_at_utc"]:
                    raise ValueError("Stopped seed differs from maintenance-begin experiment")
                files = inventory(self.root)
                if manifest_path.exists():
                    if read_json(manifest_path)["files"] != files:
                        raise ValueError("Recorded pre-archive manifest no longer matches source")
                else:
                    write_json(manifest_path, {"source_seed_at_utc": seed["timestamp_utc"], "files": files,
                                              "recorded_at_utc": utc(), "source_root": str(self.root)}, exclusive=True)
                self.phase(operation, "archiving")
            expected = read_json(manifest_path)["files"]
            if runtime_processes():
                raise ValueError("Runtime appeared during archival")
            self.archive_path.mkdir(exist_ok=True)
            for name in NAMESPACES:
                source, destination = child(self.root, name), child(self.archive_path, name)
                subset = [row for row in expected if row["relative_path"].startswith(name + "/")]
                if source.exists() and destination.exists() or not source.exists() and not destination.exists():
                    raise ValueError(f"Ambiguous interrupted archive for {name}")
                if destination.exists():
                    if inventory(self.archive_path, (name,)) != subset:
                        raise ValueError(f"Interrupted archive checksum mismatch: {name}")
                    continue
                assert_stopped(self.root, (name,))
                if inventory(self.root, (name,)) != subset:
                    raise ValueError(f"Archive source changed: {name}")
                self.operation("archiving")
                source.rename(destination)
            if inventory(self.archive_path) != expected:
                raise ValueError("Archived file checksum/size mismatch; maintenance remains active")
            result = {"result": "passed", "verified_at_utc": utc(), "archive": str(self.archive_path),
                      "file_count": len(expected), "bytes": sum(row["bytes"] for row in expected),
                      "manifest_sha256": digest(manifest_path)}
            write_json(self.directory / "preservation-verified.json", result)
            self.phase(operation, "archived")
            return result

    def preserve_unstarted_prepare(self, operation, source_path):
        """Retain a proven pre-entrypoint failure before resuming the same stage.

        An absent Paper namespace and empty capture files are required. A durable
        marker also permits interruption between the two evidence moves; it never
        permits replacing a partial ledger, source response, or newer capture.
        """
        output_path = child(self.directory, "prepare.stdout.json")
        source_path = safe_path(source_path)
        recovery = operation.get("prepare_preflight_recovery")
        if recovery is None:
            if (not output_path.is_file() or output_path.stat().st_size != 0
                    or not source_path.is_file() or read_json(source_path) != []):
                raise ValueError("Unstarted prepare recovery requires empty stdout and empty retained reads")
            preserved = child(self.directory, "prepare-preflight-failure-" + uuid4().hex)
            preserved.mkdir()
            recovery = {"recorded_at_utc": utc(), "cycle_id": self.cycle_id,
                        "directory": str(preserved), "reason": "No Paper namespace or captured public reads",
                        "files": [{"name": path.name, "bytes": path.stat().st_size,
                                   "sha256": digest(path)} for path in (output_path, source_path)]}
            write_json(preserved / "manifest.json", recovery, exclusive=True)
            self.phase(operation, "preparing", prepare_preflight_recovery=recovery)
        preserved = safe_path(recovery["directory"])
        if (preserved.parent != self.directory or recovery["cycle_id"] != self.cycle_id
                or read_json(preserved / "manifest.json") != recovery):
            raise ValueError("Unstarted prepare recovery evidence does not match this cycle")
        if {row["name"] for row in recovery["files"]} != {output_path.name, source_path.name}:
            raise ValueError("Unexpected unstarted prepare recovery files")
        for row in recovery["files"]:
            source, destination = child(self.directory, row["name"]), child(preserved, row["name"])
            if source.exists() == destination.exists():
                raise ValueError("Ambiguous interrupted prepare evidence preservation")
            current = source if source.exists() else destination
            if (not current.is_file() or current.stat().st_size != row["bytes"]
                    or digest(current) != row["sha256"]):
                raise ValueError("Unstarted prepare recovery evidence changed")
            if source.exists():
                source.rename(destination)

    def prepare(self):
        with self.lock():
            operation = self.operation("archived", "preparing", "prepared")
            paper = child(self.root, "_paper")
            source_path = self.directory / "opening-public-account-reads.json"
            unstarted = operation["phase"] == "preparing" and not paper.exists()
            if operation["phase"] == "archived" or unstarted:
                if any(row["module"] != "ml.hyperliquid_model_runtime" for row in runtime_processes()):
                    raise ValueError("Paper/Powder runtime appeared before fresh preparation")
                if paper.exists():
                    raise ValueError("Fresh Paper namespace required; an existing ledger is never reseeded")
                from ml.hyperliquid_paper_policy import load_config
                config_path = self.project / "configs/hyperliquid-paper.json"
                config = load_config(config_path)
                if config.mode != "paper" or config.seed_mode != "mirror" or config.data_root != self.root:
                    raise ValueError("Expected mirror-mode Paper config for this exact datastore")
                if unstarted:
                    self.preserve_unstarted_prepare(operation, source_path)
                from ml.hyperliquid_paper_seed import AccountReader
                from ml.hyperliquid_paper_runtime import main as paper_main
                original = AccountReader.post_info
                records, record_lock = [], threading.Lock()

                def recording(reader, payload):
                    started = utc()
                    response = original(reader, payload)
                    with record_lock:
                        records.append({"type": payload["type"],
                                        "public_owner_sha256": hashlib.sha256(payload["user"].lower().encode()).hexdigest(),
                                        "started_at_utc": started, "completed_at_utc": utc(), "response": response})
                    return response

                self.phase(operation, "preparing")
                AccountReader.post_info = recording
                try:
                    with (self.directory / "prepare.stdout.json").open("x", encoding="utf-8") as output:
                        with redirect_stdout(output):
                            result = paper_main(["--config", str(config_path), "--prepare-only"])
                    if result:
                        raise ValueError("Native prepare-only failed")
                finally:
                    AccountReader.post_info = original
                    write_json(source_path, records, exclusive=True)
            verification = verify_opening(paper, read_json(source_path))
            write_json(self.directory / "opening-verification.json", verification)
            self.phase(operation, "prepared")
            return verification

    def accept(self):
        """Publish a verified baseline; only complete() releases maintenance."""
        with self.lock():
            operation = self.operation("prepared", "accepted")
            opening = read_json(self.directory / "opening-verification.json")
            current = seed_info(self.root / "_paper")
            if current["immutable_opening_hashes"] != opening["immutable_opening_hashes"]:
                raise ValueError("Immutable opening changed before acceptance")
            if operation["phase"] == "accepted":
                return read_json(self.operations / BASELINE_NAME)
            provenance = snapshot_provenance(self.project, self.directory / "accepted-source")
            seed = current["seed"]
            policy = read_json(self.root / "_paper/policy.json")
            record = {"experiment_id": self.cycle_id, "analysis_eligible": True,
                      "status": "accepted_under_maintenance", "accepted_at_utc": utc(),
                      "seed_at_utc": seed["timestamp_utc"], "opening_equity": sum(seed["baseline_equity"].values()),
                      "baseline_equity": seed["baseline_equity"], "policy_id": policy["policy_id"],
                      "opening_policy_id": policy["policy_id"],
                      "source_equity": opening["source_equity"],
                      "opening_verification_path": str(self.directory / "opening-verification.json"),
                      "source_verification_path": str(self.directory / "opening-public-account-reads.json"),
                      "immutable_opening_hashes": opening["immutable_opening_hashes"],
                      "preserved_previous_archive": operation["archive_directory"],
                      "superseded_experiment_id": operation["previous_experiment"].get("experiment_id"),
                      "evidence_directory": str(self.directory), "provenance": provenance,
                      "config_sha256": {Path(row["relative_path"]).name: row["sha256"] for row in provenance["files"]
                                        if row["relative_path"].startswith("configs/")}}
            write_json(self.root / "_paper/experiment.json", record)
            write_json(self.operations / BASELINE_NAME, record)
            self.phase(operation, "accepted")
            return record

    def complete(self, health_path):
        with self.lock():
            existing = read_json(self.operation_path)
            guard = read_json(self.guard)
            if existing.get("phase") == "completed" or (existing.get("phase") == "accepted"
                                                         and guard.get("status") == "completed"):
                deployment = read_json(self.directory / "deployment.json")
                if (guard.get("cycle_id") != self.cycle_id or guard.get("status") != "completed"
                        or deployment.get("experiment_id") != self.cycle_id):
                    raise ValueError("Completion identity mismatch")
                if existing["phase"] != "completed":
                    existing.update(phase="completed", updated_at_utc=deployment["completed_at_utc"])
                    write_json(self.operation_path, existing)
                return deployment
            operation = self.operation("accepted")
            health_path = safe_path(health_path)
            health = read_json(health_path)
            if health.get("result") != "passed" or health.get("expectation") != "trading":
                raise ValueError("Broader trading health evidence must explicitly pass before handoff")
            record = read_json(self.operations / BASELINE_NAME)
            health_age = (datetime.now(timezone.utc) - datetime.fromisoformat(health["read_at_utc"])).total_seconds()
            if not -5 <= health_age <= 300 or health.get("paper_seed_at_utc") != record["seed_at_utc"]:
                raise ValueError("Health evidence is stale or belongs to another seed")
            current = seed_info(self.root / "_paper")
            if record["experiment_id"] != self.cycle_id or current["immutable_opening_hashes"] != record["immutable_opening_hashes"]:
                raise ValueError("Accepted opening identity changed")
            if current["latest_committed_at_utc"] <= record["seed_at_utc"]:
                raise ValueError("Paper has not committed a post-opening valuation")
            for row in record["provenance"]["files"]:
                if digest(self.project / row["relative_path"]) != row["sha256"]:
                    raise ValueError(f"Accepted source/config changed: {row['relative_path']}")
            process_evidence = verify_running(self.root, self.project)
            completed = utc()
            record.update(status="running", completed_at_utc=completed, health_verification_path=str(health_path),
                          health_evidence_sha256=digest(health_path), runtime_verification=process_evidence)
            deployment_path = self.directory / "deployment.json"
            if deployment_path.exists():
                existing = read_json(deployment_path)
                if (existing["experiment_id"] != self.cycle_id
                        or existing["immutable_opening_hashes"] != record["immutable_opening_hashes"]):
                    raise ValueError("Conflicting completion receipt")
                record = existing
                completed = existing["completed_at_utc"]
            else:
                write_json(deployment_path, record, exclusive=True)
            write_json(self.root / "_paper/experiment.json", record)
            write_json(self.operations / BASELINE_NAME, record)
            receipts = child(self.directory.parent, "completed")
            receipts.mkdir(exist_ok=True)
            receipt = child(receipts, self.cycle_id + ".json")
            if not receipt.exists():
                write_json(receipt, {"experiment_id": self.cycle_id, "seed_at_utc": record["seed_at_utc"],
                                     "completed_at_utc": completed, "deployment": str(deployment_path),
                                     "deployment_sha256": digest(deployment_path)}, exclusive=True)
            guard = read_json(self.guard)
            guard.update(status="completed", phase="completed", completed_at_utc=completed, experiment_id=self.cycle_id,
                         seed_at_utc=record["seed_at_utc"], opening_equity=record["opening_equity"],
                         verification_path=str(health_path))
            write_json(self.guard, guard)
            operation.update(phase="completed", updated_at_utc=completed)
            write_json(self.operation_path, operation)
            return record


def _close(actual, expected, label, tolerance=1e-7):
    if not math.isfinite(float(actual)) or not math.isclose(float(actual), float(expected), rel_tol=1e-10, abs_tol=tolerance):
        raise ValueError(f"Opening source mismatch for {label}: {actual} versus {expected}")


def verify_opening(paper, records):
    """Reconcile quantities, historical entries and cash against exact API reads."""
    from ml.hyperliquid_paper_seed import ALIASES
    info = seed_info(paper)
    seed = info["seed"]
    if any(info["counts"].values()) or info["latest_committed_at_utc"] != seed["timestamp_utc"]:
        raise ValueError("Prepare-only opening has already traded or advanced")
    if (set(seed["baseline_equity"]) != ACCOUNTS or set(seed["metadata"].get("accounts", {})) != ACCOUNTS
            or seed["metadata"].get("seed_mode") != "mirror"):
        raise ValueError("Opening is not a complete three-account mirror")
    expected_positions, source_equity = {}, {}
    account_owners = {}
    for account, metadata in seed["metadata"]["accounts"].items():
        sources, candidates_by_kind = {}, {}
        for kind in ("clearinghouseState", "spotClearinghouseState", "userAbstraction", "frontendOpenOrders"):
            observation = metadata["read_observations"][kind]
            start, end = (datetime.fromisoformat(observation[key]) for key in ("started_at_utc", "completed_at_utc"))
            matches = [row for row in records if row["type"] == kind
                       and start <= datetime.fromisoformat(row["started_at_utc"])
                       and datetime.fromisoformat(row["completed_at_utc"]) <= end]
            if not matches:
                raise ValueError(f"Exact source observation missing/ambiguous: {account}/{kind}")
            candidates_by_kind[kind] = matches
        # Account reads run concurrently. One account's slow request can fully
        # contain another account's response, so a single interval is not an
        # identity. Require the same unique owner across *all four* intervals.
        owners = set.intersection(*({row["public_owner_sha256"] for row in matches}
                                    for matches in candidates_by_kind.values()))
        if len(owners) != 1:
            raise ValueError(f"Mixed or ambiguous account owners in opening source reads: {account}")
        account_owners[account] = owners.pop()
        for kind, candidates in candidates_by_kind.items():
            matches = [row for row in candidates if row["public_owner_sha256"] == account_owners[account]]
            if len(matches) != 1:
                raise ValueError(f"Exact source observation missing/ambiguous: {account}/{kind}")
            sources[kind] = matches[0]["response"]
        if sources["frontendOpenOrders"]:
            raise ValueError("A 1:1 opening with active real open orders needs explicit reconciliation")
        unrealized = 0.0
        for row in sources["clearinghouseState"]["assetPositions"]:
            position = row["position"]
            quantity = float(position["szi"])
            if quantity:
                expected_positions[(account, position["coin"], "perp")] = (quantity, float(position["entryPx"]))
                unrealized += float(position["unrealizedPnl"])
        spot_cash, spot_value = 0.0, 0.0
        for row in sources["spotClearinghouseState"]["balances"]:
            quantity = float(row["total"])
            if row["coin"] == "USDC":
                spot_cash += quantity
            elif quantity:
                coin = ALIASES.get(row["coin"], row["coin"])
                mark = seed["marks"][f"spot:{coin}"]
                expected_positions[(account, coin, "spot")] = (quantity, mark)
                spot_value += quantity * mark
        perp = sources["clearinghouseState"]
        perp_value = float((perp.get("marginSummary") or perp["crossMarginSummary"])["accountValue"])
        unified = sources["userAbstraction"] in {"unifiedAccount", "portfolioMargin"}
        cash = spot_cash - unrealized + (0 if unified else perp_value)
        _close(seed["cash"][account], cash, f"{account} collateral")
        source_equity[account] = spot_cash + spot_value + (0 if unified else perp_value)
        _close(metadata["source_equity"], source_equity[account], f"{account} source equity")
    if len(set(account_owners.values())) != len(ACCOUNTS):
        raise ValueError("Distinct real account owners are required")
    actual_positions = {(p["account"], p["coin"], p["kind"]): p for p in seed["positions"]}
    if actual_positions.keys() != expected_positions.keys():
        raise ValueError("Inherited source inventory differs from opening positions")
    for key, (quantity, entry) in expected_positions.items():
        _close(actual_positions[key]["quantity"], quantity, f"{key} quantity", 1e-10)
        _close(actual_positions[key]["avg_entry"], entry, f"{key} entry")
    opening = info["opening"]["state"]
    for account in ACCOUNTS:
        expected = seed["cash"][account]
        for (owner, coin, kind), (quantity, entry) in expected_positions.items():
            if owner == account:
                expected += quantity * (seed["marks"][f"{kind}:{coin}"] - (entry if kind == "perp" else 0))
        _close(seed["baseline_equity"][account], expected, f"{account} marked equity")
        _close(opening["accounts"][account]["equity"], expected, f"{account} opening observation")
        _close(opening["accounts"][account]["total_pnl"], 0, f"{account} opening P/L")
    for field in ("fees", "funding", "total_pnl"):
        _close(opening["pooled"][field], 0, f"opening {field}")
    return {"result": "passed", "verified_at_utc": utc(), "seed_at_utc": seed["timestamp_utc"],
            "baseline_equity": seed["baseline_equity"], "opening_equity": sum(seed["baseline_equity"].values()),
            "source_equity": source_equity, "public_owner_sha256": account_owners,
            "counts": info["counts"], "position_count": len(expected_positions),
            "immutable_opening_hashes": info["immutable_opening_hashes"],
            "valuation_note": "Exact source quantities/collateral; sequential account reads and market marks can differ from displayed live totals."}


def verify_running(root, project):
    processes = runtime_processes()
    if any(row["module"] == "ml.hyperliquid_powder_runtime" for row in processes):
        raise ValueError("Powder became active during Paper maintenance")
    verified = {}
    for role, (relative, _, module, config_name) in RUNTIMES.items():
        control = root / relative
        state = read_json(control / "status.json")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["updated_at_utc"])).total_seconds()
        if (state.get("status") != "running" or not -5 <= age <= 120
                or state.get("last_error") or state.get("config_error") or (control / "stop.request").exists()):
            raise ValueError(f"{role} runtime is not clean and fresh")
        matches = [row for row in processes if row["pid"] == state.get("pid") and row["module"] == module]
        if len(matches) != 1:
            raise ValueError(f"{role} runtime process identity is unverified")
        process = psutil.Process(state["pid"])
        command = matches[0]["cmdline"]
        config = project / "configs" / config_name
        if safe_path(process.cwd()) != project or safe_path(state["config_path"]) != config:
            raise ValueError(f"{role} cwd/config identity mismatch")
        if "--config" not in command or safe_path(command[command.index("--config") + 1]) != config:
            raise ValueError(f"{role} explicit command config is unverified")
        verified[role] = {"pid": state["pid"], "module": module, "config": str(config),
                          "created_at_utc": datetime.fromtimestamp(process.create_time(), timezone.utc).isoformat(),
                          "observed_at_utc": state["updated_at_utc"]}
    return verified


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("bootstrap", "begin", "archive", "prepare", "accept", "complete"))
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--root", type=Path, default=Path("C:/DATASTORE/hyperliquid"))
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--health-evidence", "--health-report", type=Path)
    args = parser.parse_args(argv)
    if args.stage == "complete" and not args.health_evidence:
        parser.error("complete requires --health-evidence JSON with result=passed")
    cycle = ReviewCycle(args.root, args.project, args.cycle_id)
    result = cycle.complete(args.health_evidence) if args.stage == "complete" else getattr(cycle, args.stage)()
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
