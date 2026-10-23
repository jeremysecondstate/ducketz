"""Durable inventory for independent stock horizons; this module never trades.

The caller retains broker/session/quote checks and the existing combined order,
cash and exposure limits. A reservation is not a fill. Per-order fill evidence
creates purchased inventory. The opt-in Gameplan direction policy may explicitly
attribute observed existing stock to a sale; unsold terminal quantities return
to the unallocated pool. All account identities are one-way fingerprints.

The explicit 15m path is separate from Gameplan's horizon eligibility and caps.
It attributes one account-wide sale's exact fills across actual lots, without
creating fills or invoking brokers. Shared authority admits actual capacity.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


HORIZON_WEIGHTS = {"1h": 1, "4h": 2, "1d": 3, "1w": 4}
LEDGER_VERSION = "independent-stock-horizon-ledger-v1"
_OPEN = ("RESERVED", "SUBMITTED", "UNKNOWN", "WORKING", "PARTIAL")
_TERMINAL = ("FILLED", "CANCELLED", "REJECTED")


class LedgerError(ValueError):
    """The evidence or requested reservation cannot safely be accepted."""


@dataclass(frozen=True)
class FillEvidence:
    fill_id: str
    quantity: int
    price: str | float
    executed_at: str


@dataclass(frozen=True)
class OrderEvidence:
    evidence_id: str
    reservation_id: str
    account_fingerprint: str
    observed_at: str
    broker_order_id: str
    status: str
    order_quantity: int
    cumulative_filled_quantity: int
    remaining_quantity: int
    fills: tuple[FillEvidence, ...] = ()
    broker_status: str | None = None
    broker_status_description: str | None = None
    fill_time_provenance: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PortfolioEvidence:
    snapshot_id: str
    account_fingerprint: str
    observed_at: str
    held_shares: Mapping[str, int | float]
    prices: Mapping[str, str | float]
    symbol_budgets: Mapping[str, str | float]
    source_fingerprint: str


@dataclass(frozen=True)
class ReservationState:
    reservation_id: str
    allocation_id: str
    symbol: str
    horizon: str
    forecast_id: str
    side: str
    quantity: int
    limit_price: str
    filled_quantity: int
    status: str
    broker_order_id: str | None
    idempotency_key: str
    batch_id: str
    last_evidence_at: str | None
    cancel_requested_at: str | None = None
    target_start: str | None = None
    target_end: str | None = None
    trigger_horizon: str | None = None
    trigger_forecast_id: str | None = None
    owner_forecast_id: str | None = None
    fallback_policy_version: str | None = None
    fallback_action_date: str | None = None
    fallback_baseline_id: str | None = None
    submitted_at: str | None = None
    timeout_requested_at: str | None = None
    retry_parent_id: str | None = None
    retry_requested_quantity: int | None = None

    @property
    def reserved_quantity(self) -> int:
        return self.quantity - self.filled_quantity if self.status in _OPEN else 0


@dataclass(frozen=True)
class AllocationState:
    allocation_id: str
    account_fingerprint: str
    symbol: str
    horizon: str
    forecast_id: str
    target_start: str
    target_end: str
    status: str
    filled_shares: int
    reserved_buy_shares: int
    reserved_sell_shares: int


@dataclass(frozen=True)
class LedgerSnapshot:
    allocations: tuple[AllocationState, ...]
    reservations: tuple[ReservationState, ...]
    persistent_blocks: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AuditRecord:
    evidence_id: str
    kind: str
    payload_json: str


@dataclass(frozen=True)
class ReconciliationResult:
    snapshot_id: str
    ready: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ExitPlan:
    allocation_id: str
    symbol: str
    horizon: str
    forecast_id: str
    target_end: str
    quantity: int
    ready: bool
    reasons: tuple[str, ...]


def _utc(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("Invalid evidence timestamp") from exc
    if parsed.tzinfo is None:
        raise LedgerError("Evidence timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def validate_fill_time_provenance(provenance, fills):
    """Validate original wire timestamps without changing immutable fill cores.

    Only explicit whole-second wire strings identify a represented second.
    A normalized zero microsecond value cannot establish that precision: the
    original ``.000`` string is still fractional. Missing metadata keeps the
    strict legacy timestamp interpretation.
    """
    if provenance is None:
        return None, frozenset()
    if (not isinstance(provenance, Mapping) or set(provenance) != {"version", "fills"}
            or provenance["version"] != "schwab-execution-time-v1"
            or not isinstance(provenance["fills"], Mapping)):
        raise LedgerError("FILL_TIME_PROVENANCE_INVALID")
    identities = [fill.fill_id for fill in fills]
    if (any(not isinstance(identity, str) or not identity for identity in identities)
            or len(set(identities)) != len(identities)
            or set(provenance["fills"]) != set(identities)):
        raise LedgerError("FILL_TIME_PROVENANCE_INVALID")
    validated, whole_seconds = {}, set()
    for fill in fills:
        item = provenance["fills"][fill.fill_id]
        if (not isinstance(item, Mapping) or set(item) != {"raw_timestamp", "source_field"}
                or not isinstance(item["source_field"], str)
                or item["source_field"] not in {"execution_leg.time", "execution_leg.executionTime",
                    "activity.executionTime", "activity.time"}):
            raise LedgerError("FILL_TIME_PROVENANCE_INVALID")
        raw = item["raw_timestamp"]
        if (not isinstance(raw, str) or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})", raw) is None):
            raise LedgerError("FILL_TIME_PROVENANCE_INVALID")
        if _utc(raw) != _utc(fill.executed_at):
            raise LedgerError("FILL_TIME_PROVENANCE_DOES_NOT_MATCH_FILL")
        validated[fill.fill_id] = {"raw_timestamp": raw, "source_field": item["source_field"]}
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})", raw):
            whole_seconds.add(fill.fill_id)
    return {"version": "schwab-execution-time-v1", "fills": validated}, frozenset(whole_seconds)


def _number(value: object, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise LedgerError("Invalid numeric evidence") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise LedgerError("Numeric evidence is not finite and nonnegative")
    return result


def _quantity(value: object, *, positive: bool = False) -> int:
    number = _number(value, positive=positive)
    if number != number.to_integral_value():
        raise LedgerError("Order/fill quantities must be whole shares")
    return int(number)


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(value: object) -> str:
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _name(value: object) -> str:
    result = str(value).strip()
    if not result:
        raise LedgerError("Evidence identity must not be empty")
    return result


class HorizonLedger:
    def __init__(self, db_path: Path, account_fingerprint: str, *, maximum_evidence_age_seconds: int = 60):
        if not re.fullmatch(r"[a-f0-9]{64}", str(account_fingerprint)):
            raise LedgerError("A SHA-256 account fingerprint is required; never pass an account number")
        if not 1 <= maximum_evidence_age_seconds <= 300:
            raise LedgerError("Evidence age must be between one second and five minutes")
        self.path = Path(db_path).resolve()
        self.account_fingerprint = account_fingerprint
        self.maximum_evidence_age_seconds = maximum_evidence_age_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS allocations (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, symbol TEXT NOT NULL,
                    horizon TEXT NOT NULL, forecast TEXT NOT NULL, start TEXT NOT NULL,
                    end TEXT NOT NULL, status TEXT NOT NULL,
                    UNIQUE(account,symbol,horizon,forecast));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_horizon
                    ON allocations(account,symbol,horizon) WHERE status='ACTIVE';
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, allocation TEXT NOT NULL REFERENCES allocations(id),
                    side TEXT NOT NULL, quantity INTEGER NOT NULL, price TEXT NOT NULL,
                    filled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                    broker_order TEXT, idempotency_key TEXT NOT NULL UNIQUE,
                    batch TEXT NOT NULL, request TEXT NOT NULL, last_evidence_at TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS unique_broker_order
                    ON reservations(broker_order) WHERE broker_order IS NOT NULL;
                CREATE TABLE IF NOT EXISTS fills (
                    id TEXT PRIMARY KEY, reservation TEXT NOT NULL REFERENCES reservations(id),
                    quantity INTEGER NOT NULL, price TEXT NOT NULL, executed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                    ready INTEGER NOT NULL, reasons TEXT NOT NULL, owned TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS blocks (symbol TEXT PRIMARY KEY, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cancellations (
                    reservation TEXT PRIMARY KEY REFERENCES reservations(id), requested_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS order_submission_times (
                    reservation TEXT PRIMARY KEY REFERENCES reservations(id), submitted_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS order_timeouts (
                    reservation TEXT PRIMARY KEY REFERENCES reservations(id), requested_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS order_timeout_retries (
                    child TEXT PRIMARY KEY REFERENCES reservations(id),
                    parent TEXT NOT NULL REFERENCES reservations(id), linked_at TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS fallback_days (
                    account TEXT NOT NULL, action_date TEXT NOT NULL, baseline_id TEXT NOT NULL UNIQUE,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(id), source_fingerprint TEXT NOT NULL,
                    policy TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account,action_date));
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            previous = db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
            if previous is not None and previous[0] != account_fingerprint:
                raise LedgerError("ACCOUNT_FINGERPRINT_MISMATCH")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('account', ?)", (account_fingerprint,))
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('version', ?)", (LEDGER_VERSION,))

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=EXTRA")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _account(self, fingerprint: str) -> None:
        if fingerprint != self.account_fingerprint:
            raise LedgerError("ACCOUNT_FINGERPRINT_MISMATCH")

    @staticmethod
    def _save_evidence(db, evidence_id: str, kind: str, payload: object) -> bool:
        evidence_id, encoded = _name(evidence_id), _encoded(payload)
        old = db.execute("SELECT kind,payload FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if old is not None:
            if (old["kind"], old["payload"]) != (kind, encoded):
                raise LedgerError("EVIDENCE_ID_REUSED_WITH_DIFFERENT_CONTENT")
            return False
        db.execute("INSERT INTO evidence VALUES (?,?,?)", (evidence_id, kind, encoded))
        return True

    @staticmethod
    def _read_rows(db, query, columns, parameters=()):
        """Return a bounded ledger query with one SQLite-to-Python handoff.

        A normal fetchall releases the GIL again for every sqlite3_step. During
        concurrent model preparation even a batched query can therefore age
        broker evidence. SQLite builds the ordered scalar rows in one value;
        JSON decoding preserves these ledger TEXT/INTEGER/NULL values.
        Columns are internal literals, never account or provider input.
        """
        fields = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
        payload = db.execute("SELECT json_group_array(json_array(" + fields + ")) FROM (" + query + ")",
                             parameters).fetchone()[0]
        return json.loads(payload)

    @classmethod
    def _reservation_rows(cls, db, reservation_id=None):
        columns = ("id", "allocation", "side", "quantity", "price", "filled", "status", "broker_order",
                   "idempotency_key", "batch", "request", "last_evidence_at", "symbol", "horizon", "forecast",
                   "requested_at", "submitted_at", "timeout_requested_at", "retry_parent_id", "retry_requested_quantity")
        rows = cls._read_rows(db, """SELECT r.*,a.symbol,a.horizon,a.forecast,c.requested_at,
            s.submitted_at,t.requested_at AS timeout_requested_at,retry.parent AS retry_parent_id,
            retry.requested_quantity AS retry_requested_quantity FROM reservations r
            LEFT JOIN allocations a ON a.id=r.allocation
            LEFT JOIN cancellations c ON c.reservation=r.id
            LEFT JOIN order_submission_times s ON s.reservation=r.id
            LEFT JOIN order_timeouts t ON t.reservation=r.id
            LEFT JOIN order_timeout_retries retry ON retry.child=r.id"""
            + (" WHERE r.id=?" if reservation_id is not None else " ORDER BY r.id"),
            columns, (reservation_id,) if reservation_id is not None else ())
        return [dict(zip(columns, row)) for row in rows]

    @classmethod
    def _reservation(cls, db, reservation_id: str) -> ReservationState:
        rows = cls._reservation_rows(db, reservation_id)
        row = rows[0] if rows else None
        if row is None:
            raise LedgerError("UNKNOWN_RESERVATION")
        submitted_at = row["submitted_at"]
        if submitted_at is None:
            # Older ledgers already retain the first attempted broker submission
            # in immutable evidence. Never substitute a scheduled start or a
            # later refreshed broker observation when that evidence is missing.
            evidence = db.execute("""SELECT payload FROM evidence WHERE kind='submission'
                AND json_extract(payload,'$.reservation_id')=?
                AND json_extract(payload,'$.status') IN ('SUBMITTED','UNKNOWN')
                ORDER BY rowid LIMIT 1""", (reservation_id,)).fetchone()
            if evidence is not None:
                submitted_at = _utc(json.loads(evidence["payload"])["observed_at"])
                db.execute("INSERT INTO order_submission_times VALUES (?,?)", (reservation_id, submitted_at))
        return cls._reservation_value(row, submitted_at)

    @staticmethod
    def _reservation_value(row, submitted_at):
        if row["symbol"] is None:
            # A damaged foreign key must not disappear from the bulk join.
            raise LedgerError("UNKNOWN_RESERVATION")
        request = json.loads(row["request"])
        return ReservationState(row["id"], row["allocation"], row["symbol"], row["horizon"],
            request.get("forecast", row["forecast"]), row["side"], row["quantity"], row["price"], row["filled"],
            row["status"], row["broker_order"], row["idempotency_key"], row["batch"], row["last_evidence_at"], row["requested_at"],
            request.get("start"), request.get("end"), request.get("trigger_horizon"),
            request.get("trigger_forecast"), row["forecast"], request.get("fallback_policy_version"),
            request.get("action_date"), request.get("baseline_id"), submitted_at,
            row["timeout_requested_at"], row["retry_parent_id"], row["retry_requested_quantity"])

    @classmethod
    def _reservations(cls, db):
        """Restore every identity with bounded SQL work, including old receipts.

        SQLite releases the GIL per call. Hundreds of per-order queries can
        age a fresh capacity snapshot while model workers run in this process.
        Preserve the same earliest submission fallback without rescanning all
        evidence once for every rejected or otherwise never-submitted order.
        """
        rows = cls._reservation_rows(db)
        missing = {row["id"] for row in rows if row["submitted_at"] is None}
        recovered = {}
        if missing:
            for evidence in cls._read_rows(db, """SELECT json_extract(payload,'$.reservation_id') AS reservation_id,payload
                    FROM evidence WHERE kind='submission'
                    AND json_extract(payload,'$.status') IN ('SUBMITTED','UNKNOWN') ORDER BY rowid""",
                    ("reservation_id", "payload")):
                identity = evidence[0]
                if identity in missing and identity not in recovered:
                    recovered[identity] = _utc(json.loads(evidence[1])["observed_at"])
            db.executemany("INSERT INTO order_submission_times VALUES (?,?)", recovered.items())
        return tuple(cls._reservation_value(row, row["submitted_at"] if row["submitted_at"] is not None
                                           else recovered.get(row["id"])) for row in rows)

    @classmethod
    def _assigned_shares_by_allocation(cls, db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignments'").fetchone():
            return {}
        assigned = dict(cls._read_rows(db, "SELECT allocation,SUM(quantity) AS quantity FROM inventory_assignments GROUP BY allocation",
                                      ("allocation", "quantity")))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignment_releases'").fetchone():
            for allocation, quantity in cls._read_rows(db, """SELECT i.allocation,SUM(r.quantity) AS quantity
                    FROM inventory_assignment_releases r JOIN inventory_assignments i ON i.id=r.assignment
                    GROUP BY i.allocation""", ("allocation", "quantity")):
                assigned[allocation] = assigned.get(allocation, 0) - quantity
        return assigned

    @staticmethod
    def _forecast_reserved(db, account, symbol, horizon, forecast):
        rows = db.execute("""SELECT r.request,a.horizon FROM reservations r JOIN allocations a ON a.id=r.allocation
            WHERE a.account=? AND a.symbol=?""", (account, symbol))
        return any((request := json.loads(row["request"])).get("forecast") == forecast and
                   request.get("trigger_horizon", row["horizon"]) == horizon for row in rows)

    @staticmethod
    def _assigned_shares(db, allocation_id):
        # This optional additive table appears only when the explicitly selected
        # Gameplan policy records existing stock for a directional sale. It is
        # observed opening inventory, never a fabricated BUY or broker fill.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignments'").fetchone():
            return 0
        assigned = db.execute("SELECT COALESCE(SUM(quantity),0) FROM inventory_assignments WHERE allocation=?", (allocation_id,)).fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignment_releases'").fetchone():
            assigned -= db.execute("""SELECT COALESCE(SUM(r.quantity),0) FROM inventory_assignment_releases r
                JOIN inventory_assignments i ON i.id=r.assignment WHERE i.allocation=?""", (allocation_id,)).fetchone()[0]
        return assigned

    @classmethod
    def _release_unfilled_assignment(cls, db, reservation_id, observed):
        """Return definitively unsold opening inventory to the unallocated pool.

        A direction sale consumes its prior owned shares first, then the newly
        assigned shares. No release is a broker fill, and uncertain acceptance
        keeps all inventory reserved until actual order evidence resolves it.
        """
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignments'").fetchone():
            return
        order = cls._reservation(db, reservation_id)
        if order.side != "SELL" or order.status not in _TERMINAL:
            return
        assignment_id = _identity(["gameplan-observed-opening-stock", order.idempotency_key])
        assignment = db.execute("SELECT * FROM inventory_assignments WHERE id=?", (assignment_id,)).fetchone()
        if assignment is None:
            return
        prior_owned_quantity = order.quantity - assignment["quantity"]
        assigned_filled = max(0, order.filled_quantity - prior_owned_quantity)
        released = assignment["quantity"] - assigned_filled
        if released <= 0:
            return
        baseline = db.execute("SELECT id FROM snapshots WHERE ready=1 ORDER BY observed_at DESC,rowid DESC LIMIT 1").fetchone()
        if baseline is None:
            raise LedgerError("ASSIGNED_INVENTORY_RELEASE_REQUIRES_RECONCILED_BASELINE")
        db.execute("""CREATE TABLE IF NOT EXISTS inventory_assignment_releases (
            id TEXT PRIMARY KEY, assignment TEXT NOT NULL REFERENCES inventory_assignments(id),
            reservation TEXT NOT NULL REFERENCES reservations(id),
            snapshot_id TEXT NOT NULL REFERENCES snapshots(id), quantity INTEGER NOT NULL,
            observed_at TEXT NOT NULL)""")
        release_id = _identity(["gameplan-unsold-opening-stock-release", reservation_id])
        previous = db.execute("SELECT quantity FROM inventory_assignment_releases WHERE id=?", (release_id,)).fetchone()
        if previous is not None:
            if previous["quantity"] != released:
                raise LedgerError("TERMINAL_ASSIGNED_INVENTORY_RELEASE_CHANGED")
            return
        db.execute("INSERT INTO inventory_assignment_releases VALUES (?,?,?,?,?,?)",
                   (release_id, assignment_id, reservation_id, baseline["id"], released, observed))
        cls._save_evidence(db, release_id, "gameplan-unsold-opening-stock-release", {
            "assignment_id": assignment_id, "reservation_id": reservation_id,
            "snapshot_id": baseline["id"], "quantity": released, "observed_at": observed,
            "prior_owned_shares_consumed_first": True, "assigned_shares_filled": assigned_filled,
            "reason": "Definitively unsold opening inventory returns to the unallocated pool"})

    @classmethod
    def _snapshot(cls, db) -> LedgerSnapshot:
        reservations = cls._reservations(db)
        sliced, debits, pending = cls._intraday_share_accounting(db, reservations)
        assigned = cls._assigned_shares_by_allocation(db)
        by_allocation = {}
        for reservation in reservations:
            by_allocation.setdefault(reservation.allocation_id, []).append(reservation)
        allocations = []
        columns = ("id", "account", "symbol", "horizon", "forecast", "start", "end", "status")
        for values in cls._read_rows(db, "SELECT * FROM allocations ORDER BY symbol,horizon,start,id", columns):
            row = dict(zip(columns, values))
            orders = by_allocation.get(row["id"], ())
            held = (assigned.get(row["id"], 0) + sum(r.filled_quantity * (1 if r.side == "BUY" else -1)
                    for r in orders if r.reservation_id not in sliced) - debits.get(row["id"], 0))
            allocations.append(AllocationState(row["id"], row["account"], row["symbol"], row["horizon"],
                row["forecast"], row["start"], row["end"], row["status"], held,
                sum(r.reserved_quantity for r in orders if r.side == "BUY"),
                sum(r.reserved_quantity for r in orders if r.side == "SELL" and r.reservation_id not in sliced)
                + pending.get(row["id"], 0)))
        return LedgerSnapshot(tuple(allocations), reservations,
            tuple((row[0], row[1]) for row in cls._read_rows(db, "SELECT symbol,reason FROM blocks ORDER BY symbol", ("symbol", "reason"))))

    def snapshot(self) -> LedgerSnapshot:
        """Return frozen allocation/reservation records, with no live operations."""
        with self._transaction() as db:
            return self._snapshot(db)

    def lookup_reservation(self, reservation_id: str) -> ReservationState:
        with self._transaction() as db:
            return self._reservation(db, reservation_id)

    def pending_reservations(self) -> tuple[ReservationState, ...]:
        return tuple(r for r in self.snapshot().reservations if r.status in _OPEN)

    @staticmethod
    def _stream(db, kind, identity):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_streams'").fetchone():
            return "gameplan"
        row = db.execute("SELECT stream FROM intraday_streams WHERE kind=? AND id=?",(kind,identity)).fetchone()
        return row[0] if row else "gameplan"

    def reservation_stream(self, reservation_id: str) -> str:
        """Identify policy ownership without changing legacy record payloads."""
        with self._transaction() as db:
            self._reservation(db,reservation_id)
            return self._stream(db,"reservation",reservation_id)

    def allocation_stream(self, allocation_id: str) -> str:
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM allocations WHERE id=?",(allocation_id,)).fetchone():
                raise LedgerError("UNKNOWN_ALLOCATION")
            return self._stream(db,"allocation",allocation_id)

    def intraday_attempts(self, *, symbol: str, forecast_id: str) -> tuple[ReservationState, ...]:
        """Return durable insertion order, independent of hashes or clock ties.

        Attempts retain independent idempotency identities. Public legacy
        snapshot ordering and record serialization remain unchanged.
        """
        with self._transaction() as db:
            output=[]
            # Transfer history once instead of releasing the GIL for each
            # SQLite row while current execution evidence ages. Preserve raw
            # BLOBs too: json.loads accepts encoded JSON bytes in old rows.
            rows = self._read_rows(db, """SELECT
                CASE WHEN typeof(r.id)='blob' THEN hex(r.id) ELSE r.id END AS id,
                typeof(r.id) AS id_storage,
                CASE WHEN typeof(r.request)='blob' THEN hex(r.request) ELSE r.request END AS request,
                typeof(r.request) AS request_storage
                FROM reservations r JOIN allocations a ON a.id=r.allocation
                WHERE a.account=? AND a.symbol=? ORDER BY r.rowid""",
                ("id", "id_storage", "request", "request_storage"),
                (self.account_fingerprint,_name(symbol).upper()))
            for key, id_storage, raw, request_storage in rows:
                if id_storage == "blob":
                    key = bytes.fromhex(key)
                if request_storage == "blob":
                    raw = bytes.fromhex(raw)
                request=json.loads(raw)
                if request.get("stream") in {"atlas-15m", "scout-15m"} and request.get("forecast")==_name(forecast_id):
                    output.append(self._reservation(db,key))
            return tuple(output)

    @staticmethod
    def _intraday_schema(db):
        for sql in (
            "CREATE TABLE IF NOT EXISTS intraday_streams(kind TEXT NOT NULL,id TEXT NOT NULL,stream TEXT NOT NULL,proof TEXT NOT NULL,PRIMARY KEY(kind,id))",
            "CREATE TABLE IF NOT EXISTS intraday_sell_slices(reservation TEXT NOT NULL REFERENCES reservations(id),allocation TEXT NOT NULL REFERENCES allocations(id),quantity INTEGER NOT NULL CHECK(quantity>0),ordinal INTEGER NOT NULL,PRIMARY KEY(reservation,ordinal))",
            "CREATE TABLE IF NOT EXISTS intraday_fill_attribution(fill TEXT NOT NULL REFERENCES fills(id),allocation TEXT NOT NULL REFERENCES allocations(id),slice_ordinal INTEGER NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),PRIMARY KEY(fill,slice_ordinal))",
        ):
            db.execute(sql)
        db.execute("INSERT OR IGNORE INTO metadata VALUES('intraday_accounting_version','account-shares-attribution-v1')")

    @classmethod
    def _intraday_share_accounting(cls, db, reservations):
        """Charge exact filled/unfilled SELL slices once to their real owners."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_sell_slices'").fetchone():
            return set(),{},{}
        by_id = {r.reservation_id:r for r in reservations}
        filled = {(row[0],row[1]):row[2] for row in cls._read_rows(db, """SELECT f.reservation,a.slice_ordinal,SUM(a.quantity) AS quantity
            FROM intraday_fill_attribution a JOIN fills f ON f.id=a.fill GROUP BY f.reservation,a.slice_ordinal""",
            ("reservation", "slice_ordinal", "quantity"))}
        sliced, debits, pending = set(),{},{}
        for row in cls._read_rows(db, "SELECT reservation,allocation,quantity,ordinal FROM intraday_sell_slices",
                                 ("reservation", "allocation", "quantity", "ordinal")):
            order = by_id[row[0]]
            charged = filled.get((row[0],row[3]),0)
            if order.side!="SELL" or not 0<=charged<=row[2]:
                raise LedgerError("INTRADAY_ATTRIBUTION_DOES_NOT_BALANCE")
            sliced.add(row[0])
            debits[row[1]] = debits.get(row[1],0)+charged
            if order.status in _OPEN:
                pending[row[1]] = pending.get(row[1],0)+row[2]-charged
        for identity in sliced:
            if sum(q for (r,_),q in filled.items() if r==identity)!=by_id[identity].filled_quantity:
                raise LedgerError("INTRADAY_ATTRIBUTION_DOES_NOT_BALANCE")
        return sliced,debits,pending

    @classmethod
    def _attribute_intraday_fills(cls, db, reservation_id):
        """Append per-fill ownership evidence; previous attributions never move."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_sell_slices'").fetchone():
            return
        slices = db.execute("SELECT allocation,quantity,ordinal FROM intraday_sell_slices WHERE reservation=? ORDER BY ordinal",(reservation_id,)).fetchall()
        if not slices:
            return
        used = {row[0]:row[1] for row in db.execute("""SELECT a.slice_ordinal,SUM(a.quantity)
            FROM intraday_fill_attribution a JOIN fills f ON f.id=a.fill WHERE f.reservation=? GROUP BY a.slice_ordinal""",(reservation_id,))}
        for fill in db.execute("SELECT id,quantity FROM fills WHERE reservation=? ORDER BY executed_at,id",(reservation_id,)).fetchall():
            old = db.execute("SELECT COALESCE(SUM(quantity),0) FROM intraday_fill_attribution WHERE fill=?",(fill[0],)).fetchone()[0]
            if old:
                if old!=fill[1]:
                    raise LedgerError("INTRADAY_ATTRIBUTION_DOES_NOT_BALANCE")
                continue
            remaining = fill[1]
            for allocation,quantity,ordinal in slices:
                take = min(remaining,quantity-used.get(ordinal,0))
                if take>0:
                    db.execute("INSERT INTO intraday_fill_attribution VALUES(?,?,?,?)",(fill[0],allocation,ordinal,take))
                    used[ordinal] = used.get(ordinal,0)+take
                    remaining -= take
                if not remaining:
                    break
            if remaining:
                raise LedgerError("INTRADAY_FILL_EXCEEDS_RESERVED_SLICES")

    def intraday_sell_attribution(self, reservation_id: str):
        """Audit planned owner slices and actual fill debits, never broker fills."""
        with self._transaction() as db:
            self._reservation(db,reservation_id)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_sell_slices'").fetchone():
                return {"slices":[],"fills":[]}
            return {"slices":[dict(row) for row in db.execute("SELECT allocation,quantity,ordinal FROM intraday_sell_slices WHERE reservation=? ORDER BY ordinal",(reservation_id,))],
                    "fills":[dict(row) for row in db.execute("""SELECT a.fill,a.allocation,a.slice_ordinal,a.quantity FROM intraday_fill_attribution a
                        JOIN fills f ON f.id=a.fill WHERE f.reservation=? ORDER BY a.fill,a.allocation""",(reservation_id,))]}

    def reserve_stream_cancellation(self, reservation_id: str, *, stream: str, reason: str, as_of: str, evidence_id: str) -> bool:
        """Persist one shared cancellation request after the stream chose why.

        The caller owns timing/supersession eligibility and broker guards. This
        never invokes a broker or releases capacity, including after DELETE.
        """
        if stream not in {"atlas-15m", "scout-15m"} or reason not in {"new-forecast","repricing","manual-stop"}:
            raise LedgerError("UNREVIEWED_STREAM_CANCELLATION_POLICY")
        now = _utc(as_of)
        with self._transaction() as db:
            order = self._reservation(db,reservation_id)
            if self._stream(db,"reservation",reservation_id)!=stream:
                raise LedgerError("CANCELLATION_STREAM_MISMATCH")
            if order.cancel_requested_at is not None:
                return False
            if order.status not in {"SUBMITTED","WORKING","PARTIAL"} or not order.broker_order_id or not order.reserved_quantity:
                raise LedgerError("CANCELLATION_REQUIRES_KNOWN_WORKING_ORDER")
            if order.last_evidence_at and now<order.last_evidence_at:
                raise LedgerError("STALE_CANCELLATION_REQUEST")
            self._save_evidence(db,evidence_id,"stream-cancellation-reservation",{
                "reservation_id":reservation_id,"broker_order_id":order.broker_order_id,
                "requested_at":now,"stream":stream,"reason":reason})
            db.execute("INSERT INTO cancellations VALUES(?,?)",(reservation_id,now))
            return True

    def _require_intraday_snapshot(self, db, snapshot_id, now):
        row = db.execute("SELECT * FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()
        if not row or row["id"]!=snapshot_id:
            raise LedgerError("CURRENT_RECONCILED_PORTFOLIO_REQUIRED")
        age = (datetime.fromisoformat(now)-datetime.fromisoformat(row["observed_at"])).total_seconds()
        if not 0<=age<=self.maximum_evidence_age_seconds:
            raise LedgerError("STALE_OR_FUTURE_PORTFOLIO_EVIDENCE")
        # Exact pending commitments remain unavailable; their existence is not
        # an independent horizon funding gate. Actual inventory discrepancies
        # and stale status evidence still require reconciliation.
        ignored = (":UNRECONCILED_RESERVED:",":UNRECONCILED_SUBMITTED:",":UNRECONCILED_UNKNOWN:")
        if any(not any(flag in reason for flag in ignored) for reason in json.loads(row["reasons"])):
            raise LedgerError("CURRENT_RECONCILED_PORTFOLIO_REQUIRED")
        if db.execute("SELECT 1 FROM blocks LIMIT 1").fetchone():
            raise LedgerError("PERSISTENT_INVENTORY_DISCREPANCY")
        return json.loads(row["payload"])

    def reserve_intraday(self, *, symbol: str, forecast_id: str, side: str, quantity: int, limit_price,
                         snapshot_id: str, idempotency_key: str, batch_id: str, as_of: str,
                         reference_at: str, target_close_at: str, forecast_sha256: str,
                         forecast_snapshot_sha256: str, local_symbols: Sequence[str], pending_sell_shares=0,
                         broker_pending_sell_orders: Mapping[str, int] | None = None,
                         retry_of_reservation_id: str | None = None, stream: str = "atlas-15m") -> ReservationState:
        """Record 15m commitment/fills without Gameplan horizon funding rules.

        The shared authority admits cash and exact account shares. A SELL can
        debit every actual lot plus observed unallocated inventory. Slice order
        is a stable accounting identity order, never a donor eligibility policy.
        A fresh forecast is independently sized; filled purchases never reenter.
        """
        symbol,forecast_id = _name(symbol).upper(),_name(forecast_id)
        if (stream not in {"atlas-15m", "scout-15m"} or side not in {"BUY","SELL"} or symbol not in local_symbols or len(local_symbols)!=len(set(local_symbols))
                or any(not isinstance(s,str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}",s) for s in local_symbols)):
            raise LedgerError("INTRADAY_SYMBOL_OR_SIDE_OUTSIDE_LOCAL_SCOPE")
        for value in (forecast_sha256,forecast_snapshot_sha256):
            if not re.fullmatch(r"[a-f0-9]{64}",str(value)):
                raise LedgerError("IMMUTABLE_INTRADAY_PROOF_REQUIRED")
        if forecast_id!=forecast_sha256:
            raise LedgerError("IMMUTABLE_INTRADAY_FORECAST_HASH_REQUIRED")
        reference,target,now = _utc(reference_at),_utc(target_close_at),_utc(as_of)
        parsed = datetime.fromisoformat(reference)
        if parsed.minute%15 or parsed.second or parsed.microsecond or datetime.fromisoformat(target)!=parsed+timedelta(minutes=45):
            raise LedgerError("EXACT_INTRADAY_TARGET_REQUIRED")
        quantity,price = _quantity(quantity,positive=True),str(_number(limit_price,positive=True))
        external_pending = _quantity(pending_sell_shares)
        exact_pending = None
        if broker_pending_sell_orders is not None:
            exact_pending = {_name(k):_quantity(v,positive=True) for k,v in broker_pending_sell_orders.items()}
            if len(exact_pending)!=len(broker_pending_sell_orders) or sum(exact_pending.values())!=external_pending:
                raise LedgerError("EXACT_BROKER_PENDING_SELL_QUANTITIES_REQUIRED")
        key,batch = _name(idempotency_key),_name(batch_id)
        request = {"kind":"intraday-account-intention","stream":stream,"symbol":symbol,"horizon":"15m",
                   "forecast":forecast_id,"side":side,"quantity":quantity,"price":price,"snapshot":snapshot_id,
                   "batch":batch,"start":reference,"end":target,"forecast_sha256":forecast_sha256,
                   "forecast_snapshot_sha256":forecast_snapshot_sha256,"local_symbols":sorted(local_symbols),
                   "pending_sell_shares":external_pending,"broker_pending_sell_orders":exact_pending}
        if retry_of_reservation_id is not None:
            request["retry_of_reservation_id"] = _name(retry_of_reservation_id)
        with self._transaction() as db:
            existing = self._idempotent(db,key,request)
            if existing is not None:
                return existing
            if not parsed+timedelta(minutes=15)<=datetime.fromisoformat(now)<datetime.fromisoformat(target):
                raise LedgerError("INTRADAY_FORECAST_NOT_CURRENT")
            portfolio = self._require_intraday_snapshot(db,snapshot_id,now)
            if retry_of_reservation_id is not None:
                self._intraday_retry_parent(db,request)
            elif self._forecast_reserved(db,self.account_fingerprint,symbol,"15m",forecast_id):
                raise LedgerError("FORECAST_ALREADY_RESERVED_NO_REENTRY")
            state = self._snapshot(db)
            active = next((a for a in state.allocations if a.symbol==symbol and a.horizon=="15m" and a.status=="ACTIVE"),None)
            if active and self._stream(db,"allocation",active.allocation_id)!=stream:
                raise LedgerError("INTRADAY_ALLOCATION_STREAM_MISMATCH")
            allocation_id = active.allocation_id if active else _identity([self.account_fingerprint,symbol,"15m",forecast_id])
            if not active:
                previous_allocation = db.execute("SELECT * FROM allocations WHERE id=?",(allocation_id,)).fetchone()
                if previous_allocation is not None and retry_of_reservation_id is not None:
                    if previous_allocation["status"]!="CLOSED" or self._stream(db,"allocation",allocation_id)!=stream:
                        raise LedgerError("INTRADAY_RETRY_ALLOCATION_UNVERIFIED")
                    db.execute("UPDATE allocations SET status='ACTIVE' WHERE id=?",(allocation_id,))
                else:
                    db.execute("INSERT INTO allocations VALUES(?,?,?,?,?,?,?,'ACTIVE')",
                        (allocation_id,self.account_fingerprint,symbol,"15m",forecast_id,reference,target))
            self._intraday_schema(db)
            proof = {"forecast_sha256":forecast_sha256,"forecast_snapshot_sha256":forecast_snapshot_sha256,
                     "reference_at":reference,"target_close_at":target,"forecast_id":forecast_id}
            db.execute("INSERT OR IGNORE INTO intraday_streams VALUES('allocation',?,?,?)",(allocation_id,stream,_encoded(proof)))
            reservation = self._insert_reservation(db,allocation_id,side,quantity,price,key,batch,request)
            db.execute("INSERT INTO intraday_streams VALUES('reservation',?,?,?)",(reservation.reservation_id,stream,_encoded(proof)))
            if side=="SELL":
                held = int(_number(portfolio["held_shares"].get(symbol,0)))
                local_orders = [r for r in state.reservations if r.symbol==symbol and r.side=="SELL" and r.reserved_quantity]
                local_pending = sum(r.reserved_quantity for r in local_orders)
                represented = 0
                if exact_pending is not None:
                    for local in local_orders:
                        if local.broker_order_id in exact_pending:
                            if exact_pending[local.broker_order_id]!=local.reserved_quantity:
                                raise LedgerError("BROKER_RESERVATION_DOES_NOT_MATCH_LOCAL_ORDER")
                            represented += local.reserved_quantity
                unmatched_broker_pending = external_pending-represented
                if quantity>max(0,held-local_pending-unmatched_broker_pending):
                    raise LedgerError("INTRADAY_SELL_EXCEEDS_ACCOUNT_ELIGIBLE_SHARES")
                remaining,ordinal = quantity,0
                for owner in sorted((a for a in state.allocations if a.symbol==symbol),key=lambda a:a.allocation_id):
                    take = min(remaining,max(0,owner.filled_shares-owner.reserved_sell_shares))
                    if take:
                        db.execute("INSERT INTO intraday_sell_slices VALUES(?,?,?,?)",(reservation.reservation_id,owner.allocation_id,take,ordinal))
                        remaining -= take; ordinal += 1
                    if not remaining:
                        break
                if remaining:
                    owned = sum(a.filled_shares for a in state.allocations if a.symbol==symbol)
                    free = held-owned-unmatched_broker_pending
                    if remaining>free:
                        raise LedgerError("INTRADAY_SHARE_ACCOUNTING_DOES_NOT_BALANCE")
                    db.execute("CREATE TABLE IF NOT EXISTS inventory_assignments(id TEXT PRIMARY KEY,allocation TEXT NOT NULL REFERENCES allocations(id),snapshot_id TEXT NOT NULL REFERENCES snapshots(id),quantity INTEGER NOT NULL,observed_at TEXT NOT NULL)")
                    assignment_id = _identity(["gameplan-observed-opening-stock",key])
                    db.execute("INSERT INTO inventory_assignments VALUES(?,?,?,?,?)",(assignment_id,allocation_id,snapshot_id,remaining,now))
                    self._save_evidence(db,assignment_id,"intraday-observed-account-stock-assignment",{
                        "allocation_id":allocation_id,"snapshot_id":snapshot_id,"quantity":remaining,"observed_at":now,
                        "reason":"Observed unallocated stock attributed for account-wide 15m sale; not a BUY or fill"})
                    db.execute("INSERT INTO intraday_sell_slices VALUES(?,?,?,?)",(reservation.reservation_id,allocation_id,remaining,ordinal))
            self._save_evidence(db,"intraday-proof:"+reservation.reservation_id,"intraday-forecast-binding",proof)
            return reservation

    @classmethod
    def _intraday_retry_parent(cls, db, request):
        """No new attempt can replace a possible broker submission or a fill."""
        previous = cls._reservation(db,request["retry_of_reservation_id"])
        original = json.loads(db.execute("SELECT request FROM reservations WHERE id=?",(previous.reservation_id,)).fetchone()[0])
        fields = ("kind","stream","symbol","horizon","forecast","side","start","end",
                  "forecast_sha256","forecast_snapshot_sha256","local_symbols")
        if any(original.get(k)!=request[k] for k in fields) or cls._stream(db,"reservation",previous.reservation_id) not in {"atlas-15m", "scout-15m"}:
            raise LedgerError("INTRADAY_RETRY_PREDECESSOR_SCOPE_MISMATCH")
        predecessors = []
        account = db.execute("SELECT account FROM allocations WHERE id=?",(previous.allocation_id,)).fetchone()[0]
        for row in db.execute("SELECT r.id,r.request FROM reservations r JOIN allocations a ON a.id=r.allocation WHERE a.account=? AND a.symbol=? ORDER BY r.rowid",
                              (account,request["symbol"])):
            candidate = json.loads(row["request"])
            if candidate.get("stream") in {"atlas-15m", "scout-15m"} and candidate.get("forecast")==request["forecast"]:
                predecessors.append(cls._reservation(db,row["id"]))
        if (not predecessors or predecessors[-1].reservation_id!=previous.reservation_id
                or any(order.status!="REJECTED" or order.broker_order_id is not None or order.filled_quantity
                       or order.submitted_at is not None for order in predecessors)):
            raise LedgerError("INTRADAY_RETRY_REQUIRES_LATEST_PROVEN_UNSUBMITTED_ATTEMPT")
        return previous

    def history(self) -> tuple[AuditRecord, ...]:
        """Read immutable evidence records; payloads never contain raw account IDs."""
        with self._transaction() as db:
            return tuple(AuditRecord(*row) for row in db.execute("SELECT id,kind,payload FROM evidence ORDER BY rowid"))

    def reserve_cancellation(self, reservation_id: str, *, as_of: str, evidence_id: str) -> bool:
        """Reserve at most one cancellation attempt for a known expired entry.

        This does not cancel an order or release shares/cash. The caller must
        revalidate identity/authority before a broker call, and reconcile its
        actual result with complete OrderEvidence even after an uncertain call.
        """
        now = _utc(as_of)
        with self._transaction() as db:
            order = self._reservation(db, reservation_id)
            if self._stream(db,"reservation",reservation_id) in {"atlas-15m", "scout-15m"}:
                raise LedgerError("CANCELLATION_STREAM_MISMATCH")
            if order.cancel_requested_at is not None:
                return False
            if order.side != "BUY" or order.status not in {"SUBMITTED", "WORKING", "PARTIAL"} or not order.broker_order_id:
                raise LedgerError("CANCELLATION_REQUIRES_KNOWN_WORKING_ENTRY")
            if order.reserved_quantity <= 0:
                raise LedgerError("NO_UNFILLED_ENTRY_QUANTITY")
            allocation = db.execute("SELECT start FROM allocations WHERE id=?", (order.allocation_id,)).fetchone()
            start = datetime.fromisoformat(allocation["start"])
            local = start.astimezone(ZoneInfo("America/Los_Angeles"))
            grace = 600 if local.hour == 13 and local.minute == 0 else 300
            if datetime.fromisoformat(now) < start + timedelta(seconds=grace):
                raise LedgerError("ENTRY_CANCELLATION_GRACE_NOT_EXPIRED")
            self._save_evidence(db, evidence_id, "cancellation-reservation",
                {"reservation_id": reservation_id, "broker_order_id": order.broker_order_id, "requested_at": now})
            db.execute("INSERT INTO cancellations VALUES (?,?)", (reservation_id, now))
            return True

    def reserve_order_timeout(self, reservation_id: str, *, as_of: str, evidence_id: str) -> bool:
        """Reserve one five-minute cancellation without releasing any inventory.

        Both owned buy and sell limits remain reserved until exact terminal
        broker evidence arrives, including after an ambiguous cancellation call.
        """
        now = _utc(as_of)
        with self._transaction() as db:
            order = self._reservation(db, reservation_id)
            if self._stream(db,"reservation",reservation_id) in {"atlas-15m", "scout-15m"}:
                raise LedgerError("CANCELLATION_STREAM_MISMATCH")
            self._account(db.execute("SELECT account FROM allocations WHERE id=?", (order.allocation_id,)).fetchone()[0])
            if order.cancel_requested_at is not None:
                return False
            if (order.side not in {"BUY", "SELL"} or order.status not in {"WORKING", "PARTIAL"}
                    or not order.broker_order_id or order.reserved_quantity <= 0):
                raise LedgerError("TIMEOUT_REQUIRES_KNOWN_WORKING_ORDER")
            if order.submitted_at is None:
                raise LedgerError("TIMEOUT_SUBMISSION_TIME_UNAVAILABLE")
            if datetime.fromisoformat(now) < datetime.fromisoformat(order.submitted_at) + timedelta(seconds=300):
                raise LedgerError("ORDER_TIMEOUT_NOT_DUE")
            self._save_evidence(db, evidence_id, "order-timeout-reservation", {
                "reservation_id": reservation_id, "broker_order_id": order.broker_order_id,
                "submitted_at": order.submitted_at, "requested_at": now,
                "reason": "UNFILLED_ORDER_FIVE_MINUTE_TIMEOUT"})
            db.execute("INSERT INTO cancellations VALUES (?,?)", (reservation_id, now))
            db.execute("INSERT INTO order_timeouts VALUES (?,?)", (reservation_id, now))
            return True

    def bind_timeout_retry(self, child_reservation_id: str, parent_reservation_id: str, *,
                           requested_quantity: int, as_of: str, evidence_id: str) -> ReservationState:
        """Bind one refreshed attempt before broker submission, atomically.

        Proven pre-POST rejections do not consume the broker retry. Any reserved,
        unknown, submitted or terminal broker attempt does. Raw links remain in
        snapshots so callers can reconstruct accounting without the new child.
        The requested quantity is the net economic retry before cash/price
        sizing; recording it avoids abandoning quantities already offset by a
        newer opposite instruction.
        """
        now = _utc(as_of)
        if type(requested_quantity) is not int or requested_quantity <= 0:
            raise LedgerError("TIMEOUT_RETRY_REQUESTED_QUANTITY_INVALID")
        with self._transaction() as db:
            child = self._reservation(db, child_reservation_id)
            parent = self._reservation(db, parent_reservation_id)
            for order in (child, parent):
                self._account(db.execute("SELECT account FROM allocations WHERE id=?", (order.allocation_id,)).fetchone()[0])
            if child.retry_parent_id is not None:
                if child.retry_parent_id != parent_reservation_id:
                    raise LedgerError("TIMEOUT_RETRY_PARENT_CHANGED")
                if child.retry_requested_quantity != requested_quantity:
                    raise LedgerError("TIMEOUT_RETRY_REQUESTED_QUANTITY_CHANGED")
                return child
            if (child.status != "RESERVED" or child.broker_order_id or child.submitted_at
                    or child_reservation_id == parent_reservation_id):
                raise LedgerError("TIMEOUT_RETRY_REQUIRES_UNSUBMITTED_CHILD")
            if (parent.status != "CANCELLED" or parent.timeout_requested_at is None
                    or parent.retry_parent_id is not None):
                raise LedgerError("TIMEOUT_RETRY_REQUIRES_CANCELLED_INITIAL_ORDER")
            if (child.symbol, child.horizon, child.side) != (parent.symbol, parent.horizon, parent.side):
                raise LedgerError("TIMEOUT_RETRY_INTENTION_MISMATCH")
            # Catch-up IDs change as fills/reservations change, but the accepted
            # plan, symbol and inventory horizon prefix must remain identical.
            parent_parts, child_parts = parent.forecast_id.split(":"), child.forecast_id.split(":")
            if (len(parent_parts) != 5 or len(child_parts) != 5 or parent_parts[0] != "catchup"
                    or not re.fullmatch(r"[a-f0-9]{64}", parent_parts[1])
                    or parent_parts[2:4] != [parent.symbol, parent.horizon]
                    or parent_parts[:4] != child_parts[:4]):
                raise LedgerError("TIMEOUT_RETRY_PLAN_FAMILY_MISMATCH")
            if (not 0 < child.quantity <= requested_quantity <= parent.quantity - parent.filled_quantity
                    or not parent.last_evidence_at or datetime.fromisoformat(now) < datetime.fromisoformat(parent.last_evidence_at)):
                raise LedgerError("TIMEOUT_RETRY_EXCEEDS_CONFIRMED_REMAINDER")
            for row in db.execute("SELECT child FROM order_timeout_retries WHERE parent=?", (parent_reservation_id,)):
                previous = self._reservation(db, row["child"])
                if not (previous.status == "REJECTED" and previous.broker_order_id is None
                        and previous.submitted_at is None and previous.filled_quantity == 0):
                    raise LedgerError("TIMEOUT_RETRY_ALREADY_RESERVED")
            self._save_evidence(db, evidence_id, "order-timeout-retry", {
                "child_reservation_id": child_reservation_id, "parent_reservation_id": parent_reservation_id,
                "linked_at": now, "requested_quantity": requested_quantity})
            db.execute("INSERT INTO order_timeout_retries VALUES (?,?,?,?)",
                       (child_reservation_id, parent_reservation_id, now, requested_quantity))
            return self._reservation(db, child_reservation_id)

    @classmethod
    def _close_empty_allocations(cls, db) -> None:
        state = cls._snapshot(db)
        for allocation in state.allocations:
            if allocation.status != "ACTIVE":
                continue
            orders = tuple(r for r in state.reservations if r.allocation_id == allocation.allocation_id)
            if allocation.filled_shares < 0:
                raise LedgerError("HORIZON_INVENTORY_WOULD_BECOME_NEGATIVE")
            if (orders and allocation.filled_shares == 0 and allocation.reserved_sell_shares == 0
                    and all(r.status in _TERMINAL for r in orders)):
                db.execute("UPDATE allocations SET status='CLOSED' WHERE id=?", (allocation.allocation_id,))

    def mark_submission(self, reservation_id: str, *, status: str, observed_at: str,
                        evidence_id: str, broker_order_id: str | None = None) -> ReservationState:
        """Record acceptance/uncertainty, never inventing broker fills.

        UNKNOWN keeps its complete reservation. A definitive pre-acceptance
        rejection releases it; cancellation/fill outcomes require OrderEvidence.
        """
        if status not in {"SUBMITTED", "UNKNOWN", "REJECTED"}:
            raise LedgerError("Unsupported submission status")
        observed = _utc(observed_at)
        if status == "SUBMITTED" and not broker_order_id:
            raise LedgerError("Accepted submission requires a broker order identity")
        payload = {"reservation_id": reservation_id, "status": status, "observed_at": observed,
                   "broker_order_id": broker_order_id}
        with self._transaction() as db:
            order = self._reservation(db, reservation_id)
            if not self._save_evidence(db, evidence_id, "submission", payload):
                return order
            if order.status not in {"RESERVED", "UNKNOWN"}:
                raise LedgerError("SUBMISSION_ALREADY_RECORDED_USE_ORDER_EVIDENCE")
            if order.broker_order_id and broker_order_id != order.broker_order_id:
                raise LedgerError("BROKER_ORDER_ID_MISMATCH")
            # An uncertain attempted submission cannot be assumed rejected.
            if status == "REJECTED" and order.status == "UNKNOWN":
                raise LedgerError("UNKNOWN_SUBMISSION_REQUIRES_BROKER_RECONCILIATION")
            db.execute("UPDATE reservations SET status=?,broker_order=?,last_evidence_at=? WHERE id=?",
                       (status, broker_order_id, observed, reservation_id))
            if status in {"SUBMITTED", "UNKNOWN"}:
                db.execute("INSERT OR IGNORE INTO order_submission_times VALUES (?,?)", (reservation_id, observed))
            self._release_unfilled_assignment(db, reservation_id, observed)
            self._close_empty_allocations(db)
            return self._reservation(db, reservation_id)

    def _apply_order(self, db, evidence: OrderEvidence) -> None:
        self._account(evidence.account_fingerprint)
        observed, status = _utc(evidence.observed_at), evidence.status
        if status not in {"UNKNOWN", "WORKING", "PARTIAL", *_TERMINAL}:
            raise LedgerError("Unsupported broker order status")
        order = self._reservation(db, evidence.reservation_id)
        payload = asdict(evidence)
        provenance, _ = validate_fill_time_provenance(evidence.fill_time_provenance, evidence.fills)
        if provenance is None:
            payload.pop("fill_time_provenance")
        else:
            payload["fill_time_provenance"] = provenance
        if evidence.broker_status_description is None:
            # Keep old normalized evidence byte-identical when no new broker
            # detail is present; never manufacture or backfill a reason.
            payload.pop("broker_status_description")
        if not self._save_evidence(db, evidence.evidence_id, "order", payload):
            return
        broker_id = _name(evidence.broker_order_id)
        if order.broker_order_id and order.broker_order_id != broker_id:
            raise LedgerError("BROKER_ORDER_ID_MISMATCH")
        if order.last_evidence_at and observed < order.last_evidence_at:
            raise LedgerError("STALE_ORDER_EVIDENCE")
        if order.status in _TERMINAL and status != order.status:
            raise LedgerError("TERMINAL_ORDER_STATUS_CANNOT_CHANGE")
        quantity = _quantity(evidence.order_quantity, positive=True)
        filled = _quantity(evidence.cumulative_filled_quantity)
        remaining = _quantity(evidence.remaining_quantity)
        if quantity != order.quantity or not order.filled_quantity <= filled <= quantity:
            raise LedgerError("ORDER_QUANTITY_OR_CUMULATIVE_FILL_MISMATCH")
        if remaining != (0 if status in _TERMINAL else quantity - filled):
            raise LedgerError("ORDER_REMAINING_QUANTITY_MISMATCH")
        if (status == "FILLED" and filled != quantity) or (status == "REJECTED" and filled):
            raise LedgerError("TERMINAL_FILL_QUANTITY_MISMATCH")
        if status == "WORKING" and filled or status == "PARTIAL" and not 0 < filled < quantity:
            raise LedgerError("WORKING_OR_PARTIAL_STATUS_MISMATCH")
        normalized = []
        seen = set()
        for fill in evidence.fills:
            identity = _name(fill.fill_id)
            if identity in seen:
                raise LedgerError("DUPLICATE_FILL_IN_CUMULATIVE_EVIDENCE")
            seen.add(identity)
            at = _utc(fill.executed_at)
            if at > observed:
                raise LedgerError("FILL_POSTDATES_ORDER_EVIDENCE")
            normalized.append((identity, order.reservation_id, _quantity(fill.quantity, positive=True),
                               str(_number(fill.price, positive=True)), at))
        if sum(fill[2] for fill in normalized) != filled:
            raise LedgerError("COMPLETE_PER_ORDER_FILL_EVIDENCE_REQUIRED")
        old_ids = {row[0] for row in db.execute("SELECT id FROM fills WHERE reservation=?", (order.reservation_id,))}
        if not old_ids.issubset(seen):
            raise LedgerError("PREVIOUS_FILL_MISSING_FROM_CUMULATIVE_EVIDENCE")
        for fill in normalized:
            previous = db.execute("SELECT * FROM fills WHERE id=?", (fill[0],)).fetchone()
            if previous is not None and tuple(previous) != fill:
                raise LedgerError("FILL_ID_REUSED_WITH_DIFFERENT_CONTENT")
            db.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?)", fill)
        db.execute("UPDATE reservations SET status=?,broker_order=?,filled=?,last_evidence_at=? WHERE id=?",
                   (status, broker_id, filled, observed, order.reservation_id))
        self._attribute_intraday_fills(db, order.reservation_id)
        self._release_unfilled_assignment(db, order.reservation_id, observed)

    def reconcile(self, portfolio: PortfolioEvidence, *, order_evidence: Sequence[OrderEvidence] = ()) -> ReconciliationResult:
        """Apply cumulative fill evidence and reconcile a subsequent fresh portfolio.

        Gather order evidence before the portfolio snapshot. Reductions not
        explained by recorded fills create a persistent block requiring review;
        the ledger never silently shrinks a horizon or claims manual inventory.
        """
        self._account(portfolio.account_fingerprint)
        snapshot_id, observed = _name(portfolio.snapshot_id), _utc(portfolio.observed_at)
        _name(portfolio.source_fingerprint)
        held = {str(k).upper(): str(_number(v)) for k, v in portfolio.held_shares.items()}
        prices = {str(k).upper(): str(_number(v, positive=True)) for k, v in portfolio.prices.items()}
        budgets = {str(k).upper(): str(_number(v)) for k, v in portfolio.symbol_budgets.items()}
        payload = {**asdict(portfolio), "observed_at": observed, "held_shares": held, "prices": prices, "symbol_budgets": budgets}
        with self._transaction() as db:
            old = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if old is not None:
                if old["payload"] != _encoded(payload) or order_evidence:
                    raise LedgerError("SNAPSHOT_ALREADY_RECONCILED")
                return ReconciliationResult(snapshot_id, bool(old["ready"]), tuple(json.loads(old["reasons"])))
            latest = db.execute("SELECT observed_at FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()
            if latest is not None and observed <= latest[0]:
                raise LedgerError("PORTFOLIO_SNAPSHOT_MUST_ADVANCE")
            for evidence in order_evidence:
                if _utc(evidence.observed_at) > observed:
                    raise LedgerError("ORDER_EVIDENCE_POSTDATES_PORTFOLIO")
                self._apply_order(db, evidence)
            self._close_empty_allocations(db)
            state = self._snapshot(db)
            reasons = []
            owned = {}
            for allocation in state.allocations:
                owned[allocation.symbol] = owned.get(allocation.symbol, 0) + allocation.filled_shares
            pending_buys, pending_sells = {}, {}
            for reservation in state.reservations:
                quantities = pending_buys if reservation.side == "BUY" else pending_sells
                quantities[reservation.symbol] = quantities.get(reservation.symbol, 0) + reservation.reserved_quantity
            previous = db.execute("SELECT rowid AS sequence,* FROM snapshots WHERE ready=1 ORDER BY observed_at DESC LIMIT 1").fetchone()
            unresolved_symbols = set()
            if previous is not None:
                before, previous_owned = json.loads(previous["payload"])["held_shares"], json.loads(previous["owned"])
                for symbol in set(before) & set(held):
                    actual_change = _number(held[symbol]) - _number(before[symbol])
                    assigned = 0
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignments'").fetchone():
                        assigned = db.execute("""SELECT COALESCE(SUM(i.quantity),0) FROM inventory_assignments i
                            JOIN allocations a ON a.id=i.allocation JOIN snapshots s ON s.id=i.snapshot_id
                            WHERE a.symbol=? AND s.rowid>=?""", (symbol, previous["sequence"])).fetchone()[0]
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignment_releases'").fetchone():
                        assigned -= db.execute("""SELECT COALESCE(SUM(r.quantity),0) FROM inventory_assignment_releases r
                            JOIN inventory_assignments i ON i.id=r.assignment
                            JOIN allocations a ON a.id=i.allocation JOIN snapshots s ON s.id=r.snapshot_id
                            WHERE a.symbol=? AND s.rowid>=?""", (symbol, previous["sequence"])).fetchone()[0]
                    tracked_change = owned.get(symbol, 0) - previous_owned.get(symbol, 0) - assigned
                    residual = actual_change - tracked_change
                    # Order history and account positions are separate broker
                    # reads. A fill between them, or a delayed account update,
                    # must not become a false manual-inventory baseline. Keep
                    # the last reconciled baseline and wait for explicit fills
                    # plus matching positions; never assign the residual shares.
                    possible_sell_or_buy_lag = pending_sells.get(symbol, 0) + max(0, tracked_change)
                    if residual < 0 and -residual <= possible_sell_or_buy_lag:
                        unresolved_symbols.add(symbol)
                    elif residual < 0:
                        db.execute("INSERT OR IGNORE INTO blocks VALUES (?,?)", (symbol, "UNEXPLAINED_MANUAL_OR_EXTERNAL_SHARE_REDUCTION"))
                    elif residual > 0 and (pending_buys.get(symbol, 0) or tracked_change < 0):
                        unresolved_symbols.add(symbol)
            for symbol, shares in owned.items():
                if symbol not in held or symbol not in prices or symbol not in budgets:
                    reasons.append(f"{symbol}:MISSING_PORTFOLIO_SYMBOL_EVIDENCE")
                elif _number(held[symbol]) < shares:
                    if symbol in unresolved_symbols or shares - _number(held[symbol]) <= pending_sells.get(symbol, 0):
                        unresolved_symbols.add(symbol)
                    else:
                        db.execute("INSERT OR IGNORE INTO blocks VALUES (?,?)", (symbol, "BROKER_SHARES_BELOW_HORIZON_INVENTORY"))
            reasons.extend(f"{symbol}:ORDER_AND_ACCOUNT_FILL_STATE_NOT_YET_RECONCILED" for symbol in sorted(unresolved_symbols))
            cutoff = (datetime.fromisoformat(observed) - timedelta(seconds=self.maximum_evidence_age_seconds)).isoformat()
            for reservation in state.reservations:
                if reservation.status in {"RESERVED", "UNKNOWN", "SUBMITTED"}:
                    reasons.append(f"{reservation.symbol}:UNRECONCILED_{reservation.status}:{reservation.reservation_id}")
                elif reservation.status in _OPEN and (not reservation.last_evidence_at or reservation.last_evidence_at < cutoff):
                    reasons.append(f"{reservation.symbol}:STALE_WORKING_ORDER_EVIDENCE:{reservation.reservation_id}")
            reasons.extend(f"{row[0]}:{row[1]}" for row in db.execute("SELECT symbol,reason FROM blocks ORDER BY symbol"))
            reasons = sorted(set(reasons))
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)", (snapshot_id, observed, _encoded(payload),
                int(not reasons), _encoded(reasons), _encoded(owned)))
            self._save_evidence(db, f"portfolio:{snapshot_id}", "portfolio", payload)
            return ReconciliationResult(snapshot_id, not reasons, tuple(reasons))

    def _require_snapshot(self, db, snapshot_id: str, now: str, *, batch_id: str | None = None):
        row = db.execute("SELECT * FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()
        if row is None or row["id"] != snapshot_id or not row["ready"]:
            raise LedgerError("CURRENT_RECONCILED_PORTFOLIO_REQUIRED")
        age = (datetime.fromisoformat(now) - datetime.fromisoformat(row["observed_at"])).total_seconds()
        if not 0 <= age <= self.maximum_evidence_age_seconds:
            raise LedgerError("STALE_OR_FUTURE_PORTFOLIO_EVIDENCE")
        if db.execute("SELECT 1 FROM blocks LIMIT 1").fetchone():
            raise LedgerError("PERSISTENT_INVENTORY_DISCREPANCY")
        for reservation in self._snapshot(db).reservations:
            if reservation.status == "UNKNOWN":
                raise LedgerError("UNKNOWN_SUBMISSION_REQUIRES_BROKER_RECONCILIATION")
            if reservation.status in {"RESERVED", "SUBMITTED"} and reservation.batch_id != batch_id:
                raise LedgerError("UNRECONCILED_PRIOR_BATCH")
        return json.loads(row["payload"])

    @staticmethod
    def _fallback_binding(action_date, policy, source_fingerprint, now):
        from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy

        normalized = validate_fallback_policy(policy, action_date)
        if normalized is None:
            raise LedgerError("FALLBACK_POLICY_NOT_ENABLED_FOR_ACTION_DATE")
        if datetime.fromisoformat(now).astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat() != action_date:
            raise LedgerError("FALLBACK_ACTION_DATE_MISMATCH")
        if not re.fullmatch(r"[a-f0-9]{64}", str(source_fingerprint)):
            raise LedgerError("FALLBACK_SOURCE_FINGERPRINT_REQUIRED")
        return normalized

    def _fallback_baseline(self, db, *, action_date, policy, source_fingerprint,
                           snapshot_id, now, batch_id=None, persist=False):
        policy = self._fallback_binding(action_date, policy, source_fingerprint, now)
        portfolio = self._require_snapshot(db, snapshot_id, now, batch_id=batch_id)
        observed_date = datetime.fromisoformat(portfolio["observed_at"]).astimezone(
            ZoneInfo("America/Los_Angeles")).date().isoformat()
        if observed_date != action_date:
            raise LedgerError("FALLBACK_BASELINE_SNAPSHOT_DATE_MISMATCH")
        previous = db.execute("SELECT * FROM fallback_days WHERE account=? AND action_date=?",
                              (self.account_fingerprint, action_date)).fetchone()
        if previous is not None:
            if previous["source_fingerprint"] != source_fingerprint or previous["policy"] != _encoded(policy):
                raise LedgerError("FALLBACK_DAY_SOURCE_OR_POLICY_MISMATCH")
            return json.loads(previous["payload"])
        state = self._snapshot(db)
        donors, symbols = {}, {}
        for allocation in state.allocations:
            if allocation.status != "ACTIVE" or allocation.horizon not in {"4h", "1d", "1w"}:
                continue
            held = _quantity(allocation.filled_shares)
            donors[allocation.allocation_id] = {"allocation_id":allocation.allocation_id,
                "symbol":allocation.symbol, "horizon":allocation.horizon,
                "owner_forecast_id":allocation.forecast_id, "initial_owned_shares":held,
                "daily_cap":held // 2}
            symbols[allocation.symbol] = symbols.get(allocation.symbol, 0) + held
        payload = {"account_fingerprint":self.account_fingerprint, "action_date":action_date,
            "source_fingerprint":source_fingerprint, "policy":policy, "snapshot_id":snapshot_id,
            "observed_at":portfolio["observed_at"], "donors":donors,
            "symbols":{symbol:{"initial_owned_longer_horizon_shares":held, "daily_cap":held // 2}
                       for symbol, held in sorted(symbols.items())}}
        payload["baseline_id"] = _identity(["fallback-day", payload])
        if persist:
            db.execute("INSERT INTO fallback_days VALUES (?,?,?,?,?,?,?)", (self.account_fingerprint,
                action_date, payload["baseline_id"], snapshot_id, source_fingerprint, _encoded(policy), _encoded(payload)))
            self._save_evidence(db, payload["baseline_id"], "fallback-day-baseline", payload)
        return payload

    def freeze_fallback_day(self, *, action_date: str, policy: Mapping, source_fingerprint: str,
                            snapshot_id: str, as_of: str, batch_id: str | None = None,
                            persist: bool = True) -> dict:
        """Freeze the first live, ready day baseline; previews never create it.

        Call immediately after the first ready live reconciliation for the date,
        before reserving normal or fallback orders. Neither new publications,
        repurchases nor restarts replenish a previously frozen day allowance.
        """
        if type(persist) is not bool:
            raise LedgerError("FALLBACK_PERSIST_FLAG_MUST_BE_BOOLEAN")
        with self._transaction() as db:
            return self._fallback_baseline(db, action_date=action_date, policy=policy,
                source_fingerprint=source_fingerprint, snapshot_id=snapshot_id, now=_utc(as_of),
                batch_id=batch_id, persist=persist)

    @staticmethod
    def _fallback_usage(db, state, action_date):
        """Filled today plus every still-reserved fallback share consumes caps.

        Prior-day pending orders remain charged until terminal broker evidence.
        Cancellation releases only the unfilled part through ordinary ledger
        status transitions. Fills are charged on their actual Pacific date.
        """
        symbols, donors = {}, {}
        for order in state.reservations:
            if order.fallback_policy_version is None:
                continue
            filled_today = sum(row["quantity"] for row in db.execute(
                "SELECT quantity,executed_at FROM fills WHERE reservation=?", (order.reservation_id,))
                if datetime.fromisoformat(row["executed_at"]).astimezone(
                    ZoneInfo("America/Los_Angeles")).date().isoformat() == action_date)
            used = filled_today + order.reserved_quantity
            symbols[order.symbol] = symbols.get(order.symbol, 0) + used
            donors[order.allocation_id] = donors.get(order.allocation_id, 0) + used
        return symbols, donors

    def _fallback_plan(self, db, *, symbol, horizon, forecast_id, target_start, target_end,
                       action_date, policy, source_fingerprint, snapshot_id, as_of, batch_id=None,
                       pending_sell_shares=0, pending_buy_shares=0, excluded_donor_horizons=(),
                       reserved_donor_shares=None, preview=False):
        from ml.stock_trader.cross_horizon_fallback import donor_horizons, slot_index, slot_quota

        symbol, horizon, forecast_id = _name(symbol).upper(), _name(horizon), _name(forecast_id)
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) or horizon not in HORIZON_WEIGHTS:
            raise LedgerError("Unsupported symbol or horizon")
        start, end, now = _utc(target_start), _utc(target_end), _utc(as_of)
        if not start <= now < end:
            raise LedgerError("FALLBACK_OUTSIDE_TRIGGER_TARGET_WINDOW")
        if type(preview) is not bool:
            raise LedgerError("FALLBACK_PREVIEW_FLAG_MUST_BE_BOOLEAN")
        policy = self._fallback_binding(action_date, policy, source_fingerprint, now)
        pending_sell, pending_buy = _number(pending_sell_shares), _number(pending_buy_shares)
        excluded = tuple(excluded_donor_horizons)
        if any(h not in HORIZON_WEIGHTS for h in excluded):
            raise LedgerError("FALLBACK_INVALID_EXCLUDED_DONOR")
        draft = {h:_quantity(q) for h,q in (reserved_donor_shares or {}).items()}
        if any(h not in HORIZON_WEIGHTS for h in draft):
            raise LedgerError("FALLBACK_INVALID_DRAFT_DONOR")
        portfolio = self._require_snapshot(db, snapshot_id, now, batch_id=batch_id)
        saved = db.execute("SELECT 1 FROM fallback_days WHERE account=? AND action_date=?",
                           (self.account_fingerprint, action_date)).fetchone()
        if saved is None and not preview:
            raise LedgerError("FALLBACK_DAY_BASELINE_REQUIRED")
        baseline = self._fallback_baseline(db, action_date=action_date, policy=policy,
            source_fingerprint=source_fingerprint, snapshot_id=snapshot_id, now=now,
            batch_id=batch_id, persist=False)
        state = self._snapshot(db)
        symbol_used, donor_used = self._fallback_usage(db, state, action_date)
        daily_cap = baseline["symbols"].get(symbol, {}).get("daily_cap", 0)
        quota = slot_quota(daily_cap, horizon, start, action_date)
        result = {"eligible":False, "reason":"NO_ELIGIBLE_LONGER_DONOR", "baseline_id":baseline["baseline_id"],
            "symbol_daily_cap":daily_cap, "symbol_used":symbol_used.get(symbol, 0),
            "symbol_remaining":max(0, daily_cap-symbol_used.get(symbol, 0)), "slot_quota":quota,
            "slot_index":None if horizon == "1w" else slot_index(horizon, start, action_date),
            "donor_allocation_id":None, "donor_horizon":None, "donor_forecast_id":None,
            "donor_remaining":0, "maximum_quantity":0, "action_date":action_date,
            "trigger_horizon":horizon, "trigger_forecast_id":forecast_id,
            "source_fingerprint":source_fingerprint, "preview":preview}
        def blocked(reason):
            return {**result, "reason":reason}
        if self._forecast_reserved(db, self.account_fingerprint, symbol, horizon, forecast_id):
            return blocked("FORECAST_ALREADY_RESERVED_NO_REENTRY")
        if any(r.fallback_action_date == action_date and r.symbol == symbol and r.trigger_horizon == horizon
               and r.target_start == start for r in state.reservations):
            return blocked("FALLBACK_SLOT_ALREADY_RESERVED_NO_ROLLOVER")
        own = [a for a in state.allocations if a.symbol == symbol and a.horizon == horizon]
        if any(a.filled_shares > 0 for a in own):
            return blocked("FALLBACK_REQUIRES_GENUINELY_ZERO_OWN_INVENTORY")
        if any(r.status in _OPEN and r.symbol == symbol and
               (r.horizon == horizon or r.trigger_horizon == horizon) for r in state.reservations):
            return blocked("FALLBACK_TRIGGER_HAS_PENDING_ORDER")
        symbol_allocations = [a for a in state.allocations if a.symbol == symbol]
        owned = sum(a.filled_shares for a in symbol_allocations)
        reserved_sells = sum(a.reserved_sell_shares for a in symbol_allocations)
        reserved_buys = sum(a.reserved_buy_shares for a in symbol_allocations)
        # Pending external sells cannot manufacture zero eligible unallocated stock.
        if pending_sell > reserved_sells or pending_buy > reserved_buys:
            return blocked("FALLBACK_EXTERNAL_PENDING_ORDER")
        if symbol not in portfolio["held_shares"] or symbol not in portfolio["prices"]:
            return blocked("MISSING_PORTFOLIO_SYMBOL_EVIDENCE")
        if _number(portfolio["held_shares"][symbol])-owned >= 1:
            return blocked("FALLBACK_REQUIRES_ZERO_ELIGIBLE_UNALLOCATED_STOCK")
        if quota <= 0:
            return blocked("FALLBACK_SLOT_QUOTA_ZERO")
        if result["symbol_remaining"] <= 0:
            return blocked("FALLBACK_SYMBOL_DAILY_CAP_EXHAUSTED")
        for donor_horizon in donor_horizons(horizon):
            if donor_horizon in excluded:
                continue
            for allocation in symbol_allocations:
                if allocation.status != "ACTIVE" or allocation.horizon != donor_horizon or allocation.reserved_buy_shares:
                    continue
                if any(r.allocation_id == allocation.allocation_id and r.status in _OPEN
                       and (batch_id is None or r.batch_id != batch_id or r.fallback_policy_version is None)
                       for r in state.reservations):
                    continue
                frozen = baseline["donors"].get(allocation.allocation_id)
                if frozen is None:
                    continue
                additional = draft.get(donor_horizon, 0)
                remaining = max(0, min(frozen["daily_cap"]-donor_used.get(allocation.allocation_id, 0)-additional,
                    allocation.filled_shares-allocation.reserved_sell_shares-additional))
                if not remaining:
                    continue
                return {**result, "eligible":True, "reason":"ELIGIBLE_HIERARCHICAL_BEARISH_FALLBACK",
                    "donor_allocation_id":allocation.allocation_id, "donor_horizon":donor_horizon,
                    "donor_forecast_id":allocation.forecast_id, "donor_daily_cap":frozen["daily_cap"],
                    "donor_used":donor_used.get(allocation.allocation_id, 0), "donor_remaining":remaining,
                    "maximum_quantity":min(quota, result["symbol_remaining"], remaining)}
        return result

    def fallback_direction_plan(self, *, symbol: str, horizon: str, forecast_id: str, target_start: str,
                                target_end: str, action_date: str, policy: Mapping, source_fingerprint: str,
                                snapshot_id: str, as_of: str, batch_id: str | None = None,
                                pending_sell_shares=0, pending_buy_shares=0, excluded_donor_horizons=(),
                                reserved_donor_shares=None, preview: bool = False) -> dict:
        """Read caps and the nearest eligible donor; never reserve or freeze a day."""
        with self._transaction() as db:
            return self._fallback_plan(db, symbol=symbol, horizon=horizon, forecast_id=forecast_id,
                target_start=target_start, target_end=target_end, action_date=action_date, policy=policy,
                source_fingerprint=source_fingerprint, snapshot_id=snapshot_id, as_of=as_of, batch_id=batch_id,
                pending_sell_shares=pending_sell_shares, pending_buy_shares=pending_buy_shares,
                excluded_donor_horizons=excluded_donor_horizons, reserved_donor_shares=reserved_donor_shares,
                preview=preview)

    def reserve_fallback_direction_exit(self, *, symbol: str, horizon: str, forecast_id: str,
                                        target_start: str, target_end: str, action_date: str, policy: Mapping,
                                        source_fingerprint: str, donor_allocation_id: str, quantity: int,
                                        limit_price, snapshot_id: str, idempotency_key: str, batch_id: str,
                                        as_of: str, pending_sell_shares=0, pending_buy_shares=0,
                                        excluded_donor_horizons=(), reserved_donor_shares=None) -> ReservationState:
        """Atomically reserve donor-owned stock and charge its triggering slot.

        The SELL belongs to the original donor allocation. Broker fill evidence
        reduces that owner's shares through the ordinary reconciliation path;
        no transfer, artificial BUY or invented forecast identity is recorded.
        """
        symbol, horizon, forecast_id = _name(symbol).upper(), _name(horizon), _name(forecast_id)
        now, start, end = _utc(as_of), _utc(target_start), _utc(target_end)
        normalized = self._fallback_binding(action_date, policy, source_fingerprint, now)
        quantity, price = _quantity(quantity, positive=True), str(_number(limit_price, positive=True))
        key, batch = _name(idempotency_key), _name(batch_id)
        request = {"kind":"fallback-direction-exit", "symbol":symbol, "forecast":forecast_id,
            "trigger_forecast":forecast_id, "trigger_horizon":horizon, "start":start, "end":end,
            "action_date":action_date, "policy":normalized, "source_fingerprint":source_fingerprint,
            "quantity":quantity, "price":price, "snapshot":snapshot_id, "batch":batch,
            "donor_allocation_id":_name(donor_allocation_id),
            "pending_sell_shares":str(_number(pending_sell_shares)),
            "pending_buy_shares":str(_number(pending_buy_shares)),
            "excluded_donor_horizons":sorted(set(excluded_donor_horizons)),
            "reserved_donor_shares":{h:_quantity(q) for h,q in sorted((reserved_donor_shares or {}).items())}}
        with self._transaction() as db:
            prior = db.execute("SELECT id,request FROM reservations WHERE idempotency_key=?", (key,)).fetchone()
            if prior is not None:
                saved_request = json.loads(prior["request"])
                for derived in ("baseline_id", "fallback_policy_version", "slot_quota", "owner_horizon", "owner_forecast"):
                    saved_request.pop(derived, None)
                if _encoded(saved_request) != _encoded(request):
                    raise LedgerError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
                return self._reservation(db, prior["id"])
            plan = self._fallback_plan(db, symbol=symbol, horizon=horizon, forecast_id=forecast_id,
                target_start=start, target_end=end, action_date=action_date, policy=normalized,
                source_fingerprint=source_fingerprint, snapshot_id=snapshot_id, as_of=now, batch_id=batch,
                pending_sell_shares=pending_sell_shares, pending_buy_shares=pending_buy_shares,
                excluded_donor_horizons=excluded_donor_horizons, reserved_donor_shares=reserved_donor_shares)
            if not plan["eligible"]:
                raise LedgerError(plan["reason"])
            if donor_allocation_id != plan["donor_allocation_id"]:
                raise LedgerError("FALLBACK_DONOR_IS_NOT_NEAREST_ELIGIBLE")
            if quantity > plan["maximum_quantity"]:
                raise LedgerError("FALLBACK_QUANTITY_EXCEEDS_SLOT_OR_DAILY_CAP")
            request.update({"baseline_id":plan["baseline_id"], "fallback_policy_version":"hierarchical-bearish-fallback-v1",
                "slot_quota":plan["slot_quota"], "owner_horizon":plan["donor_horizon"],
                "owner_forecast":plan["donor_forecast_id"]})
            return self._insert_reservation(db, donor_allocation_id, "SELL", quantity, price, key, batch, request)

    def reserve_entry(self, *, symbol: str, horizon: str, forecast_id: str, target_start: str,
                      target_end: str, quantity: int, limit_price: str | float, snapshot_id: str,
                      idempotency_key: str, batch_id: str, as_of: str,
                      allow_accumulation: bool = False) -> ReservationState:
        symbol, forecast_id = _name(symbol).upper(), _name(forecast_id)
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) or horizon not in HORIZON_WEIGHTS:
            raise LedgerError("Unsupported symbol or horizon")
        start, end, now = _utc(target_start), _utc(target_end), _utc(as_of)
        if end <= start or not start <= now < end:
            raise LedgerError("ENTRY_OUTSIDE_ITS_TARGET_WINDOW")
        quantity, price = _quantity(quantity, positive=True), str(_number(limit_price, positive=True))
        key, batch = _name(idempotency_key), _name(batch_id)
        request = {"kind": "entry", "symbol": symbol, "horizon": horizon, "forecast": forecast_id,
                   "start": start, "end": end, "quantity": quantity, "price": price,
                   "snapshot": snapshot_id, "batch": batch}
        if type(allow_accumulation) is not bool:
            raise LedgerError("Accumulation policy must be boolean")
        if allow_accumulation:
            request["allow_accumulation"] = True
        with self._transaction() as db:
            existing = self._idempotent(db, key, request)
            if existing is not None:
                return existing
            portfolio = self._require_snapshot(db, snapshot_id, now, batch_id=batch)
            if symbol not in portfolio["held_shares"] or symbol not in portfolio["prices"] or symbol not in portfolio["symbol_budgets"]:
                raise LedgerError("MISSING_SYMBOL_BUDGET_OR_QUOTE")
            cap = _number(portfolio["symbol_budgets"][symbol]) * HORIZON_WEIGHTS[horizon] / 10
            if quantity * max(_number(price), _number(portfolio["prices"][symbol])) > cap:
                raise LedgerError("HORIZON_WEIGHTED_BUDGET_EXCEEDED")
            if self._forecast_reserved(db, self.account_fingerprint, symbol, horizon, forecast_id):
                raise LedgerError("FORECAST_ALREADY_RESERVED_NO_REENTRY")
            if db.execute("SELECT 1 FROM allocations WHERE account=? AND symbol=? AND horizon=? AND forecast=?",
                          (self.account_fingerprint, symbol, horizon, forecast_id)).fetchone():
                raise LedgerError("FORECAST_ALREADY_RESERVED_NO_REENTRY")
            active = db.execute("SELECT id FROM allocations WHERE symbol=? AND horizon=? AND status='ACTIVE'", (symbol, horizon)).fetchone()
            if active:
                if not allow_accumulation:
                    raise LedgerError("HORIZON_ALREADY_HAS_ACTIVE_ALLOCATION")
                return self._insert_reservation(db, active["id"], "BUY", quantity, price, key, batch, request)
            allocation_id = _identity([self.account_fingerprint, symbol, horizon, forecast_id])
            db.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,'ACTIVE')",
                (allocation_id, self.account_fingerprint, symbol, horizon, forecast_id, start, end))
            return self._insert_reservation(db, allocation_id, "BUY", quantity, price, key, batch, request)

    @classmethod
    def _idempotent(cls, db, key: str, request: object) -> ReservationState | None:
        row = db.execute("SELECT id,request FROM reservations WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["request"] != _encoded(request):
            raise LedgerError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
        return cls._reservation(db, row["id"])

    @classmethod
    def _insert_reservation(cls, db, allocation_id, side, quantity, price, key, batch, request):
        identity = _identity([allocation_id, side, key])
        db.execute("""INSERT INTO reservations
            (id,allocation,side,quantity,price,status,idempotency_key,batch,request)
            VALUES (?,?,?,?,?,'RESERVED',?,?,?)""", (identity, allocation_id, side, quantity, price, key, batch, _encoded(request)))
        cls._save_evidence(db, f"reservation:{identity}", "reservation", request)
        return cls._reservation(db, identity)

    @staticmethod
    def _exit_due(target_end: str, now: str, exit_lead_seconds: int) -> bool:
        if not isinstance(exit_lead_seconds, int) or not 0 <= exit_lead_seconds <= 60:
            raise LedgerError("Closing exit lead must be between zero and sixty seconds")
        end = datetime.fromisoformat(target_end)
        local = end.astimezone(ZoneInfo("America/Los_Angeles"))
        lead = exit_lead_seconds if (local.hour, local.minute, local.second) == (17, 0, 0) else 0
        return datetime.fromisoformat(now) >= end - timedelta(seconds=lead)

    def due_exits(self, *, as_of: str, snapshot_id: str, exit_lead_seconds: int = 0) -> tuple[ExitPlan, ...]:
        now = _utc(as_of)
        with self._transaction() as db:
            reasons = []
            try:
                self._require_snapshot(db, snapshot_id, now)
            except LedgerError as exc:
                reasons.append(str(exc))
            plans = []
            for allocation in self._snapshot(db).allocations:
                if self._stream(db,"allocation",allocation.allocation_id) in {"atlas-15m", "scout-15m"}:
                    continue
                if allocation.status != "ACTIVE" or not self._exit_due(allocation.target_end, now, exit_lead_seconds):
                    continue
                blocked = list(reasons)
                if allocation.reserved_buy_shares:
                    blocked.append("ENTRY_ORDER_STILL_WORKING_OR_UNKNOWN")
                quantity = max(0, allocation.filled_shares - allocation.reserved_sell_shares)
                if quantity == 0:
                    blocked.append("NO_UNRESERVED_HORIZON_SHARES")
                plans.append(ExitPlan(allocation.allocation_id, allocation.symbol, allocation.horizon,
                    allocation.forecast_id, allocation.target_end, quantity, not blocked, tuple(blocked)))
            return tuple(plans)

    def reserve_exit(self, *, allocation_id: str, quantity: int, limit_price: str | float,
                     snapshot_id: str, idempotency_key: str, batch_id: str, as_of: str,
                     exit_lead_seconds: int = 0) -> ReservationState:
        now, quantity, price = _utc(as_of), _quantity(quantity, positive=True), str(_number(limit_price, positive=True))
        key, batch = _name(idempotency_key), _name(batch_id)
        request = {"kind": "exit", "allocation": allocation_id, "quantity": quantity,
                   "price": price, "snapshot": snapshot_id, "batch": batch, "exit_lead_seconds": exit_lead_seconds}
        with self._transaction() as db:
            existing = self._idempotent(db, key, request)
            if existing is not None:
                return existing
            self._require_snapshot(db, snapshot_id, now, batch_id=batch)
            allocation = next((a for a in self._snapshot(db).allocations if a.allocation_id == allocation_id), None)
            if self._stream(db,"allocation",allocation_id) in {"atlas-15m", "scout-15m"}:
                raise LedgerError("INTRADAY_HAS_NO_SCHEDULED_LIQUIDATION")
            if allocation is None or allocation.status != "ACTIVE":
                raise LedgerError("NO_ACTIVE_HORIZON_ALLOCATION")
            if not self._exit_due(allocation.target_end, now, exit_lead_seconds):
                raise LedgerError("HORIZON_EXIT_IS_NOT_DUE")
            if allocation.reserved_buy_shares:
                raise LedgerError("ENTRY_ORDER_STILL_WORKING_OR_UNKNOWN")
            if quantity > allocation.filled_shares - allocation.reserved_sell_shares:
                raise LedgerError("EXIT_EXCEEDS_OWN_UNRESERVED_HORIZON_SHARES")
            return self._insert_reservation(db, allocation_id, "SELL", quantity, price, key, batch, request)

    def reserve_direction_exit(self, *, symbol: str, horizon: str, forecast_id: str,
                               target_start: str, target_end: str, quantity: int, limit_price,
                               snapshot_id: str, idempotency_key: str, batch_id: str, as_of: str,
                               pending_sell_shares=0) -> ReservationState:
        """Reserve a user-selected Gameplan sale of currently available stock.

        Existing shares are explicitly attributed to this sale with their
        observed snapshot. Other horizon inventory and external working sells
        remain protected. Only later broker fill evidence reduces holdings.
        """
        symbol, horizon, forecast_id = _name(symbol).upper(), _name(horizon), _name(forecast_id)
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) or horizon not in HORIZON_WEIGHTS:
            raise LedgerError("Unsupported symbol or horizon")
        start, end, now = _utc(target_start), _utc(target_end), _utc(as_of)
        if not start <= now < end:
            raise LedgerError("DIRECTION_SALE_OUTSIDE_TARGET_WINDOW")
        quantity, price = _quantity(quantity, positive=True), str(_number(limit_price, positive=True))
        pending = _number(pending_sell_shares)
        key, batch = _name(idempotency_key), _name(batch_id)
        request = {"kind": "direction-exit", "symbol": symbol, "horizon": horizon, "forecast": forecast_id,
                   "start": start, "end": end, "quantity": quantity, "price": price,
                   "snapshot": snapshot_id, "batch": batch, "pending_sell_shares": str(pending)}
        with self._transaction() as db:
            existing = self._idempotent(db, key, request)
            if existing is not None:
                return existing
            portfolio = self._require_snapshot(db, snapshot_id, now, batch_id=batch)
            if self._forecast_reserved(db, self.account_fingerprint, symbol, horizon, forecast_id):
                raise LedgerError("FORECAST_ALREADY_RESERVED_NO_REENTRY")
            state = self._snapshot(db)
            owned = sum(a.filled_shares for a in state.allocations if a.symbol == symbol)
            owned_reserved = sum(a.reserved_sell_shares for a in state.allocations if a.symbol == symbol)
            free = _number(portfolio["held_shares"].get(symbol, 0)) - owned - max(Decimal(0), pending - owned_reserved)
            allocation = next((a for a in state.allocations if a.symbol == symbol and a.horizon == horizon and a.status == "ACTIVE"), None)
            if allocation and allocation.reserved_buy_shares:
                raise LedgerError("DIRECTION_SALE_HAS_PENDING_HORIZON_BUY")
            available = allocation.filled_shares - allocation.reserved_sell_shares if allocation else 0
            assigned = max(0, quantity - available)
            if assigned > max(Decimal(0), free):
                raise LedgerError("DIRECTION_SALE_EXCEEDS_AVAILABLE_HELD_STOCK")
            if allocation:
                allocation_id = allocation.allocation_id
            else:
                allocation_id = _identity([self.account_fingerprint, symbol, horizon, forecast_id])
                if db.execute("SELECT 1 FROM allocations WHERE id=?", (allocation_id,)).fetchone():
                    raise LedgerError("DIRECTION_FORECAST_ALREADY_USED")
                db.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,'ACTIVE')",
                           (allocation_id, self.account_fingerprint, symbol, horizon, forecast_id, start, end))
            if assigned:
                db.execute("""CREATE TABLE IF NOT EXISTS inventory_assignments (
                    id TEXT PRIMARY KEY, allocation TEXT NOT NULL REFERENCES allocations(id),
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(id), quantity INTEGER NOT NULL,
                    observed_at TEXT NOT NULL)""")
                assignment_id = _identity(["gameplan-observed-opening-stock", key])
                db.execute("INSERT INTO inventory_assignments VALUES (?,?,?,?,?)",
                           (assignment_id, allocation_id, snapshot_id, assigned, now))
                self._save_evidence(db, assignment_id, "gameplan-existing-stock-assignment",
                    {"allocation_id": allocation_id, "snapshot_id": snapshot_id, "quantity": assigned,
                     "observed_at": now, "reason": "User-selected Gameplan directional sale of existing stock"})
            return self._insert_reservation(db, allocation_id, "SELL", quantity, price, key, batch, request)


class OwnOrderLedger(HorizonLedger):
    """Retain this trader's orders/fills; broker positions are capacity only.

    Other account activity cannot be inferred from our fills. Consequently an
    external position movement is neither adopted as a fill nor a discrepancy
    against another trader. Exact own order evidence remains mandatory.
    """
    def reconcile(self, portfolio, *, order_evidence=()):
        self._account(portfolio.account_fingerprint)
        snapshot_id, observed = _name(portfolio.snapshot_id), _utc(portfolio.observed_at)
        _name(portfolio.source_fingerprint)
        held = {str(k).upper(): str(_number(v)) for k, v in portfolio.held_shares.items()}
        prices = {str(k).upper(): str(_number(v, positive=True)) for k, v in portfolio.prices.items()}
        budgets = {str(k).upper(): str(_number(v)) for k, v in portfolio.symbol_budgets.items()}
        payload = {**asdict(portfolio), "observed_at": observed, "held_shares": held,
                   "prices": prices, "symbol_budgets": budgets}
        with self._transaction() as db:
            old = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if old is not None:
                if old["payload"] != _encoded(payload) or order_evidence:
                    raise LedgerError("SNAPSHOT_ALREADY_RECONCILED")
                return ReconciliationResult(snapshot_id, bool(old["ready"]), tuple(json.loads(old["reasons"])))
            latest = db.execute("SELECT observed_at FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()
            if latest and observed <= latest[0]:
                raise LedgerError("PORTFOLIO_SNAPSHOT_MUST_ADVANCE")
            for evidence in order_evidence:
                if _utc(evidence.observed_at) > observed:
                    raise LedgerError("ORDER_EVIDENCE_POSTDATES_PORTFOLIO")
                self._apply_order(db, evidence)
            self._close_empty_allocations(db)
            state = self._snapshot(db)
            reasons = []
            cutoff = (datetime.fromisoformat(observed) - timedelta(seconds=self.maximum_evidence_age_seconds)).isoformat()
            for order in state.reservations:
                if order.status in {"RESERVED", "UNKNOWN", "SUBMITTED"}:
                    reasons.append(f"{order.symbol}:UNRECONCILED_{order.status}:{order.reservation_id}")
                elif order.reserved_quantity and (not order.last_evidence_at or order.last_evidence_at < cutoff):
                    reasons.append(f"{order.symbol}:STALE_WORKING_ORDER_EVIDENCE:{order.reservation_id}")
            owned = {}
            for allocation in state.allocations:
                owned[allocation.symbol] = owned.get(allocation.symbol, 0) + allocation.filled_shares
            reasons = sorted(set(reasons))
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)", (snapshot_id, observed, _encoded(payload),
                       int(not reasons), _encoded(reasons), _encoded(owned)))
            self._save_evidence(db, f"portfolio:{snapshot_id}", "portfolio", payload)
            return ReconciliationResult(snapshot_id, not reasons, tuple(reasons))
