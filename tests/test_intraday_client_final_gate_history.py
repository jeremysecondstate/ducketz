"""Public synthetic historical proofs; no private identities or live receipts."""
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


@pytest.fixture
def history(env, monkeypatch, tmp_path):
    # Historical failures lack the prospective witness. Do not synthesize it.
    monkeypatch.setattr(execution, "create_client_gate_clearance", lambda *a, **kw: None)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(scout_journal, "datetime", Clock)
    saved_read, calls = execution._saved_forecast, []
    def fail_claimed_client_gate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = 261
            raise error
        return saved_read(*args, **kwargs)
    monkeypatch.setattr(execution, "_saved_forecast", fail_claimed_client_gate)
    value = env.executor.step(env.forecast)
    monkeypatch.setattr(execution, "_saved_forecast", saved_read)
    original = record(env)
    assert len(calls) == 3 and env.broker.posts == []
    assert original["state"] == "UNKNOWN" and original["broker_order_id"] is None
    assert value["evidence"]["submission_failure"]["stage"] == "final_gate"
    assert not (env.executor.books.root / "client-final-gate-clearances").exists()

    # Synthetic source bytes exercise the evidence contract without publishing
    # old local artifacts. Production constants are separately tested below.
    repository = tmp_path / "synthetic-original-repository"
    source_files = {name: "# public synthetic profile: " + name + "\n"
                    for name in clearance.HISTORICAL_CLIENT_FINAL_GATE_SOURCE}
    supported = {name: hashlib.sha256(raw.encode()).hexdigest() for name, raw in source_files.items()}
    for name, raw in source_files.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.encode())
    manifest_text = json.dumps(dict(schema="scout-isolated-live-runtime-v1", files=supported,
        source_root=str(repository), recipe_sha256="2" * 64))
    digest = hashlib.sha256(manifest_text.encode()).hexdigest()
    production = (clearance.HISTORICAL_CLIENT_FINAL_GATE_MANIFEST,
                  dict(clearance.HISTORICAL_CLIENT_FINAL_GATE_SOURCE))
    monkeypatch.setattr(clearance, "HISTORICAL_CLIENT_FINAL_GATE_SOURCE", supported)
    monkeypatch.setattr(clearance, "HISTORICAL_CLIENT_FINAL_GATE_MANIFEST", digest)
    source = dict(manifest_text=manifest_text, manifest_sha256=digest, files=source_files)
    # The diagnostic is an explicit synthetic reconstruction of the reviewed
    # raw historical frame contract, never a claim that current frames are old.
    value = deepcopy(value)
    value["evidence"]["submission_failure"] = dict(stage="final_gate", original=dict(
        chain_truncated=False, chain=[dict(type="OperationalError", message="database is locked",
            sqlite_errorcode=261, frames=deepcopy(clearance.HISTORICAL_CLIENT_FINAL_GATE_FRAMES))]))
    session = env.executor.session_id
    at = env.state.now + timedelta(seconds=1)
    event = dict(id=100, cycle_id=session, symbol=original["request"]["symbol"],
        recorded_at=at.isoformat(), kind="execution_step", payload=value)
    stop = dict(id=101, cycle_id=session, symbol=None, recorded_at=(at + timedelta(seconds=1)).isoformat(),
        kind="manual_session_stopped", payload=dict(pending_order_identity_preserved=True,
            stop_reason="manual_stop", worker_errors=[]))
    journal_id, events = clearance._audit(env.executor.books, env.executor.journal, original)
    born = (at - timedelta(minutes=10)).timestamp()
    evidence = dict(version=clearance.HISTORICAL_CLIENT_FINAL_GATE_VERSION,
        original_record=deepcopy(original), original_record_sha256=canonical_sha256(original),
        journal_id=journal_id, journal_events=events, execution_event=event,
        execution_event_sha256=canonical_sha256(event), stop_event=stop,
        stop_event_sha256=canonical_sha256(stop), source=source,
        process_end=dict(session_id=session, pid=123, process_created_at_epoch=born,
            verified_absent_at=(at + timedelta(seconds=2)).isoformat(), original_manifest_sha256=digest),
        runtime_observation=dict(at=(at + timedelta(microseconds=1)).isoformat(), session_id=session,
            process=dict(verified=True, pid=123, created_at_epoch=born, module="ml.intraday.live_session",
                binding="exact_birth", entrypoint_kind="reviewed_isolated_live_launcher",
                launcher_path=str(repository.parent / "launch.py"),
                launcher_sha256=clearance.HISTORICAL_CLIENT_FINAL_GATE_LAUNCHER,
                manifest_path=str(repository.parent / "manifest.json"), manifest_sha256=digest,
                source_root=str(repository), source_files_verified=len(supported), recipe_sha256="2" * 64)))
    return SimpleNamespace(env=env, original=original, evidence=evidence, repository=repository,
        path=tmp_path / "synthetic-history.json", production=production,
        now=instant(original["request"]["valid_until"]) + timedelta(seconds=1))


def install(history):
    history.path.write_text(json.dumps(history.evidence), encoding="utf-8")
    return clearance.create_clearance(history.env.executor.books, history.env.executor.journal,
        history.original["request"]["request_id"], evidence_path=history.path,
        repository=history.repository, review_reference="public synthetic stopped-process profile review",
        as_of=history.now)


def load(history, *, original=None):
    return clearance.load_clearance(history.env.executor.books, history.env.executor.journal,
        history.original if original is None else original, as_of=history.now)


def rehash(history):
    for name in ("execution_event", "stop_event"):
        history.evidence[name + "_sha256"] = canonical_sha256(history.evidence[name])


def test_production_profile_is_distinct_and_pins_exact_historical_manifest():
    assert clearance.VERSION == "scout-reviewed-first-gate-clearance-v1"
    assert clearance.HISTORICAL_CLIENT_FINAL_GATE_VERSION == "scout-reviewed-client-final-gate-history-v1"
    assert clearance.HISTORICAL_CLIENT_FINAL_GATE_MANIFEST == "88af48af7ce1b3b2bc1e50951ba627bebd046e0801f672a97fcf2b1250665bf7"
    assert clearance.HISTORICAL_CLIENT_FINAL_GATE_LAUNCHER == "1d544928f99cacef1af6cb3adc194121e73122b9a646b633b861d0f072db5b47"
    assert len(clearance.HISTORICAL_CLIENT_FINAL_GATE_SOURCE) == 12
    assert clearance.HISTORICAL_CLIENT_FINAL_GATE_FRAMES == [
        dict(file="scout_client.py", function="submit", line=184),
        dict(file="scout_client.py", function="_gate", line=155),
        dict(file="scout_local_execution.py", function="local_authorization", line=565),
        dict(file="local_forecast.py", function="_saved_forecast", line=182),
        dict(file="local_forecast.py", function="_saved_forecast_once", line=139)]


def test_real_production_profile_rejects_synthetic_source(history, monkeypatch):
    digest, supported = history.production
    monkeypatch.setattr(clearance, "HISTORICAL_CLIENT_FINAL_GATE_MANIFEST", digest)
    monkeypatch.setattr(clearance, "HISTORICAL_CLIENT_FINAL_GATE_SOURCE", supported)
    with pytest.raises(ContractError, match="reviewed historical client final-gate manifest"):
        install(history)
    assert record(history.env) == history.original and history.env.broker.posts == []


def test_new_history_receipt_is_durable_idempotent_without_history_rewrite_or_replay(history):
    journal = history.env.executor.journal
    before = journal.pending_events()
    receipt = install(history)
    assert receipt["historical_execution"] == "UNKNOWN" and receipt["commitment"] == clearance.COMMITMENT
    path = Path(receipt["path"])
    assert json.loads(path.read_text())["payload"]["version"] == clearance.HISTORICAL_CLIENT_FINAL_GATE_VERSION
    raw = path.read_bytes()
    assert install(history) == load(history) == receipt and path.read_bytes() == raw
    assert record(history.env) == history.original and journal.pending_events() == before
    with pytest.raises(JournalStateError):
        journal.claim_send(history.original["request"]["request_id"])
    assert history.env.broker.posts == []


@pytest.mark.parametrize("damage", ["extra", "legacy_version", "unknown_version", "record", "account",
    "request", "journal", "events", "event_hash", "symbol", "forecast", "session", "kind", "stage",
    "sender_stage", "no_trace", "truncated", "second_exception", "extra_error", "after_sender", "reordered",
    "line", "code", "boolean_code", "message", "type", "source", "source_missing", "manifest", "manifest_digest",
    "process_session", "process_live", "process_manifest", "birth", "stop_session", "stop_reason", "stop_error",
    "stop_before_error", "pending_identity", "unexpired", "runtime_pid", "runtime_birth", "runtime_source",
    "runtime_verified", "runtime_time", "runtime_count", "runtime_launcher", "runtime_recipe", "missing_stop",
    "missing_process_end", "extra_failure_field"])
def test_invalid_or_live_original_history_fails_closed(history, damage):
    e = history.evidence
    row = e["execution_event"]
    failure = row["payload"]["evidence"]["submission_failure"]
    error = failure["original"]["chain"][0]
    if damage == "extra": e["no_sender"] = True
    elif damage == "legacy_version": e["version"] = clearance.VERSION
    elif damage == "unknown_version": e["version"] = "unknown-history-version"
    elif damage == "record": e["original_record_sha256"] = "0" * 64
    elif damage == "account": e["original_record"]["request"]["account_fingerprint"] = "0" * 64
    elif damage == "request": row["payload"]["evidence"]["request_id"] = "scout-local:other"
    elif damage == "journal": e["journal_id"] = "0" * 32
    elif damage == "events": e["journal_events"].pop()
    elif damage == "event_hash": e["execution_event_sha256"] = "0" * 64
    elif damage == "symbol": row["symbol"] = "OTHER"
    elif damage == "forecast": row["payload"]["forecast_id"] = "0" * 64
    elif damage == "session": row["cycle_id"] = "other-session"
    elif damage == "kind": row["kind"] = "execution_retry"
    elif damage == "stage": failure["stage"] = "record_acceptance"
    elif damage == "sender_stage": failure["stage"] = "send"
    elif damage == "no_trace": error.pop("frames")
    elif damage == "truncated": failure["original"]["chain_truncated"] = True
    elif damage == "second_exception": failure["original"]["chain"].append(deepcopy(error))
    elif damage == "extra_error": error["sqlite_errorname"] = "SQLITE_BUSY_RECOVERY"
    elif damage == "after_sender": error["frames"].append(dict(file="scout_broker.py", function="send", line=203))
    elif damage == "reordered": error["frames"].reverse()
    elif damage == "line": error["frames"][3]["line"] += 1
    elif damage == "code": error["sqlite_errorcode"] = 5
    elif damage == "boolean_code": error["sqlite_errorcode"] = True
    elif damage == "message": error["message"] = "unknown OperationalError"
    elif damage == "type": row["payload"]["evidence"]["error_type"] = "ContractError"
    elif damage == "source": e["source"]["files"]["ml/intraday/scout_broker.py"] += "\n"
    elif damage == "source_missing": e["source"]["files"].pop("ml/intraday/pretransport_clearance.py")
    elif damage == "manifest": e["source"]["manifest_text"] += " "
    elif damage == "manifest_digest": e["source"]["manifest_sha256"] = "0" * 64
    elif damage == "process_session": e["process_end"]["session_id"] = "other-session"
    elif damage == "process_live": e["process_end"]["verified_absent_at"] = row["recorded_at"]
    elif damage == "process_manifest": e["process_end"]["original_manifest_sha256"] = "0" * 64
    elif damage == "birth": e["process_end"]["process_created_at_epoch"] = float("nan")
    elif damage == "stop_session": e["stop_event"]["cycle_id"] = "other-session"
    elif damage == "stop_reason": e["stop_event"]["payload"]["stop_reason"] = "worker_failure"
    elif damage == "stop_error": e["stop_event"]["payload"]["worker_errors"] = ["unresolved"]
    elif damage == "stop_before_error": e["stop_event"]["recorded_at"] = (instant(row["recorded_at"]) - timedelta(seconds=1)).isoformat()
    elif damage == "pending_identity": e["stop_event"]["payload"]["pending_order_identity_preserved"] = False
    elif damage == "unexpired": history.now = instant(history.original["request"]["valid_until"]) - timedelta(seconds=1)
    elif damage == "runtime_pid": e["runtime_observation"]["process"]["pid"] += 1
    elif damage == "runtime_birth": e["runtime_observation"]["process"]["created_at_epoch"] += 1
    elif damage == "runtime_source": e["runtime_observation"]["process"]["source_root"] = "other"
    elif damage == "runtime_verified": e["runtime_observation"]["process"]["verified"] = False
    elif damage == "runtime_time": e["runtime_observation"]["at"] = (history.now + timedelta(seconds=1)).isoformat()
    elif damage == "runtime_count": e["runtime_observation"]["process"]["source_files_verified"] += 1
    elif damage == "runtime_launcher": e["runtime_observation"]["process"]["launcher_sha256"] = "0" * 64
    elif damage == "runtime_recipe": e["runtime_observation"]["process"]["recipe_sha256"] = "0" * 64
    elif damage == "missing_stop": e.pop("stop_event")
    elif damage == "missing_process_end": e.pop("process_end")
    elif damage == "extra_failure_field": failure["returned_broker_order_id"] = "123"
    if damage != "event_hash" and "stop_event" in e: rehash(history)
    with pytest.raises((ContractError, ValueError, KeyError)):
        install(history)
    assert record(history.env) == history.original and history.env.broker.posts == []
    assert not (history.env.executor.books.root / "pretransport-clearances").exists()


@pytest.mark.parametrize("damage", ["proof", "receipt", "missing", "record", "future", "hardlink", "lifecycle", "version"])
def test_load_rechecks_full_original_and_saved_new_version_proof(history, damage, tmp_path):
    receipt = install(history)
    path = Path(receipt["path"])
    proof, = path.parent.glob("*.proof.json")
    original = deepcopy(history.original)
    if damage == "proof": proof.write_text("{}")
    elif damage == "receipt":
        data = json.loads(path.read_text()); data["payload"]["commitment"] = "accepted"
        data["sha256"] = canonical_sha256(data["payload"]); path.write_text(json.dumps(data))
    elif damage == "missing": proof.unlink()
    elif damage == "record": original["send_token"] = "0" * 64
    elif damage == "future": history.now -= timedelta(seconds=1)
    elif damage == "hardlink":
        import os
        os.link(proof, tmp_path / "proof-alias.json")
    elif damage == "lifecycle":
        with sqlite3.connect(history.env.executor.journal.path) as db:
            db.execute("insert into lifecycle_records values (?,?,?)", (original["request"]["request_id"], "{}", canonical_sha256({})))
    elif damage == "version":
        data = json.loads(path.read_text()); data["payload"]["version"] = clearance.VERSION
        data["sha256"] = canonical_sha256(data["payload"]); path.write_text(json.dumps(data))
    with pytest.raises((ContractError, ValueError)):
        load(history, original=original)


@pytest.mark.parametrize("cash", [0, 250])
def test_distinct_forecast_still_uses_fresh_capacity_and_never_rearms_history(history, cash):
    install(history)
    e = history.env
    e.state.now = history.now
    assert e.executor.step(e.forecast)["submitted_quantity"] == 0 and e.broker.posts == []
    newer = forecast(e, advance=True)
    e.state.now, e.cash = history.now + timedelta(seconds=10), cash
    captures = e.captures
    result = e.executor.step(newer)
    assert e.captures >= captures + 2
    assert result["disposition"] == ("submitted" if cash else "no_purchasing_capacity")
    assert len(e.broker.posts) == (1 if cash else 0) and record(e) == history.original
    with pytest.raises(JournalStateError):
        e.executor.journal.claim_send(history.original["request"]["request_id"])
