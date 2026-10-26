"""Original independent 15m native accounting, isolated from Gameplan ledgers.

The additive subclass preserves canonical Gameplan behavior and DTOs. Its own
SQLite schema, reservation IDs, exact order recovery, and fill attribution are
the reviewed Atlas implementation; construction never connects to a broker.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
import json,re
from typing import Mapping,Sequence
from zoneinfo import ZoneInfo
from ml.stock_trader.horizon_ledger import (
    HorizonLedger, ReservationState as _ReservationState, AllocationState,
    LedgerSnapshot, ReconciliationResult, PortfolioEvidence, LedgerError, OrderEvidence, ExitPlan,
    _utc,_number,_quantity,_name,_encoded,_identity,_OPEN,_TERMINAL,
    validate_fill_time_provenance, LEDGER_VERSION)

@dataclass(frozen=True)
class ReservationState(_ReservationState):
    submitted_at: str | None = None
    timeout_requested_at: str | None = None
    retry_parent_id: str | None = None
    retry_requested_quantity: int | None = None

class IntradayLedger(HorizonLedger):
    """Own reservations and fills; observed broker positions supply capacity."""
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

