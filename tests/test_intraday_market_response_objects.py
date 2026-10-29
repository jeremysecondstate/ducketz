"""Date-independent raw objects retain every original receipt and legacy read."""

from tests.intraday_shared_storage_support import require_composed_storage
require_composed_storage()
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from datafetching.intraday_fmp_observations import saved_market_responses
from ml.intraday.acquisition_objects import market_response_root, resolve_market_payload
from ml.intraday.market_inputs import MarketInputStore, _bytes, _write_immutable, read_response


def append(store, at, payload):
    return store.append(symbol="AAPL", source="fmp_quote", started_at=at-timedelta(seconds=1),
        received_at=at, payload=payload, features={"price": 12}, producer_runtime={})


def test_equal_bodies_across_dates_share_file_but_legacy_paths_and_clocks_survive(tmp_path):
    store = MarketInputStore(tmp_path)
    first = datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
    second = first + timedelta(days=1)
    payload = {"symbol": "AAPL", "price": 12}
    receipts = [append(store, at, payload) for at in (first, second)]
    assert receipts[0]["payload_ref"]["sha256"] == receipts[1]["payload_ref"]["sha256"]
    assert receipts[0]["payload_ref"]["path"] != receipts[1]["payload_ref"]["path"]
    assert receipts[0]["id"] != receipts[1]["id"]
    assert len(list(market_response_root(store.root).glob("*.json"))) == 1
    paths = [store.root / receipt["payload_ref"]["path"] for receipt in receipts]
    canonical = next(market_response_root(store.root).glob("*.json"))
    assert all(path.samefile(canonical) for path in paths)
    assert all(path.is_relative_to(store.root / "raw") for path in paths)
    for receipt, at in zip(receipts, (first, second)):
        decoded = read_response(store.root, receipt["object_path"])
        assert decoded["received_at"] == at.isoformat()
        assert decoded["request_started_at"] == (at-timedelta(seconds=1)).isoformat()
        assert decoded["payload"] == payload
        assert store._validated_retained(receipt["object_path"])[1]["received_at"] == at.isoformat()
    saved = list(saved_market_responses(tmp_path, symbol="AAPL", source="fmp_quote"))
    assert [row["received_at"].to_pydatetime() for row in saved] == [first, second]
    assert saved[0]["paths"][1].samefile(saved[1]["paths"][1])
    assert saved[0]["payload"] == saved[1]["payload"] == payload


def test_legacy_and_global_references_read_without_rewriting_original_envelope(tmp_path):
    store = MarketInputStore(tmp_path)
    at = datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
    receipt = append(store, at, {"symbol": "AAPL", "price": 12})
    original = json.loads(open(receipt["object_path"], encoding="utf-8").read())
    ref = original["payload_ref"]
    original_bytes = open(receipt["object_path"], "rb").read()
    original["payload_ref"] = {**ref, "path": f"../source-objects/market-responses/{ref['sha256']}.json"}
    raw = _bytes(original)
    envelope = store.root / "objects/2026/10/28" / (hashlib.sha256(raw).hexdigest()+".json")
    _write_immutable(envelope, raw)
    store._index_retained(envelope, original)
    assert read_response(store.root, envelope)["payload"] == {"symbol": "AAPL", "price": 12}
    store._validated_retained(envelope)
    assert len(list(saved_market_responses(tmp_path, symbol="AAPL", source="fmp_quote"))) == 2
    assert envelope.read_bytes() == raw
    assert open(receipt["object_path"], "rb").read() == original_bytes


@pytest.mark.parametrize("relative", ["../../outside/{hash}.json", "objects/{hash}.json",
    "../source-objects/responses/{hash}.json", "../source-objects/market-responses/subdir/{hash}.json"])
def test_shared_reference_does_not_widen_reader_path_boundary(tmp_path, relative):
    store = MarketInputStore(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        resolve_market_payload(store.root, relative.format(hash="a"*64), "a"*64)
