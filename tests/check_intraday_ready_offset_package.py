"""Run ready-configuration regression fixtures from this exact offline source."""
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
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--basetemp")
    args = parser.parse_args()
    if args.basetemp is None:
        args.basetemp = tempfile.mkdtemp(prefix="ducketz-ready-offset-")
    candidate = Path(__file__).resolve().parents[1]
    helper_path = candidate / "tests/intraday_split_package_support.py"
    spec = importlib.util.spec_from_file_location("offset_import_isolation", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    originals = helper.isolate(candidate)
    attempts = []

    def denied(*values, **kwargs):
        attempts.append(True)
        raise AssertionError("Ready offset checks prohibit network/provider calls")

    socket.create_connection = socket.getaddrinfo = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied

    def boundary(event, values):
        if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn"}:
            raise AssertionError("Ready offset checks prohibit subprocess operations")

    sys.addaudithook(boundary)
    import pytest
    targets = ["tests/test_intraday_ready_configuration_identity.py"]
    if args.integration:
        targets += ["tests/test_intraday_split_handoff.py", "tests/test_intraday_split_services.py",
                    "tests/test_intraday_split_model_admission.py", "tests/test_intraday_split_runtime.py",
                    "tests/test_intraday_split_preparation_fairness.py",
                    "tests/test_intraday_partitioned_storage_setup.py",
                    "tests/test_intraday_renewal_storage.py"]
    arguments = ["-q", "-p", "no:cacheprovider", "--confcutdir=" + str(candidate),
                 "--rootdir=" + str(candidate), *[str(candidate / name) for name in targets]]
    if args.basetemp:
        arguments += ["--basetemp", args.basetemp]
    if args.integration:
        # That independent lifetime fixture deliberately spawns child processes;
        # this focused configuration check denies all subprocess operations.
        arguments += ["-k", "not test_spawned_services_have_independent_lifetimes_and_recovery"]
    code = pytest.main(arguments)
    helper.check_imports(candidate, originals)
    closure = {}
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename:
            source = Path(filename).resolve()
            if name.split(".")[0] in {"ml", "app", "datafetching", "tests"}:
                if not source.is_relative_to(candidate):
                    raise AssertionError("Regression imported a different checkout")
            if source.is_relative_to(candidate) and source.suffix == ".py":
                closure[source.relative_to(candidate).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ("pyproject.toml", "tests/conftest.py", "tests/check_intraday_ready_offset_package.py",
                 "tests/intraday_split_package_support.py"):
        path = candidate / name
        if path.is_file():
            closure[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    print("READY_OFFSET_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    print("READY_OFFSET_CHECK_RESULT=" + json.dumps({"exit_code": code, "network_attempts": len(attempts),
          "candidate": str(candidate), "integration": args.integration, "members": len(closure)}))
    if attempts:
        raise AssertionError("Regression attempted network access")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
