"""Independent trading handoff across a verified full model iteration.

Model work may append forecasts and retire stale queue entries during this gap.
Trading owns its journal and effects; a handoff cannot rewrite their identity.
This module reads existing stores and never constructs a broker.
"""
from pathlib import Path
import hashlib
import json
import time

from .prediction import ContractError, digest, instant

VERSION = "scout-trading-rotation-v1"
SCHEMA = "scout-independent-trading-checkpoint-v1"


def enabled(lifecycle):
    return getattr(lifecycle, "request", {}).get("trading_rotation_version") == VERSION


def validate_request(request, *, lifecycle):
    if not isinstance(request, dict) or request.get("schema") != VERSION:
        raise ContractError("Unsupported trading rotation request")
    for name in ("session_id", "generation", "token", "release"):
        if request.get(name) != lifecycle.header().get(name):
            raise ContractError("Trading rotation request changed generation identity")
    if (request.get("role") != "trading"
            or type(request.get("iteration_sequence")) is not int or request["iteration_sequence"] < 1
            or type(request.get("model_generation")) is not int or request["model_generation"] < 1
            or not isinstance(request.get("boundary_cycle_id"), str) or not request["boundary_cycle_id"]
            or request.get("boundary_id") != "model-" + str(request["model_generation"]) + ":" + request["boundary_cycle_id"]
            or not isinstance(request.get("checkpoint_sha256"), str)
            or len(request["checkpoint_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in request["checkpoint_sha256"])):
        raise ContractError("Trading rotation request lacks a complete model boundary")
    return request


def _queue_witnesses(store, session_id):
    # One read transaction prevents mixed before/after retirement evidence.
    with store.connect() as db:
        db.execute("BEGIN")
        rows = db.execute("""SELECT q.forecast_id,q.symbol,q.issued_at,q.next_attempt_at,q.status,
            f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,f.payload,c.payload
            FROM intraday_live_queue q LEFT JOIN forecasts f ON f.id=q.forecast_id
            LEFT JOIN cycles c ON c.id=f.cycle_id
            WHERE q.session_id=? ORDER BY q.forecast_id""", (session_id,)).fetchall()
        retry = db.execute("SELECT next_attempt_at FROM intraday_live_retry WHERE scope='broker'").fetchone()
    values = []
    for row in rows:
        if row[5] is None or row[12] is None:
            raise ContractError("Queued trading identity lost its original forecast/cycle")
        forecast = store._forecast_value(row[5:12])
        cycle = json.loads(row[12])
        if (forecast["pc"] != "Scout" or cycle.get("pc") != "Scout"
                or cycle.get("experiment") != session_id
                or forecast["symbol"] != row[1]
                or instant(forecast["issued_at"]) != instant(row[2])):
            raise ContractError("Queued forecast belongs to another trading identity")
        values.append(dict(forecast_id=row[0], symbol=row[1], issued_at=row[2],
            next_attempt_at=row[3], status=row[4], forecast_sha256=digest(forecast)))
    return values, retry[0] if retry else None


def _journal_witness(datastore, session_id):
    from .scout_session_books import ScoutBooks
    from .scout_protocol import canonical_sha256
    root = Path(datastore) / "intraday/scout"
    envelope = json.loads((root / "inventory.json").read_bytes())
    books = ScoutBooks(root, account_fingerprint=envelope["inventory"]["account_fingerprint"])
    inventory = books.inspect()
    try:
        item = inventory["sessions"][session_id]
    except KeyError as exc:
        raise ContractError("Trading handoff lost its original journal") from exc
    path = root / "journals" / (canonical_sha256({"session_id": session_id}) + ".sqlite3")
    if item["filename"] != path.name:
        raise ContractError("Trading journal filename changed")
    return dict(path=str(path.resolve()), inventory_item=item,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def capture_checkpoint(store, *, lifecycle, symbols, rotation_request, datastore):
    validate_request(rotation_request, lifecycle=lifecycle)
    values, retry = _queue_witnesses(store, lifecycle.session_id)
    return {**lifecycle.header(), "schema": SCHEMA,
        "started_at": instant(lifecycle.started_at).isoformat(),
        "wake_at": instant(lifecycle.wake_at).isoformat(),
        "deadline": instant(lifecycle.deadline).isoformat(), "symbols": list(symbols),
        "rotation_request": rotation_request, "boundary_cycle_id": rotation_request["boundary_cycle_id"],
        "iteration_sequence": rotation_request["iteration_sequence"],
        "rotation_request_sha256": digest(rotation_request),
        "boundary_kind": "model_full_symbol_attempt", "owned_workers_drained": True,
        "pending_order_identity_preserved": True, "queue_witnesses": values,
        "broker_retry_floor": retry, "journal_witness": _journal_witness(datastore, lifecycle.session_id)}


def capture_with_retry(store, *, lifecycle, symbols, rotation_request, datastore,
                       sleep=time.sleep, monotonic=time.monotonic):
    """A drained worker may retry transient evidence reads, never broker effects."""
    from .live_session import _execution_store_busy
    until = monotonic() + lifecycle.drain_timeout_seconds
    while True:
        if lifecycle.stop_reason() is not None:
            raise InterruptedError("Trading authority ended during handoff capture")
        try:
            return capture_checkpoint(store, lifecycle=lifecycle, symbols=symbols,
                rotation_request=rotation_request, datastore=datastore)
        except Exception as exc:
            if not _execution_store_busy(exc) or monotonic() >= until:
                raise
            sleep(min(.2, max(0, until-monotonic())))


def validate_resume(store, *, lifecycle, symbols, datastore):
    """Called before broker construction; never substitute or replay saved work."""
    checkpoint = getattr(lifecycle, "previous_checkpoint", None)
    generation = lifecycle.generation
    if type(generation) is not int or generation < 1:
        raise ContractError("Invalid trading generation")
    if generation == 1:
        if checkpoint is not None:
            raise ContractError("Initial trading activation cannot adopt a checkpoint")
        return
    if (not isinstance(checkpoint, dict) or checkpoint.get("schema") != SCHEMA
            or checkpoint.get("session_id") != lifecycle.session_id
            or checkpoint.get("role") != "trading"
            or checkpoint.get("generation") != generation - 1
            or checkpoint.get("symbols") != list(symbols)
            or checkpoint.get("owned_workers_drained") is not True
            or checkpoint.get("pending_order_identity_preserved") is not True
            or checkpoint.get("boundary_kind") != "model_full_symbol_attempt"):
        raise ContractError("Incompatible trading checkpoint identity")
    for name in ("started_at", "wake_at", "deadline"):
        if instant(checkpoint[name]) != instant(getattr(lifecycle, name)):
            raise ContractError("Trading checkpoint changed original session authority")
    if checkpoint["release"]["recipe_file_sha256"] != lifecycle.release["recipe_file_sha256"]:
        raise ContractError("Trading checkpoint changed the private recipe")
    request = checkpoint.get("rotation_request")
    if (not isinstance(request, dict) or request.get("schema") != VERSION
            or digest(request) != checkpoint.get("rotation_request_sha256")
            or any(request.get(k) != checkpoint.get(k) for k in ("session_id", "role", "generation", "token", "release"))
            or request.get("boundary_cycle_id") != checkpoint.get("boundary_cycle_id")
            or request.get("iteration_sequence") != checkpoint.get("iteration_sequence")):
        raise ContractError("Trading checkpoint lost its model boundary request")
    actual, retry = _queue_witnesses(store, lifecycle.session_id)
    current = {row["forecast_id"]: row for row in actual}
    prior = checkpoint.get("queue_witnesses")
    if not isinstance(prior, list) or len({r["forecast_id"] for r in prior}) != len(prior):
        raise ContractError("Trading checkpoint queue witness is invalid")
    for old in prior:
        row = current.get(old["forecast_id"])
        if row is None or any(row[k] != old[k] for k in ("forecast_id", "symbol", "issued_at", "forecast_sha256")):
            raise ContractError("Trading handoff changed an original forecast")
        if (instant(row["next_attempt_at"]) < instant(old["next_attempt_at"])
                or row["status"] != old["status"] and not (
                    old["status"] == "pending" and row["status"] in {"expired", "superseded"})):
            raise ContractError("Trading handoff regressed a queue disposition or retry floor")
    floor = checkpoint.get("broker_retry_floor")
    if floor is not None and (retry is None or instant(retry) < instant(floor)):
        raise ContractError("Trading handoff regressed the broker retry floor")
    if _journal_witness(datastore, lifecycle.session_id) != checkpoint.get("journal_witness"):
        raise ContractError("Trading handoff changed its original closed journal")
