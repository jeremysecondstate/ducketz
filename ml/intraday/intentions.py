"""Durable execution intentions and projections, separate from issued forecasts.

The first valid sizing result fixes the desired quantity. Later capacity, order
and fill observations append records without rewriting that intention. This
module has no broker connection and cannot create an order or a fill.
"""
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
import hashlib
import json
import math
import time
from dataclasses import asdict

from ml.stock_direction_policy import stock_direction

from .prediction import ContractError, digest, encoded, identity, instant, quantity

VERSION = "intraday-independent-intentions-v1"
_RECOVERY_SIZING_CACHE_LIMIT = 4096


# Every mutation of an original sizing row advances its revision in the same
# transaction. Recovery can then recheck small indexed identities instead of
# rereading unchanged scientific/research trees. Exact schema checks and an
# epoch prevent an absent or replaced marker from authorizing a cached value.
_RECOVERY_ROW_SCHEMA = {
    "recovery_sizing_generation": """CREATE TABLE recovery_sizing_generation(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        generation INTEGER NOT NULL CHECK(generation>=0),
        epoch TEXT NOT NULL CHECK(length(epoch)=32))""",
    "recovery_sizing_revisions": """CREATE TABLE recovery_sizing_revisions(
        forecast_id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=0))""",
    "recovery_generation_insert": """CREATE TRIGGER recovery_generation_insert
        BEFORE INSERT ON recovery_sizing_generation
        WHEN EXISTS(SELECT 1 FROM recovery_sizing_generation) BEGIN
        SELECT RAISE(ABORT,'Recovery sizing generation cannot be replaced'); END""",
    "recovery_generation_update": """CREATE TRIGGER recovery_generation_update
        BEFORE UPDATE ON recovery_sizing_generation
        WHEN NEW.singleton!=OLD.singleton OR NEW.generation!=OLD.generation+1
            OR typeof(NEW.generation)!='integer' OR NEW.epoch!=OLD.epoch BEGIN
        SELECT RAISE(ABORT,'Recovery sizing generation must advance'); END""",
    "recovery_generation_delete": """CREATE TRIGGER recovery_generation_delete
        BEFORE DELETE ON recovery_sizing_generation BEGIN
        SELECT RAISE(ABORT,'Recovery sizing generation cannot be deleted'); END""",
    "recovery_revision_insert": """CREATE TRIGGER recovery_revision_insert
        BEFORE INSERT ON recovery_sizing_revisions
        WHEN NEW.generation!=(SELECT generation FROM recovery_sizing_generation WHERE singleton=1) BEGIN
        SELECT RAISE(ABORT,'Recovery sizing revision must name current generation'); END""",
    "recovery_revision_update": """CREATE TRIGGER recovery_revision_update
        BEFORE UPDATE ON recovery_sizing_revisions BEGIN
        SELECT RAISE(ABORT,'Recovery sizing revisions cannot be updated'); END""",
}
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _name = "recovery_sizing_" + _operation.lower()
    _identity = "NEW.forecast_id" if _operation == "INSERT" else "OLD.forecast_id"
    _remove = f"DELETE FROM recovery_sizing_revisions WHERE forecast_id={_identity};"
    if _operation == "UPDATE":
        _remove += " DELETE FROM recovery_sizing_revisions WHERE forecast_id=NEW.forecast_id;"
    _add = """INSERT INTO recovery_sizing_revisions VALUES(
        NEW.forecast_id,(SELECT generation FROM recovery_sizing_generation WHERE singleton=1));""" if _operation != "DELETE" else ""
    _RECOVERY_ROW_SCHEMA[_name] = f"""CREATE TRIGGER {_name} AFTER {_operation} ON intraday_intentions BEGIN
        UPDATE recovery_sizing_generation SET generation=generation+1 WHERE singleton=1;
        SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'Missing recovery sizing generation') END;
        {_remove}
        {_add}
    END"""
del _operation, _name, _identity, _remove, _add


class _RecoveryTimings:
    """Bounded, private aggregate timings; no forecast or account values.

    Group totals include connection entry, commit/rollback and close. Named
    inner phases are subsets, so their totals must not be added to group_total.
    Diagnostic clock failures never change a recovery result or exception.
    """
    _STAGES = frozenset({"group_total", "writer_lock", "forecast_read",
                         "forecast_validate", "sizing_read", "sizing_validate",
                         "writer_recheck", "projection_append"})

    def __init__(self):
        self.phases = {}
        self.unavailable = False

    def _now(self):
        try:
            value = time.monotonic()
            if type(value) in {int, float} and math.isfinite(value):
                return value
        except Exception:
            pass
        self.unavailable = True
        return None

    @contextmanager
    def measure(self, stage):
        started = self._now()
        try:
            yield
        finally:
            ended = self._now()
            try:
                if stage not in self._STAGES or started is None or ended is None or ended < started:
                    self.unavailable = True
                else:
                    elapsed = ended - started
                    if not math.isfinite(elapsed):
                        self.unavailable = True
                    else:
                        value = self.phases.setdefault(stage, dict(count=0, total_seconds=0., maximum_seconds=0.))
                        total = value["total_seconds"] + elapsed
                        if not math.isfinite(total):
                            self.unavailable = True
                        else:
                            value["count"] += 1
                            value["total_seconds"] = total
                            value["maximum_seconds"] = max(value["maximum_seconds"], elapsed)
            except Exception:
                self.unavailable = True

    def snapshot(self):
        return dict(schema_version="intraday-recovery-phase-timings-v1",
                    phases={name: {key: round(value, 6) if type(value) is float else value
                                   for key, value in phase.items()}
                            for name, phase in self.phases.items()},
                    diagnostics_unavailable=self.unavailable)


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
            db.execute("BEGIN IMMEDIATE")
            if not self._row_schema(db):
                for statement in _RECOVERY_ROW_SCHEMA.values():
                    db.execute(statement)
                db.execute("INSERT INTO recovery_sizing_generation VALUES(1,0,lower(hex(randomblob(16))))")
                db.execute("INSERT INTO recovery_sizing_revisions SELECT forecast_id,0 FROM intraday_intentions")
            self._row_generation(db)
        self._recovery_row_cache = OrderedDict()
        self._recovery_row_identity = None
        self._recovery_row_highwater = 0

    @staticmethod
    def _row_schema(db):
        names = tuple(_RECOVERY_ROW_SCHEMA)
        rows = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name IN (" +
                               ",".join("?" for _ in names) + ")", names))
        if rows and rows != _RECOVERY_ROW_SCHEMA:
            raise ContractError("Recovery sizing revision schema changed")
        return rows

    @classmethod
    def _row_generation(cls, db):
        if cls._row_schema(db) != _RECOVERY_ROW_SCHEMA:
            raise ContractError("Recovery sizing revision schema is missing")
        rows = db.execute("SELECT singleton,generation,epoch FROM recovery_sizing_generation").fetchall()
        if (len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int or rows[0][1] < 0
                or not isinstance(rows[0][2], str) or len(rows[0][2]) != 32
                or any(c not in "0123456789abcdef" for c in rows[0][2])):
            raise ContractError("Recovery sizing generation is invalid")
        return rows[0][1], rows[0][2]

    def _revision_sizing(self, db, forecast_id, measure):
        """Read a coherent row marker; validate cold/changed original bytes.

        Called under the store forecast-cache lock and pinned write transaction.
        No cache key uses timestamps, row length, or another original's identity.
        """
        with measure("sizing_read"):
            generation, epoch = self._row_generation(db)
            stat = self.store.path.stat()
            physical = (stat.st_dev, stat.st_ino, db.execute("PRAGMA schema_version").fetchone()[0], epoch)
            if physical != self._recovery_row_identity:
                self._recovery_row_cache.clear()
                self._recovery_row_highwater = 0
                self._recovery_row_identity = physical
            if generation < self._recovery_row_highwater:
                raise ContractError("Recovery sizing generation regressed")
            indexed = db.execute("SELECT i.forecast_id,i.id,r.generation FROM intraday_intentions i "
                "LEFT JOIN recovery_sizing_revisions r ON r.forecast_id=i.forecast_id "
                "WHERE i.forecast_id=?", (forecast_id,)).fetchone()
            if indexed is None:
                return None, None, generation
            revision = indexed[2]
            if type(revision) is not int or not 0 <= revision <= generation:
                raise ContractError("Recovery sizing row revision is invalid")
            signature = tuple(indexed)
            previous = self._recovery_row_cache.get(forecast_id)
            if previous is not None and previous[0] == signature:
                row = None
            else:
                row = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",
                                 (forecast_id,)).fetchone()
                if row is None or row[:2] != indexed[:2]:
                    raise ContractError("Recovery sizing indexed identity changed")
        with measure("sizing_validate"):
            if row is None:
                _, side, desired, calculated_at = previous
                value = {"side": side, "desired_quantity": desired, "sizing": {"calculated_at": calculated_at}}
            else:
                # A changed marker must retain the original full semantic check,
                # even if its raw bytes match an earlier single-call cache entry.
                getattr(self, "_recovery_sizing_cache", {}).pop(forecast_id, None)
                value = self._recovery_sizing(row)
                sizing = value.get("sizing") if isinstance(value, dict) else None
                if (all(isinstance(item, str) for item in row) and isinstance(sizing, dict)
                        and isinstance(value.get("side"), str) and type(value.get("desired_quantity")) is int
                        and isinstance(sizing.get("calculated_at"), str)):
                    self._recovery_row_cache[forecast_id] = (
                        signature, value["side"], value["desired_quantity"], sizing["calculated_at"])
                    if len(self._recovery_row_cache) > _RECOVERY_SIZING_CACHE_LIMIT:
                        self._recovery_row_cache.popitem(last=False)
        return (indexed[0], indexed[1], None), value, generation

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

    def _recovery_sizing(self, row):
        """Reuse only scalar sizing facts from exact validated original bytes.

        Recovery never needs the frozen model/research trees in sizing. Every
        read still hashes those bytes and their indexed identities; a cold or
        changed row takes the original full semantic validator. Other readers
        continue to receive and validate the complete immutable intention.
        """
        if row is None:
            return self._value(row)
        forecast_id, key, raw = row
        signature = ((forecast_id, key, hashlib.sha256(raw.encode()).digest())
                     if all(isinstance(item, str) for item in row) else None)
        cache = getattr(self, "_recovery_sizing_cache", None)
        if cache is None:
            cache = self._recovery_sizing_cache = OrderedDict()
        previous = cache.get(forecast_id) if signature is not None else None
        if previous is not None and previous[0] == signature:
            _, side, desired, calculated_at = previous
            return {"side": side, "desired_quantity": desired,
                    "sizing": {"calculated_at": calculated_at}}
        value = self._value(row)
        sizing = value.get("sizing") if isinstance(value, dict) else None
        if (signature is not None and isinstance(sizing, dict)
                and isinstance(value.get("side"), str)
                and type(value.get("desired_quantity")) is int
                and isinstance(sizing.get("calculated_at"), str)):
            cache[forecast_id] = (signature, value["side"], value["desired_quantity"], sizing["calculated_at"])
            if len(cache) > _RECOVERY_SIZING_CACHE_LIMIT:
                cache.popitem(last=False)
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
               capacity=None, order=None, scope_to_intention=False):
        """Append one idempotent projection; execution counts require exact evidence.

        ``order`` is a native ledger ReservationState with complete identified
        fills. Its submission count remains unknown when acceptance is unknown.
        The native/account ledgers, not this projection, own broker recovery.
        ``scope_to_intention`` binds execution observations to the immutable
        sizing available in this transaction; unsized identities stay unchanged.
        Protected unchanged sizing rows reuse only scalar facts. The supplied
        forecast still requires complete equality with its validated original.
        """
        identity(event_key); identity(disposition); identity(kind)
        at = instant(recorded_at)
        if at < instant(forecast["issued_at"]) or not isinstance(evidence, dict) or not evidence:
            raise ContractError("Intention event needs available explicit evidence")
        # Share recovery's cache-lock -> connection ordering and pinned marker
        # snapshot. Full caller equality cannot be certified by scalar headers.
        with self.store._execution_forecast_lock:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._forecast(db, forecast)
                row, intention, generation = self._revision_sizing(
                    db, forecast["forecast_id"], lambda stage: nullcontext())
                result = self._append_validated(db, forecast, row, intention,
                    event_key=event_key, at=at, disposition=disposition,
                    evidence=evidence, kind=kind, capacity=capacity, order=order,
                    scope_to_intention=scope_to_intention)
            # A failed append, rollback or transaction exit cannot publish a
            # committed sizing watermark for an unsuccessful projection.
            self._recovery_row_highwater = generation
            return result

    def record_recovery(self, forecast_id, *, actor, event_key, recorded_at, disposition, evidence, kind, order):
        """Project an own order from its original, never from a caller's forecast.

        Every read hashes exact original bytes and indexed identities. The
        store's bounded scalar cache skips only repeated model-tree decoding;
        first reads and changed rows retain full semantic validation.
        """
        identity(forecast_id); identity(event_key); identity(disposition); identity(kind)
        if actor not in {"Atlas", "Scout"} or order is None:
            raise ContractError("Recovery requires an explicit local actor")
        at = instant(recorded_at)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                             "FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
            if row is None:
                raise ContractError("Native 15m order lacks original persisted intention")
            forecast, _ = self.store._forecast_projection(row)
            if forecast["pc"] != actor:
                raise ContractError("Recovery forecast belongs to another actor")
            if at < instant(forecast["issued_at"]) or not isinstance(evidence, dict) or not evidence:
                raise ContractError("Intention event needs available explicit evidence")
            return self._append(db, forecast, event_key=event_key, at=at,
                disposition=disposition, evidence=evidence, kind=kind, order=order, required_sizing=True)

    def record_recovery_group(self, forecast_id, *, actor, observations, timings=None):
        """Project every native attempt for one original in a pinned transaction.

        Cold/changed originals retain full semantic validation. Warm originals
        reuse only scalar facts after exact revision schema, epoch and indexed
        identity checks in this transaction. Every attempt still validates its
        projection identity, evidence and chronology. The transaction covers
        one forecast, rather than the entire own history.
        """
        identity(forecast_id)
        if actor not in {"Atlas", "Scout"}:
            raise ContractError("Recovery requires an explicit local actor")
        observations = tuple(dict(item) for item in observations)
        if not observations:
            return []
        measure = timings.measure if isinstance(timings, _RecoveryTimings) else lambda stage: nullcontext()
        def originals(db, measured):
            with measured("forecast_read"):
                headers = self.store._revision_forecast_headers(db, ["f.id=?"], [forecast_id])
            with measured("forecast_validate"):
                if not headers:
                    raise ContractError("Native 15m order lacks original persisted intention")
                forecast = headers[0]
                if forecast["pc"] != actor:
                    raise ContractError("Recovery forecast belongs to another actor")
            intention_row, intention, generation = self._revision_sizing(db, forecast_id, measured)
            if intention is None:
                raise ContractError("Native 15m order lacks original persisted intention")
            return forecast, intention_row, intention, generation

        # Cold semantic validation reads large scientific originals. Keep that
        # work in a read snapshot so collectors/scorers can keep writing. The
        # write transaction still rechecks every protected revision/identity;
        # a concurrent change invalidates the caches and takes full validation.
        # Match chronology's cache-lock -> connection ordering throughout.
        with measure("group_total"), self.store._execution_forecast_lock:
            with self.store.connect() as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                originals(db, measure)
            with self.store.connect() as db:
                with measure("writer_lock"):
                    db.execute("BEGIN IMMEDIATE")
                with measure("writer_recheck"):
                    forecast, intention_row, intention, generation = originals(
                        db, lambda stage: nullcontext())
                results = []
                with measure("projection_append"):
                    for item in observations:
                        identity(item["event_key"]); identity(item["disposition"]); identity(item["kind"])
                        if item["order"] is None:
                            raise ContractError("Recovery requires an explicit local actor")
                        at = instant(item["recorded_at"])
                        if at < instant(forecast["issued_at"]) or not isinstance(item["evidence"], dict) or not item["evidence"]:
                            raise ContractError("Intention event needs available explicit evidence")
                        results.append(self._append_validated(db, forecast, intention_row, intention,
                            event_key=item["event_key"], at=at, disposition=item["disposition"],
                            evidence=item["evidence"], kind=item["kind"], order=item["order"]))
            # Publish this watermark only after successful transaction exit.
            self._recovery_row_highwater = generation
            return results

    def _append(self, db, forecast, *, event_key, at, disposition, evidence, kind,
                capacity=None, order=None, required_sizing=False, scope_to_intention=False):
        intention_row = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",
                                   (forecast["forecast_id"],)).fetchone()
        intention = (self._recovery_sizing(intention_row) if required_sizing
                     else self._value(intention_row))
        if required_sizing and intention is None:
            raise ContractError("Native 15m order lacks original persisted intention")
        return self._append_validated(db, forecast, intention_row, intention,
            event_key=event_key, at=at, disposition=disposition, evidence=evidence, kind=kind,
            capacity=capacity, order=order, scope_to_intention=scope_to_intention)

    def _append_validated(self, db, forecast, intention_row, intention, *, event_key, at,
                          disposition, evidence, kind, capacity=None, order=None, scope_to_intention=False):
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
        key_parts = [forecast["forecast_id"], event_key]
        if scope_to_intention and intention is not None:
            key_parts.append({"intention_id": intention_row[1]})
        key = digest(key_parts)
        value.update(event_identity=key, recorded_at=at.isoformat())
        old = db.execute("SELECT i.id,i.payload,i.recorded_at FROM intraday_intention_events e JOIN intents i ON i.id=e.intent_entry_id WHERE e.event_key=?", (key,)).fetchone()
        if old:
            previous = json.loads(old[1])
            value["recorded_at"] = previous["recorded_at"]
            if digest(previous) != old[0] or previous["recorded_at"] != old[2] or encoded(value) != old[1]:
                raise ContractError("Intention event identity reused with changed evidence")
            return json.loads(old[1])
        # Fresh projections cannot attach sizing before it was available.
        # Exact immutable receipt replay above keeps its original time.
        if intention is not None and at < instant(intention["sizing"]["calculated_at"]):
            raise ContractError("Intention event cannot be backdated before sizing")
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
