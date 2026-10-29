"""Run portable storage-maintenance checks without network or child processes.

Maintenance fixtures, the exact adapter packet and guarded integration fixtures
run here. A known public base explicitly skips uninstalled adapters; this runner
does not substitute for testing the separately composed private runtime source.
"""
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
                             if key in {"app", "ml", "datafetching", "tools"})
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
        raise AssertionError("Storage maintenance checks prohibit network and child processes")
    socket.create_connection = socket.getaddrinfo = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied
    def boundary(event, args):
        if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn", "socket.connect", "socket.sendto"}:
            denied()
    sys.addaudithook(boundary)
    import pytest
    tests = ["tests/test_atlas_datastore_manager.py", "tests/test_intraday_storage_migration.py",
             "tests/test_intraday_snapshot_compaction.py", "tests/test_intraday_shared_storage_package.py",
             "tests/test_intraday_acquisition_objects.py", "tests/test_intraday_canonical_acquisition.py",
             "tests/test_intraday_market_response_objects.py", "tests/test_intraday_model_snapshot_references.py",
             "tests/test_intraday_snapshot_reference_readers.py", "tests/test_intraday_reference_provenance_objects.py"]
    paths = ["ml/intraday/prediction.py", "ml/intraday/snapshot_objects.py",
             "tools/atlas_datastore_manager.py", "tools/intraday_storage_migration.py",
             "tools/intraday_snapshot_compaction.py", "tests/check_isolated_intraday_shared_storage.py",
             "tests/intraday_shared_storage_support.py",
             "docs/development/patches/intraday-shared-storage.json",
             "docs/development/patches/intraday-shared-storage.patch", *tests]
    hashes = lambda: {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}
    before = hashes()
    config = basetemp.parent / (basetemp.name + "-pytest.ini")
    config.parent.mkdir(parents=True, exist_ok=True)
    if config.exists():
        assert config.read_bytes() == b"[pytest]\n", "Fixture configuration changed"
    else:
        with config.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("[pytest]\n")
    result = pytest.main(["-q", "-s", "-p", "no:cacheprovider", "--noconftest", "-c", str(config),
        "--rootdir=" + str(root), *[str(root / name) for name in tests], "--basetemp", str(basetemp)])
    assert hashes() == before, "Reviewed source changed during offline checks"
    closure = dict(before)
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if name.split(".")[0] in {"ml", "app", "datafetching", "tests", "tools"}:
                assert path.is_relative_to(root), "Application import escaped candidate"
            if not path.is_relative_to(root) and ".venv" not in path.parts:
                assert not any(path.is_relative_to(original) for original in originals), "Import escaped into another checkout"
            if path.is_relative_to(root) and path.suffix == ".py" and ".venv" not in path.relative_to(root).parts:
                closure[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    print("SHARED_STORAGE_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    print("SHARED_STORAGE_CHECK_RESULT=" + json.dumps(dict(exit_code=result,
        network_or_process_attempts=len(attempts), candidate=str(root), members=len(closure),
        scope="maintenance_packet_and_guarded_adapter_fixtures")))
    assert not attempts
    return result


if __name__ == "__main__":
    raise SystemExit(main())
