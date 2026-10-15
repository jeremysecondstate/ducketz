"""Offline prestart proof: immutable original books and no trading effects."""
from dataclasses import replace
import builtins
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import scout_prestart as pre
from ml.intraday.scout_session_books import ScoutBooks
from ml.intraday.scout_journal import ScoutJournal, JournalCorruptionError
from test_intraday_scout_journal import request, grant
from test_intraday_saved_accounting import book as saved_book, seed as seed_saved_history


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network in prestart tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def byte_snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def books_fixture(tmp_path, state="SENDING"):
    books = ScoutBooks(tmp_path / "books", account_fingerprint="a" * 64)
    req = request()
    with books.create_session(req.session_id) as journal:
        journal.prepare(req)
        if state != "PREPARED":
            journal.grant(grant(req))
        if state in {"SENDING", "UNKNOWN", "ACCEPTED"}:
            token = journal.claim_send(req.request_id)
            if state == "UNKNOWN":
                journal.outcome(req.request_id, token, None)
            elif state == "ACCEPTED":
                journal.outcome(req.request_id, token, "201")
    return books


def test_absent_books_is_valid_and_creates_nothing(tmp_path):
    root = tmp_path / "missing"
    assert pre.inspect_books(root, "a" * 64)["status"] == "no_journals_yet"
    assert not root.exists()


def test_accounting_audit_receives_original_requests_and_remaining_deadline(tmp_path, monkeypatch):
    from ml.intraday import saved_accounting
    books = books_fixture(tmp_path, "ACCEPTED")
    before = byte_snapshot(books.root)
    seen = []
    def audit(root, *, account_fingerprint, requests, check_deadline):
        assert root == books.root and account_fingerprint == "a" * 64
        assert requests == [request().payload()]
        check_deadline()
        seen.append(True)
        return {"requests": 1, "snapshots": 0, "pending": 1}
    monkeypatch.setattr(saved_accounting, "audit_saved_accounting", audit)
    result = pre.inspect_books(books.root, "a" * 64)
    assert seen == [True] and result["accounting"]["pending"] == 1
    assert request().payload() not in result.values()
    assert byte_snapshot(books.root) == before


def test_accounting_audit_cannot_extend_original_books_deadline(tmp_path, monkeypatch):
    from ml.intraday import saved_accounting
    books = books_fixture(tmp_path, "PREPARED")
    now = [100.0]
    monkeypatch.setattr(pre.time, "monotonic", lambda: now[0])
    def audit(root, *, account_fingerprint, requests, check_deadline):
        now[0] += pre.BOOKS_AUDIT_SECONDS
        check_deadline()
        pytest.fail("Expired total audit deadline was ignored")
    monkeypatch.setattr(saved_accounting, "audit_saved_accounting", audit)
    with pytest.raises(pre.PrestartError, match="time bound"):
        pre.inspect_books(books.root, "a" * 64)


def test_absent_inventory_still_audits_orphan_accounting_without_creating_books(tmp_path, monkeypatch):
    from ml.intraday import saved_accounting
    root = tmp_path / "missing"
    def audit(selected, *, account_fingerprint, requests, check_deadline):
        assert selected == root and requests == []
        check_deadline()
        raise ValueError("Orphan accounting evidence")
    monkeypatch.setattr(saved_accounting, "audit_saved_accounting", audit)
    with pytest.raises(ValueError, match="Orphan accounting"):
        pre.inspect_books(root, "a" * 64)
    assert not root.exists()


@pytest.mark.parametrize("state", ["PREPARED", "GRANTED", "SENDING", "UNKNOWN", "ACCEPTED"])
def test_original_books_audited_without_store_constructors_or_writes(tmp_path, monkeypatch, state):
    books = books_fixture(tmp_path, state)
    with books.create_session("manual-scout:reload"):
        pass
    before = byte_snapshot(books.root)
    def forbidden(*args, **kwargs):
        raise AssertionError("Production store constructor forbidden")
    monkeypatch.setattr(ScoutBooks, "__init__", forbidden)
    monkeypatch.setattr(ScoutJournal, "__init__", forbidden)
    result = pre.inspect_books(books.root, "a" * 64)
    assert result["journals"] == 2 and result["records"] == 1
    assert result["sessions"][request().session_id]["states"] == {state: 1}
    assert result["authority_release_inferred"] is False
    assert byte_snapshot(books.root) == before


@pytest.mark.parametrize("terminal", [False, True])
def test_partial_fill_and_cancel_ack_retained_without_authority_release(tmp_path, terminal):
    from test_intraday_scout_journal import order_evidence, observe, cancellation
    books = books_fixture(tmp_path, "ACCEPTED")
    with books.open_session(request().session_id) as journal:
        partial = replace(order_evidence(status="PARTIAL", filled=1), broker_order_id="201")
        observe(journal, partial)
        assert cancellation(journal)
        detail = dict(reservation_id="reservation-1", stream="scout-15m", observed_at="2026-10-12T18:01:02+00:00", acknowledgment_is_terminal=False)
        journal.record_cancellation_event("cancel:ack", "cancellation_acknowledged", detail)
        if terminal:
            final = replace(order_evidence(identity="final", status="CANCELLED", filled=1, observed="2026-10-12T18:02:00+00:00"), broker_order_id="201")
            observe(journal, final)
    before = byte_snapshot(books.root)
    result = pre.inspect_books(books.root, "a" * 64)
    assert result["sessions"][request().session_id]["lifecycle_statuses"] == {"CANCELLED" if terminal else "PARTIAL": 1}
    assert result["authority_release_inferred"] is False
    assert byte_snapshot(books.root) == before


@pytest.mark.parametrize("damage", ["missing_inventory", "missing_journal", "orphan_sidecar", "wrong_account", "wrong_original_id", "missing_event", "mutated_payload", "missing_evidence_count"])
def test_missing_or_corrupt_original_books_fail_readonly(tmp_path, damage):
    books = books_fixture(tmp_path)
    inventory = json.loads(books.path.read_text())
    item = inventory["inventory"]["sessions"][request().session_id]
    path = books.root / "journals" / item["filename"]
    if damage == "missing_inventory":
        books.path.unlink()
    elif damage == "missing_journal":
        path.unlink()
    elif damage == "orphan_sidecar":
        (path.parent / "unknown.sqlite3-journal").write_bytes(b"orphan")
    elif damage == "wrong_account":
        inventory["inventory"]["account_fingerprint"] = "b" * 64
        inventory["sha256"] = pre.digest(pre.canonical(inventory["inventory"]))
        write_json(books.path, inventory)
    elif damage == "wrong_original_id":
        item["journal_id"] = "f" * 32
        inventory["sha256"] = pre.digest(pre.canonical(inventory["inventory"]))
        write_json(books.path, inventory)
    else:
        with sqlite3.connect(path) as db:
            if damage == "missing_event":
                db.execute("DELETE FROM events WHERE sequence=2")
            elif damage == "mutated_payload":
                db.execute("UPDATE records SET payload='{}'")
            else:
                raw = json.loads(db.execute("SELECT payload FROM lifecycle_control").fetchone()[0])
                raw["evidence_count"] = 1
                encoded = pre.canonical(raw)
                db.execute("UPDATE lifecycle_control SET payload=?,sha256=?", (encoded.decode(), pre.digest(encoded)))
    before = byte_snapshot(books.root)
    with pytest.raises((pre.PrestartError, JournalCorruptionError, sqlite3.Error)):
        pre.inspect_books(books.root, "a" * 64)
    assert byte_snapshot(books.root) == before


def release_fixture(tmp_path):
    repository = tmp_path / "repo"
    release = repository / "scratch/cross-pc/releases/pinned"
    path = release / "coordination/contract.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"contract":"fixture"}')
    write_json(release / "installation.json", {"files": {"coordination/contract.json": pre.digest(path.read_bytes())}})
    write_json(repository / "scratch/cross-pc/active.json", dict(release_root=str(release), manifest_sha256=pre.digest((release / "installation.json").read_bytes())))
    return repository, release


def test_pinned_release_verified_before_use(tmp_path):
    repository, release = release_fixture(tmp_path)
    assert pre.verify_release(repository)["files_verified"] == 1
    (release / "coordination/contract.json").write_text("changed")
    with pytest.raises(pre.PrestartError, match="differs"):
        pre.verify_release(repository)


def test_manifest_requires_complete_source_and_exact_bytes(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    values = {}
    for relative in pre.REQUIRED_SOURCE:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
        values[relative] = pre.digest(path.read_bytes())
    manifest = dict(version="scout-live-installed-v1", files=values, source_commits=["a" * 40], owner_chat_id="fixture")
    path = repository / pre.MANIFEST_PATH
    write_json(path, manifest)
    monkeypatch.setattr(pre, "__file__", str(repository / "ml/intraday/scout_prestart.py"))
    assert pre.verify_installation(repository, path)["files_verified"] == len(pre.REQUIRED_SOURCE)
    (repository / "ml/intraday/scout_broker.py").write_text("changed")
    with pytest.raises(pre.PrestartError, match="differs"):
        pre.verify_installation(repository, path)
    del manifest["files"]["ml/intraday/scout_broker.py"]
    write_json(path, manifest)
    with pytest.raises(pre.PrestartError, match="lacks required"):
        pre.verify_installation(repository, path)


@pytest.mark.parametrize("missing", sorted(pre.REQUIRED_SOURCE))
def test_manifest_rejects_incomplete_independent_local_installation(tmp_path, monkeypatch, missing):
    repository = tmp_path / "repo"
    values = {}
    for relative in pre.REQUIRED_SOURCE:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
        values[relative] = pre.digest(path.read_bytes())
    # Auditing available helpers must not certify a missing manual launcher
    # or omit the exact local execution and retained-history source.
    values.pop(missing, None)
    manifest = dict(version="scout-live-installed-v1", files=values,
                    source_commits=["a" * 40], owner_chat_id="fixture")
    path = repository / pre.MANIFEST_PATH
    write_json(path, manifest)
    monkeypatch.setattr(pre, "__file__", str(repository / "ml/intraday/scout_prestart.py"))
    with pytest.raises(pre.PrestartError, match="lacks required"):
        pre.verify_installation(repository, path)


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "bad\\path.py"])
def test_manifest_rejects_noncanonical_paths(tmp_path, path):
    with pytest.raises(pre.PrestartError):
        pre.file_map(tmp_path, {path: "a" * 64})


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"files":{},"files":{"evil":1}}')
    with pytest.raises(pre.PrestartError, match="Duplicate"):
        pre.read_json(path)


def test_binding_checks_preserve_scout_profile_watchlist(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    watchlist = repository / "datafetching/watchlist.local.txt"
    watchlist.parent.mkdir(parents=True)
    watchlist.write_text("# Scout\nDOCU\nDBX\n")
    python = repository / ".venv/Scripts/python.exe"
    profile = dict(actor="Scout", machine="pc-new", timezone="America/Los_Angeles", checkout=str(repository),
                   symbols=["DOCU", "DBX"], symbol_profile_path=str(watchlist), python_executable=str(python))
    write_json(repository / "scratch/cross-pc/local-profile.json", profile)
    monkeypatch.setattr(pre.sys, "executable", str(python))
    assert pre.verify_profile(repository) == profile
    watchlist.write_text("DOCU\nMSFT\n")
    with pytest.raises(pre.PrestartError, match="watchlist and profile differ"):
        pre.verify_profile(repository)


def test_no_session_is_valid_and_running_session_blocks(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda _: [])
    assert pre.inspect_session(tmp_path)["status"] == "not_started_yet"
    process = SimpleNamespace(pid=17, info=dict(name="python.exe", cmdline=["python", "-m", "ml.intraday.live_session"], create_time=1))
    monkeypatch.setattr(psutil, "process_iter", lambda _: [process])
    with pytest.raises(pre.PrestartError, match="already running"):
        pre.inspect_session(tmp_path)


def test_saved_status_is_not_a_loaded_session(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda _: [])
    write_json(tmp_path / "intraday/manual-session.json", dict(session_id="prior", status="STOPPED"))
    assert pre.inspect_session(tmp_path) == dict(status="saved_session_not_running", prior_session_id="prior", prior_status="STOPPED", affected_workers=[])


def test_process_access_failure_is_explicit_and_no_silent_absence(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda _: (_ for _ in ()).throw(psutil.AccessDenied(1)))
    with pytest.raises(pre.PrestartError, match="resolve process access"):
        pre.inspect_session(tmp_path)
    inaccessible = SimpleNamespace(pid=17, info=dict(name="python.exe", cmdline=None, create_time=1))
    monkeypatch.setattr(psutil, "process_iter", lambda _: [inaccessible])
    with pytest.raises(pre.PrestartError, match="Cannot inspect a Python"):
        pre.inspect_session(tmp_path)


def test_disappearing_process_causes_one_fresh_snapshot(tmp_path, monkeypatch):
    import psutil
    calls = []
    def processes(_):
        calls.append(True)
        if len(calls) == 1:
            raise psutil.NoSuchProcess(1)
        return []
    monkeypatch.setattr(psutil, "process_iter", processes)
    assert pre.inspect_session(tmp_path)["status"] == "not_started_yet"
    assert len(calls) == 2


def test_windows_fallback_resolves_protected_process_without_pid_exception(tmp_path, monkeypatch):
    import psutil
    protected = SimpleNamespace(pid=816, info=dict(name="", cmdline=None, create_time=1))
    monkeypatch.setattr(psutil, "process_iter", lambda _: [protected])
    monkeypatch.setattr(pre, "windows_process_names", lambda: {816: "Secure System"})
    assert pre.inspect_session(tmp_path)["status"] == "not_started_yet"
    monkeypatch.setattr(pre, "windows_process_names", lambda: {816: "python.exe"})
    with pytest.raises(pre.PrestartError, match="Cannot inspect a Python"):
        pre.inspect_session(tmp_path)
    monkeypatch.setattr(pre, "windows_process_names", lambda: {})
    with pytest.raises(pre.PrestartError, match="Cannot identify"):
        pre.inspect_session(tmp_path)


def test_book_audit_has_one_total_deadline(tmp_path, monkeypatch):
    books = books_fixture(tmp_path)
    ticks = iter([0, 31])
    monkeypatch.setattr(pre.time, "monotonic", lambda: next(ticks))
    with pytest.raises(pre.PrestartError, match="time bound"):
        pre.inspect_books(books.root, "a" * 64)


LEGACY_MODULES = frozenset({
    "ml.intraday.scout_accounting", "ml.intraday.scout_authority",
    "ml.intraday.scout_books", "ml.intraday.scout_execution",
    "ml.intraday.scout_runtime_gate", "ml.intraday.runtime_compatibility",
    "ml.intraday.admission_service", "ml.intraday.admission_transport",
    "ml.intraday.admission_enrollment", "ml.intraday.scout_enrollment",
    "ml.account_gameplan.config",
})


def block_legacy_imports(monkeypatch):
    original = builtins.__import__
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = importlib.util.resolve_name("." * level + name, globals["__package__"]) if level else name
        requested = {resolved, *(resolved + "." + item for item in (fromlist or ()))}
        assert not requested.intersection(LEGACY_MODULES), "Local prestart resolved a retired authority dependency"
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", guarded)


def mocked_local_checks(monkeypatch):
    monkeypatch.setattr(pre, "verify_release", lambda _: {"verified": True})
    monkeypatch.setattr(pre, "verify_profile", lambda _: {"symbols": ["DOCU"]})
    monkeypatch.setattr(pre, "verify_installation", lambda *_: {"verified": True})
    monkeypatch.setattr(pre, "verify_dependencies", lambda _: {"verified": True})
    monkeypatch.setattr(pre, "inspect_session", lambda _: {"status": "not_started_yet"})


@pytest.mark.parametrize("folder", ["accounting", "accounting-history", "receipts"])
def test_inventory_free_first_start_rejects_retained_history_without_writes(tmp_path, folder):
    root = tmp_path / "original-books"
    retained = root / folder / "original-request" / "retained.json"
    write_json(retained, {"original_history": True})
    before = byte_snapshot(root)
    with pytest.raises(pre.PrestartError, match="without their original inventory"):
        pre.inspect_books(root)
    assert byte_snapshot(root) == before


def test_first_start_without_account_binding_creates_nothing(tmp_path):
    root = tmp_path / "missing"
    result = pre.inspect_books(root)
    assert result["status"] == "no_journals_yet"
    assert result["account_binding"] == "existing_local_broker_at_manual_start"
    assert not root.exists()


def test_local_books_infer_only_their_original_inventory_account(tmp_path, monkeypatch):
    books = books_fixture(tmp_path, "ACCEPTED")
    before = byte_snapshot(books.root)
    block_legacy_imports(monkeypatch)
    result = pre.inspect_books(books.root)
    assert result["journals"] == 1 and result["records"] == 1
    assert result["accounting"]["requests"] == 1
    assert result["authority_release_inferred"] is False
    assert byte_snapshot(books.root) == before


@pytest.mark.parametrize("damage_old_receipt", [False, True])
def test_composed_prestart_audits_entire_paged_accounting_history_readonly(saved_book, damage_old_receipt):
    books = ScoutBooks(saved_book.root, account_fingerprint=saved_book.request.account_fingerprint)
    with books.create_session(saved_book.request.session_id) as journal:
        journal.prepare(saved_book.request)
    _, observations = seed_saved_history(saved_book, 129)
    if damage_old_receipt:
        path = Path(observations[0]["authority_receipt"]["path"])
        path.write_bytes(path.read_bytes() + b"changed")
    before = byte_snapshot(books.root)
    if damage_old_receipt:
        with pytest.raises(ValueError):
            pre.inspect_books(books.root)
    else:
        result = pre.inspect_books(books.root)
        assert result["records"] == 1
        assert result["accounting"]["snapshots"] == 129
        assert result["authority_release_inferred"] is False
    assert byte_snapshot(books.root) == before


def test_local_checks_prepare_independent_manual_start_without_account_or_remote_binding(tmp_path, monkeypatch):
    mocked_local_checks(monkeypatch)
    block_legacy_imports(monkeypatch)
    monkeypatch.setattr(pre, "check_connection", lambda *_a, **_k: pytest.fail("No Atlas connection"))
    monkeypatch.setattr(pre, "inspect_local_account_writers", lambda *_a, **_k: pytest.fail("No other trader prerequisite"))
    result = pre.run_local_checks(tmp_path, tmp_path)
    assert result["ready"] is True and result["local_checks_passed"] is True
    assert result["mode"] == "broker-local" and result["atlas_connection_required"] is False
    assert result["runtime_started"] is False and result["orders_or_commitments_created"] is False
    assert result["checks"]["broker_account"] == {
        "binding": "existing_local_broker_at_manual_start", "gameplan_configuration_required": False}
    assert result["checks"]["books"]["status"] == "no_journals_yet"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("module", ["ml.gameplan_stock_trader", "ml.stock_trader.independent_runtime",
                                   "ml.account_gameplan.execution"])
def test_other_trader_process_is_not_a_local_prestart_prerequisite(tmp_path, monkeypatch, module):
    import psutil
    inspect_session = pre.inspect_session
    mocked_local_checks(monkeypatch)
    monkeypatch.setattr(pre, "inspect_session", inspect_session)
    process = SimpleNamespace(pid=17, info=dict(name="python.exe", cmdline=["python", "-m", module], create_time=1))
    monkeypatch.setattr(psutil, "process_iter", lambda _: [process])
    block_legacy_imports(monkeypatch)
    result = pre.run_local_checks(tmp_path, tmp_path)
    assert result["ready"] is True and result["errors"] == []
    assert result["checks"]["session"]["affected_workers"] == []
    assert list(tmp_path.iterdir()) == []


def test_compatibility_run_checks_uses_the_independent_local_report(tmp_path, monkeypatch):
    marker = {"ready": True, "mode": "broker-local"}
    selected = []
    manifest = tmp_path / "manifest.json"
    def local(repository, datastore, *, manifest_path=None):
        selected.append((repository, datastore, manifest_path))
        return marker
    monkeypatch.setattr(pre, "run_local_checks", local)
    assert pre.run_checks(tmp_path, tmp_path, manifest_path=manifest) is marker
    assert selected == [(tmp_path, tmp_path, manifest)]


@pytest.mark.parametrize("function, arguments, keywords", [
    ("run_checks", (object(), object()), {"connect": True}),
    ("check_connection", (object(), object()), {}),
    ("inspect_local_account_writers", (object(), object()), {}),
])
def test_retired_authority_apis_reject_before_path_or_account_io(monkeypatch, function, arguments, keywords):
    block_legacy_imports(monkeypatch)
    def forbidden(*_a, **_k):
        pytest.fail("Retired authority mode performed local IO")
    monkeypatch.setattr(pre, "local_path", forbidden)
    monkeypatch.setattr(pre, "run_local_checks", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(pre.PrestartError, match="retired"):
        getattr(pre, function)(*arguments, **keywords)


@pytest.mark.parametrize("function, stage", [
    ("verify_release", "coordination"), ("verify_profile", "profile"),
    ("verify_installation", "installation"), ("verify_dependencies", "dependencies"),
    ("inspect_session", "session"), ("inspect_books", "books"),
])
def test_local_checks_report_real_stage_failure_without_starting(tmp_path, monkeypatch, function, stage):
    mocked_local_checks(monkeypatch)
    def failed(*_a, **_k):
        raise pre.PrestartError("Exact local prerequisite is missing")
    monkeypatch.setattr(pre, function, failed)
    result = pre.run_local_checks(tmp_path, tmp_path)
    assert result["ready"] is False and result["local_checks_passed"] is False
    assert result["runtime_started"] is False and result["orders_or_commitments_created"] is False
    assert result["errors"] == [{"check": stage, "type": "PrestartError",
                                 "message": "Exact local prerequisite is missing"}]
    assert list(tmp_path.iterdir()) == []


def test_private_exception_details_are_not_echoed_in_local_report(tmp_path, monkeypatch):
    mocked_local_checks(monkeypatch)
    def failed(*_a):
        raise OSError("secret-account-or-endpoint-marker")
    monkeypatch.setattr(pre, "inspect_books", failed)
    result = pre.run_local_checks(tmp_path, tmp_path)
    assert result["errors"][0]["check"] == "books"
    assert result["errors"][0]["type"] == "OSError"
    assert "secret-account-or-endpoint-marker" not in json.dumps(result)
    assert not result["ready"] and not result["runtime_started"]


def test_prestart_module_and_history_audit_do_not_resolve_retired_runtime_modules(tmp_path, monkeypatch):
    books = books_fixture(tmp_path, "ACCEPTED")
    before = byte_snapshot(books.root)
    block_legacy_imports(monkeypatch)
    spec = importlib.util.spec_from_file_location("ml.intraday._prestart_dependency_probe", pre.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.inspect_books(books.root)
    assert result["records"] == 1 and result["accounting"]["requests"] == 1
    assert byte_snapshot(books.root) == before


@pytest.mark.parametrize("arguments", [["--check-connection"], ["--scout-admission", "atlas-service"]])
def test_retired_cli_modes_reject_before_io(monkeypatch, arguments):
    def forbidden(*_a, **_k):
        pytest.fail("Rejected CLI mode performed checks or IO")
    monkeypatch.setattr(pre, "run_checks", forbidden)
    monkeypatch.setattr(pre, "run_local_checks", forbidden)
    monkeypatch.setattr(pre, "local_path", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(SystemExit) as stop:
        pre.main(arguments)
    assert stop.value.code == 2


def test_cli_help_has_no_io_or_provider_construction(monkeypatch, capsys):
    monkeypatch.setattr(pre, "run_checks", lambda *_a, **_k: pytest.fail("No checks during help"))
    monkeypatch.setattr(pre, "run_local_checks", lambda *_a, **_k: pytest.fail("No checks during help"))
    with pytest.raises(SystemExit) as stop:
        pre.main(["--help"])
    assert stop.value.code == 0
    output = capsys.readouterr().out
    assert "--manifest" in output and "broker-local" in output
    assert "--check-connection" not in output and "atlas-service" not in output


@pytest.mark.parametrize("ready, expected_exit", [(True, 0), (False, 2)])
def test_cli_uses_only_independent_local_report(tmp_path, monkeypatch, capsys, ready, expected_exit):
    observed = []
    manifest = tmp_path / "manifest.json"
    def local(repository, datastore, *, manifest_path=None):
        observed.append((repository, datastore, manifest_path))
        return {"ready": ready, "local_checks_passed": ready, "mode": "broker-local"}
    monkeypatch.setattr(pre, "run_local_checks", local)
    result = pre.main(["--repository", str(tmp_path), "--datastore", str(tmp_path), "--manifest", str(manifest)])
    assert result == expected_exit
    assert observed == [(tmp_path, tmp_path, manifest)]
    assert json.loads(capsys.readouterr().out) == {"ready": ready, "local_checks_passed": ready, "mode": "broker-local"}
