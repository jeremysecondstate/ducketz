"""Verify exact shared-storage adapters; public source may lack local composition."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "docs/development/patches/intraday-shared-storage.json"
TARGETS = (
    "app/ui/intraday_data.py", "datafetching/intraday_fmp_observations.py",
    "ml/intraday/data.py", "ml/intraday/gap_recovery.py", "ml/intraday/market_inputs.py",
    "ml/intraday/readiness.py", "ml/intraday/split_model.py", "ml/intraday/store.py",
    "ml/intraday/supervision_data.py", "ml/intraday/supervisor.py",
    "tests/test_intraday_unpublished_recovery.py",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def parse(raw):
    assert isinstance(raw, bytes) and b"\r" not in raw
    raw.decode("utf-8")
    lines, index, result = raw.splitlines(keepends=True), 0, {}
    for target in TARGETS:
        assert lines[index:index+2] == [("--- a/" + target + "\n").encode(),
                                       ("+++ b/" + target + "\n").encode()]
        index += 2
        hunks, old_end, new_end = [], 0, 0
        while index < len(lines) and lines[index].startswith(b"@@ "):
            match = re.fullmatch(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", lines[index])
            assert match is not None
            start, count, new_start, new_count = (int(match[1]), int(match[2] or b"1"),
                                                 int(match[3]), int(match[4] or b"1"))
            assert start > 0 and new_start > 0 and start >= old_end and new_start >= new_end
            index += 1
            old, new = [], []
            while len(old) < count or len(new) < new_count:
                assert index < len(lines)
                marker, body = lines[index][:1], lines[index][1:]
                assert marker in (b" ", b"-", b"+") and body.endswith(b"\n")
                if marker != b"+": old.append(body)
                if marker != b"-": new.append(body)
                index += 1
            assert len(old) == count and len(new) == new_count and old != new
            hunks.append(dict(old_start=start, new_start=new_start, old=old, new=new))
            old_end, new_end = start + count, new_start + new_count
        assert hunks
        result[target] = hunks
    assert index == len(lines)
    return result


def apply(raw, hunks, reverse=False):
    lines, out, end = raw.splitlines(keepends=True), [], 0
    side, other = ("new", "old") if reverse else ("old", "new")
    for h in hunks:
        start = h[side + "_start"] - 1
        assert start >= end and lines[start:start + len(h[side])] == h[side]
        out.extend(lines[end:start]); out.extend(h[other]); end = start + len(h[side])
    return b"".join(out + lines[end:])


def projection(h):
    return {"old_start": h["old_start"], "new_start": h["new_start"],
            "old_count": len(h["old"]), "new_count": len(h["new"]),
            "old_sha256": sha(b"".join(h["old"])), "new_sha256": sha(b"".join(h["new"]))}


def validate(meta, raw, root=ROOT):
    assert meta["schema"] == "intraday-shared-storage-v1"
    assert meta["patch_path"] == "docs/development/patches/intraday-shared-storage.patch"
    assert sha(raw) == meta["patch_sha256"] and tuple(meta["targets"]) == TARGETS
    hunks, modes = parse(raw), {}
    for target in TARGETS:
        item = meta["targets"][target]
        assert item["operation"] == "modify"
        assert all(re.fullmatch("[a-f0-9]{64}", item[k]) for k in (
            "before_sha256", "after_sha256", "before_raw_sha256", "after_raw_sha256"))
        assert item["before_newlines"] in {"LF", "CRLF"} and item["after_newlines"] in {"LF", "CRLF"}
        assert all(type(item[k]) is int and item[k] > 0 for k in (
            "before_bytes", "after_bytes", "before_raw_bytes", "after_raw_bytes"))
        assert item["hunks"] == [projection(h) for h in hunks[target]]
        path = root / target
        source = path.read_bytes() if path.is_file() else None
        actual = sha(source) if source is not None else None
        normalized = source.replace(b"\r\n", b"\n") if source is not None else None
        if actual == item["after_raw_sha256"]:
            assert len(source) == item["after_raw_bytes"] and sha(normalized) == item["after_sha256"]
            before = apply(normalized, hunks[target], True)
            assert sha(before) == item["before_sha256"] and len(before) == item["before_bytes"]
            assert len(normalized) == item["after_bytes"] and apply(before, hunks[target]) == normalized
            raw_before = before.replace(b"\n", b"\r\n") if item["before_newlines"] == "CRLF" else before
            assert sha(raw_before) == item["before_raw_sha256"] and len(raw_before) == item["before_raw_bytes"]
            modes[target] = "exact_composed_adapter"
        elif actual == item["before_raw_sha256"]:
            assert len(source) == item["before_raw_bytes"] and sha(normalized) == item["before_sha256"]
            after = apply(normalized, hunks[target])
            assert sha(after) == item["after_sha256"] and len(after) == item["after_bytes"]
            assert len(normalized) == item["before_bytes"]
            raw_after = after.replace(b"\n", b"\r\n") if item["after_newlines"] == "CRLF" else after
            assert sha(raw_after) == item["after_raw_sha256"] and len(raw_after) == item["after_raw_bytes"]
            modes[target] = "exact_preimage"
        else:
            # Windows Git checkout may add CRLF to a reviewed LF blob. Accept
            # only that exact newline transformation, not arbitrary source drift.
            assert (actual == item["public_base_sha256"] or normalized is not None
                    and sha(normalized) == item["public_base_sha256"]), "Adapter differs from every reviewed source"
            modes[target] = "public_base_only_integration_not_installed"
    assert meta["dependencies"]
    for name, item in meta["dependencies"].items():
        assert name.startswith(("ml/", "tools/", "tests/")) and ".." not in Path(name).parts
        source = (root / name).read_bytes()
        normalized = source.replace(b"\r\n", b"\n")
        assert (sha(source) == item["sha256"] and len(source) == item["bytes"]
                or sha(normalized) == item["sha256"] and len(normalized) == item["bytes"])
    return modes


def package():
    meta = json.loads(META.read_bytes())
    return meta, (ROOT / meta["patch_path"]).read_bytes()


def test_exact_storage_package():
    meta, raw = package()
    print("SHARED_STORAGE_ADAPTER_VALIDATION=" + json.dumps(validate(meta, raw), sort_keys=True))


@pytest.mark.parametrize("mutation", ["patch_hash", "extra_path", "path_escape", "trailing", "hunk_hash", "dependency"])
def test_corrupt_packet_is_rejected(mutation):
    meta, raw = package(); meta = deepcopy(meta)
    if mutation == "patch_hash": meta["patch_sha256"] = "0" * 64
    elif mutation == "extra_path": meta["targets"]["unowned.py"] = {}
    elif mutation == "path_escape":
        raw = raw.replace(b"--- a/app/ui/intraday_data.py", b"--- a/../outside.py", 1)
        meta["patch_sha256"] = sha(raw)
    elif mutation == "trailing":
        raw += b"+unowned bytes\n"; meta["patch_sha256"] = sha(raw)
    elif mutation == "hunk_hash": meta["targets"][TARGETS[0]]["hunks"][0]["new_sha256"] = "0" * 64
    elif mutation == "dependency": meta["dependencies"][next(iter(meta["dependencies"]))]["sha256"] = "0" * 64
    with pytest.raises(AssertionError): validate(meta, raw)
