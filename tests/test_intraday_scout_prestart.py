"""Offline prestart proof: immutable original books and no trading effects."""
from dataclasses import replace
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import scout_prestart as pre
from ml.intraday.scout_books import ScoutBooks
from ml.intraday.scout_journal import ScoutJournal, JournalCorruptionError
from test_intraday_scout_journal import request, grant


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


def mocked_local_checks(tmp_path, monkeypatch):
    import ml.intraday.scout_authority as authority
    monkeypatch.setattr(pre, "verify_release", lambda _: {"verified": True})
    monkeypatch.setattr(pre, "verify_profile", lambda _: {"symbols": ["DOCU"]})
    monkeypatch.setattr(pre, "verify_installation", lambda *_: {"verified": True})
    monkeypatch.setattr(pre, "verify_dependencies", lambda _: {"verified": True})
    monkeypatch.setattr(pre, "inspect_session", lambda _: {"status": "not_started_yet"})
    monkeypatch.setattr(pre, "inspect_local_account_writers", lambda *_: {"ready": True, "mode": "verified_gameplan_absent"})
    monkeypatch.setattr(pre, "inspect_books", lambda *_: {"journals": 0})
    binding = SimpleNamespace(symbols=("DOCU",), sha256="b" * 64, account_fingerprint="a" * 64)
    monkeypatch.setattr(authority, "load_binding", lambda _: binding)
    return binding


def test_local_mode_never_connects_or_claims_readiness(tmp_path, monkeypatch):
    mocked_local_checks(tmp_path, monkeypatch)
    monkeypatch.setattr(pre, "check_connection", lambda *_: pytest.fail("Connection forbidden"))
    result = pre.run_checks(tmp_path, tmp_path)
    assert result["local_checks_passed"] and not result["ready"]
    assert result["orders_or_commitments_created"] is False
    assert list(tmp_path.iterdir()) == []


def test_missing_final_manifest_blocks_before_binding_or_connection(tmp_path, monkeypatch):
    mocked_local_checks(tmp_path, monkeypatch)
    monkeypatch.setattr(pre, "verify_installation", lambda *_: (_ for _ in ()).throw(pre.PrestartError("Final installed manifest is missing")))
    monkeypatch.setattr(pre, "check_connection", lambda *_: pytest.fail("Connection forbidden"))
    result = pre.run_checks(tmp_path, tmp_path, connect=True)
    assert not result["ready"] and not result["local_checks_passed"]
    assert result["errors"][0]["message"] == "Final installed manifest is missing"


def test_connection_mode_only_calls_capabilities_and_readiness(tmp_path, monkeypatch):
    import ml.intraday.scout_authority as authority
    binding = mocked_local_checks(tmp_path, monkeypatch)
    calls = []
    class Client:
        def __init__(self, selected, receipts):
            assert selected is binding and isinstance(receipts, pre.PrestartReceipts)
        def capabilities(self):
            calls.append("capabilities")
            return SimpleNamespace(path="saved-capability")
        def readiness(self):
            calls.append("readiness")
            return dict(ready=True, authenticated=True, protocol_compatible=True, participant_id="pc-new",
                        stream="scout-15m", account_fingerprint=binding.account_fingerprint)
    monkeypatch.setattr(authority, "ScoutAuthorityClient", Client)
    result = pre.run_checks(tmp_path, tmp_path, connect=True)
    assert result["ready"] and calls == ["capabilities", "readiness"]
    assert list(tmp_path.iterdir()) == []


def test_readiness_unavailable_fails_closed_after_capability(tmp_path, monkeypatch):
    import ml.intraday.scout_authority as authority
    binding = mocked_local_checks(tmp_path, monkeypatch)
    class Client:
        def __init__(self, *_):
            pass
        def capabilities(self):
            return SimpleNamespace(path="saved-capability")
        def readiness(self):
            raise authority.AuthorityProtocolError("Atlas readiness route and schema have not been published")
    monkeypatch.setattr(authority, "ScoutAuthorityClient", Client)
    result = pre.run_checks(tmp_path, tmp_path, connect=True)
    assert result["local_checks_passed"] and not result["ready"]
    assert result["errors"][0]["type"] == "AuthorityProtocolError"
    assert result["errors"][0]["check"] == "connection"
    assert result["errors"][0]["message"] == "Atlas readiness route and schema have not been published"


@pytest.mark.parametrize("change", [{"authenticated": False}, {"ready": 1}, {"account_fingerprint": "b" * 64}, {"stream": "atlas-15m"}, {"participant_id": "pc-original"}])
def test_incompatible_or_wrong_service_identity_fails(tmp_path, monkeypatch, change):
    import ml.intraday.scout_authority as authority
    binding = mocked_local_checks(tmp_path, monkeypatch)
    class Client:
        def __init__(self, *_):
            pass
        def capabilities(self):
            return SimpleNamespace(path="saved-capability")
        def readiness(self):
            return dict(ready=True, authenticated=True, protocol_compatible=True, participant_id="pc-new", stream="scout-15m", account_fingerprint=binding.account_fingerprint) | change
    monkeypatch.setattr(authority, "ScoutAuthorityClient", Client)
    result = pre.run_checks(tmp_path, tmp_path, connect=True)
    assert result["local_checks_passed"] and not result["ready"]
    assert result["errors"][0]["check"] == "connection"


def test_prestart_receipts_cannot_hold_trading_operations(tmp_path):
    sink = pre.PrestartReceipts(tmp_path / "receipts", "a" * 64)
    with pytest.raises(pre.PrestartError, match="cannot save trading"):
        sink.save_receipt(kind="authority:reserve", request_id="r1", payload={})
    assert not sink.root.exists()
    first = sink.save_receipt(kind="authority:capabilities", request_id="c1", payload={"read_only": True})
    assert sink.save_receipt(kind="authority:capabilities", request_id="c1", payload={"read_only": True}) == first
    assert set(p.name for p in sink.root.iterdir()) == {first["sha256"] + ".json"}


def test_cli_help_has_no_io_or_provider_construction(monkeypatch, capsys):
    monkeypatch.setattr(pre, "run_checks", lambda *_a, **_k: pytest.fail("No checks during help"))
    with pytest.raises(SystemExit) as stop:
        pre.main(["--help"])
    assert stop.value.code == 0
    assert "--check-connection" in capsys.readouterr().out


def process_record(tmp_path, command=None):
    return dict(pid=31, parent_pid=10, created_at=1.0, executable=str(tmp_path / "python.exe"),
                command_line=command or [str(tmp_path / "python.exe"), "-m", "ml.hyperliquid_coordinator"])


def test_local_writer_absence_uses_shared_reader_twice_and_compiles_source_only(tmp_path):
    calls = []
    def inventory():
        calls.append(True)
        return [process_record(tmp_path)]
    result = pre.inspect_local_account_writers(tmp_path, Path(__file__).resolve().parents[1], process_inventory=inventory)
    assert result["ready"] and result["mode"] == "verified_gameplan_absent"
    assert len(calls) == 2 and len(result["process_inventory_observations"]) == 2
    assert result["orders_placed"] == 0 and not result["workers_changed"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", [["python", "-m", "ml.gameplan_stock_trader"],
                                     ["python", "-m", "ml.stock_trader.independent_runtime"],
                                     ["python", "-m", "ml.account_gameplan.execution"],
                                     ["python", "C:\\dev\\ducketz\\ml\\stock_trader\\independent_runtime.py"]])
def test_any_scout_local_account_writer_blocks_even_if_compatible(tmp_path, command):
    with pytest.raises(pre.PrestartError, match="Atlas exclusively owns Gameplan"):
        pre.inspect_local_account_writers(tmp_path, Path(__file__).resolve().parents[1],
                                         process_inventory=lambda: [process_record(tmp_path, command)])


def test_local_writer_appearing_between_absence_snapshots_blocks(tmp_path):
    inventories = iter([[], [process_record(tmp_path, ["python", "-m", "ml.gameplan_stock_trader"])]])
    with pytest.raises(pre.PrestartError, match="writer is running"):
        pre.inspect_local_account_writers(tmp_path, Path(__file__).resolve().parents[1], process_inventory=lambda: next(inventories))


@pytest.mark.parametrize("change", [{"command_line": None}, {"created_at": None}, {"parent_pid": None}, {"executable": "relative"}])
def test_incomplete_python_identity_cannot_prove_local_absence(tmp_path, change):
    with pytest.raises(pre.PrestartError, match="identity is incomplete"):
        pre.inspect_local_account_writers(tmp_path, Path(__file__).resolve().parents[1],
                                         process_inventory=lambda: [process_record(tmp_path) | change])


def test_missing_installed_shared_capability_blocks_local_absence(tmp_path):
    with pytest.raises(pre.PrestartError, match="installed shared capability"):
        pre.inspect_local_account_writers(tmp_path, tmp_path, process_inventory=lambda: [])


def test_complete_python_inventory_resolves_protected_name_and_reads_birth(tmp_path, monkeypatch):
    import psutil
    protected = SimpleNamespace(pid=816, info=dict(name=""))
    python = SimpleNamespace(pid=31, info=dict(name="python.exe"), create_time=lambda: 1.0,
                             ppid=lambda: 10, exe=lambda: str(tmp_path / "python.exe"), cmdline=lambda: ["python", "-m", "fixture"])
    monkeypatch.setattr(psutil, "process_iter", lambda _: [protected, python])
    monkeypatch.setattr(pre, "windows_process_names", lambda: {816: "Secure System"})
    assert pre.complete_python_inventory() == [dict(pid=31, created_at=1.0, parent_pid=10,
                                                   executable=str(tmp_path / "python.exe"), command_line=["python", "-m", "fixture"])]
    python.cmdline = lambda: (_ for _ in ()).throw(psutil.AccessDenied(31))
    with pytest.raises(pre.PrestartError, match="resolve process access"):
        pre.complete_python_inventory()
