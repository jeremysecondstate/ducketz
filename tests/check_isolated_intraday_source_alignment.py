"""Verify native source alignment in a candidate checkout with network denied."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import socket
import sys


def _isolate_ducketz_imports(candidate: Path) -> set[Path]:
    original_roots = set()
    retained_finders = []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            module = sys.modules.get(name)
            mapping = getattr(module, "MAPPING", {})
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
    for name in ("ml.intraday.coverage", "ml.intraday.data", "ml.intraday.recovered_population"):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(candidate):
            raise RuntimeError("Source alignment import escaped the candidate")
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if (filename and name.split(".")[0] in {"ml", "app", "datafetching", "tests"}
                and not Path(filename).resolve().is_relative_to(candidate)):
            raise RuntimeError("Ducketz application/fixture import escaped the candidate")
        if (filename and not Path(filename).resolve().is_relative_to(candidate)
                and any(Path(filename).resolve().is_relative_to(root) for root in original_roots)
                and ".venv" not in Path(filename).resolve().parts):
            raise RuntimeError("Ducketz application import escaped the candidate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    parser.add_argument("--focused", action="store_true")
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original_roots = _isolate_ducketz_imports(candidate)
    def deny_network(*args, **kwargs):
        raise RuntimeError("Source alignment verification forbids network calls")
    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    sys.addaudithook(lambda event, values: deny_network() if event in {
        "socket.connect", "socket.getaddrinfo", "socket.bind"} else None)
    _assert_candidate_imports(candidate, original_roots)
    import pytest

    required = ["tests/test_intraday_source_alignment.py"]
    adjacent = ["tests/test_intraday_recovered_population.py",
                "tests/test_intraday_dataset_provenance_performance.py", "tests/test_intraday_data.py"]
    missing = [name for name in adjacent if not (candidate / name).is_file()]
    selected = required if args.focused else required + [name for name in adjacent if name not in missing]
    arguments = ["-q", "-p", "no:cacheprovider", *[str(candidate / name) for name in selected]]
    if args.basetemp is not None:
        arguments += ["--basetemp", str(args.basetemp)]
    result = pytest.main(arguments)
    _assert_candidate_imports(candidate, original_roots)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            source = Path(filename).resolve()
            if source.is_relative_to(candidate) and source.suffix == ".py" and ".venv" not in source.parts:
                closure[source.relative_to(candidate).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ("pyproject.toml", "tests/conftest.py", "tests/check_isolated_intraday_source_alignment.py"):
        source = candidate / name
        closure[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    print("SOURCE_ALIGNMENT_UNAVAILABLE_TEST_FILES=" + json.dumps(missing))
    print("SOURCE_ALIGNMENT_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
