"""Check retry-journal capacity and publication with offline temporary fixtures."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import uuid


def _isolate_ducketz_imports(candidate: Path) -> set[Path]:
    original_roots, retained_finders = set(), []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            for package in ("app", "datafetching", "ml"):
                if package in mapping:
                    original_roots.add(Path(mapping[package]).resolve().parent)
        else:
            retained_finders.append(finder)
    sys.meta_path[:] = retained_finders
    original_roots.discard(candidate)
    retained_paths = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        collision = (path.name == "cross_pc" and path.parent.name == "tools"
                     and (path / "test_runner.py").is_file() and (path / "requests.py").is_file())
        if path not in original_roots | {candidate} and not collision:
            retained_paths.append(entry)
    sys.path[:] = [str(candidate), *retained_paths]
    return original_roots


def _assert_candidate_imports(candidate: Path, original_roots: set[Path]) -> None:
    module = importlib.import_module("ml.intraday.provider_recovery")
    if not Path(module.__file__).resolve().is_relative_to(candidate):
        raise RuntimeError("Retry-journal source imported from a different checkout")
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        app_or_fixture = name.split(".")[0] in {"ml", "app", "datafetching", "tests"} or name.startswith("test_")
        if app_or_fixture and not path.is_relative_to(candidate):
            raise RuntimeError("Application or fixture import escaped the candidate")
        if (not path.is_relative_to(candidate) and ".venv" not in path.parts and
                any(path.is_relative_to(root) for root in original_roots)):
            raise RuntimeError("Application import escaped the candidate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original_roots = _isolate_ducketz_imports(candidate)
    scratch = candidate / "scratch"
    basetemp = (args.basetemp or scratch / ("retry-journal-capacity-" + uuid.uuid4().hex)).resolve()
    if not basetemp.is_relative_to(scratch.resolve()) or basetemp == scratch.resolve():
        parser.error("--basetemp must be a dedicated directory inside this candidate's scratch")
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    attempts = []

    def deny_network(*args, **kwargs):
        attempts.append("network")
        raise RuntimeError("Retry journal capacity checks prohibit network/provider actions")

    socket.create_connection = socket.getaddrinfo = deny_network
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = deny_network

    def offline_boundary(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn"}:
            attempts.append(event)
            raise RuntimeError("Retry journal capacity checks prohibit network/subprocess actions")

    sys.addaudithook(offline_boundary)
    _assert_candidate_imports(candidate, original_roots)
    import pytest

    targets = ["tests/test_intraday_retry_journal_capacity.py", "tests/test_intraday_retry_journal_replace.py"]
    fixed = ["ml/intraday/provider_recovery.py", "pyproject.toml", "tests/conftest.py",
             "tests/check_isolated_intraday_retry_journal_capacity.py", *targets]
    fingerprints = lambda: {name: hashlib.sha256((candidate / name).read_bytes()).hexdigest()
                            for name in fixed}
    before = fingerprints()
    result = pytest.main(["-q", "-p", "no:cacheprovider", "--basetemp", str(basetemp),
                          "--rootdir=" + str(candidate), "--confcutdir=" + str(candidate),
                          *[str(candidate / name) for name in targets]])
    if before != fingerprints():
        raise RuntimeError("Reviewed retry-journal candidate changed during checks")
    _assert_candidate_imports(candidate, original_roots)
    closure = dict(before)
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            source = Path(filename).resolve()
            if source.is_relative_to(candidate) and source.suffix == ".py" and ".venv" not in source.parts:
                closure[source.relative_to(candidate).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    print("INTRADAY_RETRY_JOURNAL_CAPACITY_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    print("INTRADAY_RETRY_JOURNAL_CAPACITY_CHECK_RESULT=" + json.dumps(dict(
        exit_code=result, network_or_process_attempts=len(attempts), candidate=str(candidate), members=len(closure))))
    if attempts:
        raise RuntimeError("Retry-journal capacity fixture attempted network or subprocess work")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
