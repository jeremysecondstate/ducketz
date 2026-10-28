"""Durable Scout data/model boundary. No provider, broker or activation code.

Large immutable objects are fsynced and renamed before a small SQLite ready
record can reference them. Queue retirement never removes scientific evidence.
Neither publication nor retirement needs a consumer acknowledgement.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import time
import uuid

from .prediction import ContractError, STEP, TARGET_VERSION, digest, encoded, instant
from .store import ShadowStore, utcnow
from .immutable_file_witness import file_integrity_witness, stat_witness

SCHEMA = "scout-service-ready-v1"
EXTERNAL = "scout-external-input-v1"


def file_witness(path):
    # Preserve the original envelope and pinned older readers. Strong change
    # proofs are additive forecast fields, captured after full verification.
    return stat_witness(Path(path).stat())


def _integrity_witness(path):
    try:
        return file_integrity_witness(path)
    except ValueError as error:
        raise ContractError(str(error)) from error


def _verified_file_witness(path, expected_hash):
    """Model/publication path only: hash bytes without rebuilding a matrix."""
    before = _integrity_witness(path)
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    after = _integrity_witness(path)
    if before != after or hasher.hexdigest() != expected_hash:
        raise ContractError("Retained immutable execution evidence changed")
    return after


def _verify_file_witness(path, witness, expected_hash, integrity=None):
    current = _integrity_witness(path)
    if integrity is not None:
        valid = current == integrity and current["metadata"] == witness
    else:
        # Preserve old frozen forecasts without treating their weak Windows
        # metadata as a content proof. They retain full hash verification; new
        # records use the strong witness and do not reread a training matrix.
        valid = (isinstance(witness, list) and current["metadata"] == witness
                 and _verified_file_witness(path, expected_hash) == current)
    if not valid:
        raise ContractError("Retained immutable execution evidence changed")


@dataclass(frozen=True)
class ExecutionIndicators:
    """Verified projection retaining the original full indicator evidence ID.

    Constructed only after the immutable forecast binds the entire projection.
    Complete candle evidence stays in its separately hashed immutable object.
    """
    snapshot_id: str
    projection_hash: str
    payload_json: str

    def payload(self):
        value = json.loads(self.payload_json)
        if digest(value) != self.projection_hash:
            raise ContractError("Verified indicator projection changed")
        return value


def atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            if path.read_bytes() != raw:
                raise ContractError("Immutable service object changed")
            return
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _object_path(value):
    path = Path(value["object_path"])
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//"))
            or path.name != value["snapshot_hash"] + ".json" or path.parent.name != "objects"
            or any(p.is_symlink() or (getattr(p.lstat(), "st_file_attributes", 0) & 0x400)
                   for p in (path, *path.parents) if p.exists())):
        raise ContractError("Snapshot object is not a standalone local immutable object")
    return path


def full_snapshot(key, envelope):
    if envelope.get("schema") != EXTERNAL or envelope.get("snapshot_hash") != key:
        raise ContractError("External snapshot identity changed")
    raw = _object_path(envelope).read_bytes()
    if hashlib.sha256(raw).hexdigest() != key:
        raise ContractError("External snapshot bytes changed")
    value = json.loads(raw)
    if not isinstance(value, dict) or digest(value) != key:
        raise ContractError("External snapshot canonical evidence changed")
    return value


def execution_envelope(key, raw, forecast):
    value = json.loads(raw) if isinstance(raw, str) else raw
    if (value.get("schema") != EXTERNAL or value.get("snapshot_hash") != key
            or digest(value) != forecast.get("execution_input_hash")
            or value.get("symbol") != forecast.get("symbol")
            or instant(value["reference_at"]) != instant(forecast["reference_at"])
            or instant(value["cutoff"]) != instant(forecast["training_cutoff"])):
        raise ContractError("Compact execution evidence changed")
    indicators = value["indicators"]
    if (indicators.get("forecast_snapshot_hash") != key
            or indicators.get("symbol") != forecast["symbol"]):
        raise ContractError("Compact indicator source changed")
    proofs = forecast.get("execution_integrity_witnesses", {})
    for path_key, witness_key, hash_key in (("object_path", "object_witness", "snapshot_hash"),
            ("indicator_object_path", "indicator_object_witness", "indicator_snapshot_hash")):
        _verify_file_witness(value[path_key], value[witness_key], value[hash_key], proofs.get(path_key))
    if forecast.get("scientific_forecast_hash"):
        _verify_file_witness(forecast["scientific_forecast_path"], forecast["scientific_forecast_witness"],
                             forecast["scientific_forecast_hash"], forecast.get("scientific_forecast_integrity"))
    return value


class ReadySpool:
    def __init__(self, root, *, pc, symbols, recipe_hash, recipe_identity=None, clock=utcnow, per_symbol=2):
        if pc != "Scout" or not symbols or len(set(symbols)) != len(symbols):
            raise ContractError("Explicit Scout symbol ownership required")
        if type(per_symbol) is not int or not 1 <= per_symbol <= 4:
            raise ContractError("Bounded ready queue required")
        self.root, self.pc, self.symbols = Path(root), pc, tuple(symbols)
        self.recipe_hash, self.clock, self.per_symbol = recipe_hash, clock, per_symbol
        self.recipe_identity = recipe_identity
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "handoff.sqlite3"
        with self.connect() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS ready(
                id TEXT PRIMARY KEY,symbol TEXT NOT NULL,reference_at TEXT NOT NULL,
                recipe TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ready_scope ON ready(symbol,recipe,reference_at,status);
              CREATE TABLE IF NOT EXISTS recovery(
                id INTEGER PRIMARY KEY,recorded_at TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
            """)

    def connect(self):
        # Every transaction is small. A busy handoff is a retry, never a 30s
        # wait holding a model or broker transaction open.
        return closing_connection(self.path)

    def record(self, kind, payload):
        with self.connect() as db:
            db.execute("INSERT INTO recovery(recorded_at,kind,payload) VALUES(?,?,?)",
                       (instant(self.clock()).isoformat(), kind, encoded(payload)))

    def publish(self, *, symbol, reference_at, envelope, source_session):
        reference = instant(reference_at)
        if symbol not in self.symbols or envelope.get("symbol") != symbol:
            raise ContractError("Snapshot belongs to another symbol")
        with self.connect() as db:
            previous = db.execute("SELECT id,payload FROM ready WHERE symbol=? AND reference_at=? AND recipe=?",
                                  (symbol, reference.isoformat(), self.recipe_hash)).fetchall()
        for previous_key, previous_raw in previous:
            previous_value = json.loads(previous_raw)
            if previous_value.get("source_session") == source_session and previous_value.get("envelope") == envelope:
                if (digest(previous_value) != previous_key or
                        (self.root / "manifests" / (previous_key + ".json")).read_text() != previous_raw):
                    raise ContractError("Existing ready publication changed")
                full_snapshot(envelope["snapshot_hash"], envelope)
                return {"ready_id": previous_key, **previous_value}
        snapshot = full_snapshot(envelope["snapshot_hash"], envelope)
        if self.recipe_identity is not None and digest(snapshot.get("manifest", {}).get("manual_model_recipe")) != self.recipe_identity:
            raise ContractError("Prepared snapshot changed the exact local model recipe")
        inference = snapshot["inference"]
        cutoff = instant(snapshot["cutoff"])
        if (instant(inference["reference_at"]) != reference
                or inference.get("availability_kind") != "actual"
                or cutoff > instant(self.clock())
                or instant(inference["information_available_at"]) > cutoff
                or any(instant(t) > cutoff for t in snapshot["label_available_at"])):
            raise ContractError("Ready snapshot violates actual causal cutoff")
        value = {"schema": SCHEMA, "pc": self.pc, "symbol": symbol,
                 "symbols": list(self.symbols), "reference_at": reference.isoformat(),
                 "target_close": (reference + 3 * STEP).isoformat(),
                 "training_cutoff": snapshot["cutoff"], "target_version": TARGET_VERSION,
                 "available_at": inference["information_available_at"],
                 "published_at": instant(self.clock()).isoformat(), "recipe_hash": self.recipe_hash,
                 "recipe_identity": self.recipe_identity,
                 "feature_order_hash": digest(snapshot["feature_names"]),
                 "provenance_hash": digest(snapshot.get("source_manifest", {})),
                 "source_session": source_session, "snapshot_hash": envelope["snapshot_hash"],
                 "execution_input_hash": digest(envelope), "envelope": envelope}
        key, raw = digest(value), encoded(value)
        # Ready manifest itself remains durable after queue supersession.
        atomic_bytes(self.root / "manifests" / (key + ".json"), raw.encode())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO ready VALUES(?,?,?,?,?,'ready')",
                       (key, symbol, reference.isoformat(), self.recipe_hash, raw))
            self._retire(db, instant(self.clock()))
        return {"ready_id": key, **value}

    def _retire(self, db, now):
        for symbol in self.symbols:
            rows = db.execute("SELECT id,payload FROM ready WHERE symbol=? AND recipe=? AND status='ready' "
                              "ORDER BY reference_at DESC,rowid DESC", (symbol, self.recipe_hash)).fetchall()
            retained = 0
            seen = set()
            for key, raw in rows:
                value = json.loads(raw)
                expired = instant(value["target_close"]) <= now
                reference = value["reference_at"]
                superseded = reference in seen or retained >= self.per_symbol
                state = "expired" if expired else "superseded" if superseded else None
                if state:
                    db.execute("UPDATE ready SET status=? WHERE id=?", (state, key))
                else:
                    retained += 1
                    seen.add(reference)

    def ready(self, symbol, reference_at, *, now=None):
        if symbol not in self.symbols:
            raise ContractError("Ready request exceeds local ownership")
        now, reference = instant(now or self.clock()), instant(reference_at)
        with self.connect() as db:
            self._retire(db, now)
            row = db.execute("SELECT id,payload FROM ready WHERE symbol=? AND recipe=? AND reference_at=? "
                             "AND status='ready' ORDER BY rowid DESC LIMIT 1",
                             (symbol, self.recipe_hash, reference.isoformat())).fetchone()
        if row is None:
            return None
        key, raw = row
        value = json.loads(raw)
        if (digest(value) != key or value.get("schema") != SCHEMA or value.get("pc") != self.pc
                or value.get("symbols") != list(self.symbols) or value.get("recipe_hash") != self.recipe_hash
                or value.get("recipe_identity") != self.recipe_identity
                or value.get("target_version") != TARGET_VERSION
                or value.get("symbol") != symbol or instant(value["reference_at"]) != reference
                or instant(value["target_close"]) != reference + 3 * STEP
                or instant(value["target_close"]) <= now
                or instant(value["published_at"]) > now
                or (self.root / "manifests" / (key + ".json")).read_text() != raw):
            raise ContractError("Ready manifest identity or freshness changed")
        return {"ready_id": key, **value}


class closing_connection:
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        self.db = sqlite3.connect(self.path, timeout=.1)
        return self.db
    def __exit__(self, kind, error, trace):
        try:
            self.db.rollback() if error else self.db.commit()
        finally:
            self.db.close()


class ServiceStore(ShadowStore):
    """Original journal/forecast API, with matrices outside writer transactions."""
    def __init__(self, path, *, object_root):
        self.object_root = Path(object_root)
        self.ready_loader = None
        self.run_binding = None
        self.connection_timeout = .25
        metric_root = self.object_root
        class MeasuredConnection(sqlite3.Connection):
            def __init__(connection, *args, **kwargs):
                super().__init__(*args, **kwargs)
                connection._database_metrics = []
                connection._database_metrics_dropped = 0

            def execute(connection, sql, *args, **kwargs):
                statement = sql.strip().upper()
                admission = statement.startswith("BEGIN IMMEDIATE")
                measured = admission or statement.startswith(("INSERT", "UPDATE", "DELETE"))
                begun = time.perf_counter()
                busy = False
                try:
                    return super().execute(sql, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    busy = "locked" in str(exc).lower() or "busy" in str(exc).lower()
                    raise
                finally:
                    if measured:
                        # Keep only bounded scalars while SQLite may own the
                        # writer. Telemetry locks and file I/O happen after close.
                        try:
                            sample = ("writer_admission" if admission else "write_statement",
                                      time.perf_counter()-begun, busy)
                            if len(connection._database_metrics) < 128:
                                connection._database_metrics.append(sample)
                            else:
                                connection._database_metrics_dropped += 1
                        except Exception:
                            pass  # Diagnostics never replace a durable effect.

            def close(connection):
                result = super().close()  # A failed close cannot prove lock release.
                pending = connection._database_metrics
                dropped = connection._database_metrics_dropped
                connection._database_metrics = []
                connection._database_metrics_dropped = 0
                try:
                    from .scout_services import metric
                except Exception:
                    return result
                for phase, seconds, busy in pending:
                    try:
                        metric(metric_root, "database", phase, seconds=seconds, busy=busy)
                    except Exception:
                        pass  # Diagnostic I/O cannot mask the committed/rolled-back effect.
                if dropped:
                    try:
                        metric(metric_root, "database", "measurement_overflow", dropped_count=dropped)
                    except Exception:
                        pass
                return result
        self.connection_factory = MeasuredConnection
        super().__init__(path)

    def inputs(self, cycle_id, symbol):
        frozen = super().inputs(cycle_id, symbol)
        if frozen is None and self.ready_loader is not None:
            return self.ready_loader(cycle_id, symbol)
        return frozen

    def freeze_inputs(self, cycle_id, symbol, payload):
        old = self.inputs(cycle_id, symbol)
        if old:
            return old
        raw = encoded(payload).encode()
        key = hashlib.sha256(raw).hexdigest()
        path = (self.object_root / "objects" / (key + ".json")).resolve()
        atomic_bytes(path, raw)
        from .starter_sizing import starter_policy
        from .technical_sizing import indicator_snapshot
        policy = starter_policy(symbol)
        indicators = indicator_snapshot(payload["candles"], symbol=symbol,
            reference_at=payload["inference"]["reference_at"], data_cutoff=payload["cutoff"],
            indicator_specs=policy.indicators, window_policy=policy.window,
            forecast_snapshot=payload, forecast_snapshot_hash=key).payload()
        indicator_hash = digest(indicators)
        indicator_path = (self.object_root / "objects" / (indicator_hash + ".json")).resolve()
        atomic_bytes(indicator_path, encoded(indicators).encode())
        projection = {name: value for name, value in indicators.items() if name != "candles"}
        if "provenance" in projection:
            projection["provenance"] = {name: value for name, value in projection["provenance"].items()
                                        if name != "session_ids"}
        if len(encoded(projection)) > 65536:
            raise ContractError("Execution indicator projection exceeds its bounded contract")
        envelope = {"schema": EXTERNAL, "snapshot_hash": key, "object_path": str(path),
                    "object_witness": file_witness(path),
                    "symbol": symbol, "reference_at": payload["inference"]["reference_at"],
                    "cutoff": payload["cutoff"], "indicators": projection,
                    "indicator_snapshot_hash": indicator_hash, "indicator_object_path": str(indicator_path),
                    "indicator_object_witness": file_witness(indicator_path)}
        self.attach(cycle_id, symbol, envelope)
        return key, json.loads(raw)

    def attach(self, cycle_id, symbol, envelope):
        key = envelope["snapshot_hash"]
        raw = encoded(envelope)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT snapshot_id FROM symbol_inputs WHERE cycle_id=? AND symbol=?",
                             (cycle_id, symbol)).fetchone()
            if old:
                if old[0] != key:
                    raise ContractError("Frozen cycle inputs cannot be replaced")
                return
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?)", (key, raw))
            saved = db.execute("SELECT payload FROM snapshots WHERE id=?", (key,)).fetchone()
            if saved[0] != raw:
                raise ContractError("Snapshot pointer changed")
            db.execute("INSERT INTO symbol_inputs VALUES(?,?,?)", (cycle_id, symbol, key))

    def envelope(self, cycle_id, symbol):
        with self.connect() as db:
            row = db.execute("SELECT s.id,s.payload FROM snapshots s JOIN symbol_inputs i ON i.snapshot_id=s.id "
                             "WHERE i.cycle_id=? AND i.symbol=?", (cycle_id, symbol)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def execution_inputs(self, cycle_id, symbol, *, forecast):
        row = self.envelope(cycle_id, symbol)
        if row is None:
            return None
        if row[1].get("schema") != EXTERNAL:
            return self.inputs(cycle_id, symbol)
        return row[0], execution_envelope(row[0], row[1], forecast)

    def save_forecast(self, value, *, intent=None):
        row = self.envelope(value["cycle_id"], value["symbol"])
        if row and row[1].get("schema") == EXTERNAL:
            value = {**value, "execution_input_hash": digest(row[1])}
            if self.run_binding is not None:
                value.update(self.run_binding)
            # This model/publication process rechecks exact retained bytes after
            # fitting, then binds native change proofs into the forecast. Older
            # trading processes ignore these additive fields; their original
            # five-field witnesses and snapshot envelope stay byte compatible.
            value["execution_integrity_witnesses"] = {
                path_key: _verified_file_witness(row[1][path_key], row[1][hash_key])
                for path_key, hash_key in (("object_path", "snapshot_hash"),
                                           ("indicator_object_path", "indicator_snapshot_hash"))}
            # Keep the complete scientific output, including every feature,
            # recipe and individual fit diagnostic. The trading queue receives
            # a separate small immutable forecast with the same probabilities,
            # target, cutoff and execution facts, bound to that original object.
            scientific_hash = digest(value)
            path = (self.object_root / "objects" / (scientific_hash + ".json")).resolve()
            atomic_bytes(path, encoded(value).encode())
            manifest = value.get("dataset_manifest", {})
            summary_fields = ("training_rows", "input_training_rows", "feature_version", "source_dataset",
                "manual_recipe_symbol", "first_training_reference", "last_training_reference",
                "synthetic_policy", "training_timing_contract", "label_provenance", "training_window")
            model_fields = ("name", "status", "probability_not_down", "version", "training_seconds", "inference_seconds")
            value = {**value, "scientific_forecast_hash": scientific_hash, "scientific_forecast_path": str(path),
                     "scientific_forecast_witness": file_witness(path),
                     "scientific_forecast_integrity": _verified_file_witness(path, scientific_hash),
                     "features_hash": digest(value.get("features", {})),
                     "dataset_manifest": {name:manifest[name] for name in summary_fields if name in manifest},
                     "models": [{name:model[name] for name in model_fields if name in model}
                                for model in value.get("models", [])]}
            value.pop("features", None)
        return super().save_forecast(value, intent=intent)

    def _validate_forecast_record(self, value, raw):
        # ShadowStore has now added direction, classification and forecast ID.
        # Bound the exact serialization supplied to INSERT, not an allowance
        # for fields that another layer is about to append.
        if value.get("execution_input_hash") and len(raw.encode("utf-8")) > 65536:
            raise ContractError("Execution forecast exceeds the bounded 64KiB record contract; full output retained")

    @staticmethod
    def scientific_forecast(forecast):
        """Explicit offline/display access, never part of the final send path."""
        key = forecast["scientific_forecast_hash"]
        envelope = {"schema": EXTERNAL, "snapshot_hash": key,
                    "object_path": forecast["scientific_forecast_path"]}
        return full_snapshot(key, envelope)
