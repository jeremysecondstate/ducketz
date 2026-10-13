"""Final local eligibility checks for Scout's already authorized one-shot POST.

This helper does not authenticate a service response, reserve account capacity,
or establish prior-order clearance. Its required proof callback must verify those
already saved facts for the exact request and grant. It must also bind the frozen
sizing hash, original desired quantity/side and current rounded quote limit, and
the account writers' current compatibility proof. The callback must use only
nonblocking local evidence and explicitly return True; an event ACK is not proof.

Forecast reads use the existing store's immutable validators through SQLite
mode=ro/query_only with zero lock wait. No production store or provider/broker is
constructed. This is a submission gate, never a cancellation/recovery gate:
exact-order reconciliation must remain available after cutoff or target expiry.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from decimal import Decimal
import json
import math
import sqlite3

from ml.stock_trader.session import stock_execution_window

from .execution_policy import submission_gate
from .prediction import ContractError, PACIFIC, STEP, TARGET_VERSION, digest, encoded, instant, number
from .scout_journal import _safe_path
from .scout_protocol import Grant, ProtocolError, Request
from .store import ShadowStore


_FORECAST_COLUMNS = "id,pc,symbol,cycle_id,reference_at,issued_at,payload"


def _affirm(callback, *args, message):
    try:
        result = callback(*args)
    except Exception as exc:
        raise ContractError(message) from exc
    if result is not True:
        raise ContractError(message)


def _controls(manual_authority, stopped):
    _affirm(manual_authority, message="Current manual Scout session authority required")
    try:
        stopped_value = stopped()
    except Exception as exc:
        raise ContractError("Manual stop state unavailable") from exc
    if stopped_value is not False:
        raise ContractError("Manual Scout session is stopped or stop state is invalid")


def _cycle(db, cycle_id):
    row = db.execute("SELECT pc,started_at,payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
    if row is None:
        raise ContractError("Persisted forecast cycle is missing")
    value = json.loads(row[2])
    if (not isinstance(value, dict) or value.get("pc") != row[0]
            or value.get("started_at") != row[1]):
        raise ContractError("Persisted cycle indexed identity changed")
    return value


def _saved_forecast(path, forecast, *, session_id, session_started):
    # mode=ro must not silently create an absent database. Zero timeout prevents
    # a locked writer from delaying the final effect gate until evidence expires.
    with closing(sqlite3.connect(_safe_path(path).as_uri() + "?mode=ro",
                                 uri=True, timeout=0)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT " + _FORECAST_COLUMNS + " FROM forecasts WHERE id=?",
                         (forecast["forecast_id"],)).fetchone()
        if row is None or ShadowStore._forecast_value(row) != forecast:
            raise ContractError("Exact immutable forecast is not saved")
        cycle = _cycle(db, forecast["cycle_id"])
        if (cycle.get("pc") != "Scout" or cycle.get("mode") != "live"
                or cycle.get("experiment") != session_id
                or not session_started <= instant(cycle["started_at"]) <= instant(forecast["issued_at"])
                or instant(cycle["reference_at"]) != instant(forecast["reference_at"])):
            raise ContractError("Forecast belongs to another manual Scout session")
        snapshot = db.execute(
            "SELECT s.id,s.payload FROM snapshots s JOIN symbol_inputs i ON i.snapshot_id=s.id "
            "WHERE i.cycle_id=? AND i.symbol=?", (forecast["cycle_id"], forecast["symbol"])).fetchone()
        if snapshot is None or snapshot[0] != forecast["snapshot_hash"]:
            raise ContractError("Exact frozen forecast snapshot is missing")
        ShadowStore._snapshot_value(*snapshot)

        # julianday compares offsets correctly and limits returned payloads to
        # this or later references, rather than loading historical forecast books.
        rows = db.execute("SELECT " + _FORECAST_COLUMNS + " FROM forecasts "
            "WHERE pc=? AND symbol=? AND julianday(reference_at)>=julianday(?)",
            ("Scout", forecast["symbol"], forecast["reference_at"]))
        original = (instant(forecast["reference_at"]), instant(forecast["issued_at"]))
        for candidate_row in rows:
            candidate = ShadowStore._forecast_value(candidate_row)
            if (candidate["mode"] != "live" or candidate["ensemble_status"] != "full"
                    or instant(candidate["issued_at"]) < session_started
                    or (instant(candidate["reference_at"]), instant(candidate["issued_at"])) <= original):
                continue
            source = _cycle(db, candidate["cycle_id"])
            if (source.get("pc") == "Scout" and source.get("mode") == "live"
                    and source.get("experiment") == session_id):
                raise ContractError("Newer current-session forecast supersedes this intention")


def local_gate(request, grant, *, forecast, forecast_path, session_id, session_started,
               local_symbols, account_fingerprint, quote_observed_at,
               max_quote_age_seconds, expected_time_in_force, manual_authority,
               stopped, authorization_current, clock):
    """Return True only for an exact, still eligible Scout POST; fail closed.

    All bindings, callbacks and the configured quote-age limit are explicit.
    ``authorization_current(request, grant)`` must be safe to repeat and cannot
    refresh remote evidence or create commitments. The adapter independently
    verifies its durable journal send claim and stable broker account. The caller
    must preserve those checks; success here alone never authorizes a POST.
    """
    try:
        return _local_gate(request, grant, forecast=forecast, forecast_path=forecast_path,
            session_id=session_id, session_started=session_started, local_symbols=local_symbols,
            account_fingerprint=account_fingerprint, quote_observed_at=quote_observed_at,
            max_quote_age_seconds=max_quote_age_seconds, expected_time_in_force=expected_time_in_force,
            manual_authority=manual_authority, stopped=stopped,
            authorization_current=authorization_current, clock=clock)
    except ContractError:
        raise
    except (ProtocolError, sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ContractError("Scout local submission evidence is unavailable or invalid") from exc


def _local_gate(request, grant, *, forecast, forecast_path, session_id, session_started,
                local_symbols, account_fingerprint, quote_observed_at,
                max_quote_age_seconds, expected_time_in_force, manual_authority,
                stopped, authorization_current, clock):
    if type(request) is not Request or type(grant) is not Grant:
        raise ContractError("Exact Request and Grant required")
    grant.validate(request)
    if (not local_symbols or isinstance(local_symbols, str)
            or any(type(symbol) is not str or not symbol for symbol in local_symbols)
            or request.symbol not in local_symbols or request.session_id != session_id
            or request.account_fingerprint != account_fingerprint):
        raise ContractError("Request differs from the bound Scout session, account or symbols")
    if (type(max_quote_age_seconds) not in (int, float)
            or not math.isfinite(max_quote_age_seconds) or max_quote_age_seconds <= 0
            or expected_time_in_force not in {"AM", "DAY", "PM"}
            or not all(callable(callback) for callback in
                       (manual_authority, stopped, authorization_current, clock))):
        raise ContractError("Explicit valid local gates, quote age and time-in-force required")
    if not isinstance(forecast, Mapping):
        raise ContractError("Immutable exact-target forecast required")
    # Own a detached value for this check, even if the caller retains a mutable
    # forecast mapping while a callback inspects other runtime evidence.
    forecast = json.loads(encoded(dict(forecast)))
    saved = dict(forecast)
    key = saved.pop("forecast_id", None)
    if key != digest(saved) or key != request.forecast_sha256 or saved["target_version"] != TARGET_VERSION:
        raise ContractError("Immutable exact-target forecast required")
    if (saved["pc"] != "Scout" or saved["symbol"] != request.symbol
            or saved["mode"] != "live" or saved["availability_basis"] != "observed"):
        raise ContractError("Current local Scout live forecast required")
    if (saved["ensemble_status"] != "full" or type(saved["configured_count"]) is not int
            or saved["configured_count"] <= 0 or type(saved["successful_count"]) is not int
            or saved["successful_count"] != saved["configured_count"]):
        raise ContractError("Complete configured ensemble required")
    reference, issued = instant(saved["reference_at"]), instant(saved["issued_at"])
    started, target = instant(session_started), instant(saved["target_close"])
    if (reference.minute % 15 or reference.second or reference.microsecond
            or instant(saved["target_open"]) != reference + 2 * STEP or target != reference + 3 * STEP
            or instant(saved["data_through"]) != reference + STEP
            or issued < reference + STEP or instant(saved["information_available_at"]) > issued):
        raise ContractError("Forecast fixed target or input availability changed")
    probability = number(saved["probability_not_down"])
    if (probability > 1 or request.side != ("BUY" if probability >= Decimal("0.50") else "SELL")
            or request.snapshot_sha256 != saved["snapshot_hash"]
            or instant(request.reference_at) != reference or instant(request.target_close_at) != target
            or instant(request.issued_at) < issued or issued < started):
        raise ContractError("Request differs from its forecast or predates the manual session")

    def current_time():
        current = instant(clock())
        if (issued > current or instant(request.issued_at) > current
                or instant(grant.issued_at) > current):
            raise ContractError("Forecast, request or grant issue lies in the future")
        gate = submission_gate(as_of=current, eligible_session=reference.astimezone(PACIFIC).date())
        if gate.state != "before_submission_cutoff":
            raise ContractError(gate.state)
        if current >= target:
            raise ContractError("Fixed forecast target already elapsed")
        if current >= instant(request.valid_until) or current >= instant(grant.expires_at):
            raise ContractError("Request or grant authorization expired")
        age = (current - instant(quote_observed_at)).total_seconds()
        if not 0 <= age <= max_quote_age_seconds:
            raise ContractError("Current quote is stale or lies in the future")
        window = stock_execution_window(current, allow_open_queue=False, allow_premarket_queue=False)
        if not window.executable or window.time_in_force != expected_time_in_force:
            raise ContractError("Current stock execution window or time-in-force changed")

    _controls(manual_authority, stopped)
    current_time()
    _affirm(authorization_current, request, grant, message="Current exact authenticated send and prior-order proof required")
    _saved_forecast(forecast_path, forecast, session_id=session_id, session_started=started)
    _affirm(authorization_current, request, grant, message="Current exact authenticated send and prior-order proof required")
    _controls(manual_authority, stopped)
    current_time()
    return True
