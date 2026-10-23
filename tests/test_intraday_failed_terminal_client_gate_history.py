"""Public fixtures for one separate drained FAILED-session historical contract."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pytest

from ml.intraday import pretransport_clearance as clearance
from ml.intraday.prediction import ContractError, instant
from ml.intraday.scout_journal import JournalStateError
from ml.intraday.scout_protocol import canonical_sha256
from test_intraday_client_final_gate_history import history
from test_intraday_scout_local_execution import env, forecast, record


@pytest.fixture
def failed_history(history, monkeypatch):
    h = history
    e = h.evidence
    h.production_failed = (clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_MANIFEST,
                          dict(clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE))
    source_files = e["source"]["files"]
    for name in clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE:
        if name not in source_files:
            source_files[name] = "# public synthetic terminal dependency: " + name + "\n"
            path = h.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source_files[name].encode())
    supported = {name: hashlib.sha256(raw.encode()).hexdigest() for name, raw in source_files.items()}
    manifest = json.loads(e["source"]["manifest_text"])
    manifest["files"] = supported
    raw = json.dumps(manifest)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    e["source"].update(manifest_text=raw, manifest_sha256=digest)
    monkeypatch.setattr(clearance, "FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE", supported)
    monkeypatch.setattr(clearance, "FAILED_TERMINAL_CLIENT_FINAL_GATE_MANIFEST", digest)
    e["version"] = clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_VERSION
    session = e["original_record"]["request"]["session_id"]
    fatal_at = h.now + timedelta(seconds=1)
    diagnostic = dict(schema_version="intraday-symbol-exception-v1", chain_truncated=False,
        exception_chain=[dict(type="Timeout", relation="raised", frames_truncated=False,
            frames=deepcopy(clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_FRAMES))])
    worker_error = dict(worker="market_context", error_type="Timeout",
        message="Inspect private collection status", diagnostics=deepcopy(diagnostic))
    control_diagnostic = dict(schema_version="intraday-symbol-exception-v1", chain_truncated=False,
        exception_chain=[dict(type="ContractError", relation="raised", frames_truncated=False,
            frames=deepcopy(clearance.FAILED_TERMINAL_EXECUTION_CONTROL_FRAMES))])
    def row(index, kind, payload, symbol=None):
        return dict(id=101 + index, cycle_id=session, symbol=symbol,
            recorded_at=(fatal_at + timedelta(seconds=index)).isoformat(), kind=kind, payload=payload)
    phases = [dict(phase=name, status=state, process_cpu_seconds=0.01, thread_cpu_seconds=0.01,
                  wall_seconds=0.01, **({"error_type": "ContractError"} if state == "error" else {}))
              for name, state in [("snapshot_validation", "success"), ("chronology_check", "success"),
                                   ("initial_capture", "error"), ("total_step", "error")]]
    terminal = [
        row(0, "market_context_worker_stopped", dict(error_type="Timeout", diagnostics=diagnostic)),
        row(1, "target_scoring_retry", dict(error_type="ContractError", forecast_id="a" * 64,
            message="History reference lies outside this feed's latched eligible day"), "DOCU"),
        row(2, "local_execution_profile", dict(context_caches=[], cpu_scope="current_thread",
            error_type="ContractError", forecast_id="b" * 64, manual_session_id=session,
            observer_errors=[], phases=phases, process_cpu_scope="all_process_threads",
            schema_version="intraday-local-execution-profile-v1", status="error", symbol="DOCU",
            target_close=(fatal_at + timedelta(minutes=15)).isoformat(),
            worker=dict(native_thread_id=10, thread_name="MainThread")), "DOCU"),
        row(3, "execution_retry", dict(diagnostics=control_diagnostic, error_type="ContractError",
            forecast_id="b" * 64, message="Current manual Scout session authority required",
            retry_after_seconds=3.0), "DOCU"),
        row(4, "forecast_worker_stopped", dict(error_type="InterruptedError",
            message="Manual session stopped; acquisition recovery retained")),
        row(5, "manual_session_stopped", dict(pending_order_identity_preserved=True,
            stop_reason="worker_failure", worker_errors=[deepcopy(worker_error)]))]
    e["terminal_events"] = terminal
    e["stop_event"] = terminal[-1]
    ended_at = fatal_at + timedelta(seconds=6)
    e["process_end"].update(verified_absent_at=ended_at.isoformat(), original_manifest_sha256=digest)
    e["runtime_observation"]["process"].update(manifest_sha256=digest, source_files_verified=len(supported))
    born = e["process_end"]["process_created_at_epoch"]
    started = instant(e["execution_event"]["recorded_at"]) - timedelta(minutes=1)
    h.status = dict(automatic_stop_at=(fatal_at + timedelta(hours=1)).isoformat(),
        broker_orders_automatically_cancelled=False, evidence_paths={},
        heartbeat_at=terminal[-1]["recorded_at"], manual_start=True, pc="Scout", pid=123,
        process_create_time=born, schema_version="manual-scout-15m-session-v1", scout_admission="broker-local",
        session_id=session, started_at=started.isoformat(), status="FAILED", stop_reason="worker_failure",
        wake_at=started.isoformat(), worker_errors=[deepcopy(worker_error)],
        worker_threads=[dict(native_thread_id=i + 1, role=role, thread_name="intraday-manual-" + suffix)
                        for i, (role, suffix) in enumerate([("forecast", "forecast"), ("scoring", "scoring"),
                                                           ("minute_data", "minute-data"), ("market_context", "market-context")])])
    e["terminal_checkpoint"] = dict(session_id=session, observed_at=ended_at.isoformat(), query_only=True,
        last_own_event_id=terminal[-1]["id"], terminal_event_count=len(terminal), terminal_events_sha256="")
    h.now = ended_at + timedelta(seconds=1)
    rehash(h)
    return h


def rehash(h):
    e = h.evidence
    e["execution_event_sha256"] = canonical_sha256(e["execution_event"])
    e["stop_event_sha256"] = canonical_sha256(e["stop_event"])
    e["terminal_events_sha256"] = canonical_sha256({"events": e["terminal_events"]})
    e["terminal_checkpoint"]["terminal_events_sha256"] = e["terminal_events_sha256"]
    e["terminal_status_text"] = json.dumps(h.status)
    e["terminal_status_sha256"] = hashlib.sha256(e["terminal_status_text"].encode()).hexdigest()


def install(h):
    h.path.write_text(json.dumps(h.evidence), encoding="utf-8")
    return clearance.create_clearance(h.env.executor.books, h.env.executor.journal,
        h.original["request"]["request_id"], evidence_path=h.path, repository=h.repository,
        review_reference="public synthetic drained FAILED-session profile review", as_of=h.now)


def load(h, original=None):
    return clearance.load_clearance(h.env.executor.books, h.env.executor.journal,
        h.original if original is None else original, as_of=h.now)


def test_new_profile_is_separate_and_pins_fourteen_original_sources():
    assert clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_VERSION == "scout-reviewed-failed-terminal-client-final-gate-history-v1"
    assert clearance.HISTORICAL_CLIENT_FINAL_GATE_VERSION == "scout-reviewed-client-final-gate-history-v1"
    assert clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_MANIFEST == clearance.HISTORICAL_CLIENT_FINAL_GATE_MANIFEST
    assert clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_LAUNCHER == clearance.HISTORICAL_CLIENT_FINAL_GATE_LAUNCHER
    assert len(clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE) == 14
    assert {name: clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE[name]
            for name in clearance.HISTORICAL_CLIENT_FINAL_GATE_SOURCE} == clearance.HISTORICAL_CLIENT_FINAL_GATE_SOURCE
    assert clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE["ml/intraday/market_collection.py"] == "c7d67d74dc7a060b92001d96a4e3a3fa30ff92ddfc601bc7e169beaab467f938"
    assert clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE["ml/intraday/runtime.py"] == "d1a5d4e246e18334b0a1025553a22e8864f6661d96f658eb1975cef6365342f2"
    assert len(clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_FRAMES) == 7
    assert len(clearance.FAILED_TERMINAL_EXECUTION_CONTROL_FRAMES) == 5


def test_production_source_pins_reject_public_synthetic_evidence(failed_history, monkeypatch):
    h = failed_history
    digest, supported = h.production_failed
    monkeypatch.setattr(clearance, "FAILED_TERMINAL_CLIENT_FINAL_GATE_MANIFEST", digest)
    monkeypatch.setattr(clearance, "FAILED_TERMINAL_CLIENT_FINAL_GATE_SOURCE", supported)
    with pytest.raises(ContractError, match="reviewed failed-terminal client final-gate manifest"):
        install(h)
    assert record(h.env) == h.original and h.env.broker.posts == []


def test_durable_receipt_preserves_failed_history_and_permanent_send_fence(failed_history):
    h = failed_history
    before = h.env.executor.journal.pending_events()
    raw_status = h.evidence["terminal_status_text"]
    receipt = install(h)
    assert install(h) == load(h) == receipt
    assert receipt["historical_execution"] == "UNKNOWN" and receipt["commitment"] == clearance.COMMITMENT
    assert json.loads(Path(receipt["path"]).read_text())["payload"]["version"] == clearance.FAILED_TERMINAL_CLIENT_FINAL_GATE_VERSION
    assert h.evidence["terminal_status_text"] == raw_status
    assert json.loads(raw_status)["status"] == "FAILED"
    assert h.evidence["stop_event"]["payload"]["stop_reason"] == "worker_failure"
    assert record(h.env) == h.original and h.env.executor.journal.pending_events() == before
    with pytest.raises(JournalStateError):
        h.env.executor.journal.claim_send(h.original["request"]["request_id"])
    assert h.env.broker.posts == []


def test_pure_validator_reads_no_files_journals_or_network(failed_history, monkeypatch):
    h = failed_history
    def forbidden(*a, **kw):
        raise AssertionError("Pure proof validator must not read files or journals")
    monkeypatch.setattr(clearance, "_read", forbidden)
    monkeypatch.setattr(clearance, "_audit", forbidden)
    clearance._validate_proof(h.evidence, h.original, journal_id=h.evidence["journal_id"],
        events=h.evidence["journal_events"], as_of=h.now)


DAMAGES = ["extra", "old_version", "unknown_version", "record_hash", "request", "journal", "chain",
    "stage", "sender", "native_code", "bool_code", "sql_message", "source", "missing_source", "manifest",
    "original_trace", "original_truncated", "original_second", "expired", "process_live", "birth",
    "runtime_pid", "runtime_birth", "runtime_manifest", "runtime_launcher", "runtime_recipe", "runtime_count",
    "runtime_verified", "runtime_time", "terminal_count", "terminal_reordered", "terminal_duplicate", "late_event",
    "terminal_session", "terminal_time", "fatal_type", "fatal_frame", "fatal_truncated", "fatal_second",
    "fatal_symbol", "forecast_type", "forecast_message", "stop_clean", "stop_extra_error", "stop_execution_error",
    "stop_identity", "stop_copy", "score_type", "score_message", "profile_send", "profile_forecast",
    "profile_phase", "profile_nan", "profile_observer", "refused_send", "refused_message", "refused_forecast",
    "refused_wait", "status_running", "status_clean", "status_extra_error", "status_pid", "status_birth",
    "status_session", "status_cancelled", "status_manual", "status_pc", "status_schema", "status_admission",
    "status_heartbeat", "status_started", "status_wake", "status_auto_stop", "threads_missing", "thread_repeat",
    "thread_identity", "checkpoint_last", "checkpoint_count", "checkpoint_session", "checkpoint_time",
    "checkpoint_not_readonly", "missing_process", "missing_status", "extra_failure"]


@pytest.mark.parametrize("damage", DAMAGES)
def test_invalid_failed_terminal_profile_fails_closed(failed_history, damage):
    h = failed_history
    e = h.evidence
    terminal = e["terminal_events"]
    fatal, scoring, prof, refused, drained, stop = terminal
    original_error = e["execution_event"]["payload"]["evidence"]["submission_failure"]
    sql = original_error["original"]["chain"][0]
    if damage == "extra": e["assumed_no_transport"] = True
    elif damage == "old_version": e["version"] = clearance.HISTORICAL_CLIENT_FINAL_GATE_VERSION
    elif damage == "unknown_version": e["version"] = "unknown"
    elif damage == "record_hash": e["original_record_sha256"] = "0" * 64
    elif damage == "request": e["execution_event"]["payload"]["evidence"]["request_id"] = "scout-local:other"
    elif damage == "journal": e["journal_id"] = "0" * 32
    elif damage == "chain": e["journal_events"].pop()
    elif damage == "stage": original_error["stage"] = "record_acceptance"
    elif damage == "sender": original_error["stage"] = "send"
    elif damage == "native_code": sql["sqlite_errorcode"] = 5
    elif damage == "bool_code": sql["sqlite_errorcode"] = True
    elif damage == "sql_message": sql["message"] = "unknown"
    elif damage == "source": e["source"]["files"]["ml/intraday/market_collection.py"] += "\n"
    elif damage == "missing_source": e["source"]["files"].pop("ml/intraday/runtime.py")
    elif damage == "manifest": e["source"]["manifest_text"] += " "
    elif damage == "original_trace": sql["frames"].append(dict(file="scout_broker.py", function="send", line=203))
    elif damage == "original_truncated": original_error["original"]["chain_truncated"] = True
    elif damage == "original_second": original_error["original"]["chain"].append(deepcopy(sql))
    elif damage == "expired": h.now = instant(h.original["request"]["valid_until"]) - timedelta(seconds=1)
    elif damage == "process_live": e["process_end"]["verified_absent_at"] = fatal["recorded_at"]
    elif damage == "birth": e["process_end"]["process_created_at_epoch"] = float("nan")
    elif damage.startswith("runtime_"):
        field, value = {"runtime_pid": ("pid", 124), "runtime_birth": ("created_at_epoch", 1),
            "runtime_manifest": ("manifest_sha256", "0" * 64), "runtime_launcher": ("launcher_sha256", "0" * 64),
            "runtime_recipe": ("recipe_sha256", "0" * 64), "runtime_count": ("source_files_verified", 15),
            "runtime_verified": ("verified", False), "runtime_time": ("at", h.now.isoformat())}[damage]
        (e["runtime_observation"] if field == "at" else e["runtime_observation"]["process"])[field] = value
    elif damage == "terminal_count": terminal.pop(1)
    elif damage == "terminal_reordered": terminal[1], terminal[2] = terminal[2], terminal[1]
    elif damage == "terminal_duplicate": terminal[1]["id"] = fatal["id"]
    elif damage == "late_event": terminal.append(deepcopy(refused))
    elif damage == "terminal_session": fatal["cycle_id"] = "other"
    elif damage == "terminal_time": fatal["recorded_at"] = e["execution_event"]["recorded_at"]
    elif damage == "fatal_type": fatal["payload"]["error_type"] = "RuntimeError"
    elif damage == "fatal_frame": fatal["payload"]["diagnostics"]["exception_chain"][0]["frames"][-1]["line"] += 1
    elif damage == "fatal_truncated": fatal["payload"]["diagnostics"]["chain_truncated"] = True
    elif damage == "fatal_second": fatal["payload"]["diagnostics"]["exception_chain"].append({})
    elif damage == "fatal_symbol": fatal["symbol"] = "DOCU"
    elif damage == "forecast_type": drained["payload"]["error_type"] = "RuntimeError"
    elif damage == "forecast_message": drained["payload"]["message"] = "unknown interruption"
    elif damage == "stop_clean": stop["payload"]["stop_reason"] = "manual_stop"
    elif damage == "stop_extra_error": stop["payload"]["worker_errors"].append({"worker": "execution"})
    elif damage == "stop_execution_error": stop["payload"]["worker_errors"][0]["worker"] = "execution"
    elif damage == "stop_identity": stop["payload"]["pending_order_identity_preserved"] = False
    elif damage == "stop_copy": e["stop_event"] = deepcopy(stop); e["stop_event"]["id"] += 1
    elif damage == "score_type": scoring["payload"]["error_type"] = "RuntimeError"
    elif damage == "score_message": scoring["payload"]["message"] = "unknown data processing"
    elif damage == "profile_send": prof["payload"]["phases"][-2]["phase"] = "submission"
    elif damage == "profile_forecast": prof["payload"]["forecast_id"] = "c" * 64
    elif damage == "profile_phase": prof["payload"]["phases"].pop()
    elif damage == "profile_nan": prof["payload"]["phases"][0]["wall_seconds"] = float("nan")
    elif damage == "profile_observer": prof["payload"]["observer_errors"] = ["unknown"]
    elif damage == "refused_send": refused["payload"]["diagnostics"]["exception_chain"][0]["frames"][-1]["function"] = "send"
    elif damage == "refused_message": refused["payload"]["message"] = "unknown execution failure"
    elif damage == "refused_forecast": refused["payload"]["forecast_id"] = "c" * 64
    elif damage == "refused_wait": refused["payload"]["retry_after_seconds"] = True
    elif damage.startswith("status_"):
        field, value = {"status_running": ("status", "RUNNING"), "status_clean": ("stop_reason", "manual_stop"),
            "status_extra_error": ("worker_errors", h.status["worker_errors"] + [{"worker": "execution"}]),
            "status_pid": ("pid", 124), "status_birth": ("process_create_time", 1),
            "status_session": ("session_id", "other"), "status_cancelled": ("broker_orders_automatically_cancelled", True),
            "status_manual": ("manual_start", False), "status_pc": ("pc", "Other"),
            "status_schema": ("schema_version", "unknown"), "status_admission": ("scout_admission", "unknown"),
            "status_heartbeat": ("heartbeat_at", h.now.isoformat()), "status_started": ("started_at", h.now.isoformat()),
            "status_wake": ("wake_at", h.now.isoformat()), "status_auto_stop": ("automatic_stop_at", fatal["recorded_at"])}[damage]
        h.status[field] = value
    elif damage == "threads_missing": h.status["worker_threads"].pop()
    elif damage == "thread_repeat": h.status["worker_threads"][-1] = deepcopy(h.status["worker_threads"][0])
    elif damage == "thread_identity": h.status["worker_threads"][0]["thread_name"] = "other"
    elif damage.startswith("checkpoint_"):
        field, value = {"checkpoint_last": ("last_own_event_id", stop["id"] + 1),
            "checkpoint_count": ("terminal_event_count", 7), "checkpoint_session": ("session_id", "other"),
            "checkpoint_time": ("observed_at", fatal["recorded_at"]), "checkpoint_not_readonly": ("query_only", False)}[damage]
        e["terminal_checkpoint"][field] = value
    elif damage == "missing_process": e.pop("process_end")
    elif damage == "missing_status": e.pop("terminal_status_text")
    elif damage == "extra_failure": original_error["returned_broker_order_id"] = "123"
    if damage not in {"profile_nan", "missing_status"}:
        rehash(h)
    with pytest.raises((ContractError, ValueError, KeyError, TypeError)):
        install(h)
    assert record(h.env) == h.original and h.env.broker.posts == []
    assert not (h.env.executor.books.root / "pretransport-clearances").exists()


@pytest.mark.parametrize("damage", ["proof", "receipt", "missing", "record", "future", "lifecycle", "version", "checkpoint"])
def test_load_reaudits_original_and_failed_terminal_receipt(failed_history, damage):
    h = failed_history
    receipt = install(h)
    path = Path(receipt["path"])
    proof, = path.parent.glob("*.proof.json")
    original = deepcopy(h.original)
    if damage == "proof": proof.write_text("{}")
    elif damage == "receipt":
        value = json.loads(path.read_text()); value["payload"]["commitment"] = "accepted"
        value["sha256"] = canonical_sha256(value["payload"]); path.write_text(json.dumps(value))
    elif damage == "missing": proof.unlink()
    elif damage == "record": original["send_token"] = "0" * 64
    elif damage == "future": h.now -= timedelta(seconds=1)
    elif damage == "lifecycle":
        import sqlite3
        with sqlite3.connect(h.env.executor.journal.path) as db:
            db.execute("insert into lifecycle_records values (?,?,?)", (original["request"]["request_id"], "{}", canonical_sha256({})))
    elif damage == "version":
        value = json.loads(path.read_text()); value["payload"]["version"] = clearance.HISTORICAL_CLIENT_FINAL_GATE_VERSION
        value["sha256"] = canonical_sha256(value["payload"]); path.write_text(json.dumps(value))
    elif damage == "checkpoint":
        data = json.loads(proof.read_text()); data["terminal_checkpoint"]["last_own_event_id"] += 1
        raw = json.dumps(data).encode(); new = path.parent / (hashlib.sha256(raw).hexdigest() + ".proof.json")
        new.write_bytes(raw); value = json.loads(path.read_text()); value["payload"]["evidence_sha256"] = hashlib.sha256(raw).hexdigest()
        value["sha256"] = canonical_sha256(value["payload"]); path.write_text(json.dumps(value))
    with pytest.raises((ContractError, ValueError)):
        load(h, original)
    assert h.env.broker.posts == []


@pytest.mark.parametrize("fail_at", [1, 2])
def test_persistence_failure_never_releases_original_block(failed_history, monkeypatch, fail_at):
    h = failed_history
    native = clearance._write_once
    calls = []
    def fail(path, raw):
        calls.append(str(path))
        if len(calls) == fail_at:
            raise OSError("synthetic private receipt storage failure")
        return native(path, raw)
    monkeypatch.setattr(clearance, "_write_once", fail)
    with pytest.raises(OSError):
        install(h)
    assert load(h) is None and record(h.env) == h.original
    newer = forecast(h.env, advance=True)
    result = h.env.executor.step(newer)
    assert result["disposition"] == "submission_recovery_required" and h.env.broker.posts == []
    with pytest.raises(JournalStateError):
        h.env.executor.journal.claim_send(h.original["request"]["request_id"])


@pytest.mark.parametrize("cash", [0, 250])
def test_future_direction_keeps_manual_stop_and_fresh_capacity_gates(failed_history, cash):
    h = failed_history
    install(h)
    e = h.env
    e.state.now = h.now
    assert e.executor.step(e.forecast)["submitted_quantity"] == 0 and e.broker.posts == []
    newer = forecast(e, advance=True)
    e.state.now, e.cash = h.now + timedelta(seconds=10), cash
    e.state.manual, e.state.stop = False, True
    with pytest.raises(ContractError, match="manual Scout session authority"):
        e.executor.step(newer)
    assert e.broker.posts == []
    # Fixture-only human permission for a distinct forecast; the repair itself
    # never starts a session or changes these controls.
    e.state.manual, e.state.stop = True, False
    captures = e.captures
    result = e.executor.step(newer)
    assert e.captures >= captures + 2
    assert result["disposition"] == ("submitted" if cash else "no_purchasing_capacity")
    assert len(e.broker.posts) == (1 if cash else 0) and record(e) == h.original
    with pytest.raises(JournalStateError):
        e.executor.journal.claim_send(h.original["request"]["request_id"])
