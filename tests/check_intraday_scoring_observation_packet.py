"""Verify the exact scoring-observation patch and inert portable dispatch tests.

The public packet carries source changes as data. A recipient must separately
review and test its complete local observation adapters before installation.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile


MANIFEST = "docs/development/patches/intraday-scoring-observation.json"
TEST = "tests/test_intraday_scoring_observation.py"
CHECKER = "tests/check_intraday_scoring_observation_packet.py"
RUNTIME = ("ml/intraday/scoring_observation.py", "ml/intraday/final_scoring.py",
           "ml/intraday/split_scoring.py")
GUARD_INPUTS = ("ml/intraday/data.py", "ml/intraday/coverage.py",
                "ml/intraday/recovered_population.py", "ml/intraday/store.py",
                "ml/intraday/final_scoring.py", "ml/intraday/split_scoring.py",
                "ml/market_gap_evidence.py", "tests/test_intraday_recovered_population.py",
                "tests/test_intraday_store.py")


def normalized(raw):
    return raw.replace(b"\r\n", b"\n")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_support(candidate):
    path = candidate / "tests/intraday_split_package_support.py"
    spec = importlib.util.spec_from_file_location("scoring_packet_support", path)
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    return support


def reconstructed_sources(candidate, support):
    manifest = json.loads((candidate / MANIFEST).read_bytes())
    if (manifest.get("schema") != "intraday-scoring-observation-package-v1"
            or manifest.get("normalization") != "CRLF to LF only"):
        raise ValueError("Unsupported scoring-observation packet")
    patch_name = support.relative(manifest["patch"])
    patch_raw = (candidate / patch_name).read_bytes()
    if digest(patch_raw) != manifest["patch_sha256"]:
        raise ValueError("Reviewed scoring patch changed")
    patches = support.parse_patch(patch_raw)
    entries = {support.relative(row["path"]): row for row in manifest["files"]}
    if len(entries) != len(manifest["files"]) or set(entries) != set(patches) or set(entries) != set(RUNTIME):
        raise ValueError("Scoring runtime patch inventory changed")
    source, evidence = {}, {}
    for name in RUNTIME:
        entry, patch = entries[name], patches[name]
        after = entry["after_sha256"]
        if not support.SHA.fullmatch(after):
            raise ValueError("Invalid reviewed postimage")
        actual_path = candidate / name
        actual = normalized(actual_path.read_bytes()) if actual_path.is_file() else None
        if entry["operation"] == "add":
            if (name != RUNTIME[0] or entry["before_sha256"] is not None
                    or patch["before"] is not None or patch["after"] != name):
                raise ValueError("Unexpected added runtime file")
            updated = support.apply_hunks(b"", patch["hunks"])
            if actual is not None and digest(actual) != after:
                raise ValueError("Existing added source differs from the reviewed postimage")
            evidence[name] = "exact_added_source_reconstruction" if actual is None else "exact_postimage"
        elif entry["operation"] == "modify":
            before = entry["before_sha256"]
            if (name == RUNTIME[0] or not support.SHA.fullmatch(before)
                    or patch["before"] != name or patch["after"] != name or actual is None):
                raise ValueError("Unexpected modified runtime file")
            if digest(actual) == before:
                updated = support.apply_hunks(actual, patch["hunks"])
                evidence[name] = "exact_preimage_reconstruction"
            elif digest(actual) == after:
                reverse = [{"old_start": row["new_start"], "old_count": row["new_count"],
                            "new_start": row["old_start"], "new_count": row["old_count"],
                            "old_lines": row["new_lines"], "new_lines": row["old_lines"]}
                           for row in patch["hunks"]]
                if digest(support.apply_hunks(actual, reverse)) != before:
                    raise ValueError("Reverse patch differs from the reviewed preimage")
                updated = actual
                evidence[name] = "exact_postimage_reverse_reconstruction"
            else:
                raise ValueError("Runtime source is neither the exact preimage nor postimage")
        else:
            raise ValueError("Only reviewed add/modify operations are supported")
        if digest(updated) != after:
            raise ValueError("Reconstructed runtime source differs from the reviewed postimage")
        ast.parse(updated.decode("utf-8-sig"), filename=name)
        source[name] = updated
    direct = manifest["direct_files"]
    if set(direct) != {TEST, CHECKER}:
        raise ValueError("Direct source packet inventory changed")
    for name, expected in direct.items():
        if not support.SHA.fullmatch(expected) or digest((candidate / support.relative(name)).read_bytes()) != expected:
            raise ValueError("Reviewed packet member changed: " + name)
    return manifest, patch_name, source, evidence


class PortableProjection:
    def __init__(self, source_root):
        self.source_root = source_root
        self.real_cases_not_run = 0

    def pytest_collection_modifyitems(self, items):
        import pytest
        for item in items:
            if Path(item.module.__file__).name != Path(TEST).name:
                continue
            item.module.ROOT = self.source_root
            if item.cls is not None and item.cls.__name__ == "TestRealObservationIntegration":
                self.real_cases_not_run += 1
                item.add_marker(pytest.mark.skip(reason="Portable source packet tests inert dispatch only; complete local adapter integration is separate"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--basetemp")
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    support = read_support(candidate)
    manifest, patch_name, sources, evidence = reconstructed_sources(candidate, support)
    if args.integration and any(value not in {"exact_postimage", "exact_postimage_reverse_reconstruction"} for value in evidence.values()):
        raise ValueError("Full integration requires all three reviewed runtime postimages already installed in the candidate")
    originals = support.isolate(candidate)
    network_attempts, subprocess_attempts = [], []

    def denied(*values, **kwargs):
        network_attempts.append(True)
        raise AssertionError("Scoring packet checks prohibit network/provider calls")

    socket.create_connection = socket.getaddrinfo = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied

    def boundary(event, values):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            denied()
        if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn"}:
            subprocess_attempts.append(event)
            raise AssertionError("Scoring packet checks prohibit subprocess operations")

    sys.addaudithook(boundary)
    import pytest
    targets = [TEST]
    if args.integration:
        targets += ["tests/test_intraday_final_scoring.py", "tests/test_intraday_final_scoring_indexed.py",
                    "tests/test_intraday_final_scoring_concurrency.py", "tests/test_intraday_split_scoring.py",
                    "tests/test_intraday_split_scoring_continuation.py", "tests/test_intraday_recovered_population.py"]
    with tempfile.TemporaryDirectory(prefix="ducketz-scoring-packet-") as temp_name:
        projected = Path(temp_name) / "source"
        for name, raw in sources.items():
            path = projected / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        plugin = PortableProjection(projected)
        arguments = ["-q", "-p", "no:cacheprovider", "--confcutdir=" + str(candidate),
                     "--rootdir=" + str(candidate), *[str(candidate / name) for name in targets],
                     "--basetemp", args.basetemp or str(Path(temp_name) / "pytest")]
        code = pytest.main(arguments, plugins=[] if args.integration else [plugin])
        support.check_imports(candidate, originals)
        closure = {}
        for name, module in list(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if filename:
                path = Path(filename).resolve()
                if name.split(".")[0] in {"ml", "tools", "app", "datafetching", "tests"} and not path.is_relative_to(candidate):
                    raise AssertionError("Regression imported a different application checkout")
                if path.is_relative_to(candidate) and path.suffix == ".py":
                    closure[path.relative_to(candidate).as_posix()] = digest(path.read_bytes())
        absent_guards = []
        for name in [MANIFEST, patch_name, TEST, CHECKER, "pyproject.toml", "tests/conftest.py",
                     "tests/intraday_split_package_support.py", *GUARD_INPUTS]:
            path = candidate / name
            if path.is_file():
                closure[name] = digest(path.read_bytes())
            elif name in GUARD_INPUTS:
                absent_guards.append(name)
        result = {"exit_code": code, "network_attempts": len(network_attempts),
                  "subprocess_attempts": len(subprocess_attempts), "candidate": str(candidate),
                  "integration": args.integration, "members": len(closure),
                  "runtime_patch_evidence": evidence, "reconstructed_source_hashes": {name: digest(raw) for name, raw in sources.items()},
                  "real_integration_cases_not_run": 0 if args.integration else plugin.real_cases_not_run,
                  "absent_guard_inputs": absent_guards,
                  "public_runtime_dependency_closure_claimed": False,
                  "local_runtime_postimages_verified": args.integration,
                  "scope": "Complete local offline fixtures" if args.integration else "Exact source patch reconstruction and inert portable helper dispatch"}
        print("SCORING_OBSERVATION_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
        print("SCORING_OBSERVATION_RESULT=" + json.dumps(result, sort_keys=True))
        if network_attempts or subprocess_attempts:
            raise AssertionError("Offline packet checks attempted network or process operations")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
