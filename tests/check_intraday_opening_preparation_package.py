"""Exact owned patch and portable opening-clock/provenance checks.

Full data/model/account integration runs separately against the complete local
installation. This check imports no mutable application or provider modules.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import unittest
from zoneinfo import ZoneInfo

MANIFEST = "docs/development/patches/intraday-opening-preparation.json"
OWNED_BODIES = {
    "ml/intraday/split_model.py": {"model_pass"},
    "ml/intraday/account_execution.py": {"forecast_disposition", "IntradayAccountExecutor.step"},
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def reconstruct(raw):
    lines, cursor, files = raw.splitlines(keepends=True), 0, {}
    while cursor < len(lines):
        if not lines[cursor].startswith(b"--- a/"):
            raise ValueError("Unexpected opening patch header")
        name = lines[cursor][6:].rstrip(b"\r\n").decode()
        if name not in OWNED_BODIES or name in files:
            raise ValueError("Unexpected opening patch path")
        if lines[cursor+1].rstrip(b"\r\n") != ("+++ b/"+name).encode():
            raise ValueError("Opening patch rename is not owned")
        match = re.fullmatch(rb"@@ -1,(\d+) \+1,(\d+) @@\r?\n", lines[cursor+2])
        if not match:
            raise ValueError("Complete exact before/after context required")
        counts = tuple(map(int, match.groups()))
        before, after = [], []
        cursor += 3
        while len(before) < counts[0] or len(after) < counts[1]:
            line = lines[cursor]
            if line[:1] not in {b" ", b"-", b"+"}:
                raise ValueError("Unexpected opening patch marker")
            if line[:1] != b"+": before.append(line[1:])
            if line[:1] != b"-": after.append(line[1:])
            cursor += 1
        if (len(before), len(after)) != counts:
            raise ValueError("Opening patch line count differs")
        files[name] = (b"".join(before), b"".join(after))
    if set(files) != set(OWNED_BODIES):
        raise ValueError("Opening patch closure differs")
    return files


def unowned_tree(raw, owned):
    tree = ast.parse(raw)
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in owned:
            node.body = [ast.Pass()]
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                name = node.name + "." + getattr(method, "name", "")
                if isinstance(method, ast.FunctionDef) and name in owned:
                    method.body = [ast.Pass()]
                    found.add(name)
    if found != owned:
        raise ValueError("Opening patch changed an unavailable owner")
    return ast.dump(tree, include_attributes=False)


def primitive_instant(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Aware timestamp required")
    return value.astimezone(timezone.utc)


def primitive_digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def load_pure_functions(raw):
    names = {"opening_window", "_opening_forecast", "opening_handoff_valid"}
    parsed = ast.parse(raw)
    nodes = [node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != names:
        raise ValueError("Opening clock/admission contract unavailable")
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for function in nodes for node in ast.walk(function)):
        raise ValueError("Portable opening functions must not import operational modules")
    namespace = {"instant": primitive_instant, "digest": primitive_digest,
        "ContractError": ValueError, "PACIFIC": ZoneInfo("America/Los_Angeles"),
        "STEP": timedelta(minutes=15), "timedelta": timedelta, "VERSION": "intraday-opening-preparation-v1"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<exact-opening-functions>", "exec"), namespace)
    return namespace


def checks(api):
    class OpeningChecks(unittest.TestCase):
        def forecast(self):
            source = {"pc": "Atlas", "symbol": "AAA", "cycle_id": "source-cycle", "mode": "live",
                "reference_at": "2026-10-29T10:30:00+00:00", "target_open": "2026-10-29T11:00:00+00:00",
                "target_close": "2026-10-29T11:15:00+00:00", "availability_basis": "observed",
                "information_available_at": "2026-10-29T10:45:00+00:00", "training_cutoff": "2026-10-29T10:46:00+00:00",
                "issued_at": "2026-10-29T10:47:00+00:00", "probability_not_down": .5001,
                "models": [{"name": "fixture", "probability_not_down": .5001}]}
            link = {"schema": api["VERSION"], "source_forecast_id": primitive_digest(source),
                "source_cycle_id": source["cycle_id"], "session_id": "manual", "configuration_hash": "configuration",
                "session_started_at": "2026-10-29T11:00:00+00:00", "admitted_at": "2026-10-29T11:00:01+00:00"}
            cycle = primitive_digest(dict(pc="Atlas", reference_at=source["reference_at"],
                configuration_hash="configuration", mode="live", experiment="manual"))
            return {**source, "cycle_id": cycle, "opening_handoff": link}

        def valid(self, forecast):
            return api["opening_handoff_valid"](forecast, session_started_at="2026-10-29T11:00:00+00:00")

        def test_exact_original_clock_and_probability(self):
            self.assertTrue(self.valid(self.forecast()))

        def test_changing_science_or_issue_clock_rejects(self):
            for field, value in (("probability_not_down", .9), ("issued_at", "2026-10-29T11:00:00+00:00"),
                                 ("models", [{"name": "other"}])):
                self.assertFalse(self.valid({**self.forecast(), field: value}))

        def test_another_session_cannot_reuse_admission(self):
            original = self.forecast()
            changed = {**original, "opening_handoff": {**original["opening_handoff"], "session_id": "second"}}
            self.assertFalse(self.valid(changed))
            self.assertFalse(api["opening_handoff_valid"](original, session_started_at="2026-10-29T11:01:00+00:00"))

        def test_early_and_expired_admission_rejects(self):
            for when in ("2026-10-29T10:59:59+00:00", "2026-10-29T11:15:00+00:00"):
                original = self.forecast()
                original["opening_handoff"]["admitted_at"] = when
                self.assertFalse(self.valid(original))

        def test_original_target_and_dst(self):
            for day, expected in (("2026-10-29", 11), ("2026-11-03", 12)):
                reference, ready, target, close = api["opening_window"](day+"T09:00:00Z")
                self.assertEqual(target.hour, expected)
                self.assertEqual(target-reference, timedelta(minutes=30))
                self.assertEqual(ready-reference, timedelta(minutes=15, seconds=30))
                self.assertEqual(close-target, timedelta(minutes=15))

    return unittest.defaultTestLoader.loadTestsFromTestCase(OpeningChecks)


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / MANIFEST).read_text("utf-8"))
    if manifest["schema"] != "intraday-opening-preparation-package-v1":
        raise ValueError("Unknown opening package")
    patch = (root / manifest["patch"]).read_bytes()
    if sha(patch) != manifest["patch_sha256"]:
        raise ValueError("Opening patch hash differs")
    reconstructed = reconstruct(patch)
    entries = {item["path"]: item for item in manifest["files"]}
    if set(entries) != set(reconstructed) or len(entries) != len(manifest["files"]):
        raise ValueError("Opening manifest closure differs")
    evidence = {}
    for name, (before, after) in reconstructed.items():
        if unowned_tree(before, OWNED_BODIES[name]) != unowned_tree(after, OWNED_BODIES[name]):
            raise ValueError("Opening patch changed unowned source")
        if entries[name]["before_sha256"] != sha(before) or entries[name]["after_sha256"] != sha(after):
            raise ValueError("Opening exact reconstruction differs")
        compile(after, name, "exec")
        evidence[name] = {"before_sha256": sha(before), "after_sha256": sha(after)}
    direct = {"ml/intraday/opening_preparation.py", "tests/test_intraday_opening_preparation.py",
              "tests/check_intraday_opening_preparation_package.py"}
    if set(manifest["direct_files"]) != direct:
        raise ValueError("Opening direct file closure differs")
    for name, expected in manifest["direct_files"].items():
        if sha((root / name).read_bytes()) != expected:
            raise ValueError("Opening direct bytes differ")
    api = load_pure_functions((root / "ml/intraday/opening_preparation.py").read_bytes())
    result = unittest.TextTestRunner(verbosity=2).run(checks(api))
    print("OPENING_PREPARATION_PACKAGE=" + json.dumps({"owned_files": evidence,
        "tests_run": result.testsRun, "skipped": len(result.skipped), "successful": result.wasSuccessful(),
        "runtime_integration_exercised": False, "provider_calls": 0, "broker_calls": 0}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
