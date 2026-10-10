"""One host's durable account reservation authority, without a broker adapter.

The reviewed binding chooses one execution host. This database is host-local;
putting independent copies (or this SQLite file on a network share) on two PCs
does not provide distributed coordination. Native horizon ownership remains
authoritative. Requests must identify its real reservation and allocation.

Snapshot budgets come from the existing engine, net of observed broker orders.
Only explicit matching broker IDs overlap local commitments. Filled buys remain
charged until a newer coherent balance snapshot accounts for them; filled sales
never create cash here. The caller must retain its quote, source, ownership and
execution-window checks, and invoke the supplied gate immediately before POST.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Callable, Mapping
from zoneinfo import ZoneInfo


VERSION = "single-host-account-authority-v1"
OPEN = frozenset({"RESERVED", "SUBMITTING", "SUBMITTED", "UNKNOWN", "WORKING", "PARTIAL"})
TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED"})
HORIZONS = frozenset({"1h", "4h", "1d", "1w"})
ZERO = Decimal(0)


class AuthorityError(ValueError):
    pass


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
                "CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,observed TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,key TEXT UNIQUE NOT NULL,native_id TEXT UNIQUE NOT NULL,forecast_key TEXT UNIQUE NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,filled INTEGER NOT NULL,broker_id TEXT UNIQUE,created TEXT NOT NULL,last_evidence TEXT)",
                "CREATE TABLE IF NOT EXISTS order_evidence(id TEXT PRIMARY KEY,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS fills(reservation TEXT NOT NULL,fill_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(reservation,fill_id))",
                "CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,observed TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)",
            ):
                db.execute(statement)
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
        if not row or not secrets.compare_digest(row["token"], str(token)) or now >= _time(row["expires"]):
            raise AuthorityError("CURRENT_FENCED_LEASE_REQUIRED")
        return now, row

    @staticmethod
    def _ttl(seconds):
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise AuthorityError("LEASE_DURATION_MUST_BE_1_TO_60_SECONDS")
        return seconds

    def acquire_lease(self, *, coordinator_id: str, holder_id: str, ttl_seconds=60) -> Lease:
        if coordinator_id != self.binding["coordinator_id"]:
            raise AuthorityError("HOST_IS_NOT_THE_REVIEWED_COORDINATOR")
        holder = _name(holder_id)
        with self._transaction() as db:
            now = self._now(db)
            previous = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
            if previous and now < _time(previous["expires"]):
                raise AuthorityError("LEASE_ALREADY_OWNED")
            lease = Lease(secrets.token_hex(32), 1 if not previous else previous["epoch"]+1,
                          holder, (now+timedelta(seconds=self._ttl(ttl_seconds))).isoformat())
            db.execute("INSERT OR REPLACE INTO lease VALUES(1,?,?,?,?)", (lease.token,lease.epoch,holder,lease.expires_at))
            self._event(db,now,"lease-acquired",{"epoch":lease.epoch,"holder":holder})
            return lease

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
            plan = db.execute("SELECT * FROM plans WHERE action_date=?", (snapshot.action_date,)).fetchone()
            if plan and (plan["generation"]!=snapshot.generation or plan["sources"]!=_json(sources)):
                raise AuthorityError("ACTION_DATE_SOURCE_BINDING_IS_IMMUTABLE")
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

    def submit(self, token: str, reservation_id: str, callback: Callable[[Callable[[], None]], str]) -> Reservation:
        """Durably fence a single submission; callback must invoke its final gate.

        An audited pre-gate stop is NOT_SUBMITTED. Other exceptions or a missing
        reliable ID remain UNKNOWN. A crash leaves
        SUBMITTING. Neither state can be resubmitted or released by guessing.
        """
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            row = self._row(db,reservation_id)
            if row["status"]!="RESERVED":
                return self._reservation(row)
            if db.execute("SELECT 1 FROM reservations WHERE status IN ('SUBMITTING','UNKNOWN') AND id!=? LIMIT 1",
                          (reservation_id,)).fetchone():
                raise AuthorityError("UNCERTAIN_SUBMISSION_REQUIRES_RECONCILIATION")
            self._snapshot(db,now,json.loads(row["request"])["snapshot_id"])
            db.execute("UPDATE reservations SET status='SUBMITTING' WHERE id=?", (reservation_id,))
            self._event(db,now,"submitting",{"reservation_id":reservation_id})
        gate_called = False

        def before_send():
            nonlocal gate_called
            with self._transaction() as db:
                now, _ = self._lease(db,token)
                row = self._row(db,reservation_id)
                if row["status"]!="SUBMITTING" or gate_called:
                    raise AuthorityError("SUBMISSION_GATE_ALREADY_USED_OR_STATE_CHANGED")
                self._snapshot(db,now,json.loads(row["request"])["snapshot_id"])
                gate_called = True

        try:
            broker_id = callback(before_send)
            if not gate_called or not isinstance(broker_id,str) or not re.fullmatch(r"[0-9]+",broker_id):
                raise AuthorityError("EXACT_BROKER_ORDER_ID_AND_FINAL_GATE_REQUIRED")
        except BaseException as exc:
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
        with self._transaction() as db:
            now, _ = self._lease(db,token)
            row = self._row(db,reservation_id)
            if row["status"]!="SUBMITTING":
                raise AuthorityError("SUBMISSION_STATE_CHANGED")
            try:
                db.execute("UPDATE reservations SET status='SUBMITTED',broker_id=? WHERE id=?", (broker_id,reservation_id))
            except sqlite3.IntegrityError as exc:
                raise AuthorityError("BROKER_ORDER_ID_ALREADY_BOUND") from exc
            self._event(db,now,"submitted",{"reservation_id":reservation_id,"broker_order_id":broker_id})
            return self._reservation(self._row(db,reservation_id))

    def reconcile(self, token: str, evidence: OrderEvidence) -> Reservation:
        """Apply complete cumulative evidence for an already exact-bound ID.

        Unknown acceptance without a stored broker ID needs a separate reviewed
        recovery adapter; symbol/price/time matching is deliberately unsupported.
        """
        payload = asdict(evidence)
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
                if (not _time(row["created"])<=_time(item["executed_at"])<=observed
                        or (request["side"]=="BUY" and price>_money(request["limit_price"]))
                        or (request["side"]=="SELL" and price<_money(request["limit_price"]))):
                    raise AuthorityError("FILL_TIME_OR_LIMIT_PRICE_INVALID")
                db.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?)", (evidence.reservation_id,item["fill_id"],_json(item)))
            db.execute("UPDATE reservations SET status=?,filled=?,last_evidence=? WHERE id=?", (status,filled,observed.isoformat(),evidence.reservation_id))
            db.execute("INSERT INTO order_evidence VALUES(?,?)", (evidence.evidence_id,_json(payload)))
            self._event(db,now,"order-reconciled",{"reservation_id":evidence.reservation_id,"broker_order_id":evidence.broker_order_id,"status":status,"filled":filled})
            return self._reservation(self._row(db,evidence.reservation_id))

    def read_state(self):
        with self._transaction() as db:
            return {"version":VERSION,"binding":json.loads(_json(self.binding)),
                    "reservations":[asdict(self._reservation(row)) for row in db.execute("SELECT * FROM reservations ORDER BY created,id")],
                    "snapshot_count":db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                    "event_count":db.execute("SELECT COUNT(*) FROM events").fetchone()[0]}
