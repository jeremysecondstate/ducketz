import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import file_checksum
import ml.gameplan_archive_seconds as archive


START = pd.Timestamp("2026-09-22T11:00:00Z")
END = START + pd.Timedelta(minutes=3)
CUTOFF = pd.Timestamp("2026-09-23T01:00:00Z")


def bars(seconds=(7, 42), *, prices=(100.0, 101.0), volumes=(2, 3)):
    return pd.DataFrame({"timestamp": [START + pd.Timedelta(seconds=s) for s in seconds],
                         "open": prices, "high": prices, "low": prices, "close": prices,
                         "volume": volumes})


def minute(*, price=100.0, close=101.0, volume=5, offset=0):
    return pd.DataFrame({"timestamp": [START + pd.Timedelta(minutes=offset)], "open": [price],
                         "high": [max(price, close)], "low": [min(price, close)],
                         "close": [close], "volume": [volume]})


def compare(seconds=None, minutes=None, **kwargs):
    return archive.compare_second_minute_overlap(
        bars() if seconds is None else seconds, minute() if minutes is None else minutes,
        coverage=kwargs.pop("coverage", [(START, END)]),
        available_at=kwargs.pop("available_at", CUTOFF), **kwargs)


def test_sparse_seconds_compare_exactly_without_creating_boundary_observations():
    result = compare()
    assert result["overlap_minutes"] == result["exact_ohlcv_overlap_minutes"] == 1
    assert result["exact_field_matches"] == dict.fromkeys(archive._VALUES, 1)
    assert result["second_rows"] == 2
    assert result["first_second_at_minute_start"] == result["last_second_completes_at_minute_end"] == 0
    assert result["minutes_with_60_observed_seconds"] == result["added_training_rows"] == result["synthetic_rows"] == 0
    assert result["second_range"] == {"first": "2026-09-22T11:00:07+00:00", "last": "2026-09-22T11:00:42+00:00"}


@pytest.mark.parametrize("column", archive._VALUES)
def test_every_exact_ohlcv_conflict_fails_closed(column):
    native = minute()
    # Keep each native bar internally coherent so the resolution check is tested.
    native.loc[0, column] += 1 if column in {"high", "volume"} else -0.01
    if column == "open":
        native.loc[0, "low"] = native.loc[0, "open"]
    with pytest.raises(ValueError, match="Second/minute native OHLCV conflict"):
        compare(minutes=native)


def test_volume_comparison_does_not_round_integers_through_float():
    large = 2**53
    result = compare(seconds=bars(volumes=(large, 1)), minutes=minute(volume=large+1))
    assert result["exact_field_matches"]["volume"] == 1
    with pytest.raises(ValueError, match="OHLCV conflict"):
        compare(seconds=bars(volumes=(large, 1)), minutes=minute(volume=large))


def test_seconds_without_native_minutes_are_reported_unused():
    result = compare(minutes=minute(offset=1))
    assert result["status"] == "NO_ELIGIBLE_OVERLAP"
    assert result["only_seconds_minutes_unused"] == 1
    assert result["only_seconds_examples"] == [{"minute": "2026-09-22T11:00:00+00:00",
        "open_observed_at": "2026-09-22T11:00:07+00:00", "close_observed_at": "2026-09-22T11:00:43+00:00"}]


def test_entire_minute_must_have_requested_acquisition_coverage():
    result = compare(coverage=[(START + pd.Timedelta(seconds=1), END)])
    assert result["second_minutes_outside_complete_request"] == 1
    assert result["overlap_minutes"] == result["only_seconds_minutes_unused"] == 0
    result = compare(coverage=[(START, START + pd.Timedelta(seconds=30)),
                               (START + pd.Timedelta(seconds=30), END)])
    assert result["overlap_minutes"] == 1


def test_cutoff_requires_completed_second_and_minute():
    result = compare(available_at=START + pd.Timedelta(seconds=42))
    assert result["second_rows"] == 1
    assert result["second_rows_after_cutoff"] == result["minute_rows_after_cutoff"] == 1
    assert result["second_minutes_after_cutoff"] == 1
    assert result["overlap_minutes"] == 0
    assert compare(available_at=START + pd.Timedelta(minutes=1))["overlap_minutes"] == 1


def test_native_undefined_seconds_exclude_whole_bucket_and_preserve_input():
    seconds = bars()
    seconds.loc[0, list(archive._VALUES[:-1])] = float("nan")
    before = seconds.copy(deep=True)
    result = compare(seconds=seconds)
    pd.testing.assert_frame_equal(seconds, before)
    assert result["undefined_second_rows"] == result["second_minutes_with_undefined_observation"] == 1
    assert result["undefined_second_examples"] == ["2026-09-22T11:00:07+00:00"]
    assert result["overlap_minutes"] == 0


def test_native_undefined_minute_is_reported_without_false_nonoverlap():
    native = minute()
    native.loc[0, list(archive._VALUES[:-1])] = float("nan")
    result = compare(minutes=native)
    assert result["undefined_minute_rows"] == result["second_minutes_with_undefined_native_counterpart"] == 1
    assert result["overlap_minutes"] == result["only_seconds_minutes_unused"] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1])
def test_partial_undefined_and_invalid_ohlc_are_not_silently_dropped(bad):
    seconds = bars()
    seconds.loc[0, "open"] = bad
    with pytest.raises(ValueError, match="undefined native OHLC|Invalid observed native OHLC"):
        compare(seconds=seconds)


@pytest.mark.parametrize("volume", [-1, 0.5, float("inf"), float("nan")])
def test_invalid_volume_fails(volume):
    seconds = bars(volumes=(volume, 3))
    with pytest.raises(ValueError, match="Invalid observed native volume"):
        compare(seconds=seconds)


def test_identical_rows_deduplicate_but_conflicting_same_clock_fails():
    seconds = pd.concat([bars(), bars()], ignore_index=True)
    assert compare(seconds=seconds)["second_rows"] == 2
    seconds.loc[2, "volume"] = 8
    with pytest.raises(ValueError, match="Conflicting same-clock"):
        compare(seconds=seconds)
    native = pd.concat([minute(), minute(volume=6)], ignore_index=True)
    with pytest.raises(ValueError, match="Conflicting same-clock"):
        compare(minutes=native)


def test_naive_clocks_and_subsecond_observations_fail():
    with pytest.raises(ValueError, match="timezone aware"):
        compare(available_at="2026-09-23")
    seconds = bars()
    seconds.loc[0, "timestamp"] += pd.Timedelta(milliseconds=1)
    with pytest.raises(ValueError, match="native interval start"):
        compare(seconds=seconds)


def write_partition(root, schema, frame, *, symbol="COST", suffix="first", header=None):
    directory = root / "market-data/databento/us-equities/XNAS.ITCH" / schema / symbol / "windows" / suffix
    directory.mkdir(parents=True)
    frame = frame.rename(columns={"timestamp": "ts_event"}).assign(symbol=symbol)
    normalized, raw = directory / "normalized.parquet", directory / "provider.dbn.zst"
    frame.to_parquet(normalized, index=False)
    native = {"dataset": "XNAS.ITCH", "schema": schema, "stype_in": "raw_symbol", "symbols": [symbol],
              "start": START.value, "end": END.value, "partial": [], "not_found": []}
    native.update(header or {})
    raw.write_text(json.dumps(native))
    manifest = {"schema_version": "databento-cold-start-partition-v1", "published_at": "2026-09-23T00:05:00Z",
        "request": {"request_id": f"{symbol}-{schema}-{suffix}", "dataset": "XNAS.ITCH", "schema": schema,
                    "stype_in": "raw_symbol", "symbol_scope": [symbol], "start": START.isoformat(), "end": END.isoformat()},
        "normalized": {"path": normalized.name, "size_bytes": normalized.stat().st_size,
                       "checksum_sha256": file_checksum(normalized), "row_count": len(frame), "timestamp_column": "ts_event",
                       "earliest_timestamp": frame.ts_event.min().isoformat(), "latest_timestamp": frame.ts_event.max().isoformat()},
        "raw": {"path": raw.name, "size_bytes": raw.stat().st_size, "checksum_sha256": file_checksum(raw)}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    write_receipt(directory, manifest)
    return directory


def write_receipt(directory, manifest):
    (directory / "receipt.json").write_text(json.dumps({"schema_version": "databento-cold-start-receipt-v1",
        "published_at": manifest["published_at"], "request_id": manifest["request"]["request_id"],
        "manifest_checksum_sha256": file_checksum(directory / "manifest.json"),
        "normalized_checksum_sha256": manifest["normalized"]["checksum_sha256"],
        "raw_checksum_sha256": manifest["raw"]["checksum_sha256"]}))


@pytest.fixture
def native_headers(monkeypatch):
    # Only raw header decoding is substituted. Native receipt, both SHA256s,
    # Parquet schema/row/range checks and production comparison remain real.
    def read_header(path):
        return SimpleNamespace(metadata=SimpleNamespace(**json.loads(path.read_text())),
                               reader=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(archive.db.DBNStore, "from_file", read_header)


def fixtures(root):
    return (write_partition(root, "ohlcv-1s", bars()), write_partition(root, "ohlcv-1m", minute()))


def verify(root, **kwargs):
    return archive.verify_second_minute_overlap(root, symbols=("COST",), available_at=CUTOFF, **kwargs)


def test_loader_binds_all_sources_with_streaming_minute_boundary(native_headers, tmp_path, monkeypatch):
    directories = fixtures(tmp_path)
    monkeypatch.setattr(archive, "_BATCH_ROWS", 1)
    before = {str(p): file_checksum(p) for d in directories for p in d.iterdir()}
    report, files = verify(tmp_path)
    assert report["status"] == "VERIFIED"
    assert report["native_archive_partitions_verified"] == 2
    assert report["by_symbol"]["COST"]["overlap_minutes"] == 1
    assert len(files) == 8
    assert report["raw_record_replay"] == "NOT_PERFORMED"
    assert {str(p): file_checksum(p) for d in directories for p in d.iterdir()} == before
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("payload", ["provider.dbn.zst", "normalized.parquet", "manifest.json"])
def test_loader_rejects_tampered_native_evidence(native_headers, tmp_path, payload):
    directory, _ = fixtures(tmp_path)
    with (directory / payload).open("ab") as output:
        output.write(b"changed")
    with pytest.raises(RuntimeError):
        verify(tmp_path)


@pytest.mark.parametrize("header", [{"dataset": "OTHER"}, {"schema": "ohlcv-1m"}, {"symbols": ["MU"]},
                                  {"stype_in": "instrument_id"}, {"end": END.value+1}, {"not_found": ["COST"]}])
def test_loader_rejects_request_header_mismatch(native_headers, tmp_path, header):
    write_partition(tmp_path, "ohlcv-1s", bars(), header=header)
    write_partition(tmp_path, "ohlcv-1m", minute())
    with pytest.raises(ValueError, match="Native DBN header"):
        verify(tmp_path)


def test_loader_reports_partial_symbology_and_ignores_unselected_symbols(native_headers, tmp_path):
    write_partition(tmp_path, "ohlcv-1s", bars(), header={"partial": ["COST"]})
    write_partition(tmp_path, "ohlcv-1m", minute())
    write_partition(tmp_path, "ohlcv-1s", bars(prices=(2, 3)), symbol="OTHER")
    result, files = verify(tmp_path)
    assert result["partitions"][0]["native_partial_symbology"] == ["COST"]
    assert result["symbols"] == ["COST"] and len(files) == 8


def test_loader_rejects_mismatched_row_metadata_even_with_fresh_receipt(native_headers, tmp_path):
    directory, _ = fixtures(tmp_path)
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["normalized"]["row_count"] += 1
    path.write_text(json.dumps(manifest))
    write_receipt(directory, manifest)
    with pytest.raises(RuntimeError, match="row_count"):
        verify(tmp_path)


def test_loader_rejects_receipt_raw_hash_and_future_publication(native_headers, tmp_path):
    directory, _ = fixtures(tmp_path)
    receipt = directory / "receipt.json"
    data = json.loads(receipt.read_text())
    data["raw_checksum_sha256"] = "wrong"
    receipt.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="receipt/raw publication binding"):
        verify(tmp_path)
    write_receipt(directory, json.loads((directory / "manifest.json").read_text()))
    with pytest.raises(ValueError, match="after cutoff"):
        archive.verify_second_minute_overlap(tmp_path, symbols=("COST",), available_at=END)


def test_loader_deduplicates_identical_overlapping_second_acquisitions(native_headers, tmp_path):
    fixtures(tmp_path)
    write_partition(tmp_path, "ohlcv-1s", bars(), suffix="overlap")
    result, files = verify(tmp_path)
    assert result["by_symbol"]["COST"]["second_rows"] == 2
    assert len(files) == 12


def test_loader_rejects_conflicting_overlapping_second_acquisitions(native_headers, tmp_path):
    fixtures(tmp_path)
    write_partition(tmp_path, "ohlcv-1s", bars(volumes=(6, 3)), suffix="overlap")
    with pytest.raises(ValueError, match="Conflicting same-clock"):
        verify(tmp_path)


def test_loader_requires_both_schemas_and_unique_universe(native_headers, tmp_path):
    write_partition(tmp_path, "ohlcv-1m", minute())
    with pytest.raises(ValueError, match="archive is missing"):
        verify(tmp_path)
    with pytest.raises(ValueError, match="unique configured universe"):
        archive.verify_second_minute_overlap(tmp_path, symbols=("COST", "cost"), available_at=CUTOFF)
