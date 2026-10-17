"""Versioned source-observation populations; absence is never a market-wide fact.

No provider client, file writes, fitting, or runtime state is created here.
Historical request ranges describe saved source coverage and explicitly retain
assumed publication timing. They do not certify complete exchange/SIP activity.
"""
from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .prediction import ContractError, STEP, instant

POPULATION_CONTRACT = "intraday-verified-source-observations-v1"
NO_TRADE_EVIDENCE_VERSION = "intraday-independent-complete-scope-no-trade-v1"


def check_population_contract(value):
    if value not in {None, POPULATION_CONTRACT}:
        raise ContractError("Unsupported intraday population contract")
    return value


def _utc(value):
    return pd.Timestamp(instant(value))


def independently_verified_no_trade(proof, *, symbol, source_id, source_dataset,
                                    opened_at, closed_at, available_at) -> bool:
    """Validate an explicit independent complete-market no-trade assertion.

    The current MINI adapters never produce this proof. A provider omission
    string, advertised dataset range, repeated emptiness, or continuity alone
    cannot satisfy it. Future adapters must retain the independently sourced
    complete-scope evidence identified here, not manufacture these fields.
    """
    if not isinstance(proof, Mapping):
        return False
    try:
        return (proof.get("schema_version") == NO_TRADE_EVIDENCE_VERSION
                and proof.get("scope") == "complete_market_trade_activity"
                and proof.get("coverage_complete") is True
                and proof.get("confirmed_no_trades") is True
                and proof.get("symbol") == symbol
                and proof.get("source_dataset") == source_dataset
                and isinstance(proof.get("evidence_id"), str) and bool(proof["evidence_id"].strip())
                and isinstance(proof.get("verifier_source_id"), str)
                and bool(proof["verifier_source_id"].strip())
                and proof["verifier_source_id"] != source_id
                and _utc(proof["interval_open"]) <= _utc(opened_at)
                and _utc(proof["interval_close"]) >= _utc(closed_at)
                and _utc(proof["interval_close"]) <= _utc(proof["verified_at"]) <= _utc(available_at))
    except (KeyError, TypeError, ValueError, ContractError):
        return False


def response_no_trade_verified(response, *, opened_at=None, closed_at=None):
    return independently_verified_no_trade(response.no_trade_scope_evidence,
        symbol=response.symbol, source_id=response.source_id, source_dataset=response.source_dataset,
        opened_at=opened_at or response.interval_open, closed_at=closed_at or response.interval_close,
        available_at=response.request_ended_at)


def coverage_ranges(records: Sequence[Mapping]) -> tuple[dict, ...]:
    """Merge overlapping successful-source ranges, retaining their basis/IDs."""
    ordered = []
    for item in records:
        op, cl = _utc(item["opened_at"]), _utc(item["closed_at"])
        if cl <= op or not item.get("basis"):
            raise ContractError("Source coverage requires a positive explicit range and basis")
        ordered.append((op, cl, dict(item)))
    ordered.sort(key=lambda item: item[0])
    merged = []
    for op, cl, evidence in ordered:
        if merged and op <= merged[-1][1]:
            previous = merged[-1]
            previous[1] = max(cl, previous[1])
            previous[2].append(evidence)
        else:
            merged.append([op, cl, [evidence]])
    return tuple({"opened_at": op.isoformat(), "closed_at": cl.isoformat(),
                  "basis": "union_of_recorded_source_request_ranges", "range_count": len(evidence),
                  "ranges_sha256": hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(",", ":")).encode()).hexdigest()}
                 for op, cl, evidence in merged)


def clip_source_ranges(records, *, start=None, end=None):
    """Do not claim complete quarters after native rows were window-filtered."""
    result = []
    for row in records:
        op, cl = _utc(row["opened_at"]), _utc(row["closed_at"])
        op = max(op, _utc(start)) if start is not None else op
        cl = min(cl, _utc(end)) if end is not None else cl
        if op < cl:
            result.append({**row,"opened_at":op.isoformat(),"closed_at":cl.isoformat()})
    return result


def verified_source_observation(candle: Mapping) -> bool:
    """Structural identity and coverage proof for one nonempty source quarter."""
    try:
        if candle.get("population_contract") != POPULATION_CONTRACT:
            return False
        if any(not isinstance(candle.get(key), (bool, np.bool_)) or bool(candle[key])
               for key in ("synthetic", "no_trade")):
            return False
        prices = [float(candle[key]) for key in ("open", "high", "low", "close", "volume")]
        op_price, high, low, close, volume = prices
        if (not all(math.isfinite(v) for v in prices) or min(prices[:4]) <= 0 or volume < 0
                or high < max(op_price, close, low) or low > min(op_price, close)):
            return False
        if candle.get("interval_seconds") != 900 or candle.get("synthetic_constituents") != 0:
            return False
        observed, empty = candle["observed_constituents"], candle["source_empty_constituents"]
        if (isinstance(observed, (bool, np.bool_)) or isinstance(empty, (bool, np.bool_))
                or not float(observed).is_integer() or not float(empty).is_integer()
                or not 1 <= observed <= 15 or empty != 15-observed):
            return False
        op, av = _utc(candle["opened_at"]), _utc(candle["available_at"])
        if op != op.floor("15min") or av < op+STEP:
            return False
        evidence = candle["evidence_json"]
        evidence = evidence if isinstance(evidence, Mapping) else json.loads(evidence)
        if evidence.get("aggregation") != POPULATION_CONTRACT or evidence.get("source_id") != candle["source_id"] or evidence.get("source_dataset") != candle["source_dataset"]:
            return False
        coverage = evidence["source_coverage"]
        if not coverage.get("basis") or _utc(coverage["opened_at"]) > op or _utc(coverage["closed_at"]) < op+STEP:
            return False
        times = [_utc(row["opened_at"]) for row in evidence["observations"]]
        if (len(times) != observed or len(set(times)) != len(times) or times != sorted(times)
                or any(t < op or t+pd.Timedelta(minutes=1) > op+STEP or t != t.floor("min") for t in times)):
            return False
        if _utc(candle["native_first_opened_at"]) != times[0] or _utc(candle["native_last_opened_at"]) != times[-1]:
            return False
        for row in evidence["observations"]:
            if row.get("source_id") != candle["source_id"] or row.get("synthetic") is not False:
                return False
            if _utc(row["available_at"]) > av or _utc(row["available_at"]) < _utc(row["opened_at"])+pd.Timedelta(minutes=1):
                return False
            vals = [float(row[key]) for key in ("open","high","low","close","volume")]
            a,h,l,c,v = vals
            if not all(math.isfinite(x) for x in vals) or min(vals[:4])<=0 or v<0 or h<max(a,c,l) or l>min(a,c):
                return False
        native = evidence["observations"]
        aggregates = {"open": native[0]["open"], "high": max(row["high"] for row in native),
                      "low": min(row["low"] for row in native), "close": native[-1]["close"],
                      "volume": sum(row["volume"] for row in native)}
        if any(not math.isclose(float(candle[key]), float(value), rel_tol=1e-12, abs_tol=1e-12)
               for key, value in aggregates.items()):
            return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError, ContractError):
        return False


def aggregate_source_minutes(minutes: pd.DataFrame, *, sessions, source_coverage):
    """Aggregate genuine observations only, within a covered exact quarter.

    First/last native observation define interval open/close prices. An empty
    quarter stays unavailable; an absent minute is not a synthetic observation.
    """
    from .data import MINUTE, validate_candles, _require_native_observations
    _require_native_observations(minutes)
    frame = validate_candles(minutes, duration=MINUTE)
    if not frame.empty and (frame.source_id.isna().any() or frame.source_id.nunique() != 1):
        raise ContractError("Source-observation aggregation must preserve one source identity")
    if frame.synthetic.any():
        raise ContractError("Source-observation aggregation cannot consume synthetic minutes")
    ranges = coverage_ranges(source_coverage)
    records = []
    incomplete = uncovered = 0
    # Only represented quarters are traversed. Empty counts are source-range
    # denominators in the manifest, not manufactured prices/candles.
    sessions_by_day = {}
    for session in sessions:
        for day in pd.date_range(_utc(session.opened_at).normalize(),
                                 (_utc(session.closed_at)-pd.Timedelta(nanoseconds=1)).normalize(),freq="D"):
            sessions_by_day.setdefault(day, []).append(session)
    expected = set()
    for session in sessions:
        for quarter in pd.date_range(_utc(session.opened_at),_utc(session.closed_at),freq="15min",inclusive="left"):
            if any(_utc(r["opened_at"]) <= quarter and quarter+STEP <= _utc(r["closed_at"]) for r in ranges):
                expected.add(quarter)
    for slot, group in frame.groupby(frame.opened_at.dt.floor("15min"),sort=True):
        session = next((s for s in sessions_by_day.get(slot.normalize(),()) if s.contains(slot)),None)
        if session is None:
            continue
        proof = next((r for r in ranges if _utc(r["opened_at"]) <= slot and slot+STEP <= _utc(r["closed_at"])),None)
        if proof is None:
            uncovered += 1
            continue
        observations = [{"opened_at": row.opened_at.isoformat(),"available_at": row.available_at.isoformat(),
                         "source_id": row.source_id,"synthetic":False,
                         **{key:float(getattr(row,key)) for key in ("open","high","low","close","volume")}}
                        for row in group.itertuples()]
        count = len(group)
        incomplete += count < 15
        evidence = {"aggregation":POPULATION_CONTRACT,"source_id":str(group.source_id.iloc[0]),
                    "source_dataset":str(group.source_dataset.iloc[0]),"source_coverage":proof,
                    "observations":observations,"absence_policy":"unobserved_not_no_trade"}
        records.append(dict(symbol=str(group.symbol.iloc[0]),opened_at=slot,
            available_at=max(group.available_at.max(),slot+STEP),
            availability_kind="assumed_historical_close" if group.availability_kind.ne("actual").any() else "actual",
            open=float(group.open.iloc[0]),high=float(group.high.max()),low=float(group.low.min()),
            close=float(group.close.iloc[-1]),volume=float(group.volume.sum()),
            source_id=str(group.source_id.iloc[0]),source_dataset=str(group.source_dataset.iloc[0]),
            session_id=session.session_id,session_open=session.opened_at,session_close=session.closed_at,
            synthetic=False,no_trade=False,synthetic_open=False,synthetic_close=False,
            observed_constituents=count,synthetic_constituents=0,source_empty_constituents=15-count,
            interval_seconds=900,population_contract=POPULATION_CONTRACT,
            native_first_opened_at=group.opened_at.iloc[0].isoformat(),
            native_last_opened_at=group.opened_at.iloc[-1].isoformat(),
            evidence_json=json.dumps(evidence,sort_keys=True,separators=(",", ":"))))
    columns = ["symbol","opened_at","available_at","availability_kind","open","high","low","close","volume",
               "source_id","source_dataset","session_id","session_open","session_close","synthetic","no_trade",
               "synthetic_open","synthetic_close","observed_constituents","synthetic_constituents",
               "source_empty_constituents","interval_seconds","population_contract","native_first_opened_at",
               "native_last_opened_at","evidence_json"]
    return pd.DataFrame(records,columns=columns),dict(population_contract=POPULATION_CONTRACT,
        covered_intervals=len(expected),observed_intervals=len(records),sparse_intervals=incomplete,
        empty_intervals=len(expected)-len(records),uncovered_observed_intervals=uncovered,
        missing_policy="empty_or_uncovered_unavailable_no_carry",availability_limitation="source_range_is_not_market_wide_completeness")


def population_frame(candles: pd.DataFrame, population_contract=None):
    """Select only this population's proven observations before features/labels."""
    check_population_contract(population_contract)
    if population_contract is None:
        return candles
    mask = [verified_source_observation(row) for row in candles.to_dict("records")]
    result = candles.loc[mask].copy().reset_index(drop=True)
    result.attrs = dict(candles.attrs)
    return result


def load_data_policy(root):
    """Read this PC's explicit operating binding; absent means legacy policy.

    Unknown extra keys are retained for independent input adapters. This helper
    does not authorize anything or turn a coordination notice into authority.
    """
    path = Path(root) / "intraday/data-policy.json"
    if not path.is_file():
        return {"population_contract":None,"market_inputs_enabled":False}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value,dict) or value.get("schema_version") != "intraday-data-policy-v1":
        raise ContractError("Invalid local intraday data policy")
    check_population_contract(value.get("population_contract"))
    if not isinstance(value.get("market_inputs_enabled",False), bool):
        raise ContractError("Local market_inputs_enabled must be boolean")
    return value
