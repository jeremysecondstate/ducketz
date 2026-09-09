"""Bounded Live replay of a missing completed OPRA exchange session.

Called under Loop A's existing OPRA history lock. Historical files remain the
preferred archive; replay has its own immutable segment and delivery evidence.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from datafetching.databento_opra_history import (
    OPRA_STRATEGY_HISTORY_SCHEMAS, OpraSyncError, _download_partition,
    canonical_root, partition_directory, verify_partition,
)
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp


def completed_action_session(observed_at: object | None = None) -> str:
    """Latest actual XNYS session whose 17:00 Pacific action close has passed."""
    now = utc_timestamp(observed_at)
    local = now.tz_convert("America/Los_Angeles")
    day = pd.Timestamp(local.date())
    calendar = xcals.get_calendar("XNYS", start=day - pd.Timedelta(days=20),
                                  end=day + pd.Timedelta(days=2))
    for session in reversed(calendar.sessions):
        close = pd.Timestamp(session.date()).tz_localize(ZoneInfo("America/Los_Angeles")) + pd.Timedelta(hours=17)
        if close <= now:
            return session.date().isoformat()
    raise OpraSyncError("No completed OPRA action session is available")


def replay_session_bounds(session: str, schema: str, *, observed_at: object | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the completed session's immutable scope, independent of retention."""
    if schema not in OPRA_STRATEGY_HISTORY_SCHEMAS:
        raise OpraSyncError("Live fallback supports only production OPRA schemas")
    day = pd.Timestamp(session)
    if day.tzinfo is not None or day != day.normalize():
        raise OpraSyncError("An exact exchange-session date is required")
    calendar = xcals.get_calendar("XNYS", start=day - pd.Timedelta(days=10),
                                  end=day + pd.Timedelta(days=10))
    if not calendar.is_session(day):
        raise OpraSyncError("Live fallback cannot invent a weekend or holiday session")
    now = utc_timestamp(observed_at)
    action_close = day.tz_localize("America/Los_Angeles") + pd.Timedelta(hours=17)
    if now < action_close:
        raise OpraSyncError("Live fallback requires the completed action session")
    start = day.tz_localize("UTC")
    end = start + pd.Timedelta(days=1)
    if end > now:
        raise OpraSyncError("Live fallback cannot request a future interval")
    # CBBO retention differs from definitions/bars. Cover the entire executable
    # single-stock options session plus one hour before open, without claiming
    # that the rolling quote stream supplied the preceding UTC midnight hours.
    if schema == "cbbo-1m":
        start = utc_timestamp(calendar.session_open(day)) - pd.Timedelta(hours=1)
    return start, end


def verify_replay_cursor_coverage(
    root: Path, *, symbol: str, schema: str, cursor: Mapping[str, object],
) -> None:
    """Verify a replay-backed cursor against its immutable canonical delivery."""
    coverage = cursor.get("replay_coverage")
    if not isinstance(coverage, Mapping) or coverage.get("basis") != "VERIFIED_EXCHANGE_SESSION":
        raise OpraSyncError("OPRA replay cursor has invalid coverage evidence")
    root = Path(root).resolve()
    session = str(coverage.get("session", ""))
    start, end = replay_session_bounds(session, schema)
    parent = f"{symbol.strip().upper()}.OPT"
    expected = partition_directory(
        root, schema=schema, day=session, symbols=(parent,), segment="live-session",
    ).resolve()
    recorded = coverage.get("manifest_path")
    if not isinstance(recorded, str) or not recorded.strip():
        raise OpraSyncError("OPRA replay cursor has no manifest path")
    manifest_path = (root / recorded).resolve()
    if (
        root not in manifest_path.parents
        or manifest_path != expected / "manifest.json"
        or file_checksum(manifest_path) != coverage.get("manifest_checksum_sha256")
    ):
        raise OpraSyncError("OPRA replay cursor manifest binding is invalid")
    manifest = verify_partition(expected, datastore_root=root)["manifest"]
    request = manifest.get("request", {})
    if (
        cursor.get("provider") != "databento-opra"
        or cursor.get("dataset") != "OPRA.PILLAR"
        or cursor.get("symbol") != symbol.strip().upper()
        or cursor.get("provider_symbol") != parent
        or cursor.get("schema") != schema
        or pd.Timestamp(str(cursor["completed_through"]))
        != pd.Timestamp(session) + pd.Timedelta(days=1)
        or manifest.get("provider") != "databento-opra"
        or manifest.get("dataset") != "OPRA.PILLAR"
        or manifest.get("schema") != schema
        or manifest.get("partition_date") != session
        or manifest.get("symbol_scope") != parent
        or manifest.get("time_segment") != "live-session"
        or manifest.get("provider_delivery", {}).get("mode") != "live-intraday-replay"
        or request.get("dataset") != "OPRA.PILLAR"
        or request.get("schema") != schema
        or request.get("symbols") != [parent]
        or request.get("stype_in") != "parent"
        or utc_timestamp(request.get("start")) != start
        or utc_timestamp(request.get("end")) != end
        or utc_timestamp(manifest.get("partition_start")) != start
        or utc_timestamp(manifest.get("partition_end")) != end
        or utc_timestamp(coverage.get("partition_start")) != start
        or utc_timestamp(coverage.get("partition_end")) != end
    ):
        raise OpraSyncError("OPRA replay cursor coverage does not match its delivery")


def complete_session_from_replay(
    root: Path, *, api_key: str, symbols: Sequence[str], schemas: Sequence[str],
    session: str, entitlement: Mapping[str, object], max_bytes: int,
    reporter: Callable[[str], None] | None = None,
) -> tuple[int, int, int]:
    """Return completed, failed scopes and actual captured bytes; caller owns lock."""
    from datafetching.databento_opra_replay import capture_replay
    from datafetching.options_runtime import (
        _read_opra_symbol_history_cursor, _write_opra_symbol_history_cursor,
    )
    completed = failed = used = 0
    required_end = (pd.Timestamp(session) + pd.Timedelta(days=1)).date().isoformat()
    for schema in schemas:
        for symbol in symbols:
            try:
                marker = _read_opra_symbol_history_cursor(root, symbol=symbol, schema=schema)
                if marker is None:
                    raise OpraSyncError("A verified existing history cursor is required before replay")
                if pd.Timestamp(str(marker["completed_through"])) >= pd.Timestamp(required_end):
                    continue
                # Never jump over an older unfilled exchange session.
                first = pd.Timestamp(str(marker["completed_through"]))
                day = pd.Timestamp(session)
                calendar = xcals.get_calendar("XNYS", start=first - pd.Timedelta(days=2),
                                              end=day + pd.Timedelta(days=2))
                if any(first <= label < day for label in calendar.sessions):
                    raise OpraSyncError("Older missing OPRA sessions require Historical catch-up first")
                start, end = replay_session_bounds(session, schema)
                parents = (f"{symbol}.OPT",)
                destination = partition_directory(root, schema=schema, day=session,
                                                  symbols=parents, segment="live-session")
                if destination.exists():
                    manifest = verify_partition(destination, datastore_root=root)["manifest"]
                    request = manifest.get("request", {})
                    if (manifest.get("provider_delivery", {}).get("mode") != "live-intraday-replay"
                        or request.get("symbols") != list(parents)
                        or request.get("schema") != schema
                        or utc_timestamp(request.get("start")) != start
                        or utc_timestamp(request.get("end")) != end):
                        raise OpraSyncError("Existing replay partition differs from the required scope")
                else:
                    # Retention constrains a new subscription, not a saved,
                    # checksum-verified delivery or a Historical/preflight run.
                    if utc_timestamp() - start > pd.Timedelta(days=2):
                        raise OpraSyncError("Required OPRA replay is outside the bounded recent-session window")
                    remaining = max_bytes - used
                    if remaining < 1:
                        raise OpraSyncError("Live replay exhausted the remaining run byte budget")
                    staging_parent = canonical_root(root) / ".staging" / "live-replay" / symbol / schema / session
                    staging_parent.mkdir(parents=True, exist_ok=True)
                    # Native capture plus publication copy/normalized output share
                    # this volume; reserve conservatively before each subscription.
                    if shutil.disk_usage(staging_parent).free < 3 * remaining + 5 * 1024**3:
                        raise OpraSyncError("Insufficient free space for bounded Live replay")
                    capture = create_timestamp_directory(staging_parent)
                    raw_path = capture / "provider.dbn"
                    if reporter:
                        reporter(f"OPRA_LIVE_REPLAY_START symbol={symbol}; schema={schema}; start={start.isoformat()}; end={end.isoformat()}; byte_cap={remaining}")
                    delivery = capture_replay(api_key=api_key, schema=schema, symbols=parents,
                                              start=start.isoformat(), end=end.isoformat(),
                                              raw_path=raw_path, max_bytes=remaining)
                    used += raw_path.stat().st_size
                    (capture / "delivery.json").write_text(json.dumps(delivery, indent=2, default=str), encoding="utf-8")
                    manifest = _download_partition(None, datastore_root=root, entitlement=entitlement,
                        schema=schema, day=session, symbols=parents,
                        request_start=start.isoformat(), request_end=end.isoformat(),
                        segment="live-session", provider_file=raw_path, provider_delivery=delivery)
                _write_opra_symbol_history_cursor(root, symbol=symbol, schema=schema,
                    completed_through=required_end, requested_start=str(marker["requested_start"]),
                    lookback_policy=dict(marker["lookback_policy"]),
                    replay_coverage={"basis": "VERIFIED_EXCHANGE_SESSION", "session": session,
                        "partition_start": manifest["partition_start"], "partition_end": manifest["partition_end"],
                        "manifest_path": str((destination / "manifest.json").relative_to(root)),
                        "manifest_checksum_sha256": file_checksum(destination / "manifest.json")})
                completed += 1
                if reporter:
                    reporter(f"OPRA_LIVE_REPLAY_COMPLETE symbol={symbol}; schema={schema}; session={session}; rows={manifest['normalized']['row_count']}; path={destination}")
            except Exception as exc:
                failed += 1
                # SDK key material must never reach run/operator logs.
                reason = str(exc).replace(api_key, "[REDACTED]") if api_key else str(exc)
                if reporter:
                    reporter(f"OPRA_LIVE_REPLAY_FAILED symbol={symbol}; schema={schema}; {type(exc).__name__}: {reason}")
                # A failed capture may have consumed bytes. Avoid opening a second
                # subscription against an unknown remainder of the shared budget.
                return completed, failed, used
    return completed, failed, used
