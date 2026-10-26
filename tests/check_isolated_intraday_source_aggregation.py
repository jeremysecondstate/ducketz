"""Exercise exact native aggregation in this checkout with network denied."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import socket
import sys


def main():
    candidate = Path(__file__).resolve().parents[1]
    original_roots = set()
    finders = []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            original_roots.update(Path(mapping[k]).resolve().parent for k in ("ml", "app", "datafetching") if k in mapping)
        else:
            finders.append(finder)
    sys.meta_path[:] = finders
    original_roots.discard(candidate)
    paths = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        helper_collision = (path.name == "cross_pc" and path.parent.name == "tools"
                            and (path / "requests.py").is_file() and (path / "test_runner.py").is_file())
        if path not in original_roots | {candidate} and not helper_collision:
            paths.append(entry)
    sys.path[:] = [str(candidate), *paths]
    def deny(*args, **kwargs):
        raise RuntimeError("Aggregation checks prohibit network")
    socket.create_connection = deny
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    sys.addaudithook(lambda event, args: deny() if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"} else None)
    import pytest
    selected = ["tests/test_intraday_source_aggregation_latency.py"]
    result = pytest.main([*selected, "-q", "-p", "no:cacheprovider"])
    closure = {}
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if name.split(".")[0] in {"ml", "app", "datafetching", "tests"} and not path.is_relative_to(candidate):
            raise RuntimeError("Aggregation import escaped this checkout: " + name)
        if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
            closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for relative in [*selected, "tests/check_isolated_intraday_source_aggregation.py", "tests/conftest.py", "pyproject.toml"]:
        path = candidate / relative
        closure[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    print("AGGREGATION_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
