"""Host-local shadow evidence. Forecasts, inputs and outcome revisions are immutable.

This module deliberately has no broker, account, or execution dependency. Operational
cycle summaries may advance; every transition also has an append-only event.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock

from ml.stock_direction_policy import (
    BEARISH_PROBABILITY, BULLISH_PROBABILITY, STOCK_DIRECTION_POLICY_VERSION,
    stock_direction, stock_direction_for_policy,
)

from .prediction import ContractError, STEP, TARGET_VERSION, digest, encoded, instant, number


_DIRECTION_POLICY_FIELDS = (
    "direction", "direction_policy_version", "direction_up_threshold", "direction_down_threshold",
)


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

    @staticmethod
    def _execution_forecast_schema(db):
        names = tuple(_EXECUTION_FORECAST_SCHEMA)
        rows = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name IN (" +
                               ",".join("?" for _ in names) + ")", names))
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

    @contextmanager
    def connect(self):
        """Commit/rollback deterministically, then close even on interruption."""
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _snapshot_value(key, raw):
        value = json.loads(raw)
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
        key = digest(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT snapshot_id FROM symbol_inputs WHERE cycle_id=? AND symbol=?", (cycle_id, symbol)).fetchone()
            if old:
                row = db.execute("SELECT payload FROM snapshots WHERE id=?", old).fetchone()
                if row is None:
                    raise ContractError("Persisted snapshot evidence is missing")
                return old[0], self._snapshot_value(old[0], row[0])
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?)", (key, encoded(payload)))
            self._snapshot_value(key, db.execute("SELECT payload FROM snapshots WHERE id=?", (key,)).fetchone()[0])
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

    def _forecast_projection(self, row):
        """Cache only scalar headers of exact semantically validated evidence."""
        fields = ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at",
                  "mode", "ensemble_status", "target_open", "target_close",
                  "population_contract", "source_dataset", "source_id")
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
        if signature is not None and all(item is None or isinstance(item, str) for item in projection.values()):
            cache[row[0]] = (signature, tuple(projection.items()))
            if len(cache) > 4096:
                cache.popitem(last=False)
        return dict(projection), value

    def execution_forecast_headers(self, *, pc, symbol, forecast_ids=None):
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
        headers = []
        # Serialize this instance's cache publication so concurrent snapshots
        # cannot make a valid older reader look like a generation rollback.
        with self._execution_forecast_lock, self.connect() as db:
            db.execute("BEGIN")
            generation, epoch = self._execution_forecast_generation(db)
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino, db.execute("PRAGMA schema_version").fetchone()[0], epoch)
            if identity != self._execution_forecast_identity:
                self._execution_forecast_cache.clear()
                self._execution_forecast_highwater = 0
                self._execution_forecast_identity = identity
            if generation < self._execution_forecast_highwater:
                raise ContractError("Execution forecast generation regressed")
            rows = db.execute("SELECT f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,r.generation "
                "FROM forecasts f LEFT JOIN execution_forecast_revisions r ON r.forecast_id=f.id WHERE " +
                " AND ".join(clauses) + " ORDER BY f.rowid", parameters).fetchall()
            for row in rows:
                revision = row[6]
                if type(revision) is not int or not 0 <= revision <= generation:
                    raise ContractError("Execution forecast row revision is invalid")
                signature = (*row[:6], revision)
                previous = self._execution_forecast_cache.get(row[0])
                if previous is not None and previous[0] == signature:
                    projection = dict(previous[1])
                else:
                    original = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                                          "FROM forecasts WHERE id=?", (row[0],)).fetchone()
                    projection, _ = self._forecast_projection(original)
                    if all(item is None or isinstance(item, str) for item in projection.values()):
                        self._execution_forecast_cache[row[0]] = (signature, tuple(projection.items()))
                        if len(self._execution_forecast_cache) > 4096:
                            self._execution_forecast_cache.popitem(last=False)
                headers.append(projection)
            self._execution_forecast_highwater = generation
        return headers

    def scoring_forecasts(self, *, pc, observed_candidates, as_of, stopped=lambda: False):
        """Validate immutable bytes, reusing unchanged history after first read.

        First reads and changed rows take the full semantic validator. Repeated
        scans still read/hash all bytes but decode model trees only for selected
        work; small validated headers retain the original selection behavior.
        """
        now = instant(as_of)
        selected = []
        with self.connect() as db:
            rows = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                "FROM forecasts WHERE pc=? ORDER BY rowid", (pc,))
            for row in rows:
                if stopped():
                    return []
                key, _, symbol = row[:3]
                projection, full = self._forecast_projection(row)
                if projection["mode"] != "live" or instant(projection["target_close"]) > now:
                    continue
                # Retain validation of every prior revision, including a corrupt
                # older outcome hidden behind a valid latest revision.
                revisions = self.revisions(key)
                latest = revisions[-1] if revisions else None
                candidates = observed_candidates.get((symbol, instant(projection["target_open"])), [])
                observed = next((value for value in reversed(candidates)
                    if value.get("population_contract") == projection.get("population_contract")
                    and instant(value["available_at"]) <= now
                    and all(not projection.get(field) or value.get(field) == projection[field]
                            for field in ("source_dataset", "source_id"))), None)
                if observed is not None and instant(observed["available_at"]) <= now:
                    if (latest is not None and latest["status"] in {"scored", "synthetic"}
                            and latest.get("candle_id") == digest(observed)
                            and instant(latest["observed_at"]) <= now):
                        continue
                elif latest is not None and latest["status"] in {"scored", "synthetic", "unscorable"}:
                    continue
                if full is None:
                    original = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                                          "FROM forecasts WHERE id=?", (key,)).fetchone()
                    full = self._forecast_value(original)
                selected.append(full)
        # Release the read transaction before the caller acquires provider data
        # or records an outcome on another connection.
        return selected

    def retained_session_scoring_targets(self, *, pc, session_id, as_of, stopped=lambda: False):
        """Validate ready targets for this manual session without model inventory reads.

        The ordinary full-history scorer retains older-session work and its
        corruption checks. This priority path reads lightweight own headers,
        then validates the exact selected original, all revisions and candle.
        Timestamp joins use actual instants rather than ISO text equality.
        """
        from .data import completed_15m_observation, synthetic_continuity_verified
        now = instant(as_of)
        if stopped():
            return []
        with self.connect() as db:
            headers = db.execute("SELECT f.id,f.symbol,f.reference_at FROM forecasts f "
                "JOIN cycles c ON c.id=f.cycle_id WHERE f.pc=? AND c.pc=? "
                "AND json_extract(c.payload,'$.experiment')=? ORDER BY f.rowid",
                (pc, pc, session_id)).fetchall()
            targets = {}
            for key, symbol, reference in headers:
                if stopped():
                    return []
                targets.setdefault((symbol, instant(reference) + 2 * STEP), []).append(key)
            if not targets:
                return []
            # Only scalar candle headers are scanned; unrelated model trees
            # and candle payloads never enter this ready-target lookup.
            candle_headers = db.execute("SELECT id,symbol,opened_at,available_at FROM candles "
                                        "ORDER BY available_at,rowid").fetchall()
            candidates = {}
            for key, symbol, opened, available in candle_headers:
                if stopped():
                    return []
                target = (symbol, instant(opened))
                if target not in targets or instant(available) > now:
                    continue
                raw = db.execute("SELECT payload FROM candles WHERE id=?", (key,)).fetchone()[0]
                value = json.loads(raw)
                if digest(value) != key:
                    raise ContractError("Persisted candle evidence hash changed")
                if (value.get("availability_kind") == "actual"
                        and completed_15m_observation(value) and synthetic_continuity_verified(value)
                        and instant(value["available_at"]) <= now):
                    candidates.setdefault((value["symbol"], instant(value["opened_at"])), []).append(value)
            result = []
            for target, keys in targets.items():
                if stopped():
                    return []
                values = candidates.get(target, [])
                if not values:
                    continue
                for key in keys:
                    if stopped():
                        return []
                    row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload "
                                     "FROM forecasts WHERE id=?", (key,)).fetchone()
                    projection, full = self._forecast_projection(row)
                    if projection["mode"] != "live" or instant(projection["target_close"]) > now:
                        continue
                    revisions = self.revisions(key)
                    observed = next((value for value in reversed(values)
                        if value.get("population_contract") == projection.get("population_contract")
                        and all(not projection.get(field) or value.get(field) == projection[field]
                                for field in ("source_dataset", "source_id"))), None)
                    if observed is None:
                        continue
                    latest = revisions[-1] if revisions else None
                    if (latest is not None and latest["status"] in {"scored", "synthetic"}
                            and latest.get("candle_id") == digest(observed)
                            and instant(latest["observed_at"]) <= now):
                        continue
                    if full is None:
                        full = self._forecast_value(row)
                    result.append((full, observed))
        return result

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
        value.pop("forecast_id", None)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE cycle_id=? AND symbol=?", (value["cycle_id"], value["symbol"])).fetchone()
            previous = self._forecast_value(old) if old else None
            if previous is not None:
                # A raw producer retry has no policy fields. Retain the first
                # issue's recorded policy, including its historical absence;
                # supplied changes still participate in the exact comparison.
                for field in _DIRECTION_POLICY_FIELDS:
                    if field not in value and field in previous:
                        value[field] = previous[field]
                value["forecast_id"] = digest(value)
                if previous != value:
                    raise ContractError("Issued forecast is immutable")
                return previous
            policy = dict(direction=stock_direction_for_policy(value["probability_not_down"], STOCK_DIRECTION_POLICY_VERSION),
                          direction_policy_version=STOCK_DIRECTION_POLICY_VERSION,
                          direction_up_threshold=BULLISH_PROBABILITY,
                          direction_down_threshold=BEARISH_PROBABILITY)
            if any(field in value and value[field] != expected for field, expected in policy.items()):
                raise ContractError("New forecast direction policy differs from the current policy")
            value.update(policy)
            key = digest(value)
            value["forecast_id"] = key
            intent_value = self._intent(key, value, intent)
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (key, value["pc"], value["symbol"], value["cycle_id"], value["reference_at"], value["issued_at"], encoded(value)))
            db.execute("INSERT INTO intents VALUES(?,?,?,?)", (digest(intent_value), key, value["issued_at"], encoded(intent_value)))
        return value

    @staticmethod
    def _intent(key, forecast, supplied):
        side = {"BULLISH": "BUY", "BEARISH": "SELL"}.get(stock_direction(forecast["probability_not_down"]), "HOLD")
        value = dict(supplied or {})
        if value.get("side", side) != side:
            raise ContractError("Intent direction disagrees with the forecast")
        desired, adjusted = value.get("desired_quantity"), value.get("adjusted_quantity")
        if side == "HOLD" and (desired not in (None, 0) or adjusted not in (None, 0)):
            raise ContractError("A neutral forecast cannot request a trade")
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
        return {**value, "forecast_id": key, "side": side, "desired_quantity": desired,
                "adjusted_quantity": adjusted, "disposition": disposition, "submission_status": "not_submitted_shadow",
                "submitted_quantity": 0, "filled_quantity": 0, "realized_pnl": None}

    def record_candle(self, candle):
        value = dict(candle)
        if value.get("population_contract"):
            from .coverage import verified_source_observation, RECOVERY_POPULATION_CONTRACT
            validator = verified_source_observation
            if value["population_contract"] == RECOVERY_POPULATION_CONTRACT:
                from .recovered_population import verified_recovery_candle
                validator = verified_recovery_candle
            if not validator(value):
                raise ContractError("Population candle requires its frozen source/recovery evidence")
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

    def observe(self, forecast, *, as_of, candle=None, unavailable_reason=None):
        """Append corrected outcomes; repeated identical evidence is idempotent."""
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
            if (candle["symbol"] != forecast["symbol"] or instant(candle["opened_at"]) != instant(forecast["target_open"]) or instant(candle["available_at"]) > now):
                raise ContractError("Outcome must use the exact available target")
            if forecast.get("source_dataset") and candle.get("source_dataset")!=forecast["source_dataset"]:
                raise ContractError("Outcome source differs from the forecast's selected dataset")
            if forecast.get("source_id") and candle.get("source_id")!=forecast["source_id"]:
                raise ContractError("Outcome source provenance differs from the original input")
            if forecast.get("population_contract") != candle.get("population_contract"):
                raise ContractError("Outcome population contract differs from the frozen forecast")
            if forecast["mode"] in {"shadow", "live"} and candle.get("availability_kind") != "actual":
                raise ContractError("Shadow outcome availability must be observed")
            candle_id = self.record_candle(candle)
            label = number(candle["close"], positive=True) >= number(forecast["reference_open"], positive=True)
            price = str(candle["close"])
            status = "synthetic" if candle.get("synthetic") else "scored"
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
                raise ContractError("Outcome evidence cannot be backdated before an existing observation")
            if previous and candle is None and previous["status"] in {"scored", "synthetic"}:
                return previous
            old = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE id=?", (revision_id,)).fetchone()
            if old:
                return self._outcome_value(old)
            result = {**evidence, "observed_at": now.isoformat(), "revision_id": revision_id}
            db.execute("INSERT INTO outcomes VALUES(?,?,?,?)", (revision_id, key, now.isoformat(), encoded(result)))
        return result

    def revisions(self, forecast_id):
        with self.connect() as db:
            return [self._outcome_value(row) for row in db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE forecast_id=? ORDER BY rowid", (forecast_id,))]
