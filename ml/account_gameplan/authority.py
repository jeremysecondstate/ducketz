"""One host's durable account reservation authority, without a broker adapter.

The reviewed binding chooses one execution host. This database is host-local;
putting independent copies (or this SQLite file on a network share) on two PCs
does not provide distributed coordination. Native horizon ownership remains
authoritative. Requests must identify its real reservation and allocation.

The dedicated Atlas 15m path uses actual available cash and eligible account
shares, with all outstanding commitments retained. Native lots attribute fills;
their Gameplan horizon labels do not gate 15m capacity or sizing.

Snapshot budgets come from the existing engine, net of observed broker orders.
Only explicit matching broker IDs overlap local commitments. Filled buys remain
charged until a newer coherent balance snapshot accounts for them; filled sales
never create cash here. The caller must retain its quote, source, ownership and
execution-window checks, and invoke the supplied gate immediately before POST.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from ml.stock_trader.horizon_ledger import LedgerError, validate_fill_time_provenance


VERSION = "single-host-account-authority-v1"
SUBMISSION_RECEIPT_VERSION = "account-exact-submission-receipt-v1"
OPEN = frozenset({"RESERVED", "SUBMITTING", "SUBMITTED", "UNKNOWN", "WORKING", "PARTIAL"})
TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED"})
HORIZONS = frozenset({"1h", "4h", "1d", "1w"})
ZERO = Decimal(0)


class AuthorityError(ValueError):
    pass


class _LeaseAcquisitionFailure:
    """Only acquisition's locally constructed timings cross diagnostic boundaries."""
    def __init__(self, evidence):
        self.evidence = evidence


def lease_acquisition_failure_evidence(exc):
    saved = getattr(exc, "lease_acquisition_failure", None)
    if isinstance(saved, _LeaseAcquisitionFailure):
        return {**saved.evidence, "phases": [dict(item) for item in saved.evidence["phases"]]}
    return None


class _LeaseAcquisitionTrace:
    def __init__(self):
        self.started = self.stage_started = time.monotonic()
        self.stage, self.attempt, self.phases = "validation", 0, []

    def advance(self, stage, attempt):
        now = time.monotonic()
        self.phases.append({"attempt": self.attempt, "stage": self.stage,
                            "elapsed_seconds": round(max(0, now-self.stage_started), 6)})
        self.phases = self.phases[-20:]
        self.stage, self.attempt, self.stage_started = stage, attempt, now

    def remember(self, exc):
        now = time.monotonic()
        exc.lease_acquisition_failure = _LeaseAcquisitionFailure({
            "attempt": self.attempt, "stage": self.stage,
            "elapsed_seconds": round(max(0, now-self.started), 6),
            "stage_elapsed_seconds": round(max(0, now-self.stage_started), 6),
            "phases": [dict(item) for item in self.phases]})


class SubmissionNotStarted(AuthorityError):
    """Audited adapter's final pre-POST check stopped before the send gate."""


@dataclass(frozen=True)
class AuthorityBinding:
    account_fingerprint: str
    coordinator_id: str
    participants: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class Lease:
    token: str
    epoch: int
    holder_id: str
    expires_at: str


@dataclass(frozen=True)
class _CurrentLeaseExpiry:
    """Private proof from the same transaction that rejected this exact token."""

    lease: Lease = field(repr=False)


def matches_current_lease_expiry(exc, lease):
    saved = getattr(exc, "current_lease_expiry", None)
    return (isinstance(exc, AuthorityError) and str(exc) == "CURRENT_FENCED_LEASE_REQUIRED"
            and isinstance(saved, _CurrentLeaseExpiry) and isinstance(lease, Lease)
            and secrets.compare_digest(saved.lease.token, lease.token)
            and saved.lease.epoch == lease.epoch
            and saved.lease.holder_id == lease.holder_id
            and saved.lease.expires_at == lease.expires_at)


@dataclass(frozen=True)
class BrokerReservation:
    broker_order_id: str
    symbol: str
    side: str
    remaining_quantity: int
    cumulative_filled_quantity: int
    cash_reserved: str | float
    exposure_reserved: str | float


@dataclass(frozen=True)
class AccountSnapshot:
    snapshot_id: str
    account_fingerprint: str
    generation: str
    action_date: str
    source_fingerprints: Mapping[str, str]
    observed_at: str
    account_equity: str | float
    cash_available: str | float
    cash_budget: str | float
    gross_exposure: str | float
    gross_budget: str | float
    symbol_exposure: Mapping[str, str | float]
    symbol_budgets: Mapping[str, str | float]
    held_shares: Mapping[str, int]
    broker_pending: tuple[BrokerReservation, ...] = ()
    accounted_fills: Mapping[str, int] = field(default_factory=dict)
    evidence_fingerprint: str = ""
    identity_before: str = ""
    identity_after: str = ""
    complete: bool = False


@dataclass(frozen=True)
class ReservationRequest:
    idempotency_key: str
    snapshot_id: str
    generation: str
    action_date: str
    source_fingerprints: Mapping[str, str]
    participant_id: str
    symbol: str
    horizon: str
    forecast_id: str
    side: str
    quantity: int
    limit_price: str | float
    exposure_price: str | float
    native_reservation_id: str
    native_allocation_id: str


@dataclass(frozen=True)
class IntradayCapacitySnapshot:
    """Actual account capacity, independent of any daily Gameplan budget.

    Cash is the broker's available cash (already net of its identified working
    orders). Exact local commitments not represented there are additionally
    charged, including fills not yet accounted for by a newer balance.
    """
    snapshot_id: str
    account_fingerprint: str
    action_date: str
    observed_at: str
    cash_available: str | float
    held_shares: Mapping[str, int]
    broker_pending: tuple[BrokerReservation, ...] = ()
    accounted_fills: Mapping[str, int] = field(default_factory=dict)
    evidence_fingerprint: str = ""
    identity_before: str = ""
    identity_after: str = ""
    complete: bool = False


@dataclass(frozen=True)
class IntradayReservationRequest:
    """A distinct 15m intention bound to its immutable forecasting evidence.

    The local adapter verifies the persisted forecast bytes. This authority
    retains their hashes and exact target, and admits actual cash/shares only.
    It does not select direction, sizing, forecast eligibility or sessions.
    """
    idempotency_key: str
    snapshot_id: str
    account_fingerprint: str
    action_date: str
    participant_id: str
    symbol: str
    forecast_id: str
    side: str
    quantity: int
    limit_price: str | float
    exposure_price: str | float
    native_reservation_id: str
    native_allocation_id: str
    forecast_sha256: str
    forecast_snapshot_sha256: str
    issued_at: str
    reference_at: str
    target_close_at: str
    target_version: str
    local_symbols: tuple[str, ...]
    stream: str = "atlas-15m"
    horizon: str = "15m"
    retry_of_native_reservation_id: str | None = None


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    request: Mapping[str, object]
    status: str
    filled_quantity: int
    broker_order_id: str | None
    last_evidence_at: str | None


@dataclass(frozen=True)
class Fill:
    fill_id: str
    quantity: int
    price: str | float
    executed_at: str


@dataclass(frozen=True)
class OrderEvidence:
    evidence_id: str
    reservation_id: str
    account_fingerprint: str
    broker_order_id: str
    observed_at: str
    status: str
    order_quantity: int
    cumulative_filled_quantity: int
    remaining_quantity: int
    fills: tuple[Fill, ...]
    complete: bool = False
    fill_time_provenance: Mapping[str, object] | None = None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise AuthorityError("INVALID_IDENTITY")
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise AuthorityError("SHA256_IDENTITY_REQUIRED")
    return value


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value):
        raise AuthorityError("INVALID_SYMBOL")
    return value


def _quantity(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise AuthorityError("WHOLE_NONNEGATIVE_QUANTITY_REQUIRED")
    return value


def _money(value, *, positive=False):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AuthorityError("FINITE_NONNEGATIVE_AMOUNT_REQUIRED") from exc
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise AuthorityError("FINITE_NONNEGATIVE_AMOUNT_REQUIRED")
    return amount


def _time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("naive")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AuthorityError("UTC_TIMESTAMP_REQUIRED") from exc


def _amount(value):
    return format(_money(value).normalize(), "f")


class AccountAuthority:
    def __init__(self, path: Path, binding: AuthorityBinding, *, clock: Callable[[], datetime] | None = None):
        if str(path).startswith(("\\\\", "//")):
            raise AuthorityError("HOST_LOCAL_DATABASE_REQUIRED")
        self.path = Path(path).resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.binding = self._binding(binding)
        self.symbols = frozenset(s for symbols in self.binding["participants"].values() for s in symbols)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS lease(singleton INTEGER PRIMARY KEY CHECK(singleton=1),token TEXT NOT NULL,epoch INTEGER NOT NULL,holder TEXT NOT NULL,expires TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS plans(action_date TEXT PRIMARY KEY,generation TEXT UNIQUE NOT NULL,sources TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS plan_revisions(generation TEXT PRIMARY KEY,action_date TEXT NOT NULL,sources TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,observed TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,key TEXT UNIQUE NOT NULL,native_id TEXT UNIQUE NOT NULL,forecast_key TEXT UNIQUE NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,filled INTEGER NOT NULL,broker_id TEXT UNIQUE,created TEXT NOT NULL,last_evidence TEXT)",
                "CREATE TABLE IF NOT EXISTS order_evidence(id TEXT PRIMARY KEY,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS fills(reservation TEXT NOT NULL,fill_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(reservation,fill_id))",
                "CREATE TABLE IF NOT EXISTS submission_receipts(reservation TEXT PRIMARY KEY,broker_id TEXT,receipt_id TEXT UNIQUE NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,observed TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)",
            ):
                db.execute(statement)
            # Preserve the original date binding as historical evidence. A
            # current plan revision has its own immutable generation; it does
            # not replace earlier snapshots, reservations or order evidence.
            db.execute("INSERT OR IGNORE INTO plan_revisions(generation,action_date,sources) "
                       "SELECT generation,action_date,sources FROM plans")
            saved = db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
            value = _json({"version":VERSION, **self.binding})
            if saved and saved[0] != value:
                raise AuthorityError("IMMUTABLE_AUTHORITY_BINDING_MISMATCH")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('binding',?)", (value,))

    @staticmethod
    def _binding(binding):
        result = {"account_fingerprint":_hash(binding.account_fingerprint),
                  "coordinator_id":_name(binding.coordinator_id), "participants":{}}
        seen = set()
        if not binding.participants:
            raise AuthorityError("PARTICIPANT_REGISTRY_REQUIRED")
        for participant, symbols in sorted(binding.participants.items()):
            values = tuple(_symbol(s) for s in symbols)
            if not values or len(set(values)) != len(values) or seen.intersection(values):
                raise AuthorityError("SYMBOL_OWNERSHIP_MUST_BE_EXPLICIT_AND_DISJOINT")
            result["participants"][_name(participant)] = sorted(values)
            seen.update(values)
        if result["coordinator_id"] not in result["participants"]:
            raise AuthorityError("COORDINATOR_MUST_BE_REGISTERED")
        return result

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self, db):
        now = _time(self.clock())
        previous = db.execute("SELECT value FROM metadata WHERE key='clock'").fetchone()
        if previous and now < _time(previous[0]):
            raise AuthorityError("AUTHORITY_CLOCK_MOVED_BACKWARDS")
        db.execute("INSERT OR REPLACE INTO metadata VALUES('clock',?)", (now.isoformat(),))
        return now

    def _lease(self, db, token):
        now = self._now(db)
        row = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
        if not row or not secrets.compare_digest(row["token"], str(token)):
            raise AuthorityError("CURRENT_FENCED_LEASE_REQUIRED")
        if now >= _time(row["expires"]):
            exc = AuthorityError("CURRENT_FENCED_LEASE_REQUIRED")
            exc.current_lease_expiry = _CurrentLeaseExpiry(Lease(
                row["token"], row["epoch"], row["holder"], row["expires"]))
            raise exc
        return now, row

    @staticmethod
    def _ttl(seconds):
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise AuthorityError("LEASE_DURATION_MUST_BE_1_TO_60_SECONDS")
        return seconds

    def acquire_lease(self, *, coordinator_id: str, holder_id: str, ttl_seconds=60) -> Lease:
        trace = _LeaseAcquisitionTrace()
        try:
            if coordinator_id != self.binding["coordinator_id"]:
                raise AuthorityError("HOST_IS_NOT_THE_REVIEWED_COORDINATOR")
            holder, ttl = _name(holder_id), self._ttl(ttl_seconds)
            for attempt in range(1, 4):
                trace.advance("transaction_enter", attempt)
                with self._transaction() as db:
                    now = self._now(db)
                    previous = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
                    if previous and now < _time(previous["expires"]):
                        raise AuthorityError("LEASE_ALREADY_OWNED")
                    lease = Lease(secrets.token_hex(32), 1 if not previous else previous["epoch"]+1,
                                  holder, (now+timedelta(seconds=ttl)).isoformat())
                    db.execute("INSERT OR REPLACE INTO lease VALUES(1,?,?,?,?)",
                               (lease.token,lease.epoch,holder,lease.expires_at))
                    trace.advance("lease_event", attempt)
                    self._event(db,now,"lease-acquired",{"epoch":lease.epoch,"holder":holder})
                    # An earlier sender may have received an exact ID after its lease
                    # expired. Promote that immutable evidence only under this new fence.
                    # This new fence is still unpublished inside BEGIN IMMEDIATE;
                    # no competing owner can commit while receipt recovery runs.
                    # Grant its TTL after that work, rather than returning a lease
                    # already consumed by acquisition. Published expiry remains strict.
                    trace.advance("receipt_recovery", attempt)
                    current = self._now(db)
                    self._recover_submission_receipts(db,current,lease.epoch)
                    trace.advance("grant_timestamp", attempt)
                    granted = self._now(db)
                    lease = Lease(lease.token,lease.epoch,holder,
                                  (granted+timedelta(seconds=ttl)).isoformat())
                    db.execute("UPDATE lease SET expires=? WHERE singleton=1", (lease.expires_at,))
                    trace.advance("transaction_commit", attempt)
                trace.advance("post_commit_clock", attempt)
                observed = _time(self.clock())
                if observed < granted:
                    raise AuthorityError("AUTHORITY_CLOCK_MOVED_BACKWARDS")
                if observed < _time(lease.expires_at):
                    return lease
                # This token has never escaped acquire_lease. A slow commit
                # may consume its entire TTL: try a new epoch, using normal
                # owner exclusion again. Never revive an expired token.
            raise AuthorityError("LEASE_ACQUISITION_EXPIRED_BEFORE_RETURN")
        except BaseException as exc:
            trace.remember(exc)
            raise

    def renew_lease(self, token: str, *, ttl_seconds=60) -> Lease:
        with self._transaction() as db:
            now, row = self._lease(db,token)
            expires = (now+timedelta(seconds=self._ttl(ttl_seconds))).isoformat()
            db.execute("UPDATE lease SET expires=? WHERE singleton=1", (expires,))
            return Lease(row["token"],row["epoch"],row["holder"],expires)

    def release_lease(self, token: str):
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            db.execute("UPDATE lease SET expires=? WHERE singleton=1", (now.isoformat(),))

    @staticmethod
    def _event(db, now, kind, payload):
        db.execute("INSERT INTO events(observed,kind,payload) VALUES(?,?,?)", (now.isoformat(),kind,_json(payload)))

    def _sources(self, value):
        if set(value) != set(self.binding["participants"]):
            raise AuthorityError("ALL_REGISTERED_SOURCES_REQUIRED")
        return {participant:_hash(digest) for participant,digest in sorted(value.items())}

    def _plan(self, generation, action_date, sources, now):
        _name(generation)
        try:
            action = date.fromisoformat(action_date)
        except (TypeError, ValueError) as exc:
            raise AuthorityError("ACTION_DATE_INVALID") from exc
        if action != now.astimezone(ZoneInfo("America/Los_Angeles")).date():
            raise AuthorityError("ACTION_DATE_NOT_CURRENT")
        return self._sources(sources)

    def _snapshot(self, db, now, snapshot_id=None):
        row = db.execute("SELECT * FROM snapshots ORDER BY observed DESC LIMIT 1").fetchone()
        if not row or (snapshot_id is not None and row["id"] != snapshot_id):
            raise AuthorityError("LATEST_ACCOUNT_SNAPSHOT_REQUIRED")
        if not 0 <= (now-_time(row["observed"])).total_seconds() <= 60:
            raise AuthorityError("ACCOUNT_SNAPSHOT_STALE_OR_FUTURE")
        payload = json.loads(row["payload"])
        self._plan(payload["generation"],payload["action_date"],payload["source_fingerprints"],now)
        return payload

    def publish_snapshot(self, token: str, snapshot: AccountSnapshot):
        payload = asdict(snapshot)
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            _name(snapshot.snapshot_id)
            sources = self._plan(snapshot.generation,snapshot.action_date,snapshot.source_fingerprints,now)
            account = self.binding["account_fingerprint"]
            if (snapshot.account_fingerprint != account or snapshot.identity_before != account
                    or snapshot.identity_after != account or snapshot.complete is not True):
                raise AuthorityError("COMPLETE_COHERENT_ACCOUNT_SNAPSHOT_REQUIRED")
            _hash(snapshot.evidence_fingerprint)
            observed = _time(snapshot.observed_at)
            if not 0 <= (now-observed).total_seconds() <= 60:
                raise AuthorityError("ACCOUNT_SNAPSHOT_STALE_OR_FUTURE")
            payload.update(observed_at=observed.isoformat(),source_fingerprints=sources)
            for name in ("account_equity","cash_available","cash_budget","gross_exposure","gross_budget"):
                payload[name] = _amount(payload[name])
            equity = _money(payload["account_equity"],positive=True)
            for name in ("symbol_exposure","symbol_budgets","held_shares"):
                if set(payload[name]) != self.symbols:
                    raise AuthorityError("COMPLETE_ACCOUNT_SYMBOL_UNION_REQUIRED")
                payload[name] = {s:(_quantity(v) if name=="held_shares" else _amount(v)) for s,v in payload[name].items()}
            pending, ids = [], set()
            for original in snapshot.broker_pending:
                item = asdict(original)
                _name(item["broker_order_id"])
                _symbol(item["symbol"])
                if item["broker_order_id"] in ids or item["side"] not in {"BUY","SELL"}:
                    raise AuthorityError("AMBIGUOUS_BROKER_RESERVATIONS")
                ids.add(item["broker_order_id"])
                _quantity(item["remaining_quantity"],positive=True)
                _quantity(item["cumulative_filled_quantity"])
                for name in ("cash_reserved","exposure_reserved"):
                    item[name] = _amount(item[name])
                if item["side"]=="SELL" and any(_money(item[k]) for k in ("cash_reserved","exposure_reserved")):
                    raise AuthorityError("SALE_PROCEEDS_ARE_NOT_CAPACITY")
                pending.append(item)
            payload["broker_pending"] = sorted(pending,key=lambda p:p["broker_order_id"])
            payload["accounted_fills"] = {str(k):_quantity(v) for k,v in snapshot.accounted_fills.items()}
            pending_gross = sum((_money(p["exposure_reserved"]) for p in pending),ZERO)
            if (_money(payload["cash_budget"]) > _money(payload["cash_available"])*Decimal("0.95")
                    or _money(payload["gross_budget"]) > max(ZERO,equity*Decimal("1.30")-_money(payload["gross_exposure"])-pending_gross)):
                raise AuthorityError("ACCOUNT_ENGINE_BUDGET_EXCEEDS_EXISTING_RISK_LIMIT")
            for symbol,budget in payload["symbol_budgets"].items():
                pending_symbol = sum((_money(p["exposure_reserved"]) for p in pending if p["symbol"]==symbol),ZERO)
                cap = max(ZERO,equity*Decimal("0.15")-_money(payload["symbol_exposure"][symbol])-pending_symbol)
                if _money(budget) > cap:
                    raise AuthorityError("SYMBOL_ENGINE_BUDGET_EXCEEDS_EXISTING_RISK_LIMIT")
            orders = {row["broker_id"]:row for row in db.execute("SELECT * FROM reservations WHERE broker_id IS NOT NULL")}
            for broker_id,filled in payload["accounted_fills"].items():
                row = orders.get(broker_id)
                if not row or filled != row["filled"] or (filled and (not row["last_evidence"] or observed <= _time(row["last_evidence"]))):
                    raise AuthorityError("FILL_NOT_PROVEN_IN_NEWER_ACCOUNT_SNAPSHOT")
            for pending_order in pending:
                row = orders.get(pending_order["broker_order_id"])
                if row:
                    request = json.loads(row["request"])
                    if (row["status"] not in OPEN or pending_order["symbol"]!=request["symbol"]
                            or pending_order["side"]!=request["side"] or pending_order["cumulative_filled_quantity"]!=row["filled"]
                            or pending_order["remaining_quantity"]!=request["quantity"]-row["filled"]):
                        raise AuthorityError("BROKER_RESERVATION_DOES_NOT_MATCH_LOCAL_ORDER")
            previous = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot.snapshot_id,)).fetchone()
            if previous:
                if previous["payload"] != _json(payload):
                    raise AuthorityError("SNAPSHOT_ID_REUSED_WITH_DIFFERENT_EVIDENCE")
                return
            latest = db.execute("SELECT observed FROM snapshots ORDER BY observed DESC LIMIT 1").fetchone()
            if latest and observed <= _time(latest[0]):
                raise AuthorityError("ACCOUNT_SNAPSHOT_MUST_ADVANCE")
            reconciled = db.execute("SELECT MAX(last_evidence) FROM reservations").fetchone()[0]
            if reconciled and observed <= _time(reconciled):
                raise AuthorityError("ACCOUNT_SNAPSHOT_MUST_FOLLOW_ORDER_RECONCILIATION")
            plan = db.execute("SELECT * FROM plan_revisions WHERE generation=?", (snapshot.generation,)).fetchone()
            if plan and (plan["action_date"]!=snapshot.action_date or plan["sources"]!=_json(sources)):
                raise AuthorityError("GENERATION_SOURCE_BINDING_IS_IMMUTABLE")
            db.execute("INSERT OR IGNORE INTO plan_revisions VALUES(?,?,?)",
                       (snapshot.generation,snapshot.action_date,_json(sources)))
            db.execute("INSERT OR IGNORE INTO plans VALUES(?,?,?)", (snapshot.action_date,snapshot.generation,_json(sources)))
            db.execute("INSERT INTO snapshots VALUES(?,?,?)", (snapshot.snapshot_id,observed.isoformat(),_json(payload)))
            self._event(db,now,"account-snapshot",{"snapshot_id":snapshot.snapshot_id,"fingerprint":_sha(payload)})

    @staticmethod
    def _reservation(row):
        return Reservation(row["id"],json.loads(row["request"]),row["status"],row["filled"],row["broker_id"],row["last_evidence"])

    def reservation(self, reservation_id: str) -> Reservation:
        with self._transaction() as db:
            row = self._row(db,reservation_id)
            return self._reservation(row)

    @staticmethod
    def _row(db, reservation_id):
        row = db.execute("SELECT * FROM reservations WHERE id=?", (_name(reservation_id),)).fetchone()
        if not row:
            raise AuthorityError("RESERVATION_NOT_FOUND")
        return row

    def _remaining(self, db, snapshot):
        cash, gross = _money(snapshot["cash_budget"]), _money(snapshot["gross_budget"])
        symbols = {s:_money(v) for s,v in snapshot["symbol_budgets"].items()}
        shares = dict(snapshot["held_shares"])
        pending = {p["broker_order_id"]:p for p in snapshot["broker_pending"]}
        for item in pending.values():
            if item["side"]=="SELL" and item["symbol"] in shares:
                shares[item["symbol"]] -= item["remaining_quantity"]
        for row in db.execute("SELECT * FROM reservations"):
            request = json.loads(row["request"])
            symbol, side = request["symbol"],request["side"]
            live = request["quantity"]-row["filled"] if row["status"] in OPEN else 0
            accounted = snapshot["accounted_fills"].get(row["broker_id"],0)
            unaccounted = row["filled"]-accounted
            if unaccounted < 0:
                raise AuthorityError("SNAPSHOT_FILL_COUNT_EXCEEDS_RECONCILED_ORDER")
            represented = pending.get(row["broker_id"])
            if represented and (represented["remaining_quantity"]!=live or represented["cumulative_filled_quantity"]!=row["filled"]):
                raise AuthorityError("BROKER_RESERVATION_SNAPSHOT_REQUIRES_REFRESH")
            if side=="BUY":
                cost = live*_money(request["limit_price"])
                exposure = live*_money(request["exposure_price"])
                if represented:
                    cost = max(ZERO,cost-_money(represented["cash_reserved"]))
                    exposure = max(ZERO,exposure-_money(represented["exposure_reserved"]))
                # A working reservation represents only the still-open shares;
                # it cannot erase a fill not yet proven in a newer balance.
                cost += unaccounted*_money(request["limit_price"])
                exposure += unaccounted*_money(request["exposure_price"])
                cash -= cost
                gross -= exposure
                symbols[symbol] -= exposure
            else:
                shares[symbol] -= live+unaccounted-(represented["remaining_quantity"] if represented else 0)
        return cash,gross,symbols,shares

    def _intraday_snapshot(self, db, now, snapshot_id):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_capacity_snapshots'").fetchone():
            raise AuthorityError("INTRADAY_CAPACITY_SNAPSHOT_REQUIRED")
        row = db.execute("SELECT * FROM intraday_capacity_snapshots ORDER BY observed DESC LIMIT 1").fetchone()
        if not row or row["id"] != snapshot_id:
            raise AuthorityError("LATEST_INTRADAY_CAPACITY_SNAPSHOT_REQUIRED")
        if not 0 <= (now-_time(row["observed"])).total_seconds() <= 60:
            raise AuthorityError("INTRADAY_CAPACITY_SNAPSHOT_STALE_OR_FUTURE")
        return json.loads(row["payload"])

    def publish_intraday_snapshot(self, token: str, snapshot: IntradayCapacitySnapshot):
        """Publish coherent actual capacity without requiring a Gameplan plan.

        This adds a separate evidence table, not another account authority or
        an earmarked stream pool. Every common reservation is counted below.
        """
        payload = asdict(snapshot)
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            _name(snapshot.snapshot_id)
            account = self.binding["account_fingerprint"]
            if (snapshot.account_fingerprint != account or snapshot.identity_before != account
                    or snapshot.identity_after != account or snapshot.complete is not True):
                raise AuthorityError("COMPLETE_COHERENT_ACCOUNT_SNAPSHOT_REQUIRED")
            if snapshot.action_date != now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat():
                raise AuthorityError("ACTION_DATE_NOT_CURRENT")
            _hash(snapshot.evidence_fingerprint)
            observed = _time(snapshot.observed_at)
            if not 0 <= (now-observed).total_seconds() <= 60:
                raise AuthorityError("ACCOUNT_SNAPSHOT_STALE_OR_FUTURE")
            payload["observed_at"] = observed.isoformat()
            payload["cash_available"] = _amount(snapshot.cash_available)
            if set(snapshot.held_shares) != self.symbols:
                raise AuthorityError("COMPLETE_ACCOUNT_SYMBOL_UNION_REQUIRED")
            payload["held_shares"] = {s:_quantity(q) for s,q in snapshot.held_shares.items()}
            pending, ids = [], set()
            orders = {row["broker_id"]:row for row in db.execute("SELECT * FROM reservations WHERE broker_id IS NOT NULL")}
            for original in snapshot.broker_pending:
                item = asdict(original)
                _name(item["broker_order_id"]); _symbol(item["symbol"])
                if item["broker_order_id"] in ids or item["side"] not in {"BUY","SELL"}:
                    raise AuthorityError("AMBIGUOUS_BROKER_RESERVATIONS")
                ids.add(item["broker_order_id"])
                _quantity(item["remaining_quantity"],positive=True)
                _quantity(item["cumulative_filled_quantity"])
                for key in ("cash_reserved","exposure_reserved"):
                    item[key] = _amount(item[key])
                if item["side"] == "SELL" and any(_money(item[k]) for k in ("cash_reserved","exposure_reserved")):
                    raise AuthorityError("SALE_PROCEEDS_ARE_NOT_CAPACITY")
                row = orders.get(item["broker_order_id"])
                if row:
                    request = json.loads(row["request"])
                    if (row["status"] not in OPEN or item["symbol"]!=request["symbol"]
                            or item["side"]!=request["side"] or item["cumulative_filled_quantity"]!=row["filled"]
                            or item["remaining_quantity"]!=request["quantity"]-row["filled"]):
                        raise AuthorityError("BROKER_RESERVATION_DOES_NOT_MATCH_LOCAL_ORDER")
                pending.append(item)
            payload["broker_pending"] = sorted(pending,key=lambda p:p["broker_order_id"])
            payload["accounted_fills"] = {str(k):_quantity(v) for k,v in snapshot.accounted_fills.items()}
            for broker_id,filled in payload["accounted_fills"].items():
                row = orders.get(broker_id)
                if not row or filled!=row["filled"] or (filled and (not row["last_evidence"] or observed<=_time(row["last_evidence"]))):
                    raise AuthorityError("FILL_NOT_PROVEN_IN_NEWER_ACCOUNT_SNAPSHOT")
            reconciled = db.execute("SELECT MAX(last_evidence) FROM reservations").fetchone()[0]
            if reconciled and observed <= _time(reconciled):
                raise AuthorityError("ACCOUNT_SNAPSHOT_MUST_FOLLOW_ORDER_RECONCILIATION")
            db.execute("CREATE TABLE IF NOT EXISTS intraday_capacity_snapshots(id TEXT PRIMARY KEY,observed TEXT NOT NULL,payload TEXT NOT NULL)")
            prior = db.execute("SELECT payload FROM intraday_capacity_snapshots WHERE id=?",(snapshot.snapshot_id,)).fetchone()
            if prior:
                if prior[0] != _json(payload):
                    raise AuthorityError("SNAPSHOT_ID_REUSED_WITH_DIFFERENT_EVIDENCE")
                return
            latest = db.execute("SELECT MAX(observed) FROM intraday_capacity_snapshots").fetchone()[0]
            if latest and observed <= _time(latest):
                raise AuthorityError("ACCOUNT_SNAPSHOT_MUST_ADVANCE")
            db.execute("INSERT INTO intraday_capacity_snapshots VALUES(?,?,?)",(snapshot.snapshot_id,observed.isoformat(),_json(payload)))
            self._event(db,now,"intraday-capacity-snapshot",{"snapshot_id":snapshot.snapshot_id,"fingerprint":_sha(payload)})

    def _intraday_remaining(self, db, snapshot):
        # _remaining's commitment accounting is shared, but its engine budgets
        # are not. Populate only neutral values to reuse exact overlap/fill math.
        neutral = {**snapshot,"cash_budget":snapshot["cash_available"],"gross_budget":"0",
                   "symbol_budgets":{s:"0" for s in self.symbols}}
        cash, _, _, shares = self._remaining(db,neutral)
        return cash,shares

    def available_intraday_capacity(self, token: str, snapshot_id: str):
        """Return available capacity; callers still persist zero-sized intent."""
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            snapshot = self._intraday_snapshot(db,now,snapshot_id)
            cash,shares = self._intraday_remaining(db,snapshot)
            return {"cash_available":_amount(max(ZERO,cash)),
                    "eligible_shares":{s:max(0,q) for s,q in shares.items()}}

    def reserve_intraday(self, token: str, request: IntradayReservationRequest) -> Reservation:
        """Admit an already sized 15m order against the one actual account pool.

        No Gameplan allocation, weighted cap, reserve fraction, equity exposure
        cap or due instruction is a 15m funding gate. Unknown commitments keep
        their complete quantities; they are never guessed away or resubmitted.
        """
        payload = asdict(request)
        if request.retry_of_native_reservation_id is None:
            payload.pop("retry_of_native_reservation_id")
        else:
            _name(request.retry_of_native_reservation_id)
        for name in ("idempotency_key","snapshot_id","participant_id","forecast_id","native_reservation_id","native_allocation_id"):
            _name(payload[name])
        _symbol(request.symbol)
        _hash(request.account_fingerprint); _hash(request.forecast_sha256); _hash(request.forecast_snapshot_sha256)
        if request.forecast_id!=request.forecast_sha256:
            raise AuthorityError("IMMUTABLE_INTRADAY_FORECAST_HASH_REQUIRED")
        if (request.account_fingerprint!=self.binding["account_fingerprint"] or request.stream not in {"atlas-15m", "scout-15m"} or request.horizon!="15m"
                or request.stream != {"pc-original":"atlas-15m", "pc-new":"scout-15m"}.get(request.participant_id)
                or request.participant_id!=self.binding["coordinator_id"] or request.side not in {"BUY","SELL"}
                or len(request.local_symbols)!=len(set(request.local_symbols))
                or set(request.local_symbols)!=set(self.binding["participants"].get(request.participant_id,()))
                or request.symbol not in request.local_symbols):
            raise AuthorityError("REQUEST_OUTSIDE_REVIEWED_INTRADAY_SCOPE")
        _quantity(request.quantity,positive=True)
        reference, issued, target = (_time(request.reference_at),_time(request.issued_at),_time(request.target_close_at))
        if (request.target_version!="intraday-15m-open-to-third-close-not-down-v1"
                or reference.minute%15 or reference.second or reference.microsecond
                or target!=reference+timedelta(minutes=45) or issued<reference+timedelta(minutes=15)):
            raise AuthorityError("EXACT_INTRADAY_TARGET_REQUIRED")
        payload.update(reference_at=reference.isoformat(),issued_at=issued.isoformat(),target_close_at=target.isoformat())
        payload["local_symbols"] = sorted(request.local_symbols)
        for name in ("limit_price","exposure_price"):
            amount = _money(payload[name],positive=True)
            if amount != amount.quantize(Decimal(".01")):
                raise AuthorityError("CENT_ROUNDED_ORDER_PRICE_REQUIRED")
            payload[name] = _amount(amount)
        if _money(payload["exposure_price"])<_money(payload["limit_price"]):
            raise AuthorityError("EXPOSURE_PRICE_MUST_COVER_LIMIT_PRICE")
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            prior = db.execute("SELECT * FROM reservations WHERE key=?",(request.idempotency_key,)).fetchone()
            if prior:
                if prior["request"]!=_json(payload):
                    raise AuthorityError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
                return self._reservation(prior)
            if (request.action_date!=now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
                    or reference.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()!=request.action_date
                    or not issued<=now<target):
                raise AuthorityError("INTRADAY_FORECAST_NOT_CURRENT")
            snapshot = self._intraday_snapshot(db,now,request.snapshot_id)
            if request.action_date!=snapshot["action_date"]:
                raise AuthorityError("RESERVATION_SOURCE_BINDING_MISMATCH")
            cash,shares = self._intraday_remaining(db,snapshot)
            prior_attempt = self._intraday_retry_parent(db,payload) if request.retry_of_native_reservation_id is not None else None
            if request.side=="BUY" and request.quantity*_money(payload["limit_price"])>cash:
                raise AuthorityError("ACCOUNT_AVAILABLE_CASH_EXHAUSTED")
            if request.side=="SELL" and request.quantity>shares[request.symbol]:
                raise AuthorityError("ACCOUNT_SELL_INVENTORY_EXHAUSTED")
            identity = _sha([self.binding["account_fingerprint"],request.idempotency_key])
            forecast_key = _sha([request.action_date,request.participant_id,request.symbol,"15m",request.forecast_id])
            if prior_attempt is not None:
                forecast_key = _sha([forecast_key,"proven-never-submitted-retry",prior_attempt["id"]])
            try:
                db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,'RESERVED',0,NULL,?,NULL)",
                    (identity,request.idempotency_key,request.native_reservation_id,forecast_key,_json(payload),now.isoformat()))
            except sqlite3.IntegrityError as exc:
                raise AuthorityError("FORECAST_OR_NATIVE_RESERVATION_ALREADY_REGISTERED") from exc
            self._event(db,now,"intraday-reserved",{"reservation_id":identity,"request":payload})
            return self._reservation(self._row(db,identity))

    @staticmethod
    def _intraday_retry_parent(db, request):
        """Only the latest proven no-POST predecessor admits a fresh attempt.

        Native validates its own REJECTED/no-ID/no-fill record separately under
        the same caller lock. Authority proves its matching NOT_SUBMITTED record.
        Price, clipped quantity and capacity snapshot may change; forecasting
        evidence and direction do not. Every preceding attempt remains stored.
        """
        native_id = request["retry_of_native_reservation_id"]
        prior = db.execute("SELECT * FROM reservations WHERE native_id=?",(native_id,)).fetchone()
        identity_fields = ("account_fingerprint","action_date","participant_id","symbol","forecast_id","side","stream","horizon",
                           "forecast_sha256","forecast_snapshot_sha256","issued_at","reference_at","target_close_at","target_version","local_symbols")
        if prior is None or any(json.loads(prior["request"]).get(k)!=request[k] for k in identity_fields):
            raise AuthorityError("INTRADAY_RETRY_PREDECESSOR_SCOPE_MISMATCH")
        predecessors = []
        for row in db.execute("SELECT * FROM reservations ORDER BY rowid"):
            original = json.loads(row["request"])
            if (original.get("stream")==request["stream"] and original.get("participant_id")==request["participant_id"]
                    and original.get("symbol")==request["symbol"] and original.get("forecast_id")==request["forecast_id"]):
                predecessors.append(row)
        if (not predecessors or predecessors[-1]["id"]!=prior["id"]
                or any(row["status"]!="NOT_SUBMITTED" or row["broker_id"] is not None or row["filled"]
                       for row in predecessors)):
            raise AuthorityError("INTRADAY_RETRY_REQUIRES_LATEST_PROVEN_UNSUBMITTED_ATTEMPT")
        return prior

    def _submission_snapshot(self, db, now, request):
        if request.get("stream") in {"atlas-15m", "scout-15m"}:
            if not _time(request["issued_at"])<=now<_time(request["target_close_at"]):
                raise AuthorityError("INTRADAY_FORECAST_NOT_CURRENT")
            return self._intraday_snapshot(db,now,request["snapshot_id"])
        return self._snapshot(db,now,request["snapshot_id"])

    def available_capacity(self, token: str, snapshot_id: str):
        """Expose this Gameplan trader's exact conservative reservation limits.

        The caller sizes before creating a native reservation. Final reserve
        still checks the same snapshot and commitments atomically.
        """
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            snapshot = self._snapshot(db,now,snapshot_id)
            cash,gross,symbols,shares = self._remaining(db,snapshot)
            return {"cash_available":_amount(max(ZERO,cash)),
                    "gross_available":_amount(max(ZERO,gross)),
                    "symbol_available":{s:_amount(max(ZERO,v)) for s,v in symbols.items()},
                    "eligible_shares":{s:max(0,q) for s,q in shares.items()}}

    def reserve(self, token: str, request: ReservationRequest) -> Reservation:
        payload = asdict(request)
        for name in ("idempotency_key","snapshot_id","generation","participant_id","forecast_id","native_reservation_id","native_allocation_id"):
            _name(payload[name])
        _symbol(request.symbol)
        if (request.symbol not in self.binding["participants"].get(request.participant_id,())
                or request.horizon not in HORIZONS or request.side not in {"BUY","SELL"}):
            raise AuthorityError("REQUEST_OUTSIDE_REVIEWED_PARTICIPANT_SCOPE")
        _quantity(request.quantity,positive=True)
        for name in ("limit_price","exposure_price"):
            amount = _money(payload[name],positive=True)
            if amount != amount.quantize(Decimal("0.01")):
                raise AuthorityError("CENT_ROUNDED_ORDER_PRICE_REQUIRED")
            payload[name] = _amount(amount)
        if _money(payload["exposure_price"]) < _money(payload["limit_price"]):
            raise AuthorityError("EXPOSURE_PRICE_MUST_COVER_LIMIT_PRICE")
        payload["source_fingerprints"] = self._sources(request.source_fingerprints)
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            self._plan(request.generation,request.action_date,request.source_fingerprints,now)
            prior = db.execute("SELECT * FROM reservations WHERE key=?", (request.idempotency_key,)).fetchone()
            if prior:
                if prior["request"] != _json(payload):
                    raise AuthorityError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
                return self._reservation(prior)
            snapshot = self._snapshot(db,now,request.snapshot_id)
            if any(payload[k]!=snapshot[k] for k in ("generation","action_date","source_fingerprints")):
                raise AuthorityError("RESERVATION_SOURCE_BINDING_MISMATCH")
            if db.execute("SELECT 1 FROM reservations WHERE status IN ('SUBMITTING','UNKNOWN') LIMIT 1").fetchone():
                raise AuthorityError("UNCERTAIN_SUBMISSION_REQUIRES_RECONCILIATION")
            cash,gross,symbols,shares = self._remaining(db,snapshot)
            notional = request.quantity*_money(payload["limit_price"])
            exposure = request.quantity*_money(payload["exposure_price"])
            if exposure > _money(snapshot["account_equity"])*Decimal("0.05"):
                raise AuthorityError("EXISTING_SINGLE_ORDER_LIMIT_EXCEEDED")
            if request.side=="BUY" and (notional>cash or exposure>gross or exposure>symbols[request.symbol]):
                raise AuthorityError("ACCOUNT_RESERVATION_BUDGET_EXHAUSTED")
            if request.side=="SELL" and request.quantity>shares[request.symbol]:
                raise AuthorityError("ACCOUNT_SELL_INVENTORY_EXHAUSTED")
            identity = _sha([self.binding["account_fingerprint"],request.idempotency_key])
            forecast_key = _sha([request.action_date,request.participant_id,request.symbol,request.horizon,request.forecast_id])
            try:
                db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,'RESERVED',0,NULL,?,NULL)",
                    (identity,request.idempotency_key,request.native_reservation_id,forecast_key,_json(payload),now.isoformat()))
            except sqlite3.IntegrityError as exc:
                raise AuthorityError("FORECAST_OR_NATIVE_RESERVATION_ALREADY_REGISTERED") from exc
            self._event(db,now,"reserved",{"reservation_id":identity,"request":payload})
            return self._reservation(self._row(db,identity))

    def abandon_unsubmitted(self, token: str, reservation_id: str):
        """Release only a reservation that has never reached SUBMITTING."""
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            row = self._row(db,reservation_id)
            if row["status"]=="NOT_SUBMITTED":
                return self._reservation(row)
            if row["status"]!="RESERVED":
                raise AuthorityError("POSSIBLE_BROKER_SUBMISSION_CANNOT_BE_ABANDONED")
            db.execute("UPDATE reservations SET status='NOT_SUBMITTED' WHERE id=?", (reservation_id,))
            self._event(db,now,"not-submitted",{"reservation_id":reservation_id})
            return self._reservation(self._row(db,reservation_id))

    @staticmethod
    def _submission_receipt_value(row):
        if row is None:
            return None
        try:
            value = json.loads(row["payload"])
            fields = {"version","reservation_id","account_fingerprint","request_sha256",
                      "broker_order_id","outcome","gate_used","no_send_error_type",
                      "submission_epoch","submission_holder_id","submission_started_at",
                      "gate_epoch","gate_holder_id","gate_at","received_at"}
            if (not isinstance(value,dict) or set(value)!=fields
                    or value["version"]!=SUBMISSION_RECEIPT_VERSION
                    or _sha(value)!=row["receipt_id"] or value["reservation_id"]!=row["reservation"]
                    or value["broker_order_id"]!=row["broker_id"] or value["received_at"]!=row["received_at"]):
                raise AuthorityError("IMMUTABLE_SUBMISSION_RECEIPT_CHANGED")
            _name(value["reservation_id"]); _name(value["submission_holder_id"])
            _hash(value["account_fingerprint"]); _hash(value["request_sha256"])
            _quantity(value["submission_epoch"],positive=True)
            start, received = _time(value["submission_started_at"]),_time(value["received_at"])
            if start > received:
                raise AuthorityError("SUBMISSION_RECEIPT_TIME_ORDER_INVALID")
            if value["outcome"]=="ACCEPTED":
                if (value["gate_used"] is not True or value["no_send_error_type"] is not None
                        or not isinstance(value["broker_order_id"],str)
                        or not re.fullmatch(r"[0-9]+",value["broker_order_id"])):
                    raise AuthorityError("SUBMISSION_RECEIPT_OUTCOME_INVALID")
                _name(value["gate_holder_id"]); _quantity(value["gate_epoch"],positive=True)
                if (value["gate_epoch"]!=value["submission_epoch"]
                        or value["gate_holder_id"]!=value["submission_holder_id"]
                        or not start<=_time(value["gate_at"])<=received):
                    raise AuthorityError("SUBMISSION_RECEIPT_TIME_OR_GATE_INVALID")
            elif value["outcome"]=="NOT_SUBMITTED":
                if (value["gate_used"] is not False or value["broker_order_id"] is not None
                        or value["no_send_error_type"]!="SubmissionNotStarted"
                        or any(value[field] is not None for field in ("gate_epoch","gate_holder_id","gate_at"))):
                    raise AuthorityError("SUBMISSION_RECEIPT_OUTCOME_INVALID")
            else:
                raise AuthorityError("SUBMISSION_RECEIPT_OUTCOME_INVALID")
            return value
        except (KeyError,TypeError,json.JSONDecodeError) as exc:
            raise AuthorityError("IMMUTABLE_SUBMISSION_RECEIPT_CHANGED") from exc

    def submission_receipt(self, reservation_id: str):
        """Read exact callback evidence; this does not promote order status."""
        _name(reservation_id)
        with self._transaction() as db:
            return self._submission_receipt_value(db.execute(
                "SELECT * FROM submission_receipts WHERE reservation=?",(reservation_id,)).fetchone())

    def _record_submission_receipt(self, reservation_id, broker_id, gate, *, submission, outcome="ACCEPTED"):
        """Append trusted returned identity without renewing an expired sender.

        Accepted identity requires submit's successfully invoked final gate.
        Audited no-send evidence is allowed only before that gate was used.
        This transaction changes no lease, reservation, capacity or fill state.
        A known response must survive a later fenced-projection failure.
        """
        with self._transaction() as db:
            row = self._row(db,reservation_id)
            received = _time(self.clock())
            if (_sha(json.loads(row["request"]))!=submission["request_sha256"]
                    or _time(row["created"])>_time(submission["submission_started_at"])
                    or received<_time(submission["submission_started_at"])):
                raise AuthorityError("SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH")
            gate = gate or {"gate_epoch":None,"gate_holder_id":None,"gate_at":None}
            value = {"version":SUBMISSION_RECEIPT_VERSION,"reservation_id":reservation_id,
                     "account_fingerprint":self.binding["account_fingerprint"],"broker_order_id":broker_id,
                     "outcome":outcome,"gate_used":outcome=="ACCEPTED",
                     "no_send_error_type":"SubmissionNotStarted" if outcome=="NOT_SUBMITTED" else None,
                     "received_at":received.isoformat(),**submission,**gate}
            probe = dict(reservation=reservation_id,broker_id=broker_id,receipt_id=_sha(value),
                         received_at=value["received_at"],payload=_json(value))
            self._submission_receipt_value(probe)
            old = self._submission_receipt_value(db.execute(
                "SELECT * FROM submission_receipts WHERE reservation=?",(reservation_id,)).fetchone())
            if old is not None:
                if any(old[field]!=value[field] for field in value if field!="received_at"):
                    raise AuthorityError("IMMUTABLE_SUBMISSION_RECEIPT_CHANGED")
                return old
            try:
                db.execute("INSERT INTO submission_receipts VALUES(?,?,?,?,?)",
                    (reservation_id,broker_id,_sha(value),value["received_at"],_json(value)))
            except sqlite3.IntegrityError as exc:
                raise AuthorityError("BROKER_ORDER_ID_ALREADY_RECEIVED") from exc
            self._event(db,received,"submission-receipt-recorded",{
                "reservation_id":reservation_id,"receipt_id":_sha(value),"broker_order_id":broker_id,
                "outcome":outcome})
            return value

    def _recover_submission_receipts(self, db, now, epoch):
        """Caller must hold the current fence inside this same transaction."""
        recovered = []
        for raw in db.execute("SELECT * FROM submission_receipts ORDER BY rowid").fetchall():
            receipt = self._submission_receipt_value(raw)
            row = self._row(db,receipt["reservation_id"])
            if (receipt["account_fingerprint"]!=self.binding["account_fingerprint"]
                    or receipt["request_sha256"]!=_sha(json.loads(row["request"]))
                    or receipt["submission_epoch"]>epoch or _time(receipt["received_at"])>now
                    or _time(row["created"])>_time(receipt["submission_started_at"])):
                raise AuthorityError("SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH")
            if receipt["outcome"]=="NOT_SUBMITTED":
                if row["broker_id"] is not None or row["filled"]:
                    raise AuthorityError("SUBMISSION_RECEIPT_STATE_CONFLICT")
                if row["status"]=="NOT_SUBMITTED":
                    continue
                if row["status"] not in {"SUBMITTING","UNKNOWN"}:
                    raise AuthorityError("SUBMISSION_RECEIPT_STATE_CONFLICT")
                db.execute("UPDATE reservations SET status='NOT_SUBMITTED' WHERE id=?",(row["id"],))
                self._event(db,now,"not-submitted",{"reservation_id":row["id"],
                                                     "submission_receipt_id":raw["receipt_id"]})
                recovered.append(self._reservation(self._row(db,row["id"])))
                continue
            if row["broker_id"] is not None:
                if row["broker_id"]!=receipt["broker_order_id"]:
                    raise AuthorityError("SUBMISSION_RECEIPT_BROKER_ID_CONFLICT")
                continue  # Complete status/fill evidence is never downgraded.
            if row["status"] not in {"SUBMITTING","UNKNOWN"} or row["filled"]:
                raise AuthorityError("SUBMISSION_RECEIPT_STATE_CONFLICT")
            try:
                db.execute("UPDATE reservations SET status='SUBMITTED',broker_id=? WHERE id=?",
                           (receipt["broker_order_id"],row["id"]))
            except sqlite3.IntegrityError as exc:
                raise AuthorityError("BROKER_ORDER_ID_ALREADY_BOUND") from exc
            self._event(db,now,"submitted",{"reservation_id":row["id"],
                "broker_order_id":receipt["broker_order_id"],"submission_receipt_id":raw["receipt_id"]})
            recovered.append(self._reservation(self._row(db,row["id"])))
        return recovered

    def recover_submission_receipts(self, token: str) -> list[Reservation]:
        """Promote exact response/no-send evidence under a fence; never POST."""
        with self._transaction() as db:
            now, owner = self._lease(db,token)
            return self._recover_submission_receipts(db,now,owner["epoch"])

    def submit(self, token: str, reservation_id: str, callback: Callable[[Callable[[], None]], str]) -> Reservation:
        """Durably fence a single submission; callback must invoke its final gate.

        An audited pre-gate stop is NOT_SUBMITTED. Other exceptions or a missing
        reliable ID remain UNKNOWN. A crash leaves
        SUBMITTING. Exact returned IDs are journaled before fenced projection,
        even if the lease expires in flight. Audited pre-gate no-send evidence
        is also journaled before fenced release. Neither uncertain state can be
        resubmitted or released by guessing.
        """
        with self._transaction() as db:
            now, owner = self._lease(db,token)
            row = self._row(db,reservation_id)
            if row["status"]!="RESERVED":
                return self._reservation(row)
            request = json.loads(row["request"])
            if request.get("stream") not in {"atlas-15m", "scout-15m"} and db.execute("SELECT 1 FROM reservations WHERE status IN ('SUBMITTING','UNKNOWN') AND id!=? LIMIT 1",
                          (reservation_id,)).fetchone():
                raise AuthorityError("UNCERTAIN_SUBMISSION_REQUIRES_RECONCILIATION")
            self._submission_snapshot(db,now,request)
            db.execute("UPDATE reservations SET status='SUBMITTING' WHERE id=?", (reservation_id,))
            self._event(db,now,"submitting",{"reservation_id":reservation_id})
            submission = {"request_sha256":_sha(request),"submission_epoch":owner["epoch"],
                          "submission_holder_id":owner["holder"],"submission_started_at":now.isoformat()}
        gate_called = False
        gate_evidence = None

        def before_send():
            nonlocal gate_called, gate_evidence
            with self._transaction() as db:
                now, owner = self._lease(db,token)
                row = self._row(db,reservation_id)
                if row["status"]!="SUBMITTING" or gate_called:
                    raise AuthorityError("SUBMISSION_GATE_ALREADY_USED_OR_STATE_CHANGED")
                self._submission_snapshot(db,now,json.loads(row["request"]))
                gate_evidence = {"request_sha256":_sha(json.loads(row["request"])),
                    "gate_epoch":owner["epoch"],"gate_holder_id":owner["holder"],"gate_at":now.isoformat()}
                gate_called = True

        try:
            broker_id = callback(before_send)
            if not gate_called or not isinstance(broker_id,str) or not re.fullmatch(r"[0-9]+",broker_id):
                raise AuthorityError("EXACT_BROKER_ORDER_ID_AND_FINAL_GATE_REQUIRED")
            self._record_submission_receipt(reservation_id,broker_id,gate_evidence,submission=submission)
        except BaseException as exc:
            if isinstance(exc,SubmissionNotStarted) and not gate_called:
                self._record_submission_receipt(reservation_id,None,None,
                    submission=submission,outcome="NOT_SUBMITTED")
            # Do not let an expired owner mutate authority state. SUBMITTING is
            # equally conservative if it cannot mark UNKNOWN under its lease.
            definitely_unsubmitted = False
            try:
                with self._transaction() as db:
                    now, _ = self._lease(db,token)
                    status = "NOT_SUBMITTED" if isinstance(exc, SubmissionNotStarted) and not gate_called else "UNKNOWN"
                    db.execute("UPDATE reservations SET status=? WHERE id=? AND status='SUBMITTING'", (status,reservation_id))
                    self._event(db,now,"submission-" + status.lower(),{"reservation_id":reservation_id})
                definitely_unsubmitted = status == "NOT_SUBMITTED"
            except AuthorityError:
                pass
            if isinstance(exc, SubmissionNotStarted) and not definitely_unsubmitted:
                raise AuthorityError("SUBMISSION_OUTCOME_REQUIRES_RECONCILIATION") from exc
            raise
        self.recover_submission_receipts(token)
        return self.reservation(reservation_id)

    def reconcile(self, token: str, evidence: OrderEvidence) -> Reservation:
        """Apply complete cumulative evidence for an already exact-bound ID.

        Unknown acceptance without a stored broker ID needs a separate reviewed
        recovery adapter; symbol/price/time matching is deliberately unsupported.
        """
        payload = asdict(evidence)
        try:
            provenance, whole_second_ids = validate_fill_time_provenance(evidence.fill_time_provenance, evidence.fills)
        except LedgerError as exc:
            raise AuthorityError("FILL_TIME_PROVENANCE_INVALID") from exc
        if provenance is None:
            payload.pop("fill_time_provenance")
        else:
            payload["fill_time_provenance"] = provenance
        _name(evidence.evidence_id)
        if evidence.complete is not True or evidence.account_fingerprint!=self.binding["account_fingerprint"]:
            raise AuthorityError("COMPLETE_SAME_ACCOUNT_ORDER_EVIDENCE_REQUIRED")
        observed = _time(evidence.observed_at)
        payload["observed_at"] = observed.isoformat()
        fills, ids = [], set()
        for original in evidence.fills:
            item = asdict(original)
            _name(item["fill_id"])
            if item["fill_id"] in ids:
                raise AuthorityError("DUPLICATE_FILL_ID")
            ids.add(item["fill_id"])
            _quantity(item["quantity"],positive=True)
            item["price"] = _amount(_money(item["price"],positive=True))
            item["executed_at"] = _time(item["executed_at"]).isoformat()
            fills.append(item)
        payload["fills"] = sorted(fills,key=lambda f:f["fill_id"])
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            row = self._row(db,evidence.reservation_id)
            request = json.loads(row["request"])
            if not row["broker_id"] or row["broker_id"]!=evidence.broker_order_id:
                raise AuthorityError("EXACT_PREVIOUSLY_BOUND_BROKER_ORDER_ID_REQUIRED")
            if not _time(row["created"])<=observed<=now or (now-observed).total_seconds()>60:
                raise AuthorityError("ORDER_EVIDENCE_STALE_OR_INVALID")
            previous = db.execute("SELECT payload FROM order_evidence WHERE id=?", (evidence.evidence_id,)).fetchone()
            if previous:
                if previous[0]!=_json(payload):
                    raise AuthorityError("ORDER_EVIDENCE_ID_REUSED")
                return self._reservation(row)
            quantity = _quantity(evidence.order_quantity,positive=True)
            filled = _quantity(evidence.cumulative_filled_quantity)
            remaining = _quantity(evidence.remaining_quantity)
            if quantity!=request["quantity"] or not row["filled"]<=filled<=quantity or sum(f["quantity"] for f in fills)!=filled:
                raise AuthorityError("CUMULATIVE_FILL_EVIDENCE_DOES_NOT_BALANCE")
            status = evidence.status
            if (status not in {"WORKING","PARTIAL","FILLED","CANCELLED","REJECTED"}
                    or (status=="WORKING" and filled!=0) or (status=="PARTIAL" and not 0<filled<quantity)
                    or (status=="FILLED" and filled!=quantity) or (status=="REJECTED" and filled!=0)
                    or remaining!=(quantity-filled if status in {"WORKING","PARTIAL"} else 0)):
                raise AuthorityError("ORDER_STATUS_AND_QUANTITIES_DISAGREE")
            if row["status"] in TERMINAL and (status!=row["status"] or filled!=row["filled"]):
                raise AuthorityError("TERMINAL_ORDER_CANNOT_CHANGE")
            if row["last_evidence"] and observed<=_time(row["last_evidence"]):
                raise AuthorityError("ORDER_EVIDENCE_MUST_ADVANCE")
            old = {r["fill_id"]:r["payload"] for r in db.execute("SELECT * FROM fills WHERE reservation=?",(evidence.reservation_id,))}
            current = {item["fill_id"]:_json(item) for item in fills}
            if any(current.get(key)!=value for key,value in old.items()):
                raise AuthorityError("PREVIOUS_FILL_EVIDENCE_CHANGED_OR_DISAPPEARED")
            for item in fills:
                price = _money(item["price"])
                created, executed = _time(row["created"]), _time(item["executed_at"])
                lower_bound_valid = created <= executed or (
                    item["fill_id"] in whole_second_ids and executed == created.replace(microsecond=0)
                )
                if (not lower_bound_valid or executed > observed
                        or (request["side"]=="BUY" and price>_money(request["limit_price"]))
                        or (request["side"]=="SELL" and price<_money(request["limit_price"]))):
                    raise AuthorityError("FILL_TIME_OR_LIMIT_PRICE_INVALID")
                db.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?)", (evidence.reservation_id,item["fill_id"],_json(item)))
            db.execute("UPDATE reservations SET status=?,filled=?,last_evidence=? WHERE id=?", (status,filled,observed.isoformat(),evidence.reservation_id))
            db.execute("INSERT INTO order_evidence VALUES(?,?)", (evidence.evidence_id,_json(payload)))
            self._event(db,now,"order-reconciled",{"reservation_id":evidence.reservation_id,"broker_order_id":evidence.broker_order_id,"status":status,"filled":filled})
            return self._reservation(self._row(db,evidence.reservation_id))

    def read_state(self):
        # Read one coherent SQLite snapshot without taking the writer lock needed
        # by this operation's lease renewal. Aggregate raw scalar rows in SQLite
        # to avoid a GIL handoff per reservation. Close even the read transaction
        # before Python decoding: a long reader can block commits in DELETE mode.
        columns = ("id", "request", "status", "filled", "broker_id", "last_evidence")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                                     timeout=30, isolation_level=None)) as db:
            rows, snapshot_count, event_count = db.execute("""SELECT
                (SELECT json_group_array(json_array(id,request,status,filled,broker_id,last_evidence))
                 FROM (SELECT id,request,status,filled,broker_id,last_evidence
                       FROM reservations ORDER BY created,id)),
                (SELECT COUNT(*) FROM snapshots), (SELECT COUNT(*) FROM events)""").fetchone()
        return {"version":VERSION,"binding":json.loads(_json(self.binding)),
                "reservations":[asdict(self._reservation(dict(zip(columns, row)))) for row in json.loads(rows)],
                "snapshot_count":snapshot_count,"event_count":event_count}
