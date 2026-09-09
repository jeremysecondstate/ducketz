from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from datafetching import databento_opra_history as history
from datafetching import databento_opra_replay as replay
from ml.artifacts import file_checksum
from ml.strategy_selection import opra_cache


DAY = "2026-09-04"
START = "2026-09-04T13:30:00+00:00"
END = "2026-09-04T20:01:00+00:00"


@pytest.fixture
def publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source.dbn"
    source.write_bytes(b"native-replay-with-control-and-mapping-records")
    rows = pd.DataFrame({
        "ts_recv": pd.to_datetime([
            "2026-09-04T13:29:00Z", START, "2026-09-04T20:00:00Z", END,
        ]),
        # CBBO samples can retain an older event time or no event time.
        "ts_event": pd.to_datetime(["2026-09-03T00:00:00Z", None, None, None]),
        "publisher_id": [1] * 4,
        "instrument_id": [2] * 4,
        "symbol": ["AAPL  260904C00200000"] * 4,
    })
    delivery = {
        "schema_version": replay.DELIVERY_VERSION,
        "mode": replay.MODE,
        "dataset": history.DATASET,
        "schema": "cbbo-1m",
        "symbols": ["AAPL.OPT"],
        "stype_in": "parent",
        "source_request_start": "2026-09-04T12:30:00+00:00",
        "requested_start": "2026-09-04T12:30:00+00:00",
        "requested_end": "2026-09-04T21:00:00+00:00",
        "connection_started_at": "2026-09-05T08:00:00+00:00",
        "finished_at": "2026-09-05T08:00:05+00:00",
        "subscription_id": 0,
        "subscription_ack": [{"code": 1, "msg": "Subscription request 0 for cbbo-1m data succeeded", "ts_event": 1}],
        "replay_completed": [{"code": 3, "msg": "Finished cbbo-1m replay", "ts_event": 2}],
        "completed": True,
        "error_messages": [],
        "callback_errors": [],
        "reconnect_count": 0,
        "slow_reader_behavior": "warn",
        "reconnect_policy": "none",
        "raw_format": "dbn",
        "transport_compression": "zstd",
        "raw_bytes": source.stat().st_size,
        "raw_sha256": file_checksum(source),
        "data_record_count": 4,
    }

    class Store:
        def to_parquet(self, path, *, schema, map_symbols):
            assert schema == delivery["schema"]
            assert map_symbols is True
            rows.to_parquet(path, index=False)

    # The capture helper's tests exercise native controls and mappings. Here
    # keep its real receipt validator, file hashing, and scope checks while
    # isolating the writer from DBN fixture construction.
    monkeypatch.setattr(replay, "_inspect_native", lambda *_: {"data_record_count": 4})
    monkeypatch.setattr(history, "_load_dbn_store", lambda _: Store())
    kwargs = dict(
        datastore_root=tmp_path,
        entitlement={"entitlements": {"cbbo-1m": {"source": "account-plan"}}},
        schema="cbbo-1m", day=DAY, symbols=("AAPL.OPT",),
        request_start=START, request_end=END, segment="live-session",
        provider_file=source, provider_delivery=delivery,
    )
    return kwargs, rows


def _destination(kwargs):
    return history.partition_directory(
        kwargs["datastore_root"], schema=kwargs["schema"], day=DAY,
        symbols=kwargs["symbols"], segment="live-session",
    )


def test_live_replay_publication_retains_raw_and_filters_receive_clock(publication):
    kwargs, _ = publication
    original = kwargs["provider_file"].read_bytes()
    manifest = history._download_partition(None, **kwargs)
    directory = _destination(kwargs)
    assert manifest["provider_delivery"]["mode"] == "live-intraday-replay"
    assert manifest["raw"]["path"] == "provider.dbn"
    assert (directory / "provider.dbn").read_bytes() == original
    assert kwargs["provider_file"].read_bytes() == original
    assert manifest["normalized"]["row_count"] == 2
    assert manifest["normalized"]["partition_timestamp_column"] == "ts_recv"
    assert manifest["time_segment"] == "live-session"
    assert manifest["partition_start"] == START
    assert manifest["partition_end"] == END
    evidence = manifest["provider_delivery"]["normalization_filter"]
    assert evidence["before_start_rows_removed"] == 1
    assert evidence["at_or_after_end_rows_removed"] == 1
    assert evidence["input_row_count"] == 4
    assert history.verify_partition(directory, datastore_root=kwargs["datastore_root"])["manifest"] == manifest


@pytest.mark.parametrize(("field", "value"), [
    ("completed", False), ("error_messages", ["start outside replay window"]),
    ("callback_errors", ["reader lost data"]), ("reconnect_count", 1),
    ("replay_completed", []), ("subscription_ack", []),
    ("symbols", ["MSFT.OPT"]), ("raw_sha256", "0" * 64),
    ("requested_end", "2026-09-04T19:00:00+00:00"),
])
def test_live_replay_rejects_incomplete_or_mismatched_evidence(publication, field, value):
    kwargs, _ = publication
    kwargs["provider_delivery"][field] = value
    with pytest.raises(history.OpraSyncError, match="replay evidence is invalid"):
        history._download_partition(None, **kwargs)
    assert not _destination(kwargs).exists()


@pytest.mark.parametrize("symbol", [None, "   "])
def test_live_replay_rejects_missing_symbol_mapping(publication, symbol):
    kwargs, rows = publication
    rows["symbol"] = symbol
    with pytest.raises(history.OpraSyncError, match="symbol mapping"):
        history._download_partition(None, **kwargs)
    assert not _destination(kwargs).exists()


def test_live_replay_empty_target_is_failure_not_no_data(publication):
    kwargs, rows = publication
    rows["ts_recv"] = pd.Timestamp("2026-09-04T12:00:00Z")
    with pytest.raises(history.OpraSyncError, match="no records in the requested interval") as failure:
        history._download_partition(None, **kwargs)
    assert not isinstance(failure.value, history.OpraNoDataError)
    assert not _destination(kwargs).exists()


def test_live_replay_rejects_null_partition_clock(publication):
    kwargs, rows = publication
    rows.loc[1, "ts_recv"] = pd.NaT
    with pytest.raises(history.OpraSyncError, match="null partition timestamps"):
        history._download_partition(None, **kwargs)


@pytest.mark.parametrize("segment", [None, "", "full-day"])
def test_live_replay_never_publishes_as_full_day(publication, segment):
    kwargs, _ = publication
    kwargs["segment"] = segment
    with pytest.raises(history.OpraSyncError, match="scoped strategy-schema segment"):
        history._download_partition(None, **kwargs)


@pytest.mark.parametrize("tamper", ["interval", "counts", "completion", "scope"])
def test_verify_replay_rejects_resealed_invalid_provenance(publication, tamper):
    kwargs, _ = publication
    manifest = copy.deepcopy(history._download_partition(None, **kwargs))
    directory = _destination(kwargs)
    if tamper == "interval":
        manifest["partition_start"] = "2026-09-04T00:00:00+00:00"
    elif tamper == "counts":
        manifest["provider_delivery"]["normalization_filter"]["input_row_count"] += 1
    elif tamper == "completion":
        manifest["provider_delivery"]["replay_completed"] = []
    else:
        manifest["request"]["symbols"] = ["MSFT.OPT"]
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt_path = directory / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["manifest_checksum_sha256"] = file_checksum(manifest_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(history.OpraSyncError):
        history.verify_partition(directory, datastore_root=kwargs["datastore_root"])


@pytest.mark.parametrize("schema", ["definition", "ohlcv-1h"])
def test_replay_non_cbbo_filter_uses_event_clock(tmp_path: Path, schema):
    path = tmp_path / "normalized.parquet"
    pd.DataFrame({
        "ts_event": pd.to_datetime([START, END]),
        "ts_recv": pd.to_datetime(["2026-09-05T01:00:00Z"] * 2),
    }).to_parquet(path, index=False)
    evidence = history._filter_live_replay_parquet(path, schema=schema, start=START, end=END)
    assert evidence["partition_timestamp_column"] == "ts_event"
    assert evidence["output_row_count"] == 1


def test_replay_publication_does_not_replace_existing_segment(publication):
    kwargs, _ = publication
    history._download_partition(None, **kwargs)
    directory = _destination(kwargs)
    original = (directory / "manifest.json").read_bytes()
    with pytest.raises(history.OpraSyncError, match="appeared during atomic publication"):
        history._download_partition(None, **kwargs)
    assert (directory / "manifest.json").read_bytes() == original


def _publish_historical(publication, *, segment=None):
    kwargs, rows = publication

    class HistoricalStore:
        def to_parquet(self, path, **_):
            rows.assign(ts_event=rows["ts_recv"]).to_parquet(path, index=False)

    def get_range(**request):
        request["path"].write_bytes(b"native-historical-source")
        return HistoricalStore()

    options = {**kwargs, "provider_file": None, "provider_delivery": None,
               "segment": segment, "request_start": DAY, "request_end": "2026-09-05"}
    history._download_partition(SimpleNamespace(timeseries=SimpleNamespace(get_range=get_range)), **options)
    return history.partition_directory(kwargs["datastore_root"], schema="cbbo-1m",
                                       symbols=kwargs["symbols"], day=DAY, segment=segment)


@pytest.mark.parametrize("reader", ["iterator", "cache"])
def test_historical_full_day_supersedes_only_matching_live_session(publication, reader):
    kwargs, _ = publication
    history._download_partition(None, **kwargs)
    replay_directory = _destination(kwargs)
    replay_manifest = (replay_directory / "manifest.json").read_bytes()
    historical = _publish_historical(publication)
    unrelated = _publish_historical(publication, segment="130000-140000")
    if reader == "iterator":
        selected = {item["directory"] for item in history.iter_verified_partitions(
            kwargs["datastore_root"], schemas=("cbbo-1m",))}
    else:
        selected = {item.directory for item in opra_cache._cheap_partitions(
            kwargs["datastore_root"], schema="cbbo-1m", symbols=("AAPL",))}
    assert selected == {historical, unrelated}
    assert (replay_directory / "manifest.json").read_bytes() == replay_manifest
    assert (replay_directory / "provider.dbn").is_file()


@pytest.mark.parametrize("reader", ["iterator", "cache"])
@pytest.mark.parametrize("corruption", ["raw", "manifest"])
def test_corrupt_historical_day_cannot_shadow_valid_replay(publication, reader, corruption):
    kwargs, _ = publication
    history._download_partition(None, **kwargs)
    historical = _publish_historical(publication)
    if corruption == "raw":
        (historical / "provider.dbn.zst").write_bytes(b"corrupted-historical-source")
    else:
        (historical / "manifest.json").write_text("broken", encoding="utf-8")
    with pytest.raises(history.OpraSyncError, match="Conflicting Historical full-day"):
        if reader == "iterator":
            tuple(history.iter_verified_partitions(kwargs["datastore_root"], schemas=("cbbo-1m",)))
        else:
            opra_cache._cheap_partitions(kwargs["datastore_root"], schema="cbbo-1m", symbols=("AAPL",))


@pytest.mark.parametrize("reader", ["iterator", "cache"])
def test_live_session_remains_selected_without_historical_replacement(publication, reader):
    kwargs, _ = publication
    history._download_partition(None, **kwargs)
    if reader == "iterator":
        selected = {item["directory"] for item in history.iter_verified_partitions(
            kwargs["datastore_root"], schemas=("cbbo-1m",))}
    else:
        selected = {item.directory for item in opra_cache._cheap_partitions(
            kwargs["datastore_root"], schema="cbbo-1m", symbols=("AAPL",))}
    assert selected == {_destination(kwargs)}
