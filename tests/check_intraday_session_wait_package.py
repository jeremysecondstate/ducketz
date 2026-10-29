"""Verify exact launcher adapters and run synthetic session-start tests offline.

The public patch owns only the two invocation changes. Full unchanged context
allows exact byte reconstruction even where a public base has an older host.
No installed host, scheduler, provider, or live control is invoked by this check.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sys
import tempfile
import unittest


MANIFEST = "docs/development/patches/intraday-session-wait.json"
EXPECTED = {"Start-15m-Data.cmd": "data", "Start-15m-Trader.cmd": "trader"}
HUNK = re.compile(rb"@@ -1,(\d+) \+1,(\d+) @@\r?\n\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def relative(value):
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or path.is_absolute() or path.as_posix() != value or ".." in path.parts
            or any(part.lower() in {".git", ".env", ".venv", "scratch", "data", "artifacts"}
                   for part in path.parts)):
        raise ValueError("Package needs a portable nonprivate relative path")
    return value


def reconstruct(raw):
    """Require one complete, exact, nonoverlapping hunk per known launcher."""
    lines, cursor, result = raw.splitlines(keepends=True), 0, {}
    while cursor < len(lines):
        if cursor + 2 >= len(lines) or not lines[cursor].startswith(b"--- a/"):
            raise ValueError("Unexpected patch header")
        name = relative(lines[cursor][6:].rstrip(b"\r\n").decode("utf-8"))
        if name not in EXPECTED or name in result:
            raise ValueError("Unexpected or repeated launcher adapter")
        if lines[cursor + 1].rstrip(b"\r\n") != ("+++ b/" + name).encode():
            raise ValueError("Renamed launcher adapter")
        match = HUNK.fullmatch(lines[cursor + 2])
        if not match:
            raise ValueError("Adapter must contain one full-file hunk from line one")
        old_count, new_count = map(int, match.groups())
        cursor += 3
        before, after = [], []
        while len(before) < old_count or len(after) < new_count:
            if cursor >= len(lines):
                raise ValueError("Truncated adapter")
            line = lines[cursor]
            marker, body = line[:1], line[1:]
            if marker not in {b" ", b"-", b"+"}:
                raise ValueError("Unexpected adapter line")
            if marker != b"+":
                before.append(body)
            if marker != b"-":
                after.append(body)
            cursor += 1
        if len(before) != old_count or len(after) != new_count:
            raise ValueError("Adapter line counts differ")
        result[name] = (b"".join(before), b"".join(after))
    if set(result) != set(EXPECTED):
        raise ValueError("Both launchers must be reconstructed")
    return result


def check_package(candidate):
    manifest = json.loads((candidate / MANIFEST).read_text("utf-8"))
    if manifest.get("schema") != "intraday-session-wait-adapters-v1":
        raise ValueError("Unsupported adapter manifest")
    patch = (candidate / relative(manifest["patch"])).read_bytes()
    if sha(patch) != manifest["patch_sha256"]:
        raise ValueError("Adapter patch hash differs")
    reconstructed = reconstruct(patch)
    entries = {relative(item["path"]): item for item in manifest["files"]}
    if len(entries) != len(manifest["files"]) or set(entries) != set(EXPECTED):
        raise ValueError("Adapter manifest inventory differs")
    evidence = {}
    for name, (before, after) in reconstructed.items():
        entry = entries[name]
        if entry["operation"] != "modify":
            raise ValueError("Existing launcher must remain a modification")
        for key, raw in (("before_sha256", before), ("after_sha256", after)):
            if not SHA.fullmatch(entry[key]) or sha(raw) != entry[key]:
                raise ValueError("Exact launcher reconstruction differs: " + name)
        old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
        differences = [i for i, pair in enumerate(zip(old_lines, new_lines)) if pair[0] != pair[1]]
        if len(old_lines) != len(new_lines) or len(differences) != 1:
            raise ValueError("Only the existing host invocation may change")
        changed = differences[0]
        expected = ('".venv\\Scripts\\python.exe" -I -S -B '
                    '"tools\\intraday_session_wait.py" ' + EXPECTED[name] + ' %*').encode()
        if new_lines[changed].rstrip(b" \r\n") != expected:
            raise ValueError("Launcher does not forward the exact wait-wrapper invocation")
        if name == "Start-15m-Data.cmd":
            if (b"\r" in before or b"\r" in after or len(before) != len(after)
                    or len(new_lines[changed]) != 116
                    or sum(map(len, new_lines[:changed + 1])) != 373
                    or before[373:] != after[373:]
                    or not new_lines[changed].endswith(b" " * 39 + b"\n")):
                raise ValueError("Data launcher's original LF bytes, padding, or live batch tail moved")
        elif (b"\n" in before.replace(b"\r\n", b"")
              or b"\n" in after.replace(b"\r\n", b"")):
            raise ValueError("Trader launcher CRLF bytes changed")
        actual = (candidate / name).read_bytes() if (candidate / name).is_file() else None
        evidence[name] = {
            "reconstruction": "exact_complete_before_and_after",
            "candidate_consumer": "exact_after" if actual == after else "exact_before" if actual == before else "different_or_absent_preserved",
            "changed_line": changed + 1,
            "before_sha256": sha(before), "after_sha256": sha(after),
        }
    for name, expected in manifest["direct_files"].items():
        path = candidate / relative(name)
        if not SHA.fullmatch(expected) or sha(path.read_bytes()) != expected:
            raise ValueError("Reviewed direct package bytes differ: " + name)
        if path.suffix == ".py":
            ast.parse(path.read_text("utf-8"), filename=name)
    return reconstructed, evidence


def main():
    candidate = Path(__file__).resolve().parents[1]
    reconstructed, evidence = check_package(candidate)
    attempts = []

    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Session-start checks prohibit network/provider access")

    socket.create_connection = socket.getaddrinfo = no_network
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = no_network
    sys.dont_write_bytecode = True
    # Load only the two new Python files by exact path in a synthetic project.
    # No test discovery, conftest, installed editable package or live host exists.
    with tempfile.TemporaryDirectory(prefix="ducketz-session-start-check-") as temporary:
        staged = Path(temporary)
        for name, (_, after) in reconstructed.items():
            (staged / name).write_bytes(after)
        for name in ("tools/intraday_session_wait.py", "tests/test_intraday_session_wait.py"):
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate / name, target)
        spec = importlib.util.spec_from_file_location("session_start_offline_tests", staged / "tests/test_intraday_session_wait.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    if attempts:
        raise AssertionError("Session-start fixture attempted network access")
    print("SESSION_START_ADAPTER_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    print("SESSION_START_PACKAGE_RESULT=" + json.dumps({
        "successful": result.wasSuccessful(), "tests_run": result.testsRun,
        "network_attempts": len(attempts), "live_hosts_available": False,
        "full_launcher_bytes_reconstructed": True,
    }, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
