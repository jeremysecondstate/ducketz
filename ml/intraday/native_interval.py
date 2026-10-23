"""Opt-in exact-quarter OHLC from covered, genuine native observations.

Source-empty minutes are occupancy gaps, never fabricated OHLC or market-wide
no-trade claims. This contract is separate from the legacy fifteen-constituent
contract and is selected only by a frozen local manual recipe.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np
import pandas as pd

from .data import (History, IntervalResponse, MINUTE, STEP, equity_sessions,
                   utc, validate_candles)
from .observed_data import build_observed_dataset
from .prediction import ContractError
from .training_timing import LEGACY


CONTRACT = "intraday-covered-native-interval-v1"
FEATURE_VERSION = "intraday-covered-native-observed-features-v1"
FEATURE_POLICY = "covered_exact_quarter_actual_observations_only_no_price_fill_v1"
LABEL_PROVENANCE = "covered_native_reference_open_and_exact_target_observed_close"
RECOVERY_CONTRACT = "intraday-covered-native-recovery-v2"
FIELDS = ("open", "high", "low", "close", "volume")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _rows(values, *, opened, closed):
    result = []
    for value in values:
        at = utc(value.get("opened_at", value.get("timestamp")))
        if pd.isna(at) or at != at.floor("min") or not opened <= at < closed:
            raise ContractError("Native observation is outside its exact minute/quarter")
        row = {"opened_at": at.isoformat()}
        for name in FIELDS:
            number = value[name]
            if isinstance(number, (bool, np.bool_)) or not isinstance(number, (int, float, np.number)):
                raise ContractError("Native OHLCV must be numeric")
            number = float(number)
            if not math.isfinite(number) or number < (0 if name == "volume" else np.nextafter(0, 1)):
                raise ContractError("Native OHLCV is invalid")
            row[name] = number
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]) or row["high"] < row["low"]:
            raise ContractError("Native OHLC range is invalid")
        result.append(row)
    result.sort(key=lambda row: row["opened_at"])
    if len(result) > 15 or len({row["opened_at"] for row in result}) != len(result):
        raise ContractError("Native minutes must be unique within the quarter")
    return result


def _aggregate(rows):
    return {"open": rows[0]["open"], "high": max(row["high"] for row in rows),
            "low": min(row["low"] for row in rows), "close": rows[-1]["close"],
            "volume": sum(row["volume"] for row in rows)}


def _candle(*, symbol, source_dataset, opened, available, kind, session, rows, coverage):
    if not rows:
        raise ContractError("An empty native interval has no observed OHLC")
    value = {"symbol": symbol, "source_dataset": source_dataset,
        "source_id": f"databento:{source_dataset}:ohlcv-1m", "opened_at": opened.isoformat(),
        "available_at": available.isoformat(), "availability_kind": kind,
        "session_id": session.session_id, "session_open": session.opened_at,
        "session_close": session.closed_at, **_aggregate(rows), "synthetic": False,
        "no_trade": False, "synthetic_open": False, "synthetic_close": False,
        "interval_seconds": 900, "observed_constituents": len(rows),
        "synthetic_constituents": 0, "source_empty_constituents": 15 - len(rows),
        "native_interval_contract": CONTRACT,
        "evidence_json": _json({"native_interval_contract": CONTRACT,
            "symbol": symbol, "source_dataset": source_dataset,
            "interval_open": opened.isoformat(), "interval_close": (opened + STEP).isoformat(),
            "coverage": coverage, "observations": rows, "prices_filled": False})}
    if kind == "actual_historical_recovery":
        value.update(native_recovery_contract=RECOVERY_CONTRACT,
                     feature_cutoff_at=(opened + STEP).isoformat())
    if not completed_native_interval(value):
        raise ContractError("Native interval evidence failed its own validation")
    return value


def completed_native_interval(candle, *, from_frame=False):
    """Verify exact native values, source scope, occupancy and coverage proof."""
    try:
        if candle.get("native_interval_contract") != CONTRACT:
            return False
        opened, available = utc(candle["opened_at"]), utc(candle["available_at"])
        if pd.isna(opened) or pd.isna(available) or opened != opened.floor("15min") or available < opened + STEP:
            return False
        if (candle.get("interval_seconds") != 900 or bool(candle.get("synthetic", True))
                or bool(candle.get("no_trade", True)) or bool(candle.get("synthetic_open", True))
                or bool(candle.get("synthetic_close", True))):
            return False
        symbol, dataset = candle["symbol"], candle["source_dataset"]
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol)
                or dataset != "EQUS.MINI" or candle["source_id"] != f"databento:{dataset}:ohlcv-1m"):
            return False
        raw = candle["evidence_json"]
        evidence = json.loads(raw) if isinstance(raw, str) else raw
        if (evidence.get("native_interval_contract") != CONTRACT or evidence.get("symbol") != symbol
                or evidence.get("source_dataset") != dataset or evidence.get("prices_filled") is not False
                or utc(evidence["interval_open"]) != opened or utc(evidence["interval_close"]) != opened + STEP):
            return False
        observations = _rows(evidence["observations"], opened=opened, closed=opened + STEP)
        if not observations:
            return False
        counts = [candle[k] for k in ("observed_constituents", "synthetic_constituents", "source_empty_constituents")]
        if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.number))
               or not math.isfinite(float(v)) or not float(v).is_integer() for v in counts):
            return False
        if counts != [len(observations), 0, 15 - len(observations)]:
            return False
        if any(float(candle[k]) != v for k, v in _aggregate(observations).items()):
            return False
        coverage = evidence["coverage"]
        if (coverage.get("symbol") != symbol or coverage.get("source_dataset") != dataset
                or coverage.get("status") != "success" or utc(coverage["start"]) > opened
                or utc(coverage["end"]) < opened + STEP):
            return False
        kind = candle["availability_kind"]
        if kind == "actual":
            if (coverage.get("kind") != "actual_exact_response"
                    or utc(coverage["interval_open"]) != opened or utc(coverage["interval_close"]) != opened + STEP
                    or not coverage.get("attempt_id") or available != utc(coverage["request_ended_at"])
                    or utc(coverage["request_started_at"]) < opened + STEP
                    or utc(coverage["request_started_at"]) > available):
                return False
            if len(observations) < 15:
                previous = coverage["stable_previous"]
                retry = previous["retry_after_seconds"]
                if (isinstance(retry, (bool, np.bool_)) or not isinstance(retry, (int, float))
                        or not math.isfinite(retry) or retry < 0
                        or previous.get("status") != "success" or previous.get("symbol") != symbol
                        or previous.get("source_dataset") != dataset
                        or previous.get("source_id") != candle["source_id"]
                        or not previous.get("attempt_id") or previous["attempt_id"] == coverage["attempt_id"]
                        or utc(previous["interval_open"]) != opened or utc(previous["interval_close"]) != opened + STEP
                        or utc(previous["start"]) > opened or utc(previous["end"]) < opened + STEP
                        or utc(previous["request_started_at"]) < opened + STEP
                        or utc(previous["request_ended_at"]) < utc(previous["request_started_at"])
                        or (utc(coverage["request_started_at"]) - utc(previous["request_ended_at"])).total_seconds() < max(4.0, retry)
                        or _rows(previous["observations"], opened=opened, closed=opened + STEP) != observations):
                    return False
        elif kind == "actual_historical_recovery":
            from .native_recovery import valid_recovery_coverage
            if not valid_recovery_coverage(candle, coverage):
                return False
        elif kind == "assumed_historical_close":
            if (coverage.get("kind") != "historical_native_raw_range" or available != opened + STEP
                    or coverage.get("observation_basis") != "canonical_native_file_after_successful_fetch_merges"
                    or not isinstance(coverage.get("raw_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", coverage["raw_sha256"])
                    or not isinstance(coverage.get("normalized_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", coverage["normalized_sha256"])):
                return False
        else:
            return False
        from .data import SessionBounds
        return SessionBounds(str(candle["session_id"]), candle["session_open"], candle["session_close"]).contains(opened)
    except (KeyError, ValueError, TypeError, OverflowError, ContractError, AttributeError):
        return False


def resolve_native_interval(responses, *, session):
    """Use stable covered source responses; never synthesize empty minutes."""
    if not responses:
        return {"status": "missing", "reason": "no_attempts", "candle": None}
    attempts = [r.validate() for r in responses]
    final = attempts[-1]
    opened, closed = utc(final.interval_open), utc(final.interval_close)
    scope = lambda r: (r.symbol, r.source_id, r.source_dataset, utc(r.interval_open), utc(r.interval_close))
    if len({scope(r) for r in attempts}) != 1 or closed != opened + STEP or opened != opened.floor("15min"):
        raise ContractError("Native interval responses changed exact source/symbol/window")
    if final.source_dataset != "EQUS.MINI" or final.source_id != "databento:EQUS.MINI:ohlcv-1m":
        raise ContractError("Native interval recipe requires its exact EQUS source")
    if not session.contains(opened):
        return {"status": "closed", "reason": "outside_eligible_session", "candle": None}
    if final.status == "permanent_error":
        return {"status": "failed", "reason": final.status, "candle": None}
    if final.status != "success" or not final.covered():
        return {"status": "retry", "reason": final.status, "candle": None, "retry_after_seconds": max(4.0, final.retry_after_seconds)}
    rows = _rows(final.rows, opened=opened, closed=closed)
    stable = None
    if len(rows) < 15:
        previous = attempts[-2:-1]
        stable = next((r for r in previous if r.status == "success" and r.covered() and r.attempt_id != final.attempt_id
            and (utc(final.request_started_at) - utc(r.request_ended_at)).total_seconds() >= max(4.0, r.retry_after_seconds)
            and _rows(r.rows, opened=opened, closed=closed) == rows), None)
        if stable is None:
            return {"status": "retry", "reason": "native_observation_snapshot_not_stable", "candle": None, "retry_after_seconds": max(4.0, final.retry_after_seconds)}
    if not rows:
        return {"status": "missing", "reason": "source_empty_interval", "candle": None}
    coverage = {"kind": "actual_exact_response", "status": "success", "symbol": final.symbol,
        "source_dataset": final.source_dataset, "start": final.coverage_open, "end": final.coverage_close,
        "interval_open": final.interval_open, "interval_close": final.interval_close,
        "request_started_at": final.request_started_at, "request_ended_at": final.request_ended_at,
        "attempt_id": final.attempt_id}
    if stable is not None:
        coverage["stable_previous"] = {"status": stable.status, "symbol": stable.symbol,
            "source_id": stable.source_id, "source_dataset": stable.source_dataset,
            "start": stable.coverage_open, "end": stable.coverage_close,
            "interval_open": stable.interval_open, "interval_close": stable.interval_close,
            "request_started_at": stable.request_started_at, "request_ended_at": stable.request_ended_at,
            "attempt_id": stable.attempt_id, "retry_after_seconds": stable.retry_after_seconds,
            "observations": rows}
    candle = _candle(symbol=final.symbol, source_dataset=final.source_dataset, opened=opened,
        available=utc(final.request_ended_at), kind="actual", session=session, rows=rows, coverage=coverage)
    return {"status": "observed", "reason": None, "candle": candle}


def merge_native_history_frames(original, addition):
    """Retain earliest actual evidence for the current native revision only.

    Legacy candles keep their existing latest-availability selection. Native
    identity includes every validated minute, not just aggregate OHLCV. A
    changed intervening revision ends the group, so A/B/A never reuses old A's
    clock. Inputs and immutable saved revisions are not modified.
    """
    merged=pd.concat([original,addition],ignore_index=True)
    ordered=merged.sort_values('available_at',kind='stable')
    selected=ordered.drop_duplicates('opened_at',keep='last').index.to_list()
    duplicate=ordered.loc[ordered.duplicated('opened_at',keep=False)]
    replacements={}
    def identity(row):
        if not completed_native_interval(row,from_frame=True):
            return None
        evidence=json.loads(row['evidence_json'])
        return _json({'symbol':row['symbol'],'source_id':row['source_id'],
            'source_dataset':row['source_dataset'],'native_interval_contract':CONTRACT,
            'opened_at':utc(row['opened_at']).isoformat(),
            'observations':_rows(evidence['observations'],opened=utc(row['opened_at']),
                                 closed=utc(row['opened_at'])+STEP)})
    for _,group in duplicate.groupby('opened_at',sort=False):
        latest=group.iloc[-1].to_dict()
        if latest.get('availability_kind') not in {'actual','actual_historical_recovery'}:
            continue
        final_identity=identity(latest)
        if final_identity is None:
            continue
        suffix=[]
        boundaries=[]
        for index,row in reversed(list(group.iterrows())):
            value=row.to_dict()
            if identity(value)!=final_identity:
                break
            suffix.append((index,value))
            boundary=json.loads(value['evidence_json'])['coverage'].get('revision_boundary')
            if boundary is not None:
                if (boundary.get('kind')!='observed_native_revision_v1' or not boundary.get('attempt_id')
                        or utc(boundary['available_at'])>=utc(value['available_at'])):
                    raise ContractError('Native revision boundary must precede its verified current snapshot')
                boundaries.append(boundary)
        boundary=max(boundaries,key=lambda b:utc(b['available_at'])) if boundaries else None
        chosen=group.index[-1]
        for index,value in suffix:
            if boundary is not None and utc(value['available_at'])<=utc(boundary['available_at']):
                break
            if value.get('availability_kind') in {'actual','actual_historical_recovery'}:
                chosen=index
        if boundary is not None:
            evidence=json.loads(merged.at[chosen,'evidence_json'])
            evidence['coverage']['revision_boundary']=boundary
            merged.at[chosen,'evidence_json']=_json(evidence)
        replacements[group.index[-1]]=chosen
    selected=[replacements.get(index,index) for index in selected]
    return merged.loc[selected].sort_values('opened_at').reset_index(drop=True)


def load_native_interval_history(root, symbol, *, source_dataset="EQUS.MINI",
                                 recovery_contract=None, pc=None):
    """Read exact native/raw pairs; range coverage and assumed receipt differ."""
    if source_dataset != "EQUS.MINI" or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol):
        raise ContractError("Explicit supported native history scope required")
    folder = Path(root).resolve() / "stocks" / symbol / "bars/1m/databento"
    paths = sorted((folder / "normalized").glob("*_ohlcv-1m_1m.parquet"))
    if not paths:
        raise ContractError("No canonical native minute history")
    pieces, coverages, files = [], [], []
    for path in paths:
        raw_path = folder / "raw" / (path.stem + "_raw.parquet")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, raw_path)}
        raw, normalized = pd.read_parquet(raw_path), pd.read_parquet(path)
        if "ts_event" not in raw:
            raw = raw.reset_index()
        required = {"provider_dataset": source_dataset, "source_schema": "ohlcv-1m", "symbol": symbol,
            "price_basis": "unadjusted_market_scale", "aggregation_method": "native"}
        if raw.empty or any(k not in raw or raw[k].isna().any() or not raw[k].eq(v).all() for k, v in required.items()):
            raise ContractError("Every raw observation requires exact native source provenance")
        if any(k not in raw or raw[k].isna().any() for k in ("range_start", "range_end")):
            raise ContractError("Historical native observations lack successful request coverage")
        native = raw[["ts_event", *FIELDS]].rename(columns={"ts_event": "opened_at"})
        norm = normalized[["timestamp", *FIELDS]].rename(columns={"timestamp": "opened_at"})
        native["opened_at"], norm["opened_at"] = pd.to_datetime(native.opened_at, utc=True), pd.to_datetime(norm.opened_at, utc=True)
        native = native.sort_values("opened_at").drop_duplicates().reset_index(drop=True)
        norm = norm.sort_values("opened_at").drop_duplicates().reset_index(drop=True)
        if len(native) != len(norm) or any(not (native[k].to_numpy() == norm[k].to_numpy()).all() for k in native):
            raise ContractError("Canonical observations differ from their native raw values")
        starts, ends, times = pd.to_datetime(raw.range_start, utc=True), pd.to_datetime(raw.range_end, utc=True), pd.to_datetime(raw.ts_event, utc=True)
        if starts.isna().any() or ends.isna().any() or times.isna().any() or not (starts <= times).all() or not (times + MINUTE <= ends).all():
            raise ContractError("Native observation escapes its request coverage")
        # ParquetStore merges historical fetches by minute identity. Per-row
        # range metadata proves acquired coverage, not a reconstructible response
        # snapshot. Compare the entire immutable canonical file under each range.
        snapshots = {opened: group.to_dict("records") for opened, group in
                     native.groupby(native.opened_at.dt.floor("15min"), sort=False)}
        for start, end in set(zip(starts, ends)):
            coverages.append(({"kind": "historical_native_raw_range", "status": "success", "symbol": symbol,
                "source_dataset": source_dataset, "start": start.isoformat(), "end": end.isoformat(),
                "observation_basis": "canonical_native_file_after_successful_fetch_merges",
                "raw_sha256": before[raw_path], "normalized_sha256": before[path]}, snapshots))
        for p, checksum in before.items():
            if hashlib.sha256(p.read_bytes()).hexdigest() != checksum:
                raise ContractError("Concurrent native source mutation during read")
            files.append({"path": str(p), "sha256": checksum, "bytes": p.stat().st_size})
        pieces.append(native)
    minutes = pd.concat(pieces, ignore_index=True).drop_duplicates().sort_values("opened_at")
    if minutes.opened_at.duplicated().any():
        raise ContractError("Conflicting native observations require explicit selection")
    sessions = equity_sessions(minutes.opened_at.min(), minutes.opened_at.max() + MINUTE)
    by_day = {}
    for session in sessions:
        for day in pd.date_range(utc(session.opened_at).normalize(), utc(session.closed_at).normalize(), freq="D"):
            by_day.setdefault(day, []).append(session)
    candles, uncovered = [], 0
    for opened, group in minutes.groupby(minutes.opened_at.dt.floor("15min"), sort=True):
        session = next((s for s in by_day.get(opened.normalize(), ()) if s.contains(opened)), None)
        if session is None:
            continue
        candidates = [(c, snapshots.get(opened, [])) for c, snapshots in coverages
                      if utc(c["start"]) <= opened and utc(c["end"]) >= opened + STEP]
        if not candidates:
            uncovered += 1
            continue
        coverage, first = candidates[0]
        observations = _rows(first, opened=opened, closed=opened + STEP)
        if any(_rows(rows, opened=opened, closed=opened + STEP) != observations for _, rows in candidates[1:]):
            raise ContractError("Conflicting covered native snapshots require explicit selection: " + _json({
                "opened_at": opened.isoformat(), "snapshots": [{"raw_sha256": c["raw_sha256"],
                    "observed_minutes": [utc(r["opened_at"]).isoformat() for r in rows]} for c, rows in candidates]}))
        if not observations:
            continue
        candles.append(_candle(symbol=symbol, source_dataset=source_dataset, opened=opened,
            available=opened + STEP, kind="assumed_historical_close", session=session, rows=observations, coverage=coverage))
    if not candles:
        raise ContractError("No covered nonempty native quarters")
    frame = validate_candles(pd.DataFrame(candles))
    manifest = {"symbol": symbol, "source_dataset": source_dataset, "source_schema": "ohlcv-1m",
        "native_interval_contract": CONTRACT, "availability_kind": "assumed_historical_close",
        "observation_basis": "canonical_native_file_after_successful_fetch_merges",
        "availability_limitation": "Historical receipt times are unknown; close availability is an explicit assumption",
        "minute_rows": len(minutes), "candle_rows": len(frame), "uncovered_intervals_excluded": uncovered,
        "files": files, "price_policy": "actual_native_observations_only_no_fill_no_carry",
        "first_minute_open": minutes.opened_at.min().isoformat(), "last_minute_open": minutes.opened_at.max().isoformat()}
    frame.attrs["intraday_history"] = manifest
    history = History(frame, manifest)
    if recovery_contract is not None:
        if recovery_contract != RECOVERY_CONTRACT or pc not in {"Atlas", "Scout"}:
            raise ContractError("Recovered history requires explicit version and local PC")
        from .native_recovery import merge_recovered_history
        history = merge_recovered_history(history, root=root, symbol=symbol, pc=pc)
        from .preparation_watch import overlay_saved_native_history
        history = overlay_saved_native_history(history, root=root, symbol=symbol, pc=pc)
    return history


def build_native_interval_dataset(candles, *, cutoff, reference_at, lookback_days=None,
                                  recovery_contract=None, preparation_only=False, timing_mode=LEGACY):
    if recovery_contract not in {None, RECOVERY_CONTRACT}:
        raise ContractError("Unknown native recovery projection contract")
    recovered = candles.availability_kind.eq("actual_historical_recovery")
    if recovered.any() and recovery_contract != RECOVERY_CONTRACT:
        raise ContractError("Recovered observations require explicit local recipe opt-in")
    if any(not completed_native_interval(row, from_frame=True) for row in candles.to_dict("records")):
        raise ContractError("Native interval recipe cannot consume another candle contract")
    dataset = build_observed_dataset(candles, cutoff=cutoff, reference_at=reference_at,
        lookback_days=lookback_days, preparation_only=preparation_only, timing_mode=timing_mode)
    provenance = {"feature_version": FEATURE_VERSION, "feature_policy": FEATURE_POLICY,
        "label_provenance": LABEL_PROVENANCE, "native_interval_contract": CONTRACT}
    if recovery_contract is not None:
        provenance["native_recovery_contract"] = recovery_contract
    snapshot = {**deepcopy(dataset.inference["snapshot"]), **provenance}
    inference = {**dataset.inference, **provenance, "snapshot": snapshot,
        "snapshot_sha256": hashlib.sha256(_json(snapshot).encode()).hexdigest()}
    return replace(dataset, inference=inference, manifest={**dataset.manifest, **provenance})


def candle_matches_forecast(candle, forecast):
    """A new observation contract never revises a legacy forecast's target."""
    def present(value):
        return value is not None and not (isinstance(value, (float, np.floating)) and math.isnan(float(value)))
    tag = candle.get("native_interval_contract")
    manifest = forecast.get("dataset_manifest") or {}
    if any(present(value) and value != CONTRACT for value in
           (tag, forecast.get("native_interval_contract"), manifest.get("native_interval_contract"))):
        return False
    if not present(tag):
        if present(candle.get("source_empty_constituents")):
            return False
        evidence = candle.get("evidence_json")
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (ValueError, TypeError):
                evidence = None
        if isinstance(evidence, dict) and "native_interval_contract" in evidence:
            return False
    expected = (forecast.get("feature_version") == FEATURE_VERSION or forecast.get("native_interval_contract") == CONTRACT
                or manifest.get("native_interval_contract") == CONTRACT)
    actual = tag == CONTRACT
    return expected == actual and (not actual or completed_native_interval(candle, from_frame=True))
