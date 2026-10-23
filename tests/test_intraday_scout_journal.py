from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
               "ml.intraday.scout_protocol": "ml/intraday/scout_protocol.py",
               "ml.intraday.dataset_diagnostics": "ml/intraday/dataset_diagnostics.py"}
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


def accepted(tmp_path):
    ledger = ready(tmp_path)
    token = ledger.claim_send("request-1")
    ledger.outcome("request-1", token, "123")
    return ledger


def order_evidence(*, identity="observation-1", status="WORKING", filled=0,
                   observed="2026-10-12T18:01:00+00:00", **changes):
    from ml.stock_trader.horizon_ledger import FillEvidence, OrderEvidence
    fills = tuple(FillEvidence("fill-" + str(i), 1, "10.2", "2026-10-12T18:00:30+00:00")
                  for i in range(filled))
    value = OrderEvidence(identity, "reservation-1", BINDING["account_fingerprint"], observed,
        "123", status, 2, filled, 0 if status in {"FILLED", "CANCELLED", "REJECTED"} else 2 - filled,
        fills, status)
    return replace(value, **changes)


def observe(ledger, value):
    return ledger.observe_order("request-1", value, as_of="2026-10-12T18:10:00+00:00")


def cancellation(ledger, **changes):
    args = dict(stream="scout-15m", reason="new-forecast", as_of="2026-10-12T18:01:01+00:00",
                evidence_id="cancel-1")
    return ledger.reserve_stream_cancellation("reservation-1", **(args | changes))


def test_private_lifecycle_projection_reopens_without_changing_submission_outbox(tmp_path):
    ledger = accepted(tmp_path)
    old_events = ledger.pending_events()
    initial = ledger.lookup_reservation("reservation-1")
    assert initial.status == "SUBMITTED" and initial.broker_order_id == "123"
    assert initial.allocation_id == initial.batch_id == ""
    assert ledger.account_fingerprint == BINDING["account_fingerprint"]
    assert ledger.reservation_stream("reservation-1") == "scout-15m"
    observe(ledger, order_evidence(status="PARTIAL", filled=1))
    assert cancellation(ledger)
    actual = reopen(ledger).lookup_reservation("reservation-1")
    assert actual.status == "PARTIAL" and actual.filled_quantity == 1 and actual.reserved_quantity == 1
    assert actual.last_evidence_at == "2026-10-12T18:01:00+00:00"
    assert actual.cancel_requested_at == "2026-10-12T18:01:01+00:00"
    assert actual.quantity == grant().quantity and ledger.pending_events() == old_events
    assert ledger.pending_reservations() == (actual,)


def test_records_preserve_unresolved_states_without_misclassifying_inflight_send(tmp_path):
    ledger = ready(tmp_path)
    ledger.claim_send("request-1")
    assert ledger.records()[0]["state"] == "SENDING"
    assert ledger.pending_reservations() == ()
    with pytest.raises(JournalStateError, match="accepted"):
        ledger.lookup_reservation("reservation-1")
    with pytest.raises(JournalStateError, match="accepted"):
        observe(ledger, order_evidence())
    copy = ledger.records()
    copy[0]["state"] = "ACCEPTED"
    assert ledger.records()[0]["state"] == "SENDING"


def test_complete_cumulative_terminal_evidence_preserves_fills_and_prevents_demotions(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence(status="PARTIAL", filled=1))
    terminal = order_evidence(identity="terminal", status="CANCELLED", filled=2,
                              observed="2026-10-12T18:01:02+00:00")
    observe(ledger, terminal)
    assert ledger.pending_reservations() == ()
    assert ledger.lookup_reservation("reservation-1").filled_quantity == 2
    assert ledger.get("request-1")["state"] == "ACCEPTED"  # Submission history is immutable.
    with pytest.raises(JournalStateError, match="regressed"):
        observe(ledger, order_evidence(identity="demotion", status="PARTIAL", filled=1,
                                     observed="2026-10-12T18:01:03+00:00"))
    assert reopen(ledger).order_state("request-1")["evidence"]["status"] == "CANCELLED"


@pytest.mark.parametrize("changes", [
    {"broker_order_id": "999"}, {"reservation_id": "other"}, {"account_fingerprint": "f" * 64},
    {"order_quantity": 3}, {"cumulative_filled_quantity": True}, {"remaining_quantity": 2},
    {"fills": ()}, {"status": "UNKNOWN"}, {"status": "WORKING"},
    {"observed_at": "2026-10-12T17:59:00+00:00"},
])
def test_bad_exact_order_or_fill_evidence_is_never_recorded(tmp_path, changes):
    ledger = accepted(tmp_path)
    with pytest.raises(JournalStateError):
        observe(ledger, replace(order_evidence(status="PARTIAL", filled=1), **changes))
    assert ledger.order_state("request-1")["evidence"] is None


def test_future_order_or_fill_evidence_is_rejected(tmp_path):
    ledger = accepted(tmp_path)
    with pytest.raises(JournalStateError, match="future"):
        ledger.observe_order("request-1", order_evidence(), as_of="2026-10-12T18:00:59+00:00")
    original = order_evidence(status="PARTIAL", filled=1)
    with pytest.raises(JournalStateError):
        observe(ledger, replace(original, fills=(replace(original.fills[0], executed_at="2026-10-12T18:02:00Z"),)))


@pytest.mark.parametrize("mutation", ["missing", "price", "time", "duplicate"])
def test_complete_cumulative_fill_ids_are_immutable(tmp_path, mutation):
    ledger = accepted(tmp_path)
    first = order_evidence(status="PARTIAL", filled=1)
    observe(ledger, first)
    later = order_evidence(identity="filled", status="FILLED", filled=2,
                           observed="2026-10-12T18:01:03+00:00")
    a, b = later.fills
    if mutation == "missing": a = replace(a, fill_id="replacement-id")
    if mutation == "price": a = replace(a, price="10.1")
    if mutation == "time": a = replace(a, executed_at="2026-10-12T18:00:31Z")
    if mutation == "duplicate": b = a
    with pytest.raises(JournalStateError):
        observe(ledger, replace(later, fills=(a, b)))
    assert ledger.lookup_reservation("reservation-1").filled_quantity == 1


def test_same_evidence_id_retries_exactly_and_rejects_mutation_even_after_later_poll(tmp_path):
    ledger = accepted(tmp_path)
    first = order_evidence()
    observe(ledger, first)
    second = replace(first, evidence_id="observation-2", observed_at="2026-10-12T18:01:02+00:00")
    state = observe(ledger, second)
    assert observe(ledger, first) == state  # Exact old receipt is an idempotent replay.
    with pytest.raises(JournalStateError, match="ID reused"):
        observe(ledger, replace(first, observed_at="2026-10-12T18:01:03+00:00"))
    assert ledger.order_state("request-1") == state


def test_unchanged_polling_refreshes_receipt_without_growing_replay_history(tmp_path):
    ledger = accepted(tmp_path)
    initial = datetime(2026, 10, 12, 18, 1, tzinfo=timezone.utc)
    for index in range(100):
        observe(ledger, order_evidence(identity="poll-" + str(index), observed=(initial + timedelta(seconds=index)).isoformat()))
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM lifecycle_evidence_ids").fetchone()[0] == 100
    state = reopen(ledger).order_state("request-1")
    assert state["evidence"]["evidence_id"] == "poll-99"
    assert ledger.lookup_reservation("reservation-1").last_evidence_at == "2026-10-12T18:02:39+00:00"


def test_cancel_intent_is_one_stable_marker_and_ack_is_not_terminal(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    assert cancellation(ledger)
    assert not cancellation(ledger, as_of="2026-10-12T18:01:02+00:00")
    assert not cancellation(ledger, evidence_id="a-new-forecast-does-not-replace-intent", as_of="2026-10-12T18:01:03+00:00")
    assert ledger.lookup_reservation("reservation-1").cancel_requested_at == "2026-10-12T18:01:01+00:00"
    detail = {"reservation_id": "reservation-1", "stream": "scout-15m", "observed_at": "2026-10-12T18:01:02+00:00",
              "acknowledgment_is_terminal": False}
    state = ledger.record_cancellation_event("cancel-attempt-1:ack", "cancellation_acknowledged", detail)
    assert reopen(ledger).record_cancellation_event("cancel-attempt-1:ack", "cancellation_acknowledged", detail) == state
    assert ledger.lookup_reservation("reservation-1").status == "WORKING"
    assert ledger.lookup_reservation("reservation-1").reserved_quantity == 2
    with pytest.raises(JournalStateError, match="ID reused"):
        ledger.record_cancellation_event("cancel-attempt-1:ack", "cancellation_acknowledged", detail | {"observed_at": "2026-10-12T18:01:03+00:00"})
    with pytest.raises(JournalStateError):
        ledger.record_cancellation_event("false-terminal", "cancellation_acknowledged", detail | {"acknowledgment_is_terminal": True})


@pytest.mark.parametrize("change", [{"stream": "atlas-15m"}, {"stream": "gameplan"},
    {"reason": "unreviewed-policy"}, {"as_of": "2026-10-12T18:00:59+00:00"}])
def test_cancel_fences_stream_policy_and_stale_intent(tmp_path, change):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    with pytest.raises(JournalStateError):
        cancellation(ledger, **change)
    assert ledger.lookup_reservation("reservation-1").cancel_requested_at is None


def cancel_send_payload(**changes):
    return {"reservation_id": "reservation-1", "stream": "scout-15m",
            "observed_at": "2026-10-12T18:01:02+00:00", "attempt_id": "delete-attempt-1"} | changes


def test_cancel_send_claim_is_durable_ordered_and_idempotent_without_releasing(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    cancellation(ledger)
    prior = {"reservation_id": "reservation-1", "stream": "scout-15m",
             "observed_at": "2026-10-12T18:01:01+00:00", "known_before_delete": True}
    ledger.record_cancellation_event("before-delete-failure", "cancellation_reconciliation_required", prior)
    original = ledger.pending_events()
    payload = cancel_send_payload()
    state = ledger.record_cancellation_event("claim-1", "cancellation_send_claimed", payload)
    restored = reopen(ledger)
    assert restored.record_cancellation_event("claim-1", "cancellation_send_claimed", payload) == state
    assert [event["kind"] for event in state["cancellation_events"]] == [
        "cancellation_reconciliation_required", "cancellation_send_claimed"]
    assert restored.pending_events() == original
    assert restored.lookup_reservation("reservation-1").status == "WORKING"
    assert restored.lookup_reservation("reservation-1").reserved_quantity == 2
    assert restored.pending_reservations()
    with pytest.raises(JournalStateError, match="ID reused"):
        restored.record_cancellation_event("claim-1", "cancellation_send_claimed",
                                            payload | {"attempt_id": "changed"})
    with pytest.raises(JournalStateError, match="ID reused"):
        restored.record_cancellation_event("different-event", "cancellation_send_claimed", payload)
    with sqlite3.connect(restored.path) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 4


@pytest.mark.parametrize("changes", [
    {"attempt_id": ""}, {"attempt_id": "  "}, {"attempt_id": None}, {"attempt_id": 1},
    {"stream": "atlas-15m"}, {"stream": "gameplan"}, {"reservation_id": "other"},
    {"observed_at": "2026-10-12T18:01:00+00:00"}, {"unrecognized": True},
])
def test_cancel_send_claim_rejects_incomplete_or_wrong_identity(tmp_path, changes):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    cancellation(ledger)
    before = ledger.order_state("request-1")
    with pytest.raises(JournalError):
        ledger.record_cancellation_event("bad-claim", "cancellation_send_claimed", cancel_send_payload(**changes))
    assert reopen(ledger).order_state("request-1") == before


def test_cancel_send_claim_requires_intent_working_order_and_attempt_id(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    with pytest.raises(JournalStateError):
        ledger.record_cancellation_event("no-intent", "cancellation_send_claimed", cancel_send_payload())
    cancellation(ledger)
    missing = cancel_send_payload()
    del missing["attempt_id"]
    with pytest.raises(JournalStateError):
        ledger.record_cancellation_event("no-attempt", "cancellation_send_claimed", missing)
    observe(ledger, order_evidence(identity="terminal", status="CANCELLED",
                                  observed="2026-10-12T18:01:02+00:00"))
    with pytest.raises(JournalStateError):
        ledger.record_cancellation_event("already-terminal", "cancellation_send_claimed", cancel_send_payload())


def test_cancel_attempt_identity_cannot_be_reassigned_to_another_order(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    cancellation(ledger)
    ledger.record_cancellation_event("claim-1", "cancellation_send_claimed", cancel_send_payload())
    req = request("request-2")
    ledger.prepare(req)
    ledger.grant(grant(req, gid="grant-2", reservation="reservation-2"))
    ledger.outcome(req.request_id, ledger.claim_send(req.request_id), "456")
    ledger.observe_order(req.request_id, order_evidence(identity="order-2", reservation_id="reservation-2",
        broker_order_id="456"), as_of="2026-10-12T18:01:00+00:00")
    ledger.reserve_stream_cancellation("reservation-2", stream="scout-15m", reason="new-forecast",
        as_of="2026-10-12T18:01:01+00:00", evidence_id="cancel-2")
    with pytest.raises(JournalStateError, match="ID reused"):
        ledger.record_cancellation_event("claim-2", "cancellation_send_claimed",
                                          cancel_send_payload(reservation_id="reservation-2"))
    assert reopen(ledger).order_state("request-2")["cancellation_events"] == []


def test_rehashed_duplicate_cancel_attempt_identity_fails_replay(tmp_path):
    import ml.intraday.scout_journal as module
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    cancellation(ledger)
    ledger.record_cancellation_event("claim-1", "cancellation_send_claimed", cancel_send_payload())
    ledger.record_cancellation_event("claim-2", "cancellation_send_claimed",
                                      cancel_send_payload(attempt_id="delete-attempt-2"))
    with sqlite3.connect(ledger.path) as db:
        number, raw = db.execute("SELECT sequence,payload FROM lifecycle_events ORDER BY sequence DESC LIMIT 1").fetchone()
        event = json.loads(raw)
        event["payload"]["payload"]["attempt_id"] = "delete-attempt-1"
        db.execute("UPDATE lifecycle_events SET payload=?,sha256=? WHERE sequence=?",
                   (module._encoded(event), canonical_sha256(event), number))
    with pytest.raises(JournalCorruptionError, match="attempt ID reused"):
        reopen(ledger)


def test_lifecycle_needs_explicit_audited_known_legacy_migration(tmp_path):
    import ml.intraday.scout_journal as module
    ledger = ready(tmp_path)
    ledger.claim_send("request-1")
    events = ledger.pending_events()
    with sqlite3.connect(ledger.path) as db:
        for name in reversed(tuple(module._LIFECYCLE_SCHEMA)):
            db.execute("DROP TABLE " + name)
        control = json.loads(db.execute("SELECT payload FROM control").fetchone()[0])
        control["schema_version"] = module.LEGACY_VERSION
        db.execute("UPDATE control SET payload=?,sha256=?", (module._encoded(control), canonical_sha256(control)))
    before = ledger.path.read_bytes()
    with pytest.raises(JournalCorruptionError):
        reopen(ledger)
    assert ledger.path.read_bytes() == before
    restored = ScoutJournal(ledger.path, binding=BINDING, migrate_legacy=True)
    assert restored.pending_events() == events and restored.get("request-1")["state"] == "SENDING"
    assert restored.order_state("request-1")["evidence"] is None
    with pytest.raises(JournalStateError):
        restored.claim_send("request-1")


@pytest.mark.parametrize("damage", ["event", "tail", "state", "evidence_identity", "lost_table"])
def test_private_lifecycle_corruption_fails_closed_for_all_journal_work(tmp_path, damage):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    with sqlite3.connect(ledger.path) as db:
        if damage == "event": db.execute("UPDATE lifecycle_events SET payload='{}'")
        if damage == "tail": db.execute("DELETE FROM lifecycle_events")
        if damage == "state": db.execute("UPDATE lifecycle_records SET payload='{}'")
        if damage == "evidence_identity": db.execute("DELETE FROM lifecycle_evidence_ids")
        if damage == "lost_table": db.execute("DROP TABLE lifecycle_events")
    with pytest.raises(JournalCorruptionError):
        ledger.records()
    with pytest.raises(JournalCorruptionError):
        ScoutJournal(ledger.path, binding=BINDING, migrate_legacy=True)


def test_lifecycle_write_failure_rolls_back_evidence_id_state_and_chain(tmp_path, monkeypatch):
    ledger = accepted(tmp_path)
    original = ledger._append_lifecycle
    def fail(*args):
        original(*args)
        raise RuntimeError("before transaction commit")
    monkeypatch.setattr(ledger, "_append_lifecycle", fail)
    with pytest.raises(RuntimeError):
        observe(ledger, order_evidence())
    assert reopen(ledger).order_state("request-1")["evidence"] is None
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_evidence_ids").fetchone()[0] == 0
    assert observe(reopen(ledger), order_evidence())["evidence"]["status"] == "WORKING"


def test_open_existing_never_recreates_missing_original_journal(tmp_path):
    path = tmp_path / "missing-parent" / "missing.sqlite3"
    with pytest.raises(JournalCorruptionError):
        ScoutJournal(path, binding=BINDING, create=False)
    assert not path.exists() and not path.parent.exists()
    ledger = ready(tmp_path)
    identity = ledger.journal_id
    ledger.path.unlink()
    with pytest.raises(JournalCorruptionError):
        ScoutJournal(ledger.path, binding=BINDING, create=False, expected_journal_id=identity)
    assert not ledger.path.exists()


def test_original_identity_pin_rejects_replacement_even_with_same_binding(tmp_path):
    ledger = ready(tmp_path)
    original = ledger.journal_id
    assert ScoutJournal(ledger.path, binding=BINDING, create=False, expected_journal_id=original).journal_id == original
    ledger.path.unlink()
    replacement = ScoutJournal(ledger.path, binding=BINDING)
    assert replacement.journal_id != original
    before = replacement.path.read_bytes()
    with pytest.raises(JournalCorruptionError, match="original identity"):
        ScoutJournal(ledger.path, binding=BINDING, create=False, expected_journal_id=original)
    assert replacement.path.read_bytes() == before


_LIFECYCLE_CRASH = """
import json, os, socket, sys
from pathlib import Path
data = json.load(sys.stdin)
sys.path.insert(0, data['root'])
def no_network(*args, **kwargs): raise RuntimeError('offline test forbids network')
socket.create_connection = socket.socket.connect = socket.socket.connect_ex = no_network
from ml.intraday.scout_journal import ScoutJournal
from ml.stock_trader.horizon_ledger import OrderEvidence
journal = ScoutJournal(data['path'], binding=data['binding'], create=False, expected_journal_id=data['journal_id'])
if data['when'] == 'before_commit':
    append = journal._append_lifecycle
    def crash(*args, **kwargs):
        append(*args, **kwargs)
        os._exit(23)
    journal._append_lifecycle = crash
if data['action'] == 'observe':
    journal.observe_order('request-1', OrderEvidence('crash-evidence','reservation-1',data['binding']['account_fingerprint'],
        '2026-10-12T18:01:00+00:00','123','WORKING',2,0,2), as_of='2026-10-12T18:01:01+00:00')
elif data['action'] == 'cancel':
    journal.reserve_stream_cancellation('reservation-1', stream='scout-15m', reason='new-forecast',
        as_of='2026-10-12T18:01:01+00:00', evidence_id='crash-cancel')
else:
    journal.record_cancellation_event('crash-send-claim', 'cancellation_send_claimed', {
        'reservation_id': 'reservation-1', 'stream': 'scout-15m',
        'observed_at': '2026-10-12T18:01:02+00:00', 'attempt_id': 'crash-attempt'})
os._exit(23)
"""


@pytest.mark.parametrize("action", ["observe", "cancel", "send_claim"])
@pytest.mark.parametrize("when", ["before_commit", "after_commit"])
def test_abrupt_death_retains_atomic_lifecycle_and_cancel_marker(tmp_path, action, when):
    ledger = accepted(tmp_path)
    if action in {"cancel", "send_claim"}:
        observe(ledger, order_evidence())
    if action == "send_claim":
        cancellation(ledger)
    before = ledger.order_state("request-1")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-B", "-c", _LIFECYCLE_CRASH], input=json.dumps({
        "root": str(root), "path": str(ledger.path), "binding": BINDING, "journal_id": ledger.journal_id,
        "action": action, "when": when,
    }), cwd=root, text=True, capture_output=True, timeout=20)
    assert result.returncode == 23, result.stderr
    recovered = reopen(ledger)
    if when == "before_commit":
        assert recovered.order_state("request-1") == before
    elif action == "observe":
        assert recovered.order_state("request-1")["evidence"]["evidence_id"] == "crash-evidence"
    elif action == "cancel":
        assert recovered.lookup_reservation("reservation-1").cancel_requested_at == "2026-10-12T18:01:01+00:00"
    else:
        assert recovered.order_state("request-1")["cancellation_events"] == [{
            "kind": "cancellation_send_claimed", "payload": cancel_send_payload(attempt_id="crash-attempt")}]
    assert recovered.get("request-1")["broker_order_id"] == "123"
    with pytest.raises(JournalStateError):
        recovered.claim_send("request-1")


def test_competing_lifecycle_writers_cannot_mutate_one_evidence_identity(tmp_path):
    ledger = accepted(tmp_path)
    barrier = Barrier(2)
    def attempt(index):
        other = reopen(ledger)
        value = order_evidence(observed="2026-10-12T18:01:0" + str(index) + "+00:00")
        barrier.wait(timeout=10)
        try:
            observe(other, value)
            return True
        except JournalStateError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, (0, 1))) == [False, True]
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM lifecycle_evidence_ids").fetchone()[0] == 1


def test_fill_identity_cannot_be_attributed_to_two_local_orders(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence(status="PARTIAL", filled=1))
    req = request("request-2")
    ledger.prepare(req)
    ledger.grant(grant(req, gid="grant-2", reservation="reservation-2"))
    token = ledger.claim_send(req.request_id)
    ledger.outcome(req.request_id, token, "456")
    evidence = order_evidence(identity="second-evidence", status="PARTIAL", filled=1,
                              reservation_id="reservation-2", broker_order_id="456")
    with pytest.raises(JournalStateError, match="Fill identity reused"):
        ledger.observe_order(req.request_id, evidence, as_of="2026-10-12T18:01:01+00:00")
    assert ledger.lookup_reservation("reservation-2").filled_quantity == 0


@pytest.mark.parametrize("status", ["WORKING", "CANCELLED"])
def test_new_evidence_before_cancel_marker_cannot_release_or_refresh_order(tmp_path, status):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    cancellation(ledger, as_of="2026-10-12T18:01:10+00:00")
    with pytest.raises(JournalStateError, match="predates.*cancellation"):
        observe(ledger, order_evidence(identity="stale-cancel", status=status, observed="2026-10-12T18:01:05+00:00"))
    assert ledger.pending_reservations()[0].status == "WORKING"


def test_lost_older_poll_identity_cannot_be_reused_with_new_content(tmp_path):
    ledger = accepted(tmp_path)
    first = order_evidence()
    observe(ledger, first)
    observe(ledger, replace(first, evidence_id="second-poll", observed_at="2026-10-12T18:01:02+00:00"))
    with sqlite3.connect(ledger.path) as db:
        db.execute("DELETE FROM lifecycle_evidence_ids WHERE evidence_id=?", (first.evidence_id,))
    with pytest.raises(JournalCorruptionError):
        ledger.records()
    with pytest.raises(JournalCorruptionError):
        observe(ledger, replace(first, observed_at="2026-10-12T18:01:03+00:00"))


def test_changed_order_economics_must_have_a_newer_observation_time(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence())
    with pytest.raises(JournalStateError, match="regressed"):
        observe(ledger, order_evidence(identity="conflicting-same-time", status="PARTIAL", filled=1))
    assert ledger.lookup_reservation("reservation-1").filled_quantity == 0


def test_terminal_order_cumulative_fills_cannot_change_after_confirmation(tmp_path):
    ledger = accepted(tmp_path)
    observe(ledger, order_evidence(status="CANCELLED", filled=1))
    with pytest.raises(JournalStateError, match="regressed"):
        observe(ledger, order_evidence(identity="late-changed-terminal", status="CANCELLED", filled=2,
                                      observed="2026-10-12T18:01:02+00:00"))


@pytest.mark.parametrize("raw,provenance,allowed", [
    ("2026-10-12T18:00:01Z", True, True),
    ("2026-10-12T18:00:01.000Z", True, False),
    ("2026-10-12T18:00:01Z", False, False),
    ("2026-10-12T18:00:00Z", True, False),
])
def test_fill_lower_bound_reuses_only_verified_whole_second_precision_tolerance(tmp_path, raw, provenance, allowed):
    ledger = journal(tmp_path)
    req = replace(request(), issued_at="2026-10-12T18:00:01.500000+00:00")
    ledger.prepare(req)
    ledger.grant(replace(grant(req), issued_at=req.issued_at))
    ledger.outcome(req.request_id, ledger.claim_send(req.request_id), "123")
    value = order_evidence(status="PARTIAL", filled=1)
    fill = replace(value.fills[0], executed_at=raw)
    detail = {"version": "schwab-execution-time-v1", "fills": {
        fill.fill_id: {"raw_timestamp": raw, "source_field": "execution_leg.time"}}} if provenance else None
    value = replace(value, fills=(fill,), fill_time_provenance=detail)
    if allowed:
        assert observe(ledger, value)["evidence"]["cumulative_filled_quantity"] == 1
    else:
        with pytest.raises(JournalStateError):
            observe(ledger, value)


def test_fill_cannot_violate_immutable_buy_limit(tmp_path):
    ledger = accepted(tmp_path)
    value = order_evidence(status="PARTIAL", filled=1)
    with pytest.raises(JournalStateError):
        observe(ledger, replace(value, fills=(replace(value.fills[0], price="10.26"),)))
