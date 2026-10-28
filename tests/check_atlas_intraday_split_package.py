"""Verify the portable Atlas split package and run its offline fixture tests.

Common-main consumers and Atlas's installed consumers may differ. A structural
hunk projection is reported as such; only exact before/after bytes establish
complete reconstruction. Local installation additionally requires a complete
reviewed source generation and its unchanged Python runtime.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path
import sys


MANIFEST = "docs/development/patches/atlas-intraday-split.json"


def helper(candidate):
    spec = importlib.util.spec_from_file_location(
        "atlas_split_package_support", candidate / "tests/intraday_split_package_support.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_package(candidate, support):
    manifest = json.loads((candidate / MANIFEST).read_text("utf-8"))
    if manifest.get("schema") != "atlas-intraday-split-package-v1":
        raise ValueError("Unsupported Atlas split package schema")
    patch_raw = (candidate / support.relative(manifest["patch"])).read_bytes()
    if support.sha(patch_raw) != manifest["patch_sha256"]:
        raise ValueError("Reviewed adapter patch changed")
    patches = support.parse_patch(patch_raw)
    entries = {support.relative(entry["path"]): entry for entry in manifest["files"]}
    if len(entries) != len(manifest["files"]) or set(entries) != set(patches):
        raise ValueError("Adapter patch inventory mismatch")
    evidence = {}
    for name, patch in patches.items():
        entry = entries[name]
        if entry["operation"] != "modify" or patch["before"] != name or patch["after"] != name:
            raise ValueError("Existing-consumer adapters must be explicit modifications")
        if any(not support.SHA.fullmatch(entry[key]) for key in ("before_sha256", "after_sha256")):
            raise ValueError("Invalid exact adapter hash")
        support.check_projection(patch["hunks"])
        path = candidate / name
        actual = path.read_bytes() if path.is_file() else None
        if actual is not None and support.sha(actual) == entry["before_sha256"]:
            updated = support.apply_hunks(actual, patch["hunks"])
            if support.sha(updated) != entry["after_sha256"]:
                raise ValueError("Reconstructed adapter differs from reviewed postimage")
            ast.parse(updated.decode("utf-8-sig"), filename=name)
            evidence[name] = "exact_preimage_reconstruction"
        elif actual is not None and support.sha(actual) == entry["after_sha256"]:
            reverse = [{"old_start": h["new_start"], "old_count": h["new_count"],
                        "new_start": h["old_start"], "new_count": h["old_count"],
                        "old_lines": h["new_lines"], "new_lines": h["old_lines"]}
                       for h in patch["hunks"]]
            if support.sha(support.apply_hunks(actual, reverse)) != entry["before_sha256"]:
                raise ValueError("Reverse adapter differs from reviewed preimage")
            ast.parse(actual.decode("utf-8-sig"), filename=name)
            evidence[name] = "exact_postimage_reverse_reconstruction"
        else:
            evidence[name] = "hunk_projection_only_local_exact_preimage_required"
    direct = manifest["direct_files"]
    if not isinstance(direct, dict) or not direct or set(direct) & set(entries):
        raise ValueError("Direct package inventory must be nonempty and disjoint")
    for name, expected in direct.items():
        path = candidate / support.relative(name)
        if not support.SHA.fullmatch(expected) or support.sha(path.read_bytes()) != expected:
            raise ValueError("Direct package file changed: " + name)
        if path.suffix == ".py":
            ast.parse(path.read_text("utf-8-sig"), filename=name)
    tests = manifest["offline_tests"]
    if not isinstance(tests, list) or not tests or len(tests) != len(set(tests)):
        raise ValueError("Explicit unique offline test inventory required")
    for name in tests:
        support.relative(name)
        if not name.startswith("tests/test_") or not name.endswith(".py") or name not in direct:
            raise ValueError("Offline tests must be reviewed direct package members")
    portable = manifest["portable_tests"]
    if not isinstance(portable, list) or not portable or len(portable) != len(set(portable)) or not set(portable) <= set(tests):
        raise ValueError("Explicit portable test subset required")
    return evidence, tests, portable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-only", action="store_true")
    args = parser.parse_args(argv)
    candidate = Path(__file__).resolve().parents[1]
    support = helper(candidate)
    evidence, all_tests, portable = check_package(candidate, support)
    print("ATLAS_SPLIT_ADAPTER_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    if args.package_only:
        return 0
    adapted = all(value == "exact_postimage_reverse_reconstruction" for value in evidence.values())
    # Visit every module during ordinary pytest collection. Narrow pre-import
    # capability guards explicitly skip only unsupported adapter integrations.
    tests = all_tests
    print("ATLAS_SPLIT_INTEGRATION_NOT_RUN=" + json.dumps(
        {"tests": [] if adapted else sorted(set(all_tests) - set(portable)),
         "reason": None if adapted else "Common-base consumer bytes differ; exact local composed generation checks are required."},
        sort_keys=True))
    originals = support.isolate(candidate)
    import pytest
    (candidate / "scratch/checks").mkdir(parents=True, exist_ok=True)
    result = pytest.main(["--noconftest", "-q", "-p", "no:cacheprovider",
                          "--basetemp", str(candidate / "scratch/checks/atlas-intraday-split"),
                          *[str(candidate / name) for name in tests]])
    support.check_imports(candidate, originals)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
                closure[path.relative_to(candidate).as_posix()] = support.sha(path.read_bytes())
    print("ATLAS_SPLIT_PORTABLE_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
