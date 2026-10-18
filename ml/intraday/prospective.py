"""Frozen-model prospective observations, with no training or order interface.

This collector owns its journal and evidence store. A forecast is eligible only
after its durable SQLite publication has an actual pre-target commit receipt.
Interrupted publications are retained as candidates, never backdated on restart.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import uuid

import numpy as np
import pandas as pd

from .data import (FEATURE_NAMES, AcquisitionJournal, causal_features, equity_sessions,
                   fetch_databento_interval_attempts, validate_candles)
from .prediction import ContractError, PACIFIC, STEP, TARGET_VERSION, digest, encoded, instant
from .native_interval import CONTRACT as NATIVE_CONTRACT, completed_native_interval, resolve_native_interval

SCHEMA = "intraday-prospective-collector-v1"
REQUIRED_SOURCES = tuple("ml/intraday/" + name for name in (
    "prospective.py", "prospective_worker.py", "data.py", "native_interval.py", "models.py",
    "feature_support.py", "prediction.py")) + ("app/services/databento_market_data.py",)


def bounded_existing_provider():
    """Same configured authenticated read provider, with bounded per-call I/O."""
    from app.services.databento_market_data import DatabentoMarketDataProvider

    class BoundedProvider(DatabentoMarketDataProvider):
        def _client(self):
            client = super()._client()
            # Per-instance endpoint settings: never mutate the shared library,
            # subscription, dataset, native schema or authentication bindings.
            for endpoint in (client.metadata, client.timeseries):
                if not hasattr(endpoint, "TIMEOUT"):
                    raise ContractError("Installed Databento client lacks bounded I/O setting")
                endpoint.TIMEOUT = 5
            return client

    return BoundedProvider()


def utcnow():
    return datetime.now(timezone.utc)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked_bytes(binding):
    raw = Path(binding["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
        raise ContractError("Frozen file hash differs: " + str(binding["path"]))
    return raw


def jsonable(value):
    return json.loads(json.dumps(value, default=lambda x: x.isoformat()
                                if hasattr(x, "isoformat") else x.item(), allow_nan=False))


def slots(config):
    """Explicit predeclared dates; exchange closures are never fabricated slots."""
    start, end = [time.fromisoformat(config[k]) for k in ("decision_start_local", "decision_end_local")]
    if (start < time(4, 15) or end > time(16, 30) or start > end
            or any(t.minute % 15 or t.second or t.microsecond for t in (start, end))):
        raise ContractError("Decision window must be quarter hours within 04:15--16:30 Pacific")
    dates = config["session_dates"]
    if not dates or dates != sorted(set(dates)):
        raise ContractError("Assessment dates must be explicit, unique and chronological")
    offset = float(config["publication_offset_seconds"])
    if not 0 <= offset < 900:
        raise ContractError("Publication offset must be within the decision quarter")
    bounds = equity_sessions(dates[0] + "T00:00:00Z", dates[-1] + "T23:59:59Z")
    result = []
    for day in dates:
        cursor = datetime.combine(datetime.fromisoformat(day).date(), start, PACIFIC)
        last = datetime.combine(cursor.date(), end, PACIFIC)
        while cursor <= last:
            decision = instant(cursor)
            reference, target = decision - STEP, decision + STEP
            session = next((b for b in bounds if b.session_id == day
                            and b.contains(reference) and b.contains(target)), None)
            if session:
                result.append({"session": day, "reference_at": reference.isoformat(),
                    "decision_open": decision.isoformat(),
                    "scheduled_at": (decision + timedelta(seconds=offset)).isoformat(),
                    "target_open": target.isoformat(), "target_close": (target + STEP).isoformat(),
                    "session_bounds": asdict(session)})
            cursor += STEP
    return tuple(result)


class FrozenModels:
    """Load the designated fitted bytes. Qualification is deliberately not an input."""
    def __init__(self, config):
        import joblib
        self.config = config
        recipe = json.loads(checked_bytes(config["recipe"]))
        protocol = json.loads(checked_bytes(config["protocol"]))
        window = protocol.get("decision_window", {})
        if (protocol.get("experiment_id") != config["experiment_id"]
                or protocol.get("symbols") != config["symbols"]
                or protocol.get("model_artifacts") != config["model_artifacts"]
                or protocol.get("recipe_sha256") != recipe.get("sha256")
                or protocol.get("assessment_sessions", {}).get("collection_dates") != config["session_dates"]
                or window.get("timezone") != "America/Los_Angeles"
                or window.get("first_decision") != config["decision_start_local"]
                or window.get("last_decision") != config["decision_end_local"]
                or window.get("offset_seconds") != config["publication_offset_seconds"]
                or window.get("cadence_minutes") != 15):
            raise ContractError("Frozen protocol scope, artifacts or decision window differs from configuration")
        if recipe.get("target_version") != TARGET_VERSION:
            raise ContractError("Frozen recipe target differs")
        self.models = {}
        first_decision = instant(config["session_dates"][0] + "T00:00:00Z")
        names = tuple(m["name"] for m in recipe["models"])
        for symbol in config["symbols"]:
            bindings = config["model_artifacts"][symbol]
            if tuple(b["name"] for b in bindings) != names:
                raise ContractError("Every designated manual member is required independently")
            loaded = []
            for binding in bindings:
                fit = json.loads(checked_bytes(binding["fit_evidence"]))
                if "training_evidence" in binding:
                    checked_bytes(binding["training_evidence"])
                if not fit.get("complete") or not any(a.get("name") == binding["name"]
                    and a.get("status") == "success" and a.get("artifact_sha256") == binding["sha256"]
                    for a in fit.get("artifacts", ())):
                    raise ContractError("Artifact lacks matching successful fit evidence")
                artifact = joblib.load(io.BytesIO(checked_bytes(binding)))
                metadata = artifact["metadata"]
                if (artifact["version"] != binding["version"] or digest(metadata) != artifact["version"]
                        or metadata["target_version"] != TARGET_VERSION
                        or metadata["spec"] != next(m for m in recipe["models"] if m["name"] == binding["name"])
                        or metadata["dataset_metadata"].get("manual_recipe_symbol") != symbol
                        or metadata["dataset_metadata"].get("manual_model_recipe") != recipe
                        or metadata["dataset_metadata"].get("native_interval_contract") != NATIVE_CONTRACT
                        or instant(metadata["training_cutoff"]) >= first_decision
                        or instant(metadata["latest_label_available_at"]) > instant(metadata["training_cutoff"])):
                    raise ContractError("Frozen artifact identity, recipe, symbol or fit cutoff differs")
                support = metadata.get("market_feature_support", {})
                feature_names = tuple(metadata["feature_names"])
                projection = artifact["estimator"].base.named_steps.get("market_support")
                if (feature_names[:len(FEATURE_NAMES)] != FEATURE_NAMES
                        or support.get("retained_columns") != list(FEATURE_NAMES)
                        or projection is None
                        or list(projection.retained_indices_) != list(range(len(FEATURE_NAMES)))):
                    raise ContractError("This collector requires frozen price-only projection; context model needs a new experiment")
                calibration = metadata.get("calibration", {})
                if (calibration.get("method") != "purged_chronological_sigmoid"
                        or calibration.get("base_rows", 0) < 1 or calibration.get("calibration_rows", 0) < 1):
                    raise ContractError("Artifact lacks fitted chronological calibration evidence")
                loaded.append((binding, artifact))
            self.models[symbol] = loaded

    def predict(self, symbol, values):
        from .models import probability_not_down
        outputs = []
        for binding, artifact in self.models[symbol]:
            if file_hash(binding["path"]) != binding["sha256"]:
                raise ContractError("Frozen model changed after loading")
            # These are explicit missing optional inputs, discarded by the exact
            # fitted projection verified above, not fabricated context observations.
            features = np.full((1, len(artifact["metadata"]["feature_names"])), np.nan)
            features[0, :len(FEATURE_NAMES)] = values
            outputs.append({"name": binding["name"], "artifact_sha256": binding["sha256"],
                "version": binding["version"], "fit_evidence_sha256": binding["fit_evidence"]["sha256"],
                "probability_not_down": probability_not_down(artifact["estimator"], features)})
        return {"members": outputs, "probability_not_down": float(np.mean([
            row["probability_not_down"] for row in outputs])), "aggregation": "arithmetic_mean_all_frozen_manual_members",
            "optional_context": "missing_and_discarded_by_frozen_training_only_projection"}


class ProspectiveStore:
    """Append-only publications, receipts, missing slots, inputs, and raw outcomes."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publications(slot TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS terminals(slot TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candles(slot TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outcomes(slot TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coverage(slot TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS candles_symbol_time ON candles(
                    json_extract(payload, '$.symbol'), json_extract(payload, '$.opened_at'));
            """)
            for table in ("identity", "publications", "terminals", "candles", "outcomes", "coverage", "events"):
                for verb in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{verb} BEFORE {verb} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable prospective evidence'); END")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return closing_transaction(db)

    def bind(self, config):
        value = {"config_sha256": digest(config), "experiment_id": config["experiment_id"]}
        with self.connect() as db:
            old = db.execute("SELECT payload FROM identity WHERE id='experiment'").fetchone()
            if old and old[0] != encoded(value):
                raise ContractError("Store already belongs to a different frozen experiment")
            db.execute("INSERT OR IGNORE INTO identity VALUES('experiment',?)", (encoded(value),))

    def get(self, table, slot):
        if table not in {"publications", "terminals", "candles", "outcomes", "coverage"}:
            raise ContractError("Unknown evidence table")
        with self.connect() as db:
            row = db.execute(f"SELECT payload FROM {table} WHERE slot=?", (slot,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, table, slot, payload):
        if table not in {"publications", "terminals", "candles", "outcomes", "coverage"}:
            raise ContractError("Unknown evidence table")
        raw = encoded(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(f"SELECT payload FROM {table} WHERE slot=?", (slot,)).fetchone()
            if old and old[0] != raw:
                raise ContractError("Prospective evidence is immutable")
            db.execute(f"INSERT OR IGNORE INTO {table} VALUES(?,?)", (slot, raw))

    def rows(self, table):
        if table not in {"publications", "terminals", "candles", "outcomes", "coverage", "events"}:
            raise ContractError("Unknown evidence table")
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(f"SELECT payload FROM {table} ORDER BY rowid")]

    def recent_candles(self, symbol, cutoff):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM candles WHERE json_extract(payload,'$.symbol')=? "
                "AND json_extract(payload,'$.opened_at')<=? ORDER BY json_extract(payload,'$.opened_at') DESC LIMIT 42",
                (symbol, instant(cutoff).isoformat())).fetchall()
        return [json.loads(r[0]) for r in rows]

    def terminal_keys(self):
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT slot FROM terminals")}

    def coverage_keys(self, *, include_unresolved=False, cutoff=None):
        with self.connect() as db:
            sql = "SELECT slot FROM coverage"
            predicates, parameters = [], []
            if not include_unresolved:
                predicates.append("json_extract(payload,'$.status') IN ('observed','source_empty')")
            if cutoff is not None:
                predicates.append("json_extract(payload,'$.available_at')<=?")
                parameters.append(instant(cutoff).isoformat())
            if predicates:
                sql += " WHERE " + " AND ".join(predicates)
            return {r[0] for r in db.execute(sql, parameters)}

    def cursor(self, name, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                           (name, value))
            row = db.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        return row[0] if row else ""

    def pending_outcomes(self, now):
        with self.connect() as db:
            rows = db.execute("SELECT t.payload FROM terminals t LEFT JOIN outcomes o ON t.slot=o.slot "
                "WHERE o.slot IS NULL AND json_extract(t.payload,'$.status')='published' "
                "AND json_extract(t.payload,'$.target_close')<=? ORDER BY json_extract(t.payload,'$.target_open')",
                (instant(now).isoformat(),)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def event(self, kind, clock, **details):
        payload = {"kind": kind, "recorded_at": instant(clock()).isoformat(), **details}
        with self.connect() as db:
            db.execute("INSERT INTO events VALUES(?,?)", (uuid.uuid4().hex, encoded(payload)))


class closing_transaction:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        try:
            return self.db.__exit__(*args)
        finally:
            self.db.close()


class RetainedProvider:
    """Wrap only market-data reads and preserve the provider's actual raw frames."""
    def __init__(self, provider, root, store, clock):
        self.provider, self.root, self.store, self.clock = provider, Path(root), store, clock
        self.root.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def fetch_native_bars_range(self, symbols, spec, **kwargs):
        result, selected = self.provider.fetch_native_bars_range(symbols, spec, **kwargs)
        receipt = instant(self.clock()).isoformat()
        raw_files = []
        for symbol, (_, raw) in result.items():
            if raw is None:
                raise ContractError("Actual native provider response required")
            path = self.root / (uuid.uuid4().hex + ".parquet")
            with path.open("xb") as handle:
                raw.to_parquet(handle, index=True)
                handle.flush()
                import os
                os.fsync(handle.fileno())
            raw_files.append({"symbol": symbol, "path": str(path.resolve()), "sha256": file_hash(path),
                              "rows": len(raw)})
        self.store.event("native_response", self.clock, source_dataset=self.dataset,
            source_schema=spec.schema, source_id=f"databento:{self.dataset}:{spec.schema}",
            interval_open=instant(kwargs["start"]).isoformat(), interval_close=instant(kwargs["end"]).isoformat(),
            actual_response_received_at=receipt, raw_files=raw_files)
        return result, selected


class Collector:
    def __init__(self, config, *, models=None, acquire=None, clock=utcnow):
        if config.get("schema") != SCHEMA or config.get("source_dataset") != "EQUS.MINI":
            raise ContractError("Explicit prospective collector schema and single EQUS source required")
        if not config.get("symbols") or len(set(config["symbols"])) != len(config["symbols"]):
            raise ContractError("Unique owned symbols required")
        self.config, self.clock = config, clock
        grace = float(config.get("outcome_recovery_seconds", 86400))
        if not np.isfinite(grace) or grace <= 0:
            raise ContractError("Outcome recovery grace must be finite and predeclared")
        self.schedule = slots(config)
        self.capture_slots = []
        dates = config["session_dates"]
        for session in equity_sessions(dates[0] + "T00:00:00Z", dates[-1] + "T23:59:59Z"):
            if session.session_id not in dates:
                continue
            opened = instant(session.opened_at)
            while session.contains(opened):
                self.capture_slots.append({"session": session.session_id, "reference_at": opened.isoformat(),
                    "session_bounds": asdict(session), "scheduled_at":
                    (opened + STEP + timedelta(seconds=config["publication_offset_seconds"])).isoformat()})
                opened += STEP
        self.root = Path(config["state_root"])
        self.store = ProspectiveStore(self.root / "prospective.sqlite3")
        self.store.bind(config)
        self.verify_sources()
        self.models = models if models is not None else FrozenModels(config)
        self.acquire = acquire
        self.journal = AcquisitionJournal(self.root / "acquisition.sqlite3")
        self.provider = None
        self.warmup = {}
        for symbol in config["symbols"]:
            binding = config["warmup"][symbol]
            rows = json.loads(checked_bytes(binding))["candles"]
            frame = validate_candles(pd.DataFrame(rows))
            if (set(frame.symbol) != {symbol} or set(frame.source_dataset) != {"EQUS.MINI"}
                    or frame.synthetic.any() or len(frame) < 21
                    or (frame.opened_at + STEP).gt(pd.Timestamp(instant(binding["available_at"]))).any()):
                raise ContractError("Frozen warmup requires authentic observed same-source history")
            self.warmup[symbol] = rows

    def verify_sources(self):
        base = Path(__file__).resolve().parents[2]
        if set(REQUIRED_SOURCES) - set(self.config.get("source_sha256", {})):
            raise ContractError("Required prospective source bindings are missing")
        for relative, expected in self.config.get("source_sha256", {}).items():
            path = (base / relative).resolve()
            if not path.is_relative_to(base) or file_hash(path) != expected:
                raise ContractError("Pinned prospective implementation changed: " + relative)
        checked_bytes(self.config["protocol"])
        checked_bytes(self.config["recipe"])

    def key(self, symbol, slot):
        return digest([self.config["experiment_id"], symbol, slot["reference_at"], TARGET_VERSION])

    def missing(self, symbol, slot, reason):
        key = self.key(symbol, slot)
        if not self.store.get("terminals", key):
            self.store.put("terminals", key, {"slot_id": key, "symbol": symbol, **slot,
                "status": "missing_prediction", "reason": reason,
                "recorded_at": instant(self.clock()).isoformat(), "experiment_id": self.config["experiment_id"]})

    def _history(self, symbol, cutoff):
        binding = self.config["warmup"][symbol]
        if instant(binding["available_at"]) > instant(cutoff):
            raise ContractError("Frozen historical snapshot not possessed by decision")
        rows = [*self.warmup[symbol], *self.store.recent_candles(symbol, cutoff)]
        selected = {}
        for original in rows:
            row = dict(original)
            if instant(row["available_at"]) > instant(cutoff) or row["synthetic"]:
                continue
            opened = instant(row["opened_at"])
            if opened in selected and selected[opened] != row:
                raise ContractError("Conflicting observed history; no silent revision selection")
            selected[opened] = row
        return [selected[k] for k in sorted(selected)]

    def acquire_interval(self, symbols, slot, interval):
        if self.acquire:
            return self.acquire(symbols, slot, interval)
        if self.provider is None:
            provider = bounded_existing_provider()
            if provider.dataset != self.config["source_dataset"]:
                raise ContractError("Existing provider binding differs from frozen source")
            self.provider = RetainedProvider(provider, self.root / "raw", self.store, self.clock)
        due = self.journal.provider_retry_due()
        if due is not None and due > pd.Timestamp(self.clock()):
            return {}
        from .data import SessionBounds
        session = SessionBounds(**slot["session_bounds"])
        eligible = []
        for symbol in symbols:
            old = self.journal.responses(symbol=symbol, source_id="databento:EQUS.MINI:ohlcv-1m",
                interval_open=interval, interval_close=(instant(interval) + STEP).isoformat())
            if old and old[-1].status == "permanent_error":
                continue
            if (not old or instant(self.clock()) >= instant(old[-1].request_ended_at)
                    + timedelta(seconds=max(4.0, old[-1].retry_after_seconds))):
                eligible.append(symbol)
        if not eligible:
            return {}
        responses = fetch_databento_interval_attempts(self.provider, symbols=eligible, opened_at=interval,
            attempt_id=uuid.uuid4().hex, clock=self.clock)
        result = {}
        for symbol, response in responses.items():
            self.journal.save(response)
            attempts = self.journal.responses(symbol=symbol, source_id=response.source_id,
                interval_open=response.interval_open, interval_close=response.interval_close)
            resolved = resolve_native_interval(attempts[-2:], session=session)
            if resolved.get("candle") is not None and not resolved["candle"]["synthetic"]:
                result[symbol] = jsonable(resolved["candle"])
            else:
                if resolved.get("reason") == "source_empty_interval" or response.status == "permanent_error":
                    key = digest([symbol, instant(interval).isoformat()])
                    if not self.store.get("coverage", key):
                        self.store.put("coverage", key, {"symbol": symbol, "opened_at": instant(interval).isoformat(),
                            "status": "source_empty" if resolved.get("reason") == "source_empty_interval" else "provider_action_required",
                            "available_at": response.request_ended_at,
                            "source_id": response.source_id, "attempts_sha256": digest([asdict(a) for a in attempts[-2:]]),
                            "attempt_ids": [a.attempt_id for a in attempts[-2:]], "candle_created": False})
                self.store.event("input_unavailable", self.clock, symbol=symbol, interval_open=interval,
                    reason=resolved.get("reason", resolved.get("status")), observed_rows=len(response.rows))
        return result

    def _save_candles(self, incoming, interval):
        for symbol, row in incoming.items():
            frame = validate_candles(pd.DataFrame([row]))
            if (symbol not in self.config["symbols"] or row["symbol"] != symbol
                    or row["source_dataset"] != self.config["source_dataset"]
                    or row["source_id"] != "databento:EQUS.MINI:ohlcv-1m"
                    or instant(row["opened_at"]) != instant(interval)
                    or row["availability_kind"] != "actual" or bool(frame.synthetic.iloc[0])
                    or not completed_native_interval(row)
                    or instant(row["available_at"]) > instant(self.clock())):
                raise ContractError("Live input identity, timing or observation basis differs")
            key = digest([symbol, instant(interval).isoformat()])
            coverage = self.store.get("coverage", key)
            if coverage and coverage["status"] != "observed":
                raise ContractError("Observed revision conflicts with preserved source coverage evidence")
            old = self.store.get("candles", key)
            if old is None:
                self.store.put("candles", key, jsonable(row))
            elif old != jsonable(row):
                raise ContractError("Conflicting immutable source observation; retain both raw attempts for review")
            if coverage is None:
                self.store.put("coverage", key, {"symbol": symbol, "opened_at": instant(interval).isoformat(),
                    "status": "observed", "candle_sha256": digest(row), "available_at": row["available_at"]})

    def predict_slot(self, symbol, slot):
        key = self.key(symbol, slot)
        if self.store.get("terminals", key):
            return
        if self.store.get("publications", key):
            self.missing(symbol, slot, "interrupted_before_publication_commit_receipt")
            return
        decided = instant(self.clock())
        if not instant(slot["scheduled_at"]) <= decided < instant(slot["target_open"]):
            self.missing(symbol, slot, "decision_window_missed")
            return
        history = [r for r in self._history(symbol, decided) if instant(r["opened_at"]) <= instant(slot["reference_at"])]
        if not history or instant(history[-1]["opened_at"]) != instant(slot["reference_at"]):
            raise ContractError("Exact observed reference unavailable")
        history = history[-21:]
        coverage = self.store.coverage_keys(cutoff=decided)
        forward_coverage = []
        for required in self.capture_slots:
            opened = instant(required["reference_at"])
            if opened > instant(slot["reference_at"]):
                break
            if opened > instant(history[0]["opened_at"]):
                coverage_key = digest([symbol, required["reference_at"]])
                if coverage_key not in coverage:
                    raise ContractError("Forward history has an unexamined interval within the feature window")
                forward_coverage.append(self.store.get("coverage", coverage_key))
        values = causal_features(pd.DataFrame(history)).iloc[-1].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ContractError("Observed price feature warmup insufficient")
        result = self.models.predict(symbol, values)
        probabilities = [m["probability_not_down"] for m in result["members"]]
        if (not probabilities or not np.isfinite(probabilities).all()
                or any(not 0 <= p <= 1 for p in probabilities)
                or not np.isfinite(result["probability_not_down"])
                or result["probability_not_down"] != float(np.mean(probabilities))):
            raise ContractError("Manual probabilities must be finite and preserve all-member arithmetic mean")
        if instant(self.clock()) < decided:
            self.missing(symbol, slot, "wall_clock_regressed_during_inference")
            return
        if instant(self.clock()) >= instant(slot["target_open"]):
            self.missing(symbol, slot, "inference_finished_after_deadline")
            return
        inputs = {"candles": history, "feature_names": list(FEATURE_NAMES), "feature_values": values.tolist(),
            "warmup_snapshot": self.config["warmup"][symbol], "forward_coverage": forward_coverage,
            "original_receipt_limitation_preserved": True}
        payload = {"schema": SCHEMA, "slot_id": key, "symbol": symbol, **slot,
            "experiment_id": self.config["experiment_id"], "target_version": TARGET_VERSION,
            "recipe_sha256": self.config["recipe"]["sha256"], "protocol_sha256": self.config["protocol"]["sha256"],
            "source_sha256": self.config.get("source_sha256", {}), "source_id": "databento:EQUS.MINI:ohlcv-1m",
            "decided_at": decided.isoformat(), "inputs": inputs, "inputs_sha256": digest(inputs), **result}
        self.verify_sources()
        if instant(self.clock()) >= instant(slot["target_open"]):
            self.missing(symbol, slot, "source_verification_finished_after_deadline")
            return
        self.store.put("publications", key, payload)
        # Clock is sampled AFTER the FULL-synchronous publication commit. A crash
        # before this separate receipt leaves an explicitly unconfirmed candidate.
        published = instant(self.clock())
        if published < decided:
            self.missing(symbol, slot, "wall_clock_regressed_during_publication")
            return
        if published >= instant(slot["target_open"]):
            self.missing(symbol, slot, "publication_commit_completed_after_deadline")
            return
        self.store.put("terminals", key, {"slot_id": key, "symbol": symbol, **slot,
            "status": "published", "publication_sha256": digest(payload), "published_at": published.isoformat(),
            "experiment_id": self.config["experiment_id"], "qualification": "not_assessed"})

    def tick(self):
        self.verify_sources()
        now = instant(self.clock())
        processed = 0
        completed = self.store.terminal_keys()
        for slot in self.schedule:
            if now < instant(slot["scheduled_at"]):
                continue
            pending = [s for s in self.config["symbols"] if self.key(s, slot) not in completed]
            if not pending:
                continue
            if now >= instant(slot["target_open"]):
                for symbol in pending:
                    self.missing(symbol, slot, "collector_unavailable_before_deadline")
                continue
            for symbol in tuple(pending):
                if self.store.get("publications", self.key(symbol, slot)):
                    self.missing(symbol, slot, "interrupted_before_publication_commit_receipt")
                    pending.remove(symbol)
            need = [s for s in pending if not self.store.get("candles", digest([s, slot["reference_at"]]))]
            if need:
                try:
                    self._save_candles(self.acquire_interval(need, slot, slot["reference_at"]), slot["reference_at"])
                except Exception as exc:
                    self.store.event("acquisition_error", self.clock, exception_type=type(exc).__name__,
                                     reference_at=slot["reference_at"])
            for symbol in pending:
                try:
                    self.predict_slot(symbol, slot)
                except Exception as exc:
                    self.store.event("prediction_not_ready", self.clock, symbol=symbol,
                        reference_at=slot["reference_at"], exception_type=type(exc).__name__, reason=str(exc)[:500])
                processed += 1
        self.collect_background()
        self.collect_outcomes()
        result = {"status": "tick_complete", "at": instant(self.clock()).isoformat(),
            "processed": processed, "scheduled_slots": len(self.schedule), "trading": False,
            "future_slots": sum(instant(s["scheduled_at"]) > instant(self.clock()) for s in self.schedule)}
        self.store.event("tick", self.clock, **result)
        return result

    def collect_background(self):
        """Acquire the full exchange envelope, including premarket before decisions.

        At most two exact interval groups per tick. Earlier unknown intervals
        remain explicit and can never be silently skipped in the feature window.
        Current predictions run first; missed predictions are never recreated.
        """
        coverage = self.store.coverage_keys(include_unresolved=True)
        count = 0
        due = [s for s in self.capture_slots if instant(s["scheduled_at"]) <= instant(self.clock())]
        cursor = self.store.cursor("background_interval")
        due = [s for s in due if s["reference_at"] > cursor] + [s for s in due if s["reference_at"] <= cursor]
        for slot in due:
            interval = slot["reference_at"]
            need = [s for s in self.config["symbols"] if digest([s, interval]) not in coverage]
            if not need:
                continue
            if not self.background_budget_available():
                break
            try:
                self._save_candles(self.acquire_interval(need, slot, interval), interval)
            except Exception as exc:
                self.store.event("background_acquisition_error", self.clock, reference_at=interval,
                                 exception_type=type(exc).__name__)
            self.store.cursor("background_interval", interval)
            count += 1
            if count >= 2:
                break

    def background_budget_available(self):
        """Admit auxiliary work only with runway before required forecast work.

        Each exact-range group makes at most metadata and price reads with 5s
        connect/read timeouts. Recheck a conservative 120s budget between groups
        so four serial background/outcome groups cannot consume a new decision.
        """
        now = instant(self.clock())
        completed = self.store.terminal_keys()
        deadlines = []
        for slot in self.schedule:
            scheduled = instant(slot["scheduled_at"])
            if scheduled > now:
                deadlines.append(scheduled)
                break
            if now < instant(slot["target_open"]) and any(
                    self.key(s, slot) not in completed for s in self.config["symbols"]):
                deadlines.append(instant(slot["target_open"]))
        return not deadlines or (min(deadlines) - now).total_seconds() > 120

    def collect_outcomes(self):
        """Collect raw labels separately; never fit, score, select or extend here."""
        terminals = self.store.pending_outcomes(self.clock())
        groups = {}
        for row in terminals:
            recovery_end = instant(row["target_close"]) + timedelta(seconds=float(
                self.config.get("outcome_recovery_seconds", 86400)))
            saved = self.store.get("candles", digest([row["symbol"], row["target_open"]]))
            if saved and instant(saved["available_at"]) <= recovery_end:
                self._record_outcome(row, saved)
            elif instant(self.clock()) >= recovery_end:
                self.store.put("outcomes", row["slot_id"], {"slot_id": row["slot_id"],
                    "symbol": row["symbol"], "status": "missing_outcome", "target_open": row["target_open"],
                    "reason": "source_observation_unavailable_by_predeclared_recovery_deadline",
                    "recorded_at": instant(self.clock()).isoformat(), "scored": False})
            else:
                groups.setdefault(row["target_open"], []).append(row)
        # Bound catch-up work; on the next durable scheduled tick resume the rest.
        for interval, rows in list(sorted(groups.items()))[:2]:
            if not self.background_budget_available():
                break
            try:
                need = [r["symbol"] for r in rows if not self.store.get("candles", digest([r["symbol"], interval]))]
                if need:
                    self._save_candles(self.acquire_interval(need, rows[0], interval), interval)
                for row in rows:
                    candle = self.store.get("candles", digest([row["symbol"], interval]))
                    recovery_end = instant(row["target_close"]) + timedelta(seconds=float(
                        self.config.get("outcome_recovery_seconds", 86400)))
                    if candle and instant(candle["available_at"]) <= recovery_end:
                        self._record_outcome(row, candle)
                    else:
                        self.store.event("outcome_unavailable", self.clock, slot_id=row["slot_id"],
                                         symbol=row["symbol"], target_open=interval)
            except Exception as exc:
                self.store.event("outcome_acquisition_error", self.clock, target_open=interval,
                                 exception_type=type(exc).__name__)

    def _record_outcome(self, row, candle):
        forecast = self.store.get("publications", row["slot_id"])
        reference = forecast["inputs"]["candles"][-1]
        self.store.put("outcomes", row["slot_id"], {"slot_id": row["slot_id"],
            "symbol": row["symbol"], "status": "observed", "candle": candle,
            "label_not_down": int(float(candle["close"]) >= float(reference["open"])),
            "recorded_at": instant(self.clock()).isoformat(), "scored": False})
