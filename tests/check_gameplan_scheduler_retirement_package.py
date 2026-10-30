"""Reconstruct and exercise the exact retirement additions without live services."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import unittest


MANIFEST = "docs/development/patches/gameplan-scheduler-retirement.json"
INSERTIONS = {
    "Start-Gameplan-Trader.cmd": (
        b"@echo off\r\nsetlocal DisableDelayedExpansion\r\n",
        b"echo Gameplan Trader is retired. Use the 15-Minute Trader.\r\n"
        b"exit /b 1\r\n"
        b"rem Historical launcher retained below; retirement exits before runtime access.\r\n",
    ),
    "tools/register_nightly_watchdog.ps1": (
        b"$ErrorActionPreference = 'Stop'\n",
        b"# Retirement is explicit human direction; historical inspection remains available.\n"
        b"if (-not $ReadOnly) {\n"
        b"    throw 'Gameplan nightly dispatch is retired. Use the existing 15-minute preparation owners.'\n"
        b"}\n",
    ),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def reconstruct(raw):
    lines, cursor, files = raw.splitlines(keepends=True), 0, {}
    while cursor < len(lines):
        if not lines[cursor].startswith(b"--- a/"):
            raise ValueError("Unexpected patch header")
        name = lines[cursor][6:].rstrip(b"\r\n").decode()
        if name not in INSERTIONS or name in files:
            raise ValueError("Unexpected or repeated retirement path")
        if lines[cursor + 1].rstrip(b"\r\n") != ("+++ b/" + name).encode():
            raise ValueError("Renamed retirement path")
        match = re.fullmatch(rb"@@ -1,(\d+) \+1,(\d+) @@\r?\n", lines[cursor + 2])
        if not match:
            raise ValueError("Complete before/after context is required")
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
            raise ValueError("Patch line counts differ")
        files[name] = (b"".join(before), b"".join(after))
    if set(files) != set(INSERTIONS):
        raise ValueError("Both retirement additions must be included")
    return files


def main():
    candidate = Path(__file__).resolve().parents[1]
    manifest = json.loads((candidate / MANIFEST).read_text("utf-8"))
    if manifest["schema"] != "gameplan-scheduler-retirement-v1":
        raise ValueError("Unsupported retirement package")
    if manifest["patch"] != "docs/development/patches/gameplan-scheduler-retirement.patch":
        raise ValueError("Unexpected package location")
    patch = (candidate / manifest["patch"]).read_bytes()
    if sha(patch) != manifest["patch_sha256"]:
        raise ValueError("Retirement patch hash differs")
    reconstructed = reconstruct(patch)
    entries = {item["path"]: item for item in manifest["files"]}
    if set(entries) != set(INSERTIONS) or len(entries) != len(manifest["files"]):
        raise ValueError("Retirement manifest paths differ")
    evidence = {}
    for name, (before, after) in reconstructed.items():
        entry = entries[name]
        anchor, insertion = INSERTIONS[name]
        if before.count(anchor) != 1 or after != before.replace(anchor, anchor + insertion, 1):
            raise ValueError("Package may add only the reviewed retirement guard")
        if entry["before_sha256"] != sha(before) or entry["after_sha256"] != sha(after):
            raise ValueError("Retirement reconstruction differs")
        actual = (candidate / name).read_bytes() if (candidate / name).is_file() else None
        evidence[name] = dict(before_sha256=sha(before), after_sha256=sha(after),
                             candidate="exact_after" if actual == after else "preserved_other_composition")
    expected_direct = {"tests/test_gameplan_scheduler_retirement.py",
                       "tests/check_gameplan_scheduler_retirement_package.py"}
    if set(manifest["direct_files"]) != expected_direct:
        raise ValueError("Retirement direct file closure differs")
    for name, digest in manifest["direct_files"].items():
        if sha((candidate / name).read_bytes()) != digest:
            raise ValueError("Retirement direct bytes differ")
    attempts = []

    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Retirement tests are offline")

    socket.create_connection = socket.getaddrinfo = no_network
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = no_network
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="ducketz-retirement-check-") as temporary:
        staged = Path(temporary)
        for name, (_, after) in reconstructed.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(after)
        test = "tests/test_gameplan_scheduler_retirement.py"
        (staged / "tests").mkdir()
        shutil.copyfile(candidate / test, staged / test)
        spec = importlib.util.spec_from_file_location("retirement_offline_tests", staged / test)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    if attempts:
        raise AssertionError("Retirement checks attempted network access")
    print("GAMEPLAN_RETIREMENT_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    print("GAMEPLAN_RETIREMENT_CHECK=" + json.dumps(dict(
        successful=result.wasSuccessful(), tests_run=result.testsRun, skipped=len(result.skipped),
        network_attempts=len(attempts), native_scheduler_mutations=0, runtime_available=False,
    ), sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
