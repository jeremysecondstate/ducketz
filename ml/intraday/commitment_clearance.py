"""Reviewed absence of a historical DAY commitment, without resolving its send.

This narrow repair accepts complete same-account saved broker evidence only when
no order for the original symbol appears anywhere in history and there are no
current working orders. It never identifies fills, changes the original UNKNOWN
journal record, releases ledger reservations, or makes that request sendable.
The caller still obtains fresh broker capacity for every new forecast.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

from ml.stock_trader.session import stock_execution_window
from .prediction import ContractError, instant
from .scout_journal import _safe_path
from .scout_protocol import canonical_sha256

VERSION = "scout-historical-day-commitment-clearance-v1"
CONTRACT_VERSION = "reviewed-scout-normal-day-sender-v1"
SOURCE_PATHS = ("ml/intraday/scout_broker.py", "ml/stock_trader/session.py",
                "app/services/schwab_stock_orders.py", "app/services/schwab_order_fields.py")
EASTERN = ZoneInfo("America/New_York")
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_SECONDS = 60
TERMINAL = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})


def _require(condition, message):
    if not condition:
        raise ContractError("Historical commitment clearance: " + message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def reviewed_day_sender_contract(repository, *, review_reference):
    """Record source bytes explicitly reviewed by the incident-repair caller."""
    _require(type(review_reference) is str and bool(review_reference.strip()), "explicit source review required")
    return dict(version=CONTRACT_VERSION, reviewed_by="Scout", review_reference=review_reference,
        source_hash_meaning="current_reviewed_source_not_original_loaded_attestation",
        order_type="LIMIT", session="NORMAL", duration="DAY",
        source_sha256={name: _sha((Path(repository) / name).read_bytes()) for name in SOURCE_PATHS})


def _validate_contract(contract, record):
    _require(type(contract) is dict and set(contract) == {
        "version", "reviewed_by", "review_reference", "source_hash_meaning", "order_type", "session", "duration", "source_sha256"},
        "exact reviewed sender contract required")
    _require(contract["version"] == CONTRACT_VERSION and contract["reviewed_by"] == "Scout"
        and type(contract["review_reference"]) is str and bool(contract["review_reference"].strip())
        and contract["source_hash_meaning"] == "current_reviewed_source_not_original_loaded_attestation"
        and (contract["order_type"], contract["session"], contract["duration"]) == ("LIMIT", "NORMAL", "DAY"),
        "only a reviewed NORMAL DAY limit sender is supported")
    hashes = contract["source_sha256"]
    _require(type(hashes) is dict and set(hashes) == set(SOURCE_PATHS)
        and all(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes.values()),
        "complete reviewed source hashes required")
    request, grant = record["request"], record["grant"]
    first, last = instant(request["issued_at"]), instant(grant["expires_at"])
    _require(first < last and first.astimezone(EASTERN).date() == last.astimezone(EASTERN).date(),
        "original send window must stay inside one order date")
    # Request expiry is a send gate, not the broker order's expiration.
    for at in (first, last - timedelta(microseconds=1)):
        window = stock_execution_window(at)
        _require(window.executable and window.time_in_force == "DAY" and window.mode == "CORE",
            "original send window is not entirely NORMAL DAY")


def _validate_orders(rows, *, symbol, lower, upper, seen):
    _require(type(rows) is list, "history and child orders must be complete lists")
    for row in rows:
        _require(type(row) is dict, "malformed history row")
        order_id = row.get("orderId")
        _require(type(order_id) in (str, int) and not isinstance(order_id, bool)
            and re.fullmatch(r"[1-9][0-9]{0,63}", str(order_id)), "invalid history order identity")
        _require(str(order_id) not in seen, "duplicate history order identity")
        seen.add(str(order_id))
        _require(row.get("status") in TERMINAL, "nonterminal, replaced, or unknown history status")
        entered = instant(row.get("enteredTime"))
        _require(lower <= entered <= upper, "history row outside the complete query interval")
        legs = row.get("orderLegCollection")
        _require(type(legs) is list and bool(legs), "history row lacks complete instrument legs")
        for leg in legs:
            instrument = leg.get("instrument") if type(leg) is dict else None
            _require(type(instrument) is dict and type(instrument.get("symbol")) is str
                and bool(instrument["symbol"].strip()), "history instrument identity unavailable")
            _require(instrument["symbol"].strip().upper() != symbol, "original-symbol history requires exact identity investigation")
        _validate_orders(row.get("childOrderStrategies", []), symbol=symbol, lower=lower, upper=upper, seen=seen)


def _validate_evidence(evidence, record, *, captured_at):
    _require(type(evidence) is dict, "saved broker evidence must be an object")
    request = record["request"]
    _require(evidence.get("account_fingerprint") == request["account_fingerprint"], "broker account differs")
    _require(evidence.get("original_request_id") == request["request_id"]
        and evidence.get("original_record_sha256") == canonical_sha256(record), "broker read binds another original attempt")
    _require(type(evidence.get("snapshot_identity")) is str and bool(evidence["snapshot_identity"]),
        "verified snapshot identity required")
    observed = instant(evidence.get("observed_at"))
    lower, upper = instant(evidence.get("from_entered_time")), instant(evidence.get("to_entered_time"))
    _require(0 <= (observed - upper).total_seconds() <= MAX_CAPTURE_SECONDS,
        "history cutoff was stale or future at capture")
    _require(observed <= captured_at, "broker evidence lies after receipt creation")
    day = instant(request["issued_at"]).astimezone(EASTERN).date()
    day_start = datetime.combine(day, time.min, EASTERN)
    conservative_expiry = datetime.combine(day, time(20), EASTERN)
    _require(lower <= day_start and upper >= conservative_expiry + timedelta(days=1),
        "complete original date and a full day beyond DAY expiry are required")
    limit, rows = evidence.get("max_results"), evidence.get("orders")
    _require(type(limit) is int and 1 <= limit <= 3000 and type(rows) is list and len(rows) < limit,
        "history is partial or potentially truncated")
    _require(evidence.get("working_orders") == [], "current working orders are not proven empty")
    _validate_orders(rows, symbol=request["symbol"], lower=lower, upper=upper, seen=set())


def _receipt_path(books, record):
    return _safe_path(Path(books.root) / "commitment-clearances" /
                      (canonical_sha256({"request_id": record["request"]["request_id"]}) + ".json"))


def _bound(books, journal, record):
    _require(record is not None and record.get("state") == "UNKNOWN" and record.get("broker_order_id") is None,
        "only an exact unresolved UNKNOWN attempt can have commitment clearance")
    _require(record["request"]["request_id"].startswith("scout-local:")
        and record["request"]["account_fingerprint"] == books.account_fingerprint
        and journal.binding["account_fingerprint"] == books.account_fingerprint
        and journal.binding["session_id"] == record["request"]["session_id"], "journal/account/request binding differs")
    _require(journal.get(record["request"]["request_id"]) == record, "original attempt changed")


def load_clearance(books, journal, record, *, as_of):
    """Read a durable clearance; missing is unresolved, malformed fails closed."""
    _bound(books, journal, record)
    path = _receipt_path(books, record)
    if not path.exists():
        return None
    try:
        _require(path.stat().st_size <= 1024 * 1024, "receipt exceeds its size bound")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        _require(type(envelope) is dict and set(envelope) == {"payload", "sha256"}, "malformed receipt envelope")
        payload = envelope["payload"]
        _require(canonical_sha256(payload) == envelope["sha256"], "receipt digest differs")
        _require(set(payload) == {"version", "journal_id", "original_record", "original_record_sha256",
            "created_at", "sender_contract", "broker_evidence_sha256", "historical_execution", "commitment"},
            "receipt fields differ")
        _require(payload["version"] == VERSION and payload["journal_id"] == journal.journal_id
            and payload["original_record"] == record
            and payload["original_record_sha256"] == canonical_sha256(record)
            and payload["historical_execution"] == "UNKNOWN"
            and payload["commitment"] == "no_surviving_day_commitment", "receipt belongs to another original attempt")
        created = instant(payload["created_at"])
        _require(created <= instant(as_of), "receipt is from the future")
        digest = payload["broker_evidence_sha256"]
        _require(type(digest) is str and re.fullmatch(r"[a-f0-9]{64}", digest), "invalid broker evidence digest")
        evidence_path = _safe_path(path.parent / (digest + ".broker.json"))
        _require(evidence_path.stat().st_size <= MAX_EVIDENCE_BYTES, "broker evidence exceeds size bound")
        raw = evidence_path.read_bytes()
        _require(_sha(raw) == digest, "broker evidence bytes changed")
        _validate_contract(payload["sender_contract"], record)
        _validate_evidence(json.loads(raw), record, captured_at=created)
        return dict(path=str(path), sha256=envelope["sha256"],
            created_at=payload["created_at"], historical_execution="UNKNOWN",
            commitment="no_surviving_day_commitment")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("Historical commitment clearance: unreadable or invalid saved evidence") from exc


def _write_once(path, raw):
    _safe_path(path)
    if path.exists():
        _require(path.read_bytes() == raw, "immutable evidence already exists with different bytes")
        return
    temporary = _safe_path(path.with_name(".clearance-" + uuid.uuid4().hex + ".tmp"))
    with temporary.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def create_clearance(books, journal, request_id, *, evidence_path, sender_contract, repository, as_of):
    """Install explicitly reviewed saved evidence; no broker or order operations.

    The caller authenticates the broker read and reviews source/history before
    invoking this repair. Hashes preserve bytes; they do not authenticate a broker.
    """
    now = instant(as_of)
    record = journal.get(request_id)
    _bound(books, journal, record)
    _validate_contract(sender_contract, record)
    for name in SOURCE_PATHS:
        _require(_sha((Path(repository) / name).read_bytes()) == sender_contract["source_sha256"][name],
            "reviewed sender source changed before installation")
    source = _safe_path(Path(evidence_path))
    _require(source.stat().st_size <= MAX_EVIDENCE_BYTES, "broker evidence exceeds size bound")
    raw = source.read_bytes()
    evidence = json.loads(raw)
    _validate_evidence(evidence, record, captured_at=now)
    # Do not turn a historical read into a new freshness assertion. The query
    # had to be current when captured; the post-expiry absence proof is durable.
    path = _receipt_path(books, record)
    with books.locked():
        books.inspect()
        _bound(books, journal, record)
        if path.exists():
            return load_clearance(books, journal, record, as_of=now)
        path.parent.mkdir(exist_ok=True)
        evidence_digest = _sha(raw)
        _write_once(path.parent / (evidence_digest + ".broker.json"), raw)
        payload = dict(version=VERSION, journal_id=journal.journal_id, original_record=record,
            original_record_sha256=canonical_sha256(record), created_at=now.isoformat(),
            sender_contract=sender_contract, broker_evidence_sha256=evidence_digest,
            historical_execution="UNKNOWN", commitment="no_surviving_day_commitment")
        envelope = dict(payload=payload, sha256=canonical_sha256(payload))
        _write_once(path, json.dumps(envelope, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return load_clearance(books, journal, record, as_of=now)
