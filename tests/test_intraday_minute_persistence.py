"""Actual local Parquet roundtrips, using fake native provider reads only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.models.market_data import MarketBar
from app.services.market_fetch_specs import databento_analysis_source_specs
from ml.intraday import minute_persistence as persistence
from ml.intraday.data import (
    SessionBounds, fetch_databento_interval_attempts, resolve_minute_candle,
)
from ml.intraday.prediction import ContractError, STEP


SPEC = next(s for s in databento_analysis_source_specs() if s.frequency == "1m")
FIRST = pd.Timestamp("2026-05-11T13:30:00Z")


def result(symbol, start=FIRST, *, count=15, missing=()):
    bars, rows = [], []
    for index in range(count):
        if index in missing:
            continue
        timestamp = start + pd.Timedelta(minutes=index)
        price = 100 + index / 10
        bars.append(MarketBar(symbol=symbol, source="databento", timeframe="1m",
            timestamp=timestamp.to_pydatetime(), open=price, high=price + 2,
            low=price - 1, close=price + .5, volume=10 + index))
        rows.append(dict(symbol=symbol, ts_event=timestamp, open=price, high=price + 2,
            low=price - 1, close=price + .5, volume=10 + index,
            instrument_id=1234567, publisher_id=78))
    return bars, pd.DataFrame(rows)


def selected(start=FIRST, end=None):
    return SimpleNamespace(schema="ohlcv-1m", start=start, end=end or start + STEP)


class Provider:
    dataset = "EQUS.MINI"
    custom_attribute = "existing provider binding"
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback
        self.last_results = self.last_selected = None
    def native_specs(self):
        return (SPEC,)
    def dataset_range(self):
        return {"existing": "metadata"}
    def available_range_for_schema(self, schema, *, dataset_range):
        assert schema == "ohlcv-1m" and dataset_range == {"existing": "metadata"}
        return selected(FIRST - pd.Timedelta(days=1), FIRST + pd.Timedelta(days=2))
    def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range):
        self.calls.append((tuple(symbols), spec, start, end, available_range))
        results = ({s: result(s, pd.Timestamp(start)) for s in symbols}
                   if self.callback is None else self.callback(symbols, spec, start, end))
        self.last_results, self.last_selected = results, selected(pd.Timestamp(start), pd.Timestamp(end))
        return self.last_results, self.last_selected


def wrapper(tmp_path, provider=None, *, clock=lambda: FIRST + 2 * STEP + pd.Timedelta(minutes=1)):
    provider = provider or Provider()
    return persistence.PersistedMinuteProvider(provider, datastore=tmp_path,
        pc="Scout", symbols=("ABCL", "DBX"), clock=clock)


def fetch(provider, start=FIRST, symbols=("ABCL",)):
    return provider.fetch_native_bars_range(symbols, SPEC, start=start,
        end=start + STEP, available_range="same original advertised range")


def records(tmp_path):
    return sorted((tmp_path / "intraday/scout/minute-fetches").glob("*/response.json"))


def canonical_path(status, symbol="ABCL"):
    return Path(status["symbols"][symbol]["normalized_path"])


def test_wrapper_delegates_existing_binding_and_returns_original_objects(tmp_path):
    original = Provider()
    provider = wrapper(tmp_path, original)
    results, actual_selected = fetch(provider, symbols=("ABCL", "DBX"))
    assert provider.dataset == original.dataset
    assert provider.custom_attribute == original.custom_attribute
    assert provider.native_specs() == (SPEC,)
    assert results is original.last_results and actual_selected is original.last_selected
    assert len(original.calls) == 1
    assert original.calls[0] == (("ABCL", "DBX"), SPEC, FIRST, FIRST + STEP,
                                 "same original advertised range")
    assert provider.last_receipt["status"] == "complete"
    returned = provider.last_receipt
    returned["status"] = "caller mutation"
    assert provider.last_receipt["status"] == "complete"


def test_two_quarters_append_thirty_native_minutes_and_replay_deduplicates(tmp_path):
    provider = wrapper(tmp_path)
    fetch(provider)
    first_path = canonical_path(provider.last_receipt)
    first = pd.read_parquet(first_path)
    assert len(first) == 15
    fetch(provider, FIRST + STEP)
    combined = pd.read_parquet(first_path)
    assert len(combined) == 30 and combined.timestamp.nunique() == 30
    assert combined.timestamp.min() == FIRST and combined.timestamp.max() == FIRST + pd.Timedelta(minutes=29)
    fetch(provider)
    pd.testing.assert_frame_equal(pd.read_parquet(first_path), combined)
    assert len(records(tmp_path)) == 3
    assert all(json.loads(p.with_name("persistence.json").read_text())["status"] == "complete"
               for p in records(tmp_path))
    raw = pd.read_parquet(provider.last_receipt["symbols"]["ABCL"]["raw_path"])
    assert raw.ts_event.nunique() == 30
    assert raw.instrument_id.eq(1234567).all() and raw.publisher_id.eq(78).all()
    assert raw.provider_dataset.eq("EQUS.MINI").all()
    assert raw.aggregation_method.eq("native").all()
    assert raw.price_basis.eq("unadjusted_market_scale").all()


def test_original_raw_response_is_immutable_and_precedes_canonical_writes(tmp_path, monkeypatch):
    original = Provider()
    store_type = persistence.ParquetStore
    checked = []
    class CheckingStore(store_type):
        def save_raw_frame(self, **kwargs):
            receipt = records(tmp_path)
            assert len(receipt) == 1 and not receipt[0].with_name("persistence.json").exists()
            value = json.loads(receipt[0].read_text())
            evidence = value["symbols"][0]["raw"]
            path = receipt[0].parent / evidence["file"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence["sha256"]
            pd.testing.assert_frame_equal(pd.read_parquet(path), original.last_results["ABCL"][1])
            checked.append(path)
            return super().save_raw_frame(**kwargs)
    monkeypatch.setattr(persistence, "ParquetStore", CheckingStore)
    fetch(wrapper(tmp_path, original))
    before = checked[0].read_bytes()
    def correction(symbols, spec, start, end):
        bars, raw = result("ABCL", pd.Timestamp(start))
        bars[0] = replace(bars[0], close=bars[0].close + .1)
        raw.loc[0, "close"] += .1
        return {"ABCL": (bars, raw)}
    monkeypatch.setattr(persistence, "ParquetStore", store_type)
    provider = wrapper(tmp_path, Provider(correction))
    fetch(provider)
    assert checked[0].read_bytes() == before
    assert len(records(tmp_path)) == 2
    normalized = pd.read_parquet(canonical_path(provider.last_receipt))
    assert len(normalized) == 15 and normalized.iloc[0].close == 100.6


def test_fifteen_observed_native_minutes_aggregate_using_existing_resolver(tmp_path):
    provider = wrapper(tmp_path)
    clock = lambda: FIRST + STEP + pd.Timedelta(seconds=10)
    responses = fetch_databento_interval_attempts(provider, symbols=("ABCL", "DBX"),
        opened_at=FIRST, attempt_id="actual-fixture", clock=clock)
    session = SessionBounds("2026-05-11", FIRST.isoformat(), (FIRST + 6 * STEP).isoformat())
    resolved = resolve_minute_candle([responses["ABCL"]], session=session, previous=None)
    assert resolved["status"] == "observed"
    candle = resolved["candle"]
    assert candle["open"] == 100 and candle["close"] == 101.9
    assert candle["high"] == 103.4 and candle["low"] == 99
    assert candle["volume"] == sum(10 + i for i in range(15))
    assert candle["synthetic"] is False
    assert len(pd.read_parquet(canonical_path(provider.last_receipt))) == 15
    assert len(provider._provider.calls) == 1


def test_empty_and_partial_responses_archive_without_filling_canonical_minutes(tmp_path):
    def partial(symbols, spec, start, end):
        return {"ABCL": result("ABCL", pd.Timestamp(start), missing=(7,)),
                "DBX": ([], pd.DataFrame())}
    provider = wrapper(tmp_path, Provider(partial))
    responses = fetch_databento_interval_attempts(provider, symbols=("ABCL", "DBX"), opened_at=FIRST,
        attempt_id="incomplete-fixture", clock=lambda: FIRST + STEP + pd.Timedelta(seconds=10))
    status = provider.last_receipt
    assert status["status"] == "complete"
    assert status["symbols"]["ABCL"]["observed_minutes"] == 14
    assert status["symbols"]["ABCL"]["missing_minutes"] == 1
    assert status["symbols"]["DBX"]["observed_minutes"] == 0
    assert status["symbols"]["DBX"]["normalized_path"] is None
    normalized = pd.read_parquet(canonical_path(status))
    assert len(normalized) == 14
    assert not normalized.timestamp.eq(FIRST + pd.Timedelta(minutes=7)).any()
    assert not list((tmp_path / "stocks/DBX").rglob("*.parquet"))
    receipt = json.loads(Path(status["response_receipt"]).read_text())
    assert [s["raw"]["rows"] for s in receipt["symbols"]] == [14, 0]
    session = SessionBounds("2026-05-11", FIRST.isoformat(), (FIRST + 6 * STEP).isoformat())
    assert resolve_minute_candle([responses["ABCL"]], session=session, previous=None)["status"] == "retry"
    assert resolve_minute_candle([responses["DBX"]], session=session, previous=None)["status"] == "retry"


@pytest.mark.parametrize("change", ["duplicate", "outside", "offminute", "nan", "bad_range", "negative_volume",
                                    "synthetic", "different_raw", "raw_dropped"])
def test_invalid_native_response_retains_receipt_but_does_not_write_canonical(tmp_path, change):
    def invalid(symbols, spec, start, end):
        bars, raw = result("ABCL", pd.Timestamp(start))
        if change == "duplicate":
            bars[1] = replace(bars[1], timestamp=bars[0].timestamp)
            raw.loc[1, "ts_event"] = raw.loc[0, "ts_event"]
        elif change in {"outside", "offminute"}:
            timestamp = pd.Timestamp(end) if change == "outside" else pd.Timestamp(start) + pd.Timedelta(seconds=1)
            bars[0] = replace(bars[0], timestamp=timestamp.to_pydatetime())
            raw.loc[0, "ts_event"] = timestamp
        elif change == "nan":
            bars[0] = replace(bars[0], close=float("nan"))
            raw.loc[0, "close"] = np.nan
        elif change == "bad_range":
            bars[0] = replace(bars[0], high=1)
            raw.loc[0, "high"] = 1
        elif change == "negative_volume":
            bars[0] = replace(bars[0], volume=-1)
            raw.loc[0, "volume"] = -1
        elif change == "synthetic":
            raw["synthetic"] = False
            raw.loc[0, "synthetic"] = True
        elif change == "different_raw":
            raw.loc[0, "close"] += .1
        elif change == "raw_dropped":
            bars.pop(0)
        return {"ABCL": (bars, raw)}
    provider = wrapper(tmp_path, Provider(invalid))
    with pytest.raises(ContractError):
        fetch(provider)
    assert len(records(tmp_path)) == 1
    failed = json.loads(records(tmp_path)[0].with_name("persistence.json").read_text())
    assert failed["status"] == "failed" and failed["error"]["type"] == "ContractError"
    assert provider.last_receipt == failed
    assert not list((tmp_path / "stocks").rglob("*.parquet"))


def test_active_wrong_scope_or_nonquarter_request_is_rejected_before_provider_read(tmp_path):
    original = Provider()
    provider = wrapper(tmp_path, original, clock=lambda: FIRST + pd.Timedelta(minutes=14))
    with pytest.raises(ContractError, match="completed whole quarters"):
        fetch(provider)
    provider = wrapper(tmp_path, original)
    with pytest.raises(ContractError, match="own symbol scope"):
        fetch(provider, symbols=("OTHER",))
    with pytest.raises(ContractError, match="completed whole quarters"):
        fetch(provider, start=FIRST + pd.Timedelta(minutes=1))
    assert original.calls == [] and list(tmp_path.iterdir()) == []


def test_canonical_persistence_failure_never_returns_success_and_keeps_response(tmp_path, monkeypatch):
    store_type = persistence.ParquetStore
    class BrokenStore(store_type):
        def save_bars(self, *args, **kwargs):
            raise OSError("fixture disk failure after raw was saved")
    monkeypatch.setattr(persistence, "ParquetStore", BrokenStore)
    provider = wrapper(tmp_path)
    with pytest.raises(OSError, match="fixture disk failure") as caught:
        fetch(provider)
    assert len(records(tmp_path)) == 1
    assert provider.last_receipt["status"] == "failed"
    assert caught.value.minute_persistence_receipt == provider.last_receipt
    assert provider.last_receipt["symbols"]["ABCL"]["status"] == "persisting"
    assert Path(provider.last_receipt["symbols"]["ABCL"]["raw_path"]).is_file()
    assert not list((tmp_path / "stocks").rglob("normalized/*.parquet"))
    monkeypatch.setattr(persistence, "ParquetStore", store_type)
    fetch(provider)
    assert len(pd.read_parquet(canonical_path(provider.last_receipt))) == 15
    assert len(records(tmp_path)) == 2


def test_explicit_whole_hour_catchup_keeps_actual_range_and_sixty_observed_minutes(tmp_path):
    end = FIRST + 4 * STEP
    results = {"ABCL": result("ABCL", FIRST, count=60)}
    actual_selected = selected(FIRST, end)
    common = dict(datastore=tmp_path, pc="Scout", symbols=("ABCL",), spec=SPEC, dataset="EQUS.MINI",
        start=FIRST, end=end, requested_at=end + pd.Timedelta(seconds=5),
        observed_at=end + pd.Timedelta(seconds=6))
    with pytest.raises(ContractError, match="live reads use one quarter"):
        persistence.persist_native_minutes(results, actual_selected, **common)
    status = persistence.persist_native_minutes(results, actual_selected, allow_catchup=True, **common)
    assert status["status"] == "complete"
    normalized = pd.read_parquet(canonical_path(status))
    assert len(normalized) == normalized.timestamp.nunique() == 60
    receipt = json.loads(Path(status["response_receipt"]).read_text())
    assert receipt["request"]["start"] == FIRST.isoformat()
    assert receipt["request"]["end"] == end.isoformat()
    assert receipt["request"]["range_policy"] == "explicit_catchup_up_to_one_day"
    assert status["symbols"]["ABCL"]["expected_minutes"] == 60
    assert status["symbols"]["ABCL"]["missing_minutes"] == 0
    with pytest.raises(ContractError, match="limited to one day"):
        persistence.persist_native_minutes(results, selected(FIRST, FIRST + pd.Timedelta(days=2)),
            allow_catchup=True, **{**common, "end": FIRST + pd.Timedelta(days=2),
                "requested_at": FIRST + pd.Timedelta(days=3), "observed_at": FIRST + pd.Timedelta(days=3)})
