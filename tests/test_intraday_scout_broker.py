"""Offline broker effects, real one-shot journal and existing exact-ID recovery."""
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from ml.intraday.scout_broker import ScoutBrokerAdapter, ScoutBrokerError
from ml.intraday.scout_client import ScoutClient
from ml.intraday.scout_journal import JournalStateError, ScoutJournal
from ml.intraday.scout_protocol import Grant, ProtocolError, Request
from ml.stock_trader.horizon_broker import OrderHistoryError


ACCOUNT = "a" * 64
GENERATION = "local-snapshot-generation"
NOW = datetime.fromisoformat("2026-10-12T17:15:05+00:00")


def request(**changes):
    values = dict(request_id="scout-request:1", account_fingerprint=ACCOUNT,
        session_id="manual-scout:1", symbol="DOCU", side="BUY", quantity=10,
        limit_price="42.5", forecast_id="b" * 64, forecast_sha256="b" * 64,
        snapshot_sha256="c" * 64, sizing_sha256="d" * 64,
        reference_at="2026-10-12T17:00:00Z", issued_at="2026-10-12T17:15:01Z",
        target_close_at="2026-10-12T17:45:00Z", valid_until="2026-10-12T17:16:00Z")
    return Request(**(values | changes))


def grant(req, **changes):
    values = dict(grant_id="scout-grant:1", request_id=req.request_id,
        request_sha256=req.sha256, account_fingerprint=ACCOUNT, session_id=req.session_id,
        authority_epoch=1, quantity=3, reservation_id="shared-reservation:1",
        issued_at=req.issued_at, expires_at=req.valid_until)
    return Grant(**(values | changes))


def raw_order(*, status="WORKING", filled=0, identity="123"):
    return {"orderId": identity, "status": status, "quantity": 3,
        "filledQuantity": filled, "remainingQuantity": 0 if status == "CANCELED" else 3 - filled,
        "orderLegCollection": [{"legId": 1, "instruction": "BUY", "quantity": 3,
            "instrument": {"assetType": "EQUITY", "symbol": "DOCU"}}],
        "orderActivityCollection": ([{"activityType": "EXECUTION", "executionLegs": [
            {"legId": 1, "executionId": "fill:1", "quantity": filled, "price": 42.4,
             "time": "2026-10-12T17:15:03+00:00"}]}] if filled else [])}


class Broker:
    def __init__(self):
        self.account = ACCOUNT
        self.generation = GENERATION
        self.context_generation = GENERATION
        self.posts, self.deletes, self.reads = [], [], []
        self.location = "https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/123"
        self.before_transport = lambda: None
        self.after_read = lambda: None
        self.post_error = self.delete_error = None
        self.orders = [raw_order()]
        self.contexts = []

    def stable_account_fingerprint(self):
        return self.account

    def verify_read_snapshot(self, expected):
        if self.generation != expected:
            raise ScoutBrokerError("Account/OAuth generation changed")

    def prepare_order_submission(self):
        value = SimpleNamespace(account_hash="synthetic-account", identity_fingerprint=self.context_generation)
        self.contexts.append(value)
        return value

    def submit_prepared_order(self, payload, context, *, before_post):
        self.before_transport()
        before_post()
        self.posts.append((payload, context))
        if self.post_error:
            raise self.post_error
        return self.location

    def cancel_prepared_order(self, order_id, context, *, before_delete):
        self.before_transport()
        before_delete()
        self.deletes.append((order_id, context))
        if self.delete_error:
            raise self.delete_error

    def get_orders(self, **kwargs):
        self.reads.append(kwargs)
        self.after_read()
        return self.orders


@pytest.fixture
def env(tmp_path):
    journal = ScoutJournal(tmp_path / "scout.sqlite3", binding={"account_fingerprint": ACCOUNT,
        "session_id": "manual-scout:1", "participant_id": "pc-new", "stream": "scout-15m"})
    clock, broker = SimpleNamespace(now=NOW), Broker()
    adapter = ScoutBrokerAdapter(broker, journal, account_fingerprint=ACCOUNT,
        session_id="manual-scout:1", symbols=frozenset({"DOCU"}), authority_epoch=1,
        maximum_evidence_age_seconds=30, clock=lambda: clock.now)
    yield SimpleNamespace(journal=journal, broker=broker, clock=clock, adapter=adapter)
    journal.close()


def prepared(env, req=None, permitted=None, *, claim=True, accepted=False):
    req = req or request()
    permitted = permitted or grant(req)
    env.journal.prepare(req)
    env.journal.grant(permitted)
    if claim:
        token = env.journal.claim_send(req.request_id)
        if accepted:
            env.journal.outcome(req.request_id, token, "123")
    return req, permitted


def send(env, req, permitted, **changes):
    options = dict(expected_snapshot_identity=GENERATION,
                   authorization_gate=lambda *_: True, final_gate=lambda *_: True)
    return env.adapter.send(req, permitted, **(options | changes))


def cancel(env, req, **changes):
    options = dict(expected_snapshot_identity=GENERATION,
                   authorization_gate=lambda *_: True, final_gate=lambda *_: True)
    return env.adapter.cancel(req.request_id, **(options | changes))


def test_exact_local_post_uses_granted_quantity_frozen_context_and_both_gates(env):
    req, permitted = prepared(env)
    events = []
    def authorized(actual, actual_grant):
        assert (actual, actual_grant) == (req, permitted)
        assert env.journal.get(req.request_id)["state"] == "SENDING"
        events.append("durable-atlas-send-authorization")
        return True
    def final(*_):
        events.append("runtime-gate")
        return True
    assert send(env, req, permitted, authorization_gate=authorized, final_gate=final) == "123"
    assert events == ["durable-atlas-send-authorization", "runtime-gate"] * 2
    payload, context = env.broker.posts[0]
    assert context is env.broker.contexts[0]
    assert payload["orderLegCollection"][0] == {"instruction": "BUY", "positionEffect": "AUTOMATIC",
        "quantity": 3, "instrument": {"symbol": "DOCU", "assetType": "EQUITY"}}
    assert payload["price"] == "42.50" and payload["orderType"] == "LIMIT"


@pytest.mark.parametrize("state", ["missing", "PREPARED", "GRANTED", "UNKNOWN", "ACCEPTED", "NOT_SENT"])
def test_no_unclaimed_or_previously_sent_journal_state_can_post(env, state):
    req, permitted = request(), grant(request())
    if state != "missing":
        env.journal.prepare(req)
    if state in {"GRANTED", "UNKNOWN", "ACCEPTED", "NOT_SENT"}:
        env.journal.grant(permitted)
    if state in {"UNKNOWN", "ACCEPTED"}:
        token = env.journal.claim_send(req.request_id)
        env.journal.outcome(req.request_id, token, "123" if state == "ACCEPTED" else None)
    if state == "NOT_SENT":
        env.journal.revoke_unsent(req.request_id, "human-stop")
    with pytest.raises(ScoutBrokerError):
        send(env, req, permitted)
    assert env.broker.posts == [] and env.broker.contexts == []


@pytest.mark.parametrize("field,value", [("quantity", 2), ("authority_epoch", 2),
    ("reservation_id", "other"), ("grant_id", "other")])
def test_mutated_grant_never_reaches_post(env, field, value):
    req, permitted = prepared(env)
    with pytest.raises(ScoutBrokerError):
        send(env, req, replace(permitted, **{field: value}))
    assert env.broker.posts == []


@pytest.mark.parametrize("callback", ["authorization_gate", "final_gate"])
@pytest.mark.parametrize("value", [False, None, 1])
def test_explicit_authorization_and_final_gate_are_required(env, callback, value):
    req, permitted = prepared(env)
    with pytest.raises(ScoutBrokerError):
        send(env, req, permitted, **{callback: lambda *_: value})
    assert env.broker.posts == []


@pytest.mark.parametrize("change", ["account", "generation", "prepared_generation", "expiry", "stop"])
def test_last_transport_gate_checks_mutations_and_leaves_claim_consumed(env, change):
    req, permitted = prepared(env)
    allowed = {"value": True}
    if change == "prepared_generation":
        env.broker.context_generation = "other"
    def mutate():
        if change == "account": env.broker.account = "f" * 64
        if change == "generation": env.broker.generation = "other"
        if change == "expiry": env.clock.now = datetime.fromisoformat(permitted.expires_at)
        if change == "stop": allowed["value"] = False
    env.broker.before_transport = mutate
    with pytest.raises(ScoutBrokerError):
        send(env, req, permitted, final_gate=lambda *_: allowed["value"])
    assert env.broker.posts == [] and env.journal.get(req.request_id)["state"] == "SENDING"


def test_slow_final_gate_cannot_send_after_expiry(env):
    req, permitted = prepared(env)
    def gate(*_):
        env.clock.now = datetime.fromisoformat(permitted.expires_at)
        return True
    with pytest.raises(ScoutBrokerError, match="current"):
        send(env, req, permitted, final_gate=gate)
    assert env.broker.posts == []


@pytest.mark.parametrize("stamp,reason", [("2026-10-12T23:45:00+00:00", "cutoff"),
    ("2026-10-12T09:15:05+00:00", "closed"), ("2026-10-11T17:15:05+00:00", "closed")])
def test_cutoff_and_closed_sessions_fail_with_otherwise_current_request(env, stamp, reason):
    now = datetime.fromisoformat(stamp)
    req = request(reference_at=(now - timedelta(minutes=15)).isoformat(),
        issued_at=(now - timedelta(seconds=1)).isoformat(),
        valid_until=(now + timedelta(seconds=30)).isoformat(),
        target_close_at=(now + timedelta(minutes=15)).isoformat())
    req, permitted = prepared(env, req)
    env.clock.now = now
    with pytest.raises(ScoutBrokerError, match=reason):
        send(env, req, permitted)
    assert env.broker.posts == []


def test_price_that_builder_would_round_cannot_change_reserved_economics(env):
    req, permitted = prepared(env, request(limit_price="42.501"))
    with pytest.raises(ScoutBrokerError, match="price"):
        send(env, req, permitted)
    assert env.broker.posts == []


def test_large_quantity_that_builder_would_round_cannot_change_reserved_shares(env):
    req = request(quantity=2**53 + 1)
    req, permitted = prepared(env, req, grant(req, quantity=req.quantity))
    with pytest.raises(ScoutBrokerError, match="quantity"):
        send(env, req, permitted)
    assert env.broker.posts == []


@pytest.mark.parametrize("location", [None, "123", "", " https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/123",
    "http://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/123",
    "https://other.example/trader/v1/accounts/synthetic-account/orders/123",
    "https://api.schwabapi.com/trader/v1/accounts/other/orders/123",
    "https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/123?other=1",
    "https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/01",
    "https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/123/",
    "https://api.schwabapi.com/trader/v1/accounts/synthetic-account/orders/１２３"])
def test_missing_or_mismatched_acceptance_is_unknown_and_never_reposted(env, location):
    req, permitted = prepared(env, claim=False)
    env.broker.location = location
    client = ScoutClient(env.journal, SimpleNamespace(), account_fingerprint=ACCOUNT,
        session_id=req.session_id, symbols=frozenset({"DOCU"}), authority_epoch=1, clock=lambda: env.clock.now)
    with pytest.raises(ProtocolError):
        client.submit(req.request_id, send=lambda r, g: send(env, r, g), final_gate=lambda *_: True)
    assert env.journal.get(req.request_id)["state"] == "UNKNOWN"
    with pytest.raises(JournalStateError):
        client.submit(req.request_id, send=lambda r, g: send(env, r, g), final_gate=lambda *_: True)
    assert len(env.broker.posts) == 1


@pytest.mark.parametrize("error", [TimeoutError(), KeyboardInterrupt()])
def test_lost_post_reply_or_process_exit_retains_one_shot_claim(env, error):
    req, permitted = prepared(env, claim=False)
    env.broker.post_error = error
    client = ScoutClient(env.journal, SimpleNamespace(), account_fingerprint=ACCOUNT,
        session_id=req.session_id, symbols=frozenset({"DOCU"}), authority_epoch=1, clock=lambda: env.clock.now)
    with pytest.raises(type(error)):
        client.submit(req.request_id, send=lambda r, g: send(env, r, g), final_gate=lambda *_: True)
    assert env.journal.get(req.request_id)["state"] == ("UNKNOWN" if isinstance(error, Exception) else "SENDING")
    with pytest.raises(JournalStateError):
        client.submit(req.request_id, send=lambda r, g: send(env, r, g), final_gate=lambda *_: True)
    assert len(env.broker.posts) == 1


def test_status_uses_only_journal_accepted_id_grant_quantity_and_complete_fill_detail(env):
    req, permitted = prepared(env, accepted=True)
    env.broker.orders = [raw_order(status="CANCELED", filled=2), raw_order(identity="999")]
    observed = env.adapter.status(req.request_id)
    assert observed.broker_order_id == "123" and observed.reservation_id == permitted.reservation_id
    assert observed.order_quantity == 3 and observed.cumulative_filled_quantity == 2
    assert observed.status == "CANCELLED" and observed.remaining_quantity == 0
    assert len(observed.fills) == 1 and observed.fills[0].quantity == 2
    assert observed.fills[0].fill_id.startswith("schwab-fill:")
    assert env.journal.get(req.request_id)["state"] == "ACCEPTED"


@pytest.mark.parametrize("damage", ["missing", "wrong_quantity", "missing_fills", "future_fill", "changed_account"])
def test_incomplete_or_mismatched_history_cannot_be_terminal_evidence(env, damage):
    req, _ = prepared(env, accepted=True)
    env.broker.orders = [raw_order(status="FILLED", filled=3)]
    if damage == "missing": env.broker.orders = [raw_order(identity="999")]
    if damage == "wrong_quantity": env.broker.orders[0]["quantity"] = 10
    if damage == "missing_fills": env.broker.orders[0]["orderActivityCollection"] = []
    if damage == "future_fill": env.broker.orders[0]["orderActivityCollection"][0]["executionLegs"][0]["time"] = "2026-10-12T17:30:00Z"
    if damage == "changed_account": env.broker.after_read = lambda: setattr(env.broker, "account", "f" * 64)
    with pytest.raises(OrderHistoryError):
        env.adapter.status(req.request_id)
    assert env.journal.get(req.request_id)["state"] == "ACCEPTED"


def test_unknown_submission_is_never_searched_by_symbol_or_cancelled(env):
    req, _ = prepared(env)
    record = env.journal.get(req.request_id)
    env.journal.outcome(req.request_id, record["send_token"], None)
    with pytest.raises(ScoutBrokerError, match="accepted"):
        env.adapter.status(req.request_id)
    with pytest.raises(ScoutBrokerError, match="accepted"):
        cancel(env, req)
    assert env.broker.reads == [] and env.broker.deletes == []


def test_cancel_ack_is_not_terminal_and_partial_fill_remains_exact_evidence(env):
    req, _ = prepared(env, accepted=True)
    env.broker.orders = [raw_order(filled=1)]
    persisted = []
    def authorize(actual, actual_grant, observed):
        persisted.append((actual.request_id, actual_grant.reservation_id, observed.evidence_id))
        return True
    result = cancel(env, req, authorization_gate=authorize)
    assert result.delete_acknowledged and result.evidence.status == "PARTIAL"
    assert result.evidence.cumulative_filled_quantity == 1 and result.evidence.remaining_quantity == 2
    assert env.broker.deletes[0][0] == "123" and len(persisted) == 2
    assert env.journal.get(req.request_id)["state"] == "ACCEPTED"
    env.broker.orders = [raw_order(status="CANCELED", filled=2)]
    terminal = cancel(env, req)
    assert not terminal.delete_acknowledged and terminal.evidence.status == "CANCELLED"
    assert terminal.evidence.cumulative_filled_quantity == 2 and len(env.broker.deletes) == 1


def test_pending_cancel_never_issues_another_delete(env):
    req, _ = prepared(env, accepted=True)
    env.broker.orders = [raw_order(status="PENDING_CANCEL")]
    result = cancel(env, req)
    assert not result.delete_acknowledged and result.evidence.status == "WORKING"
    assert env.broker.deletes == []


@pytest.mark.parametrize("mutation", ["account", "generation", "age", "stop", "authorization"])
def test_last_delete_gate_preserves_identity_commitment_on_failed_prerequisite(env, mutation):
    req, _ = prepared(env, accepted=True)
    flags = {"stop": False, "authorized": True}
    def mutate():
        if mutation == "account": env.broker.account = "f" * 64
        if mutation == "generation": env.broker.generation = "other"
        if mutation == "age": env.clock.now += timedelta(seconds=31)
        if mutation == "stop": flags["stop"] = True
        if mutation == "authorization": flags["authorized"] = False
    env.broker.before_transport = mutate
    with pytest.raises(ScoutBrokerError):
        cancel(env, req, authorization_gate=lambda *_: flags["authorized"], final_gate=lambda *_: not flags["stop"])
    assert env.broker.deletes == [] and env.journal.get(req.request_id)["broker_order_id"] == "123"


def test_cancel_timeout_never_retries_or_marks_terminal(env):
    req, _ = prepared(env, accepted=True)
    env.broker.delete_error = TimeoutError("synthetic lost DELETE reply")
    with pytest.raises(TimeoutError):
        cancel(env, req)
    assert len(env.broker.deletes) == 1 and env.journal.get(req.request_id)["state"] == "ACCEPTED"


def test_recovery_and_cancellation_continue_after_grant_expiry_and_cutoff(env):
    req, _ = prepared(env, accepted=True)
    env.clock.now = datetime.fromisoformat("2026-10-13T00:30:00+00:00")
    assert cancel(env, req).delete_acknowledged
    assert env.broker.deletes[0][0] == "123"


@pytest.mark.parametrize("operation", ["post", "delete"])
def test_local_stop_during_last_account_verification_blocks_the_transport(env, operation):
    req, permitted = prepared(env, accepted=operation == "delete")
    running, checks = {"value": True}, []
    original = env.broker.verify_read_snapshot
    def verify(expected):
        original(expected)
        checks.append(expected)
        if len(checks) == 4:
            running["value"] = False
    env.broker.verify_read_snapshot = verify
    with pytest.raises(ScoutBrokerError, match="Final Scout"):
        if operation == "post":
            send(env, req, permitted, final_gate=lambda *_: running["value"])
        else:
            cancel(env, req, final_gate=lambda *_: running["value"])
    assert len(checks) == 4 and env.broker.posts == [] and env.broker.deletes == []
