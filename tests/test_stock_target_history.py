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
    assert len(manifest["requests"]) == len(STOCK_TRADER_SYMBOLS)
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


def _publish_cursor_fixture(root, request, *, observed=None, publish_cursor=True):
    directory = Path(request["storage_path"])
    directory.mkdir(parents=True)
    normalized = directory / "normalized.parquet"
    raw = directory / "provider.dbn.zst"
    observed = observed or ["2026-09-04T11:00Z", "2026-09-04T23:59Z"]
    pd.DataFrame({"ts_event": pd.to_datetime(observed), "open": 100.0, "high": 102.0,
                  "low": 99.0, "close": 100.5}).to_parquet(normalized, index=False)
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
    if publish_cursor:
        return cold._write_request_cursor(root, manifest_id="fixture", request=request, status="PUBLISHED")
    return directory


def test_history_does_not_read_unrelated_legacy_cursors(tmp_path):
    path = cold.history_cursor_path(tmp_path, market=cold.MARKET_OPRA, dataset=cold.OPRA_DATASET,
                                    schema="ohlcv-1m", symbol="COST.OPT")
    path.parent.mkdir(parents=True)
    path.write_text("damaged unrelated options cursor")
    assert len(history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))["requests"]) == len(STOCK_TRADER_SYMBOLS)


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
    assert len(current["requests"]) == len(STOCK_TRADER_SYMBOLS) - 1
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
    assert json.loads(records[0].read_text())["total_cost_usd"] == pytest.approx(0.01 * len(STOCK_TRADER_SYMBOLS))


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


def _feature_fixture(root, symbol, schema, *, start="2019-01-01", observed="2019-01-02T13:30Z"):
    end = "2026-09-05"
    identity = {"dataset": "XNAS.ITCH", "standard_plan_dataset": cold.PLAN_DATASET_US_EQUITIES,
        "schema": schema, "symbol_scope": [symbol], "stype_in": "raw_symbol", "start": start, "end": end,
        "storage_contract": "isolated-cold-start", "window": cold.schema_window(cold.PLAN_DATASET_US_EQUITIES, schema),
        "fetch_mode": "initial-baseline", "baseline_start": start, "previous_completed_through": None}
    request = {"request_id": cold._checksum(identity)[:24], **identity, "status": "PENDING",
        "storage_path": str(cold._entry_storage_path(root, dataset="XNAS.ITCH", market=cold.MARKET_US_EQUITIES,
            schema=schema, symbol=symbol, start=date.fromisoformat(start), end=date.fromisoformat(end),
            contract="isolated-cold-start"))}
    return _publish_cursor_fixture(root, request, observed=[observed], publish_cursor=False)


def _extension_fixture(root, monkeypatch):
    manifest = history.build_target_history_manifest(root, through=date(2026, 9, 5))
    request = next(item for item in manifest["requests"] if item["symbol_scope"] == ["COST"])
    cursor = _publish_cursor_fixture(root, request)
    _feature_fixture(root, "COST", "ohlcv-1d")
    _feature_fixture(root, "COST", "ohlcv-1h", observed="2020-02-03T15:00Z")
    monkeypatch.setattr(history, "STOCK_TRADER_SYMBOLS", ("COST",))
    monkeypatch.setattr(history, "discover_dataset_catalog", lambda *a, **kw: {
        "ohlcv-1m": {"start": "2018-01-01", "end": "2026-09-05"}})
    return cursor, request


def _extension_client(*, cost=0.0, size=100, records=1):
    return SimpleNamespace(metadata=SimpleNamespace(get_cost=lambda **kw: cost,
        get_billable_size=lambda **kw: size, get_record_count=lambda **kw: records))


def test_feature_prefix_uses_actual_observed_start_and_existing_minute_boundary(tmp_path, monkeypatch):
    cursor, minute = _extension_fixture(tmp_path, monkeypatch)
    original = cursor.read_bytes()
    plan = history.build_feature_history_extension_manifest(tmp_path, through=date(2026, 9, 5))
    request, = plan["requests"]
    assert request["start"] == "2019-01-02"  # Native request started Jan 1, before observations.
    assert request["end"] == minute["start"]
    assert request["fetch_mode"] == "feature-history-prefix"
    cold._validate_execution_request_identity(tmp_path, request)
    assert cursor.read_bytes() == original


@pytest.mark.parametrize("payload", ["provider.dbn.zst", "normalized.parquet"])
def test_feature_prefix_rejects_changed_feature_provenance(tmp_path, monkeypatch, payload):
    _extension_fixture(tmp_path, monkeypatch)
    path = next((tmp_path / "market-data/databento/us-equities/XNAS.ITCH/ohlcv-1d/COST").rglob(payload))
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(cold.ColdStartError, match="checksum"):
        history.build_feature_history_extension_manifest(tmp_path, through=date(2026, 9, 5))


def test_feature_prefix_respects_verified_ipo_floor(tmp_path, monkeypatch):
    from datafetching.history_scope import policy_digest
    _extension_fixture(tmp_path, monkeypatch)
    data = {"symbol": "COST", "history_floor": "2018-01-01", "listing_date": "2020-01-15"}
    path = tmp_path / "state/symbol-history-policy/COST.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**data, "sha256": policy_digest(data)}))
    request, = history.build_feature_history_extension_manifest(tmp_path, through=date(2026, 9, 5))["requests"]
    assert request["start"] == "2020-01-15"


def test_feature_prefix_preserves_cursor_and_reuses_verified_prefix_without_provider_calls(tmp_path, monkeypatch):
    cursor, _ = _extension_fixture(tmp_path, monkeypatch)
    original = cursor.read_bytes()
    downloaded = []
    def download(client, *, datastore_root, request, reporter):
        downloaded.append(request["request_id"])
        _publish_cursor_fixture(datastore_root, request, observed=[request["start"] + "T13:30Z"], publish_cursor=False)
    monkeypatch.setattr(history, "_download_generic_entry", download)
    run = tmp_path / "first-run"
    run.mkdir()
    report = history._extend_to_feature_history(tmp_path, client=_extension_client(), through=date(2026, 9, 5),
                                               run=run, execute=True, reporter=None)
    assert report["status"] == "VERIFIED" and len(report["completed_requests"]) == 1
    assert cursor.read_bytes() == original
    second = tmp_path / "second-run"
    second.mkdir()
    report = history._extend_to_feature_history(tmp_path, client=object(), through=date(2026, 9, 5),
                                               run=second, execute=True, reporter=None)
    assert report["requests"] == 0 and len(downloaded) == 1
    assert cursor.read_bytes() == original


@pytest.mark.parametrize("client,message", [
    (_extension_client(cost=0.01), "zero-cost"), (_extension_client(cost=float("nan")), "invalid.*cost"),
    (_extension_client(size=float("inf")), "invalid.*billable"),
    (_extension_client(records=-1), "invalid.*record"), (_extension_client(records=0), "no provider records"),
    (_extension_client(size=history.FEATURE_HISTORY_MAX_BILLABLE_BYTES + 1), "capacity"),
])
def test_feature_prefix_fails_before_download_and_retains_failure_receipt(tmp_path, monkeypatch, client, message):
    cursor, _ = _extension_fixture(tmp_path, monkeypatch)
    original = cursor.read_bytes()
    monkeypatch.setattr(history, "_download_generic_entry", lambda *a, **kw: pytest.fail("must not download"))
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ValueError, match=message):
        history._extend_to_feature_history(tmp_path, client=client, through=date(2026, 9, 5),
                                           run=run, execute=True, reporter=None)
    receipt = json.loads((run / "receipt.json").read_text())
    assert receipt["status"] == "FAILED" and receipt["failed_stage"] == "feature_history_extension"
    assert (run / "feature-history-original-cursors.json").is_file()
    assert cursor.read_bytes() == original


def test_feature_prefix_provider_gap_fails_explicitly_without_acquisition(tmp_path, monkeypatch):
    _extension_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(history, "discover_dataset_catalog", lambda *a, **kw: {
        "ohlcv-1m": {"start": "2020-01-01", "end": "2026-09-05"}})
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ValueError, match="Provider range"):
        history._extend_to_feature_history(tmp_path, client=object(), through=date(2026, 9, 5),
                                           run=run, execute=True, reporter=None)
    assert json.loads((run / "receipt.json").read_text())["status"] == "FAILED"


def test_feature_prefix_success_requires_verified_native_partition(tmp_path, monkeypatch):
    _extension_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(history, "_download_generic_entry", lambda *a, **kw: None)
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(cold.ColdStartError, match="metadata is unreadable"):
        history._extend_to_feature_history(tmp_path, client=_extension_client(), through=date(2026, 9, 5),
                                           run=run, execute=True, reporter=None)
    assert json.loads((run / "receipt.json").read_text())["status"] == "FAILED"


def test_opt_in_current_receipt_binds_verified_extension(tmp_path, monkeypatch):
    manifest = history.build_target_history_manifest(tmp_path, through=date(2026, 9, 5))
    for request in manifest["requests"]:
        _publish_cursor_fixture(tmp_path, request)
        symbol = request["symbol_scope"][0]
        for schema in ("ohlcv-1d", "ohlcv-1h"):
            _feature_fixture(tmp_path, symbol, schema, start=request["start"], observed=request["start"] + "T13:30Z")
    run = history.maintain_target_history(tmp_path, client=object(), through=date(2026, 9, 5),
                                         execute=True, extend_to_feature_history=True)
    receipt = json.loads((run / "receipt.json").read_text())
    assert receipt["status"] == "CURRENT"
    assert receipt["feature_history_extension"]["status"] == "VERIFIED"
    assert receipt["feature_history_extension"]["sha256"] == cold.file_checksum(run / "feature-history-extension.json")
