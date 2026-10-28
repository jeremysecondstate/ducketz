"""Independent data-service handoff: immutable bytes, bounded live pointers.

Objects and first-publication claims are retained as scientific evidence. Only
the small SQLite index is mutable; one pointer per owned symbol bounds live
work. No consumer acknowledgment, trading state or broker is involved.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import local
import time
import uuid

from .prediction import ContractError, STEP, TARGET_VERSION, digest, encoded, instant

VERSION = "independent-intraday-ready-snapshot-v1"
MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ContractError("Invalid immutable handoff identity")
    return value


def _now():
    return datetime.now(timezone.utc)


class ReadySnapshots:
    def __init__(self, directory, *, actor, symbols, clock=_now):
        self.root = Path(directory).resolve()
        self.actor, self.symbols, self.clock = actor, tuple(symbols), clock
        if (actor not in {"Atlas", "Scout"} or not self.symbols
                or len(set(self.symbols)) != len(self.symbols)
                or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", s)
                       for s in self.symbols)):
            raise ContractError("Exact local data-service ownership required")
        self.machine = {"Atlas": "pc-original", "Scout": "pc-new"}[actor]
        self.path = self.root / "ready.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        self.rejected = []
        self._timing = local()
        self.measurements = {}
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS ready(
                actor TEXT NOT NULL,symbol TEXT NOT NULL,reference_at TEXT NOT NULL,
                configuration_hash TEXT NOT NULL,record_sha256 TEXT NOT NULL,
                PRIMARY KEY(actor,symbol))""")

    @property
    def measurements(self):
        return dict(getattr(self._timing, "value", {}))

    @measurements.setter
    def measurements(self, value):
        self._timing.value = dict(value)

    @contextmanager
    def connect(self):
        # Short index operations cannot wait behind a training/preparation DB.
        db = sqlite3.connect(self.path, timeout=.25)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _path(self, kind, identity):
        return self.root / kind / _hash(identity)[:2] / (identity + ".json")

    def _read(self, kind, identity, bound):
        path = self._path(kind, identity)
        with path.open("rb") as stream:
            raw = stream.read(bound + 1)
        if len(raw) > bound or _sha(raw) != identity:
            raise ContractError("Incomplete or changed immutable handoff object")
        return raw

    def _write(self, kind, raw, bound):
        if len(raw) > bound:
            raise ContractError("Immutable handoff exceeds its resource bound")
        identity = _sha(raw)
        path = self._path(kind, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self._read(kind, identity, bound) != raw:
                raise ContractError("Immutable handoff collision")
            return identity
        temporary = path.with_name("." + uuid.uuid4().hex + ".partial")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic no-replace publication on NTFS and POSIX. A crash while
            # writing the temporary file never makes a ready object visible.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self._read(kind, identity, bound) != raw:
                    raise ContractError("Immutable handoff collision")
        finally:
            temporary.unlink(missing_ok=True)
        return identity

    def _validate_snapshot(self, cycle_id, symbol, cycle, snapshot):
        _hash(cycle_id)
        if (symbol not in self.symbols or cycle.get("pc") != self.actor
                or cycle.get("symbols") != list(self.symbols)
                or cycle.get("mode") != "live"):
            raise ContractError("Snapshot ownership or mode differs from local service")
        configuration = dict(cycle["configuration"])
        # Selected policy overlay is deliberately outside original cycle identity.
        configuration.pop("selected_models_by_symbol", None)
        if digest(configuration) != cycle.get("configuration_hash"):
            raise ContractError("Snapshot configuration identity mismatch")
        identity = digest(dict(pc=self.actor, reference_at=cycle["reference_at"],
            configuration_hash=cycle["configuration_hash"], mode=cycle["mode"], experiment=cycle["experiment"]))
        if identity != cycle_id:
            raise ContractError("Snapshot cycle identity mismatch")
        reference = instant(cycle["reference_at"])
        cutoff = instant(snapshot["cutoff"])
        inference = snapshot["inference"]
        if (reference.minute % 15 or reference.second or reference.microsecond
                or inference.get("symbol") != symbol
                or instant(inference["reference_at"]) != reference
                or inference.get("availability_kind") != "actual"
                or not reference + STEP <= instant(inference["information_available_at"]) <= cutoff
                or cutoff > instant(self.clock())):
            raise ContractError("Snapshot reference, actual availability or causal cutoff mismatch")
        if snapshot["manifest"].get("target_version", TARGET_VERSION) != TARGET_VERSION:
            raise ContractError("Snapshot target definition mismatch")
        names, rows, labels = snapshot["feature_names"], snapshot["X"], snapshot["y"]
        if (not names or len(set(names)) != len(names) or not rows
                or len(rows) != len(labels) or any(label not in (0, 1) for label in labels)
                or any(len(row) != len(names) for row in rows)
                or len(snapshot["inference_X"]) != 1 or len(snapshot["inference_X"][0]) != len(names)
                or any(len(snapshot[field]) != len(rows)
                       for field in ("label_available_at", "reference_at", "target_close_at"))):
            raise ContractError("Snapshot matrix, labels or feature order mismatch")
        if any(instant(value) > cutoff for value in snapshot["label_available_at"]):
            raise ContractError("Snapshot includes a label unavailable at training cutoff")
        if any(instant(value) > cutoff for value in snapshot["target_close_at"]):
            raise ContractError("Snapshot includes an unmatured training target")
        if any(instant(value) >= reference for value in snapshot["reference_at"]):
            raise ContractError("Snapshot includes a future training reference")
        for candle in snapshot.get("candles", ()):
            if (candle.get("symbol") != symbol or instant(candle["opened_at"]) > reference
                    or instant(candle["available_at"]) > cutoff):
                raise ContractError("Snapshot candle ownership or cutoff mismatch")
        return reference, cutoff

    def publish(self, cycle_id, symbol, cycle, snapshot):
        begun = time.perf_counter()
        self.measurements = {}
        # Cycle progress/failure summaries are mutable operational records.
        # The handoff freezes only the original scientific/admission envelope.
        cycle = {key: cycle[key] for key in ("pc", "symbols", "reference_at", "mode", "experiment",
            "configuration_hash", "configuration", "supervised_model_policy", "started_at",
            "training_cutoff", "candle_close", "scheduled_acquisition", "acquisition",
            "full_retrain", "runtime_provenance", "scheduling_policy", "overrun_policy") if key in cycle}
        if isinstance(snapshot, tuple):
            claimed, snapshot = snapshot
            if digest(snapshot) != claimed:
                raise ContractError("Prepared snapshot digest mismatch")
        reference, cutoff = self._validate_snapshot(cycle_id, symbol, cycle, snapshot)
        serialized = time.perf_counter()
        raw = encoded(snapshot).encode()
        snapshot_hash = self._write("snapshots", raw, MAX_SNAPSHOT_BYTES)
        cycle_hash = self._write("cycles", encoded(cycle).encode(), MAX_RECORD_BYTES)
        claim_id = digest(dict(actor=self.actor, cycle_id=cycle_id, symbol=symbol))
        claim_path = self._path("claims", claim_id)
        prior_record = None
        if claim_path.exists():
            claim_bytes = claim_path.read_bytes()
            if len(claim_bytes) > MAX_RECORD_BYTES:
                raise ContractError("Oversized snapshot publication claim")
            prior_record = self._record(json.loads(claim_bytes)["record_sha256"])
        metadata = dict(schema=VERSION, actor=self.actor, machine=self.machine, symbols=list(self.symbols),
            symbol=symbol, cycle_id=cycle_id, configuration_hash=cycle["configuration_hash"],
            scientific_configuration_hash=digest({key: value for key, value in cycle["configuration"].items()
                if key not in {"symbol_workers", "selected_models_by_symbol"}}),
            producer_session=cycle["experiment"], reference_at=reference.isoformat(),
            training_cutoff=cutoff.isoformat(), information_available_at=snapshot["inference"]["information_available_at"],
            actual_availability=True, target_definition=TARGET_VERSION,
            target_open=(reference + 2 * STEP).isoformat(), target_close=(reference + 3 * STEP).isoformat(),
            feature_order_sha256=digest(snapshot["feature_names"]), recipe_sha256=digest(snapshot.get("model_selection")),
            dataset_scope_sha256=digest(snapshot.get("dataset_scope")), source_provenance_sha256=digest(snapshot.get("source_manifest")),
            snapshot_hash=snapshot_hash, snapshot_bytes=len(raw), cycle_hash=cycle_hash,
            ready_at=prior_record["ready_at"] if prior_record else instant(self.clock()).isoformat())
        record_hash = self._write("records", encoded(metadata).encode(), MAX_RECORD_BYTES)
        # The first exact cycle/symbol claim survives index loss, crashes, and
        # retries. A later policy/payload cannot replace that cycle's inputs.
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        claim_raw = encoded({"record_sha256": record_hash}).encode()
        temporary = claim_path.with_name("." + uuid.uuid4().hex + ".partial")
        try:
            with temporary.open("xb") as stream:
                stream.write(claim_raw); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, claim_path)
            except FileExistsError:
                if claim_path.read_bytes() != claim_raw:
                    raise ContractError("Cycle snapshot publication was already claimed with different bytes")
        finally:
            temporary.unlink(missing_ok=True)
        indexed = time.perf_counter()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT reference_at,record_sha256 FROM ready WHERE actor=? AND symbol=?",
                                  (self.actor, symbol)).fetchone()
            if previous is None or instant(previous[0]) < reference:
                db.execute("INSERT INTO ready VALUES(?,?,?,?,?) ON CONFLICT(actor,symbol) DO UPDATE SET "
                           "reference_at=excluded.reference_at,configuration_hash=excluded.configuration_hash,"
                           "record_sha256=excluded.record_sha256",
                           (self.actor, symbol, reference.isoformat(), cycle["configuration_hash"], record_hash))
            elif instant(previous[0]) == reference and previous[1] != record_hash:
                # Keep first producer identity for this reference, never publish
                # a revised model opportunity with a different recipe/session.
                return self._record(previous[1])
        self.measurements = dict(validation_seconds=serialized-begun,
            serialization_publication_seconds=indexed-serialized,
            index_seconds=time.perf_counter()-indexed, snapshot_bytes=len(raw))
        return metadata | {"record_sha256": record_hash}

    def _record(self, identity):
        value = json.loads(self._read("records", identity, MAX_RECORD_BYTES))
        if (value.get("schema") != VERSION or value.get("actor") != self.actor
                or value.get("machine") != self.machine or value.get("symbols") != list(self.symbols)
                or value.get("symbol") not in self.symbols or value.get("actual_availability") is not True
                or value.get("target_definition") != TARGET_VERSION):
            raise ContractError("Ready record ownership or contract mismatch")
        reference = instant(value["reference_at"])
        if (instant(value["target_open"]) != reference + 2 * STEP
                or instant(value["target_close"]) != reference + 3 * STEP
                or not reference + STEP <= instant(value["information_available_at"]) <= instant(value["training_cutoff"])
                or instant(value["ready_at"]) < instant(value["training_cutoff"])):
            raise ContractError("Ready record target or causal clocks changed")
        for key in ("cycle_id", "configuration_hash", "scientific_configuration_hash", "snapshot_hash", "cycle_hash",
                    "feature_order_sha256", "recipe_sha256", "dataset_scope_sha256", "source_provenance_sha256"):
            _hash(value[key])
        return value | {"record_sha256": identity}

    def ready(self, now, *, configuration_hash, session_started_at=None, scientific_configuration_hash=None):
        current = instant(now)
        self.rejected = []
        # All filtering identities are verified against immutable record bytes.
        with self.connect() as db:
            rows = db.execute("SELECT symbol,reference_at,configuration_hash,record_sha256 FROM ready "
                              "WHERE actor=? ORDER BY reference_at,symbol", (self.actor,)).fetchall()
        result = []
        for symbol, reference, configuration, identity in rows:
            try:
                value = self._record(identity)
                if (value["symbol"], value["reference_at"], value["configuration_hash"]) != (symbol, reference, configuration):
                    raise ContractError("Ready index differs from immutable record")
                if ((value["configuration_hash"] == configuration_hash
                        or scientific_configuration_hash is not None
                        and value["scientific_configuration_hash"] == scientific_configuration_hash)
                        and instant(value["ready_at"]) <= current < instant(value["target_close"])):
                    result.append(value)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.rejected.append(dict(symbol=symbol, error_type=type(exc).__name__))
        # session_started_at does not relabel producer evidence. A newly issued
        # consumer forecast is still bound to its own manual-session admission.
        return result

    def load(self, record):
        value = self._record(record["record_sha256"])
        if value != record:
            raise ContractError("Ready record caller identity mismatch")
        cycle = json.loads(self._read("cycles", value["cycle_hash"], MAX_RECORD_BYTES))
        raw = self._read("snapshots", value["snapshot_hash"], MAX_SNAPSHOT_BYTES)
        if len(raw) != value["snapshot_bytes"]:
            raise ContractError("Ready snapshot length mismatch")
        snapshot = json.loads(raw)
        self._validate_snapshot(value["cycle_id"], value["symbol"], cycle, snapshot)
        for key, actual in (("feature_order_sha256", digest(snapshot["feature_names"])),
                            ("recipe_sha256", digest(snapshot.get("model_selection"))),
                            ("dataset_scope_sha256", digest(snapshot.get("dataset_scope"))),
                            ("source_provenance_sha256", digest(snapshot.get("source_manifest")))):
            if actual != value[key]:
                raise ContractError("Ready snapshot contract differs from its publication")
        if (cycle["configuration_hash"] != value["configuration_hash"] or cycle["experiment"] != value["producer_session"]
                or digest({key: item for key, item in cycle["configuration"].items()
                    if key not in {"symbol_workers", "selected_models_by_symbol"}}) != value["scientific_configuration_hash"]
                or snapshot["cutoff"] != value["training_cutoff"]
                or snapshot["inference"]["information_available_at"] != value["information_available_at"]):
            raise ContractError("Ready snapshot provenance differs from its publication")
        return cycle, snapshot
