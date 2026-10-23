"""Check final scoring concurrency projection in an isolated network-denied checkout."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys

_helper_spec = importlib.util.spec_from_file_location(
    "intraday_forecast_retry_fairness_checks",
    Path(__file__).with_name("check_isolated_intraday_forecast_retry_fairness.py"))
_helper = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(_helper)
_assert_candidate_imports = _helper._assert_candidate_imports
_isolate_ducketz_imports = _helper._isolate_ducketz_imports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    parser.add_argument("--focused", action="store_true")
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original_roots = _isolate_ducketz_imports(candidate)

    def deny_network(*args, **kwargs):
        raise RuntimeError("Final scoring concurrency checks prohibit network/provider actions")

    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.socket.sendto = deny_network

    def offline_boundary(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn"}:
            raise RuntimeError("Final scoring concurrency checks prohibit network/subprocess actions")

    sys.addaudithook(offline_boundary)
    _assert_candidate_imports(candidate, original_roots)
    import pytest

    targets = ["tests/test_intraday_final_scoring_concurrency.py"]
    if not args.focused:
        targets += ["tests/test_intraday_store.py", "tests/test_intraday_live_session.py",
                    "tests/test_intraday_final_scoring.py", "tests/test_intraday_scoring_selection.py"]
    arguments = ["-q", "-p", "no:cacheprovider", *[str(candidate / name) for name in targets]]
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
    for name in ("pyproject.toml", "tests/conftest.py", "tests/check_isolated_intraday_final_scoring_concurrency.py",
                 "tests/check_isolated_intraday_forecast_retry_fairness.py"):
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    print("INTRADAY_FINAL_SCORING_CONCURRENCY_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
