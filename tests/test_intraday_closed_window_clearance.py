"""Narrow historical CLOSED-window proof; original UNKNOWN never replays."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
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
def closed_case(env, monkeypatch, tmp_path):
    # Reproduce the actual closed-session refusal with a real isolated journal
    # and an injected broker. The historical source predates current retained
    # observations; do not substitute a retrospective synchronous observation.
    monkeypatch.setattr(execution, "create_initial_gate_clearance", lambda *a, **k: None)
    env.reference = instant("2026-10-20T19:30:00Z")
    env.state.now = instant("2026-10-20T20:03:39Z")
    env.forecast = forecast(env)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now

    monkeypatch.setattr(scout_journal, "datetime", Clock)
    value = env.executor.step(env.forecast)
    original = record(env)
    assert original["state"] == "UNKNOWN" and env.broker.posts == []
    assert env.broker.contexts == []
    assert value["evidence"]["error_type"] == "ScoutBrokerError"
    assert value["evidence"]["submission_failure"]["stage"] == "send"
    value = deepcopy(value)
    value["evidence"]["submission_failure"] = dict(stage="send", original=dict(
        chain_truncated=False, chain=[dict(type="ScoutBrokerError",
            message="Exception text withheld; inspect error type and stack locations",
            frames=deepcopy(clearance.CLOSED_WINDOW_FRAMES))]))
    # Tests use their local fixture bytes; production retains the exact frozen
    # original source map and rejects any other implementation/profile.
    source_files = {name: (REPOSITORY / name).read_bytes().decode("utf-8")
                    for name in clearance.CLOSED_WINDOW_SOURCE}
    supported = {name: hashlib.sha256(raw.encode()).hexdigest() for name, raw in source_files.items()}
    monkeypatch.setattr(clearance, "CLOSED_WINDOW_SOURCE", supported)
    manifest = json.dumps(dict(schema="scout-isolated-live-runtime-v1", files=supported,
                               source_root=str(REPOSITORY)))
    source = dict(manifest_text=manifest, manifest_sha256=hashlib.sha256(manifest.encode()).hexdigest(),
                  files=source_files)
    session = env.executor.session_id
    at = env.state.now + timedelta(seconds=1)
    event = dict(id=100, cycle_id=session, symbol="DOCU", recorded_at=at.isoformat(),
                 kind="execution_step", payload=value)
    stop = dict(id=101, cycle_id=session, symbol=None, recorded_at=(at + timedelta(seconds=2)).isoformat(),
                kind="manual_session_stopped", payload=dict(pending_order_identity_preserved=True,
                    stop_reason="manual_stop", worker_errors=[]))
    journal_id, events = clearance._audit(env.executor.books, env.executor.journal, original)
    evidence = dict(version=clearance.VERSION, original_record=deepcopy(original),
        original_record_sha256=canonical_sha256(original), journal_id=journal_id, journal_events=events,
        execution_event=event, execution_event_sha256=canonical_sha256(event), stop_event=stop,
        stop_event_sha256=canonical_sha256(stop), source=source,
        process_end=dict(session_id=session, pid=123,
            process_created_at_epoch=(at-timedelta(minutes=10)).timestamp(),
            verified_absent_at=(at+timedelta(seconds=3)).isoformat(),
            original_manifest_sha256=source["manifest_sha256"]),
        runtime_observation=dict(at=(at+timedelta(seconds=1)).isoformat(), session_id=session,
            process=dict(verified=True, pid=123, created_at_epoch=(at-timedelta(minutes=10)).timestamp(),
                module="ml.intraday.live_session", binding="exact_birth",
                entrypoint_kind="reviewed_isolated_live_launcher", launcher_path=str(REPOSITORY/"launch.py"),
                launcher_sha256="1"*64, manifest_path=str(REPOSITORY/"manifest.json"),
                manifest_sha256=source["manifest_sha256"], source_root=str(REPOSITORY),
                source_files_verified=len(supported), recipe_sha256="2"*64)))
    return SimpleNamespace(env=env, original=original, evidence=evidence,
        now=instant(original["request"]["valid_until"])+timedelta(seconds=1),
        path=tmp_path/"closed-proof.json")


def install(case):
    case.path.write_text(json.dumps(case.evidence), encoding="utf-8")
    return clearance.create_clearance(case.env.executor.books, case.env.executor.journal,
        case.original["request"]["request_id"], evidence_path=case.path, repository=REPOSITORY,
        review_reference="offline exact initial CLOSED-window source/process review", as_of=case.now)


def test_closed_window_proof_is_durable_without_rewriting_unknown_or_rearming(closed_case):
    c = closed_case
    before = c.env.executor.journal.pending_events()
    receipt = install(c)
    assert receipt["commitment"] == "no_order_transport_entered"
    assert receipt["historical_execution"] == "UNKNOWN"
    assert record(c.env) == c.original and c.env.executor.journal.pending_events() == before
    c.path.unlink()
    assert clearance.load_clearance(c.env.executor.books, c.env.executor.journal,
        c.original, as_of=c.now) == receipt
    with pytest.raises(JournalStateError):
        c.env.executor.journal.claim_send(c.original["request"]["request_id"])
    assert c.env.broker.posts == []


@pytest.mark.parametrize("damage", ["type", "frames", "transport_frame", "message", "truncated",
    "source", "missing_dependency", "borrowed_profile", "manifest", "session", "forecast", "token",
    "events", "stop_missing", "stop_error", "process_not_ended", "process_birth", "runtime_source",
    "runtime_unverified", "unexpired", "open_window", "failure_after_transition", "payload_before_claim"])
def test_unsupported_or_incomplete_closed_window_proof_fails_closed(closed_case, monkeypatch, damage):
    c = closed_case
    e, row = c.evidence, c.evidence["execution_event"]
    chain = row["payload"]["evidence"]["submission_failure"]["original"]
    error = chain["chain"][0]
    if damage == "type": error["type"] = "ValueError"
    elif damage == "frames": error["frames"][-1]["line"] = 142
    elif damage == "transport_frame": error["frames"].append(dict(file="schwab.py", function="submit_prepared_order", line=692))
    elif damage == "message": error["message"] = "Stock execution session is closed"
    elif damage == "truncated": chain["chain_truncated"] = True
    elif damage == "source": e["source"]["files"]["ml/intraday/scout_broker.py"] += "\n"
    elif damage == "missing_dependency": e["source"]["files"].pop("ml/calendars.py")
    elif damage == "borrowed_profile":
        monkeypatch.setattr(clearance, "SUPPORTED_SOURCE", clearance.CLOSED_WINDOW_SOURCE)
        monkeypatch.setattr(clearance, "CLOSED_WINDOW_SOURCE", dict(clearance.CLOSED_WINDOW_SOURCE,
            **{"ml/intraday/scout_broker.py": "0"*64}))
    elif damage == "manifest": e["source"]["manifest_text"] += " "
    elif damage == "session": row["cycle_id"] = "another-session"
    elif damage == "forecast": row["payload"]["forecast_id"] = "0"*64
    elif damage == "token": e["original_record"]["send_token"] = "0"*64
    elif damage == "events": e["journal_events"].pop()
    elif damage == "stop_missing": e.pop("stop_event")
    elif damage == "stop_error": e["stop_event"]["payload"]["worker_errors"] = ["error"]
    elif damage == "process_not_ended": e["process_end"]["verified_absent_at"] = row["recorded_at"]
    elif damage == "process_birth": e["process_end"]["process_created_at_epoch"] += 1
    elif damage == "runtime_source": e["runtime_observation"]["process"]["source_root"] = "other"
    elif damage == "runtime_unverified": e["runtime_observation"]["process"]["verified"] = False
    elif damage == "unexpired": c.now = instant(c.original["request"]["valid_until"])-timedelta(seconds=1)
    elif damage == "open_window":
        monkeypatch.setattr(clearance, "stock_execution_window", lambda _: SimpleNamespace(
            executable=True, time_in_force="PM", mode="AFTER_HOURS", reason="SCHWAB_PM_EXTENDED_SESSION_OPEN"))
    elif damage == "failure_after_transition": row["recorded_at"] = "2026-10-20T20:05:00+00:00"
    elif damage == "payload_before_claim": row["payload"]["recorded_at"] = "2026-10-20T20:03:38+00:00"
    e["execution_event_sha256"] = canonical_sha256(row)
    if "stop_event" in e: e["stop_event_sha256"] = canonical_sha256(e["stop_event"])
    with pytest.raises((ContractError, KeyError, ValueError)):
        install(c)
    assert record(c.env) == c.original and c.env.broker.posts == []
    assert not (c.env.executor.books.root/"pretransport-clearances").exists()


@pytest.mark.parametrize("cash", [0, 250])
def test_clearance_only_unblocks_distinct_future_forecasts_with_fresh_capacity(closed_case, cash):
    c = closed_case
    install(c)
    c.env.state.now = c.now
    same = c.env.executor.step(c.env.forecast)
    assert same["submitted_quantity"] == 0 and c.env.broker.posts == []
    newer = forecast(c.env, advance=True)
    c.env.state.now = c.now + timedelta(seconds=10)
    c.env.cash = cash
    before = c.env.captures
    result = c.env.executor.step(newer)
    assert c.env.captures >= before + 2
    assert result["disposition"] == ("submitted" if cash else "no_purchasing_capacity")
    assert len(c.env.broker.posts) == (1 if cash else 0)
    assert record(c.env, c.env.forecast) == c.original
    with pytest.raises(JournalStateError):
        c.env.executor.journal.claim_send(c.original["request"]["request_id"])
