"""Check exact opening-window patches and execute their policy nodes offline.

This portable check executes the reconstructed prediction implementation and
actual eligibility/submission functions with a deterministic calendar fixture.
It checks only the opening guard projection of ShadowRunner; full local runtime
and broker-boundary integration are tested by test_intraday_opening_policy.py.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re
import socket
import sys
from types import ModuleType, SimpleNamespace
from typing import Literal


MANIFEST = "docs/development/patches/intraday-opening-policy.json"
ALLOWED = {
    "ml/intraday/live_session.py": {"eligible_reference"},
    "ml/intraday/runtime.py": {"ShadowRunner"},
    "ml/intraday/prediction.py": {"issue_prediction"},
    "ml/intraday/execution_policy.py": {"POLICY_VERSION", "SubmissionGate", "submission_gate", "ForecastTransitionPlan"},
    "tests/test_intraday_prediction.py": {"test_unresolved_session_boundaries_are_not_fabricated"},
    "tests/test_intraday_live_session.py": {"test_no_closed_or_unspecified_opening_target"},
    "tests/test_intraday_execution_policy.py": {"test_cutoff_is_not_market_calendar_or_opening_permission"},
}
DIRECT = {"tests/test_intraday_opening_policy.py", "tests/check_intraday_opening_policy_package.py"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def reconstruct(raw):
    lines, cursor, files = raw.splitlines(keepends=True), 0, {}
    while cursor < len(lines):
        if not lines[cursor].startswith(b"--- a/"):
            raise ValueError("Unexpected patch header")
        name = lines[cursor][6:].rstrip(b"\r\n").decode()
        if name not in ALLOWED or name in files:
            raise ValueError("Unexpected or repeated opening policy path")
        if lines[cursor+1].rstrip(b"\r\n") != ("+++ b/" + name).encode():
            raise ValueError("Renamed policy path")
        match = re.fullmatch(rb"@@ -1,(\d+) \+1,(\d+) @@\r?\n", lines[cursor+2])
        if not match:
            raise ValueError("Complete before/after context required")
        counts = tuple(map(int, match.groups()))
        before, after = [], []
        cursor += 3
        while len(before) < counts[0] or len(after) < counts[1]:
            if cursor >= len(lines):
                raise ValueError("Truncated opening patch")
            line = lines[cursor]
            if line[:1] not in {b" ", b"-", b"+"}:
                raise ValueError("Unexpected patch marker")
            if line[:1] != b"+": before.append(line[1:])
            if line[:1] != b"-": after.append(line[1:])
            cursor += 1
        if (len(before), len(after)) != counts:
            raise ValueError("Opening patch counts differ")
        files[name] = (b"".join(before), b"".join(after))
    if set(files) != set(ALLOWED):
        raise ValueError("Opening patch inventory differs")
    return files


def key(node):
    if hasattr(node, "name"): return node.name
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def representation(nodes):
    return ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False)


def owned_only(name, before, after):
    old, new = (ast.parse(raw.decode("utf-8")) for raw in (before, after))
    if representation([n for n in old.body if key(n) not in ALLOWED[name]]) != representation(
            [n for n in new.body if key(n) not in ALLOWED[name]]):
        raise ValueError("Unowned syntax changed: " + name)
    if name == "ml/intraday/runtime.py":
        classes = [next(n for n in tree.body if key(n) == "ShadowRunner") for tree in (old, new)]
        if representation([n for n in classes[0].body if key(n) != "_symbol"]) != representation(
                [n for n in classes[1].body if key(n) != "_symbol"]):
            raise ValueError("Unowned runner method changed")
        methods = [next(n for n in node.body if key(n) == "_symbol") for node in classes]
        for anchor, suffix in (("local_boundary", False), ("begun", True)):
            parts = []
            for method in methods:
                index = next(i for i, n in enumerate(method.body) if key(n) == anchor)
                parts.append(method.body[index:] if suffix else method.body[:index])
            if representation(parts[0]) != representation(parts[1]):
                raise ValueError("Unowned runtime preparation/model path changed")
    return new


def exercise(files):
    package = ModuleType("opening_checked")
    package.__path__ = []
    sys.modules[package.__name__] = package
    prediction = ModuleType("opening_checked.prediction")
    prediction.__package__ = package.__name__
    sys.modules[prediction.__name__] = prediction
    exec(compile(files["ml/intraday/prediction.py"][1], "reconstructed-prediction.py", "exec"), prediction.__dict__)
    p = prediction
    calendar = ModuleType("opening_checked.data")
    def sessions(start, end):
        local = p.instant(start).astimezone(p.PACIFIC)
        # Deliberate fixture, not a replacement production calendar.
        if local.weekday() >= 5 or local.date() in {date(2026, 12, 25), date(2026, 11, 27)}:
            return ()
        opened, closed = local.replace(hour=1, minute=0), local.replace(hour=17, minute=0)
        return (SimpleNamespace(contains=lambda value: opened <= p.instant(value) and p.instant(value)+p.STEP <= closed),)
    calendar.equity_sessions = sessions
    sys.modules[calendar.__name__] = calendar
    namespace = dict(p.__dict__, dataclass=dataclass, date=date, datetime=datetime, time=time, Literal=Literal)
    for path, names in (("ml/intraday/live_session.py", {"eligible_reference"}),
                        ("ml/intraday/execution_policy.py", {"SubmissionGate", "submission_gate"})):
        tree = ast.parse(files[path][1].decode())
        selected = ast.Module(body=[n for n in tree.body if key(n) in names], type_ignores=[])
        exec(compile(selected, path, "exec"), namespace)
    runtime = ast.parse(files["ml/intraday/runtime.py"][1].decode())
    method = next(n for n in next(n for n in runtime.body if key(n) == "ShadowRunner").body if key(n) == "_symbol")
    boundary = next(i for i, n in enumerate(method.body) if key(n) == "begun")
    method.body = method.body[:boundary] + [ast.Return(value=ast.Constant(value="admitted"))]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "runner-opening-projection.py", "exec"), namespace)
    checks = 0
    for day, offset in (("2026-10-30", "-07:00"), ("2026-11-02", "-08:00")):
        ref = p.instant(f"{day}T03:30:00{offset}")
        issued = ref + p.STEP + timedelta(seconds=30)
        row = p.Candle("TEST", ref.isoformat(), (ref+p.STEP+timedelta(seconds=1)).isoformat(),
                       "100", "101", "99", "100", "10", "offline")
        value = p.issue_prediction([row], symbol="TEST", issued_at=issued, eligible_session=date.fromisoformat(day),
            model_version="offline", model_target_version=p.TARGET_VERSION, infer=lambda _: "0.5001").validate()
        assert p.instant(value.target_open) == ref+2*p.STEP and p.instant(value.target_close) == ref+3*p.STEP
        assert value.probability_not_down == "0.5001"
        assert namespace["eligible_reference"](ref)
        assert not namespace["eligible_reference"](ref-p.STEP)
        gate = namespace["submission_gate"]
        assert gate(as_of=f"{day}T03:59:59.999999{offset}", eligible_session=date.fromisoformat(day)).state == "before_session_open"
        assert gate(as_of=f"{day}T04:00:00{offset}", eligible_session=date.fromisoformat(day)).state == "before_submission_cutoff"
        assert gate(as_of=f"{day}T16:45:00{offset}", eligible_session=date.fromisoformat(day)).state == "submission_cutoff"
        runner = SimpleNamespace(stopped=lambda: False, store=SimpleNamespace(forecast=lambda *a: None))
        assert namespace["_symbol"](runner, "cycle", "TEST", None, ref, issued, "live", None, None, 0) == "admitted"
        try:
            namespace["_symbol"](runner, "cycle", "TEST", None, ref-p.STEP, issued, "live", None, None, 0)
        except p.UnresolvedPolicy:
            pass
        else:
            raise AssertionError("Runtime admitted a target before04:00")
        checks += 9
    for closed in ("2026-10-31", "2026-12-25", "2026-11-27"):
        assert not namespace["eligible_reference"](datetime.fromisoformat(closed).replace(hour=3, minute=30, tzinfo=p.PACIFIC))
        checks += 1
    return checks


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root/MANIFEST).read_text("utf-8"))
    if manifest["schema"] != "intraday-opening-policy-v1" or manifest["patch"] != MANIFEST.replace(".json", ".patch"):
        raise ValueError("Unexpected opening package")
    raw = (root/manifest["patch"]).read_bytes()
    if sha(raw) != manifest["patch_sha256"]: raise ValueError("Opening patch hash differs")
    files = reconstruct(raw)
    entries = {entry["path"]: entry for entry in manifest["files"]}
    if len(entries) != len(manifest["files"]) or set(entries) != set(ALLOWED):
        raise ValueError("Opening manifest inventory differs")
    for name, (before, after) in files.items():
        entry = entries[name]
        if entry["operation"] != "modify" or entry["before_sha256"] != sha(before) or entry["after_sha256"] != sha(after):
            raise ValueError("Opening reconstruction differs: " + name)
        owned_only(name, before, after)
    if set(manifest["direct_files"]) != DIRECT: raise ValueError("Opening direct closure differs")
    for name, checksum in manifest["direct_files"].items():
        if sha((root/name).read_bytes()) != checksum: raise ValueError("Opening direct file changed")
    attempts = []
    def forbidden(*a, **k):
        attempts.append(True)
        raise AssertionError("Opening policy checks are offline")
    socket.create_connection = socket.getaddrinfo = forbidden
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = forbidden
    checks = exercise(files)
    if attempts: raise AssertionError("Opening policy checks attempted network access")
    print("OPENING_POLICY_PACKAGE=" + json.dumps(dict(checks=checks, network_attempts=0,
        files_reconstructed=len(files), full_runtime_integration=False,
        calendar="deterministic_fixture", projection="actual_prediction_and_opening_gate_nodes"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
