"""Bounded inventory and exact immutable-file consolidation; no trading imports.

This utility never deletes unique content or edits a database. Known immutable
hash-named payloads may share one physical NTFS/file-system object while every
existing reader path remains valid. Chat ownership is checked by the invoking
scheduled task; a native maintenance lock also serializes local applications.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid


VERSION = "atlas-datastore-immutable-consolidation-v1"
HASH_NAME = re.compile(r"([0-9a-f]{64})\.(?:json|bin)$")
OBJECT_ROOTS = (
    "intraday/source-objects", "intraday/acquisition-source-objects",
    "intraday/system-1a/acquisition-source-objects",
    "intraday/system-1a/acquisition-receipts",
    "intraday/system-1a/ready/snapshots", "intraday/system-1b/evidence",
    "intraday/market-inputs/raw", "intraday/market-inputs/objects",
    "intraday/gap-recovery/responses", "intraday/gap-recovery/candles",
)
RECOVERY_ROOT = "state/intraday-storage-manager/exact-file-sharing"


def now():
    return datetime.now(timezone.utc).isoformat()


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def safe_root(value):
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Datastore must be an existing absolute directory")
    for part in (root, *root.parents):
        if _reparse(part.lstat()):
            raise ValueError("Reparse/symlink datastore roots are not supported")
    return root.resolve()


def safe_file(root, relative):
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Expected a contained relative object path")
    candidate = root / path
    for entry in (candidate, *candidate.parents):
        if entry == root:
            break
        if _reparse(entry.lstat()):
            raise ValueError("Reparse/symlink object paths are not supported")
    if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
        raise ValueError("Object escaped datastore or is not a regular file")
    return candidate


def allowed_object(relative):
    path = Path(relative)
    name = path.as_posix()
    return (HASH_NAME.fullmatch(path.name) is not None
            and "manifests" not in path.parts
            and any(name.startswith(prefix + "/") for prefix in OBJECT_ROOTS))


def signature(path):
    info = path.stat()
    return {"bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev, "inode": info.st_ino, "links": info.st_nlink}


def file_hash(path, *, deadline=None):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Immutable hash budget reached")
            block = stream.read(1024 * 1024)
            if not block:
                return result.hexdigest()
            result.update(block)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recovery_path(root, relative):
    """A contained state path; absent final components are permitted."""
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Expected a contained recovery path")
    candidate = root / path
    for entry in (*reversed(candidate.parents), candidate):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if _reparse(info):
            raise ValueError("Recovery path cannot be redirected")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Recovery path escaped datastore")
    return candidate


def _restore_original(root, staged, duplicate):
    """Keep a replacement writer's file and retain the original on collision."""
    recovery_path(root, staged.relative_to(root))
    recovery_path(root, duplicate.relative_to(root))
    if not staged.exists():
        return False
    try:
        os.link(staged, duplicate)
    except FileExistsError:
        return False
    staged.unlink()
    return True


def reviewed_quiescence(proposal, evidence):
    expected = {item[key] for item in proposal["pairs"] for key in ("canonical", "duplicate")}
    if (not isinstance(evidence, dict) or set(evidence) != {"paths", "evidence", "evidence_sha256"}
            or not isinstance(evidence["paths"], list)
            or any(not isinstance(path, str) for path in evidence["paths"])
            or set(evidence["paths"]) != expected
            or not isinstance(evidence["evidence"], str)
            or not re.fullmatch("[a-f0-9]{64}", str(evidence["evidence_sha256"]))):
        raise ValueError("Apply requires exact hashed quiescence evidence scoped to every planned path")
    path = Path(evidence["evidence"])
    if not path.is_absolute():
        raise ValueError("Quiescence evidence must be an absolute path")
    for entry in (path, *path.parents):
        if _reparse(entry.lstat()):
            raise ValueError("Quiescence evidence cannot be redirected")
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Quiescence evidence must be a bounded regular file")
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != evidence["evidence_sha256"]:
        raise ValueError("Quiescence evidence changed")


@contextmanager
def maintenance_lock(root):
    directory = recovery_path(root, "locks")
    directory.mkdir(exist_ok=True)
    path = recovery_path(root, "locks/intraday-storage-manager.lock")
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def inventory(datastore, *, max_files=1000000, max_seconds=60):
    root = safe_root(datastore)
    deadline = time.monotonic() + max_seconds
    groups, objects, errors = defaultdict(lambda: [0, 0]), defaultdict(list), []
    stack, count, complete = [root], 0, True
    while stack:
        if time.monotonic() >= deadline or count >= max_files:
            complete = False
            break
        # Start with the active trader's payloads even on a bounded inventory.
        # Metadata for the remaining datastore is still included when time allows.
        stack.sort(key=lambda p: p.relative_to(root).parts[:1] == ("intraday",))
        directory = stack.pop()
        try:
            if _reparse(directory.lstat()):
                errors.append({"path": directory.relative_to(root).as_posix(), "reason": "reparse_skipped"})
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline or count >= max_files:
                        complete = False
                        break
                    path = Path(entry.path)
                    info = entry.stat(follow_symlinks=False)
                    relative = path.relative_to(root).as_posix()
                    if _reparse(info):
                        errors.append({"path": relative, "reason": "reparse_skipped"})
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        count += 1
                        key = "/".join(Path(relative).parts[:2])
                        groups[key][0] += info.st_size
                        groups[key][1] += 1
                        if allowed_object(relative):
                            identity = HASH_NAME.fullmatch(path.name).group(1)
                            objects[(identity, info.st_size)].append(relative)
        except OSError as exc:
            errors.append({"path": directory.relative_to(root).as_posix(), "reason": type(exc).__name__})
    candidates = []
    for (identity, size), paths in sorted(objects.items()):
        if len(paths) > 1:
            candidates.append({"sha256": identity, "bytes": size, "paths": sorted(paths)})
    return {"schema": VERSION, "at": now(), "datastore": str(root),
            "coverage_complete": complete and not errors, "files_examined": count,
            "groups": dict(groups), "candidate_groups": candidates, "errors": errors,
            "disk_free_bytes": shutil.disk_usage(root).free,
            "limitation": "Metadata inventory; same hash filename is only a candidate. Database payloads and non-hash files are not deduplicated by this utility."}


def plan(datastore, scan, *, max_pairs=500, max_hash_bytes=8 * 1024**3, max_seconds=120):
    root = safe_root(datastore)
    if scan.get("schema") != VERSION or scan.get("datastore") != str(root):
        raise ValueError("Inventory belongs to a different datastore/protocol")
    deadline = time.monotonic() + max_seconds
    pairs, rejected, hashed, complete = [], [], 0, True
    for group in scan["candidate_groups"]:
        paths = sorted(group["paths"], key=lambda p: (next((i for i, prefix in enumerate(OBJECT_ROOTS)
                                                       if p.startswith(prefix + "/")), len(OBJECT_ROOTS)), p))
        canonical = None
        for relative in paths:
            if len(pairs) >= max_pairs or time.monotonic() >= deadline:
                complete = False
                break
            try:
                if not allowed_object(relative):
                    raise ValueError("Not an approved immutable object")
                path = safe_file(root, relative)
                before = signature(path)
                if canonical is not None and os.path.samefile(path, root / canonical[0]):
                    continue
                if hashed + before["bytes"] > max_hash_bytes:
                    complete = False
                    break
                actual = file_hash(path, deadline=deadline)
                hashed += before["bytes"]
                if actual != group["sha256"] or before != signature(path):
                    raise ValueError("Object hash or stable bytes do not match")
                if canonical is None:
                    canonical = (relative, before)
                else:
                    pairs.append({"canonical": canonical[0], "duplicate": relative,
                                  "sha256": actual, "canonical_before": canonical[1], "duplicate_before": before})
            except (OSError, ValueError) as exc:
                rejected.append({"path": relative, "reason": type(exc).__name__ + ": " + str(exc)})
        if not complete:
            break
    return {"schema": VERSION, "at": now(), "datastore": str(root), "pairs": pairs,
            "hashed_bytes": hashed, "coverage_complete": complete and scan["coverage_complete"] and not rejected,
            "rejected": rejected, "action": "replace verified duplicate immutable file with hardlink; preserve every reader path"}


def apply(datastore, proposal, *, max_pairs=500, max_seconds=120,
          quiescent=False, quiescence_evidence=None):
    root = safe_root(datastore)
    if type(quiescent) is not bool or not quiescent:
        raise ValueError("Apply requires caller-reviewed quiescence of selected immutable paths")
    if (type(max_pairs) is not int or not 0 < max_pairs <= 10000
            or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds) or not 0 < max_seconds <= 300):
        raise ValueError("Invalid apply bounds")
    if proposal.get("schema") != VERSION or proposal.get("datastore") != str(root):
        raise ValueError("Plan belongs to a different datastore/protocol")
    reviewed_quiescence(proposal, quiescence_evidence)
    results, deadline = [], time.monotonic() + max_seconds
    with maintenance_lock(root):
        for item in proposal["pairs"][:max_pairs]:
            if time.monotonic() >= deadline:
                break
            temporary = staged = journal = receipt = None
            try:
                reviewed_quiescence(proposal, quiescence_evidence)
                for key in ("canonical", "duplicate"):
                    if not allowed_object(item[key]):
                        raise ValueError("Unsupported immutable object path")
                    if HASH_NAME.fullmatch(Path(item[key]).name).group(1) != item["sha256"]:
                        raise ValueError("Object filename differs from reviewed content identity")
                pair_id = hashlib.sha256(json.dumps(
                    [item["canonical"], item["duplicate"], item["sha256"]]).encode()).hexdigest()
                receipt = recovery_path(root, f"{RECOVERY_ROOT}/transactions/{pair_id}.json")
                staged = recovery_path(root, f"{RECOVERY_ROOT}/staging/{pair_id}.raw")
                # A crash may leave the exact original retained at its journaled
                # path. Restore only into an absent reader name, never overwrite.
                if staged.exists():
                    duplicate = recovery_path(root, item["duplicate"])
                    canonical = safe_file(root, item["canonical"])
                    if duplicate.exists():
                        duplicate = safe_file(root, item["duplicate"])
                        before = signature(staged)
                        if (os.path.samefile(canonical, duplicate)
                                and file_hash(duplicate, deadline=deadline) == item["sha256"]
                                and file_hash(staged, deadline=deadline) == item["sha256"]
                                and signature(staged) == before):
                            reviewed_quiescence(proposal, quiescence_evidence)
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Recovery time bound reached; original retained")
                            staged.unlink()
                            atomic_json(receipt, {"schema": VERSION, "at": now(), "status": "complete",
                                                  "pair": item, "recovered": True,
                                                  "quiescence_evidence": quiescence_evidence})
                            results.append({"duplicate": item["duplicate"], "status": "shared_recovered",
                                            "recovery_receipt": str(receipt),
                                            "reclaimed_bytes_estimate": before["bytes"] if before["links"] == 1 else 0})
                            continue
                    restored = _restore_original(root, staged, duplicate)
                    results.append({"duplicate": item["duplicate"], "status": "pending",
                                    "reason": "Retained prior original restored; replan" if restored else
                                              "Retained prior original requires review; current reader path preserved",
                                    "recovery_receipt": str(receipt), "retained_original": str(staged) if staged.exists() else None,
                                    "reclaimed_bytes_estimate": 0})
                    continue
                canonical, duplicate = (safe_file(root, item[key]) for key in ("canonical", "duplicate"))
                if os.path.samefile(canonical, duplicate):
                    results.append({"duplicate": item["duplicate"], "status": "already_shared", "reclaimed_bytes_estimate": 0})
                    continue
                # Link count legitimately increases when earlier pairs are shared.
                for path, key in ((canonical, "canonical_before"), (duplicate, "duplicate_before")):
                    actual = signature(path)
                    if any(actual[field] != item[key][field] for field in ("bytes", "mtime_ns", "device", "inode")):
                        raise ValueError("Object changed since reviewed plan")
                    if file_hash(path, deadline=deadline) != item["sha256"]:
                        raise ValueError("Object no longer matches reviewed hash")
                duplicate_before = signature(duplicate)
                temporary = duplicate.with_name("." + uuid.uuid4().hex + ".link")
                os.link(canonical, temporary)
                if (signature(duplicate) != duplicate_before or not os.path.samefile(temporary, canonical)
                        or file_hash(temporary, deadline=deadline) != item["sha256"]):
                    raise ValueError("Object changed while preparing consolidation")
                journal = {"schema": VERSION, "at": now(), "status": "prepared", "pair": item,
                           "quiescence_evidence": quiescence_evidence,
                           "staged_original": str(staged), "temporary_link": str(temporary)}
                atomic_json(receipt, journal)
                staged.parent.mkdir(parents=True, exist_ok=True)
                recovery_path(root, staged.relative_to(root))
                os.rename(duplicate, staged)
                after = signature(staged)
                if (any(after[field] != duplicate_before[field] for field in
                        ("bytes", "mtime_ns", "device", "inode"))
                        or file_hash(staged, deadline=deadline) != item["sha256"]):
                    raise ValueError("Duplicate changed while staging; changed original retained")
                if file_hash(temporary, deadline=deadline) != item["sha256"]:
                    raise ValueError("Canonical changed before publication; original retained")
                # No-overwrite publication preserves a new concurrent writer.
                os.link(temporary, duplicate)
                if (not os.path.samefile(canonical, duplicate)
                        or file_hash(duplicate, deadline=deadline) != item["sha256"]):
                    raise ValueError("Consolidation identity verification failed")
                if signature(staged) != after or file_hash(staged, deadline=deadline) != item["sha256"]:
                    raise ValueError("Retained original changed; preserved for review")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Maintenance time bound reached; retained original preserved")
                reviewed_quiescence(proposal, quiescence_evidence)
                staged.unlink()
                journal.update(status="complete", completed_at=now())
                atomic_json(receipt, journal)
                results.append({"duplicate": item["duplicate"], "canonical": item["canonical"],
                                "sha256": item["sha256"], "status": "shared",
                                "recovery_receipt": str(receipt),
                                "reclaimed_bytes_estimate": duplicate_before["bytes"] if duplicate_before["links"] == 1 else 0})
            except (OSError, ValueError) as exc:
                restored = False
                if staged is not None and staged.exists():
                    try:
                        restored = _restore_original(root, staged, recovery_path(root, item["duplicate"]))
                    except (OSError, ValueError):
                        pass
                if journal is not None:
                    journal.update(status="pending", reason=str(exc), original_restored=restored,
                                   retained_original=str(staged) if staged.exists() else None)
                    atomic_json(receipt, journal)
                results.append({"duplicate": item.get("duplicate"), "status": "pending",
                                "reason": type(exc).__name__ + ": " + str(exc), "reclaimed_bytes_estimate": 0,
                                "recovery_receipt": str(receipt) if receipt is not None else None,
                                "retained_original": str(staged) if staged is not None and staged.exists() else None})
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    return {"schema": VERSION, "at": now(), "datastore": str(root), "results": results,
            "reclaimed_bytes_estimate": sum(row["reclaimed_bytes_estimate"] for row in results),
            "completed_pairs": len(results), "planned_pairs": len(proposal["pairs"]),
            "disk_free_bytes": shutil.disk_usage(root).free,
            "limitation": "Reclaimed bytes are a logical last-link estimate, not a whole-datastore zero-duplication claim; compression and concurrent writes affect disk-free measurements."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory", "plan", "apply"))
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--quiescent", action="store_true",
                        help="Caller reviewed that selected paths have no active readers/writers")
    parser.add_argument("--quiescence-evidence", type=Path,
                        help="JSON binding with exact planned paths, evidence absolute path and evidence_sha256")
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--max-files", type=int, default=1000000)
    parser.add_argument("--max-pairs", type=int, default=500)
    parser.add_argument("--max-hash-bytes", type=int, default=8 * 1024**3)
    args = parser.parse_args()
    if min(args.max_seconds, args.max_files, args.max_pairs, args.max_hash_bytes) <= 0:
        parser.error("Resource bounds must be positive")
    if args.action == "inventory":
        result = inventory(args.datastore, max_files=args.max_files, max_seconds=args.max_seconds)
    else:
        if args.input is None:
            parser.error("--input is required for a saved inventory or plan")
        value = json.loads(args.input.read_text(encoding="utf-8-sig"))
        if args.action == "plan":
            result = plan(args.datastore, value, max_pairs=args.max_pairs,
                          max_hash_bytes=args.max_hash_bytes, max_seconds=args.max_seconds)
        else:
            if not args.reviewed:
                parser.error("Apply requires review of the exact saved plan")
            if not args.quiescent:
                parser.error("Apply requires reviewed quiescence of the selected immutable paths")
            if args.quiescence_evidence is None:
                parser.error("--quiescence-evidence is required")
            if args.quiescence_evidence.stat().st_size > 4 * 1024 * 1024:
                parser.error("Evidence binding exceeds its resource bound")
            evidence = json.loads(args.quiescence_evidence.read_text(encoding="utf-8-sig"))
            result = apply(args.datastore, value, max_pairs=args.max_pairs,
                           max_seconds=args.max_seconds, quiescent=args.quiescent,
                           quiescence_evidence=evidence)
    atomic_json(args.output, result)
    print(json.dumps({key: value for key, value in result.items()
                      if key not in {"groups", "candidate_groups", "pairs", "results"}}, indent=2))


if __name__ == "__main__":
    main()
