from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from ml.intraday.scout_journal import (
    JournalCorruptionError, JournalError, JournalStateError, ScoutJournal,
)
from ml.intraday.scout_protocol import Grant, Request, canonical_sha256


BINDING = {"account_fingerprint": "a" * 64, "session_id": "offline-session",
           "participant_id": "pc-new", "stream": "scout-15m", "authority_id": "offline-atlas"}


def request(rid="request-1"):
    return Request(request_id=rid, account_fingerprint=BINDING["account_fingerprint"],
                   session_id=BINDING["session_id"], symbol="DOCU", side="BUY", quantity=2,
                   limit_price="10.25", forecast_id="b" * 64, forecast_sha256="b" * 64,
                   snapshot_sha256="c" * 64, sizing_sha256="d" * 64,
                   reference_at="2026-10-12T18:00:00Z", issued_at="2026-10-12T18:00:01Z",
                   target_close_at="2026-10-12T18:15:00Z", valid_until="2026-10-12T18:14:00Z")


def grant(req=None, *, gid="grant-1", reservation="reservation-1"):
    req = req or request()
    return Grant(grant_id=gid, request_id=req.request_id, request_sha256=req.sha256,
                 account_fingerprint=req.account_fingerprint, session_id=req.session_id,
                 authority_epoch=1, quantity=2, reservation_id=reservation,
                 issued_at="2026-10-12T18:00:02Z", expires_at="2026-10-12T18:13:00Z")


def journal(tmp_path):
    return ScoutJournal(tmp_path / "scout-submissions.sqlite3", binding=BINDING)


def ready(tmp_path):
    result = journal(tmp_path)
    result.prepare(request())
    result.grant(grant())
    return result


def reopen(value):
    return ScoutJournal(value.path, binding=BINDING)


def test_immutable_idempotent_requests_and_grants(tmp_path):
    ledger = journal(tmp_path)
    first = ledger.prepare(request())
    assert first["state"] == "PREPARED"
    assert ledger.prepare(request()) == first
    assert len(ledger.pending_events()) == 1
    with pytest.raises(JournalStateError, match="changed request"):
        ledger.prepare(replace(request(), quantity=3))
    admitted = ledger.grant(grant())
    assert admitted["grant_sha256"] == grant().sha256
    assert ledger.grant(grant()) == admitted
    assert len(ledger.pending_events()) == 2
    with pytest.raises(JournalStateError, match="immutable"):
        ledger.grant(replace(grant(), authority_epoch=2))
    with pytest.raises(ValueError, match="exact request"):
        ledger.grant(replace(grant(), request_sha256="e" * 64))
    assert reopen(ledger).get("request-1") == admitted


@pytest.mark.parametrize("alias", ["grant_id", "reservation_id"])
def test_one_authority_identity_cannot_back_two_requests(tmp_path, alias):
    ledger = ready(tmp_path)
    other = request("request-2")
    ledger.prepare(other)
    candidate = grant(other, gid="grant-2", reservation="reservation-2")
    candidate = replace(candidate, **{alias: getattr(grant(), alias)})
    with pytest.raises(JournalStateError, match="identity reused"):
        ledger.grant(candidate)
    assert ledger.get("request-2")["state"] == "PREPARED"


def test_lost_receipt_late_exact_id_and_no_second_claim(tmp_path):
    ledger = ready(tmp_path)
    token = ledger.claim_send("request-1")
    assert ledger.get("request-1")["state"] == "SENDING"
    ledger = reopen(ledger)
    with pytest.raises(JournalStateError, match="once"):
        ledger.claim_send("request-1")
    assert ledger.outcome("request-1", token, None)["state"] == "UNKNOWN"
    assert reopen(ledger).outcome("request-1", token, None)["state"] == "UNKNOWN"
    assert ledger.outcome("request-1", token, "offline-order-1")["state"] == "ACCEPTED"
    count = len(ledger.pending_events())
    assert reopen(ledger).outcome("request-1", token, "offline-order-1")["broker_order_id"] == "offline-order-1"
    # A reporting exception after an accepted commit must not erase the receipt.
    assert ledger.outcome("request-1", token, None)["state"] == "ACCEPTED"
    assert len(ledger.pending_events()) == count
    with pytest.raises(JournalStateError, match="Conflicting"):
        ledger.outcome("request-1", token, "different-order")
    with pytest.raises(JournalStateError, match="claim"):
        ledger.outcome("request-1", "0" * 64, "offline-order-1")
    with pytest.raises(JournalStateError):
        ledger.claim_send("request-1")
    assert ledger.prepare(request())["state"] == "ACCEPTED"
    assert ledger.grant(grant())["state"] == "ACCEPTED"


def test_one_broker_receipt_cannot_be_bound_to_two_requests(tmp_path):
    ledger = ready(tmp_path)
    token = ledger.claim_send("request-1")
    ledger.outcome("request-1", token, "offline-order-1")
    other = request("request-2")
    ledger.prepare(other)
    ledger.grant(grant(other, gid="grant-2", reservation="reservation-2"))
    other_token = ledger.claim_send("request-2")
    with pytest.raises(JournalStateError, match="receipt identity reused"):
        ledger.outcome("request-2", other_token, "offline-order-1")
    assert ledger.get("request-2")["state"] == "SENDING"
    assert ledger.outcome("request-2", other_token, None)["state"] == "UNKNOWN"


@pytest.mark.parametrize("initial", ["PREPARED", "GRANTED", "SENDING", "UNKNOWN", "ACCEPTED"])
def test_revocation_never_releases_a_possibly_sent_claim(tmp_path, initial):
    ledger = journal(tmp_path)
    ledger.prepare(request())
    if initial != "PREPARED":
        ledger.grant(grant())
    if initial in {"SENDING", "UNKNOWN", "ACCEPTED"}:
        token = ledger.claim_send("request-1")
        if initial != "SENDING":
            ledger.outcome("request-1", token, "offline-order" if initial == "ACCEPTED" else None)
        with pytest.raises(JournalStateError, match="unclaimed"):
            ledger.revoke_unsent("request-1", "operator-fence")
        assert ledger.get("request-1")["state"] == initial
    else:
        assert ledger.revoke_unsent("request-1", "operator-fence")["state"] == "NOT_SENT"
        assert reopen(ledger).revoke_unsent("request-1", "operator-fence")["state"] == "NOT_SENT"
        with pytest.raises(JournalStateError):
            ledger.revoke_unsent("request-1", "changed-reason")
        with pytest.raises(JournalStateError):
            ledger.claim_send("request-1")
        assert ledger.prepare(request())["state"] == "NOT_SENT"


def test_two_connections_issue_exactly_one_claim(tmp_path):
    first = ready(tmp_path)
    second = reopen(first)
    barrier = Barrier(2)

    def claim(ledger):
        barrier.wait()
        try:
            return ledger.claim_send("request-1")
        except JournalStateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first, second)))
    assert sum(value is not None for value in results) == 1
    assert reopen(first).get("request-1")["send_token"] == next(value for value in results if value)
    assert [item["event"]["kind"] for item in first.pending_events()] == ["PREPARED", "GRANTED", "SENDING"]


def test_revocation_and_claim_serialize_without_ambiguous_release(tmp_path):
    first = ready(tmp_path)
    second = reopen(first)
    barrier = Barrier(2)

    def compete(action):
        barrier.wait()
        try:
            return action(), True
        except JournalStateError:
            return None, False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, (lambda: first.claim_send("request-1"),
                                          lambda: second.revoke_unsent("request-1", "gate-closed"))))
    assert sum(succeeded for _, succeeded in results) == 1
    stored = reopen(first).get("request-1")
    assert stored["state"] == ("SENDING" if results[0][1] else "NOT_SENT")
    assert len(first.pending_events()) == 3


_CRASH = """
import hashlib, json, os, socket, sys
from pathlib import Path
data = json.load(sys.stdin)
root = Path(data['root']).resolve()
sys.path.insert(0, str(root))
sys.dont_write_bytecode = True
fresh_cache = Path(data['pycache'])
fresh_cache.mkdir(exist_ok=False)
sys.pycache_prefix = str(fresh_cache)
def no_network(*args, **kwargs):
    raise RuntimeError('network disabled for crash subprocess')
socket.create_connection = no_network
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
from ml.intraday.scout_journal import ScoutJournal
from ml.intraday.scout_protocol import Request, Grant
def attest():
    expected = data['module_fingerprints']
    imported_names = {name for name in sys.modules if name == 'ml' or name.startswith('ml.')}
    assert imported_names == set(expected), imported_names
    imported = {}
    for name, evidence in expected.items():
        path = Path(sys.modules[name].__file__).resolve()
        assert path == (root / evidence['relative_path']).resolve(), (name, str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == evidence['sha256'], name
        imported[name] = {'path': str(path), 'sha256': digest}
    assert socket.create_connection is no_network
    assert socket.socket.connect is no_network
    assert socket.socket.connect_ex is no_network
    assert not list(fresh_cache.rglob('*'))
    Path(data['attestation']).write_text(json.dumps({
        'imports': imported, 'network_disabled': True, 'pycache_prefix': str(fresh_cache),
        'phase': data['when'], 'action': data['action'],
    }, sort_keys=True), encoding='utf-8')
attest()
journal = ScoutJournal(data['path'], binding=data['binding'])
if data['when'] == 'before_commit':
    append = journal._append
    def crash(*args, **kwargs):
        append(*args, **kwargs)
        attest()
        os._exit(23)
    journal._append = crash
action = data['action']
if action == 'prepare': journal.prepare(Request.from_payload(data['request']))
elif action == 'grant': journal.grant(Grant.from_payload(data['grant']))
elif action == 'claim': journal.claim_send('request-1')
elif action == 'outcome': journal.outcome('request-1', data['token'], 'offline-order-1')
attest()
os._exit(23)
"""


@pytest.mark.parametrize("action,before,after,event_count", [
    ("prepare", None, "PREPARED", 1), ("grant", "PREPARED", "GRANTED", 2),
    ("claim", "GRANTED", "SENDING", 3), ("outcome", "SENDING", "ACCEPTED", 4),
])
@pytest.mark.parametrize("when", ["before_commit", "after_commit"])
def test_abrupt_process_death_at_transaction_boundaries(tmp_path, action, before, after, event_count, when):
    ledger = journal(tmp_path)
    token = None
    if action != "prepare":
        ledger.prepare(request())
    if action in {"claim", "outcome"}:
        ledger.grant(grant())
    if action == "outcome":
        token = ledger.claim_send("request-1")
    candidate = Path(__file__).resolve().parents[1]
    modules = {"ml": "ml/__init__.py", "ml.intraday": "ml/intraday/__init__.py",
               "ml.intraday.scout_journal": "ml/intraday/scout_journal.py",
               "ml.intraday.scout_protocol": "ml/intraday/scout_protocol.py"}
    fingerprints = {name: {"relative_path": relative,
                           "sha256": hashlib.sha256((candidate / relative).read_bytes()).hexdigest()}
                    for name, relative in modules.items()}
    attestation = tmp_path / "child-attestation.json"
    result = subprocess.run([sys.executable, "-B", "-c", _CRASH], input=json.dumps({
        "path": str(ledger.path), "binding": BINDING, "when": when, "action": action,
        "request": request().payload(), "grant": grant().payload(), "token": token,
        "root": str(candidate), "module_fingerprints": fingerprints,
        "pycache": str(tmp_path / "child-empty-pycache"), "attestation": str(attestation),
    }), text=True, capture_output=True, cwd=candidate, timeout=20)
    assert result.returncode == 23, result.stderr
    imported = json.loads(attestation.read_text(encoding="utf-8"))
    assert imported["network_disabled"] is True
    assert imported["phase"] == when and imported["action"] == action
    assert imported["imports"] == {
        name: {"path": str((candidate / evidence["relative_path"]).resolve()), "sha256": evidence["sha256"]}
        for name, evidence in fingerprints.items()}
    recovered = reopen(ledger)
    record = recovered.get("request-1")
    expected = before if when == "before_commit" else after
    assert (record["state"] if record else None) == expected
    assert len(recovered.pending_events()) == event_count - (when == "before_commit")
    if expected in {"SENDING", "ACCEPTED"}:
        with pytest.raises(JournalStateError):
            recovered.claim_send("request-1")
    if expected == "ACCEPTED":
        assert record["broker_order_id"] == "offline-order-1"
    if action == "claim" and when == "before_commit":
        assert recovered.claim_send("request-1")


def test_durable_outbox_loss_duplicate_delivery_and_exact_ack(tmp_path):
    ledger = ready(tmp_path)
    token = ledger.claim_send("request-1")
    ledger.outcome("request-1", token, "offline-order-1")
    events = ledger.pending_events()
    assert len(events) == 4
    assert token not in json.dumps(events)
    assert reopen(ledger).pending_events() == events  # Lost transport receipt resends same bytes.
    for item in events:
        assert canonical_sha256(item["event"]) == item["event_sha256"]
    first = events[0]
    ledger.acknowledge(first["event_id"], first["event_sha256"])
    reopen(ledger).acknowledge(first["event_id"], first["event_sha256"])
    assert reopen(ledger).pending_events() == events[1:]
    with pytest.raises(JournalStateError):
        ledger.acknowledge(first["event_id"], "0" * 64)
    with pytest.raises(JournalStateError):
        ledger.acknowledge("unknown-event", first["event_sha256"])
    for item in events[1:]:
        ledger.acknowledge(item["event_id"], item["event_sha256"])
    assert reopen(ledger).pending_events() == []


@pytest.mark.parametrize("damage", ["event_bytes", "last_event_lost", "record_bytes", "record_lost", "ack_hash", "unknown_schema"])
def test_stored_corruption_fails_closed_without_rearming(tmp_path, damage):
    ledger = ready(tmp_path)
    ledger.claim_send("request-1")
    first = ledger.pending_events()[0]
    ledger.acknowledge(first["event_id"], first["event_sha256"])
    with sqlite3.connect(ledger.path) as db:
        if damage == "event_bytes":
            db.execute("UPDATE events SET payload = '{}' WHERE sequence = 1")
        elif damage == "last_event_lost":
            db.execute("DELETE FROM events WHERE sequence = 3")
        elif damage == "record_bytes":
            db.execute("UPDATE records SET payload = '{}'")
        elif damage == "record_lost":
            db.execute("DELETE FROM records")
        elif damage == "ack_hash":
            db.execute("UPDATE acknowledgements SET event_sha256 = ?", ("0" * 64,))
        else:
            db.execute("CREATE TABLE unexpected (value TEXT)")
    for action in (lambda: ledger.claim_send("request-1"), ledger.pending_events, lambda: reopen(ledger)):
        with pytest.raises(JournalCorruptionError):
            action()


def test_rehashed_illegal_transition_still_fails_replay(tmp_path):
    ledger = ready(tmp_path)
    ledger.claim_send("request-1")
    with sqlite3.connect(ledger.path) as db:
        raw = db.execute("SELECT payload FROM events WHERE sequence = 3").fetchone()[0]
        event = json.loads(raw)
        event["record"]["request"]["quantity"] = 3
        event["record"]["request_sha256"] = canonical_sha256(event["record"]["request"])
        db.execute("UPDATE events SET payload = ?, sha256 = ? WHERE sequence = 3",
                   (json.dumps(event, sort_keys=True, separators=(",", ":")), canonical_sha256(event)))
    with pytest.raises(JournalCorruptionError):
        ledger.get("request-1")


def test_binding_and_schema_are_fail_closed(tmp_path):
    ledger = ready(tmp_path)
    for changes in ({"account_fingerprint": "e" * 64}, {"session_id": "other"}, {"authority_id": "other"}):
        with pytest.raises(JournalCorruptionError):
            ScoutJournal(ledger.path, binding={**BINDING, **changes})
    with pytest.raises(JournalStateError):
        ledger.prepare(replace(request("other"), session_id="other"))
    with pytest.raises(JournalError):
        ScoutJournal(tmp_path / "bad.sqlite3", binding={"account_fingerprint": "a" * 64})
    unrelated = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE account_cash (value INTEGER)")
        db.execute("INSERT INTO account_cash VALUES (123)")
    original = unrelated.read_bytes()
    with pytest.raises(JournalCorruptionError):
        ScoutJournal(unrelated, binding=BINDING)
    assert unrelated.read_bytes() == original


@pytest.mark.parametrize("damage", ["truncated", "all_tables_dropped", "deleted"])
def test_existing_empty_or_lost_journal_never_silently_initializes(tmp_path, damage):
    ledger = ready(tmp_path)
    ledger.claim_send("request-1")
    if damage == "truncated":
        ledger.path.write_bytes(b"")
    elif damage == "all_tables_dropped":
        with sqlite3.connect(ledger.path) as db:
            for table in ("acknowledgements", "events", "records", "control"):
                db.execute("DROP TABLE " + table)
    else:
        ledger.path.unlink()
    with pytest.raises(JournalCorruptionError):
        ledger.claim_send("request-1")
    if damage == "deleted":
        assert not ledger.path.exists()  # mode=rw did not recreate it.
    else:
        with pytest.raises(JournalCorruptionError):
            reopen(ledger)


def test_concurrent_creation_has_one_identity_and_no_reset(tmp_path):
    barrier = Barrier(2)
    path = tmp_path / "concurrent.sqlite3"

    def create(_):
        barrier.wait()
        try:
            return ScoutJournal(path, binding=BINDING)
        except JournalCorruptionError:
            # The exclusive creator may have created its empty file but not
            # initialized it yet. The other opener must refuse that ambiguity.
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    successful = [value for value in results if value is not None]
    assert successful
    assert len({value._journal_id for value in successful}) == 1
    ledger = successful[0]
    ledger.prepare(request())
    ledger.grant(grant())
    ledger.claim_send("request-1")
    recovered = ScoutJournal(path, binding=BINDING)
    assert recovered._journal_id == ledger._journal_id
    with pytest.raises(JournalStateError):
        recovered.claim_send("request-1")


def test_partial_append_exception_rolls_back_entire_transaction(tmp_path, monkeypatch):
    ledger = ready(tmp_path)
    events = ledger.pending_events()
    original = ledger._append

    def fail(*args):
        original(*args)
        raise RuntimeError("simulated failure after all writes before commit")

    monkeypatch.setattr(ledger, "_append", fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        ledger.claim_send("request-1")
    recovered = reopen(ledger)
    assert recovered.pending_events() == events
    assert recovered.get("request-1")["state"] == "GRANTED"
    assert recovered.claim_send("request-1")


@pytest.mark.parametrize("path", ["relative.sqlite3", "//server/share/journal.sqlite3", r"\\server\share\journal.sqlite3"])
def test_rejects_relative_and_network_paths(path):
    with pytest.raises(JournalError, match="host-local"):
        ScoutJournal(path, binding=BINDING)


def test_rejects_symlink_journal_ancestor_and_sidecar(tmp_path):
    # Windows developer mode is not guaranteed; a mocked lstat exercises the
    # same required reparse-point rejection without needing administrator rights.
    from unittest.mock import patch
    import stat

    ledger = ready(tmp_path)
    original_lstat = Path.lstat
    for redirected in (ledger.path, ledger.path.parent, Path(str(ledger.path) + "-journal")):
        def lstat(path, *args, **kwargs):
            if path == redirected:
                class Redirected:
                    st_mode = stat.S_IFLNK | 0o777
                    st_file_attributes = 0x400
                return Redirected()
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat), pytest.raises(JournalError, match="symlinks"):
            ledger.claim_send("request-1")
    assert ledger.get("request-1")["state"] == "GRANTED"


def test_returned_values_cannot_mutate_stored_records(tmp_path):
    ledger = ready(tmp_path)
    ledger.get("request-1")["request"]["quantity"] = 999
    ledger.pending_events()[0]["event"]["record"]["state"] = "ACCEPTED"
    assert ledger.get("request-1")["request"]["quantity"] == 2
    ledger.close()
    with pytest.raises(JournalError, match="closed"):
        ledger.get("request-1")
