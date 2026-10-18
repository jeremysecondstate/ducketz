"""Historical commitment absence never resolves or rearms an uncertain send."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ml.intraday import commitment_clearance as clearance
from ml.intraday.prediction import ContractError, instant
from ml.intraday.scout_journal import JournalStateError
from ml.intraday.scout_protocol import canonical_sha256
from ml.intraday.scout_session_books import ScoutBooks
from test_intraday_scout_broker import ACCOUNT, request, grant
from test_intraday_scout_local_execution import env, forecast, record, raw_order

REPOSITORY = Path(__file__).resolve().parents[1]
NOW = instant("2026-10-15T17:00:10Z")


def history_order(symbol="MSFT", identity=123):
    return dict(orderId=identity, status="FILLED", enteredTime="2026-10-12T17:20:00Z",
        orderLegCollection=[dict(instrument=dict(assetType="EQUITY", symbol=symbol))])


def broker_read(original):
    return dict(observed_at=NOW.isoformat(), from_entered_time="2026-10-12T00:00:00Z",
        to_entered_time=(NOW-timedelta(seconds=5)).isoformat(), account_fingerprint=ACCOUNT,
        snapshot_identity="verified-fixture-snapshot", original_request_id=original["request"]["request_id"],
        original_record_sha256=canonical_sha256(original), max_results=3000,
        orders=[history_order()], working_orders=[], transactions=[])


@pytest.fixture
def case(tmp_path):
    books = ScoutBooks(tmp_path / "books", account_fingerprint=ACCOUNT)
    journal = books.create_session("manual-scout:1")
    req = request(request_id="scout-local:" + "b" * 64)
    journal.prepare(req)
    journal.grant(grant(req))
    token = journal.claim_send(req.request_id)
    original = journal.outcome(req.request_id, token, None)
    result = SimpleNamespace(books=books, journal=journal, original=original, request=req,
        evidence=broker_read(original), path=tmp_path / "broker-read.json",
        contract=clearance.reviewed_day_sender_contract(REPOSITORY, review_reference="explicit offline fixture review"))
    yield result
    journal.close()


def install(case, *, now=NOW):
    case.path.write_text(json.dumps(case.evidence), encoding="utf-8")
    return clearance.create_clearance(case.books, case.journal, case.request.request_id,
        evidence_path=case.path, sender_contract=case.contract, repository=REPOSITORY, as_of=now)


def test_clearance_preserves_original_unknown_and_one_shot_and_survives_restart(case):
    before = deepcopy(case.original)
    before_events = case.journal.pending_events()
    receipt = install(case)
    assert receipt["historical_execution"] == "UNKNOWN"
    assert receipt["commitment"] == "no_surviving_day_commitment"
    assert case.journal.get(case.request.request_id) == before
    assert case.journal.pending_events() == before_events
    with pytest.raises(JournalStateError):
        case.journal.claim_send(case.request.request_id)
    # Raw evidence is durable under the private books, not dependent on scratch.
    case.path.unlink()
    with case.books.open_session(case.request.session_id) as reopened:
        assert clearance.load_clearance(case.books, reopened, before, as_of=NOW+timedelta(days=3)) == receipt
    assert len(list((case.books.root / "commitment-clearances").glob("*.broker.json"))) == 1


@pytest.mark.parametrize("damage", ["wrong_account", "wrong_request", "wrong_record", "missing_identity", "cap",
    "not_list", "partial_day", "insufficient_post_day", "stale_at_capture", "future_cutoff", "future_observation",
    "working", "original_symbol", "nested_original_symbol", "nested_working", "duplicate", "missing_symbol",
    "missing_entered", "outside_interval", "malformed_child", "replaced", "unknown_status", "missing_legs"])
def test_partial_stale_malformed_or_relevant_broker_evidence_cannot_clear(case, damage):
    e = case.evidence
    if damage == "wrong_account": e["account_fingerprint"] = "c" * 64
    elif damage == "wrong_request": e["original_request_id"] = "different"
    elif damage == "wrong_record": e["original_record_sha256"] = "c" * 64
    elif damage == "missing_identity": e.pop("snapshot_identity")
    elif damage == "cap": e["max_results"] = len(e["orders"])
    elif damage == "not_list": e["orders"] = {}
    elif damage == "partial_day": e["from_entered_time"] = "2026-10-12T17:00:00Z"
    elif damage == "insufficient_post_day":
        e["to_entered_time"] = "2026-10-13T23:00:00Z"
        e["observed_at"] = "2026-10-13T23:00:05Z"
    elif damage == "stale_at_capture": e["to_entered_time"] = (NOW-timedelta(seconds=61)).isoformat()
    elif damage == "future_cutoff": e["to_entered_time"] = (NOW+timedelta(seconds=1)).isoformat()
    elif damage == "future_observation":
        e["observed_at"] = (NOW+timedelta(seconds=1)).isoformat()
    elif damage == "working": e["working_orders"] = [history_order()]
    elif damage == "original_symbol": e["orders"] = [history_order("DOCU")]
    elif damage == "nested_original_symbol": e["orders"][0]["childOrderStrategies"] = [history_order("DOCU", 124)]
    elif damage == "nested_working":
        child = history_order(identity=124); child["status"] = "WORKING"
        e["orders"][0]["childOrderStrategies"] = [child]
    elif damage == "duplicate": e["orders"].append(deepcopy(e["orders"][0]))
    elif damage == "missing_symbol": e["orders"][0]["orderLegCollection"][0]["instrument"].pop("symbol")
    elif damage == "missing_entered": e["orders"][0].pop("enteredTime")
    elif damage == "outside_interval": e["orders"][0]["enteredTime"] = "2026-10-11T17:20:00Z"
    elif damage == "malformed_child": e["orders"][0]["childOrderStrategies"] = {}
    elif damage == "replaced": e["orders"][0]["status"] = "REPLACED"
    elif damage == "unknown_status": e["orders"][0]["status"] = "UNKNOWN"
    elif damage == "missing_legs": e["orders"][0].pop("orderLegCollection")
    with pytest.raises(ContractError): install(case)
    assert case.journal.get(case.request.request_id) == case.original
    assert not (case.books.root / "commitment-clearances").exists()


@pytest.mark.parametrize("damage", ["gtc", "unreviewed", "source_changed", "wrong_session", "loaded_claim"])
def test_non_day_or_unreviewed_sender_contract_is_rejected(case, damage):
    if damage == "gtc": case.contract["duration"] = "GOOD_TILL_CANCEL"
    elif damage == "unreviewed": case.contract["review_reference"] = ""
    elif damage == "source_changed": case.contract["source_sha256"][clearance.SOURCE_PATHS[0]] = "c" * 64
    elif damage == "wrong_session": case.contract["session"] = "PM"
    elif damage == "loaded_claim": case.contract["source_hash_meaning"] = "original_loaded_source"
    with pytest.raises(ContractError): install(case)


@pytest.mark.parametrize("damage", ["receipt", "broker_bytes", "missing_broker", "journal_binding", "original_record", "future"])
def test_readback_rejects_tampering_and_wrong_original_bindings(case, damage):
    saved = install(case)
    path = Path(saved["path"])
    evidence_path, = path.parent.glob("*.broker.json")
    record_value = deepcopy(case.original)
    if damage == "receipt":
        data = json.loads(path.read_text()); data["payload"]["commitment"] = "accepted"
        path.write_text(json.dumps(data))
    elif damage == "broker_bytes": evidence_path.write_text("{}")
    elif damage == "missing_broker": evidence_path.unlink()
    elif damage == "journal_binding":
        data = json.loads(path.read_text()); data["payload"]["journal_id"] = "f" * 32
        data["sha256"] = canonical_sha256(data["payload"]); path.write_text(json.dumps(data))
    elif damage == "original_record": record_value["request"]["symbol"] = "AAPL"
    with pytest.raises(ContractError):
        clearance.load_clearance(case.books, case.journal, record_value,
            as_of=NOW-timedelta(seconds=1) if damage == "future" else NOW)


def test_clearance_is_idempotent_and_does_not_rewrite_installed_bytes(case):
    first = install(case)
    raw = Path(first["path"]).read_bytes()
    second = install(case, now=NOW+timedelta(days=1))
    assert second == first and Path(first["path"]).read_bytes() == raw


@pytest.mark.parametrize("cash", [0, 250])
def test_new_forecast_uses_fresh_capacity_after_clearance_but_original_never_resends(env, tmp_path, cash):
    env.broker.post_error = TimeoutError("offline lost response")
    original_forecast = env.forecast
    first = env.executor.step(original_forecast)
    assert first["submission_status"] == "UNKNOWN" and len(env.broker.posts) == 1
    original_record = record(env)
    e = broker_read(original_record)
    path = tmp_path / "captured.json"; path.write_text(json.dumps(e))
    saved = clearance.create_clearance(env.executor.books, env.executor.journal,
        original_record["request"]["request_id"], evidence_path=path,
        sender_contract=clearance.reviewed_day_sender_contract(REPOSITORY, review_reference="offline review"),
        repository=REPOSITORY, as_of=NOW)
    env.state.now = NOW
    history_before = env.executor.intents.history(original_forecast["forecast_id"])
    recovery, = env.executor.recover()
    assert env.executor.intents.history(original_forecast["forecast_id"]) == history_before
    assert recovery["submitted_quantity"] is None and recovery["filled_quantity"] is None
    assert recovery["realized_pnl"] is None
    assert recovery["disposition"] == "historical_submission_unknown_no_open_commitment"
    assert recovery["submission_status"] == "UNKNOWN"
    assert env.executor.journal.get(original_record["request"]["request_id"]) == original_record
    assert env.executor.step(original_forecast)["disposition"] == "wrong_session_date"
    assert len(env.broker.posts) == 1
    env.reference = instant("2026-10-15T17:00:00Z")
    env.state.now = env.reference + timedelta(minutes=16, seconds=10)
    env.forecast = forecast(env)
    env.broker.post_error = None
    env.cash = cash
    before_captures = env.captures
    outcome = env.executor.step(env.forecast)
    assert outcome["disposition"] == ("no_purchasing_capacity" if cash == 0 else "submitted")
    assert env.captures >= before_captures+2 and len(env.broker.posts) == (1 if cash == 0 else 2)
    if cash:
        assert outcome["adjusted_quantity"] == 2 < outcome["desired_quantity"] == 4
    assert env.executor.journal.get(original_record["request"]["request_id"]) == original_record
    with pytest.raises(JournalStateError):
        env.executor.journal.claim_send(original_record["request"]["request_id"])


def test_later_exact_acceptance_uses_ordinary_status_recovery_without_replaying(env, tmp_path):
    env.broker.post_error = TimeoutError("offline lost response")
    env.executor.step(env.forecast)
    original = record(env)
    path = tmp_path / "captured.json"; path.write_text(json.dumps(broker_read(original)))
    clearance.create_clearance(env.executor.books, env.executor.journal, original["request"]["request_id"],
        evidence_path=path, sender_contract=clearance.reviewed_day_sender_contract(REPOSITORY,
            review_reference="offline historical review"), repository=REPOSITORY, as_of=NOW)
    # A validated exact acceptance arriving later remains authoritative. The
    # historical absence receipt cannot suppress ordinary known-order recovery.
    env.executor.journal.outcome(original["request"]["request_id"], original["send_token"], "123")
    env.broker.orders = [raw_order(env, status="CANCELED", filled=1)]
    env.state.now = NOW
    observed, = env.executor.recover()
    assert observed["disposition"] == "order_status_reconciled"
    assert observed["broker_order_id"] == "123" and observed["filled_quantity"] == 1
    assert record(env)["state"] == "ACCEPTED" and len(env.broker.posts) == 1
