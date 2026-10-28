"""Host-local shadow evidence. Forecasts, inputs and outcome revisions are immutable.

This module opens no broker or account connection. Optional publication proofs
validate already-existing local revision metadata without initializing execution
state or making it a condition of scientific publication. Operational cycle
summaries may advance; every transition also has an append-only event.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock

from .prediction import ContractError, STEP, TARGET_VERSION, digest, encoded, instant, number
from .direction_metadata import FIELDS as DIRECTION_FIELDS, recorded_direction, stamp_new_direction


# These local change markers let execution recheck chronology without rereading
# unchanged scientific model trees while a broker quote ages. Forecast originals
# remain untouched. Every SQL mutation, including updates outside this class,
# advances one generation in the same transaction as the original row.
_EXECUTION_FORECAST_SCHEMA = {
    "execution_forecast_generation": """CREATE TABLE execution_forecast_generation(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        generation INTEGER NOT NULL CHECK(generation>=0),
        epoch TEXT NOT NULL CHECK(length(epoch)=32))""",
    "execution_forecast_revisions": """CREATE TABLE execution_forecast_revisions(
        forecast_id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=0))""",
    "execution_generation_insert": """CREATE TRIGGER execution_generation_insert
        BEFORE INSERT ON execution_forecast_generation
        WHEN EXISTS(SELECT 1 FROM execution_forecast_generation) BEGIN
        SELECT RAISE(ABORT,'Execution forecast generation cannot be replaced'); END""",
    "execution_generation_update": """CREATE TRIGGER execution_generation_update
        BEFORE UPDATE ON execution_forecast_generation
        WHEN NEW.singleton!=OLD.singleton OR NEW.generation!=OLD.generation+1
            OR typeof(NEW.generation)!='integer' OR NEW.epoch!=OLD.epoch BEGIN
        SELECT RAISE(ABORT,'Execution forecast generation must advance'); END""",
    "execution_generation_delete": """CREATE TRIGGER execution_generation_delete
        BEFORE DELETE ON execution_forecast_generation BEGIN
        SELECT RAISE(ABORT,'Execution forecast generation cannot be deleted'); END""",
    "execution_revision_insert": """CREATE TRIGGER execution_revision_insert
        BEFORE INSERT ON execution_forecast_revisions
        WHEN NEW.generation!=(SELECT generation FROM execution_forecast_generation WHERE singleton=1) BEGIN
        SELECT RAISE(ABORT,'Execution forecast revision must name current generation'); END""",
    "execution_revision_update": """CREATE TRIGGER execution_revision_update
        BEFORE UPDATE ON execution_forecast_revisions BEGIN
        SELECT RAISE(ABORT,'Execution forecast revisions cannot be updated'); END""",
}
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _name = "execution_forecast_" + _operation.lower()
    _identity = "NEW.id" if _operation == "INSERT" else "OLD.id"
    _remove = f"DELETE FROM execution_forecast_revisions WHERE forecast_id={_identity};"
    if _operation == "UPDATE":
        _remove += " DELETE FROM execution_forecast_revisions WHERE forecast_id=NEW.id;"
    _add = """INSERT INTO execution_forecast_revisions VALUES(
        NEW.id,(SELECT generation FROM execution_forecast_generation WHERE singleton=1));""" if _operation != "DELETE" else ""
    _EXECUTION_FORECAST_SCHEMA[_name] = f"""CREATE TRIGGER {_name} AFTER {_operation} ON forecasts BEGIN
        UPDATE execution_forecast_generation SET generation=generation+1 WHERE singleton=1;
        SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'Missing execution forecast generation') END;
        {_remove}
        {_add}
    END"""
del _operation, _name, _identity, _remove, _add


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class ShadowStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, pc TEXT, started_at TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, cycle_id TEXT, symbol TEXT, recorded_at TEXT, kind TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY, pc TEXT, symbol TEXT, cycle_id TEXT, reference_at TEXT, issued_at TEXT, payload TEXT NOT NULL, UNIQUE(cycle_id,symbol));
                CREATE TABLE IF NOT EXISTS outcomes(id TEXT PRIMARY KEY, forecast_id TEXT, observed_at TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, forecast_id TEXT, recorded_at TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candles(id TEXT PRIMARY KEY, symbol TEXT, opened_at TEXT, available_at TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS symbol_inputs(cycle_id TEXT,symbol TEXT,snapshot_id TEXT,PRIMARY KEY(cycle_id,symbol));
                CREATE INDEX IF NOT EXISTS outcomes_forecast ON outcomes(forecast_id);
                CREATE INDEX IF NOT EXISTS forecasts_target ON forecasts(pc,symbol,reference_at);
            """)
            db.execute("BEGIN IMMEDIATE")
            present = self._execution_forecast_schema(db)
            if not present:
                for statement in _EXECUTION_FORECAST_SCHEMA.values():
                    db.execute(statement)
                db.execute("INSERT INTO execution_forecast_generation VALUES(1,0,lower(hex(randomblob(16))))")
                db.execute("INSERT INTO execution_forecast_revisions SELECT id,0 FROM forecasts")
            self._execution_forecast_generation(db)
        self._execution_forecast_lock = RLock()
        self._execution_forecast_cache = OrderedDict()
        self._execution_forecast_highwater = 0
        self._execution_forecast_identity = None

    @contextmanager
    def connect(self):
        """Commit/rollback deterministically, then close even on interruption."""
        try:
            before = self.path.stat()
        except OSError:
            before = None
        options = {"timeout": getattr(self, "connection_timeout", 30)}
        if getattr(self, "connection_factory", None) is not None:
            options["factory"] = self.connection_factory
        db = sqlite3.connect(self.path, **options)
        identities = getattr(self, "_connection_source_identities", None)
        if identities is None:
            identities = self._connection_source_identities = {}
        identities[id(db)] = None
        try:
            try:
                opened = self.path.stat()
                if before is not None and (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino):
                    identities[id(db)] = (opened.st_dev, opened.st_ino)
            except OSError:
                pass
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            identities.pop(id(db), None)
            db.close()

    @staticmethod
    def _snapshot_value(key, raw):
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("schema") == "scout-external-input-v1":
            from .service_handoff import full_snapshot
            return full_snapshot(key, value)
        if not isinstance(value, dict) or digest(value) != key:
            raise ContractError("Persisted snapshot evidence hash changed")
        return value

    @staticmethod
    def _forecast_value(row):
        key, pc, symbol, cycle_id, reference_at, issued_at, raw = row
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ContractError("Persisted forecast evidence is invalid")
        fingerprint = dict(value)
        identity = fingerprint.pop("forecast_id", None)
        if (identity != key or digest(fingerprint) != key
                or any(value.get(field) != indexed for field, indexed in
                       (("pc", pc), ("symbol", symbol), ("cycle_id", cycle_id),
                        ("reference_at", reference_at), ("issued_at", issued_at)))):
            raise ContractError("Persisted forecast evidence hash or indexed identity changed")
        return value

    @staticmethod
    def _outcome_value(row):
        key, forecast_id, observed_at, raw = row
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ContractError("Persisted outcome evidence is invalid")
        evidence = dict(value)
        revision = evidence.pop("revision_id", None)
        observed = evidence.pop("observed_at", None)
        if (revision != key or digest(evidence) != key or observed != observed_at
                or value.get("forecast_id") != forecast_id):
            raise ContractError("Persisted outcome evidence hash or indexed identity changed")
        return value

    def event(self, cycle_id, kind, payload, *, symbol=None, recorded_at=None):
        with self.connect() as db:
            db.execute("INSERT INTO events(cycle_id,symbol,recorded_at,kind,payload) VALUES(?,?,?,?,?)",
                       (cycle_id, symbol, instant(recorded_at or utcnow()).isoformat(), kind, encoded(payload)))

    def scoring_audit_notices(self, session_id):
        """Read exact prior diagnostic notices for this logical session only.

        These identities deduplicate notifications, never candle validation or
        outcome qualification. Malformed historical rows remain untouched and
        cannot suppress a valid notice. Stream one scan across worker restarts.
        """
        notices = set()
        with self.connect() as db:
            for symbol, raw in db.execute(
                    "SELECT symbol,payload FROM events WHERE cycle_id=? AND kind=?",
                    (session_id, "unverified_synthetic_outcome_requires_audit")):
                if type(symbol) is not str or not symbol.strip() or type(raw) is not str or len(raw) > 256:
                    continue
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if type(value) is not dict or set(value) != {"candle_hash", "immutable_original_retained"}:
                    continue
                key = value["candle_hash"]
                if (value["immutable_original_retained"] is True and type(key) is str
                        and len(key) == 64 and all(char in "0123456789abcdef" for char in key)
                        and raw == encoded(value)):
                    notices.add((symbol, key))
        return notices

    def begin_cycle(self, cycle_id, payload):
        payload = dict(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            if old:
                saved = json.loads(old[0])
                for field in ("pc", "reference_at", "configuration_hash", "mode", "symbols"):
                    if saved.get(field) != payload.get(field):
                        raise ContractError("Cycle identity reused with changed configuration")
                return saved
            db.execute("INSERT INTO cycles VALUES(?,?,?,?)",
                       (cycle_id, payload["pc"], payload["started_at"], encoded(payload)))
        self.event(cycle_id, "cycle_started", payload)
        return payload

    def update_cycle(self, cycle_id, changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            if row is None:
                raise ContractError("Unknown cycle")
            value = json.loads(row[0])
            if set(changes) & {"pc", "reference_at", "configuration_hash", "mode", "symbols", "started_at"}:
                raise ContractError("Cycle identity is immutable")
            value.update(changes)
            db.execute("UPDATE cycles SET payload=? WHERE id=?", (encoded(value), cycle_id))
        self.event(cycle_id, "cycle_progress", changes)
        return value

    def freeze_inputs(self, cycle_id, symbol, payload):
        """First validated snapshot wins, including after a crash before inference."""
        payload = json.loads(encoded(payload))
        canonical = encoded(payload)
        key = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT snapshot_id FROM symbol_inputs WHERE cycle_id=? AND symbol=?", (cycle_id, symbol)).fetchone()
            if old:
                row = db.execute("SELECT payload FROM snapshots WHERE id=?", old).fetchone()
                if row is None:
                    raise ContractError("Persisted snapshot evidence is missing")
                return old[0], self._snapshot_value(old[0], row[0])
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?)", (key, canonical))
            recorded = db.execute("SELECT payload FROM snapshots WHERE id=?", (key,)).fetchone()[0]
            # The normalized payload and canonical bytes were validated before
            # entering the writer. Exact readback preserves that proof without
            # decoding and re-encoding the same scientific snapshot again.
            # Existing/colliding or changed stored bytes still take the full
            # original semantic-hash validation path.
            if recorded != canonical or not isinstance(payload, dict):
                self._snapshot_value(key, recorded)
            db.execute("INSERT INTO symbol_inputs VALUES(?,?,?)", (cycle_id, symbol, key))
        return key, payload

    def inputs(self, cycle_id, symbol):
        with self.connect() as db:
            row = db.execute("SELECT s.id,s.payload FROM snapshots s JOIN symbol_inputs i ON i.snapshot_id=s.id WHERE i.cycle_id=? AND i.symbol=?", (cycle_id, symbol)).fetchone()
        return (row[0], self._snapshot_value(row[0], row[1])) if row else None

    def forecast(self, cycle_id, symbol):
        with self.connect() as db:
            row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE cycle_id=? AND symbol=?", (cycle_id, symbol)).fetchone()
        return self._forecast_value(row) if row else None

    @staticmethod
    def _execution_forecast_schema(db):
        from .schema_reads import schema_definitions
        rows = schema_definitions(db, _EXECUTION_FORECAST_SCHEMA)
        if rows and rows != _EXECUTION_FORECAST_SCHEMA:
            raise ContractError("Execution forecast revision schema changed")
        return rows

    @classmethod
    def _execution_forecast_generation(cls, db):
        if cls._execution_forecast_schema(db) != _EXECUTION_FORECAST_SCHEMA:
            raise ContractError("Execution forecast revision schema is missing")
        rows = db.execute("SELECT singleton,generation,epoch FROM execution_forecast_generation").fetchall()
        if (len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int or rows[0][1] < 0
                or not isinstance(rows[0][2], str) or len(rows[0][2]) != 32
                or any(c not in "0123456789abcdef" for c in rows[0][2])):
            raise ContractError("Execution forecast generation is invalid")
        return rows[0][1], rows[0][2]

    def _forecast_projection(self, row):
        """Cache only scalar headers of exact semantically validated evidence."""
        fields = ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at",
                  "mode", "ensemble_status", "target_open", "target_close",
                  "population_contract", "source_dataset", "source_id", "reference_open")
        cache = getattr(self, "_forecast_header_cache", None)
        if cache is None:
            cache = self._forecast_header_cache = OrderedDict()
        # A hash of unsorted JSON is not proof of the semantic digest. New
        # payloads and indexed identities always take the full validator.
        # BLOB/irregular SQLite values retain its original behavior too.
        signature = (*row[:6], hashlib.sha256(row[6].encode()).digest()) if isinstance(row[6], str) else None
        previous = cache.get(row[0])
        if signature is not None and previous is not None and previous[0] == signature:
            return dict(previous[1]), None
        value = self._forecast_value(row)
        projection = {field: value[field] for field in fields if field in value}
        if "reference_open" in projection:
            # Preserve the exact numeric meaning as a scalar cache field. The
            # final scorer still applies the ordinary positive Decimal check.
            projection["reference_open"] = str(projection["reference_open"])
        if signature is not None and all(item is None or isinstance(item, str) for item in projection.values()):
            cache[row[0]] = (signature, tuple(projection.items()))
            if len(cache) > 4096:
                cache.popitem(last=False)
        return dict(projection), value

    def execution_forecast_headers(self, *, pc, symbol, forecast_ids=None, cache_io_budget_seconds=.25):
        """Revalidate changed originals and reuse unchanged scalar headers.

        Exact trigger definitions and monotonic row revisions are checked in the
        same fresh read transaction as payloads. Cold and changed rows retain
        full semantic/index validation, including obsolete history and BLOBs.
        No read transaction survives this method or spans broker operations.
        """
        clauses, parameters = ["f.pc=?", "f.symbol=?"], [pc, symbol]
        if forecast_ids is not None:
            identities = tuple(dict.fromkeys(forecast_ids))
            if not identities:
                return []
            clauses.append("f.id IN (SELECT value FROM json_each(?))")
            parameters.append(encoded(identities))
        # Serialize cache publication so older concurrent snapshots cannot
        # appear to roll back the forecast generation.
        with self._execution_forecast_lock, self.connect() as db:
            db.execute("BEGIN")
            return self._revision_forecast_headers(db, clauses, parameters, cache_io_budget_seconds=cache_io_budget_seconds)

    def _revision_forecast_headers(self, db, clauses, parameters, stopped=lambda: False, read_timings=None, cache_io_budget_seconds=.25):
        """Reuse exact headers inside the caller's locked read transaction."""
        headers = []
        detail = read_timings.detail if read_timings is not None else lambda stage: nullcontext()
        with detail("forecast_revision_metadata"):
            generation, epoch = self._execution_forecast_generation(db)
        with detail("forecast_physical_identity"):
            try:
                stat = self.path.stat()
                current_physical = (stat.st_dev, stat.st_ino)
            except OSError:
                current_physical = None
            opened = getattr(self, "_connection_source_identities", {}).get(id(db))
            cache_source_certain = opened is not None and opened == current_physical
            physical = opened if opened is not None else (None, id(db))
            identity = (*physical, db.execute("PRAGMA schema_version").fetchone()[0], epoch)
        from .forecast_header_cache import DurableForecastHeaders
        durable = DurableForecastHeaders(self.path, budget_seconds=cache_io_budget_seconds)
        if identity != self._execution_forecast_identity:
            self._execution_forecast_cache.clear()
            self._execution_forecast_highwater = 0
            self._execution_forecast_identity = identity
        if generation < self._execution_forecast_highwater:
            raise ContractError("Execution forecast generation regressed")
        with detail("forecast_index_read"):
            from .forecast_index_reads import forecast_index_rows
            rows = forecast_index_rows(db, clauses, parameters)
        try:
            durable_identity = (str(self.path.resolve()), *identity)
        except OSError:
            durable_identity = None
            cache_source_certain = False
        # Snapshot exact scalar proofs for this locked source inventory. Later
        # misses may evict bounded cache entries while this inventory is still
        # being returned; they cannot invalidate these already checked proofs.
        warm_headers, missing_signatures = {}, []
        for row in rows:
            signature = tuple(row)
            previous = self._execution_forecast_cache.get(row[0])
            if previous is not None and previous[0] == signature:
                warm_headers[signature] = previous[1]
            else:
                missing_signatures.append(signature)
        # Durable reads attest cold/changed originals only. Warm proofs already
        # retain the same physical/schema/epoch/revision checks above and below.
        durable_headers = (durable.load_many(durable_identity, missing_signatures)
            if cache_source_certain and missing_signatures else {})
        validated_for_cache = []
        for row in rows:
            if stopped():
                return []
            revision = row[6]
            if type(revision) is not int or not 0 <= revision <= generation:
                raise ContractError("Execution forecast row revision is invalid")
            signature = (*row[:6], revision)
            previous = warm_headers.get(signature)
            if previous is not None:
                projection = dict(previous)
                if read_timings is not None:
                    read_timings.count("forecast_cache_hits")
            else:
                if read_timings is not None:
                    read_timings.count("forecast_cache_misses")
                projection = durable_headers.get(signature)
                if projection is None:
                    with detail("forecast_payload_read"):
                        original = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                                              "FROM forecasts WHERE id=?", (row[0],)).fetchone()
                    if read_timings is not None and original is not None:
                        read_timings.payload("forecast_", original[6])
                    with detail("forecast_payload_validate"):
                        projection, _ = self._forecast_projection(original)
                    if cache_source_certain:
                        validated_for_cache.append((signature, projection))
                if all(item is None or isinstance(item, str) for item in projection.values()):
                    self._execution_forecast_cache[row[0]] = (signature, tuple(projection.items()))
                    if len(self._execution_forecast_cache) > 4096:
                        self._execution_forecast_cache.popitem(last=False)
            headers.append(projection)
        # Finish validating the entire source inventory before publishing its
        # optional proofs. One private cache transaction avoids spending the
        # bounded budget on a separate commit for every historical original.
        if validated_for_cache:
            try:
                current_stat = self.path.stat()
            except OSError:
                pass
            else:
                if opened == (current_stat.st_dev, current_stat.st_ino):
                    durable.save_many(durable_identity, validated_for_cache)
        self._execution_forecast_highwater = generation
        return headers

    def forecast_by_id(self, forecast_id, *, pc=None):
        """Validate one immutable forecast without loading unrelated history."""
        with self.connect() as db:
            row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
        value = self._forecast_value(row) if row else None
        # Validate indexed identity before scope filtering, so a changed pc
        # column cannot disguise corruption as an ordinary missing forecast.
        return value if value is not None and (pc is None or value["pc"] == pc) else None

    def forecasts(self, *, pc=None, symbol=None, forecast_ids=None):
        # Execution chronology concerns one symbol. Use the existing index to
        # avoid decoding unrelated model payloads while account evidence ages.
        clauses, parameters = [], []
        if pc:
            clauses.append("pc=?")
            parameters.append(pc)
        if symbol is not None:
            clauses.append("symbol=?")
            parameters.append(symbol)
        if forecast_ids is not None:
            identities = tuple(dict.fromkeys(forecast_ids))
            if not identities:
                return []
            clauses.append("id IN (SELECT value FROM json_each(?))")
            parameters.append(encoded(identities))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts" +
                              where + " ORDER BY rowid", parameters).fetchall()
        return [self._forecast_value(row) for row in rows]

    def _published_forecast_header_proof(self, db, value):
        """Bind this producer's already validated INSERT to optional scalars.

        No payload is reread, decoded or hashed here. The canonical scientific
        value was validated and digested above, then supplied directly to the
        INSERT. Only the exact known source triggers may have run since then;
        unfamiliar trigger composition disables this optional optimization.
        """
        try:
            expected = {name: sql for name, sql in _EXECUTION_FORECAST_SCHEMA.items()
                        if sql.lstrip().startswith("CREATE TRIGGER")}
            actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
            recovery_objects = db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'recovery_%' LIMIT 1").fetchone()
            if actual != expected or recovery_objects is not None:
                # Existing installations may also carry independent sizing
                # revisions. Plain forecast-only stores need no such capability.
                # Partial or unfamiliar compositions retain the full reader.
                from . import intentions
                recovery = getattr(intentions, "_RECOVERY_ROW_SCHEMA", None)
                owner = getattr(intentions, "IntentionStore", None)
                schema_reader = getattr(owner, "_row_schema", None)
                generation_reader = getattr(owner, "_row_generation", None)
                if (not isinstance(recovery, dict) or not callable(schema_reader)
                        or not callable(generation_reader) or not schema_reader(db)):
                    return None
                generation_reader(db)
                expected.update({name: sql for name, sql in recovery.items()
                                 if sql.lstrip().startswith("CREATE TRIGGER")})
            if actual != expected:
                return None
            generation, epoch = self._execution_forecast_generation(db)
            opened = getattr(self, "_connection_source_identities", {}).get(id(db))
            stat = self.path.stat()
            if opened is None or opened != (stat.st_dev, stat.st_ino):
                return None
            indexed = db.execute("SELECT f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,r.generation "
                "FROM forecasts f LEFT JOIN execution_forecast_revisions r ON r.forecast_id=f.id WHERE f.id=?",
                (value["forecast_id"],)).fetchone()
            fields = ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")
            if (indexed is None or tuple(value[field] for field in fields) != indexed[:6]
                    or any(not isinstance(item, str) for item in indexed[:6])
                    or type(indexed[6]) is not int or not 0 <= indexed[6] <= generation):
                return None
            fields += ("mode", "ensemble_status", "target_open", "target_close",
                       "population_contract", "source_dataset", "source_id", "reference_open")
            projection = {field: value[field] for field in fields if field in value}
            if "reference_open" in projection:
                projection["reference_open"] = str(projection["reference_open"])
            if any(item is not None and not isinstance(item, str) for item in projection.values()):
                return None
            identity = (str(self.path.resolve()), *opened, db.execute("PRAGMA schema_version").fetchone()[0], epoch)
            return identity, tuple(indexed), projection
        except Exception:
            return None

    def _publish_forecast_header_proof(self, proof):
        """An unavailable optional cache never fails scientific publication."""
        if proof is None:
            return
        try:
            identity, signature, projection = proof
            stat = self.path.stat()
            if identity[1:3] != (stat.st_dev, stat.st_ino):
                return
            from .forecast_header_cache import DurableForecastHeaders
            DurableForecastHeaders(self.path).save_many(identity, ((signature, projection),))
        except Exception:
            pass

    def _validate_forecast_record(self, value, raw):
        """Optional storage contract for the final immutable serialization."""

    def save_forecast(self, payload, *, intent=None):
        # Return exactly the persisted JSON representation. Model metadata can
        # contain tuples; their decoded list form must compare identically on
        # the first issue, restart reuse, and scoring.
        value = json.loads(encoded(payload))
        reference = instant(value["reference_at"])
        if reference.minute % 15 or reference.second or reference.microsecond:
            raise ContractError("Reference must be a quarter-hour opening")
        if instant(value["issued_at"]) < reference + STEP:
            raise ContractError("Forecast issue precedes its completed input candle")
        if value["target_version"] != TARGET_VERSION:
            raise ContractError("Wrong target definition")
        if (instant(value["target_open"]) != reference + 2 * STEP or
                instant(value["target_close"]) != reference + 3 * STEP or
                instant(value["data_through"]) != reference + STEP):
            raise ContractError("Forecast target changed from its original reference")
        if instant(value["information_available_at"]) > instant(value["issued_at"]):
            raise ContractError("Forecast uses future information")
        if value["mode"] in {"shadow", "live"} and value.get("availability_basis") != "observed":
            raise ContractError("Shadow inference needs observed reference availability")
        number(value["reference_open"], positive=True)
        outputs = value["models"]
        if len({m["name"] for m in outputs}) != len(outputs) or len(outputs) != value["configured_count"]:
            raise ContractError("All configured models need independent evidence")
        successful = [m for m in outputs if m["status"] == "success"]
        if not successful or len(successful) != value["successful_count"]:
            raise ContractError("Failed cycle cannot masquerade as a forecast")
        probabilities = [number(m["probability_not_down"]) for m in successful]
        if any(p > 1 for p in probabilities):
            raise ContractError("Invalid model probability")
        expected = sum(probabilities) / len(probabilities)
        if abs(number(value["probability_not_down"]) - expected) > Decimal("1e-12"):
            raise ContractError("Consensus must be the equal arithmetic mean")
        expected_status = "full" if len(successful) == len(outputs) else "incomplete"
        if value["ensemble_status"] != expected_status:
            raise ContractError("Incomplete ensemble must remain visible")
        value["classification"] = "not-down" if expected >= Decimal("0.50") else "down"
        value["execution_eligibility"] = ("manual_live_checks_required" if value["mode"] == "live"
                                          else "unresolved_shadow_only")
        supplied_id = value.pop("forecast_id", None)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE cycle_id=? AND symbol=?", (value["cycle_id"], value["symbol"])).fetchone()
            previous = self._forecast_value(old) if old else None
            if previous is not None:
                # Inherit only omitted store-derived fields. Never stamp an old
                # forecast or reinterpret its action with the loaded policy.
                for field in DIRECTION_FIELDS:
                    if field not in value and field in previous:
                        value[field] = previous[field]
                key = digest(value)
                value["forecast_id"] = key
                if previous != value or supplied_id not in (None, key):
                    raise ContractError("Issued forecast is immutable")
                if intent:
                    original = db.execute("SELECT id,payload FROM intents WHERE forecast_id=? ORDER BY rowid LIMIT 1", (key,)).fetchone()
                    saved_intent = json.loads(original[1]) if original else None
                    if (saved_intent is None or digest(saved_intent) != original[0]
                            or any(field not in saved_intent or saved_intent[field] != item for field, item in intent.items())):
                        raise ContractError("Original forecast intention is immutable")
                return previous
            value = stamp_new_direction(value)
            key = digest(value)
            value["forecast_id"] = key
            intent_value = self._intent(key, value, intent)
            raw = encoded(value)
            self._validate_forecast_record(value, raw)
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (key, value["pc"], value["symbol"], value["cycle_id"], value["reference_at"], value["issued_at"], raw))
            db.execute("INSERT INTO intents VALUES(?,?,?,?)", (digest(intent_value), key, value["issued_at"], encoded(intent_value)))
            proof = self._published_forecast_header_proof(db, value)
        # Publish optional proof only after the scientific source commits.
        # Missing cache proof retains the complete original validation path.
        self._publish_forecast_header_proof(proof)
        return value

    @staticmethod
    def _intent(key, forecast, supplied):
        side = forecast["stock_action"]
        value = dict(supplied or {})
        metadata = recorded_direction(forecast)
        if any(field in value and value[field] != item for field, item in metadata.items()):
            raise ContractError("Intent stock direction metadata differs from forecast")
        if value.get("side", side) != side:
            raise ContractError("Intent direction disagrees with the forecast")
        desired, adjusted = value.get("desired_quantity"), value.get("adjusted_quantity")
        if desired is None:
            if adjusted is not None:
                raise ContractError("Adjusted quantity needs a desired quantity")
            disposition = "sizing_policy_unresolved"
        else:
            if type(desired) is not int or desired < 0 or type(adjusted) is not int or not 0 <= adjusted <= desired:
                raise ContractError("Invalid intent quantities")
            disposition = value.get("disposition")
            if not disposition:
                raise ContractError("Explicit execution disposition required")
        if side == "HOLD":
            if desired not in (None, 0):
                raise ContractError("Neutral forecast cannot create a trade intention")
            disposition = "probability_between_action_cutoffs"
        return {**value, **metadata, "forecast_id": key, "side": side, "desired_quantity": desired,
                "adjusted_quantity": adjusted, "disposition": disposition, "submission_status": "not_submitted_shadow",
                "submitted_quantity": 0, "filled_quantity": 0, "realized_pnl": None}

    def record_candle(self, candle):
        value = dict(candle)
        for name in ("opened_at", "available_at"):
            value[name] = instant(value[name]).isoformat()
        if instant(value["available_at"]) < instant(value["opened_at"]) + STEP:
            raise ContractError("Target candle is not complete")
        op, hi, lo, cl = [number(value[name], positive=True) for name in ("open", "high", "low", "close")]
        if not lo <= min(op, cl) <= max(op, cl) <= hi:
            raise ContractError("Invalid target OHLC")
        number(value["volume"])
        if value.get("synthetic") and not value.get("evidence_json"):
            raise ContractError("Synthetic constituents require confirmed no-trade provenance")
        if value.get("no_trade") and (not value.get("synthetic") or not (op == hi == lo == cl) or number(value["volume"]) != 0):
            raise ContractError("Whole no-trade candle must carry a flat prior close and zero volume")
        key = digest(value)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?)", (key, value["symbol"], value["opened_at"], value["available_at"], encoded(value)))
        return key

    def observed_candles(self, symbol=None):
        """Restore actual observations/revisions after restart, never infer receipt."""
        with self.connect() as db:
            rows=db.execute("SELECT id,payload FROM candles"+(" WHERE symbol=?" if symbol else "")+" ORDER BY available_at,rowid",
                            (symbol,) if symbol else ()).fetchall()
        result=[]
        for key,raw in rows:
            value=json.loads(raw)
            if digest(value)!=key:
                raise ContractError("Persisted candle evidence hash changed")
            if value.get("availability_kind")=="actual":
                result.append(value)
        return result

    def observe(self, forecast, *, as_of, candle=None, unavailable_reason=None, skip_newer=False):
        """Append corrected outcomes; repeated identical evidence is idempotent.

        A concurrent scorer may opt to retain an already validated newer
        observation. Ordinary callers keep the strict no-backdating contract.
        """
        now = instant(as_of)
        key = forecast["forecast_id"]
        with self.connect() as db:
            original = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE id=?", (key,)).fetchone()
        if original is None or encoded(self._forecast_value(original)) != encoded(forecast):
            raise ContractError("Scoring requires the original immutable forecast")
        status = "pending" if now < instant(forecast["target_close"]) else "missing"
        label, price, candle_id = None, None, None
        if unavailable_reason:
            status = "unscorable"
        if candle is not None:
            from .data import synthetic_continuity_verified
            if not synthetic_continuity_verified(candle):
                raise ContractError("Synthetic outcome lacks verified source absence and price continuity")
            from .native_interval import candle_matches_forecast
            if not candle_matches_forecast(candle, forecast):
                raise ContractError("Outcome observation contract differs from the immutable forecast")
            if (candle["symbol"] != forecast["symbol"] or instant(candle["opened_at"]) != instant(forecast["target_open"]) or instant(candle["available_at"]) > now):
                raise ContractError("Outcome must use the exact available target")
            if forecast.get("source_dataset") and candle.get("source_dataset")!=forecast["source_dataset"]:
                raise ContractError("Outcome source differs from the forecast's selected dataset")
            if forecast.get("source_id") and candle.get("source_id")!=forecast["source_id"]:
                raise ContractError("Outcome source provenance differs from the original input")
            if forecast["mode"] in {"shadow", "live"} and candle.get("availability_kind") != "actual":
                raise ContractError("Shadow outcome availability must be observed")
            candle_id = self.record_candle(candle)
            label = number(candle["close"], positive=True) >= number(forecast["reference_open"], positive=True)
            price = str(candle["close"])
            derived_reference = isinstance(forecast.get("reference_provenance", {}).get("forward_fill_contract"), str)
            status = "synthetic" if candle.get("synthetic") or derived_reference else "scored"
        market_return = None if price is None else str(number(price) / number(forecast["reference_open"]) - 1)
        evidence = {"forecast_id": key, "status": status, "target_not_down": label,
                    "reference_to_target_return": market_return,
                    "return_basis": "reference_open_to_exact_target_close_not_trade_pnl",
                    "target_close_price": price, "candle_id": candle_id, "unavailable_reason": unavailable_reason,
                    "availability_basis": candle.get("availability_kind") if candle else None}
        revision_id = digest(evidence)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE forecast_id=? ORDER BY rowid DESC LIMIT 1", (key,)).fetchone()
            previous = self._outcome_value(prior) if prior else None
            if previous and instant(previous["observed_at"])>now:
                if skip_newer:
                    return None
                raise ContractError("Outcome evidence cannot be backdated before an existing observation")
            if previous and candle is None and previous["status"] in {"scored", "synthetic", "unscorable"}:
                return previous
            old = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE id=?", (revision_id,)).fetchone()
            if old:
                return self._outcome_value(old)
            result = {**evidence, "observed_at": now.isoformat(), "revision_id": revision_id}
            db.execute("INSERT INTO outcomes VALUES(?,?,?,?)", (revision_id, key, now.isoformat(), encoded(result)))
        return result

    def invalidate_unverified_outcome(self, forecast_id, *, as_of):
        """Append a precise audit revision; never rewrite prices or forecasts.

        Legacy synthetic outcomes could be stored with only a generic omission
        statement. They cannot become observed targets under a newer contract.
        Retain the original outcome and candle, marking the latest assessment
        unscorable only when their saved provenance demonstrably fails.
        """
        from .data import synthetic_continuity_verified
        now = instant(as_of)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes "
                "WHERE forecast_id=? ORDER BY rowid DESC LIMIT 1", (forecast_id,)).fetchone()
            if row is None:
                return None
            previous = self._outcome_value(row)
            if (previous["status"] not in {"scored", "synthetic"}
                    or instant(previous["observed_at"]) > now or not previous.get("candle_id")):
                return None
            saved = db.execute("SELECT payload FROM candles WHERE id=?", (previous["candle_id"],)).fetchone()
            if saved is None:
                raise ContractError("Saved outcome target candle is missing")
            candle = json.loads(saved[0])
            if digest(candle) != previous["candle_id"]:
                raise ContractError("Saved outcome candle evidence hash changed")
            if synthetic_continuity_verified(candle):
                return None
            evidence = {"forecast_id": forecast_id, "status": "unscorable", "target_not_down": None,
                "reference_to_target_return": None,
                "return_basis": "reference_open_to_exact_target_close_not_trade_pnl",
                "target_close_price": None, "candle_id": None,
                "unavailable_reason": "legacy_synthetic_target_without_verified_source_no_trade_proof",
                "availability_basis": None, "invalidates_revision_id": previous["revision_id"],
                "rejected_candle_id": previous["candle_id"],
                "audit_contract": "verified_source_no_trade_outcome_audit_v1"}
            revision_id = digest(evidence)
            result = {**evidence, "observed_at": now.isoformat(), "revision_id": revision_id}
            db.execute("INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?)",
                (revision_id, forecast_id, now.isoformat(), encoded(result)))
            return result

    def revisions(self, forecast_id):
        with self.connect() as db:
            return [self._outcome_value(row) for row in db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE forecast_id=? ORDER BY rowid", (forecast_id,))]


class ScoringForecastReader:
    """Keep bounded, verified routing projections for one scoring worker.

    Forecast model evidence can be megabytes per row. Polls reread the small
    indexed identities in original row order, not every model payload. New or
    changed identities are fully validated before projection. The cache retains
    immutable serialized projections only; originals are reloaded and validated
    again immediately before a scoring mutation. This is not an all-history
    payload-corruption audit on every poll.
    """

    MAX_ENTRIES = 4096
    MAX_BYTES = 16 * 1024 * 1024
    _FIELDS = ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at",
               "mode", "target_open", "target_close", "reference_open", "source_dataset",
               "source_id", "feature_version", "native_interval_contract")

    def __init__(self, store, *, pc, session_id=None):
        if pc not in {"Atlas", "Scout"}:
            raise ContractError("Explicit scoring participant required")
        self.store, self.pc = store, pc
        self.session_id = session_id
        self._cache = {}
        self._cache_bytes = 0

    @classmethod
    def _projection(cls, forecast):
        value = {key: forecast[key] for key in cls._FIELDS if key in forecast}
        # These are the only nested fields read by interval matching and outcome
        # classification. Never retain the model's full dataset manifest.
        for name, field in (("dataset_manifest", "native_interval_contract"),
                            ("reference_provenance", "forward_fill_contract")):
            if name in forecast:
                nested = forecast[name]
                value[name] = ({field: nested[field]} if field in nested else {}) if isinstance(nested, dict) else nested
        return encoded(value)

    def _discard(self, identity):
        old = self._cache.pop(identity, None)
        if old is not None:
            self._cache_bytes -= old[2]

    def __iter__(self):
        with self.store.connect() as db:
            if self.session_id is None:
                rows = db.execute("SELECT rowid,id,pc,symbol,cycle_id,reference_at,issued_at "
                                  "FROM forecasts WHERE pc=? ORDER BY rowid", (self.pc,)).fetchall()
            else:
                rows = db.execute("SELECT f.rowid,f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at "
                    "FROM forecasts f JOIN cycles c ON c.id=f.cycle_id WHERE f.pc=? AND c.pc=? "
                    "AND json_extract(c.payload,'$.experiment')=? ORDER BY f.rowid",
                    (self.pc, self.pc, self.session_id)).fetchall()
        present = {row[1] for row in rows}
        for identity in self._cache.keys() - present:
            self._discard(identity)
        for row in rows:
            identity = row[1]
            cached = self._cache.get(identity)
            if cached is None or cached[0] != row:
                self._discard(identity)
                original = self.store.forecast_by_id(identity, pc=self.pc)
                if original is None or any(original.get(field) != indexed for field, indexed in
                        zip(("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at"), row[1:])):
                    raise ContractError("Scoring forecast identity changed during selection")
                projection = self._projection(original)
                del original
                size = len(projection.encode("utf-8"))
                if len(self._cache) < self.MAX_ENTRIES and self._cache_bytes + size <= self.MAX_BYTES:
                    self._cache[identity] = (row, projection, size)
                    self._cache_bytes += size
            else:
                projection = cached[1]
            # A consumer cannot mutate the cached projection or retain a large
            # original through a projection's nested dictionaries.
            yield json.loads(projection)

    def original(self, projection):
        """Validate the complete original and its routing before every write."""
        original = self.store.forecast_by_id(projection["forecast_id"], pc=self.pc)
        if original is None or self._projection(original) != encoded(projection):
            raise ContractError("Scoring forecast changed before outcome mutation")
        return original
