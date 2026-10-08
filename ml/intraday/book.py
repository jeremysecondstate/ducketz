"""Durable single-account OFFLINE book, never an active Gameplan database.

Admission order is supplied by the offline caller. Serialization prevents double
commitment; it does not specify contention priority or a distributed protocol.
Capacity observations are net of unmatched working orders and existing account
constraints. Explicit local order matches and identified fills avoid double count.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3

from .prediction import ContractError, UnresolvedPolicy, encoded, identity, instant, number, quantity

HORIZONS = ("15m", "1h", "4h", "1d", "1w")
OPEN = {"RESERVED", "SUBMITTING", "UNKNOWN", "WORKING", "PARTIAL", "CANCEL_PENDING"}
TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "NOT_ACCEPTED"}
VERSION = "offline-atlas-intraday-book-v1"


@dataclass(frozen=True)
class Request:
    request_id: str
    forecast_id: str
    symbol: str
    side: str
    desired: int
    limit: str
    stream: str
    horizon: str = "15m"
    # Explicit policy-admitted quantities in hierarchy order, not blanket donor access.
    allocation_caps: tuple[tuple[str, int], ...] = ()
    sale_policy_version: str | None = None
    sizing: dict = field(default_factory=dict)
    parent_request: str | None = None


@dataclass(frozen=True)
class Capacity:
    evidence_id: str
    observed_at: str
    cash_headroom: str
    gross_headroom: str
    symbol_headroom: dict[str, str]
    # These working commitments have already been deducted from the headrooms.
    included_working: dict[str, int] = field(default_factory=dict)
    accounted_fill_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fill:
    fill_id: str
    quantity: int
    price: str
    executed_at: str


@dataclass(frozen=True)
class OrderEvidence:
    evidence_id: str
    request_id: str
    broker_id: str
    observed_at: str
    status: str
    order_quantity: int
    fills: tuple[Fill, ...]
    complete: bool = True


class OfflineBook:
    def __init__(self, path: Path, *, account_id: str, atlas_symbols, gameplan_symbols):
        self.path = Path(path).resolve()
        # Each fixture is deliberately fenced from live account/ledger namespaces.
        if self.path.name != "offline-intraday.sqlite3" or str(path).startswith(("\\\\", "//")):
            raise ContractError("Use a host-local offline-intraday.sqlite3 fixture database")
        atlas, gameplan = tuple(sorted(atlas_symbols)), tuple(sorted(gameplan_symbols))
        if not atlas or len(set(atlas)) != len(atlas) or len(set(gameplan)) != len(gameplan) or not set(atlas) <= set(gameplan):
            raise ContractError("Explicit nonoverlapping Atlas symbol scope required")
        for symbol in gameplan:
            identity(symbol)
        self.binding = {"version": VERSION, "account_id": identity(account_id), "executor": "Atlas",
                        "atlas_symbols": atlas, "gameplan_symbols": gameplan}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY, recorded_at TEXT, kind TEXT, payload TEXT)")
            initial = {"binding": self.binding, "allocations": {}, "orders": {}, "fills": {},
                       "evidence": {}, "capacity": None, "capacity_history": {}}
            db.execute("INSERT OR IGNORE INTO state VALUES(1,?)", (encoded(initial),))
            if encoded(self.binding) != encoded(json.loads(db.execute("SELECT payload FROM state").fetchone()[0])["binding"]):
                raise ContractError("Offline book binding differs; preserve existing database")

    @contextmanager
    def transaction(self, kind, payload):
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])
            yield state
            db.execute("UPDATE state SET payload=? WHERE id=1", (encoded(state),))
            db.execute("INSERT INTO events(recorded_at,kind,payload) VALUES(?,?,?)",
                       (datetime.now(timezone.utc).isoformat(), kind, encoded(payload)))

    def snapshot(self):
        with sqlite3.connect(self.path) as db:
            return json.loads(db.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])

    def history(self):
        with sqlite3.connect(self.path) as db:
            return tuple({"sequence": r[0], "recorded_at": r[1], "kind": r[2], "payload": json.loads(r[3])}
                         for r in db.execute("SELECT * FROM events ORDER BY sequence"))

    def import_allocation(self, *, allocation_id, symbol, horizon, held, completed_purchase_id):
        """Identified fixture inventory only; snapshots never invent purchases."""
        if allocation_id.startswith("purchase:"):
            raise ContractError("Internal purchase allocation namespace cannot be imported")
        if symbol not in self.binding["gameplan_symbols"] or horizon not in HORIZONS:
            raise ContractError("Invalid allocation scope")
        record = {"allocation_id": identity(allocation_id), "symbol": symbol, "horizon": horizon,
                  "held": quantity(held), "completed_purchase_id": identity(completed_purchase_id)}
        with self.transaction("fixture_allocation", record) as state:
            old = state["allocations"].get(allocation_id)
            if old and old != record:
                raise ContractError("Existing inventory is not a replenishment target")
            state["allocations"][allocation_id] = record

    def capacity(self, evidence: Capacity):
        payload = asdict(evidence)
        identity(evidence.evidence_id)
        now = instant(evidence.observed_at)
        number(evidence.cash_headroom)
        number(evidence.gross_headroom)
        for symbol, headroom in evidence.symbol_headroom.items():
            if symbol not in self.binding["gameplan_symbols"]:
                raise ContractError("Capacity symbol outside binding")
            number(headroom)
        with self.transaction("capacity_evidence", payload) as state:
            prior = state["capacity_history"].get(evidence.evidence_id)
            if prior:
                if encoded(prior) != encoded(payload):
                    raise ContractError("Capacity evidence identity changed")
                return
            if state["capacity"] and now <= instant(state["capacity"]["observed_at"]):
                raise ContractError("Capacity must advance coherent observation time")
            if len(set(evidence.accounted_fill_ids)) != len(evidence.accounted_fill_ids):
                raise ContractError("Repeated accounted fill")
            for fill_id in evidence.accounted_fill_ids:
                fill = state["fills"].get(fill_id)
                if fill is None or instant(fill["executed_at"]) > now:
                    raise ContractError("Balance evidence must identify actual known executions")
            if state["capacity"] and not set(state["capacity"]["accounted_fill_ids"]) <= set(evidence.accounted_fill_ids):
                raise ContractError("Balance evidence cannot forget accounted executions")
            for request_id, qty in evidence.included_working.items():
                order = state["orders"].get(request_id)
                if (order is None or not order["broker_id"] or order["status"] not in OPEN
                        or quantity(qty) != order["permitted"] - order["filled"]):
                    raise ContractError("Working overlap requires exact identified local broker order")
            state["capacity_history"][evidence.evidence_id] = payload
            state["capacity"] = payload

    @staticmethod
    def _headroom(state, symbol):
        cap = state["capacity"]
        if cap is None or symbol not in cap["symbol_headroom"]:
            raise ContractError("Explicit coherent account/exposure capacity required")
        cash, gross, specific = map(number, (cap["cash_headroom"], cap["gross_headroom"], cap["symbol_headroom"][symbol]))
        for rid, order in state["orders"].items():
            request = order["request"]
            if request["side"] != "BUY":
                continue
            remaining = order["permitted"] - order["filled"] if order["status"] in OPEN else 0
            # Included orders are already netted in the snapshot. Never add their
            # later cancellations back without new account evidence.
            liability = number(request["limit"]) * remaining
            liability += sum(number(f["price"]) * f["quantity"] for fid, f in state["fills"].items()
                             if f["request_id"] == rid and fid not in cap["accounted_fill_ids"])
            # A fill after this snapshot consumes part of its already-netted
            # working commitment. Offset the total liability, not just remainder.
            included = number(request["limit"]) * cap["included_working"].get(rid, 0)
            cost = max(Decimal(0), liability - included)
            cash -= cost
            gross -= cost
            if request["symbol"] == symbol:
                specific -= cost
        return max(Decimal(0), min(cash, gross, specific))

    @staticmethod
    def _available(state, allocation_id):
        allocation = state["allocations"][allocation_id]
        assigned = sum(o["sources"].get(allocation_id, 0) for o in state["orders"].values() if o["status"] in OPEN)
        return allocation["held"] - assigned

    def reserve(self, request: Request, *, admission_id):
        payload = json.loads(encoded(asdict(request)))
        for value in (request.request_id, request.forecast_id, admission_id):
            identity(value)
        if request.sale_policy_version is not None:
            identity(request.sale_policy_version)
        quantity(request.desired, positive=True)
        price = number(request.limit, positive=True)
        if request.side not in {"BUY", "SELL"} or request.horizon not in HORIZONS:
            raise ContractError("Invalid side or horizon")
        if request.stream not in {"atlas-15m", "gameplan"}:
            raise ContractError("Scout execution is not implemented")
        symbols = self.binding["atlas_symbols"] if request.stream == "atlas-15m" else self.binding["gameplan_symbols"]
        if (request.symbol not in symbols or (request.stream == "atlas-15m" and request.horizon != "15m")
                or (request.stream == "gameplan" and request.horizon == "15m")):
            raise ContractError("Request outside executing stream scope")
        with self.transaction("offline_admission", {"admission_id": admission_id, "request": payload}) as state:
            existing = state["orders"].get(request.request_id)
            if existing:
                if encoded(existing["request"]) != encoded(payload):
                    raise ContractError("Persistent request identity reused with changed intent")
                return deepcopy(existing)
            if request.parent_request:
                parent = state["orders"].get(request.parent_request)
                if parent is None or parent["status"] != "CANCELLED":
                    raise ContractError("Replacement requires confirmed cancellation and complete fills")
                original = parent["request"]
                if any(payload[k] != original[k] for k in ("symbol", "side", "stream", "horizon", "forecast_id")):
                    raise ContractError("Replacement changes original ownership or forecast")
                if request.desired > parent["permitted"] - parent["filled"]:
                    raise ContractError("Replacement can pursue only the remaining quantity")
                if any(o["request"]["parent_request"] == request.parent_request for o in state["orders"].values()):
                    raise ContractError("Old remainder already has a replacement")
            elif any(all(o["request"][k] == payload[k] for k in ("forecast_id", "stream", "symbol"))
                     for o in state["orders"].values()):
                raise ContractError("Forecast already has a committed instruction")
            sources = {}
            if request.side == "BUY":
                if request.allocation_caps:
                    raise ContractError("BUY cannot consume donor allocations")
                permitted = min(request.desired, int(self._headroom(state, request.symbol) // price))
            else:
                if not request.allocation_caps:
                    raise UnresolvedPolicy("SELL requires explicit eligible allocation quantities")
                seen, ranks = set(), []
                for aid, ceiling in request.allocation_caps:
                    quantity(ceiling)
                    allocation = state["allocations"].get(aid)
                    if aid in seen or allocation is None or allocation["symbol"] != request.symbol:
                        raise ContractError("Invalid sale allocation identity")
                    seen.add(aid)
                    rank = HORIZONS.index(allocation["horizon"])
                    if rank < HORIZONS.index(request.horizon):
                        raise ContractError("Longer horizons cannot sell shorter allocations")
                    if allocation["horizon"] != request.horizon and not request.sale_policy_version:
                        raise UnresolvedPolicy("Longer-allocation ceilings, pacing and spanning need explicit policy")
                    ranks.append(rank)
                    take = min(ceiling, self._available(state, aid), request.desired - sum(sources.values()))
                    if take > 0:
                        sources[aid] = take
                if ranks != sorted(ranks):
                    raise ContractError("Sale allocation sequence must follow the settled hierarchy")
                permitted = sum(sources.values())
            if permitted == 0:
                raise ContractError("No uncommitted capacity; no order reserved")
            order = {"request": payload, "admission_id": admission_id, "account_id": self.binding["account_id"],
                     "permitted": permitted, "filled": 0, "status": "RESERVED", "broker_id": None,
                     "sources": sources, "last_observed": None}
            state["orders"][request.request_id] = order
            return deepcopy(order)

    def order(self, request_id):
        state = self.snapshot()
        if request_id not in state["orders"]:
            raise ContractError("Unknown request")
        return self._view(state, request_id)

    def begin_submit(self, request_id):
        with self.transaction("submission_started", {"request_id": request_id}) as state:
            order = state["orders"][request_id]
            if order["status"] != "RESERVED":
                raise ContractError("Already sent or unresolved; retrieve status before any resend")
            order["status"] = "SUBMITTING"
            return deepcopy(order)

    def unknown(self, request_id):
        with self.transaction("submission_unknown", {"request_id": request_id}) as state:
            if state["orders"][request_id]["status"] == "SUBMITTING":
                state["orders"][request_id]["status"] = "UNKNOWN"

    def cancel_requested(self, request_id):
        with self.transaction("cancel_requested", {"request_id": request_id}) as state:
            order = state["orders"][request_id]
            if order["status"] not in OPEN or not order["broker_id"]:
                raise ContractError("Cancellation needs an identified working order")
            order["status"] = "CANCEL_PENDING"

    def apply(self, evidence: OrderEvidence):
        payload = asdict(evidence)
        identity(evidence.evidence_id)
        identity(evidence.broker_id)
        observed = instant(evidence.observed_at)
        quantity(evidence.order_quantity, positive=True)
        if evidence.complete is not True or evidence.status not in {"WORKING", "PARTIAL", *TERMINAL}:
            raise ContractError("Complete broker status and execution evidence required")
        with self.transaction("order_evidence", payload) as state:
            prior = state["evidence"].get(evidence.evidence_id)
            if prior:
                if encoded(prior) != encoded(payload):
                    raise ContractError("Order evidence identity changed")
                return self._view(state, evidence.request_id)
            order = state["orders"][evidence.request_id]
            if order["status"] == "RESERVED" or evidence.order_quantity != order["permitted"]:
                raise ContractError("Evidence does not match submitted quantity")
            if order["broker_id"] not in {None, evidence.broker_id}:
                raise ContractError("Broker identity changed")
            if any(o["broker_id"] == evidence.broker_id for rid, o in state["orders"].items() if rid != evidence.request_id):
                raise ContractError("Broker order matched to another commitment")
            if order["last_observed"] and observed < instant(order["last_observed"]):
                raise ContractError("Stale order evidence cannot rewind state")
            fills = {}
            request = order["request"]
            for fill in evidence.fills:
                identity(fill.fill_id)
                quantity(fill.quantity, positive=True)
                price = number(fill.price, positive=True)
                if instant(fill.executed_at) > observed or fill.fill_id in fills:
                    raise ContractError("Invalid or duplicate fill evidence")
                if (request["side"] == "BUY" and price > number(request["limit"])) or (request["side"] == "SELL" and price < number(request["limit"])):
                    raise ContractError("Fill violates limit price protection")
                fills[fill.fill_id] = {**asdict(fill), "request_id": evidence.request_id}
            previous = {fid: f for fid, f in state["fills"].items() if f["request_id"] == evidence.request_id}
            if not set(previous) <= set(fills) or any(fills[fid] != {k: v for k, v in f.items() if k != "allocation_effects"}
                                                    for fid, f in previous.items()):
                raise ContractError("Complete evidence cannot omit or rewrite prior executions")
            total = sum(f["quantity"] for f in fills.values())
            if total > order["permitted"] or (evidence.status == "FILLED") != (total == order["permitted"]):
                raise ContractError("Status and cumulative fills disagree")
            if (evidence.status == "PARTIAL" and not 0 < total < order["permitted"]) or (evidence.status == "WORKING" and total):
                raise ContractError("Normalize partially executed working status to PARTIAL")
            if evidence.status in {"REJECTED", "NOT_ACCEPTED"} and total:
                raise ContractError("Nonacceptance cannot contain fills")
            if order["status"] in TERMINAL and (total != order["filled"] or evidence.status != order["status"]):
                raise ContractError("Terminal evidence changed; reconcile discrepancy explicitly")
            for fid, fill in fills.items():
                if fid in previous:
                    continue
                if fid in state["fills"]:
                    raise ContractError("Execution identity already attributed to another order")
                qty = fill["quantity"]
                effects = {}
                if request["side"] == "BUY":
                    aid = "purchase:" + evidence.request_id
                    allocation = state["allocations"].setdefault(aid, {"allocation_id": aid, "symbol": request["symbol"],
                        "horizon": request["horizon"], "held": 0, "completed_purchase_id": evidence.request_id})
                    allocation["held"] += qty
                    effects[aid] = qty
                else:
                    # JSON keys are canonicalized alphabetically; preserve the
                    # explicit hierarchy in the ordered request, not dict order.
                    for aid, _ in request["allocation_caps"]:
                        if aid not in order["sources"]:
                            continue
                        taken = min(qty, order["sources"][aid])
                        state["allocations"][aid]["held"] -= taken
                        order["sources"][aid] -= taken
                        qty -= taken
                        if taken:
                            effects[aid] = -taken
                    if qty:
                        raise ContractError("Fill exceeds identified allocation commitments")
                state["fills"][fid] = {**fill, "allocation_effects": effects}
            payload["allocation_effects"] = {fid: state["fills"][fid]["allocation_effects"] for fid in fills}
            order.update(status=evidence.status, broker_id=evidence.broker_id, filled=total,
                         last_observed=observed.isoformat())
            # Keep original evidence bytes distinct from derived allocation effects.
            state["evidence"][evidence.evidence_id] = asdict(evidence)
            return self._view(state, evidence.request_id)

    @staticmethod
    def _view(state, request_id):
        order = deepcopy(state["orders"][request_id])
        fills = [f for f in state["fills"].values() if f["request_id"] == request_id]
        order["remaining"] = order["permitted"] - order["filled"]
        order["average_fill_price"] = str(sum(number(f["price"]) * f["quantity"] for f in fills) / order["filled"]) if order["filled"] else None
        return order

    def reconcile_holdings(self, observed_shares):
        """Balances reveal discrepancies, never anonymous fills or allocation changes."""
        state = self.snapshot()
        tracked = {s: sum(a["held"] for a in state["allocations"].values() if a["symbol"] == s)
                   for s in self.binding["gameplan_symbols"]}
        return {s: {"tracked": tracked[s], "observed": quantity(observed_shares.get(s, 0))}
                for s in tracked if tracked[s] != observed_shares.get(s, 0)}
