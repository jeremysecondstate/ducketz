"""Durable Scout iteration handoff; this module never grants order authority.

A returned cycle is a completed *attempt*, not necessarily a complete forecast
population. Missing symbols retain their actual deferred/failure disposition.
Only the session worker can request rotation, after execution and thread drain.
"""
from dataclasses import asdict
import json

from .prediction import ContractError, digest, instant

SCHEMA = "scout-iteration-checkpoint-v1"


def _cycle_rows(store, session_id):
    with store.connect() as db:
        rows = db.execute("SELECT id,payload FROM cycles ORDER BY id").fetchall()
    return [(identity, json.loads(raw)) for identity, raw in rows
            if json.loads(raw).get("experiment") == session_id
            and json.loads(raw).get("pc") == "Scout"
            and json.loads(raw).get("mode") == "live"]


def inspect_cycle(store, cycle_id, *, session_id, symbols):
    """Prove every configured symbol has durable output or a recorded failure."""
    rows = dict(_cycle_rows(store, session_id))
    cycle = rows.get(cycle_id)
    if (cycle is None or cycle.get("symbols") != list(symbols)
            or cycle.get("status") not in {"completed", "partial_failure"}):
        raise ContractError("Iteration boundary lacks a returned full-scope cycle")
    forecasts, failures = {}, {}
    for symbol in symbols:
        forecast = store.forecast(cycle_id, symbol)
        if forecast is not None:
            if forecast["pc"] != "Scout" or forecast["cycle_id"] != cycle_id:
                raise ContractError("Iteration forecast identity differs")
            forecasts[symbol] = {"forecast_id": forecast["forecast_id"], "sha256": digest(forecast)}
            continue
        failure = cycle.get("failures", {}).get(symbol)
        if not isinstance(failure, dict) or not failure.get("type") or not failure.get("recorded_at"):
            raise ContractError("Iteration symbol has unfinished work without a durable disposition")
        instant(failure["recorded_at"])
        if failure.get("retryable"):
            if failure.get("retryable") is not True or not failure.get("reason"):
                raise ContractError("Iteration retry disposition is invalid")
            instant(failure["retry_at"])
        failures[symbol] = failure
    if cycle["status"] == "completed" and failures:
        raise ContractError("Incomplete iteration cannot be labeled completed")
    return {"cycle_id": cycle_id, "cycle_sha256": digest(cycle),
            "reference_at": instant(cycle["reference_at"]).isoformat(),
            "status": cycle["status"], "forecasts": forecasts, "failures": failures}


def _queue_state(store, session_id):
    with store.connect() as db:
        rows = db.execute("""SELECT forecast_id,symbol,issued_at,next_attempt_at,status
            FROM intraday_live_queue WHERE session_id=? ORDER BY forecast_id""", (session_id,)).fetchall()
        retry = db.execute("SELECT scope,next_attempt_at FROM intraday_live_retry ORDER BY scope").fetchall()
    keys = ("forecast_id", "symbol", "issued_at", "next_attempt_at", "status")
    values = [dict(zip(keys, row)) for row in rows]
    return {"queue": values, "broker_retry": [list(row) for row in retry]}


def capture_checkpoint(store, *, lifecycle, symbols, boundary_cycle_id):
    """Called only after owned threads exit. DB originals remain authoritative."""
    cycles = [inspect_cycle(store, identity, session_id=lifecycle.session_id, symbols=symbols)
              for identity, _ in _cycle_rows(store, lifecycle.session_id)]
    if boundary_cycle_id not in {row["cycle_id"] for row in cycles}:
        raise ContractError("Missing iteration boundary cycle")
    queue = _queue_state(store, lifecycle.session_id)
    return {"schema": SCHEMA, "session_id": lifecycle.session_id,
            "generation": lifecycle.generation, "release": lifecycle.release,
            "started_at": instant(lifecycle.started_at).isoformat(),
            "wake_at": instant(lifecycle.wake_at).isoformat(),
            "deadline": instant(lifecycle.deadline).isoformat(),
            "symbols": list(symbols), "boundary_cycle_id": boundary_cycle_id,
            "boundary_kind": "full_symbol_attempt_with_durable_deferrals",
            "cycles": cycles, "last_reference": max(row["reference_at"] for row in cycles),
            "queue_sha256": digest(queue),
            "pending_queue": [row for row in queue["queue"] if row["status"] == "pending"],
            "completed_queue": [row for row in queue["queue"] if row["status"] != "pending"],
            "broker_retry": queue["broker_retry"], "owned_workers_drained": True,
            "pending_order_identity_preserved": True, "recovery_first": True}


def validate_resume(store, *, lifecycle, symbols, config):
    """Reject incompatible handoffs before broker construction or any effects."""
    checkpoint = getattr(lifecycle, "previous_checkpoint", None)
    generation = lifecycle.generation
    if type(generation) is not int or generation < 1:
        raise ContractError("Invalid iteration generation")
    if generation == 1:
        if checkpoint is not None or _cycle_rows(store, lifecycle.session_id):
            raise ContractError("Initial activation cannot adopt another session's cycles")
        return
    if (not isinstance(checkpoint, dict) or checkpoint.get("schema") != SCHEMA
            or checkpoint.get("session_id") != lifecycle.session_id
            or checkpoint.get("generation") != generation - 1
            or checkpoint.get("symbols") != list(symbols)
            or checkpoint.get("owned_workers_drained") is not True):
        raise ContractError("Incompatible iteration checkpoint identity")
    for field in ("started_at", "wake_at", "deadline"):
        if instant(checkpoint[field]) != instant(getattr(lifecycle, field)):
            raise ContractError("Iteration checkpoint changed original session authority")
    actual = capture_checkpoint(store, lifecycle=lifecycle, symbols=symbols,
                                boundary_cycle_id=checkpoint["boundary_cycle_id"])
    for field in ("cycles", "last_reference", "queue_sha256", "pending_queue", "completed_queue", "broker_retry"):
        if checkpoint.get(field) != actual[field]:
            raise ContractError("Iteration checkpoint differs from durable " + field)
    # Config contributes to cycle identity. Retrying a partial old cycle under
    # a new config would manufacture a second identity for the same reference.
    from .runtime import _jsonable
    expected = digest(_jsonable(asdict(config)))
    for _, cycle in _cycle_rows(store, lifecycle.session_id):
        if (any(error.get("retryable") is True for error in cycle.get("failures", {}).values())
                and cycle.get("configuration_hash") != expected):
            raise ContractError("Pending iteration requires its saved model/configuration recipe")
