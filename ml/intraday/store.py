"""Host-local shadow evidence. Forecasts, inputs and outcome revisions are immutable.

This module deliberately has no broker, account, or execution dependency. Operational
cycle summaries may advance; every transition also has an append-only event.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from .prediction import ContractError, STEP, TARGET_VERSION, digest, encoded, instant, number


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

    def forecast_by_id(self, forecast_id, *, pc=None):
        """Validate one immutable forecast without loading unrelated history."""
        with self.connect() as db:
            row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
        value = self._forecast_value(row) if row else None
        # Validate indexed identity before scope filtering, so a changed pc
        # column cannot disguise corruption as an ordinary missing forecast.
        return value if value is not None and (pc is None or value["pc"] == pc) else None

    def forecasts(self, *, pc=None):
        with self.connect() as db:
            rows = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts" + (" WHERE pc=?" if pc else "") + " ORDER BY rowid", (pc,) if pc else ()).fetchall()
        return [self._forecast_value(row) for row in rows]

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
        key = digest(value)
        value["forecast_id"] = key
        intent_value = self._intent(key, value, intent)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE cycle_id=? AND symbol=?", (value["cycle_id"], value["symbol"])).fetchone()
            previous = self._forecast_value(old) if old else None
            if previous is not None and previous != value:
                raise ContractError("Issued forecast is immutable")
            if old:
                return previous
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (key, value["pc"], value["symbol"], value["cycle_id"], value["reference_at"], value["issued_at"], encoded(value)))
            db.execute("INSERT INTO intents VALUES(?,?,?,?)", (digest(intent_value), key, value["issued_at"], encoded(intent_value)))
        return value

    @staticmethod
    def _intent(key, forecast, supplied):
        side = "BUY" if float(forecast["probability_not_down"]) >= .5 else "SELL"
        value = dict(supplied or {})
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
        return {**value, "forecast_id": key, "side": side, "desired_quantity": desired,
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
