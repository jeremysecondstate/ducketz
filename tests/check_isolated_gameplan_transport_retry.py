"""Run safe-read retry regressions from this exact candidate without network."""
from pathlib import Path
import hashlib
import importlib
import json
import socket
import sys
import tempfile


def main():
    candidate = Path(__file__).resolve().parents[1]
    originals = set()
    retained = []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            originals.update(Path(mapping[p]).resolve().parent for p in ("app", "datafetching", "ml") if p in mapping)
        else:
            retained.append(finder)
    sys.meta_path[:] = retained
    originals.discard(candidate)
    retained = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        collision = path.name == "cross_pc" and path.parent.name == "tools" and (path/"requests.py").is_file()
        if path not in originals | {candidate} and not collision:
            retained.append(entry)
    sys.path[:] = [str(candidate), *retained]
    def deny(*args, **kwargs):
        raise RuntimeError("Network disabled during safe-read retry checks")
    socket.create_connection = socket.getaddrinfo = deny
    socket.socket.connect = socket.socket.connect_ex = deny
    sys.addaudithook(lambda event, args: deny() if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"} else None)
    import pytest
    basetemp = tempfile.mkdtemp(prefix="ducketz-gameplan-read-retry-")
    result = pytest.main(["-q", "-p", "no:cacheprovider",
        "--basetemp", basetemp,
        str(candidate/"tests/test_gameplan_transport_capture_retry.py"),
        str(candidate/"tests/test_schwab_retry.py"),
        str(candidate/"tests/test_stock_trader.py"),
        "-k", "transport_capture_retry or schwab_retry or runtime"])
    runtime = importlib.import_module("ml.stock_trader.runtime")
    assert Path(runtime.__file__).resolve().is_relative_to(candidate)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
                closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif ".venv" not in path.parts and any(path.is_relative_to(root) for root in originals):
                raise RuntimeError("Application import escaped candidate: " + str(path))
    for name in ("pyproject.toml", "datafetching/watchlist.txt", "tests/conftest.py", "tests/check_isolated_gameplan_transport_retry.py"):
        closure[name] = hashlib.sha256((candidate/name).read_bytes()).hexdigest()
    print("GAMEPLAN_TRANSPORT_RETRY_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
