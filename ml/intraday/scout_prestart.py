"""Read-only Scout installation checks for the independent manual trader.

These engineering checks never start a trader, connect to a provider or inspect
another trader. Original journals and historical accounting are audited through
read-only readers without constructing a production store. Actual broker capacity
and fresh market checks remain the human-started execution path's responsibility.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import tomllib

MANIFEST_PATH = "scratch/cross-pc/evidence/scout-live-final-preparation/final-installed-manifest.json"
# Reviewed installed entry points and local execution helpers. A manifest is
# optional engineering evidence; it never authorizes trading or nightly work.
REQUIRED_SOURCE = frozenset({
    'ml/intraday/scout_prestart.py',
    'ml/intraday/live_session.py',
    'ml/intraday/live_model_recipe.py',
    'ml/intraday/scout_local_execution.py',
    'ml/intraday/scout_journal.py',
    'ml/intraday/scout_session_books.py',
    'ml/intraday/saved_accounting.py',
    'ml/intraday/saved_authority.py',
    'ml/intraday/scout_client.py',
    'ml/intraday/scout_protocol.py',
    'ml/intraday/scout_broker.py',
    'ml/intraday/local_forecast.py',
    'ml/intraday/submission_time.py',
    'ml/intraday/exact_status.py',
    'ml/intraday/capacity.py',
    'ml/intraday/intentions.py',
    'ml/intraday/starter_sizing.py',
    'ml/intraday/technical_sizing.py',
    'ml/intraday/prediction.py',
    'ml/intraday/store.py',
    'ml/intraday/runtime.py',
    'ml/intraday/data.py',
    'ml/intraday/models.py',
    'ml/intraday/model_policy.py',
    'ml/intraday/sizing.py',
    'ml/intraday/observed_data.py',
    'ml/intraday/minute_feed.py',
    'ml/intraday/minute_persistence.py',
    'pyproject.toml',
})
HASH = re.compile(r"[a-f0-9]{64}")
BOOKS_AUDIT_SECONDS = 30


class PrestartError(ValueError):
    pass


def require(value, message):
    if not value:
        raise PrestartError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def local_path(value):
    path = Path(value)
    require(path.is_absolute() and not str(value).startswith(("\\\\", "//"))
            and ".." not in path.parts, "Absolute host-local path required")
    if os.name == "nt":
        import ctypes
        require(ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 3, "Fixed host-local drive required")
        require(not any(":" in part for part in path.parts[1:]), "Alternate streams are not supported")
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                "Redirected paths are not supported")
    if path.is_file():
        require(path.stat().st_nlink == 1, "Standalone local file required")
    return path


def read_json(path):
    path = local_path(path)
    require(path.is_file() and path.stat().st_size <= 16 * 1024 * 1024, "Required JSON file is missing or too large: " + path.name)
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(PrestartError("Nonfinite JSON value")))


def file_map(root, values):
    require(type(values) is dict and values, "Nonempty exact source fingerprint map required")
    for relative, expected in values.items():
        require(type(relative) is str and "\\" not in relative and not Path(relative).is_absolute()
                and ".." not in Path(relative).parts and type(expected) is str and HASH.fullmatch(expected),
                "Invalid manifest path or fingerprint")
        target = local_path(root / relative)
        require(target.is_file() and digest(target.read_bytes()) == expected,
                "Installed file differs from reviewed manifest: " + relative)
    return len(values)


def verify_release(repository):
    active = read_json(repository / "scratch/cross-pc/active.json")
    release = local_path(active["release_root"])
    manifest = local_path(release / "installation.json")
    require(digest(manifest.read_bytes()) == active["manifest_sha256"], "Pinned coordination manifest differs")
    count = file_map(release, read_json(manifest)["files"])
    return dict(release_root=str(release), files_verified=count, manifest_sha256=active["manifest_sha256"])


def verify_profile(repository):
    profile = read_json(repository / "scratch/cross-pc/local-profile.json")
    require(profile.get("actor") == "Scout" and profile.get("machine") == "pc-new"
            and profile.get("timezone") == "America/Los_Angeles", "Scout local profile identity differs")
    require(local_path(profile["checkout"]).resolve() == repository.resolve(), "Profile checkout differs")
    symbols = profile.get("symbols")
    require(type(symbols) is list and symbols and len(symbols) == len(set(symbols))
            and all(type(s) is str and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", s) for s in symbols),
            "Explicit Scout symbol partition required")
    watchlist = local_path(profile["symbol_profile_path"])
    require(watchlist.resolve() == (repository / "datafetching/watchlist.local.txt").resolve(), "Scout watchlist path differs")
    actual = [line.strip() for line in watchlist.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    require(len(actual) == len(set(actual)) and set(actual) == set(symbols), "Scout watchlist and profile differ")
    expected_python = repository / ".venv/Scripts/python.exe"
    require(local_path(profile["python_executable"]).resolve() == expected_python.resolve(), "Profile Python differs")
    require(Path(sys.executable).resolve() == expected_python.resolve(), "Run this check using the installed ducketz virtual environment")
    return profile


def verify_installation(repository, manifest_path):
    manifest = read_json(manifest_path)
    require(set(manifest) == {"version", "files", "source_commits", "owner_chat_id"}
            and manifest["version"] == "scout-live-installed-v1", "Final installed manifest schema differs")
    require(type(manifest["owner_chat_id"]) is str and manifest["owner_chat_id"], "Installation owner is missing")
    require(type(manifest["source_commits"]) is list and manifest["source_commits"]
            and all(type(x) is str and re.fullmatch(r"[a-f0-9]{40}", x) for x in manifest["source_commits"]),
            "Reviewed source commit identities required")
    require(type(manifest["files"]) is dict and REQUIRED_SOURCE <= set(manifest["files"]), "Final manifest lacks required Scout integration source")
    count = file_map(repository, manifest["files"])
    require(Path(__file__).resolve() == (repository / "ml/intraday/scout_prestart.py").resolve(), "Check module is not loaded from the reviewed checkout")
    return dict(files_verified=count, manifest_sha256=digest(Path(manifest_path).read_bytes()), source_commits=manifest["source_commits"])


def verify_dependencies(repository):
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    require(SpecifierSet(project["requires-python"]).contains(".".join(map(str, sys.version_info[:3]))), "Python version differs from installed requirements")
    requirements = list(project["dependencies"])
    for group in ("ml", "ml-tree", "ml-sequence"):
        requirements.extend(project["optional-dependencies"][group])
    versions = {}
    for text in requirements:
        requirement = Requirement(text)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            version = metadata.version(requirement.name)
        except metadata.PackageNotFoundError as error:
            raise PrestartError("Required dependency is missing: " + requirement.name) from error
        require(requirement.specifier.contains(version), "Dependency version differs: " + requirement.name)
        versions[requirement.name] = version
    return dict(python=".".join(map(str, sys.version_info[:3])), versions=versions)


def audit_journal(path, item, *, deadline=None, original_requests=None):
    # Only pure validators are reused. No ScoutJournal or ScoutBooks instance,
    # writable transaction, migration or application client is constructed.
    from .scout_journal import ScoutJournal, _SCHEMA, _safe_path
    class ReadOnlyAudit:
        _validate_record = ScoutJournal._validate_record
        _replay = staticmethod(ScoutJournal._replay)
        _audit_lifecycle = ScoutJournal._audit_lifecycle
        _lifecycle_transition = ScoutJournal._lifecycle_transition
        _empty_lifecycle = staticmethod(ScoutJournal._empty_lifecycle)
        _economic_evidence = staticmethod(ScoutJournal._economic_evidence)
        _validate_order_payload = staticmethod(ScoutJournal._validate_order_payload)
    audit = ReadOnlyAudit()
    audit.binding, audit._journal_id = item["binding"], item["journal_id"]
    path = _safe_path(path)
    deadline = min(time.monotonic() + 10, deadline if deadline is not None else float("inf"))
    require(time.monotonic() < deadline, "Original books audit exceeded its time bound; explicit review is required")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2, isolation_level=None)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("BEGIN")
        schema = list(db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        require(all(kind == "table" for kind, _, _ in schema)
                and {name: sql for _, name, sql in schema} == _SCHEMA, "Original journal schema differs")
        require(db.execute("PRAGMA quick_check").fetchall() == [("ok",)] and not db.execute("PRAGMA foreign_key_check").fetchall(), "Original journal SQLite integrity failed")
        control, records, events, acked, lifecycle = ScoutJournal._audit(audit, db, include_lifecycle=True)
        if original_requests is not None:
            original_requests.extend(record["request"] for record in records.values())
        counts, lifecycle_counts = {}, {}
        for record in records.values():
            counts[record["state"]] = counts.get(record["state"], 0) + 1
        for state in lifecycle[1].values():
            if state["evidence"]:
                status = state["evidence"]["status"]
                lifecycle_counts[status] = lifecycle_counts.get(status, 0) + 1
        return dict(journal_id=control["journal_id"], records=len(records), states=counts,
                    pending_outbox_events=len(events) - len(acked), lifecycle_events=len(lifecycle[2]),
                    lifecycle_statuses=lifecycle_counts,
                    authority_release_inferred=False)


def inspect_books(root, account_fingerprint=None):
    from .saved_accounting import audit_saved_accounting
    deadline = time.monotonic() + BOOKS_AUDIT_SECONDS
    root = local_path(root)
    def check_deadline():
        require(time.monotonic() < deadline, "Original books audit exceeded its time bound; explicit review is required")
    path = root / "inventory.json"
    journals = root / "journals"
    actual = {p.name for p in journals.iterdir()} if journals.exists() else set()
    if account_fingerprint is None:
        if path.exists():
            account_fingerprint = read_json(path)["inventory"]["account_fingerprint"]
        else:
            require(not actual, "Original books inventory is missing while journals remain")
            require(not any(p.is_file() for folder in (root / "accounting", root / "accounting-history", root / "receipts")
                            if folder.exists() for p in folder.rglob("*")),
                    "Saved order records remain without their original inventory")
            return dict(status="no_journals_yet", journals=0, records=0,
                        account_binding="existing_local_broker_at_manual_start")
    if not path.exists():
        require(not actual, "Original books inventory is missing while journals remain")
        accounting = audit_saved_accounting(root, account_fingerprint=account_fingerprint,
                                            requests=[], check_deadline=check_deadline)
        return dict(status="no_journals_yet", journals=0, records=0, accounting=accounting,
                    authority_release_inferred=False)
    envelope = read_json(path)
    require(set(envelope) == {"inventory", "sha256"} and digest(canonical(envelope["inventory"])) == envelope["sha256"], "Original books inventory hash differs")
    inventory = envelope["inventory"]
    require(set(inventory) == {"version", "account_fingerprint", "participant_id", "stream", "sessions"}
            and inventory["version"] == "scout-session-books-v1" and inventory["account_fingerprint"] == account_fingerprint
            and inventory["participant_id"] == "pc-new" and inventory["stream"] == "scout-15m"
            and type(inventory["sessions"]) is dict, "Original books account or participant binding differs")
    require(len(inventory["sessions"]) <= 1000, "Books exceed bounded prestart review; explicit review required")
    expected, result = set(), {}
    for session, item in inventory["sessions"].items():
        require(time.monotonic() < deadline, "Original books audit exceeded its time bound; explicit review is required")
        binding = dict(account_fingerprint=account_fingerprint, participant_id="pc-new", stream="scout-15m", session_id=session)
        require(type(item) is dict and set(item) == {"filename", "binding", "journal_id"}
                and item["binding"] == binding and type(item["journal_id"]) is str
                and re.fullmatch(r"[a-f0-9]{32}", item["journal_id"])
                and item["filename"] == digest(canonical({"session_id": session})) + ".sqlite3", "Original session inventory identity differs")
        expected.add(item["filename"])
    require(expected <= actual and not actual - expected - {p + "-journal" for p in expected}, "Missing or unregistered original journal; preserve for repair")
    original_requests = []
    for session, item in inventory["sessions"].items():
        result[session] = audit_journal(journals / item["filename"], item, deadline=deadline,
                                      original_requests=original_requests)
    accounting = audit_saved_accounting(root, account_fingerprint=account_fingerprint,
                                        requests=original_requests, check_deadline=check_deadline)
    require(read_json(path) == envelope, "Concurrent inventory update; rerun the prestart check")
    return dict(status="audited", journals=len(result), records=sum(v["records"] for v in result.values()), sessions=result,
                accounting=accounting, authority_release_inferred=False,
                startup_recovery_required=any(v["records"] for v in result.values()))


def windows_process_names():
    """Read Toolhelp names when psutil cannot name a protected Windows process."""
    require(os.name == "nt", "Cannot resolve an unnamed process on this platform")
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(0x2, 0)
    require(handle != ctypes.c_void_p(-1).value, "Windows process snapshot unavailable; rerun prestart")
    result = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            result[entry.th32ProcessID] = entry.szExeFile
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
        require(ctypes.get_last_error() == 18, "Windows process enumeration incomplete; rerun prestart")
    finally:
        kernel.CloseHandle(handle)
    return result


def inspect_local_account_writers(*args, **kwargs):
    """Reject the retired cross-trader check without inspecting any process."""
    raise PrestartError("Cross-trader prestart checks are retired; use broker-local checks")


def inspect_session(datastore):
    import psutil
    workers = []
    # A process that vanishes during enumeration permits one fresh snapshot.
    # Inaccessible Python identity cannot establish absence and fails closed.
    for attempt in range(2):
        try:
            snapshot = list(psutil.process_iter(["pid", "name", "cmdline", "create_time"]))
            break
        except psutil.NoSuchProcess:
            require(attempt == 0, "Process identities changed during both checks; rerun prestart")
        except psutil.Error as error:
            raise PrestartError("Cannot inspect local process identities; resolve process access and rerun prestart") from error
    fallback_names = windows_process_names() if any(not p.info.get("name") for p in snapshot) else {}
    for process in snapshot:
        info = process.info
        name = info.get("name") or fallback_names.get(process.pid)
        require(type(name) is str and name, "Cannot identify a local process; resolve process access and rerun prestart")
        if name.lower() in {"python.exe", "pythonw.exe", "python", "python3"}:
            require(type(info.get("cmdline")) is list and info["cmdline"] and info.get("create_time") is not None,
                    "Cannot inspect a Python process; resolve process access and rerun prestart")
        args = info.get("cmdline") or []
        if "-m" in args and args.index("-m") + 1 < len(args) and args[args.index("-m") + 1] == "ml.intraday.live_session":
            workers.append(dict(pid=process.pid, create_time=process.info["create_time"]))
    require(not workers, "A manual intraday session is already running; do not start another")
    path = datastore / "intraday/manual-session.json"
    status = read_json(path) if path.exists() else None
    return dict(status="not_started_yet" if status is None else "saved_session_not_running",
                prior_session_id=status.get("session_id") if status else None,
                prior_status=status.get("status") if status else None, affected_workers=[])


def check_connection(*args, **kwargs):
    """Reject retired admission connections without loading clients or bindings."""
    raise PrestartError("Remote admission prestart connections are retired; use broker-local checks")


def run_checks(repository, datastore, *, manifest_path=None, connect=False):
    """Compatibility entry point for the independent local engineering checks."""
    require(connect is False, "Remote admission prestart connections are retired; use broker-local checks")
    return run_local_checks(repository, datastore, manifest_path=manifest_path)


def run_local_checks(repository, datastore, *, manifest_path=None):
    """Read local launch prerequisites without asking Atlas or contacting Schwab.

    Ready means the installed manual launcher is prepared. Current broker
    credentials, quotes and order status are checked by the human-started run.
    Cash or held shares are never a launch prerequisite.
    """
    repository, datastore = local_path(repository), local_path(datastore)
    result = dict(version="scout-local-prestart-v1", checked_at_utc=datetime.now(timezone.utc).isoformat(),
        mode="broker-local", ready=False, local_checks_passed=False, runtime_started=False,
        orders_or_commitments_created=False, atlas_connection_required=False, checks={}, errors=[])
    stage = "coordination"
    try:
        result["checks"][stage] = verify_release(repository)
        stage = "profile"
        profile = verify_profile(repository)
        result["checks"][stage] = dict(actor="Scout",participant_id="pc-new",symbol_count=len(profile["symbols"]))
        stage = "installation"
        result["checks"][stage] = verify_installation(repository,manifest_path or repository / MANIFEST_PATH)
        stage = "dependencies"
        result["checks"][stage] = verify_dependencies(repository)
        result["checks"]["broker_account"] = dict(binding="existing_local_broker_at_manual_start",
            gameplan_configuration_required=False)
        stage = "session"
        result["checks"][stage] = inspect_session(datastore)
        stage = "books"
        result["checks"][stage] = inspect_books(datastore / "intraday/scout")
        result.update(ready=True,local_checks_passed=True)
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,ImportError) as error:
        result["errors"].append(dict(check=stage,type=type(error).__name__,message=str(error)
            if isinstance(error,PrestartError) else "Local launch prerequisite failed; inspect private configuration"))
    result["scope"] = "Installed manual-start readiness. Live broker/provider access remains untested until human start; account capacity affects submitted quantity, not launch or desired trade."
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--datastore", type=Path, default=Path(r"C:\DATASTORE"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--scout-admission", choices=("broker-local",), default="broker-local")
    args = parser.parse_args(argv)
    result = run_local_checks(args.repository, args.datastore, manifest_path=args.manifest)
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if result["ready"] and result["local_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
