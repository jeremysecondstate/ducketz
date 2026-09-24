"""Causal, source-bound Gameplan inputs from verified XNAS archives.

This module only reads data. It does not fit models, change labels, write an
operational view, publish a pointer, or access a provider. UTC daily OHLCV is
feature evidence, never a substitute for an intraday target endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
import re

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from datafetching.databento_archive import discover_archive_partitions
from datafetching.layout import safe_token
from ml.artifacts import file_checksum
from technicals.split_adjustments import discover_split_events


ARCHIVE_FEATURE_CONTRACT = "xnas-archive-prior-session-features-v1"
DATASET = "XNAS.ITCH"
CORE_FEATURE_COLUMNS = (
    "arch__daily_return_1", "arch__daily_return_5", "arch__daily_return_20",
    "arch__daily_volatility_20", "arch__daily_open_close_return",
    "arch__daily_range_fraction", "arch__daily_volume_ratio_20",
    "arch__daily_close_position_20",
)
HOURLY_FEATURE_COLUMNS = (
    "arch__hourly_open_close_return", "arch__hourly_range_fraction",
    "arch__hourly_volume_ratio_20_observed",
)
ARCHIVE_FEATURE_COLUMNS = CORE_FEATURE_COLUMNS + HOURLY_FEATURE_COLUMNS
_VALUES = ("open", "high", "low", "close", "volume")
_COVERAGE = ("source_coverage_start", "source_coverage_end", "source_kind")
_PROCESSING_DELAY = pd.Timedelta(minutes=5)
_WARNING_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*\(([^)]+)\)")


@dataclass(frozen=True)
class ArchiveFeatureSources:
    sources: pd.DataFrame
    feature_columns: tuple[str, ...]
    source_files: tuple[Path, ...]
    report: dict


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Archive feature clocks must be timezone aware")
    return stamp.tz_convert("UTC")


def _local(day, hour, minute=0):
    day = pd.Timestamp(day)
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour,
                        minute=minute, tz="America/Los_Angeles")


def _empty():
    out = pd.DataFrame(columns=("symbol", "timestamp", *_VALUES, *_COVERAGE))
    for name in ("timestamp", "source_coverage_start", "source_coverage_end"):
        out[name] = pd.Series(dtype="datetime64[ns, UTC]")
    return out


def _normalize(frame: pd.DataFrame, interval: pd.Timedelta, *, as_of) -> pd.DataFrame:
    """Validate observed bars and their declared complete source intervals."""
    if frame.empty:
        return _empty()
    required = {"symbol", "timestamp", *_VALUES, *_COVERAGE}
    if not required.issubset(frame):
        raise ValueError(f"Archive bars lack observed OHLCV/provenance: {sorted(required - set(frame))}")
    out = frame.copy()
    for name in ("timestamp", "source_coverage_start", "source_coverage_end"):
        if isinstance(out[name].dtype, pd.DatetimeTZDtype):
            if out[name].isna().any():
                raise ValueError("Archive feature clocks must be timezone aware")
            out[name] = out[name].dt.tz_convert("UTC")
        else:
            out[name] = pd.to_datetime([_utc(v) for v in out[name]], utc=True)
    out = out.loc[out.timestamp.add(interval + _PROCESSING_DELAY).le(_utc(as_of))].copy()
    out["symbol"] = out.symbol.astype(str).str.strip().str.upper()
    schema = {pd.Timedelta(days=1): "ohlcv-1d", pd.Timedelta(hours=1): "ohlcv-1h",
              pd.Timedelta(minutes=1): "ohlcv-1m"}[interval]
    if not out.source_kind.isin((f"verified-native-{schema}", "verified-native-1m-aggregate")).all():
        raise ValueError("Archive feature source kind is not verified XNAS OHLCV")
    if out.symbol.isin(("", "NAN", "NONE")).any() or out.timestamp.ne(out.timestamp.dt.floor(interval)).any():
        raise ValueError("Archive bar identity or UTC interval is invalid")
    if (out.timestamp.lt(out.source_coverage_start)
            | out.timestamp.add(interval).gt(out.source_coverage_end)).any():
        raise ValueError("Archive bar interval lacks complete declared source coverage")
    for name in _VALUES:
        out[name] = pd.to_numeric(out[name], errors="raise")
    if not np.isfinite(out[list(_VALUES)].to_numpy(dtype=float)).all():
        raise ValueError("Archive feature OHLCV must be finite observations")
    if (out[list(_VALUES[:-1])].le(0).any(axis=1) | out.volume.lt(0)
            | out.high.lt(out[["open", "close", "low"]].max(axis=1))
            | out.low.gt(out[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Archive feature OHLCV is inconsistent")
    distinct = out.drop_duplicates(["symbol", "timestamp", *_VALUES])
    if distinct.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("Conflicting observed archive OHLCV")
    return distinct.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)


def _covered(start, end, ranges):
    cursor = start
    for left, right in sorted(ranges):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def _aggregate_minutes(minutes, *, frequency, coverage, as_of):
    """Aggregate actual trades only; source receipts prove absent-minute coverage."""
    if minutes.empty:
        return _empty()
    interval = pd.Timedelta(frequency.replace("d", "D"))
    rows = []
    for (symbol, start), group in minutes.groupby(["symbol", minutes.timestamp.dt.floor(interval)], sort=True):
        end = start + interval
        if end + _PROCESSING_DELAY > _utc(as_of) or not _covered(start, end, coverage.get(symbol, ())):
            continue
        ordered = group.sort_values("timestamp")
        # Actual records must belong to this interval; no forward/backward fill.
        if ordered.timestamp.min() < start or ordered.timestamp.max() + pd.Timedelta(minutes=1) > end:
            raise ValueError("Minute aggregation escaped its source interval")
        rows.append({"symbol": symbol, "timestamp": start, "open": ordered.iloc[0].open,
                     "high": ordered.high.max(), "low": ordered.low.min(), "close": ordered.iloc[-1].close,
                     "volume": ordered.volume.sum(), "source_coverage_start": start,
                     "source_coverage_end": end, "source_kind": "verified-native-1m-aggregate",
                     "observed_constituent_count": len(ordered),
                     "first_actual_observation": ordered.timestamp.min(),
                     "last_actual_observation": ordered.timestamp.max()})
    return pd.DataFrame(rows) if rows else _empty()


def _prefer_native(native, derived):
    if native.empty:
        return derived
    if derived.empty:
        return native
    identities = pd.MultiIndex.from_frame(native[["symbol", "timestamp"]])
    remaining = derived.loc[~pd.MultiIndex.from_frame(derived[["symbol", "timestamp"]]).isin(identities)]
    return pd.concat([native, remaining], ignore_index=True).sort_values(["symbol", "timestamp"])


def _quality_intervals(partition):
    rows = []
    for warning in partition.manifest.get("provider_warnings", ()):
        message = str(warning.get("message", "")) if isinstance(warning, Mapping) else str(warning)
        dates = _WARNING_DATE.findall(message)
        if not dates:
            # Unclassified provider warnings are not permission to trust a bar.
            raise ValueError(f"Unclassified archive provider warning: {partition.manifest_path}")
        for day, condition in dates:
            if condition.lower() in {"available", "normal"}:
                continue
            start = pd.Timestamp(day, tz="UTC")
            rows.append({"symbol": partition.request["symbol_scope"][0], "start": start.isoformat(),
                         "end": (start + pd.Timedelta(days=1)).isoformat(),
                         "reason": "PROVIDER_QUALITY_" + condition.upper(),
                         "schema": partition.request["schema"], "manifest_path": str(partition.manifest_path)})
    return rows


def _split_source_files(root: Path, symbol: str) -> tuple[Path, ...]:
    """Bind every file read by native split discovery, including empty reports."""
    modern = root / "stocks" / safe_token(symbol) / "corporate/stock_splits/fmp/normalized"
    paths = tuple(sorted(modern.glob("*.parquet")))
    if paths:
        return paths
    return tuple(sorted((root / "normalized/fmp/corporate").glob(f"{safe_token(symbol)}_stock_splits_*.parquet")))


def _load_schema(root, symbols, schema, *, as_of):
    frames, files, exclusions = [], [], []
    coverage = {symbol: [] for symbol in symbols}
    interval = {"ohlcv-1d": pd.Timedelta(days=1), "ohlcv-1h": pd.Timedelta(hours=1),
                "ohlcv-1m": pd.Timedelta(minutes=1)}[schema]
    for part in discover_archive_partitions(root, market="us-equities", dataset=DATASET,
                                           schema=schema, verify_payload=True):
        request = part.request
        if request["symbol_scope"][0] not in symbols:
            continue
        if (request.get("dataset") != DATASET or request.get("stype_in") != "raw_symbol"
                or request.get("schema") != schema or len(request["symbol_scope"]) != 1):
            raise ValueError("Archive feature source identity mismatch")
        directory = part.directory.resolve()
        allowed = (root / "market-data/databento/us-equities" / DATASET).resolve()
        raw = part.manifest.get("raw", {})
        raw_path = (directory / str(raw.get("path", ""))).resolve()
        normalized_path = part.normalized_path.resolve()
        if (not directory.is_relative_to(allowed) or not raw_path.is_relative_to(directory)
                or not normalized_path.is_relative_to(directory)):
            raise ValueError("Archive feature payload escaped its source directory")
        if (not raw_path.is_file() or raw_path.stat().st_size != raw.get("size_bytes")
                or file_checksum(raw_path) != raw.get("checksum_sha256")
                or part.receipt.get("raw_checksum_sha256") != raw.get("checksum_sha256")):
            raise ValueError("Archive feature raw evidence verification failed")
        if _utc(part.manifest["published_at"]) > _utc(as_of):
            raise ValueError("Archive feature evidence was published after the frozen as-of")
        start, end = pd.Timestamp(request["start"], tz="UTC"), pd.Timestamp(request["end"], tz="UTC")
        symbol = request["symbol_scope"][0]
        frame = pd.read_parquet(normalized_path)
        timestamp = part.manifest["normalized"]["timestamp_column"]
        if timestamp not in frame and frame.index.name == timestamp:
            frame = frame.reset_index()
        if "symbol" in frame and set(frame.symbol.dropna().astype(str)) != {symbol}:
            raise ValueError("Archive feature payload contains another symbol")
        frame = frame.rename(columns={timestamp: "timestamp"}).assign(
            symbol=symbol, source_coverage_start=start, source_coverage_end=end,
            source_kind=f"verified-native-{schema}")
        missing_prices = frame[list(_VALUES[:-1])].isna()
        if (missing_prices.any(axis=1) & ~missing_prices.all(axis=1)).any():
            raise ValueError("Partially undefined archive feature OHLCV")
        for stamp in frame.loc[missing_prices.all(axis=1), "timestamp"]:
            stamp = _utc(stamp)
            exclusions.append({"symbol": symbol, "start": stamp.isoformat(),
                               "end": (stamp + interval).isoformat(),
                               "reason": "UNDEFINED_OBSERVED_OHLC", "schema": schema,
                               "manifest_path": str(part.manifest_path)})
        frame = frame.loc[~missing_prices.all(axis=1)]
        frames.append(frame[["symbol", "timestamp", *_VALUES, *_COVERAGE]])
        coverage[symbol].append((start, end))
        exclusions.extend(_quality_intervals(part))
        files.extend((*part.source_files, raw_path))
    joined = pd.concat(frames, ignore_index=True) if frames else _empty()
    return _normalize(joined, interval, as_of=as_of), tuple(files), coverage, exclusions


def load_archive_feature_sources(datastore_root: Path, *, symbols: Sequence[str], available_at,
                                 minute_bars: pd.DataFrame | None = None) -> ArchiveFeatureSources:
    """Read native daily/hourly and minute evidence without altering DATASTORE.

    The optional target-price frame is deliberately not treated as OHLCV feature
    evidence: that frame may contain only open/close endpoints. Native OHLCV and
    full interval coverage are independently verified here.
    """
    root = Path(datastore_root).resolve()
    clean = tuple(str(s).strip().upper() for s in symbols)
    if not clean or len(set(clean)) != len(clean) or any(not s for s in clean):
        raise ValueError("Archive features require a unique configured universe")
    daily, dfiles, _, dex = _load_schema(root, clean, "ohlcv-1d", as_of=available_at)
    hourly, hfiles, _, hex_ = _load_schema(root, clean, "ohlcv-1h", as_of=available_at)
    minutes, mfiles, coverage, mex = _load_schema(root, clean, "ohlcv-1m", as_of=available_at)
    daily = _prefer_native(daily, _aggregate_minutes(minutes, frequency="1d", coverage=coverage, as_of=available_at))
    hourly = _prefer_native(hourly, _aggregate_minutes(minutes, frequency="1h", coverage=coverage, as_of=available_at))
    split_events = {symbol: discover_split_events(root, symbol=symbol) for symbol in clean}
    split_files = tuple(path for symbol in clean for path in _split_source_files(root, symbol))
    result = build_archive_feature_sources(daily, hourly, symbols=clean, available_at=available_at,
                                          split_events=split_events, excluded_intervals=(*dex, *hex_, *mex))
    return ArchiveFeatureSources(result.sources, result.feature_columns,
                                 tuple(dict.fromkeys((*dfiles, *hfiles, *mfiles, *split_files))), result.report)


def build_archive_feature_sources(daily_bars: pd.DataFrame, hourly_bars: pd.DataFrame, *,
                                  symbols: Sequence[str], available_at,
                                  split_events: Mapping[str, Sequence] | None = None,
                                  excluded_intervals: Sequence[Mapping] = ()) -> ArchiveFeatureSources:
    """Pure feature construction; inputs must carry explicit source coverage.

    Core features require 21 consecutive observed exchange-session daily bars.
    Splits, source-quality intervals, missing sessions and >=3x unexplained raw
    discontinuities reset that warmup. This conservative reset is not a claim
    that the discontinuity heuristic detects every possible corporate action.
    """
    now = _utc(available_at)
    daily = _normalize(daily_bars, pd.Timedelta(days=1), as_of=now)
    hourly = _normalize(hourly_bars, pd.Timedelta(hours=1), as_of=now)
    clean = tuple(str(s).strip().upper() for s in symbols)
    if not clean or len(set(clean)) != len(clean) or any(not s for s in clean):
        raise ValueError("Archive features require a unique configured universe")
    if daily.empty or set(daily.symbol) - set(clean) or set(hourly.symbol) - set(clean):
        raise ValueError("Archive daily feature source universe is unavailable or inconsistent")
    calendar = xcals.get_calendar("XNYS", start=daily.timestamp.min().date() - pd.Timedelta(days=7),
                                 end=max(now.date(), daily.timestamp.max().date()) + pd.Timedelta(days=14))
    sessions = list(calendar.sessions)
    session_index = {pd.Timestamp(s).date(): i for i, s in enumerate(sessions)}
    exclusions = []
    for item in excluded_intervals:
        left, right = _utc(item["start"]), _utc(item["end"])
        if right <= left or str(item.get("symbol", "")).strip().upper() not in clean:
            raise ValueError("Archive quality interval identity or bounds are invalid")
        exclusions.append({**item, "symbol": str(item["symbol"]).strip().upper(),
                           "start": left.isoformat(), "end": right.isoformat()})
    rows, resets, by_symbol, split_boundaries, discontinuities = [], [], {}, [], []
    for symbol in clean:
        raw = daily.loc[daily.symbol.eq(symbol)].copy()
        if raw.empty:
            by_symbol[symbol] = {"status": "UNAVAILABLE_DAILY_ARCHIVE", "selected_rows": 0}
            continue
        raw["session"] = raw.timestamp.dt.date
        raw = raw.loc[raw.session.isin(session_index)].copy()
        excluded_dates = set()
        symbol_hours = hourly.loc[hourly.symbol.eq(symbol)].copy()
        for item in exclusions:
            if str(item.get("symbol", "")).upper() != symbol:
                continue
            left, right = _utc(item["start"]), _utc(item["end"])
            excluded_dates.update(raw.loc[raw.timestamp.lt(right) & raw.timestamp.add(pd.Timedelta(days=1)).gt(left), "session"])
            symbol_hours = symbol_hours.loc[~(symbol_hours.timestamp.lt(right)
                                             & symbol_hours.timestamp.add(pd.Timedelta(hours=1)).gt(left))]
        known_splits = {pd.Timestamp(event.ex_date if hasattr(event, "ex_date") else event["ex_date"]).date()
                        for event in (split_events or {}).get(symbol, ())}
        split_boundaries.extend({"symbol": symbol, "at": _local(day, 0).tz_convert("UTC").isoformat()}
                                for day in sorted(known_splits))
        segment, previous_day, previous_close = 0, None, None
        segment_ids = []
        for item in raw.itertuples():
            reasons = []
            if item.session in excluded_dates:
                reasons.append("PROVIDER_QUALITY_INTERVAL")
            if previous_day is not None and session_index[item.session] != session_index[previous_day] + 1:
                reasons.append("MISSING_EXCHANGE_SESSION")
            if previous_day is not None and any(previous_day < ex <= item.session for ex in known_splits):
                reasons.append("KNOWN_SPLIT_RESET")
            if previous_close is not None and max(previous_close / item.open, item.open / previous_close) >= 3:
                reasons.append("RAW_PRICE_DISCONTINUITY_RESET")
                discontinuities.append({"symbol": symbol,
                                        "at": _local(item.session, 0).tz_convert("UTC").isoformat(),
                                        "reason": "RAW_PRICE_DISCONTINUITY_RESET"})
            if reasons:
                segment += 1
                resets.append({"symbol": symbol, "session": str(item.session), "reasons": reasons})
            segment_ids.append(segment)
            previous_day, previous_close = item.session, item.close
            if item.session in excluded_dates:
                segment += 1
        raw["segment"] = segment_ids
        raw = raw.loc[~raw.session.isin(excluded_dates)].copy()
        pieces = []
        for _, frame in raw.groupby("segment", sort=False):
            frame = frame.copy()
            frame["segment_start"] = frame.timestamp.min()
            returns = frame.close.pct_change(fill_method=None)
            for lag in (1, 5, 20):
                frame[f"arch__daily_return_{lag}"] = frame.close.pct_change(lag, fill_method=None)
            frame["arch__daily_volatility_20"] = returns.rolling(20, min_periods=20).std(ddof=0)
            frame["arch__daily_open_close_return"] = frame.close / frame.open - 1
            frame["arch__daily_range_fraction"] = (frame.high - frame.low) / frame.close
            frame["arch__daily_volume_ratio_20"] = frame.volume / frame.volume.shift(1).rolling(20, min_periods=20).mean().replace(0, np.nan)
            lo, hi = frame.low.rolling(20).min(), frame.high.rolling(20).max()
            frame["arch__daily_close_position_20"] = (frame.close - lo) / (hi - lo).replace(0, np.nan)
            pieces.append(frame)
        featured = pd.concat(pieces) if pieces else raw
        selected = 0
        for item in featured.to_dict("records"):
            if not all(pd.notna(item.get(name)) and np.isfinite(item[name]) for name in CORE_FEATURE_COLUMNS):
                continue
            day = item["session"]
            label = pd.Timestamp(day)
            action = pd.Timestamp(calendar.next_session(label)).date()
            cutoff, action_start = _local(day, 17, 5).tz_convert("UTC"), _local(action, 4).tz_convert("UTC")
            completed = item["timestamp"] + pd.Timedelta(days=1)
            information = completed + _PROCESSING_DELAY
            if information > min(now, cutoff) or information >= action_start:
                continue
            optional = {name: np.nan for name in HOURLY_FEATURE_COLUMNS}
            hours = symbol_hours.loc[symbol_hours.timestamp.ge(item["segment_start"])
                                     & symbol_hours.timestamp.add(pd.Timedelta(hours=1) + _PROCESSING_DELAY).le(min(now, cutoff))]
            last_hour = None
            if not hours.empty:
                last_hour = hours.iloc[-1]
                hour_end = last_hour.timestamp + pd.Timedelta(hours=1)
                if (last_hour.timestamp >= pd.Timestamp(day, tz="UTC")
                        and hour_end >= pd.Timestamp(calendar.session_close(label))):
                    optional[HOURLY_FEATURE_COLUMNS[0]] = last_hour.close / last_hour.open - 1
                    optional[HOURLY_FEATURE_COLUMNS[1]] = (last_hour.high - last_hour.low) / last_hour.close
                    # Previous 20 actual hours in the same clean causal segment;
                    # a missing hour is never manufactured or treated as zero.
                    previous_volume = hours.iloc[:-1].volume.tail(20)
                    if len(previous_volume) == 20 and previous_volume.mean() > 0:
                        optional[HOURLY_FEATURE_COLUMNS[2]] = last_hour.volume / previous_volume.mean()
                    information = max(information, hour_end + _PROCESSING_DELAY)
                else:
                    last_hour = None
            if information >= action_start:
                continue
            rows.append({"symbol": symbol, "action_date": action, "source_session": day,
                         "source_selection_contract": ARCHIVE_FEATURE_CONTRACT,
                         "archive_feature_contract": ARCHIVE_FEATURE_CONTRACT,
                         "source_feature_cutoff": cutoff, "source_effective_cutoff": min(now, cutoff),
                         "source_action_start": action_start, "source_regular_close": pd.Timestamp(calendar.session_close(label)),
                         "source_bar_timestamp": item["timestamp"], "source_bar_end_timestamp": completed,
                         "bar_timestamp": item["timestamp"], "bar_end_timestamp": completed,
                         "information_available_at": information, "decision_timestamp": information,
                         "source_feature_timeframe": "1d", "source_feature_dataset": DATASET,
                         "source_feature_bar_kind": item["source_kind"], "archive_core_ready": True,
                         "archive_hourly_observed_at": last_hour.timestamp if last_hour is not None else pd.NaT,
                         "assumed_round_trip_cost": 0.001,
                         **{name: float(item[name]) for name in CORE_FEATURE_COLUMNS}, **optional})
            selected += 1
        by_symbol[symbol] = {"status": "READY" if selected else "UNAVAILABLE_CORE_WARMUP",
                             "daily_observations": len(raw), "selected_rows": selected,
                             "known_split_ex_dates": [str(day) for day in sorted(known_splits)],
                             "first_daily_observation": str(raw.timestamp.min()) if not raw.empty else None}
    sources = pd.DataFrame(rows)
    if sources.empty:
        raise ValueError("No complete causal archive feature rows are available")
    sources = sources.sort_values(["symbol", "action_date"]).reset_index(drop=True)
    report = {"contract_version": ARCHIVE_FEATURE_CONTRACT, "dataset": DATASET, "as_of": now.isoformat(),
              "source_selection_contract": ARCHIVE_FEATURE_CONTRACT, "selected_rows": len(sources),
              "selected_rows_by_symbol": sources.groupby("symbol").size().to_dict(), "by_symbol": by_symbol,
              "action_date_start": str(sources.action_date.min()), "action_date_end": str(sources.action_date.max()),
              "core_features": list(CORE_FEATURE_COLUMNS), "optional_hourly_features": list(HOURLY_FEATURE_COLUMNS),
              "feature_non_null_rows": {name: int(sources[name].notna().sum()) for name in ARCHIVE_FEATURE_COLUMNS},
              "minimum_core_daily_observations": 21, "synthetic_feature_bars": 0,
              "daily_timestamp_semantics": "UTC interval start; availability is full interval end plus five minutes",
              "split_policy": "causal warmup reset; no historical backadjustment; >=3x heuristic is not complete split detection",
              "quality_resets": resets, "excluded_intervals": exclusions,
              "excluded_undefined_observations": sum(item.get("reason") == "UNDEFINED_OBSERVED_OHLC"
                                                     for item in exclusions),
              "split_boundaries": split_boundaries,
              "target_discontinuity_boundaries": discontinuities,
              "label_policy_changed": False}
    sources.attrs["source_selection"] = report
    return ArchiveFeatureSources(sources, ARCHIVE_FEATURE_COLUMNS, (), report)
