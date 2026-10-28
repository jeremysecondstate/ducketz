"""Exact patch validation and offline import isolation for the Atlas split package.

Reuses the reviewed generic functions from the local renewal package checker;
includes no manual-trader adapter, launcher or renewal test invocation.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import socket
import sys

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
        raise RuntimeError("Atlas split package tests prohibit network/provider calls")

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
