"""Offline retrospective proof: no transport, no journal rewrite, no replay."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import pretransport_clearance as clearance
from ml.intraday import scout_journal, scout_local_execution as execution
from ml.intraday.prediction import ContractError, instant
from ml.intraday.scout_journal import JournalStateError
from ml.intraday.scout_protocol import canonical_sha256
from test_intraday_scout_local_execution import env, forecast, record

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def case(env, monkeypatch, tmp_path):
    # Fixture source is the checked-out implementation. Only tests substitute
    # its hash map; production accepts the reviewed historical source constants.
    source_files = {name: (REPOSITORY / name).read_bytes().decode("utf-8")
                    for name in clearance.SUPPORTED_SOURCE}
    supported = {name: hashlib.sha256(raw.encode()).hexdigest() for name, raw in source_files.items()}
    monkeypatch.setattr(clearance, "SUPPORTED_SOURCE", supported)
    manifest_text = json.dumps(dict(schema="scout-isolated-live-runtime-v1", files=supported, source_root=str(REPOSITORY)))
    source = dict(manifest_text=manifest_text, manifest_sha256=hashlib.sha256(manifest_text.encode()).hexdigest(),
                  files=source_files)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(scout_journal, "datetime", Clock)
    saved_read = execution._saved_forecast
    calls = []

    def fail_first_adapter_final_gate(*args, **kwargs):
        calls.append(1)
        # step, outer client gate, claimed client gate, adapter authorization,
        # then adapter's initial final gate. No transport was entered.
        if len(calls) == 5:
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = 261
            raise exc
        return saved_read(*args, **kwargs)

    monkeypatch.setattr(execution, "_saved_forecast", fail_first_adapter_final_gate)
    value = env.executor.step(env.forecast)
    monkeypatch.setattr(execution, "_saved_forecast", saved_read)
    assert len(calls) == 5 and env.broker.posts == []
    original = record(env)
    assert original["state"] == "UNKNOWN"
    actual = value["evidence"]["submission_failure"]
    assert actual["stage"] == "send"
    assert any(f["file"] == "scout_broker.py" and f["function"] == "send"
               and f["line"] == 182 for f in actual["original"]["chain"][0]["frames"])
    # Reconstitute the reviewed legacy diagnostic using synthetic identities;
    # the actual adapter failure above independently exercises the zero-POST path.
    value = deepcopy(value)
    value["evidence"]["submission_failure"] = dict(stage="send", original=dict(chain_truncated=False,
        chain=[dict(type="OperationalError", message="database is locked", sqlite_errorcode=261,
                    frames=deepcopy(clearance.EXPECTED_FRAMES))]))
    session = env.executor.session_id
    at = env.state.now + timedelta(seconds=1)
    event = dict(id=100, cycle_id=session, symbol=original["request"]["symbol"],
                 recorded_at=at.isoformat(), kind="execution_step", payload=value)
    stop = dict(id=101, cycle_id=session, symbol=None, recorded_at=(at + timedelta(seconds=1)).isoformat(),
                kind="manual_session_stopped", payload=dict(pending_order_identity_preserved=True,
                    stop_reason="manual_stop", worker_errors=[]))
    now = instant(original["request"]["valid_until"]) + timedelta(seconds=1)
    journal_id, events = clearance._audit(env.executor.books, env.executor.journal, original)
    evidence = dict(version=clearance.VERSION, original_record=deepcopy(original),
        original_record_sha256=canonical_sha256(original), journal_id=journal_id, journal_events=events,
        execution_event=event, execution_event_sha256=canonical_sha256(event), stop_event=stop,
        stop_event_sha256=canonical_sha256(stop), source=source,
        process_end=dict(session_id=session, pid=123, process_created_at_epoch=(at-timedelta(minutes=10)).timestamp(),
            verified_absent_at=(at+timedelta(seconds=2)).isoformat(), original_manifest_sha256=source["manifest_sha256"]),
        runtime_observation=dict(at=(at+timedelta(microseconds=1)).isoformat(), session_id=session,
            process=dict(verified=True, pid=123, created_at_epoch=(at-timedelta(minutes=10)).timestamp(),
                module="ml.intraday.live_session", binding="exact_birth", entrypoint_kind="reviewed_isolated_live_launcher",
                launcher_path=str(REPOSITORY / "launch.py"), launcher_sha256="1"*64,
                manifest_path=str(REPOSITORY / "manifest.json"), manifest_sha256=source["manifest_sha256"],
                source_root=str(REPOSITORY), source_files_verified=len(supported), recipe_sha256="2"*64)))
    path = tmp_path / "reviewed-proof.json"
    return SimpleNamespace(env=env, original=original, evidence=evidence, path=path, now=now)


def install(case):
    case.path.write_text(json.dumps(case.evidence), encoding="utf-8")
    return clearance.create_clearance(case.env.executor.books, case.env.executor.journal,
        case.original["request"]["request_id"], evidence_path=case.path, repository=REPOSITORY,
        review_reference="offline synthetic first-gate source and stopped-process review", as_of=case.now)


def load(case, *, original=None, now=None):
    return clearance.load_clearance(case.env.executor.books, case.env.executor.journal,
        case.original if original is None else original, as_of=case.now if now is None else now)


def rehash_events(case):
    for name in ("execution_event", "stop_event"):
        case.evidence[name + "_sha256"] = canonical_sha256(case.evidence[name])


def test_initial_gate_failure_receipt_is_durable_idempotent_and_preserves_one_send(case):
    journal = case.env.executor.journal
    before = journal.pending_events()
    receipt = install(case)
    assert receipt["historical_execution"] == "UNKNOWN"
    assert receipt["commitment"] == "no_order_transport_entered"
    assert record(case.env) == case.original and journal.pending_events() == before
    raw = Path(receipt["path"]).read_bytes()
    assert install(case) == receipt and Path(receipt["path"]).read_bytes() == raw
    case.path.unlink()
    assert load(case) == receipt
    with pytest.raises(JournalStateError):
        journal.claim_send(case.original["request"]["request_id"])
    assert case.env.broker.posts == []


def test_missing_receipt_is_read_only_none(case):
    assert load(case) is None
    assert not (case.env.executor.books.root / "pretransport-clearances").exists()


@pytest.mark.parametrize("damage", ["extra", "record", "account", "request", "journal", "token", "events",
    "event_hash", "wrong_symbol", "wrong_forecast", "wrong_event_session", "wrong_event_type", "stage",
    "no_trace", "truncated", "after_post", "transport_callback", "line", "code", "message", "extra_error",
    "source", "manifest", "source_digest", "process_session", "process_live", "process_manifest", "birth",
    "stop_session", "stop_error", "stop_before_error", "unexpired", "runtime_pid", "runtime_birth",
    "runtime_source", "runtime_verified", "runtime_time", "runtime_count"])
def test_incomplete_or_ambiguous_proof_cannot_clear(case, damage):
    e = case.evidence
    row = e["execution_event"]
    failure = row["payload"]["evidence"]["submission_failure"]
    error = failure["original"]["chain"][0]
    if damage == "extra": e["generic_unsent"] = True
    elif damage == "record": e["original_record_sha256"] = "0" * 64
    elif damage == "account": e["original_record"]["request"]["account_fingerprint"] = "0" * 64
    elif damage == "request": row["payload"]["evidence"]["request_id"] = "scout-local:wrong"
    elif damage == "journal": e["journal_id"] = "0" * 32
    elif damage == "token": e["original_record"]["send_token"] = "0" * 64
    elif damage == "events": e["journal_events"].pop()
    elif damage == "event_hash": e["execution_event_sha256"] = "0" * 64
    elif damage == "wrong_symbol": row["symbol"] = "OTHER"
    elif damage == "wrong_forecast": row["payload"]["forecast_id"] = "0" * 64
    elif damage == "wrong_event_session": row["cycle_id"] = "another-session"
    elif damage == "wrong_event_type": row["kind"] = "execution_retry"
    elif damage == "stage": failure["stage"] = "record_acceptance"
    elif damage == "no_trace": error.pop("frames")
    elif damage == "truncated": failure["original"]["chain_truncated"] = True
    elif damage == "after_post": error["frames"].append(dict(file="schwab.py", function="submit_prepared_order", line=692))
    elif damage == "transport_callback": error["frames"][2]["line"] = 183
    elif damage == "line": error["frames"][3]["line"] += 1
    elif damage == "code": error["sqlite_errorcode"] = 5
    elif damage == "message": error["message"] = "unknown OperationalError"
    elif damage == "extra_error": error["returned_broker_order_id"] = "123"
    elif damage == "source": e["source"]["files"]["ml/intraday/scout_broker.py"] += "\n"
    elif damage == "manifest": e["source"]["manifest_text"] += " "
    elif damage == "source_digest": e["source"]["files"].pop("ml/intraday/live_session.py")
    elif damage == "process_session": e["process_end"]["session_id"] = "wrong"
    elif damage == "process_live": e["process_end"]["verified_absent_at"] = row["recorded_at"]
    elif damage == "process_manifest": e["process_end"]["original_manifest_sha256"] = "0" * 64
    elif damage == "birth": e["process_end"]["process_created_at_epoch"] = float("nan")
    elif damage == "stop_session": e["stop_event"]["cycle_id"] = "other"
    elif damage == "stop_error": e["stop_event"]["payload"]["worker_errors"] = ["unresolved"]
    elif damage == "stop_before_error": e["stop_event"]["recorded_at"] = (instant(row["recorded_at"])-timedelta(seconds=1)).isoformat()
    elif damage == "unexpired": case.now = instant(case.original["request"]["valid_until"]) - timedelta(seconds=1)
    elif damage == "runtime_pid": e["runtime_observation"]["process"]["pid"] += 1
    elif damage == "runtime_birth": e["runtime_observation"]["process"]["created_at_epoch"] += 1
    elif damage == "runtime_source": e["runtime_observation"]["process"]["source_root"] = "different-source"
    elif damage == "runtime_verified": e["runtime_observation"]["process"]["verified"] = False
    elif damage == "runtime_time": e["runtime_observation"]["at"] = (case.now+timedelta(seconds=1)).isoformat()
    elif damage == "runtime_count": e["runtime_observation"]["process"]["source_files_verified"] += 1
    if damage != "event_hash": rehash_events(case)
    with pytest.raises((ContractError, ValueError, KeyError)):
        install(case)
    assert record(case.env) == case.original and case.env.broker.posts == []
    assert not (case.env.executor.books.root / "pretransport-clearances").exists()


@pytest.mark.parametrize("damage", ["proof", "receipt", "missing", "changed_record", "future", "hardlink", "lifecycle"])
def test_load_rechecks_durable_proof_and_live_original(case, damage, tmp_path):
    receipt = install(case)
    path = Path(receipt["path"])
    proof, = path.parent.glob("*.proof.json")
    original = deepcopy(case.original)
    if damage == "proof": proof.write_text("{}")
    elif damage == "receipt":
        data = json.loads(path.read_text()); data["payload"]["commitment"] = "accepted"
        data["sha256"] = canonical_sha256(data["payload"]); path.write_text(json.dumps(data))
    elif damage == "missing": proof.unlink()
    elif damage == "changed_record": original["send_token"] = "0" * 64
    elif damage == "future": case.now -= timedelta(seconds=1)
    elif damage == "hardlink":
        import os
        os.link(proof, tmp_path / "proof-alias.json")
    elif damage == "lifecycle":
        # Corruption cannot turn an unaudited lifecycle record into clearance.
        with sqlite3.connect(case.env.executor.journal.path) as db:
            db.execute("insert into lifecycle_records values (?,?,?)", (original["request"]["request_id"], "{}", canonical_sha256({})))
    with pytest.raises((ContractError, ValueError)):
        load(case, original=original)


@pytest.mark.parametrize("cash", [0, 250])
def test_distinct_new_forecast_uses_fresh_capacity_and_original_never_replays(case, cash):
    install(case)
    env = case.env
    old = deepcopy(case.original)
    env.state.now = case.now
    same = env.executor.step(env.forecast)
    assert same["submitted_quantity"] == 0 and env.broker.posts == []
    newer = forecast(env, advance=True)
    env.state.now = case.now + timedelta(seconds=10)
    env.cash = cash
    before = env.captures
    value = env.executor.step(newer)
    assert env.captures >= before + 2
    assert value["disposition"] == ("submitted" if cash else "no_purchasing_capacity")
    assert len(env.broker.posts) == (1 if cash else 0)
    assert record(env) == old
    with pytest.raises(JournalStateError):
        env.executor.journal.claim_send(old["request"]["request_id"])


@pytest.mark.parametrize("boundary", ["manual_stop", "expired"])
def test_new_clearance_cannot_override_controls_or_target(case, boundary):
    install(case)
    env = case.env
    newer = forecast(env, advance=True)
    env.state.now = case.now + timedelta(seconds=10)
    if boundary == "manual_stop":
        env.state.stop = True
        with pytest.raises(ContractError): env.executor.step(newer)
    else:
        env.state.now = instant(newer["target_close"]) + timedelta(seconds=1)
        assert env.executor.step(newer)["submitted_quantity"] == 0
    assert env.broker.posts == [] and record(env) == case.original


def test_recover_retains_unknown_without_inventing_or_appending_execution_history(case):
    install(case)
    env = case.env
    env.state.now = case.now
    before = env.executor.intents.history(env.forecast["forecast_id"])
    result, = env.executor.recover()
    assert result["disposition"] == "historical_submission_unknown_no_transport_commitment"
    assert result["submission_status"] == "UNKNOWN"
    assert result["submitted_quantity"] is None and result["filled_quantity"] is None
    assert result["realized_pnl"] is None
    assert env.executor.intents.history(env.forecast["forecast_id"]) == before
    assert record(env) == case.original and env.broker.posts == []
