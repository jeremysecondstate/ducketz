"""Run exact candidate history-contention fixtures with network/process denial."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    support = root / "tests/intraday_split_package_support.py"
    spec = importlib.util.spec_from_file_location("history_contention_isolation", support)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    originals = helper.isolate(root)
    attempts = []
    def denied(*values, **kwargs):
        attempts.append(True)
        raise AssertionError("History contention checks prohibit network and processes")
    socket.create_connection = socket.getaddrinfo = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied
    def boundary(event, values):
        if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn", "socket.connect", "socket.sendto"}:
            denied()
    sys.addaudithook(boundary)
    import pytest
    source_names = ["ml/intraday/live_session.py", "tests/test_intraday_renewal_history_contention.py",
        "tests/check_isolated_intraday_renewal_history_contention.py", "tests/intraday_split_package_support.py",
        "docs/development/patches/intraday-renewal-history-contention.json",
        "docs/development/patches/intraday-renewal-history-contention.patch"]
    hashes = lambda: {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                     for name in source_names if (root / name).is_file()}
    before = hashes()
    basetemp = args.basetemp or tempfile.mkdtemp(prefix="ducketz-history-contention-")
    code = pytest.main(["-q", "-p", "no:cacheprovider", "--confcutdir=" + str(root), "--rootdir=" + str(root),
        str(root / "tests/test_intraday_renewal_history_contention.py"), "--basetemp", basetemp])
    assert before == hashes(), "Reviewed candidate source changed during the offline checks"
    helper.check_imports(root, originals)
    closure = dict(before)
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if name.split(".")[0] in {"ml", "app", "datafetching", "tests"}:
            assert path.is_relative_to(root), "Fixture imported a different checkout"
        if path.is_relative_to(root) and path.suffix == ".py" and ".venv" not in path.relative_to(root).parts:
            closure[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    print("RENEWAL_HISTORY_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    print("RENEWAL_HISTORY_CHECK_RESULT=" + json.dumps(dict(exit_code=code, network_or_process_attempts=len(attempts),
        candidate=str(root), members=len(closure))))
    assert not attempts, "A fixture attempted network or subprocess use"
    return code


if __name__ == "__main__":
    raise SystemExit(main())
