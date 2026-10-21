"""Offline Gameplan own-capacity regressions on one exact candidate graph."""
from pathlib import Path
import argparse
import hashlib
import json
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_isolated_gameplan_session import _isolate_ducketz_imports, _assert_candidate_imports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    parser.add_argument("--focused", action="store_true")
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original = _isolate_ducketz_imports(candidate)
    def no_network(*args, **kwargs):
        raise RuntimeError("Network disabled for Gameplan capacity offline checks")
    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    _assert_candidate_imports(candidate, original)
    import pytest
    files = ["test_gameplan_authority_capacity.py"]
    if not args.focused:
        files += ["test_account_gameplan_authority.py", "test_account_gameplan_execution.py",
                  "test_gameplan_direction_engine.py", "test_oct19_direction_execution.py",
                  "test_gameplan_batch_refresh.py", "test_independent_stock_runtime.py"]
    arguments = ["-q", "-p", "no:cacheprovider", *[str(candidate / "tests" / name) for name in files]]
    if not args.focused:
        # These four original cases request the old fixed policy with September
        # forecasts at an October-19 clock. The permanent effective policy now
        # selects Gameplan, so they exit before capture on both original and
        # repaired source. Keep their original baseline failures as evidence.
        arguments += ["-k", "not test_signal_deadline_at_normal_close_remains_expired_read_failure "
            "and not test_final_calendar_close_distinguishes_actual_signal_expiry "
            "and not test_close_during_broker_retry_keeps_unrecovered_read_failure"]
    if args.basetemp:
        arguments += ["--basetemp", str(args.basetemp)]
    result = pytest.main(arguments)
    _assert_candidate_imports(candidate, original)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(candidate) and path.suffix == ".py" and ".venv" not in path.parts:
                closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ["pyproject.toml", "datafetching/watchlist.txt", "tests/conftest.py", "tests/check_isolated_gameplan_authority_capacity.py"]:
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    print("GAMEPLAN_AUTHORITY_CAPACITY_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
