"""Local forecast classification and read-only saved-evidence validation.

The classifier assumes a forecast payload and checks its immutable identity,
local scope, manual-session time, ensemble and fixed-target eligibility. The
saved reader validates the persisted forecast, cycle and frozen snapshot in one
read transaction, with bounded fresh attempts for transient SQLite contention.
Neither helper grants execution authority or reads
another trader's state. Callers still enforce their own current manual controls,
quote freshness, broker capacity and one-send order journal.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat
from threading import Lock
from types import BuiltinFunctionType
import time

from ml.stock_direction_policy import stock_action
from .prediction import ContractError, PACIFIC, TARGET_VERSION, digest, instant
from .store import ShadowStore
from . import prediction as _prediction_module
from . import store as _store_module
from .submission_time import submission_gate


_FORECAST_COLUMNS = "id,pc,symbol,cycle_id,reference_at,issued_at,payload"
_READ_RETRY_SECONDS = .25
_READ_RETRY_PAUSE_SECONDS = .01
_READ_MAX_ATTEMPTS = 8
_NATIVE_READ_CONNECT = sqlite3.connect
_NATIVE_READ_CONNECT_IS_BUILTIN = (
    type(_NATIVE_READ_CONNECT) is BuiltinFunctionType
    and _NATIVE_READ_CONNECT.__module__ == "_sqlite3"
    and _NATIVE_READ_CONNECT.__name__ == "connect")
_SNAPSHOT_CACHE_LIMIT = 64
_SNAPSHOT_VALIDATIONS = OrderedDict()
_SNAPSHOT_VALIDATIONS_LOCK = Lock()

# A successful native validator may witness exact current raw bytes for the
# Scout input loader. Custom readers and serializers retain their original call.
_NATIVE_STORE = ShadowStore
_NATIVE_INPUTS = ShadowStore.inputs
_NATIVE_CONNECT = ShadowStore.connect
_NATIVE_SNAPSHOT_VALUE = ShadowStore._snapshot_value
_NATIVE_DIGEST = _prediction_module.digest
_NATIVE_ENCODED = _prediction_module.encoded
_NATIVE_JSON_LOADS, _NATIVE_JSON_DUMPS = json.loads, json.dumps
_NATIVE_JSON_ENCODER = json.JSONEncoder
_NATIVE_JSON_DECODER = json.JSONDecoder
_NATIVE_DEFAULT_DECODER = json._default_decoder
_NATIVE_DECODER_STATE = tuple(vars(json._default_decoder).items())
_NATIVE_SHA256 = sha256
_NATIVE_JSON_PARTS = (
    json.JSONEncoder.__init__, json.JSONEncoder.encode, json.JSONEncoder.iterencode,
    json.JSONEncoder.default, json.encoder.c_make_encoder, json.encoder._make_iterencode,
    json.encoder.encode_basestring_ascii, json.JSONDecoder.__init__,
    json.JSONDecoder.decode, json.JSONDecoder.raw_decode,
    json._default_decoder.scan_once,
)


def _native_snapshot_validation():
    return (ShadowStore is _NATIVE_STORE
            and ShadowStore._snapshot_value is _NATIVE_SNAPSHOT_VALUE
            and _store_module.json is json and _prediction_module.json is json
            and _store_module.digest is _NATIVE_DIGEST
            and _prediction_module.digest is _NATIVE_DIGEST
            and _prediction_module.encoded is _NATIVE_ENCODED
            and _prediction_module.hashlib.sha256 is _NATIVE_SHA256 and sha256 is _NATIVE_SHA256
            and json.loads is _NATIVE_JSON_LOADS and json.dumps is _NATIVE_JSON_DUMPS
            and json.JSONEncoder is _NATIVE_JSON_ENCODER
            and json.JSONDecoder is _NATIVE_JSON_DECODER
            and json._default_decoder is _NATIVE_DEFAULT_DECODER
            and len(vars(json._default_decoder)) == len(_NATIVE_DECODER_STATE)
            and all(vars(json._default_decoder).get(name) is original
                    for name, original in _NATIVE_DECODER_STATE)
            and type(vars(json._default_decoder).get("memo")) is dict
            and not vars(json._default_decoder)["memo"]
            and all(current is original for current, original in zip((
                json.JSONEncoder.__init__, json.JSONEncoder.encode, json.JSONEncoder.iterencode,
                json.JSONEncoder.default, json.encoder.c_make_encoder, json.encoder._make_iterencode,
                json.encoder.encode_basestring_ascii, json.JSONDecoder.__init__,
                json.JSONDecoder.decode, json.JSONDecoder.raw_decode,
                json._default_decoder.scan_once), _NATIVE_JSON_PARTS)))


def _saved_inputs(store, cycle_id, symbol):
    """Read fresh Scout inputs, reusing only successful exact-byte validation.

    Account capture separates forecast validation from this read. Reread the
    original SQL links and raw payload, and decode a new owned dictionary even
    when the complete canonical-hash validation already witnessed those bytes.
    Sizing still validates that mutable dictionary before calculating anything.
    """
    if (type(store) is not _NATIVE_STORE or ShadowStore.inputs is not _NATIVE_INPUTS
            or ShadowStore.connect is not _NATIVE_CONNECT
            or ShadowStore.__getattribute__ is not object.__getattribute__
            or any(name in vars(store) for name in ('inputs', 'connect', '_snapshot_value'))
            or not _native_snapshot_validation()):
        return store.inputs(cycle_id, symbol)
    with store.connect() as db:
        row = db.execute("SELECT s.id,s.payload FROM snapshots s JOIN symbol_inputs i ON i.snapshot_id=s.id WHERE i.cycle_id=? AND i.symbol=?",
                         (cycle_id, symbol)).fetchone()
    if row is None:
        return None
    key, raw = row
    if type(raw) is str:
        witness = (key, sha256(raw.encode('utf-8')).digest())
        with _SNAPSHOT_VALIDATIONS_LOCK:
            native = _SNAPSHOT_VALIDATIONS.get(witness) is True
            if native:
                _SNAPSHOT_VALIDATIONS.move_to_end(witness)
        # The fresh connection/row read is a customization boundary too.
        # Recheck the validator immediately before using a prior witness.
        if (native and _native_snapshot_validation()
                and '_snapshot_value' not in vars(store)):
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ContractError("Persisted snapshot evidence hash changed")
            return key, value
    return key, store._snapshot_value(key, raw)


def _validate_saved_snapshot(key, raw):
    """Recheck exact bytes without repeatedly decoding a validated snapshot.

    A witness records only the immutable ID and raw-byte fingerprint after the
    original dictionary/canonical-hash validator succeeds. Every caller still
    reads the current row and validates its forecast/session links separately.
    """
    if type(raw) is not str:
        # json.loads detects encodings for bytes, but rejects some equivalent
        # text (for example a UTF-8 BOM). Never reuse a binary witness for text.
        ShadowStore._snapshot_value(key, raw)
        return
    witness = (key, sha256(raw.encode("utf-8")).digest())
    with _SNAPSHOT_VALIDATIONS_LOCK:
        if witness in _SNAPSHOT_VALIDATIONS:
            _SNAPSHOT_VALIDATIONS.move_to_end(witness)
            return
    # No long validation or database work under the cache lock. Concurrent
    # misses may validate twice; only successfully validated bytes are added.
    native = type(key) is str and _native_snapshot_validation()
    ShadowStore._snapshot_value(key, raw)
    native = native and _native_snapshot_validation()
    with _SNAPSHOT_VALIDATIONS_LOCK:
        _SNAPSHOT_VALIDATIONS[witness] = native
        _SNAPSHOT_VALIDATIONS.move_to_end(witness)
        while len(_SNAPSHOT_VALIDATIONS) > _SNAPSHOT_CACHE_LIMIT:
            _SNAPSHOT_VALIDATIONS.popitem(last=False)


def forecast_disposition(forecast, *, now, session_started_at, local_symbols):
    original = dict(forecast)
    key = original.pop('forecast_id', None)
    if key != digest(original) or original['target_version'] != TARGET_VERSION:
        raise ContractError('Immutable exact-target forecast required')
    if forecast['pc'] != 'Scout' or forecast['symbol'] not in local_symbols:
        return 'outside_local_scout_scope'
    if forecast['mode'] != 'live' or forecast['availability_basis'] != 'observed':
        return 'not_a_current_live_forecast'
    if instant(forecast['issued_at']) < instant(session_started_at):
        return 'forecast_precedes_manual_start'
    if instant(forecast['issued_at']) > instant(now):
        raise ContractError('Forecast issue lies in the future')
    if forecast['ensemble_status'] != 'full' or forecast['successful_count'] != forecast['configured_count']:
        return 'incomplete_ensemble'
    if stock_action(forecast['probability_not_down']) == 'HOLD':
        return 'probability_between_action_cutoffs'
    gate = submission_gate(as_of=now, eligible_session=instant(forecast['reference_at']).astimezone(PACIFIC).date())
    if gate.state != 'before_submission_cutoff':
        return gate.state
    if instant(now) >= instant(forecast['target_close']):
        return 'fixed_target_already_elapsed'
    return 'eligible_for_account_checks'


def _safe_path(path):
    """Reject network and redirected paths before SQLite can create any files."""
    raw = os.fspath(path)
    if not isinstance(raw, str) or raw.startswith(("\\\\", "//")):
        raise ContractError("Forecast evidence must use an absolute host-local path")
    result = Path(raw)
    if not result.is_absolute() or ".." in result.parts or result.name in {"", ".", ".."}:
        raise ContractError("Forecast evidence must use an absolute host-local file path")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(result.anchor)) != 3:
            raise ContractError("Forecast evidence requires a fixed host-local drive")
        # Alternate streams are not standalone files and bypass normal sidecars.
        if any(":" in part for part in result.parts[1:]):
            raise ContractError("Forecast evidence cannot use an alternate data stream")
    candidates = [result, *result.parents]
    candidates.extend(Path(str(result) + suffix) for suffix in ("-journal", "-wal", "-shm"))
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ContractError("Forecast evidence and its ancestors/sidecars cannot be symlinks or reparse points")
        if candidate == result and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ContractError("Forecast evidence must be a standalone regular file")
    return result


def _cycle(db, cycle_id):
    row = db.execute("SELECT pc,started_at,payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
    if row is None:
        raise ContractError("Persisted forecast cycle is missing")
    value = json.loads(row[2])
    if (not isinstance(value, dict) or value.get("pc") != row[0]
            or value.get("started_at") != row[1]):
        raise ContractError("Persisted cycle indexed identity changed")
    return value


def _saved_forecast_once(path, forecast, *, session_id, session_started,
                         _retry_deadline=None):
    # mode=ro cannot create absent evidence. Connection/setup stay zero-wait.
    # The original native first attempt configures at most the existing pause
    # as SQLite busy timeout, avoiding a Python sleep/re-entry during brief WAL
    # recovery. SQLite/OS scheduling is not a hard elapsed-time bound.
    with closing(sqlite3.connect(_safe_path(path).as_uri() + "?mode=ro",
                                 uri=True, timeout=0)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        wait_ms = 0
        if (_retry_deadline is not None and _NATIVE_READ_CONNECT_IS_BUILTIN
                and sqlite3.connect is _NATIVE_READ_CONNECT):
            remaining = _retry_deadline - time.monotonic()
            wait_ms = min(int(_READ_RETRY_PAUSE_SECONDS * 1000),
                          max(0, int(remaining * 1000)))
            if wait_ms:
                db.execute("PRAGMA busy_timeout=" + str(wait_ms))
        row = db.execute("SELECT " + _FORECAST_COLUMNS + " FROM forecasts WHERE id=?",
                         (forecast["forecast_id"],)).fetchone()
        if wait_ms:
            # This transaction now holds its consistent read view. Do not
            # multiply the native allowance across later integrity statements.
            db.execute("PRAGMA busy_timeout=0")
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
        _validate_saved_snapshot(*snapshot)

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


_NATIVE_SAVED_FORECAST_ONCE = _saved_forecast_once

def _record_read_retry_failure(error, *, attempt_count, elapsed_seconds,
                               sleep_count, sleep_requested_seconds, stop_code):
    """Attach numeric observations only; callers protect the original failure.

    Stop codes: 1 is the deadline before retry, 2 is the attempt limit, and
    3 is the deadline after sleep. This is diagnostic evidence, not authority.
    """
    error.scout_saved_forecast_retry = dict(
        version="saved-forecast-read-retry-v1", attempt_count=attempt_count,
        maximum_attempts=_READ_MAX_ATTEMPTS, retry_budget_seconds=_READ_RETRY_SECONDS,
        retry_pause_seconds=_READ_RETRY_PAUSE_SECONDS, elapsed_seconds=elapsed_seconds,
        sleep_count=sleep_count, sleep_requested_seconds=sleep_requested_seconds,
        stop_code=stop_code)


def _saved_forecast(path, forecast, *, session_id, session_started):
    # A new WAL reader can encounter SQLITE_BUSY_RECOVERY even without a stale
    # or conflicting forecast. Retry only native contention, never failed
    # evidence validation or I/O. Callers must recheck time/manual authority
    # after this read, as they must after any potentially slow integrity check.
    deadline = time.monotonic() + _READ_RETRY_SECONDS
    sleep_count, sleep_requested_seconds = 0, 0.0
    for attempt in range(_READ_MAX_ATTEMPTS):
        try:
            if (attempt == 0 and _saved_forecast_once is _NATIVE_SAVED_FORECAST_ONCE
                    and _NATIVE_READ_CONNECT_IS_BUILTIN
                    and sqlite3.connect is _NATIVE_READ_CONNECT):
                return _saved_forecast_once(path, forecast, session_id=session_id,
                    session_started=session_started, _retry_deadline=deadline)
            return _saved_forecast_once(path, forecast, session_id=session_id,
                                        session_started=session_started)
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            remaining = deadline - time.monotonic()
            if (type(code) is not int or code < 0 or code & 255 not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or attempt + 1 == _READ_MAX_ATTEMPTS or remaining <= 0):
                if type(code) is int and code >= 0 and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    try:
                        _record_read_retry_failure(exc, attempt_count=attempt + 1,
                            elapsed_seconds=_READ_RETRY_SECONDS - remaining,
                            sleep_count=sleep_count, sleep_requested_seconds=sleep_requested_seconds,
                            stop_code=2 if attempt + 1 == _READ_MAX_ATTEMPTS else 1)
                    except BaseException:
                        pass
                raise
            delay = min(_READ_RETRY_PAUSE_SECONDS, remaining)
            time.sleep(delay)
            sleep_count += 1
            sleep_requested_seconds += delay
            after_pause = time.monotonic()
            if after_pause >= deadline:
                try:
                    _record_read_retry_failure(exc, attempt_count=attempt + 1,
                        elapsed_seconds=after_pause - deadline + _READ_RETRY_SECONDS,
                        sleep_count=sleep_count, sleep_requested_seconds=sleep_requested_seconds,
                        stop_code=3)
                except BaseException:
                    pass
                raise


def saved_forecast(path, forecast, *, session_id, session_started):
    """Validate exact current-session evidence without creating or changing it.

    Missing, locked, malformed or redirected evidence raises ContractError.
    The session start is normalized with the same aware-clock parser used by
    the forecast store. Success returns None and is only an evidence check.
    """
    try:
        return _saved_forecast(path, forecast, session_id=session_id,
                               session_started=instant(session_started))
    except ContractError:
        raise
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        raise ContractError("Saved local forecast evidence is unavailable or invalid") from exc
