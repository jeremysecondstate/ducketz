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
import sqlite3
import time
from dataclasses import asdict

from .intentions import IntentionStore as _OriginalIntentionStore
from .direction_metadata import FIELDS as _DIRECTION_FIELDS, recorded_direction
from . import ordinary_diagnostics as _ordinary

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
                         "projection_append"})
    _DETAILS = frozenset({"preflight_connection", "writer_connection",
        "forecast_revision_metadata", "forecast_physical_identity", "forecast_index_read",
        "forecast_payload_read", "forecast_payload_validate", "sizing_revision_metadata",
        "sizing_physical_identity", "sizing_index_read", "sizing_payload_read", "sizing_payload_validate"})
    _COUNTS = frozenset(prefix + suffix for prefix in ("forecast_", "sizing_")
        for suffix in ("cache_hits", "cache_misses", "payload_reads", "text_characters", "blob_bytes"))

    def __init__(self):
        self.phases = {}
        self.unavailable = False
        self.read_details = {}
        self.workload = {}
        self.read_unavailable = self.thread_cpu_unavailable = False

    def _detail_clock(self, name):
        try:
            value = getattr(time, name)()
            if type(value) in {int, float} and math.isfinite(value):
                return value
        except Exception:
            pass
        if name == "thread_time":
            self.thread_cpu_unavailable = True
        else:
            self.read_unavailable = True
        return None

    @contextmanager
    def detail(self, stage):
        """Fixed nested read seams; elapsed time is not proof of a SQL wait.

        Connection totals include their bodies and exit/commit. Detail stages
        are subsets of the original phases, not additional elapsed work.
        Thread CPU excludes time spent descheduled, waiting for IO or the GIL;
        a low CPU ratio does not distinguish those causes.
        """
        started, cpu = self._detail_clock("monotonic"), self._detail_clock("thread_time")
        try:
            yield
        finally:
            ended, cpu_end = self._detail_clock("monotonic"), self._detail_clock("thread_time")
            try:
                if stage not in self._DETAILS:
                    self.read_unavailable = True
                else:
                    for first, last, prefix in ((started, ended, ""), (cpu, cpu_end, "thread_cpu_")):
                        if first is None or last is None or last < first or not math.isfinite(last - first):
                            if prefix:
                                self.thread_cpu_unavailable = True
                            else:
                                self.read_unavailable = True
                            continue
                        value = self.read_details.setdefault(stage, {})
                        count, total, maximum = prefix + "count", prefix + "total_seconds", prefix + "maximum_seconds"
                        elapsed = last - first
                        updated = value.get(total, 0.) + elapsed
                        if not math.isfinite(updated):
                            self.read_unavailable = True
                            continue
                        value[count] = value.get(count, 0) + 1
                        value[total] = updated
                        value[maximum] = max(value.get(maximum, 0.), elapsed)
            except Exception:
                self.read_unavailable = True

    def count(self, name, amount=1):
        try:
            if name not in self._COUNTS or type(amount) is not int or amount < 0:
                self.read_unavailable = True
            else:
                self.workload[name] = self.workload.get(name, 0) + amount
        except Exception:
            self.read_unavailable = True

    def payload(self, prefix, raw):
        # O(1) lengths of the row already read: no encode, new query or tree.
        # Text characters are deliberately not presented as UTF-8 byte counts.
        if prefix not in {"forecast_", "sizing_"}:
            self.read_unavailable = True
            return
        self.count(prefix + "payload_reads")
        if isinstance(raw, str):
            self.count(prefix + "text_characters", len(raw))
        elif isinstance(raw, bytes):
            self.count(prefix + "blob_bytes", len(raw))

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
                    diagnostics_unavailable=self.unavailable,
                    read_details=dict(schema_version="intraday-recovery-read-details-v1",
                        phases={name: {key: round(value, 6) if type(value) is float else value
                                       for key, value in phase.items()}
                                for name, phase in self.read_details.items()},
                        workload=dict(self.workload), diagnostics_unavailable=self.read_unavailable,
                        thread_cpu_unavailable=self.thread_cpu_unavailable))


class IntentionStore(_OriginalIntentionStore):
    def _projection_direction(self, db, forecast):
        """Carry original policy through a canonical scalar recovery header.

        The canonical store deliberately keeps policy out of its execution
        header cache. Revalidate that original with its existing guarded
        projection, then extract only saved scalar policy fields in SQLite.
        Absent historical fields stay absent; no current policy is assigned.
        """
        if any(field in forecast for field in _DIRECTION_FIELDS):
            return recorded_direction(forecast)
        row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                         "FROM forecasts WHERE id=?", (forecast["forecast_id"],)).fetchone()
        if row is None:
            raise ContractError("Intention requires the original saved forecast")
        header, _ = self.store._forecast_projection(row)
        if any(header.get(key) != forecast.get(key) for key in ("forecast_id", "pc", "symbol", "cycle_id")):
            raise ContractError("Recovery header differs from original forecast")
        fields = ("probability_not_down", *_DIRECTION_FIELDS)
        expressions = ["json_extract(payload,'$." + field + "')" for field in fields]
        expressions += ["json_type(payload,'$." + field + "')" for field in _DIRECTION_FIELDS]
        values = db.execute("SELECT " + ",".join(expressions) + " FROM forecasts WHERE id=?",
                            (forecast["forecast_id"],)).fetchone()
        original = {"probability_not_down": values[0]}
        original.update({field: values[index + 1] for index, field in enumerate(_DIRECTION_FIELDS)
                         if values[len(fields) + index] is not None})
        return recorded_direction(original)

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

    def _revision_sizing(self, db, forecast_id, measure, read_timings=None):
        """Read a coherent row marker; validate cold/changed original bytes.

        Called under the store forecast-cache lock and a pinned transaction.
        No cache key uses timestamps, row length, or another original's identity.
        """
        detail = read_timings.detail if read_timings is not None else lambda stage: nullcontext()
        with measure("sizing_read"):
            with detail("sizing_revision_metadata"):
                generation, epoch = self._row_generation(db)
            with detail("sizing_physical_identity"):
                stat = self.store.path.stat()
                physical = (stat.st_dev, stat.st_ino, db.execute("PRAGMA schema_version").fetchone()[0], epoch)
            if physical != self._recovery_row_identity:
                self._recovery_row_cache.clear()
                self._recovery_row_highwater = 0
                self._recovery_row_identity = physical
            if generation < self._recovery_row_highwater:
                raise ContractError("Recovery sizing generation regressed")
            with detail("sizing_index_read"):
                indexed = db.execute("SELECT i.forecast_id,i.id,r.generation FROM intraday_intentions i "
                    "LEFT JOIN recovery_sizing_revisions r ON r.forecast_id=i.forecast_id "
                    "WHERE i.forecast_id=?", (forecast_id,)).fetchone()
            if indexed is None:
                return None, None, generation
            revision = indexed[2]
            if type(revision) is not int or not 0 <= revision <= generation:
                raise ContractError("Recovery sizing row revision is invalid")
            signature = tuple(indexed)
            # A separate authenticated scalar proof can survive a manual
            # restart. Exact source/schema/epoch/revision checks above still
            # guard it; uncertainty only takes the original full validator.
            from .sizing_header_cache import DurableSizingHeaders
            durable = DurableSizingHeaders(self.store.path)
            opened = getattr(self.store, "_connection_source_identities", {}).get(id(db))
            cache_source_certain = opened is not None and opened == (stat.st_dev, stat.st_ino)
            try:
                durable_identity = (str(self.store.path.resolve()), *physical)
            except OSError:
                durable_identity = None
                cache_source_certain = False
            previous = self._recovery_row_cache.get(forecast_id)
            if previous is None or previous[0] != signature:
                saved = durable.load(durable_identity, signature) if cache_source_certain else None
                if saved is not None:
                    previous = (signature, saved["side"], saved["desired_quantity"], saved["calculated_at"])
                    self._recovery_row_cache[forecast_id] = previous
                    if len(self._recovery_row_cache) > _RECOVERY_SIZING_CACHE_LIMIT:
                        self._recovery_row_cache.popitem(last=False)
            if previous is not None and previous[0] == signature:
                row = None
                if read_timings is not None:
                    read_timings.count("sizing_cache_hits")
            else:
                if read_timings is not None:
                    read_timings.count("sizing_cache_misses")
                with detail("sizing_payload_read"):
                    row = db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?",
                                     (forecast_id,)).fetchone()
                if row is None or row[:2] != indexed[:2]:
                    raise ContractError("Recovery sizing indexed identity changed")
                if read_timings is not None:
                    read_timings.payload("sizing_", row[2])
        with measure("sizing_validate"):
            if row is None:
                _, side, desired, calculated_at = previous
                value = {"side": side, "desired_quantity": desired, "sizing": {"calculated_at": calculated_at}}
            else:
                # A changed marker must retain the original full semantic check,
                # even if its raw bytes match an earlier single-call cache entry.
                getattr(self, "_recovery_sizing_cache", {}).pop(forecast_id, None)
                with detail("sizing_payload_validate"):
                    value = self._recovery_sizing(row)
                sizing = value.get("sizing") if isinstance(value, dict) else None
                if (all(isinstance(item, str) for item in row) and isinstance(sizing, dict)
                        and isinstance(value.get("side"), str) and type(value.get("desired_quantity")) is int
                        and isinstance(sizing.get("calculated_at"), str)):
                    self._recovery_row_cache[forecast_id] = (
                        signature, value["side"], value["desired_quantity"], sizing["calculated_at"])
                    if len(self._recovery_row_cache) > _RECOVERY_SIZING_CACHE_LIMIT:
                        self._recovery_row_cache.popitem(last=False)
                    # Cache only facts produced by the full original validator.
                    # Recheck the pathname before writing the optional sidecar;
                    # it never mutates scientific or execution source rows.
                    if cache_source_certain:
                        try:
                            current_stat = self.store.path.stat()
                        except OSError:
                            pass
                        else:
                            if opened == (current_stat.st_dev, current_stat.st_ino):
                                durable.save(durable_identity, signature, dict(
                                    forecast_id=indexed[0], intention_id=indexed[1],
                                    side=value["side"], desired_quantity=value["desired_quantity"],
                                    calculated_at=sizing["calculated_at"]))
                    # Release the fully validated research tree in this read
                    # preflight, before the final transaction reserves a writer.
                    # Both passes need only these same protected scalar facts.
                    value = {"side": value["side"], "desired_quantity": value["desired_quantity"],
                             "sizing": {"calculated_at": sizing["calculated_at"]}}
        return (indexed[0], indexed[1], None), value, generation

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

    def record(self, forecast, *, event_key, recorded_at, disposition, evidence, kind,
               capacity=None, order=None, scope_to_intention=False, require_original_intention=False):
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
        # Decode/hash the complete original without reserving SQLite's writer.
        # This independently decoded tree belongs only to this call, and full
        # caller equality (including nested/extra fields) is checked again in
        # the final writer. Scalar headers cannot certify caller equivalence.
        with _ordinary.context(self.store._execution_forecast_lock, "record_cache_lock", "record_cache_unlock"):
            marker, original = self._ordinary_forecast_preflight(forecast)
            with _ordinary.context(self.store.connect(), "writer_connection_open", "writer_commit_close") as db:
                with _ordinary.phase("writer_lock"):
                    db.execute("BEGIN IMMEDIATE")
                with _ordinary.phase("writer_forecast_recheck"):
                    self._ordinary_forecast_recheck(db, forecast, marker, original)
                del original  # Do not overlap forecast and sizing research trees.
                with _ordinary.phase("sizing_read"):
                    row, intention, generation = self._revision_sizing(
                        db, forecast["forecast_id"], lambda stage: nullcontext())
                if require_original_intention and intention is None:
                    raise ContractError("Local order lacks its original immutable intention")
                with _ordinary.phase("projection_append"):
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

        Cold/changed originals first retain full semantic validation in a read
        transaction, without reserving SQLite's writer. The final write
        transaction repeats the same revision schema, epoch and indexed identity
        checks; changed originals still take full validation. Only safe scalar
        caches cross that boundary. Every attempt validates its projection
        identity, evidence and chronology before an atomic group append.
        """
        identity(forecast_id)
        if actor not in {"Atlas", "Scout"}:
            raise ContractError("Recovery requires an explicit local actor")
        observations = tuple(dict(item) for item in observations)
        if not observations:
            return []
        measure = timings.measure if isinstance(timings, _RecoveryTimings) else lambda stage: nullcontext()
        read_timings = timings if isinstance(timings, _RecoveryTimings) else None
        detail = read_timings.detail if read_timings is not None else lambda stage: nullcontext()
        # Match chronology's cache-lock -> connection ordering. Cold payload
        # reads/decoding need no reserved writer. Close that snapshot before the
        # final writer repeats every protected identity check and appends.
        # Timed read/validation phases include both passes; group_total includes
        # both connection exits, while writer_lock measures acquisition only.
        with measure("group_total"), self.store._execution_forecast_lock:
            with detail("preflight_connection"), self.store.connect() as db:
                db.execute("BEGIN")
                with measure("forecast_read"):
                    headers = self.store._revision_forecast_headers(db, ["f.id=?"], [forecast_id],
                                                                    read_timings=read_timings)
                with measure("forecast_validate"):
                    if not headers:
                        raise ContractError("Native 15m order lacks original persisted intention")
                    if headers[0]["pc"] != actor:
                        raise ContractError("Recovery forecast belongs to another actor")
                _, intention, _ = self._revision_sizing(db, forecast_id, measure, read_timings)
                if intention is None:
                    raise ContractError("Native 15m order lacks original persisted intention")
            with detail("writer_connection"), self.store.connect() as db:
                with measure("writer_lock"):
                    db.execute("BEGIN IMMEDIATE")
                # Includes protected marker/header lookup AND full semantic
                # validation on cold/changed forecasts. It is not pure SQL I/O.
                with measure("forecast_read"):
                    headers = self.store._revision_forecast_headers(db, ["f.id=?"], [forecast_id],
                                                                    read_timings=read_timings)
                with measure("forecast_validate"):
                    if not headers:
                        raise ContractError("Native 15m order lacks original persisted intention")
                    forecast = headers[0]
                    if forecast["pc"] != actor:
                        raise ContractError("Recovery forecast belongs to another actor")
                intention_row, intention, generation = self._revision_sizing(db, forecast_id, measure, read_timings)
                if intention is None:
                    raise ContractError("Native 15m order lacks original persisted intention")
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
        # Preserve canonical frozen actions and each first immutable sizing.
        side = intention["side"] if intention is not None else self._original_action(db, forecast)
        value = dict(version=VERSION, forecast_id=forecast["forecast_id"], symbol=forecast["symbol"],
            side=side, kind=kind, desired_quantity=desired, adjusted_quantity=None,
            disposition=disposition, evidence=evidence, submission_status="not_submitted",
            submitted_quantity=0, filled_quantity=0, realized_pnl=None, **self._projection_direction(db, forecast))
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
