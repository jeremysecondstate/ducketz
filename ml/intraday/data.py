"""Causal 15m inputs, exact not-down labels, and auditable acquisition evidence.

This module has no broker or provider client. Historical normalization does not
claim point-in-time availability. Omitted minutes are never filled merely because
a file contains later rows; live no-trade confirmation requires repeated covered
responses to the exact requested interval.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from .prediction import ContractError, STEP, TARGET_VERSION, UnresolvedPolicy, instant

MINUTE = timedelta(minutes=1)
FEATURE_VERSION = "intraday-observed-causal-features-v1"
SYNTHETIC_CONTINUITY_POLICY = "same-session-adjacent-prior-close-v1"
_PROVIDER_LOCKS = {}
_PROVIDER_LOCKS_GUARD = threading.Lock()


class InsufficientTrainingData(ContractError):
    """Valid observed history has too few matured samples for assessment."""


class AcquisitionPending(ContractError):
    """An exact live interval/prerequisite remains unavailable, without a cap."""
    def __init__(self, reason, retry_after_seconds=4.0):
        self.reason = str(reason)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__("Exact reference unavailable: " + self.reason)


def _recoverable_prerequisite(result):
    if result.get("status") == "missing" and result.get("reason") in {
            "no_prior_known_price", "unverified_prior_price_continuity"}:
        return {**result, "status": "retry", "data_status": "missing",
                "retry_after_seconds": max(4.0, float(result.get("retry_after_seconds", 0)))}
    return result
FEATURE_NAMES = (
    "return_1_observed", "return_4_observed", "return_20_observed",
    "body_return", "range_relative", "rsi14_observed", "distance_sma20_observed",
    "distance_vwap20_observed", "atr14_relative_observed", "relative_volume20_observed",
    "momentum_change", "volatility20_observed", "elapsed_minutes_previous",
    "elapsed_minutes20", "minute_of_session", "synthetic_input_fraction20",
)


def utc(value) -> pd.Timestamp:
    """Require timezone awareness; never guess the host timezone."""
    return pd.Timestamp(instant(value))


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def exact_target(reference_at) -> tuple[pd.Timestamp, pd.Timestamp]:
    reference = utc(reference_at)
    if reference != reference.floor("15min"):
        raise ContractError("Reference must name a quarter-hour candle open")
    return reference + 2 * STEP, reference + 3 * STEP


def next_acquisition(after, *, offset_seconds: float) -> pd.Timestamp:
    """First strictly future quarter boundary plus offset; no interval drift."""
    if not math.isfinite(offset_seconds) or not 0 <= offset_seconds < 900:
        raise ContractError("Publication offset must be finite and in [0,900) seconds")
    now = utc(after)
    offset = pd.Timedelta(seconds=offset_seconds)
    candidate = (now - offset).floor("15min") + offset
    return candidate if candidate > now else candidate + STEP


@dataclass(frozen=True)
class SessionBounds:
    session_id: str
    opened_at: str
    closed_at: str
    policy: str = "explicit-session-bounds"

    def contains(self, opened_at, duration=STEP) -> bool:
        start = utc(opened_at)
        return utc(self.opened_at) <= start and start + duration <= utc(self.closed_at)


def equity_sessions(start, end) -> tuple[SessionBounds, ...]:
    """Reuse the repository's XNAS observed-data envelope, including holidays.

    Standard sessions use 04:00--20:00 Eastern. Short sessions are conservatively
    limited to official regular hours, as in existing derived-bar logic. This is
    an ingestion envelope, not an agreed 16:45-Pacific submission policy.
    """
    import exchange_calendars as xcals
    first, last = utc(start), utc(end)
    calendar = xcals.get_calendar("XNAS", start=first.date() - timedelta(days=7),
                                 end=last.date() + timedelta(days=7))
    result = []
    for label, schedule in calendar.schedule.iterrows():
        op, cl = pd.Timestamp(schedule["open"]), pd.Timestamp(schedule["close"])
        local_op, local_cl = op.tz_convert("America/New_York"), cl.tz_convert("America/New_York")
        if (local_op.hour, local_op.minute, local_cl.hour, local_cl.minute) == (9, 30, 16, 0):
            midnight = local_op.normalize()
            op, cl = midnight + pd.Timedelta(hours=4), midnight + pd.Timedelta(hours=20)
        result.append(SessionBounds(str(pd.Timestamp(label).date()), op.isoformat(), cl.isoformat(),
                                    "xnas-standard-extended-short-session-regular-v1"))
    return tuple(result)


def _require_native_observations(rows):
    """Never erase explicit synthetic provenance while normalizing native rows."""
    from ml.market_gap_evidence import MARKERS as markers
    def valid_flag(value):
        return isinstance(value, (bool, np.bool_)) and not bool(value)
    def valid_count(value):
        return (not isinstance(value, (bool, np.bool_))
                and isinstance(value, (int, float, np.integer, np.floating))
                and math.isfinite(float(value)) and float(value) == 0)
    if isinstance(rows, pd.DataFrame):
        invalid = any(not rows[key].map(valid_flag).all() for key in markers if key in rows)
        invalid |= ("synthetic_constituents" in rows
                    and not rows.synthetic_constituents.map(valid_count).all())
    else:
        invalid = any(any(key in row and not valid_flag(row[key]) for key in markers)
            or ("synthetic_constituents" in row and not valid_count(row["synthetic_constituents"]))
            for row in rows)
    if invalid:
        raise ContractError("Native source observations cannot contain synthetic or no-trade markers")


@dataclass(frozen=True)
class IntervalResponse:
    """One provider attempt. Empty rows alone do not establish no trades."""
    attempt_id: str
    symbol: str
    source_id: str
    source_dataset: str
    interval_open: str
    interval_close: str
    request_started_at: str
    request_ended_at: str
    status: str  # success, temporary_error, unavailable_coverage, permanent_error
    rows: tuple[dict, ...] = ()
    coverage_open: str | None = None
    coverage_close: str | None = None
    provider_no_trade_evidence: str | None = None
    retry_after_seconds: float = 0.0
    error: str | None = None
    native_response: dict | None = None
    no_trade_scope_evidence: dict | None = None
    failure_diagnostics: dict | None = None

    def validate(self):
        if not self.attempt_id or not self.symbol or not self.source_id or not self.source_dataset:
            raise ContractError("Acquisition identities must be explicit")
        if self.status not in {"success", "temporary_error", "unavailable_coverage", "permanent_error"}:
            raise ContractError("Unknown acquisition response status")
        op, cl = utc(self.interval_open), utc(self.interval_close)
        begin, finish = utc(self.request_started_at), utc(self.request_ended_at)
        if cl <= op or finish < begin or finish < cl:
            raise ContractError("Exact completed interval and ordered request times required")
        if not math.isfinite(self.retry_after_seconds) or self.retry_after_seconds < 0:
            raise ContractError("Invalid provider waiting interval")
        if self.rows and self.status != "success":
            raise ContractError("Failed response cannot supply usable observations")
        _require_native_observations(self.rows)
        return self

    def covered(self) -> bool:
        return (self.coverage_open is not None and self.coverage_close is not None
                and utc(self.coverage_open) <= utc(self.interval_open)
                and utc(self.coverage_close) >= utc(self.interval_close))


class AcquisitionJournal:
    """Append-only exact attempts and durable retry state on the local PC."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, slot TEXT, payload TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS attempts_slot ON attempts(slot)")
            db.execute("CREATE TABLE IF NOT EXISTS attempt_provenance(attempt_id TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, receipt_path TEXT NOT NULL, PRIMARY KEY(attempt_id,receipt_sha256))")
            existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_waits'").fetchone()
            db.execute("CREATE TABLE IF NOT EXISTS provider_waits(provider TEXT PRIMARY KEY,due_at TEXT NOT NULL)")
            if not existed:
                # Existing durable attempts predate provider-wide retry state.
                for (raw,) in db.execute("SELECT payload FROM attempts").fetchall():
                    self._provider_wait(db, IntervalResponse(**json.loads(raw)).validate())

    @staticmethod
    def slot(symbol, source_id, interval_open, interval_close):
        return _json([symbol, source_id, utc(interval_open).isoformat(), utc(interval_close).isoformat()])

    def save(self, response: IntervalResponse):
        response.validate()
        record = asdict(response)
        # Old immutable attempts predate diagnostics. Reading and saving one
        # again must not add an absent optional field to its original bytes.
        if record["failure_diagnostics"] is None:
            record.pop("failure_diagnostics")
        payload = _json(record)
        slot = self.slot(response.symbol, response.source_id, response.interval_open, response.interval_close)
        # Sidecar identity retains the actual loaded acquisition implementation;
        # old journal rows are never upgraded into invented historical evidence.
        from ml.market_provenance import capture_runtime, preserve_bytes
        runtime = capture_runtime([__name__, "app.services.databento_market_data",
            "app.services.databento_lifecycle", "app.services.databento_retry"],
            object_root=self.path.parent / "acquisition-source-objects")
        receipt = preserve_bytes(self.path.parent / "acquisition-receipts", _json({
            "attempt_id": response.attempt_id, "response_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "response": record, "producer_runtime": runtime,
            "native_response_scope": "SDK-returned native DataFrame before minute projection; not raw HTTP/DBN wire bytes"
        }).encode(), suffix=".json")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM attempts WHERE id=?", (response.attempt_id,)).fetchone()
            if existing and existing[0] != payload:
                raise ContractError("Attempt identity is immutable")
            db.execute("INSERT OR IGNORE INTO attempts VALUES(?,?,?)", (response.attempt_id, slot, payload))
            self._provider_wait(db, response)
            db.execute("INSERT OR IGNORE INTO attempt_provenance VALUES(?,?,?)",
                (response.attempt_id, receipt["sha256"], receipt["path"]))

    @staticmethod
    def _provider_wait(db, response):
        if response.source_id.startswith("databento:") and (response.retry_after_seconds > 0 or response.status == "temporary_error"):
            due = (utc(response.request_ended_at) + pd.Timedelta(seconds=max(4.0,response.retry_after_seconds))).isoformat()
            db.execute("INSERT INTO provider_waits VALUES('databento',?) ON CONFLICT(provider) "
                       "DO UPDATE SET due_at=max(due_at,excluded.due_at)", (due,))

    def provider_retry_due(self):
        """Latest Databento Retry-After, independent of symbol and interval."""
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT due_at FROM provider_waits WHERE provider='databento'").fetchone()
        return utc(row[0]) if row else None

    def responses(self, *, symbol, source_id, interval_open, interval_close):
        slot = self.slot(symbol, source_id, interval_open, interval_close)
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = db.execute("SELECT payload FROM attempts WHERE slot=? ORDER BY rowid", (slot,)).fetchall()
        return tuple(IntervalResponse(**json.loads(row[0])) for row in rows)


def resolve_interval(responses: Sequence[IntervalResponse], *, session: SessionBounds,
                     previous: Mapping | None, duration=STEP) -> dict:
    """Resolve observed/no-trade/missing separately, with exact retry evidence.

    At least two distinct successful covered empty attempts must be separated
    by a positive wait. Both must assert explicit provider no-trade evidence.
    Failures and advertised-range lag never contribute to that confirmation.
    """
    if not responses:
        return {"status": "missing", "reason": "no_attempts", "candle": None}
    rows = [response.validate() for response in responses]
    last = rows[-1]
    identities = {(r.symbol, r.source_id, r.source_dataset, utc(r.interval_open), utc(r.interval_close)) for r in rows}
    if len(identities) != 1 or utc(last.interval_close) - utc(last.interval_open) != duration:
        raise ContractError("Attempts must retry the identical interval, symbol, and source")
    if not session.contains(last.interval_open, duration):
        return {"status": "closed", "reason": "outside_eligible_session", "candle": None}
    evidence = [asdict(r) for r in rows]
    if last.status == "permanent_error":
        return {"status": "failed", "reason": "permanent_error", "candle": None, "evidence": evidence}
    if last.status == "success" and last.covered() and last.rows:
        frame = pd.DataFrame(last.rows)
        timestamp_col = "opened_at" if "opened_at" in frame else "timestamp"
        if timestamp_col not in frame:
            raise ContractError("Provider observations lack opening timestamps")
        frame["opened_at"] = pd.to_datetime(frame[timestamp_col], utc=True)
        op, cl = utc(last.interval_open), utc(last.interval_close)
        if (frame.opened_at.lt(op).any() or frame.opened_at.ge(cl).any()
                or frame.opened_at.duplicated().any()):
            raise ContractError("Provider rows escape or conflict within the exact interval")
        # A single requested candle requires one exact row; constituent minutes
        # are resolved separately, then aggregated by aggregate_minutes.
        if len(frame) != 1 or frame.opened_at.iloc[0] != op:
            return {"status": "missing", "reason": "constituents_require_validation", "candle": None,
                    "evidence": evidence}
        native = frame.iloc[0]
        candle = _candle_record(last, session, {field: native[field] for field in ("open", "high", "low", "close", "volume")},
                                synthetic=False, evidence=evidence)
        validate_candles(pd.DataFrame([candle]), duration=duration)
        return {"status": "observed", "candle": candle, "evidence": evidence}
    from .coverage import response_no_trade_verified
    if last.status == "success" and last.covered() and not last.rows and not response_no_trade_verified(last):
        prior_covered = [response for response in rows[:-1] if response.status == "success" and response.covered()]
        previous_response = prior_covered[-1] if prior_covered else None
        stable_empty = (previous_response is not None and not previous_response.rows
            and previous_response.attempt_id != last.attempt_id
            and (utc(last.request_started_at)-utc(previous_response.request_ended_at)).total_seconds()
                >= max(4.0,previous_response.retry_after_seconds))
        if not stable_empty:
            return {"status":"retry", "reason":"source_observations_unstable", "candle":None,
                    "retry_after_seconds":max(4.0,last.retry_after_seconds), "evidence":evidence}
        return {"status": "unavailable", "reason": "source_empty", "final": True,
                "candle": None, "evidence": evidence}
    qualifying = []
    for response in rows:
        if (response.status == "success" and response.covered() and not response.rows
                and response.provider_no_trade_evidence and response_no_trade_verified(response)):
            if not qualifying or (response.attempt_id != qualifying[-1].attempt_id
                                  and (utc(response.request_started_at) - utc(qualifying[-1].request_ended_at)).total_seconds()
                                  >= max(4.0, qualifying[-1].retry_after_seconds)):
                qualifying.append(response)
    if last in qualifying and len(qualifying) >= 2:
        if previous is None:
            return {"status": "missing", "reason": "no_prior_known_price", "candle": None, "evidence": evidence}
        if (previous.get("symbol") != last.symbol or previous.get("source_id") != last.source_id
                or previous.get("source_dataset") != last.source_dataset):
            raise ContractError("Prior known close must belong to the same symbol and source")
        if utc(previous["available_at"]) > utc(last.request_ended_at):
            raise ContractError("Prior close unavailable when no-trade interval was confirmed")
        proof = _prior_price_continuity(previous, current_open=last.interval_open,
            current_available=last.request_ended_at, session=session, prior_duration=duration,
            current_duration=duration)
        if proof is None:
            return {"status": "missing", "reason": "unverified_prior_price_continuity", "candle": None,
                    "evidence": evidence}
        close = float(previous["close"])
        candle = _candle_record(last, session, dict(open=close, high=close, low=close, close=close, volume=0.0),
                                synthetic=True, evidence=evidence)
        candle["prior_price_opened_at"] = utc(previous["opened_at"]).isoformat()
        candle["prior_price_close"] = close
        candle["synthetic_continuity"] = proof
        candle["no_trade_scope_evidence"] = dict(last.no_trade_scope_evidence)
        validate_candles(pd.DataFrame([candle]), duration=duration)
        return {"status": "synthetic_no_trade", "candle": candle, "evidence": evidence}
    return {"status": "retry", "reason": last.status if last.status != "success" else "publication_lag_or_unconfirmed_gap",
            "candle": None, "evidence": evidence,
            "retry_after_seconds": max(4.0, last.retry_after_seconds)}


def _candle_record(response, session, values, *, synthetic, evidence, duration=None):
    duration = utc(response.interval_close) - utc(response.interval_open) if duration is None else duration
    return dict(symbol=response.symbol, opened_at=utc(response.interval_open).isoformat(),
                available_at=utc(response.request_ended_at).isoformat(), availability_kind="actual",
                source_id=response.source_id, source_dataset=response.source_dataset,
                session_id=session.session_id, session_open=session.opened_at, session_close=session.closed_at,
                interval_seconds=duration.total_seconds(),
                synthetic=synthetic, no_trade=synthetic, evidence_json=_json(evidence),
                **{key: float(value) for key, value in values.items()})


def _prior_price_continuity(previous, *, current_open, current_available, session,
                            prior_duration, current_duration):
    """No omitted interval bridges an unobserved gap or a closed session.

    A preceding 15m candle may seed the first missing minute. Thereafter the
    immediately preceding resolved minute seeds the next. A prior synthetic
    price must carry the same verified policy rather than propagate old evidence
    that did not establish continuity.
    """
    try:
        prior_open, current_open = utc(previous["opened_at"]), utc(current_open)
        recorded_seconds = previous.get("interval_seconds")
        if recorded_seconds is not None and not pd.isna(recorded_seconds) and recorded_seconds != prior_duration.total_seconds():
            return None
        if (str(previous.get("session_id")) != session.session_id
                or not session.contains(prior_open, prior_duration)
                or prior_open + prior_duration != current_open
                or utc(previous["available_at"]) > utc(current_available)
                or not synthetic_continuity_verified(previous)):
            return None
        close = float(previous["close"])
        if not math.isfinite(close) or close <= 0:
            return None
        return {"policy": SYNTHETIC_CONTINUITY_POLICY,
                "current_opened_at": current_open.isoformat(),
                "current_interval_seconds": current_duration.total_seconds(),
                "prior_opened_at": prior_open.isoformat(), "prior_close": close,
                "prior_interval_seconds": prior_duration.total_seconds(),
                "prior_available_at": utc(previous["available_at"]).isoformat(),
                "prior_session_id": session.session_id, "prior_symbol": previous["symbol"],
                "prior_source_id": previous["source_id"]}
    except (KeyError, ValueError, TypeError, OverflowError, ContractError):
        return None


def _verified_continuity_proof(candle, proof, *, duration):
    if not isinstance(proof, Mapping) or proof.get("policy") != SYNTHETIC_CONTINUITY_POLICY:
        return False
    session = SessionBounds(str(candle["session_id"]), candle["session_open"], candle["session_close"])
    seconds = proof.get("prior_interval_seconds")
    if isinstance(seconds, bool) or seconds not in {60, 900}:
        return False
    prior_duration = timedelta(seconds=seconds)
    prior_open, current_open = utc(proof["prior_opened_at"]), utc(candle["opened_at"])
    return (utc(proof["current_opened_at"]) == current_open
            and proof.get("current_interval_seconds") == duration.total_seconds()
            and prior_open + prior_duration == current_open
            and session.contains(prior_open, prior_duration)
            and session.contains(current_open, duration)
            and str(proof.get("prior_session_id")) == str(candle["session_id"])
            and proof.get("prior_symbol") == candle["symbol"]
            and proof.get("prior_source_id") == candle["source_id"]
            and utc(candle["prior_price_opened_at"]) == prior_open
            and float(candle["prior_price_close"]) == float(proof["prior_close"])
            and prior_open + prior_duration <= utc(proof["prior_available_at"]) <= utc(candle["available_at"])
            and math.isfinite(float(proof["prior_close"])) and float(proof["prior_close"]) > 0
            and all(float(candle[key]) == float(proof["prior_close"]) for key in ("open", "high", "low", "close"))
            and float(candle["volume"]) == 0)


def synthetic_continuity_verified(candle: Mapping) -> bool:
    """Whether a persisted candle can safely seed future live synthetic prices.

    Observed candles need no synthetic proof. Legacy synthetic records remain
    readable for audit but return False: their immutable forecasts/outcomes are
    never rewritten. Live restoration, training admission and scoring must
    explicitly exclude those records until separately audited or observed anew.
    This structural check consumes persisted evidence, without provider calls.
    """
    try:
        synthetic = candle.get("synthetic")
        from .coverage import RECOVERY_POPULATION_CONTRACT
        if candle.get("population_contract") == RECOVERY_POPULATION_CONTRACT:
            from .recovered_population import verified_recovery_candle
            return verified_recovery_candle(candle)
        if not isinstance(synthetic, (bool, np.bool_)):
            return False
        if not synthetic:
            return True
        from .coverage import independently_verified_no_trade
        def scope_verified(value, duration):
            return independently_verified_no_trade(value.get("no_trade_scope_evidence"),
                symbol=candle["symbol"], source_id=candle["source_id"], source_dataset=candle["source_dataset"],
                opened_at=value["opened_at"], closed_at=utc(value["opened_at"])+duration,
                available_at=value["available_at"])
        evidence = json.loads(candle.get("evidence_json", "{}"))
        if isinstance(evidence, dict) and "constituents" in evidence:
            constituents = evidence["constituents"]
            if len(constituents) != 15 or evidence.get("synthetic_continuity_policy") != SYNTHETIC_CONTINUITY_POLICY:
                return False
            found = 0
            for index, constituent in enumerate(constituents):
                if (utc(constituent["opened_at"]) != utc(candle["opened_at"]) + index * MINUTE
                        or constituent.get("source_id") != candle["source_id"]
                        or utc(constituent["available_at"]) > utc(candle["available_at"])
                        or not isinstance(constituent.get("synthetic"), bool)):
                    return False
                if constituent.get("synthetic") is not True:
                    continue
                found += 1
                proof = constituent.get("synthetic_continuity")
                admitted = {**candle, **constituent}
                if not scope_verified(admitted, MINUTE):
                    return False
                if not _verified_continuity_proof(admitted, proof, duration=MINUTE):
                    return False
                if any(constituent.get(field) != proof.get(key) for field, key in (
                        ("prior_price_opened_at", "prior_opened_at"), ("prior_price_close", "prior_close"),
                        ("prior_price_interval_seconds", "prior_interval_seconds"),
                        ("prior_price_session_id", "prior_session_id"), ("prior_price_available_at", "prior_available_at"))):
                    return False
            return found > 0 and found == candle.get("synthetic_constituents")
        duration = timedelta(seconds=candle.get("interval_seconds", 900))
        return (scope_verified(candle, duration)
                and _verified_continuity_proof(candle, candle.get("synthetic_continuity"), duration=duration))
    except (KeyError, ValueError, TypeError, OverflowError, ContractError, json.JSONDecodeError):
        return False


def completed_15m_observation(candle: Mapping, *, from_frame=False) -> bool:
    """Require evidence for the selected completed-quarter definition.

    The source-observation contract accepts covered nonempty sparse aggregates.
    Bare native rows and undocumented partial aggregates do not qualify.
    Legacy explicit 900-second bars remain valid. Only DataFrame rows
    may treat null optional fields introduced by concatenation as absent proof;
    raw durable mappings retain strict validation of explicitly present fields.
    Synthetic price continuity remains a separate check at each caller.
    """
    def missing(value):
        return value is None or (isinstance(value, (float, np.floating))
                                 and math.isnan(float(value)))

    if not isinstance(candle, Mapping):
        return False
    from .coverage import POPULATION_CONTRACT, RECOVERY_POPULATION_CONTRACT, verified_source_observation
    if candle.get("population_contract") == RECOVERY_POPULATION_CONTRACT:
        from .recovered_population import verified_recovery_candle
        return verified_recovery_candle(candle)
    if isinstance(candle.get("population_contract"), str) and candle["population_contract"] == POPULATION_CONTRACT:
        return verified_source_observation(candle)
    try:
        opened, available = utc(candle["opened_at"]), utc(candle["available_at"])
        if pd.isna(opened) or pd.isna(available) or opened != opened.floor("15min") or available < opened + STEP:
            return False
        proof = False
        seconds = candle.get("interval_seconds")
        if "interval_seconds" in candle and not (from_frame and missing(seconds)):
            if seconds != 900:
                return False
            proof = True
        counts = [candle.get(field) for field in ("observed_constituents", "synthetic_constituents")]
        if (any(field in candle for field in ("observed_constituents", "synthetic_constituents"))
                and not (from_frame and all(missing(value) for value in counts))):
            if any(isinstance(value, (bool, np.bool_))
                   or not isinstance(value, (int, float, np.integer, np.floating))
                   or not math.isfinite(float(value)) or not float(value).is_integer()
                   or not 0 <= value <= 15 for value in counts) or sum(counts) != 15:
                return False
            proof = True
        raw_evidence = candle.get("evidence_json")
        evidence = {} if missing(raw_evidence) else (
            raw_evidence if isinstance(raw_evidence, Mapping) else json.loads(raw_evidence))
        if isinstance(evidence, Mapping) and "constituents" in evidence:
            constituents = evidence["constituents"]
            if not isinstance(constituents, list) or len(constituents) != 15:
                return False
            if any(not isinstance(row, Mapping) or utc(row["opened_at"]) != opened + index * MINUTE
                   for index, row in enumerate(constituents)):
                return False
            proof = True
        return proof
    except (KeyError, ValueError, TypeError, OverflowError, ContractError, json.JSONDecodeError):
        return False


def _wait_for_persisted_retry(journal: AcquisitionJournal, *, slot: Mapping,
                              sleep: Callable[[float], None], clock: Callable):
    """Honor the last exact-slot waiting rule even after a process restart."""
    attempts = journal.responses(**slot)
    if not attempts:
        return
    latest = attempts[-1].validate()
    due = utc(latest.request_ended_at) + pd.Timedelta(seconds=max(4.0, latest.retry_after_seconds))
    while True:
        remaining = (due - utc(clock())).total_seconds()
        if remaining <= 0:
            return
        # Bounded sleeps keep cancellation responsive. The caller's clock must
        # advance with its sleeper, including when both are injected for tests.
        sleep(min(4.0, remaining))


def recover_interval(fetch: Callable[[], IntervalResponse], *, journal: AcquisitionJournal,
                     session: SessionBounds, previous: Mapping | None, slot: Mapping,
                     sleep: Callable[[float], None], duration=STEP,
                     clock: Callable = lambda: datetime.now(timezone.utc)) -> dict:
    """Exact-slot durable retries with no count ceiling, including restart waits.

    ``slot`` explicitly identifies symbol, source_id, interval_open and
    interval_close before the first request. The injected fetch must preserve
    that identity; without it a generic callback cannot honor stored Retry-After
    before issuing a request. Interruption leaves all evidence durable.
    """
    if set(slot) != {"symbol", "source_id", "interval_open", "interval_close"}:
        raise ContractError("Generic recovery requires an explicit exact interval identity")
    while True:
        _wait_for_persisted_retry(journal, slot=slot, sleep=sleep, clock=clock)
        response = fetch()
        actual_slot = dict(symbol=response.symbol, source_id=response.source_id,
                           interval_open=response.interval_open, interval_close=response.interval_close)
        if journal.slot(**actual_slot) != journal.slot(**slot):
            raise ContractError("Generic recovery callback changed its exact interval identity")
        journal.save(response)
        result = resolve_interval(journal.responses(symbol=response.symbol, source_id=response.source_id,
                                  interval_open=response.interval_open, interval_close=response.interval_close),
                                  session=session, previous=previous, duration=duration)
        if result["status"] != "retry":
            return result
        sleep(result["retry_after_seconds"])


def _acquisition_failure_diagnostics(exc, *, stage, provider):
    """Bounded exception messages only; never copy response bodies or headers."""
    import re
    from app.services.databento_retry import _http_status
    from .provider_recovery import sanitize_cause
    try:
        key = getattr(provider, "api_key", None)
        secrets = (key,) if isinstance(key, str) and key else ()
        chain, seen, current = [], set(), exc
        while isinstance(current, Exception) and id(current) not in seen and len(chain) < 4:
            seen.add(id(current))
            # Basic credentials are not covered by the shared Bearer sanitizer.
            message = re.sub(r"(?i)\b(?:basic|bearer)\s+[^\s,;}\"']+",
                             "[AUTH REDACTED]", str(current))
            chain.append({"error_type": type(current).__name__[:120],
                          "http_status": _http_status(current),
                          "cause": sanitize_cause(message, secrets)})
            current = current.__cause__ or current.__context__
        return {"stage": stage, "chain": chain, "chain_truncated": isinstance(current, Exception)}
    except Exception as diagnostic_error:
        # Diagnostics must not change the original classification or retry.
        return {"stage": stage, "diagnostics_error_type": type(diagnostic_error).__name__[:120]}


def fetch_databento_interval_attempt(provider, *, symbol: str, opened_at, attempt_id: str,
                                    clock: Callable = lambda: datetime.now(timezone.utc),
                                    stopped: Callable = lambda: False) -> IntervalResponse:
    """Read the exact 15m native-minute range through an existing provider.

    Caller supplies its already-authorized configured provider; construction,
    credentials, source selection, spending authorization, and invocation are
    outside this function. Only metadata and historical read methods are used.
    A schema end bound is checked before requesting prices. No old recovery
    deadline is imported and failures retain their concrete status/evidence.
    """
    op = utc(opened_at)
    if op != op.floor("15min"):
        raise ContractError("Expected quarter-hour reference opening")
    cl = op + STEP
    begin = utc(clock())
    if begin < cl:
        raise ContractError("Cannot acquire an unfinished reference candle")
    if stopped():
        raise InterruptedError("Candle acquisition stopped before request")
    dataset = str(getattr(provider, "dataset", "")).strip()
    if not dataset:
        raise ContractError("Existing provider must declare its dataset")
    source_id = f"databento:{dataset}:ohlcv-1m"
    coverage = None
    stage = "native_specs"
    try:
        if stopped():
            raise InterruptedError("Candle acquisition stopped before metadata request")
        specs = [spec for spec in provider.native_specs() if spec.schema == "ohlcv-1m" and spec.frequency == "1m"]
        if len(specs) != 1:
            raise ContractError("One native ohlcv-1m provider spec is required")
        if stopped():
            raise InterruptedError("Candle acquisition stopped before dataset range request")
        stage = "dataset_range"
        dataset_range = provider.dataset_range()
        if stopped():
            raise InterruptedError("Candle acquisition stopped before schema range request")
        stage = "schema_range"
        advertised = provider.available_range_for_schema("ohlcv-1m", dataset_range=dataset_range)
        coverage = (utc(advertised.start).isoformat(), utc(advertised.end).isoformat())
        if utc(advertised.start) > op or utc(advertised.end) < cl:
            return IntervalResponse(attempt_id, symbol, source_id, dataset, op.isoformat(), cl.isoformat(),
                begin.isoformat(), utc(clock()).isoformat(), "unavailable_coverage", coverage_open=coverage[0],
                coverage_close=coverage[1], error="requested_interval_outside_advertised_schema_range")
        if stopped():
            raise InterruptedError("Candle acquisition stopped before price request")
        stage = "native_bars"
        results, selected = provider.fetch_native_bars_range([symbol], specs[0], start=op.to_pydatetime(),
                                                            end=cl.to_pydatetime(), available_range=advertised)
        if symbol not in results or utc(selected.start) != op or utc(selected.end) != cl:
            raise ContractError("Provider did not preserve exact requested interval or symbol")
        bars, _raw = results[symbol]
        if _raw is not None:
            _require_native_observations(_raw)
        if _raw is not None and len(_raw) != len(bars):
            raise ContractError("Provider raw rows were omitted during price parsing; invalid data is not no-trade evidence")
        native = tuple(dict(opened_at=utc(bar.timestamp).isoformat(), open=bar.open, high=bar.high,
                            low=bar.low, close=bar.close, volume=bar.volume) for bar in bars)
        return IntervalResponse(attempt_id, symbol, source_id, dataset, op.isoformat(), cl.isoformat(),
            begin.isoformat(), utc(clock()).isoformat(), "success", native, coverage[0], coverage[1],
            None, native_response=_native_response_snapshot(_raw))
    except (ContractError, InterruptedError):
        raise
    except Exception as exc:
        from app.services.databento_retry import is_retryable_databento_error, _retry_after_seconds, _http_status
        retryable = is_retryable_databento_error(exc) or _http_status(exc) == 429
        return IntervalResponse(attempt_id, symbol, source_id, dataset, op.isoformat(), cl.isoformat(),
            begin.isoformat(), utc(clock()).isoformat(), "temporary_error" if retryable else "permanent_error",
            coverage_open=coverage[0] if coverage else None, coverage_close=coverage[1] if coverage else None,
            retry_after_seconds=_retry_after_seconds(exc), error=type(exc).__name__,
            failure_diagnostics=_acquisition_failure_diagnostics(exc, stage=stage, provider=provider))


def fetch_databento_interval_attempts(provider, *, symbols: Sequence[str], opened_at, attempt_id: str,
                                     clock: Callable = lambda: datetime.now(timezone.utc),
                                     stopped: Callable = lambda: False) -> dict[str, IntervalResponse]:
    """One exact native-minute request for all symbols; independent evidence.

    Price reads use the existing provider's batching implementation. No newly
    subscribed data or alternate dataset is requested. This reduces eleven
    per-symbol price requests to one request for the same interval/symbol set.
    """
    if stopped():
        raise InterruptedError("Candle acquisition stopped before batch request")
    clean = tuple(str(s).strip().upper() for s in symbols)
    if not clean or len(set(clean)) != len(clean):
        raise ContractError("A nonempty unique symbol set is required")
    op = utc(opened_at)
    if op != op.floor("15min"):
        raise ContractError("Expected quarter-hour reference opening")
    cl, begin = op + STEP, utc(clock())
    if begin < cl:
        raise ContractError("Cannot acquire an unfinished reference candle")
    dataset = str(getattr(provider, "dataset", "")).strip()
    if not dataset:
        raise ContractError("Existing provider must declare its dataset")
    source_id, coverage = f"databento:{dataset}:ohlcv-1m", None
    status, error, wait, results = "success", None, 0.0, {}
    stage, diagnostics = "native_specs", None
    try:
        if stopped():
            raise InterruptedError("Candle acquisition stopped before metadata request")
        specs = [spec for spec in provider.native_specs() if spec.schema == "ohlcv-1m" and spec.frequency == "1m"]
        if len(specs) != 1:
            raise ContractError("One native ohlcv-1m provider spec is required")
        if stopped():
            raise InterruptedError("Candle acquisition stopped before dataset range request")
        stage = "dataset_range"
        dataset_range = provider.dataset_range()
        if stopped():
            raise InterruptedError("Candle acquisition stopped before schema range request")
        stage = "schema_range"
        advertised = provider.available_range_for_schema("ohlcv-1m", dataset_range=dataset_range)
        coverage = (utc(advertised.start).isoformat(), utc(advertised.end).isoformat())
        if utc(advertised.start) > op or utc(advertised.end) < cl:
            status, error = "unavailable_coverage", "requested_interval_outside_advertised_schema_range"
        else:
            if stopped():
                raise InterruptedError("Candle acquisition stopped before price request")
            stage = "native_bars"
            results, selected = provider.fetch_native_bars_range(clean, specs[0], start=op.to_pydatetime(),
                end=cl.to_pydatetime(), available_range=advertised)
            if set(results) != set(clean) or utc(selected.start) != op or utc(selected.end) != cl:
                raise ContractError("Provider did not preserve exact requested interval or symbol set")
    except (ContractError, InterruptedError):
        raise
    except Exception as exc:
        from app.services.databento_retry import is_retryable_databento_error, _retry_after_seconds, _http_status
        status = "temporary_error" if is_retryable_databento_error(exc) or _http_status(exc) == 429 else "permanent_error"
        wait, error = _retry_after_seconds(exc), type(exc).__name__
        diagnostics = _acquisition_failure_diagnostics(exc, stage=stage, provider=provider)
    finished = utc(clock())
    responses = {}
    for symbol in clean:
        bars = results[symbol][0] if status == "success" else ()
        if status == "success" and results[symbol][1] is not None:
            _require_native_observations(results[symbol][1])
        if status == "success" and results[symbol][1] is not None and len(results[symbol][1]) != len(bars):
            raise ContractError("Provider raw rows were omitted during price parsing; invalid data is not no-trade evidence")
        native = tuple(dict(opened_at=utc(bar.timestamp).isoformat(), open=bar.open, high=bar.high,
                            low=bar.low, close=bar.close, volume=bar.volume) for bar in bars)
        responses[symbol] = IntervalResponse(f"{attempt_id}:{symbol}", symbol, source_id, dataset,
            op.isoformat(), cl.isoformat(), begin.isoformat(), finished.isoformat(), status, native,
            coverage[0] if coverage else None, coverage[1] if coverage else None,
            None, wait, error, native_response=_native_response_snapshot(results[symbol][1]) if status == "success" else None,
            failure_diagnostics=diagnostics).validate()
    return responses


def _native_response_snapshot(frame):
    """Retain original SDK-native identities/fields before OHLCV projection."""
    if frame is None:
        return None
    return json.loads(frame.to_json(orient="table", date_format="iso", date_unit="ns", double_precision=15))


def resolve_minute_candle(responses: Sequence[IntervalResponse], *, session: SessionBounds,
                          previous: Mapping | None, population_contract=None) -> dict:
    """Validate one exact 15m response; retry that range for omitted minutes.

    All native rows are 1m. The source-observation contract admits stable covered
    sparse payloads without filling missing minutes; stable emptiness remains
    unavailable. Legacy carry requires independent complete-market no-trade
    evidence plus separated confirmations and an adjacent known close. No
    previous whole OHLC range is copied. Observed data in the final response is
    the snapshot; older response rows never silently supplement missing rows.
    """
    from .coverage import (POPULATION_CONTRACT, check_population_contract,
                           aggregate_source_minutes, response_no_trade_verified)
    check_population_contract(population_contract)
    if not responses:
        return {"status": "missing", "reason": "no_attempts", "candle": None}
    attempts = [response.validate() for response in responses]
    final = attempts[-1]
    if len({(r.symbol, r.source_id, r.source_dataset, utc(r.interval_open), utc(r.interval_close)) for r in attempts}) != 1:
        raise ContractError("Minute responses must preserve the identical cycle interval/source")
    if utc(final.interval_close) - utc(final.interval_open) != STEP:
        raise ContractError("Minute acquisition must request exactly one 15m interval")
    if not session.contains(final.interval_open):
        return {"status": "closed", "reason": "outside_eligible_session", "candle": None}
    if final.status == "permanent_error":
        return {"status": "failed", "reason": "permanent_error", "candle": None,
                "evidence": [asdict(item) for item in attempts]}
    if final.status != "success" or not final.covered():
        return {"status": "retry", "reason": final.status, "candle": None,
                "retry_after_seconds": max(4.0, final.retry_after_seconds)}
    op, cl = utc(final.interval_open), utc(final.interval_close)
    lookup_by_attempt = []
    for attempt in attempts:
        lookup = {}
        for record in attempt.rows:
            timestamp = utc(record.get("opened_at", record.get("timestamp")))
            if timestamp != timestamp.floor("1min") or timestamp < op or timestamp + MINUTE > cl or timestamp in lookup:
                raise ContractError("Invalid or duplicate minute in exact provider range")
            lookup[timestamp] = record
        lookup_by_attempt.append(lookup)
    native = lookup_by_attempt[-1]
    if not native and population_contract is None and not response_no_trade_verified(final):
        return resolve_interval(attempts,session=session,previous=previous)
    if population_contract == POPULATION_CONTRACT:
        if len(native) < 15:
            # Covered range publication can precede the settled sparse payload.
            # Stability is evidence about this selected source, not all markets.
            def canonical_rows(lookup):
                return _json([{"opened_at": stamp.isoformat(), **{field: float(row[field])
                    for field in ("open", "high", "low", "close", "volume")}}
                    for stamp, row in sorted(lookup.items())])
            final_rows = canonical_rows(native)
            prior_covered = [(attempt, lookup) for attempt, lookup in zip(attempts[:-1], lookup_by_attempt[:-1])
                             if attempt.status == "success" and attempt.covered()]
            stable = False
            if prior_covered:
                attempt, lookup = prior_covered[-1]
                stable = (attempt.attempt_id != final.attempt_id
                    and (utc(final.request_started_at)-utc(attempt.request_ended_at)).total_seconds() >= max(4.0, attempt.retry_after_seconds)
                    and canonical_rows(lookup) == final_rows)
            if not stable:
                return {"status": "retry", "reason": "source_observations_unstable", "candle": None,
                        "retry_after_seconds": max(4.0, final.retry_after_seconds),
                        "population_contract": population_contract, "evidence": [asdict(item) for item in attempts]}
        if not native:
            return {"status": "unavailable", "reason": "source_empty", "final": True,
                    "candle": None, "population_contract": population_contract,
                    "evidence": [asdict(item) for item in attempts]}
        observed = []
        for timestamp, row in sorted(native.items()):
            record = _candle_record(final, session,
                {field: row[field] for field in ("open", "high", "low", "close", "volume")},
                synthetic=False, evidence=[asdict(final)], duration=MINUTE)
            record["opened_at"] = timestamp.isoformat()
            observed.append(record)
        coverage = [{"opened_at": final.interval_open, "closed_at": final.interval_close,
                     "basis": "successful_exact_source_request", "attempt_id": final.attempt_id,
                     "request_ended_at": final.request_ended_at}]
        frame, report = aggregate_source_minutes(pd.DataFrame(observed), sessions=[session], source_coverage=coverage)
        candle = frame.iloc[0].to_dict()
        for key in ("opened_at", "available_at"):
            candle[key] = candle[key].isoformat()
        return {"status": "observed" if len(native) == 15 else "sparse_observed",
                "candle": candle, "constituents": observed, "aggregation": report,
                "population_contract": population_contract, "evidence": [asdict(item) for item in attempts]}
    observed, prior = [], dict(previous) if previous is not None else None
    for timestamp in pd.date_range(op, periods=15, freq="1min"):
        if timestamp in native:
            values = {field: native[timestamp][field] for field in ("open", "high", "low", "close", "volume")}
            synthetic = False
        else:
            if not response_no_trade_verified(final, opened_at=timestamp, closed_at=timestamp+MINUTE):
                return {"status": "unavailable", "reason": "source_empty" if not native else "unverified_source_omission",
                        "final": True, "candle": None, "missing_minute": timestamp.isoformat(),
                        "evidence": [asdict(item) for item in attempts]}
            confirmations = []
            for attempt, lookup in zip(attempts, lookup_by_attempt):
                if (attempt.status == "success" and attempt.covered() and attempt.provider_no_trade_evidence
                        and response_no_trade_verified(attempt, opened_at=timestamp, closed_at=timestamp+MINUTE)
                        and timestamp not in lookup):
                    if not confirmations or (attempt.attempt_id != confirmations[-1].attempt_id
                            and (utc(attempt.request_started_at)-utc(confirmations[-1].request_ended_at)).total_seconds()
                            >= max(4.0, confirmations[-1].retry_after_seconds)):
                        confirmations.append(attempt)
            if len(confirmations) < 2 or confirmations[-1].attempt_id != final.attempt_id:
                return {"status": "retry", "reason": "unconfirmed_no_trade_constituent", "candle": None,
                        "missing_minute": timestamp.isoformat(), "retry_after_seconds": max(4.0, final.retry_after_seconds)}
            if prior is None:
                return {"status": "missing", "reason": "no_prior_known_price", "candle": None}
            if (prior.get("symbol") != final.symbol or prior.get("source_id") != final.source_id
                    or prior.get("source_dataset") != final.source_dataset
                    or utc(prior["opened_at"]) >= timestamp or utc(prior["available_at"]) > utc(final.request_ended_at)):
                raise ContractError("Synthetic constituent lacks an earlier causal same-source close")
            prior_duration = STEP if timestamp == op else MINUTE
            proof = _prior_price_continuity(prior, current_open=timestamp,
                current_available=final.request_ended_at, session=session,
                prior_duration=prior_duration, current_duration=MINUTE)
            if proof is None:
                return {"status": "missing", "reason": "unverified_prior_price_continuity", "candle": None,
                        "missing_minute": timestamp.isoformat(), "evidence": [asdict(item) for item in attempts]}
            close = float(prior["close"])
            values = dict(open=close, high=close, low=close, close=close, volume=0.0)
            synthetic = True
        record = _candle_record(final, session, values, synthetic=synthetic,
                                evidence=[asdict(attempt) for attempt in attempts], duration=MINUTE)
        record["opened_at"] = timestamp.isoformat()
        if synthetic:
            record.update(prior_price_opened_at=utc(prior["opened_at"]).isoformat(),
                          prior_price_close=float(prior["close"]), synthetic_continuity=proof,
                          no_trade_scope_evidence=dict(final.no_trade_scope_evidence))
        observed.append(record)
        prior = record
    frame, _ = aggregate_minutes(pd.DataFrame(observed), sessions=[session])
    candle = frame.iloc[0].to_dict()
    for key in ("opened_at", "available_at"):
        candle[key] = utc(candle[key]).isoformat()
    return {"status": "synthetic_no_trade" if candle["no_trade"] else "mixed_synthetic" if candle["synthetic"] else "observed",
            "candle": candle, "constituents": observed, "evidence": [asdict(item) for item in attempts]}


def _provider_request_lock(journal):
    # Requests made by forecasting and scoring share one journal. A nonblocking
    # lock gives exact-slot single flight and prevents another request beginning
    # before a just-received provider-wide Retry-After is durably recorded.
    key = str(journal.path.resolve()).casefold()
    with _PROVIDER_LOCKS_GUARD:
        return _PROVIDER_LOCKS.setdefault(key, threading.Lock())


def recover_databento_candle_step(provider, *, symbol, opened_at, journal: AcquisitionJournal,
                                  session: SessionBounds, previous: Mapping | None,
                                  clock: Callable = lambda: datetime.now(timezone.utc),
                                  stopped: Callable = lambda: False, population_contract=None) -> dict:
    """One bounded exact attempt or durable wait; no internal sleep or retry cap.

    Provider temporary errors wait at least four seconds across all Databento
    slots in this journal; longer Retry-After survives restart. Covered omission
    confirmations still need four seconds between requests. A concurrent completed request's observed
    evidence can be reused during that waiting interval; later calls can fetch
    corrections, retaining every original attempt. Broker methods are absent.
    """
    import uuid
    if stopped():
        raise InterruptedError("Candle acquisition stopped before recovery step")
    op, now = utc(opened_at), utc(clock())
    if op != op.floor("15min"):
        raise ContractError("Expected quarter-hour reference opening")
    if now < op + STEP:
        raise ContractError("Cannot acquire an unfinished reference candle")
    dataset = getattr(provider, "dataset", None)
    if not isinstance(dataset, str) or not dataset.strip():
        raise ContractError("Existing provider must declare its dataset")
    if not session.contains(op):
        return {"status":"closed", "reason":"outside_eligible_session", "candle":None}
    slot = dict(symbol=symbol, source_id=f"databento:{dataset.strip()}:ohlcv-1m",
                interval_open=op.isoformat(), interval_close=(op+STEP).isoformat())
    request_lock = _provider_request_lock(journal)
    if not request_lock.acquire(blocking=False):
        return {"status":"retry", "reason":"provider_request_in_flight", "candle":None,
                "retry_after_seconds":4.0}
    try:
        now = utc(clock())
        attempts = journal.responses(**slot)
        due = None
        if attempts:
            last = attempts[-1].validate()
            due = utc(last.request_ended_at) + pd.Timedelta(seconds=max(4.0,last.retry_after_seconds))
            if (utc(last.request_ended_at) <= now < due
                    and (previous is None or utc(previous["available_at"]) <= utc(last.request_ended_at))):
                resolved = _recoverable_prerequisite(resolve_minute_candle(attempts,session=session,previous=previous,
                                                                         population_contract=population_contract))
                if resolved["status"] != "retry":
                    return resolved
        provider_due = journal.provider_retry_due()
        exact_wait = max(0.0,(due-now).total_seconds()) if due is not None else 0.0
        provider_wait = max(0.0,(provider_due-now).total_seconds()) if provider_due is not None else 0.0
        if exact_wait or provider_wait:
            return {"status":"retry", "reason":"provider_retry_after" if provider_wait else "exact_interval_retry_wait",
                    "candle":None, "retry_after_seconds":max(exact_wait,provider_wait),
                    "evidence":[asdict(item) for item in attempts]}
        attempt = fetch_databento_interval_attempt(provider,symbol=symbol,opened_at=op,
                                                   attempt_id=uuid.uuid4().hex,clock=clock,stopped=stopped)
        actual = dict(symbol=attempt.symbol,source_id=attempt.source_id,
                      interval_open=attempt.interval_open,interval_close=attempt.interval_close)
        if journal.slot(**actual) != journal.slot(**slot) or attempt.source_dataset != dataset.strip():
            raise ContractError("Recovery callback changed its exact interval or source identity")
        journal.save(attempt)
        result = _recoverable_prerequisite(resolve_minute_candle(journal.responses(**slot),session=session,previous=previous,
                                                               population_contract=population_contract))
        if result["status"] == "retry":
            current = utc(clock())
            exact_due = utc(attempt.request_ended_at) + pd.Timedelta(seconds=max(4.0,attempt.retry_after_seconds))
            provider_due = journal.provider_retry_due()
            result["retry_after_seconds"] = max(0.0,(exact_due-current).total_seconds(),
                (provider_due-current).total_seconds() if provider_due is not None else 0.0)
        return result
    finally:
        request_lock.release()


def recover_databento_candle(provider, *, symbol, opened_at, journal: AcquisitionJournal,
                             session: SessionBounds, previous: Mapping | None,
                             sleep: Callable[[float], None], clock: Callable = lambda: datetime.now(timezone.utc),
                             population_contract=None, stopped: Callable = lambda: False) -> dict:
    """Indefinite exact recovery around bounded steps, preserving provider waits."""
    while True:
        result = recover_databento_candle_step(provider,symbol=symbol,opened_at=opened_at,
            journal=journal,session=session,previous=previous,clock=clock,population_contract=population_contract,stopped=stopped)
        if result["status"] != "retry":
            return result
        if stopped():
            raise InterruptedError("Candle recovery stopped before retry wait")
        # Short sleeps keep interruption responsive; remaining waits are durable.
        sleep(min(4.0,result["retry_after_seconds"]))


def fetch_15m(provider, *, symbol, reference, previous: Mapping | None, session: SessionBounds,
              journal: AcquisitionJournal, sleep: Callable[[float], None],
              clock: Callable = lambda: datetime.now(timezone.utc), population_contract=None,
              stopped: Callable = lambda: False) -> pd.DataFrame:
    """Convenience read-only live adapter returning an actual-availability row."""
    result = recover_databento_candle(provider, symbol=symbol, opened_at=reference, journal=journal,
                                      session=session, previous=previous, sleep=sleep, clock=clock,
                                      population_contract=population_contract,stopped=stopped)
    if result["candle"] is None:
        raise ContractError("Live candle unavailable: " + result.get("reason", result["status"]))
    return validate_candles(pd.DataFrame([result["candle"]]))


def validate_candles(frame: pd.DataFrame, *, duration=STEP) -> pd.DataFrame:
    required = {"symbol", "opened_at", "available_at", "availability_kind", "open", "high", "low", "close",
                "volume", "source_id", "source_dataset", "session_id", "synthetic"}
    if required - set(frame):
        raise ContractError("Candle frame lacks " + ",".join(sorted(required - set(frame))))
    out = frame.copy()
    out["opened_at"] = [utc(v) for v in out.opened_at]
    out["available_at"] = [utc(v) for v in out.available_at]
    if out.empty:
        return out
    if out.opened_at.duplicated().any() or out.symbol.nunique() != 1 or out.source_dataset.nunique() != 1:
        raise ContractError("One symbol/dataset and one immutable candle revision per snapshot required")
    rule = "15min" if duration == STEP else "1min"
    if (out.opened_at != out.opened_at.dt.floor(rule)).any():
        raise ContractError("Candles must be named by interval opening times")
    if out.available_at.lt(out.opened_at + duration).any():
        raise ContractError("Candle availability precedes interval completion")
    if not out.availability_kind.isin(["actual", "assumed_historical_close"]).all():
        raise ContractError("Unknown information availability basis")
    for field in ("open", "high", "low", "close", "volume"):
        out[field] = pd.to_numeric(out[field], errors="raise")
        if not np.isfinite(out[field]).all() or (out[field] < (0 if field == "volume" else np.nextafter(0, 1))).any():
            raise ContractError("Nonfinite or invalid OHLCV price")
    if (out.low.gt(out[["open", "close"]].min(axis=1)).any()
            or out.high.lt(out[["open", "close"]].max(axis=1)).any()
            or out.high.lt(out.low).any()):
        raise ContractError("Invalid OHLC range")
    if not out.synthetic.map(lambda v: isinstance(v, (bool, np.bool_))).all():
        raise ContractError("Synthetic provenance must be explicit boolean")
    if "no_trade" in out:
        no_trade = out.no_trade.fillna(False).astype(bool)
        if (no_trade & (~out.synthetic | out.volume.ne(0)
                | out.open.ne(out.close) | out.high.ne(out.close) | out.low.ne(out.close))).any():
            raise ContractError("Whole no-trade candle must be synthetic flat prior-close OHLC with zero volume")
    if {"session_open", "session_close"} <= set(out):
        if any(not SessionBounds(str(row.session_id), row.session_open, row.session_close).contains(row.opened_at, duration)
               for row in out.itertuples()):
            raise ContractError("Candle crosses a closed session")
    return out.sort_values("opened_at", kind="stable").reset_index(drop=True)


def aggregate_minutes(minutes: pd.DataFrame, *, sessions: Sequence[SessionBounds]) -> tuple[pd.DataFrame, dict]:
    """All 15 validated constituents, including explicitly confirmed no-trade.

    Synthetic minutes carry strictly prior close and zero volume. Thus a leading
    no-trade minute supplies the 15m open, all constituents affect high/low, and
    a trailing no-trade minute supplies close. A mixed candle is marked synthetic
    (and its observed/synthetic minute counts retained), so outcome policy cannot
    silently count a synthetic endpoint as an observed equality.
    """
    frame = validate_candles(minutes, duration=MINUTE)
    records, missing = [], []
    # A multi-year archive can contain hundreds of thousands of slots. Retain
    # the same explicit envelope test without scanning every historical session
    # for each slot (including envelopes that cross a UTC date boundary).
    sessions_by_day = {}
    for session in sessions:
        for day in pd.date_range(utc(session.opened_at).normalize(),
                                 (utc(session.closed_at) - pd.Timedelta(nanoseconds=1)).normalize(), freq="D"):
            sessions_by_day.setdefault(day, []).append(session)
    by_slot = {slot: group for slot, group in frame.groupby(frame.opened_at.dt.floor("15min"), sort=True)}
    for slot, group in by_slot.items():
        session = next((s for s in sessions_by_day.get(slot.normalize(), ()) if s.contains(slot)), None)
        if session is None:
            continue
        expected = pd.date_range(slot, periods=15, freq="1min")
        if len(group) != 15 or not pd.DatetimeIndex(group.opened_at).equals(expected):
            missing.append(dict(opened_at=slot.isoformat(), observed_constituents=int((~group.synthetic).sum()),
                                supplied_constituents=len(group), status="missing_constituents"))
            continue
        synthetic_count = int(group.synthetic.sum())
        constituents = []
        for row in group.itertuples():
            constituent = {"opened_at": row.opened_at.isoformat(), "available_at": row.available_at.isoformat(),
                           "synthetic": bool(row.synthetic), "source_id": row.source_id,
                           "evidence": json.loads(getattr(row, "evidence_json", "{}"))}
            if row.synthetic:
                # Optional columns on observed rows are NaN after concatenation;
                # omit them there, retain explicit price/time/session proof here.
                proof = getattr(row, "synthetic_continuity", None)
                if isinstance(proof, Mapping):
                    constituent.update(synthetic_continuity=dict(proof),
                        no_trade_scope_evidence=getattr(row, "no_trade_scope_evidence", None),
                        prior_price_opened_at=proof["prior_opened_at"], prior_price_close=proof["prior_close"],
                        prior_price_interval_seconds=proof["prior_interval_seconds"],
                        prior_price_session_id=proof["prior_session_id"],
                        prior_price_available_at=proof["prior_available_at"],
                        **{field: float(getattr(row, field)) for field in ("open", "high", "low", "close", "volume")})
            constituents.append(constituent)
        evidence = {"aggregation": "15-valid-minute-including-confirmed-flat-no-trade-v2",
                    "synthetic_continuity_policy": SYNTHETIC_CONTINUITY_POLICY, "constituents": constituents}
        records.append(dict(symbol=str(group.symbol.iloc[0]), opened_at=slot,
            available_at=max(group.available_at.max(), slot + STEP),
            availability_kind="assumed_historical_close" if group.availability_kind.ne("actual").any() else "actual",
            open=float(group.open.iloc[0]), high=float(group.high.max()), low=float(group.low.min()),
            close=float(group.close.iloc[-1]), volume=float(group.volume.sum()),
            source_id=str(group.source_id.iloc[0]), source_dataset=str(group.source_dataset.iloc[0]),
            session_id=session.session_id, session_open=session.opened_at, session_close=session.closed_at,
            synthetic=synthetic_count > 0, no_trade=synthetic_count == 15,
            synthetic_open=bool(group.synthetic.iloc[0]), synthetic_close=bool(group.synthetic.iloc[-1]),
            observed_constituents=15-synthetic_count, synthetic_constituents=synthetic_count,
            evidence_json=_json(evidence)))
    columns = ["symbol", "opened_at", "available_at", "availability_kind", "open", "high", "low", "close", "volume",
               "source_id", "source_dataset", "session_id", "session_open", "session_close", "synthetic", "no_trade",
               "synthetic_open", "synthetic_close", "observed_constituents", "synthetic_constituents", "evidence_json"]
    return pd.DataFrame(records, columns=columns), {"full_intervals": len(records), "incomplete_intervals": len(missing),
        "missing_examples": missing[:50], "missing_policy": "remain_missing_without_exact_no_trade_evidence",
        "closure_policy": "no_aggregation_outside_explicit_exchange_session"}


@dataclass(frozen=True)
class History:
    frame: pd.DataFrame
    manifest: dict


def _archive_native_frame(path: Path, *, symbol: str, request: Mapping) -> pd.DataFrame:
    """Decode already-saved DBN bytes locally; this constructs no API client."""
    import databento
    from app.services.databento_lifecycle import register_databento_cleanup
    register_databento_cleanup()
    saved = databento.DBNStore.from_file(path)
    if (saved.dataset != "XNAS.ITCH" or str(saved.schema) != "ohlcv-1m"
            or set(saved.symbols) != {symbol}
            or utc(saved.start) != pd.Timestamp(request["start"], tz="UTC")
            or utc(saved.end) != pd.Timestamp(request["end"], tz="UTC")):
        raise ContractError("Archive raw DBN identity or requested range differs")
    return saved.to_df(price_type="float", pretty_ts=True, map_symbols=True).reset_index()


def load_xnas_archive_history(root, symbol: str, *, start=None, end=None,
                              availability_offset_seconds: float = 0.0, population_contract=None) -> History:
    """Verified venue-only XNAS history, never mixed with canonical EQUS bars.

    Each immutable cold partition must retain its receipt, manifest, normalized
    payload and native DBN. Comparing all OHLCV observations with the native
    float-price decode establishes the unadjusted market price basis instead of
    guessing it from a filename or adding an undocumented scaling factor.
    """
    if not symbol or not symbol.replace(".", "").isalnum():
        raise ContractError("Invalid symbol")
    if not math.isfinite(availability_offset_seconds) or availability_offset_seconds < 0:
        raise ContractError("Historical availability assumption must have a nonnegative finite offset")
    symbol = symbol.upper()
    folder = Path(root).resolve() / "market-data/databento/us-equities/XNAS.ITCH/ohlcv-1m" / symbol / "windows"
    paths = sorted(folder.glob("*/manifest.json"))
    if not paths:
        raise ContractError(f"No existing XNAS minute archive for {symbol}")
    from .coverage import POPULATION_CONTRACT, check_population_contract, aggregate_source_minutes, clip_source_ranges
    check_population_contract(population_contract)
    frames, files, source_coverage = [], [], []
    columns = ["ts_event", "symbol", "open", "high", "low", "close", "volume"]
    for manifest_path in paths:
        receipt_path = manifest_path.with_name("receipt.json")
        normalized_path = manifest_path.with_name("normalized.parquet")
        raw_path = manifest_path.with_name("provider.dbn.zst")
        pinned = (manifest_path, receipt_path, normalized_path, raw_path)
        if not all(p.is_file() for p in pinned):
            raise ContractError("Archive partition lacks complete native provenance")
        before = {p: _hash_file(p) for p in pinned}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            request, normalized, raw = manifest["request"], manifest["normalized"], manifest["raw"]
            if (manifest["schema_version"] != "databento-cold-start-partition-v1"
                    or receipt["schema_version"] != "databento-cold-start-receipt-v1"
                    or request["dataset"] != "XNAS.ITCH" or request["schema"] != "ohlcv-1m"
                    or request["symbol_scope"] != [symbol] or request["stype_in"] != "raw_symbol"
                    or request["storage_contract"] not in {"generic", "isolated-cold-start"}
                    or not request["request_id"] or request["request_id"] != receipt["request_id"]
                    or normalized["path"] != normalized_path.name or raw["path"] != raw_path.name
                    or before[manifest_path] != receipt["manifest_checksum_sha256"]
                    or before[normalized_path] != normalized["checksum_sha256"]
                    or before[normalized_path] != receipt["normalized_checksum_sha256"]
                    or before[raw_path] != raw["checksum_sha256"]
                    or before[raw_path] != receipt["raw_checksum_sha256"]
                    or normalized_path.stat().st_size != normalized["size_bytes"]
                    or raw_path.stat().st_size != raw["size_bytes"]):
                raise ContractError("Archive identity, size or receipt checksum differs")
            frame = pd.read_parquet(normalized_path)
            if "ts_event" not in frame and frame.index.name == "ts_event":
                frame = frame.reset_index()
            if set(columns) - set(frame):
                raise ContractError("Archive minute format is incompatible")
            if (set(frame.symbol.astype(str)) != {symbol}
                    or not isinstance(frame.ts_event.dtype, pd.DatetimeTZDtype)
                    or any(not pd.api.types.is_float_dtype(frame[c]) for c in ("open", "high", "low", "close"))):
                raise ContractError("Archive needs exact symbol, aware timestamps and native float prices")
            if population_contract is not None:
                _require_native_observations(frame)
            frame = frame.loc[:, columns].reset_index(drop=True)
            frame["ts_event"] = frame.ts_event.dt.tz_convert("UTC")
            if (frame.empty or len(frame) != normalized["row_count"]
                    or normalized["timestamp_column"] != "ts_event"
                    or frame.ts_event.min() != utc(normalized["earliest_timestamp"])
                    or frame.ts_event.max() != utc(normalized["latest_timestamp"])
                    or frame.ts_event.lt(pd.Timestamp(request["start"], tz="UTC")).any()
                    or frame.ts_event.ge(pd.Timestamp(request["end"], tz="UTC")).any()):
                raise ContractError("Archive timestamp bounds or row count differ")
            native = _archive_native_frame(raw_path, symbol=symbol, request=request)
            if population_contract is not None:
                _require_native_observations(native)
            # Exact comparison also rejects split-adjusted, rescaled, filtered or
            # otherwise transformed prices even if their local hashes agree.
            if not frame.equals(native.loc[:, columns].reset_index(drop=True)):
                raise ContractError("Archive observations differ from unadjusted native DBN prices")
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ContractError):
                raise
            raise ContractError("Malformed archive provenance or observations") from exc
        for path, checksum in before.items():
            if _hash_file(path) != checksum:
                raise ContractError("Concurrent source mutation during archive read")
            files.append({"path": str(path), "sha256": checksum, "bytes": path.stat().st_size})
        frame = frame.rename(columns={"ts_event": "opened_at"})
        frame["available_at"] = frame.opened_at + MINUTE + pd.Timedelta(seconds=availability_offset_seconds)
        frames.append(frame.assign(availability_kind="assumed_historical_close",
            source_id="databento:XNAS.ITCH:ohlcv-1m", source_dataset="XNAS.ITCH",
            session_id="unassigned", synthetic=False, no_trade=False, evidence_json="{}"))
        source_coverage.append({"opened_at": pd.Timestamp(request["start"], tz="UTC").isoformat(),
            "closed_at": pd.Timestamp(request["end"], tz="UTC").isoformat(),
            "basis": "receipt_verified_native_archive_request", "request_id": request["request_id"],
            "manifest_sha256": before[manifest_path]})
    minutes = pd.concat(frames, ignore_index=True).drop_duplicates(["opened_at", "open", "high", "low", "close", "volume"])
    if minutes.opened_at.duplicated().any():
        raise ContractError("Conflicting archive minute revisions require explicit selection")
    if start is not None:
        minutes = minutes.loc[minutes.opened_at.ge(utc(start))]
    if end is not None:
        minutes = minutes.loc[minutes.opened_at.lt(utc(end))]
    if minutes.empty:
        raise ContractError("Selected historical window has no observations")
    # Native provider history can contain null/sentinel OHLCV observations.
    # Raw equality and immutable provenance have already been checked above;
    # exclude invalid observations explicitly as gaps, never as no-trade proof.
    if minutes.opened_at.isna().any() or (minutes.opened_at != minutes.opened_at.dt.floor("min")).any():
        raise ContractError("Archive minutes must have exact aware opening timestamps")
    prices = minutes[["open", "high", "low", "close"]]
    bad_price = (~np.isfinite(prices).all(axis=1)) | prices.le(0).any(axis=1)
    bad_volume = (~np.isfinite(minutes.volume)) | minutes.volume.lt(0)
    bad_range = (minutes.low.gt(minutes[["open", "close"]].min(axis=1))
                 | minutes.high.lt(minutes[["open", "close"]].max(axis=1)) | minutes.high.lt(minutes.low))
    invalid = bad_price | bad_volume | bad_range
    excluded = [{"opened_at": row.opened_at.isoformat(),
                 "reasons": [reason for reason, mask in (("nonfinite_or_nonpositive_price", bad_price),
                             ("nonfinite_or_negative_volume", bad_volume), ("invalid_ohlc_range", bad_range)) if mask.loc[index]],
                 "status": "missing_invalid_native_observation"}
                for index, row in minutes.loc[invalid].iterrows()]
    source_minute_rows = len(minutes)
    minutes = minutes.loc[~invalid].copy()
    if minutes.empty:
        raise ContractError("Selected archive history has no valid native observations")
    sessions = equity_sessions(minutes.opened_at.min(), minutes.opened_at.max() + MINUTE)
    if population_contract == POPULATION_CONTRACT:
        source_coverage = clip_source_ranges(source_coverage,start=start,end=end)
        frame, aggregation = aggregate_source_minutes(minutes, sessions=sessions, source_coverage=source_coverage)
    else:
        frame, aggregation = aggregate_minutes(minutes, sessions=sessions)
    manifest = {"symbol": symbol, "source_dataset": "XNAS.ITCH", "source_schema": "ohlcv-1m",
        "availability_kind": "assumed_historical_close", "availability_offset_seconds": availability_offset_seconds,
        "availability_limitation": "Historical publication/receipt timestamps not recorded; no point-in-time claim",
        "price_basis": "unadjusted_market_scale", "price_basis_evidence": "all_OHLCV_equal_native_DBN_float_decode",
        "source_policy": "single_verified_dataset_no_fill_no_fallback", "venue_limitation": "XNAS venue-only observations, not consolidated EQUS",
        "source_minute_rows": source_minute_rows, "invalid_native_rows_excluded": len(excluded),
        "invalid_native_observations": excluded, "invalid_native_policy": "remain_missing_never_no_trade_or_synthetic",
        "minute_rows": len(minutes), "first_minute_open": minutes.opened_at.min().isoformat(),
        "last_minute_open": minutes.opened_at.max().isoformat(), "candle_rows": len(frame),
        "first_candle_open": frame.opened_at.min().isoformat() if len(frame) else None,
        "last_candle_open": frame.opened_at.max().isoformat() if len(frame) else None,
        "files": files, "aggregation": aggregation}
    if population_contract is not None:
        manifest.update(population_contract=population_contract, source_coverage=source_coverage,
                        source_policy="verified_native_source_observations_no_fill_no_fallback")
    frame.attrs["intraday_history"] = manifest
    return History(frame, manifest)


def load_local_history(root, symbol: str, *, source_dataset="EQUS.MINI", start=None, end=None,
                       availability_offset_seconds: float = 0.0, population_contract=None, recovery_cutoff=None) -> History:
    """Read canonical minutes and retain native input objects for the new contract.

    Current compact canonical files contain no dataset/receipt columns. Dataset
    identity is therefore verified in their exact adjacent raw provider file.
    Close-plus-offset is an explicit historical assumption, never an actual
    receipt time and never a file-mtime proxy. No source mixing or gap fill.
    """
    from .coverage import POPULATION_CONTRACT, check_population_contract, aggregate_source_minutes, clip_source_ranges
    check_population_contract(population_contract)
    from .coverage import RECOVERY_POPULATION_CONTRACT
    if population_contract == RECOVERY_POPULATION_CONTRACT:
        # Canonical native source bytes remain unchanged. The model population
        # explicitly names its multi-provider selection and retains each origin.
        if source_dataset != "EQUS.MINI":
            raise ContractError("Recovery population requires the explicit primary EQUS.MINI binding")
        recovery_as_of = utc(recovery_cutoff) if recovery_cutoff is not None else utc(datetime.now(timezone.utc))
        original = load_local_history(root, symbol, source_dataset=source_dataset, start=start, end=end,
            availability_offset_seconds=availability_offset_seconds, population_contract=POPULATION_CONTRACT)
        from .recovered_population import wrap_primary, load_recovery_candles
        frame = pd.DataFrame([wrap_primary(row) for row in original.frame.to_dict("records")])
        recovered = load_recovery_candles(root, symbol, start=start, end=end,
            cutoff=recovery_as_of)
        represented = {utc(value) for value in frame.opened_at}
        recovered = [row for row in recovered if utc(row["opened_at"]) not in represented]
        if recovered:
            frame = pd.concat([frame, pd.DataFrame(recovered)], ignore_index=True)
        for column in ("opened_at", "available_at"):
            # Primary close clocks and actual recovery receipts have different ISO precision.
            # Parse each aware instant without pandas inferring one format for the column.
            frame[column] = [utc(value) for value in frame[column]]
        frame = frame.sort_values("opened_at").reset_index(drop=True)
        manifest = {**original.manifest, "population_contract": RECOVERY_POPULATION_CONTRACT,
            "source_policy": "primary_EQUS_observations_with_explicit_cross_provider_recovery",
            "model_candle_dataset": "MULTI_PROVIDER", "primary_source_dataset": source_dataset,
            "saved_recovery_rows": len(recovered), "recovery_precedence": "primary_observed_quarters_first",
            "recovery_selection_cutoff": recovery_as_of.isoformat(),
            "candle_rows": len(frame), "first_candle_open": frame.opened_at.min().isoformat(),
            "last_candle_open": frame.opened_at.max().isoformat()}
        frame.attrs["intraday_history"] = manifest
        return History(frame, manifest)
    if source_dataset == "XNAS.ITCH":
        return load_xnas_archive_history(root, symbol, start=start, end=end,
                                        availability_offset_seconds=availability_offset_seconds,
                                        population_contract=population_contract)
    if source_dataset != "EQUS.MINI":
        raise ContractError("Select explicit EQUS.MINI canonical history or XNAS.ITCH archive history")
    if not symbol or not symbol.replace(".", "").isalnum():
        raise ContractError("Invalid symbol")
    if not math.isfinite(availability_offset_seconds) or availability_offset_seconds < 0:
        raise ContractError("Historical availability assumption must have a nonnegative finite offset")
    folder = Path(root).resolve() / "stocks" / symbol.upper() / "bars/1m/databento/normalized"
    paths = sorted(folder.glob("*_ohlcv-1m_1m.parquet"))
    if not paths:
        raise ContractError(f"No existing canonical EQUS minute history for {symbol}")
    frames, files, source_coverage, native_objects = [], [], [], []
    for path in paths:
        raw_path = path.parent.parent / "raw" / (path.stem + "_raw.parquet")
        if not raw_path.is_file():
            raise ContractError(f"Canonical dataset provenance is unavailable: {raw_path}")
        before = {item: _hash_file(item) for item in (path, raw_path)}
        raw = pd.read_parquet(raw_path) if population_contract else pd.read_parquet(
            raw_path, columns=["provider_dataset", "source_schema", "symbol", "price_basis"])
        if (set(raw.provider_dataset.dropna().astype(str)) != {source_dataset}
                or set(raw.source_schema.dropna().astype(str)) != {"ohlcv-1m"}
                or set(raw.symbol.dropna().astype(str).str.upper()) != {symbol.upper()}
                or set(raw.price_basis.dropna().astype(str)) != {"unadjusted_market_scale"}):
            raise ContractError("Canonical raw identity or price basis differs from selected source")
        frame = pd.read_parquet(path)
        if not {"timestamp", "open", "high", "low", "close", "volume"} <= set(frame):
            raise ContractError("Canonical minute format is incompatible")
        if population_contract is not None:
            _require_native_observations(raw)
            _require_native_observations(frame)
            required = {"ts_event", "open", "high", "low", "close", "volume", "range_start", "range_end"}
            if required - set(raw):
                raise ContractError("Source-observation history requires native OHLCV and saved request ranges")
            if raw[["provider_dataset", "source_schema", "symbol", "price_basis"]].isna().any().any():
                raise ContractError("Raw source identity cannot contain missing values")
            if not isinstance(raw.ts_event.dtype, pd.DatetimeTZDtype) or not isinstance(frame.timestamp.dtype, pd.DatetimeTZDtype):
                raise ContractError("Native and canonical timestamps must be timezone aware")
            left = raw.rename(columns={"ts_event": "timestamp"})[["timestamp", "open", "high", "low", "close", "volume"]]
            right = frame[["timestamp", "open", "high", "low", "close", "volume"]]
            left, right = (item.sort_values("timestamp").reset_index(drop=True) for item in (left, right))
            if (len(left) != len(right) or not left.timestamp.equals(right.timestamp)
                    or not np.array_equal(left.iloc[:, 1:].to_numpy(dtype=float), right.iloc[:, 1:].to_numpy(dtype=float), equal_nan=True)):
                raise ContractError("Canonical observations differ from the saved native source")
            starts, ends = pd.DatetimeIndex([utc(v) for v in raw.range_start]), pd.DatetimeIndex([utc(v) for v in raw.range_end])
            if (raw.ts_event.lt(starts).any() or (raw.ts_event + MINUTE).gt(ends).any()):
                raise ContractError("Native observations escape their recorded source request range")
            for first, last in raw[["range_start", "range_end"]].drop_duplicates().itertuples(index=False, name=None):
                source_coverage.append({"opened_at": utc(first).isoformat(), "closed_at": utc(last).isoformat(),
                    "basis": "saved_native_request_range_historical_receipt_unrecorded", "raw_sha256": before[raw_path]})
        for item, checksum in before.items():
            if _hash_file(item) != checksum:
                raise ContractError("Concurrent source mutation during history read")
            files.append({"path": str(item), "sha256": checksum, "bytes": item.stat().st_size})
        if population_contract is not None:
            from io import BytesIO
            from ml.market_provenance import preserve_bytes
            # Preserve exactly the selected native observations and their saved
            # identity/ranges. Receipt times were not saved by this old format;
            # preserving bytes now cannot invent historical availability.
            native = raw.copy()
            if start is not None:
                native = native.loc[native.ts_event.ge(utc(start))]
            if end is not None:
                native = native.loc[native.ts_event.lt(utc(end))]
            native = native.sort_values("ts_event").reset_index(drop=True)
            payload = BytesIO()
            native.to_parquet(payload, index=False, compression="zstd")
            obj = preserve_bytes(Path(root) / "intraday/source-objects", payload.getvalue(), suffix=".parquet")
            native_objects.append({**obj, "rows": len(native), "original_raw_sha256": before[raw_path],
                "canonical_sha256": before[path], "content": "native_rows_with_original_source_identity_and_request_ranges",
                "availability_kind": "historical_receipt_unrecorded"})
        frame["opened_at"] = pd.to_datetime(frame.timestamp, utc=True)
        frame["available_at"] = frame.opened_at + MINUTE + pd.Timedelta(seconds=availability_offset_seconds)
        frame = frame.assign(symbol=symbol.upper(), availability_kind="assumed_historical_close",
                             source_id="databento:EQUS.MINI:ohlcv-1m", source_dataset=source_dataset,
                             session_id="unassigned", synthetic=False, no_trade=False, evidence_json="{}")
        frames.append(frame)
    minutes = pd.concat(frames, ignore_index=True)
    # Duplicate equal observations are benign; contradictory historical revisions
    # cannot be chosen silently in a point-in-time snapshot.
    minutes = minutes.drop_duplicates(["opened_at", "open", "high", "low", "close", "volume"])
    if minutes.opened_at.duplicated().any():
        raise ContractError("Conflicting canonical minute revisions require explicit selection")
    if start is not None:
        minutes = minutes.loc[minutes.opened_at.ge(utc(start))]
    if end is not None:
        minutes = minutes.loc[minutes.opened_at.lt(utc(end))]
    if minutes.empty:
        raise ContractError("Selected historical window has no observations")
    sessions = equity_sessions(minutes.opened_at.min(), minutes.opened_at.max() + MINUTE)
    if population_contract == POPULATION_CONTRACT:
        source_coverage = clip_source_ranges(source_coverage,start=start,end=end)
        frame, aggregation = aggregate_source_minutes(minutes, sessions=sessions, source_coverage=source_coverage)
    else:
        frame, aggregation = aggregate_minutes(minutes, sessions=sessions)
    manifest = {"symbol": symbol.upper(), "source_dataset": source_dataset, "source_schema": "ohlcv-1m",
        "availability_kind": "assumed_historical_close", "availability_offset_seconds": availability_offset_seconds,
        "availability_limitation": "Historical publication/receipt timestamps not recorded; no point-in-time claim",
        "price_basis": "unadjusted_market_scale", "source_policy": "single_verified_dataset_no_fill_no_fallback",
        "minute_rows": len(minutes), "first_minute_open": minutes.opened_at.min().isoformat(),
        "last_minute_open": minutes.opened_at.max().isoformat(), "candle_rows": len(frame),
        "first_candle_open": frame.opened_at.min().isoformat() if len(frame) else None,
        "last_candle_open": frame.opened_at.max().isoformat() if len(frame) else None,
        "files": files, "aggregation": aggregation}
    if population_contract is not None:
        manifest.update(population_contract=population_contract, source_coverage=source_coverage,
                        native_objects=native_objects,
                        source_policy="verified_native_source_observations_no_fill_no_fallback")
    frame.attrs["intraday_history"] = manifest
    return History(frame, manifest)


def causal_features(candles: pd.DataFrame) -> pd.DataFrame:
    """Trailing observed-candle features; holes remain explicit elapsed time.

    These are observed-step indicators, not claims of contiguous wall-clock
    coverage or complete session VWAP. No fitted normalization is done here.
    The model adapter fits preprocessing only on admitted training rows.
    """
    frame = validate_candles(candles)
    if frame.empty:
        return pd.DataFrame(columns=FEATURE_NAMES)
    cl, op, hi, lo, vol = [frame[field].astype(float) for field in ("close", "open", "high", "low", "volume")]
    change = cl.diff()
    gains, losses = change.clip(lower=0).rolling(14).sum(), (-change.clip(upper=0)).rolling(14).sum()
    denominator = gains + losses
    sma = cl.rolling(20).mean()
    volume_sum = vol.rolling(20).sum()
    vwap = (((hi + lo + cl) / 3) * vol).rolling(20).sum() / volume_sum.replace(0, np.nan)
    true_range = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    ret = cl.pct_change(fill_method=None)
    elapsed = frame.opened_at.diff().dt.total_seconds() / 60
    local = frame.opened_at.dt.tz_convert("America/New_York")
    out = pd.DataFrame({
        "return_1_observed": ret, "return_4_observed": cl / cl.shift(4) - 1,
        "return_20_observed": cl / cl.shift(20) - 1, "body_return": cl / op - 1,
        "range_relative": (hi - lo) / cl,
        "rsi14_observed": (gains / denominator.replace(0, np.nan) * 100).where(denominator.ne(0), 50),
        "distance_sma20_observed": cl / sma - 1, "distance_vwap20_observed": cl / vwap - 1,
        "atr14_relative_observed": true_range.rolling(14).mean() / cl,
        "relative_volume20_observed": vol / vol.shift().rolling(20).mean().replace(0, np.nan),
        "momentum_change": ret.diff(), "volatility20_observed": ret.rolling(20).std(ddof=0),
        "elapsed_minutes_previous": elapsed,
        "elapsed_minutes20": (frame.opened_at - frame.opened_at.shift(20)).dt.total_seconds() / 60,
        "minute_of_session": local.dt.hour * 60 + local.dt.minute - 240,
        "synthetic_input_fraction20": frame.synthetic.astype(float).rolling(20).mean(),
    })
    out.index = frame.opened_at
    return out.loc[:, FEATURE_NAMES]


@dataclass(frozen=True)
class Dataset:
    X: np.ndarray
    y: np.ndarray
    inference_X: np.ndarray
    feature_names: tuple[str, ...]
    label_available_at: pd.DatetimeIndex  # Joint feature/reference and target-label receipt.
    training_reference_at: pd.DatetimeIndex
    target_close_at: pd.DatetimeIndex
    inference: dict
    manifest: dict

    @property
    def reference_at(self):
        return self.training_reference_at


def build_dataset(candles: pd.DataFrame, *, cutoff, reference_at, lookback_days: float | None = None,
                  synthetic_policy: str | None = None, population_contract=None) -> Dataset:
    """Labels are >= reference OPEN at exact third CLOSE, mature by joint receipt.

    Training sample availability includes its features/reference as well as the
    target observation. A late reference correction cannot backdate a sample to
    an earlier target receipt when calibration or validation purges information.

    Explicit ``include``/``exclude`` synthetic policy is required when any
    otherwise eligible reference or target is synthetic. No default decides
    the unresolved research/headline treatment. Feature inputs can carry
    synthetic intervals, retained in the input snapshot and fraction feature.
    """
    if synthetic_policy not in {None, "include", "exclude"}:
        raise ContractError("Synthetic label policy must be include/exclude or unresolved")
    cutoff, reference = utc(cutoff), utc(reference_at)
    target_open, target_close = exact_target(reference)
    attrs = dict(candles.attrs.get("intraday_history", {}))
    from .coverage import population_frame, check_population_contract
    population_contract = population_contract or attrs.get("population_contract")
    check_population_contract(population_contract)
    admitted = population_frame(candles, population_contract)
    frame = validate_candles(admitted)
    frame = frame.loc[frame.available_at.le(cutoff) & (frame.opened_at + STEP).le(cutoff)].reset_index(drop=True)
    if lookback_days is not None:
        if not math.isfinite(lookback_days) or lookback_days <= 0:
            raise ContractError("Lookback must be a positive finite number of days")
        # Keep 20 earlier observations for causal feature warmup while limiting
        # admitted training reference timestamps to the requested time span.
        window_start = reference - pd.Timedelta(days=lookback_days)
    else:
        window_start = frame.opened_at.min() if len(frame) else reference
    if len(frame) == 0 or not frame.opened_at.eq(reference).any():
        raise ContractError("Exact completed reference is unavailable at training cutoff")
    # Inputs after the fixed cycle reference are never inference features even
    # if retraining runs late enough that another candle becomes available.
    frame = frame.loc[frame.opened_at.le(reference)].reset_index(drop=True)
    features = causal_features(frame)
    lookup = frame.set_index("opened_at", drop=False)
    inference_row = lookup.loc[reference]
    inference_values = features.loc[reference].to_numpy(dtype=float)
    if not np.isfinite(inference_values).all():
        raise ContractError("Inference lacks causal observed-history feature warmup")
    if {"session_open", "session_close"} <= set(frame):
        bounds = SessionBounds(str(inference_row.session_id), inference_row.session_open, inference_row.session_close)
        if not bounds.contains(target_open):
            raise UnresolvedPolicy("Exact intraday target crosses session closure; next-session policy is unresolved")
    X, y, references, closes, available, synthetic_excluded, synthetic_included = [], [], [], [], [], 0, 0
    for row in frame.itertuples():
        ref = row.opened_at
        if ref < window_start:
            continue
        targ, finish = exact_target(ref)
        if targ not in lookup.index:
            continue
        outcome = lookup.loc[targ]
        if str(outcome.session_id) != str(row.session_id) or outcome.available_at > cutoff or finish > cutoff:
            continue
        # Information used in each historical feature must have matured by that
        # row's own decision time, not merely the later overall training cutoff.
        position = int(frame.index[frame.opened_at.eq(ref)][0])
        own_cutoff = row.available_at
        history = frame.iloc[max(0, position - 20):position + 1]
        if history.available_at.gt(own_cutoff).any():
            continue
        values = features.loc[ref].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            continue
        if bool(row.synthetic) or bool(outcome.synthetic):
            if synthetic_policy is None:
                raise UnresolvedPolicy("Synthetic training label inclusion needs an explicit research policy")
            if synthetic_policy == "exclude":
                synthetic_excluded += 1
                continue
            synthetic_included += 1
        X.append(values)
        y.append(int(float(outcome.close) >= float(row.open)))
        references.append(ref)
        closes.append(finish)
        available.append(max(outcome.available_at, own_cutoff))
    if not X:
        raise InsufficientTrainingData("No matured finite training labels for this cycle/window")
    causal_input_rows = frame.tail(21)
    records = []
    for record in causal_input_rows.to_dict(orient="records"):
        clean = {}
        for key, value in record.items():
            if value is pd.NA or value is pd.NaT or (isinstance(value, float) and not math.isfinite(value)):
                # Optional provenance columns can be absent on older schema
                # rows when genuine live records are appended. Required price,
                # time and feature values were validated separately above.
                value = None
            elif isinstance(value, (pd.Timestamp, datetime)):
                value = value.isoformat()
            elif isinstance(value, np.generic):
                value = value.item()
            clean[key] = value
        records.append(clean)
    snapshot = {"candles": records, "feature_version": FEATURE_VERSION,
                "feature_names": list(FEATURE_NAMES), "features": inference_values.tolist()}
    fingerprint = hashlib.sha256(_json(snapshot).encode()).hexdigest()
    information_available = causal_input_rows.available_at.max()
    inference = {"symbol": str(inference_row.symbol), "reference_at": reference.isoformat(),
        "reference_open": float(inference_row.open), "data_through": (reference + STEP).isoformat(),
        "information_available_at": information_available.isoformat(), "target_open": target_open.isoformat(),
        "target_close": target_close.isoformat(), "target_version": TARGET_VERSION,
        "features": inference_values.tolist(), "source": str(inference_row.source_id),
        "source_dataset": str(inference_row.source_dataset), "availability_kind": str(inference_row.availability_kind),
        "snapshot": snapshot, "snapshot_sha256": fingerprint}
    if population_contract is not None:
        inference["population_contract"] = population_contract
        snapshot["population_contract"] = population_contract
        fingerprint = hashlib.sha256(_json(snapshot).encode()).hexdigest()
        inference["snapshot_sha256"] = fingerprint
    manifest = {"target_version": TARGET_VERSION, "feature_version": FEATURE_VERSION, "training_cutoff": cutoff.isoformat(),
        "training_rows": len(X), "label_available_at_max": max(available).isoformat(),
        "label_availability_policy": "max_target_and_feature_receipt",
        "first_training_reference": min(references).isoformat(), "last_training_reference": max(references).isoformat(),
        "lookback_days": lookback_days, "synthetic_policy": synthetic_policy, "synthetic_labels_excluded": synthetic_excluded,
        "synthetic_labels_included": synthetic_included,
        "availability_kinds": sorted(frame.availability_kind.unique().tolist()), "source_history": attrs,
        "feature_policy": "trailing_observed_steps_explicit_elapsed_gaps_no_fitted_global_preprocessing",
        "validation_requirement": "chronological_purge_targets_and_joint_information_receipts_before_validation_decisions"}
    if population_contract is not None:
        manifest.update(population_contract=population_contract,
            unverified_or_other_population_candles_excluded=len(candles)-len(admitted),
            absence_policy="source_empty_unavailable_never_unverified_synthetic")
    return Dataset(np.asarray(X, dtype=float), np.asarray(y, dtype=np.uint8), inference_values.reshape(1, -1),
                   FEATURE_NAMES, pd.DatetimeIndex(available), pd.DatetimeIndex(references), pd.DatetimeIndex(closes),
                   inference, manifest)
