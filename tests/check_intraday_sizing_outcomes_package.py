"""Run portable sizing-cache outcome regressions with isolated imports and no network."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    args = parser.parse_args()
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
            original_roots.update(Path(path).resolve().parent for path in mapping.values())
        else:
            finders.append(finder)
    sys.meta_path[:] = finders
    original_roots.discard(candidate)
    retained = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        collision = (path.name == "cross_pc" and (path / "test_runner.py").is_file()
                     and (path / "requests.py").is_file())
        if path not in original_roots | {candidate} and not collision:
            retained.append(entry)
    sys.path[:] = [str(candidate), *retained]
    attempts = []
    def denied(*args, **kwargs):
        attempts.append("socket operation")
        raise RuntimeError("Partial-header checks prohibit network/provider actions")
    socket.create_connection = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied
    def offline_boundary(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo",
                     "socket.gethostbyname", "socket.gethostbyaddr", "subprocess.Popen",
                     "os.system", "os.spawn", "os.posix_spawn"}:
            attempts.append(event)
            raise RuntimeError("Partial-header checks prohibit network/subprocess actions")
    sys.addaudithook(offline_boundary)
    scratch = (candidate / "scratch").resolve()
    basetemp = (args.basetemp or scratch / "checks/sizing-cache-outcomes").resolve()
    if basetemp == scratch or not basetemp.is_relative_to(scratch):
        raise RuntimeError("Partial-header fixtures require candidate-owned scratch")
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    import pytest
    result = pytest.main(["-q", "--noconftest", "-p", "no:cacheprovider",
        "--rootdir", str(candidate), "--basetemp", str(basetemp),
        str(candidate / "tests/test_intraday_partial_sizing_reads.py"),
        str(candidate / "tests/test_intraday_sizing_cache_diagnostics.py"),
        str(candidate / "tests/test_intraday_sizing_read_deadline.py")])
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
            closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if ".venv" not in path.parts and any(path.is_relative_to(root) for root in original_roots):
            raise RuntimeError("Partial-header checks imported mutable application source")
    for name in ("ml/intraday/sizing_header_cache.py", "tests/test_intraday_partial_sizing_reads.py",
                 "tests/test_intraday_sizing_cache_diagnostics.py",
                 "tests/check_intraday_sizing_outcomes_package.py"):
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    print("SIZING_CACHE_OUTCOME_CHECK=" + json.dumps({"exit_code": result,
        "network_attempts": len(attempts), "import_closure": closure}, sort_keys=True))
    if attempts:
        raise RuntimeError("Partial-header checks attempted forbidden external actions")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
