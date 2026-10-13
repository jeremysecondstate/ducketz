"""Offline interruptions through real temporary authority and native books."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
import json
import sqlite3

import pytest

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.authority import AccountAuthority, AuthorityError, Fill, OrderEvidence
from ml.intraday.admission_service import AdmissionService, AdmissionError, BookOperation, BookSnapshot
from ml.intraday.scout_protocol import Grant, Request, canonical_sha256
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from tests.test_intraday_account_authority import ACCOUNT, ATLAS, SCOUT, BINDING, Clock, snapshot


def request(**changes):
    value = Request("request-one", ACCOUNT, "human-manual-session", "MSFT", "BUY", 20, "10",
                    "c" * 64, "c" * 64, "d" * 64, "e" * 64,
                    "2026-10-12T17:00:00+00:00", "2026-10-12T17:15:02+00:00",
                    "2026-10-12T17:45:00+00:00", "2026-10-12T17:30:00+00:00")
    return replace(value, **changes)


def bound(value, **extra):
    return {"request_id": value.request_id, "request_sha256": value.sha256,
            "session_id": value.session_id, **extra}


class Fixture:
    def __init__(self, root):
        self.root, self.clock = root, Clock()
        self.authority = AccountAuthority(root / "state/account-gameplan/authority.sqlite3", BINDING, clock=self.clock)
        self.ledger = HorizonLedger(root / "state/independent-stock-trader/holdings.sqlite3", ACCOUNT)
        self.cash = "100"
        self.ready = True
        self.captures = 0
        self.service = self.make_service()

    @contextmanager
    def context(self):
        lease = self.authority.acquire_lease(coordinator_id="pc-original", holder_id="synthetic-capture")
        try:
            yield BookOperation(self.authority, self.ledger, lease)
        finally:
            self.authority.release_lease(lease.token)

    def refresh(self, operation):
        self.clock.tick()
        self.captures += 1
        identity = f"capture-{self.captures}"
        value = snapshot(self.clock, snapshot_id=identity, cash_available=self.cash)
        operation.authority.publish_intraday_snapshot(operation.lease.token, value)
        operation.ledger.reconcile(PortfolioEvidence(identity, ACCOUNT, self.clock().isoformat(),
            {s: 20 for s in (*ATLAS, *SCOUT)}, {s: "10" for s in (*ATLAS, *SCOUT)},
            {s: "10000" for s in (*ATLAS, *SCOUT)}, "f" * 64))
        return BookSnapshot(identity, self.clock().isoformat())

    def make_service(self):
        return AdmissionService(datastore=self.root, binding=BINDING, server_identity="reviewed-atlas-server",
            compatibility=lambda: {"ready": self.ready, "atlas_ready": self.ready, "gameplan_ready": self.ready},
            context_factory=self.context, refresh=self.refresh, clock=self.clock)

    def grant(self, value=None):
        value = value or request()
        response = self.service.reserve("pc-new", value.payload())
        return value, Grant.from_payload(response["grant"])

    def claim(self, value=None):
        value, grant = self.grant(value)
        response = self.service.authorize_send("pc-new", bound(value, claim_id="one-local-journal-token"))
        return value, grant, response["claim"]

    def accepted(self, value=None):
        value, grant, claim = self.claim(value)
        self.service.evidence("pc-new", bound(value, event_id="accepted-event", kind="ACCEPTED",
            claim_id=claim["claim_id"], broker_order_id="201"))
        return value, grant, claim


@pytest.fixture
def env(tmp_path):
    return Fixture(tmp_path)


def test_capabilities_creates_no_book_lease_or_runtime_binding(tmp_path):
    def forbidden(*args):
        raise AssertionError("Capabilities may not activate a binding")
    service = AdmissionService(datastore=tmp_path, binding=BINDING, server_identity="offline-atlas",
        compatibility=forbidden, context_factory=forbidden, refresh=forbidden)
    assert service.capabilities("pc-new")["read_only"] is True
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(AdmissionError, match="AUTHENTICATED_SCOUT"):
        service.capabilities("pc-original")


def test_common_capacity_clips_quantity_and_exact_retry_uses_original_decision(env):
    value, grant = env.grant()
    assert grant.quantity == 10
    assert env.ledger.snapshot().reservations[0].quantity == 10
    assert env.authority.reservation(grant.reservation_id).request["quantity"] == 10
    env.cash = "1000"
    response = env.service.reserve("pc-new", value.payload())
    assert response["grant"] == grant.payload() and env.captures == 1
    assert len(env.authority.read_state()["reservations"]) == 1
    with pytest.raises(AdmissionError, match="DIFFERENT_BYTES"):
        env.service.reserve("pc-new", replace(value, quantity=30).payload())


def test_zero_capacity_records_decline_without_fabricated_grant_or_native_order(env):
    env.cash = "0"
    result = env.service.reserve("pc-new", request().payload())
    assert result["status"] == "DECLINED" and "grant" not in result
    assert env.ledger.snapshot().reservations == ()
    assert env.authority.read_state()["reservations"] == []
    env.cash = "1000"
    assert env.service.reserve("pc-new", request().payload()) == result
    assert env.captures == 1


def test_service_requires_loaded_compatibility_and_common_lock(env):
    env.ready = False
    with pytest.raises(AdmissionError, match="ALL_LOADED_WRITERS"):
        env.service.reserve("pc-new", request().payload())
    assert not env.service.path.exists()
    env.ready = True
    with exclusive_runtime_lock(env.root / "locks/stock-trader-hourly.lock", process_name="stock-trader-hourly"):
        with pytest.raises(RuntimeError, match="owns these artifacts"):
            env.service.reserve("pc-new", request().payload())
    assert not env.service.path.exists()


@pytest.mark.parametrize("changes", [{"symbol": "AAPL"}, {"account_fingerprint": "b" * 64}, {"limit_price": "10.001"}])
def test_invalid_scope_never_reaches_capture_or_reservation(env, changes):
    with pytest.raises(AdmissionError):
        env.service.reserve("pc-new", request(**changes).payload())
    assert env.captures == 0 and env.authority.read_state()["reservations"] == []


def test_new_operation_lease_epoch_does_not_invalidate_original_grant(env):
    value, grant, claim = env.claim()
    assert claim["authority_epoch"] > grant.authority_epoch
    assert env.ledger.snapshot().reservations[0].status == "UNKNOWN"
    assert env.authority.reservation(grant.reservation_id).status == "SUBMITTING"
    status = env.service.status("pc-new", bound(value))
    assert status["claim"] == claim and status["send_authorized"] is False
    with pytest.raises(AuthorityError, match="ALREADY_CLAIMED"):
        env.service.authorize_send("pc-new", bound(value, claim_id=claim["claim_id"]))


def test_native_first_crash_rejects_unpaired_reservation_without_reentry(env, monkeypatch):
    original = env.authority.reserve_remote_intraday
    monkeypatch.setattr(env.authority, "reserve_remote_intraday", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("simulated power loss")))
    with pytest.raises(SystemExit):
        env.service.reserve("pc-new", request().payload())
    assert env.ledger.snapshot().reservations[0].status == "RESERVED"
    assert env.authority.read_state()["reservations"] == []
    monkeypatch.setattr(env.authority, "reserve_remote_intraday", original)
    with pytest.raises(AdmissionError, match="SHARED_BOOK_WRITE_REQUIRES_RECOVERY"):
        env.service.reserve("pc-new", request(request_id="new-request", forecast_id="b" * 64, forecast_sha256="b" * 64).payload())
    result = env.make_service().reserve("pc-new", request().payload())
    assert result["status"] == "DECLINED" and "grant" not in result
    assert env.ledger.snapshot().reservations[0].status == "REJECTED"
    assert env.authority.read_state()["reservations"] == []


def test_paired_crash_before_reply_recovers_exact_original_grant(env, monkeypatch):
    original = env.service._save
    def crash_on_response(value, column, payload):
        if column == "response":
            raise SystemExit("reply not journaled")
        return original(value, column, payload)
    monkeypatch.setattr(env.service, "_save", crash_on_response)
    with pytest.raises(SystemExit):
        env.service.reserve("pc-new", request().payload())
    reserved = env.authority.read_state()["reservations"][0]
    result = env.make_service().reserve("pc-new", request().payload())
    assert result["status"] == "GRANTED"
    assert result["grant"]["reservation_id"] == reserved["reservation_id"]
    assert result["grant"]["authority_epoch"] == 1
    assert len(env.ledger.snapshot().reservations) == 1 and env.captures == 1


def test_claim_crash_then_late_exact_acceptance_keeps_original_submission_time(env, monkeypatch):
    value, grant = env.grant()
    original = env.ledger.mark_submission
    monkeypatch.setattr(env.ledger, "mark_submission", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("native projection interrupted")))
    with pytest.raises(SystemExit):
        env.service.authorize_send("pc-new", bound(value, claim_id="once"))
    claim = env.authority.remote_claim(grant.reservation_id, participant_id="pc-new", request_sha256=value.sha256)
    executed = env.clock().isoformat()
    env.clock.tick(10)
    monkeypatch.setattr(env.ledger, "mark_submission", original)
    env.service.evidence("pc-new", bound(value, event_id="late-accepted", kind="ACCEPTED", claim_id="once", broker_order_id="201"))
    native = env.ledger.snapshot().reservations[0]
    assert native.submitted_at == native.last_evidence_at == claim["issued_at"]
    evidence = OrderEvidence("early-fill", grant.reservation_id, ACCOUNT, "201", executed,
                            "CANCELLED", grant.quantity, 1, 0, (Fill("f1", 1, "10", executed),), complete=True)
    result = env.service.evidence("pc-new", bound(value, event_id="early-fill-event", kind="ORDER_STATUS",
                                                claim_id="once", order_evidence=json.loads(json.dumps(asdict(evidence)))))
    assert result["status"] == "CANCELLED" and env.ledger.snapshot().reservations[0].filled_quantity == 1


def test_postclaim_expiry_and_not_sent_never_release_unknown_send(env):
    value, grant, claim = env.claim()
    env.clock.value = env.clock.value + timedelta(minutes=20)
    with pytest.raises(AdmissionError, match="POSSIBLE_NATIVE_SEND"):
        env.service.release_unsent("pc-new", bound(value, event_id="not-sent-after-claim", proof_sha256="a" * 64))
    status = env.service.status("pc-new", bound(value))
    assert status["status"] == "SUBMITTING" and status["claim"] == claim
    assert env.authority.reservation(grant.reservation_id).status == "SUBMITTING"


def test_preclaim_expiry_releases_both_books_and_never_replays_live_grant(env):
    value, grant = env.grant()
    env.clock.value = env.clock.value + timedelta(minutes=20)
    assert env.service.status("pc-new", bound(value))["status"] == "EXPIRED"
    assert env.authority.reservation(grant.reservation_id).status == "NOT_SUBMITTED"
    assert env.ledger.snapshot().reservations[0].status == "REJECTED"
    assert env.service.reserve("pc-new", value.payload())["status"] == "NOT_SUBMITTED"


def test_explicit_unclaimed_revocation_is_durable_and_claim_is_then_impossible(env):
    value, grant = env.grant()
    body = bound(value, event_id="revoked-locally", proof_sha256="f" * 64)
    result = env.service.release_unsent("pc-new", body)
    assert result["capacity_released"] is True
    assert env.service.release_unsent("pc-new", body) == result
    with pytest.raises(AuthorityError, match="REQUIRES_RESERVED"):
        env.service.authorize_send("pc-new", bound(value, claim_id="too-late"))


def test_partial_cancel_exactly_reconciles_both_books_and_retains_filled_cost(env):
    value, grant, claim = env.accepted()
    cancel = bound(value, event_id="cancel-request", claim_id=claim["claim_id"], broker_order_id="201", reason="new-forecast")
    response = env.service.cancel_intent("pc-new", cancel)
    assert response["capacity_released"] is False and response["broker_action_performed"] is False
    assert env.authority.reservation(grant.reservation_id).status == "SUBMITTED"
    env.clock.tick()
    evidence = OrderEvidence("partial-cancel", grant.reservation_id, ACCOUNT, "201", env.clock().isoformat(),
                            "CANCELLED", grant.quantity, 2, 0, (Fill("fill", 2, "10", env.clock().isoformat()),), complete=True)
    body = bound(value, event_id="partial-cancel-event", kind="ORDER_STATUS", claim_id=claim["claim_id"], order_evidence=json.loads(json.dumps(asdict(evidence))))
    result = env.service.evidence("pc-new", body)
    assert result["status"] == "CANCELLED" and env.service.evidence("pc-new", body) == result
    native = env.ledger.snapshot().reservations[0]
    assert native.status == "CANCELLED" and native.filled_quantity == 2
    with env.context() as operation:
        assert operation.authority.available_intraday_capacity(operation.lease.token, "capture-2")["cash_available"] == "80"


def test_evidence_event_identity_cannot_be_reused_for_changed_bytes(env):
    value, grant, claim = env.accepted()
    with pytest.raises(AdmissionError, match="EVENT_ID_REUSED"):
        env.service.evidence("pc-new", bound(value, event_id="accepted-event", kind="ACCEPTED",
            claim_id=claim["claim_id"], broker_order_id="202"))
    assert env.authority.reservation(grant.reservation_id).broker_order_id == "201"


@pytest.mark.parametrize("damage", [{"session_id": "other-session"}, {"request_sha256": "b" * 64}])
def test_remote_scope_mismatch_never_changes_commitment(env, damage):
    value, grant = env.grant()
    body = {**bound(value, claim_id="untrusted"), **damage}
    with pytest.raises(AdmissionError, match="SESSION_MISMATCH"):
        env.service.authorize_send("pc-new", body)
    assert env.authority.reservation(grant.reservation_id).status == "RESERVED"


@pytest.mark.parametrize("column", ["plan", "response"])
def test_valid_json_journal_corruption_is_detected_before_claim(env, column):
    value, grant = env.grant()
    with sqlite3.connect(env.service.path) as db:
        raw = json.loads(db.execute(f"SELECT {column} FROM requests").fetchone()[0])
        if column == "response":
            raw["payload"]["grant"]["quantity"] += 1
        else:
            raw["payload"]["authority_epoch"] += 1
        db.execute(f"UPDATE requests SET {column}=?", (json.dumps(raw),))
    with pytest.raises(AdmissionError, match="JOURNAL_VALUE_CHANGED"):
        env.service.authorize_send("pc-new", bound(value, claim_id="must-not-send"))
    assert env.authority.reservation(grant.reservation_id).status == "RESERVED"


def test_changed_service_identity_cannot_reopen_original_request_journal(env):
    value, _ = env.grant()
    second = env.make_service()
    second.server_identity = "different-server"
    with pytest.raises(AdmissionError, match="SERVICE_BINDING_MISMATCH"):
        second.status("pc-new", bound(value))


def test_event_response_corruption_is_detected_on_delivery_retry(env):
    value, _, claim = env.accepted()
    with sqlite3.connect(env.service.path) as db:
        raw = json.loads(db.execute("SELECT response FROM events WHERE id='accepted-event'").fetchone()[0])
        raw["payload"]["broker_order_id"] = "999"
        db.execute("UPDATE events SET response=? WHERE id='accepted-event'", (json.dumps(raw),))
    with pytest.raises(AdmissionError, match="JOURNAL_VALUE_CHANGED"):
        env.service.evidence("pc-new", bound(value, event_id="accepted-event", kind="ACCEPTED",
            claim_id=claim["claim_id"], broker_order_id="201"))


def test_delayed_exact_evidence_replay_repairs_native_after_authority_commit(env, monkeypatch):
    value, grant, claim = env.accepted()
    env.clock.tick()
    evidence = OrderEvidence("delayed-cancel", grant.reservation_id, ACCOUNT, "201", env.clock().isoformat(),
        "CANCELLED", grant.quantity, 2, 0, (Fill("fill", 2, "10", env.clock().isoformat()),), complete=True)
    body = bound(value, event_id="delayed-cancel-event", kind="ORDER_STATUS", claim_id=claim["claim_id"],
                 order_evidence=json.loads(json.dumps(asdict(evidence))))
    original = env.ledger.apply_stream_order_evidence
    monkeypatch.setattr(env.ledger, "apply_stream_order_evidence", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("lost native projection")))
    with pytest.raises(SystemExit):
        env.service.evidence("pc-new", body)
    assert env.authority.reservation(grant.reservation_id).filled_quantity == 2
    assert env.ledger.snapshot().reservations[0].filled_quantity == 0
    env.clock.tick(70)
    monkeypatch.setattr(env.ledger, "apply_stream_order_evidence", original)
    result = env.make_service().evidence("pc-new", body)
    assert result["status"] == "CANCELLED" and env.ledger.snapshot().reservations[0].filled_quantity == 2
    assert env.make_service().evidence("pc-new", body) == result


def test_status_repairs_interrupted_never_submitted_native_projection(env, monkeypatch):
    value, grant = env.grant()
    original = env.ledger.mark_submission
    monkeypatch.setattr(env.ledger, "mark_submission", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("lost release projection")))
    with pytest.raises(SystemExit):
        env.service.release_unsent("pc-new", bound(value, event_id="release-before-crash", proof_sha256="e" * 64))
    assert env.authority.reservation(grant.reservation_id).status == "NOT_SUBMITTED"
    assert env.ledger.snapshot().reservations[0].status == "RESERVED"
    monkeypatch.setattr(env.ledger, "mark_submission", original)
    assert env.make_service().status("pc-new", bound(value))["status"] == "NOT_SUBMITTED"
    assert env.ledger.snapshot().reservations[0].status == "REJECTED"


def test_rehashed_grant_cannot_exceed_canonical_book_quantity(env):
    from ml.intraday.scout_protocol import canonical_sha256
    value, grant = env.grant()
    with sqlite3.connect(env.service.path) as db:
        raw = json.loads(db.execute("SELECT response FROM requests").fetchone()[0])
        raw["payload"]["grant"]["quantity"] += 1
        raw["sha256"] = canonical_sha256(raw["payload"])
        db.execute("UPDATE requests SET response=?", (json.dumps(raw),))
    with pytest.raises(AdmissionError, match="GRANT_DIFFERS_FROM_BOOKS"):
        env.service.authorize_send("pc-new", bound(value, claim_id="must-not-send"))
    assert env.authority.reservation(grant.reservation_id).status == "RESERVED"


def test_lost_partial_projection_then_terminal_evidence_allows_exact_old_event_ack(env, monkeypatch):
    value, grant, claim = env.accepted()
    env.clock.tick()
    fill = Fill("exact-partial-fill", 2, "10", env.clock().isoformat())
    partial = OrderEvidence("partial-before-crash", grant.reservation_id, ACCOUNT, "201", env.clock().isoformat(),
        "PARTIAL", grant.quantity, 2, grant.quantity - 2, (fill,), complete=True)
    body = bound(value, event_id="partial-before-crash-event", kind="ORDER_STATUS", claim_id=claim["claim_id"],
                 order_evidence=json.loads(json.dumps(asdict(partial))))
    original = env.ledger.apply_stream_order_evidence
    monkeypatch.setattr(env.ledger, "apply_stream_order_evidence", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("projection lost")))
    with pytest.raises(SystemExit):
        env.service.evidence("pc-new", body)
    monkeypatch.setattr(env.ledger, "apply_stream_order_evidence", original)
    env.clock.tick()
    terminal = replace(partial, evidence_id="later-terminal", observed_at=env.clock().isoformat(), status="CANCELLED", remaining_quantity=0)
    newer = bound(value, event_id="later-terminal-event", kind="ORDER_STATUS", claim_id=claim["claim_id"],
                  order_evidence=json.loads(json.dumps(asdict(terminal))))
    assert env.service.evidence("pc-new", newer)["status"] == "CANCELLED"
    before = env.ledger.history()
    env.clock.tick(70)
    replay = env.service.evidence("pc-new", body)
    assert replay["status"] == "CANCELLED" and replay["filled_quantity"] == 2
    assert env.ledger.history() == before
    assert env.service.evidence("pc-new", body) == replay


def test_new_evidence_id_does_not_bypass_first_time_freshness(env):
    value, grant, claim = env.accepted()
    env.clock.tick()
    old = env.clock().isoformat()
    env.clock.tick(70)
    evidence = OrderEvidence("never-accepted-old-id", grant.reservation_id, ACCOUNT, "201", old,
                            "CANCELLED", grant.quantity, 0, 0, (), complete=True)
    with pytest.raises(AuthorityError, match="STALE_OR_INVALID"):
        env.service.evidence("pc-new", bound(value, event_id="never-accepted-old-event", kind="ORDER_STATUS",
            claim_id=claim["claim_id"], order_evidence=json.loads(json.dumps(asdict(evidence)))))
    assert env.authority.reservation(grant.reservation_id).status == "SUBMITTED"


def test_stored_grant_never_replays_when_canonical_account_reservation_is_missing(env):
    value, grant = env.grant()
    with sqlite3.connect(env.authority.path) as db:
        db.execute("DELETE FROM reservations WHERE id=?", (grant.reservation_id,))
    with pytest.raises(AdmissionError, match="CANONICAL_RESERVATION_PAIR_MISSING"):
        env.service.reserve("pc-new", value.payload())
    assert env.ledger.snapshot().reservations[0].status == "RESERVED"


def expired(env):
    env.clock.value += timedelta(minutes=20)


@pytest.mark.parametrize("publish_snapshot", [False, True])
@pytest.mark.parametrize("recover_with", ["reserve", "status"])
def test_failed_capture_before_plan_gets_durable_expired_negative_proof(env, publish_snapshot, recover_with):
    value = request()
    original = env.service.refresh

    def failed_refresh(operation):
        if publish_snapshot:
            original(operation)
        raise RuntimeError("synthetic fresh capture failed")

    env.service.refresh = failed_refresh
    with pytest.raises(RuntimeError, match="capture failed"):
        env.service.reserve("pc-new", value.payload())
    assert env.service.status("pc-new", bound(value))["status"] == "PENDING"
    expired(env)
    response = (env.service.reserve("pc-new", value.payload()) if recover_with == "reserve"
                else env.service.status("pc-new", bound(value)))
    assert response["status"] == "DECLINED" and response["reason"] == "expired_before_admission"
    assert response["terminal"] is True and response["admitted"] is False
    assert response["session_id"] == value.session_id and response["request_sha256"] == value.sha256
    assert response["capacity_released"] is response["send_authorized"] is False
    proof = response["negative_admission_proof"]
    assert proof["canonical_account_absent"] is proof["canonical_native_absent"] is proof["send_claim_absent"] is True
    assert env.authority.read_state()["reservations"] == [] and env.ledger.snapshot().reservations == ()
    assert env.service.reserve("pc-new", value.payload()) == response
    assert env.service.status("pc-new", bound(value)) == response
    assert env.captures == int(publish_snapshot)
    _, plan, stored = env.service._stored(value.request_id)
    assert plan is None and stored == response


def test_never_received_until_expiry_is_irrevocably_declined_across_restarts_and_clock_rollback(env):
    value = request()
    original_time = env.clock()
    expired(env)
    response = env.service.reserve("pc-new", value.payload())
    assert response["reason"] == "expired_before_admission" and "grant" not in response
    assert env.captures == 0
    restarted = env.make_service()
    # Even if this service's clock becomes eligible again, the authority keeps
    # its monotonic clock and the exact saved terminal decision cannot rearm.
    restarted.clock = lambda: original_time
    assert restarted.reserve("pc-new", value.payload()) == response
    assert restarted.status("pc-new", bound(value)) == response
    with pytest.raises(AdmissionError, match="DURABLE_GRANT_REQUIRED"):
        restarted.authorize_send("pc-new", bound(value, claim_id="must-never-send"))
    with pytest.raises(AdmissionError, match="DIFFERENT_BYTES"):
        restarted.reserve("pc-new", replace(value, session_id="new-session").payload())
    assert env.captures == 0 and env.authority.read_state()["reservations"] == []


@pytest.mark.parametrize("changes", [{"symbol": "AAPL"}, {"account_fingerprint": "b" * 64}, {"limit_price": "10.001"}])
def test_invalid_expired_scope_never_receives_negative_proof(env, changes):
    value = request(**changes)
    expired(env)
    with pytest.raises(AdmissionError):
        env.service.reserve("pc-new", value.payload())
    with pytest.raises(AdmissionError):
        env.service.status("pc-new", bound(value))
    assert env.service._stored(value.request_id)[2] is None


def test_negative_proof_bound_to_exact_session_hash_and_readback_integrity(env):
    value = request()
    expired(env)
    env.service.reserve("pc-new", value.payload())
    with pytest.raises(AdmissionError, match="SESSION_MISMATCH"):
        env.service.status("pc-new", {**bound(value), "session_id": "other-session"})
    with pytest.raises(AdmissionError, match="SESSION_MISMATCH"):
        env.service.status("pc-new", {**bound(value), "request_sha256": "b" * 64})
    with sqlite3.connect(env.service.path) as db:
        raw = json.loads(db.execute("SELECT response FROM requests").fetchone()[0])
        raw["payload"]["negative_admission_proof"]["session_id"] = "other-session"
        db.execute("UPDATE requests SET response=?", (json.dumps(raw),))
    with pytest.raises(AdmissionError, match="JOURNAL_VALUE_CHANGED"):
        env.service.reserve("pc-new", value.payload())


def test_expired_native_first_partial_pair_keeps_existing_recovery_without_absence_proof(env, monkeypatch):
    value = request()
    original = env.authority.reserve_remote_intraday
    monkeypatch.setattr(env.authority, "reserve_remote_intraday", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("native first crash")))
    with pytest.raises(SystemExit):
        env.service.reserve("pc-new", value.payload())
    monkeypatch.setattr(env.authority, "reserve_remote_intraday", original)
    expired(env)
    status = env.service.status("pc-new", bound(value))
    assert status["status"] == "PENDING" and "negative_admission_proof" not in status
    response = env.service.reserve("pc-new", value.payload())
    assert response["reason"] == "interrupted_before_paired_admission"
    assert "negative_admission_proof" not in response
    assert env.ledger.snapshot().reservations[0].status == "REJECTED"


@pytest.mark.parametrize("remove_live_rows", [False, True])
def test_uncertain_claim_or_its_retained_history_cannot_be_negatively_certified(env, remove_live_rows):
    value, grant, claim = env.claim()
    with sqlite3.connect(env.service.path) as db:
        db.execute("UPDATE requests SET plan=NULL,response=NULL WHERE id=?", (value.request_id,))
    if remove_live_rows:
        with sqlite3.connect(env.authority.path) as db:
            db.execute("DELETE FROM reservations WHERE id=?", (grant.reservation_id,))
        with sqlite3.connect(env.ledger.path) as db:
            db.execute("DELETE FROM reservations")
    expired(env)
    with pytest.raises(AdmissionError, match="PRIOR_ADMISSION_EVIDENCE"):
        env.service.reserve("pc-new", value.payload())
    assert env.service._stored(value.request_id)[2] is None
    if not remove_live_rows:
        assert env.authority.remote_claim(grant.reservation_id, participant_id="pc-new", request_sha256=value.sha256) == claim


def test_negative_proof_replay_rejects_a_later_orphan_claim(env):
    value = request()
    expired(env)
    env.service.reserve("pc-new", value.payload())
    orphan = {"reservation_id": "orphan-account-row", "request_sha256": value.sha256,
              "participant_id": "pc-new", "claim_id": "possible-earlier-send"}
    with sqlite3.connect(env.authority.path) as db:
        db.execute("INSERT INTO remote_submission_claims VALUES(?,?)", (orphan["reservation_id"], json.dumps(orphan)))
    for operation in (lambda: env.service.reserve("pc-new", value.payload()),
                      lambda: env.service.status("pc-new", bound(value))):
        with pytest.raises(AdmissionError, match="PRIOR_ADMISSION_EVIDENCE"):
            operation()


def test_no_response_without_plan_does_not_hide_a_corrupt_partial_pair(env):
    value, grant = env.grant()
    with sqlite3.connect(env.service.path) as db:
        db.execute("UPDATE requests SET plan=NULL,response=NULL WHERE id=?", (value.request_id,))
    with sqlite3.connect(env.authority.path) as db:
        db.execute("DELETE FROM reservations WHERE id=?", (grant.reservation_id,))
    expired(env)
    with pytest.raises(AdmissionError, match="PRIOR_ADMISSION_EVIDENCE"):
        env.service.status("pc-new", bound(value))
    assert env.service._stored(value.request_id)[2] is None


def test_negative_proof_replay_validates_scope_even_with_recomputed_journal_hash(env):
    value = request()
    expired(env)
    env.service.reserve("pc-new", value.payload())
    with sqlite3.connect(env.service.path) as db:
        raw = json.loads(db.execute("SELECT response FROM requests").fetchone()[0])
        raw["payload"]["negative_admission_proof"]["canonical_native_absent"] = False
        raw["payload"]["negative_admission_proof_sha256"] = canonical_sha256(raw["payload"]["negative_admission_proof"])
        raw["sha256"] = canonical_sha256(raw["payload"])
        db.execute("UPDATE requests SET response=?", (json.dumps(raw),))
    with pytest.raises(AdmissionError, match="NEGATIVE_ADMISSION_PROOF_CHANGED"):
        env.service.status("pc-new", bound(value))


def test_corrupt_retained_native_reservation_history_cannot_certify_absence(env):
    value, grant = env.grant()
    with sqlite3.connect(env.service.path) as db:
        db.execute("UPDATE requests SET plan=NULL,response=NULL WHERE id=?", (value.request_id,))
    with sqlite3.connect(env.authority.path) as db:
        db.execute("DELETE FROM reservations WHERE id=?", (grant.reservation_id,))
    with sqlite3.connect(env.ledger.path) as db:
        db.execute("DELETE FROM reservations")
        db.execute("UPDATE evidence SET payload='[]' WHERE kind='reservation'")
    expired(env)
    with pytest.raises(AdmissionError, match="NATIVE_RESERVATION_HISTORY_INVALID"):
        env.service.status("pc-new", bound(value))
    assert env.service._stored(value.request_id)[2] is None


@pytest.mark.parametrize("retained", ["native-submission", "native-order", "native-fills", "native-submission-time",
                                      "authority-submission", "authority-order", "authority-fills"])
def test_retained_effect_alone_prevents_negative_proof_when_reservation_history_is_lost(env, retained):
    value, grant, claim = env.accepted()
    evidence = OrderEvidence("retained-fill", grant.reservation_id, ACCOUNT, "201", env.clock().isoformat(),
                            "CANCELLED", grant.quantity, 1, 0, (Fill("retained-f1", 1, "10", env.clock().isoformat()),), complete=True)
    env.service.evidence("pc-new", bound(value, event_id="retained-fill-event", kind="ORDER_STATUS",
        claim_id=claim["claim_id"], order_evidence=json.loads(json.dumps(asdict(evidence)))))
    with sqlite3.connect(env.service.path) as db:
        db.execute("UPDATE requests SET plan=NULL,response=NULL WHERE id=?", (value.request_id,))
    with sqlite3.connect(env.ledger.path) as db:
        db.execute("DELETE FROM reservations")
        db.execute("DELETE FROM evidence WHERE kind != ?", ({"native-submission": "submission", "native-order": "order"}.get(retained, "keep-none"),))
        if retained != "native-fills":
            db.execute("DELETE FROM fills")
        if retained != "native-submission-time":
            db.execute("DELETE FROM order_submission_times")
    with sqlite3.connect(env.authority.path) as db:
        for table in ("reservations", "events", "remote_submission_claims"):
            db.execute(f"DELETE FROM {table}")
        if retained != "authority-order":
            db.execute("DELETE FROM order_evidence")
        if retained != "authority-fills":
            db.execute("DELETE FROM fills")
        if retained == "authority-submission":
            now = env.clock().isoformat()
            receipt = {"version": "account-exact-submission-receipt-v1", "reservation_id": grant.reservation_id,
                "account_fingerprint": ACCOUNT, "request_sha256": value.sha256, "broker_order_id": "201",
                "outcome": "ACCEPTED", "gate_used": True, "no_send_error_type": None, "submission_epoch": 1,
                "submission_holder_id": "synthetic-holder", "submission_started_at": now, "gate_epoch": 1,
                "gate_holder_id": "synthetic-holder", "gate_at": now, "received_at": now}
            db.execute("INSERT INTO submission_receipts VALUES(?,?,?,?,?)", (grant.reservation_id, "201",
                canonical_sha256(receipt), now, json.dumps(receipt)))
    expired(env)
    for operation in (lambda: env.service.reserve("pc-new", value.payload()),
                      lambda: env.service.status("pc-new", bound(value))):
        # Authority lease acquisition already rejects an orphan exact callback
        # receipt before the service can certify absence.
        error = AuthorityError if retained == "authority-submission" else AdmissionError
        match = "RESERVATION_NOT_FOUND" if retained == "authority-submission" else "RETAINED_OR_ORPHAN_EFFECT"
        with pytest.raises(error, match=match):
            operation()
    assert env.service._stored(value.request_id)[2] is None


def test_unrelated_retained_effect_with_intact_canonical_reservation_does_not_block_absence_proof(env):
    env.accepted()
    value = request(request_id="never-admitted-other-forecast", forecast_id="b" * 64, forecast_sha256="b" * 64)
    expired(env)
    result = env.service.reserve("pc-new", value.payload())
    assert result["reason"] == "expired_before_admission"
    assert result["negative_admission_proof"]["request_sha256"] == value.sha256
