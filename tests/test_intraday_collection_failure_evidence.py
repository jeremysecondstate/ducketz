"""Offline evidence for exceptions caught inside an independent collection."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday.market_inputs import MarketInputCollector


NOW = datetime(2026, 10, 19, 17, 10, tzinfo=timezone.utc)
REFERENCE = NOW - timedelta(minutes=25)
SECRET = "fixture-collector-secret-7fbc"
LANES = ("schwab_rest", "fmp_rest", "databento_stream", "schwab_stream")


def locked_catalog(path):
    """Raise an actual SQLite lock error beneath a credential-bearing wrapper."""
    first = sqlite3.connect(path, timeout=0)
    second = sqlite3.connect(path, timeout=0)
    try:
        first.execute("CREATE TABLE receipts (value INTEGER)")
        first.commit()
        first.execute("BEGIN IMMEDIATE")
        try:
            second.execute("INSERT INTO receipts VALUES (1)")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("retention failed " + SECRET + " https://fixture.test/?apikey=" + SECRET) from exc
        pytest.fail("Fixture did not produce SQLite contention")
    finally:
        second.close()
        first.close()


@pytest.fixture
def collection(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Offline collection diagnostics attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr("ml.market_provenance.capture_runtime", lambda *args, **kwargs: {"offline_fixture": True})
    collector = MarketInputCollector(tmp_path / "store", ["AAPL"], clock=lambda: NOW,
        fmp=SimpleNamespace(api_key=SECRET))
    collector.store.producer_runtime = {"offline_fixture": True}
    monkeypatch.setattr(collector.store, "capture_producer", lambda: collector.store.producer_runtime)
    rest = {}
    for lane, source in (("schwab_rest", "schwab_quote"), ("fmp_rest", "fmp_quote")):
        rest[lane] = collector.store.append(symbol="AAPL", source=source, started_at=NOW,
            received_at=NOW, payload={"fixture": lane}, features={"last" if source == "schwab_quote" else "price": 100})
    monkeypatch.setattr(collector, "_schwab_collect", lambda: [rest["schwab_rest"]])
    monkeypatch.setattr(collector, "_fmp_collect", lambda: [rest["fmp_rest"]])
    stream_sources = {"databento_stream": "db_equity", "schwab_stream": "schwab_stream_quote"}
    streams = {}
    for lane, source in stream_sources.items():
        item = {"symbol": "AAPL", "source": source, "started_at": NOW, "received_at": NOW,
                "payload": {"fixture": lane}, "features": {"spread_relative": .01}, "status": "ok"}
        streams[lane] = SimpleNamespace(snapshot=lambda item=item: [item])
    collector.streams = streams["databento_stream"]
    collector.schwab_stream = streams["schwab_stream"]
    return collector, rest, streams


def set_failure(collector, streams, monkeypatch, lane, operation):
    if lane in {"schwab_rest", "fmp_rest"}:
        monkeypatch.setattr(collector, "_schwab_collect" if lane == "schwab_rest" else "_fmp_collect", operation)
    else:
        monkeypatch.setattr(streams[lane], "snapshot", operation)


def read_manifest(result):
    path = Path(result["manifest_path"])
    raw = path.read_bytes()
    assert path.stem == hashlib.sha256(raw).hexdigest()
    saved = json.loads(raw)
    assert saved == {key: value for key, value in result.items() if key != "manifest_path"}
    assert SECRET not in raw.decode() and "https://" not in raw.decode()
    return saved


@pytest.mark.parametrize("lane", LANES)
def test_caught_lane_error_retains_real_sqlite_cause_and_other_receipts(collection, tmp_path, monkeypatch, lane):
    collector, rest, streams = collection
    original = {Path(row["object_path"]): Path(row["object_path"]).read_bytes() for row in rest.values()}
    set_failure(collector, streams, monkeypatch, lane, lambda: locked_catalog(tmp_path / (lane + ".sqlite3")))

    result = collector.collect(REFERENCE)

    assert result["errors"] == ["RuntimeError"]
    assert len(result["receipts"]) == 3
    sources = {"schwab_rest": "schwab_quote", "fmp_rest": "fmp_quote",
               "databento_stream": "db_equity", "schwab_stream": "schwab_stream_quote"}
    assert {row["source"] for row in result["receipts"]} == {source for name, source in sources.items() if name != lane}
    assert all(path.read_bytes() == raw for path, raw in original.items())
    saved = read_manifest(result)
    assert len(saved["error_details"]) == 1
    detail = saved["error_details"][0]
    assert detail["lane"] == lane
    assert detail["stage"] == ("provider_collection" if lane.endswith("rest") else "stream_snapshot")
    assert detail["reference_at"] == REFERENCE.isoformat() and detail["recorded_at"] == NOW.isoformat()
    assert detail["error_type"] == "RuntimeError" and not detail["exception_chain_truncated"]
    chain = detail["exception_chain"]
    assert [cause["error_type"] for cause in chain] == ["RuntimeError", "OperationalError"]
    assert "[REDACTED]" in chain[0]["message"] and "[URL REDACTED]" in chain[0]["message"]
    assert chain[1]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
    assert chain[1]["sqlite_errorname"] == "SQLITE_BUSY" and chain[1]["message"] == "database is locked"
    assert chain[1]["frames"][-1]["function"] == "locked_catalog"
    assert all("/" not in frame["file"] and "\\" not in frame["file"] for cause in chain for frame in cause["frames"])


def test_all_failure_lanes_keep_legacy_order_and_bounded_diagnostics(collection, tmp_path, monkeypatch):
    collector, _, streams = collection
    for lane in LANES:
        set_failure(collector, streams, monkeypatch, lane, lambda lane=lane: locked_catalog(tmp_path / (lane + ".sqlite3")))
    result = collector.collect(REFERENCE)
    assert result["errors"] == ["RuntimeError"] * 4 and result["receipts"] == []
    saved = read_manifest(result)
    assert [detail["lane"] for detail in saved["error_details"]] == list(LANES)
    assert all(len(detail["exception_chain"]) <= 4 for detail in saved["error_details"])
    assert all(len(cause["frames"]) <= 8 for detail in saved["error_details"] for cause in detail["exception_chain"])


def test_successful_collection_has_empty_diagnostics_and_preserves_receipts(collection):
    collector, _, _ = collection
    result = collector.collect(REFERENCE)
    assert result["errors"] == [] and len(result["receipts"]) == 4
    assert read_manifest(result)["error_details"] == []
