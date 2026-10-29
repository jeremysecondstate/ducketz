"""Compact explicit, quiescent raw snapshots without changing their identity.

Reader and quiescence receipts are caller-reviewed evidence, not automatic
process detection. The caller must establish that every consumer supports the
installed chunk decoder and that the selected objects have no active consumers
or writers. This helper never opens databases or invokes application runtimes.
Raw bytes remain until an independently verified immutable manifest exists.
An interrupted removal keeps its original in a durable, resumable staging path.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.intraday.prediction import encoded
from ml.intraday.snapshot_objects import (
    CHUNK_THRESHOLD, MANIFEST_VERSION, MAX_CHUNK_BYTES, MAX_CHUNKS,
    MAX_MANIFEST_BYTES, MAX_SNAPSHOT_BYTES,
)
from tools.intraday_storage_migration import (
    MigrationBusy, MigrationError, OBJECT_DIRECTORY, _HASH, _SafeObjects,
    _atomic, _binding, _json, _plain, _sha, _within, maintenance_lock,
)

SCHEMA = "intraday-raw-snapshot-compaction-v1"
STATE_DIRECTORY = "state/intraday-storage-manager/snapshot-compaction"


class CompactionError(MigrationError):
    pass


class TimeLimit(CompactionError):
    pass


class ByteLimit(CompactionError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _check_time(deadline):
    if time.monotonic() >= deadline:
        raise TimeLimit("Time bound reached; retained work can resume")


def _signature(path):
    info = _plain(path).stat()
    if not stat.S_ISREG(info.st_mode):
        raise CompactionError("Expected a regular file")
    return {key: getattr(info, key) for key in
            ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}


def _read(path, bound):
    if _signature(path)["st_size"] > bound:
        raise CompactionError("File exceeds resource bound")
    with _plain(path).open("rb") as stream:
        raw = stream.read(bound + 1)
    if len(raw) > bound:
        raise CompactionError("File changed or exceeds resource bound")
    return raw


def _allocated(path):
    """Best-effort file allocation estimate, never a volume-space claim."""
    info = _plain(path).stat()
    # Removing one name cannot release a file with other hard links.
    if info.st_nlink > 1:
        return 0
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True).GetCompressedFileSizeW
        api.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
        api.restype = wintypes.DWORD
        high = wintypes.DWORD()
        ctypes.set_last_error(0)
        low = api(str(path), ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.get_last_error():
            return None
        return (high.value << 32) | low
    return info.st_blocks * 512 if hasattr(info, "st_blocks") else None


def _evidence(root, identities, value):
    if not isinstance(value, dict) or set(value) != {"identities", "reader", "quiescence"}:
        raise CompactionError("Apply requires reader and quiescence evidence scoped to identities")
    allowed = value["identities"]
    if (not isinstance(allowed, list) or not 0 < len(allowed) <= 10000
            or any(not isinstance(item, str) or not _HASH.fullmatch(item) for item in allowed)
            or not set(identities).issubset(allowed)):
        raise CompactionError("Evidence does not cover the selected snapshot identities")
    for kind in ("reader", "quiescence"):
        _binding(root, kind, value)
    return value


def _verify_manifest(objects, identity, *, expected_raw=None, expected_size=None,
                     max_size=MAX_SNAPSHOT_BYTES, deadline):
    """Verify chunks independently; SnapshotObjects.read would prefer retained raw."""
    _check_time(deadline)
    manifest = _json(_read(objects._manifest_path(identity), MAX_MANIFEST_BYTES))
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema", "sha256", "bytes", "chunks"}
            or manifest["schema"] != MANIFEST_VERSION or manifest["sha256"] != identity
            or type(manifest["bytes"]) is not int or not 0 <= manifest["bytes"] <= MAX_SNAPSHOT_BYTES
            or expected_size is not None and manifest["bytes"] != expected_size
            or expected_raw is not None and manifest["bytes"] != len(expected_raw)
            or not isinstance(manifest["chunks"], list)
            or not 0 < len(manifest["chunks"]) <= MAX_CHUNKS):
        raise CompactionError("Invalid snapshot manifest")
    if manifest["bytes"] > max_size:
        raise ByteLimit("Snapshot manifest exceeds remaining byte budget")
    digest, offset = hashlib.sha256(), 0
    for descriptor in manifest["chunks"]:
        _check_time(deadline)
        if (not isinstance(descriptor, dict) or set(descriptor) != {"sha256", "bytes"}
                or not isinstance(descriptor["sha256"], str)
                or not _HASH.fullmatch(descriptor["sha256"])
                or type(descriptor["bytes"]) is not int
                or not 0 < descriptor["bytes"] <= MAX_CHUNK_BYTES
                or offset + descriptor["bytes"] > manifest["bytes"]):
            raise CompactionError("Invalid snapshot chunk descriptor")
        raw = _read(objects._chunk_path(descriptor["sha256"]), descriptor["bytes"])
        if len(raw) != descriptor["bytes"] or _sha(raw) != descriptor["sha256"]:
            raise CompactionError("Snapshot chunk changed or is incomplete")
        if expected_raw is not None and expected_raw[offset:offset + len(raw)] != raw:
            raise CompactionError("Manifest does not reconstruct original raw bytes")
        digest.update(raw)
        offset += len(raw)
    if offset != manifest["bytes"] or digest.hexdigest() != identity:
        raise CompactionError("Manifest does not reconstruct original identity")
    return manifest


def _publish(objects, identity, raw, deadline, account):
    """Publish immutable chunks first. Never trust a manifest hidden by raw."""
    manifest_path = objects._manifest_path(identity)
    if manifest_path.exists():
        _verify_manifest(objects, identity, expected_raw=raw, deadline=deadline)
        return {"new_bytes": 0, "new_allocation_estimate": 0}
    payload = _json(raw)
    pieces = objects._plan(raw, payload)
    if pieces is None:
        raise CompactionError("Only exact canonical JSON objects support chunk compaction")
    descriptors = [{"sha256": _sha(piece), "bytes": len(piece)} for piece in pieces]
    manifest = encoded({"schema": MANIFEST_VERSION, "sha256": identity,
                        "bytes": len(raw), "chunks": descriptors}).encode()
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise CompactionError("Manifest exceeds resource bound")
    new_bytes, allocation = 0, 0
    publications = [(objects._chunk_path(item["sha256"]), piece, MAX_CHUNK_BYTES)
                    for item, piece in zip(descriptors, pieces)]
    publications.append((manifest_path, manifest, MAX_MANIFEST_BYTES))
    for path, content, bound in publications:
        _check_time(deadline)
        existed = path.exists()
        objects._publish(path, content, bound)
        if not existed:
            new_bytes += len(content)
            measured = _allocated(path)
            allocation = None if allocation is None or measured is None else allocation + measured
            account(len(content), measured)
    _verify_manifest(objects, identity, expected_raw=raw, deadline=deadline)
    return {"new_bytes": new_bytes, "new_allocation_estimate": allocation}


def _journal(root, identity):
    return _within(root, f"{STATE_DIRECTORY}/transactions/{identity}.json")


def _load_journal(root, identity):
    path = _journal(root, identity)
    if not path.exists():
        return None
    value = _json(_read(path, 64 * 1024))
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or value.get("identity") != identity
            or value.get("status") not in {"verified", "staged", "complete", "source_changed"}
            or not isinstance(value.get("staging"), str)
            or not value["staging"].startswith(f"{STATE_DIRECTORY}/staging/{identity}-")
            or not value["staging"].endswith(".raw")):
        raise CompactionError("Invalid compaction transaction journal")
    _within(root, value["staging"])
    return value


def _save(root, journal):
    _atomic(_journal(root, journal["identity"]), journal)


def _restore(staged, raw_path):
    """Restore a raced source without replacing any newly published raw file."""
    _plain(staged)
    _plain(raw_path)
    try:
        os.link(staged, raw_path)
    except FileExistsError:
        return False
    staged.unlink()
    return True


def _remove(root, objects, identity, raw, source_signature, journal, evidence, deadline):
    raw_path = objects._path(identity)
    staged = _within(root, journal["staging"])
    _check_time(deadline)
    _evidence(root, [identity], evidence)
    _verify_manifest(objects, identity, expected_raw=raw, deadline=deadline)
    if not staged.exists():
        if _signature(raw_path) != source_signature or _read(raw_path, len(raw)) != raw:
            raise CompactionError("Raw source changed before staging; retained")
        _check_time(deadline)
        staged.parent.mkdir(parents=True, exist_ok=True)
        _plain(staged)
        # The destination is unique and durable in the journal before this move.
        # Verify the moved bytes again: a race must never delete a replacement.
        os.rename(raw_path, staged)
    moved = _signature(staged)
    # Rename can change ctime, so compare stable file identity, length and mtime.
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (any(moved[key] != source_signature[key] for key in stable)
            or _read(staged, len(raw)) != raw):
        restored = _restore(staged, raw_path)
        journal.update(status="source_changed", restored=restored)
        _save(root, journal)
        raise CompactionError("Raw source raced with staging; changed bytes preserved")
    journal["status"] = "staged"
    _save(root, journal)
    _check_time(deadline)
    _evidence(root, [identity], evidence)
    try:
        _verify_manifest(objects, identity, expected_raw=raw, deadline=deadline)
    except (MigrationError, ValueError, OSError):
        # A detected broken replacement can still recover the original name.
        # Never overwrite a raw file recreated by an unexpected writer.
        if _restore(staged, raw_path):
            journal["restored_after_manifest_failure"] = True
            _save(root, journal)
        raise
    # Recheck immediately before deletion; quiescence is still a caller promise.
    if _signature(staged) != moved or _read(staged, len(raw)) != raw:
        raise CompactionError("Staged source changed; retained for review")
    _check_time(deadline)
    allocation = _allocated(staged)
    _plain(staged).unlink()
    journal.update(status="complete", completed_at=_now(), raw_bytes_removed=len(raw),
                   raw_allocation_removed_estimate=allocation)
    _save(root, journal)
    return allocation


def compact(datastore, identities, *, apply=False, quiescent=False, evidence=None,
            max_objects=8, max_bytes=512 * 1024 * 1024, max_seconds=20):
    """One bounded, durable pass over explicit SHA-256s, with dry-run default.

    Allocation figures are estimates for removed raw names and newly published
    chunks/manifests. They exclude small maintenance receipts and cannot prove
    changes to physical volume free space; the first chunk seed may cost space.
    No source deletion occurs without explicit apply, quiescence and evidence.
    Pass the receipt's pending_identities to the next bounded pass. Time limits
    are cooperative checkpoints; one bounded JSON parse/encode can overrun.
    """
    if (type(max_objects) is not int or not 1 <= max_objects <= 10000
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SNAPSHOT_BYTES
            or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds) or not 0 < max_seconds <= 300
            or type(apply) is not bool or type(quiescent) is not bool):
        raise CompactionError("Invalid compaction bounds or flags")
    if (not isinstance(identities, (list, tuple)) or not 0 < len(identities) <= 10000
            or any(not isinstance(item, str) or not _HASH.fullmatch(item) for item in identities)
            or len(set(identities)) != len(identities)):
        raise CompactionError("Provide a bounded unique list of exact snapshot SHA-256 identities")
    if not Path(datastore).is_absolute():
        raise CompactionError("DATASTORE must be an absolute directory")
    root = _plain(datastore)
    if not root.is_absolute() or not root.is_dir():
        raise CompactionError("DATASTORE must be an existing directory")
    if apply:
        if not quiescent:
            raise CompactionError("Apply requires explicit caller-reviewed quiescence")
        _evidence(root, identities, evidence)
    deadline = time.monotonic() + max_seconds
    run = {"schema": SCHEMA, "run_id": uuid.uuid4().hex, "started_at": _now(),
           "apply": apply, "selected_identities": list(identities), "results": [],
           "raw_bytes_removed": 0, "new_object_bytes": 0,
           "raw_allocation_removed_estimate": 0, "new_object_allocation_estimate": 0,
           "filesystem_bytes_reclaimed": None,
           "allocation_note": "Estimates exclude receipts; physical volume savings are not measured.",
           "evidence": evidence if apply else None}
    with maintenance_lock(root):
        objects = _SafeObjects(_within(root, OBJECT_DIRECTORY))
        consumed = 0
        for identity in identities[:max_objects]:
            result = {"identity": identity, "status": "PENDING"}
            run["results"].append(result)
            try:
                _check_time(deadline)
                if apply:
                    _evidence(root, [identity], evidence)
                journal = _load_journal(root, identity)
                raw_path = objects._path(identity)
                staged = _within(root, journal["staging"]) if journal else None
                source = staged if staged is not None and staged.exists() else raw_path
                if not source.exists():
                    manifest = _verify_manifest(objects, identity, max_size=max_bytes - consumed,
                                                deadline=deadline)
                    consumed += manifest["bytes"]
                    result["source_bytes"] = manifest["bytes"]
                    result["status"] = "ALREADY_CHUNKED"
                    if apply and journal and journal["status"] != "complete":
                        journal.update(status="complete", completed_at=_now(),
                                       note="Removal completed before receipt; savings not counted again")
                        _save(root, journal)
                    continue
                signature = _signature(source)
                size = signature["st_size"]
                result["source_bytes"] = size
                if size > max_bytes - consumed:
                    result["status"] = "BYTE_LIMIT"
                    break
                consumed += size
                raw = _read(source, size)
                if _signature(source) != signature or _sha(raw) != identity:
                    raise CompactionError("Raw snapshot changed or does not match original identity")
                if len(raw) < CHUNK_THRESHOLD:
                    result["status"] = "BELOW_CHUNK_THRESHOLD"
                    continue
                _check_time(deadline)
                if not apply:
                    if objects._manifest_path(identity).exists():
                        _verify_manifest(objects, identity, expected_raw=raw, deadline=deadline)
                    elif objects._plan(raw, _json(raw)) is None:
                        raise CompactionError("Only exact canonical JSON objects support chunk compaction")
                    result["status"] = "WOULD_COMPACT"
                    continue
                def account(size, allocation):
                    run["new_object_bytes"] += size
                    run["new_object_allocation_estimate"] = _sum_estimate(
                        run["new_object_allocation_estimate"], allocation)
                added = _publish(objects, identity, raw, deadline, account)
                result.update(added)
                if staged is not None and staged.exists():
                    original_signature = journal["source_signature"]
                else:
                    original_signature = signature
                    journal = {"schema": SCHEMA, "identity": identity, "status": "verified",
                               "created_at": _now(), "source_signature": signature,
                               "staging": f"{STATE_DIRECTORY}/staging/{identity}-{uuid.uuid4().hex}.raw"}
                    _save(root, journal)
                reclaimed = _remove(root, objects, identity, raw, original_signature,
                                    journal, evidence, deadline)
                run["raw_bytes_removed"] += len(raw)
                run["raw_allocation_removed_estimate"] = _sum_estimate(
                    run["raw_allocation_removed_estimate"], reclaimed)
                result["status"] = "COMPACTED"
                result["raw_allocation_removed_estimate"] = reclaimed
                if raw_path.exists():
                    result["status"] = "COMPACTED_SOURCE_REAPPEARED"
                    result["note"] = "New canonical raw retained; caller must review concurrent writer"
                    break
            except TimeLimit as exc:
                result.update(status="TIME_LIMIT", error=str(exc))
                break
            except ByteLimit as exc:
                result.update(status="BYTE_LIMIT", error=str(exc))
                break
            except (MigrationError, ValueError, OSError, KeyError, TypeError) as exc:
                result.update(status="PENDING_REVIEW", error=str(exc))
                # Preserve this exact object; independent selected objects may proceed.
            finally:
                run["finished_at"] = _now()
                run["source_bytes_reviewed"] = consumed
                left, right = run["raw_allocation_removed_estimate"], run["new_object_allocation_estimate"]
                run["net_object_allocation_reclaimed_estimate"] = (
                    left - right if left is not None and right is not None else None)
                receipt = _within(root, f"{STATE_DIRECTORY}/runs/{run['run_id']}.json")
                _atomic(receipt, run)
                _atomic(_within(root, f"{STATE_DIRECTORY}/latest.json"),
                        {"run_id": run["run_id"], "receipt": str(receipt)})
        terminal = {"COMPACTED", "ALREADY_CHUNKED", "BELOW_CHUNK_THRESHOLD", "WOULD_COMPACT"}
        finished = {row["identity"] for row in run["results"] if row["status"] in terminal}
        run["pending_identities"] = [item for item in identities if item not in finished]
        _atomic(receipt, run)
    return run


def _sum_estimate(left, right):
    return left + right if left is not None and right is not None else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", required=True)
    parser.add_argument("--identity", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--quiescent", action="store_true")
    parser.add_argument("--evidence", help="JSON with identities, reader and quiescence bindings")
    parser.add_argument("--max-objects", type=int, default=8)
    parser.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=float, default=20)
    args = parser.parse_args()
    try:
        evidence = _json(_read(Path(args.evidence), 4 * 1024 * 1024)) if args.evidence else None
        result = compact(args.datastore, args.identity, apply=args.apply,
                         quiescent=args.quiescent, evidence=evidence,
                         max_objects=args.max_objects, max_bytes=args.max_bytes,
                         max_seconds=args.max_seconds)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1 if any(row["status"] == "PENDING_REVIEW" for row in result["results"]) else 0
    except (MigrationError, ValueError, OSError) as exc:
        print(json.dumps({"status": "BUSY" if isinstance(exc, MigrationBusy) else "REJECTED",
                          "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
