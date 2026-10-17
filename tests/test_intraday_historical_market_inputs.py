import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from ml.intraday.historical_market_inputs import (
    HistoricalMarketInputs, VERSION, discover_sources, materialize_cme_history,
)

T = pd.Timestamp("2026-08-14T14:15:00Z")


def native(at=None, **changes):
    at = at or T-pd.Timedelta(seconds=1)
    result = {"ts_event": at-pd.Timedelta(microseconds=1), "ts_recv": at,
              "rtype": 10, "publisher_id": 1, "instrument_id": 99,
              "sequence": 7, "flags": 128, "symbol": "ES.v.0"}
    for i in range(10):
        result.update({f"bid_px_{i:02d}": 100.-i, f"ask_px_{i:02d}": 102.+i,
                       f"bid_sz_{i:02d}": 3, f"ask_sz_{i:02d}": 1})
    return result | changes


def archive(root, rows, *, selector="ES.v.0", folder="sample"):
    path = root / f"market-data/databento/cme/GLBX.MDP3/mbp-10/{selector.upper()}/windows/{folder}"
    path.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), path/"normalized.parquet", row_group_size=2)
    sha = hashlib.sha256((path/"normalized.parquet").read_bytes()).hexdigest()
    manifest = {"request": {"symbol_scope": [selector], "stype_in": "continuous", "dataset": "GLBX.MDP3", "schema": "mbp-10"},
                "normalized": {"path": "normalized.parquet", "checksum_sha256": sha,
                               "earliest_timestamp": min(x["ts_recv"] for x in rows).isoformat(),
                               "latest_timestamp": max(x["ts_recv"] for x in rows).isoformat()}}
    (path/"manifest.json").write_text(json.dumps(manifest))
    receipt = {"manifest_checksum_sha256": hashlib.sha256((path/"manifest.json").read_bytes()).hexdigest(),
               "published_at": "2026-08-17T01:00:00+00:00", "request_id": "test-history"}
    (path/"receipt.json").write_text(json.dumps(receipt))
    return path


def snapshot(root, rows, *, stamp=T, folder="snapshot"):
    path = root / f"pools/cme/snapshots/l2/databento/5m/{folder}"
    path.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), path/"snapshot.parquet")
    manifest = {"snapshot_for": stamp.isoformat(), "available_at": "2026-08-18T01:00:00+00:00",
                "snapshot_checksum_sha256": hashlib.sha256((path/"snapshot.parquet").read_bytes()).hexdigest()}
    (path/"manifest.json").write_text(json.dumps(manifest))
    return path


def build(root, **kw):
    return materialize_cme_history(root, start=T-pd.Timedelta(hours=1), end=T+pd.Timedelta(hours=1), roots=("ES",), **kw)


def field(joined, name, row=0):
    return joined["rows"][row][joined["feature_names"].index(name)]


def test_materializes_genuine_native_books_for_exact_prior_reference_close(tmp_path):
    archive(tmp_path, [native(), native(T+pd.Timedelta(seconds=1), bid_sz_00=500)])
    ref = build(tmp_path)
    frozen = json.loads(Path(ref["path"]).read_bytes())
    assert ref["rows"] == 2 and ref["bytes"] < 12000
    assert frozen["materialized_at"].startswith("20")
    assert frozen["inventory"]["sources"][0]["published_at"] == "2026-08-17T01:00:00+00:00"
    reader = HistoricalMarketInputs(tmp_path, ref["path"])
    joined = reader.join([T, T+pd.Timedelta(minutes=15)], cutoff=T)
    assert field(joined, "market_db_cme_es_depth_imbalance") == .5
    assert field(joined, "market_db_cme_es_spread_relative") == pytest.approx(2/101)
    assert field(joined, "market_db_cme_es_depth_imbalance__available") == 1
    assert field(joined, "market_db_cme_es_depth_imbalance__available", 1) == 0
    assert field(joined, "market_db_cme_es_trade_count__available") == 0
    assert joined["provenance"][0]["sources"]["db_cme_es"]["native_instrument_id"] == 99
    assert joined["version"] == VERSION
    assert "received_at" not in joined["provenance"][0]["sources"]["db_cme_es"]


@pytest.mark.parametrize("purpose", ["inference", "contemporaneous_evaluation", "backtest", "live"])
def test_reconstruction_cannot_claim_contemporaneous_inference(tmp_path, purpose):
    with pytest.raises(ValueError, match="contemporaneous"):
        HistoricalMarketInputs(tmp_path).join([T], cutoff=T, purpose=purpose)


def test_explicit_retrospective_validation_permitted_without_future_or_forward_fill(tmp_path):
    archive(tmp_path, [native()])
    reader = HistoricalMarketInputs(tmp_path, build(tmp_path)["path"])
    joined = reader.join([T-pd.Timedelta(minutes=15), T, T+pd.Timedelta(minutes=15)], cutoff=T+pd.Timedelta(minutes=15), purpose="retrospective_validation")
    assert [field(joined, "market_db_cme_es_depth_imbalance__available", i) for i in range(3)] == [0, 1, 0]


@pytest.mark.parametrize("marker", ["synthetic", "is_synthetic", "is_derived", "no_trade", "is_no_trade", "source_empty", "is_source_empty", "synthetic_open", "synthetic_close"])
@pytest.mark.parametrize("value", [True, "false", 1, 0])
def test_markers_rejected_before_projection(tmp_path, marker, value):
    archive(tmp_path, [native(**{marker: value})])
    assert build(tmp_path)["rows"] == 0


def test_explicit_false_markers_are_genuine(tmp_path):
    archive(tmp_path, [native(synthetic=False)])
    assert build(tmp_path)["rows"] == 1


@pytest.mark.parametrize("change", [{"synthetic_constituents": 1}, {"synthetic_constituents": False},
                                    {"sequence": 1.5}, {"instrument_id": .5}])
def test_derived_count_and_nonintegral_identity_are_rejected(tmp_path, change):
    archive(tmp_path, [native(**change)])
    assert build(tmp_path)["rows"] == 0


@pytest.mark.parametrize("change", [{"flags": 4}, {"ts_event": T+pd.Timedelta(seconds=2)}, {"rtype": 1},
                                    {"bid_sz_00": 4294967295, **{f"bid_sz_{i:02d}": 4294967295 for i in range(1,10)}}])
def test_invalid_books_gaps_and_future_exchange_clocks_are_not_fabricated(tmp_path, change):
    archive(tmp_path, [native(**change)])
    assert build(tmp_path)["rows"] == 0


def test_conflicting_latest_duplicate_is_excluded_but_exact_duplicates_deduplicate(tmp_path):
    archive(tmp_path, [native(), native()], folder="one")
    assert build(tmp_path)["rows"] == 1
    archive(tmp_path, [native(bid_sz_00=44)], folder="two")
    ref = build(tmp_path)
    assert ref["rows"] == 0
    assert ref["counts"]["conflicting_decisions_excluded"] == 1


def test_same_clock_different_contract_cannot_win_by_sequence(tmp_path):
    archive(tmp_path, [native(), native(instrument_id=101, sequence=99), native(instrument_id=101, sequence=100)])
    assert build(tmp_path)["rows"] == 0


def test_snapshot_uses_original_quote_clock_not_later_snapshot_or_publication(tmp_path):
    row = native(T-pd.Timedelta(minutes=31), provider_schema="mbp-10", provider_stype_in="continuous",
                 provider_symbol="ES.v.0", cme_context_group="context")
    snapshot(tmp_path, [row])
    ref = build(tmp_path)
    joined = HistoricalMarketInputs(tmp_path, ref["path"]).join([T-pd.Timedelta(minutes=30), T], cutoff=T)
    assert [field(joined, "market_db_cme_es_depth_imbalance__available", i) for i in range(2)] == [1, 0]


def test_source_hash_changes_and_resource_bounds_fail_before_output(tmp_path):
    path = archive(tmp_path, [native()])
    with pytest.raises(ValueError, match="resource bounds"):
        build(tmp_path, bounds={"max_rows": 0})
    assert not (tmp_path/"intraday").exists()
    pq.write_table(pa.Table.from_pylist([native(bid_sz_00=99)]), path/"normalized.parquet")
    with pytest.raises(ValueError, match="checksum"):
        build(tmp_path)
    assert not (tmp_path/"intraday").exists()


def test_manifest_rebinding_path_escape_and_artifact_tamper_are_rejected(tmp_path):
    path = archive(tmp_path, [native()])
    ref = build(tmp_path)
    artifact = Path(ref["path"])
    artifact.write_bytes(artifact.read_bytes()+b" ")
    with pytest.raises(ValueError, match="hash"):
        HistoricalMarketInputs(tmp_path, artifact)
    manifest = json.loads((path/"manifest.json").read_text());manifest["normalized"]["path"] = "../../../../../../../../outside.parquet"
    (path/"manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest receipt"):
        build(tmp_path)


def test_no_artifact_has_explicit_missingness_and_no_filesystem_writes(tmp_path):
    joined = HistoricalMarketInputs(tmp_path).join([T], cutoff=T)
    assert not any(joined["rows"][0]) and joined["artifact"] is None
    assert list(tmp_path.iterdir()) == []


def test_native_parquet_index_metadata_does_not_remove_clock_column(tmp_path):
    path = archive(tmp_path, [native()])
    frame = pd.DataFrame([native()]).set_index("ts_recv")
    frame.to_parquet(path/"normalized.parquet")
    manifest = json.loads((path/"manifest.json").read_text())
    manifest["normalized"]["checksum_sha256"] = hashlib.sha256((path/"normalized.parquet").read_bytes()).hexdigest()
    (path/"manifest.json").write_text(json.dumps(manifest))
    receipt = json.loads((path/"receipt.json").read_text())
    receipt["manifest_checksum_sha256"] = hashlib.sha256((path/"manifest.json").read_bytes()).hexdigest()
    (path/"receipt.json").write_text(json.dumps(receipt))
    assert build(tmp_path)["rows"] == 1
