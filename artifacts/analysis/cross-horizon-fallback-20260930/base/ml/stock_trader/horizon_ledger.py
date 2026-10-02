"""Durable inventory for independent stock horizons; this module never trades.

The caller retains broker/session/quote checks and the existing combined order,
cash and exposure limits. A reservation is not a fill. Per-order fill evidence
creates purchased inventory. The opt-in Gameplan direction policy may explicitly
attribute observed existing stock to a sale; unsold terminal quantities return
to the unallocated pool. All account identities are one-way fingerprints.
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
    def _reservation(db, reservation_id: str) -> ReservationState:
        row = db.execute("""SELECT r.*,a.symbol,a.horizon,a.forecast,c.requested_at FROM reservations r
            JOIN allocations a ON a.id=r.allocation
            LEFT JOIN cancellations c ON c.reservation=r.id WHERE r.id=?""", (reservation_id,)).fetchone()
        if row is None:
            raise LedgerError("UNKNOWN_RESERVATION")
        request = json.loads(row["request"])
        return ReservationState(row["id"], row["allocation"], row["symbol"], row["horizon"],
            request.get("forecast", row["forecast"]), row["side"], row["quantity"], row["price"], row["filled"],
            row["status"], row["broker_order"], row["idempotency_key"], row["batch"], row["last_evidence_at"], row["requested_at"],
            request.get("start"), request.get("end"))

    @staticmethod
    def _forecast_reserved(db, account, symbol, horizon, forecast):
        rows = db.execute("""SELECT r.request FROM reservations r JOIN allocations a ON a.id=r.allocation
            WHERE a.account=? AND a.symbol=? AND a.horizon=?""", (account, symbol, horizon))
        return any(json.loads(row["request"]).get("forecast") == forecast for row in rows)

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
        reservations = tuple(cls._reservation(db, row[0]) for row in db.execute("SELECT id FROM reservations ORDER BY id"))
        allocations = []
        for row in db.execute("SELECT * FROM allocations ORDER BY symbol,horizon,start,id"):
            orders = tuple(r for r in reservations if r.allocation_id == row["id"])
            held = cls._assigned_shares(db, row["id"]) + sum(r.filled_quantity * (1 if r.side == "BUY" else -1) for r in orders)
            allocations.append(AllocationState(row["id"], row["account"], row["symbol"], row["horizon"],
                row["forecast"], row["start"], row["end"], row["status"], held,
                sum(r.reserved_quantity for r in orders if r.side == "BUY"),
                sum(r.reserved_quantity for r in orders if r.side == "SELL")))
        return LedgerSnapshot(tuple(allocations), reservations,
            tuple((row[0], row[1]) for row in db.execute("SELECT symbol,reason FROM blocks ORDER BY symbol")))

    def snapshot(self) -> LedgerSnapshot:
        """Return frozen allocation/reservation records, with no live operations."""
        with self._transaction() as db:
            return self._snapshot(db)

    def lookup_reservation(self, reservation_id: str) -> ReservationState:
        with self._transaction() as db:
            return self._reservation(db, reservation_id)

    def pending_reservations(self) -> tuple[ReservationState, ...]:
        return tuple(r for r in self.snapshot().reservations if r.status in _OPEN)

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

    @classmethod
    def _close_empty_allocations(cls, db) -> None:
        for row in db.execute("SELECT id FROM allocations WHERE status='ACTIVE'").fetchall():
            orders = db.execute("SELECT side,filled,status FROM reservations WHERE allocation=?", (row[0],)).fetchall()
            owned = cls._assigned_shares(db, row[0]) + sum(r["filled"] * (1 if r["side"] == "BUY" else -1) for r in orders)
            if owned < 0:
                raise LedgerError("HORIZON_INVENTORY_WOULD_BECOME_NEGATIVE")
            if orders and owned == 0 and all(r["status"] in _TERMINAL for r in orders):
                db.execute("UPDATE allocations SET status='CLOSED' WHERE id=?", (row[0],))

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
            self._release_unfilled_assignment(db, reservation_id, observed)
            self._close_empty_allocations(db)
            return self._reservation(db, reservation_id)

    def _apply_order(self, db, evidence: OrderEvidence) -> None:
        self._account(evidence.account_fingerprint)
        observed, status = _utc(evidence.observed_at), evidence.status
        if status not in {"UNKNOWN", "WORKING", "PARTIAL", *_TERMINAL}:
            raise LedgerError("Unsupported broker order status")
        order = self._reservation(db, evidence.reservation_id)
        if not self._save_evidence(db, evidence.evidence_id, "order", asdict(evidence)):
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
