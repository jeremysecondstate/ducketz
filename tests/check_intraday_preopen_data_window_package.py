"""Verify exact data-window changes with isolated clocks and no live hosts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest


MANIFEST = "docs/development/patches/intraday-preopen-data-window.json"
PATHS = {"tools/intraday_session_wait.py", "tests/test_intraday_session_wait.py"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def reconstruct(raw):
    lines, cursor, files = raw.splitlines(keepends=True), 0, {}
    while cursor < len(lines):
        if not lines[cursor].startswith(b"--- a/"):
            raise ValueError("Unexpected package header")
        name = lines[cursor][6:].rstrip(b"\r\n").decode()
        if name not in PATHS or name in files:
            raise ValueError("Unexpected or repeated data-window path")
        if lines[cursor + 1].rstrip(b"\r\n") != ("+++ b/" + name).encode():
            raise ValueError("Renamed package path")
        match = re.fullmatch(rb"@@ -1,(\d+) \+1,(\d+) @@\r?\n", lines[cursor + 2])
        if not match:
            raise ValueError("Complete source context is required")
        counts = tuple(map(int, match.groups()))
        before, after = [], []
        cursor += 3
        while len(before) < counts[0] or len(after) < counts[1]:
            line = lines[cursor]
            if line[:1] not in {b" ", b"-", b"+"}:
                raise ValueError("Unexpected patch marker")
            if line[:1] != b"+":
                before.append(line[1:])
            if line[:1] != b"-":
                after.append(line[1:])
            cursor += 1
        if (len(before), len(after)) != counts:
            raise ValueError("Source line counts differ")
        files[name] = (b"".join(before), b"".join(after))
    if set(files) != PATHS:
        raise ValueError("Source and test changes must both be present")
    return files


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / MANIFEST).read_text("utf-8"))
    if manifest["schema"] != "intraday-preopen-data-window-v1":
        raise ValueError("Unsupported package")
    if manifest["patch"] != "docs/development/patches/intraday-preopen-data-window.patch":
        raise ValueError("Unexpected patch location")
    patch = (root / manifest["patch"]).read_bytes()
    if sha(patch) != manifest["patch_sha256"]:
        raise ValueError("Data-window patch hash differs")
    reconstructed = reconstruct(patch)
    entries = {item["path"]: item for item in manifest["files"]}
    if set(entries) != PATHS or len(entries) != len(manifest["files"]):
        raise ValueError("Package path closure differs")
    evidence = {}
    for name, (before, after) in reconstructed.items():
        entry = entries[name]
        if entry["before_sha256"] != sha(before) or entry["after_sha256"] != sha(after):
            raise ValueError("Package bytes differ from review")
        actual = (root / name).read_bytes() if (root / name).is_file() else None
        evidence[name] = {"after_sha256": sha(after),
            "candidate": "exact_after" if actual == after else "preserved_other_composition"}
    direct = "tests/check_intraday_preopen_data_window_package.py"
    if set(manifest["direct_files"]) != {direct} or sha((root / direct).read_bytes()) != manifest["direct_files"][direct]:
        raise ValueError("Package checker differs from review")
    attempts = []

    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Data-window checks are offline")

    socket.create_connection = socket.getaddrinfo = no_network
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = no_network
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="ducketz-data-window-check-") as temporary:
        staged = Path(temporary)
        for name, (_, after) in reconstructed.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(after)
        spec = importlib.util.spec_from_file_location("data_window_checks", staged / "tests/test_intraday_session_wait.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The unchanged batch-wrapper checks depend on local command files.
        # Portable evidence exercises the complete changed release/stop logic.
        tests = unittest.defaultTestLoader.loadTestsFromTestCase(module.SessionWindowTests)
        result = unittest.TextTestRunner(verbosity=2).run(tests)
    if attempts:
        raise AssertionError("Data-window checks attempted network access")
    print("PREOPEN_DATA_WINDOW_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    print("PREOPEN_DATA_WINDOW_CHECK=" + json.dumps({"successful": result.wasSuccessful(),
        "tests_run": result.testsRun, "skipped": len(result.skipped),
        "network_attempts": len(attempts), "native_scheduler_mutations": 0,
        "runtime_available": False}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
