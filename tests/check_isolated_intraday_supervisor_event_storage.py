"""Run portable exact supervisor storage projections with network/process denial."""
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
    parser.add_argument("--basetemp", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    basetemp = args.basetemp.resolve()
    scratch = (root / "scratch").resolve()
    if basetemp == scratch or not basetemp.is_relative_to(scratch):
        parser.error("--basetemp must be a dedicated directory below candidate scratch")
    originals, retained = set(), []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            originals.update(Path(value).resolve().parent for key, value in mapping.items()
                             if key in {"app", "ml", "datafetching"})
        else:
            retained.append(finder)
    sys.meta_path[:] = retained
    originals.discard(root)
    sys.path[:] = [str(root), *[entry for entry in sys.path
        if Path(entry or ".").resolve() not in originals | {root}
        and not (Path(entry or ".").name == "cross_pc" and Path(entry or ".").parent.name == "tools")]]
    sys.dont_write_bytecode = True
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    attempts = []
    def denied(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Supervisor event checks prohibit network and child processes")
    socket.create_connection = socket.getaddrinfo = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied
    def boundary(event, args):
        if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn", "socket.connect", "socket.sendto"}:
            denied()
    sys.addaudithook(boundary)
    import pytest
    paths = ["ml/intraday/supervisor.py", "tests/test_intraday_supervisor_event_storage.py",
        "tests/test_intraday_supervisor_event_storage_integration.py",
        "tests/check_isolated_intraday_supervisor_event_storage.py",
        "docs/development/patches/intraday-supervisor-event-storage.json",
        "docs/development/patches/intraday-supervisor-event-storage.patch"]
    hashes = lambda: {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                      for name in paths if (root / name).is_file()}
    before = hashes()
    config = basetemp.parent / (basetemp.name + "-pytest.ini")
    config.parent.mkdir(parents=True, exist_ok=True)
    if config.exists():
        assert config.read_bytes() == b"[pytest]\n", "Fixture configuration changed"
    else:
        with config.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("[pytest]\n")
    result = pytest.main(["-q", "-p", "no:cacheprovider", "--noconftest", "-c", str(config), "--rootdir=" + str(root),
        str(root / "tests/test_intraday_supervisor_event_storage.py"),
        str(root / "tests/test_intraday_supervisor_event_storage_integration.py"),
        "--basetemp", str(basetemp)])
    assert hashes() == before, "Reviewed source changed during offline checks"
    closure = dict(before)
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if name.split(".")[0] in {"ml", "app", "datafetching", "tests"}:
                assert path.is_relative_to(root), "Application import escaped the candidate"
            if not path.is_relative_to(root) and ".venv" not in path.parts:
                assert not any(path.is_relative_to(original) for original in originals), "Import escaped into another checkout"
            if path.is_relative_to(root) and path.suffix == ".py" and ".venv" not in path.relative_to(root).parts:
                closure[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    print("SUPERVISOR_EVENT_STORAGE_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    print("SUPERVISOR_EVENT_STORAGE_CHECK_RESULT=" + json.dumps(dict(exit_code=result,
        network_or_process_attempts=len(attempts), candidate=str(root), members=len(closure))))
    assert not attempts
    return result


if __name__ == "__main__":
    raise SystemExit(main())
