"""Read-only Gameplan technical reconstruction from genuine XNAS observations.

This explicit source contract replaces saved optional technical cells. It never
edits LoopB, normalizes operational files, fills missing hours, or fits a model.
Historical interval-end availability is an assumption, not a recovered receipt.
"""
from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

from ml.artifacts import file_checksum
from ml.gameplan_archive_features import (
    DATASET, _aggregate_minutes, _covered, _empty, _load_schema, _normalize,
    _quality_intervals, _utc,
)
from ml.stock_observation_sources import genuine_observation_mask
from technicals.calculations import bar_shape, breakout_pressure, market_regime, session_aware_breakout

OBSERVED_TECHNICAL_CONTRACT = "gameplan-observed-technicals-v1"
AVAILABILITY_BASIS = "reconstructed_interval_end_plus_processing_delay_not_historical_receipt"
TECHNICAL_EVIDENCE_COLUMNS = (
    "observed_technical_contract", "observed_technical_bar_timestamp",
    "observed_technical_bar_end_timestamp", "observed_technical_available_at",
    "observed_technical_segment_start", "observed_technical_history_rows",
    "observed_technical_raw_input_hashes_json", "observed_technical_availability_basis",
)
_HOUR = pd.Timedelta(hours=1)
_DELAY = pd.Timedelta(minutes=5)
_PRICES = ("open", "high", "low", "close", "volume")
_PREFIXES = ("mr__", "bp__", "bar__", "life__")


def _hashes(value):
    try:
        hashes = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Observed technical source lacks raw input hashes") from exc
    if (not isinstance(hashes, list) or not hashes
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes)):
        raise ValueError("Observed technical source has invalid raw input hashes")
    return set(hashes)


def _eligible(frame):
    numeric = frame.loc[:, _PRICES].apply(pd.to_numeric, errors="coerce")
    valid = (genuine_observation_mask(frame)
             & np.isfinite(numeric).all(axis=1)
             & numeric.loc[:, _PRICES[:-1]].gt(0).all(axis=1)
             & numeric.volume.gt(0)
             & numeric.high.ge(numeric.loc[:, ("open", "low", "close")].max(axis=1))
             & numeric.low.le(numeric.loc[:, ("open", "high", "close")].min(axis=1)))
    return frame.assign(**{name: numeric[name] for name in _PRICES}).loc[valid].copy(), int((~valid).sum())


def _load_tail_hours(root, symbols, hourly_coverage, *, available_at, evidence_available_at):
    """Read only minute partitions overlapping the native hourly coverage tail.

    Minute observations are filtered before aggregation; a covered response does
    not imply a complete market or permission to create an empty price interval.
    """
    from datafetching.databento_archive import discover_archive_partitions, verify_archive_payload
    tails = {s: max((right for _, right in hourly_coverage.get(s, ())), default=pd.Timestamp.min.tz_localize("UTC"))
             for s in symbols}
    frames, files, exclusions, coverage = [], [], [], {s: [] for s in symbols}
    evidence = _utc(evidence_available_at)
    rejected = 0
    for part in discover_archive_partitions(root, market="us-equities", dataset=DATASET,
                                           schema="ohlcv-1m", verify_payload=False):
        request = part.request
        scope = request.get("symbol_scope", [])
        if len(scope) != 1 or scope[0] not in symbols:
            continue
        symbol = scope[0]
        start, end = pd.Timestamp(request["start"], tz="UTC"), pd.Timestamp(request["end"], tz="UTC")
        if end <= tails[symbol] or start > _utc(available_at) or _utc(part.manifest["published_at"]) > evidence:
            continue
        if request.get("dataset") != DATASET or request.get("stype_in") != "raw_symbol" or request.get("schema") != "ohlcv-1m":
            raise ValueError("Observed technical minute source identity mismatch")
        directory = part.directory.resolve()
        raw = part.manifest.get("raw", {})
        raw_path = (directory / str(raw.get("path", ""))).resolve()
        normalized = part.normalized_path.resolve()
        if (not directory.is_relative_to((root / "market-data/databento/us-equities" / DATASET).resolve())
                or not raw_path.is_relative_to(directory) or not normalized.is_relative_to(directory)):
            raise ValueError("Observed technical source escaped archive directory")
        if (not raw_path.is_file() or raw_path.stat().st_size != raw.get("size_bytes")
                or file_checksum(raw_path) != raw.get("checksum_sha256")
                or part.receipt.get("raw_checksum_sha256") != raw.get("checksum_sha256")):
            raise ValueError("Observed technical raw hash mismatch")
        verify_archive_payload(part)
        frame = pd.read_parquet(normalized)
        verify_archive_payload(part)
        timestamp = part.manifest["normalized"]["timestamp_column"]
        if timestamp not in frame and frame.index.name == timestamp:
            frame = frame.reset_index()
        if "symbol" in frame and set(frame.symbol.dropna().astype(str)) != {symbol}:
            raise ValueError("Observed technical minute symbol mismatch")
        frame = frame.rename(columns={timestamp: "timestamp"})
        frame["timestamp"] = pd.to_datetime([_utc(v) for v in frame.timestamp], utc=True)
        frame = frame.loc[frame.timestamp.ge(tails[symbol])].copy()
        frame, count = _eligible(frame)
        rejected += count
        frame = frame.assign(symbol=symbol, source_coverage_start=start, source_coverage_end=end,
                             source_kind="verified-native-ohlcv-1m",
                             source_raw_input_hashes_json=json.dumps([raw["checksum_sha256"]]))
        frames.append(frame)
        coverage[symbol].append((max(start, tails[symbol]), end))
        exclusions.extend(_quality_intervals(part))
        files.extend((*part.source_files, raw_path))
    minutes = pd.concat(frames, ignore_index=True) if frames else _empty()
    minutes = _normalize(minutes, pd.Timedelta(minutes=1), as_of=available_at)
    # Do not let a flagged minute influence any aggregate in its warning window.
    for item in exclusions:
        left, right = _utc(item["start"]), _utc(item["end"])
        minutes = minutes.loc[~(minutes.symbol.eq(item["symbol"]) & minutes.timestamp.lt(right)
                                & minutes.timestamp.add(pd.Timedelta(minutes=1)).gt(left))]
    hours = _aggregate_minutes(minutes, frequency="1h", coverage=coverage, as_of=available_at)
    return hours, tuple(files), coverage, exclusions, rejected


def _segments(frame, *, symbol, coverage, exclusions, boundaries):
    """Reset recursive calculations at quality, split and unverified-source gaps."""
    intervals = [(_utc(v["start"]), _utc(v["end"])) for v in exclusions if v["symbol"] == symbol]
    points = [_utc(v["at"]) for v in boundaries if v["symbol"] == symbol]
    excluded = pd.Series(False, index=frame.index)
    for left, right in intervals:
        if left >= right:
            raise ValueError("Observed technical quality interval is invalid")
        excluded |= frame.timestamp.lt(right) & frame.timestamp.add(_HOUR).gt(left)
    frame = frame.loc[~excluded].copy()
    identifiers, resets = [], []
    previous = None
    segment = 0
    for row in frame.itertuples():
        reasons = []
        if previous is not None:
            if any(previous.timestamp < point <= row.timestamp for point in points):
                reasons.append("SPLIT_OR_PRICE_DISCONTINUITY")
            if any(previous.timestamp < right and row.timestamp >= left for left, right in intervals):
                reasons.append("SOURCE_QUALITY_INTERVAL")
            if not _covered(previous.timestamp + _HOUR, row.timestamp, coverage):
                reasons.append("UNVERIFIED_SOURCE_COVERAGE_GAP")
        if reasons:
            segment += 1
            resets.append({"symbol": symbol, "at": row.timestamp.isoformat(), "reasons": reasons})
        identifiers.append(segment)
        previous = row
    frame["_segment"] = identifiers
    return frame, resets, int(excluded.sum())


def _calculate(frame, columns):
    """Use original formulas; FULL outputs require sixty already observed bars."""
    if len(frame) < 60:
        return pd.DataFrame()
    symbol = str(frame.symbol.iloc[0])
    kwargs = dict(symbol=symbol, provider="databento", timeframe="1h")
    try:
        families = {
            "mr": market_regime.calculate_market_regime(frame, **kwargs),
            "bp": session_aware_breakout.calculate_breakout_pressure(frame, **kwargs),
            "bar": bar_shape.calculate_bar_shape(frame, **kwargs).rename(columns={"bar_timestamp": "timestamp"}),
        }
    except ValueError as exc:
        if "produced no fully initialized rows" in str(exc):
            return pd.DataFrame()
        raise
    # Functions select FULL mode from input length. Discard their earlier outputs
    # until sixty observations actually existed, making future appends invariant.
    output = frame[["timestamp"]].copy().set_index("timestamp")
    output["_history"] = np.arange(1, len(frame) + 1)
    for column in columns:
        family, field = column.split("__", 1)
        if family not in families:
            continue
        calculated = families[family].set_index("timestamp")
        if field not in calculated:
            raise ValueError(f"Unsupported observed technical feature: {column}")
        # Persisted LoopB samples contain raw calculation values; semantic model
        # transforms (e.g. log1p count) are NOT applied by their assembler.
        output[column] = pd.to_numeric(calculated[field], errors="coerce").replace([np.inf, -np.inf], np.nan)
    prefix, hashes = [], set()
    for value in frame.source_raw_input_hashes_json:
        hashes.update(_hashes(value))
        prefix.append(json.dumps(sorted(hashes), separators=(",", ":")))
    output["_hashes"] = prefix
    output["_start"] = frame.timestamp.iloc[0]
    output["_available"] = output.index + _HOUR + _DELAY
    return output.loc[output._history.ge(60)].reset_index()


def build_observed_technicals(archive, hourly, *, feature_columns, available_at,
                             coverage, excluded_intervals=(), split_boundaries=()):
    """Pure builder for verified hourly observations and the frozen daily grid."""
    now = _utc(available_at)
    sources = archive.sources.copy()
    columns = tuple(dict.fromkeys(c for c in (*feature_columns, *sources.columns) if c.startswith(_PREFIXES)))
    original = sources.reindex(columns=columns).copy()
    sources.loc[:, list(columns)] = np.nan
    for column in TECHNICAL_EVIDENCE_COLUMNS:
        if column.endswith(("timestamp", "available_at", "segment_start")):
            sources[column] = pd.Series(pd.NaT, index=sources.index, dtype="datetime64[ns, UTC]")
        else:
            sources[column] = pd.Series(None, index=sources.index, dtype=object)
    sources["observed_technical_contract"] = OBSERVED_TECHNICAL_CONTRACT
    sources["observed_technical_availability_basis"] = AVAILABILITY_BASIS
    sources["observed_technical_history_rows"] = 0
    sources["observed_technical_raw_input_hashes_json"] = "[]"
    clean, rejected = _eligible(hourly)
    clean = _normalize(clean if len(clean) else _empty(), _HOUR, as_of=now)
    if len(clean) and "source_raw_input_hashes_json" not in clean:
        raise ValueError("Observed technical bars require source hashes")
    reset_rows, counts = [], {}
    for symbol, selected in sources.groupby("symbol", sort=True):
        native = clean.loc[clean.symbol.eq(symbol)].copy().sort_values("timestamp")
        native, resets, excluded = _segments(native, symbol=symbol, coverage=coverage.get(symbol, ()),
                                             exclusions=excluded_intervals, boundaries=split_boundaries)
        reset_rows.extend(resets)
        pieces = [_calculate(group, columns) for _, group in native.groupby("_segment", sort=True)]
        pieces = [piece for piece in pieces if not piece.empty]
        features = pd.concat(pieces, ignore_index=True).sort_values("timestamp") if pieces else pd.DataFrame()
        attached = 0
        if not features.empty:
            features["_session"] = features.timestamp.dt.tz_convert("America/New_York").dt.date
            by_session = {day: frame for day, frame in features.groupby("_session")}
            for index, row in selected.iterrows():
                day = pd.Timestamp(row.source_session).date()
                candidates = by_session.get(day)
                if candidates is None:
                    continue
                limit = min(now, _utc(row.decision_timestamp), _utc(row.source_feature_cutoff))
                candidates = candidates.loc[candidates._available.le(limit)
                                            & candidates.timestamp.add(_HOUR).ge(_utc(row.source_regular_close))]
                if candidates.empty:
                    continue
                item = candidates.iloc[-1]
                for column in columns:
                    if column in item:
                        sources.at[index, column] = item[column]
                sources.at[index, "observed_technical_bar_timestamp"] = item.timestamp
                sources.at[index, "observed_technical_bar_end_timestamp"] = item.timestamp + _HOUR
                sources.at[index, "observed_technical_available_at"] = item._available
                sources.at[index, "observed_technical_segment_start"] = item._start
                sources.at[index, "observed_technical_history_rows"] = int(item._history)
                sources.at[index, "observed_technical_raw_input_hashes_json"] = item._hashes
                attached += 1
        counts[symbol] = {"positive_observed_hours": len(native), "source_quality_excluded": excluded,
                          "selected_source_rows": len(selected), "technical_rows_attached": attached}
    changed = {}
    for column in columns:
        left = pd.to_numeric(original[column], errors="coerce")
        right = pd.to_numeric(sources[column], errors="coerce")
        equal = (left.eq(right) | (left.isna() & right.isna()))
        changed[column] = {"old_nonnull": int(left.notna().sum()), "new_nonnull": int(right.notna().sum()),
                           "changed_rows": int((~equal).sum())}
    policy = {
        "contract_version": OBSERVED_TECHNICAL_CONTRACT, "dataset": DATASET,
        "source_policy": "native_hourly_history_then_covered_native_minute_tail_no_cross_feed_blending",
        "availability_basis": AVAILABILITY_BASIS, "information_cutoff": now.isoformat(),
        "population_policy": "positive_finite_volume_genuine_valid_ohlcv_before_every_recursive_calculation",
        "lookback_basis": "counts_of_actual_observations_not_elapsed_wall_hours; omitted hours are never filled",
        "minimum_past_observations": 60, "bootstrap_policy": "FULL_only_after_60_actual_past_observations",
        "retained_nonpositive_volume_bars": 0, "retained_synthetic_or_source_empty_bars": 0,
        "full_recursive_ancestry_hashes_bound": True,
        "calculation_versions": {"mr": market_regime.CALCULATION_VERSION,
                                 "bp": session_aware_breakout.CALCULATION_VERSION,
                                 "bar": bar_shape.CALCULATION_VERSION},
        "parameters": {"mr": market_regime.FULL_WINDOWS, "bp": breakout_pressure.FULL_WINDOWS,
                       "bar": {"atr_period": bar_shape.ATR_PERIOD, "atr_min_periods": bar_shape.ATR_MIN_PERIODS}},
        "persisted_transform": "raw_calculation_values_match_LoopB_sample_assembler; no_new_log1p",
        "cleared_families": list(_PREFIXES), "lifecycle_policy": "missing_no_clean_historical_calculation_receipt",
        "rejected_nonpositive_or_invalid_hours": rejected, "resets": reset_rows, "by_symbol": counts,
        "column_changes": changed, "label_or_model_gates_changed": False,
        "calculation_source_sha256": {Path(inspect.getsourcefile(module)).name: file_checksum(Path(inspect.getsourcefile(module)))
                                      for module in (market_regime, breakout_pressure, session_aware_breakout, bar_shape)},
    }
    report = {**archive.report, "observed_technicals": policy}
    sources.attrs = {**archive.sources.attrs, "source_selection": report}
    validate_observed_technical_sources(sources, feature_columns=columns)
    return replace(archive, sources=sources, report=report)


def validate_observed_technical_sources(frame, *, feature_columns):
    """Fail closed on saved reconstructed-feature lineage or availability drift."""
    columns = [c for c in feature_columns if c.startswith(_PREFIXES)]
    if any(c.startswith("life__") and frame[c].notna().any() for c in columns if c in frame):
        raise ValueError("Historical lifecycle values lack clean actual calculation receipts")
    numeric = [c for c in columns if c.startswith(("mr__", "bp__", "bar__")) and c in frame]
    active = frame[numeric].notna().any(axis=1) if numeric else pd.Series(False, index=frame.index)
    for column in numeric:
        values = pd.to_numeric(frame[column], errors="raise")
        if not np.isfinite(values.dropna()).all():
            raise ValueError("Observed technical numeric values must be finite or missing")
    if not set(TECHNICAL_EVIDENCE_COLUMNS).issubset(frame):
        raise ValueError("Observed technical evidence columns are absent")
    if not frame.observed_technical_contract.eq(OBSERVED_TECHNICAL_CONTRACT).all():
        raise ValueError("Observed technical contract mismatch")
    for row in frame.loc[active].itertuples():
        start, end, available = (_utc(getattr(row, c)) for c in TECHNICAL_EVIDENCE_COLUMNS[1:4])
        segment = _utc(row.observed_technical_segment_start)
        count = row.observed_technical_history_rows
        if (end != start + _HOUR or available != end + _DELAY
                or available > _utc(row.decision_timestamp) or available > _utc(row.source_feature_cutoff)
                or start.tz_convert("America/New_York").date() != pd.Timestamp(row.source_session).date()
                or end < _utc(row.source_regular_close) or segment > start
                or not isinstance(count, (int, float, np.integer, np.floating))
                or not np.isfinite(count) or count < 60 or count != int(count)
                or row.observed_technical_availability_basis != AVAILABILITY_BASIS):
            raise ValueError("Observed technical clocks or warmup are invalid")
        _hashes(row.observed_technical_raw_input_hashes_json)


def rebuild_observed_technicals(root, archive, *, symbols, feature_columns, available_at,
                               evidence_available_at=None):
    """Load verified archive evidence and replace optional cells in a new grid."""
    root = Path(root).resolve()
    symbols = tuple(str(s).upper().strip() for s in symbols)
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Observed technicals require a unique configured universe")
    now = _utc(available_at)
    evidence = _utc(available_at if evidence_available_at is None else evidence_available_at)
    if evidence < now:
        raise ValueError("Observed technical evidence cutoff precedes information cutoff")
    hours, files, coverage, excluded = _load_schema(root, symbols, "ohlcv-1h", as_of=now,
                                                   evidence_available_at=evidence)
    tail, tail_files, tail_coverage, tail_excluded, rejected_minutes = _load_tail_hours(
        root, symbols, coverage, available_at=now, evidence_available_at=evidence)
    hours = pd.concat([hours, tail], ignore_index=True)
    joined_coverage = {symbol: [*coverage.get(symbol, ()), *tail_coverage.get(symbol, ())] for symbol in symbols}
    result = build_observed_technicals(
        archive, hours, feature_columns=feature_columns, available_at=now, coverage=joined_coverage,
        excluded_intervals=(*archive.report.get("excluded_intervals", ()), *excluded, *tail_excluded),
        split_boundaries=(*archive.report.get("split_boundaries", ()),
                          *archive.report.get("target_discontinuity_boundaries", ())))
    result.report["observed_technicals"].update(evidence_available_at=evidence.isoformat(),
                                               rejected_native_minutes_before_aggregation=rejected_minutes)
    return replace(result, source_files=tuple(dict.fromkeys((*archive.source_files, *files, *tail_files))))
