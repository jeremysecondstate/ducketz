"""Check transient-sizing dispatch in an isolated network-denied checkout."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import socket
import sys


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
    for name in ("ml.intraday.live_session", "ml.intraday.runtime", "ml.intraday.store"):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(candidate):
            raise RuntimeError("Transient sizing imported from a different checkout")
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if name.split(".")[0] in {"ml", "app", "datafetching", "tests"} and not path.is_relative_to(candidate):
            raise RuntimeError("Application or fixture import escaped the candidate")
        if (not path.is_relative_to(candidate) and ".venv" not in path.parts and
                any(path.is_relative_to(root) for root in original_roots)):
            raise RuntimeError("Application import escaped the candidate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    parser.add_argument("--focused", action="store_true")
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original_roots = _isolate_ducketz_imports(candidate)

    def deny_network(*args, **kwargs):
        raise RuntimeError("Transient-sizing checks prohibit network/provider actions")

    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.socket.sendto = deny_network

    def offline_boundary(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn"}:
            raise RuntimeError("Transient-sizing checks prohibit network/subprocess actions")

    sys.addaudithook(offline_boundary)
    _assert_candidate_imports(candidate, original_roots)
    import pytest

    targets = ["tests/test_intraday_transient_sizing_retry.py"]
    if not args.focused:
        targets += ["tests/test_intraday_live_session.py", "tests/test_intraday_candle_close.py", "tests/test_intraday_candle_completion.py",
                    "tests/test_intraday_final_scoring.py", "tests/test_intraday_collection_storage_retry.py",
                    "tests/test_intraday_collection_completion_retry.py", "tests/test_intraday_intentions.py",
                    "tests/test_intraday_starter_sizing.py", "tests/test_intraday_sizing_freshness.py",
                    "tests/test_intraday_expired_no_order.py"]
        # Later installed bases may supply these cadence/recovery fixtures.
        targets += [name for name in ("tests/test_intraday_collection_cadence.py",
                    "tests/test_intraday_unpublished_recovery.py", "tests/test_intraday_repair_integration.py")
                    if (candidate / name).is_file()]
    arguments = ["-q", "-p", "no:cacheprovider", *[str(candidate / name) for name in targets]]
    if args.basetemp is not None:
        args.basetemp.parent.mkdir(parents=True, exist_ok=True)
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
    for name in ("pyproject.toml", "tests/conftest.py", "tests/check_isolated_intraday_transient_sizing_retry.py"):
        source = candidate / name
        closure[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    print("INTRADAY_TRANSIENT_SIZING_RETRY_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
