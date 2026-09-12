"""Versioned research policy. Scores are analyst judgments, not probabilities."""
from __future__ import annotations

import math
import re
from datetime import date
from urllib.parse import urlparse

import pandas as pd

VERSION = "opportunity-research-v1"
WEIGHTS = {"valuation": 30, "product_economics": 25, "catalysts": 20,
           "financial_resilience": 15, "evidence": 10}
GATES = ("identity_verified", "financials_reconciled", "liquidity_sufficient",
         "financed_through_catalyst", "primary_corroboration", "countercase_complete")
STATUSES = {"active", "strengthened", "weakened", "withdrawn", "target_reached", "falsified"}


def timestamp(value: object) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result.tz_convert("UTC")


def number(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, (bool, str)) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"Invalid {name}")
    return result


def symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", value):
        raise ValueError("Use a valid uppercase exchange ticker")
    return value


def text(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value.strip()) < 8:
        raise ValueError(f"{name} needs an explanation")
    return value.strip()


def sources_valid(sources: list[dict], cutoff: pd.Timestamp) -> set[str]:
    ids = set()
    for source in sources:
        identity = source["id"]
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("Source IDs must be unique nonempty strings")
        ids.add(identity)
        parsed = urlparse(source["url"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Sources need public HTTPS URLs without credentials")
        if any(word in parsed.query.lower() for word in ("apikey=", "api_key=", "token=")):
            raise ValueError("Source URLs must not contain credentials")
        text(source["title"], "source title")
        text(source["supports"], "source evidence")
        if source["kind"] not in {"filing", "company", "independent", "market_data", "social"}:
            raise ValueError("Invalid source kind")
        if timestamp(source["accessed_at"]) > cutoff:
            raise ValueError("Source access occurs after the research cutoff")
        if source.get("published_at") and timestamp(source["published_at"]) > cutoff:
            raise ValueError("Source was published after the research cutoff")
    return ids


def valuation_case(case: dict) -> float:
    """Amounts and shares use matching units (USD millions / million shares)."""
    shares = number(case["diluted_shares"], "diluted shares", minimum=1e-9)
    net_debt = number(case["net_debt"], "net debt")
    text(case["assumptions"], "valuation assumptions")
    if case["method"] == "dcf":
        flows = [number(x, "FCFF") for x in case["fcff"]]
        if not 3 <= len(flows) <= 15:
            raise ValueError("DCF requires 3-15 annual FCFF forecasts")
        rate = number(case["discount_rate"], "discount rate", minimum=0.001)
        growth = number(case["terminal_growth"], "terminal growth")
        if not -0.02 <= growth <= 0.04 or rate <= growth or flows[-1] <= 0:
            raise ValueError("DCF needs sustainable growth below the discount rate and positive terminal FCFF")
        enterprise = sum(flow / (1 + rate) ** year for year, flow in enumerate(flows, 1))
        enterprise += flows[-1] * (1 + growth) / (rate - growth) / (1 + rate) ** len(flows)
    elif case["method"] == "comparables":
        if case["metric"] not in {"revenue", "ebit", "ebitda", "fcff"}:
            raise ValueError("Comparables must use an enterprise-value operating metric")
        enterprise = number(case["forward_metric"], "forward metric", minimum=0) * number(
            case["multiple"], "enterprise multiple", minimum=0)
    else:
        raise ValueError("Valuation method must be dcf or comparables")
    return max(0.0, (enterprise - net_debt) / shares)


def assess(candidate: dict, cutoff: pd.Timestamp, latest_session: str) -> dict:
    symbol(candidate["symbol"])
    symbol(candidate["sector_benchmark"])
    if candidate["sector_benchmark"] == candidate["symbol"]:
        raise ValueError("The sector benchmark cannot be the candidate")
    if not re.fullmatch(r"CIK\d{10}:[A-Z0-9_-]+", candidate["security_id"]):
        raise ValueError("security_id must be CIK plus a stable share-class identifier")
    if candidate["track"] not in {"established", "emerging"} or candidate["currency"] != "USD":
        raise ValueError("V1 covers USD US-listed established/emerging equities")
    if candidate["decision"] not in {"recommend", "watch", "pass"}:
        raise ValueError("Invalid research decision")
    price = number(candidate["reference_price"], "reference price", minimum=1e-9)
    date.fromisoformat(candidate["reference_session"])
    ids = sources_valid(candidate["sources"], cutoff)
    for field in ("company_name", "business", "product_evidence", "mispricing", "reverse_valuation",
                  "countercase", "invalidation", "financing", "benchmark_rationale", "accounting_adjustments"):
        text(candidate[field], field)
    total = 0.0
    for dimension, weight in WEIGHTS.items():
        score = candidate["scores"][dimension]
        rating = number(score["rating"], dimension, minimum=0)
        if rating > 5:
            raise ValueError("Score ratings must be between 0 and 5")
        text(score["rationale"], dimension)
        if not score["source_ids"] or not set(score["source_ids"]).issubset(ids):
            raise ValueError("Each score needs valid evidence references")
        total += weight * rating / 5
    failures = []
    for gate in GATES:
        item = candidate["gates"][gate]
        if not isinstance(item["passed"], bool):
            raise ValueError("Gate passed values must be boolean")
        text(item["rationale"], gate)
        if not item["source_ids"] or not set(item["source_ids"]).issubset(ids):
            raise ValueError("Each gate needs valid evidence references")
        if not item["passed"]:
            failures.append(gate)
    if candidate["reference_session"] != latest_session:
        failures.append("stale_reference_price")
    source_kinds = {s["kind"] for s in candidate["sources"]}
    if len(ids) < 4 or not {"filing", "company", "independent", "market_data"}.issubset(source_kinds):
        failures.append("insufficient_source_diversity")
    if number(candidate["median_daily_dollar_volume_20d"], "liquidity", minimum=0) < 2_000_000:
        failures.append("liquidity_below_2m")
    if candidate["track"] == "emerging" and number(candidate["cash_runway_months"], "cash runway", minimum=0) < 18:
        failures.append("cash_runway_below_18_months")
    if not candidate["catalysts"]:
        failures.append("no_catalyst")
    for catalyst in candidate["catalysts"]:
        deadline = date.fromisoformat(catalyst["due_date"])
        if not cutoff.date() < deadline <= (cutoff + pd.DateOffset(months=12)).date():
            raise ValueError("Catalysts must be future events within twelve months")
        for field in ("milestone", "success_test", "failure_consequence"):
            text(catalyst[field], field)
        if not catalyst["source_ids"] or not set(catalyst["source_ids"]).issubset(ids):
            raise ValueError("Catalysts need evidence references")
    values = None
    if candidate.get("valuation") is not None:
        values = {name: valuation_case(candidate["valuation"][name]) for name in ("bear", "base", "bull")}
        if not values["bear"] <= values["base"] <= values["bull"] or values["base"] <= 0:
            raise ValueError("Present valuation scenarios must be ordered and base value positive")
    else:
        failures.append("valuation_unavailable")
    forecasts = candidate["price_targets"]
    for horizon in (("6m", "12m") if forecasts is not None else ()):
        targets = [number(forecasts[horizon][name], f"{horizon} target", minimum=0)
                   for name in ("bear", "base", "bull")]
        if targets != sorted(targets):
            raise ValueError("Price-target scenarios must be ordered")
        text(forecasts[horizon]["rationale"], "price target rationale")
    discount = 1 - price / values["base"] if values else None
    hurdle = 0.30 if candidate["track"] == "emerging" else 0.20
    if discount is not None and discount < hurdle:
        failures.append("insufficient_valuation_discount")
    if forecasts is None:
        failures.append("price_targets_unavailable")
    elif forecasts["12m"]["base"] / price - 1 < hurdle:
        failures.append("insufficient_12m_base_upside")
    if total < 70 or any(candidate["scores"][d]["rating"] < 3 for d in ("valuation", "product_economics", "catalysts", "evidence")):
        failures.append("score_below_threshold")
    if candidate["decision"] == "recommend" and failures:
        raise ValueError(f"{candidate['symbol']} cannot be recommended: {', '.join(failures)}")
    return {"policy_version": VERSION, "score": round(total, 2), "fair_values": values,
            "discount_to_base_value": discount, "failed_gates": failures}
