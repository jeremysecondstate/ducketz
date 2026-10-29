"""Confined offline checks for the forecast cache's configured time budget."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def main():
    candidate = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    original_roots = set()
    finders = []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            original_roots.update(Path(p).resolve().parent for p in mapping.values())
        else:
            finders.append(finder)
    sys.meta_path[:] = finders
    original_roots.discard(candidate)
    paths = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        collision = path.name == "cross_pc" and (path / "test_runner.py").is_file()
        if path not in original_roots | {candidate} and not collision:
            paths.append(entry)
    sys.path[:] = [str(candidate), *paths]
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("network")
        raise RuntimeError("Forecast cache budget checks are offline")

    socket.create_connection = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied

    def boundary(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo",
                     "socket.gethostbyname", "socket.gethostbyaddr",
                     "subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn"}:
            attempts.append(event)
            raise RuntimeError("Forecast cache budget checks prohibit external operations")

    sys.addaudithook(boundary)
    (candidate / "scratch/checks").mkdir(parents=True, exist_ok=True)
    import pytest
    names = [
        "ml/intraday/forecast_header_cache.py",
        "tests/test_intraday_forecast_cache_budget.py",
        "tests/test_intraday_partial_header_reads.py",
        "tests/test_intraday_durable_forecast_diagnostics.py",
        "tests/check_isolated_intraday_forecast_cache_budget.py",
    ]
    result = pytest.main([
        "-q", "--noconftest", "-p", "no:cacheprovider",
        "--rootdir", str(candidate),
        "--basetemp", str(candidate / "scratch/checks/forecast-cache-budget"),
        *[str(candidate / name) for name in names[1:-1]],
    ])
    closure = {name: hashlib.sha256((candidate / name).read_bytes()).hexdigest()
               for name in names}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
            closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if ".venv" not in path.parts and any(path.is_relative_to(root) for root in original_roots):
            raise RuntimeError("Application import escaped the candidate")
    print("FORECAST_CACHE_BUDGET_CHECK=" + json.dumps(
        dict(exit_code=result, external_attempts=len(attempts), import_closure=closure,
             integration_scope="Synthetic cache regressions; live recovery remains separate"),
        sort_keys=True))
    if attempts:
        raise RuntimeError("Forbidden external operation attempted")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
