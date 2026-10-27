"""Offline portable package checks; adapted trading tests run in local adoption.

The public patch carries only this change's reviewed hunks. When the public base
does not contain an exact installed consumer, this checker reports a structural
hunk projection, never an exact full-file reconstruction. Local adoption must
also verify the before/after hashes using its preserved complete originals.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import sys


MANIFEST = "docs/development/patches/trader-process-renewal.json"
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Package paths must be portable and relative")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("Invalid package path")
    if set(part.lower() for part in path.parts) & {".git", ".env", "scratch", ".venv", "data", "artifacts"}:
        raise ValueError("Private operating data cannot be part of the package")
    return value


def file_header(line, prefix):
    value = line.removeprefix(prefix).rstrip(b"\r\n").decode("utf-8")
    if value == "/dev/null":
        return None
    if not value.startswith(("a/", "b/")):
        raise ValueError("Patch needs portable a/ and b/ paths")
    return relative(value[2:])


def parse_patch(raw):
    lines, offset, files = raw.splitlines(keepends=True), 0, {}
    while offset < len(lines):
        if lines[offset].startswith((b"diff --git ", b"index ", b"new file mode ", b"deleted file mode ")):
            offset += 1
            continue
        if not lines[offset].startswith(b"--- ") or offset + 1 >= len(lines) or not lines[offset + 1].startswith(b"+++ "):
            raise ValueError("Unexpected unified-patch content")
        before = file_header(lines[offset], b"--- ")
        after = file_header(lines[offset + 1], b"+++ ")
        name = before or after
        if not name or (before and after and before != after) or name in files:
            raise ValueError("Duplicate, renamed or missing patch path")
        offset += 2
        hunks = []
        while offset < len(lines) and lines[offset].startswith(b"@@ "):
            match = HUNK.match(lines[offset])
            if not match:
                raise ValueError("Malformed unified hunk header")
            old_start, old_count, new_start, new_count = [int(value) for value in (
                match[1], match[2] or b"1", match[3], match[4] or b"1")]
            old_lines, new_lines, previous = [], [], None
            offset += 1
            while len(old_lines) < old_count or len(new_lines) < new_count:
                if offset >= len(lines):
                    raise ValueError("Truncated patch hunk")
                line = lines[offset]
                marker, body = line[:1], line[1:]
                if marker not in {b" ", b"-", b"+"}:
                    raise ValueError("Unexpected patch hunk line")
                if marker != b"+":
                    old_lines.append(body)
                if marker != b"-":
                    new_lines.append(body)
                previous = marker
                offset += 1
                if offset < len(lines) and lines[offset].startswith(b"\\ No newline at end of file"):
                    if previous != b"+":
                        old_lines[-1] = old_lines[-1].removesuffix(b"\n").removesuffix(b"\r")
                    if previous != b"-":
                        new_lines[-1] = new_lines[-1].removesuffix(b"\n").removesuffix(b"\r")
                    offset += 1
            if len(old_lines) != old_count or len(new_lines) != new_count:
                raise ValueError("Patch line count mismatch")
            hunks.append({"old_start": old_start, "old_count": old_count, "new_start": new_start,
                          "new_count": new_count, "old_lines": old_lines, "new_lines": new_lines})
        if not hunks:
            raise ValueError("Empty patch file entry")
        files[name] = {"before": before, "after": after, "hunks": hunks}
    return files


def apply_hunks(original, hunks):
    lines, cursor, output = original.splitlines(keepends=True), 0, []
    for hunk in hunks:
        start = hunk["old_start"] - (1 if hunk["old_count"] else 0)
        if start < cursor or start > len(lines) or lines[start:start + hunk["old_count"]] != hunk["old_lines"]:
            raise ValueError("Hunk does not match the exact preimage")
        output.extend(lines[cursor:start])
        expected_start = len(output) + (1 if hunk["new_count"] else 0)
        if hunk["new_start"] != expected_start:
            raise ValueError("Hunk target offset changed unrelated line positions")
        output.extend(hunk["new_lines"])
        cursor = start + hunk["old_count"]
    output.extend(lines[cursor:])
    return b"".join(output)


def check_projection(hunks):
    original = []
    for hunk in hunks:
        start = hunk["old_start"] - (1 if hunk["old_count"] else 0)
        if start < len(original):
            raise ValueError("Overlapping patch hunks")
        original.extend([b"# unchanged context omitted from the public patch\n"] * (start - len(original)))
        original.extend(hunk["old_lines"])
    return apply_hunks(b"".join(original), hunks)


def check_package(candidate):
    manifest = json.loads((candidate / MANIFEST).read_text("utf-8"))
    if manifest.get("schema") != "trader-renewal-patches-v1":
        raise ValueError("Unsupported renewal patch manifest")
    patch_path = relative(manifest["patch"])
    raw = (candidate / patch_path).read_bytes()
    if sha(raw) != manifest["patch_sha256"]:
        raise ValueError("Public compatibility patch bytes changed")
    parsed = parse_patch(raw)
    entries = {relative(entry["path"]): entry for entry in manifest["files"]}
    if len(entries) != len(manifest["files"]) or set(entries) != set(parsed):
        raise ValueError("Patch inventory differs from its manifest")
    evidence = {}
    for name, patch in parsed.items():
        entry = entries[name]
        operation = "add" if patch["before"] is None else "delete" if patch["after"] is None else "modify"
        if entry["operation"] != operation:
            raise ValueError("Patch operation differs from manifest")
        for key in ("before_sha256", "after_sha256"):
            if entry[key] is not None and not SHA.fullmatch(entry[key]):
                raise ValueError("Invalid adapter hash")
        if (entry["before_sha256"] is None) != (operation == "add") or (entry["after_sha256"] is None) != (operation == "delete"):
            raise ValueError("Adapter preimage/postimage requirement is inconsistent")
        projected = check_projection(patch["hunks"])
        actual = (candidate / name).read_bytes() if (candidate / name).is_file() else None
        if operation == "add" or (actual is not None and sha(actual) == entry["before_sha256"]):
            updated = apply_hunks(actual if operation != "add" else b"", patch["hunks"])
            if operation == "delete":
                if updated:
                    raise ValueError("Deletion retained content")
            elif sha(updated) != entry["after_sha256"]:
                raise ValueError("Exact adapter postimage differs from its review hash")
            if name.endswith(".py") and operation != "delete":
                ast.parse(updated.decode("utf-8-sig"), filename=name)
            evidence[name] = "exact_preimage_reconstruction"
        elif actual is not None and sha(actual) == entry["after_sha256"]:
            reverse = [{"old_start": h["new_start"], "old_count": h["new_count"],
                        "new_start": h["old_start"], "new_count": h["old_count"],
                        "old_lines": h["new_lines"], "new_lines": h["old_lines"]}
                       for h in patch["hunks"]]
            if sha(apply_hunks(actual, reverse)) != entry["before_sha256"]:
                raise ValueError("Exact adapter reverse reconstruction differs from its review hash")
            if name.endswith(".py"):
                ast.parse(actual.decode("utf-8-sig"), filename=name)
            evidence[name] = "exact_postimage_reverse_reconstruction"
        else:
            # Hunk projections prove offsets and literal payload consistency. They
            # cannot establish omitted surrounding bytes; that proof belongs to
            # the separately retained complete local adoption preimage.
            evidence[name] = "hunk_projection_only_local_exact_preimage_required"
        if operation == "add" and projected != updated:
            raise ValueError("Added file projection changed")
    direct = manifest["direct_files"]
    if not isinstance(direct, dict) or not direct:
        raise ValueError("Direct portable source inventory is required")
    for name, expected in direct.items():
        path = candidate / relative(name)
        if not SHA.fullmatch(expected) or sha(path.read_bytes()) != expected:
            raise ValueError("Direct package file changed: " + name)
        if path.suffix == ".py":
            ast.parse(path.read_text("utf-8-sig"), filename=name)
    return evidence


def isolate(candidate):
    originals, retained = set(), []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            originals.update(Path(path).resolve().parent for path in mapping.values())
        else:
            retained.append(finder)
    sys.meta_path[:] = retained
    originals.discard(candidate)
    retained_paths = []
    for value in sys.path:
        path = Path(value or ".").resolve()
        if path in originals | {candidate}:
            continue
        if path.name == "cross_pc" and (path / "test_runner.py").exists():
            continue  # Its requests.py must not shadow the dependency package.
        retained_paths.append(value)
    sys.path[:] = [str(candidate), *retained_paths]
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True

    def denied(*args, **kwargs):
        raise RuntimeError("Trader renewal package tests prohibit network/provider calls")

    socket.create_connection = socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied
    return originals


def check_imports(candidate, originals):
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if name.split(".")[0] in {"ml", "tools", "app", "datafetching"} and not path.is_relative_to(candidate):
            raise RuntimeError("Portable test imported another application checkout")
        if ".venv" not in path.parts and any(path.is_relative_to(root) for root in originals):
            raise RuntimeError("Portable test imported mutable installed application code")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helpers-only", action="store_true", help="Development-only before the reviewed patch manifest exists")
    args = parser.parse_args(argv)
    candidate = Path(__file__).resolve().parents[1]
    originals = isolate(candidate)
    if not args.helpers_only:
        print("ADAPTER_PATCH_EVIDENCE=" + json.dumps(check_package(candidate), sort_keys=True))
    import pytest
    (candidate / "scratch/checks").mkdir(parents=True, exist_ok=True)
    result = pytest.main(["--noconftest", "-q", "-p", "no:cacheprovider",
        "--basetemp", str(candidate / "scratch/checks/trader-renewal-package"),
        str(candidate / "tests/test_trader_installation.py"),
        str(candidate / "tests/test_trader_session_host.py"),
        str(candidate / "tests/test_trader_controller_handoff.py")])
    check_imports(candidate, originals)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
                closure[path.relative_to(candidate).as_posix()] = sha(path.read_bytes())
    print("TRADER_RENEWAL_PORTABLE_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
