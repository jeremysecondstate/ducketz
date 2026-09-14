import json
from datetime import date

import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml.gameplan_evaluation import evaluate_forecasts
from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, build_stock_training_groups
from ml.stock_target_prices import (
    CANONICAL_STOCK_PRICE_SOURCE, XNAS_STOCK_PRICE_SOURCE,
    independent_price_identity, load_stock_target_prices,
)


def archive_partition(root, symbol, *, suffix="first", open_price=100.0, close_price=100.5):
    directory = root / "market-data/databento/us-equities/XNAS.ITCH/ohlcv-1m" / symbol / "windows" / suffix
    directory.mkdir(parents=True)
    frame = pd.DataFrame({"ts_event": pd.to_datetime(["2026-08-17T11:00Z", "2026-08-17T23:59Z"]),
                          "open": [open_price, 101.0], "close": [close_price, 102.0], "symbol": symbol})
    frame.to_parquet(directory / "normalized.parquet", index=False)
    (directory / "provider.dbn.zst").write_bytes(b"provider fixture bytes")
    normalized, raw = directory / "normalized.parquet", directory / "provider.dbn.zst"
    manifest = {"schema_version": "databento-cold-start-partition-v1", "published_at": "2026-08-18T01:00Z",
        "request": {"request_id": f"{symbol}-{suffix}", "dataset": "XNAS.ITCH", "schema": "ohlcv-1m",
                    "stype_in": "raw_symbol", "symbol_scope": [symbol], "start": "2026-08-17", "end": "2026-08-18"},
        "normalized": {"path": normalized.name, "size_bytes": normalized.stat().st_size,
                       "checksum_sha256": file_checksum(normalized), "row_count": len(frame), "timestamp_column": "ts_event"},
        "raw": {"path": raw.name, "size_bytes": raw.stat().st_size, "checksum_sha256": file_checksum(raw)}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "receipt.json").write_text(json.dumps({"schema_version": "databento-cold-start-receipt-v1",
        "request_id": manifest["request"]["request_id"], "manifest_checksum_sha256": file_checksum(directory / "manifest.json"),
        "normalized_checksum_sha256": file_checksum(normalized)}))
    return directory


def test_explicit_archive_verifies_all_sources_and_deduplicates_identical_overlap(tmp_path):
    symbols = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST")
    for symbol in symbols:
        archive_partition(tmp_path, symbol)
    archive_partition(tmp_path, "COST", suffix="overlap")
    bars, files, report = load_stock_target_prices(tmp_path, symbols=symbols, source_contract=XNAS_STOCK_PRICE_SOURCE)
    assert len(bars) == 14
    assert len(files) == 32
    assert report["native_archive_partitions_verified"] == 8
    assert report["dataset"] == "XNAS.ITCH"
    assert report["by_symbol"]["COST"]["rows"] == 2
    assert all(item["sha256"] == file_checksum(item["path"]) for item in report["files"])
    sources = pd.DataFrame([{"symbol": "COST", "action_date": date(2026, 8, 17),
        "decision_timestamp": pd.Timestamp("2026-08-15T00:05Z"), "information_available_at": pd.Timestamp("2026-08-15T00:05Z")}])
    groups = build_stock_training_groups(sources, feature_columns=(), minute_bars=bars, available_at="2026-08-18T01:00Z")
    target = groups["1d"].iloc[0]
    assert target.target_price_source_contract == XNAS_STOCK_PRICE_SOURCE
    assert target.target_price_dataset == "XNAS.ITCH"
    assert target.observed_return == pytest.approx(0.02)
    with pytest.raises(RuntimeError, match="disagree with the selected"):
        build_stock_training_groups(sources, feature_columns=(), minute_bars=bars, available_at="2026-08-18T01:00Z",
                                    price_source_contract=CANONICAL_STOCK_PRICE_SOURCE)


@pytest.mark.parametrize("damaged", ["provider.dbn.zst", "normalized.parquet", "manifest.json"])
def test_altered_archive_evidence_is_rejected(tmp_path, damaged):
    directory = archive_partition(tmp_path, "COST")
    with (directory / damaged).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(RuntimeError):
        load_stock_target_prices(tmp_path, symbols=("COST",), source_contract=XNAS_STOCK_PRICE_SOURCE)


def test_conflicting_archive_overlap_is_not_selected_silently(tmp_path):
    archive_partition(tmp_path, "COST")
    archive_partition(tmp_path, "COST", suffix="revision", open_price=110.0)
    with pytest.raises(RuntimeError, match="conflicting minute"):
        load_stock_target_prices(tmp_path, symbols=("COST",), source_contract=XNAS_STOCK_PRICE_SOURCE)


def test_missing_archive_symbol_never_falls_back_to_canonical_prices(tmp_path):
    archive_partition(tmp_path, "AAPL")
    directory = tmp_path / "stocks/COST/bars/1m/databento/normalized"
    directory.mkdir(parents=True)
    pd.DataFrame({"timestamp": [pd.Timestamp("2026-08-17T11:00Z")], "open": [100.0], "close": [102.0],
                  "provider_dataset": ["EQUS.MINI"]}).to_parquet(directory / "COST_ohlcv-1m_1m.parquet", index=False)
    with pytest.raises(RuntimeError, match="archive is missing: COST"):
        load_stock_target_prices(tmp_path, symbols=("AAPL", "COST"), source_contract=XNAS_STOCK_PRICE_SOURCE)


def test_canonical_source_requires_its_declared_dataset(tmp_path):
    directory = tmp_path / "stocks/COST/bars/1m/databento/normalized"
    directory.mkdir(parents=True)
    pd.DataFrame({"timestamp": [pd.Timestamp("2026-08-17T11:00Z")], "open": [100.0], "close": [102.0],
                  "provider_dataset": ["XNAS.ITCH"]}).to_parquet(directory / "COST_ohlcv-1m_1m.parquet", index=False)
    with pytest.raises(RuntimeError, match="dataset differs"):
        load_stock_target_prices(tmp_path, symbols=("COST",))


def test_undefined_price_row_is_disclosed_without_modifying_native_evidence(tmp_path):
    directory = archive_partition(tmp_path, "COST", open_price=float('nan'), close_price=float('nan'))
    before = {p.name:file_checksum(p) for p in directory.iterdir()}
    bars,_,report = load_stock_target_prices(tmp_path,symbols=("COST",),source_contract=XNAS_STOCK_PRICE_SOURCE)
    assert len(bars)==1 and bars.iloc[0].open==101
    assert report['missing_price_rows_by_symbol']=={'COST':1}
    assert report['missing_price_examples']==[{'symbol':'COST','timestamp':'2026-08-17T11:00:00+00:00'}]
    assert before=={p.name:file_checksum(p) for p in directory.iterdir()}


@pytest.mark.parametrize('invalid',[float('nan'),float('inf'),0,-1])
def test_partial_or_invalid_price_observation_still_fails_closed(tmp_path,invalid):
    archive_partition(tmp_path,"COST",open_price=invalid)
    with pytest.raises(RuntimeError,match='invalid observed open'):
        load_stock_target_prices(tmp_path,symbols=("COST",),source_contract=XNAS_STOCK_PRICE_SOURCE)


def test_prospective_evaluation_never_changes_a_saved_forecast_price_source():
    base = {"symbol": "COST", "model_group": "1d", "route": "1d@D+1", "action_date": "2026-08-17",
            "target_window_start": pd.Timestamp("2026-08-17T11:00Z"), "target_window_end": pd.Timestamp("2026-08-18T00:00Z"),
            "calibrated_probability": 0.7, "source_gameplan_run": "saved", "target_contract_version": STOCK_TARGET_CONTRACT_VERSION}
    old = {**base, "id": "existing-canonical"}
    new = {**base, "id": "archive", "target_price_source_contract": XNAS_STOCK_PRICE_SOURCE, "target_price_dataset": "XNAS.ITCH"}
    outcomes = pd.DataFrame([{**new, "target": 1, "observed_return": 0.02}])
    result = evaluate_forecasts(pd.DataFrame([old, new]), observed_groups={"xnas": outcomes}, evaluated_at="2026-08-18T01:00Z")
    result = result.set_index("source_forecast_id")
    assert result.loc["existing-canonical", "evaluation_status"] == "MATURE_AWAITING_DATA"
    assert result.loc["archive", "evaluation_status"] == "EVALUATED"
    with pytest.raises(RuntimeError, match="source and dataset disagree"):
        independent_price_identity({"target_price_source_contract": XNAS_STOCK_PRICE_SOURCE, "target_price_dataset": "EQUS.MINI"})
