"""Injected effects only: crashes/retries must not produce a second send."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from threading import Barrier

import pytest

from ml.intraday.scout_client import ClientGateError, ScoutClient
from ml.intraday.scout_journal import JournalError, JournalStateError, ScoutJournal
from ml.intraday.scout_protocol import Grant, ProtocolError, Request


def request(**changes):
    fields = dict(request_id="request:1", account_fingerprint="a" * 64,
        session_id="manual-scout:1", symbol="DOCU", side="BUY", quantity=10,
        limit_price="42.5", forecast_id="b" * 64, forecast_sha256="b" * 64,
        snapshot_sha256="c" * 64, sizing_sha256="d" * 64,
        reference_at="2026-10-12T14:00:00Z", issued_at="2026-10-12T14:15:01Z",
        target_close_at="2026-10-12T14:45:00Z", valid_until="2026-10-12T14:16:00Z")
    return Request(**(fields | changes))


def grant(req, **changes):
    fields = dict(grant_id="grant:1", request_id=req.request_id, request_sha256=req.sha256,
        account_fingerprint=req.account_fingerprint, session_id=req.session_id,
        authority_epoch=1, quantity=3, reservation_id="reservation:1",
        issued_at="2026-10-12T14:15:02Z", expires_at="2026-10-12T14:15:30Z")
    return Grant(**(fields | changes))


class Clock:
    now = datetime.fromisoformat("2026-10-12T14:15:05+00:00")

    def __call__(self):
        return self.now


class Authority:
    def __init__(self):
        self.requests = []
        self.events = []
        self.lose_admission = False
        self.lose_ack = False
        self.grant_changes = {}

    def admit(self, payload):
        self.requests.append(payload)
        if self.lose_admission:
            self.lose_admission = False
            raise TimeoutError("reply lost after authority persisted reservation")
        return grant(Request.from_payload(payload), **self.grant_changes).payload()

    def deliver(self, envelope):
        self.events.append(envelope)
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("ack lost after authority persisted event")
        return {key: envelope[key] for key in ("event_id", "event_sha256")}


def binding():
    return dict(account_fingerprint="a" * 64, session_id="manual-scout:1",
                participant_id="pc-new", stream="scout-15m")


def client(journal, authority=None, clock=None, **changes):
    args = dict(account_fingerprint="a" * 64, session_id="manual-scout:1",
                symbols=frozenset({"DOCU"}), authority_epoch=1, clock=clock or Clock())
    return ScoutClient(journal, authority or Authority(), **(args | changes))


def test_lost_admission_reply_retries_exact_durable_request(tmp_path):
    path = tmp_path / "journal.db"
    authority = Authority()
    authority.lose_admission = True
    req = request()
    with ScoutJournal(path, binding=binding()) as journal:
        with pytest.raises(TimeoutError):
            client(journal, authority).admit(req)
        assert journal.get(req.request_id)["state"] == "PREPARED"
    with ScoutJournal(path, binding=binding()) as journal:
        adapter = client(journal, authority)
        received = adapter.admit(req)
        assert adapter.admit(req) == received  # cached reply causes no third call
        assert len(authority.requests) == 2
        assert authority.requests[0] == authority.requests[1] == req.payload()
        with pytest.raises(JournalError):
            adapter.admit(replace(req, quantity=9))
        assert len(authority.requests) == 2


@pytest.mark.parametrize("changes", [
    {"request_sha256": "f" * 64}, {"account_fingerprint": "f" * 64},
    {"session_id": "other"}, {"authority_epoch": 2}, {"quantity": 11},
    {"expires_at": "2026-10-12T14:15:04Z"},
])
def test_invalid_grant_never_arms_send(tmp_path, changes):
    authority = Authority()
    authority.grant_changes = changes
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        adapter = client(journal, authority)
        with pytest.raises((ProtocolError, ClientGateError)):
            adapter.admit(request())
        calls = []
        with pytest.raises(JournalStateError):
            adapter.submit("request:1", send=lambda *args: calls.append(args), final_gate=lambda *args: True)
        assert calls == []
        assert journal.get("request:1")["state"] == "PREPARED"


@pytest.mark.parametrize("changes", [
    {"account_fingerprint": "f" * 64}, {"session_id": "other"}, {"symbol": "AAPL"},
])
def test_wrong_client_scope_fails_before_admission(tmp_path, changes):
    authority = Authority()
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        with pytest.raises(ClientGateError):
            client(journal, authority).admit(request(**changes))
        assert authority.requests == []
        assert journal.get("request:1") is None


def test_exact_accepted_order_and_granted_quantity_survive_reopen(tmp_path):
    path = tmp_path / "journal.db"
    calls = []
    with ScoutJournal(path, binding=binding()) as journal:
        adapter = client(journal)
        adapter.admit(request())
        def send(req, permitted):
            calls.append((req, permitted))
            assert journal.get(req.request_id)["state"] == "SENDING"
            return "123"
        result = adapter.submit("request:1", send=send, final_gate=lambda *args: True)
        assert result["state"] == "ACCEPTED"
        assert calls[0][0].quantity == 10 and calls[0][1].quantity == 3
    with ScoutJournal(path, binding=binding()) as journal:
        assert journal.get("request:1")["broker_order_id"] == "123"
        with pytest.raises(JournalStateError):
            client(journal).submit("request:1", send=send, final_gate=lambda *args: True)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [TimeoutError, KeyboardInterrupt])
def test_ambiguous_send_or_process_exit_cannot_be_reposted(tmp_path, failure):
    path = tmp_path / "journal.db"
    calls = []
    with ScoutJournal(path, binding=binding()) as journal:
        adapter = client(journal)
        adapter.admit(request())
        def send(*args):
            calls.append("possible broker acceptance")
            raise failure()
        with pytest.raises(failure):
            adapter.submit("request:1", send=send, final_gate=lambda *args: True)
    with ScoutJournal(path, binding=binding()) as journal:
        record = journal.get("request:1")
        assert record["state"] == ("UNKNOWN" if failure is TimeoutError else "SENDING")
        with pytest.raises(JournalStateError):
            client(journal).submit("request:1", send=send, final_gate=lambda *args: True)
        # External exact-identity recovery may later persist the true ID; never
        # infer this receipt from a balance or same-symbol order search.
        journal.outcome("request:1", record["send_token"], "456")
        with pytest.raises(JournalStateError):
            client(journal).submit("request:1", send=send, final_gate=lambda *args: True)
    assert len(calls) == 1


@pytest.mark.parametrize("after_claim", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_stop_or_expiry_before_and_after_claim_never_calls_sender(tmp_path, after_claim, expired):
    clock = Clock()
    calls, gates = [], []
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        adapter = client(journal, clock=clock)
        adapter.admit(request())
        def gate(*args):
            gates.append(1)
            fail = len(gates) == (2 if after_claim else 1)
            if fail and expired:
                clock.now = datetime.fromisoformat("2026-10-12T14:15:30+00:00")
            return not fail or expired
        with pytest.raises(ClientGateError):
            adapter.submit("request:1", send=lambda *args: calls.append(args), final_gate=gate)
        assert calls == []
        assert journal.get("request:1")["state"] == ("UNKNOWN" if after_claim else "GRANTED")


def test_naive_clock_fails_before_admission(tmp_path):
    authority = Authority()
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        with pytest.raises(ClientGateError):
            client(journal, authority, clock=lambda: datetime(2026, 10, 12)).admit(request())
    assert authority.requests == []


def test_lost_event_ack_retries_identical_event_without_resending_order(tmp_path):
    path = tmp_path / "journal.db"
    authority = Authority()
    with ScoutJournal(path, binding=binding()) as journal:
        adapter = client(journal, authority)
        adapter.admit(request())
        adapter.submit("request:1", send=lambda *args: "123", final_gate=lambda *args: True)
        authority.lose_ack = True
        with pytest.raises(TimeoutError):
            adapter.flush_outbox()
        assert journal.pending_events()
    with ScoutJournal(path, binding=binding()) as journal:
        adapter = client(journal, authority)
        delivered = adapter.flush_outbox()
        assert delivered > 0 and adapter.flush_outbox() == 0
        assert authority.events[0] == authority.events[1]
        assert journal.get("request:1")["state"] == "ACCEPTED"
        assert journal.pending_events() == []
        with pytest.raises(JournalStateError):
            journal.revoke_unsent("request:1", "stop")


@pytest.mark.parametrize("ack", [None, {}, {"event_id": "wrong", "event_sha256": "f" * 64}])
def test_wrong_ack_preserves_durable_pending_evidence(tmp_path, ack):
    authority = Authority()
    authority.deliver = lambda event: ack
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        adapter = client(journal, authority)
        adapter.admit(request())
        before = journal.pending_events()
        with pytest.raises(ProtocolError):
            adapter.flush_outbox()
        assert journal.pending_events() == before


def test_two_clients_compete_for_one_durable_send_claim(tmp_path):
    path = tmp_path / "journal.db"
    with ScoutJournal(path, binding=binding()) as journal:
        client(journal).admit(request())
    barrier, calls = Barrier(2), []
    def compete():
        with ScoutJournal(path, binding=binding()) as journal:
            adapter = client(journal)
            gate_calls = 0
            def gate(*args):
                nonlocal gate_calls
                gate_calls += 1
                if gate_calls == 1:
                    barrier.wait(timeout=10)
                return True
            def send(*args):
                calls.append("123")
                return "123"
            try:
                return adapter.submit("request:1", send=send, final_gate=gate)["state"]
            except JournalStateError:
                return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete), pool.submit(compete)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == ["ACCEPTED", "blocked"]
    assert calls == ["123"]


def test_late_exception_cannot_erase_an_already_recorded_acceptance(tmp_path):
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        adapter = client(journal)
        adapter.admit(request())
        def send(*args):
            record = journal.get("request:1")
            journal.outcome("request:1", record["send_token"], "123")
            raise TimeoutError("later local processing failed")
        with pytest.raises(TimeoutError):
            adapter.submit("request:1", send=send, final_gate=lambda *args: True)
        record = journal.get("request:1")
        assert record["state"] == "ACCEPTED" and record["broker_order_id"] == "123"


@pytest.mark.parametrize("receipt", [None, "unknown", " 123 ", "", "0", "01", 123, "１２３", "https://broker/orders/123", "1" * 65])
def test_ambiguous_sender_receipt_stays_unknown(tmp_path, receipt):
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        adapter = client(journal)
        adapter.admit(request())
        with pytest.raises(ProtocolError):
            adapter.submit("request:1", send=lambda *args: receipt, final_gate=lambda *args: True)
        assert journal.get("request:1")["state"] == "UNKNOWN"
        with pytest.raises(JournalStateError):
            adapter.submit("request:1", send=lambda *args: "123", final_gate=lambda *args: True)


def test_transport_cannot_mutate_envelope_to_redirect_ack(tmp_path):
    authority = Authority()
    other = {}
    def bad_ack(envelope):
        envelope["event_id"] = other["event_id"]
        envelope["event_sha256"] = other["event_sha256"]
        return {key: envelope[key] for key in ("event_id", "event_sha256")}
    authority.deliver = bad_ack
    with ScoutJournal(tmp_path / "journal.db", binding=binding()) as journal:
        adapter = client(journal, authority)
        adapter.admit(request())
        before = journal.pending_events()
        other.update(before[1])  # A real pending event the journal could accept.
        with pytest.raises(ProtocolError):
            adapter.flush_outbox()
        assert journal.pending_events() == before
