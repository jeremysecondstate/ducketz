from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import databento_dbn as dbn
import pandas as pd
import pytest

from datafetching import databento_xnas_replay as replay
from datafetching import xnas_replay_archive as archive
from ml.artifacts import file_checksum


SESSION = "2026-09-08"
START = pd.Timestamp("2026-09-08T11:00:00Z")
END = pd.Timestamp("2026-09-09T00:00:00Z")
NOW = pd.Timestamp("2026-09-09T02:00:00Z")


class NativeClient:
    """Synthetic gateway retaining native metadata, mapping and control bytes."""
    def __init__(self, *, scenario="success", **kwargs):
        self.scenario = scenario

    def add_callback(self, callback, exception_callback):
        self.callback, self.callback_error = callback, exception_callback

    def add_stream(self, stream, exception_callback):
        self.stream = stream

    def add_reconnect_callback(self, callback, exception_callback):
        pass

    def subscribe(self, **request):
        self.request = request
        return 0

    def start(self):
        pass

    def stop(self):
        pass

    def terminate(self):
        pass

    def is_connected(self):
        return False

    def block_for_close(self, timeout):
        start = pd.Timestamp(self.request["start"]).value
        symbol = self.request["symbols"][0]
        if self.scenario == "other_symbol":
            symbol = "MSFT"
        self.stream.write(dbn.Metadata("XNAS.ITCH", start, None, dbn.SType.INSTRUMENT_ID, None).encode())
        mapping = dbn.SymbolMappingMsg(0, 7, NOW.value, dbn.SType.RAW_SYMBOL, symbol,
                                       dbn.SType.INSTRUMENT_ID, "7", 2**64 - 1, 2**64 - 1)
        def bar(stamp, close=105_000_000_000):
            return dbn.OHLCVMsg(dbn.RType.OHLCV_1M, 2, 7, stamp,
                100_000_000_000, 110_000_000_000, 90_000_000_000, close, 10)
        bars = [bar(start), bar(END.value - 60 * 10**9), bar(END.value)]
        if self.scenario == "conflicting":
            bars.insert(1, bar(start, close=106_000_000_000))
        elif self.scenario == "malformed":
            bars[0] = bar(start, close=120_000_000_000)
        elif self.scenario == "misaligned":
            bars[0] = bar(start + 1)
        elif self.scenario == "empty_window":
            bars = [bar(END.value)]
        elif self.scenario == "before_start":
            bars.insert(0, bar(start - 60 * 10**9))
        records = [dbn.SystemMsg(NOW.value, "Subscription request 0 for ohlcv-1m data succeeded",
                                  dbn.SystemCode.SUBSCRIPTION_ACK), mapping, *bars,
                   dbn.SystemMsg(NOW.value, "Finished ohlcv-1m replay", dbn.SystemCode.REPLAY_COMPLETED)]
        if self.scenario == "incomplete":
            records.pop()
        for record in records:
            self.stream.write(bytes(record))
            try:
                self.callback(record)
            except Exception as exc:
                self.callback_error(exc)


@pytest.fixture(autouse=True)
def clocks(monkeypatch):
    monkeypatch.setattr(archive, "_utc_now", lambda: NOW)
    monkeypatch.setattr(replay, "_utc_now", lambda: NOW)
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024**3))


def publish(tmp_path, *, scenario="success", capture_fn=None):
    def capture(**kwargs):
        return replay.capture_replay(**kwargs, client_factory=lambda **kw: NativeClient(scenario=scenario, **kw))
    return archive.publish_session(tmp_path, symbol="AAPL", session=SESSION, api_key="synthetic",
                                   max_bytes=100000, capture_fn=capture_fn or capture)


def test_exact_action_window_native_bytes_and_restart_reuse(tmp_path, monkeypatch):
    item = publish(tmp_path)
    assert item["directory"] == tmp_path / "market-data/databento/stock-session-replay/XNAS.ITCH/ohlcv-1m/AAPL/2026-09-08"
    assert len(item["source_files"]) == 5
    assert all(path.is_file() for path in item["source_files"])
    assert item["frame"].timestamp.tolist() == [START, END - pd.Timedelta(minutes=1)]
    assert item["frame"].close.tolist() == [105.0, 105.0]
    assert item["delivery"]["raw_data_record_count"] == 3
    assert item["delivery"]["out_of_window_record_count"] == 1
    assert item["manifest"]["request"]["start"] == START.isoformat()
    assert item["manifest"]["request"]["end"] == END.isoformat()
    before = {path: (path.stat().st_mtime_ns, file_checksum(path)) for path in item["source_files"]}
    # Retention limits a new subscription; an old verified artifact remains valid.
    monkeypatch.setattr(archive, "_utc_now", lambda: NOW + pd.Timedelta(days=4))
    def forbidden(**kwargs):
        pytest.fail("Restart must not repeat provider acquisition")
    repeated = publish(tmp_path, capture_fn=forbidden)
    assert repeated["directory"] == item["directory"]
    assert before == {path: (path.stat().st_mtime_ns, file_checksum(path)) for path in item["source_files"]}
    found = archive.discover_partitions(tmp_path, symbols=["AAPL", "COST"])
    assert [row["symbol"] for row in found] == ["AAPL"]
    assert found[0]["manifest_path"] == item["directory"] / "manifest.json"
    assert not (tmp_path / "market-data/databento/us-equities").exists()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("filename", ["provider.dbn", "normalized.parquet", "delivery.json", "manifest.json", "receipt.json"])
def test_tampered_publication_fails_reuse_without_capture_or_overwrite(tmp_path, filename):
    item = publish(tmp_path)
    path = item["directory"] / filename
    with path.open("ab") as output:
        output.write(b"tampered")
    tampered = path.read_bytes()
    def forbidden(**kwargs):
        pytest.fail("An invalid existing publication must never be overwritten")
    with pytest.raises(archive.XnasReplayArchiveError):
        publish(tmp_path, capture_fn=forbidden)
    assert path.read_bytes() == tampered


def _rebind_payload(directory: Path, payload: str):
    manifest_path, receipt_path = directory / "manifest.json", directory / "receipt.json"
    manifest, receipt = json.loads(manifest_path.read_text()), json.loads(receipt_path.read_text())
    path = directory / manifest[payload]["path"]
    manifest[payload].update(size_bytes=path.stat().st_size, checksum_sha256=file_checksum(path))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt.update(manifest_checksum_sha256=file_checksum(manifest_path))
    receipt[f"{payload}_checksum_sha256"] = file_checksum(path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


@pytest.mark.parametrize("change", ["price", "extra_row", "missing_row", "other_symbol"])
def test_rehashed_normalized_payload_still_must_equal_native_source(tmp_path, change):
    item = publish(tmp_path)
    path = item["normalized_path"]
    frame = pd.read_parquet(path)
    if change == "price":
        frame.loc[0, "close"] = 106.0
    elif change == "extra_row":
        frame = pd.concat([frame, frame.iloc[[-1]].assign(timestamp=END)], ignore_index=True)
    elif change == "missing_row":
        frame = frame.iloc[[0]].copy()
    else:
        frame.loc[0, "symbol"] = "MSFT"
    frame.to_parquet(path, index=False)
    _rebind_payload(item["directory"], "normalized")
    with pytest.raises(archive.XnasReplayArchiveError, match="source/normalized"):
        archive.verify_partition(item["directory"], root=tmp_path)


def test_rehashed_delivery_cannot_shorten_the_session(tmp_path):
    item = publish(tmp_path)
    path = item["directory"] / "delivery.json"
    delivery = json.loads(path.read_text())
    delivery["source_request_start"] = (START + pd.Timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(delivery), encoding="utf-8")
    _rebind_payload(item["directory"], "delivery")
    with pytest.raises(archive.XnasReplayArchiveError, match="shortened"):
        archive.verify_partition(item["directory"], root=tmp_path)


@pytest.mark.parametrize("scenario", ["incomplete", "other_symbol", "conflicting", "malformed", "misaligned", "empty_window", "before_start"])
def test_failed_native_capture_is_preserved_and_never_discovered(tmp_path, scenario):
    with pytest.raises((archive.XnasReplayArchiveError, replay.ReplayCaptureError)):
        publish(tmp_path, scenario=scenario)
    assert list(tmp_path.rglob("provider.dbn"))
    assert not archive.partition_directory(tmp_path, "AAPL", SESSION).exists()
    assert archive.discover_partitions(tmp_path, symbols=["AAPL"]) == []


@pytest.mark.parametrize("session", ["2026-09-07", "2026-09-06", "2026-09-09", "2026-09-08T00:00:00", "2026-02-30", "../2026-09-08"])
def test_holiday_weekend_incomplete_and_malformed_session_rejected(tmp_path, session):
    with pytest.raises(archive.XnasReplayArchiveError):
        archive.session_bounds(session)
    assert not list(tmp_path.iterdir())


def test_action_bounds_use_pacific_clock_in_winter_and_summer():
    assert archive.session_bounds(SESSION) == (START, END)
    assert archive.session_bounds("2026-01-06") == (
        pd.Timestamp("2026-01-06T12:00:00Z"), pd.Timestamp("2026-01-07T01:00:00Z"))
    with pytest.raises(archive.XnasReplayArchiveError, match="completed"):
        archive.session_bounds(SESSION, observed_at=END - pd.Timedelta(nanoseconds=1))
    assert archive.session_bounds(SESSION, observed_at=END) == (START, END)


def test_free_space_checked_before_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    def forbidden(**kwargs):
        pytest.fail("Disk preflight must precede provider capture")
    with pytest.raises(archive.XnasReplayArchiveError, match="free space"):
        publish(tmp_path, capture_fn=forbidden)
    assert not list(tmp_path.rglob("provider.dbn"))


def test_capture_byte_cap_rechecked_independently(tmp_path):
    def oversized(**kwargs):
        kwargs["raw_path"].write_bytes(b"x" * 100001)
        return {}
    with pytest.raises(archive.XnasReplayArchiveError, match="byte budget"):
        publish(tmp_path, capture_fn=oversized)
    assert list(tmp_path.rglob("provider.dbn"))


def test_unfinished_published_directory_is_an_error(tmp_path):
    directory = archive.partition_directory(tmp_path, "AAPL", SESSION)
    directory.mkdir(parents=True)
    with pytest.raises(archive.XnasReplayArchiveError, match="metadata"):
        archive.discover_partitions(tmp_path, symbols=["AAPL"])


def test_staging_and_other_dataset_are_never_discovered(tmp_path):
    staging = tmp_path / "market-data/databento/stock-session-replay/XNAS.ITCH/ohlcv-1m/.staging/AAPL/2026-09-08"
    staging.mkdir(parents=True)
    (staging / "manifest.json").write_text("broken")
    other = tmp_path / "market-data/databento/stock-session-replay/EQUS.MINI/ohlcv-1m/AAPL/2026-09-08"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text("broken")
    assert archive.discover_partitions(tmp_path, symbols=["AAPL"]) == []
    with pytest.raises(archive.XnasReplayArchiveError, match="published archive"):
        archive.verify_partition(staging, root=tmp_path)
    with pytest.raises(archive.XnasReplayArchiveError, match="published archive"):
        archive.verify_partition(other, root=tmp_path)


def test_target_price_loader_combines_replay_and_history_with_verified_overlap(tmp_path):
    from ml.stock_target_prices import XNAS_STOCK_PRICE_SOURCE, load_stock_target_prices
    from test_stock_target_prices import archive_partition

    historical = archive_partition(tmp_path, "AAPL")
    replay_item = publish(tmp_path)
    bars, files, report = load_stock_target_prices(
        tmp_path, symbols=["AAPL"], source_contract=XNAS_STOCK_PRICE_SOURCE)
    assert len(bars) == 4
    assert len(files) == 9
    assert set(replay_item["source_files"]).issubset(files)
    assert {historical / filename for filename in (
        "provider.dbn.zst", "normalized.parquet", "manifest.json", "receipt.json")}.issubset(files)
    assert bars.loc[bars.timestamp.ge(START), ["timestamp", "open", "close"]].to_dict("records") == [
        {"timestamp": START, "open": 100.0, "close": 105.0},
        {"timestamp": END - pd.Timedelta(minutes=1), "open": 100.0, "close": 105.0},
    ]
    assert report["source_contract"] == XNAS_STOCK_PRICE_SOURCE
    assert report["dataset"] == "XNAS.ITCH"
    assert any(row.get("delivery_mode") == "live-intraday-replay" for row in report["partitions"])
    assert {row["path"] for row in report["files"]} == {str(path) for path in files}

    # Historical catch-up may eventually deliver the same completed session.
    overlap = archive_partition(tmp_path, "AAPL", suffix="historical-caught-up")
    frame = replay_item["frame"].loc[:, ["timestamp", "symbol", "open", "close"]].rename(
        columns={"timestamp": "ts_event"})
    frame.to_parquet(overlap / "normalized.parquet", index=False)
    manifest_path = overlap / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["request"].update(start=START.isoformat(), end=END.isoformat())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _rebind_payload(overlap, "normalized")
    caught_up, caught_up_files, _ = load_stock_target_prices(
        tmp_path, symbols=["AAPL"], source_contract=XNAS_STOCK_PRICE_SOURCE)
    pd.testing.assert_frame_equal(caught_up, bars)
    assert len(caught_up_files) == 13
    assert set(replay_item["source_files"]).issubset(caught_up_files)

    frame.loc[0, "close"] = 106.0
    frame.to_parquet(overlap / "normalized.parquet", index=False)
    _rebind_payload(overlap, "normalized")
    with pytest.raises(RuntimeError, match="conflicting minute observations"):
        load_stock_target_prices(tmp_path, symbols=["AAPL"], source_contract=XNAS_STOCK_PRICE_SOURCE)


def test_canonical_target_price_loader_never_ingests_xnas_replay(tmp_path):
    from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE, load_stock_target_prices

    replay_item = publish(tmp_path)
    with pytest.raises(RuntimeError, match="Canonical stock minute prices are missing"):
        load_stock_target_prices(tmp_path, symbols=["AAPL"], source_contract=CANONICAL_STOCK_PRICE_SOURCE)
    canonical = tmp_path / "stocks/AAPL/bars/1m/databento/normalized/AAPL_ohlcv-1m_1m.parquet"
    canonical.parent.mkdir(parents=True)
    pd.DataFrame({"timestamp": [START], "open": [110.0], "close": [111.0],
                  "provider_dataset": ["EQUS.MINI"]}).to_parquet(canonical, index=False)
    bars, files, report = load_stock_target_prices(
        tmp_path, symbols=["AAPL"], source_contract=CANONICAL_STOCK_PRICE_SOURCE)
    assert bars[["timestamp", "open", "close"]].to_dict("records") == [
        {"timestamp": START, "open": 110.0, "close": 111.0}]
    assert files == (canonical,)
    assert set(files).isdisjoint(replay_item["source_files"])
    assert report["dataset"] == "EQUS.MINI"
    assert report["partitions"] == []
    # Explicit canonical mode must not even inspect an unrelated replay archive.
    replay_item["raw_path"].write_bytes(b"corrupt unrelated XNAS archive")
    reread, _, _ = load_stock_target_prices(
        tmp_path, symbols=["AAPL"], source_contract=CANONICAL_STOCK_PRICE_SOURCE)
    pd.testing.assert_frame_equal(reread, bars)
