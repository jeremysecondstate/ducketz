"""Start only the independent data service from a complete installed generation.

This entry has no trading activation, session ownership, worker-renewal loop or
broker adapter. The existing data schedule may invoke it while the manually
started 15-minute trader is stopped, busy or absent.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
import uuid


SCHEMA = "trader-installation-v1"
SHA = re.compile(r"[0-9a-f]{64}\Z")
HOST = "tools/intraday_data_host.py"
INSTALLER = "tools/trader_installation.py"


def record_host(options, status, **fields):
    """Keep bootstrap failures visible without touching the trading stop surface."""
    directory = options.datastore / "intraday/system-1a"
    directory.mkdir(parents=True, exist_ok=True)
    value = {"schema": "intraday-system-1a-bootstrap-v1", "status": status,
             "at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
             "trading_authority": False, **fields}
    raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    temporary = directory / (".bootstrap-" + uuid.uuid4().hex + ".partial")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "bootstrap-status.json")
        with (directory / "bootstrap-events.jsonl").open("ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        temporary.unlink(missing_ok=True)


def _safe(path):
    path = Path(os.path.abspath(path))
    for member in (*reversed(path.parents), path):
        info = member.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("Data-service bootstrap does not accept reparse paths")
    return path


def _sha(path):
    return hashlib.sha256(_safe(path).read_bytes()).hexdigest()


def _manifest(generation):
    generation = _safe(generation)
    if generation.parent.name != "generations" or not SHA.fullmatch(generation.name):
        raise ValueError("Expected an immutable installed generation")
    raw = _safe(generation / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != generation.name:
        raise ValueError("Data-service generation manifest changed")
    manifest = json.loads(raw)
    if manifest.get("schema") != SCHEMA or manifest.get("protocol") != 1:
        raise ValueError("Unsupported data-service installation protocol")
    for name in (HOST, INSTALLER):
        if _sha(generation / "source" / name) != manifest["source_files"][name]:
            raise ValueError("Data-service bootstrap source changed")
    return generation, manifest


def select_generation(installation):
    installation = _safe(installation)
    active = json.loads((installation / "active.json").read_bytes())
    identity = active.get("generation", "")
    if (set(active) != {"schema", "protocol", "generation", "manifest_sha256"}
            or active.get("schema") != SCHEMA or active.get("protocol") != 1
            or not isinstance(identity, str) or not SHA.fullmatch(identity)
            or active.get("manifest_sha256") != identity):
        raise ValueError("Unsupported or incomplete data-service installation")
    generation, manifest = _manifest(installation / "generations" / identity)
    runtime_id = manifest["runtime"]["id"]
    if not isinstance(runtime_id, str) or not SHA.fullmatch(runtime_id) or manifest["runtime"]["manifest_sha256"] != runtime_id:
        raise ValueError("Invalid data-service runtime identity")
    runtime = installation / "runtimes" / runtime_id
    raw = _safe(runtime / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != runtime_id:
        raise ValueError("Data-service runtime manifest changed")
    runtime_manifest = json.loads(raw)
    if runtime_manifest.get("python_relative") != "python/python.exe":
        raise ValueError("Unsupported data-service interpreter layout")
    python = runtime / "python/python.exe"
    if _sha(python) != runtime_manifest["files"]["python/python.exe"]:
        raise ValueError("Data-service interpreter changed")
    return {"generation": generation, "python": python}


def bootstrap(generation):
    generation, manifest = _manifest(generation)
    if Path(__file__).resolve() != (generation / "source" / HOST).resolve():
        raise ValueError("The data worker must run from its selected immutable generation")
    spec = importlib.util.spec_from_file_location(
        "intraday_data_installer", generation / "source" / INSTALLER)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    # Source and complete dependencies are verified before importing the service.
    selected = installer.verify(generation.parent.parent, generation.name,
                                generation.name, full=True)
    if Path(sys.executable).resolve() != Path(selected["python"]).resolve():
        raise ValueError("Data worker interpreter differs from its installed runtime")
    sys.path[:] = [selected["source_root"], *selected["stdlib_paths"], selected["site_packages"]]
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    if (Path(selected["site_packages"]) / "pip_system_certs").is_dir():
        import pip_system_certs.wrapt_requests
        pip_system_certs.wrapt_requests.inject_truststore()
    return selected


def service_arguments(options):
    arguments = ["--repository", str(options.repository), "--datastore", str(options.datastore),
                 "--profile", str(options.profile), "--watchlist", str(options.watchlist),
                 "--publication-offset-seconds", str(options.publication_offset_seconds)]
    for option in ("symbol_workers", "lookback_days"):
        value = getattr(options, option)
        if value is not None:
            arguments.extend(["--" + option.replace("_", "-"), str(value)])
    if options.daily_data_wake is not None:
        arguments.extend(["--daily-data-wake", options.daily_data_wake,
                          "--data-weekdays", options.data_weekdays])
    if options.once:
        arguments.append("--once")
    return arguments


def run_generation(generation, options):
    selected = bootstrap(generation)
    if options.verify_only:
        print(json.dumps({"status": "VERIFIED_DATA_ONLY_GENERATION",
                          "generation": selected["generation"],
                          "runtime_id": selected["runtime_id"], "service_started": False}))
        return 0
    service = importlib.import_module("ml.intraday.split_service")
    return service.main(service_arguments(options))


def launch(installation, options):
    selected = select_generation(installation)
    command = [str(selected["python"]), "-I", "-S", "-B",
               str(selected["generation"] / "source" / HOST),
               "--generation", str(selected["generation"]), *service_arguments(options)]
    if options.verify_only:
        command.append("--verify-only")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # One independently owned data process. No initial trading launch or retry.
    return subprocess.run(command, close_fds=True, creationflags=flags, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--watchlist", type=Path)
    parser.add_argument("--installation", type=Path)
    parser.add_argument("--generation", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--publication-offset-seconds", type=float, default=30)
    parser.add_argument("--symbol-workers", type=int)
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--daily-data-wake", help="Existing local data schedule HH:MM; no implicit schedule")
    parser.add_argument("--data-weekdays", help="Existing local data schedule weekdays, Monday=0")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    options = parser.parse_args(argv)
    if not options.repository.is_absolute() or not options.datastore.is_absolute():
        parser.error("Repository and datastore require explicit absolute local bindings")
    options.profile = options.profile or options.repository / "scratch/cross-pc/local-profile.json"
    options.watchlist = options.watchlist or options.repository / "datafetching/watchlist.local.txt"
    options.installation = options.installation or options.repository / "scratch/trader-installation"
    if any(not path.is_absolute() for path in (options.profile, options.watchlist, options.installation)):
        parser.error("Profile, watchlist and installation require absolute local bindings")
    if (options.daily_data_wake is None) != (options.data_weekdays is None):
        parser.error("Data rollover requires both explicit existing schedule bindings")
    if options.daily_data_wake is not None:
        if (not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", options.daily_data_wake)
                or not re.fullmatch(r"[0-6](?:,[0-6])*", options.data_weekdays)
                or len(set(options.data_weekdays.split(","))) != len(options.data_weekdays.split(","))):
            parser.error("Invalid existing local data schedule binding")
    def run():
        return (run_generation(options.generation, options) if options.generation is not None
                else launch(options.installation, options))
    if options.verify_only:
        return run()  # Read-only verification must not overwrite real lifecycle state.
    record_host(options, "STARTING")
    try:
        result = run()
    except BaseException as exc:
        record_host(options, "FAILED", error_type=type(exc).__name__)
        raise
    record_host(options, "EXITED" if result == 0 else "FAILED", exit_code=result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
