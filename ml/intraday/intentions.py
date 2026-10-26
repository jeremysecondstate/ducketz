"""Durable execution intentions and projections, separate from issued forecasts.

The first valid sizing result fixes the desired quantity. Later capacity, order
and fill observations append records without rewriting that intention. This
module has no broker connection and cannot create an order or a fill.
"""
import json
import sqlite3
from dataclasses import asdict

from .prediction import ContractError, digest, encoded, identity, instant, quantity
from .direction_metadata import recorded_action, recorded_direction
from . import ordinary_diagnostics as _ordinary

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
        with _ordinary.phase("writer_payload_read"):
            row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE id=?",
                             (forecast["forecast_id"],)).fetchone()
        if row is None:
            raise ContractError("Intention requires the original saved forecast")
        _ordinary.payload(row[6])
        with _ordinary.phase("writer_payload_validate"):
            original = self.store._forecast_value(row)
        with _ordinary.phase("writer_caller_compare"):
            if original != forecast:
                raise ContractError("Intention requires the original saved forecast")

    def _ordinary_forecast_marker(self, db, forecast_id):
        """Optional call-local proof that a fully validated row is unchanged.

        Marker uncertainty never replaces the original full validator with a
        new prerequisite. Both the opened handle and current pathname must
        name the same physical database; revisions alone do not establish that.
        """
        try:
            generation, epoch = self.store._execution_forecast_generation(db)
            opened = getattr(self.store, "_connection_source_identities", {}).get(id(db))
            stat = self.store.path.stat()
            if opened is None or opened != (stat.st_dev, stat.st_ino):
                return None
            indexed = db.execute("SELECT f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,r.generation "
                "FROM forecasts f LEFT JOIN execution_forecast_revisions r ON r.forecast_id=f.id "
                "WHERE f.id=?", (forecast_id,)).fetchone()
            if (indexed is None or type(indexed[6]) is not int
                    or not 0 <= indexed[6] <= generation):
                return None
            return (opened, db.execute("PRAGMA schema_version").fetchone()[0],
                    epoch, generation, tuple(indexed))
        except (ContractError, sqlite3.Error, OSError):
            return None


    def _ordinary_forecast_preflight(self, forecast):
        """Close a complete original-validation read before taking a writer.

        The caller holds the store lock through its final write transaction.
        The independent tree is local to that operation, never a store cache.
        """
        with _ordinary.context(self.store.connect(), "preflight_connection_open", "preflight_connection_close") as db:
            with _ordinary.phase("preflight_begin"):
                db.execute("BEGIN")
            with _ordinary.phase("preflight_marker"):
                marker = self._ordinary_forecast_marker(db, forecast["forecast_id"])
            with _ordinary.phase("preflight_payload_read"):
                row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                                 "FROM forecasts WHERE id=?", (forecast["forecast_id"],)).fetchone()
            if row is None:
                raise ContractError("Intention requires the original saved forecast")
            _ordinary.payload(row[6])
            with _ordinary.phase("preflight_payload_validate"):
                original = self.store._forecast_value(row)
            with _ordinary.phase("preflight_caller_compare"):
                if original != forecast:
                    raise ContractError("Intention requires the original saved forecast")
        return marker, original


    def _ordinary_forecast_recheck(self, db, forecast, marker, original):
        # Both branches retain full caller equality, including nested/extra
        # fields. Optional markers only avoid decoding the same original again.
        with _ordinary.phase("writer_marker"):
            changed = marker is None or marker != self._ordinary_forecast_marker(db, forecast["forecast_id"])
        if changed:
            self._forecast(db, forecast)
        else:
            with _ordinary.phase("writer_caller_compare"):
                if original != forecast:
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
        """Reuse guarded scalar proofs within the original chronology scope."""
        reference = instant(forecast["reference_at"])
        with _ordinary.phase("processed_id_read"), self.store.connect() as db:
            # Preserve main's conservative exact-scope SQL prefilter. Older
            # malformed originals cannot block an unrelated current fence.
            # Python instants below retain the inclusive chronology decision.
            ids = [row[0] for row in db.execute(
                "SELECT f.id FROM forecasts f JOIN intraday_intentions i ON i.forecast_id=f.id "
                "WHERE f.pc=? AND f.symbol=? "
                "AND julianday(f.reference_at)>=julianday(?)-1.0/86400 "
                "ORDER BY f.rowid",
                (forecast["pc"], forecast["symbol"], forecast["reference_at"]))]
        if not ids:
            return []
        with _ordinary.phase("processed_header_read"):
            return [f["forecast_id"] for f in self.store.execution_forecast_headers(
                        pc=forecast["pc"], symbol=forecast["symbol"], forecast_ids=ids)
                    if f["forecast_id"] != forecast["forecast_id"] and f["mode"] == "live"
                    and instant(f["reference_at"]) >= reference]

    def _original_action(self, db, forecast):
        row = db.execute("SELECT id,payload FROM intents WHERE forecast_id=? ORDER BY rowid LIMIT 1",
                         (forecast["forecast_id"],)).fetchone()
        original = json.loads(row[1]) if row else None
        if original is None or digest(original) != row[0]:
            raise ContractError("Original forecast intention is missing or changed")
        side = recorded_action(forecast, legacy_side=original.get("side"))
        if side != original.get("side"):
            raise ContractError("Original intention differs from frozen stock direction")
        return side

    def save_sizing(self, forecast, result):
        value = result.payload()
        if value["forecast_id"] != forecast["forecast_id"] or value["symbol"] != forecast["symbol"]:
            raise ContractError("Sizing belongs to another forecast")
        previous = self.get(forecast["forecast_id"])
        with self.store.connect() as db:
            side = previous["side"] if previous else self._original_action(db, forecast)
        if value["side"] != side or instant(value["calculated_at"]) < instant(forecast["issued_at"]):
            raise ContractError("Sizing direction or availability differs from forecast")
        metadata = recorded_direction(forecast)
        if metadata and any(field in value and value[field] != item for field, item in metadata.items()):
            raise ContractError("Sizing stock direction metadata differs from forecast")
        if value["status"] != "sized":
            return self.record(forecast, event_key="unavailable:" + result.result_id,
                recorded_at=value["calculated_at"], disposition=value["reason"],
                evidence={"sizing": value}, kind="sizing_unavailable")
        if side == "HOLD":
            raise ContractError("Neutral forecast cannot create a sized trade")
        desired = quantity(value["desired_quantity"])
        payload = {"version": VERSION, "forecast_id": forecast["forecast_id"], "symbol": forecast["symbol"],
                   "side": side, "desired_quantity": desired, "sizing_id": result.result_id, "sizing": value,
                   **metadata}
        key = digest(payload)
        with self.store._execution_forecast_lock:
            marker, original = self._ordinary_forecast_preflight(forecast)
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._ordinary_forecast_recheck(db, forecast, marker, original)
                del original  # Release the complete tree before sizing research is read.
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
               capacity=None, order=None, require_original_intention=False):
        """Append one idempotent projection; execution counts require exact evidence.

        ``order`` is a native ledger ReservationState with complete identified
        fills. Its submission count remains unknown when acceptance is unknown.
        The native/account ledgers, not this projection, own broker recovery.
        """
        identity(event_key); identity(disposition); identity(kind)
        at = instant(recorded_at)
        if at < instant(forecast["issued_at"]) or not isinstance(evidence, dict) or not evidence:
            raise ContractError("Intention event needs available explicit evidence")
        with self.store._execution_forecast_lock:
            marker, original = self._ordinary_forecast_preflight(forecast)
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._ordinary_forecast_recheck(db, forecast, marker, original)
                del original  # Release the complete tree before sizing research is read.
                intention = self._value(db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",
                                                   (forecast["forecast_id"],)).fetchone())
                if require_original_intention and intention is None:
                    raise ContractError("Local order lacks its original immutable intention")
                desired = intention["desired_quantity"] if intention else None
                side = intention["side"] if intention else self._original_action(db, forecast)
                value = dict(version=VERSION, forecast_id=forecast["forecast_id"], symbol=forecast["symbol"],
                    side=side, kind=kind, desired_quantity=desired, adjusted_quantity=None,
                    disposition=disposition, evidence=evidence, submission_status="not_submitted",
                    submitted_quantity=0, filled_quantity=0, realized_pnl=None, **recorded_direction(forecast))
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
