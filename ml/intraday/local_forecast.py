"""Local forecast classification and read-only saved-evidence validation.

The classifier assumes a forecast payload and checks its immutable identity,
local scope, manual-session time, ensemble and fixed-target eligibility. The
saved reader validates the persisted forecast, cycle and frozen snapshot in one
zero-wait read transaction. Neither helper grants execution authority or reads
another trader's state. Callers still enforce their own current manual controls,
quote freshness, broker capacity and one-send order journal.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat

from .prediction import ContractError, PACIFIC, TARGET_VERSION, digest, instant
from .store import ShadowStore
from .submission_time import submission_gate


_FORECAST_COLUMNS = "id,pc,symbol,cycle_id,reference_at,issued_at,payload"


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
