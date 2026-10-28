"""Check warm recovery boundaries and partial headers in an offline candidate."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sys


def isolate(candidate):
    original_roots = set()
    retained_finders = []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            original_roots.update(Path(path).resolve().parent for path in mapping.values())
        else:
            retained_finders.append(finder)
    sys.meta_path[:] = retained_finders
    original_roots.discard(candidate)
    retained_paths = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        collision = (path.name == "cross_pc" and (path / "test_runner.py").is_file()
                     and (path / "requests.py").is_file())
        if path not in original_roots | {candidate} and not collision:
            retained_paths.append(entry)
    sys.path[:] = [str(candidate), *retained_paths]
    return original_roots


def assert_candidate_imports(candidate, original_roots):
    for name in ("ml.intraday.store", "ml.intraday.intentions",
                 "ml.intraday.forecast_header_cache", "ml.intraday.sizing_header_cache"):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(candidate):
            raise RuntimeError("Recovery-boundary API imported outside candidate")
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if name.split(".")[0] in {"ml", "app", "datafetching", "tests"} and not path.is_relative_to(candidate):
            raise RuntimeError("Application or fixture import escaped candidate")
        if (not path.is_relative_to(candidate) and ".venv" not in path.parts
                and any(path.is_relative_to(root) for root in original_roots)):
            raise RuntimeError("Recovery-boundary check imported mutable source")


class Results:
    def __init__(self):
        self.passed = self.failed = self.skipped = 0

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.passed += int(report.passed)
            self.failed += int(report.failed)
            self.skipped += int(report.skipped)
        elif report.failed:
            self.failed += 1
        elif report.skipped:
            self.skipped += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    original_roots = isolate(candidate)
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("socket operation")
        raise RuntimeError("Recovery-boundary checks prohibit external actions")

    socket.create_connection = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied

    def offline_boundary(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo",
                     "socket.gethostbyname", "socket.gethostbyaddr", "subprocess.Popen",
                     "os.system", "os.spawn", "os.posix_spawn"}:
            attempts.append(event)
            raise RuntimeError("Recovery-boundary checks prohibit network/subprocess actions")

    sys.addaudithook(offline_boundary)
    scratch = (candidate / "scratch").resolve()
    basetemp = (args.basetemp or scratch / "checks/warm-fixture-boundary").resolve()
    if basetemp == scratch or not basetemp.is_relative_to(scratch):
        raise RuntimeError("Recovery-boundary fixtures require candidate-owned scratch")
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    targets = ["tests/test_intraday_distinct_recovery_boundaries.py",
               "tests/test_intraday_partial_header_reads.py"]
    import pytest
    results = Results()
    result = pytest.main(["-q", "--noconftest", "-p", "no:cacheprovider",
        "--rootdir", str(candidate), "--basetemp", str(basetemp),
        *[str(candidate / name) for name in targets]], plugins=[results])
    assert_candidate_imports(candidate, original_roots)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
                closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    artifacts = [*targets, "tests/check_intraday_warm_fixture_package.py",
        "docs/development/patches/intraday-warm-fixture-boundary.patch",
        "docs/development/patches/intraday-warm-fixture-boundary.json"]
    for name in artifacts:
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    exact_cases_passed = results.passed == 31 and results.failed == results.skipped == 0
    summary = dict(exit_code=result, passed=results.passed, failed=results.failed,
        skipped=results.skipped, expected_passes=31, exact_cases_passed=exact_cases_passed,
        network_attempts=len(attempts), targets=targets, import_closure=closure)
    print("WARM_FIXTURE_CHECK=" + json.dumps(summary, sort_keys=True))
    if attempts or not exact_cases_passed:
        raise RuntimeError("Recovery-boundary checks require all 31 offline cases without skips")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
