"""Explicit live reference projection from a genuinely acquired native close.

The source candles and empty provider responses remain unchanged. Only the
current completed reference is projected; earlier projections never become
training observations, target labels, or a chain of zero-volume indicators.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import math

import numpy as np
import pandas as pd

from .data import (FEATURE_NAMES, FEATURE_VERSION as BASE_FEATURE_VERSION,
                   IntervalResponse, SessionBounds, STEP, build_dataset, utc,
                   validate_candles)
from .native_interval import (CONTRACT as NATIVE_CONTRACT, LABEL_PROVENANCE,
                              RECOVERY_CONTRACT, build_native_interval_dataset,
                              completed_native_interval, resolve_native_interval)
from .prediction import ContractError, TARGET_VERSION
from .training_timing import LEGACY


CONTRACT = "intraday-genuine-close-reference-fill-v1"
FEATURE_VERSION = "intraday-genuine-close-reference-features-v1"
FEATURE_POLICY = "genuine_native_training_and_exact_current_close_carry_reference_v1"
PRICE_BASIS = "forward_filled_last_genuine_close"


def policy_contract():
    return {"version": CONTRACT, "trigger": "stable_covered_source_empty_interval",
            "price": "latest_genuine_native_close", "volume": "zero_derived_not_observed",
            "population": "genuine_history_plus_exact_current_reference",
            "labels": "genuine_native_reference_and_target_only"}


def feature_contract():
    return {"version": "intraday-genuine-close-reference-feature-contract-v1",
            "base_feature_version": BASE_FEATURE_VERSION, "feature_version": FEATURE_VERSION,
            "feature_policy": FEATURE_POLICY, "feature_names": list(FEATURE_NAMES),
            "label_provenance": LABEL_PROVENANCE, "synthetic_policy": "exclude",
            "target_version": TARGET_VERSION, "native_interval_contract": NATIVE_CONTRACT}


def _clean(value):
    """Canonical public evidence; omit only absent optional frame columns."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()
                if item is not None and item is not pd.NA and item is not pd.NaT
                and not (isinstance(item, (float, np.floating)) and math.isnan(float(item)))}
    if isinstance(value, (tuple, list)):
        return [_clean(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _empty_pair(responses, session):
    if session is None or len(responses) < 2:
        raise ContractError("Reference fill requires two stable covered empty responses")
    pair = tuple(responses[-2:])
    result = resolve_native_interval(pair, session=session)
    if result.get("reason") != "source_empty_interval":
        raise ContractError("Reference fill requires a stable covered source-empty interval")
    opened, closed = utc(pair[-1].interval_open), utc(pair[-1].interval_close)
    for response in pair:
        clocks = [utc(response.request_started_at), utc(response.request_ended_at),
                  utc(response.coverage_open), utc(response.coverage_close)]
        if any(pd.isna(clock) for clock in clocks) or clocks[0] < closed:
            raise ContractError("Reference fill proof must be received after interval completion")
    return pair, opened


def _reference(anchor, responses, session):
    pair, opened = _empty_pair(responses, session)
    final = pair[-1]
    available = utc(final.request_ended_at)
    anchor = _clean(anchor)
    if (not completed_native_interval(anchor)
            or any(anchor.get(key) != getattr(final, key)
                   for key in ("symbol", "source_id", "source_dataset"))
            or utc(anchor["opened_at"]) + STEP > opened
            or utc(anchor["available_at"]) > available):
        raise ContractError("Reference fill requires a prior genuinely known native price anchor")
    anchor_time = utc(anchor["opened_at"])
    age = (opened + STEP - (anchor_time + STEP)).total_seconds()
    observation_time = max(utc(row["opened_at"]) for row in json.loads(anchor["evidence_json"])["observations"])
    proof = {"contract": CONTRACT, "policy": policy_contract(), "anchor": anchor,
             "anchor_sha256": _hash(anchor), "responses": [_clean(asdict(r)) for r in pair],
             "source_empty_is_not_market_wide_no_trade": True,
             "derived_prices_not_observations": True}
    price = float(anchor["close"])
    return {"symbol": final.symbol, "source_id": final.source_id,
            "source_dataset": final.source_dataset, "opened_at": opened.isoformat(),
            "available_at": available.isoformat(), "availability_kind": "actual",
            "session_id": session.session_id, "session_open": session.opened_at,
            "session_close": session.closed_at, "open": price, "high": price,
            "low": price, "close": price, "volume": 0.0, "synthetic": True,
            "no_trade": False, "synthetic_open": True, "synthetic_close": True,
            "interval_seconds": 900, "observed_constituents": 0,
            "synthetic_constituents": 15, "source_empty_constituents": 15,
            "forward_fill_contract": CONTRACT, "reference_price_basis": PRICE_BASIS,
            "anchor_opened_at": anchor_time.isoformat(),
            "anchor_available_at": utc(anchor["available_at"]).isoformat(),
            "anchor_sha256": proof["anchor_sha256"], "anchor_age_seconds": age,
            "anchor_last_observation_at": observation_time.isoformat(),
            "anchor_observation_age_seconds": (opened + STEP - (observation_time + pd.Timedelta(minutes=1))).total_seconds(),
            "evidence_json": _json(proof)}


def make_reference(candles, responses, *, session):
    """Project one exact completed interval from its latest known real close."""
    pair, opened = _empty_pair(responses, session)
    final = pair[-1]
    eligible = []
    for value in candles.to_dict("records"):
        row = _clean(value)
        if any(row.get(key) != getattr(final, key) for key in ("symbol", "source_id", "source_dataset")):
            raise ContractError("Reference anchor history changed its genuine native source scope")
        if row.get("forward_fill_contract") is not None:
            if not verified_reference(row):
                raise ContractError("Unverified derived reference in native history")
            continue
        if (not completed_native_interval(row)
                or any(row.get(key) != getattr(final, key)
                       for key in ("symbol", "source_id", "source_dataset"))):
            raise ContractError("Reference anchor history changed its genuine native source scope")
        if utc(row["opened_at"]) + STEP <= opened and utc(row["available_at"]) <= utc(final.request_ended_at):
            eligible.append(row)
    if not eligible:
        raise ContractError("No genuinely known native close is available for reference fill")
    newest = max(utc(row["opened_at"]) for row in eligible)
    candidates = [row for row in eligible if utc(row["opened_at"]) == newest]
    if len(candidates) != 1:
        raise ContractError("Reference anchor requires one explicitly selected native revision")
    return _reference(candidates[0], pair, session)


def verified_reference(candle):
    """Reconstruct the exact derived values and verify their retained lineage."""
    try:
        value = _clean(dict(candle))
        if value.get("forward_fill_contract") != CONTRACT or "native_interval_contract" in value:
            return False
        evidence = json.loads(value["evidence_json"])
        if (evidence.get("contract") != CONTRACT or evidence.get("policy") != policy_contract()
                or evidence.get("anchor_sha256") != _hash(evidence["anchor"])
                or len(evidence["responses"]) != 2):
            return False
        responses = tuple(IntervalResponse(**item) for item in evidence["responses"])
        session = SessionBounds(str(value["session_id"]), value["session_open"], value["session_close"])
        expected = _reference(evidence["anchor"], responses, session)
        # Canonical equality rejects copied proofs, changed flags/counts/volume,
        # forged clocks, and extra interpretation fields on durable evidence.
        return _json(value) == _json(expected)
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError, ContractError):
        return False


def build_forward_fill_dataset(candles, *, cutoff, reference_at, lookback_days=None,
                               recovery_contract=None, preparation_only=False, timing_mode=LEGACY):
    """Keep genuine training unchanged and add only this cycle's projection."""
    reference = utc(reference_at)
    observed, carried, current_fill = [], [], None
    for raw in candles.to_dict("records"):
        value = _clean(raw)
        if value.get("forward_fill_contract") is not None:
            if not verified_reference(value):
                raise ContractError("Dataset has an unverified carried reference")
            carried.append(value)
            if utc(value["opened_at"]) == reference:
                if current_fill is not None:
                    raise ContractError("Dataset has duplicate exact carried references")
                current_fill = value
            continue
        if not completed_native_interval(value):
            raise ContractError("Reference fill dataset requires genuine native history")
        observed.append(value)
    if not observed:
        raise ContractError("Reference fill cannot create genuine training observations")
    genuine = validate_candles(pd.DataFrame(observed))
    genuine.attrs = deepcopy(candles.attrs)
    if any(any(row[key] != genuine.iloc[0][key] for key in ("symbol", "source_id", "source_dataset"))
           for row in carried):
        raise ContractError("Carried reference conflicts with the genuine source population")
    if current_fill is not None:
        if (genuine.opened_at.eq(reference).any()
                or any(current_fill[key] != genuine.iloc[0][key]
                       for key in ("symbol", "source_id", "source_dataset"))):
            raise ContractError("Carried reference conflicts with the genuine source population")
        if recovery_contract not in {None, RECOVERY_CONTRACT}:
            raise ContractError("Unknown native recovery projection contract")
        if genuine.availability_kind.eq("actual_historical_recovery").any() and recovery_contract != RECOVERY_CONTRACT:
            raise ContractError("Recovered observations require explicit local recipe opt-in")
        # Recheck against this supplied population: a valid older anchor cannot
        # supersede a newer genuinely known candle merely by reusing its proof.
        proof = json.loads(current_fill["evidence_json"])
        rebuilt = make_reference(genuine, [IntervalResponse(**r) for r in proof["responses"]],
            session=SessionBounds(current_fill["session_id"], current_fill["session_open"], current_fill["session_close"]))
        if _json(rebuilt) != _json(current_fill):
            raise ContractError("Carried reference is not anchored to the latest genuine input")
        projected = pd.concat([genuine, pd.DataFrame([current_fill])], ignore_index=True)
        projected.attrs = deepcopy(candles.attrs)
        dataset = build_dataset(projected, cutoff=cutoff, reference_at=reference,
            lookback_days=lookback_days, synthetic_policy="exclude", preparation_only=preparation_only,
            timing_mode=timing_mode)
    else:
        dataset = build_native_interval_dataset(genuine, cutoff=cutoff, reference_at=reference,
            lookback_days=lookback_days, recovery_contract=recovery_contract,
            preparation_only=preparation_only, timing_mode=timing_mode)
    contract = feature_contract()
    provenance = {key: contract[key] for key in
                  ("feature_version", "feature_policy", "label_provenance", "native_interval_contract")}
    provenance.update(reference_fill_policy=policy_contract(),
        reference_price_basis=PRICE_BASIS if current_fill is not None else "observed_native",
        reference_fill_reference=current_fill)
    if recovery_contract is not None:
        provenance["native_recovery_contract"] = recovery_contract
    snapshot = {**deepcopy(dataset.inference["snapshot"]), **provenance}
    inference = {**dataset.inference, **provenance, "snapshot": snapshot,
                 "snapshot_sha256": _hash(snapshot)}
    manifest = {**dataset.manifest, **provenance,
                "training_population": "genuine_native_observations_only",
                "older_derived_references_excluded": sum(
                    bool(_clean(r).get("forward_fill_contract")) and utc(r["opened_at"]) != reference
                    for r in candles.to_dict("records"))}
    return replace(dataset, inference=inference, manifest=manifest)
