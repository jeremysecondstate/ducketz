"""Start only the independent data service from a complete installed generation.

This entry owns only the independent data lifetime and completed-iteration
worker renewal. It has no trading activation or execution adapter. The existing data schedule may invoke it while the manually
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
DATA_PROTOCOL = "tools/intraday_data_renewal.py"


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
    for name in (HOST, INSTALLER, DATA_PROTOCOL):
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


def bootstrap(generation, *, full=True):
    generation, manifest = _manifest(generation)
    if Path(__file__).resolve() != (generation / "source" / HOST).resolve():
        raise ValueError("The data worker must run from its selected immutable generation")
    spec = importlib.util.spec_from_file_location(
        "intraday_data_installer", generation / "source" / INSTALLER)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    # Source and complete dependencies are verified before importing the service.
    selected = installer.verify(generation.parent.parent, generation.name,
                                generation.name, full=full)
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


def _record_loaded(renewal, selected, *, full=False):
    """Actual selected bytes and request, separate from admission or recovery."""
    value = {"schema": "intraday-data-worker-loaded-v1", "diagnostic_only": True,
        "at": datetime.now(timezone.utc).isoformat(), "worker_pid": os.getpid(),
        "role": renewal.request["role"], "turn": renewal.request["turn"],
        "session_id": renewal.session["session_id"],
        "request_sha256": hashlib.sha256(renewal.request_path.read_bytes()).hexdigest(),
        "generation": selected["generation"], "runtime_id": selected["runtime_id"],
        "worker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "isolated": bool(sys.flags.isolated), "no_site": bool(sys.flags.no_site),
        "trading_authority": False, "full_runtime_verified": full}
    from tools.intraday_data_renewal import atomic_json
    atomic_json(renewal.request_path.with_suffix(".loaded.json"), value)


def run_controller(options, *, selected=None, continuation=None):
    from tools.intraday_data_renewal import bind_kernel_lifetime, own_data_session, run_protocol, resume_controller
    lifetime = "data-" + uuid.uuid4().hex
    directory = options.datastore / "state/intraday-data-renewal" / lifetime
    envelope = {"lifetime_id": lifetime, "repository": str(options.repository),
        "datastore": str(options.datastore), "profile": str(options.profile),
        "watchlist": str(options.watchlist), "installation": str(options.installation),
        "session": {"session_id": "data-atlas-1a:" + uuid.uuid4().hex},
        "options": {"publication_offset_seconds": options.publication_offset_seconds,
                    "symbol_workers": options.symbol_workers, "lookback_days": options.lookback_days},
        "daily_data_wake": options.daily_data_wake,
        "data_weekdays": [int(day) for day in options.data_weekdays.split(",")] if options.data_weekdays is not None else []}
    verified_runtimes = {selected["runtime_id"]} if selected is not None else set()
    pending_verifications = []
    def current_selection():
        item = select_generation(options.installation)
        manifest = json.loads((item["generation"] / "manifest.json").read_bytes())
        identity = hashlib.sha256(json.dumps({name: manifest["source_files"][name]
            for name in (HOST, DATA_PROTOCOL)} | {"runtime": manifest["runtime"]["id"]}, sort_keys=True).encode()).hexdigest()
        return item, manifest, identity
    if selected is not None:
        own_manifest = json.loads((Path(selected["source_root"]).parent / "manifest.json").read_bytes())
        own_identity = hashlib.sha256(json.dumps({name: own_manifest["source_files"][name]
            for name in (HOST, DATA_PROTOCOL)} | {"runtime": own_manifest["runtime"]["id"]}, sort_keys=True).encode()).hexdigest()
    else:
        own_identity = current_selection()[2]
    def refresh_verified_runtimes():
        for request, runtime in pending_verifications:
            loaded_path = request.with_suffix(".loaded.json")
            claim_path = request.with_suffix(".claim.json")
            if not loaded_path.exists() or not claim_path.exists():
                continue
            try:
                loaded = json.loads(loaded_path.read_bytes()); claim = json.loads(claim_path.read_bytes())
                original = json.loads(request.read_bytes())
                if (loaded.get("full_runtime_verified") is True and loaded.get("runtime_id") == runtime
                        and loaded.get("request_sha256") == hashlib.sha256(request.read_bytes()).hexdigest()
                        and loaded.get("worker_pid") == claim.get("pid")
                        and loaded.get("turn") == original.get("turn") == claim.get("turn")
                        and original.get("token") == claim.get("token")):
                    verified_runtimes.add(runtime)
            except (OSError, ValueError, KeyError, TypeError):
                continue  # An unavailable diagnostic never grants verification or stops acquisition.
    def command(role, request):
        refresh_verified_runtimes()
        selected, manifest, _ = current_selection()
        runtime = manifest["runtime"]["id"]
        argv = [str(selected["python"]), "-I", "-S", "-B",
            str(selected["generation"] / "source" / HOST), "--generation", str(selected["generation"]),
            "--request", str(request), "--installation", str(options.installation), *service_arguments(options)]
        if runtime not in verified_runtimes:
            argv.append("--full-verify")
            pending_verifications.append((request, runtime))
        return argv
    def relay_selector():
        item, _, identity = current_selection()
        return {"identity": identity, "command": [str(item["python"]), "-I", "-S", "-B",
            str(item["generation"] / "source" / HOST), "--generation", str(item["generation"]),
            "--installation", str(options.installation), *service_arguments(options)]}
    if continuation is not None:
        return resume_controller(continuation, command=command, controller_identity=own_identity,
                                 relay_selector=relay_selector, controller_generation=selected["generation"] if selected is not None else None)
    with own_data_session(options.datastore):
        bind_kernel_lifetime()
        return run_protocol(directory=directory, envelope=envelope, command=command,
                            controller_identity=own_identity, relay_selector=relay_selector,
                            controller_generation=selected["generation"] if selected is not None else None)


def run_generation(generation, options):
    request = getattr(options, "request", None)
    selected = (bootstrap(generation, full=bool(getattr(options, "full_verify", False)))
                if request is not None else bootstrap(generation))
    if options.verify_only:
        print(json.dumps({"status": "VERIFIED_DATA_ONLY_GENERATION",
                          "generation": selected["generation"],
                          "runtime_id": selected["runtime_id"], "service_started": False}))
        return 0
    if request is not None:
        from tools.intraday_data_renewal import Renewal, PlannedRenewal, PLANNED_EXIT
        renewal = Renewal(request)
        try:
            _record_loaded(renewal, selected, full=bool(getattr(options, "full_verify", False)))
        except Exception as exc:
            print("Data loaded-source evidence unavailable: " + type(exc).__name__, file=sys.stderr)
        try:
            renewal.wait_due()
            if renewal.stopped():
                return 1
            result = importlib.import_module("ml.intraday.data_renewal").run(renewal)
            renewal.finish(result)
            return 0
        except PlannedRenewal:
            return PLANNED_EXIT
    if getattr(options, "continuation", None) is not None:
        return run_controller(options, selected=selected, continuation=options.continuation)
    if getattr(options, "controller", False):
        return run_controller(options, selected=selected)
    service = importlib.import_module("ml.intraday.split_service")
    return service.main(service_arguments(options))


def bind_parent_lifetime(selected):
    # The unchanged launcher parent must also own a kill-on-close job. Closing
    # its console/task cannot leave the data controller running behind it.
    spec = importlib.util.spec_from_file_location("intraday_data_parent_protocol",
        selected["generation"] / "source" / DATA_PROTOCOL)
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    protocol.bind_kernel_lifetime()


def launch(installation, options):
    selected = select_generation(installation)
    command = [str(selected["python"]), "-I", "-S", "-B",
               str(selected["generation"] / "source" / HOST),
               "--generation", str(selected["generation"]), *service_arguments(options)]
    if options.verify_only:
        command.append("--verify-only")
    elif not options.once:
        bind_parent_lifetime(selected)
        command.extend(["--controller", "--installation", str(installation)])
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # The controller owns data-only admission. No unknown exit is retried.
    return subprocess.run(command, close_fds=True, creationflags=flags, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--watchlist", type=Path)
    parser.add_argument("--installation", type=Path)
    parser.add_argument("--generation", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--controller", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--continue", dest="continuation", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--full-verify", action="store_true", help=argparse.SUPPRESS)
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
    if sum((options.controller, options.request is not None, options.continuation is not None)) > 1 or ((options.controller or options.request is not None or options.continuation is not None) and options.generation is None):
        parser.error("Data continuation requires its exact installed generation")
    if options.verify_only or options.request is not None:
        return run()  # Verification and lane workers never overwrite bootstrap lifecycle.
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
