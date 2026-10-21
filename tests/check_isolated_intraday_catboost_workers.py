"""Run numeric model worker regressions from an isolated, network-denied checkout."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import socket
import sys


def _isolate_imports(candidate):
    original_roots = set()
    finders = []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            original_roots.update(Path(mapping[name]).resolve().parent
                                  for name in ("app", "datafetching", "ml") if name in mapping)
        else:
            finders.append(finder)
    sys.meta_path[:] = finders
    original_roots.discard(candidate)
    paths = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        collision = (path.name == "cross_pc" and path.parent.name == "tools"
                     and (path / "requests.py").is_file() and (path / "test_runner.py").is_file())
        if path not in original_roots | {candidate} and not collision:
            paths.append(entry)
    sys.path[:] = [str(candidate), *paths]
    return original_roots


def _assert_imports(candidate, original_roots):
    module = importlib.import_module("ml.intraday.models")
    assert Path(module.__file__).resolve().is_relative_to(candidate)
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if name.split(".")[0] in {"ml", "app", "datafetching", "tests"}:
                assert path.is_relative_to(candidate), f"Application/fixture import escaped candidate: {name}"
            if (not path.is_relative_to(candidate) and ".venv" not in path.parts
                    and any(path.is_relative_to(root) for root in original_roots)):
                raise RuntimeError("Application import escaped candidate")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original_roots = _isolate_imports(candidate)

    def deny_network(*args, **kwargs):
        raise RuntimeError("CatBoost worker regression forbids network calls")

    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    sys.addaudithook(lambda event, values: deny_network() if event in {
        "socket.connect", "socket.getaddrinfo", "socket.bind"} else None)
    _assert_imports(candidate, original_roots)
    import pytest
    checks = ("test_intraday_catboost_worker_contract.py", "test_intraday_models.py",
              "test_intraday_model_concurrency.py", "test_intraday_full_catalog_fits.py")
    arguments = ["-q", "-p", "no:cacheprovider", *[str(candidate / "tests" / name) for name in checks]]
    if args.basetemp is not None:
        arguments += ["--basetemp", str(args.basetemp)]
    result = pytest.main(arguments)
    _assert_imports(candidate, original_roots)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
                closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("pyproject.toml", "tests/conftest.py", "tests/check_isolated_intraday_catboost_workers.py"):
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    print("INTRADAY_CATBOOST_WORKERS_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
