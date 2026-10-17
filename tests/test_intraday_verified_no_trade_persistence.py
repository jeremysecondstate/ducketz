"""Production decode/persistence/receipt pipeline with an entirely fake transport."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.databento_market_data import DatabentoMarketDataProvider
from ml.intraday import data, minute_persistence, no_trade
from tests.test_intraday_minute_persistence import result, SPEC

OPEN = pd.Timestamp("2026-05-11T14:00:00Z")


def production_fixture(tmp_path, *, missing=(1,), damage=None):
    now, calls = [OPEN + data.STEP + pd.Timedelta(seconds=5)], []
    _, raw = result("ABCL", OPEN, missing=missing)
    if damage == "outside":
        raw.loc[0, "ts_event"] = OPEN-pd.Timedelta(minutes=1)
    elif damage == "unparsed":
        raw.loc[0, "close"] = float("nan")
    def get_range(**kwargs):
        calls.append(kwargs)
        assert kwargs["symbols"] == ["ABCL"] and kwargs["stype_in"] == "raw_symbol"
        assert "limit" not in kwargs
        def to_df(**options):
            assert options == {"map_symbols": True}
            return raw.copy(deep=True)
        return SimpleNamespace(to_df=to_df)
    client = SimpleNamespace(timeseries=SimpleNamespace(get_range=get_range),
        metadata=SimpleNamespace(get_dataset_range=lambda **_: {
            "start": OPEN.isoformat(), "end": (OPEN+data.STEP).isoformat()}))
    original = DatabentoMarketDataProvider(api_key="offline-fixture-not-a-key", dataset="EQUS.MINI",
        native_schemas=("ohlcv-1m",))
    original._client = lambda: client
    clock = lambda: now[0]
    provider = minute_persistence.PersistedMinuteProvider(original, datastore=tmp_path,
        pc="Scout", symbols=("ABCL",), clock=clock)
    return provider, now, clock, calls


def fetch(provider, clock, attempt):
    return data.fetch_databento_interval_attempt(provider, symbol="ABCL", opened_at=OPEN,
        attempt_id=attempt, clock=clock)


def test_real_adapter_complete_decode_and_actual_receipts_authorize_source_scoped_sparse_fill(tmp_path):
    provider, now, clock, calls = production_fixture(tmp_path)
    first = fetch(provider, clock, "first")
    receipt = provider.last_receipt
    assert no_trade.verified_response(first)
    response_path = Path(receipt["response_receipt"])
    assert first.provider_no_trade_evidence["response_receipt"] == str(response_path)
    assert first.provider_no_trade_evidence["response_receipt_sha256"] == hashlib.sha256(response_path.read_bytes()).hexdigest()
    response = json.loads(response_path.read_text())
    raw = response["symbols"][0]["raw"]
    assert first.provider_no_trade_evidence["raw_file"] == raw["file"]
    assert first.provider_no_trade_evidence["raw_sha256"] == hashlib.sha256((response_path.parent/raw["file"]).read_bytes()).hexdigest()
    now[0] += pd.Timedelta(seconds=5)
    second = fetch(provider, clock, "second")
    assert len(calls) == 2 and no_trade.verified_omission_pair([first, second], opened_at=OPEN+pd.Timedelta(minutes=1))
    resolved = data.resolve_minute_candle([first, second], previous=None,
        session=data.SessionBounds("2026-05-11", "2026-05-11T13:30:00Z", "2026-05-11T23:00:00Z"))
    assert resolved["status"] == "mixed_synthetic" and data.synthetic_continuity_verified(resolved["candle"])
    assert len(pd.read_parquet(receipt["symbols"]["ABCL"]["normalized_path"])) == 14
    assert len(list((tmp_path/"intraday/scout/minute-fetches").glob("*/response.json"))) == 2


@pytest.mark.parametrize("damage", ["failed", "symbol_failed", "wrong_interval"])
def test_failed_or_unrelated_persistence_receipt_cannot_attest_a_response(tmp_path, damage):
    provider, _, clock, _ = production_fixture(tmp_path)
    response = fetch(provider, clock, "first")
    receipt = deepcopy(provider.last_receipt)
    if damage == "failed": receipt["status"] = "failed"
    elif damage == "symbol_failed": receipt["symbols"]["ABCL"]["status"] = "persisting"
    else: receipt["start"] = (OPEN-pd.Timedelta(minutes=15)).isoformat()
    assert no_trade.make_omission_evidence(response, receipt) is None


@pytest.mark.parametrize("damage", ["outside", "unparsed"])
def test_raw_outside_or_dropped_during_parse_never_becomes_omission_proof(tmp_path, damage):
    provider, _, clock, calls = production_fixture(tmp_path, damage=damage)
    with pytest.raises(data.ContractError):
        fetch(provider, clock, "invalid")
    assert provider.last_receipt["status"] == "failed"
    assert len(calls) == 1 and not list((tmp_path/"stocks").rglob("*.parquet"))
    assert len(list((tmp_path/"intraday/scout/minute-fetches").glob("*/response.json"))) == 1


def test_complete_empty_adapter_response_remains_unresolved_without_mapping_evidence(tmp_path):
    provider, _, clock, _ = production_fixture(tmp_path, missing=tuple(range(15)))
    response = fetch(provider, clock, "empty")
    assert response.status == "success" and response.rows == ()
    assert response.provider_no_trade_evidence is None
    assert provider.last_receipt["status"] == "complete"
    assert "source_response" not in provider.last_receipt["symbols"]["ABCL"]
