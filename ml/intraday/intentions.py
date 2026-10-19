"""Durable execution intentions and projections, separate from issued forecasts.

The first valid sizing result fixes the desired quantity. Later capacity, order
and fill observations append records without rewriting that intention. This
module has no broker connection and cannot create an order or a fill.
"""
import json
from dataclasses import asdict

from ml.stock_direction_policy import stock_direction

from .prediction import ContractError, digest, encoded, identity, instant, quantity

VERSION = "intraday-independent-intentions-v1"


class IntentionStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS intraday_intentions(
                forecast_id TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS intraday_intention_events(
                event_key TEXT PRIMARY KEY, forecast_id TEXT NOT NULL,
                intent_entry_id TEXT UNIQUE NOT NULL);
            """)

    def _forecast(self, db, forecast):
        row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE id=?",
                         (forecast["forecast_id"],)).fetchone()
        if row is None or self.store._forecast_value(row) != forecast:
            raise ContractError("Intention requires the original saved forecast")

    @staticmethod
    def _value(row):
        if row is None:
            return None
        forecast_id, key, raw = row
        value = json.loads(raw)
        if digest(value) != key or value.get("forecast_id") != forecast_id:
            raise ContractError("Persisted sizing intention changed")
        return value

    def get(self, forecast_id):
        with self.store.connect() as db:
            return self._value(db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",
                                          (forecast_id,)).fetchone())

    def processed_at_or_after(self, forecast):
        """Valid sized intentions fence chronology even when no order was possible."""
        with self.store.connect() as db:
            ids = {row[0] for row in db.execute("SELECT forecast_id FROM intraday_intentions")}
        if not ids:
            return []
        # Unsized forecasts cannot fence an intention. Reuse scalar headers of
        # unchanged validated sizing identities; every selected byte is still
        # hashed, and changed payloads/indexed identities are validated again.
        return [f["forecast_id"] for f in self.store.execution_forecast_headers(
                    pc=forecast["pc"], symbol=forecast["symbol"], forecast_ids=ids)
                if f["forecast_id"] in ids and f["forecast_id"] != forecast["forecast_id"]
                and f["symbol"] == forecast["symbol"] and f["mode"] == "live"
                and instant(f["reference_at"]) >= instant(forecast["reference_at"])]

    def save_sizing(self, forecast, result):
        value = result.payload()
        if value["forecast_id"] != forecast["forecast_id"] or value["symbol"] != forecast["symbol"]:
            raise ContractError("Sizing belongs to another forecast")
        existing = self.get(forecast["forecast_id"])
        side = (existing["side"] if existing is not None else
                {"BULLISH": "BUY", "BEARISH": "SELL"}.get(stock_direction(forecast["probability_not_down"]), "HOLD"))
        if value["side"] != side or instant(value["calculated_at"]) < instant(forecast["issued_at"]):
            raise ContractError("Sizing direction or availability differs from forecast")
        if side == "HOLD" and value["status"] == "sized":
            raise ContractError("A neutral forecast cannot request a trade")
        if value["status"] != "sized":
            return self.record(forecast, event_key="unavailable:" + result.result_id,
                recorded_at=value["calculated_at"], disposition=value["reason"],
                evidence={"sizing": value}, kind="sizing_unavailable")
        desired = quantity(value["desired_quantity"])
        payload = {"version": VERSION, "forecast_id": forecast["forecast_id"], "symbol": forecast["symbol"],
                   "side": side, "desired_quantity": desired, "sizing_id": result.result_id, "sizing": value}
        key = digest(payload)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._forecast(db, forecast)
            old = self._value(db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",
                                        (forecast["forecast_id"],)).fetchone())
            if old is not None and old != payload:
                raise ContractError("Original desired quantity and sizing evidence are immutable")
            db.execute("INSERT OR IGNORE INTO intraday_intentions VALUES(?,?,?)",
                       (forecast["forecast_id"], key, encoded(payload)))
        # Recovery can repeat this after a crash between the two transactions.
        self.record(forecast, event_key="sized:" + result.result_id, recorded_at=value["calculated_at"],
                    disposition="awaiting_capacity", evidence={"sizing_id": result.result_id}, kind="sized")
        return payload

    def record(self, forecast, *, event_key, recorded_at, disposition, evidence, kind,
               capacity=None, order=None):
        """Append one idempotent projection; execution counts require exact evidence.

        ``order`` is a native ledger ReservationState with complete identified
        fills. Its submission count remains unknown when acceptance is unknown.
        The native/account ledgers, not this projection, own broker recovery.
        """
        identity(event_key); identity(disposition); identity(kind)
        at = instant(recorded_at)
        if at < instant(forecast["issued_at"]) or not isinstance(evidence, dict) or not evidence:
            raise ContractError("Intention event needs available explicit evidence")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._forecast(db, forecast)
            intention = self._value(db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",
                                               (forecast["forecast_id"],)).fetchone())
            desired = intention["desired_quantity"] if intention else None
            # Recovery preserves the first immutable intention and its actual
            # order side across later policy changes. New unsized forecasts
            # still take their direction from the current cutoff.
            side = (intention["side"] if intention is not None else
                    {"BULLISH": "BUY", "BEARISH": "SELL"}.get(stock_direction(forecast["probability_not_down"]), "HOLD"))
            value = dict(version=VERSION, forecast_id=forecast["forecast_id"], symbol=forecast["symbol"],
                side=side, kind=kind, desired_quantity=desired, adjusted_quantity=None,
                disposition=disposition, evidence=evidence, submission_status="not_submitted",
                submitted_quantity=0, filled_quantity=0, realized_pnl=None)
            if capacity is not None:
                cap = capacity.payload()
                if desired is None or (cap["symbol"], cap["side"], cap["desired_quantity"]) != (forecast["symbol"], side, desired):
                    raise ContractError("Capacity differs from original intended trade")
                if instant(cap["calculated_at"]) > at:
                    raise ContractError("Capacity evidence is in the future")
                value.update(adjusted_quantity=cap["adjusted_quantity"], capacity=cap)
            if order is not None:
                raw = asdict(order)
                if desired is None or order.symbol != forecast["symbol"] or order.side != side or order.quantity > desired:
                    raise ContractError("Order differs from intended trade")
                if order.forecast_id != forecast["forecast_id"]:
                    raise ContractError("Order belongs to another forecast")
                if order.filled_quantity and not order.broker_order_id:
                    raise ContractError("Filled quantity requires exact broker identity")
                value.update(order=raw, adjusted_quantity=order.quantity, filled_quantity=order.filled_quantity,
                             submission_status=order.status, broker_order_id=order.broker_order_id,
                             submitted_quantity=order.quantity if order.broker_order_id else
                             None if order.status in {"UNKNOWN", "SUBMITTING"} else 0)
            key = digest([forecast["forecast_id"], event_key])
            value.update(event_identity=key, recorded_at=at.isoformat())
            old = db.execute("SELECT i.id,i.payload,i.recorded_at FROM intraday_intention_events e JOIN intents i ON i.id=e.intent_entry_id WHERE e.event_key=?", (key,)).fetchone()
            if old:
                previous = json.loads(old[1])
                value["recorded_at"] = previous["recorded_at"]
                if digest(previous) != old[0] or previous["recorded_at"] != old[2] or encoded(value) != old[1]:
                    raise ContractError("Intention event identity reused with changed evidence")
                return json.loads(old[1])
            latest = db.execute("SELECT max(i.recorded_at) FROM intraday_intention_events e JOIN intents i ON i.id=e.intent_entry_id WHERE e.forecast_id=?", (forecast["forecast_id"],)).fetchone()[0]
            if latest and instant(latest) > at:
                raise ContractError("Intention event cannot be backdated")
            entry_id = digest(value)
            db.execute("INSERT OR IGNORE INTO intents VALUES(?,?,?,?)", (entry_id, forecast["forecast_id"], at.isoformat(), encoded(value)))
            db.execute("INSERT INTO intraday_intention_events VALUES(?,?,?)", (key, forecast["forecast_id"], entry_id))
        return value

    def history(self, forecast_id):
        with self.store.connect() as db:
            rows = db.execute("SELECT i.id,i.payload,i.recorded_at FROM intraday_intention_events e JOIN intents i ON i.id=e.intent_entry_id WHERE e.forecast_id=? ORDER BY e.rowid", (forecast_id,)).fetchall()
        result = []
        for key, raw, at in rows:
            value = json.loads(raw)
            if digest(value) != key or value.get("forecast_id") != forecast_id or value.get("recorded_at") != at:
                raise ContractError("Persisted intention event changed")
            result.append({**value, "recorded_at": at})
        return result
