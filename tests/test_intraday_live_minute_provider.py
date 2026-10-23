"""Offline protocol and durability checks; never contact a data provider."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from ml.intraday.data import fetch_databento_interval_attempts
from ml.intraday.live_minute_provider import LiveMinuteReplayProvider
from ml.intraday.minute_persistence import PersistedMinuteProvider
from ml.intraday.prediction import ContractError

OPEN = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
CLOSE = OPEN + timedelta(minutes=15)
NOW = CLOSE + timedelta(seconds=10)


def row(symbol="SDGR", **changes):
    return {"symbol": symbol, "ts_event": OPEN + timedelta(minutes=2),
            "open": 10., "high": 12., "low": 9., "close": 11., "volume": 5, **changes}


def captured(rows=None, *, mutate=None, calls=None):
    def capture(key, request, start, end, path, **kwargs):
        if calls is not None:
            calls.append((request, path, kwargs))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline-native-fixture-" + path.parent.name.encode())
        selected = [row()] if rows is None else rows
        evidence = {"mode": "live_intraday_replay", "completed": True, "dataset": request.dataset,
            "schema": request.schema, "symbols": request.params["symbols"], "stype_in": "raw_symbol",
            "requested_start": start.isoformat(), "requested_end": end.isoformat(),
            "connection_started_at": NOW.isoformat(), "finished_at": NOW.isoformat(),
            "subscription_ack": [{"code": 1}], "replay_completed": [{"code": 3}],
            "selected_quarter_records": len(selected), "selected_quarter_native_records": len(selected),
            "projection_version": "intraday-native-context-projection-v1",
            "raw_path": str(path), "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "raw_bytes": path.stat().st_size}
        if mutate:
            mutate(evidence)
        return selected, evidence
    return capture


def provider(tmp_path, **kwargs):
    return LiveMinuteReplayProvider(datastore=tmp_path, pc="Scout", symbols=("SDGR", "GLOB"),
        clock=lambda: NOW, api_key="offline-fixture", **kwargs)


def test_capability_bounds_do_not_query_historical_metadata_or_claim_delivery(tmp_path):
    p = provider(tmp_path, capture=lambda *args: pytest.fail("No capture expected"))
    bounds = p.dataset_range()
    assert bounds["range_basis"] == "local_live_replay_request_bounds_not_provider_availability"
    assert p.available_range_for_schema("ohlcv-1m", dataset_range=bounds).end == NOW
    assert not p.root.exists() and p.last_delivery_evidence is None
    with pytest.raises(ContractError):
        p.available_range_for_schema("ohlcv-1m", dataset_range={"schema": {}})


def test_each_sparse_confirmation_performs_an_independent_native_capture(tmp_path):
    calls = []
    p = provider(tmp_path, capture=captured(calls=calls))
    receipts = []
    for attempt in ("first", "second"):
        result = fetch_databento_interval_attempts(p, symbols=["SDGR", "GLOB"], opened_at=OPEN,
            attempt_id=attempt, clock=lambda: NOW)
        assert result["SDGR"].status == result["GLOB"].status == "success"
        assert len(result["SDGR"].rows) == 1
        assert result["GLOB"].rows == ()
        receipts.append(p.last_delivery_evidence)
    assert len(calls) == 2 and calls[0][1] != calls[1][1]
    assert receipts[0]["attempt_id"] != receipts[1]["attempt_id"]
    assert all(r["independent_capture"] and not r["response_cache_used"] for r in receipts)
    assert all(r["source_id"] == "databento:EQUS.MINI:ohlcv-1m" for r in receipts)
    assert all(r["observed_minutes"] == {"SDGR": 1, "GLOB": 0} for r in receipts)


def test_live_minute_provider_uses_existing_canonical_persistence_without_filling(tmp_path):
    p = provider(tmp_path, capture=captured())
    wrapped = PersistedMinuteProvider(p, datastore=tmp_path, pc="Scout", symbols=p.symbols, clock=lambda: NOW)
    result = fetch_databento_interval_attempts(wrapped, symbols=["SDGR", "GLOB"], opened_at=OPEN,
        attempt_id="actual", clock=lambda: NOW)
    assert result["SDGR"].status == "success"
    receipt = wrapped.last_receipt
    assert receipt["status"] == "complete"
    assert receipt["symbols"]["SDGR"]["observed_minutes"] == 1
    assert receipt["symbols"]["GLOB"]["observed_minutes"] == 0
    native = pd.read_parquet(receipt["symbols"]["SDGR"]["normalized_path"])
    assert len(native) == 1
    assert p.last_delivery_evidence["raw_delivery"]["completed"]
    response_path = Path(receipt["response_receipt"])
    archived = json.loads(response_path.read_text())
    entry = next(item for item in archived["symbols"] if item["symbol"] == "SDGR")
    raw_path = response_path.parent / entry["raw"]["file"]
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == entry["raw"]["sha256"]
    assert pd.read_parquet(raw_path).attrs["live_replay_delivery"] == p.last_delivery_evidence
    delivery = p.last_delivery_evidence
    assert hashlib.sha256(Path(delivery["receipt_path"]).read_bytes()).hexdigest() == delivery["receipt_sha256"]
    assert hashlib.sha256(Path(delivery["raw_delivery"]["raw_path"]).read_bytes()).hexdigest() == delivery["raw_delivery"]["raw_sha256"]
    # Empty scope is unavailable, never fabricated minute prices/no-trade proof.
    assert "source_response" not in receipt["symbols"]["GLOB"]
    assert result["GLOB"].provider_no_trade_evidence is None


@pytest.mark.parametrize("change", [lambda e: e.update(completed=False),
    lambda e: e.update(raw_sha256="0" * 64), lambda e: e.update(dataset="OPRA.PILLAR"),
    lambda e: e.update(replay_completed=[]), lambda e: e.update(selected_quarter_native_records=2),
    lambda e: e.update(finished_at=(NOW + timedelta(seconds=1)).isoformat())])
def test_incomplete_or_unbound_delivery_never_becomes_success(tmp_path, change):
    p = provider(tmp_path, capture=captured(mutate=change))
    with pytest.raises(ContractError):
        p.fetch_native_bars_range(["SDGR"], p.native_specs()[0], start=OPEN, end=CLOSE)
    assert p.last_delivery_evidence is None
    assert not list(p.root.rglob("receipt.json"))


@pytest.mark.parametrize("rows", [[row("PYPL")], [row(), row()], [row(volume=-1)],
    [row(ts_event=CLOSE)], [row(high=9)], [row(ts_event=OPEN.replace(tzinfo=None))]])
def test_foreign_duplicate_unfinished_or_invalid_native_rows_fail_closed(tmp_path, rows):
    p = provider(tmp_path, capture=captured(rows))
    with pytest.raises(ContractError):
        p.fetch_native_bars_range(["SDGR"], p.native_specs()[0], start=OPEN, end=CLOSE)
    assert p.last_delivery_evidence is None


def test_scope_and_recent_exact_quarter_are_required_before_any_capture(tmp_path):
    p = provider(tmp_path, capture=lambda *args: pytest.fail("Invalid request must not fetch"))
    spec = p.native_specs()[0]
    for symbols, start, end in ((["PYPL"], OPEN, CLOSE), (["SDGR"], OPEN, CLOSE + timedelta(minutes=15)),
                               (["SDGR"], OPEN + timedelta(days=1), CLOSE + timedelta(days=1)),
                               (["SDGR"], OPEN - timedelta(days=3), CLOSE - timedelta(days=3))):
        with pytest.raises(ContractError):
            p.fetch_native_bars_range(symbols, spec, start=start, end=end)
