"""Bounded cross-provider gap corroboration; never a market-wide no-trade proof.

This pure module neither requests data nor fills a price frame. Callers retain
and verify the referenced raw receipts. A derived carry is a separately marked
estimate, not an observed price and not admissible to an observed-only target.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re


POLICY = "cross-provider-probable-no-trade-v1"
PROVIDERS = {"databento": "databento", "schwab": "schwab", "schwabapi": "schwab",
             "fmp": "fmp", "financialmodelingprep": "fmp"}
MARKERS = ("synthetic", "is_synthetic", "is_derived", "no_trade", "is_no_trade",
           "source_empty", "is_source_empty", "synthetic_open", "synthetic_close")


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Gap evidence requires timezone-aware clocks")
    return result.astimezone(timezone.utc)


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a price/volume")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Gap evidence requires finite values")
    return result


def _native(record):
    if not isinstance(record, dict) or not all(key not in record or record[key] is False for key in MARKERS):
        return False
    count = record.get("synthetic_constituents", 0)
    return (not isinstance(count, bool) and isinstance(count, (int, float))
            and count == 0)


def _reference(item):
    if not isinstance(item.get("receipt_id"), str) or not item["receipt_id"]:
        raise ValueError("Missing immutable receipt identity")
    if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("payload_sha256", ""))):
        raise ValueError("Missing immutable payload hash")
    return {key: item.get(key) for key in ("provider", "source", "dataset", "receipt_id", "payload_sha256")}


def assess_interval_gap(symbol, start, end, *, evidence, anchor, as_of,
                        max_context_age_seconds=900, absolute_tolerance=.01,
                        relative_tolerance=.0001):
    """Assess one completed interval from independently retained provider calls.

    ``response_complete`` means the complete response was parsed, NOT that the
    provider covers all markets. ``records`` contains native bars overlapping
    this interval. An empty response is only a provider-specific absence vote.
    Positive native activity always defeats a probable-no-trade conclusion.
    A recent genuine trade anchors a probable carry. Optional native contexts
    can contradict it, but absent later activity is not a new waiting condition.
    """
    start, end, as_of = map(_time, (start, end, as_of))
    max_context_age_seconds = _number(max_context_age_seconds)
    absolute_tolerance, relative_tolerance = map(_number, (absolute_tolerance, relative_tolerance))
    if not start < end <= as_of or not symbol:
        raise ValueError("Gap interval must be completed before its decision")
    if (max_context_age_seconds <= 0 or absolute_tolerance < 0
            or relative_tolerance < 0 or len(evidence) > 32):
        raise ValueError("Invalid bounded corroboration policy")
    anchor_price = _number(anchor["price"])
    anchor_clock, anchor_received = map(_time, (anchor["timestamp"], anchor["received_at"]))
    anchor_ok = (anchor_price > 0 and anchor.get("kind") == "observed_trade_close"
                 and _native(anchor) and bool(anchor.get("receipt_id")) and bool(anchor.get("source"))
                 and bool(re.fullmatch(r"[0-9a-f]{64}", str(anchor.get("payload_sha256", ""))))
                 and anchor_clock <= start and 0 <= (start - anchor_clock).total_seconds() <= max_context_age_seconds
                 and anchor_clock <= anchor_received <= as_of)
    tolerance = max(float(absolute_tolerance), anchor_price * float(relative_tolerance))
    providers, diagnostics, references, candidates, limitations = set(), [], [], [], []
    proof_clocks = [end, anchor_received]
    contradiction = False
    for item in evidence:
        provider = PROVIDERS.get(str(item.get("provider", "")).lower())
        source = str(item.get("source", ""))
        reasons, observed = [], []
        try:
            reference = _reference(item)
            received = _time(item["received_at"])
            request_start, request_end = map(_time, (item["request_start"], item["request_end"]))
            seconds = _number(item["native_interval_seconds"])
            if seconds not in (1, 60, 300):
                reasons.append("unsupported_native_granularity")
            elif (start.timestamp() % seconds or end.timestamp() % seconds
                  or (end - start).total_seconds() < seconds):
                reasons.append("interval_not_whole_native_bars")
            if provider is None or not source or not item.get("dataset"):
                reasons.append("unidentified_provider_or_source")
            if item.get("symbol") != symbol:
                reasons.append("symbol_mismatch")
            if item.get("status") != "ok" or item.get("response_complete") is not True:
                reasons.append("failed_or_incomplete_response")
            if not request_start <= start < end <= request_end:
                reasons.append("request_does_not_cover_interval")
            if not end <= received <= as_of:
                reasons.append("receipt_not_available_at_decision")
            if reasons:
                diagnostics.append({"provider": provider, "source": source, "reasons": sorted(set(reasons))})
                continue
            records = item.get("records")
            if not isinstance(records, list) or len(records) > 10000:
                raise ValueError("Invalid bounded native response")
            by_clock = {}
            for row in records:
                clock = _time(row["timestamp"])
                if not start <= clock < end:
                    reasons.append("record_outside_exact_interval")
                    continue
                prices = tuple(_number(row[key]) for key in ("open", "high", "low", "close"))
                volume = _number(row["volume"])
                if (not _native(row) or min(prices) <= 0 or volume < 0
                        or prices[1] < max(prices) or prices[2] > min(prices)
                        or clock.timestamp() % seconds or clock.timestamp() + seconds > end.timestamp()):
                    reasons.append("invalid_or_non_native_record")
                    continue
                identity = (*prices, volume)
                repeated = clock in by_clock
                if clock in by_clock and by_clock[clock] != identity:
                    reasons.append("conflicting_native_records")
                    contradiction = True
                by_clock[clock] = identity
                if volume > 0:
                    contradiction = True
                    if not repeated:
                        observed.append({**row, "timestamp": clock.isoformat()})
                elif max(abs(p - anchor_price) for p in prices) > tolerance:
                    contradiction = True
                    reasons.append("zero_volume_price_disagrees")
            if observed:
                reasons.append("observed_activity_present")
            context = item.get("price_context", {})
            for side in ("before", "after"):
                record = context.get(side, {})
                try:
                    clock, receipt = map(_time, (record["timestamp"], record["received_at"]))
                    price = _number(record["price"])
                    distance = (start - clock).total_seconds() if side == "before" else (clock - end).total_seconds()
                    if not _native(record) or price <= 0 or not 0 <= distance <= max_context_age_seconds or not clock <= receipt <= received:
                        limitations.append({"provider": provider, "side": side, "reason": "missing_stale_or_future_bracket"})
                        continue
                    if abs(price - anchor_price) > tolerance:
                        reasons.append("bracketing_prices_disagree")
                        contradiction = True
                    proof_clocks.append(receipt)
                except (ValueError, TypeError, KeyError, OverflowError):
                    limitations.append({"provider": provider, "side": side, "reason": "missing_stale_or_future_bracket"})
            references.append({**reference, "canonical_provider": provider,
                               "received_at": received.isoformat(), "request_start": request_start.isoformat(),
                               "request_end": request_end.isoformat(), "native_interval_seconds": seconds,
                               "coverage_scope": item.get("coverage_scope", "provider-specific; upstream overlap unknown")})
            proof_clocks.append(received)
            if not reasons:
                providers.add(provider)
            if observed and not any(r in reasons for r in ("conflicting_native_records", "receipt_not_available_at_decision",
                    "failed_or_incomplete_response", "symbol_mismatch", "unidentified_provider_or_source",
                    "invalid_or_non_native_record", "request_does_not_cover_interval", "record_outside_exact_interval")):
                candidates.append({"provider": provider, "source": source, "dataset": item["dataset"],
                                   "receipt_id": item["receipt_id"], "payload_sha256": item["payload_sha256"],
                                   "received_at": received.isoformat(), "native_interval_seconds": seconds,
                                   "records": observed})
        except (ValueError, TypeError, KeyError, OverflowError):
            reasons.append("malformed_evidence")
        diagnostics.append({"provider": provider, "source": source, "reasons": sorted(set(reasons))})
    allowed = anchor_ok and len(providers) == 3 and not contradiction
    status = "observed_recovery" if candidates else "conflict" if contradiction else "probable_no_trade" if allowed else "incomplete"
    result = {"policy": POLICY, "symbol": symbol, "start": start.isoformat(), "end": end.isoformat(),
              "as_of": as_of.isoformat(), "status": status, "derived_allowed": allowed,
              "anchor_valid": anchor_ok, "anchor": dict(anchor), "agreeing_providers": sorted(providers),
              "price_tolerance": tolerance, "max_context_age_seconds": max_context_age_seconds,
              "absolute_tolerance": absolute_tolerance, "relative_tolerance": relative_tolerance,
              "available_at": max(proof_clocks).isoformat() if allowed else None,
              "confidence_basis": ("three successful completed-interval provider responses without native activity, recent genuine anchor, no available contradictory context; probable absence, not complete-market proof" if allowed else
                                   "probable absence not established; inspect status, provider votes, anchor validity and diagnostics"),
              "provider_independence": "not established; upstream venues/vendors may overlap; no multiplied probability",
              "source_references": references, "diagnostics": diagnostics, "limitations": limitations,
              "observed_candidates": candidates, "evidence": list(evidence)}
    result["assessment_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return result


def derive_gap_carry(assessment):
    """Construct a flagged estimate only from this policy's eligible assessment."""
    body = {key: value for key, value in assessment.items() if key != "assessment_sha256"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    if (assessment.get("policy") != POLICY or not assessment.get("derived_allowed")
            or assessment.get("status") != "probable_no_trade" or digest != assessment.get("assessment_sha256")):
        raise ValueError("No eligible unmodified corroboration assessment")
    # Re-run the eligibility rules from the frozen native proof, rather than
    # accepting decision flags whose digest a caller could simply recompute.
    expected = assess_interval_gap(assessment["symbol"], assessment["start"], assessment["end"],
        evidence=assessment["evidence"], anchor=assessment["anchor"], as_of=assessment["as_of"],
        max_context_age_seconds=assessment["max_context_age_seconds"],
        absolute_tolerance=assessment["absolute_tolerance"], relative_tolerance=assessment["relative_tolerance"])
    if expected["assessment_sha256"] != digest or not expected["derived_allowed"]:
        raise ValueError("Corroboration decision differs from frozen native evidence")
    price = float(assessment["anchor"]["price"])
    return {"symbol": assessment["symbol"], "timestamp": assessment["start"], "interval_end": assessment["end"],
            "open": price, "high": price, "low": price, "close": price, "volume": 0,
            "is_synthetic": True, "is_derived": True, "observation_kind": "corroborated_price_carry_estimate",
            "derivation_policy": POLICY, "available_at": assessment["available_at"],
            "assessment_sha256": digest, "source_references": assessment["source_references"],
            "anchor_reference": assessment["anchor"], "market_wide_no_trade_verified": False}
