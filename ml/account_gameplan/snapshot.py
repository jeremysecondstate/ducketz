"""Read-only combined planning evidence; never sum producer cash or write a ledger."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from ml.gameplan_trade_snapshot import _allocation_timestamp, _capture_trade_planning_snapshot
from ml.stock_trader.contracts import canonical_sha256, utc
from ml.stock_trader.state import validated_symbols


_HASH = re.compile(r"[a-f0-9]{64}\Z")
_HORIZONS = {"1h", "4h", "1d", "1w"}
_FIELDS = {"producer", "source_fingerprint", "account_fingerprint", "observed_at",
           "symbols", "held_shares", "ownership"}
_OWNERSHIP_FIELDS = {"status", "safe_for_planning", "account_matches", "active_allocations",
    "blocked_symbols", "owned_shares", "reason_codes", "last_saved_reconciliation_at",
    "last_saved_reconciliation_ready", "current_broker_reconciliation_performed"}


def ownership_evidence_fingerprint(record: Mapping) -> str:
    """Content digest of an independently reviewed producer ownership envelope."""
    if not isinstance(record, Mapping) or set(record) - _FIELDS:
        raise ValueError("Ownership envelope contains unsupported fields")
    return canonical_sha256({key: value for key, value in record.items() if key != "source_fingerprint"})


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _quantity(value, *, whole=False):
    if isinstance(value, bool):
        raise ValueError("Invalid ownership quantity")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid ownership quantity") from exc
    if not number.is_finite() or number < 0 or (whole and number != number.to_integral_value()):
        raise ValueError("Invalid ownership quantity")
    return number


def _fresh(value, observed, maximum_age_seconds):
    stamp = _allocation_timestamp(value)
    if stamp is None or not 0 <= (utc(observed) - utc(stamp)).total_seconds() <= maximum_age_seconds:
        raise ValueError("Ownership or account evidence is missing, future or stale")
    return stamp


def _validated_ownership(records, *, symbols, account_fingerprint, expected_sources,
                         snapshot, maximum_age_seconds):
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence) or not records:
        raise ValueError("Complete producer ownership evidence is required")
    producers, covered, identities, routes = set(), set(), set(), set()
    active, evidence_refs, reconciled = [], [], []
    owned = {symbol: 0 for symbol in symbols}
    for record in records:
        if not isinstance(record, Mapping) or set(record) != _FIELDS:
            raise ValueError("Incomplete producer ownership envelope")
        producer = record["producer"]
        if producer not in expected_sources or producer in producers:
            raise ValueError("Unexpected or duplicate ownership producer")
        source = record["source_fingerprint"]
        if source != expected_sources[producer] or source != ownership_evidence_fingerprint(record):
            raise ValueError("Ownership source contents differ from reviewed evidence")
        if record["account_fingerprint"] != account_fingerprint:
            raise ValueError("Ownership account identity differs")
        observed = _fresh(record["observed_at"], snapshot["observed_at"], maximum_age_seconds)
        partition = validated_symbols(record["symbols"])
        if covered.intersection(partition) or not set(partition).issubset(symbols):
            raise ValueError("Ownership producer symbols overlap or exceed the reviewed universe")
        held = record["held_shares"]
        if not isinstance(held, Mapping) or set(held) != set(partition):
            raise ValueError("Ownership held-share coverage is incomplete")
        for symbol in partition:
            if _quantity(held[symbol]) != _quantity(snapshot["held_shares"][symbol]):
                raise ValueError("Ownership evidence differs from current broker holdings")
            # Pending union orders need native reconciliation, not an assumed
            # assignment. Off-universe commitments still bound account cash.
            if any(_quantity(snapshot[field][symbol]) for field in ("pending_buy_shares", "pending_sell_shares")):
                raise ValueError("Pending union orders require fresh native reconciliation")
        ownership = record["ownership"]
        if (not isinstance(ownership, Mapping) or set(ownership) != _OWNERSHIP_FIELDS
                or type(ownership.get("current_broker_reconciliation_performed")) is not bool
                or ownership.get("status") != "OBSERVED_CONSISTENT"
                or ownership.get("safe_for_planning") is not True or ownership.get("account_matches") is not True
                or ownership.get("blocked_symbols") != [] or ownership.get("reason_codes") != []
                or ownership.get("last_saved_reconciliation_ready") is not True):
            raise ValueError("Ownership evidence is blocked, unknown or not reconciled")
        saved_at = _allocation_timestamp(ownership.get("last_saved_reconciliation_at"))
        if saved_at is None or utc(saved_at) > utc(observed):
            raise ValueError("Ownership reconciliation time is invalid")
        # A fresh observation can reconcile current exact holdings against an
        # older saved ledger; an old producer observation itself is rejected.
        reconciled.append(saved_at)
        allocations = ownership.get("active_allocations")
        totals = ownership.get("owned_shares")
        if not isinstance(allocations, list) or not isinstance(totals, Mapping) or set(totals) != set(partition):
            raise ValueError("Exact ownership allocation and symbol totals are required")
        for item in allocations:
            if not isinstance(item, Mapping):
                raise ValueError("Invalid ownership allocation")
            identity, symbol, horizon = item.get("allocation_id_sha256"), item.get("symbol"), item.get("horizon")
            if (not _hash(identity) or identity in identities or symbol not in partition
                    or horizon not in _HORIZONS or (symbol, horizon) in routes or item.get("status") != "ACTIVE"):
                raise ValueError("Duplicate or invalid ownership allocation")
            quantity = _quantity(item.get("owned_shares"), whole=True)
            if any(_quantity(item.get(key), whole=True) for key in ("reserved_buy_shares", "reserved_sell_shares")):
                raise ValueError("Pending or unknown ledger reservations require reconciliation")
            start, end = _allocation_timestamp(item.get("target_start")), _allocation_timestamp(item.get("target_end"))
            if start is None or end is None or utc(end) <= utc(start):
                raise ValueError("Invalid ownership allocation timestamps")
            identities.add(identity)
            routes.add((symbol, horizon))
            owned[symbol] += int(quantity)
            active.append({"allocation_id_sha256": identity, "symbol": symbol, "horizon": horizon,
                "status": "ACTIVE", "owned_shares": int(quantity), "reserved_buy_shares": 0,
                "reserved_sell_shares": 0, "target_start": start, "target_end": end})
        for symbol in partition:
            if owned[symbol] != _quantity(totals[symbol], whole=True) or owned[symbol] > _quantity(held[symbol]):
                raise ValueError("Owned shares do not reconcile to allocations and broker holdings")
        evidence_refs.append({"producer": producer, "source_fingerprint": source,
                              "observed_at": observed, "symbols": list(partition)})
        producers.add(producer)
        covered.update(partition)
    if producers != set(expected_sources) or covered != set(symbols):
        raise ValueError("Ownership producers do not cover the complete reviewed universe")
    return {"status": "OBSERVED_CONSISTENT", "safe_for_planning": True, "account_matches": True,
        "active_allocations": sorted(active, key=lambda row: (row["symbol"], row["horizon"])),
        "owned_shares": owned, "blocked_symbols": [], "reason_codes": [],
        "last_saved_reconciliation_at": min(reconciled), "last_saved_reconciliation_ready": True,
        "current_broker_reconciliation_performed": False,
        "producer_evidence": sorted(evidence_refs, key=lambda row: row["producer"])}


def capture_account_planning_snapshot(*, symbols: Sequence[str], expected_account_fingerprint: str,
        expected_sources: Mapping[str, str], ownership_evidence: Sequence[Mapping] | Callable,
        session, observed_at=None, maximum_age_seconds: float = 60,
        clock: Callable = lambda: datetime.now(timezone.utc)) -> dict:
    """Capture one account once and validate independently bound ownership partitions.

    A callback receives keyword arguments ``account_fingerprint``, ``held_shares``
    and ``observed_at`` and returns the same source-bound producer envelopes.
    It confers no authority on stale source-bundle ownership or producer cash.
    The mandatory session is supplied by the separately authorized coordinator.
    """
    requested = validated_symbols(symbols)
    if not _hash(expected_account_fingerprint):
        raise ValueError("Expected account fingerprint is required")
    if (not isinstance(expected_sources, Mapping) or not expected_sources
            or any(not isinstance(producer, str) or not producer or not _hash(digest)
                   for producer, digest in expected_sources.items())):
        raise ValueError("Reviewed producer source fingerprints are required")
    if type(maximum_age_seconds) not in (int, float) or not 0 < maximum_age_seconds <= 60:
        raise ValueError("Snapshot freshness must be positive and at most 60 seconds")
    if session is None:
        raise ValueError("An explicit read-only account session is required")
    expected_sources = dict(expected_sources)
    initial = utc(clock())
    observed = initial.isoformat() if observed_at is None else _fresh(observed_at, initial, maximum_age_seconds)

    def ownership_reader(identity, snapshot):
        if identity != expected_account_fingerprint or snapshot["cash_status"] != "CASH_ONLY_BOUNDED":
            raise ValueError("Account identity or complete cash evidence is unavailable")
        _fresh(snapshot["observed_at"], utc(clock()), maximum_age_seconds)
        records = (ownership_evidence(account_fingerprint=identity,
                   held_shares=dict(snapshot["held_shares"]), observed_at=snapshot["observed_at"])
                   if callable(ownership_evidence) else ownership_evidence)
        _fresh(snapshot["observed_at"], utc(clock()), maximum_age_seconds)
        return _validated_ownership(records, symbols=requested, account_fingerprint=identity,
            expected_sources=expected_sources, snapshot=snapshot, maximum_age_seconds=maximum_age_seconds)

    result = _capture_trade_planning_snapshot(None, requested=requested, session=session,
        observed_at=observed, explicit_universe=True, ownership_reader=ownership_reader)
    result.update(account_planning_schema_version="account-gameplan-snapshot-v1", symbols=list(requested),
        account_fingerprint=expected_account_fingerprint, ownership_source_fingerprints=expected_sources)
    result["source_fingerprint"] = canonical_sha256(result)
    return result


__all__ = ["capture_account_planning_snapshot", "ownership_evidence_fingerprint"]
