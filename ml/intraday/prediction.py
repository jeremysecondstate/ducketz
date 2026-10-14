"""Causal opening-time candles and immutable not-down predictions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
STEP = timedelta(minutes=15)
TARGET_VERSION = "intraday-15m-open-to-third-close-not-down-v1"


class ContractError(ValueError):
    pass


class UnresolvedPolicy(ContractError):
    pass


def instant(value: str | datetime) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError("Timezone-aware timestamp required") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractError("Timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def number(value, *, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ContractError("Finite nonnegative number required") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ContractError("Finite nonnegative number required")
    return result


def quantity(value, *, positive=False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError("Nonnegative whole-share quantity required")
    return value


def identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ContractError("Nonempty identity required")
    return value


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True)
class Candle:
    symbol: str
    opened_at: str
    available_at: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    source_id: str

    def validate(self):
        identity(self.symbol)
        identity(self.source_id)
        start = instant(self.opened_at)
        if start.minute % 15 or start.second or start.microsecond:
            raise ContractError("Candle must be named by its quarter-hour opening")
        if instant(self.available_at) < start + STEP:
            raise ContractError("Completed candle availability precedes its close")
        op, hi, lo, cl = [number(v, positive=True) for v in (self.open, self.high, self.low, self.close)]
        if not lo <= min(op, cl) <= max(op, cl) <= hi:
            raise ContractError("Invalid OHLC candle")
        number(self.volume)
        return self


def causal_candles(candles, *, symbol, issued_at):
    now = instant(issued_at)
    rows = {}
    for candle in candles:
        candle.validate()
        if candle.symbol != symbol or instant(candle.available_at) > now:
            continue
        start = instant(candle.opened_at)
        if start in rows and rows[start] != candle:
            raise ContractError("Conflicting available candle observations")
        rows[start] = candle
    return tuple(rows[k] for k in sorted(rows))


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    symbol: str
    issued_at: str
    data_through: str
    information_available_at: str
    reference_at: str
    reference_open: str
    target_open: str
    target_close: str
    probability_not_down: str
    model_version: str
    target_version: str
    inputs_json: str
    input_fingerprint: str

    def validate(self):
        payload = asdict(self)
        payload.pop("prediction_id")
        inputs = json.loads(self.inputs_json)
        if self.prediction_id != digest(payload) or self.input_fingerprint != digest(inputs):
            raise ContractError("Prediction identity or inputs changed")
        if self.target_version != TARGET_VERSION or number(self.probability_not_down) > 1:
            raise ContractError("Prediction target or probability invalid")
        now, ref = instant(self.issued_at), instant(self.reference_at)
        rows = causal_candles([Candle(**c) for c in inputs["candles"]], symbol=self.symbol, issued_at=now)
        if (len(rows) != len(inputs["candles"]) or not rows or instant(rows[-1].opened_at) != ref
                or number(rows[-1].open) != number(self.reference_open)):
            raise ContractError("Prediction reference differs from recorded causal inputs")
        if (instant(self.data_through) != ref + STEP or instant(self.target_open) != ref + 2 * STEP
                or instant(self.target_close) != ref + 3 * STEP or not ref + STEP <= now < ref + 2 * STEP):
            raise ContractError("Prediction timing differs from the moving target")
        availability = [instant(c.available_at) for c in rows]
        availability.extend(instant(v["available_at"]) for v in inputs["additional_inputs"].values())
        if max(availability) != instant(self.information_available_at) or max(availability) > now:
            raise ContractError("Prediction information availability differs from recorded inputs")
        identity(self.model_version)
        return self


def issue_prediction(candles, *, symbol, issued_at, eligible_session: date,
                     model_version, model_target_version, infer, additional_inputs=None):
    """An injected inference seam, with no training or legacy label substitution."""
    if model_target_version != TARGET_VERSION:
        raise ContractError("Model must explicitly implement the not-down target")
    identity(model_version)
    now = instant(issued_at)
    local = now.astimezone(PACIFIC)
    slot = local.replace(minute=local.minute // 15 * 15, second=0, microsecond=0)
    if local.date() != eligible_session:
        raise ContractError("Issue date differs from caller's eligible session")
    if (slot.hour, slot.minute) <= (4, 0):
        raise UnresolvedPolicy("04:00 next-open reference and target remain unresolved")
    if (slot.hour, slot.minute) >= (16, 45):
        raise UnresolvedPolicy("Final 16:45 target and genuine observation availability remain unresolved")
    rows = causal_candles(candles, symbol=symbol, issued_at=now)
    ref_time = instant(slot) - STEP
    if not rows or instant(rows[-1].opened_at) != ref_time:
        raise ContractError("Fresh completed reference candle unavailable")
    ref = rows[-1]
    # Additional inputs carry their own availability; never infer it from a fetch time.
    extras = {}
    for key, observation in (additional_inputs or {}).items():
        value, available = observation
        if instant(available) > now:
            raise ContractError("Additional input was unavailable at issue time")
        extras[key] = {"value": value, "available_at": instant(available).isoformat()}
    inputs = {"candles": [asdict(c) for c in rows], "additional_inputs": extras}
    available = max([instant(c.available_at) for c in rows] +
                    [instant(v["available_at"]) for v in extras.values()])
    # Pass a copy so an adapter cannot rewrite the recorded input snapshot.
    probability = number(infer(json.loads(encoded(inputs))))
    if probability > 1:
        raise ContractError("Probability must be in [0, 1]")
    payload = dict(symbol=symbol, issued_at=now.isoformat(), data_through=(ref_time + STEP).isoformat(),
                   information_available_at=available.isoformat(), reference_at=ref_time.isoformat(),
                   reference_open=str(number(ref.open, positive=True)),
                   target_open=(ref_time + 2 * STEP).isoformat(), target_close=(ref_time + 3 * STEP).isoformat(),
                   probability_not_down=str(probability), model_version=model_version, target_version=TARGET_VERSION,
                   inputs_json=encoded(inputs), input_fingerprint=digest(inputs))
    return Prediction(prediction_id=digest(payload), **payload)


def sizing_features(candles, *, symbol, issued_at, expected_volume, holdings, recent_executed):
    """Transparent causal measurements, without selecting a score mapping.

    Expected volume must be a causal baseline supplied with its availability.
    RSI uses the last 14 changes; ATR is the last 14 simple true ranges.
    Rates are per 15m step, accelerations are second differences.
    """
    rows = causal_candles(candles, symbol=symbol, issued_at=issued_at)[-23:]
    if len(rows) < 23 or any(instant(b.opened_at) - instant(a.opened_at) != STEP for a, b in zip(rows, rows[1:])):
        raise ContractError("Features require at least 23 contiguous completed candles")
    expected, available = expected_volume
    if instant(available) > instant(issued_at):
        raise ContractError("Relative-volume baseline unavailable at issue")
    baseline = float(number(expected, positive=True))
    closes = [float(number(c.close, positive=True)) for c in rows]
    volumes = [float(number(c.volume)) for c in rows]
    measurements = []
    for stop in range(len(rows) - 2, len(rows) + 1):
        cs, vs, rs = closes[:stop], volumes[:stop], rows[:stop]
        changes = [b - a for a, b in zip(cs[-15:-1], cs[-14:])]
        gain = sum(max(x, 0) for x in changes)
        loss = sum(max(-x, 0) for x in changes)
        rsi = 50.0 if gain == loss == 0 else 100.0 * gain / (gain + loss)
        tr = [max(float(c.high) - float(c.low), abs(float(c.high) - previous), abs(float(c.low) - previous))
              for c, previous in zip(rs[-14:], cs[-15:-1])]
        recent = rs[-20:]
        total_volume = sum(vs[-20:])
        if total_volume == 0:
            raise ContractError("VWAP unavailable without observed volume")
        vwap = sum((float(c.high) + float(c.low) + float(c.close)) / 3 * float(c.volume) for c in recent) / total_volume
        measurements.append({"momentum": cs[-1] / cs[-2] - 1, "rsi": rsi,
                             "distance_vwap": cs[-1] / vwap - 1,
                             "distance_sma20": cs[-1] / (sum(cs[-20:]) / 20) - 1,
                             "atr_relative_price": sum(tr) / 14 / cs[-1],
                             "relative_volume": vs[-1] / baseline})
    result = dict(measurements[-1])
    for key in measurements[-1]:
        a, b, c = [m[key] for m in measurements]
        result[key + "_rate"] = c - b
        result[key + "_acceleration"] = c - 2 * b + a
    holding_qty, holding_available = holdings
    executed_qty, executed_available = recent_executed
    if max(instant(holding_available), instant(executed_available)) > instant(issued_at):
        raise ContractError("Account sizing inputs unavailable at issue")
    result.update(holdings=quantity(holding_qty), recent_executed_quantity=quantity(executed_qty),
                  input_availability={"expected_volume": instant(available).isoformat(),
                                      "holdings": instant(holding_available).isoformat(),
                                      "recent_executed": instant(executed_available).isoformat()})
    return result


class PredictionHistory:
    """Immutable issues; append-only outcome revisions, including missing data."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY, slot TEXT UNIQUE, payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS outcomes(id TEXT PRIMARY KEY, prediction TEXT, observed TEXT, payload TEXT)")

    def save(self, prediction: Prediction):
        prediction.validate()
        payload = asdict(prediction)
        slot = encoded([prediction.symbol, prediction.reference_at, prediction.target_version])
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM predictions WHERE id=? OR slot=?", (prediction.prediction_id, slot)).fetchone()
            if existing:
                if existing[0] != encoded(payload):
                    raise ContractError("Issued quarter-hour prediction is immutable")
                return prediction
            db.execute("INSERT INTO predictions VALUES(?,?,?)", (prediction.prediction_id, slot, encoded(payload)))
        return prediction

    def get(self, prediction_id):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT payload FROM predictions WHERE id=?", (prediction_id,)).fetchone()
        if row is None:
            raise ContractError("Unknown prediction")
        return Prediction(**json.loads(row[0])).validate()

    def for_slot(self, symbol, reference_at):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT id FROM predictions WHERE slot=?",
                             (encoded([symbol, reference_at, TARGET_VERSION]),)).fetchone()
        return self.get(row[0]) if row else None

    def save_final_information(self, *, checkpoint_id, candles, observed_at, eligible_session):
        """Preserve final available inputs without inventing a next-open target."""
        now = instant(observed_at)
        local = now.astimezone(PACIFIC)
        if local.date() != eligible_session or (local.hour, local.minute) < (16, 45):
            raise ContractError("Final-information checkpoint needs the eligible session's final fetch")
        rows = []
        for candle in candles:
            candle.validate()
            if instant(candle.available_at) > now:
                raise ContractError("Final checkpoint cannot contain unavailable observations")
            rows.append(asdict(candle))
        payload = {"observed_at": now.isoformat(), "eligible_session": eligible_session.isoformat(),
                   "candles": rows, "next_open_target": None, "status": "awaiting-next-open-target-policy"}
        key = identity(checkpoint_id)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS final_information(id TEXT PRIMARY KEY, payload TEXT)")
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM final_information WHERE id=?", (key,)).fetchone()
            if existing and existing[0] != encoded(payload):
                raise ContractError("Final information checkpoint is immutable")
            db.execute("INSERT OR IGNORE INTO final_information VALUES(?,?)", (key, encoded(payload)))
        return payload

    def observe(self, prediction_id, *, as_of, candle: Candle | None, observation_id):
        prediction = self.get(prediction_id)
        now = instant(as_of)
        status = "pending" if now < instant(prediction.target_close) else "missing"
        result = {"prediction_id": prediction_id, "observed_at": now.isoformat(), "status": status,
                  "target_not_down": None, "candle": None}
        if candle is not None:
            candle.validate()
            if (candle.symbol != prediction.symbol or instant(candle.opened_at) != instant(prediction.target_open)
                    or instant(candle.available_at) > now):
                raise ContractError("Outcome requires exact genuine available target candle")
            result.update(status="completed", target_not_down=number(candle.close) >= number(prediction.reference_open),
                          candle=asdict(candle))
        key = identity(observation_id)
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM outcomes WHERE id=?", (key,)).fetchone()
            if existing and existing[0] != encoded(result):
                raise ContractError("Outcome evidence identity reused with changed bytes")
            db.execute("INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?)", (key, prediction_id, now.isoformat(), encoded(result)))
        return result

    def revisions(self, prediction_id):
        with sqlite3.connect(self.path) as db:
            return tuple(json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM outcomes WHERE prediction=? ORDER BY rowid", (prediction_id,)))
