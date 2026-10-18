"""Offline source-omission proof checks; fixture attestations never fetch data."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import json

import pandas as pd
import pytest

from ml.intraday import data, no_trade

SOURCE = "databento:EQUS.MINI:ohlcv-1m"
OPEN = pd.Timestamp("2026-05-11T14:00:00Z")


def attest(response):
    """Model a validated immutable raw/receipt pair for pure resolver tests.

    The actual file/hash path is exercised separately by persistence roundtrips.
    Legacy prose, arbitrary providers and empty frames do not gain this proof.
    """
    receipt = {"status": "complete", "start": response.interval_open, "end": response.interval_close,
        "response_receipt": "offline-fixture/response.json",
        "response_receipt_sha256": "b" * 64, "symbols": {response.symbol: {"status": "complete",
        "source_response": {"contract": no_trade.CONTRACT, "complete": True,
            "symbol_resolution": "returned_native_raw_symbol", "raw_rows": len(response.rows),
            "parsed_rows": len(response.rows), "raw_sha256": "a" * 64,
            "raw_file": "raw-000.parquet",
            "requested_at": response.request_started_at, "received_at": response.request_ended_at}}}}
    proof = no_trade.make_omission_evidence(response, receipt)
    return replace(response, provider_no_trade_evidence=proof)


def sparse_rows(opened=OPEN, missing=(0, 14)):
    return tuple(dict(opened_at=(opened + timedelta(minutes=n)).isoformat(),
        open=100., high=101., low=99., close=100.5, volume=10.) for n in range(15) if n not in missing)


def attempt(index, *, rows=None, status="success", **changes):
    value = data.IntervalResponse(str(index), "AAPL", SOURCE, "EQUS.MINI", OPEN.isoformat(),
        (OPEN + data.STEP).isoformat(), (OPEN + data.STEP + timedelta(seconds=5*index)).isoformat(),
        (OPEN + data.STEP + timedelta(seconds=5*index+1)).isoformat(), status,
        sparse_rows() if rows is None else rows, OPEN.isoformat(), (OPEN+data.STEP).isoformat())
    return attest(replace(value, **changes))


def prior():
    return dict(symbol="AAPL", source_id=SOURCE, source_dataset="EQUS.MINI",
        opened_at=(OPEN-data.STEP).isoformat(), available_at=OPEN.isoformat(), interval_seconds=900,
        session_id="2026-05-11", session_open="2026-05-11T13:30:00Z", session_close="2026-05-11T23:00:00Z",
        synthetic=False, no_trade=False, open=99., high=100., low=98., close=99.5, volume=20.)


def session():
    return data.SessionBounds("2026-05-11", "2026-05-11T13:30:00Z", "2026-05-11T23:00:00Z")


def test_two_scoped_stable_receipts_preserve_genuine_rows_and_fill_only_proven_omissions():
    responses = [attempt(1), attempt(2)]
    result = data.resolve_minute_candle(responses, session=session(), previous=prior())
    assert result["status"] == "mixed_synthetic"
    row = result["candle"]
    assert row["observed_constituents"] == 13 and row["synthetic_constituents"] == 2
    assert row["open"] == 99.5 and row["close"] == 100.5 and row["volume"] == 130
    assert data.synthetic_continuity_verified(row)
    assert no_trade.verified_omission_pair([asdict(r) for r in responses], opened_at=OPEN)
    assert not no_trade.verified_omission_pair(responses, opened_at=OPEN+timedelta(minutes=1))


@pytest.mark.parametrize("proof", [None, 1, [], "successful exact covered native omission", {},
    {"version": no_trade.VERSION, "complete_response": True}])
def test_advertised_range_and_prose_alone_never_prove_zero_trades(proof):
    responses = [replace(attempt(i), provider_no_trade_evidence=proof) for i in (1, 2)]
    result = data.resolve_minute_candle(responses, session=session(), previous=prior())
    assert result["status"] == "missing" and result["reason"] == "unverified_source_no_trade"
    assert result["candle"] is None


@pytest.mark.parametrize("field,value", [
    ("scope", "market_wide"), ("source_dataset", "XNAS.ITCH"), ("symbol", "OTHER"),
    ("interval_close", "2026-05-11T14:16:00Z"), ("complete_response", False),
    ("raw_rows", 14), ("parsed_rows", True), ("raw_sha256", "missing"),
    ("response_receipt_sha256", "bad"), ("rows_sha256", "0" * 64),
    ("response_receipt", None), ("raw_file", "../raw-000.parquet"),
    ("source_requested_at", "2026-05-11T14:14:59Z"),
    ("source_received_at", "2026-05-11T14:20:00Z"), ("symbol_resolution", "unresolved")])
def test_malformed_or_mismatched_proof_rejected(field, value):
    response = attempt(1)
    proof = deepcopy(response.provider_no_trade_evidence)
    proof[field] = value
    assert not no_trade.verified_response(replace(response, provider_no_trade_evidence=proof))


def test_empty_response_cannot_establish_symbol_resolution_or_market_wide_absence():
    responses = [attempt(i, rows=()) for i in (1, 2)]
    assert all(r.provider_no_trade_evidence is None for r in responses)
    for resolver in (data.resolve_interval, data.resolve_minute_candle):
        result = resolver(responses, session=session(), previous=prior())
        assert result["status"] == "missing" and result["reason"] == "unverified_source_no_trade"
        assert result["candle"] is None


@pytest.mark.parametrize("damage", ["error", "lag", "changed", "raw_changed", "too_soon", "retry_after", "reused_id"])
def test_only_consecutive_complete_stable_responses_establish_proof(damage):
    first, second = attempt(1), attempt(2)
    if damage == "error":
        responses = [first, attempt(2, status="temporary_error", rows=()), attempt(3)]
    elif damage == "lag":
        responses = [first, replace(second, status="unavailable_coverage", rows=())]
    elif damage == "changed":
        responses = [first, attempt(2, rows=sparse_rows(missing=(0,)))]
    elif damage == "raw_changed":
        proof = {**second.provider_no_trade_evidence, "raw_sha256": "c" * 64}
        responses = [first, replace(second, provider_no_trade_evidence=proof)]
    elif damage == "too_soon":
        responses = [first, attest(replace(second, request_started_at=first.request_ended_at))]
    elif damage == "retry_after":
        responses = [replace(first, retry_after_seconds=30), second]
    else:
        responses = [first, attest(replace(second, attempt_id=first.attempt_id))]
    assert not no_trade.verified_omission_pair(responses, opened_at=OPEN)
    assert data.resolve_minute_candle(responses, session=session(), previous=prior())["candle"] is None


@pytest.mark.parametrize("bad", ["duplicate", "outside", "invalid_ohlc", "negative_volume"])
def test_invalid_complete_response_cannot_be_attested(bad):
    rows = list(sparse_rows())
    if bad == "duplicate": rows.append(rows[0])
    elif bad == "outside": rows.append(dict(rows[0], opened_at=(OPEN-timedelta(minutes=1)).isoformat()))
    elif bad == "invalid_ohlc": rows[0] = dict(rows[0], high=98.)
    else: rows[0] = dict(rows[0], volume=-1.)
    assert attempt(1, rows=tuple(rows)).provider_no_trade_evidence is None


def test_malformed_raw_row_or_receipt_fails_closed_without_throwing():
    assert attempt(1, rows=(None,)).provider_no_trade_evidence is None
    response = attempt(1)
    assert no_trade.make_omission_evidence(response, {"symbols": {"AAPL": {"source_response": 1}}}) is None


def test_unverified_historical_synthetic_excluded_before_features_or_labels():
    from tests.test_intraday_data import candles
    frame = candles(40)
    frame.loc[25, "synthetic"] = True
    frame.loc[25, "evidence_json"] = '{"legacy_confirmations": 2}'
    reference = frame.opened_at.iloc[35]
    dataset = data.build_dataset(frame, cutoff=reference+data.STEP, reference_at=reference, synthetic_policy="include")
    assert dataset.manifest["unverified_synthetic_rows_excluded"] == 1
    assert frame.opened_at.iloc[25] not in dataset.reference_at
    assert all(row["opened_at"] != frame.opened_at.iloc[25].isoformat()
               for row in dataset.inference["snapshot"]["candles"])


@pytest.mark.parametrize("damage", ["symbol", "source", "price", "receipt"])
def test_valid_proof_cannot_be_copied_to_an_unrelated_or_altered_candle(damage):
    candle = data.resolve_minute_candle([attempt(1), attempt(2)], session=session(), previous=prior())["candle"]
    if damage == "symbol": candle["symbol"] = "OTHER"
    elif damage == "source": candle["source_dataset"] = "OTHER"
    elif damage == "price": candle["high"] += 1
    else:
        evidence = json.loads(candle["evidence_json"])
        evidence["constituents"][0]["available_at"] = "2026-05-11T14:15:09Z"
        candle["evidence_json"] = json.dumps(evidence)
    assert not data.synthetic_continuity_verified(candle)


def test_unresolved_empty_symbol_does_not_block_genuine_symbol_or_next_quarter(tmp_path, monkeypatch):
    from tests.test_intraday_minute_feed import setup
    feed, _, clock, calls = setup(tmp_path, monkeypatch,
        behavior=lambda r, symbol, n: {"rows": ()} if symbol == "ABCL" else {})
    first = clock()-timedelta(minutes=16)
    feed.step()
    assert len(feed.store.observed_candles("DBX")) == 1
    assert not feed.store.observed_candles("ABCL")
    clock.advance(4)
    feed.step()
    assert feed.outcomes[first, "ABCL"]["reason"] == "unverified_source_no_trade"
    clock.advance(900)
    feed.step()
    assert len(feed.store.observed_candles("DBX")) == 2
    assert not feed.store.observed_candles("ABCL")
