"""Offline causal, granularity and provider-corroboration boundaries."""
from datetime import datetime, timedelta, timezone

import pytest

from ml.market_gap_evidence import assess_interval_gap, derive_gap_carry


START = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)
NOW = END + timedelta(minutes=1)


def proof(provider, *, interval=60):
    return {"provider": provider, "source": provider + ":native", "dataset": provider + ".NATIVE",
            "symbol": "TEST", "receipt_id": provider + "-receipt", "payload_sha256": "a" * 64,
            "status": "ok", "response_complete": True, "request_start": START.isoformat(),
            "request_end": END.isoformat(), "received_at": NOW.isoformat(),
            "native_interval_seconds": interval, "records": [], "price_context": {
                "before": {"timestamp": START.isoformat(), "received_at": NOW.isoformat(), "price": 100},
                "after": {"timestamp": END.isoformat(), "received_at": NOW.isoformat(), "price": 100}}}


def anchor():
    return {"timestamp": START.isoformat(), "received_at": START.isoformat(), "price": 100,
            "receipt_id": "native-anchor", "source": "databento:native", "kind": "observed_trade_close",
            "is_synthetic": False, "payload_sha256": "b" * 64}


def assess(items=None, **kwargs):
    return assess_interval_gap("TEST", START, kwargs.pop("end", END),
        evidence=items if items is not None else [proof("databento"), proof("schwab"), proof("fmp", interval=300)],
        anchor=kwargs.pop("anchor", anchor()), as_of=kwargs.pop("as_of", NOW), **kwargs)


def bar(*, volume=1, price=100):
    return {"timestamp": START.isoformat(), "open": price, "high": price, "low": price,
            "close": price, "volume": volume}


def test_three_successes_create_explicit_derived_estimate_with_actual_availability():
    result = assess()
    assert result["status"] == "probable_no_trade"
    assert result["agreeing_providers"] == ["databento", "fmp", "schwab"]
    carry = derive_gap_carry(result)
    assert carry["volume"] == 0 and carry["is_synthetic"] and carry["is_derived"]
    assert carry["available_at"] == NOW.isoformat()
    assert carry["market_wide_no_trade_verified"] is False
    assert len(carry["source_references"]) == 3 and "no_trade" not in carry


@pytest.mark.parametrize("change", [
    {"status": "error"}, {"response_complete": False}, {"received_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"request_start": (START + timedelta(minutes=1)).isoformat()}, {"payload_sha256": "missing"},
    {"symbol": "OTHER"}, {"provider": "unverified_vendor"},
])
def test_failure_incomplete_late_or_unidentified_response_is_not_a_vote(change):
    items = [proof("databento"), proof("schwab"), proof("fmp", interval=300)]
    items[-1].update(change)
    result = assess(items)
    assert not result["derived_allowed"]
    with pytest.raises(ValueError):
        derive_gap_carry(result)


def test_provider_alias_or_multiple_databento_venues_are_not_independent_votes():
    result = assess([proof("databento"), proof("schwab"), proof("schwabapi")])
    assert not result["derived_allowed"] and len(result["agreeing_providers"]) == 2
    items = [proof("databento"), proof("databento"), proof("fmp")]
    items[1]["source"] = "databento:other_venue"
    assert not assess(items)["derived_allowed"]


def test_positive_trade_vetoes_empty_votes_and_returns_one_source_native_records():
    items = [proof("databento"), proof("schwab"), proof("fmp", interval=300)]
    items[1]["records"] = [bar()]
    items[1].pop("price_context")  # Observed recovery does not require no-trade brackets.
    result = assess(items)
    assert result["status"] == "observed_recovery" and not result["derived_allowed"]
    assert result["observed_candidates"][0]["source"] == "schwab:native"
    assert result["observed_candidates"][0]["records"][0]["volume"] == 1


def test_native_five_minute_evidence_cannot_authorize_one_minute_carry():
    assert not assess(end=START + timedelta(minutes=1))["derived_allowed"]


@pytest.mark.parametrize("defect", ["disagree", "missing", "stale", "future"])
def test_optional_context_vetoes_only_when_fresh_and_available(defect):
    items = [proof("databento"), proof("schwab"), proof("fmp", interval=300)]
    context = items[-1]["price_context"]
    if defect == "disagree": context["after"]["price"] = 102
    if defect == "missing": context.pop("after")
    if defect == "stale": context["before"]["timestamp"] = (START-timedelta(minutes=16)).isoformat()
    if defect == "future": context["after"]["received_at"] = (NOW+timedelta(seconds=1)).isoformat()
    result = assess(items)
    assert result["derived_allowed"] is (defect != "disagree")
    if defect != "disagree": assert result["limitations"]


def test_duplicate_conflicting_native_rows_and_derived_records_cannot_vote():
    items = [proof("databento"), proof("schwab"), proof("fmp", interval=300)]
    items[-1]["records"] = [bar(volume=0), bar(volume=0, price=101)]
    assert not assess(items)["derived_allowed"]
    items[-1]["records"] = [{**bar(volume=0), "is_synthetic": True}]
    assert not assess(items)["derived_allowed"]


def test_late_conflicting_response_does_not_change_an_earlier_causal_assessment():
    items = [proof("databento"), proof("schwab"), proof("fmp", interval=300)]
    late = proof("fmp", interval=300)
    late["received_at"] = (NOW+timedelta(seconds=1)).isoformat()
    late["records"] = [bar(price=102)]
    result = assess(items+[late])
    assert result["derived_allowed"] and result["available_at"] == NOW.isoformat()
    assert len(result["source_references"]) == 3


def test_forged_assessment_or_bad_anchor_cannot_create_carry():
    result = assess()
    result["anchor"]["price"] = 101
    with pytest.raises(ValueError): derive_gap_carry(result)
    bad = anchor(); bad["is_synthetic"] = "false"
    assert not assess(anchor=bad)["derived_allowed"]
    bad = anchor(); bad["timestamp"] = (START-timedelta(minutes=16)).isoformat()
    assert not assess(anchor=bad)["derived_allowed"]


def test_three_empty_responses_without_later_brackets_allow_only_probable_estimate():
    items = [proof("databento"), proof("schwab"), proof("fmp", interval=300)]
    for item in items: item.pop("price_context")
    result = assess(items)
    assert result["status"] == "probable_no_trade" and result["derived_allowed"]
    assert len(result["limitations"]) == 6
    assert derive_gap_carry(result)["market_wide_no_trade_verified"] is False


@pytest.mark.parametrize("value", [float('nan'), float('inf'), True])
def test_nonfinite_or_boolean_tolerance_cannot_disable_agreement(value):
    with pytest.raises(ValueError):
        assess(relative_tolerance=value)


@pytest.mark.parametrize('defect', ['stale_anchor', 'missing_provider', 'positive_activity'])
def test_rehashed_false_decision_flags_do_not_replace_eligibility_recomputation(defect):
    import hashlib,json
    result=assess()
    if defect=='stale_anchor':result['anchor']['timestamp']=(START-timedelta(hours=1)).isoformat()
    if defect=='missing_provider':result['evidence'].pop()
    if defect=='positive_activity':result['evidence'][0]['records']=[bar()]
    result['assessment_sha256']=hashlib.sha256(json.dumps({k:v for k,v in result.items() if k!='assessment_sha256'},sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
    with pytest.raises(ValueError):derive_gap_carry(result)


def test_eligible_assessment_keeps_frozen_hash_and_derived_carry_validity():
    # This identity predates the diagnostic correction. Revalidation must retain
    # existing eligible publications and the exact bytes they committed to.
    result = assess()
    assert result["assessment_sha256"] == "aaf92bd4738167f674a39b2c809655f64ade383a5b6b6ff29e95e1a20c9448c0"
    assert derive_gap_carry(result)["assessment_sha256"] == result["assessment_sha256"]


@pytest.mark.parametrize("defect,expected_status", [
    ("missing_provider", "incomplete"),
    ("invalid_anchor", "incomplete"),
    ("contradiction", "conflict"),
    ("observed_recovery", "observed_recovery"),
])
def test_denied_assessment_does_not_claim_successful_probable_absence(defect, expected_status):
    items = [proof("databento"), proof("schwab"), proof("fmp", interval=300)]
    selected_anchor = anchor()
    if defect == "missing_provider":
        items.pop()
    elif defect == "invalid_anchor":
        selected_anchor["is_synthetic"] = True
    elif defect == "contradiction":
        items[-1]["price_context"]["after"]["price"] = 102
    else:
        items[1]["records"] = [bar()]
    result = assess(items, anchor=selected_anchor)
    assert result["status"] == expected_status
    assert result["derived_allowed"] is False
    assert result["confidence_basis"] == (
        "probable absence not established; inspect status, provider votes, anchor validity and diagnostics"
    )
    with pytest.raises(ValueError):
        derive_gap_carry(result)
