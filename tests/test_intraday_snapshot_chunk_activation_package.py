"""Validate the exact opt-in snapshot chunk adapter packet without live operations."""
from copy import deepcopy
import ast
import hashlib
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "docs/development/patches/intraday-snapshot-chunk-activation.json"
TARGETS = ("ml/intraday/data_renewal.py", "ml/intraday/split_service.py")
DEPENDENCIES = ("ml/intraday/snapshot_objects.py", "tests/test_intraday_snapshot_chunk_activation.py")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_hunks(raw):
    """Reject other paths, malformed counts, overlapping ranges and extra data."""
    assert isinstance(raw, bytes) and b"\r" not in raw
    raw.decode("utf-8")
    lines = raw.splitlines(keepends=True)
    index, result = 0, {}
    for target in TARGETS:
        headers = [("--- a/" + target + "\n").encode(), ("+++ b/" + target + "\n").encode()]
        assert lines[index:index+2] == headers
        index += 2
        hunks, old_end, new_end = [], 0, 0
        while index < len(lines) and lines[index].startswith(b"@@ "):
            match = re.fullmatch(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", lines[index])
            assert match is not None
            old_start = int(match[1]); old_count = int(match[2] or b"1")
            new_start = int(match[3]); new_count = int(match[4] or b"1")
            assert old_start >= old_end and new_start >= new_end
            assert old_start > 0 and new_start > 0
            index += 1
            old, new = [], []
            while len(old) < old_count or len(new) < new_count:
                assert index < len(lines)
                marker, body = lines[index][:1], lines[index][1:]
                assert marker in (b" ", b"-", b"+") and body.endswith(b"\n")
                if marker != b"+": old.append(body)
                if marker != b"-": new.append(body)
                index += 1
            assert (len(old), len(new)) == (old_count, new_count)
            assert old != new
            old_end, new_end = old_start + old_count, new_start + new_count
            hunks.append(dict(old_start=old_start, old_count=old_count,
                new_start=new_start, new_count=new_count, old_lines=old, new_lines=new))
        assert hunks
        result[target] = hunks
    assert index == len(lines)
    return result


def projected_metadata(hunk):
    return {**{key: hunk[key] for key in ("old_start", "old_count", "new_start", "new_count")},
            "old_sha256": sha(b"".join(hunk["old_lines"])),
            "new_sha256": sha(b"".join(hunk["new_lines"]))}


def apply_hunks(raw, hunks, *, reverse=False):
    lines = raw.splitlines(keepends=True)
    result, end = [], 0
    side, other = ("new", "old") if reverse else ("old", "new")
    for hunk in hunks:
        start = hunk[side + "_start"] - 1
        count = hunk[side + "_count"]
        assert start >= end and lines[start:start+count] == hunk[side + "_lines"]
        result.extend(lines[end:start]); result.extend(hunk[other + "_lines"])
        end = start + count
    result.extend(lines[end:])
    return b"".join(result)


def validate_package(meta, raw, *, root=ROOT):
    assert meta["schema"] == "intraday-snapshot-chunk-activation-v1"
    assert meta["patch_path"] == "docs/development/patches/intraday-snapshot-chunk-activation.patch"
    assert sha(raw) == meta["patch_sha256"]
    hunks = checked_hunks(raw)
    assert list(meta["targets"]) == list(TARGETS)
    modes = {}
    for target in TARGETS:
        item = meta["targets"][target]
        assert item["operation"] == "modify"
        assert all(re.fullmatch(r"[a-f0-9]{64}", item[field])
                   for field in ("before_sha256", "after_sha256"))
        assert item["hunks"] == [projected_metadata(h) for h in hunks[target]]
        path = root / target
        source = path.read_bytes() if path.is_file() else None
        if source is not None and sha(source) == item["after_sha256"]:
            assert len(source) == item["after_bytes"]
            before = apply_hunks(source, hunks[target], reverse=True)
            assert sha(before) == item["before_sha256"] and len(before) == item["before_bytes"]
            assert apply_hunks(before, hunks[target]) == source
            modes[target] = "exact_after_and_reconstructed_before"
        elif source is not None and sha(source) == item["before_sha256"]:
            assert len(source) == item["before_bytes"]
            after = apply_hunks(source, hunks[target])
            assert sha(after) == item["after_sha256"] and len(after) == item["after_bytes"]
            modes[target] = "exact_before_and_reconstructed_after"
        else:
            # Public main has not installed this composed local source. These
            # checks attest the patch projections, not an installed 1A adapter.
            if source is not None:
                tree = ast.parse(source)
                assert not any(isinstance(n, ast.keyword) and n.arg == "chunked_snapshots"
                               and isinstance(n.value, ast.Constant) and n.value.value is True
                               for n in ast.walk(tree)), "Composed source changed after exact-byte review"
            modes[target] = "projected_hunks_only_local_adapter_absent"
    assert list(meta["dependencies"]) == list(DEPENDENCIES)
    for dependency in DEPENDENCIES:
        path = root / dependency
        assert path.is_file()
        source = path.read_bytes()
        expected = meta["dependencies"][dependency]
        observed = {"sha256": sha(source), "bytes": len(source)}
        if observed != {key: expected[key] for key in ("sha256", "bytes")}:
            # Git's Windows checkout may use CRLF for this unchanged Python
            # dependency. Bind both reviewed byte encodings explicitly; the
            # adapter patch preimages remain exact and are never normalized.
            assert dependency == "ml/intraday/snapshot_objects.py"
            assert observed in expected["working_byte_variants"]
            canonical = source.replace(b"\r\n", b"\n")
            assert sha(canonical) == expected["sha256"] and len(canonical) == expected["bytes"]
    return modes


def package():
    meta = json.loads(META.read_bytes())
    return meta, (ROOT / meta["patch_path"]).read_bytes()


def test_exact_package_metadata_and_source_projections():
    meta, raw = package()
    modes = validate_package(meta, raw)
    print("SNAPSHOT_CHUNK_PACKAGE_VALIDATION=" + json.dumps(modes, sort_keys=True))


@pytest.mark.parametrize("mutation", ["patch_hash", "bytes", "count", "target", "trailing", "hunk_hash", "extra_target", "dependency"])
def test_changed_hash_hunk_path_or_dependency_is_rejected(mutation):
    meta, raw = package()
    meta = deepcopy(meta)
    if mutation == "patch_hash": meta["patch_sha256"] = "0" * 64
    elif mutation == "bytes":
        raw = raw.replace(b"chunked_snapshots", b"chunked_snapshotZ", 1)
        meta["patch_sha256"] = sha(raw)
    elif mutation == "count":
        raw = re.sub(rb"(@@ -\d+),(\d+)", lambda m: m[1] + b"," + str(int(m[2])+1).encode(), raw, count=1)
        meta["patch_sha256"] = sha(raw)
    elif mutation == "target":
        raw = raw.replace(b"--- a/ml/intraday/data_renewal.py", b"--- a/../outside.py", 1)
        meta["patch_sha256"] = sha(raw)
    elif mutation == "trailing":
        raw += b"+unowned trailing data\n"; meta["patch_sha256"] = sha(raw)
    elif mutation == "hunk_hash": meta["targets"][TARGETS[0]]["hunks"][0]["new_sha256"] = "0" * 64
    elif mutation == "extra_target": meta["targets"]["unowned.py"] = {}
    elif mutation == "dependency": meta["dependencies"][DEPENDENCIES[0]]["sha256"] = "0" * 64
    with pytest.raises(AssertionError):
        validate_package(meta, raw)


@pytest.mark.parametrize("field", ["before_sha256", "after_sha256"])
@pytest.mark.parametrize("target", TARGETS)
def test_false_raw_source_hash_is_rejected_on_local_composition(field, target):
    meta, raw = package()
    if not (ROOT / target).is_file() or sha((ROOT / target).read_bytes()) != meta["targets"][target]["after_sha256"]:
        pytest.skip("Exact local chunk adapter composition absent; projected checks remain active")
    meta = deepcopy(meta)
    meta["targets"][target][field] = "0" * 64
    with pytest.raises(AssertionError):
        validate_package(meta, raw)
