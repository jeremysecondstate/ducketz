import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml import stock_target_history as history
from datafetching import databento_cold_start as cold
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS


def test_target_history_uses_exact_native_stock_only_requests(tmp_path):
    manifest = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    assert len(manifest["requests"]) == 7
    assert {r["symbol_scope"][0] for r in manifest["requests"]} == set(STOCK_TRADER_SYMBOLS)
    assert {(r["dataset"], r["schema"], r["stype_in"]) for r in manifest["requests"]} == {
        ("XNAS.ITCH", "ohlcv-1m", "raw_symbol")}
    cold._validate_manifest_checksum(manifest)
    cold._validate_manifest_included_scope(manifest)
    for request in manifest["requests"]:
        cold._validate_execution_request_identity(tmp_path, request)
    full = cold.build_manifest(datastore_root=tmp_path, equities_symbols=STOCK_TRADER_SYMBOLS,
                               cme_dataset="GLBX.MDP3", cme_scopes=(), equities_dataset="XNAS.ITCH",
                               as_of=date(2026, 9, 5))
    expected = [r for r in full["requests"] if r["dataset"] == "XNAS.ITCH" and r["schema"] == "ohlcv-1m"]
    assert manifest["requests"] == expected


def _publish_cursor_fixture(root, request):
    directory = Path(request["storage_path"])
    directory.mkdir(parents=True)
    normalized = directory / "normalized.parquet"
    raw = directory / "provider.dbn.zst"
    pd.DataFrame({"ts_event": pd.to_datetime(["2026-09-04T11:00Z", "2026-09-04T23:59Z"]),
                  "open": [100.0, 101.0], "close": [100.5, 101.5]}).to_parquet(normalized, index=False)
    raw.write_bytes(b"fixture native provider bytes")
    manifest = {"schema_version": cold.PARTITION_VERSION, "request": request,
                "raw": {"path": raw.name, "size_bytes": raw.stat().st_size, "checksum_sha256": cold.file_checksum(raw)},
                "normalized": {"path": normalized.name, "size_bytes": normalized.stat().st_size,
                               "checksum_sha256": cold.file_checksum(normalized),
                               **cold._validate_generic_parquet(normalized, request)}}
    cold._write_json_atomic(directory / "manifest.json", manifest)
    cold._write_json_atomic(directory / "receipt.json", {
        "schema_version": cold.RECEIPT_VERSION, "request_id": request["request_id"],
        "manifest_checksum_sha256": cold.file_checksum(directory / "manifest.json"),
        "normalized_checksum_sha256": cold.file_checksum(normalized)})
    return cold._write_request_cursor(root, manifest_id="fixture", request=request, status="PUBLISHED")


def test_history_does_not_read_unrelated_legacy_cursors(tmp_path):
    path = cold.history_cursor_path(tmp_path, market=cold.MARKET_OPRA, dataset=cold.OPRA_DATASET,
                                    schema="ohlcv-1m", symbol="COST.OPT")
    path.parent.mkdir(parents=True)
    path.write_text("damaged unrelated options cursor")
    assert len(history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))["requests"]) == 7


@pytest.mark.parametrize("through", [date(2026, 9, 5), date(2026, 9, 9)])
@pytest.mark.parametrize("key,value", [
    ("completed_through", "2026-09-10"), ("end", "2026-09-10"), ("start", "2026-09-03"),
    ("request_id", "wrong-request"), ("status", "NO_DATA_VERIFIED"), ("fetch_mode", "overlap-fill"),
])
def test_cursor_must_match_its_exact_verified_archive_before_skip_or_overlap(tmp_path, through, key, value):
    manifest = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    request = next(r for r in manifest["requests"] if r["symbol_scope"] == ["COST"])
    cursor_path = _publish_cursor_fixture(tmp_path, request)
    cursor = json.loads(cursor_path.read_text())
    cursor[key] = value
    cursor_path.write_text(json.dumps(cursor))
    with pytest.raises(ValueError, match="cursor"):
        history.build_target_history_manifest(tmp_path, through=through)


def test_verified_cursor_skips_current_and_drives_the_existing_overlap_policy(tmp_path):
    manifest = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    request = next(r for r in manifest["requests"] if r["symbol_scope"] == ["COST"])
    _publish_cursor_fixture(tmp_path, request)
    current = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    assert len(current["requests"]) == 6
    assert all(r["symbol_scope"] != ["COST"] for r in current["requests"])
    overlap = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 9))
    selected = next(r for r in overlap["requests"] if r["symbol_scope"] == ["COST"])
    assert selected["fetch_mode"] == "overlap-fill"
    assert selected["previous_completed_through"] == "2026-09-05"
    assert selected["end"] == "2026-09-09"
    cold._validate_manifest_included_scope(overlap)
    cold._validate_execution_request_identity(tmp_path, selected)


@pytest.mark.parametrize("payload", ["provider.dbn.zst", "normalized.parquet"])
def test_current_cursor_requires_unchanged_raw_and_normalized_payloads(tmp_path, payload):
    manifest = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    request = next(r for r in manifest["requests"] if r["symbol_scope"] == ["COST"])
    _publish_cursor_fixture(tmp_path, request)
    path = Path(request["storage_path"]) / payload
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(cold.ColdStartError, match="checksum"):
        history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))


def test_current_verified_universe_makes_no_provider_calls(tmp_path):
    manifest = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    for request in manifest["requests"]:
        _publish_cursor_fixture(tmp_path, request)
    run = history.maintain_target_history(tmp_path, client=object(), through=date(2026, 9, 5), execute=True)
    assert json.loads((run / "receipt.json").read_text())["status"] == "CURRENT"
    assert not (run / "cost-preflight.json").exists()


def test_complete_receipt_requires_native_publication_not_only_success_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "discover_dataset_catalog", lambda *a, **kw: {
        "ohlcv-1m": {"start": "2020-01-01", "end": "2026-09-05"}})
    monkeypatch.setattr(history, "preflight_manifest", lambda *a, **kw: {"capacity_pass": True})
    monkeypatch.setattr(history, "execute_manifest", lambda *a, **kw: {
        "failed": 0, "no_data": 7, "downloaded": 0, "verified": 0})
    with pytest.raises(RuntimeError, match="verified completion"):
        history.maintain_target_history(tmp_path, through=date(2026, 9, 5), execute=True,
            client=SimpleNamespace(metadata=SimpleNamespace(get_cost=lambda **kw: 0.0)))
    assert not list((tmp_path / "ml/stock-target-history-runs").glob("*/receipt.json"))


@pytest.mark.parametrize("clock,through", [
    ("2026-09-08T07:00Z", "2026-09-05"),
    ("2026-09-08T23:59Z", "2026-09-05"),
    ("2026-09-09T00:00Z", "2026-09-09"),
])
def test_only_finished_stock_sessions_are_requested(clock, through):
    assert history.latest_completed_through(clock).isoformat() == through


def test_nonzero_cost_stops_before_download_or_capacity_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "discover_dataset_catalog", lambda *a, **kw: {
        "ohlcv-1m": {"start": "2020-01-01", "end": "2026-09-05"}})
    monkeypatch.setattr(history, "preflight_manifest", lambda *a, **kw: pytest.fail("paid request cannot advance"))
    monkeypatch.setattr(history, "execute_manifest", lambda *a, **kw: pytest.fail("paid request cannot download"))
    with pytest.raises(ValueError, match="zero-cost"):
        history.maintain_target_history(tmp_path, through=date(2026, 9, 5), execute=True,
            client=SimpleNamespace(metadata=SimpleNamespace(get_cost=lambda **kw: 0.01)))
    records = list((tmp_path / "ml/stock-target-history-runs").glob("*/cost-preflight.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["total_cost_usd"] == pytest.approx(0.07)


def test_scoped_native_progress_does_not_overwrite_date_coordinator(tmp_path):
    manifest = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    manifest["requests"] = []
    default_path = cold._progress_path(tmp_path, manifest)
    default_path.parent.mkdir(parents=True)
    default_path.write_text("unrelated coordinator evidence")
    selected_path = tmp_path / "ml/stock-target-history-runs/test/progress.json"
    selected_path.parent.mkdir(parents=True)
    result = cold.execute_manifest(object(), datastore_root=tmp_path, manifest=manifest,
        preflight={"capacity_pass": True, "manifest_id": manifest["manifest_id"], "estimates": []},
        progress_path=selected_path, reporter=None)
    assert result["failed"] == 0
    assert default_path.read_text() == "unrelated coordinator evidence"
    with pytest.raises(cold.ColdStartError, match="within its datastore"):
        cold.execute_manifest(object(), datastore_root=tmp_path, manifest=manifest,
            preflight={"capacity_pass": True, "manifest_id": manifest["manifest_id"]},
            progress_path=tmp_path.parent / "outside.json")
